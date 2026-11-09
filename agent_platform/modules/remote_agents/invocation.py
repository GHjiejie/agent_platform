"""One remote task per execution, using frozen inputs and a non-replaying journal."""

import asyncio
import time
from contextlib import suppress
from datetime import datetime
from decimal import Decimal

from sqlalchemy import insert, select, update

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal as journal_table
from agent_platform.infrastructure.remote_call_tables import calls
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.run_records import now
from agent_platform.modules.runtime.contracts import canonical_digest, validate_value
from agent_platform.modules.runtime.engine import RunCancelled
from agent_platform.modules.runtime.invocation import sync_call_receipt
from agent_platform.modules.runtime.journal import DurableJournal

from .contracts import RemoteDescriptor, RemoteRequest, RemoteStatus
from .registry import RemoteRegistry
from .transport import RemoteTransport

MAX_EXECUTION_POLLS = 20


class RemoteInvocation:
    def __init__(self, worker, context, *, transport=None):
        self.worker, self.context = worker, context
        self.platform, self.db = worker.platform, worker.db
        self.run = context.invocation_run()
        self.snapshot = context.spec["remote_snapshot"]
        self.descriptor = RemoteDescriptor.model_validate(self.snapshot["descriptor"])
        self.registry = RemoteRegistry(worker.platform)
        self.journal = DurableJournal(worker, context)
        self.transport = (
            transport
            or getattr(worker, "remote_transport", None)
            or RemoteTransport(worker.settings.remote_agent_allowed_origins)
        )

    def prepare(self, task, inputs):
        # The persisted execution input is already checked by TaskContract. Strip
        # internal artifact IDs, run IDs and the root's private conversation.
        values = [
            {"task_key": item.get("task_key"), "value": item["value"]}
            for item in inputs["inputs"]
        ]
        with self.journal.transaction() as conn:
            previous = (
                conn.execute(
                    self.journal.query().where(
                        journal_table.c.operation_key == "remote/task"
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                self.journal._owned(previous)
                intent = self.journal.codec.decode(
                    previous["request_ciphertext"], previous["request_digest"]
                )
                if (
                    intent["inputs"],
                    intent["objective"],
                    intent["task_type"],
                    intent["digest"],
                ) != (
                    values,
                    task["objective"],
                    task["task_type"],
                    self.snapshot["digest"],
                ):
                    raise PlatformError(
                        409, "remote_intent_conflict", "远程任务意图与冻结输入不匹配。"
                    )
                return dict(previous), intent
            _, points = self.registry.eligible(
                conn,
                self.context.tenant_id,
                self.context.spec["remote_ref"],
                endpoint_ids=self.snapshot["endpoint_ids"],
            )
            created = conn.scalar(
                select(ct.executions.c.created_at).where(
                    ct.executions.c.id == self.context.execution_id
                )
            )
            deadline = datetime.fromisoformat(created).timestamp() + min(
                self.descriptor.timeout_seconds,
                self.context.spec["execution_limits"]["timeout_seconds"],
            )
            if self.context.deadline is not None:
                deadline = min(deadline, self.context.deadline)
            intent = {
                "registration_id": self.snapshot["registration_id"],
                "endpoint_id": points[0]["id"],
                "digest": self.snapshot["digest"],
                "task_type": task["task_type"],
                "objective": task["objective"],
                "inputs": values,
                "deadline_ms": int(deadline * 1000),
            }
        record = self.journal.prepare("remote/task", "remote", intent)
        return record, intent

    def _point(self, conn, intent, *, sending):
        from agent_platform.infrastructure import remote_agent_tables as rt

        if sending:
            version, points = self.registry.eligible(
                conn,
                self.context.tenant_id,
                self.context.spec["remote_ref"],
                endpoint_ids=[intent["endpoint_id"]],
            )
            if version["digest"] != intent["digest"]:
                raise PlatformError(
                    409, "remote_version_changed", "远程描述与冻结版本不匹配。"
                )
            point = points[0]
        else:
            # Health expiration does not authorize failover and must not prevent
            # bounded read-only reconciliation of a request already sent.
            point = self.registry.owned(
                conn, rt.endpoints, self.context.tenant_id, intent["endpoint_id"]
            )
        if point["registration_id"] != intent["registration_id"]:
            raise PlatformError(
                409, "remote_endpoint_changed", "远程请求端点绑定不匹配。"
            )
        token = self.registry.credential(
            conn, self.context.tenant_id, point["credential_id"]
        )
        return point["url"], token

    def _admission(self, conn, record, intent):
        self.journal.guard(conn)
        current = self.journal._record(conn, record["id"])
        if current["state"] != "prepared":
            raise PlatformError(
                409, "remote_not_dispatchable", "远程请求已发送或终止，不能新增预占。"
            )
        self._point(conn, intent, sending=True)
        previous = (
            conn.execute(
                select(calls).where(
                    calls.c.id == record["call_id"],
                    calls.c.tenant_id == self.context.tenant_id,
                )
            )
            .mappings()
            .first()
        )
        if previous:
            if (
                previous["execution_id"],
                previous["endpoint_id"],
                previous["registration_id"],
            ) != (
                self.context.execution_id,
                intent["endpoint_id"],
                intent["registration_id"],
            ):
                raise PlatformError(
                    409, "remote_call_conflict", "调用标识已绑定其他任务。"
                )
            return
        self.worker.invocation.assert_call_limit(conn, self.run)
        self.worker.invocation.authorize_budget(
            conn, self.run, 0, Decimal(self.descriptor.max_charge), 0
        )
        conn.execute(
            insert(calls).values(
                id=record["call_id"],
                tenant_id=self.context.tenant_id,
                root_run_id=self.context.root_run_id,
                task_id=self.context.task_id,
                execution_id=self.context.execution_id,
                registration_id=intent["registration_id"],
                endpoint_id=intent["endpoint_id"],
                deadline=intent["deadline_ms"] / 1000,
                status="admitting",
                remote_state="prepared",
                created_at=now(),
                updated_at=now(),
            )
        )

    def reserve(self, record, intent):
        billing = BillingService(
            self.db,
            self.worker.settings.redis_url,
            authorize=lambda conn: self._admission(conn, record, intent),
        )
        receipt = billing.reserve_fixed(
            self.context.tenant_id,
            self.context.user_id,
            intent["registration_id"],
            self.context.root_run_id,
            record["call_id"],
            quote_id=self.descriptor.quote_id,
            max_charge=self.descriptor.max_charge,
        )
        if receipt["status"] != "reserved":
            raise PlatformError(
                409, "remote_reservation_not_dispatchable", "费用预占不允许发送任务。"
            )
        sync_call_receipt(self.platform, receipt)

    def dispatch(self, record, intent):
        # Called by the transport after TLS handshake, before any POST bytes.
        with self.journal.transaction() as conn:
            self._point(conn, intent, sending=True)
            current = self.journal._record(conn, record["id"])
            if (
                current["state"] != "prepared"
                or time.time() * 1000 >= intent["deadline_ms"]
            ):
                raise PlatformError(
                    409, "remote_dispatch_expired", "任务已发送、撤销或超过期限。"
                )
            receipt = (
                conn.execute(
                    select(reservations_table).where(
                        reservations_table.c.tenant_id == self.context.tenant_id,
                        reservations_table.c.call_id == record["call_id"],
                    )
                )
                .mappings()
                .one()
            )
            if receipt["status"] != "reserved":
                raise PlatformError(409, "remote_hold_missing", "远程费用预占已失效。")
            self.journal._update(
                conn, current, current["revision"], state="sent", sent_at=time.time()
            )
            conn.execute(
                update(calls)
                .where(calls.c.id == record["call_id"])
                .values(remote_state="sent", updated_at=now())
            )

    def transport_access(self, intent, *, sending=False):
        with self.journal.transaction() as conn:
            if not self.worker.settings.remote_agents_enabled:
                raise PlatformError(409, "remote_agents_disabled", "远程执行已关闭。")
            return self._point(conn, intent, sending=sending)

    def next_poll(self, record, intent):
        with self.journal.transaction() as conn:
            row = (
                conn.execute(select(calls).where(calls.c.id == record["call_id"]))
                .mappings()
                .one()
            )
            if (
                row["poll_count"] >= MAX_EXECUTION_POLLS
                or time.time() * 1000 >= intent["deadline_ms"]
            ):
                raise PlatformError(
                    409,
                    "remote_result_unknown",
                    "远程任务缺少完整结果，已停止查询且未重发。",
                )
            conn.execute(
                update(calls)
                .where(calls.c.id == row["id"])
                .values(poll_count=row["poll_count"] + 1)
            )

    def settle_fee(self, record, status):
        """Fee conflicts cannot invalidate independently verified output evidence."""
        if status.fee is not None:
            try:
                receipt = self.platform.billing.settle_fixed(
                    self.context.tenant_id, record["call_id"], status.fee.model_dump()
                )
                sync_call_receipt(self.platform, receipt)
            except PlatformError as exc:
                if exc.code != "settlement_conflict":
                    raise
                # Fee conflicts keep holds; valid output is evaluated independently.

    def accept(self, record, payload, contract):
        status = RemoteStatus.model_validate(payload)
        if status.request_id != record["call_id"]:
            raise PlatformError(
                409, "remote_response_mismatch", "远程响应不属于当前请求。"
            )
        self.settle_fee(record, status)
        with self.journal.transaction() as conn:
            conn.execute(
                update(calls)
                .where(calls.c.id == record["call_id"])
                .values(remote_state=status.state, updated_at=now())
            )
        if status.result is not None:
            validate_value(contract.json_schema, status.result.value)
            current = self.journal.get(record["id"])
            self.journal.confirm(
                record["id"],
                status.model_dump(mode="json"),
                expected_revision=current["revision"],
            )
            return True, status.result.value
        if status.state in {"failed", "cancelled"}:
            raise PlatformError(502, "remote_task_failed", "远程成员未完成任务。")
        return False, None

    def finish_financial_wait(self, record):
        # This is evidence bookkeeping only; it cannot create another remote call.
        receipt = self.platform.billing.unresolved(
            self.context.tenant_id,
            record["call_id"],
            "远程调用结束，最终费用仍待核实。",
        )
        sync_call_receipt(self.platform, receipt)

    def abort(self, record):
        unsent = False
        with suppress(PlatformError, TimeoutError), self.journal.transaction() as conn:
            current = self.journal._record(conn, record["id"])
            if current["state"] == "prepared":
                self.journal._update(
                    conn, current, current["revision"], state="rejected"
                )
                unsent = True
            elif current["state"] == "sent":
                self.journal._update(
                    conn, current, current["revision"], state="unknown"
                )
        with suppress(PlatformError):
            if unsent:
                receipt = self.platform.billing.release(
                    self.context.tenant_id,
                    record["call_id"],
                    "持久日志确认远程请求未发送。",
                )
                sync_call_receipt(self.platform, receipt)
            else:
                self.finish_financial_wait(record)

    async def execute(self, task, inputs, contract):
        record, intent = await asyncio.to_thread(self.prepare, task, inputs)
        try:
            if record["state"] == "confirmed":
                payload = await asyncio.to_thread(self.journal.result, record["id"])
                status = RemoteStatus.model_validate(payload)
                if status.request_id != record["call_id"] or status.result is None:
                    raise PlatformError(
                        409, "remote_output_missing", "远程结果证据不完整。"
                    )
                await asyncio.to_thread(self.settle_fee, record, status)
                validate_value(contract.json_schema, status.result.value)
                return status.result.value
            if record["state"] == "rejected":
                raise PlatformError(
                    409, "remote_not_dispatched", "远程请求已在发送前终止。"
                )
            payload = None
            if record["state"] == "prepared":
                await asyncio.to_thread(self.reserve, record, intent)
                url, token = await asyncio.to_thread(
                    self.transport_access, intent, sending=True
                )
                request = RemoteRequest(
                    request_id=record["call_id"],
                    idempotency_key=record["idempotency_key"],
                    remote_agent_id=self.descriptor.remote_agent_id,
                    remote_version=self.descriptor.remote_version,
                    descriptor_digest=intent["digest"],
                    task_type=intent["task_type"],
                    objective=intent["objective"],
                    inputs=intent["inputs"],
                    input_digest=canonical_digest(intent["inputs"]),
                    quote_id=self.descriptor.quote_id,
                    max_charge=self.descriptor.max_charge,
                    deadline_ms=intent["deadline_ms"],
                )
                try:
                    payload = await self.transport.request(
                        url,
                        "/tasks",
                        method="POST",
                        token=token,
                        body=request.model_dump(),
                        timeout=self.remaining(intent),
                        authorize_send=lambda: self.dispatch(record, intent),
                    )
                except PlatformError:
                    current = await asyncio.to_thread(self.journal.get, record["id"])
                    if current["state"] == "prepared":
                        raise
                    # After a sent marker only status lookup is permitted.
            while True:
                if payload is not None:
                    done, output = await asyncio.to_thread(
                        self.accept, record, payload, contract
                    )
                    if done:
                        await asyncio.to_thread(self.finish_financial_wait, record)
                        return output
                if await asyncio.to_thread(self.worker.is_cancelled, self.run):
                    raise RunCancelled()
                await asyncio.to_thread(self.next_poll, record, intent)
                url, token = await asyncio.to_thread(self.transport_access, intent)
                await asyncio.sleep(min(0.5, self.remaining(intent)))
                try:
                    payload = await self.transport.request(
                        url,
                        f"/tasks/{record['call_id']}",
                        token=token,
                        timeout=self.remaining(intent),
                        authorize_send=lambda: self.transport_access(intent),
                    )
                except PlatformError as exc:
                    if exc.code != "remote_transport_failed":
                        raise
                    payload = None
        except BaseException:
            # Do not abandon proof/holds when an asyncio timeout cancels the task.
            cleanup = asyncio.create_task(asyncio.to_thread(self.abort, record))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise

    @staticmethod
    def remaining(intent):
        remaining = intent["deadline_ms"] / 1000 - time.time()
        if remaining <= 0:
            raise TimeoutError("远程任务截止时间已到。")
        return min(10, remaining)
