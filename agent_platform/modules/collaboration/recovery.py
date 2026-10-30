"""Decide takeover from durable output evidence, never from usage receipts alone."""

import time

from sqlalchemy import select, update

from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal
from agent_platform.modules.billing.service import reservations_table
from agent_platform.modules.run_records import append_event
from agent_platform.modules.runtime.journal import EvidenceCodec


def settle_journal_usage(worker, tenant_id):
    """Financial evidence survives revocation and does not authorize execution."""
    from agent_platform.modules.runtime.invocation import sync_call_receipt

    with worker.db.read() as connection:
        records = (
            connection.execute(
                select(journal)
                .join(
                    reservations_table,
                    (reservations_table.c.tenant_id == journal.c.tenant_id)
                    & (reservations_table.c.call_id == journal.c.call_id),
                )
                .where(
                    journal.c.tenant_id == tenant_id,
                    journal.c.kind == "model",
                    journal.c.state == "confirmed",
                    reservations_table.c.status.in_(("reserved", "unresolved")),
                )
                .order_by(journal.c.created_at, journal.c.id)
                .limit(32)
            )
            .mappings()
            .all()
        )
    for record in records:
        try:
            output = EvidenceCodec(worker.settings.secret_encryption_key).decode(
                record["result_ciphertext"], record["result_digest"]
            )
            usage = output["raw_usage"]
            if (usage.get("logical_call_id"), usage.get("attempt_id")) != (
                record["call_id"],
                record["attempt_id"],
            ):
                continue
            receipt = worker.platform.billing.settle(
                tenant_id,
                record["call_id"],
                output["input_tokens"],
                output["output_tokens"],
                None,
                usage,
            )
            sync_call_receipt(worker.platform, receipt)
        except (PlatformError, TypeError, ValueError, KeyError, AttributeError):
            # Keep the financial hold. Takeover independently validates evidence
            # and will fail closed if the complete output cannot be proved.
            continue


def requeue_safe_run(worker, connection, run):
    features = run["spec"].get("team_snapshot", {}).get("features", {})
    if not (
        run["spec"].get("snapshot_schema_version") == 4
        and features.get("durable")
        and features.get("recovery")
        and worker.settings.collaboration_recovery_enabled
        and run["recovery_count"] < 3
        and not run["cancel_requested"]
    ):
        return False
    worker.invocation._authorize_identity(connection, dict(run))
    codec = EvidenceCodec(worker.settings.secret_encryption_key)
    active = select(ct.executions.c.id).where(
        ct.executions.c.tenant_id == run["tenant_id"],
        ct.executions.c.root_run_id == run["id"],
        ct.executions.c.status == "running",
    )
    for record in connection.execute(
        select(journal).where(
            journal.c.tenant_id == run["tenant_id"],
            journal.c.root_run_id == run["id"],
            journal.c.execution_id.in_(active),
        )
    ).mappings():
        codec.decode(record["request_ciphertext"], record["request_digest"])
        if record["state"] == "confirmed":
            codec.decode(record["result_ciphertext"], record["result_digest"])
        elif (
            record["state"] == "prepared"
            or record["state"] in {"sent", "unknown"}
            and record["effect"] == "pure"
        ):
            continue
        else:
            raise PlatformError(
                409,
                "recovery_output_unknown",
                "执行进程失联；存在缺少完整结果证据的调用，已停止续跑且未重发。",
            )
    expiry = time.time() + worker.settings.queue_timeout
    if run["deadline"] is not None:
        expiry = min(expiry, run["deadline"])
    connection.execute(
        update(t.runs)
        .where(
            t.runs.c.tenant_id == run["tenant_id"],
            t.runs.c.id == run["id"],
            t.runs.c.fence == run["fence"],
        )
        .values(
            status="queued",
            owner=None,
            lease_until=None,
            fence=run["fence"] + 1,
            recovery_count=run["recovery_count"] + 1,
            queue_expires_at=expiry,
        )
    )
    append_event(
        connection,
        run["id"],
        "run.recovering",
        {
            "status": "queued",
            "recovery_count": run["recovery_count"] + 1,
        },
    )
    return True
