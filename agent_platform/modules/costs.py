"""Supplier cost budgets are independent of customer prices and gateway key generations."""

from sqlalchemy import select

from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import (
    ZERO,
    _cost,
    _id,
    _money,
    _now,
    _serialize,
)


class CostService:
    def __init__(self, db, *, authorize=None):
        self.db = db
        self.authorize = authorize

    def _authorize(self, connection):
        if self.authorize is None:
            raise PlatformError(403, "capability_required", "需要平台财务权限。")
        self.authorize(connection)

    def configure_budget(
        self, scope, subject_id, limit, actor_id, expected_version=None
    ):
        if scope not in {"platform", "deployment"} or not subject_id:
            raise PlatformError(422, "invalid_cost_budget", "成本预算范围无效。")
        if scope == "platform" and subject_id != "platform":
            raise PlatformError(422, "invalid_cost_budget", "平台成本预算标识无效。")
        limit = _money(limit, "cost_budget")
        with self.db.transaction("cost:configuration") as connection:
            self._authorize(connection)
            self.db.lock(connection, "cost:admission")
            row = self._budget(connection, scope, subject_id)
            if expected_version is not None and expected_version != (
                row["version"] if row else 0
            ):
                raise PlatformError(409, "version_conflict", "成本预算已变化。")
            values = {
                "scope": scope,
                "subject_id": subject_id,
                "limit": limit,
                "spent": row["spent"] if row else ZERO,
                "reserved": row["reserved"] if row else ZERO,
                "blocked": row["blocked"] if row else False,
                "version": row["version"] + 1 if row else 1,
                "updated_by": actor_id,
                "updated_at": _now(),
            }
            if row:
                connection.execute(
                    mt.cost_budgets.update()
                    .where(
                        mt.cost_budgets.c.scope == scope,
                        mt.cost_budgets.c.subject_id == subject_id,
                    )
                    .values(**values)
                )
            else:
                connection.execute(mt.cost_budgets.insert().values(**values))
            self._audit(connection, actor_id, "cost.budget", subject_id, values)
            return _serialize(values)

    def configure_price(
        self, deployment_id, input_price, output_price, actor_id, expected_version=None
    ):
        from agent_platform.infrastructure.gateway_tables import deployments

        input_price, output_price = (
            _money(input_price, "input_price"),
            _money(output_price, "output_price"),
        )
        with self.db.transaction("cost:configuration") as connection:
            self._authorize(connection)
            self.db.lock(connection, "cost:admission")
            if not connection.scalar(
                select(deployments.c.id).where(deployments.c.id == deployment_id)
            ):
                raise PlatformError(404, "deployment_not_found", "模型部署不存在。")
            previous = self._price(connection, deployment_id)
            version = previous["version"] if previous else 0
            if expected_version is not None and version != expected_version:
                raise PlatformError(409, "version_conflict", "成本价格已变化。")
            values = {
                "id": _id(),
                "deployment_id": deployment_id,
                "version": version + 1,
                "input_price": input_price,
                "output_price": output_price,
                "currency": "USD",
                "created_by": actor_id,
                "created_at": _now(),
            }
            connection.execute(mt.cost_prices.insert().values(**values))
            self._audit(connection, actor_id, "cost.price", deployment_id, values)
            return _serialize(values)

    @staticmethod
    def _audit(connection, actor_id, action, target, values):
        from agent_platform.infrastructure.tenancy_tables import operation_audits

        connection.execute(
            operation_audits.insert().values(
                id=_id(),
                actor_user_id=actor_id,
                tenant_id=None,
                action=action,
                target=target,
                reason="供应商成本控制配置",
                details=_serialize(values),
                created_at=_now(),
            )
        )

    @staticmethod
    def _price(connection, deployment_id):
        return (
            connection.execute(
                select(mt.cost_prices)
                .where(
                    mt.cost_prices.c.deployment_id == deployment_id,
                )
                .order_by(mt.cost_prices.c.version.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _budget(connection, scope, subject_id):
        return (
            connection.execute(
                select(mt.cost_budgets).where(
                    mt.cost_budgets.c.scope == scope,
                    mt.cost_budgets.c.subject_id == subject_id,
                )
            )
            .mappings()
            .first()
        )

    def reserve(
        self,
        connection,
        tenant_id,
        call_id,
        deployment_id,
        input_tokens,
        max_output_tokens,
        *,
        required=False,
    ):
        self.db.lock(connection, "cost:admission")
        price = self._price(connection, deployment_id) if deployment_id else None
        global_budget = self._budget(connection, "platform", "platform")
        deployment_budget = (
            self._budget(connection, "deployment", deployment_id)
            if deployment_id
            else None
        )
        if not price:
            if required or global_budget or deployment_budget:
                raise PlatformError(
                    503, "cost_policy_missing", "模型尚未配置可信成本上界。"
                )
            return None
        if required and not global_budget:
            raise PlatformError(503, "cost_budget_missing", "平台成本预算尚未配置。")
        amount = _cost(
            input_tokens, max_output_tokens, price["input_price"], price["output_price"]
        )
        budgets = [dict(row) for row in (global_budget, deployment_budget) if row]
        if not budgets:
            raise PlatformError(503, "cost_budget_missing", "供应商成本预算尚未配置。")
        for row in budgets:
            if row["blocked"] or row["spent"] + row["reserved"] + amount > row["limit"]:
                raise PlatformError(
                    402, "provider_budget_exceeded", "供应商成本预算不足或需要核实。"
                )
        stamp = _now()
        values = {
            "id": _id(),
            "tenant_id": tenant_id,
            "call_id": call_id,
            "deployment_id": deployment_id,
            "price_version_id": price["id"],
            "input_price": price["input_price"],
            "output_price": price["output_price"],
            "amount": amount,
            "cost": None,
            "quality": "reserved_bound",
            "status": "reserved",
            "budgets": [[row["scope"], row["subject_id"]] for row in budgets],
            "created_at": stamp,
            "updated_at": stamp,
        }
        connection.execute(mt.cost_holds.insert().values(**values))
        for row in budgets:
            self._update_budget(connection, row, reserved=row["reserved"] + amount)
        return values

    def reserve_fixed(self, connection, tenant_id, call_id, amount):
        """The administrator-published remote quote bounds supplier expense."""
        amount = _money(amount)
        self.db.lock(connection, "cost:admission")
        budget = self._budget(connection, "platform", "platform")
        if not budget:
            raise PlatformError(
                503, "cost_budget_missing", "远程调用需要平台成本预算。"
            )
        if (
            budget["blocked"]
            or budget["spent"] + budget["reserved"] + amount > budget["limit"]
        ):
            raise PlatformError(
                402, "provider_budget_exceeded", "供应商成本预算不足或需要核实。"
            )
        stamp = _now()
        values = {
            "id": _id(),
            "tenant_id": tenant_id,
            "call_id": call_id,
            "deployment_id": None,
            "price_version_id": None,
            "charge_kind": "fixed",
            "input_price": ZERO,
            "output_price": ZERO,
            "amount": amount,
            "cost": None,
            "quality": "remote_quote_bound",
            "status": "reserved",
            "budgets": [["platform", "platform"]],
            "created_at": stamp,
            "updated_at": stamp,
        }
        connection.execute(mt.cost_holds.insert().values(**values))
        self._update_budget(connection, budget, reserved=budget["reserved"] + amount)
        return values

    @staticmethod
    def _update_budget(connection, row, **changes):
        connection.execute(
            mt.cost_budgets.update()
            .where(
                mt.cost_budgets.c.scope == row["scope"],
                mt.cost_budgets.c.subject_id == row["subject_id"],
            )
            .values(**{"updated_at": _now(), **changes})
        )

    def finalize(
        self,
        connection,
        tenant_id,
        call_id,
        *,
        action,
        input_tokens=0,
        output_tokens=0,
        provider_cost=None,
    ):
        row = (
            connection.execute(
                select(mt.cost_holds).where(
                    mt.cost_holds.c.tenant_id == tenant_id,
                    mt.cost_holds.c.call_id == call_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            return
        self.db.lock(connection, "cost:admission")
        if row["status"] in {"released", "written_off"}:
            return
        if action == "unresolved":
            if row["status"] == "settled":
                return
            connection.execute(
                mt.cost_holds.update()
                .where(mt.cost_holds.c.id == row["id"])
                .values(status="unresolved", updated_at=_now())
            )
            return
        previous = row["cost"] or ZERO
        if action == "release":
            if row["status"] == "settled":
                return
            cost, status, quality = ZERO, "released", "not_sent"
        elif action == "write_off":
            cost, status, quality = row["amount"], "written_off", "written_off_bound"
        else:
            if row["charge_kind"] == "fixed" and provider_cost is None:
                raise PlatformError(
                    409, "remote_fee_missing", "远程成本需要最终金额证据。"
                )
            cost = (
                _money(provider_cost, "provider_cost")
                if provider_cost is not None
                else _cost(
                    input_tokens, output_tokens, row["input_price"], row["output_price"]
                )
            )
            status, quality = (
                "settled",
                "remote_final"
                if row["charge_kind"] == "fixed"
                else "gateway_estimate"
                if provider_cost is not None
                else "price_estimate",
            )
            if row["status"] == "settled" and provider_cost is None:
                return
        held = ZERO if row["status"] == "settled" else row["amount"]
        for scope, subject_id in row["budgets"]:
            budget = self._budget(connection, scope, subject_id)
            reserved, spent = (
                budget["reserved"] - held,
                budget["spent"] - previous + cost,
            )
            if reserved < ZERO or spent < ZERO:
                raise PlatformError(
                    503, "cost_accounting_invariant", "成本预算需核实。"
                )
            self._update_budget(
                connection,
                budget,
                reserved=reserved,
                spent=spent,
                blocked=budget["blocked"]
                or cost > row["amount"]
                or spent + reserved > budget["limit"],
            )
        connection.execute(
            mt.cost_holds.update()
            .where(mt.cost_holds.c.id == row["id"])
            .values(cost=cost, status=status, quality=quality, updated_at=_now())
        )

    def review_budget(self, scope, subject_id, actor_id, reason, expected_version):
        """Rebuild cost projection and acknowledge overages without changing evidence."""
        from agent_platform.infrastructure import tables as t
        from agent_platform.modules.billing.service import BillingService

        BillingService._require_manual_reason(actor_id, reason)
        with self.db.transaction("cost:configuration") as connection:
            self._authorize(connection)
            self.db.lock(connection, "cost:admission")
            budget = self._budget(connection, scope, subject_id)
            if not budget:
                raise PlatformError(404, "cost_budget_not_found", "成本预算不存在。")
            if expected_version != budget["version"]:
                raise PlatformError(409, "version_conflict", "成本预算已变化。")
            spent, reserved = ZERO, ZERO
            unknown = False
            # All cost hold mutations require this cost lock; no tenant locks are
            # acquired in reverse order. FORCE RLS remains enabled for each read.
            tenants = connection.scalars(select(t.tenants.c.id)).all()
            for tenant in tenants:
                self.db.set_tenant(connection, tenant)
                for hold in connection.execute(
                    select(mt.cost_holds).where(mt.cost_holds.c.tenant_id == tenant)
                ).mappings():
                    if [scope, subject_id] not in hold["budgets"]:
                        continue
                    if hold["status"] in {"reserved", "unresolved"}:
                        reserved += hold["amount"]
                        unknown = True
                    elif hold["status"] in {"settled", "written_off"}:
                        spent += hold["cost"] or ZERO
            if unknown:
                raise PlatformError(
                    409, "unresolved_cost", "成本预占需先完成核实或人工处置。"
                )
            if spent + reserved > budget["limit"]:
                raise PlatformError(
                    409, "cost_budget_exceeded", "已发生的成本超过当前预算。"
                )
            values = {
                "spent": spent,
                "reserved": reserved,
                "blocked": False,
                "version": budget["version"] + 1,
                "updated_by": actor_id,
                "updated_at": _now(),
            }
            self._update_budget(connection, budget, **values)
            self._audit(
                connection,
                actor_id,
                "cost.budget.review",
                subject_id,
                {"reason": reason, "previous": _serialize(budget), **values},
            )
            return _serialize({**budget, **values})

    def summary(self):
        with self.db.read() as connection:
            self._authorize(connection)
            return {
                "budgets": [
                    _serialize(row)
                    for row in connection.execute(select(mt.cost_budgets)).mappings()
                ],
                "prices": [
                    _serialize(row)
                    for row in connection.execute(
                        select(mt.cost_prices).order_by(
                            mt.cost_prices.c.deployment_id,
                            mt.cost_prices.c.version.desc(),
                        )
                    ).mappings()
                ],
            }
