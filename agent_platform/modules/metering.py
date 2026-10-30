"""Persist metering evidence before deciding which evidence may settle a call."""

import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select

from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.modules.billing.service import (
    _fingerprint,
    _id,
    _money,
    _now,
    _serialize,
    reservations_table,
)


def record_receipt(
    connection,
    tenant_id,
    call_id,
    input_tokens,
    output_tokens,
    provider_cost,
    raw_usage,
    *,
    conflict=False,
):
    """Immutable source-keyed evidence. Conflicting revisions are preserved too."""
    evidence = dict(raw_usage or {})
    source = evidence.get("usage_source", evidence.get("source", "litellm_gateway"))
    if source not in {
        "litellm_gateway",
        "litellm_logs",
        "manual_reconciliation",
        "remote_agent",
    }:
        source = "litellm_gateway"
    event_id = str(
        evidence.get("source_event_id")
        or evidence.get("gateway_response_id")
        or evidence.get("gateway_request_id")
        or call_id
    )
    if len(event_id) > 120:
        evidence["original_source_event_id"] = event_id
        event_id = _fingerprint(event_id)
    gateway_request_id = evidence.get("gateway_response_id") or evidence.get(
        "gateway_request_id"
    )
    if gateway_request_id is not None and not isinstance(gateway_request_id, str):
        evidence["unusable_gateway_request_id"] = gateway_request_id
        gateway_request_id = None
    elif gateway_request_id is not None and len(gateway_request_id) > 200:
        # Vendor IDs have no portable length bound. Keep the complete original
        # evidence and use a stable projection that fits PostgreSQL VARCHAR.
        evidence["original_gateway_request_id"] = gateway_request_id
        gateway_request_id = _fingerprint(gateway_request_id)
    fingerprint = _fingerprint(
        [
            input_tokens,
            output_tokens,
            format(provider_cost.normalize(), "f")
            if provider_cost is not None
            else None,
            evidence,
        ]
    )
    filters = [
        mt.usage_receipts.c.tenant_id == tenant_id,
        mt.usage_receipts.c.call_id == call_id,
        mt.usage_receipts.c.source == source,
    ]
    existing = (
        connection.execute(
            select(mt.usage_receipts).where(
                *filters, mt.usage_receipts.c.source_event_id == event_id
            )
        )
        .mappings()
        .first()
    )
    if (
        existing
        and existing["fingerprint"] == fingerprint
        and not (conflict and existing["quality"] != "conflict")
    ):
        return dict(existing), existing["quality"] == "conflict"
    if existing:
        conflict = True
        evidence["conflicts_with_receipt_id"] = existing["id"]
        event_id += ":conflict:" + fingerprint
        duplicate = (
            connection.execute(
                select(mt.usage_receipts).where(
                    *filters, mt.usage_receipts.c.source_event_id == event_id
                )
            )
            .mappings()
            .first()
        )
        if duplicate:
            return dict(duplicate), True
    values = {
        "id": _id(),
        "tenant_id": tenant_id,
        "call_id": call_id,
        "attempt_id": evidence.get("attempt_id"),
        "source": source,
        "source_event_id": event_id,
        "gateway_request_id": gateway_request_id,
        "quality": "conflict"
        if conflict
        else "manual"
        if source == "manual_reconciliation"
        else "remote_final"
        if source == "remote_agent"
        else "gateway_estimate",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider_cost": provider_cost,
        "evidence": evidence,
        "fingerprint": fingerprint,
        "created_at": _now(),
    }
    connection.execute(mt.usage_receipts.insert().values(**values))
    return values, conflict


