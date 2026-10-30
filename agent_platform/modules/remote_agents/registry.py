"""Tenant-bound remote versions, private credentials and leased health probes."""

import asyncio
import time

from pydantic import Field, SecretStr, model_validator
from sqlalchemy import and_, insert, or_, select, update

from agent_platform.infrastructure import remote_agent_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.agent_registry.contracts import Identifier
from agent_platform.modules.run_records import now, uid
from agent_platform.modules.runtime.contracts import canonical_digest
from agent_platform.modules.runtime.journal import EvidenceCodec

from .contracts import RemoteDescriptor, RemoteHealth, WireModel
from .transport import Endpoint, RemoteTransport


class CredentialCreate(WireModel):
    name: str = Field(min_length=1, max_length=120)
    token: SecretStr = Field(min_length=1, max_length=8192)


class EndpointRegistration(WireModel):
    url: str = Field(min_length=1, max_length=2048)
    credential_id: Identifier


class RegistrationCreate(WireModel):
    idempotency_key: Identifier
    agent_id: Identifier | None = None
    expected_version: int = Field(default=0, ge=0)
    descriptor: RemoteDescriptor
    endpoints: list[EndpointRegistration] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def coherent_version(self):
        if bool(self.agent_id) != bool(self.expected_version):
            raise ValueError(
                "Existing remote Agents require their current published version"
            )
        if len({endpoint.url for endpoint in self.endpoints}) != len(self.endpoints):
            raise ValueError("Remote endpoints must be unique")
        return self


class AvailabilityChange(WireModel):
    enabled: bool


