"""Shared, metered model invocation for single agents and team executions."""

import asyncio
import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import insert, select, update

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.execution_policy import effective_limits, read_policy
from agent_platform.modules.run_records import now, uid

from .context import ExecutionContext
from .engine import RunCancelled
from .gateway import Gateway, GatewayError, ModelReply

if TYPE_CHECKING:
    from agent_platform.modules.platform import Platform
    from agent_platform.modules.service_contexts import WorkerServices


def sync_call_receipt(platform: "Platform | WorkerServices", receipt: dict) -> None:
    """Rebuild the display projection from the immutable billing evidence."""
    status = {
        "settled": "confirmed",
        "released": "released",
        "written_off": "written_off",
        "unresolved": "unresolved",
        "reserved": "running",
    }[receipt["status"]]
    raw = receipt.get("raw_usage")
    if isinstance(raw, str):
        raw = json.loads(raw)
    with platform.db.transaction(f"tenant:{receipt['tenant_id']}") as connection:
        connection.execute(
            update(t.calls)
            .where(
                t.calls.c.id == receipt["call_id"],
                t.calls.c.tenant_id == receipt["tenant_id"],
            )
            .values(
                status=status,
                input_tokens=receipt.get("input_tokens") or 0,
                output_tokens=receipt.get("output_tokens") or 0,
                cost=str(receipt["cost"]),
                provider_cost=receipt.get("provider_cost"),
                raw_usage=raw,
                error=receipt.get("reason", ""),
                finished_at=receipt.get("updated_at"),
            )
        )


