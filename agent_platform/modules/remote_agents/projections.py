"""A public union of model and remote calls; endpoint/credential/journal data stay private."""

from sqlalchemy import and_, cast, func, literal, select

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.remote_call_tables import calls


def call_projection():
    values = {
        "id": calls.c.id,
        "tenant_id": calls.c.tenant_id,
        "created_at": calls.c.created_at,
        "user_id": t.runs.c.user_id,
        "run_id": calls.c.root_run_id,
        "model_id": calls.c.registration_id,
        "model": literal("Remote Agent"),
        "task_id": calls.c.task_id,
        "execution_id": calls.c.execution_id,
        "agent_name": func.coalesce(
            ct.executions.c.spec["name"].as_string(), literal("[已删除]")
        ),
        "user_email": literal(""),
        "status": calls.c.status,
        "input_tokens": literal(0),
        "output_tokens": literal(0),
        "cost": literal("0"),
        "price_version": ct.executions.c.agent_version,
        "error": literal(""),
        "finished_at": calls.c.updated_at,
    }
    remote = select(
        *[
            values.get(column.name, cast(literal(None), column.type)).label(column.name)
            for column in t.calls.c
        ],
        literal("remote").label("call_kind"),
    ).select_from(
        calls.join(
            t.runs,
            and_(
                t.runs.c.tenant_id == calls.c.tenant_id,
                t.runs.c.id == calls.c.root_run_id,
            ),
        ).join(
            ct.executions,
            and_(
                ct.executions.c.tenant_id == calls.c.tenant_id,
                ct.executions.c.id == calls.c.execution_id,
            ),
        )
    )
    return (
        select(t.calls, literal("model").label("call_kind"))
        .union_all(remote)
        .subquery("public_calls")
    )
