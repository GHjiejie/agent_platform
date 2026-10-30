"""Immutable validated outputs, persisted in the same transaction as completion."""

import json

from sqlalchemy import insert, select

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.run_records import now, uid
from agent_platform.modules.runtime.contracts import canonical_digest, validate_value


def parse_result(text, contract):
    try:
        if len(text.encode()) > 196608:
            raise ValueError("oversized result")
        result = json.loads(text)
        validate_value(contract.json_schema, result)
        canonical_digest(result)
        return result
    except (ValueError, TypeError, RecursionError):
        raise PlatformError(
            422, "task_output_invalid", "成员输出不符合已发布契约。"
        ) from None


def save(connection, run, execution_id, kind, content, *, version=1, source_key=None):
    row = {
        "id": uid(),
        "tenant_id": run["tenant_id"],
        "root_run_id": run["id"],
        "execution_id": execution_id,
        "version": version,
        "kind": kind,
        "content": content,
        "digest": canonical_digest(content),
        "validation_status": "validated",
        "source_key": source_key,
        "created_at": now(),
    }
    connection.execute(insert(ct.artifacts).values(**row))
    return row


def dependency_outputs(connection, run, tasks):
    result = []
    for task in tasks:
        row = (
            connection.execute(
                select(ct.artifacts)
                .join(
                    ct.executions,
                    (ct.executions.c.id == ct.artifacts.c.execution_id)
                    & (ct.executions.c.tenant_id == ct.artifacts.c.tenant_id)
                    & (ct.executions.c.root_run_id == ct.artifacts.c.root_run_id),
                )
                .where(
                    ct.artifacts.c.tenant_id == run["tenant_id"],
                    ct.artifacts.c.root_run_id == run["id"],
                    ct.executions.c.task_id == task["id"],
                    ct.executions.c.status == "succeeded",
                    ct.artifacts.c.kind == "task_result",
                    ct.artifacts.c.validation_status == "validated",
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError(409, "dependency_unconfirmed", "任务依赖尚未确认。")
        result.append({**row, "task_key": task["logical_key"]})
    return result