def prepare_reconciliation(connection, tenant_id, call_id, attempt_id):
    if connection.scalar(
        select(mt.reconciliation_jobs.c.attempt_id).where(
            mt.reconciliation_jobs.c.tenant_id == tenant_id,
            mt.reconciliation_jobs.c.attempt_id == attempt_id,
        )
    ):
        return
    connection.execute(
        mt.reconciliation_jobs.insert().values(
            tenant_id=tenant_id,
            attempt_id=attempt_id,
            call_id=call_id,
            status="pending",
            next_attempt_at=time.time() + 30,
            attempt_count=0,
            owner=None,
            fence=0,
            lease_until=None,
            last_error="",
            updated_at=_now(),
        )
    )


class MeteringReconciler:
    """Only the gateway control process reads logs; requests remain outside locks."""

    def __init__(self, db, settings, adapter):
        self.db, self.settings, self.adapter = db, settings, adapter
        self.owner, self.cursor = _id(), ""

    def _claim(self):
        with self.db.read() as connection:
            tenants = list(
                connection.scalars(
                    select(nt.tenant_settings.c.tenant_id)
                    .where(nt.tenant_settings.c.tenant_id > self.cursor)
                    .order_by(nt.tenant_settings.c.tenant_id)
                    .limit(32)
                )
            )
        if not tenants:
            self.cursor = ""
            return None
        for tenant_id in tenants:
            self.cursor = tenant_id
            with (
                self.db.tenant_scope(tenant_id),
                self.db.transaction(f"tenant:{tenant_id}") as connection,
            ):
                clock = time.time()
                row = (
                    connection.execute(
                        select(mt.reconciliation_jobs)
                        .where(
                            mt.reconciliation_jobs.c.tenant_id == tenant_id,
                            mt.reconciliation_jobs.c.status.in_(
                                ["pending", "retry_wait", "applying"]
                            ),
                            mt.reconciliation_jobs.c.next_attempt_at <= clock,
                            (mt.reconciliation_jobs.c.lease_until.is_(None))
                            | (mt.reconciliation_jobs.c.lease_until < clock),
                        )
                        .order_by(mt.reconciliation_jobs.c.next_attempt_at)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    continue
                receipt = (
                    connection.execute(
                        select(reservations_table).where(
                            reservations_table.c.tenant_id == tenant_id,
                            reservations_table.c.call_id == row["call_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if not receipt or receipt["status"] in {"released", "written_off"}:
                    connection.execute(
                        mt.reconciliation_jobs.update()
                        .where(
                            mt.reconciliation_jobs.c.tenant_id == tenant_id,
                            mt.reconciliation_jobs.c.attempt_id == row["attempt_id"],
                        )
                        .values(status="closed", updated_at=_now())
                    )
                    continue
                attempt = (
                    connection.execute(
                        select(rt.call_attempts).where(
                            rt.call_attempts.c.tenant_id == tenant_id,
                            rt.call_attempts.c.id == row["attempt_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if not attempt:
                    continue
                values = {
                    "owner": self.owner,
                    "fence": row["fence"] + 1,
                    "lease_until": clock + 60,
                    "status": "applying",
                    "attempt_count": row["attempt_count"] + 1,
                    "updated_at": _now(),
                }
                connection.execute(
                    mt.reconciliation_jobs.update()
                    .where(
                        mt.reconciliation_jobs.c.tenant_id == tenant_id,
                        mt.reconciliation_jobs.c.attempt_id == row["attempt_id"],
                    )
                    .values(**values)
                )
                return {
                    **dict(row),
                    **values,
                    "route": attempt["route"],
                    "created_at": attempt["created_at"],
                }
        return None

    def _finish(self, job, state, error=""):
        with (
            self.db.tenant_scope(job["tenant_id"]),
            self.db.transaction(f"tenant:{job['tenant_id']}") as connection,
        ):
            connection.execute(
                mt.reconciliation_jobs.update()
                .where(
                    mt.reconciliation_jobs.c.tenant_id == job["tenant_id"],
                    mt.reconciliation_jobs.c.attempt_id == job["attempt_id"],
                    mt.reconciliation_jobs.c.owner == self.owner,
                    mt.reconciliation_jobs.c.fence == job["fence"],
                )
                .values(
                    status=state,
                    last_error=error,
                    lease_until=None,
                    next_attempt_at=time.time()
                    + min(3600, 30 * 2 ** min(job["attempt_count"], 7)),
                    updated_at=_now(),
                )
            )

    def process_one(self):
        job = self._claim()
        if not job:
            return False
        try:
            # Query by a server-generated opaque end_user, since response IDs may
            # be missing when the stream is interrupted. Never scan all spend logs.
            started_at = datetime.fromisoformat(job["created_at"])
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            # LiteLLM v2 accepts dates or second-resolution UTC timestamps, and
            # treats both bounds inclusively. Cover whole UTC days through the
            # next midnight so fractional seconds cannot fall outside the query.
            query = urlencode(
                {
                    "end_user": "agent-platform-attempt-" + job["attempt_id"],
                    "page": 1,
                    "page_size": 100,
                    "start_date": started_at.astimezone(UTC).date().isoformat(),
                    "end_date": (
                        datetime.now(UTC).date() + timedelta(days=1)
                    ).isoformat(),
                }
            )
            payload = self.adapter._request("/spend/logs/v2?" + query)
            rows = (
                payload.get("data", payload.get("logs", []))
                if isinstance(payload, dict)
                else []
            )
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError("invalid_logs")
            matches = [
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("end_user") == "agent-platform-attempt-" + job["attempt_id"]
                and row.get("model_group", row.get("model")) == job["route"]
            ]
            if len(matches) != 1 or (
                isinstance(payload, dict)
                and payload.get("total", len(rows)) > len(rows)
            ):
                self._finish(
                    job,
                    "retry_wait",
                    "usage_not_unique" if matches else "usage_not_found",
                )
                return True
            log = matches[0]
            input_tokens, output_tokens = (
                log.get("prompt_tokens"),
                log.get("completion_tokens"),
            )
            if (
                log.get("status") != "success"
                or type(input_tokens) is not int
                or type(output_tokens) is not int
                or min(input_tokens, output_tokens) < 0
                or log.get("total_tokens") != input_tokens + output_tokens
                or not log.get("request_id")
            ):
                self._finish(job, "retry_wait", "usage_incomplete")
                return True
            raw = {
                "usage_source": "litellm_logs",
                "attempt_id": job["attempt_id"],
                "source_event_id": str(log["request_id"]),
                "gateway_request_id": str(log["request_id"]),
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "provider_cost_quality": "gateway_estimate",
            }
            with (
                self.db.tenant_scope(job["tenant_id"]),
                self.db.transaction(f"tenant:{job['tenant_id']}") as connection,
            ):
                _assert_job(connection, job, self.owner)
                _, conflict = record_receipt(
                    connection,
                    job["tenant_id"],
                    job["call_id"],
                    input_tokens,
                    output_tokens,
                    _money(log["spend"], "provider_cost")
                    if log.get("spend") is not None
                    else None,
                    raw,
                )
                connection.execute(
                    mt.reconciliation_jobs.update()
                    .where(
                        mt.reconciliation_jobs.c.tenant_id == job["tenant_id"],
                        mt.reconciliation_jobs.c.attempt_id == job["attempt_id"],
                    )
                    .values(
                        status="conflict" if conflict else "evidence_ready",
                        lease_until=None,
                        owner=None,
                        updated_at=_now(),
                    )
                )
        except Exception as error:  # noqa: BLE001 - retry without exposing gateway response or secrets
            code = getattr(error, "code", "metering_query_failed")
            self._finish(
                job,
                "conflict"
                if code in {"settlement_conflict", "receipt_conflict"}
                else "retry_wait",
                code,
            )
        return True

    def receipts(self, tenant_id, call_id):
        with self.db.tenant_scope(tenant_id), self.db.read() as connection:
            return [
                _serialize(row)
                for row in connection.execute(
                    select(mt.usage_receipts)
                    .where(
                        mt.usage_receipts.c.tenant_id == tenant_id,
                        mt.usage_receipts.c.call_id == call_id,
                    )
                    .order_by(mt.usage_receipts.c.created_at)
                ).mappings()
            ]


def _assert_job(connection, job, owner):
    from agent_platform.infrastructure.errors import PlatformError

    current = (
        connection.execute(
            select(mt.reconciliation_jobs).where(
                mt.reconciliation_jobs.c.tenant_id == job["tenant_id"],
                mt.reconciliation_jobs.c.attempt_id == job["attempt_id"],
            )
        )
        .mappings()
        .first()
    )
    if (
        not current
        or current["owner"] != owner
        or current["fence"] != job["fence"]
        or current["lease_until"] is None
        or current["lease_until"] <= time.time()
    ):
        raise PlatformError(409, "metering_lease_lost", "计量补证执行权已失效。")


def apply_ready_evidence(db, tenant_id, owner, *, redis_url=None):
    """Worker consumes persisted evidence; the control process never changes wallets."""
    with (
        db.tenant_scope(tenant_id),
        db.transaction(f"tenant:{tenant_id}") as connection,
    ):
        clock = time.time()
        job = (
            connection.execute(
                select(mt.reconciliation_jobs)
                .where(
                    mt.reconciliation_jobs.c.tenant_id == tenant_id,
                    (
                        (mt.reconciliation_jobs.c.status == "evidence_ready")
                        | (
                            (mt.reconciliation_jobs.c.status == "settling")
                            & (mt.reconciliation_jobs.c.lease_until < clock)
                        )
                    ),
                )
                .order_by(mt.reconciliation_jobs.c.updated_at)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if not job:
            return False
        values = {
            "status": "settling",
            "owner": owner,
            "fence": job["fence"] + 1,
            "lease_until": clock + 60,
            "updated_at": _now(),
        }
        connection.execute(
            mt.reconciliation_jobs.update()
            .where(
                mt.reconciliation_jobs.c.tenant_id == tenant_id,
                mt.reconciliation_jobs.c.attempt_id == job["attempt_id"],
            )
            .values(**values)
        )
        job = {**dict(job), **values}
        evidence = (
            connection.execute(
                select(mt.usage_receipts)
                .where(
                    mt.usage_receipts.c.tenant_id == tenant_id,
                    mt.usage_receipts.c.call_id == job["call_id"],
                    mt.usage_receipts.c.attempt_id == job["attempt_id"],
                    mt.usage_receipts.c.source == "litellm_logs",
                )
                .order_by(mt.usage_receipts.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
    if not evidence or evidence["quality"] == "conflict":
        state = "conflict"
    else:
        from agent_platform.infrastructure.errors import PlatformError
        from agent_platform.modules.billing.service import BillingService

        try:
            with db.tenant_scope(tenant_id):
                BillingService(
                    db,
                    redis_url,
                    authorize=lambda connection: _assert_job(connection, job, owner),
                ).settle(
                    tenant_id,
                    job["call_id"],
                    evidence["input_tokens"],
                    evidence["output_tokens"],
                    evidence["provider_cost"],
                    evidence["evidence"],
                )
            state = "applied"
        except PlatformError as error:
            if error.code == "metering_lease_lost":
                return True
            if error.code not in {"settlement_conflict", "reservation_released"}:
                raise
            state = "conflict" if error.code == "settlement_conflict" else "closed"
    with (
        db.tenant_scope(tenant_id),
        db.transaction(f"tenant:{tenant_id}") as connection,
    ):
        _assert_job(connection, job, owner)
        connection.execute(
            mt.reconciliation_jobs.update()
            .where(
                mt.reconciliation_jobs.c.tenant_id == tenant_id,
                mt.reconciliation_jobs.c.attempt_id == job["attempt_id"],
            )
            .values(status=state, lease_until=None, updated_at=_now())
        )
    return True
