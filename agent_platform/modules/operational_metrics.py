"""Small read-only operational aggregates; callers enforce operator authority."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import reservations_table


def _age(stamp, checked_at):
    if stamp is None:
        return None
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (checked_at - parsed).total_seconds())


def operational_snapshot(db, tenant_id: str, *, authorize=None) -> dict:
    """One tenant's backlog/financial holds, with no conversation/key payloads.

    Composition must supply current platform authorization. The callback runs
    in the same read connection as the aggregates, before any financial read.
    PostgreSQL lock counts cover only sessions visible to the runtime DB role;
    no claim is made about historical waits or privileged server-wide visibility.
    """
    if authorize is None:
        raise PlatformError(403, "capability_required", "需要平台诊断权限。")
    checked_at = datetime.now(UTC)
    aggregate = func.exact_sum if db.sqlite else func.sum

    def backlog(connection, table, stamp, *filters):
        count, oldest = connection.execute(
            select(func.count(), func.min(stamp)).where(
                table.c.tenant_id == tenant_id, *filters
            )
        ).one()
        return {"count": count, "oldest_age_seconds": _age(oldest, checked_at)}

    with db.tenant_scope(tenant_id), db.read() as connection:
        authorize(connection)
        queue = backlog(
            connection, t.runs, t.runs.c.created_at, t.runs.c.status == "queued"
        )
        unresolved_count, amount, oldest = connection.execute(
            select(
                func.count(),
                aggregate(reservations_table.c.amount),
                func.min(reservations_table.c.created_at),
            ).where(
                reservations_table.c.tenant_id == tenant_id,
                reservations_table.c.status == "unresolved",
            )
        ).one()
        outbox = backlog(
            connection,
            gt.outbox,
            gt.outbox.c.created_at,
            gt.outbox.c.status.not_in(("applied", "cancelled")),
        )
        metering = backlog(
            connection,
            mt.reconciliation_jobs,
            mt.reconciliation_jobs.c.updated_at,
            mt.reconciliation_jobs.c.status.not_in(("applied", "closed")),
        )
        frozen_count, frozen, oldest_cost = connection.execute(
            select(
                func.count(),
                aggregate(mt.cost_holds.c.amount),
                func.min(mt.cost_holds.c.created_at),
            ).where(
                mt.cost_holds.c.tenant_id == tenant_id,
                mt.cost_holds.c.status.in_(("reserved", "unresolved")),
            )
        ).one()
        lock_waiters = None
        if not db.sqlite:
            lock_waiters = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname=current_database() AND wait_event_type='Lock' "
                    "AND wait_event='advisory'"
                )
            )
    notifier = getattr(db, "event_notifier", None)
    return {
        "observed_at": checked_at.isoformat(),
        "queue": queue,
        "unresolved": {
            "count": unresolved_count,
            "amount": format(Decimal(str(amount or 0)), "f"),
            "oldest_age_seconds": _age(oldest, checked_at),
        },
        "gateway_outbox": outbox,
        "metering": metering,
        "supplier_frozen": {
            "count": frozen_count,
            "amount": format(Decimal(str(frozen or 0)), "f"),
            "oldest_age_seconds": _age(oldest_cost, checked_at),
        },
        "storage": {
            "advisory_waiters": lock_waiters,
            "visibility": "runtime_role_visible_sessions"
            if not db.sqlite
            else "not_available",
        },
        "notifications": notifier.stats() if notifier else None,
    }
