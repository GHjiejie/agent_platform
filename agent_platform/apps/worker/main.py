"""Leased agent execution, metered model calls and conservative recovery."""

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, insert, or_, select, update

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.events import configure_notifications
from agent_platform.modules.billing.service import reservations_table
from agent_platform.modules.execution_policy import (
    execution_timeout,
)
from agent_platform.modules.run_records import append_event, now, uid
from agent_platform.modules.runtime.context import ExecutionContext
from agent_platform.modules.runtime.engine import RunCancelled, execute_agent
from agent_platform.modules.runtime.invocation import (
    InvocationService,
    sync_call_receipt,
)

if TYPE_CHECKING:
    from agent_platform.modules.platform import Platform
    from agent_platform.modules.service_contexts import WorkerServices

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, platform: "Platform | WorkerServices", *, gateway=None):
        self.platform = platform
        self.db = platform.db
        self.settings = platform.settings
        configure_notifications(self.db, self.settings.redis_url)
        # An explicitly injected adapter is only for offline execution fixtures.
        # Production resolves fresh tenant credentials for every external call.
        self.gateway = gateway
        self.owner = uid()
        self._claim_cursor = ""
        self._recover_cursor = ""
        self.max_run_cost = Decimal(self.settings.max_run_cost)
        self.invocation = InvocationService(self)
        if not self.max_run_cost.is_finite() or self.max_run_cost <= 0:
            raise ValueError("PLATFORM_MAX_RUN_COST must be a finite positive amount")

    def claim(self) -> dict | None:
        for tenant_id in self._tenant_page(self._claim_cursor, active=True, limit=128):
            self._claim_cursor = tenant_id
            with (
                self.db.tenant_scope(tenant_id),
                self.db.transaction(f"tenant:{tenant_id}") as connection,
            ):
                status = connection.scalar(
                    select(nt.tenant_settings.c.status).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                ceiling = connection.scalar(
                    select(nt.entitlements.c.max_concurrent_runs).where(
                        nt.entitlements.c.tenant_id == tenant_id
                    )
                )
                if status != "active" or ceiling is None or ceiling <= 0:
                    continue
                checked_at = time.time()
                expired, eligible = self._queue_conditions(checked_at)
                # Expiry is also enforced during admission to execution. The
                # background sweep is repair, never the authority to run a job.
                for queued in connection.execute(
                    select(t.runs)
                    .where(
                        t.runs.c.tenant_id == tenant_id,
                        t.runs.c.status == "queued",
                        expired,
                    )
                    .order_by(t.runs.c.created_at, t.runs.c.id)
                    .limit(64)
                ).mappings():
                    self._close_due_run(
                        connection, queued, "expired", "排队超时，未调用模型。"
                    )
                active = connection.scalar(
                    select(func.count())
                    .select_from(t.runs)
                    .where(
                        t.runs.c.tenant_id == tenant_id,
                        t.runs.c.status.in_(("running", "cancelling")),
                    )
                )
                if active >= ceiling:
                    continue
                row = (
                    connection.execute(
                        select(t.runs)
                        .where(
                            t.runs.c.tenant_id == tenant_id,
                            t.runs.c.status == "queued",
                            eligible,
                        )
                        .order_by(t.runs.c.created_at, t.runs.c.id)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    continue
                queue_expires_at = row["queue_expires_at"]
                if queue_expires_at is None:
                    # Compatibility for a legacy writer. Migrations backfill
                    # existing rows; freeze this fallback before any execution.
                    queue_expires_at = (
                        datetime.fromisoformat(row["created_at"]).timestamp()
                        + self.settings.queue_timeout
                    )
                started_at = time.time()
                if queue_expires_at <= started_at:
                    self._close_due_run(
                        connection, row, "expired", "排队超时，未调用模型。"
                    )
                    continue
                try:
                    self._authorize_identity(connection, dict(row))
                except PlatformError:
                    connection.execute(
                        update(t.runs)
                        .where(
                            t.runs.c.id == row["id"], t.runs.c.tenant_id == tenant_id
                        )
                        .values(
                            status="cancelled", cancel_requested=True, finished_at=now()
                        )
                    )
                    append_event(
                        connection,
                        row["id"],
                        "run.cancelled",
                        {"status": "cancelled", "error": "运行授权已失效。"},
                    )
                    continue
                frozen_timeout = (
                    row["spec"]
                    .get("execution_limits", {})
                    .get("timeout_seconds", self.settings.run_timeout)
                )
                run_timeout = execution_timeout(
                    frozen_timeout, self.settings.run_timeout
                )
                deadline = started_at + run_timeout if run_timeout is not None else None
                changes = {
                    "status": "running",
                    "owner": self.owner,
                    "fence": row["fence"] + 1,
                    "queue_expires_at": queue_expires_at,
                    "lease_until": min(started_at + 20, deadline)
                    if deadline is not None
                    else started_at + 20,
                    "deadline": deadline,
                }
                result = connection.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.id == row["id"],
                        t.runs.c.tenant_id == tenant_id,
                        t.runs.c.status == "queued",
                    )
                    .values(**changes)
                )
                if result.rowcount:
                    return {**dict(row), **changes}
        return None

    def _queue_conditions(self, checked_at: float):
        cutoff = datetime.fromtimestamp(
            checked_at - self.settings.queue_timeout, UTC
        ).isoformat()
        legacy = t.runs.c.queue_expires_at.is_(None)
        return (
            or_(
                t.runs.c.queue_expires_at <= checked_at,
                and_(legacy, t.runs.c.created_at <= cutoff),
            ),
            or_(
                t.runs.c.queue_expires_at > checked_at,
                and_(legacy, t.runs.c.created_at > cutoff),
            ),
        )

    @staticmethod
    def _close_due_run(connection, run, status: str, message: str):
        from agent_platform.modules.collaboration.scheduler import terminalize_children

        terminalize_children(connection, run, status, message)
        connection.execute(
            update(t.runs)
            .where(t.runs.c.tenant_id == run["tenant_id"], t.runs.c.id == run["id"])
            .values(
                status=status,
                fence=run["fence"] + 1,
                error=message,
                lease_until=None,
                finished_at=now(),
            )
        )
        append_event(
            connection,
            run["id"],
            f"run.{status}",
            {"status": status, "error": message},
        )

    def _tenant_page(self, cursor: str, *, active=False, limit=32) -> list[str]:
        """Global identity metadata only; tenant content is never scanned unscoped."""
        base = select(nt.tenant_settings.c.tenant_id)
        if active:
            base = base.where(nt.tenant_settings.c.status == "active")
        with self.db.read() as connection:
            identifiers = list(
                connection.scalars(
                    base.where(nt.tenant_settings.c.tenant_id > cursor)
                    .order_by(nt.tenant_settings.c.tenant_id)
                    .limit(limit)
                )
            )
            if len(identifiers) < limit and cursor:
                identifiers.extend(
                    connection.scalars(
                        base.where(nt.tenant_settings.c.tenant_id <= cursor)
                        .order_by(nt.tenant_settings.c.tenant_id)
                        .limit(limit - len(identifiers))
                    )
                )
        return identifiers

    def _authorize_identity(self, connection, run: dict) -> dict:
        identity = {"id": run["user_id"], "tenant_id": run["tenant_id"]}
        if run["spec"].get("membership_id"):
            identity["membership_id"] = run["spec"]["membership_id"]
        return self.platform.identity.assert_current(
            connection, identity, "runs.execute"
        )

    def _assert_lease(self, connection, run: dict, *, check_deadline=True):
        current = (
            connection.execute(
                select(t.runs).where(
                    t.runs.c.id == run["id"], t.runs.c.tenant_id == run["tenant_id"]
                )
            )
            .mappings()
            .one()
        )
        checked_at = time.time()
        if (
            current["owner"] != self.owner
            or current["fence"] != run["fence"]
            or current["status"] not in ("running", "cancelling")
            or current["lease_until"] is None
        ):
            raise PlatformError(409, "lease_lost", "运行执行权已失效。")
        deadline_elapsed = (
            current["deadline"] is not None and current["deadline"] <= checked_at
        )
        if check_deadline and deadline_elapsed:
            raise TimeoutError("运行执行期限已到。")
        if current["lease_until"] <= checked_at and not (
            deadline_elapsed and not check_deadline
        ):
            raise PlatformError(409, "lease_lost", "运行执行权已失效。")
        return current

    def event(self, run: dict, kind: str, data: dict) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            self._assert_lease(connection, run)
            append_event(connection, run["id"], kind, data)

    def is_cancelled(self, run: dict) -> bool:
        with self.db.tenant_scope(run["tenant_id"]), self.db.read() as connection:
            current = (
                connection.execute(
                    select(t.runs).where(
                        t.runs.c.id == run["id"], t.runs.c.tenant_id == run["tenant_id"]
                    )
                )
                .mappings()
                .one()
            )
            try:
                if run.get("root_spec", run["spec"]).get("execution_mode") == "team":
                    self.invocation._authorize_identity(connection, run)
                else:
                    self._authorize_identity(connection, run)
                active = True
            except PlatformError:
                active = False
        if (
            current["owner"] == self.owner
            and current["fence"] == run["fence"]
            and current["status"] in ("running", "cancelling")
            and current["deadline"] is not None
            and current["deadline"] <= time.time()
        ):
            raise TimeoutError("运行执行期限已到。")
        return bool(
            not active
            or current["cancel_requested"]
            or current["owner"] != self.owner
            or current["fence"] != run["fence"]
            or current["status"] not in ("running", "cancelling")
            or current["lease_until"] is None
            or current["lease_until"] <= time.time()
        )

    def renew(self, run: dict) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run)
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(
                    lease_until=min(time.time() + 20, current["deadline"])
                    if current["deadline"] is not None
                    else time.time() + 20
                )
            )

    async def heartbeat(self, run: dict) -> None:
        while True:
            await asyncio.sleep(3)
            await asyncio.to_thread(self.renew, run)

    async def invoke(self, run, messages, tools):
        return await self.invocation.invoke(
            ExecutionContext.from_run(run), messages, tools
        )

    def finish(
        self,
        run: dict,
        status: str,
        result: str = "",
        error: str = "",
        *,
        outcome="complete",
    ) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run, check_deadline=False)
            if current["cancel_requested"] and status == "succeeded":
                status = "cancelled"
            if (
                status == "succeeded"
                and current["deadline"] is not None
                and current["deadline"] <= time.time()
            ):
                status, error = "expired", "运行超时，已产生的用量仍会结算。"
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(
                    status=status,
                    error=error,
                    finished_at=now(),
                    lease_until=None,
                    outcome=outcome if status == "succeeded" else None,
                )
            )
            from agent_platform.modules.collaboration.scheduler import (
                terminalize_children,
            )

            terminalize_children(connection, run, status, error)
            if status == "succeeded":
                connection.execute(
                    insert(t.messages).values(
                        id=uid(),
                        tenant_id=run["tenant_id"],
                        created_at=now(),
                        session_id=run["session_id"],
                        run_id=run["id"],
                        role="assistant",
                        content=result,
                    )
                )
            kind = "run.completed" if status == "succeeded" else f"run.{status}"
            append_event(
                connection,
                run["id"],
                kind,
                {
                    "status": status,
                    "outcome": outcome if status == "succeeded" else None,
                    "content": result if status == "succeeded" else "",
                    "error": error,
                },
            )

    async def execute(self, run: dict) -> None:
        with self.db.tenant_scope(run["tenant_id"]):
            await self._execute(run)

    async def _execute(self, run: dict) -> None:
        from agent_platform.modules.runtime.generative_ui import previous_ui_context

        pulse = asyncio.create_task(self.heartbeat(run))
        try:
            with self.db.read() as connection:
                messages = [
                    {"role": row["role"], "content": row["content"]}
                    for row in connection.execute(
                        select(t.messages)
                        .where(
                            t.messages.c.session_id == run["session_id"],
                            t.messages.c.tenant_id == run["tenant_id"],
                        )
                        .order_by(t.messages.c.created_at)
                    ).mappings()
                ]
                if context := previous_ui_context(connection, run):
                    messages.insert(max(0, len(messages) - 1), context)
            await asyncio.to_thread(
                self.event, run, "run.started", {"status": "running"}
            )

            async def invoke(messages, tools):
                return await self.invoke(run, messages, tools)

            async def emit(kind, data):
                await asyncio.to_thread(self.event, run, kind, data)

            async def cancelled():
                return await asyncio.to_thread(self.is_cancelled, run)

            # Claim freezes the execution budget. A delayed executor or a
            # settings change must never grant this run another full timeout.
            async with asyncio.timeout(
                max(0, run["deadline"] - time.time())
                if run["deadline"] is not None
                else None
            ):
                outcome = "complete"
                if run["spec"].get("execution_mode") == "team":
                    from agent_platform.modules.collaboration.scheduler import (
                        CollaborationScheduler,
                    )

                    result, outcome = await CollaborationScheduler(self, run).run_team(
                        messages
                    )
                else:
                    result = await execute_agent(
                        run["spec"], messages, invoke, emit, cancelled
                    )
            await asyncio.to_thread(
                self.finish, run, "succeeded", result, outcome=outcome
            )
        except RunCancelled as exc:
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish, run, "cancelled", error=exc.message
                )
        except TimeoutError:
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "expired",
                    error="运行超时，已产生的用量仍会结算。",
                )
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "failed",
                    error="执行进程停止，未确认费用等待对账；未自动重放模型请求。",
                )
            raise
        except Exception as exc:  # noqa: BLE001 - isolate failed jobs without leaking provider errors
            logger.warning(
                "run failed run_id=%s error=%s", run["id"], type(exc).__name__
            )
            with suppress(PlatformError):
                await asyncio.to_thread(
                    self.finish,
                    run,
                    "failed",
                    error=getattr(
                        exc, "message", "运行失败，请检查模型配置或联系管理员。"
                    ),
                )
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pulse

    def recover(self) -> None:
        """Bounded tenant batches repair evidence without replaying requests."""
        for tenant_id in self._tenant_page(self._recover_cursor, limit=32):
            self._recover_cursor = tenant_id
            with self.db.tenant_scope(tenant_id):
                self._recover_tenant(tenant_id)

    def _recover_tenant(self, tenant_id: str) -> None:
        from agent_platform.modules.metering import apply_ready_evidence

        for _ in range(16):
            if not apply_ready_evidence(
                self.db, tenant_id, self.owner, redis_url=self.settings.redis_url
            ):
                break
        terminal = ("failed", "cancelled", "expired", "succeeded")
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            checked_at = time.time()
            queue_expired, _ = self._queue_conditions(checked_at)
            due = list(
                connection.execute(
                    select(t.runs)
                    .where(
                        t.runs.c.tenant_id == tenant_id,
                        or_(
                            and_(
                                t.runs.c.status == "queued",
                                queue_expired,
                            ),
                            and_(
                                t.runs.c.status.in_(("running", "cancelling")),
                                or_(
                                    t.runs.c.deadline <= checked_at,
                                    t.runs.c.lease_until <= checked_at,
                                    t.runs.c.lease_until.is_(None),
                                ),
                            ),
                        ),
                    )
                    .order_by(t.runs.c.created_at, t.runs.c.id)
                    .limit(64)
                ).mappings()
            )
            for run in due:
                queued = run["status"] == "queued"
                timed_out = run["deadline"] is not None and (
                    run["deadline"] <= checked_at
                )
                state = "expired" if queued or timed_out else "failed"
                message = (
                    "排队超时，未调用模型。"
                    if queued
                    else "运行超时，已产生的用量仍会结算。"
                    if timed_out
                    else "执行进程失联，未自动重放。"
                )
                self._close_due_run(connection, run, state, message)

        mismatches = [
            and_(reservations_table.c.status == stored, t.calls.c.status != projected)
            for stored, projected in (
                ("settled", "confirmed"),
                ("released", "released"),
                ("written_off", "written_off"),
                ("unresolved", "unresolved"),
                ("reserved", "running"),
            )
        ]
        with self.db.read() as connection:
            receipts = list(
                connection.execute(
                    select(reservations_table, t.runs.c.status.label("run_status"))
                    .join(
                        t.runs,
                        and_(
                            t.runs.c.id == reservations_table.c.run_id,
                            t.runs.c.tenant_id == reservations_table.c.tenant_id,
                        ),
                    )
                    .outerjoin(
                        t.calls,
                        and_(
                            t.calls.c.id == reservations_table.c.call_id,
                            t.calls.c.tenant_id == reservations_table.c.tenant_id,
                        ),
                    )
                    .where(
                        reservations_table.c.tenant_id == tenant_id,
                        or_(
                            and_(
                                reservations_table.c.status == "reserved",
                                t.runs.c.status.in_(terminal),
                            ),
                            *mismatches,
                        ),
                    )
                    .order_by(reservations_table.c.updated_at, reservations_table.c.id)
                    .limit(64)
                ).mappings()
            )
        for row in receipts:
            if row["status"] == "reserved" and row["run_status"] in terminal:
                receipt = self.platform.billing.unresolved(
                    tenant_id, row["call_id"], "执行结束但缺少结算证据。"
                )
            else:
                from agent_platform.modules.billing.service import _serialize

                receipt = _serialize(dict(row))
            sync_call_receipt(self.platform, receipt)
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            orphan_ids = list(
                connection.scalars(
                    select(t.calls.c.id)
                    .join(
                        t.runs,
                        and_(
                            t.runs.c.id == t.calls.c.run_id,
                            t.runs.c.tenant_id == t.calls.c.tenant_id,
                        ),
                    )
                    .outerjoin(
                        reservations_table,
                        and_(
                            reservations_table.c.call_id == t.calls.c.id,
                            reservations_table.c.tenant_id == t.calls.c.tenant_id,
                        ),
                    )
                    .where(
                        t.calls.c.tenant_id == tenant_id,
                        t.calls.c.status == "admitting",
                        t.runs.c.status.in_(terminal),
                        reservations_table.c.id.is_(None),
                    )
                    .order_by(t.calls.c.created_at, t.calls.c.id)
                    .limit(64)
                )
            )
            if orphan_ids:
                connection.execute(
                    update(t.calls)
                    .where(
                        t.calls.c.tenant_id == tenant_id,
                        t.calls.c.id.in_(orphan_ids),
                        t.calls.c.status == "admitting",
                    )
                    .values(
                        status="rejected",
                        error="准入完成前运行已终止，未产生费用。",
                        finished_at=now(),
                    )
                )

    async def serve(self, stop: asyncio.Event) -> None:
        tasks: set[asyncio.Task] = set()
        recovered_at = 0.0
        try:
            while not stop.is_set():
                try:
                    if time.monotonic() - recovered_at > 10:
                        await asyncio.to_thread(self.recover)
                        recovered_at = time.monotonic()
                    while (
                        not stop.is_set()
                        and len(tasks) < self.settings.worker_concurrency
                    ):
                        run = await asyncio.to_thread(self.claim)
                        if not run:
                            break
                        task = asyncio.create_task(self.execute(run))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                except Exception as exc:  # noqa: BLE001 - background supervisor retries storage availability
                    logger.error("worker cycle failed: %s", type(exc).__name__)
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.settings.worker_poll_seconds
                    )
                except TimeoutError:
                    pass
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        else:
            # Ordinary SIGTERM stops claims and preserves model usage evidence by
            # allowing existing runs to finish within the Pod's grace period.
            await asyncio.gather(*tasks, return_exceptions=True)
