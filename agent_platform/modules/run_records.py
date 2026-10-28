"""Shared run record writes, without loading the API application facade."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, insert, select

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.events import commit_notification


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return uuid4().hex


def append_event(connection, run_id: str, kind: str, data: dict) -> dict:
    sequence = (
        connection.scalar(
            select(func.max(t.run_events.c.sequence)).where(
                t.run_events.c.run_id == run_id
            )
        )
        or 0
    ) + 1
    event = {
        "run_id": run_id,
        "tenant_id": connection.scalar(
            select(t.runs.c.tenant_id).where(t.runs.c.id == run_id)
        ),
        "sequence": sequence,
        "type": kind,
        "data": data,
        "created_at": now(),
    }
    connection.execute(insert(t.run_events).values(**event))
    commit_notification(connection, event["tenant_id"], run_id, sequence)
    return event
