"""Same-origin FastAPI application, persistent SSE and React static hosting."""

# FastAPI inspects these declarative dependency defaults; they are not executed
# as mutable application state.
# ruff: noqa: B008

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit
from uuid import uuid4

from anyio import CancelScope
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy import delete, select
from starlette.middleware.base import BaseHTTPMiddleware

from agent_platform.apps.api import responses as r
from agent_platform.apps.api import schemas as s
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.request_context import request_id
from agent_platform.modules import ag_ui
from agent_platform.modules.authorization import require_capability
from agent_platform.modules.billing.service import BillingService
from agent_platform.modules.builtin_agents import builtin_metadata
from agent_platform.modules.platform import (
    STREAM_LEASE_SECONDS,
    TERMINAL_STATUSES,
    Platform,
    audit,
    digest,
)

logger = logging.getLogger(__name__)
COOKIE = "agent_platform_session"


class _StreamLeaseLost(Exception):
    pass


class LeasedStreamingResponse(StreamingResponse):
    """Keep quota admission alive independently of a blocked response send."""

    def __init__(
        self,
        content,
        *,
        renew: Callable[[], Awaitable[None]],
        release: Callable[[], Awaitable[None]],
        valid_until: float,
        lease_seconds: float = STREAM_LEASE_SECONDS,
        renew_seconds: float = 10,
        authorize: Callable[[], Awaitable[None]] | None = None,
        authorize_seconds: float = 0.25,
        **kwargs,
    ):
        super().__init__(content, **kwargs)
        self._renew = renew
        self._release = release
        self._valid_until = valid_until
        self._lease_seconds = lease_seconds
        self._renew_seconds = renew_seconds
        self._authorize = authorize
        self._authorize_seconds = authorize_seconds
        self._authority_until = time.monotonic() + 2 * authorize_seconds

    async def _keep_authority(self):
        while True:
            await asyncio.sleep(self._authorize_seconds)
            checked_at = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._authorize(), timeout=self._authorize_seconds
                )
            except Exception as exc:  # noqa: BLE001 - fail closed after response starts
                logger.info("stream authority ended error=%s", type(exc).__name__)
                return
            self._authority_until = checked_at + 2 * self._authorize_seconds

    async def _keep_lease(self):
        while True:
            remaining = self._valid_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self._renew_seconds, remaining))
            started_at = time.monotonic()
            remaining = self._valid_until - started_at
            if remaining <= 0:
                return
            try:
                # A blocked database renewal cannot leave a sender alive beyond
                # its last confirmed lease. CAS also rejects late thread results.
                await asyncio.wait_for(self._renew(), timeout=remaining)
            except Exception as exc:  # noqa: BLE001 - fail closed after response starts
                logger.info("stream lease ended error=%s", type(exc).__name__)
                return
            self._valid_until = started_at + self._lease_seconds

    async def stream_response(self, send):
        async def leased_send(message):
            if time.monotonic() >= self._valid_until:
                raise _StreamLeaseLost()
            if self._authorize and time.monotonic() >= self._authority_until:
                raise _StreamLeaseLost()
            await send(message)

        sender = asyncio.create_task(super().stream_response(leased_send))
        keeper = asyncio.create_task(self._keep_lease())
        authority = (
            asyncio.create_task(self._keep_authority()) if self._authorize else None
        )
        tasks = {sender, keeper} | ({authority} if authority else set())
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                try:
                    await sender
                    return
                except _StreamLeaseLost:
                    pass
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            # Close writable connections cleanly; a stalled client cannot block
            # cancellation or keep an unadmitted stream alive during cleanup.
            with suppress(TimeoutError, OSError):
                await asyncio.wait_for(
                    send(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    ),
                    timeout=0.1,
                )
        finally:
            sender.cancel()
            keeper.cancel()
            if authority:
                authority.cancel()
            # Starlette cancels its response scope on disconnect/completion.
            # Shield cleanup so that cancellation cannot strand quota admission.
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                if close := getattr(self.body_iterator, "aclose", None):
                    await close()
                with suppress(Exception):
                    await asyncio.wait_for(self._release(), timeout=1)


class BoundaryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        identifier = uuid4().hex
        token = request_id.set(identifier)
        started = time.monotonic()
        try:
            response = await self.handle(request, call_next)
            response.headers["X-Request-ID"] = identifier
            route = request.scope.get("route")
            logger.info(
                "request id=%s method=%s route=%s status=%s duration_ms=%d",
                identifier,
                request.method,
                getattr(route, "path", "unmatched"),
                response.status_code,
                int((time.monotonic() - started) * 1000),
            )
            return response
        finally:
            request_id.reset(token)

    async def handle(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {
                        "error": {
                            "code": "invalid_origin",
                            "message": "请求来源不匹配。",
                        }
                    },
                    status_code=403,
                )
            try:
                if int(request.headers.get("content-length", "0")) > 256000:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "body_too_large",
                                "message": "请求内容过大。",
                            }
                        },
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_length", "message": "请求无效。"}},
                    status_code=400,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def create_app(settings: Settings | None = None, *, gateway=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.service_role != "api":
        raise ValueError("API must use API configuration validation")
    db = Database(settings.database_url)
    platform = Platform(db, settings)

    @asynccontextmanager
    async def lifespan(app):
        db.assert_schema(saas=settings.mode == "saas")
        if settings.gateway_control_key:
            raise RuntimeError("API must not receive the LiteLLM control-plane key")
        stop = asyncio.Event()
        task = None
        if settings.embedded_worker:
            from agent_platform.apps.worker.main import Worker

            task = asyncio.create_task(Worker(platform, gateway=gateway).serve(stop))
        yield
        stop.set()
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        db.close()

    app = FastAPI(title="Agent Platform", version="0.1.0", lifespan=lifespan)
    app.state.platform = platform
    app.state.database = db
    app.add_middleware(BoundaryMiddleware)

    @app.exception_handler(PlatformError)
    async def platform_error(_request, exc):
        headers = {"Retry-After": "60"} if exc.status == 429 else None
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Never echo password/request payloads in validation responses.
        details = [
            {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "请检查输入内容。",
                    "details": details,
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request, exc):
        logger.error("platform request failed: %s", type(exc).__name__)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "服务暂时不可用，请重试。",
                }
            },
            status_code=500,
        )

    def principal(request: Request):
        context = platform.authenticate(request.cookies.get(COOKIE))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, context["csrf_token"]):
                raise PlatformError(403, "csrf_failed", "会话校验失败，请刷新后重试。")
        return context["user"]

    async def auth(request: Request, actor=Depends(principal)):
        tenant_id = request.path_params.get("tenant_id")
        if not tenant_id:
            memberships = [x for x in actor["memberships"] if x["status"] == "active"]
            if len(memberships) != 1:
                raise PlatformError(
                    409, "tenant_selection_required", "请明确选择组织并使用 v2 API。"
                )
            tenant_id = memberships[0]["tenant_id"]
        readonly = request.method in {"GET", "HEAD", "OPTIONS"}
        context = platform.identity.context(
            actor["id"], tenant_id, allow_suspended=readonly
        )
        path = request.url.path
        relative = (
            path.split(f"/tenants/{tenant_id}/", 1)[-1]
            if "/tenants/" in path
            else path.removeprefix("/api/v1/")
        )
        resource = relative.split("/")[0]
        if resource in {"sessions", "runs"}:
            capability = "runs.read_own" if readonly else "runs.execute"
        elif resource == "agents":
            capability = "agents.read" if readonly else "agents.manage"
        elif resource == "models":
            capability = "models.read" if readonly else "models.policy"
        elif resource == "users":
            capability = "members.read" if readonly else "members.manage"
        elif resource == "audit":
            capability = "audit.read"
        elif resource == "quotas":
            capability = "quotas.manage"
        elif resource == "gateway":
            capability = "platform.gateway.manage"
        elif resource == "billing":
            capability = "billing.read" if readonly else "platform.billing.manage"
        else:
            capability = "tenant.read"
        require_capability(context, capability)
        with db.tenant_scope(tenant_id):
            yield context

    def admin(user=Depends(auth)):
        if user["role"] != "admin":
            raise PlatformError(403, "admin_required", "此操作需要管理员权限。")
        return user

    def billing_as(user, action=None, target=None):
        def authorize(connection):
            if action == "quota.update":
                platform.identity.assert_current(connection, user, "quotas.manage")
            else:
                platform.require_platform(connection, user, "platform.billing.manage")
            if action:
                audit(connection, user, action, target)

        return BillingService(db, settings.redis_url, authorize=authorize)

    def idempotency(key: str | None) -> str:
        if not key or len(key) > 160:
            raise PlatformError(
                400, "idempotency_required", "请提供有效的 Idempotency-Key。"
            )
        return key

    @app.get("/api/v1/health")
    def health():
        return {
            "status": "ok",
            "gateway_configured": bool(settings.litellm_url and settings.litellm_key),
            "rate_limit_storage": "redis+database"
            if settings.redis_url
            else "database",
        }

    @app.get("/api/v1/ready")
    def ready():
        from agent_platform.modules.health import readiness

        try:
            result = readiness(db, settings)
        except Exception:  # noqa: BLE001 - readiness exposes no backend exception details
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse(
            result, status_code=200 if result["status"] == "ready" else 503
        )

    @app.get("/api/v1/platform-status")
    def service_status(actor=Depends(principal)):
        from agent_platform.modules.health import platform_status

        with db.read() as connection:
            platform.require_platform(connection, actor, "platform.tenants.manage")
        return platform_status(db, settings)

    @app.post("/api/v1/auth/login", response_model=r.AuthResponse)
    def login(body: s.Login, request: Request):
        result = platform.login(
            body.email,
            body.password,
            request.client.host if request.client else "unknown",
        )
        token = result.pop("token")
        response = JSONResponse(
            r.AuthResponse.model_validate(result).model_dump(mode="json")
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/v1/auth/me", response_model=r.AuthResponse)
    def me(request: Request, _user=Depends(principal)):
        return platform.authenticate(request.cookies.get(COOKIE))

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, user=Depends(principal)):
        with db.transaction(f"identity:{user['id']}") as connection:
            connection.execute(
                delete(t.auth_sessions).where(
                    t.auth_sessions.c.token_hash
                    == digest(request.cookies.get(COOKIE, ""))
                )
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/api/v1/auth/password")
    def password(body: s.PasswordChange, user=Depends(principal)):
        platform.change_password(user, body.current_password, body.new_password)
        return {"ok": True}

    @app.get("/api/v1/dashboard")
    def dashboard(user=Depends(auth)):
        return platform.dashboard(user)

    @app.get("/api/v1/gateway")
    def gateway_admin(_user=Depends(admin)):
        return {
            "admin_url": settings.litellm_admin_url,
            "configured": bool(settings.litellm_admin_url),
        }

    @app.get("/api/v1/users")
    def users(user=Depends(admin)):
        return {"items": platform.identity.members(user)}

    @app.post("/api/v1/users", status_code=201)
    def create_user(body: s.UserCreate, user=Depends(admin)):
        raise PlatformError(
            410,
            "use_invitations",
            "请使用成员邀请接口；组织管理员不能创建或重置全局账号。",
        )

    @app.patch("/api/v1/users/{user_id}")
    def patch_user(user_id: str, body: s.UserPatch, user=Depends(admin)):
        raise PlatformError(
            410, "use_memberships", "请使用成员关系接口修改组织内权限。"
        )

    @app.get("/api/v1/models")
    def models(user=Depends(auth)):
        rows = platform.list_resources(t.models, user)
        policy = platform.identity.model_policy(user)
        return {
            "items": [
                {**row, "is_default": row["id"] == policy.get("default_model_id")}
                for row in rows
            ],
            "default_model": settings.default_model if user["role"] == "admin" else "",
        }

    @app.post("/api/v1/models", status_code=201)
    def create_model(body: s.ModelCreate, user=Depends(admin)):
        raise PlatformError(
            410, "use_platform_models", "请通过平台模型授权接口登记模型和价格。"
        )

    @app.patch("/api/v1/models/{model_id}")
    def patch_model(model_id: str, body: s.ModelPatch, user=Depends(admin)):
        values = body.model_dump(exclude_none=True)
        if set(values) == {"active"}:
            with db.transaction(f"tenant:{user['tenant_id']}") as connection:
                platform.identity.assert_current(connection, user, "models.policy")
            return platform.gateway.set_model_enabled(
                user["tenant_id"], model_id, values["active"], actor=user
            )
        raise PlatformError(
            403, "use_platform_models", "价格与部署只能由平台运营入口修改。"
        )

    @app.get("/api/v1/agents")
    def agents(user=Depends(auth)):
        rows = platform.list_resources(t.agents, user)
        if user["role"] != "admin":
            # Members only see the actual published specification, not private drafts.
            published = []
            with db.read() as connection:
                for row in rows:
                    version = (
                        connection.execute(
                            select(t.agent_versions).where(
                                t.agent_versions.c.agent_id == row["id"],
                                t.agent_versions.c.version == row["published_version"],
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if version:
                        published.append(
                            {**version["spec"], "published_version": version["version"]}
                        )
            rows = published
        return {
            "items": [
                {**row, **builtin_metadata(row.get("builtin_key"))} for row in rows
            ]
        }

    @app.get("/api/v1/token-defaults")
    def token_defaults(user=Depends(auth)):
        return {
            "output_tokens": settings.default_output_tokens,
            "context_window": settings.default_context_window,
        }

    @app.get("/api/v1/agents/execution-options")
    def agent_execution_options(user=Depends(auth)):
        from agent_platform.modules.execution_policy import (
            MAX_MODEL_CALLS,
            MAX_OUTPUT_TOKENS,
        )

        return {
            "timeout_seconds": settings.run_timeout,
            "max_cost": settings.max_run_cost,
            "max_steps": MAX_MODEL_CALLS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "default_output_tokens": settings.default_output_tokens,
            "default_context_window": settings.default_context_window,
        }

    @app.post("/api/v1/agents", status_code=201)
    def create_agent(body: s.AgentCreate, user=Depends(admin)):
        values = body.model_dump()
        if "max_tokens" not in body.model_fields_set:
            values["max_tokens"] = settings.default_output_tokens
        return platform.save_agent(user, values)

    @app.patch("/api/v1/agents/{agent_id}")
    def patch_agent(agent_id: str, body: s.AgentPatch, user=Depends(admin)):
        # Explicit null clears the model preference; omitted fields keep the draft.
        values = body.model_dump(exclude_none=True)
        if "model_id" in body.model_fields_set:
            values["model_id"] = body.model_id
        if "execution_policy" in body.model_fields_set:
            values["execution_policy"] = (
                body.execution_policy.model_dump(exclude_unset=True)
                if body.execution_policy is not None
                else {}
            )
        return platform.save_agent(user, values, agent_id)

    @app.post("/api/v1/agents/{agent_id}/publish")
    def publish(agent_id: str, user=Depends(admin)):
        return platform.publish(user, agent_id)

    @app.get("/api/v1/sessions", response_model=r.SessionsResponse)
    def sessions(
        user=Depends(auth),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=300),
    ):
        # Conversation picker shows only the caller's own sessions, including admins.
        rows = platform.list_resources(
            t.sessions, user, own=True, limit=limit, cursor=cursor
        )
        return {
            "items": rows,
            "next_cursor": platform.encode_cursor(rows[-1])
            if len(rows) == limit
            else None,
        }

    @app.post("/api/v1/sessions", status_code=201, response_model=r.SessionResponse)
    def create_session(body: s.SessionCreate, user=Depends(auth)):
        return platform.create_session(user, body.agent_id, body.title)

    @app.get(
        "/api/v1/sessions/{session_id}",
        response_model=r.SessionDetailResponse,
        response_model_exclude_unset=True,
    )
    def session_detail(session_id: str, user=Depends(auth)):
        return platform.session_detail(user, session_id)

    @app.post(
        "/api/v1/sessions/{session_id}/runs",
        status_code=202,
        response_model=r.RunResponse,
        response_model_exclude_unset=True,
    )
    def create_run(
        session_id: str,
        body: s.RunCreate,
        user=Depends(auth),
        idempotency_key: str | None = Header(default=None),
    ):
        return platform.enqueue(
            user, session_id, body.message, idempotency(idempotency_key), body.model_id
        )

    @app.get(
        "/api/v1/runs", response_model=r.RunsResponse, response_model_exclude_unset=True
    )
    def runs(
        user=Depends(auth),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=300),
    ):
        rows = platform.list_resources(
            t.runs, user, own=True, limit=limit, cursor=cursor
        )
        return {
            "items": [platform.get_run(user, row["id"]) for row in rows],
            "next_cursor": platform.encode_cursor(rows[-1])
            if len(rows) == limit
            else None,
        }

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=r.RunResponse,
        response_model_exclude_unset=True,
    )
    def run(run_id: str, user=Depends(auth)):
        return platform.get_run(user, run_id)

    @app.post(
        "/api/v1/runs/{run_id}/cancel",
        response_model=r.RunResponse,
        response_model_exclude_unset=True,
    )
    def cancel(run_id: str, user=Depends(auth)):
        return platform.cancel_run(user, run_id)

    @app.get("/api/v1/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        user=Depends(auth),
    ):
        raw_cursor = request.headers.get("last-event-id")
        if raw_cursor:
            try:
                after = max(after, int(raw_cursor))
            except ValueError:
                raise PlatformError(400, "invalid_cursor", "事件游标无效。") from None
        return await event_response(run_id, request, user, after=after)

    @app.post(
        "/api/v1/sessions/{session_id}/ag-ui",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}
        },
    )
    async def run_agent(
        session_id: str,
        body: ag_ui.PlatformRunInput,
        request: Request,
        user=Depends(auth),
    ):
        cursor = ag_ui.parse_cursor(request.headers.get("last-event-id"))
        run = await asyncio.to_thread(
            ag_ui.enqueue, platform, user, session_id, body, cursor[0]
        )
        return await event_response(
            run["id"],
            request,
            user,
            protocol_cursor=cursor,
            admitted_stream=run["_stream_id"],
        )

    @app.get(
        "/api/v1/runs/{run_id}/ag-ui",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}
        },
    )
    async def connect_agent(
        run_id: str,
        request: Request,
        after: str | None = Query(default=None, max_length=32),
        user=Depends(auth),
    ):
        cursor = max(
            ag_ui.parse_cursor(after),
            ag_ui.parse_cursor(request.headers.get("last-event-id")),
        )
        return await event_response(run_id, request, user, protocol_cursor=cursor)

    async def event_response(
        run_id,
        request,
        user,
        *,
        after=0,
        protocol_cursor=None,
        admitted_stream=None,
    ):
        projection = None
        pending = []
        stream_id = admitted_stream
        if protocol_cursor is not None:
            after = protocol_cursor[0]

        from agent_platform.infrastructure.events import configure_notifications

        notifier = configure_notifications(db, settings.redis_url)

        def validate_authority():
            # A single read composition avoids rebuilding the global principal
            # twice per poll, while enforcing session expiry and both versions.
            with db.tenant_scope(user["tenant_id"]), db.read() as connection:
                session = connection.scalar(
                    select(t.auth_sessions.c.user_id).where(
                        t.auth_sessions.c.token_hash
                        == digest(request.cookies.get(COOKIE) or ""),
                        t.auth_sessions.c.user_id == user["id"],
                        t.auth_sessions.c.expires_at > time.time(),
                    )
                )
                if session is None:
                    raise PlatformError(401, "session_expired", "登录已失效。")
                # SSE stops on suspension even though ordinary read-only API
                # pages may keep access to completed tenant resources.
                platform.identity.assert_current(
                    connection, {**user, "_allow_suspended": False}, "runs.read_own"
                )

        async def authorize_stream():
            await asyncio.to_thread(validate_authority)

        def replay(cursor):
            with db.tenant_scope(user["tenant_id"]), db.read() as connection:
                run = platform.owned(connection, t.runs, user, run_id, own=True)
                rows = [
                    dict(row)
                    for row in connection.execute(
                        select(t.run_events)
                        .where(
                            t.run_events.c.tenant_id == user["tenant_id"],
                            t.run_events.c.run_id == run_id,
                            t.run_events.c.sequence > cursor,
                        )
                        .order_by(t.run_events.c.sequence)
                        .limit(200)
                    ).mappings()
                ]
                return rows, run["status"]

        try:
            await asyncio.to_thread(platform.get_run, user, run_id)
            valid_until = time.monotonic() + STREAM_LEASE_SECONDS
            stream_id = stream_id or await asyncio.to_thread(
                platform.acquire_stream, user, run_id
            )
            if admitted_stream:
                await asyncio.to_thread(platform.update_stream, user, stream_id)
            if protocol_cursor is not None:
                projection = await asyncio.to_thread(
                    ag_ui.read_projection, platform, user, run_id
                )
                if after:
                    # Restore both mapper state and UI messages at a composite
                    # cursor. A single durable row can encode several AG-UI
                    # events (e.g. TOOL_CALL_START / ARGS / END).
                    scan, found = 0, False
                    while not found:
                        if time.monotonic() >= valid_until - 20:
                            valid_until = time.monotonic() + STREAM_LEASE_SECONDS
                            await authorize_stream()
                            await asyncio.to_thread(
                                platform.update_stream, user, stream_id
                            )
                        rows, _ = await asyncio.to_thread(replay, scan)
                        for row in rows:
                            if row["sequence"] > after:
                                break
                            mapped = projection.project(row)
                            for part, event in enumerate(mapped):
                                if (row["sequence"], part) <= protocol_cursor:
                                    projection.apply(event)
                                else:
                                    pending.append((f"{row['sequence']}.{part}", event))
                            scan = row["sequence"]
                            if scan == after:
                                if protocol_cursor[1] >= len(mapped):
                                    raise PlatformError(
                                        400, "invalid_cursor", "事件游标超出有效范围。"
                                    )
                                found = True
                                break
                        if not found and (not rows or scan < rows[-1]["sequence"]):
                            raise PlatformError(
                                400, "invalid_cursor", "事件游标超出有效范围。"
                            )
            valid_until = time.monotonic() + STREAM_LEASE_SECONDS
            # Confirm a fresh lease after reconstruction as well as admission;
            # replay preparation is covered by the same connection budget.
            await asyncio.to_thread(platform.update_stream, user, stream_id)
        except BaseException:
            if stream_id:
                with CancelScope(shield=True):
                    await asyncio.to_thread(
                        platform.update_stream, user, stream_id, release=True
                    )
            raise

        async def renew_stream():
            # Revocation and session expiry remain enforceable even when the
            # client stops consuming and the event generator is suspended.
            await authorize_stream()
            await asyncio.to_thread(platform.update_stream, user, stream_id)

        async def release_stream():
            await asyncio.to_thread(
                platform.update_stream, user, stream_id, release=True
            )

        async def stream():
            cursor = after
            heartbeat_at = time.monotonic()
            sent_terminal = False

            def frames(row):
                nonlocal sent_terminal
                if projection is None:
                    payload = json.dumps(row, ensure_ascii=False)
                    return [
                        f"id: {row['sequence']}\nevent: {row['type']}\ndata: {payload}\n\n"
                    ]
                result = []
                for part, event in enumerate(projection.project(row)):
                    projection.apply(event)
                    sent_terminal |= event is projection.terminal
                    result.append(ag_ui.encode(event, f"{row['sequence']}.{part}"))
                return result

            # Subscribe before the DB read to close the read/wait race. Every
            # wakeup replays persisted rows from the caller's durable cursor.
            async with notifier.watch(user["tenant_id"], run_id) as watch:
                try:
                    await authorize_stream()
                except PlatformError:
                    return
                if projection is not None:
                    for event in projection.prefix():
                        yield ag_ui.encode(event)
                    for identifier, event in pending:
                        projection.apply(event)
                        sent_terminal |= event is projection.terminal
                        yield ag_ui.encode(event, identifier)
                while not await request.is_disconnected():
                    observed = watch.version
                    try:
                        rows, status = await asyncio.to_thread(replay, cursor)
                    except PlatformError:
                        return
                    for row in rows:
                        cursor = row["sequence"]
                        for frame in frames(row):
                            yield frame
                    if status in TERMINAL_STATUSES and len(rows) < 200:
                        # Final status and event commit atomically. The extra
                        # replay covers SQLite's separate read snapshots too.
                        tail, _ = await asyncio.to_thread(replay, cursor)
                        for row in tail:
                            for frame in frames(row):
                                yield frame
                        if projection is not None and not sent_terminal:
                            # Even a reconnect after the final cursor must close
                            # the AG-UI lifecycle of this HTTP stream.
                            event = projection.terminal or ag_ui.RunErrorEvent(
                                message="运行事件记录不完整，请查看运行记录。",
                                code="EVENT_LOG_INCOMPLETE",
                            )
                            yield ag_ui.encode(event)
                        return
                    if len(rows) == 200:
                        continue
                    if time.monotonic() - heartbeat_at >= 10:
                        heartbeat_at = time.monotonic()
                        yield ": heartbeat\n\n"
                    # Lost wakeups, Redis outages and absent Redis recover with
                    # bounded low-frequency DB replay. Authority is independent.
                    await watch.wait(observed, timeout=2.0)

        return LeasedStreamingResponse(
            stream(),
            renew=renew_stream,
            release=release_stream,
            valid_until=valid_until,
            authorize=authorize_stream,
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "Cache-Control": "no-store",
                **(
                    {
                        "X-Run-ID": run_id,
                        "X-AG-UI-Protocol-Version": ag_ui.PROTOCOL_VERSION,
                    }
                    if projection
                    else {}
                ),
            },
        )

    @app.get(
        "/api/v1/usage/calls",
        response_model=r.CallsResponse,
        response_model_exclude_unset=True,
    )
    def calls(
        user=Depends(auth),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=300),
    ):
        rows = platform.call_records(user, limit=limit, cursor=cursor)
        return {
            "items": rows,
            "next_cursor": platform.encode_cursor(rows[-1])
            if len(rows) == limit
            else None,
        }

    @app.get("/api/v1/billing/wallet", response_model=r.WalletResponse)
    def wallet(user=Depends(auth)):
        return platform.billing.wallet(user["tenant_id"])

    @app.get("/api/v1/billing/ledger")
    def ledger(user=Depends(auth)):
        return {"items": platform.billing.ledger(user["tenant_id"])}

    @app.post("/api/v1/billing/credits")
    def credit(
        body: s.Credit,
        user=Depends(admin),
        idempotency_key: str | None = Header(default=None),
    ):
        return billing_as(user).credit(
            user["tenant_id"],
            user["id"],
            body.amount,
            body.description,
            idempotency(idempotency_key),
        )

    @app.get("/api/v1/billing/reservations")
    def reservations(user=Depends(auth)):
        rows = platform.billing.reservations(user["tenant_id"])
        if "platform.costs.read" not in user["capabilities"]:
            rows = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"provider_cost", "raw_usage"}
                }
                for row in rows
            ]
        return {"items": rows}

    @app.post("/api/v1/billing/reservations/{call_id}/resolve")
    def reconcile(
        call_id: str,
        body: s.Reconcile,
        user=Depends(admin),
        idempotency_key: str | None = Header(default=None),
    ):
        result = billing_as(user).resolve(
            user["tenant_id"],
            call_id,
            actor_id=user["id"],
            idempotency_key=idempotency(idempotency_key),
            **body.model_dump(),
        )
        from agent_platform.apps.worker.main import sync_call_receipt

        sync_call_receipt(platform, result)
        return result

    @app.post("/api/v1/billing/unblock")
    def unblock(body: s.Unblock, user=Depends(admin)):
        return billing_as(user).unblock(user["tenant_id"], user["id"], body.reason)

    @app.get("/api/v1/quotas")
    def quotas(user=Depends(admin)):
        return {"items": platform.billing.quotas(user["tenant_id"])}

    @app.put("/api/v1/quotas")
    def quota(body: s.Quota, user=Depends(admin)):
        if body.scope == "tenant" and body.subject_id != user["tenant_id"]:
            raise PlatformError(400, "invalid_subject", "租户配额必须属于当前组织。")
        if body.scope in ("user", "model"):
            with db.read() as connection:
                if body.scope == "user":
                    member = connection.scalar(
                        select(nt.memberships.c.id).where(
                            nt.memberships.c.tenant_id == user["tenant_id"],
                            nt.memberships.c.user_id == body.subject_id,
                            nt.memberships.c.status == "active",
                        )
                    )
                    if not member:
                        raise PlatformError(404, "not_found", "组织成员不存在。")
                else:
                    platform.owned(connection, t.models, user, body.subject_id)
        return billing_as(
            user, "quota.update", f"{body.scope}:{body.subject_id}"
        ).set_quota(user["tenant_id"], **body.model_dump())

    @app.get("/api/v1/audit")
    def audits(user=Depends(admin)):
        from agent_platform.modules.billing.service import billing_audit

        rows = platform.list_resources(t.audits, user)
        with db.read() as connection:
            financial = connection.execute(
                select(billing_audit).where(
                    billing_audit.c.tenant_id == user["tenant_id"]
                )
            ).mappings()
            for row in financial:
                data = dict(row)
                data["actor_email"] = (
                    connection.scalar(
                        select(t.users.c.email).where(
                            t.users.c.id == data.get("actor_id")
                        )
                    )
                    or "system"
                )
                rows.append(data)
        return {
            "items": sorted(rows, key=lambda x: x["created_at"], reverse=True)[:200]
        }

    # Reuse validated business handlers, binding tenant identity from the URL.
    for route in list(app.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/"):
            suffix = route.path.removeprefix("/api/v1")
            if suffix.startswith(
                ("/auth/", "/health", "/ready", "/platform-status", "/gateway")
            ):
                continue
            app.add_api_route(
                "/api/v2/tenants/{tenant_id}" + suffix,
                route.endpoint,
                methods=list(route.methods),
                status_code=route.status_code,
                response_model=route.response_model,
                response_model_exclude_unset=route.response_model_exclude_unset,
                response_class=route.response_class,
                responses=route.responses,
                name="tenant_" + route.name,
            )

    from agent_platform.apps.api.tenancy import register

    register(app, platform, principal, auth, idempotency)
    from agent_platform.apps.api.operations import register as register_operations

    register_operations(app, platform, principal, auth)
    from agent_platform.apps.api.support import register as register_support

    register_support(app, platform, principal, auth)

    @app.get("/api/v2/me", response_model=r.AuthResponse)
    def me_v2(request: Request, _user=Depends(principal)):
        return platform.authenticate(request.cookies.get(COOKIE))

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path.startswith("api/"):
            raise PlatformError(404, "not_found", "接口不存在。")
        directory = settings.web_directory.resolve()
        requested = (directory / path).resolve()
        if requested.is_relative_to(directory) and requested.is_file():
            return FileResponse(requested)
        if (directory / "index.html").exists():
            return FileResponse(directory / "index.html")
        return JSONResponse(
            {
                "message": "请先构建 agent_platform/apps/web，或运行前端开发服务器。",
                "api_docs": "/docs",
            }
        )

    return app
