"""Fenced durable model/tool evidence; usage receipts never stand in for output."""

import hashlib
import json
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import insert, select, update

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal
from agent_platform.modules.run_records import now, uid

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024


class EvidenceCodec:
    def __init__(self, key):
        if not key:
            raise PlatformError(
                409, "journal_key_missing", "持久执行日志需要配置加密密钥。"
            )
        self.cipher = Fernet(key.encode())

    def encode(self, value):
        try:
            raw = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (ValueError, TypeError) as exc:
            raise PlatformError(
                422, "invalid_execution_evidence", "执行证据不可序列化。"
            ) from exc
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise PlatformError(
                422, "execution_evidence_too_large", "执行证据超过存储限制。"
            )
        return hashlib.sha256(raw).hexdigest(), self.cipher.encrypt(raw).decode()

    def decode(self, ciphertext, digest):
        try:
            raw = self.cipher.decrypt(ciphertext.encode())
            if (
                len(raw) > MAX_EVIDENCE_BYTES
                or hashlib.sha256(raw).hexdigest() != digest
            ):
                raise ValueError("digest mismatch")
            return json.loads(raw)
        except (InvalidToken, ValueError, AttributeError) as exc:
            raise PlatformError(
                409, "execution_evidence_invalid", "执行证据无法验证，不能自动重放。"
            ) from exc