class RemoteRegistry:
    def __init__(self, platform):
        self.platform, self.db, self.settings = platform, platform.db, platform.settings

    def codec(self):
        if not self.settings.secret_encryption_key:
            raise PlatformError(409, "remote_key_missing", "远程凭据需要平台加密密钥。")
        return EvidenceCodec(self.settings.secret_encryption_key)

    @staticmethod
    def owned(connection, table, tenant, identifier):
        row = (
            connection.execute(
                select(table).where(
                    table.c.tenant_id == tenant, table.c.id == identifier
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PlatformError(404, "remote_not_found", "远程配置不存在或无权访问。")
        return dict(row)

    def authorize(self, connection, user):
        return self.platform.identity.assert_current(connection, user, "agents.manage")

    def create_credential(self, user, values):
        from agent_platform.modules.platform import audit

        values = CredentialCreate.model_validate(values)
        token = values.token.get_secret_value()
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise PlatformError(
                422, "invalid_remote_credential", "远程凭据不能包含空白或控制字符。"
            )
        identifier = uid()
        digest, ciphertext = self.codec().encode(
            {
                "tenant_id": user["tenant_id"],
                "credential_id": identifier,
                "token": token,
            }
        )
        row = {
            "id": identifier,
            "tenant_id": user["tenant_id"],
            "name": values.name,
            "active": True,
            "created_at": now(),
        }
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.authorize(connection, user)
            connection.execute(insert(rt.credentials).values(**row))
            connection.execute(
                insert(rt.secrets).values(
                    id=identifier,
                    tenant_id=user["tenant_id"],
                    created_at=row["created_at"],
                    digest=digest,
                    ciphertext=ciphertext,
                )
            )
            audit(connection, user, "remote.credential.create", identifier)
        return row

    def set_credential(self, user, identifier, enabled):
        from agent_platform.modules.platform import audit

        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.authorize(connection, user)
            self.owned(connection, rt.credentials, user["tenant_id"], identifier)
            connection.execute(
                update(rt.credentials)
                .where(rt.credentials.c.id == identifier)
                .values(active=enabled)
            )
            connection.execute(
                update(rt.endpoints)
                .where(
                    rt.endpoints.c.tenant_id == user["tenant_id"],
                    rt.endpoints.c.credential_id == identifier,
                )
                .values(
                    health_status="pending" if enabled else "unhealthy",
                    health_until=0,
                    next_probe_at=0,
                    probe_owner=None,
                    probe_until=None,
                    probe_fence=rt.endpoints.c.probe_fence + 1,
                )
            )
            audit(
                connection,
                user,
                "remote.credential.enable" if enabled else "remote.credential.revoke",
                identifier,
            )
        return {"id": identifier, "enabled": enabled}

    def credential(self, connection, tenant, identifier):
        metadata = self.owned(connection, rt.credentials, tenant, identifier)
        if not metadata["active"]:
            raise PlatformError(403, "remote_credential_revoked", "远程凭据已停用。")
        secret = self.owned(connection, rt.secrets, tenant, identifier)
        value = self.codec().decode(secret["ciphertext"], secret["digest"])
        if value.get("tenant_id") != tenant or value.get("credential_id") != identifier:
            raise PlatformError(409, "remote_credential_invalid", "远程凭据绑定无效。")
        return value["token"]

    def register(self, user, values):
        from agent_platform.modules.platform import audit

        values = RegistrationCreate.model_validate(values)
        normalized = []
        for endpoint in values.endpoints:
            parsed = Endpoint.parse(
                endpoint.url, allowed_origins=self.settings.remote_agent_allowed_origins
            )
            normalized.append(
                {
                    "url": parsed.origin + parsed.path,
                    "credential_id": endpoint.credential_id,
                }
            )
        if len({endpoint["url"] for endpoint in normalized}) != len(normalized):
            raise PlatformError(422, "duplicate_remote_endpoint", "远程端点不能重复。")
        fingerprint = canonical_digest({**values.model_dump(), "endpoints": normalized})
        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.authorize(connection, user)
            existing = (
                connection.execute(
                    select(rt.versions).where(
                        rt.versions.c.tenant_id == user["tenant_id"],
                        rt.versions.c.idempotency_key == values.idempotency_key,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_digest"] != fingerprint:
                    raise PlatformError(
                        409, "idempotency_conflict", "注册标识已用于不同配置。"
                    )
                return self.public(connection, dict(existing))
            current = None
            if values.agent_id:
                current = self.owned(
                    connection, t.agents, user["tenant_id"], values.agent_id
                )
                if (
                    not current["remote_ref"]
                    or current["published_version"] != values.expected_version
                ):
                    raise PlatformError(
                        409,
                        "remote_version_conflict",
                        "远程 Agent 版本已变化，请刷新后重试。",
                    )
            for endpoint in normalized:
                credential = self.owned(
                    connection,
                    rt.credentials,
                    user["tenant_id"],
                    endpoint["credential_id"],
                )
                if not credential["active"]:
                    raise PlatformError(
                        409, "remote_credential_revoked", "远程凭据已停用。"
                    )
            identifier = uid()
            descriptor = values.descriptor
            version = values.expected_version + 1
            reference = {"id": identifier, "version": version}
            agent = self.platform._save_agent(
                connection,
                user,
                {
                    "name": descriptor.name[:120],
                    "description": descriptor.description,
                    "system_prompt": "远程成员仅接收明确分配的任务输入。",
                    "model_id": None,
                    "temperature": 1,
                    "max_steps": 1,
                    "max_tokens": 1,
                    "tools": [],
                    "execution_policy": {
                        "timeout_seconds": min(
                            descriptor.timeout_seconds, self.settings.run_timeout
                        )
                        if self.settings.run_timeout
                        else descriptor.timeout_seconds
                    },
                    "execution_mode": "single",
                    "capability_keys": descriptor.capability_keys,
                    "task_contracts": [
                        contract.model_dump() for contract in descriptor.task_contracts
                    ],
                    "collaboration_spec": None,
                    "remote_ref": reference,
                },
                values.agent_id,
                managed_remote=True,
            )
            row = {
                "id": identifier,
                "tenant_id": user["tenant_id"],
                "agent_id": agent["id"],
                "version": version,
                "descriptor": descriptor.model_dump(),
                "digest": descriptor.digest,
                "enabled": True,
                "created_at": now(),
                "idempotency_key": values.idempotency_key,
                "request_digest": fingerprint,
            }
            connection.execute(insert(rt.versions).values(**row))
            for endpoint in normalized:
                connection.execute(
                    insert(rt.endpoints).values(
                        id=uid(),
                        tenant_id=user["tenant_id"],
                        registration_id=identifier,
                        created_at=now(),
                        **endpoint,
                        health_status="pending",
                        health_until=0,
                        health_error="",
                        probe_fence=0,
                        next_probe_at=0,
                    )
                )
            self.platform._publish(connection, user, agent["id"], managed_remote=True)
            audit(connection, user, "remote.register", identifier)
            return self.public(connection, row)

    def public(self, connection, row):
        points = (
            connection.execute(
                select(rt.endpoints)
                .where(
                    rt.endpoints.c.tenant_id == row["tenant_id"],
                    rt.endpoints.c.registration_id == row["id"],
                )
                .order_by(rt.endpoints.c.id)
            )
            .mappings()
            .all()
        )
        return {
            key: row[key]
            for key in (
                "id",
                "agent_id",
                "version",
                "descriptor",
                "digest",
                "enabled",
                "created_at",
            )
        } | {
            "endpoints": [
                {
                    key: point[key]
                    for key in (
                        "id",
                        "url",
                        "credential_id",
                        "health_status",
                        "health_until",
                        "health_error",
                    )
                }
                for point in points
            ],
        }

    def list(self, user, *, cursor=None, limit=50, credentials=False):
        table = rt.credentials if credentials else rt.versions
        with self.db.tenant_scope(user["tenant_id"]), self.db.read() as connection:
            self.authorize(connection, user)
            query = select(table).where(table.c.tenant_id == user["tenant_id"])
            if cursor:
                query = query.where(table.c.id > cursor)
            rows = [
                dict(row)
                for row in connection.execute(
                    query.order_by(table.c.id).limit(limit + 1)
                ).mappings()
            ]
            return {
                "items": rows[:limit]
                if credentials
                else [self.public(connection, row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

    def get(self, user, identifier):
        with self.db.tenant_scope(user["tenant_id"]), self.db.read() as connection:
            self.authorize(connection, user)
            return self.public(
                connection,
                self.owned(connection, rt.versions, user["tenant_id"], identifier),
            )

    def set_enabled(self, user, identifier, enabled):
        from agent_platform.modules.platform import audit

        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.authorize(connection, user)
            self.owned(connection, rt.versions, user["tenant_id"], identifier)
            connection.execute(
                update(rt.versions)
                .where(rt.versions.c.id == identifier)
                .values(enabled=enabled)
            )
            self._invalidate(connection, identifier)
            audit(
                connection,
                user,
                "remote.enable" if enabled else "remote.disable",
                identifier,
            )
        return self.get(user, identifier)

    @staticmethod
    def _invalidate(connection, identifier):
        connection.execute(
            update(rt.endpoints)
            .where(rt.endpoints.c.registration_id == identifier)
            .values(
                health_status="pending",
                health_until=0,
                health_error="",
                next_probe_at=0,
                probe_owner=None,
                probe_until=None,
                probe_fence=rt.endpoints.c.probe_fence + 1,
            )
        )

    def request_probe(self, user, identifier):
        from agent_platform.modules.platform import audit

        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.authorize(connection, user)
            self.owned(connection, rt.versions, user["tenant_id"], identifier)
            self._invalidate(connection, identifier)
            audit(connection, user, "remote.probe.request", identifier)
        return {"id": identifier, "status": "pending"}

    def eligible(self, connection, tenant, reference, *, endpoint_ids=None):
        if not self.settings.remote_agents_enabled:
            raise PlatformError(
                409, "remote_agents_disabled", "远程 Agent 执行尚未开放。"
            )
        row = self.owned(connection, rt.versions, tenant, reference["id"])
        descriptor = RemoteDescriptor.model_validate(row["descriptor"])
        if (
            row["version"] != reference["version"]
            or row["digest"] != descriptor.digest
            or not row["enabled"]
        ):
            raise PlatformError(
                409, "remote_unavailable", "远程版本不可用或描述无法验证。"
            )
        agent = self.owned(connection, t.agents, tenant, row["agent_id"])
        if not agent["available"]:
            raise PlatformError(403, "agent_unavailable", "远程 Agent 已停用。")
        query = (
            select(rt.endpoints)
            .join(
                rt.credentials,
                and_(
                    rt.credentials.c.tenant_id == rt.endpoints.c.tenant_id,
                    rt.credentials.c.id == rt.endpoints.c.credential_id,
                ),
            )
            .where(
                rt.endpoints.c.tenant_id == tenant,
                rt.endpoints.c.registration_id == row["id"],
                rt.endpoints.c.health_status == "healthy",
                rt.endpoints.c.health_until > time.time(),
                rt.credentials.c.active.is_(True),
            )
            .order_by(rt.endpoints.c.id)
        )
        if endpoint_ids is not None:
            query = query.where(rt.endpoints.c.id.in_(endpoint_ids))
        healthy = []
        for point in connection.execute(query).mappings():
            try:
                Endpoint.parse(
                    point["url"],
                    allowed_origins=self.settings.remote_agent_allowed_origins,
                )
            except PlatformError:
                continue
            healthy.append(dict(point))
        if not healthy:
            raise PlatformError(
                409, "remote_unavailable", "没有健康且获授权的远程端点。"
            )
        return row, healthy


class RemoteHealthQueue:
    def __init__(self, worker, *, transport=None):
        self.worker, self.db = worker, worker.db
        self.registry = RemoteRegistry(worker.platform)
        self.transport = transport or RemoteTransport(
            worker.settings.remote_agent_allowed_origins
        )
        self.cursor = ""

    def claim(self):
        if not self.worker.settings.remote_agents_enabled:
            return None
        for tenant in self.worker._tenant_page(self.cursor, active=True, limit=32):
            self.cursor = tenant
            with (
                self.db.tenant_scope(tenant),
                self.db.transaction(f"tenant:{tenant}") as connection,
            ):
                stamp = time.time()
                row = (
                    connection.execute(
                        select(rt.endpoints)
                        .join(
                            rt.versions,
                            rt.versions.c.id == rt.endpoints.c.registration_id,
                        )
                        .join(
                            rt.credentials,
                            rt.credentials.c.id == rt.endpoints.c.credential_id,
                        )
                        .join(t.agents, t.agents.c.id == rt.versions.c.agent_id)
                        .where(
                            rt.endpoints.c.tenant_id == tenant,
                            rt.versions.c.enabled.is_(True),
                            rt.credentials.c.active.is_(True),
                            t.agents.c.available.is_(True),
                            rt.endpoints.c.next_probe_at <= stamp,
                            or_(
                                rt.endpoints.c.probe_until.is_(None),
                                rt.endpoints.c.probe_until <= stamp,
                            ),
                        )
                        .order_by(rt.endpoints.c.next_probe_at, rt.endpoints.c.id)
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if row:
                    changes = {
                        "probe_owner": self.worker.owner,
                        "probe_fence": row["probe_fence"] + 1,
                        "probe_until": stamp + 20,
                    }
                    connection.execute(
                        update(rt.endpoints)
                        .where(rt.endpoints.c.id == row["id"])
                        .values(**changes)
                    )
                    return dict(row) | changes
        return None

    async def execute(self, claim):
        tenant = claim["tenant_id"]
        healthy, error = False, "remote_probe_failed"
        with self.db.tenant_scope(tenant):
            try:
                with self.db.read() as connection:
                    registration = self.registry.owned(
                        connection, rt.versions, tenant, claim["registration_id"]
                    )
                    token = self.registry.credential(
                        connection, tenant, claim["credential_id"]
                    )
                result = RemoteHealth.model_validate(
                    await self.transport.request(
                        claim["url"], "/health", token=token, timeout=10
                    )
                )
                descriptor = RemoteDescriptor.model_validate(registration["descriptor"])
                healthy = result.healthy and (
                    result.remote_agent_id,
                    result.remote_version,
                    result.descriptor_digest,
                ) == (
                    descriptor.remote_agent_id,
                    descriptor.remote_version,
                    descriptor.digest,
                )
                error = "" if healthy else "remote_identity_mismatch"
            except (PlatformError, ValueError, OSError, TimeoutError):
                pass
            await asyncio.to_thread(self.finish, claim, healthy, error)

    def finish(self, claim, healthy, error):
        with self.db.transaction(f"tenant:{claim['tenant_id']}") as connection:
            # Credential/availability edits invalidate the probe fence atomically.
            changed = connection.execute(
                update(rt.endpoints)
                .where(
                    rt.endpoints.c.id == claim["id"],
                    rt.endpoints.c.tenant_id == claim["tenant_id"],
                    rt.endpoints.c.probe_owner == self.worker.owner,
                    rt.endpoints.c.probe_fence == claim["probe_fence"],
                    rt.endpoints.c.probe_until > time.time(),
                )
                .values(
                    health_status="healthy" if healthy else "unhealthy",
                    health_until=time.time() + 60 if healthy else 0,
                    health_error=error,
                    next_probe_at=time.time() + 30,
                    probe_owner=None,
                    probe_until=None,
                )
            )
            return changed.rowcount == 1