class InvocationService:
    def __init__(self, controller):
        self.controller = controller
        self.platform = controller.platform
        self.db = controller.db
        self.settings = controller.settings
        self.gateway = controller.gateway
        self.max_run_cost = Decimal(self.settings.max_run_cost)

    def _assert_lease(self, connection, run):
        return self.controller._assert_lease(connection, run)

    def _authorize_identity(self, connection, run):
        identity = self.controller._authorize_identity(connection, run)
        for agent_id in {
            run["spec"].get("id"),
            run.get("root_spec", run["spec"]).get("id"),
        } - {None}:
            available = connection.scalar(
                select(t.agents.c.available).where(
                    t.agents.c.tenant_id == run["tenant_id"],
                    t.agents.c.id == agent_id,
                )
            )
            if not available:
                raise PlatformError(403, "agent_unavailable", "Agent 已停用或不可用。")
        return identity

    def is_cancelled(self, run):
        return self.controller.is_cancelled(run)

    def event(self, run, kind, data):
        attribution = {
            key: run[key] for key in ("task_id", "execution_id") if run.get(key)
        }
        self.controller.event(run, kind, {**data, **attribution})

    def authorize_call(
        self, connection, run: dict, input_tokens: int, amount: Decimal
    ) -> None:
        current = self._assert_lease(connection, run)
        self._authorize_identity(connection, run)
        model = (
            connection.execute(
                select(t.models).where(
                    t.models.c.id == run["spec"]["model_id"],
                    t.models.c.tenant_id == run["tenant_id"],
                )
            )
            .mappings()
            .first()
        )
        if current["cancel_requested"]:
            raise PlatformError(403, "access_revoked", "账号已停用或运行已取消。")
        if (
            not model
            or not model["active"]
            or model["alias"] != run["spec"]["model"]["alias"]
        ):
            raise PlatformError(
                403, "model_disabled", "模型已停用或其部署发生变化，请重新运行。"
            )
        self._validate_route(connection, run)
        max_output = run["spec"]["max_tokens"]
        if (
            max_output > model["max_output_tokens"]
            or input_tokens + max_output > model["context_window"]
        ):
            raise PlatformError(
                400,
                "context_limit",
                "完整会话及工具定义超过模型上下文或输出限制，请新建会话。",
            )
        existing = list(
            connection.execute(
                select(reservations_table).where(
                    reservations_table.c.tenant_id == run["tenant_id"],
                    reservations_table.c.run_id == run["id"],
                )
            ).mappings()
        )
        committed = sum(
            (
                Decimal(str(r["cost"]))
                + (
                    Decimal(str(r["amount"]))
                    if r["status"] in ("reserved", "unresolved")
                    else Decimal(0)
                )
                for r in existing
            ),
            Decimal(0),
        )
        policy = read_policy(run.get("root_spec", run["spec"]))
        limits = run.get("root_spec", run["spec"]).get(
            "execution_limits", effective_limits(policy, self.settings)
        )
        max_cost = min(self.max_run_cost, Decimal(limits["max_cost"]))
        if committed + amount > max_cost:
            raise PlatformError(
                402,
                "run_budget_exceeded",
                f"本次运行达到 {max_cost} USD 的费用上限。",
            )
        if policy.max_total_tokens is not None:
            used_tokens = sum(
                (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                if r["status"] == "settled"
                else r["estimated_input_tokens"] + r["max_output_tokens"]
                if r["status"] in ("reserved", "unresolved")
                else 0
                for r in existing
            )
            if used_tokens + input_tokens + max_output > policy.max_total_tokens:
                raise PlatformError(
                    400,
                    "run_token_budget_exceeded",
                    "本次运行剩余 Token 预算不足以容纳下一次调用的输入估算和输出预留。",
                )

    def _validate_route(self, connection, run: dict) -> None:
        if self.gateway is not None and not run["spec"].get("deployment_id"):
            return  # Offline fixtures have no external deployment or credentials.
        row = (
            connection.execute(
                select(
                    gt.model_bindings.c.enabled,
                    gt.deployments.c.id,
                    gt.deployments.c.internal_route,
                    gt.deployments.c.status,
                    gt.deployments.c.config_version,
                    gt.deployments.c.owner_scope,
                    gt.deployments.c.owner_tenant_id,
                )
                .join(
                    gt.deployments,
                    gt.model_bindings.c.deployment_id == gt.deployments.c.id,
                )
                .where(
                    gt.model_bindings.c.tenant_id == run["tenant_id"],
                    gt.model_bindings.c.model_id == run["spec"]["model_id"],
                )
            )
            .mappings()
            .first()
        )
        if (
            not row
            or not row["enabled"]
            or row["status"] != "active"
            or (
                row["owner_scope"] == "tenant"
                and row["owner_tenant_id"] != run["tenant_id"]
            )
            or row["id"] != run["spec"].get("deployment_id")
            or (row["internal_route"] != run["spec"].get("route"))
            or (
                run["spec"].get("config_version") is not None
                and row["config_version"] != run["spec"]["config_version"]
            )
        ):
            raise PlatformError(
                403, "model_grant_revoked", "模型授权或部署配置已变化，请重新发起运行。"
            )

    def _resolve_inference(self, run: dict) -> tuple[object, dict]:
        with self.db.tenant_scope(run["tenant_id"]):
            if self.gateway is not None:
                return self.gateway, {
                    "deployment_id": run["spec"].get("deployment_id"),
                    "route": run["spec"].get("route", run["spec"]["model"]["alias"]),
                    "credential_version_id": None,
                    "price_version_id": run["spec"].get("price_version_id"),
                    "config_version": run["spec"].get("config_version", 1),
                }
            routing = self.platform.gateway.resolve(
                run["tenant_id"],
                {
                    **run["spec"]["model"],
                    "deployment_id": run["spec"].get("deployment_id"),
                },
            )
            if routing["deployment_id"] != run["spec"].get("deployment_id") or (
                routing["route"] != run["spec"].get("route")
            ):
                raise PlatformError(
                    403, "deployment_changed", "模型部署已变化，请重新发起运行。"
                )
            gateway = Gateway(
                routing["base_url"],
                routing["api_key"],
                timeout=self.settings.model_timeout,
                local_address=self.settings.gateway_local_address,
                reasoning_effort=read_policy(run["spec"]).reasoning_effort,
            )
            # Credentials remain solely in the transient inference adapter.
            return gateway, {
                key: routing.get(key)
                for key in (
                    "deployment_id",
                    "route",
                    "credential_version_id",
                    "price_version_id",
                    "config_version",
                )
            }

    def _record_attempt(
        self, run: dict, call_id: str, routing: dict, attempt_id: str | None = None
    ) -> None:
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            current = self._assert_lease(connection, run)
            identity = self._authorize_identity(connection, run)
            if current["cancel_requested"]:
                raise RunCancelled()
            self._validate_route(connection, run)
            if routing["credential_version_id"]:
                current_routing = self.platform.gateway.resolve(
                    run["tenant_id"],
                    {
                        **run["spec"]["model"],
                        "deployment_id": run["spec"].get("deployment_id"),
                    },
                    connection=connection,
                )
                binding = (
                    connection.execute(
                        select(gt.bindings).where(
                            gt.bindings.c.tenant_id == run["tenant_id"],
                            gt.bindings.c.active_credential_version_id
                            == routing["credential_version_id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                credential = (
                    connection.execute(
                        select(gt.credentials).where(
                            gt.credentials.c.tenant_id == run["tenant_id"],
                            gt.credentials.c.id == routing["credential_version_id"],
                            gt.credentials.c.status == "active",
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not binding
                    or not credential
                    or current_routing["route"] != routing["route"]
                    or current_routing["credential_version_id"]
                    != routing["credential_version_id"]
                ):
                    raise PlatformError(
                        403,
                        "gateway_credential_changed",
                        "调用凭据已变化，请重新发起运行。",
                    )
            if routing["deployment_id"] is None and self.gateway is not None:
                return
            connection.execute(
                insert(rt.call_attempts).values(
                    id=attempt_id or call_id,
                    tenant_id=run["tenant_id"],
                    run_id=run["id"],
                    logical_call_id=call_id,
                    membership_id=identity["membership_id"],
                    deployment_id=routing["deployment_id"],
                    credential_version_id=routing["credential_version_id"],
                    price_version_id=run["spec"].get("price_version_id")
                    or routing["price_version_id"],
                    config_version=run["spec"].get(
                        "config_version", routing["config_version"]
                    ),
                    route=routing["route"],
                    created_at=now(),
                )
            )

            if self.gateway is None:
                from agent_platform.modules.metering import prepare_reconciliation

                prepare_reconciliation(
                    connection, run["tenant_id"], call_id, attempt_id or call_id
                )

    def register_call(self, run: dict, call_id: str) -> None:
        model = run["spec"]["model"]
        with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
            self._assert_lease(connection, run)
            user = (
                connection.execute(
                    select(t.users).where(t.users.c.id == run["user_id"])
                )
                .mappings()
                .one()
            )
            connection.execute(
                insert(t.calls).values(
                    id=call_id,
                    tenant_id=run["tenant_id"],
                    user_id=run["user_id"],
                    run_id=run["id"],
                    model_id=model["id"],
                    model=model["alias"],
                    agent_name=run["agent_name"],
                    task_id=run.get("task_id"),
                    execution_id=run.get("execution_id"),
                    user_email=user["email"],
                    status="admitting",
                    input_tokens=0,
                    output_tokens=0,
                    cost="0",
                    provider_cost=None,
                    price_version=model["price_version"],
                    error="",
                    created_at=now(),
                )
            )

    async def invoke(
        self, context: ExecutionContext, messages: list[dict], tools: list[dict]
    ) -> ModelReply:
        run = context.invocation_run()
        with self.db.tenant_scope(run["tenant_id"]):
            return await self._invoke(run, messages, tools)

    async def _invoke(
        self, run: dict, messages: list[dict], tools: list[dict]
    ) -> ModelReply:
        call_id = uid()
        attempt_id = uid()
        model = run["spec"]["model"]
        max_tokens = run["spec"]["max_tokens"]
        # Conservative UTF-8 byte bound, plus protocol framing. No cache discount
        # is assumed. Gateway usage determines platform settlement, and may be
        # estimated by LiteLLM when the provider omits its own usage.
        estimate = (
            len(
                json.dumps(
                    {"messages": messages, "tools": tools}, ensure_ascii=False
                ).encode()
            )
            + 256
            + 32 * len(messages)
        )
        amount = (
            Decimal(model["input_price"]) * estimate
            + Decimal(model["output_price"]) * max_tokens
        ) / Decimal(1000000)
        await asyncio.to_thread(self.register_call, run, call_id)
        billing = BillingService(
            self.db,
            self.settings.redis_url,
            authorize=lambda connection: self.authorize_call(
                connection, run, estimate, amount
            ),
        )
        reserved = False
        sent = False
        receipt = None
        reply = None
        try:
            # Shield transaction completion so cancellation cannot orphan a newly
            # committed reservation whose result the coroutine never received.
            reservation_task = asyncio.create_task(
                asyncio.to_thread(
                    billing.reserve,
                    run["tenant_id"],
                    run["user_id"],
                    model["id"],
                    run["id"],
                    call_id,
                    estimate,
                    max_tokens,
                    model["input_price"],
                    model["output_price"],
                    deployment_id=run["spec"].get("deployment_id"),
                    require_cost_policy=self.settings.mode == "saas",
                )
            )
            try:
                receipt = await asyncio.shield(reservation_task)
                reserved = True
            except asyncio.CancelledError:
                receipt = await reservation_task
                reserved = True
                raise
            if await asyncio.to_thread(self.is_cancelled, run):
                raise RunCancelled()
            inference, routing = await asyncio.to_thread(self._resolve_inference, run)
            await asyncio.to_thread(
                self._record_attempt, run, call_id, routing, attempt_id
            )
            await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            await asyncio.to_thread(
                self.event,
                run,
                "model.started",
                {"call_id": call_id, "model": model["alias"]},
            )
            sent = True
            buffer = ""
            flushed_at = time.monotonic()
            reasoning_buffer = ""
            reasoning_open = False
            reasoning_flushed_at = time.monotonic()

            async def flush_reasoning(*, complete: bool = False) -> None:
                nonlocal reasoning_buffer, reasoning_open, reasoning_flushed_at
                if reasoning_buffer:
                    await asyncio.to_thread(
                        self.event,
                        run,
                        "reasoning.delta",
                        {"text": reasoning_buffer, "call_id": call_id},
                    )
                    reasoning_buffer = ""
                    reasoning_flushed_at = time.monotonic()
                if complete and reasoning_open:
                    await asyncio.to_thread(
                        self.event, run, "reasoning.finished", {"call_id": call_id}
                    )
                    reasoning_open = False

            async for event in inference.stream(
                messages,
                routing["route"],
                max_tokens,
                run["spec"]["temperature"],
                tools,
                **(
                    {"attempt_id": attempt_id, "logical_call_id": call_id}
                    if self.gateway is None
                    else {}
                ),
            ):
                if event.type == "reasoning":
                    if not reasoning_open:
                        await asyncio.to_thread(
                            self.event, run, "reasoning.started", {"call_id": call_id}
                        )
                        reasoning_open = True
                    reasoning_buffer += event.data["text"]
                    if (
                        len(reasoning_buffer) >= 128
                        or time.monotonic() - reasoning_flushed_at > 0.1
                    ):
                        await flush_reasoning()
                elif event.type == "text":
                    await flush_reasoning(complete=True)
                    buffer += event.data["text"]
                    if len(buffer) >= 128 or time.monotonic() - flushed_at > 0.1:
                        await asyncio.to_thread(
                            self.event,
                            run,
                            "message.delta",
                            {"text": buffer, "call_id": call_id},
                        )
                        buffer, flushed_at = "", time.monotonic()
                elif event.type == "result":
                    reply = event.data
            await flush_reasoning(complete=True)
            if reply is None:
                raise GatewayError(
                    "usage_unavailable", "模型未返回完整用量，费用等待核实。"
                )
            reply = replace(
                reply,
                raw_usage={
                    **reply.raw_usage,
                    "attempt_id": attempt_id,
                    "logical_call_id": call_id,
                },
            )
            # Settlement does not depend on current user permissions: a revoked
            # account must still pay for usage already produced upstream.
            settlement = asyncio.create_task(
                asyncio.to_thread(
                    self.platform.billing.settle,
                    run["tenant_id"],
                    call_id,
                    reply.input_tokens,
                    reply.output_tokens,
                    None,
                    reply.raw_usage,
                )
            )
            try:
                receipt = await asyncio.shield(settlement)
            except asyncio.CancelledError:
                receipt = await settlement
                await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
                raise
            await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            if buffer:
                await asyncio.to_thread(
                    self.event,
                    run,
                    "message.delta",
                    {"text": buffer, "call_id": call_id},
                )
            await asyncio.to_thread(
                self.event,
                run,
                "usage.updated",
                {
                    "call_id": call_id,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "cost": receipt["cost"],
                    "status": "confirmed",
                },
            )
            return reply
        except BaseException as exc:
            if isinstance(exc, GatewayError) and not exc.request_sent:
                sent = False
            if reserved:
                if reply is not None:
                    receipt = await asyncio.to_thread(
                        self.platform.billing.settle,
                        run["tenant_id"],
                        call_id,
                        reply.input_tokens,
                        reply.output_tokens,
                        None,
                        reply.raw_usage,
                    )
                else:
                    operation = (
                        self.platform.billing.unresolved
                        if sent
                        else self.platform.billing.release
                    )
                    reason = (
                        "上游可能已执行，等待用量证据。"
                        if sent
                        else "请求在发送前终止。"
                    )
                    receipt = await asyncio.to_thread(
                        operation, run["tenant_id"], call_id, reason
                    )
                await asyncio.to_thread(sync_call_receipt, self.platform, receipt)
            else:
                with self.db.transaction(f"tenant:{run['tenant_id']}") as connection:
                    connection.execute(
                        update(t.calls)
                        .where(t.calls.c.id == call_id)
                        .values(
                            status="rejected",
                            error=getattr(exc, "message", "请求准入失败。"),
                            finished_at=now(),
                        )
                    )
            raise
