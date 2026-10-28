"""Versioned tenant grants and an auditable, conservative LiteLLM control plane.

Inference workers only need ``resolve`` and the encryption key. Only the separate
provisioner receives the gateway master credential. No remote exception bodies or
plaintext credentials are put in database projections, events, or API responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError

from agent_platform.infrastructure import gateway_tables as g
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.request_context import audit_details
from agent_platform.modules.authorization import capabilities

ALLOWED_ROUTES = ["/responses", "/v1/responses"]
DEFAULT_LIMITS = {
    "max_budget": 100.0,
    "rpm_limit": 60,
    "tpm_limit": 120000,
    "max_parallel_requests": 4,
}
LIVE_JOBS = ("pending", "applying", "retry_wait", "reconciling")
logger = logging.getLogger(__name__)


def _id():
    return uuid4().hex


def _now():
    return datetime.now(UTC).isoformat()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _url(value):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
        return value.rstrip("/")
    except (TypeError, ValueError):
        raise PlatformError(422, "invalid_gateway_url", "网关地址格式无效。") from None


def _limits(values=None):
    values = {**DEFAULT_LIMITS, **(values or {})}
    if set(values) != set(DEFAULT_LIMITS):
        raise PlatformError(422, "invalid_gateway_limits", "网关限额字段无效。")
    for key, value in values.items():
        if isinstance(value, bool):
            raise PlatformError(422, "invalid_gateway_limits", "网关限额必须为正数。")
        try:
            number = float(value)
            if not math.isfinite(number) or not 0 < number <= 1_000_000_000:
                raise ValueError
            if key != "max_budget" and number != int(number):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise PlatformError(
                422, "invalid_gateway_limits", "网关限额必须为有限正数。"
            ) from None
        values[key] = number if key == "max_budget" else int(number)
    return values


def _price(value):
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result < 0
            or result >= Decimal("1e18")
            or result.as_tuple().exponent < -12
        ):
            raise ValueError
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise PlatformError(
            422, "invalid_price", "费率须为非负精确小数（最多 12 位小数）。"
        ) from None


class ControlFailure(Exception):
    """A stable error code, never a provider response, URL or credential."""

    def __init__(
        self, code, *, unknown=False, retryable=False, mutation_rejected=False
    ):
        super().__init__(code)
        self.code, self.unknown, self.retryable = code, unknown, retryable
        self.mutation_rejected = mutation_rejected


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiteLLMControlAdapter:
    """Endpoints inspected in the pinned LiteLLM 1.83.0 implementation.

    We supply a randomly generated, locally encrypted key to /key/generate.
    Its hash is therefore known before sending. A timed-out generation is only
    read back, never blindly retried. Gateways disabling custom keys fail closed.
    """

    def __init__(self, base_url, master_key):
        self.base_url = _url(base_url)
        self._master_key = master_key
        self._opener = build_opener(_NoRedirect())
        if not master_key:
            raise PlatformError(
                503, "gateway_control_not_configured", "网关同步进程未配置控制凭据。"
            )

    def _request(self, path, body=None, *, mutation=False, missing=False):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._master_key,
                "Content-Type": "application/json",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise TypeError
            return result
        except HTTPError as exc:
            if missing and exc.code == 404:
                return None
            uncertain = mutation and (exc.code >= 500 or exc.code in {408, 409, 429})
            raise ControlFailure(
                "gateway_control_http_" + str(exc.code),
                unknown=uncertain,
                retryable=not mutation and exc.code >= 500,
                mutation_rejected=mutation and 400 <= exc.code < 500 and not uncertain,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ControlFailure(
                "gateway_control_unavailable", unknown=mutation, retryable=not mutation
            ) from None
        except (TypeError, ValueError, UnicodeError):
            raise ControlFailure(
                "gateway_control_invalid_response", unknown=mutation
            ) from None

    def user_info(self, user_id):
        result = self._request(
            "/user/info?" + urlencode({"user_id": user_id}), missing=True
        )
        return None if result is None else result.get("user_info")

    def create_user(self, user_id, routes):
        self._request(
            "/user/new",
            {
                "user_id": user_id,
                "user_alias": user_id,
                "user_role": "internal_user",
                "auto_create_key": False,
                "models": routes,
            },
            mutation=True,
        )

    def key_info(self, identifier):
        result = self._request("/v2/key/info", {"keys": [identifier]}, missing=True)
        if result is None:
            return None
        rows = result.get("info")
        if not isinstance(rows, list) or len(rows) > 1:
            raise ControlFailure("gateway_key_info_invalid")
        return rows[0] if rows else None

    def create_key(self, credential, key):
        if secrets.compare_digest(key, self._master_key):
            raise ControlFailure("gateway_master_key_forbidden")
        result = self._request(
            "/key/generate",
            {
                "user_id": credential["external_user_id"],
                "key_alias": credential["key_alias"],
                "key": key,
                "models": credential["routes"],
                "key_type": "default",
                "allowed_routes": ALLOWED_ROUTES,
                "duration": "30d",
                **credential["limits"],
            },
            mutation=True,
        )
        returned = result.get("key")
        if not isinstance(returned, str) or not secrets.compare_digest(returned, key):
            raise ControlFailure("gateway_key_generation_mismatch", unknown=True)

    def delete_key(self, identifier):
        self._request("/key/delete", {"keys": [identifier]}, mutation=True)


class GatewayService:
    def __init__(self, db, settings, *, adapter=None):
        self.db, self.settings = db, settings
        self._adapter = adapter
        self.owner = _id()
        self._tenant_cursor = 0
        self._stats = {
            "processed": 0,
            "applied": 0,
            "failed": 0,
            "reconciling": 0,
            "retry_wait": 0,
            "internal_errors": 0,
            "last_error_code": "",
            "last_error_at": None,
            "last_success_at": None,
        }
        self._last_internal_warning = 0.0

    def statistics(self):
        """Process counters only; no credential, endpoint, prompt or tenant data."""
        return dict(self._stats)

    @contextmanager
    def _transaction(self, tenant_id=None, *, scope=None):
        try:
            with self.db.transaction(
                scope or ("tenant:" + tenant_id if tenant_id else "gateway:control")
            ) as connection:
                if tenant_id and not self.db.sqlite:
                    connection.execute(
                        text("SELECT set_config('app.tenant_id', :tenant, true)"),
                        {"tenant": tenant_id},
                    )
                yield connection
        except IntegrityError:
            raise PlatformError(
                409,
                "gateway_state_conflict",
                "模型或网关配置存在重复或关联冲突，请刷新后重试。",
            ) from None

    def _cipher(self):
        try:
            key = getattr(self.settings, "secret_encryption_key", "")
            if not key:
                raise ValueError
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (TypeError, ValueError):
            raise PlatformError(
                503, "secret_store_not_configured", "请配置有效的服务端凭据加密密钥。"
            ) from None

    def _decrypt(self, ciphertext):
        try:
            return self._cipher().decrypt(ciphertext.encode()).decode()
        except (InvalidToken, AttributeError, UnicodeError):
            raise PlatformError(
                503, "credential_unavailable", "凭据无法解密，请检查服务端密钥版本。"
            ) from None

    def _control(self):
        if self._adapter is None:
            self._adapter = LiteLLMControlAdapter(
                getattr(self.settings, "gateway_control_url", ""),
                getattr(self.settings, "gateway_control_key", ""),
            )
        return self._adapter

    @staticmethod
    def _tenant(connection, tenant_id):
        row = (
            connection.execute(
                select(t.tenants, nt.tenant_settings.c.status)
                .join(
                    nt.tenant_settings, nt.tenant_settings.c.tenant_id == t.tenants.c.id
                )
                .where(t.tenants.c.id == tenant_id)
            )
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError(404, "tenant_not_found", "组织不存在。")
        return dict(row)

    @staticmethod
    def _audit(connection, tenant_id, actor_id, action, target, **details):
        if tenant_id is None:
            connection.execute(
                nt.operation_audits.insert().values(
                    id=_id(),
                    actor_user_id=actor_id,
                    tenant_id=None,
                    action=action,
                    target=target,
                    reason="",
                    details=audit_details(details),
                    created_at=_now(),
                )
            )
            return
        connection.execute(
            g.audits.insert().values(
                id=_id(),
                created_at=_now(),
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=action,
                target_id=target,
                details=audit_details(details),
            )
        )

    @staticmethod
    def _binding(connection, tenant_id):
        row = (
            connection.execute(
                select(g.bindings).where(
                    g.bindings.c.tenant_id == tenant_id,
                    g.bindings.c.gateway_id == "primary",
                    g.bindings.c.purpose == "inference",
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    @staticmethod
    def _safe_binding(row):
        if not row:
            return {"status": "not_enrolled", "mode": "none"}
        return {
            key: row[key]
            for key in (
                "id",
                "tenant_id",
                "gateway_id",
                "purpose",
                "mode",
                "status",
                "desired_version",
                "applied_version",
                "error_code",
            )
        }

    def status(self, tenant_id):
        with self._transaction(tenant_id) as connection:
            self._tenant(connection, tenant_id)
            binding = self._binding(connection, tenant_id)
            result = self._safe_binding(binding)
            result["credentials"] = []
            result["operations"] = []
            result["operation_counts"] = dict(
                connection.execute(
                    select(g.outbox.c.status, func.count())
                    .where(g.outbox.c.tenant_id == tenant_id)
                    .group_by(g.outbox.c.status)
                ).all()
            )
            if binding:
                rows = connection.execute(
                    select(g.credentials)
                    .where(
                        g.credentials.c.tenant_id == tenant_id,
                        g.credentials.c.binding_id == binding["id"],
                    )
                    .order_by(g.credentials.c.generation.desc())
                ).mappings()
                result["credentials"] = [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "generation",
                            "status",
                            "expires_at",
                            "created_at",
                            "revoked_at",
                        )
                    }
                    | {"fingerprint": row["fingerprint"][:12]}
                    for row in rows
                ]
                rows = connection.execute(
                    select(g.outbox)
                    .where(
                        g.outbox.c.tenant_id == tenant_id,
                        g.outbox.c.binding_id == binding["id"],
                    )
                    .order_by(g.outbox.c.created_at.desc())
                    .limit(50)
                ).mappings()
                result["operations"] = [
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "action",
                            "status",
                            "phase",
                            "error_code",
                            "attempt_count",
                            "updated_at",
                        )
                    }
                    for row in rows
                ]
            return result

    @staticmethod
    def _authorize(connection, actor_id, capability):
        active = connection.scalar(
            select(t.users.c.id)
            .where(
                t.users.c.id == actor_id,
                t.users.c.active.is_(True),
                t.users.c.global_status == "active",
            )
            .with_for_update()
        )
        roles = connection.scalars(
            select(nt.platform_roles.c.role)
            .where(
                nt.platform_roles.c.user_id == actor_id,
                nt.platform_roles.c.revoked_at.is_(None),
            )
            .with_for_update()
        ).all()
        if not active or capability not in capabilities(None, roles):
            raise PlatformError(
                403, "platform_role_required", "此操作需要当前有效的平台运营权限。"
            )

    def create_deployment(self, values: dict, *, actor_id: str):
        """Register an already configured route; never accept an upstream API key."""
        allowed = {
            "name",
            "internal_route",
            "base_url",
            "capabilities",
            "owner_scope",
            "owner_tenant_id",
        }
        if set(values) - allowed or not actor_id:
            raise PlatformError(422, "invalid_deployment", "部署字段无效。")
        route = str(values.get("internal_route", "")).strip()
        if not route or route == "*" or len(route) > 200:
            raise PlatformError(
                422, "invalid_deployment", "必须指定单个网关内部模型路由。"
            )
        owner = values.get("owner_scope", "platform")
        tenant_id = values.get("owner_tenant_id")
        if owner not in {"platform", "tenant"} or (owner == "tenant") != bool(
            tenant_id
        ):
            raise PlatformError(422, "invalid_deployment_owner", "部署归属无效。")
        base = _url(values.get("base_url") or self.settings.litellm_url)
        if base != _url(self.settings.litellm_url):
            raise PlatformError(
                422, "unsupported_gateway", "当前控制面仅支持配置的 primary 网关。"
            )
        with self._transaction(tenant_id) as connection:
            self._authorize(connection, actor_id, "platform.models.manage")
            if tenant_id:
                self._tenant(connection, tenant_id)
            previous = (
                connection.execute(
                    select(g.deployments).where(
                        g.deployments.c.gateway_id == "primary",
                        g.deployments.c.internal_route == route,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if (
                    previous["owner_scope"] != owner
                    or previous["owner_tenant_id"] != tenant_id
                ):
                    raise PlatformError(
                        409, "deployment_route_owned", "该路由已有不同归属。"
                    )
                return dict(previous)
            result = {
                "id": _id(),
                "created_at": _now(),
                "name": str(values.get("name") or route)[:120],
                "owner_scope": owner,
                "owner_tenant_id": tenant_id,
                "gateway_id": "primary",
                "internal_route": route,
                "base_url": base,
                "protocol": "responses",
                "capabilities": values.get("capabilities")
                or {"text": True, "tools": False},
                "status": "active",
                "config_version": 1,
            }
            connection.execute(g.deployments.insert().values(**result))
            return result

    def update_deployment(self, deployment_id, values: dict, *, actor_id: str):
        """Change a registered route and its grants in one authorized transaction."""
        if not values or set(values) - {
            "name",
            "internal_route",
            "base_url",
            "capabilities",
            "status",
        }:
            raise PlatformError(422, "invalid_deployment", "部署字段无效。")
        changes = dict(values)
        if "name" in changes and (
            not isinstance(changes["name"], str)
            or not changes["name"].strip()
            or len(changes["name"]) > 120
        ):
            raise PlatformError(422, "invalid_deployment", "部署名称无效。")
        if "capabilities" in changes and not isinstance(changes["capabilities"], dict):
            raise PlatformError(422, "invalid_deployment", "部署能力字段无效。")
        if "internal_route" in changes:
            route = str(changes["internal_route"]).strip()
            if not route or route == "*" or len(route) > 200:
                raise PlatformError(
                    422, "invalid_deployment", "必须指定单个网关内部模型路由。"
                )
            changes["internal_route"] = route
        if "base_url" in changes:
            changes["base_url"] = _url(changes["base_url"])
            if changes["base_url"] != _url(self.settings.litellm_url):
                raise PlatformError(
                    422, "unsupported_gateway", "当前控制面仅支持配置的 primary 网关。"
                )
        if "status" in changes and (
            not isinstance(changes["status"], str)
            or changes["status"] not in {"active", "disabled"}
        ):
            raise PlatformError(422, "invalid_deployment", "部署状态无效。")
        with self._transaction(scope="gateway:control") as connection:
            self._authorize(connection, actor_id, "platform.models.manage")
            row = (
                connection.execute(
                    select(g.deployments)
                    .where(g.deployments.c.id == deployment_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError(404, "deployment_not_found", "部署不存在。")
            if all(row[key] == value for key, value in changes.items()):
                return dict(row)
            changes["config_version"] = row["config_version"] + 1
            connection.execute(
                g.deployments.update()
                .where(g.deployments.c.id == deployment_id)
                .values(**changes)
            )
            # Grants are tenant content protected by RLS. Select each tenant
            # explicitly and take the same lock as grant/enable/entitlement writes.
            tenant_ids = connection.scalars(
                select(t.tenants.c.id).order_by(t.tenants.c.id)
            ).all()
            for tenant_id in tenant_ids:
                if not self.db.sqlite:
                    lock_id = int.from_bytes(
                        hashlib.sha256(("tenant:" + tenant_id).encode()).digest()[:8],
                        "big",
                        signed=True,
                    )
                    connection.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id}
                    )
                self.db.set_tenant(connection, tenant_id)
                granted = connection.scalar(
                    select(g.model_bindings.c.model_id)
                    .where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.deployment_id == deployment_id,
                    )
                    .limit(1)
                )
                if granted:
                    self.converge_configuration(connection, tenant_id)
            self._audit(
                connection,
                None,
                actor_id,
                "deployment.update",
                deployment_id,
                config_version=changes["config_version"],
                fields=sorted(values),
            )
            return {**dict(row), **changes}

    def _add_price(self, connection, tenant_id, model, actor_id):
        existing = (
            connection.execute(
                select(g.prices).where(
                    g.prices.c.tenant_id == tenant_id,
                    g.prices.c.model_id == model["id"],
                    g.prices.c.version == model["price_version"],
                )
            )
            .mappings()
            .first()
        )
        input_price, output_price = (
            _price(model["input_price"]),
            _price(model["output_price"]),
        )
        if existing:
            if (
                existing["input_price"] != input_price
                or existing["output_price"] != output_price
            ):
                raise PlatformError(
                    409,
                    "price_version_conflict",
                    "同一费率版本不能覆盖，请创建新版本。",
                )
            return existing["id"]
        identifier = _id()
        connection.execute(
            g.prices.insert().values(
                id=identifier,
                tenant_id=tenant_id,
                model_id=model["id"],
                version=model["price_version"],
                currency="USD",
                unit="million_tokens",
                input_price=input_price,
                output_price=output_price,
                created_by=actor_id,
                created_at=_now(),
            )
        )
        return identifier

    def grant_model(self, tenant_id, deployment_id, values: dict, *, actor_id: str):
        """Platform operator only: API capability checks precede this command."""
        if not actor_id or set(values) - {
            "model_id",
            "name",
            "alias",
            "input_price",
            "output_price",
            "context_window",
            "max_output_tokens",
            "active",
        }:
            raise PlatformError(422, "invalid_model", "模型字段无效。")
        with self._transaction(tenant_id) as connection:
            self._authorize(connection, actor_id, "platform.pricing.manage")
            self._authorize(connection, actor_id, "platform.models.manage")
            self._tenant(connection, tenant_id)
            deployment = (
                connection.execute(
                    select(g.deployments).where(g.deployments.c.id == deployment_id)
                )
                .mappings()
                .first()
            )
            if (
                not deployment
                or deployment["status"] != "active"
                or (
                    deployment["owner_scope"] == "tenant"
                    and deployment["owner_tenant_id"] != tenant_id
                )
            ):
                raise PlatformError(404, "deployment_not_found", "可授权部署不存在。")
            previous = None
            if values.get("model_id"):
                previous = (
                    connection.execute(
                        select(t.models).where(
                            t.models.c.tenant_id == tenant_id,
                            t.models.c.id == values["model_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if not previous:
                    raise PlatformError(404, "model_not_found", "模型不存在。")
            model = (
                dict(previous)
                if previous
                else {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "created_at": _now(),
                    "price_version": 0,
                }
            )
            model.update(
                {key: value for key, value in values.items() if key != "model_id"}
            )
            model.setdefault("name", deployment["name"])
            model.setdefault("alias", deployment["internal_route"])
            model.setdefault("active", True)
            model.setdefault("context_window", self.settings.default_context_window)
            model.setdefault("max_output_tokens", self.settings.default_output_tokens)
            for field in ("input_price", "output_price"):
                model[field] = format(_price(model.get(field, "0")), "f")
            if not 1 <= model["max_output_tokens"] <= model["context_window"]:
                raise PlatformError(422, "invalid_model_limits", "模型输出上限无效。")
            model["price_version"] += 1
            if previous:
                connection.execute(
                    t.models.update()
                    .where(
                        t.models.c.id == model["id"], t.models.c.tenant_id == tenant_id
                    )
                    .values(**model)
                )
            else:
                connection.execute(t.models.insert().values(**model))
            price_id = self._add_price(connection, tenant_id, model, actor_id)
            grant = (
                connection.execute(
                    select(g.model_bindings).where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.model_id == model["id"],
                    )
                )
                .mappings()
                .first()
            )
            data = {
                "tenant_id": tenant_id,
                "model_id": model["id"],
                "deployment_id": deployment_id,
                "price_version_id": price_id,
                "enabled": model["active"],
                "policy_version": grant["policy_version"] + 1 if grant else 1,
                "created_at": grant["created_at"] if grant else _now(),
            }
            if grant:
                connection.execute(
                    g.model_bindings.update()
                    .where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.model_id == model["id"],
                    )
                    .values(**data)
                )
            else:
                connection.execute(g.model_bindings.insert().values(**data))
            self._audit(
                connection,
                tenant_id,
                actor_id,
                "model.grant",
                model["id"],
                deployment_id=deployment_id,
                price_version_id=price_id,
            )
            self.converge_configuration(connection, tenant_id)
            return {
                **model,
                "deployment_id": deployment_id,
                "price_version_id": price_id,
            }

    def set_model_enabled(self, tenant_id, model_id, enabled: bool, *, actor: dict):
        with self._transaction(tenant_id) as connection:
            from agent_platform.modules.identity import IdentityService

            IdentityService(self.db, self.settings).assert_current(
                connection, actor, "models.policy"
            )
            row = (
                connection.execute(
                    select(g.model_bindings).where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.model_id == model_id,
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise PlatformError(404, "model_not_granted", "模型未授权给当前组织。")
            connection.execute(
                g.model_bindings.update()
                .where(
                    g.model_bindings.c.tenant_id == tenant_id,
                    g.model_bindings.c.model_id == model_id,
                )
                .values(enabled=bool(enabled), policy_version=row["policy_version"] + 1)
            )
            connection.execute(
                t.models.update()
                .where(t.models.c.tenant_id == tenant_id, t.models.c.id == model_id)
                .values(active=bool(enabled))
            )
            self._audit(
                connection,
                tenant_id,
                actor["id"],
                "model.enable" if enabled else "model.disable",
                model_id,
                policy_version=row["policy_version"] + 1,
            )
            self.converge_configuration(connection, tenant_id)
            return {"model_id": model_id, "enabled": bool(enabled)}

    def bootstrap_legacy_catalog(self):
        """Explicit one-tenant legacy enrollment only; never adopt newly added tenants."""
        with self._transaction(scope="gateway:legacy-bootstrap") as connection:
            tenants = list(connection.execute(select(t.tenants.c.id)).scalars())
            # Initial adoption is allowed once, only for a single existing tenant.
            if len(tenants) != 1:
                return
            tenant_id = tenants[0]
            if not self.db.sqlite:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": tenant_id},
                )
            existing_binding = connection.execute(
                select(g.bindings.c.id)
                .where(g.bindings.c.tenant_id == tenant_id)
                .limit(1)
            ).first()
            if existing_binding:
                return
            models = list(
                connection.execute(
                    select(t.models).where(t.models.c.tenant_id == tenant_id)
                ).mappings()
            )
            if not models:
                return
            for row in models:
                deployment = (
                    connection.execute(
                        select(g.deployments).where(
                            g.deployments.c.gateway_id == "primary",
                            g.deployments.c.internal_route == row["alias"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if not deployment:
                    deployment = {
                        "id": _id(),
                        "created_at": _now(),
                        "name": row["name"],
                        "owner_scope": "platform",
                        "owner_tenant_id": None,
                        "gateway_id": "primary",
                        "internal_route": row["alias"],
                        "base_url": self.settings.litellm_url.rstrip("/"),
                        "protocol": "responses",
                        "capabilities": {"text": True, "tools": True},
                        "status": "active",
                        "config_version": 1,
                    }
                    connection.execute(g.deployments.insert().values(**deployment))
                price_id = self._add_price(
                    connection, tenant_id, dict(row), "legacy-migration"
                )
                connection.execute(
                    g.model_bindings.insert().values(
                        tenant_id=tenant_id,
                        model_id=row["id"],
                        deployment_id=deployment["id"],
                        price_version_id=price_id,
                        enabled=row["active"],
                        policy_version=1,
                        created_at=_now(),
                    )
                )
            binding_id = _id()
            connection.execute(
                g.bindings.insert().values(
                    id=binding_id,
                    tenant_id=tenant_id,
                    gateway_id="primary",
                    purpose="inference",
                    mode="legacy",
                    active_credential_version_id=None,
                    desired_version=0,
                    applied_version=0,
                    status="legacy",
                    limits=DEFAULT_LIMITS,
                    error_code="",
                    created_at=_now(),
                )
            )
            self._audit(
                connection,
                tenant_id,
                "legacy-migration",
                "gateway.legacy_adopted",
                binding_id,
            )

    def _desired_configuration(self, connection, tenant_id, binding):
        routes = sorted(
            set(
                connection.scalars(
                    select(g.deployments.c.internal_route)
                    .select_from(
                        g.model_bindings.join(
                            g.deployments,
                            g.model_bindings.c.deployment_id == g.deployments.c.id,
                        ).join(
                            t.models,
                            and_(
                                t.models.c.id == g.model_bindings.c.model_id,
                                t.models.c.tenant_id == g.model_bindings.c.tenant_id,
                            ),
                        )
                    )
                    .where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.enabled.is_(True),
                        t.models.c.active.is_(True),
                        g.deployments.c.status == "active",
                        or_(
                            g.deployments.c.owner_scope == "platform",
                            g.deployments.c.owner_tenant_id == tenant_id,
                        ),
                    )
                )
            )
        )
        limits = _limits(binding["limits"])
        entitlement = (
            connection.execute(
                select(nt.entitlements).where(nt.entitlements.c.tenant_id == tenant_id)
            )
            .mappings()
            .first()
        )
        if entitlement is None:
            return [], limits
        for gateway_field, plan_field in (
            ("rpm_limit", "rpm"),
            ("tpm_limit", "tpm"),
            ("max_parallel_requests", "max_concurrent_runs"),
        ):
            ceiling = entitlement[plan_field]
            if ceiling <= 0:
                return [], limits
            limits[gateway_field] = min(limits[gateway_field], ceiling)
        if entitlement["max_budget"] is not None:
            ceiling = _price(entitlement["max_budget"])
            if ceiling <= 0:
                return [], limits
            limits["max_budget"] = min(limits["max_budget"], float(ceiling))
        return routes, limits

    @staticmethod
    def _credential_within_limits(credential, limits):
        return all(
            credential["limits"].get(field, float("inf")) <= ceiling
            for field, ceiling in limits.items()
        )

    def _discard_unsent(self, connection, binding):
        """Fence obsolete intents that have never dispatched a key creation."""
        jobs = (
            connection.execute(
                select(g.outbox).where(
                    g.outbox.c.binding_id == binding["id"],
                    g.outbox.c.action == "provision",
                    g.outbox.c.status.in_(LIVE_JOBS),
                    g.outbox.c.phase.in_(
                        ("user_pending", "user_sent", "user_rejected", "key_pending")
                    ),
                )
            )
            .mappings()
            .all()
        )
        for job in jobs:
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == job["id"])
                .values(
                    status="cancelled",
                    owner=None,
                    lease_until=None,
                    error_code="configuration_superseded_before_key_dispatch",
                    updated_at=_now(),
                )
            )
            connection.execute(
                g.credentials.update()
                .where(g.credentials.c.id == job["credential_version_id"])
                .values(status="revoked", secret_ciphertext=None, revoked_at=_now())
            )

    def converge_configuration(self, connection, tenant_id):
        """Continue a prior authorized enrollment after an authorized policy write.

        Call only inside the caller's tenant transaction, after its current actor
        and mutation have been authorized. This hook neither enrolls a tenant nor
        impersonates a platform operator; legacy and explicitly revoked bindings
        require a new explicit enrollment. Its outbox is part of the same commit.
        """
        if not connection.in_transaction():
            raise RuntimeError("Gateway convergence requires the policy transaction")
        binding = self._binding(connection, tenant_id)
        if (
            not binding
            or binding["mode"] != "enrolled"
            or binding["status"] in {"revoking", "revoked"}
        ):
            return self._safe_binding(binding)
        return self._converge_configuration(connection, tenant_id, binding)

    def _converge_configuration(self, connection, tenant_id, binding, *, force=False):
        routes, limits = self._desired_configuration(connection, tenant_id, binding)
        desired = (
            connection.execute(
                select(g.credentials).where(
                    g.credentials.c.binding_id == binding["id"],
                    g.credentials.c.generation == binding["desired_version"],
                )
            )
            .mappings()
            .first()
        )
        if not force and (
            (
                desired
                and desired["status"] not in {"revoked", "retiring", "failed"}
                and desired["routes"] == routes
                and desired["limits"] == limits
                and (
                    desired["status"] != "active"
                    or (desired["expires_at"] and desired["expires_at"] > _now())
                )
            )
            or (not routes and binding["status"] == "disabled")
        ):
            return self._safe_binding(binding)
        generation = binding["desired_version"] + 1
        self._discard_unsent(connection, binding)
        changes = {
            "mode": "enrolled",
            "desired_version": generation,
            "error_code": "",
            "status": (
                "ready" if binding["active_credential_version_id"] else "provisioning"
            )
            if routes
            else "disabled",
        }
        operation_id = None
        if routes:
            key, identifier = "sk-" + secrets.token_urlsafe(36), _id()
            credential = {
                "id": identifier,
                "tenant_id": tenant_id,
                "binding_id": binding["id"],
                "generation": generation,
                "external_user_id": "agent-platform-" + identifier,
                "external_key_id": _hash(key),
                "key_alias": "agent-platform-" + identifier,
                "secret_ciphertext": self._cipher().encrypt(key.encode()).decode(),
                "fingerprint": _hash(key),
                "routes": routes,
                "limits": limits,
                "status": "pending",
                "created_at": _now(),
                "expires_at": None,
                "activated_at": None,
                "retired_at": None,
                "revoked_at": None,
            }
            connection.execute(g.credentials.insert().values(**credential))
            operation_id = self._job(connection, binding, credential, "provision")
        else:
            changes["active_credential_version_id"] = None
            credentials = (
                connection.execute(
                    select(g.credentials).where(
                        g.credentials.c.binding_id == binding["id"],
                        g.credentials.c.status.in_(("active", "retiring")),
                    )
                )
                .mappings()
                .all()
            )
            for credential in credentials:
                if not connection.scalar(
                    select(g.outbox.c.id).where(
                        g.outbox.c.credential_version_id == credential["id"],
                        g.outbox.c.action == "revoke",
                    )
                ):
                    self._job(connection, binding, dict(credential), "revoke")
                connection.execute(
                    g.credentials.update()
                    .where(g.credentials.c.id == credential["id"])
                    .values(status="retiring", retired_at=_now())
                )
        connection.execute(
            g.bindings.update()
            .where(g.bindings.c.id == binding["id"])
            .values(**changes)
        )
        self._audit(
            connection,
            tenant_id,
            "gateway-policy-convergence",
            "gateway.configuration_requested",
            binding["id"],
            generation=generation,
            operation_id=operation_id,
        )
        result = self._safe_binding({**binding, **changes})
        if operation_id:
            result["operation_id"] = operation_id
        return result

    def availability(self, tenant_id, model_row, *, connection=None):
        """Expose route-specific readiness without returning transient secrets."""
        try:
            self.resolve(tenant_id, model_row, connection=connection)
        except PlatformError as error:
            return {"ready": False, "status": "unavailable", "reason": error.code}
        return {"ready": True, "status": "ready", "reason": ""}

    def resolve(self, tenant_id, model_row, *, connection=None):
        """Return transient inference-only credentials. Never serialize this result."""
        if model_row.get("tenant_id") != tenant_id:
            raise PlatformError(404, "model_not_found", "模型不存在。")
        manager = (
            nullcontext(connection)
            if connection is not None
            else self._transaction(tenant_id)
        )
        with manager as connection:  # noqa: PLR1704
            tenant = self._tenant(connection, tenant_id)
            if tenant.get("status", "active") != "active":
                raise PlatformError(403, "tenant_inactive", "当前组织不可发起调用。")
            model = (
                connection.execute(
                    select(t.models).where(
                        t.models.c.tenant_id == tenant_id,
                        t.models.c.id == model_row["id"],
                    )
                )
                .mappings()
                .first()
            )
            grant = (
                connection.execute(
                    select(g.model_bindings).where(
                        g.model_bindings.c.tenant_id == tenant_id,
                        g.model_bindings.c.model_id == model_row["id"],
                    )
                )
                .mappings()
                .first()
            )
            binding = self._binding(connection, tenant_id)
            if not model or not model["active"] or not grant or not grant["enabled"]:
                raise PlatformError(403, "model_not_granted", "模型未启用或未授权。")
            deployment = (
                connection.execute(
                    select(g.deployments).where(
                        g.deployments.c.id == grant["deployment_id"]
                    )
                )
                .mappings()
                .first()
            )
            if (
                not deployment
                or deployment["status"] != "active"
                or (
                    deployment["owner_scope"] == "tenant"
                    and deployment["owner_tenant_id"] != tenant_id
                )
            ):
                raise PlatformError(403, "deployment_disabled", "模型部署不可用。")
            expected_deployment = model_row.get("deployment_id")
            if expected_deployment and expected_deployment != deployment["id"]:
                raise PlatformError(
                    403, "deployment_changed", "模型部署已变更，请重新发起运行。"
                )
            result = {
                "deployment_id": deployment["id"],
                "route": deployment["internal_route"],
                "base_url": _url(deployment["base_url"]),
                "credential_version_id": None,
                "price_version_id": grant["price_version_id"],
                "config_version": deployment["config_version"],
            }
            if binding and binding["mode"] == "legacy":
                count = connection.scalar(select(func.count()).select_from(t.tenants))
                if getattr(self.settings, "mode", "local") != "local" or count != 1:
                    raise PlatformError(
                        503,
                        "gateway_enrollment_required",
                        "多租户模式必须配置组织专属网关凭据。",
                    )
                if binding["status"] != "legacy" or not self.settings.litellm_key:
                    raise PlatformError(
                        503, "gateway_not_configured", "网关凭据未配置。"
                    )
                return {**result, "api_key": self.settings.litellm_key}
            if (
                not binding
                or binding["mode"] != "enrolled"
                or binding["status"] in {"revoking", "revoked", "disabled"}
            ):
                raise PlatformError(503, "gateway_not_ready", "组织网关授权尚未就绪。")
            desired_routes, desired_limits = self._desired_configuration(
                connection, tenant_id, binding
            )
            credential = (
                connection.execute(
                    select(g.credentials).where(
                        g.credentials.c.tenant_id == tenant_id,
                        g.credentials.c.binding_id == binding["id"],
                        g.credentials.c.id == binding["active_credential_version_id"],
                    )
                )
                .mappings()
                .first()
            )
            if (
                not credential
                or credential["status"] != "active"
                or deployment["internal_route"] not in desired_routes
                or deployment["internal_route"] not in credential["routes"]
                or not self._credential_within_limits(credential, desired_limits)
            ):
                raise PlatformError(
                    503, "gateway_authorization_pending", "当前模型的网关授权尚未完成。"
                )
            try:
                expires = datetime.fromisoformat(credential["expires_at"])
                if expires.tzinfo is None or expires <= datetime.now(UTC):
                    raise ValueError
            except (TypeError, ValueError):
                raise PlatformError(
                    503, "gateway_credential_expired", "组织网关凭据已过期，请轮换。"
                ) from None
            return {
                **result,
                "credential_version_id": credential["id"],
                "api_key": self._decrypt(credential["secret_ciphertext"]),
            }

    def _job(self, connection, binding, credential, action, *, delay=0):
        now = _now()
        identifier = _id()
        connection.execute(
            g.outbox.insert().values(
                id=identifier,
                tenant_id=binding["tenant_id"],
                binding_id=binding["id"],
                credential_version_id=credential["id"],
                config_version=credential["generation"],
                action=action,
                phase="user_pending" if action == "provision" else "revoke_pending",
                status="pending",
                attempt_count=0,
                next_attempt_at=time.time() + delay,
                owner=None,
                fence=0,
                lease_until=None,
                external_resource_id=credential["external_key_id"],
                error_code="",
                created_at=now,
                updated_at=now,
            )
        )
        return identifier

    def enroll(
        self,
        tenant_id,
        *,
        actor_id: str,
        limits: dict | None = None,
        _force_rotation: bool = False,
    ):
        if not actor_id:
            raise PlatformError(422, "actor_required", "需要操作主体。")
        with self._transaction(tenant_id) as connection:
            self._authorize(connection, actor_id, "platform.gateway.manage")
            self._tenant(connection, tenant_id)
            binding = self._binding(connection, tenant_id)
            selected_limits = _limits(
                limits
                if limits is not None
                else (binding["limits"] if binding else None)
            )
            if not binding:
                binding = {
                    "id": _id(),
                    "tenant_id": tenant_id,
                    "gateway_id": "primary",
                    "purpose": "inference",
                    "mode": "enrolled",
                    "active_credential_version_id": None,
                    "desired_version": 0,
                    "applied_version": 0,
                    "status": "provisioning",
                    "limits": selected_limits,
                    "error_code": "",
                    "created_at": _now(),
                }
                connection.execute(g.bindings.insert().values(**binding))
            else:
                # Explicit re-enrollment is the only path allowed to reopen a
                # revoked/legacy binding. Policy convergence never does this.
                binding = {**binding, "mode": "enrolled", "limits": selected_limits}
                connection.execute(
                    g.bindings.update()
                    .where(g.bindings.c.id == binding["id"])
                    .values(mode="enrolled", limits=selected_limits)
                )
            if not self._desired_configuration(connection, tenant_id, binding)[0]:
                raise PlatformError(
                    400, "no_available_model", "请先授权至少一个模型并配置可用套餐。"
                )
            result = self._converge_configuration(
                connection,
                tenant_id,
                binding,
                force=_force_rotation
                or binding["status"] in {"revoking", "revoked", "legacy"},
            )
            self._audit(
                connection,
                tenant_id,
                actor_id,
                "gateway.enroll_requested",
                binding["id"],
                generation=result["desired_version"],
                operation_id=result.get("operation_id"),
            )
            return result

    def rotate(self, tenant_id, *, actor_id: str):
        return self.enroll(tenant_id, actor_id=actor_id, _force_rotation=True)

    def revoke(self, tenant_id, *, actor_id: str):
        return self._revoke(tenant_id, actor_id=actor_id)

    def revoke_for_closure(self, tenant_id):
        return self._revoke(
            tenant_id, actor_id="system:tenant-maintenance", closure=True
        )

    def _revoke(self, tenant_id, *, actor_id, closure=False):
        with self._transaction(tenant_id) as connection:
            if closure:
                from agent_platform.infrastructure.operations_tables import (
                    closures,
                )

                state = connection.scalar(
                    select(nt.tenant_settings.c.status).where(
                        nt.tenant_settings.c.tenant_id == tenant_id
                    )
                )
                job = connection.scalar(
                    select(closures.c.tenant_id).where(
                        closures.c.tenant_id == tenant_id
                    )
                )
                if state not in {"closing", "deleted"} or not job:
                    raise PlatformError(
                        409, "closing_required", "缺少已授权的组织关闭任务。"
                    )
            else:
                self._authorize(connection, actor_id, "platform.gateway.manage")
            self._tenant(connection, tenant_id)
            binding = self._binding(connection, tenant_id)
            if not binding:
                return {"status": "revoked", "mode": "none"}
            remaining = connection.scalar(
                select(func.count())
                .select_from(g.credentials)
                .where(
                    g.credentials.c.binding_id == binding["id"],
                    g.credentials.c.status != "revoked",
                )
            )
            if binding["status"] == "revoked" and not remaining:
                return self._safe_binding(binding)
            connection.execute(
                g.bindings.update()
                .where(g.bindings.c.id == binding["id"])
                .values(status="revoking", error_code="")
            )
            # Pending writes may still arrive remotely. Never discard their jobs:
            # they reconcile first, then activation detects revoking and deletes.
            rows = list(
                connection.execute(
                    select(g.credentials).where(
                        g.credentials.c.binding_id == binding["id"],
                        g.credentials.c.status != "revoked",
                    )
                ).mappings()
            )
            for row in rows:
                provision = (
                    connection.execute(
                        select(g.outbox).where(
                            g.outbox.c.credential_version_id == row["id"],
                            g.outbox.c.action == "provision",
                        )
                    )
                    .mappings()
                    .first()
                )
                if row["status"] in ("pending", "failed"):
                    if provision and provision["phase"] in (
                        "user_pending",
                        "user_sent",
                        "user_rejected",
                        "key_pending",
                    ):
                        # The durable key-send marker has never been committed.
                        # Cancel under the same fence lock before a worker can set
                        # that marker. A late empty user creation cannot mint a key.
                        connection.execute(
                            g.outbox.update()
                            .where(g.outbox.c.id == provision["id"])
                            .values(
                                status="cancelled",
                                owner=None,
                                lease_until=None,
                                error_code="revoked_before_key_dispatch",
                                updated_at=_now(),
                            )
                        )
                        connection.execute(
                            g.credentials.update()
                            .where(g.credentials.c.id == row["id"])
                            .values(
                                status="revoked",
                                secret_ciphertext=None,
                                revoked_at=_now(),
                            )
                        )
                        self._audit(
                            connection,
                            tenant_id,
                            actor_id,
                            "gateway.unissued_credential_discarded",
                            row["id"],
                        )
                        continue
                    if not provision or provision["phase"] == "key_sent":
                        # A historical failed row can have an uncertain write. A
                        # missing key in one read cannot prove a late create will
                        # not succeed. Keep it tracked and reconcile, never free it.
                        if provision and provision["status"] not in LIVE_JOBS:
                            connection.execute(
                                g.outbox.update()
                                .where(g.outbox.c.id == provision["id"])
                                .values(
                                    status="reconciling",
                                    owner=None,
                                    lease_until=None,
                                    next_attempt_at=0,
                                    error_code="gateway_creation_requires_reconcile",
                                    updated_at=_now(),
                                )
                            )
                        continue
                present = (
                    connection.execute(
                        select(g.outbox).where(
                            g.outbox.c.credential_version_id == row["id"],
                            g.outbox.c.action == "revoke",
                        )
                    )
                    .mappings()
                    .first()
                )
                if not present:
                    self._job(connection, binding, dict(row), "revoke")
                elif present["status"] == "failed":
                    # Read back this immutable key before retrying its deletion.
                    connection.execute(
                        g.outbox.update()
                        .where(g.outbox.c.id == present["id"])
                        .values(
                            status="reconciling",
                            owner=None,
                            lease_until=None,
                            next_attempt_at=0,
                            updated_at=_now(),
                        )
                    )
                connection.execute(
                    g.credentials.update()
                    .where(g.credentials.c.id == row["id"])
                    .values(status="retiring")
                )
            remaining = connection.scalar(
                select(func.count())
                .select_from(g.credentials)
                .where(
                    g.credentials.c.binding_id == binding["id"],
                    g.credentials.c.status != "revoked",
                )
            )
            final_state = (
                "revoked"
                if binding["mode"] == "legacy" or not remaining
                else "revoking"
            )
            if final_state == "revoked":
                # The env key is deployment-owned: deny locally, never delete a
                # possibly shared deployment secret through a tenant operation.
                connection.execute(
                    g.bindings.update()
                    .where(g.bindings.c.id == binding["id"])
                    .values(status="revoked", active_credential_version_id=None)
                )
            self._audit(
                connection,
                tenant_id,
                actor_id,
                "gateway.revoke_requested",
                binding["id"],
            )
            return self._safe_binding(
                {
                    **binding,
                    "status": final_state,
                }
            )

    def reconcile(self, tenant_id, operation_id, *, actor_id: str):
        """Expedite read-back only. Never resets a possibly sent operation."""
        with self._transaction(tenant_id) as connection:
            self._authorize(connection, actor_id, "platform.gateway.manage")
            job = (
                connection.execute(
                    select(g.outbox).where(
                        g.outbox.c.tenant_id == tenant_id,
                        g.outbox.c.id == operation_id,
                    )
                )
                .mappings()
                .first()
            )
            if not job or job["status"] not in ("reconciling", "retry_wait"):
                raise PlatformError(
                    409, "operation_not_reconcilable", "该操作不处于待核查状态。"
                )
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == operation_id)
                .values(next_attempt_at=0)
            )
            self._audit(
                connection,
                tenant_id,
                actor_id,
                "gateway.reconcile_requested",
                operation_id,
            )
            return {"operation_id": operation_id, "status": job["status"]}

    def _claim(self):
        # Tenant directory is global; all secret/outbox access uses tenant RLS.
        with self.db.read() as connection:
            tenants = list(
                connection.execute(
                    select(t.tenants.c.id).order_by(t.tenants.c.id)
                ).scalars()
            )
        if not tenants:
            return None
        start = self._tenant_cursor % len(tenants)
        for index in range(len(tenants)):
            offset = (start + index) % len(tenants)
            job = self._claim_tenant(tenants[offset])
            if job:
                self._tenant_cursor = offset + 1
                return job
        self._tenant_cursor = start + 1
        return None

    def _claim_tenant(self, tenant_id):
        with self._transaction(tenant_id) as connection:
            clock = time.time()
            expired = list(
                connection.execute(
                    select(g.outbox).where(
                        g.outbox.c.tenant_id == tenant_id,
                        g.outbox.c.status == "applying",
                        g.outbox.c.lease_until < clock,
                    )
                ).mappings()
            )
            for row in expired:
                connection.execute(
                    g.outbox.update()
                    .where(g.outbox.c.id == row["id"])
                    .values(
                        status="reconciling",
                        owner=None,
                        lease_until=None,
                        next_attempt_at=clock,
                        error_code="provisioner_lease_expired",
                        updated_at=_now(),
                    )
                )
            candidates = list(
                connection.execute(
                    select(g.outbox)
                    .where(
                        g.outbox.c.tenant_id == tenant_id,
                        g.outbox.c.status.in_(("pending", "retry_wait", "reconciling")),
                        g.outbox.c.next_attempt_at <= clock,
                    )
                    .order_by(g.outbox.c.created_at, g.outbox.c.id)
                    .limit(100)
                ).mappings()
            )
            for row in candidates:
                blocked = connection.scalar(
                    select(g.outbox.c.id)
                    .where(
                        g.outbox.c.binding_id == row["binding_id"],
                        g.outbox.c.id != row["id"],
                        or_(
                            g.outbox.c.status == "applying",
                            and_(
                                g.outbox.c.status == "reconciling",
                                g.outbox.c.created_at < row["created_at"],
                                # Unknown creation blocks further provisioning,
                                # but must not leave earlier known keys live while
                                # closing. Exact-hash revocation is independent.
                                row["action"] != "revoke",
                            ),
                        ),
                    )
                    .limit(1)
                )
                if blocked:
                    continue
                claimed = dict(row)
                claimed.update(
                    status="applying",
                    owner=self.owner,
                    fence=row["fence"] + 1,
                    lease_until=clock + 120,
                    attempt_count=row["attempt_count"] + 1,
                    updated_at=_now(),
                )
                connection.execute(
                    g.outbox.update()
                    .where(g.outbox.c.id == row["id"])
                    .values(**claimed)
                )
                return claimed
            return None

    def _guard(self, connection, job):
        current = (
            connection.execute(
                select(g.outbox).where(
                    g.outbox.c.id == job["id"], g.outbox.c.tenant_id == job["tenant_id"]
                )
            )
            .mappings()
            .one()
        )
        if (
            current["owner"] != self.owner
            or current["fence"] != job["fence"]
            or current["status"] != "applying"
            or current["lease_until"] < time.time()
        ):
            raise ControlFailure("gateway_sync_lease_lost", unknown=True)
        return dict(current)

    def _phase(self, job, phase):
        with self._transaction(job["tenant_id"]) as connection:
            self._guard(connection, job)
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == job["id"])
                .values(
                    phase=phase,
                    lease_until=time.time() + 120,
                    updated_at=_now(),
                )
            )
            job["phase"] = phase

    @staticmethod
    def _validate_user(info, credential):
        if (
            not isinstance(info, dict)
            or info.get("user_id") != credential["external_user_id"]
            or info.get("user_role") != "internal_user"
            or set(info.get("models") or []) != set(credential["routes"])
            or info.get("teams")
            or info.get("aliases")
            or info.get("permissions")
        ):
            raise ControlFailure("gateway_user_permissions_mismatch")

    @staticmethod
    def _validate_key(info, credential):
        if (
            not isinstance(info, dict)
            or info.get("user_id") != credential["external_user_id"]
            or set(info.get("models") or []) != set(credential["routes"])
            or set(info.get("allowed_routes") or []) != set(ALLOWED_ROUTES)
            or info.get("blocked")
            or info.get("team_id")
            or any(info.get(field) for field in ("aliases", "config", "permissions"))
        ):
            raise ControlFailure("gateway_key_permissions_mismatch")
        try:
            for field, limit in credential["limits"].items():
                actual = float(info[field])
                if not math.isfinite(actual) or not 0 < actual <= limit:
                    raise ValueError
            expires = datetime.fromisoformat(info["expires"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                raise ValueError
            if float(info.get("spend") or 0) >= float(info["max_budget"]):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ControlFailure("gateway_key_limits_mismatch") from None
        return expires.isoformat()

    def _activate(self, job, credential, expires_at):
        with self._transaction(job["tenant_id"]) as connection:
            self._guard(connection, job)
            binding = self._binding(connection, job["tenant_id"])
            routes, limits = self._desired_configuration(
                connection, job["tenant_id"], binding
            )
            if (
                binding["status"] in ("revoking", "revoked", "disabled")
                or binding["desired_version"] != credential["generation"]
                or credential["routes"] != routes
                or credential["limits"] != limits
            ):
                connection.execute(
                    g.credentials.update()
                    .where(g.credentials.c.id == credential["id"])
                    .values(status="retiring", expires_at=expires_at)
                )
                self._job(connection, binding, credential, "revoke")
            else:
                previous_id = binding["active_credential_version_id"]
                connection.execute(
                    g.credentials.update()
                    .where(g.credentials.c.id == credential["id"])
                    .values(
                        status="active",
                        expires_at=expires_at,
                        activated_at=_now(),
                    )
                )
                connection.execute(
                    g.bindings.update()
                    .where(g.bindings.c.id == binding["id"])
                    .values(
                        active_credential_version_id=credential["id"],
                        applied_version=credential["generation"],
                        status="ready",
                        error_code="",
                    )
                )
                if previous_id and previous_id != credential["id"]:
                    previous = dict(
                        connection.execute(
                            select(g.credentials).where(
                                g.credentials.c.id == previous_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if previous["status"] != "revoked":
                        connection.execute(
                            g.credentials.update()
                            .where(g.credentials.c.id == previous_id)
                            .values(status="retiring", retired_at=_now())
                        )
                    present = connection.scalar(
                        select(g.outbox.c.id).where(
                            g.outbox.c.credential_version_id == previous_id,
                            g.outbox.c.action == "revoke",
                        )
                    )
                    if not present and previous["status"] != "revoked":
                        self._job(
                            connection,
                            binding,
                            previous,
                            "revoke",
                            delay=getattr(self.settings, "model_timeout", 90) + 30,
                        )
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == job["id"])
                .values(
                    status="applied",
                    phase="verified",
                    owner=None,
                    lease_until=None,
                    error_code="",
                    updated_at=_now(),
                )
            )
            self._audit(
                connection,
                job["tenant_id"],
                "gateway-provisioner",
                "gateway.credential_verified",
                credential["id"],
                operation_id=job["id"],
            )
            # A newer policy may have committed during remote HTTP. It must not
            # be overwritten by this completed snapshot, even if a caller missed
            # the hook; ensure the latest persisted policy has a durable intent.
            self.converge_configuration(connection, job["tenant_id"])

    def _revoked(self, job, credential):
        with self._transaction(job["tenant_id"]) as connection:
            self._guard(connection, job)
            connection.execute(
                g.credentials.update()
                .where(g.credentials.c.id == credential["id"])
                .values(
                    status="revoked",
                    revoked_at=_now(),
                    secret_ciphertext=None,
                )
            )
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == job["id"])
                .values(
                    status="applied",
                    phase="verified",
                    owner=None,
                    lease_until=None,
                    error_code="",
                    updated_at=_now(),
                )
            )
            remaining = connection.scalar(
                select(func.count())
                .select_from(g.credentials)
                .where(
                    g.credentials.c.binding_id == job["binding_id"],
                    g.credentials.c.status != "revoked",
                )
            )
            binding = self._binding(connection, job["tenant_id"])
            if not remaining and binding["status"] == "revoking":
                connection.execute(
                    g.bindings.update()
                    .where(g.bindings.c.id == job["binding_id"])
                    .values(
                        status="revoked",
                        active_credential_version_id=None,
                    )
                )
            elif not remaining and binding["status"] == "disabled":
                connection.execute(
                    g.bindings.update()
                    .where(g.bindings.c.id == job["binding_id"])
                    .values(applied_version=binding["desired_version"])
                )
            self._audit(
                connection,
                job["tenant_id"],
                "gateway-provisioner",
                "gateway.credential_revoked",
                credential["id"],
                operation_id=job["id"],
            )

    def _reject_key(self, job, credential, code):
        """Known existing but unsafe key: never activate; queue explicit deletion."""
        with self._transaction(job["tenant_id"]) as connection:
            self._guard(connection, job)
            binding = self._binding(connection, job["tenant_id"])
            connection.execute(
                g.credentials.update()
                .where(
                    g.credentials.c.id == credential["id"],
                )
                .values(status="retiring")
            )
            existing = connection.scalar(
                select(g.outbox.c.id).where(
                    g.outbox.c.credential_version_id == credential["id"],
                    g.outbox.c.action == "revoke",
                )
            )
            if not existing:
                self._job(connection, binding, credential, "revoke")
            connection.execute(
                g.outbox.update()
                .where(g.outbox.c.id == job["id"])
                .values(
                    status="failed",
                    phase="permissions_rejected",
                    owner=None,
                    lease_until=None,
                    error_code=code,
                    updated_at=_now(),
                )
            )
            connection.execute(
                g.bindings.update()
                .where(g.bindings.c.id == binding["id"])
                .values(
                    status=binding["status"]
                    if binding["status"] in ("revoking", "revoked")
                    else "degraded",
                    error_code=code,
                )
            )
            self._audit(
                connection,
                job["tenant_id"],
                "gateway-provisioner",
                "gateway.unsafe_key_rejected",
                credential["id"],
                error_code=code,
            )

    def _execute(self, job):
        with self._transaction(job["tenant_id"]) as connection:
            self._guard(connection, job)
            credential = dict(
                connection.execute(
                    select(g.credentials).where(
                        g.credentials.c.id == job["credential_version_id"],
                        g.credentials.c.tenant_id == job["tenant_id"],
                    )
                )
                .mappings()
                .one()
            )
        control = self._control()
        if job["action"] == "revoke":
            info = control.key_info(credential["external_key_id"])
            if info is not None:
                # Fresh read-back confirms this immutable, locally generated key
                # still exists. Deleting the exact hash is repeatable; it never
                # creates a replacement or targets the active generation by alias.
                self._phase(job, "revoke_sent")
                control.delete_key(credential["external_key_id"])
                if control.key_info(credential["external_key_id"]) is not None:
                    raise ControlFailure(
                        "gateway_revoke_pending_confirmation", unknown=True
                    )
            self._revoked(job, credential)
            return
        user = control.user_info(credential["external_user_id"])
        if user is None:
            if job["phase"] != "user_pending":
                raise ControlFailure("gateway_user_creation_unconfirmed", unknown=True)
            self._phase(job, "user_sent")
            control.create_user(credential["external_user_id"], credential["routes"])
            user = control.user_info(credential["external_user_id"])
            if user is None:
                raise ControlFailure("gateway_user_creation_unconfirmed", unknown=True)
        self._validate_user(user, credential)
        if job["phase"] in ("user_pending", "user_sent"):
            self._phase(job, "key_pending")
        info = control.key_info(credential["external_key_id"])
        if info is None:
            if job["phase"] != "key_pending":
                raise ControlFailure("gateway_key_creation_unconfirmed", unknown=True)
            self._phase(job, "key_sent")
            control.create_key(
                credential, self._decrypt(credential["secret_ciphertext"])
            )
            info = control.key_info(credential["external_key_id"])
            if info is None:
                raise ControlFailure("gateway_key_creation_unconfirmed", unknown=True)
        try:
            expires = self._validate_key(info, credential)
        except ControlFailure as exc:
            self._reject_key(job, credential, exc.code)
            return
        self._activate(job, credential, expires)

    def process_one(self):
        job = self._claim()
        if job is None:
            return False
        self._stats["processed"] += 1
        try:
            self._execute(job)
        except (ControlFailure, PlatformError) as exc:
            code = exc.code
            unknown = getattr(exc, "unknown", False)
            # Once a mutation was dispatched, even a failed read-back must be
            # reconciled. A safe HTTP rejection is a terminal configuration error.
            dispatched = job["phase"] in ("user_sent", "key_sent", "revoke_sent")
            rejected = getattr(exc, "mutation_rejected", False)
            state = (
                "reconciling"
                if unknown or (dispatched and not rejected)
                else ("retry_wait" if getattr(exc, "retryable", False) else "failed")
            )
            try:
                with self._transaction(job["tenant_id"]) as connection:
                    self._guard(connection, job)
                    phase = job["phase"]
                    if rejected and dispatched:
                        phase = phase.removesuffix("_sent") + "_rejected"
                    connection.execute(
                        g.outbox.update()
                        .where(g.outbox.c.id == job["id"])
                        .values(
                            status=state,
                            phase=phase,
                            error_code=code,
                            owner=None,
                            lease_until=None,
                            next_attempt_at=time.time()
                            + min(300, 5 * 2 ** min(job["attempt_count"], 6)),
                            updated_at=_now(),
                        )
                    )
                    binding = self._binding(connection, job["tenant_id"])
                    status = binding["status"]
                    if status in ("revoking", "revoked"):
                        pass
                    elif state == "reconciling" and job["action"] == "provision":
                        status = "degraded"
                    elif (
                        state == "failed"
                        and not binding["active_credential_version_id"]
                    ):
                        status = "failed"
                    connection.execute(
                        g.bindings.update()
                        .where(g.bindings.c.id == binding["id"])
                        .values(status=status, error_code=code)
                    )
                    self._audit(
                        connection,
                        job["tenant_id"],
                        "gateway-provisioner",
                        "gateway.sync_" + state,
                        job["id"],
                        error_code=code,
                    )
                self._record_outcome(state, code)
            except ControlFailure:
                pass  # New owner reconciles known resource IDs; never overwrite it.
        else:
            with self._transaction(job["tenant_id"]) as connection:
                result = connection.execute(
                    select(g.outbox.c.status, g.outbox.c.error_code).where(
                        g.outbox.c.id == job["id"],
                        g.outbox.c.tenant_id == job["tenant_id"],
                    )
                ).one()
            self._record_outcome(result.status, result.error_code)
        return True

    def _record_outcome(self, state, code=""):
        if state in ("applied", "failed", "reconciling", "retry_wait"):
            self._stats[state] += 1
        if state == "applied":
            self._stats["last_success_at"] = _now()
        elif code:
            self._stats.update(last_error_code=code, last_error_at=_now())
            logger.warning("gateway_sync_operation state=%s code=%s", state, code)

    async def serve(self, stop: asyncio.Event):
        # Strict-mode misconfiguration must fail startup instead of producing a
        # healthy idle heartbeat. Local legacy mode does not require control keys.
        if getattr(self.settings, "mode", "local") == "saas":
            self._cipher()
            self._control()
        interval = max(0.2, float(getattr(self.settings, "gateway_sync_interval", 2.0)))
        last_report = time.monotonic()
        metering = None
        if self.settings.gateway_control_url and self.settings.gateway_control_key:
            from agent_platform.modules.metering import MeteringReconciler

            metering = MeteringReconciler(self.db, self.settings, self._control())
        while not stop.is_set():
            try:
                worked = await asyncio.to_thread(self.process_one)
                if metering is not None:
                    metered = await asyncio.to_thread(metering.process_one)
                    worked = worked or metered
            except Exception:  # noqa: BLE001 - keep the provisioner alive without logging credentials
                # Do not emit exception repr: HTTP/DB objects can hold secrets.
                self._stats["internal_errors"] += 1
                self._stats.update(
                    last_error_code="gateway_sync_internal_error", last_error_at=_now()
                )
                clock = time.monotonic()
                if clock - self._last_internal_warning >= 60:
                    logger.warning(
                        "gateway_sync_internal_error count=%s",
                        self._stats["internal_errors"],
                    )
                    self._last_internal_warning = clock
                worked = False
            if time.monotonic() - last_report >= 60:
                logger.info("gateway_sync_statistics %s", self.statistics())
                last_report = time.monotonic()
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
