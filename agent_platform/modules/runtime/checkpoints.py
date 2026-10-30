"""Private LangGraph checkpoints guarded by the platform execution lease."""

import asyncio
import base64

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import insert, select, update

from agent_platform.infrastructure import checkpoint_tables as cp
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.run_records import now, uid


class ExecutionCheckpointer(BaseCheckpointSaver):
    def __init__(self, journal, phase="initial"):
        super().__init__(serde=JsonPlusSerializer(pickle_fallback=False))
        if phase not in {"initial", "repair"}:
            raise ValueError("Unsupported graph phase")
        self.journal, self.phase = journal, phase
        self.context = journal.context

    def config(self, checkpoint_id=None):
        values = {"thread_id": self.context.execution_id, "checkpoint_ns": ""}
        if checkpoint_id:
            values["checkpoint_id"] = checkpoint_id
        return {"configurable": values}

    def _validate(self, config):
        values = config.get("configurable", {})
        if (
            values.get("thread_id") != self.context.execution_id
            or values.get("checkpoint_ns", "") != ""
        ):
            raise PlatformError(
                403, "checkpoint_scope_mismatch", "检查点必须属于当前执行。"
            )
        identifier = values.get("checkpoint_id")
        if identifier is not None and (
            not isinstance(identifier, str) or not 1 <= len(identifier) <= 64
        ):
            raise PlatformError(422, "invalid_checkpoint_id", "检查点标识无效。")
        return identifier

    def _query(self, table):
        return select(table).where(
            table.c.tenant_id == self.context.tenant_id,
            table.c.root_run_id == self.context.root_run_id,
            table.c.execution_id == self.context.execution_id,
            table.c.phase == self.phase,
        )

    def _pack(self, value):
        kind, payload = self.serde.dumps_typed(value)
        return {"kind": kind, "payload": base64.b64encode(payload).decode()}

    def _unpack(self, value):
        return self.serde.loads_typed(
            (value["kind"], base64.b64decode(value["payload"], validate=True))
        )

    def _row(self, checkpoint_id, value):
        digest, ciphertext = self.journal.codec.encode(value)
        return {
            "id": uid(),
            "tenant_id": self.context.tenant_id,
            "root_run_id": self.context.root_run_id,
            "execution_id": self.context.execution_id,
            "phase": self.phase,
            "checkpoint_id": checkpoint_id,
            "payload_digest": digest,
            "payload_ciphertext": ciphertext,
            "created_at": now(),
        }

    def get_tuple(self, config):
        identifier = self._validate(config)
        with self.journal.transaction() as connection:
            query = self._query(cp.checkpoints)
            if identifier:
                query = query.where(cp.checkpoints.c.checkpoint_id == identifier)
            row = (
                connection.execute(
                    query.order_by(cp.checkpoints.c.checkpoint_id.desc()).limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            payload = self.journal.codec.decode(
                row["payload_ciphertext"], row["payload_digest"]
            )
            pending = []
            for write in connection.execute(
                self._query(cp.writes)
                .where(cp.writes.c.checkpoint_id == row["checkpoint_id"])
                .order_by(cp.writes.c.graph_task_id, cp.writes.c.write_index)
            ).mappings():
                value = self.journal.codec.decode(
                    write["payload_ciphertext"], write["payload_digest"]
                )
                pending.append(
                    (write["graph_task_id"], write["channel"], self._unpack(value))
                )
            return CheckpointTuple(
                config=self.config(row["checkpoint_id"]),
                checkpoint=self._unpack(payload["checkpoint"]),
                metadata=self._unpack(payload["metadata"]),
                parent_config=self.config(row["parent_checkpoint_id"])
                if row["parent_checkpoint_id"]
                else None,
                pending_writes=pending,
            )

    def put(self, config, checkpoint, metadata, new_versions):
        parent = self._validate(config)
        identifier = checkpoint["id"]
        self._validate(self.config(identifier))
        # create_checkpoint supplies the complete channel values. Store that
        # encrypted snapshot directly; there are no shared blobs across agents.
        row = self._row(
            identifier,
            {
                "checkpoint": self._pack(checkpoint),
                "metadata": self._pack(get_checkpoint_metadata(config, metadata)),
            },
        )
        row["parent_checkpoint_id"] = parent
        with self.journal.transaction() as connection:
            existing = (
                connection.execute(
                    self._query(cp.checkpoints).where(
                        cp.checkpoints.c.checkpoint_id == identifier
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["payload_digest"] != row["payload_digest"]
                    or existing["parent_checkpoint_id"] != parent
                ):
                    raise PlatformError(
                        409, "checkpoint_conflict", "已确认检查点不能被覆盖。"
                    )
            else:
                connection.execute(insert(cp.checkpoints).values(**row))
        return self.config(identifier)

    def put_writes(self, config, writes, task_id, task_path=""):
        identifier = self._validate(config)
        if (
            identifier is None
            or not isinstance(task_id, str)
            or not 1 <= len(task_id) <= 64
        ):
            raise PlatformError(422, "invalid_checkpoint_write", "检查点写入标识无效。")
        with self.journal.transaction() as connection:
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                row = self._row(identifier, self._pack(value))
                row.update(graph_task_id=task_id, write_index=index, channel=channel)
                existing = (
                    connection.execute(
                        self._query(cp.writes).where(
                            cp.writes.c.checkpoint_id == identifier,
                            cp.writes.c.graph_task_id == task_id,
                            cp.writes.c.write_index == index,
                        )
                    )
                    .mappings()
                    .first()
                )
                if not existing:
                    connection.execute(insert(cp.writes).values(**row))
                elif index < 0:
                    connection.execute(
                        update(cp.writes)
                        .where(cp.writes.c.id == existing["id"])
                        .values(
                            payload_ciphertext=row["payload_ciphertext"],
                            payload_digest=row["payload_digest"],
                            channel=channel,
                        )
                    )
                elif (
                    existing["payload_digest"] != row["payload_digest"]
                    or existing["channel"] != channel
                ):
                    raise PlatformError(
                        409,
                        "checkpoint_write_conflict",
                        "检查点任务写入已确认不同结果。",
                    )

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(
            self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)