class DurableJournal:
    """One instance is bound to an authorized execution and a current root lease.

    Keys include the graph phase and node identity. Both request and response are
    encrypted; native model items and tool observation values never enter public
    events or billing evidence. The caller must mark sent *before* external I/O.
    """

    def __init__(self, worker, context):
        self.worker, self.context = worker, context
        self.db = worker.db
        if not context.execution_id:
            raise ValueError("Journal requires an execution identity")
        self.codec = EvidenceCodec(worker.settings.secret_encryption_key)

    def guard(self, connection):
        run = self.context.invocation_run()
        current = self.worker._assert_lease(connection, run)
        self.worker.invocation._authorize_identity(connection, run)
        execution = (
            connection.execute(
                select(ct.executions).where(
                    ct.executions.c.tenant_id == self.context.tenant_id,
                    ct.executions.c.root_run_id == self.context.root_run_id,
                    ct.executions.c.id == self.context.execution_id,
                    ct.executions.c.owner == self.context.owner,
                    ct.executions.c.fence == self.context.fence,
                    ct.executions.c.status == "running",
                )
            )
            .mappings()
            .first()
        )
        if not execution or execution["spec"] != self.context.spec:
            raise PlatformError(409, "execution_lease_lost", "执行日志访问权已失效。")
        if current["cancel_requested"]:
            raise PlatformError(409, "run_cancelled", "运行已取消。")

    @contextmanager
    def transaction(self):
        with (
            self.db.tenant_scope(self.context.tenant_id),
            self.db.transaction(f"tenant:{self.context.tenant_id}") as connection,
        ):
            self.guard(connection)
            yield connection

    def query(self):
        return select(journal).where(
            journal.c.tenant_id == self.context.tenant_id,
            journal.c.root_run_id == self.context.root_run_id,
            journal.c.execution_id == self.context.execution_id,
        )

    def _record(self, connection, identifier):
        row = (
            connection.execute(self.query().where(journal.c.id == identifier))
            .mappings()
            .first()
        )
        if row is None:
            raise PlatformError(
                404, "execution_evidence_missing", "未找到当前执行的日志。"
            )
        return dict(row)

    def _owned(self, record):
        if (record["owner"], record["fence"]) != (
            self.context.owner,
            self.context.fence,
        ):
            raise PlatformError(
                409, "journal_fence_mismatch", "日志需要由当前执行者接管。"
            )

    def _update(self, connection, record, expected_revision, **values):
        self._owned(record)
        if record["revision"] != expected_revision:
            raise PlatformError(
                409, "journal_revision_conflict", "执行日志版本已变化。"
            )
        values.update(revision=expected_revision + 1, updated_at=now())
        changed = connection.execute(
            update(journal)
            .where(
                journal.c.id == record["id"],
                journal.c.tenant_id == self.context.tenant_id,
                journal.c.revision == expected_revision,
                journal.c.fence == self.context.fence,
                journal.c.owner == self.context.owner,
            )
            .values(**values)
        )
        if changed.rowcount != 1:
            raise PlatformError(
                409, "journal_revision_conflict", "执行日志版本已变化。"
            )
        return {**record, **values}

    def prepare(self, operation_key, kind, request, *, effect="external"):
        if kind not in {"model", "tool"} or effect not in {
            "pure",
            "observation",
            "external",
        }:
            raise ValueError("Invalid journal operation")
        if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 160:
            raise ValueError("Invalid journal operation key")
        if kind == "model" and effect != "external":
            raise ValueError("A model request must never be classified as recomputable")
        digest, encrypted = self.codec.encode(request)
        with self.transaction() as connection:
            previous = (
                connection.execute(
                    self.query().where(journal.c.operation_key == operation_key)
                )
                .mappings()
                .first()
            )
            if previous:
                record = dict(previous)
                self._owned(record)
                if (record["kind"], record["effect"], record["request_digest"]) != (
                    kind,
                    effect,
                    digest,
                ):
                    raise PlatformError(
                        409, "journal_intent_conflict", "执行标识已绑定其他请求或契约。"
                    )
                return record
            record = {
                "id": uid(),
                "tenant_id": self.context.tenant_id,
                "root_run_id": self.context.root_run_id,
                "execution_id": self.context.execution_id,
                "operation_key": operation_key,
                "kind": kind,
                "effect": effect,
                "state": "prepared",
                "request_digest": digest,
                "request_ciphertext": encrypted,
                "result_digest": None,
                "result_ciphertext": None,
                "call_id": uid() if kind == "model" else None,
                "attempt_id": uid() if kind == "model" else None,
                "idempotency_key": uid(),
                "owner": self.context.owner,
                "fence": self.context.fence,
                "revision": 0,
                "sent_at": None,
                "created_at": now(),
                "updated_at": now(),
            }
            connection.execute(insert(journal).values(**record))
            return record

    def get(self, identifier):
        with self.transaction() as connection:
            return self._record(connection, identifier)

    def mark_sent(self, identifier, *, expected_revision):
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            if record["state"] != "prepared":
                raise PlatformError(
                    409, "journal_already_sent", "请求已发送或终止，不能重复发送。"
                )
            return self._update(
                connection, record, expected_revision, state="sent", sent_at=time.time()
            )

    def confirm(self, identifier, result, *, expected_revision):
        digest, encrypted = self.codec.encode(result)
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            self._owned(record)
            if record["state"] == "confirmed" and record["result_digest"] == digest:
                return record
            if record["state"] not in {"sent", "unknown"}:
                raise PlatformError(
                    409, "journal_confirmation_conflict", "执行状态不允许确认此结果。"
                )
            return self._update(
                connection,
                record,
                expected_revision,
                state="confirmed",
                result_digest=digest,
                result_ciphertext=encrypted,
            )

    def mark_unknown(self, identifier, *, expected_revision):
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            if record["state"] != "sent":
                raise PlatformError(
                    409, "journal_state_conflict", "执行状态不允许标记未知结果。"
                )
            return self._update(connection, record, expected_revision, state="unknown")

    def reject(self, identifier, *, expected_revision):
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            if record["state"] != "prepared":
                raise PlatformError(
                    409, "journal_state_conflict", "已发送的请求不能按未发送撤销。"
                )
            return self._update(connection, record, expected_revision, state="rejected")

    def adopt(self, identifier, *, expected_revision):
        """Lease takeover changes ownership only; uncertainty and identity survive."""
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            if (
                record["fence"] >= self.context.fence
                or record["revision"] != expected_revision
            ):
                raise PlatformError(
                    409, "journal_adoption_conflict", "日志接管版本或租约不匹配。"
                )
            values = {
                "fence": self.context.fence,
                "owner": self.context.owner,
                "revision": expected_revision + 1,
                "updated_at": now(),
            }
            changed = connection.execute(
                update(journal)
                .where(
                    journal.c.id == identifier,
                    journal.c.tenant_id == self.context.tenant_id,
                    journal.c.revision == expected_revision,
                    journal.c.fence == record["fence"],
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise PlatformError(
                    409, "journal_adoption_conflict", "日志接管发生竞争。"
                )
            return {**record, **values}

    def result(self, identifier):
        with self.transaction() as connection:
            record = self._record(connection, identifier)
            self._owned(record)
            if record["state"] != "confirmed":
                raise PlatformError(
                    409, "execution_output_unconfirmed", "缺少已确认的完整执行输出。"
                )
            return self.codec.decode(
                record["result_ciphertext"], record["result_digest"]
            )

    def recovery_action(self, identifier):
        record = self.get(identifier)
        self._owned(record)
        if record["state"] == "confirmed":
            self.result(
                identifier
            )  # Verify full output; a fee receipt is insufficient.
            return "reuse_result"
        if record["state"] == "prepared":
            return "dispatch"
        if record["state"] in {"sent", "unknown"} and record["effect"] == "pure":
            return "recompute"
        return "stop"

    def execute_tool(self, operation_key, name, arguments):
        from .contracts import validate_value
        from .tools import execute_tool, tool_contract

        allowed = self.context.spec.get("tools", [])
        if name not in allowed:
            raise ValueError("工具未获此 Agent 授权。")
        contract = tool_contract(name)
        # External-effect tools require a separate receipt/reconciliation adapter.
        # Current built-ins are pure computations or recorded observations only.
        if contract.effect == "external":
            raise PlatformError(
                409, "tool_adapter_required", "该工具尚未接入执行证据适配器。"
            )
        validate_value(contract.input_schema, arguments)
        record = self.prepare(
            operation_key,
            "tool",
            {
                "contract": contract.model_dump(),
                "arguments": arguments,
            },
            effect=contract.effect,
        )
        action = self.recovery_action(record["id"])
        if action == "reuse_result":
            return self.result(record["id"])
        if action not in {"dispatch", "recompute"}:
            raise PlatformError(
                409, "tool_output_unknown", "缺少已确认工具结果，不能重新观察或重发。"
            )
        if action == "dispatch":
            record = self.mark_sent(record["id"], expected_revision=record["revision"])
        try:
            output = execute_tool(name, arguments, allowed)
            validate_value(contract.output_schema, output)
            result = {"ok": True, **output}
        except (ValueError, TypeError) as exc:
            result = {
                "ok": False,
                "error": str(exc) if type(exc) is ValueError else "工具参数校验失败。",
            }
        self.confirm(record["id"], result, expected_revision=record["revision"])
        return result
