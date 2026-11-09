"""Bounded remote status/fee lookup and best-effort cancellation, never task replay."""

import asyncio
import time

from sqlalchemy import and_, or_, select, update

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import remote_agent_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal
from agent_platform.infrastructure.remote_call_tables import calls
from agent_platform.modules.billing.service import reservations_table
from agent_platform.modules.run_records import now
from agent_platform.modules.runtime.invocation import sync_call_receipt
from agent_platform.modules.runtime.journal import EvidenceCodec

from .contracts import RemoteDescriptor, RemoteStatus
from .registry import RemoteRegistry
from .transport import RemoteTransport

REMOTE_TERMINAL = ("succeeded", "failed", "cancelled")
MAX_RECONCILE_POLLS = 12


class RemoteReconciler:
    def __init__(self, worker, *, transport=None):
        self.worker, self.db = worker, worker.db
        self.registry = RemoteRegistry(worker.platform)
        self.transport = (
            transport
            or getattr(worker, "remote_transport", None)
            or RemoteTransport(worker.settings.remote_agent_allowed_origins)
        )
        self.cursor = ""

    def claim(self):
        tenants = self.worker._tenant_page(self.cursor, active=False, limit=32)
        if not tenants:
            self.cursor = ""
            return None
        for tenant in tenants:
            self.cursor = tenant
            with (
                self.db.tenant_scope(tenant),
                self.db.transaction(f"tenant:{tenant}") as conn,
            ):
                conn.execute(
                    update(calls)
                    .where(
                        calls.c.tenant_id == tenant,
                        calls.c.reconciliation == "pending",
                        calls.c.reconcile_count >= MAX_RECONCILE_POLLS,
                        or_(
                            calls.c.lease_until.is_(None),
                            calls.c.lease_until <= time.time(),
                        ),
                    )
                    .values(reconciliation="exhausted", owner=None, lease_until=None)
                )
                row = (
                    conn.execute(
                        select(
                            calls,
                            t.runs.c.cancel_requested,
                            t.runs.c.status.label("root_status"),
                            ct.executions.c.status.label("execution_status"),
                        )
                        .join(
                            t.runs,
                            and_(
                                t.runs.c.tenant_id == calls.c.tenant_id,
                                t.runs.c.id == calls.c.root_run_id,
                            ),
                        )
                        .join(
                            ct.executions,
                            and_(
                                ct.executions.c.tenant_id == calls.c.tenant_id,
                                ct.executions.c.id == calls.c.execution_id,
                            ),
                        )
                        .where(
                            calls.c.tenant_id == tenant,
                            calls.c.reconciliation == "pending",
                            calls.c.next_poll_at <= time.time(),
                            or_(
                                calls.c.lease_until.is_(None),
                                calls.c.lease_until <= time.time(),
                            ),
                            or_(
                                t.runs.c.cancel_requested.is_(True),
                                t.runs.c.status.in_(
                                    ("succeeded", "failed", "cancelled", "expired")
                                ),
                                ct.executions.c.status != "running",
                            ),
                        )
                        .order_by(calls.c.next_poll_at, calls.c.id)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                evidence = (
                    conn.execute(
                        select(journal).where(
                            journal.c.tenant_id == tenant,
                            journal.c.call_id == row["id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                abandoned = (
                    row["cancel_requested"]
                    or row["execution_status"] in {"failed", "cancelled"}
                    or row["root_status"] in {"failed", "cancelled", "expired"}
                )
                change = {
                    "owner": self.worker.owner,
                    "fence": row["fence"] + 1,
                    "lease_until": time.time() + 30,
                    "reconcile_count": row["reconcile_count"] + 1,
                    "cancel_due": bool(
                        abandoned
                        and evidence["sent_at"] is not None
                        and row["remote_state"] not in REMOTE_TERMINAL
                    ),
                    "updated_at": now(),
                }
                conn.execute(
                    update(calls).where(calls.c.id == row["id"]).values(**change)
                )
                return {**row, **change}
        return None

    def guard(self, conn, claim):
        row = (
            conn.execute(
                select(calls).where(
                    calls.c.tenant_id == claim["tenant_id"], calls.c.id == claim["id"]
                )
            )
            .mappings()
            .one()
        )
        if (
            row["owner"] != claim["owner"]
            or row["fence"] != claim["fence"]
            or (row["lease_until"] or 0) <= time.time()
        ):
            raise PlatformError(
                409, "remote_reconcile_lease_lost", "远程费用核实租约已失效。"
            )
        return row

    def access(self, claim):
        with (
            self.db.tenant_scope(claim["tenant_id"]),
            self.db.transaction(f"tenant:{claim['tenant_id']}") as conn,
        ):
            self.guard(conn, claim)
            record = (
                conn.execute(
                    select(journal).where(
                        journal.c.tenant_id == claim["tenant_id"],
                        journal.c.call_id == claim["id"],
                    )
                )
                .mappings()
                .one()
            )
            intent = EvidenceCodec(self.worker.settings.secret_encryption_key).decode(
                record["request_ciphertext"], record["request_digest"]
            )
            if (
                record["kind"] != "remote"
                or record["execution_id"] != claim["execution_id"]
                or (intent["endpoint_id"], intent["registration_id"])
                != (claim["endpoint_id"], claim["registration_id"])
            ):
                raise PlatformError(
                    409, "remote_evidence_mismatch", "远程请求证据不匹配。"
                )
            if record["sent_at"] is None and record["state"] in {
                "prepared",
                "rejected",
            }:
                return {"unsent": True}
            version = self.registry.owned(
                conn, rt.versions, claim["tenant_id"], claim["registration_id"]
            )
            descriptor = RemoteDescriptor.model_validate(version["descriptor"])
            if descriptor.digest != intent["digest"]:
                raise PlatformError(
                    409, "remote_evidence_mismatch", "远程描述证据不匹配。"
                )
            endpoint = self.registry.owned(
                conn, rt.endpoints, claim["tenant_id"], claim["endpoint_id"]
            )
            if endpoint["registration_id"] != claim["registration_id"]:
                raise PlatformError(
                    409, "remote_evidence_mismatch", "远程端点证据不匹配。"
                )
            token = self.registry.credential(
                conn, claim["tenant_id"], endpoint["credential_id"]
            )
            return {
                "unsent": False,
                "url": endpoint["url"],
                "token": token,
                "cancellation": descriptor.cancellation,
            }

    def authorize_cancel(self, claim):
        with (
            self.db.tenant_scope(claim["tenant_id"]),
            self.db.transaction(f"tenant:{claim['tenant_id']}") as conn,
        ):
            row = self.guard(conn, claim)
            if not row["cancel_due"] or row["cancel_sent"]:
                raise PlatformError(
                    409, "remote_cancel_already_sent", "远程取消已发送或不再需要。"
                )
            root = (
                conn.execute(
                    select(t.runs).where(
                        t.runs.c.tenant_id == claim["tenant_id"],
                        t.runs.c.id == claim["root_run_id"],
                    )
                )
                .mappings()
                .one()
            )
            execution = (
                conn.execute(
                    select(ct.executions).where(
                        ct.executions.c.tenant_id == claim["tenant_id"],
                        ct.executions.c.id == claim["execution_id"],
                    )
                )
                .mappings()
                .one()
            )
            if (
                not root["cancel_requested"]
                and root["status"]
                not in {"succeeded", "failed", "cancelled", "expired"}
                and execution["status"] not in {"failed", "cancelled"}
            ):
                raise PlatformError(
                    409, "remote_cancel_not_authorized", "运行仍持有执行权。"
                )
            endpoint = self.registry.owned(
                conn, rt.endpoints, claim["tenant_id"], claim["endpoint_id"]
            )
            self.registry.credential(
                conn, claim["tenant_id"], endpoint["credential_id"]
            )
            conn.execute(
                update(calls)
                .where(calls.c.id == claim["id"])
                .values(cancel_sent=True, updated_at=now())
            )

    def apply(self, claim, payload):
        status = RemoteStatus.model_validate(payload)
        if status.request_id != claim["id"]:
            raise PlatformError(
                409, "remote_response_mismatch", "远程响应不属于当前请求。"
            )
        if status.fee:
            receipt = self.worker.platform.billing.settle_fixed(
                claim["tenant_id"], claim["id"], status.fee.model_dump()
            )
            sync_call_receipt(self.worker.platform, receipt)
        # No task/artifact mutation here: financial reconciliation cannot revive
        # an execution that lost its lease, even when a late output is complete.
        return status.state

    def finish(self, claim, state):
        with (
            self.db.tenant_scope(claim["tenant_id"]),
            self.db.transaction(f"tenant:{claim['tenant_id']}") as conn,
        ):
            row = self.guard(conn, claim)
            financial = conn.scalar(
                select(reservations_table.c.status).where(
                    reservations_table.c.tenant_id == claim["tenant_id"],
                    reservations_table.c.call_id == claim["id"],
                )
            )
            done = financial in {"settled", "released", "written_off"} and state in (
                *REMOTE_TERMINAL,
                "not_sent",
            )
            conn.execute(
                update(calls)
                .where(calls.c.id == claim["id"])
                .values(
                    remote_state=state,
                    owner=None,
                    lease_until=None,
                    reconciliation="done"
                    if done
                    else "exhausted"
                    if row["reconcile_count"] >= MAX_RECONCILE_POLLS
                    else "pending",
                    next_poll_at=time.time() + 30,
                    updated_at=now(),
                )
            )

    async def execute(self, claim):
        state = claim["remote_state"]
        with self.db.tenant_scope(claim["tenant_id"]):
            try:
                access = await asyncio.to_thread(self.access, claim)
                if access["unsent"]:
                    receipt = await asyncio.to_thread(
                        self.worker.platform.billing.release,
                        claim["tenant_id"],
                        claim["id"],
                        "持久日志证明远程任务未发送。",
                    )
                    await asyncio.to_thread(
                        sync_call_receipt, self.worker.platform, receipt
                    )
                    state = "not_sent"
                else:
                    if (
                        claim["cancel_due"]
                        and not claim["cancel_sent"]
                        and access["cancellation"]
                    ):
                        try:
                            payload = await self.transport.request(
                                access["url"],
                                f"/tasks/{claim['id']}/cancel",
                                method="POST",
                                token=access["token"],
                                body={"request_id": claim["id"]},
                                timeout=10,
                                authorize_send=lambda: self.authorize_cancel(claim),
                            )
                            state = await asyncio.to_thread(self.apply, claim, payload)
                        except PlatformError:
                            # Mark-before-send prevents retries after uncertain cancellation.
                            pass
                    payload = await self.transport.request(
                        access["url"],
                        f"/tasks/{claim['id']}",
                        token=access["token"],
                        timeout=10,
                        authorize_send=lambda: self.access(claim),
                    )
                    observed = await asyncio.to_thread(self.apply, claim, payload)
                    if state not in REMOTE_TERMINAL:
                        state = observed
            except (PlatformError, ValueError, TypeError, KeyError, TimeoutError):
                # No raw endpoint errors or secrets in public projections. Holds
                # survive bounded lookup exhaustion for explicit manual review.
                pass
            finally:
                try:
                    await asyncio.to_thread(self.finish, claim, state)
                except PlatformError:
                    pass  # Another owner now has the lease; do not overwrite it.
