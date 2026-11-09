"""Authorized readable reasoning reconstructed from durable, public run events."""

from sqlalchemy import select

from agent_platform.infrastructure import tables as t


def session_reasoning(
    connection,
    user: dict,
    session_id: str,
    *,
    before: str | None = None,
    exclude_run_id: str | None = None,
) -> list[dict]:
    query = (
        select(t.run_events, t.runs.c.status.label("run_status"))
        .join(
            t.runs,
            (t.runs.c.id == t.run_events.c.run_id)
            & (t.runs.c.tenant_id == t.run_events.c.tenant_id),
        )
        .where(
            t.run_events.c.tenant_id == user["tenant_id"],
            t.runs.c.user_id == user["id"],
            t.runs.c.session_id == session_id,
            t.run_events.c.type.in_(
                ("reasoning.started", "reasoning.delta", "reasoning.finished")
            ),
        )
        .order_by(
            t.run_events.c.created_at, t.run_events.c.run_id, t.run_events.c.sequence
        )
    )
    if before is not None:
        query = query.where(t.runs.c.created_at <= before)
    if exclude_run_id is not None:
        query = query.where(t.runs.c.id != exclude_run_id)
    messages: dict[str, dict] = {}
    for row in connection.execute(query).mappings():
        data = row["data"]
        if data.get("execution_id"):
            continue
        call_id = data.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        identifier = f"reasoning:{row['run_id']}:{call_id}"
        if identifier not in messages:
            messages[identifier] = {
                "id": identifier,
                "run_id": row["run_id"],
                "content": "",
                "status": "streaming",
                "created_at": row["created_at"],
            }
        message = messages[identifier]
        if row["type"] == "reasoning.delta" and isinstance(data.get("text"), str):
            message["content"] += data["text"]
        if row["type"] == "reasoning.finished" or row["run_status"] == "succeeded":
            message["status"] = "complete"
        elif row["run_status"] not in {"queued", "running", "cancelling"}:
            message["status"] = "interrupted"
    return list(messages.values())


def reasoning_message(trace: dict) -> dict:
    """Only readable text/status enter AG-UI; no opaque native response items."""
    return {
        "id": trace["id"],
        "role": "reasoning",
        "content": trace["content"],
        "metadata": {"platform": {"reasoning_status": trace["status"]}},
    }
