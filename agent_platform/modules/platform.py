"""Tenant-scoped application services for users, agents and durable runs."""

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime
from decimal import Decimal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import (
    DateTime,
    Numeric,
    case,
    cast,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.authorization import capabilities, require_capability
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.builtin_agents import BUILTIN_AGENTS, COMMON_INSTRUCTIONS
from agent_platform.modules.execution_policy import (
    ReasoningEffort,
    effective_limits,
    read_policy,
)
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.identity import IdentityService
from agent_platform.modules.run_records import append_event, now, uid

ACTIVE_STATUSES = ("queued", "running", "cancelling")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "expired")
STREAM_LEASE_SECONDS = 30
hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash(secrets.token_urlsafe(32))


def public_user(row) -> dict:
    return {
        key: row[key]
        for key in ("id", "tenant_id", "email", "name", "role", "active", "created_at")
    }


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def audit(connection, user: dict, action: str, target: str) -> None:
    connection.execute(
        insert(t.audits).values(
            id=uid(),
            tenant_id=user["tenant_id"],
            created_at=now(),
            actor_id=user["id"],
            actor_email=user["email"],
            action=action,
            target=target,
        )
    )


class Platform:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.billing = BillingService(db, settings.redis_url)
        self.identity = IdentityService(db, settings)
        self.gateway = GatewayService(db, settings)

    def bootstrap(self) -> None:
        self._bootstrap_admin()
        self.identity.bootstrap()
        self._bootstrap_default_model()
        self._bootstrap_builtin_agents()
        self.gateway.bootstrap_legacy_catalog()

    def _bootstrap_builtin_agents(self) -> None:
        """Install once per tenant, even without a model; preserve later edits."""
        with self.db.read() as connection:
            owners = list(
                connection.execute(
                    select(t.users)
                    .join(
                        nt.memberships,
                        (nt.memberships.c.user_id == t.users.c.id)
                        & (nt.memberships.c.tenant_id == t.users.c.tenant_id),
                    )
                    .join(
                        nt.tenant_settings,
                        nt.tenant_settings.c.tenant_id == nt.memberships.c.tenant_id,
                    )
                    .where(
                        nt.memberships.c.role.in_(("owner", "tenant_admin")),
                        nt.memberships.c.status == "active",
                        nt.tenant_settings.c.status == "active",
                        t.users.c.active.is_(True),
                    )
                    .order_by(t.users.c.created_at, t.users.c.id)
                ).mappings()
            )
        seen = set()
        for owner in owners:
            tenant_id = owner["tenant_id"]
            if tenant_id in seen:
                continue
            seen.add(tenant_id)
            with self.db.transaction(f"tenant:{tenant_id}") as connection:
                self.require_current(connection, dict(owner), admin=True)
                installed = set(
                    connection.scalars(
                        select(t.agents.c.builtin_key).where(
                            t.agents.c.tenant_id == tenant_id
                        )
                    )
                )
                for template in BUILTIN_AGENTS:
                    if template.key in installed:
                        continue
                    agent = {
                        "id": uid(),
                        "tenant_id": tenant_id,
                        "created_at": now(),
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
                    from agent_platform.modules.agent_registry.contracts import (
                        AgentDeclaration,
                    )

                    agent.update(AgentDeclaration.from_spec(agent).model_dump())
                    connection.execute(insert(t.agents).values(**agent))
                    connection.execute(
                        insert(t.agent_versions).values(
                            agent_id=agent["id"],
                            version=1,
                            tenant_id=tenant_id,
                            spec=agent,
                            created_at=agent["created_at"],
                        )
                    )
                    audit(connection, dict(owner), "agent.bootstrap", agent["id"])

    def _bootstrap_admin(self) -> None:
        """Create the first tenant only with an explicit, non-default password."""
        with self.db.transaction("bootstrap") as connection:
            if connection.scalar(select(func.count()).select_from(t.users)):
                return
            if len(self.settings.admin_password) < 12:
                raise PlatformError(
                    503,
                    "bootstrap_required",
                    "首次启动请设置至少 12 位的 PLATFORM_ADMIN_PASSWORD。",
                )
            tenant_id, user_id = uid(), uid()
            self.db.set_tenant(connection, tenant_id)
            stamp = now()
            connection.execute(
                insert(t.tenants).values(
                    id=tenant_id, name="我的组织", created_at=stamp
                )
            )
            user = {
                "id": user_id,
                "tenant_id": tenant_id,
                "email": self.settings.admin_email.strip().lower(),
                "name": "管理员",
                "role": "admin",
                "active": True,
                "created_at": stamp,
            }
            connection.execute(
                insert(t.users).values(
                    **user, password_hash=hasher.hash(self.settings.admin_password)
                )
            )
            audit(connection, user, "tenant.bootstrap", tenant_id)
        self.billing.wallet(tenant_id)

    def _bootstrap_default_model(self) -> None:
        """Register the configured model once, using only explicit platform prices."""
        if not self.settings.default_model:
            return
        with self.db.read() as connection:
            user = (
                connection.execute(
                    select(t.users).where(
                        t.users.c.email == self.settings.admin_email.strip().lower(),
                        t.users.c.role == "admin",
                        t.users.c.active.is_(True),
                    )
                )
                .mappings()
                .first()
            )
        if user is None:
            return
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            existing = connection.scalar(
                select(t.models.c.id).where(
                    t.models.c.tenant_id == user["tenant_id"],
                    t.models.c.alias == self.settings.default_model,
                )
            )
            if existing:
                return
            if not (
                self.settings.default_model_input_price
                and self.settings.default_model_output_price
            ):
                return
            from agent_platform.apps.api.schemas import ModelCreate

            values = ModelCreate(
                name=self.settings.default_model,
                alias=self.settings.default_model,
                input_price=self.settings.default_model_input_price,
                output_price=self.settings.default_model_output_price,
                context_window=self.settings.default_context_window,
                max_output_tokens=self.settings.default_output_tokens,
            ).model_dump()
            self.require_current(connection, dict(user), admin=True)
            model_id = uid()
            connection.execute(
                insert(t.models).values(
                    **values,
                    id=model_id,
                    tenant_id=user["tenant_id"],
                    created_at=now(),
                    price_version=1,
                )
            )
            audit(connection, dict(user), "model.bootstrap", model_id)
            connection.execute(
                update(nt.tenant_settings)
                .where(
                    nt.tenant_settings.c.tenant_id == user["tenant_id"],
                    nt.tenant_settings.c.default_model_id.is_(None),
                )
                .values(default_model_id=model_id)
            )

    def _register_identity(self, email: str, password: str) -> dict:
        operator_emails = self.settings.operator_emails
        if isinstance(operator_emails, str):
            operator_emails = operator_emails.split(",")
        reserved = {
            value.strip().lower()
            for value in (self.settings.admin_email, *operator_emails)
        }
        if email in reserved:
            raise PlatformError(
                403,
                "registration_restricted",
                "此邮箱预留用于平台管理，请联系平台管理员开通账号。",
            )
        if len(password) < 12:
            raise PlatformError(
                422,
                "registration_password_too_short",
                "此邮箱尚未注册，首次登录请设置至少 12 位密码。",
            )
        password_hash = hasher.hash(password)
        with self.db.transaction("identity:provision") as connection:
            existing = (
                connection.execute(select(t.users).where(t.users.c.email == email))
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            # The legacy home reference grants no organization access. Only an
            # accepted invitation creates a membership for a new identity.
            home = connection.scalar(
                select(t.tenants.c.id).order_by(t.tenants.c.created_at).limit(1)
            )
            if not home:
                raise PlatformError(409, "platform_not_initialized", "请先初始化平台。")
            user = {
                "id": uid(),
                "tenant_id": home,
                "email": email,
                "name": email.split("@", 1)[0][:120],
                "password_hash": password_hash,
                "role": "member",
                "active": True,
                "global_status": "active",
                "auth_version": 1,
                "created_at": now(),
            }
            try:
                with connection.begin_nested():
                    connection.execute(insert(t.users).values(**user))
            except IntegrityError:
                # Another account-provisioning path may win the email race.
                # Authenticate its password below rather than replacing it.
                existing = (
                    connection.execute(select(t.users).where(t.users.c.email == email))
                    .mappings()
                    .first()
                )
                if not existing:
                    raise
                return dict(existing)
            self.identity._audit(
                connection,
                user["id"],
                "identity.register",
                user["id"],
                details={"email": email},
            )
            return user

    def login(self, email: str, password: str, address: str) -> dict:
        email = email.strip().lower()
        window = int(time.time() // 300)
        with self.db.transaction("authentication") as connection:
            for key, limit in (
                (digest("email:" + email), 10),
                (digest("ip:" + address), 50),
            ):
                row = (
                    connection.execute(
                        select(t.login_attempts).where(t.login_attempts.c.key == key)
                    )
                    .mappings()
                    .first()
                )
                count = row["count"] if row and row["window"] == window else 0
                if count >= limit:
                    raise PlatformError(
                        429, "login_rate_limited", "登录尝试过于频繁，请稍后重试。"
                    )
                if row:
                    connection.execute(
                        update(t.login_attempts)
                        .where(t.login_attempts.c.key == key)
                        .values(window=window, count=count + 1)
                    )
                else:
                    connection.execute(
                        insert(t.login_attempts).values(key=key, window=window, count=1)
                    )
            row = (
                connection.execute(select(t.users).where(t.users.c.email == email))
                .mappings()
                .first()
            )
        if row is None:
            row = self._register_identity(email, password)
        try:
            valid = hasher.verify(
                row["password_hash"] if row else _DUMMY_HASH, password
            )
        except (VerificationError, InvalidHashError):
            valid = False
        if (
            not valid
            or not row
            or not row["active"]
            or row["global_status"] != "active"
        ):
            raise PlatformError(401, "invalid_credentials", "邮箱或密码不正确。")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.db.transaction(f"identity:{row['id']}") as connection:
            current = (
                connection.execute(select(t.users).where(t.users.c.id == row["id"]))
                .mappings()
                .one()
            )
            if (
                not current["active"]
                or current["global_status"] != "active"
                or current["password_hash"] != row["password_hash"]
            ):
                raise PlatformError(
                    401, "invalid_credentials", "账号状态已变化，请重新登录。"
                )
            connection.execute(
                insert(t.auth_sessions).values(
                    token_hash=digest(token),
                    user_id=row["id"],
                    csrf_token=csrf,
                    expires_at=time.time() + self.settings.session_hours * 3600,
                )
            )
            self.identity._audit(connection, current["id"], "auth.login", current["id"])
        return {
            "user": self.identity.principal(current["id"]),
            "csrf_token": csrf,
            "token": token,
        }

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise PlatformError(401, "authentication_required", "请先登录。")
        with self.db.read() as connection:
            row = (
                connection.execute(
                    select(t.users, t.auth_sessions.c.csrf_token)
                    .join(t.auth_sessions, t.users.c.id == t.auth_sessions.c.user_id)
                    .where(
                        t.auth_sessions.c.token_hash == digest(token),
                        t.auth_sessions.c.expires_at > time.time(),
                        t.users.c.active.is_(True),
                        t.users.c.global_status == "active",
                    )
                )
                .mappings()
                .first()
            )
        if not row:
            raise PlatformError(401, "session_expired", "登录已失效，请重新登录。")
        return {
            "user": self.identity.principal(row["id"]),
            "csrf_token": row["csrf_token"],
        }

    def require_current(self, connection, user: dict, admin: bool = False) -> dict:
        row = self.identity.assert_current(connection, user)
        if admin and row["tenant_role"] not in {"owner", "tenant_admin"}:
            raise PlatformError(403, "admin_required", "此操作需要管理员权限。")
        return dict(row)

    def require_platform(self, connection, user: dict, capability: str) -> None:
        active = connection.scalar(
            select(t.users.c.id)
            .where(
                t.users.c.id == user["id"],
                t.users.c.active.is_(True),
                t.users.c.global_status == "active",
            )
            .with_for_update()
        )
        roles = connection.scalars(
            select(nt.platform_roles.c.role)
            .where(
                nt.platform_roles.c.user_id == user["id"],
                nt.platform_roles.c.revoked_at.is_(None),
            )
            .with_for_update()
        ).all()
        if not active or capability not in capabilities(None, roles):
            raise PlatformError(
                403, "platform_role_required", "此操作需要平台运营权限。"
            )

    def list_resources(
        self,
        table,
        user: dict,
        own: bool = False,
        limit: int = 200,
        cursor: str | None = None,
    ) -> list[dict]:
        query = select(table).where(table.c.tenant_id == user["tenant_id"])
        if own:
            require_capability(user, "runs.read_own")
            query = query.where(table.c.user_id == user["id"])
        if cursor:
            created, identifier = self.decode_cursor(cursor)
            query = query.where(
                (table.c.created_at < created)
                | ((table.c.created_at == created) & (table.c.id < identifier))
            )
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(
                        limit
                    )
                ).mappings()
            ]

    def owned(
        self, connection, table, user: dict, resource_id: str, own: bool = False
    ) -> dict:
        query = select(table).where(
            table.c.id == resource_id, table.c.tenant_id == user["tenant_id"]
        )
        if own:
            require_capability(user, "runs.read_own")
            query = query.where(table.c.user_id == user["id"])
        row = connection.execute(query).mappings().first()
        if not row:
            raise PlatformError(404, "not_found", "资源不存在或无权访问。")
        return dict(row)

    @staticmethod
    def encode_cursor(row: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps([row["created_at"], row["id"]]).encode()
        ).decode()

    @staticmethod
    def decode_cursor(cursor: str) -> tuple[str, str]:
        try:
            if len(cursor) > 300:
                raise ValueError
            values = json.loads(base64.urlsafe_b64decode(cursor).decode())
            if (
                not isinstance(values, list)
                or len(values) != 2
                or not all(isinstance(x, str) for x in values)
            ):
                raise ValueError
            datetime.fromisoformat(values[0])
            if not 1 <= len(values[1]) <= 64:
                raise ValueError
            return tuple(values)
        except (ValueError, TypeError, UnicodeError):
            raise PlatformError(400, "invalid_cursor", "分页游标无效。") from None

    def acquire_stream(self, user, run_id):
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            return self._acquire_stream(connection, user, run_id)

    def _acquire_stream(self, connection, user, run_id):
        """Also used inside AG-UI enqueue so rejected streams cannot start work."""
        self.identity.assert_current(connection, user, "runs.read_own")
        self.owned(connection, t.runs, user, run_id, own=True)
        connection.execute(
            delete(rt.stream_leases).where(
                rt.stream_leases.c.tenant_id == user["tenant_id"],
                rt.stream_leases.c.expires_at < time.time(),
            )
        )
        count = connection.scalar(
            select(func.count())
            .select_from(rt.stream_leases)
            .where(rt.stream_leases.c.tenant_id == user["tenant_id"])
        )
        maximum = connection.scalar(
            select(nt.entitlements.c.max_sse_connections).where(
                nt.entitlements.c.tenant_id == user["tenant_id"]
            )
        )
        if maximum is None or count >= maximum:
            raise PlatformError(429, "stream_limit", "组织实时连接数量已达到上限。")
        identifier = uid()
        connection.execute(
            insert(rt.stream_leases).values(
                id=identifier,
                tenant_id=user["tenant_id"],
                user_id=user["id"],
                run_id=run_id,
                expires_at=time.time() + STREAM_LEASE_SECONDS,
            )
        )
        return identifier

    def update_stream(self, user, identifier, *, release=False):
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            if not release:
                self.identity.assert_current(connection, user, "runs.read_own")
            checked_at = time.time()
            query = (
                delete(rt.stream_leases)
                if release
                else update(rt.stream_leases)
                .where(rt.stream_leases.c.expires_at > checked_at)
                .values(expires_at=checked_at + STREAM_LEASE_SECONDS)
            )
            result = connection.execute(
                query.where(
                    rt.stream_leases.c.id == identifier,
                    rt.stream_leases.c.tenant_id == user["tenant_id"],
                    rt.stream_leases.c.user_id == user["id"],
                )
            )
            if not release and result.rowcount != 1:
                raise PlatformError(409, "stream_lease_lost", "实时连接租约已失效。")

    def change_password(self, user: dict, old: str, new: str) -> None:
        with self.db.transaction(f"identity:{user['id']}") as connection:
            current = (
                connection.execute(
                    select(t.users).where(
                        t.users.c.id == user["id"],
                        t.users.c.active.is_(True),
                        t.users.c.global_status == "active",
                    )
                )
                .mappings()
                .first()
            )
            if not current:
                raise PlatformError(401, "access_revoked", "账号已停用。")
            try:
                hasher.verify(current["password_hash"], old)
            except VerificationError:
                raise PlatformError(
                    400, "invalid_password", "当前密码不正确。"
                ) from None
            connection.execute(
                update(t.users)
                .where(t.users.c.id == user["id"])
                .values(
                    password_hash=hasher.hash(new),
                    auth_version=t.users.c.auth_version + 1,
                )
            )
            connection.execute(
                delete(t.auth_sessions).where(t.auth_sessions.c.user_id == user["id"])
            )
            connection.execute(
                insert(nt.operation_audits).values(
                    id=uid(),
                    actor_user_id=user["id"],
                    action="auth.password_changed",
                    target=user["id"],
                    created_at=now(),
                )
            )

    def save_agent(self, user: dict, values: dict, agent_id: str | None = None) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            return self._save_agent(connection, user, values, agent_id)

    def _save_agent(
        self,
        connection,
        user: dict,
        values: dict,
        agent_id: str | None = None,
        *,
        managed_remote=False,
    ) -> dict:
        self.require_current(connection, user, admin=True)
        if not agent_id:
            maximum = connection.scalar(
                select(nt.entitlements.c.max_agents).where(
                    nt.entitlements.c.tenant_id == user["tenant_id"]
                )
            )
            count = connection.scalar(
                select(func.count())
                .select_from(t.agents)
                .where(t.agents.c.tenant_id == user["tenant_id"])
            )
            if maximum is not None and count >= maximum:
                raise PlatformError(
                    429, "agent_limit", "组织 Agent 数量已达到套餐上限。"
                )
        current = (
            self.owned(connection, t.agents, user, agent_id)
            if agent_id
            else {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "created_at": now(),
                "published_version": 0,
            }
        )
        if (
            current.get("remote_ref") or values.get("remote_ref")
        ) and not managed_remote:
            raise PlatformError(
                409,
                "remote_managed_agent",
                "远程 Agent 配置请通过远程版本注册接口更新。",
            )
        result = {**current, **values}
        from agent_platform.modules.agent_registry.contracts import AgentDeclaration

        try:
            result.update(AgentDeclaration.from_spec(result).model_dump())
        except ValueError as exc:
            raise PlatformError(422, "invalid_agent_declaration", str(exc)) from None
        try:
            # A nested PATCH merges declared fields; explicit null resets
            # an inherited field. Empty object resets the whole policy.
            if values.get("execution_policy"):
                result["execution_policy"] = {
                    **current.get("execution_policy", {}),
                    **values["execution_policy"],
                }
            policy = read_policy(result)
        except ValueError:
            raise PlatformError(
                422, "invalid_execution_policy", "Agent 执行策略无效。"
            ) from None
        if (
            policy.timeout_seconds
            and self.settings.run_timeout
            and policy.timeout_seconds > self.settings.run_timeout
        ):
            raise PlatformError(
                400,
                "execution_policy_limit",
                f"运行时限不能超过平台上限 {self.settings.run_timeout} 秒。",
            )
        if policy.max_cost and Decimal(policy.max_cost) > Decimal(
            self.settings.max_run_cost
        ):
            raise PlatformError(
                400,
                "execution_policy_limit",
                f"费用上限不能超过平台上限 {self.settings.max_run_cost} USD。",
            )
        result["execution_policy"] = policy.model_dump()
        if result.get("model_id"):
            model = self.owned(connection, t.models, user, result["model_id"])
            if result["max_tokens"] > model["max_output_tokens"]:
                raise PlatformError(
                    400, "output_limit_exceeded", "Agent 输出上限超过模型配置。"
                )
        if agent_id:
            connection.execute(
                update(t.agents).where(t.agents.c.id == agent_id).values(**result)
            )
        else:
            connection.execute(insert(t.agents).values(**result))
        audit(
            connection,
            user,
            "agent.update" if agent_id else "agent.create",
            result["id"],
        )
        return result

    def publish(self, user: dict, agent_id: str) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            return self._publish(connection, user, agent_id)

    def _publish(
        self, connection, user: dict, agent_id: str, *, managed_remote=False
    ) -> dict:
        self.require_current(connection, user, admin=True)
        agent = self.owned(connection, t.agents, user, agent_id)
        if agent.get("remote_ref") and not managed_remote:
            raise PlatformError(
                409, "remote_managed_agent", "远程 Agent 版本由远程注册接口发布。"
            )
        if agent.get("model_id"):
            model = self.owned(connection, t.models, user, agent["model_id"])
            if not model["active"]:
                raise PlatformError(
                    400, "model_disabled", "请先启用 Agent 使用的模型。"
                )
        from agent_platform.modules.agent_registry.contracts import AgentDeclaration
        from agent_platform.modules.collaboration.team import freeze_team

        agent.update(AgentDeclaration.from_spec(agent).model_dump())
        team = freeze_team(self, connection, user, agent)
        if team is not None:
            agent["team_snapshot"] = team
        version = agent["published_version"] + 1
        connection.execute(
            insert(t.agent_versions).values(
                agent_id=agent_id,
                version=version,
                tenant_id=user["tenant_id"],
                spec=agent,
                created_at=now(),
            )
        )
        connection.execute(
            update(t.agents)
            .where(t.agents.c.id == agent_id)
            .values(published_version=version)
        )
        audit(connection, user, "agent.publish", f"{agent_id}:{version}")
        return {"version": version}

    def create_session(self, user: dict, agent_id: str, title: str | None) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.identity.assert_current(connection, user, "runs.execute")
            agent = self.owned(connection, t.agents, user, agent_id)
            if not agent["available"]:
                raise PlatformError(409, "agent_unavailable", "Agent 已停用。")
            if agent.get("remote_ref"):
                raise PlatformError(
                    409,
                    "remote_team_member_only",
                    "远程 Agent 通过团队任务接收明确分配的输入，请选择团队入口。",
                )
            if not agent["published_version"]:
                raise PlatformError(400, "agent_not_published", "请先发布 Agent。")
            result = {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "agent_id": agent_id,
                "title": title or "新会话",
                "created_at": now(),
            }
            connection.execute(insert(t.sessions).values(**result))
        return result

    def session_detail(self, user: dict, session_id: str) -> dict:
        from agent_platform.modules.reasoning import session_reasoning
        from agent_platform.modules.runtime.generative_ui import session_artifacts

        with self.db.read() as connection:
            result = self.owned(connection, t.sessions, user, session_id, own=True)
            result["reasoning"] = session_reasoning(connection, user, session_id)
            result["artifacts"] = session_artifacts(
                connection, user["tenant_id"], session_id
            )
            result["messages"] = [
                dict(row)
                for row in connection.execute(
                    select(t.messages)
                    .where(t.messages.c.session_id == session_id)
                    .order_by(t.messages.c.created_at)
                ).mappings()
            ]
            result["runs"] = [
                self.public_run(dict(row))
                for row in connection.execute(
                    select(t.runs)
                    .where(t.runs.c.session_id == session_id)
                    .order_by(t.runs.c.created_at.desc())
                ).mappings()
            ]
        return result

    def freeze_execution_spec(
        self, connection, user, spec, *, model_id=None, reasoning_effort=None
    ):
        # Clone nested JSON: members must never mutate another execution's spec.
        spec = json.loads(json.dumps(spec))
        execution_policy = read_policy(spec)
        if reasoning_effort is not None:
            execution_policy = read_policy(
                {
                    "execution_policy": {
                        **execution_policy.model_dump(),
                        "reasoning_effort": reasoning_effort,
                    }
                }
            )
        spec["execution_policy"] = execution_policy.model_dump()
        spec["execution_limits"] = effective_limits(execution_policy, self.settings)
        if spec.get("remote_ref"):
            from agent_platform.modules.remote_agents.registry import RemoteRegistry

            registration, endpoints = RemoteRegistry(self).eligible(
                connection, user["tenant_id"], spec["remote_ref"]
            )
            if (
                registration["agent_id"] != spec["id"]
                or registration["descriptor"]["task_contracts"]
                != spec["task_contracts"]
            ):
                raise PlatformError(
                    409, "remote_binding_invalid", "远程版本与成员契约不一致。"
                )
            if not self.settings.collaboration_durable_enabled:
                raise PlatformError(
                    409, "remote_requires_journal", "远程协作需要开启持久执行日志。"
                )
            spec["remote_snapshot"] = {
                "registration_id": registration["id"],
                "version": registration["version"],
                "descriptor": registration["descriptor"],
                "digest": registration["digest"],
                "endpoint_ids": [point["id"] for point in endpoints],
            }
            spec["membership_id"] = user.get("membership_id")
            return spec
        policy = (
            connection.execute(
                select(nt.tenant_settings).where(
                    nt.tenant_settings.c.tenant_id == user["tenant_id"]
                )
            )
            .mappings()
            .one()
        )

        def eligible_route(candidate):
            resolved = self.gateway.resolve(
                user["tenant_id"], candidate, connection=connection
            )
            declared = (
                connection.scalar(
                    select(gt.deployments.c.capabilities).where(
                        gt.deployments.c.id == resolved["deployment_id"]
                    )
                )
                or {}
            )
            if not declared.get("text") or (
                spec.get("tools") and not declared.get("tools")
            ):
                raise PlatformError(
                    400,
                    "model_capability_required",
                    "该模型部署不具备 Agent 所需的文本或工具能力。",
                )
            return resolved

        preferred = model_id or spec.get("model_id")
        if preferred:
            model = self.owned(connection, t.models, user, preferred)
            routing = eligible_route(model)
        else:
            rows = (
                connection.execute(
                    select(t.models)
                    .where(
                        t.models.c.tenant_id == user["tenant_id"],
                        t.models.c.active.is_(True),
                    )
                    .order_by(t.models.c.created_at, t.models.c.id)
                )
                .mappings()
                .all()
            )
            catalog = {row["id"]: dict(row) for row in rows}
            candidates = list(
                dict.fromkeys(
                    [
                        policy["default_model_id"],
                        *policy["ordered_model_ids"],
                        *catalog,
                    ]
                )
            )
            failure = None
            model = None
            for candidate in candidates:
                if candidate not in catalog:
                    continue
                try:
                    candidate_route = eligible_route(catalog[candidate])
                except PlatformError as exc:
                    failure = exc
                    continue
                model, routing = catalog[candidate], candidate_route
                break
            if model is None:
                raise failure or PlatformError(
                    400, "no_available_model", "暂无已授权且网关就绪的模型。"
                )
        if not model["active"]:
            raise PlatformError(400, "model_disabled", "该模型已停用。")
        # Freeze model identity and rate card for every call of this run.
        if model_id or not spec.get("model_id"):
            spec["max_tokens"] = min(spec["max_tokens"], model["max_output_tokens"])
        spec["model_id"] = model["id"]
        spec["model"] = model
        spec["deployment_id"] = routing["deployment_id"]
        spec["route"] = routing["route"]
        spec["price_version_id"] = routing["price_version_id"]
        spec["config_version"] = routing["config_version"]
        spec["membership_id"] = user.get("membership_id")
        return spec

    def enqueue(
        self,
        user: dict,
        session_id: str,
        message: str,
        idempotency_key: str,
        model_id: str | None = None,
        *,
        reasoning_effort: ReasoningEffort | None = None,
        ag_ui: dict | None = None,
        admit_stream: bool = False,
        resume_sequence: int = 0,
    ) -> dict:
        request = {"session_id": session_id, "message": message}
        if model_id is not None:
            request["model_id"] = model_id
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        if ag_ui is not None:
            request["ag_ui"] = ag_ui
        fingerprint = digest(json.dumps(request, sort_keys=True))
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.identity.assert_current(connection, user, "runs.execute")
            session = self.owned(connection, t.sessions, user, session_id, own=True)
            if session["user_id"] != user["id"]:
                raise PlatformError(
                    403, "session_owner_required", "只能在自己的会话中发起运行。"
                )
            previous = (
                connection.execute(
                    select(t.runs).where(
                        t.runs.c.tenant_id == user["tenant_id"],
                        t.runs.c.user_id == user["id"],
                        t.runs.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise PlatformError(
                        409, "idempotency_conflict", "同一幂等键不能用于不同的请求。"
                    )
                result = self.public_run(dict(previous))
                if admit_stream:
                    result["_stream_id"] = self._acquire_stream(
                        connection, user, result["id"]
                    )
                return result
            if resume_sequence:
                raise PlatformError(400, "invalid_cursor", "新运行不能携带重放游标。")
            if ag_ui is not None:
                correlations = connection.scalars(
                    select(t.runs.c.spec).where(t.runs.c.session_id == session_id)
                )
                if any(
                    spec.get("ag_ui", {}).get("message_id") == ag_ui["message_id"]
                    for spec in correlations
                ):
                    raise PlatformError(
                        409, "message_id_conflict", "该消息标识已用于其他运行。"
                    )
            if connection.scalar(
                select(func.count())
                .select_from(t.runs)
                .where(
                    t.runs.c.session_id == session_id,
                    t.runs.c.status.in_(ACTIVE_STATUSES),
                )
            ):
                raise PlatformError(
                    409, "session_busy", "会话中已有运行，请等待完成或取消。"
                )
            if connection.scalar(
                select(func.count())
                .select_from(t.runs)
                .where(
                    t.runs.c.tenant_id == user["tenant_id"],
                    t.runs.c.status == "queued",
                )
            ) >= connection.scalar(
                select(nt.entitlements.c.max_queued_runs).where(
                    nt.entitlements.c.tenant_id == user["tenant_id"]
                )
            ):
                raise PlatformError(429, "queue_full", "组织运行队列已满，请稍后再试。")
            agent = self.owned(connection, t.agents, user, session["agent_id"])
            version = (
                connection.execute(
                    select(t.agent_versions).where(
                        t.agent_versions.c.agent_id == agent["id"],
                        t.agent_versions.c.version == agent["published_version"],
                    )
                )
                .mappings()
                .first()
            )
            if not version:
                raise PlatformError(400, "agent_not_published", "Agent 尚未发布。")
            if not agent["available"]:
                raise PlatformError(409, "agent_unavailable", "Agent 已停用。")
            spec = dict(version["spec"], version=version["version"])
            if spec.get("execution_mode") == "team":
                enabled = connection.scalar(
                    select(nt.tenant_settings.c.collaboration_enabled).where(
                        nt.tenant_settings.c.tenant_id == user["tenant_id"]
                    )
                )
                if not self.settings.collaboration_enabled or not enabled:
                    raise PlatformError(
                        409, "collaboration_disabled", "团队执行尚未开放。"
                    )
            spec["snapshot_schema_version"] = 2
            spec = self.freeze_execution_spec(
                connection,
                user,
                spec,
                model_id=model_id,
                reasoning_effort=reasoning_effort,
            )
            if spec.get("execution_mode") == "team":
                from agent_platform.modules.collaboration.team import freeze_team

                team = freeze_team(self, connection, user, spec)
                if (
                    team["definition"]["planning"] == "dynamic"
                    and not self.settings.collaboration_dynamic_enabled
                ):
                    raise PlatformError(
                        409, "dynamic_planning_disabled", "动态协作规划尚未开放。"
                    )
                team["features"] = {
                    "distributed": self.settings.collaboration_distributed_enabled,
                    "dynamic": self.settings.collaboration_dynamic_enabled,
                    "parallel": self.settings.collaboration_parallel_enabled,
                    "durable": self.settings.collaboration_durable_enabled,
                    "recovery": self.settings.collaboration_durable_enabled
                    and self.settings.collaboration_recovery_enabled,
                }
                if team["features"]["distributed"] and not team["features"]["durable"]:
                    raise PlatformError(
                        409,
                        "distributed_requires_journal",
                        "跨 Worker 协作需要开启持久执行日志。",
                    )
                if (
                    team["features"]["durable"]
                    and not self.settings.secret_encryption_key
                ):
                    raise PlatformError(
                        409, "journal_key_missing", "持久执行日志需要配置加密密钥。"
                    )
                if (
                    team["definition"]["max_parallel"] > 1
                    and not self.settings.collaboration_parallel_enabled
                ):
                    raise PlatformError(
                        409, "parallel_execution_disabled", "成员并行执行尚未开放。"
                    )
                for member in team["members"].values():
                    member["spec"] = self.freeze_execution_spec(
                        connection, user, member["spec"]
                    )
                team["features"]["remote"] = any(
                    member["spec"].get("remote_snapshot")
                    for member in team["members"].values()
                )
                spec["team_snapshot"] = team
                spec["snapshot_schema_version"] = (
                    6
                    if team["features"]["remote"]
                    else 5
                    if team["features"]["distributed"]
                    else 4
                    if team["features"]["durable"]
                    else 3
                )
            if ag_ui is not None:
                # Only the API's validated protocol correlation belongs here;
                # client state, tools and prompts never enter the frozen spec.
                spec["ag_ui"] = ag_ui
            result = {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "created_at": now(),
                "queue_expires_at": time.time() + self.settings.queue_timeout,
                "session_id": session_id,
                "agent_name": agent["name"],
                "spec": spec,
                "message": message,
                "status": "queued",
                "error": "",
                "idempotency_key": idempotency_key,
                "request_hash": fingerprint,
                "cancel_requested": False,
                "fence": 0,
            }
            connection.execute(insert(t.runs).values(**result))
            connection.execute(
                insert(t.messages).values(
                    id=uid(),
                    tenant_id=user["tenant_id"],
                    created_at=now(),
                    session_id=session_id,
                    run_id=result["id"],
                    role="user",
                    content=message,
                )
            )
            if session["title"] == "新会话":
                connection.execute(
                    update(t.sessions)
                    .where(t.sessions.c.id == session_id)
                    .values(title=message[:60])
                )
            append_event(connection, result["id"], "run.queued", {"status": "queued"})
            public = self.public_run(result)
            if admit_stream:
                public["_stream_id"] = self._acquire_stream(
                    connection, user, result["id"]
                )
        return public

    @staticmethod
    def public_run(row: dict) -> dict:
        return {
            **{
                key: row.get(key)
                for key in (
                    "id",
                    "tenant_id",
                    "session_id",
                    "user_id",
                    "agent_name",
                    "created_at",
                    "status",
                    "outcome",
                    "error",
                    "finished_at",
                )
            },
            "model_id": row.get("spec", {}).get("model_id"),
            "model_alias": row.get("spec", {}).get("model", {}).get("alias"),
            "execution_mode": row.get("spec", {}).get("execution_mode", "single"),
        }

    def get_run(self, user: dict, run_id: str) -> dict:
        with self.db.read() as connection:
            run = self.owned(connection, t.runs, user, run_id, own=True)
            result = self.public_run(run)
            costs = self.call_records(user, run_id=run_id, limit=None)
            result["cost"] = str(sum((Decimal(x["cost"]) for x in costs), Decimal(0)))
            result["message"] = run["message"]
        return result

    def call_records(
        self,
        user: dict,
        *,
        run_id: str | None = None,
        limit: int | None = 200,
        cursor: str | None = None,
    ) -> list[dict]:
        """Financial facts override a possibly stale worker display projection."""
        from agent_platform.modules.remote_agents.projections import call_projection

        calls = call_projection()
        query = select(calls).where(calls.c.tenant_id == user["tenant_id"])
        if "usage.read_all" not in user.get("capabilities", ()):
            query = query.where(calls.c.user_id == user["id"])
        if run_id:
            query = query.where(calls.c.run_id == run_id)
        if cursor:
            stamp, identifier = self.decode_cursor(cursor)
            query = query.where(
                (calls.c.created_at < stamp)
                | ((calls.c.created_at == stamp) & (calls.c.id < identifier))
            )
        query = query.order_by(calls.c.created_at.desc(), calls.c.id.desc())
        if limit:
            query = query.limit(limit)
        with self.db.read() as connection:
            rows = [dict(row) for row in connection.execute(query).mappings()]
            if rows:
                evidence = {
                    row["call_id"]: row
                    for row in connection.execute(
                        select(reservations_table).where(
                            reservations_table.c.tenant_id == user["tenant_id"],
                            reservations_table.c.call_id.in_(
                                [row["id"] for row in rows]
                            ),
                        )
                    ).mappings()
                }
                for row in rows:
                    if receipt := evidence.get(row["id"]):
                        row.update(
                            status={"settled": "confirmed", "reserved": "running"}.get(
                                receipt["status"], receipt["status"]
                            ),
                            cost=str(receipt["cost"]),
                            input_tokens=receipt["input_tokens"] or 0,
                            output_tokens=receipt["output_tokens"] or 0,
                            provider_cost=str(receipt["provider_cost"])
                            if receipt["provider_cost"] is not None
                            else None,
                        )
                    row["usage_source"] = (
                        "remote_agent"
                        if row["call_kind"] == "remote"
                        else "litellm_gateway"
                    )
                    row.pop("raw_usage", None)
                    if "platform.costs.read" not in user.get("capabilities", ()):
                        row.pop("provider_cost", None)
        return rows

    def cancel_run(self, user: dict, run_id: str) -> dict:
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.require_current(connection, user)
            run = self.owned(connection, t.runs, user, run_id, own=True)
            if run["status"] not in TERMINAL_STATUSES:
                status = (
                    "cancelled"
                    if run["status"] == "queued" and not run.get("recovery_count")
                    else "cancelling"
                )
                changes = {"cancel_requested": True, "status": status}
                if status == "cancelled":
                    changes["finished_at"] = now()
                connection.execute(
                    update(t.runs).where(t.runs.c.id == run_id).values(**changes)
                )
                append_event(
                    connection,
                    run_id,
                    "run.cancelled" if status == "cancelled" else "run.cancelling",
                    {"status": status},
                )
                audit(connection, user, "run.cancel", run_id)
        return self.get_run(user, run_id)

    def events(self, user: dict, run_id: str, after: int) -> list[dict]:
        with self.db.read() as connection:
            self.owned(connection, t.runs, user, run_id, own=True)
            return [
                dict(row)
                for row in connection.execute(
                    select(t.run_events)
                    .where(
                        t.run_events.c.run_id == run_id, t.run_events.c.sequence > after
                    )
                    .order_by(t.run_events.c.sequence)
                    .limit(200)
                ).mappings()
            ]

    def dashboard(self, user: dict) -> dict:
        """Aggregate in storage, preserving exact money even for local SQLite."""
        from agent_platform.modules.remote_agents.projections import call_projection

        calls = call_projection()
        receipt = reservations_table
        joined = calls.outerjoin(
            receipt,
            (receipt.c.tenant_id == calls.c.tenant_id)
            & (receipt.c.call_id == calls.c.id),
        )
        filters = [calls.c.tenant_id == user["tenant_id"]]
        all_usage = "usage.read_all" in user.get("capabilities", ())
        if not all_usage:
            filters.append(calls.c.user_id == user["id"])
        cost = func.coalesce(
            receipt.c.cost,
            calls.c.cost if self.db.sqlite else cast(calls.c.cost, Numeric(30, 12)),
        )
        cost_sum = (
            func.exact_sum(cost)
            if self.db.sqlite
            else func.sum(cast(cost, Numeric(30, 12)))
        )
        tokens = func.coalesce(
            receipt.c.input_tokens, calls.c.input_tokens, 0
        ) + func.coalesce(receipt.c.output_tokens, calls.c.output_tokens, 0)
        confirmed = case(
            (receipt.c.status == "settled", 1),
            ((receipt.c.id.is_(None)) & (calls.c.status == "confirmed"), 1),
            else_=0,
        )
        day = (
            func.strftime("%Y-%m-%d", calls.c.created_at, "+8 hours")
            if self.db.sqlite
            else func.to_char(
                func.timezone(
                    "Asia/Shanghai", cast(calls.c.created_at, DateTime(timezone=True))
                ),
                "YYYY-MM-DD",
            )
        )
        columns = [
            func.count().label("requests"),
            func.coalesce(func.sum(tokens), 0).label("tokens"),
            cost_sum.label("cost"),
        ]
        with self.db.read() as connection:
            totals = dict(
                connection.execute(
                    select(*columns, func.sum(confirmed).label("succeeded"))
                    .select_from(joined)
                    .where(*filters)
                )
                .mappings()
                .one()
            )
            daily = [
                dict(row)
                for row in connection.execute(
                    select(day.label("date"), *columns)
                    .select_from(joined)
                    .where(*filters)
                    .group_by(day)
                    .order_by(day.desc())
                    .limit(30)
                ).mappings()
            ]
            models = [
                dict(row)
                for row in connection.execute(
                    select(calls.c.model.label("model"), *columns)
                    .select_from(joined)
                    .where(*filters)
                    .group_by(calls.c.model)
                    .order_by(func.count().desc())
                    .limit(100)
                ).mappings()
            ]
            active_filter = [
                t.runs.c.tenant_id == user["tenant_id"],
                t.runs.c.status.in_(ACTIVE_STATUSES),
            ]
            if not all_usage:
                active_filter.append(t.runs.c.user_id == user["id"])
            active = connection.scalar(
                select(func.count()).select_from(t.runs).where(*active_filter)
            )
        for row in [totals, *daily, *models]:
            row["cost"] = str(row["cost"] or 0)
        result = {
            "requests": totals["requests"],
            "tokens": totals["tokens"],
            "cost": totals["cost"],
            "success_rate": round(
                (totals["succeeded"] or 0) / totals["requests"] * 100, 1
            )
            if totals["requests"]
            else 0,
            "active_runs": active,
            "daily": list(reversed(daily)),
            "models": models,
            "gateway_configured": self.gateway.status(user["tenant_id"]).get("status")
            in {"ready", "legacy"},
        }
        if "billing.read" in user.get("capabilities", ()):
            result.update(self.billing.wallet(user["tenant_id"]))
        return result
