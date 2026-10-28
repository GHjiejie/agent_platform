"""Tenant-serialized accounting; the database is the final admission authority.

Redis, when configured, adds an atomic shared rate limiter and must be available
for new reservations. SQLite uses exact decimal strings rather than binary floats.
Unconfirmed requests keep their financial holds until settlement or reconciliation.
Execution concurrency counts reserved calls only; unresolved calls retain their
wallet, budget and rate-window usage without blocking new execution slots.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.engine import Connection

from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.db import Database, metadata
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.money import ExactMoney

ZERO = Decimal(0)
PRECISION = Decimal("0.000000000001")
MAX_MONEY = Decimal(1000000000)
FINANCIAL_HOLD_STATUSES = ("reserved", "unresolved")


wallets = Table(
    "billing_wallets",
    metadata,
    Column("tenant_id", String(64), primary_key=True),
    Column("balance", ExactMoney(), nullable=False),
    Column("reserved", ExactMoney(), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("blocked", Boolean(), nullable=False, default=False),
    Column("block_reason", Text(), nullable=False, default=""),
)
quota_policies = Table(
    "billing_quotas",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("scope", String(16), nullable=False),
    Column("subject_id", String(64), nullable=False),
    Column("rpm", Integer()),
    Column("tpm", Integer()),
    Column("concurrent", Integer()),
    Column("max_budget", ExactMoney()),
    UniqueConstraint("tenant_id", "scope", "subject_id"),
)
reservations_table = Table(
    "billing_reservations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("user_id", String(64), nullable=False),
    Column("model_id", String(64), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("call_id", String(64), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("amount", ExactMoney(), nullable=False),
    Column("input_price", ExactMoney(), nullable=False),
    Column("output_price", ExactMoney(), nullable=False),
    Column("estimated_input_tokens", Integer(), nullable=False),
    Column("max_output_tokens", Integer(), nullable=False),
    Column("input_tokens", Integer()),
    Column("output_tokens", Integer()),
    Column("rate_tokens", BigInteger(), nullable=False),
    Column("rate_scopes", Text(), nullable=False),
    Column("status", String(20), nullable=False),
    Column("cost", ExactMoney(), nullable=False),
    Column("actual_cost", ExactMoney()),
    Column("overage", ExactMoney(), nullable=False),
    Column("provider_cost", ExactMoney()),
    Column("raw_usage", Text()),
    Column("reason", Text(), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "call_id"),
)
Index(
    "ix_billing_reservations_tenant_created",
    reservations_table.c.tenant_id,
    reservations_table.c.created_at,
)
Index(
    "ix_reservations_tenant_status",
    reservations_table.c.tenant_id,
    reservations_table.c.status,
    reservations_table.c.updated_at,
)
ledger_transactions = Table(
    "billing_transactions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("type", String(20), nullable=False),
    Column("amount", ExactMoney(), nullable=False),
    Column("balance", ExactMoney(), nullable=False),
    Column("description", Text(), nullable=False),
    Column("actor_id", String(64)),
    Column("call_id", String(64)),
    Column("idempotency_key", String(200)),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "type", "idempotency_key"),
    UniqueConstraint("tenant_id", "type", "call_id"),
)
ledger_entries = Table(
    "billing_entries",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("transaction_id", String(64), nullable=False),
    Column("account", String(64), nullable=False),
    Column("amount", ExactMoney(), nullable=False),
    Column("currency", String(3), nullable=False),
)
billing_audit = Table(
    "billing_audit",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("actor_id", String(64)),
    Column("action", String(64), nullable=False),
    Column("target", String(200), nullable=False),
    Column("details", Text(), nullable=False),
    Column("created_at", String(40), nullable=False),
)
reconciliations = Table(
    "billing_reconciliations",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("call_id", String(64), nullable=False),
    Column("actor_id", String(64), nullable=False),
    Column("action", String(20), nullable=False),
    Column("reason", Text(), nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("tenant_id", "idempotency_key"),
    UniqueConstraint("tenant_id", "call_id"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _money(value: Any, name: str = "amount") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PlatformError(
            422, "invalid_amount", f"{name} must be a nonnegative decimal"
        )
    try:
        result = Decimal(str(value))
        valid = result.is_finite() and ZERO <= result <= MAX_MONEY
        if not valid or result != result.quantize(PRECISION):
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise PlatformError(
            422,
            "invalid_amount",
            f"{name} must be between 0 and 1 billion, with at most 12 decimals",
        ) from None


def _tokens(value: Any, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2_000_000_000
    ):
        raise PlatformError(
            422, "invalid_usage", f"{name} must be a nonnegative integer"
        )
    return value


def _cost(
    input_tokens: int, output_tokens: int, input_price: Decimal, output_price: Decimal
) -> Decimal:
    value = (input_tokens * input_price + output_tokens * output_price) / Decimal(
        1_000_000
    )
    return value.quantize(PRECISION, rounding=ROUND_CEILING)


def _serialize(row: Any) -> dict:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, Decimal):
            result[key] = format(value, "f")
    for key in ("raw_usage", "rate_scopes"):
        if key in result and result[key] is not None:
            result[key] = json.loads(result[key])
    return result


class BillingService:
    def __init__(
        self,
        db: Database,
        redis_url: str | None = None,
        authorize: Callable[[Connection], None] | None = None,
    ):
        self.db = db
        self.authorize = authorize
        self.rate_mode = "redis+database" if redis_url else "database"
        self._limiter = RedisLimiter(redis_url) if redis_url else None

    def _authorize(self, conn: Connection) -> None:
        if self.authorize is not None:
            self.authorize(conn)

    @staticmethod
    def _wallet_view(row: dict) -> dict:
        return {
            "balance": format(row["balance"], "f"),
            "reserved": format(row["reserved"], "f"),
            "available": format(row["balance"] - row["reserved"], "f"),
            "currency": row["currency"],
            "blocked": row["blocked"],
            "block_reason": row["block_reason"],
        }

    @staticmethod
    def _get_wallet(conn, tenant_id: str, create: bool = False) -> dict:
        row = (
            conn.execute(select(wallets).where(wallets.c.tenant_id == tenant_id))
            .mappings()
            .first()
        )
        if row:
            return dict(row)
        row = {
            "tenant_id": tenant_id,
            "balance": ZERO,
            "reserved": ZERO,
            "currency": "USD",
            "blocked": False,
            "block_reason": "",
        }
        if create:
            conn.execute(wallets.insert().values(**row))
        return row

    @staticmethod
    def _audit(conn, tenant_id, actor_id, action, target, details):
        conn.execute(
            billing_audit.insert().values(
                id=_id(),
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=action,
                target=target,
                details=_json(details),
                created_at=_now(),
            )
        )

    @staticmethod
    def _transaction(
        conn,
        *,
        tenant_id,
        kind,
        amount,
        balance,
        description,
        actor_id=None,
        call_id=None,
        idempotency_key=None,
        fingerprint="",
    ):
        transaction_id = _id()
        conn.execute(
            ledger_transactions.insert().values(
                id=transaction_id,
                tenant_id=tenant_id,
                type=kind,
                amount=amount,
                balance=balance,
                description=description,
                actor_id=actor_id,
                call_id=call_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                created_at=_now(),
            )
        )
        # Signed postings sum to zero for each immutable transaction.
        contra = "external_credit" if kind == "credit" else "model_revenue"
        conn.execute(
            ledger_entries.insert(),
            [
                {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "transaction_id": transaction_id,
                    "account": "customer_wallet",
                    "amount": amount,
                    "currency": "USD",
                },
                {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "transaction_id": transaction_id,
                    "account": contra,
                    "amount": -amount,
                    "currency": "USD",
                },
            ],
        )

    def wallet(self, tenant_id: str) -> dict:
        with self.db.tenant_scope(tenant_id), self.db.read() as conn:
            return self._wallet_view(self._get_wallet(conn, tenant_id))

    def credit(
        self,
        tenant_id: str,
        actor_id: str,
        amount: Any,
        description: str,
        idempotency_key: str,
    ) -> dict:
        amount = _money(amount)
        if amount <= ZERO:
            raise PlatformError(422, "invalid_amount", "Credit must be positive")
        if not actor_id or not isinstance(description, str) or not description.strip():
            raise PlatformError(
                422, "audit_required", "Credit requires an actor and a reason"
            )
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise PlatformError(
                422, "idempotency_required", "A valid Idempotency-Key is required"
            )
        fingerprint = _fingerprint(
            [actor_id, format(amount.normalize(), "f"), description]
        )
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            previous = (
                conn.execute(
                    select(ledger_transactions).where(
                        ledger_transactions.c.tenant_id == tenant_id,
                        ledger_transactions.c.type == "credit",
                        ledger_transactions.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            wallet = self._get_wallet(conn, tenant_id, create=True)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise PlatformError(
                        409,
                        "idempotency_conflict",
                        "Idempotency-Key is bound to another credit",
                    )
                return self._wallet_view(wallet)
            balance = wallet["balance"] + amount
            if balance > MAX_MONEY:
                raise PlatformError(
                    422, "balance_limit", "Wallet maximum balance exceeded"
                )
            conn.execute(
                wallets.update()
                .where(wallets.c.tenant_id == tenant_id)
                .values(balance=balance)
            )
            self._transaction(
                conn,
                tenant_id=tenant_id,
                kind="credit",
                amount=amount,
                balance=balance,
                description=description,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            self._audit(
                conn,
                tenant_id,
                actor_id,
                "billing.credit",
                idempotency_key,
                {"amount": format(amount, "f"), "description": description},
            )
            wallet["balance"] = balance
            return self._wallet_view(wallet)

    @staticmethod
    def _quota_rows(conn, tenant_id: str) -> list[dict]:
        rows = [
            dict(row)
            for row in conn.execute(
                select(quota_policies).where(quota_policies.c.tenant_id == tenant_id)
            ).mappings()
        ]
        if not any(row["scope"] == "tenant" for row in rows):
            rows.insert(
                0,
                {
                    "id": f"default:{tenant_id}",
                    "tenant_id": tenant_id,
                    "scope": "tenant",
                    "subject_id": tenant_id,
                    "rpm": 60,
                    "tpm": 120000,
                    "concurrent": 4,
                    "max_budget": None,
                },
            )
        return rows

    def quotas(self, tenant_id: str) -> list[dict]:
        with self.db.tenant_scope(tenant_id), self.db.read() as conn:
            return [_serialize(row) for row in self._quota_rows(conn, tenant_id)]

    @staticmethod
    def _entitlement_policy(conn, tenant_id: str) -> dict:
        """Platform limits remain authoritative even when tenant quotas are NULL."""
        row = (
            conn.execute(
                select(nt.entitlements).where(nt.entitlements.c.tenant_id == tenant_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PlatformError(
                403, "entitlement_missing", "组织套餐尚未配置，不能发起新调用。"
            )
        return {
            "id": f"entitlement:{tenant_id}",
            "tenant_id": tenant_id,
            "scope": "entitlement",
            "subject_id": tenant_id,
            "rpm": row["rpm"],
            "tpm": row["tpm"],
            "concurrent": row["max_concurrent_runs"],
            "max_budget": _money(row["max_budget"], "max_budget")
            if row["max_budget"] is not None
            else None,
        }

    def set_quota(
        self,
        tenant_id: str,
        scope: str,
        subject_id: str,
        rpm: int | None,
        tpm: int | None,
        concurrent: int | None,
        max_budget: Any,
    ) -> dict:
        if scope not in ("tenant", "user", "model") or not subject_id:
            raise PlatformError(
                422, "invalid_quota", "Quota scope must be tenant, user or model"
            )
        if scope == "tenant" and subject_id != tenant_id:
            raise PlatformError(
                422, "invalid_quota", "Tenant quota subject must match current tenant"
            )
        for name, value in (("rpm", rpm), ("tpm", tpm), ("concurrent", concurrent)):
            if value is not None:
                _tokens(value, name)
        budget = _money(max_budget, "max_budget") if max_budget is not None else None
        values = {
            "tenant_id": tenant_id,
            "scope": scope,
            "subject_id": subject_id,
            "rpm": rpm,
            "tpm": tpm,
            "concurrent": concurrent,
            "max_budget": budget,
        }
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            hard_limits = self._entitlement_policy(conn, tenant_id)
            for name in ("rpm", "tpm", "concurrent", "max_budget"):
                requested, ceiling = values[name], hard_limits[name]
                if (
                    requested is not None
                    and ceiling is not None
                    and requested > ceiling
                ):
                    raise PlatformError(
                        422,
                        "entitlement_exceeded",
                        "组织内部配额不能超过平台套餐上限。",
                    )
            row = (
                conn.execute(
                    select(quota_policies).where(
                        quota_policies.c.tenant_id == tenant_id,
                        quota_policies.c.scope == scope,
                        quota_policies.c.subject_id == subject_id,
                    )
                )
                .mappings()
                .first()
            )
            quota_id = row["id"] if row else _id()
            if row:
                conn.execute(
                    quota_policies.update()
                    .where(quota_policies.c.id == quota_id)
                    .values(**values)
                )
            else:
                conn.execute(quota_policies.insert().values(id=quota_id, **values))
        return _serialize(dict(id=quota_id, **values))

    @staticmethod
    def _matches(row: dict, quota: dict) -> bool:
        return (
            quota["scope"] in ("tenant", "entitlement")
            or row[f"{quota['scope']}_id"] == quota["subject_id"]
        )

    def reserve(
        self,
        tenant_id: str,
        user_id: str,
        model_id: str,
        run_id: str,
        call_id: str,
        input_tokens: int,
        max_output_tokens: int,
        input_price: Any,
        output_price: Any,
        *,
        deployment_id: str | None = None,
        require_cost_policy: bool = False,
    ) -> dict:
        input_tokens = _tokens(input_tokens, "input_tokens")
        max_output_tokens = _tokens(max_output_tokens, "max_output_tokens")
        input_price, output_price = (
            _money(input_price, "input_price"),
            _money(output_price, "output_price"),
        )
        if not all((tenant_id, user_id, model_id, run_id, call_id)):
            raise PlatformError(
                422,
                "invalid_reservation",
                "All model call ownership identifiers are required",
            )
        amount = _cost(input_tokens, max_output_tokens, input_price, output_price)
        if amount > MAX_MONEY:
            raise PlatformError(
                422, "invalid_amount", "Reservation maximum amount exceeded"
            )
        fingerprint = _fingerprint(
            [
                user_id,
                model_id,
                run_id,
                input_tokens,
                max_output_tokens,
                format(input_price.normalize(), "f"),
                format(output_price.normalize(), "f"),
                deployment_id,
                require_cost_policy,
            ]
        )
        admitted_scopes = None
        try:
            with self.db.transaction(f"tenant:{tenant_id}") as conn:
                self._authorize(conn)
                previous = (
                    conn.execute(
                        select(reservations_table).where(
                            reservations_table.c.tenant_id == tenant_id,
                            reservations_table.c.call_id == call_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        raise PlatformError(
                            409,
                            "idempotency_conflict",
                            "Call ID is bound to another reservation",
                        )
                    return _serialize(previous)
                wallet = self._get_wallet(conn, tenant_id, create=True)
                if wallet["blocked"]:
                    raise PlatformError(
                        402, "billing_review_required", wallet["block_reason"]
                    )
                if wallet["balance"] - wallet["reserved"] < amount:
                    raise PlatformError(
                        402, "insufficient_balance", "Available balance is insufficient"
                    )
                policies = [self._entitlement_policy(conn, tenant_id)] + [
                    quota
                    for quota in self._quota_rows(conn, tenant_id)
                    if quota["scope"] == "tenant"
                    or (quota["scope"] == "user" and quota["subject_id"] == user_id)
                    or (quota["scope"] == "model" and quota["subject_id"] == model_id)
                ]
                from agent_platform.modules.costs import CostService

                cost_service = CostService(self.db)
                cutoff = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
                token_count = input_tokens + max_output_tokens
                for quota in policies:
                    filters = [reservations_table.c.tenant_id == tenant_id]
                    if quota["scope"] in {"user", "model"}:
                        filters.append(
                            reservations_table.c[quota["scope"] + "_id"]
                            == quota["subject_id"]
                        )
                    recent = conn.execute(
                        select(
                            func.count(),
                            func.coalesce(
                                func.sum(reservations_table.c.rate_tokens), 0
                            ),
                        ).where(
                            *filters,
                            reservations_table.c.created_at > cutoff,
                            reservations_table.c.status != "released",
                        )
                    ).one()
                    aggregate = func.exact_sum if self.db.sqlite else func.sum
                    concurrent_calls, held_amount = conn.execute(
                        select(
                            func.count().filter(
                                reservations_table.c.status == "reserved"
                            ),
                            aggregate(reservations_table.c.amount),
                        ).where(
                            *filters,
                            reservations_table.c.status.in_(FINANCIAL_HOLD_STATUSES),
                        )
                    ).one()
                    counters = {
                        "rpm": recent[0] + 1,
                        "tpm": recent[1] + token_count,
                        "concurrent": concurrent_calls + 1,
                    }
                    for name, count in counters.items():
                        limit = quota[name]
                        if limit is not None and (limit == 0 or count > limit):
                            raise PlatformError(
                                429,
                                f"{name}_limit",
                                f"{quota['scope']} {name} limit exceeded",
                            )
                    budget = quota["max_budget"]
                    committed = ZERO
                    if budget is not None:
                        committed = self._budget_spent(
                            conn, tenant_id, quota["scope"], quota["subject_id"]
                        )
                    reserved = Decimal(str(held_amount or 0))
                    if budget is not None and (
                        budget == ZERO or committed + reserved + amount > budget
                    ):
                        raise PlatformError(
                            402,
                            "budget_exceeded",
                            f"{quota['scope']} cumulative budget exceeded",
                        )
                rate_scopes = [
                    f"{quota['scope']}:{quota['subject_id']}" for quota in policies
                ]
                if self._limiter:
                    admitted_scopes = rate_scopes
                    self._limiter.admit(tenant_id, call_id, token_count, policies)
                now = _now()
                values = {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "model_id": model_id,
                    "run_id": run_id,
                    "call_id": call_id,
                    "fingerprint": fingerprint,
                    "amount": amount,
                    "input_price": input_price,
                    "output_price": output_price,
                    "estimated_input_tokens": input_tokens,
                    "max_output_tokens": max_output_tokens,
                    "input_tokens": None,
                    "output_tokens": None,
                    "rate_tokens": token_count,
                    "rate_scopes": _json(rate_scopes),
                    "status": "reserved",
                    "cost": ZERO,
                    "actual_cost": None,
                    "overage": ZERO,
                    "provider_cost": None,
                    "raw_usage": None,
                    "reason": "",
                    "created_at": now,
                    "updated_at": now,
                }
                conn.execute(reservations_table.insert().values(**values))
                cost_service.reserve(
                    conn,
                    tenant_id,
                    call_id,
                    deployment_id,
                    input_tokens,
                    max_output_tokens,
                    required=require_cost_policy,
                )
                conn.execute(
                    wallets.update()
                    .where(wallets.c.tenant_id == tenant_id)
                    .values(reserved=wallet["reserved"] + amount)
                )
            return _serialize(values)
        except BaseException:
            if admitted_scopes is not None:
                self._limiter.finish(
                    tenant_id, call_id, admitted_scopes, 0, rollback=True
                )
            raise

    @staticmethod
    def _get_reservation(conn, tenant_id, call_id):
        row = (
            conn.execute(
                select(reservations_table).where(
                    reservations_table.c.tenant_id == tenant_id,
                    reservations_table.c.call_id == call_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError(
                404, "reservation_not_found", "Model call reservation not found"
            )
        return dict(row)

    def _budget_spent(self, conn, tenant_id, scope, subject_id):
        scope, subject_id = (
            ("tenant", tenant_id) if scope == "entitlement" else (scope, subject_id)
        )
        row = (
            conn.execute(
                select(mt.budget_totals).where(
                    mt.budget_totals.c.tenant_id == tenant_id,
                    mt.budget_totals.c.scope == scope,
                    mt.budget_totals.c.subject_id == subject_id,
                )
            )
            .mappings()
            .first()
        )
        if row:
            return row["spent"]
        # One-time bootstrap for old records; the migration prebuilds existing
        # projections. New subjects start at zero and never scan on later calls.
        filters = [reservations_table.c.tenant_id == tenant_id]
        if scope in {"user", "model"}:
            filters.append(reservations_table.c[scope + "_id"] == subject_id)
        aggregate = func.exact_sum if self.db.sqlite else func.sum
        spent = Decimal(
            str(
                conn.scalar(
                    select(aggregate(reservations_table.c.cost)).where(*filters)
                )
                or 0
            )
        )
        conn.execute(
            mt.budget_totals.insert().values(
                tenant_id=tenant_id, scope=scope, subject_id=subject_id, spent=spent
            )
        )
        return spent

    def rebuild_budget_totals(self, tenant_id, actor_id, reason):
        """Administrative repair from immutable charge postings, under tenant lock."""
        self._require_manual_reason(actor_id, reason)
        with (
            self.db.tenant_scope(tenant_id),
            self.db.transaction(f"tenant:{tenant_id}") as conn,
        ):
            self._authorize(conn)
            rows = conn.execute(
                select(
                    ledger_transactions.c.call_id,
                    ledger_transactions.c.amount,
                    reservations_table.c.user_id,
                    reservations_table.c.model_id,
                )
                .select_from(
                    ledger_transactions.outerjoin(
                        reservations_table,
                        (
                            ledger_transactions.c.tenant_id
                            == reservations_table.c.tenant_id
                        )
                        & (
                            ledger_transactions.c.call_id
                            == reservations_table.c.call_id
                        ),
                    )
                )
                .where(
                    ledger_transactions.c.tenant_id == tenant_id,
                    ledger_transactions.c.type == "charge",
                )
            ).mappings()
            totals = {("tenant", tenant_id): ZERO}
            for row in rows:
                if not row["user_id"] or not row["model_id"] or row["amount"] > ZERO:
                    raise PlatformError(
                        503, "accounting_invariant", "账本与调用归属需人工核实。"
                    )
                for dimension in (
                    ("tenant", tenant_id),
                    ("user", row["user_id"]),
                    ("model", row["model_id"]),
                ):
                    totals[dimension] = totals.get(dimension, ZERO) - row["amount"]
            existing = {
                (row["scope"], row["subject_id"])
                for row in conn.execute(
                    select(mt.budget_totals).where(
                        mt.budget_totals.c.tenant_id == tenant_id
                    )
                ).mappings()
            }
            conn.execute(
                mt.budget_totals.update()
                .where(mt.budget_totals.c.tenant_id == tenant_id)
                .values(spent=ZERO)
            )
            for (scope, subject), spent in totals.items():
                if (scope, subject) in existing:
                    conn.execute(
                        mt.budget_totals.update()
                        .where(
                            mt.budget_totals.c.tenant_id == tenant_id,
                            mt.budget_totals.c.scope == scope,
                            mt.budget_totals.c.subject_id == subject,
                        )
                        .values(spent=spent)
                    )
                else:
                    conn.execute(
                        mt.budget_totals.insert().values(
                            tenant_id=tenant_id,
                            scope=scope,
                            subject_id=subject,
                            spent=spent,
                        )
                    )
            self._audit(
                conn,
                tenant_id,
                actor_id,
                "billing.budget.rebuild",
                tenant_id,
                {
                    "reason": reason,
                    "dimensions": len(totals),
                    "spent": format(totals[("tenant", tenant_id)], "f"),
                },
            )
            return {
                "tenant_id": tenant_id,
                "spent": format(totals[("tenant", tenant_id)], "f"),
                "dimensions": len(totals),
            }

    def _add_budget_spent(self, conn, row, charged):
        for scope, subject_id in [
            ("tenant", row["tenant_id"]),
            ("user", row["user_id"]),
            ("model", row["model_id"]),
        ]:
            previous = self._budget_spent(conn, row["tenant_id"], scope, subject_id)
            conn.execute(
                mt.budget_totals.update()
                .where(
                    mt.budget_totals.c.tenant_id == row["tenant_id"],
                    mt.budget_totals.c.scope == scope,
                    mt.budget_totals.c.subject_id == subject_id,
                )
                .values(spent=previous + charged)
            )

    def settle(
        self,
        tenant_id: str,
        call_id: str,
        input_tokens: int,
        output_tokens: int,
        provider_cost: Any = None,
        raw_usage: dict | None = None,
    ) -> dict:
        try:
            input_tokens = _tokens(input_tokens, "input_tokens")
            output_tokens = _tokens(output_tokens, "output_tokens")
            provider_cost = (
                _money(provider_cost, "provider_cost")
                if provider_cost is not None
                else None
            )
            if raw_usage is not None and not isinstance(raw_usage, dict):
                raise PlatformError(422, "invalid_usage", "Raw usage must be an object")
            raw = _json(raw_usage) if raw_usage is not None else None
        except (PlatformError, TypeError, ValueError) as error:
            self.unresolved(
                tenant_id, call_id, "Invalid usage evidence; manual review required"
            )
            if isinstance(error, PlatformError):
                raise
            raise PlatformError(
                422, "invalid_usage", "Usage evidence is not valid JSON"
            ) from None
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            previous = self._get_reservation(conn, tenant_id, call_id)
            conflict = previous["status"] == "settled" and (
                previous["input_tokens"],
                previous["output_tokens"],
            ) != (input_tokens, output_tokens)
            from agent_platform.modules.metering import record_receipt

            _, receipt_conflict = record_receipt(
                conn,
                tenant_id,
                call_id,
                input_tokens,
                output_tokens,
                provider_cost,
                raw_usage,
                conflict=conflict,
            )
            conflict = conflict or receipt_conflict
            row = (
                previous
                if conflict
                else self._settle_locked(
                    conn,
                    tenant_id,
                    call_id,
                    input_tokens,
                    output_tokens,
                    provider_cost,
                    raw,
                )
            )
            if not conflict:
                from agent_platform.modules.costs import CostService

                CostService(self.db).finalize(
                    conn,
                    tenant_id,
                    call_id,
                    action="settle",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    provider_cost=provider_cost,
                )
                if (
                    previous["status"] == "settled"
                    and provider_cost is not None
                    and previous["provider_cost"] is None
                ):
                    conn.execute(
                        reservations_table.update()
                        .where(reservations_table.c.id == previous["id"])
                        .values(provider_cost=provider_cost)
                    )
                    row["provider_cost"] = provider_cost
        if conflict:
            raise PlatformError(
                409, "settlement_conflict", "用量证据已保存，差异需核实；未重复收费。"
            )
        self._finish_rate(row, input_tokens + output_tokens)
        return _serialize(row)

    def _settle_locked(
        self,
        conn: Connection,
        tenant_id: str,
        call_id: str,
        input_tokens: int,
        output_tokens: int,
        provider_cost: Decimal | None,
        raw: str | None,
    ) -> dict:
        row = self._get_reservation(conn, tenant_id, call_id)
        if row["status"] == "settled":
            if (row["input_tokens"], row["output_tokens"]) != (
                input_tokens,
                output_tokens,
            ):
                raise PlatformError(
                    409,
                    "settlement_conflict",
                    "Confirmed usage cannot be overwritten",
                )
            return row
        if row["status"] in ("released", "written_off"):
            raise PlatformError(
                409,
                "reservation_released",
                "Released or written-off model calls cannot be charged",
            )
        actual = _cost(
            input_tokens, output_tokens, row["input_price"], row["output_price"]
        )
        charged = min(actual, row["amount"])
        overage = actual - charged
        wallet = self._get_wallet(conn, tenant_id)
        balance, reserved = (
            wallet["balance"] - charged,
            wallet["reserved"] - row["amount"],
        )
        if balance < ZERO or reserved < ZERO or balance < reserved:
            raise PlatformError(
                503, "accounting_invariant", "Wallet requires accounting review"
            )
        changes = {
            "status": "settled",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "rate_tokens": input_tokens + output_tokens,
            "cost": charged,
            "actual_cost": actual,
            "overage": overage,
            "provider_cost": provider_cost,
            "raw_usage": raw,
            "updated_at": _now(),
            "reason": "Actual fee exceeded reservation; excess borne by platform"
            if overage
            else "",
        }
        self._add_budget_spent(conn, row, charged)
        conn.execute(
            reservations_table.update()
            .where(reservations_table.c.id == row["id"])
            .values(**changes)
        )
        wallet_changes = {"balance": balance, "reserved": reserved}
        if overage:
            wallet_changes.update(
                blocked=True,
                block_reason="Usage exceeded its reserved price bound; administrator review required",
            )
            self._audit(
                conn,
                tenant_id,
                None,
                "billing.overage",
                call_id,
                {
                    "actual_cost": format(actual, "f"),
                    "charged": format(charged, "f"),
                    "platform_loss": format(overage, "f"),
                },
            )
        conn.execute(
            wallets.update()
            .where(wallets.c.tenant_id == tenant_id)
            .values(**wallet_changes)
        )
        self._transaction(
            conn,
            tenant_id=tenant_id,
            kind="charge",
            amount=-charged,
            balance=balance,
            description=f"Model call {call_id}",
            call_id=call_id,
            fingerprint=_fingerprint([call_id, input_tokens, output_tokens]),
        )
        row.update(changes)
        return row

    def _finish_rate(self, row: dict, token_count: int, rollback: bool = False):
        if self._limiter:
            self._limiter.finish(
                row["tenant_id"],
                row["call_id"],
                json.loads(row["rate_scopes"]),
                token_count,
                rollback=rollback,
            )

    def release(self, tenant_id: str, call_id: str, reason: str) -> dict:
        """Release only when the caller has proof that no upstream work was sent."""
        if not isinstance(reason, str) or not reason.strip():
            raise PlatformError(422, "reason_required", "A release reason is required")
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            row = self._get_reservation(conn, tenant_id, call_id)
            if row["status"] in ("released", "settled", "written_off"):
                return _serialize(row)
            if row["status"] == "unresolved":
                raise PlatformError(
                    409,
                    "unresolved_call",
                    "Unconfirmed calls require settlement or explicit reconciliation",
                )
            wallet = self._get_wallet(conn, tenant_id)
            from agent_platform.modules.costs import CostService

            CostService(self.db).finalize(conn, tenant_id, call_id, action="release")
            conn.execute(
                wallets.update()
                .where(wallets.c.tenant_id == tenant_id)
                .values(reserved=wallet["reserved"] - row["amount"])
            )
            row.update(
                status="released", rate_tokens=0, reason=reason, updated_at=_now()
            )
            conn.execute(
                reservations_table.update()
                .where(reservations_table.c.id == row["id"])
                .values(
                    status="released",
                    rate_tokens=0,
                    reason=reason,
                    updated_at=row["updated_at"],
                )
            )
        self._finish_rate(row, 0, rollback=True)
        return _serialize(row)

    def unresolved(self, tenant_id: str, call_id: str, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise PlatformError(
                422, "reason_required", "An unresolved reason is required"
            )
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            row = self._get_reservation(conn, tenant_id, call_id)
            if row["status"] != "reserved":
                return _serialize(row)
            row.update(status="unresolved", reason=reason, updated_at=_now())
            from agent_platform.modules.costs import CostService

            CostService(self.db).finalize(conn, tenant_id, call_id, action="unresolved")
            conn.execute(
                reservations_table.update()
                .where(reservations_table.c.id == row["id"])
                .values(
                    status="unresolved", reason=reason, updated_at=row["updated_at"]
                )
            )
            self._audit(
                conn, tenant_id, None, "billing.unresolved", call_id, {"reason": reason}
            )
        # Local execution ended. Keep the estimated rate usage and all money
        # frozen, but release the Redis execution slot after the SQL commit.
        self._finish_rate(row, row["rate_tokens"])
        return _serialize(row)

    def resolve(
        self,
        tenant_id: str,
        call_id: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        *,
        action: str,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> dict:
        """Audited manual confirmation or waiver of an unresolved model call.

        Confirmation is explicitly marked as manual evidence. A write-off releases
        held money without inventing a charge or refund; its rate estimate stays in
        the original minute because sending may already have consumed model work.
        """
        if action not in ("confirm", "write_off"):
            raise PlatformError(
                422,
                "invalid_resolution",
                "Resolution action must be confirm or write_off",
            )
        self._require_manual_reason(actor_id, reason)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise PlatformError(
                422, "idempotency_required", "A valid Idempotency-Key is required"
            )
        if action == "confirm":
            input_tokens = _tokens(input_tokens, "input_tokens")
            output_tokens = _tokens(output_tokens, "output_tokens")
        elif input_tokens is not None or output_tokens is not None:
            raise PlatformError(
                422,
                "invalid_resolution",
                "Write-off must not claim confirmed token usage",
            )
        fingerprint = _fingerprint(
            [call_id, actor_id, action, reason, input_tokens, output_tokens]
        )
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            previous = (
                conn.execute(
                    select(reconciliations).where(
                        reconciliations.c.tenant_id == tenant_id,
                        reconciliations.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise PlatformError(
                        409,
                        "idempotency_conflict",
                        "Idempotency-Key is bound to another reconciliation",
                    )
                return _serialize(self._get_reservation(conn, tenant_id, call_id))
            row = self._get_reservation(conn, tenant_id, call_id)
            if row["status"] != "unresolved":
                raise PlatformError(
                    409,
                    "resolution_not_pending",
                    "Only unresolved calls can be manually reconciled",
                )
            evidence = {
                "source": "manual_reconciliation",
                "actor_id": actor_id,
                "reason": reason,
                "action": action,
                "original_raw_usage": json.loads(row["raw_usage"])
                if row["raw_usage"]
                else None,
            }
            if action == "confirm":
                from agent_platform.modules.metering import record_receipt

                record_receipt(
                    conn,
                    tenant_id,
                    call_id,
                    input_tokens,
                    output_tokens,
                    row["provider_cost"],
                    evidence,
                )
                row = self._settle_locked(
                    conn,
                    tenant_id,
                    call_id,
                    input_tokens,
                    output_tokens,
                    row["provider_cost"],
                    _json(evidence),
                )
            else:
                wallet = self._get_wallet(conn, tenant_id)
                held = wallet["reserved"] - row["amount"]
                if held < ZERO:
                    raise PlatformError(
                        503, "accounting_invariant", "Wallet requires accounting review"
                    )
                conn.execute(
                    wallets.update()
                    .where(wallets.c.tenant_id == tenant_id)
                    .values(reserved=held)
                )
                changes = {
                    "status": "written_off",
                    "reason": reason,
                    "raw_usage": _json(evidence),
                    "updated_at": _now(),
                }
                conn.execute(
                    reservations_table.update()
                    .where(reservations_table.c.id == row["id"])
                    .values(**changes)
                )
                row.update(changes)
            from agent_platform.modules.costs import CostService

            CostService(self.db).finalize(
                conn,
                tenant_id,
                call_id,
                action="settle" if action == "confirm" else "write_off",
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                provider_cost=row["provider_cost"],
            )
            conn.execute(
                reconciliations.insert().values(
                    id=_id(),
                    tenant_id=tenant_id,
                    call_id=call_id,
                    actor_id=actor_id,
                    action=action,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    created_at=_now(),
                )
            )
            self._audit(
                conn,
                tenant_id,
                actor_id,
                f"billing.resolve.{action}",
                call_id,
                {
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                    "reserved_amount": format(row["amount"], "f"),
                    "charged_amount": format(row["cost"], "f"),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
        self._finish_rate(row, row["rate_tokens"])
        return _serialize(row)

    @staticmethod
    def _require_manual_reason(actor_id: str, reason: str) -> None:
        if not actor_id or not isinstance(reason, str) or not reason.strip():
            raise PlatformError(
                422,
                "audit_required",
                "Manual billing operations require an actor and a reason",
            )

    def unblock(self, tenant_id: str, actor_id: str, reason: str) -> dict:
        """Explicitly acknowledge recorded overages; never delete or rewrite them."""
        self._require_manual_reason(actor_id, reason)
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            self._authorize(conn)
            wallet = self._get_wallet(conn, tenant_id)
            if not wallet["blocked"]:
                return self._wallet_view(wallet)
            rows = list(
                conn.execute(
                    select(reservations_table).where(
                        reservations_table.c.tenant_id == tenant_id,
                    )
                ).mappings()
            )
            if any(row["status"] in FINANCIAL_HOLD_STATUSES for row in rows):
                raise PlatformError(
                    409,
                    "unresolved_billing",
                    "Reconcile unknown model calls before unblocking billing",
                )
            overages = [
                {"call_id": row["call_id"], "overage": format(row["overage"], "f")}
                for row in rows
                if row["overage"] > ZERO
            ]
            self._audit(
                conn,
                tenant_id,
                actor_id,
                "billing.unblock",
                tenant_id,
                {
                    "reason": reason,
                    "previous_block_reason": wallet["block_reason"],
                    "acknowledged_overages": overages,
                },
            )
            conn.execute(
                wallets.update()
                .where(wallets.c.tenant_id == tenant_id)
                .values(blocked=False, block_reason="")
            )
            wallet.update(blocked=False, block_reason="")
            return self._wallet_view(wallet)

    def ledger(self, tenant_id: str) -> list[dict]:
        with self.db.tenant_scope(tenant_id), self.db.read() as conn:
            return [
                _serialize(row)
                for row in conn.execute(
                    select(ledger_transactions)
                    .where(
                        ledger_transactions.c.tenant_id == tenant_id,
                    )
                    .order_by(ledger_transactions.c.created_at.desc())
                ).mappings()
            ]

    def reservations(self, tenant_id: str) -> list[dict]:
        with self.db.tenant_scope(tenant_id), self.db.read() as conn:
            return [
                _serialize(row)
                for row in conn.execute(
                    select(reservations_table)
                    .where(
                        reservations_table.c.tenant_id == tenant_id,
                    )
                    .order_by(reservations_table.c.created_at.desc())
                ).mappings()
            ]


# Each policy has a sliding 60-second request set, per-call token map and a short
# Redis concurrency lease. SQL counts reserved calls independently of the lease;
# unresolved calls retain financial holds and their original rate-window usage.
# All tenant keys share one hash slot; every dimension is checked before mutation.
_ADMIT_LUA = r"""
local now = tonumber(ARGV[1])
local call = ARGV[2]
local tokens = tonumber(ARGV[3])
local count = #KEYS / 3
for i = 1, count do
  local k = (i - 1) * 3
  local expired = redis.call('ZRANGEBYSCORE', KEYS[k + 1], '-inf', now - 60)
  for _, member in ipairs(expired) do redis.call('HDEL', KEYS[k + 2], member) end
  redis.call('ZREMRANGEBYSCORE', KEYS[k + 1], '-inf', now - 60)
  redis.call('ZREMRANGEBYSCORE', KEYS[k + 3], '-inf', now - 120)
  local limits = 3 + (i - 1) * 3
  local rpm = tonumber(ARGV[limits + 1])
  local tpm = tonumber(ARGV[limits + 2])
  local concurrent = tonumber(ARGV[limits + 3])
  local total = tokens
  for _, member in ipairs(redis.call('ZRANGE', KEYS[k + 1], 0, -1)) do
    total = total + tonumber(redis.call('HGET', KEYS[k + 2], member) or '0')
  end
  if rpm >= 0 and redis.call('ZCARD', KEYS[k + 1]) + 1 > rpm then return 'rpm_limit' end
  if tpm >= 0 and (tpm == 0 or total > tpm) then return 'tpm_limit' end
  if concurrent >= 0 and redis.call('ZCARD', KEYS[k + 3]) + 1 > concurrent then return 'concurrent_limit' end
