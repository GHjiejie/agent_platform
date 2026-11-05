"""Discover upstream models and provision routes through the existing control service."""

import hashlib
import http.client
import time
from uuid import uuid4

from sqlalchemy import or_, select, update

from agent_platform.infrastructure import gateway_tables as g
from agent_platform.infrastructure import provider_tables as p
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.authorization import capabilities
from agent_platform.modules.gateway_control import (
    ControlFailure,
    _now,
    _url,
)
from agent_platform.modules.remote_agents.transport import (
    Endpoint,
    PinnedHTTPSConnection,
    decode_json,
    public_addresses,
)

MAX_BYTES = 2 * 1024 * 1024


class ProviderClient:
    def list_models(self, base, token):
        """OpenAI-compatible GET /models; public HTTPS, pinned DNS, no redirect/proxy."""
        try:
            endpoint = Endpoint.parse(base)
            address = public_addresses(endpoint.host, endpoint.port)[0]
            deadline = time.monotonic() + 20
            connection = PinnedHTTPSConnection(endpoint, address, 15)
            try:
                connection.request(
                    "GET",
                    endpoint.path + "/models",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/json",
                        "Host": endpoint.authority,
                    },
                )
                response = connection.getresponse()
                if response.status in (401, 403):
                    raise PlatformError(
                        502,
                        "provider_auth_failed",
                        "服务商拒绝了 API Key，请检查密钥权限。",
                    )
                if response.status != 200:
                    raise PlatformError(
                        502,
                        "provider_http_failed",
                        "服务商模型接口暂不可用，请稍后重新获取。",
                    )
                if (
                    response.getheader("Content-Type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise ValueError()
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    if connection.sock:
                        connection.sock.settimeout(min(15, remaining))
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError()
                return parse_models(decode_json(b"".join(chunks)))
            finally:
                connection.close()
        except PlatformError as exc:
            if exc.code.startswith("remote_"):
                raise PlatformError(
                    422,
                    "provider_address_invalid",
                    "服务商地址必须是可访问的公网 HTTPS 地址。",
                ) from None
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise PlatformError(
                502,
                "provider_response_invalid",
                "无法读取服务商模型列表，请检查地址及接口兼容性。",
            ) from None


def parse_models(payload):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 10000:
        raise PlatformError(
            502, "provider_response_invalid", "服务商未返回有效的模型列表。"
        )
    models = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id")
        if (
            not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 200
            or any(ord(c) < 33 or ord(c) > 126 for c in identifier)
        ):
            continue
        name = row.get("name", identifier)
        owner = row.get("owned_by")
        context = next(
            (
                row[key]
                for key in ("context_window", "context_length", "max_model_len")
                if type(row.get(key)) is int and 1 <= row[key] <= 2000000
            ),
            None,
        )
        output = row.get("max_output_tokens")
        models[identifier] = {
            "id": identifier,
            "name": name[:200] if isinstance(name, str) and name else identifier,
            "owned_by": owner[:120] if isinstance(owner, str) else "",
            "context_window": context,
            "max_output_tokens": output
            if type(output) is int and 1 <= output <= 2000000
            else None,
        }
    if rows and not models:
        raise PlatformError(
            502, "provider_response_invalid", "服务商返回的模型没有有效标识。"
        )
    return sorted(models.values(), key=lambda item: item["id"])


class ProviderCatalog:
    def __init__(self, gateway, *, client=None):
        self.gateway, self.db, self.settings = gateway, gateway.db, gateway.settings
        self.client = client or ProviderClient()
        self.owner = uuid4().hex

    @staticmethod
    def authorize(conn, actor_id):
        # Read-only checks let the dedicated control role revalidate an operator
        # without granting it permission to change identities/platform roles.
        active = conn.scalar(
            select(t.users.c.id).where(
                t.users.c.id == actor_id,
                t.users.c.active.is_(True),
                t.users.c.global_status == "active",
            )
        )
        roles = conn.scalars(
            select(nt.platform_roles.c.role).where(
                nt.platform_roles.c.user_id == actor_id,
                nt.platform_roles.c.revoked_at.is_(None),
            )
        ).all()
        if not active or "platform.models.manage" not in capabilities(None, roles):
            raise PlatformError(
                403, "platform_role_required", "此操作需要当前有效的平台模型管理权限。"
            )

    def list(self, actor_id):
        with self.db.read() as conn:
            self.authorize(conn, actor_id)
            return {
                "items": [
                    dict(row)
                    for row in conn.execute(
                        select(p.providers).order_by(
                            p.providers.c.created_at, p.providers.c.id
                        )
                    ).mappings()
                ]
            }

    @staticmethod
    def provider(conn, identifier):
        row = (
            conn.execute(select(p.providers).where(p.providers.c.id == identifier))
            .mappings()
            .first()
        )
        if row is None:
            raise PlatformError(404, "provider_not_found", "服务商连接不存在。")
        return dict(row)

    def create(self, actor_id, *, name, api_base, api_key):
        try:
            endpoint = Endpoint.parse(api_base)
        except PlatformError:
            raise PlatformError(
                422,
                "provider_address_invalid",
                "请输入服务商 HTTPS API 地址，例如 https://api.example.com/v1。",
            ) from None
        if not name.strip():
            raise PlatformError(422, "provider_name_required", "请填写服务商名称。")
        # Reject literal private targets before persisting; resolved addresses are
        # checked again at the actual connection, so DNS changes cannot bypass it.
        import ipaddress

        try:
            address = ipaddress.ip_address(endpoint.host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise PlatformError(
                422, "provider_address_invalid", "服务商地址必须是公网 HTTPS 地址。"
            )
        identifier = uuid4().hex
        values = {
            "id": identifier,
            "name": name.strip(),
            "api_base": endpoint.origin + endpoint.path,
            "catalog": [],
            "catalog_updated_at": None,
            "created_at": _now(),
        }
        with self.db.transaction("gateway:providers") as conn:
            self.authorize(conn, actor_id)
            conn.execute(p.providers.insert().values(**values))
            conn.execute(
                p.secrets.insert().values(
                    provider_id=identifier,
                    ciphertext=self.gateway._cipher()
                    .encrypt(api_key.encode())
                    .decode(),
                )
            )
            self.gateway._audit(conn, None, actor_id, "provider.create", identifier)
        return values

    def enqueue(self, actor_id, provider_id, kind, model_id=None, tools=False):
        if kind not in {"discover", "import"}:
            raise PlatformError(
                422, "provider_operation_invalid", "不支持的服务商操作。"
            )
        with self.db.transaction("gateway:providers") as conn:
            self.authorize(conn, actor_id)
            provider = self.provider(conn, provider_id)
            if kind == "import":
                model = next(
                    (m for m in provider["catalog"] if m["id"] == model_id), None
                )
                if model is None:
                    raise PlatformError(
                        422,
                        "provider_model_not_discovered",
                        "请先从服务商获取并选择模型。",
                    )
                key = hashlib.sha256(
                    (provider_id + ":" + model_id).encode()
                ).hexdigest()
                previous = (
                    conn.execute(
                        select(p.operations).where(p.operations.c.dedupe_key == key)
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    return dict(previous)
                payload = {
                    "model_id": model_id,
                    "tools": tools,
                    "model_name": model["name"],
                }
            else:
                # Reuse an in-flight read to avoid duplicate refreshes; a completed
                # discovery is explicitly refreshed instead of a static cache.
                previous = (
                    conn.execute(
                        select(p.operations).where(
                            p.operations.c.provider_id == provider_id,
                            p.operations.c.kind == "discover",
                            p.operations.c.status.in_(("queued", "running")),
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    return dict(previous)
                key, payload = uuid4().hex, {}
            values = {
                "id": uuid4().hex,
                "provider_id": provider_id,
                "requested_by": actor_id,
                "kind": kind,
                "dedupe_key": key,
                "payload": payload,
                "result": {},
                "status": "queued",
                "error_code": "",
                "owner": None,
                "fence": 0,
                "lease_until": None,
                "attempts": 0,
                "next_poll_at": 0,
                "created_at": _now(),
                "updated_at": _now(),
                "sent_at": None,
            }
            conn.execute(p.operations.insert().values(**values))
            self.gateway._audit(
                conn,
                None,
                actor_id,
                "provider." + kind + "_requested",
                provider_id,
                operation_id=values["id"],
            )
            return values

    def operation(self, actor_id, identifier):
        with self.db.read() as conn:
            self.authorize(conn, actor_id)
            row = (
                conn.execute(
                    select(p.operations).where(p.operations.c.id == identifier)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PlatformError(
                    404, "provider_operation_not_found", "获取或接入记录不存在。"
                )
            return dict(row)

    def claim(self):
        with self.db.transaction("gateway:providers") as conn:
            clock = time.time()
            # A process may exit during its final attempt; never leave the UI
            # waiting on an expired 'running' lease that cannot be reclaimed.
            expired = (
                conn.execute(
                    select(p.operations).where(
                        p.operations.c.status == "running",
                        p.operations.c.attempts >= 3,
                        p.operations.c.lease_until <= clock,
                    )
                )
                .mappings()
                .all()
            )
            for row in expired:
                uncertain = row["kind"] == "import" and row["sent_at"] is not None
                conn.execute(
                    update(p.operations)
                    .where(p.operations.c.id == row["id"])
                    .values(
                        status="unknown" if uncertain else "failed",
                        owner=None,
                        lease_until=None,
                        error_code="provider_import_unknown"
                        if uncertain
                        else "provider_request_failed",
                        updated_at=_now(),
                    )
                )
            row = (
                conn.execute(
                    select(p.operations)
                    .where(
                        p.operations.c.status.in_(("queued", "running", "unknown")),
                        p.operations.c.attempts < 3,
                        p.operations.c.next_poll_at <= clock,
                        or_(
                            p.operations.c.lease_until.is_(None),
                            p.operations.c.lease_until <= clock,
                        ),
                    )
                    .order_by(p.operations.c.created_at, p.operations.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            changes = {
                "status": "running",
                "owner": self.owner,
                "fence": row["fence"] + 1,
                "lease_until": clock + 90,
                "attempts": row["attempts"] + 1,
                "updated_at": _now(),
            }
            conn.execute(
                update(p.operations)
                .where(p.operations.c.id == row["id"])
                .values(**changes)
            )
            return {**row, **changes}

    def guard(self, conn, claim, *, permission=True):
        row = (
            conn.execute(select(p.operations).where(p.operations.c.id == claim["id"]))
            .mappings()
            .one()
        )
        if (
            row["owner"] != self.owner
            or row["fence"] != claim["fence"]
            or (row["lease_until"] or 0) <= time.time()
        ):
            raise PlatformError(
                409, "provider_lease_lost", "处理权限已失效，请刷新状态。"
            )
        if permission:
            self.authorize(conn, claim["requested_by"])
        return row

    def execute(self, claim):
        with self.db.transaction("gateway:providers") as conn:
            row = dict(self.guard(conn, claim))
            provider = self.provider(conn, claim["provider_id"])
            ciphertext = conn.scalar(
                select(p.secrets.c.ciphertext).where(
                    p.secrets.c.provider_id == provider["id"]
                )
            )
        token = self.gateway._decrypt(ciphertext)
        if claim["kind"] == "discover":
            models = self.client.list_models(provider["api_base"], token)
            with self.db.transaction("gateway:providers") as conn:
                self.guard(conn, claim)
                conn.execute(
                    update(p.providers)
                    .where(p.providers.c.id == provider["id"])
                    .values(catalog=models, catalog_updated_at=_now())
                )
            return {"model_count": len(models)}
        target = hashlib.sha256(
            (provider["id"] + ":" + claim["payload"]["model_id"]).encode()
        ).hexdigest()[:32]
        route = "provider-" + provider["id"][:8] + "-" + target[:12]
        upstream = "openai/" + claim["payload"]["model_id"]
        adapter = self.gateway._control()
        info = adapter.provider_model_info(target)
        if info is None:
            if row["sent_at"] is not None:
                raise ControlFailure("provider_import_unknown", unknown=True)
            with self.db.transaction("gateway:providers") as conn:
                self.guard(conn, claim)
                conn.execute(
                    update(p.operations)
                    .where(p.operations.c.id == claim["id"])
                    .values(sent_at=time.time())
                )
            adapter.create_provider_model(
                target, route, upstream, provider["api_base"], token
            )
            info = adapter.provider_model_info(target)
        if info is None:
            raise ControlFailure("provider_import_unknown", unknown=True)
        params = info.get("litellm_params", {})
        if (
            info.get("model_name") != route
            or params.get("model") != upstream
            or str(params.get("api_base", "")).rstrip("/") != provider["api_base"]
        ):
            raise PlatformError(
                409,
                "provider_route_conflict",
                "网关模型信息与接入请求不一致，请由平台核查。",
            )
        deployment = {
            "id": target,
            "name": claim["payload"]["model_name"][:120],
            "created_at": _now(),
            "owner_scope": "platform",
            "owner_tenant_id": None,
            "gateway_id": "primary",
            "internal_route": route,
            "base_url": _url(self.settings.litellm_url),
            "protocol": "responses",
            "capabilities": {"text": True, "tools": claim["payload"]["tools"]},
            "status": "active",
            "config_version": 1,
        }
        with self.db.transaction("gateway:providers") as conn:
            self.guard(conn, claim)
            previous = (
                conn.execute(select(g.deployments).where(g.deployments.c.id == target))
                .mappings()
                .first()
            )
            if previous is None:
                conn.execute(g.deployments.insert().values(**deployment))
            elif previous["internal_route"] != route:
                raise PlatformError(
                    409, "provider_route_conflict", "平台部署绑定不一致。"
                )
        return {"deployment_id": target, "internal_route": route}

    def finish(self, claim, status, *, result=None, code=""):
        with self.db.transaction("gateway:providers") as conn:
            self.guard(conn, claim, permission=False)
            conn.execute(
                update(p.operations)
                .where(p.operations.c.id == claim["id"])
                .values(
                    status=status,
                    result=result or {},
                    error_code=code,
                    owner=None,
                    lease_until=None,
                    next_poll_at=time.time() + 30,
                    updated_at=_now(),
                )
            )

    def process_one(self):
        claim = self.claim()
        if claim is None:
            return False
        try:
            result = self.execute(claim)
            self.finish(claim, "succeeded", result=result)
        except ControlFailure as exc:
            self.finish(
                claim,
                "unknown" if exc.unknown else "failed",
                code="provider_import_unknown"
                if exc.unknown
                else "provider_gateway_failed",
            )
        except PlatformError as exc:
            if exc.code != "provider_lease_lost":
                self.finish(claim, "failed", code=exc.code)
        except Exception:  # noqa: BLE001 - persist only a stable, secret-free failure code
            # External/library exceptions may contain credentials; never store repr.
            self.finish(claim, "failed", code="provider_request_failed")
        return True
