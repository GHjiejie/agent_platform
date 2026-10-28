"""Explicit tenant context and independently authorized platform operations."""

# ruff: noqa: B008
from fastapi import Depends, Header
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from agent_platform.apps.api import responses as r
from agent_platform.apps.api import schemas as s
from agent_platform.infrastructure import (
    gateway_tables as gt,
)
from agent_platform.infrastructure import (
    tables as t,
)
from agent_platform.infrastructure import (
    tenancy_tables as nt,
)
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService
from agent_platform.modules.platform import hasher, now, uid


def register(app, platform, principal, auth, idempotency):
    db, settings = platform.db, platform.settings
    identity = platform.identity

    def cost_service(actor, capability="platform.billing.manage"):
        from agent_platform.modules.costs import CostService

        return CostService(
            db,
            authorize=lambda connection: platform.require_platform(
                connection, actor, capability
            ),
        )

    @app.get("/api/v2/platform/costs", response_model=r.CostsResponse)
    def costs(actor=Depends(principal)):
        return cost_service(actor, "platform.costs.read").summary()

    @app.put(
        "/api/v2/platform/costs/budgets/{scope}/{subject_id}",
        response_model=r.CostBudgetResponse,
    )
    def cost_budget(
        scope: str, subject_id: str, body: s.CostBudget, actor=Depends(principal)
    ):
        return cost_service(actor).configure_budget(
            scope, subject_id, body.limit, actor["id"], body.expected_version
        )

    @app.post(
        "/api/v2/platform/costs/budgets/{scope}/{subject_id}/review",
        response_model=r.CostBudgetResponse,
    )
    def cost_review(
        scope: str, subject_id: str, body: s.CostReview, actor=Depends(principal)
    ):
        return cost_service(actor).review_budget(
            scope, subject_id, actor["id"], body.reason, body.expected_version
        )

    @app.post(
        "/api/v2/platform/tenants/{tenant_id}/billing/budget-projection/rebuild",
        response_model=r.BudgetRebuildResponse,
    )
    def rebuild_budgets(
        tenant_id: str, body: s.ProjectionRebuild, actor=Depends(principal)
    ):
        with db.tenant_scope(tenant_id):
            return billing(actor, tenant_id).rebuild_budget_totals(
                tenant_id, actor["id"], body.reason
            )

    @app.post(
        "/api/v2/platform/deployments/{deployment_id}/cost-prices",
        status_code=201,
        response_model=r.CostPriceResponse,
    )
    def cost_price(deployment_id: str, body: s.CostPrice, actor=Depends(principal)):
        return cost_service(actor).configure_price(
            deployment_id,
            body.input_price,
            body.output_price,
            actor["id"],
            body.expected_version,
        )

    @app.patch("/api/v2/platform/deployments/{deployment_id}")
    def deployment_update(
        deployment_id: str, body: s.DeploymentPatch, actor=Depends(principal)
    ):
        return platform.gateway.update_deployment(
            deployment_id, body.model_dump(exclude_none=True), actor_id=actor["id"]
        )

    @app.get(
        "/api/v2/platform/tenants/{tenant_id}/billing/calls/{call_id}/receipts",
        response_model=r.UsageReceiptsResponse,
    )
    def usage_receipts(tenant_id: str, call_id: str, actor=Depends(principal)):
        from agent_platform.infrastructure import metering_tables as mt

        with db.tenant_scope(tenant_id), db.read() as connection:
            require_target(connection, actor, tenant_id, "platform.billing.manage")
            return {
                "items": [
                    dict(row)
                    for row in connection.execute(
                        select(mt.usage_receipts)
                        .where(
                            mt.usage_receipts.c.tenant_id == tenant_id,
                            mt.usage_receipts.c.call_id == call_id,
                        )
                        .order_by(mt.usage_receipts.c.created_at)
                    ).mappings()
                ]
            }

    def require(actor, capability):
        with db.read() as connection:
            platform.require_platform(connection, actor, capability)

    def require_target(connection, actor, tenant_id, capability):
        platform.require_platform(connection, actor, capability)
        if not connection.scalar(
            select(t.tenants.c.id).where(t.tenants.c.id == tenant_id)
        ):
            raise PlatformError(404, "not_found", "组织不存在。")

    def target_model_id(actor, tenant_id, alias):
        with db.tenant_scope(tenant_id), db.read() as connection:
            require_target(connection, actor, tenant_id, "platform.models.manage")
            platform.require_platform(connection, actor, "platform.pricing.manage")
            return connection.scalar(
                select(t.models.c.id).where(
                    t.models.c.tenant_id == tenant_id, t.models.c.alias == alias
                )
            )

    def billing(actor, tenant_id):
        def authorize(connection):
            require_target(connection, actor, tenant_id, "platform.billing.manage")

        return BillingService(db, settings.redis_url, authorize=authorize)

    def grant_model(actor, tenant_id, deployment_id, values):
        try:
            with db.tenant_scope(tenant_id):
                return platform.gateway.grant_model(
                    tenant_id, deployment_id, values, actor_id=actor["id"]
                )
        except IntegrityError:
            raise PlatformError(
                409, "model_configuration_conflict", "模型配置已变化，请刷新后重试。"
            ) from None

    @app.get("/api/v2/platform/tenants/{tenant_id}/diagnostics")
    def diagnostics(tenant_id: str, actor=Depends(principal)):
        from agent_platform.modules.operational_metrics import operational_snapshot

        def authorize(connection):
            require_target(connection, actor, tenant_id, "platform.tenants.manage")
            platform.require_platform(connection, actor, "platform.costs.read")

        return operational_snapshot(db, tenant_id, authorize=authorize)

    @app.get("/api/v2/tenants/{tenant_id}")
    def tenant(tenant_id: str, actor=Depends(principal)):
        user = identity.context(actor["id"], tenant_id, allow_suspended=True)
        with db.tenant_scope(tenant_id), db.read() as connection:
            tenant = dict(
                connection.execute(
                    select(
                        t.tenants,
                        nt.tenant_settings.c.status,
                        nt.tenant_settings.c.version,
                    )
                    .join(
                        nt.tenant_settings,
                        t.tenants.c.id == nt.tenant_settings.c.tenant_id,
                    )
                    .where(t.tenants.c.id == tenant_id)
                )
                .mappings()
                .one()
            )
        return {"tenant": tenant, "user": user}

    @app.get("/api/v2/tenants/{tenant_id}/memberships")
    def members(user=Depends(auth)):
        return {"items": identity.members(user)}

    @app.patch("/api/v2/tenants/{tenant_id}/memberships/{membership_id}")
    def member_update(membership_id: str, body: s.MembershipPatch, user=Depends(auth)):
        values = body.model_dump(exclude_none=True, exclude={"expected_version"})
        return identity.update_member(
            user, membership_id, values, body.expected_version
        )

    @app.get("/api/v2/tenants/{tenant_id}/invitations")
    def invitations(user=Depends(auth)):
        return {"items": identity.list_invitations(user)}

    @app.post("/api/v2/tenants/{tenant_id}/invitations", status_code=201)
    def invite(body: s.InvitationCreate, user=Depends(auth)):
        return identity.create_invitation(user, **body.model_dump())

    @app.post("/api/v2/invitations/accept")
    def accept(body: s.InvitationAccept, actor=Depends(principal)):
        return identity.accept_invitation(actor, body.token)

    @app.post("/api/v2/tenants/{tenant_id}/ownership-transfer")
    def transfer(body: s.OwnershipTransfer, user=Depends(auth)):
        return identity.transfer_owner(user, body.membership_id, body.expected_version)

    @app.get("/api/v2/tenants/{tenant_id}/entitlements")
    def limits(user=Depends(auth)):
        return identity.get_entitlements(user)

    @app.get("/api/v2/tenants/{tenant_id}/model-policy")
    def model_policy(user=Depends(auth)):
        return identity.model_policy(user)

    @app.put("/api/v2/tenants/{tenant_id}/model-policy")
    def model_policy_update(body: s.ModelPolicy, user=Depends(auth)):
        return identity.set_model_policy(user, **body.model_dump())

    @app.get("/api/v2/platform/tenants")
    def tenants(actor=Depends(principal)):
        return {"items": identity.list_tenants(actor)}

    @app.post("/api/v2/platform/users", status_code=201)
    def create_global_user(body: s.PlatformUserCreate, actor=Depends(principal)):
        """Provision a login identity; only accepting an invitation grants membership."""
        email, name = body.email.strip().lower(), body.name.strip()
        if not name:
            raise PlatformError(422, "invalid_name", "姓名不能为空。")
        with db.transaction("identity:provision") as connection:
            platform.require_platform(connection, actor, "platform.tenants.manage")
            if connection.scalar(select(t.users.c.id).where(t.users.c.email == email)):
                raise PlatformError(
                    409, "identity_exists", "此邮箱已有账号，可直接使用原账号接受邀请。"
                )
            # Compatibility only: this reference grants no membership or authority.
            home = connection.scalar(
                select(t.tenants.c.id).order_by(t.tenants.c.created_at).limit(1)
            )
            if not home:
                raise PlatformError(409, "platform_not_initialized", "请先初始化平台。")
            user = {"id": uid(), "email": email, "name": name, "active": True}
            try:
                connection.execute(
                    insert(t.users).values(
                        **user,
                        tenant_id=home,
                        password_hash=hasher.hash(body.password),
                        role="member",
                        global_status="active",
                        auth_version=1,
                        created_at=now(),
                    )
                )
            except IntegrityError:
                raise PlatformError(
                    409, "identity_exists", "此邮箱已有账号。"
                ) from None
            identity._audit(
                connection,
                actor["id"],
                "identity.provision",
                user["id"],
                details={"email": email},
            )
            return user

    @app.post("/api/v2/platform/tenants", status_code=201)
    def create_tenant(
        body: s.TenantCreate,
        actor=Depends(principal),
        idempotency_key: str | None = Header(default=None),
    ):
        key = idempotency(idempotency_key)
        email = body.owner_email.lower().strip()
        require(actor, "platform.tenants.manage")
        with db.transaction("identity:provision") as connection:
            platform.require_platform(connection, actor, "platform.tenants.manage")
            owner = (
                connection.execute(select(t.users).where(t.users.c.email == email))
                .mappings()
                .first()
            )
            if not owner:
                if not body.owner_password:
                    raise PlatformError(
                        400, "owner_password_required", "新账号需要至少 12 位初始密码。"
                    )
                # Legacy home reference is retained only for schema compatibility.
                # Memberships are the sole authority for all tenant access.
                home = connection.scalar(
                    select(t.tenants.c.id).order_by(t.tenants.c.created_at).limit(1)
                )
                owner_id = uid()
                try:
                    connection.execute(
                        insert(t.users).values(
                            id=owner_id,
                            tenant_id=home,
                            email=email,
                            name=body.owner_name or email.split("@")[0],
                            password_hash=hasher.hash(body.owner_password),
                            role="member",
                            active=True,
                            created_at=now(),
                        )
                    )
                except IntegrityError:
                    raise PlatformError(
                        409, "identity_conflict", "账号已创建，请重试。"
                    ) from None
            else:
                owner_id = owner["id"]
                if not owner["active"] or owner["global_status"] != "active":
                    raise PlatformError(409, "owner_inactive", "组织所有者账号已停用。")
        return identity.create_tenant(
            actor, name=body.name, owner_user_id=owner_id, idempotency_key=key
        )

    @app.post("/api/v2/platform/tenants/{tenant_id}/suspend")
    def suspend(tenant_id: str, body: s.StatusChange, actor=Depends(principal)):
        with db.tenant_scope(tenant_id):
            return identity.set_tenant_status(
                actor, tenant_id, "suspended", body.reason, body.expected_version
            )

    @app.post("/api/v2/platform/tenants/{tenant_id}/resume")
    def resume(tenant_id: str, body: s.StatusChange, actor=Depends(principal)):
        with db.tenant_scope(tenant_id):
            return identity.set_tenant_status(
                actor, tenant_id, "active", body.reason, body.expected_version
            )

    @app.get("/api/v2/platform/tenants/{tenant_id}/entitlements")
    def platform_limits(tenant_id: str, actor=Depends(principal)):
        with db.tenant_scope(tenant_id):
            return identity.get_entitlements(actor, tenant_id)

    @app.put("/api/v2/platform/tenants/{tenant_id}/entitlements")
    def platform_limit_update(
        tenant_id: str, body: s.EntitlementPatch, actor=Depends(principal)
    ):
        values = body.model_dump(exclude_none=True, exclude={"expected_version"})
        concurrency = {
            values[key]
            for key in ("max_running_runs", "max_concurrent_runs", "concurrent")
            if key in values
        }
        if len(concurrency) > 1:
            raise PlatformError(
                422, "conflicting_limits", "运行并发别名不能指定不同值。"
            )
        for alias in ("max_running_runs", "concurrent"):
            if alias in values:
                values["max_concurrent_runs"] = values.pop(alias)
        if "max_budget" in body.model_fields_set:
            values["max_budget"] = body.max_budget
        return identity.set_entitlements(
            actor, tenant_id, values, body.expected_version
        )

    @app.post("/api/v2/platform/tenants/{tenant_id}/credits")
    def credit(
        tenant_id: str,
        body: s.Credit,
        actor=Depends(principal),
        idempotency_key: str | None = Header(default=None),
    ):
        with db.tenant_scope(tenant_id):
            return billing(actor, tenant_id).credit(
                tenant_id,
                actor["id"],
                body.amount,
                body.description,
                idempotency(idempotency_key),
            )

    @app.get(
        "/api/v2/platform/tenants/{tenant_id}/billing/wallet",
        response_model=r.WalletResponse,
    )
    def platform_wallet(tenant_id: str, actor=Depends(principal)):
        with db.tenant_scope(tenant_id), db.read() as connection:
            require_target(connection, actor, tenant_id, "platform.billing.manage")
        return billing(actor, tenant_id).wallet(tenant_id)

    @app.get("/api/v2/platform/tenants/{tenant_id}/billing/reservations")
    def reservations(tenant_id: str, actor=Depends(principal)):
        with db.tenant_scope(tenant_id), db.read() as connection:
            require_target(connection, actor, tenant_id, "platform.billing.manage")
        return {"items": billing(actor, tenant_id).reservations(tenant_id)}

    @app.post(
        "/api/v2/platform/tenants/{tenant_id}/billing/reservations/{call_id}/resolve"
    )
    def resolve(
        tenant_id: str,
        call_id: str,
        body: s.Reconcile,
        actor=Depends(principal),
        idempotency_key: str | None = Header(default=None),
    ):
        from agent_platform.apps.worker.main import sync_call_receipt

        with db.tenant_scope(tenant_id):
            receipt = billing(actor, tenant_id).resolve(
                tenant_id,
                call_id,
                actor_id=actor["id"],
                idempotency_key=idempotency(idempotency_key),
                **body.model_dump(),
            )
            sync_call_receipt(platform, receipt)
            return receipt

    @app.post("/api/v2/platform/tenants/{tenant_id}/billing/unblock")
    def unblock(tenant_id: str, body: s.Unblock, actor=Depends(principal)):
        with db.tenant_scope(tenant_id):
            return billing(actor, tenant_id).unblock(
                tenant_id, actor["id"], body.reason
            )

    @app.get("/api/v2/platform/models")
    def platform_models(actor=Depends(principal)):
        require(actor, "platform.models.manage")
        result = []
        with db.read() as connection:
            tenant_ids = connection.scalars(select(t.tenants.c.id)).all()
        # Platform catalog access remains scoped for every source organization.
        for tenant_id in tenant_ids:
            with db.tenant_scope(tenant_id), db.read() as connection:
                platform.require_platform(connection, actor, "platform.models.manage")
                result.extend(
                    dict(row)
                    for row in connection.execute(
                        select(t.models)
                        .join(
                            gt.model_bindings,
                            (t.models.c.tenant_id == gt.model_bindings.c.tenant_id)
                            & (t.models.c.id == gt.model_bindings.c.model_id),
                        )
                        .join(
                            gt.deployments,
                            gt.deployments.c.id == gt.model_bindings.c.deployment_id,
                        )
                        .where(
                            t.models.c.tenant_id == tenant_id,
                            gt.deployments.c.owner_scope == "platform",
                            gt.deployments.c.status == "active",
                            gt.model_bindings.c.enabled.is_(True),
                            t.models.c.active.is_(True),
                        )
                    ).mappings()
                )
        return {"items": result}

    @app.get("/api/v2/platform/tenants/{tenant_id}/model-grants")
    def model_grants(tenant_id: str, actor=Depends(principal)):
        with db.tenant_scope(tenant_id), db.read() as connection:
            require_target(connection, actor, tenant_id, "platform.models.manage")
            binding = (
                connection.execute(
                    select(
                        gt.bindings.c.status,
                        gt.bindings.c.desired_version,
                        gt.bindings.c.applied_version,
                    ).where(
                        gt.bindings.c.tenant_id == tenant_id,
                        gt.bindings.c.gateway_id == "primary",
                        gt.bindings.c.purpose == "inference",
                    )
                )
                .mappings()
                .first()
            )
            sync = (
                dict(binding)
                if binding
                else {
                    "status": "not_enrolled",
                    "desired_version": 0,
                    "applied_version": 0,
                }
            )
            rows = connection.execute(
                select(
                    t.models,
                    gt.model_bindings.c.deployment_id,
                    gt.model_bindings.c.enabled,
                    gt.model_bindings.c.policy_version,
                    gt.model_bindings.c.price_version_id,
                )
                .join(
                    gt.model_bindings,
                    (t.models.c.tenant_id == gt.model_bindings.c.tenant_id)
                    & (t.models.c.id == gt.model_bindings.c.model_id),
                )
                .where(t.models.c.tenant_id == tenant_id)
                .order_by(t.models.c.created_at, t.models.c.id)
            ).mappings()
            return {
                "items": [
                    {
                        **dict(row),
                        "sync_status": platform.gateway.availability(
                            tenant_id, dict(row), connection=connection
                        )["status"],
                    }
                    for row in rows
                ],
                "gateway": sync,
            }

    @app.get("/api/v2/platform/token-defaults")
    def platform_token_defaults(actor=Depends(principal)):
        require(actor, "platform.models.manage")
        return {
            "output_tokens": platform.settings.default_output_tokens,
            "context_window": platform.settings.default_context_window,
        }

    @app.post("/api/v2/platform/tenants/{tenant_id}/models", status_code=201)
    def create_tenant_model(
        tenant_id: str, body: s.PlatformModelCreate, actor=Depends(principal)
    ):
        require(actor, "platform.models.manage")
        require(actor, "platform.pricing.manage")
        values = body.model_dump(exclude={"deployment_id"})
        if "context_window" not in body.model_fields_set:
            values["context_window"] = platform.settings.default_context_window
        if "max_output_tokens" not in body.model_fields_set:
            values["max_output_tokens"] = platform.settings.default_output_tokens
        existing_id = target_model_id(actor, tenant_id, body.alias)
        if existing_id:
            values["model_id"] = existing_id
        return grant_model(actor, tenant_id, body.deployment_id, values)

    @app.post("/api/v2/platform/tenants/{tenant_id}/model-grants", status_code=201)
    def grant(tenant_id: str, body: s.ModelGrant, actor=Depends(principal)):
        require(actor, "platform.models.manage")
        require(actor, "platform.pricing.manage")
        source = None
        with db.read() as connection:
            tenant_ids = connection.scalars(select(t.tenants.c.id)).all()
        for source_tenant in tenant_ids:
            with db.tenant_scope(source_tenant), db.read() as connection:
                platform.require_platform(connection, actor, "platform.models.manage")
                row = (
                    connection.execute(
                        select(t.models, gt.model_bindings.c.deployment_id)
                        .join(
                            gt.model_bindings,
                            (t.models.c.tenant_id == gt.model_bindings.c.tenant_id)
                            & (t.models.c.id == gt.model_bindings.c.model_id),
                        )
                        .join(
                            gt.deployments,
                            gt.deployments.c.id == gt.model_bindings.c.deployment_id,
                        )
                        .where(
                            t.models.c.id == body.source_model_id,
                            t.models.c.tenant_id == source_tenant,
                            t.models.c.active.is_(True),
                            gt.model_bindings.c.enabled.is_(True),
                            gt.deployments.c.owner_scope == "platform",
                            gt.deployments.c.status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if row:
                    source = dict(row)
                    break
        if not source:
            raise PlatformError(404, "not_found", "可授予的平台模型不存在。")
        values = {
            key: source[key]
            for key in (
                "name",
                "alias",
                "input_price",
                "output_price",
                "context_window",
                "max_output_tokens",
            )
        }
        existing_id = target_model_id(actor, tenant_id, values["alias"])
        if existing_id:
            values["model_id"] = existing_id
        return grant_model(actor, tenant_id, source["deployment_id"], values)

    @app.get("/api/v2/platform/gateway")
    def gateway(actor=Depends(principal)):
        require(actor, "platform.gateway.manage")
        return {
            "configured": bool(settings.litellm_admin_url),
            "admin_url": settings.litellm_admin_url,
        }

    @app.get("/api/v2/platform/tenants/{tenant_id}/gateway")
    def gateway_status(tenant_id: str, actor=Depends(principal)):
        require(actor, "platform.gateway.manage")
        with db.tenant_scope(tenant_id):
            return platform.gateway.status(tenant_id)

    @app.post("/api/v2/platform/tenants/{tenant_id}/gateway/{operation}")
    def gateway_operation(tenant_id: str, operation: str, actor=Depends(principal)):
        require(actor, "platform.gateway.manage")
        methods = {
            "enroll": platform.gateway.enroll,
            "rotate": platform.gateway.rotate,
            "revoke": platform.gateway.revoke,
        }
        if operation not in methods:
            raise PlatformError(404, "not_found", "操作不存在。")
        with db.tenant_scope(tenant_id):
            return methods[operation](tenant_id, actor_id=actor["id"])

    @app.post(
        "/api/v2/platform/tenants/{tenant_id}/gateway/operations/{operation_id}/reconcile"
    )
    def gateway_reconcile(tenant_id: str, operation_id: str, actor=Depends(principal)):
        require(actor, "platform.gateway.manage")
        with db.tenant_scope(tenant_id):
            return platform.gateway.reconcile(
                tenant_id, operation_id, actor_id=actor["id"]
            )

    @app.get("/api/v2/platform/deployments")
    def deployments(actor=Depends(principal)):
        with db.read() as connection:
            platform.require_platform(connection, actor, "platform.models.manage")
            return {
                "items": [
                    dict(row)
                    for row in connection.execute(
                        select(gt.deployments)
                        .where(gt.deployments.c.owner_scope == "platform")
                        .order_by(gt.deployments.c.created_at, gt.deployments.c.id)
                    ).mappings()
                ]
            }

    @app.post("/api/v2/platform/deployments", status_code=201)
    def deployment(body: s.DeploymentCreate, actor=Depends(principal)):
        require(actor, "platform.models.manage")
        values = body.model_dump(exclude={"gateway_id", "capabilities"})
        values["capabilities"] = {"text": True, "tools": "tools" in body.capabilities}
        return platform.gateway.create_deployment(values, actor_id=actor["id"])

    @app.get("/api/v2/platform/audit")
    def audits(actor=Depends(principal)):
        return {"items": identity.platform_audits(actor)}