end
for i = 1, count do
  local k = (i - 1) * 3
  redis.call('ZADD', KEYS[k + 1], now, call)
  redis.call('HSET', KEYS[k + 2], call, tokens)
  redis.call('ZADD', KEYS[k + 3], now, call)
  redis.call('EXPIRE', KEYS[k + 1], 180)
  redis.call('EXPIRE', KEYS[k + 2], 180)
  redis.call('EXPIRE', KEYS[k + 3], 180)
end
return 'ok'
"""
_FINISH_LUA = r"""
for i = 1, #KEYS, 3 do
  redis.call('ZREM', KEYS[i + 2], ARGV[1])
  if ARGV[3] == '1' then
    redis.call('ZREM', KEYS[i], ARGV[1])
    redis.call('HDEL', KEYS[i + 1], ARGV[1])
  elseif redis.call('ZSCORE', KEYS[i], ARGV[1]) then
    redis.call('HSET', KEYS[i + 1], ARGV[1], ARGV[2])
  end
end
return 'ok'
"""


class RedisLimiter:
    def __init__(self, url: str):
        import redis

        self.client = redis.Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True
        )

    @staticmethod
    def _keys(tenant_id: str, scopes: list[str]) -> list[str]:
        # Hash IDs prevent user-controlled braces from changing the Redis slot.
        tenant_hash = hashlib.sha256(tenant_id.encode()).hexdigest()
        keys = []
        for scope in scopes:
            scope_hash = hashlib.sha256(scope.encode()).hexdigest()
            base = f"agent-platform:rate:{{{tenant_hash}}}:{scope_hash}"
            keys.extend((f"{base}:requests", f"{base}:tokens", f"{base}:active"))
        return keys

    def admit(self, tenant_id: str, call_id: str, tokens: int, policies: list[dict]):
        scopes = [f"{q['scope']}:{q['subject_id']}" for q in policies]
        keys = self._keys(tenant_id, scopes)
        args = [datetime.now(UTC).timestamp(), call_id, tokens]
        for policy in policies:
            args.extend(
                -1 if policy[name] is None else policy[name]
                for name in ("rpm", "tpm", "concurrent")
            )
        try:
            result = self.client.eval(_ADMIT_LUA, len(keys), *keys, *args)
        except Exception as error:
            raise PlatformError(
                503, "rate_limiter_unavailable", "Shared rate limiter is unavailable"
            ) from error
        if result != "ok":
            raise PlatformError(429, str(result), "Shared model rate limit exceeded")

    def finish(
        self,
        tenant_id: str,
        call_id: str,
        scopes: list[str],
        tokens: int,
        rollback: bool = False,
    ):
        keys = self._keys(tenant_id, scopes)
        try:
            self.client.eval(
                _FINISH_LUA, len(keys), *keys, call_id, tokens, "1" if rollback else "0"
            )
        except Exception:
            # Money is already safely committed in SQL. Stale Redis reservations
            # only over-restrict and age out; SQL still protects financial holds
            # and the concurrency of calls whose execution has not ended.
            import logging

            logging.getLogger(__name__).warning(
                "Redis billing rate cleanup failed", exc_info=True
            )
