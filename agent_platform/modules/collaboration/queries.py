"""Authorized collaboration projections, excluding private prompts and routing."""

from decimal import Decimal

from sqlalchemy import select

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.remote_call_tables import calls as remote_calls
from agent_platform.modules.billing.service import reservations_table


def detail(platform, user, run_id, task_id=None):
    with platform.db.tenant_scope(user["tenant_id"]), platform.db.read() as connection:
        platform.identity.assert_current(connection, user, "runs.read_own")
        run = platform.owned(connection, t.runs, user, run_id, own=True)
        tasks = [
            dict(row)
            for row in connection.execute(
                select(ct.tasks)
                .where(
                    ct.tasks.c.tenant_id == user["tenant_id"],
                    ct.tasks.c.root_run_id == run_id,
                )
                .order_by(ct.tasks.c.created_at, ct.tasks.c.id)
            ).mappings()
        ]
        if task_id and not any(task["id"] == task_id for task in tasks):
            raise PlatformError(404, "not_found", "任务不存在或无权访问。")
        executions = [
            dict(row)
            for row in connection.execute(
                select(ct.executions)
                .where(
                    ct.executions.c.tenant_id == user["tenant_id"],
                    ct.executions.c.root_run_id == run_id,
                )
                .order_by(ct.executions.c.created_at, ct.executions.c.id)
            ).mappings()
        ]
        outputs = [
            dict(row)
            for row in connection.execute(
                select(ct.artifacts)
                .where(
                    ct.artifacts.c.tenant_id == user["tenant_id"],
                    ct.artifacts.c.root_run_id == run_id,
                )
                .order_by(ct.artifacts.c.created_at, ct.artifacts.c.id)
            ).mappings()
        ]
        receipts = list(
            connection.execute(
                select(t.calls.c.execution_id, reservations_table)
                .join(
                    reservations_table,
                    (t.calls.c.id == reservations_table.c.call_id)
                    & (t.calls.c.tenant_id == reservations_table.c.tenant_id),
                )
                .where(
                    t.calls.c.tenant_id == user["tenant_id"], t.calls.c.run_id == run_id
                )
            ).mappings()
        )

        receipts.extend(
            connection.execute(
                select(remote_calls.c.execution_id, reservations_table)
                .join(
                    reservations_table,
                    (remote_calls.c.id == reservations_table.c.call_id)
                    & (remote_calls.c.tenant_id == reservations_table.c.tenant_id),
                )
                .where(
                    remote_calls.c.tenant_id == user["tenant_id"],
                    remote_calls.c.root_run_id == run_id,
                )
            ).mappings()
        )

        def fees(rows):
            return {
                "confirmed": str(
                    sum((Decimal(str(row["cost"])) for row in rows), Decimal(0))
                ),
                "pending": str(
                    sum(
                        (
                            Decimal(str(row["amount"]))
                            for row in rows
                            if row["status"] in ("reserved", "unresolved")
                        ),
                        Decimal(0),
                    )
                ),
            }

        public_executions = []
        for execution in executions:
            if task_id and execution["task_id"] != task_id:
                continue
            public_executions.append(
                {
                    **{
                        key: execution[key]
                        for key in (
                            "id",
                            "task_id",
                            "attempt",
                            "role",
                            "agent_id",
                            "agent_version",
                            "status",
                            "output",
                            "error",
                            "created_at",
                            "finished_at",
                        )
                    },
                    "name": execution["spec"].get("name", ""),
                    "model_alias": execution["spec"].get("model", {}).get("alias", ""),
                    "input_digest": execution["input_digest"],
                    "input_summary": "\n".join(
                        message.get("content", "") for message in execution["input"]
                    )[:2000]
                    if task_id
                    else None,
                    "fees": fees(
                        [
                            row
                            for row in receipts
                            if row["execution_id"] == execution["id"]
                        ]
                    ),
                }
            )
        execution_ids = {execution["id"] for execution in public_executions}
        return {
            "run_id": run_id,
            "status": run["status"],
            "outcome": run["outcome"],
            "enabled": run["spec"].get("execution_mode") == "team",
            "tasks": [task for task in tasks if not task_id or task["id"] == task_id],
            "executions": public_executions,
            "artifacts": [
                artifact
                for artifact in outputs
                if artifact["execution_id"] in execution_ids
            ],
            "fees": fees(receipts),
            "max_cost": run["spec"].get("execution_limits", {}).get("max_cost"),
        }
