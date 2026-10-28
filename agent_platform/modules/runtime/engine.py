"""A bounded model → tools → model LangGraph with platform-owned I/O."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent_platform.modules.execution_policy import read_policy

from .contracts import validate_run_snapshot_version
from .gateway import ModelReply
from .generative_ui import UI_TOOL, GeneratedUiArguments
from .tools import execute_tool, tool_definitions

Invoke = Callable[[list[dict], list[dict]], Awaitable[ModelReply]]
Emit = Callable[[str, dict], Awaitable[None]]
Cancelled = Callable[[], Awaitable[bool]]


class RuntimeFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunCancelled(RuntimeFailure):
    def __init__(self) -> None:
        super().__init__("run_cancelled", "运行已取消。")


class StepLimitExceeded(RuntimeFailure):
    def __init__(self) -> None:
        super().__init__("step_limit_exceeded", "已达到 Agent 最大模型调用步数。")


class AgentState(TypedDict):
    # Nodes run sequentially. A node replaces the complete message list rather
    # than relying on framework message conversion or parallel reducers.
    messages: list[dict]
    step: int
    pending_tools: list[dict]
    final: str
    output_recoveries: int
    retry_output: bool


async def _cancellable_invoke(
    invoke: Invoke, messages: list[dict], tools: list[dict], cancelled: Cancelled
) -> ModelReply:
    task = asyncio.create_task(invoke(messages, tools))
    try:
        while not task.done():
            if await cancelled():
                raise RunCancelled()
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


async def execute_agent(
    spec: dict,
    messages: list[dict],
    invoke: Invoke,
    emit: Emit,
    cancelled: Cancelled,
) -> str:
    """Execute at most ``max_steps`` model calls using the native v3 stream.

    The invoke callback owns permission checks, reservation, text deltas and
    settlement for every model call. Cancellation stops that callback, which
    must retain reservations whenever an in-flight request has unknown usage.
    """
    try:
        validate_run_snapshot_version(spec)
    except ValueError:
        raise RuntimeFailure(
            "unsupported_run_snapshot", "运行快照版本不受当前执行器支持。"
        ) from None
    try:
        policy = read_policy(spec)
    except ValueError:
        raise RuntimeFailure("invalid_agent_config", "Agent 执行策略无效。") from None
    max_steps = spec.get("max_steps", 8)
    if type(max_steps) is not int or not 1 <= max_steps <= 50:
        raise RuntimeFailure("invalid_agent_config", "最大步数须为 1 至 50。")
    allowed = spec.get("tools", [])
    if not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed):
        raise RuntimeFailure("invalid_agent_config", "Agent 工具配置无效。")
    try:
        definitions = tool_definitions(allowed)
    except ValueError as exc:
        raise RuntimeFailure("invalid_agent_config", str(exc)) from exc
    history: list[dict] = []
    if prompt := spec.get("system_prompt"):
        history.append({"role": "system", "content": prompt})
    for message in messages:
        if message.get("role") not in {"user", "assistant"} or not isinstance(
            message.get("content"), str
        ):
            raise RuntimeFailure("invalid_history", "会话历史格式无效。")
        history.append({"role": message["role"], "content": message["content"]})

    async def guard() -> None:
        if await cancelled():
            raise RunCancelled()

    async def model_node(state: AgentState) -> dict[str, Any]:
        await guard()
        if state["step"] >= max_steps:
            raise StepLimitExceeded()
        step = state["step"] + 1
        await emit("step.started", {"step": step})
        reply = await _cancellable_invoke(
            invoke, state["messages"], definitions, cancelled
        )
        await guard()
        if reply.raw_usage.get("finish_reason") == "length":
            recovery = state["output_recoveries"]
            retry = recovery < policy.max_output_recoveries and step < max_steps
            await emit(
                "step.finished",
                {
                    "step": step,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                },
            )
            await emit(
                "model.output_limit",
                {
                    "step": step,
                    "recovery": recovery + 1 if retry else recovery,
                    "max_recoveries": policy.max_output_recoveries,
                    "action": "recover" if retry else "stop",
                },
            )
            if not retry:
                raise RuntimeFailure(
                    "output_limit_exceeded",
                    "模型达到单次输出上限，未完成结果；已停止或达到配置的恢复次数。已确认用量正常结算。",
                )
            # Never execute, concatenate or replay an unfinished function call.
            # Request a new complete result using only confirmed prior context.
            additions = []
            if reply.content:
                additions.append({"role": "assistant", "content": reply.content})
            additions.append(
                {
                    "role": "user",
                    "content": "运行控制提示：上一轮达到输出上限，结果未完成。请根据原始需求给出精简、完整的可用结果；需要工具时重新提交完整参数，不续接截断的 JSON 或脚本。优先完成最小版本，避免重复长篇规划。",
                }
            )
            return {
                "messages": [*state["messages"], *additions],
                "step": step,
                "pending_tools": [],
                "final": "",
                "output_recoveries": recovery + 1,
                "retry_output": True,
            }
        calls = reply.tool_calls
        if len(calls) > 8:
            raise RuntimeFailure("tool_limit_exceeded", "每步最多允许 8 次工具调用。")
        ids: set[str] = set()
        for call in calls:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not call["id"]
                or call["id"] in ids
                or not isinstance(call.get("name"), str)
            ):
                raise RuntimeFailure(
                    "invalid_tool_call", "模型返回的工具调用格式无效。"
                )
            ids.add(call["id"])
        message: dict = {"role": "assistant", "content": reply.content}
        if calls:
            if reply.response_items:
                # Replay native reasoning/function items in their original
                # order for store=false. They are private graph state, never
                # emitted as user-visible text or stored in usage receipts.
                message["platform_response_items"] = reply.response_items
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": (
                            json.dumps(call.get("args", {}), ensure_ascii=False)
                            if isinstance(call.get("args", {}), dict)
                            else str(call.get("args", ""))
                        ),
                    },
                }
                for call in calls
            ]
        await emit(
            "step.finished",
            {
                "step": step,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
            },
        )
        return {
            "messages": [*state["messages"], message],
            "step": step,
            "pending_tools": calls,
            "final": reply.content if not calls else "",
            "retry_output": False,
        }

    async def tools_node(state: AgentState) -> dict[str, Any]:
        if state["step"] >= max_steps:
            raise StepLimitExceeded()
        additions: list[dict] = []
        for call in state["pending_tools"]:
            await guard()
            await emit(
                "tool.started",
                {
                    "id": call["id"],
                    "name": call["name"],
                    "args": call["args"] if isinstance(call.get("args"), dict) else {},
                },
            )
            try:
                if not isinstance(call.get("args"), dict):
                    raise TypeError("工具参数必须为 JSON 对象。")
                output = execute_tool(call["name"], call["args"], allowed)
                data = {"ok": True, **output}
            except (ValueError, TypeError) as exc:
                # Validation errors do not execute a tool or escape the allowlist.
                # Do not echo arbitrary model-supplied arguments into the error.
                message = str(exc) if type(exc) is ValueError else "工具参数校验失败。"
                data = {"ok": False, "error": message}
            await emit(
                "tool.finished", {"id": call["id"], "name": call["name"], **data}
            )
            if call["name"] == UI_TOOL and data["ok"]:
                await guard()
                await emit(
                    "artifact.created",
                    {
                        "id": f"{state['step']}:{call['id']}",
                        "content": GeneratedUiArguments.model_validate(
                            call["args"]
                        ).model_dump(),
                    },
                )
            additions.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(data, ensure_ascii=False),
                }
            )
        return {"messages": [*state["messages"], *additions], "pending_tools": []}

    builder = StateGraph(AgentState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges(
        "model",
        lambda state: (
            "model"
            if state["retry_output"]
            else "tools"
            if state["pending_tools"]
            else END
        ),
    )
    builder.add_edge("tools", "model")
    graph = builder.compile()
    initial: AgentState = {
        "messages": history,
        "step": 0,
        "pending_tools": [],
        "final": "",
        "output_recoveries": 0,
        "retry_output": False,
    }
    async with await graph.astream_events(
        initial, config={"recursion_limit": max_steps * 2 + 2}, version="v3"
    ) as stream:
        # Consume real graph state events; invoke and tool callbacks emit the
        # durable platform protocol without exposing LangGraph's internal schema.
        async for _state in stream.values:
            pass
        output = await stream.output()
    if output is None:
        raise RuntimeFailure("empty_graph_result", "Agent 未产生最终状态。")
    await guard()
    return output["final"]
