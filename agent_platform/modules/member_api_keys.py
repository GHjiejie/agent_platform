"""Personal keys authenticate model calls only, without browser privileges."""

import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update

from agent_platform.infrastructure import api_key_tables as ak
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.authorization import require_capability
from agent_platform.modules.run_records import append_event, now, uid


class MemberKeys:
    def __init__(self, platform):
        self.platform, self.db = platform, platform.db

    @staticmethod
    def public(row):
        return {
            k: v
            for k, v in dict(row).items()
            if k
            not in {"secret_hash", "tenant_id", "membership_id", "membership_version"}
        }

    def create(self, user, body):
        from agent_platform.modules.platform import audit

        with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
            current = self.platform.identity.assert_current(
                connection, user, "runs.execute"
            )
            if not body.name.strip():
                raise PlatformError(422, "invalid_key_name", "请输入密钥名称。")
            model_ids = list(dict.fromkeys(body.model_ids))
            for model_id in model_ids:
                model = self.platform.owned(connection, t.models, user, model_id)
                if not model["active"]:
                    raise PlatformError(
                        422, "model_disabled", "仅可选择当前组织已启用的模型。"
                    )
            count = connection.scalar(
                select(func.count())
                .select_from(ak.keys)
                .where(
                    ak.keys.c.tenant_id == user["tenant_id"],
                    ak.keys.c.user_id == user["id"],
                    ak.keys.c.revoked_at.is_(None),
                    ak.keys.c.expires_at > now(),
                )
            )
            if count >= 10:
                raise PlatformError(
                    429,
                    "api_key_limit",
                    "最多保留 10 个有效个人密钥，请先撤销不再使用的密钥。",
                )
            identifier = uid()
            secret = f"ap_{user['tenant_id']}.{identifier}.{secrets.token_urlsafe(32)}"
            row = {
                "id": identifier,
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "membership_id": current["membership_id"],
                "membership_version": current["membership_version"],
                "name": body.name.strip(),
                "prefix": "ap_" + identifier[:8] + "…" + secret[-4:],
                "secret_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "model_ids": model_ids,
                "created_at": now(),
                "expires_at": (
                    datetime.now(UTC) + timedelta(days=body.expires_in_days)
                ).isoformat(),
                "revoked_at": None,
                "last_used_at": None,
            }
            connection.execute(insert(ak.keys).values(**row))
            audit(connection, current, "api_key.create", identifier)
        return {**self.public(row), "secret": secret}

    def list(self, user, all_members=False):
        with self.db.read() as connection:
            current = self.platform.identity.assert_current(
                connection, user, "runs.execute"
            )
            query = select(ak.keys).where(ak.keys.c.tenant_id == user["tenant_id"])
            if all_members:
                require_capability(current, "members.manage")
            else:
                query = query.where(ak.keys.c.user_id == user["id"])
            return {
                "items": [
                    self.public(row)
                    for row in connection.execute(
                        query.order_by(ak.keys.c.created_at.desc()).limit(200)
                    ).mappings()
                ]
            }

    def revoke(self, user, key_id):
        from agent_platform.modules.platform import audit

        with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
            current = self.platform.identity.assert_current(
                connection, user, "runs.execute"
            )
            key = self.platform.owned(connection, ak.keys, user, key_id)
            if key["user_id"] != user["id"]:
                require_capability(current, "members.manage")
            if not key["revoked_at"]:
                connection.execute(
                    update(ak.keys)
                    .where(
                        ak.keys.c.id == key_id, ak.keys.c.tenant_id == user["tenant_id"]
                    )
                    .values(revoked_at=now())
                )
                connection.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.tenant_id == user["tenant_id"],
                        t.runs.c.spec["api_key_id"].as_string() == key_id,
                        t.runs.c.status.in_(("queued", "running", "cancelling")),
                    )
                    .values(cancel_requested=True)
                )
                audit(connection, current, "api_key.revoke", key_id)
        return {"id": key_id, "revoked": True}

    def check(self, connection, tenant_id, key_id, model_id=None):
        row = (
            connection.execute(
                select(ak.keys).where(
                    ak.keys.c.tenant_id == tenant_id, ak.keys.c.id == key_id
                )
            )
            .mappings()
            .first()
        )
        if not row or row["revoked_at"] or row["expires_at"] <= now():
            raise PlatformError(
                401, "invalid_api_key", "API 密钥无效、已过期或已撤销。"
            )
        try:
            user = self.platform.identity.assert_current(
                connection,
                {
                    "id": row["user_id"],
                    "tenant_id": tenant_id,
                    "membership_id": row["membership_id"],
                    "membership_version": row["membership_version"],
                },
                "runs.execute",
            )
        except PlatformError:
            raise PlatformError(
                401, "invalid_api_key", "API 密钥所属成员或组织权限已失效。"
            ) from None
        if model_id and row["model_ids"] and model_id not in row["model_ids"]:
            raise PlatformError(403, "model_not_allowed", "此 API 密钥未获该模型授权。")
        if model_id:
            model = (
                connection.execute(
                    select(t.models).where(
                        t.models.c.tenant_id == tenant_id, t.models.c.id == model_id
                    )
                )
                .mappings()
                .first()
            )
            if not model or not model["active"]:
                raise PlatformError(403, "model_disabled", "模型已停用或授权已撤销。")
            if not self.platform.gateway.availability(
                tenant_id, dict(model), connection=connection
            )["ready"]:
                raise PlatformError(
                    403, "model_grant_revoked", "模型路由或组织授权当前不可用。"
                )
        return user, dict(row)

    def authenticate(self, token):
        try:
            tenant_id, key_id, material = token.removeprefix("ap_").split(".")
            if (
                not token.startswith("ap_")
                or len(tenant_id) != 32
                or len(key_id) != 32
                or len(material) != 43
            ):
                raise ValueError
            int(tenant_id, 16), int(key_id, 16)
        except (ValueError, AttributeError):
            raise PlatformError(
                401, "invalid_api_key", "请提供有效的 Bearer API 密钥。"
            ) from None
        with self.db.tenant_scope(tenant_id), self.db.read() as connection:
            user, row = self.check(connection, tenant_id, key_id)
            if not secrets.compare_digest(
                row["secret_hash"], hashlib.sha256(token.encode()).hexdigest()
            ):
                raise PlatformError(
                    401, "invalid_api_key", "API 密钥无效、已过期或已撤销。"
                )
        return {"user": user, "key_id": key_id}

    def models(self, auth):
        user = auth["user"]
        with self.db.read() as connection:
            _, key = self.check(connection, user["tenant_id"], auth["key_id"])
            rows = connection.execute(
                select(t.models)
                .where(
                    t.models.c.tenant_id == user["tenant_id"],
                    t.models.c.active.is_(True),
                )
                .order_by(t.models.c.alias)
            ).mappings()
            items = []
            for row in rows:
                if key["model_ids"] and row["id"] not in key["model_ids"]:
                    continue
                if not self.platform.gateway.availability(
                    user["tenant_id"], dict(row), connection=connection
                )["ready"]:
                    continue
                items.append(
                    {
                        "id": row["alias"],
                        "object": "model",
                        "created": int(
                            datetime.fromisoformat(row["created_at"]).timestamp()
                        ),
                        "owned_by": "agent-platform",
                    }
                )
            return {"object": "list", "data": items}

    def enqueue(self, auth, payload, idempotency_key=None):
        from agent_platform.modules.runtime.contracts import canonical_digest

        user, key_id = auth["user"], auth["key_id"]
        try:
            fingerprint = canonical_digest(payload)
        except ValueError:
            raise PlatformError(
                422, "invalid_request", "请求超出 JSON 结构或大小限制。"
            ) from None
        if idempotency_key and not 1 <= len(idempotency_key) <= 160:
            raise PlatformError(
                422, "invalid_idempotency_key", "幂等标识须为 1 至 160 字符。"
            )
        key = (
            "model-api:"
            + hashlib.sha256(
                f"{key_id}:{idempotency_key or uid()}".encode()
            ).hexdigest()
        )
        with self.db.transaction("tenant:" + user["tenant_id"]) as connection:
            current, _ = self.check(connection, user["tenant_id"], key_id)
            model = (
                connection.execute(
                    select(t.models).where(
                        t.models.c.tenant_id == user["tenant_id"],
                        t.models.c.alias == payload["model"],
                    )
                )
                .mappings()
                .first()
            )
            if not model:
                raise PlatformError(
                    404,
                    "model_not_found",
                    "模型不存在或当前组织无权使用。请使用 /v1/models 返回的模型 ID。",
                )
            self.check(connection, user["tenant_id"], key_id, model["id"])
            previous = (
                connection.execute(
                    select(t.runs).where(
                        t.runs.c.tenant_id == user["tenant_id"],
                        t.runs.c.user_id == user["id"],
                        t.runs.c.idempotency_key == key,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise PlatformError(
                        409,
                        "idempotency_conflict",
                        "同一幂等标识不能用于不同的模型请求。",
                    )
                if previous["status"] not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "expired",
                    "interrupted",
                }:
                    raise PlatformError(
                        409, "request_in_progress", "同一幂等请求仍在运行，请稍后重试。"
                    )
                return {
                    **dict(previous),
                    "_stream_id": self.platform._acquire_stream(
                        connection, current, previous["id"]
                    ),
                }
            queued = connection.scalar(
                select(func.count())
                .select_from(t.runs)
                .where(
                    t.runs.c.tenant_id == user["tenant_id"], t.runs.c.status == "queued"
                )
            )
            ceiling = connection.scalar(
                select(nt.entitlements.c.max_queued_runs).where(
                    nt.entitlements.c.tenant_id == user["tenant_id"]
                )
            )
            if queued >= ceiling:
                raise PlatformError(429, "queue_full", "组织运行队列已满，请稍后再试。")
            output = payload["max_tokens"] or min(
                self.platform.settings.default_output_tokens, model["max_output_tokens"]
            )
            if output > model["max_output_tokens"]:
                raise PlatformError(
                    422, "output_limit", "请求的输出上限超过模型登记的能力。"
                )
            spec = self.platform.freeze_execution_spec(
                connection,
                current,
                {
                    "snapshot_schema_version": 7,
                    "source": "model_api",
                    "api_key_id": key_id,
                    "max_tokens": output,
                    "max_steps": 1,
                    "tools": payload["tools"],
                    "temperature": payload["temperature"],
                    "execution_policy": {},
                },
                model_id=model["id"],
                reasoning_effort=payload["reasoning_effort"],
            )
            result = {
                "id": uid(),
                "tenant_id": user["tenant_id"],
                "user_id": user["id"],
                "session_id": None,
                "agent_name": "模型 API",
                "spec": spec,
                "message": "模型 API 调用",
                "created_at": now(),
                "queue_expires_at": time.time() + self.platform.settings.queue_timeout,
                "status": "queued",
                "error": "",
                "idempotency_key": key,
                "request_hash": fingerprint,
                "cancel_requested": False,
                "fence": 0,
            }
            connection.execute(insert(t.runs).values(**result))
            connection.execute(
                insert(ak.requests).values(
                    id=result["id"],
                    tenant_id=user["tenant_id"],
                    key_id=key_id,
                    messages=payload["messages"],
                    tools=payload["tools"],
                    created_at=now(),
                    error_code="",
                )
            )
            connection.execute(
                update(ak.keys)
                .where(ak.keys.c.tenant_id == user["tenant_id"], ak.keys.c.id == key_id)
                .values(last_used_at=now())
            )
            append_event(connection, result["id"], "run.queued", {"status": "queued"})
            result["_stream_id"] = self.platform._acquire_stream(
                connection, current, result["id"]
            )
        return result

    def read(self, auth, run_id, cursor=0):
        user = auth["user"]
        with self.db.read() as connection:
            self.check(connection, user["tenant_id"], auth["key_id"])
            run = self.platform.owned(connection, t.runs, user, run_id, own=True)
            record = (
                connection.execute(
                    select(ak.requests).where(
                        ak.requests.c.tenant_id == user["tenant_id"],
                        ak.requests.c.id == run_id,
                        ak.requests.c.key_id == auth["key_id"],
                    )
                )
                .mappings()
                .first()
            )
            if not record:
                raise PlatformError(404, "not_found", "模型请求不存在。")
            self.check(
                connection, user["tenant_id"], auth["key_id"], run["spec"]["model_id"]
            )
            events = [
                dict(row)
                for row in connection.execute(
                    select(t.run_events)
                    .where(
                        t.run_events.c.tenant_id == user["tenant_id"],
                        t.run_events.c.run_id == run_id,
                        t.run_events.c.sequence > cursor,
                    )
                    .order_by(t.run_events.c.sequence)
                    .limit(100)
                ).mappings()
            ]
            return run, dict(record), events

    def cancel(self, auth, run_id):
        with self.db.transaction("tenant:" + auth["user"]["tenant_id"]) as connection:
            connection.execute(
                update(t.runs)
                .where(
                    t.runs.c.tenant_id == auth["user"]["tenant_id"],
                    t.runs.c.id == run_id,
                    t.runs.c.user_id == auth["user"]["id"],
                    t.runs.c.spec["api_key_id"].as_string() == auth["key_id"],
                    t.runs.c.status.in_(("queued", "running", "cancelling")),
                )
                .values(cancel_requested=True)
            )
