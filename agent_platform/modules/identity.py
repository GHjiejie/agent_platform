"""Global identities and explicit organization memberships.

User IDs and compatibility columns remain stable. Only membership rows grant
organization access; a platform operator is not implicitly a tenant member.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.request_context import audit_details
from agent_platform.modules.authorization import (
    READ_CAPABILITIES,
    TENANT_ROLE_CAPABILITIES,
    capabilities,
    require_capability,
)
from agent_platform.modules.builtin_agents import BUILTIN_AGENTS, COMMON_INSTRUCTIONS

DEFAULT_LIMITS = {
    "max_members": 100,
    "max_agents": 100,
    "max_sse_connections": 20,
    "max_export_jobs": 2,
    "max_queued_runs": 100,
    "max_concurrent_runs": 10,
    "rpm": 120,
    "tpm": 200000,
    "max_budget": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return uuid4().hex


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _version(current, expected) -> None:
    if expected is not None and current != expected:
        raise PlatformError(409, "configuration_conflict", "配置已变化，请刷新后重试。")


def _public_invitation(row) -> dict:
    return {key: value for key, value in dict(row).items() if key != "token_hash"}


class IdentityService:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    @staticmethod
    def _audit(
        connection, actor_id, action, target, tenant_id=None, reason="", details=None
    ):
        connection.execute(
            insert(nt.operation_audits).values(
                id=_id(),
                actor_user_id=actor_id,
                tenant_id=tenant_id,
                action=action,
                target=target,
                reason=reason,
                details=audit_details(details),
                created_at=_now(),
            )
        )

    def bootstrap(self) -> None:
        """Adopt uninitialized legacy tenants once; never re-grant revoked roles."""
        with self.db.transaction("identity:bootstrap") as connection:
            initialized = set(
                connection.scalars(select(nt.tenant_settings.c.tenant_id))
            )
            for tenant in connection.execute(select(t.tenants)).mappings():
                tenant_id = tenant["id"]
                if tenant_id in initialized:
                    continue
                self.db.set_tenant(connection, tenant_id)
                users = list(
                    connection.execute(
                        select(t.users)
                        .where(t.users.c.tenant_id == tenant_id)
                        .order_by(t.users.c.created_at, t.users.c.id)
                    ).mappings()
                )
                owner = next(
                    (
                        u
                        for u in users
                        if u["active"]
                        and u.get("global_status", "active") == "active"
                        and u["role"] == "admin"
                    ),
                    None,
                )
                existing = set(
                    connection.scalars(
                        select(nt.memberships.c.user_id).where(
                            nt.memberships.c.tenant_id == tenant_id
                        )
                    )
                )
                for user in users:
                    if user["id"] in existing:
                        continue
                    role = (
                        "owner"
                        if owner and user["id"] == owner["id"]
                        else ("tenant_admin" if user["role"] == "admin" else "member")
                    )
                    connection.execute(
                        insert(nt.memberships).values(
                            id=_id(),
                            tenant_id=tenant_id,
                            user_id=user["id"],
                            role=role,
                            status="active"
                            if user["active"]
                            and user.get("global_status", "active") == "active"
                            else "revoked",
                            authz_version=1,
                            joined_at=user["created_at"],
                            revoked_at=None,
                        )
                    )
                stamp = _now()
                default_model = (
                    connection.scalar(
                        select(t.models.c.id).where(
                            t.models.c.tenant_id == tenant_id,
                            t.models.c.alias == self.settings.default_model,
                            t.models.c.active.is_(True),
                        )
                    )
                    if self.settings.default_model
                    else None
                )
                connection.execute(
                    insert(nt.tenant_settings).values(
                        tenant_id=tenant_id,
                        status="active" if owner else "suspended",
                        version=1,
                        default_model_id=default_model,
                        ordered_model_ids=[],
                        owner_unavailable=owner is None,
                        suspension_reason="" if owner else "owner_unavailable",
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                if not connection.scalar(
                    select(nt.entitlements.c.tenant_id).where(
                        nt.entitlements.c.tenant_id == tenant_id
                    )
                ):
                    connection.execute(
                        insert(nt.entitlements).values(
                            tenant_id=tenant_id,
                            version=1,
                            **DEFAULT_LIMITS,
                            updated_at=stamp,
                            updated_by=None,
                        )
                    )
                self._audit(
                    connection,
                    None,
                    "identity.legacy_adopted",
                    tenant_id,
                    tenant_id,
                    "保留原自部署组织控制权；未授予平台权限。",
                    {"owner_user_id": owner["id"] if owner else None},
                )
            raw_emails = getattr(self.settings, "operator_emails", ())
            emails = (
                raw_emails.split(",") if isinstance(raw_emails, str) else raw_emails
            )
            for email in {
                str(value).strip().lower() for value in emails if str(value).strip()
            }:
                user = (
                    connection.execute(
                        select(t.users).where(
                            t.users.c.email == email,
                            t.users.c.active.is_(True),
                            t.users.c.global_status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if not user:
                    continue
                for role in ("platform_admin", "platform_finance"):
                    if connection.execute(
                        select(nt.platform_roles).where(
                            nt.platform_roles.c.user_id == user["id"],
                            nt.platform_roles.c.role == role,
                        )
                    ).first():
                        continue
                    connection.execute(
                        insert(nt.platform_roles).values(
                            user_id=user["id"],
                            role=role,
                            granted_by=None,
                            granted_at=_now(),
                            revoked_at=None,
                        )
                    )
                    self._audit(
                        connection,
                        None,
                        "platform.role_bootstrap",
                        user["id"],
                        reason="显式 PLATFORM_OPERATOR_EMAILS 配置。",
                        details={"role": role},
                    )

    def _principal(self, connection, user_id: str) -> dict:
        row = (
            connection.execute(
                select(t.users).where(
                    t.users.c.id == user_id,
                    t.users.c.active.is_(True),
                    t.users.c.global_status == "active",
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError(
                401, "identity_inactive", "登录身份不可用，请重新登录。"
            )
        roles = list(
            connection.scalars(
                select(nt.platform_roles.c.role).where(
                    nt.platform_roles.c.user_id == user_id,
                    nt.platform_roles.c.revoked_at.is_(None),
                )
            )
        )
        member_rows = connection.execute(
            select(
                nt.memberships,
                t.tenants.c.name.label("tenant_name"),
                nt.tenant_settings.c.status.label("tenant_status"),
            )
            .join(t.tenants, t.tenants.c.id == nt.memberships.c.tenant_id)
            .join(
                nt.tenant_settings,
                nt.tenant_settings.c.tenant_id == nt.memberships.c.tenant_id,
            )
            .where(
                nt.memberships.c.user_id == user_id,
                nt.memberships.c.status == "active",
                nt.tenant_settings.c.status != "deleted",
            )
            .order_by(nt.memberships.c.joined_at, nt.memberships.c.id)
        ).mappings()
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "active": True,
            "global_status": row["global_status"],
            "created_at": row["created_at"],
            "auth_version": row["auth_version"],
            "platform_roles": sorted(roles),
            "capabilities": sorted(capabilities(None, roles)),
            "memberships": [dict(item) for item in member_rows],
        }

    def principal(self, user_id: str) -> dict:
        with self.db.read() as connection:
            return self._principal(connection, user_id)

    def _context(self, connection, user_id, tenant_id, allow_suspended=False) -> dict:
        identity = self._principal(connection, user_id)
        membership = next(
            (
                item
                for item in identity["memberships"]
                if item["tenant_id"] == tenant_id
            ),
            None,
        )
        if not membership:
            raise PlatformError(404, "not_found", "组织不存在或无权访问。")
        settings = (
            connection.execute(
                select(nt.tenant_settings).where(
                    nt.tenant_settings.c.tenant_id == tenant_id
                )
            )
            .mappings()
            .one()
        )
        if settings["status"] != "active" and not (
            allow_suspended
            and settings["status"] in ("suspended", "closing", "provisioning")
        ):
            raise PlatformError(403, "tenant_suspended", "组织当前不允许执行此操作。")
        role = membership["role"]
        result = {key: value for key, value in identity.items() if key != "memberships"}
        result.update(
            tenant_id=tenant_id,
            tenant_name=membership["tenant_name"],
            role="admin" if role in ("owner", "tenant_admin") else "member",
            tenant_role=role,
            membership_id=membership["id"],
            identity_version=identity["auth_version"],
            membership_version=membership["authz_version"],
            tenant_status=settings["status"],
            tenant_policy_version=settings["version"],
            capabilities=sorted(capabilities(role, identity["platform_roles"])),
            _allow_suspended=allow_suspended,
        )
        return result

    def context(self, user_id: str, tenant_id: str, allow_suspended=False) -> dict:
        with self.db.read() as connection:
            return self._context(connection, user_id, tenant_id, allow_suspended)

    def assert_current(
        self, connection, user: dict, capability: str | None = None
    ) -> dict:
        if not user.get("tenant_id"):
            current = self._principal(connection, user["id"])
        else:
            allow = bool(user.get("_allow_suspended")) and (
                capability is None or capability in READ_CAPABILITIES
            )
            current = self._context(connection, user["id"], user["tenant_id"], allow)
            for key in ("identity_version", "membership_version", "membership_id"):
                if key in user and user[key] != current[key]:
                    raise PlatformError(
                        403, "access_revoked", "权限已变化，请刷新后重试。"
                    )
        if capability:
            require_capability(current, capability)
        return current

    def _platform_actor(self, connection, actor, capability):
        current = self._principal(connection, actor["id"])
        require_capability(current, capability)
        return current

    def list_tenants(self, actor: dict) -> list[dict]:
        with self.db.read() as connection:
            current = self._principal(connection, actor["id"])
            if not set(current["capabilities"]).intersection(
                {
                    "platform.tenants.manage",
                    "platform.billing.manage",
                    "platform.entitlements.manage",
                    "platform.models.manage",
                    "platform.gateway.manage",
                }
            ):
                raise PlatformError(
                    403, "capability_required", "当前身份无权读取平台组织目录。"
                )
            rows = connection.execute(
                select(t.tenants, nt.tenant_settings)
                .join(
                    nt.tenant_settings, t.tenants.c.id == nt.tenant_settings.c.tenant_id
                )
                .order_by(t.tenants.c.created_at.desc(), t.tenants.c.id)
            ).mappings()
            return [dict(row) for row in rows]

    @staticmethod
    def _validated_limits(values: dict) -> dict:
        if set(values) - set(DEFAULT_LIMITS):
            raise PlatformError(422, "invalid_entitlement", "包含未知套餐限制。")
        result = dict(values)
        for key, value in result.items():
            if key == "max_budget":
                if value is None:
                    continue
                try:
                    amount = Decimal(str(value))
                    if (
                        not amount.is_finite()
                        or amount < 0
                        or amount > Decimal(1000000000)
                        or amount.as_tuple().exponent < -12
                    ):
                        raise ValueError()
                except (InvalidOperation, ValueError):
                    raise PlatformError(
                        422, "invalid_entitlement", "预算金额无效。"
                    ) from None
                result[key] = format(amount, "f")
            elif (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not ((1 if key == "max_members" else 0) <= value <= 1000000000)
            ):
                raise PlatformError(
                    422, "invalid_entitlement", "套餐限制必须为范围内的整数。"
                )
        return result

    def create_tenant(
        self,
        actor: dict,
        *,
        name: str,
        owner_user_id: str,
        idempotency_key: str,
        limits: dict | None = None,
    ) -> dict:
        if (
            not name.strip()
            or len(name.strip()) > 120
            or not idempotency_key
            or len(idempotency_key) > 160
        ):
            raise PlatformError(422, "invalid_tenant", "组织名称或幂等键无效。")
        chosen_limits = {**DEFAULT_LIMITS, **self._validated_limits(limits or {})}
        if chosen_limits["max_agents"] < len(BUILTIN_AGENTS):
            raise PlatformError(
                422, "invalid_entitlement", "Agent 数量上限必须容纳内置模板。"
            )
        fingerprint = _hash(
            json.dumps(
                {
                    "name": name.strip(),
                    "owner_user_id": owner_user_id,
                    "limits": chosen_limits,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        with self.db.transaction("identity:tenant-create") as connection:
            current = self._platform_actor(connection, actor, "platform.tenants.manage")
            previous = (
                connection.execute(
                    select(nt.tenant_creation_requests).where(
                        nt.tenant_creation_requests.c.actor_user_id == actor["id"],
                        nt.tenant_creation_requests.c.idempotency_key
                        == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise PlatformError(
                        409, "idempotency_conflict", "同一幂等键对应的请求不同。"
                    )
                return dict(
                    connection.execute(
                        select(t.tenants, nt.tenant_settings)
                        .join(
                            nt.tenant_settings,
                            t.tenants.c.id == nt.tenant_settings.c.tenant_id,
                        )
                        .where(t.tenants.c.id == previous["tenant_id"])
                    )
                    .mappings()
                    .one()
                )
            self._principal(connection, owner_user_id)
            tenant_id, stamp = _id(), _now()
            self.db.set_tenant(connection, tenant_id)
            connection.execute(
                insert(t.tenants).values(
                    id=tenant_id, name=name.strip(), created_at=stamp
                )
            )
            connection.execute(
                insert(nt.memberships).values(
                    id=_id(),
                    tenant_id=tenant_id,
                    user_id=owner_user_id,
                    role="owner",
                    status="active",
                    authz_version=1,
                    joined_at=stamp,
                )
            )
            state = {
                "tenant_id": tenant_id,
                "status": "active",
                "version": 1,
                "default_model_id": None,
                "ordered_model_ids": [],
                "owner_unavailable": False,
                "suspension_reason": "",
                "created_at": stamp,
                "updated_at": stamp,
            }
            connection.execute(insert(nt.tenant_settings).values(**state))
            connection.execute(
                insert(nt.entitlements).values(
                    tenant_id=tenant_id,
                    version=1,
                    **chosen_limits,
                    updated_at=stamp,
                    updated_by=current["id"],
                )
            )
            # Same transaction: there is never an externally visible half-created workspace.
            from agent_platform.modules.billing.service import wallets

            connection.execute(
                insert(wallets).values(
                    tenant_id=tenant_id,
                    balance=Decimal(0),
                    reserved=Decimal(0),
                    currency="USD",
                    blocked=False,
                    block_reason="",
                )
            )
            for template in BUILTIN_AGENTS:
                agent = {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "created_at": stamp,
                    "name": template.name,
                    "description": template.description,
                    "system_prompt": COMMON_INSTRUCTIONS + template.instructions,
                    "model_id": None,
                    "builtin_key": template.key,
                    "temperature": 1,
                    "max_steps": 6,
                    "max_tokens": template.max_tokens
                    or self.settings.default_output_tokens,
                    "tools": list(template.tools),
                    "published_version": 1,
                }
                connection.execute(insert(t.agents).values(**agent))
                connection.execute(
                    insert(t.agent_versions).values(
                        agent_id=agent["id"],
                        version=1,
                        tenant_id=tenant_id,
                        spec=agent,
                        created_at=stamp,
                    )
                )
            connection.execute(
                insert(nt.tenant_creation_requests).values(
                    actor_user_id=current["id"],
                    idempotency_key=idempotency_key,
                    request_hash=fingerprint,
                    tenant_id=tenant_id,
                    created_at=stamp,
                )
            )
            self._audit(
                connection,
                current["id"],
                "tenant.create",
                tenant_id,
                tenant_id,
                details={"owner_user_id": owner_user_id, "limits": chosen_limits},
            )
            return {"id": tenant_id, "name": name.strip(), **state}

    def set_tenant_status(
        self, actor, tenant_id, status, reason, expected_version=None
    ):
        if status not in ("active", "suspended", "closing") or not reason.strip():
            raise PlatformError(422, "invalid_tenant_status", "状态或操作原因无效。")
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            current_actor = self._platform_actor(
                connection, actor, "platform.tenants.manage"
            )
            current = (
                connection.execute(
                    select(nt.tenant_settings).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                .mappings()
                .first()
            )
            if not current:
                raise PlatformError(404, "not_found", "组织不存在。")
            _version(current["version"], expected_version)
            if (
                current["status"] in ("deleted", "closing")
                and current["status"] != status
            ):
                raise PlatformError(
                    409, "invalid_transition", "关闭中的组织不能直接恢复。"
                )
            if status == "active" and not self._owner_count(connection, tenant_id):
                raise PlatformError(
                    409, "owner_unavailable", "恢复前必须指定有效所有者。"
                )
            values = {
                "status": status,
                "version": current["version"] + 1,
                "suspension_reason": "" if status == "active" else reason.strip(),
                "owner_unavailable": not bool(self._owner_count(connection, tenant_id)),
                "updated_at": _now(),
            }
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == tenant_id)
                .values(**values)
            )
            if status != "active":
                self._cancel_queued(connection, tenant_id)
            self._audit(
                connection,
                current_actor["id"],
                "tenant.status",
                tenant_id,
                tenant_id,
                reason,
                {"from": current["status"], "to": status},
            )
            return {**dict(current), **values}

    @staticmethod
    def _cancel_queued(connection, tenant_id, user_id=None):
        query = select(t.runs).where(
            t.runs.c.tenant_id == tenant_id,
            t.runs.c.status.in_(("queued", "running", "cancelling")),
        )
        if user_id:
            query = query.where(t.runs.c.user_id == user_id)
        for run in connection.execute(query).mappings():
            queued = run["status"] == "queued"
            state = "cancelled" if queued else "cancelling"
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"], t.runs.c.tenant_id == tenant_id)
                .values(
                    cancel_requested=True,
                    status=state,
                    finished_at=_now() if queued else run["finished_at"],
                )
            )
            sequence = (
                connection.scalar(
                    select(func.max(t.run_events.c.sequence)).where(
                        t.run_events.c.run_id == run["id"]
                    )
                )
                or 0
            ) + 1
            connection.execute(
                insert(t.run_events).values(
                    tenant_id=tenant_id,
                    run_id=run["id"],
                    sequence=sequence,
                    type="run.cancelled" if queued else "run.cancelling",
                    data={"status": state, "reason": "access_revoked"},
                    created_at=_now(),
                )
            )

    @staticmethod
    def _owner_count(connection, tenant_id, excluding_id=None):
        query = (
            select(func.count())
            .select_from(
                nt.memberships.join(t.users, t.users.c.id == nt.memberships.c.user_id)
            )
            .where(
                nt.memberships.c.tenant_id == tenant_id,
                nt.memberships.c.role == "owner",
                nt.memberships.c.status == "active",
                t.users.c.active.is_(True),
                t.users.c.global_status == "active",
            )
        )
        if excluding_id:
            query = query.where(nt.memberships.c.id != excluding_id)
        return connection.scalar(query)

    def members(self, actor):
        with self.db.read() as connection:
            current = self.assert_current(connection, actor, "members.read")
            return [
                dict(row)
                for row in connection.execute(
                    select(
                        nt.memberships,
                        t.users.c.email,
                        t.users.c.name,
                        t.users.c.global_status,
                    )
                    .join(t.users, t.users.c.id == nt.memberships.c.user_id)
                    .where(nt.memberships.c.tenant_id == current["tenant_id"])
                    .order_by(nt.memberships.c.joined_at, nt.memberships.c.id)
                ).mappings()
            ]

    @staticmethod
    def _check_role_assignment(actor, role, existing_role=None):
        if role not in TENANT_ROLE_CAPABILITIES:
            raise PlatformError(422, "invalid_role", "组织角色无效。")
        if actor["tenant_role"] != "owner" and (
            role in ("owner", "tenant_admin")
            or existing_role in ("owner", "tenant_admin")
        ):
            raise PlatformError(403, "owner_required", "高权限成员变更需要组织所有者。")

    def create_invitation(self, actor, *, email, role="member", expires_hours=72):
        email = email.strip().lower()
        if (
            "@" not in email
            or len(email) > 254
            or not 1 <= expires_hours <= 168
            or role == "owner"
        ):
            raise PlatformError(
                422, "invalid_invitation", "邀请邮箱、角色或有效期无效。"
            )
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            current = self.assert_current(connection, actor, "members.manage")
            self._check_role_assignment(current, role)
            token = secrets.token_urlsafe(40)
            stamp = _now()
            connection.execute(
                update(nt.invitations)
                .where(
                    nt.invitations.c.tenant_id == current["tenant_id"],
                    nt.invitations.c.email == email,
                    nt.invitations.c.accepted_at.is_(None),
                    nt.invitations.c.revoked_at.is_(None),
                )
                .values(revoked_at=stamp)
            )
            invitation = {
                "id": _id(),
                "tenant_id": current["tenant_id"],
                "email": email,
                "role": role,
                "token_hash": _hash(token),
                "expires_at": time.time() + expires_hours * 3600,
                "created_at": stamp,
                "invited_by": current["id"],
                "accepted_at": None,
                "accepted_by": None,
                "revoked_at": None,
            }
            connection.execute(insert(nt.invitations).values(**invitation))
            self._audit(
                connection,
                current["id"],
                "membership.invite",
                invitation["id"],
                current["tenant_id"],
                details={"email": email, "role": role},
            )
            return {**_public_invitation(invitation), "token": token}

    def list_invitations(self, actor):
        with self.db.read() as connection:
            current = self.assert_current(connection, actor, "members.read")
            return [
                _public_invitation(row)
                for row in connection.execute(
                    select(nt.invitations)
                    .where(nt.invitations.c.tenant_id == current["tenant_id"])
                    .order_by(nt.invitations.c.created_at.desc())
                ).mappings()
            ]

    def accept_invitation(self, actor, token):
        token_hash = _hash(token)
        with self.db.read() as connection:
            tenant_id = connection.scalar(
                select(nt.invitations.c.tenant_id).where(
                    nt.invitations.c.token_hash == token_hash
                )
            )
        if not tenant_id:
            raise PlatformError(404, "invalid_invitation", "邀请不可用。")
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            identity = self._principal(connection, actor["id"])
            invite = (
                connection.execute(
                    select(nt.invitations).where(
                        nt.invitations.c.token_hash == token_hash,
                        nt.invitations.c.tenant_id == tenant_id,
                    )
                )
                .mappings()
                .one()
            )
            if identity["email"].strip().lower() != invite["email"]:
                raise PlatformError(
                    403, "invitation_identity_mismatch", "请使用受邀邮箱对应的身份。"
                )
            existing = (
                connection.execute(
                    select(nt.memberships).where(
                        nt.memberships.c.tenant_id == tenant_id,
                        nt.memberships.c.user_id == identity["id"],
                    )
                )
                .mappings()
                .first()
            )
            if invite["accepted_at"]:
                state = connection.scalar(
                    select(nt.tenant_settings.c.status).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                if (
                    invite["accepted_by"] != identity["id"]
                    or not existing
                    or existing["status"] != "active"
                    or state != "active"
                ):
                    raise PlatformError(409, "invitation_used", "邀请已被使用。")
                return dict(existing)
            if invite["revoked_at"] or invite["expires_at"] <= time.time():
                raise PlatformError(410, "invitation_expired", "邀请已失效。")
            state = connection.scalar(
                select(nt.tenant_settings.c.status).where(
                    nt.tenant_settings.c.tenant_id == tenant_id
                )
            )
            if state != "active":
                raise PlatformError(403, "tenant_suspended", "组织当前不接受成员加入。")
            # An invite cannot outlive the inviter's authority to grant that role.
            inviter = self._context(connection, invite["invited_by"], tenant_id)
            require_capability(inviter, "members.manage")
            self._check_role_assignment(inviter, invite["role"])
            if not existing or existing["status"] != "active":
                self._check_seats(connection, tenant_id)
            stamp = _now()
            if existing:
                if existing["status"] == "active":
                    result = dict(existing)  # accepting never changes an existing role
                else:
                    values = {
                        "status": "active",
                        "role": invite["role"],
                        "revoked_at": None,
                        "authz_version": existing["authz_version"] + 1,
                    }
                    connection.execute(
                        update(nt.memberships)
                        .where(
                            nt.memberships.c.id == existing["id"],
                            nt.memberships.c.tenant_id == tenant_id,
                        )
                        .values(**values)
                    )
                    result = {**dict(existing), **values}
            else:
                result = {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "user_id": identity["id"],
                    "role": invite["role"],
                    "status": "active",
                    "authz_version": 1,
                    "joined_at": stamp,
                    "revoked_at": None,
                }
                connection.execute(insert(nt.memberships).values(**result))
            connection.execute(
                update(nt.invitations)
                .where(nt.invitations.c.id == invite["id"])
                .values(accepted_at=stamp, accepted_by=identity["id"])
            )
            self._audit(
                connection, identity["id"], "membership.accept", result["id"], tenant_id
            )
            return result

    @staticmethod
    def _check_seats(connection, tenant_id):
        limit = connection.scalar(
            select(nt.entitlements.c.max_members).where(
                nt.entitlements.c.tenant_id == tenant_id
            )
        )
        count = connection.scalar(
            select(func.count())
            .select_from(nt.memberships)
            .where(
                nt.memberships.c.tenant_id == tenant_id,
                nt.memberships.c.status == "active",
            )
        )
        if limit is None or count >= limit:
            raise PlatformError(
                409, "entitlement_exceeded", "组织成员数量已达到套餐上限。"
            )

    def update_member(self, actor, membership_id, values, expected_version=None):
        if not values or set(values) - {"role", "status"}:
            raise PlatformError(
                422, "invalid_membership", "仅可修改组织角色和成员状态。"
            )
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            current = self.assert_current(connection, actor, "members.manage")
            member = (
                connection.execute(
                    select(nt.memberships).where(
                        nt.memberships.c.id == membership_id,
                        nt.memberships.c.tenant_id == current["tenant_id"],
                    )
                )
                .mappings()
                .first()
            )
            if not member:
                raise PlatformError(404, "not_found", "成员不存在。")
            _version(member["authz_version"], expected_version)
            role = values.get("role", member["role"])
            status = values.get("status", member["status"])
            self._check_role_assignment(current, role, member["role"])
            if role == "owner" and member["role"] != "owner":
                raise PlatformError(
                    409, "ownership_transfer_required", "请使用所有权交接操作。"
                )
            if status not in ("active", "revoked"):
                raise PlatformError(422, "invalid_membership", "成员状态无效。")
            if (
                member["role"] == "owner"
                and member["status"] == "active"
                and (role != "owner" or status != "active")
                and not self._owner_count(
                    connection, current["tenant_id"], membership_id
                )
            ):
                raise PlatformError(
                    409, "last_owner", "必须保留至少一名有效组织所有者。"
                )
            if member["status"] != "active" and status == "active":
                self._principal(connection, member["user_id"])
                self._check_seats(connection, current["tenant_id"])
            changed = role != member["role"] or status != member["status"]
            changes = {
                "role": role,
                "status": status,
                "authz_version": member["authz_version"] + int(changed),
                "revoked_at": _now() if status == "revoked" else None,
            }
            connection.execute(
                update(nt.memberships)
                .where(
                    nt.memberships.c.id == membership_id,
                    nt.memberships.c.tenant_id == current["tenant_id"],
                )
                .values(**changes)
            )
            if changed:
                self._cancel_queued(connection, current["tenant_id"], member["user_id"])
            self._audit(
                connection,
                current["id"],
                "membership.update",
                membership_id,
                current["tenant_id"],
                details={"role": role, "status": status},
            )
            return {**dict(member), **changes}

    def transfer_owner(self, actor, target_membership_id, expected_version=None):
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            current = self.assert_current(connection, actor, "ownership.manage")
            source = (
                connection.execute(
                    select(nt.memberships).where(
                        nt.memberships.c.id == current["membership_id"]
                    )
                )
                .mappings()
                .one()
            )
            _version(source["authz_version"], expected_version)
            target = (
                connection.execute(
                    select(nt.memberships).where(
                        nt.memberships.c.id == target_membership_id,
                        nt.memberships.c.tenant_id == current["tenant_id"],
                        nt.memberships.c.status == "active",
                    )
                )
                .mappings()
                .first()
            )
            if not target:
                raise PlatformError(404, "not_found", "接收者不是有效组织成员。")
            self._principal(connection, target["user_id"])
            if target["id"] == source["id"]:
                return dict(source)
            for member, role in ((target, "owner"), (source, "tenant_admin")):
                connection.execute(
                    update(nt.memberships)
                    .where(
                        nt.memberships.c.id == member["id"],
                        nt.memberships.c.tenant_id == current["tenant_id"],
                    )
                    .values(role=role, authz_version=member["authz_version"] + 1)
                )
            self._audit(
                connection,
                current["id"],
                "tenant.owner_transfer",
                target["id"],
                current["tenant_id"],
                details={"previous_owner": source["user_id"]},
            )
            return {
                **dict(target),
                "role": "owner",
                "authz_version": target["authz_version"] + 1,
            }

    def get_entitlements(self, actor, tenant_id=None):
        with self.db.read() as connection:
            if tenant_id is not None:
                self._platform_actor(connection, actor, "platform.entitlements.manage")
            else:
                tenant_id = self.assert_current(connection, actor, "tenant.read")[
                    "tenant_id"
                ]
            row = (
                connection.execute(
                    select(nt.entitlements).where(
                        nt.entitlements.c.tenant_id == tenant_id
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError(404, "not_found", "组织套餐不存在。")
            return dict(row)

    def set_entitlements(self, actor, tenant_id, values, expected_version=None):
        values = self._validated_limits(values)
        with self.db.transaction(f"tenant:{tenant_id}") as connection:
            current_actor = self._platform_actor(
                connection, actor, "platform.entitlements.manage"
            )
            row = (
                connection.execute(
                    select(nt.entitlements).where(
                        nt.entitlements.c.tenant_id == tenant_id
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError(404, "not_found", "组织套餐不存在。")
            _version(row["version"], expected_version)
            changes = {
                **values,
                "version": row["version"] + 1,
                "updated_at": _now(),
                "updated_by": current_actor["id"],
            }
            connection.execute(
                update(nt.entitlements)
                .where(nt.entitlements.c.tenant_id == tenant_id)
                .values(**changes)
            )
            self._audit(
                connection,
                current_actor["id"],
                "tenant.entitlements",
                tenant_id,
                tenant_id,
                details=values,
            )
            from agent_platform.modules.gateway_control import GatewayService

            GatewayService(self.db, self.settings).converge_configuration(
                connection, tenant_id
            )
            return {**dict(row), **changes}

    def model_policy(self, actor):
        with self.db.read() as connection:
            current = self.assert_current(connection, actor, "models.read")
            row = (
                connection.execute(
                    select(nt.tenant_settings).where(
                        nt.tenant_settings.c.tenant_id == current["tenant_id"]
                    )
                )
                .mappings()
                .one()
            )
            return {
                key: row[key]
                for key in (
                    "tenant_id",
                    "default_model_id",
                    "ordered_model_ids",
                    "version",
                )
            }

    def set_model_policy(
        self,
        actor,
        *,
        default_model_id=None,
        ordered_model_ids=None,
        expected_version=None,
    ):
        ordered = list(dict.fromkeys(ordered_model_ids or []))
        if len(ordered) > 200:
            raise PlatformError(422, "invalid_model_policy", "候选模型数量过多。")
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            current = self.assert_current(connection, actor, "models.policy")
            tenant_id = current["tenant_id"]
            row = (
                connection.execute(
                    select(nt.tenant_settings).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                .mappings()
                .one()
            )
            _version(row["version"], expected_version)
            for model_id in set(
                ordered + ([default_model_id] if default_model_id else [])
            ):
                if not connection.scalar(
                    select(t.models.c.id).where(
                        t.models.c.id == model_id,
                        t.models.c.tenant_id == tenant_id,
                        t.models.c.active.is_(True),
                    )
                ):
                    raise PlatformError(
                        404, "model_not_authorized", "模型不存在或未在本组织启用。"
                    )
            values = {
                "default_model_id": default_model_id,
                "ordered_model_ids": ordered,
                "version": row["version"] + 1,
                "updated_at": _now(),
            }
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == tenant_id)
                .values(**values)
            )
            self._audit(
                connection,
                current["id"],
                "tenant.model_policy",
                tenant_id,
                tenant_id,
                details={
                    "default_model_id": default_model_id,
                    "ordered_model_ids": ordered,
                },
            )
            return {"tenant_id": tenant_id, **values}

    def platform_audits(self, actor, limit=200):
        with self.db.read() as connection:
            self._platform_actor(connection, actor, "platform.audit.read")
            return [
                dict(row)
                for row in connection.execute(
                    select(nt.operation_audits)
                    .order_by(
                        nt.operation_audits.c.created_at.desc(),
                        nt.operation_audits.c.id.desc(),
                    )
                    .limit(max(1, min(limit, 500)))
                ).mappings()
            ]
