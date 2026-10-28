"""Stream native LiteLLM Responses with explicit metering evidence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import AIMessageChunk

from agent_platform.modules.execution_policy import ReasoningEffort
from chat_models.factory import create_gateway_chat_model


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: list[dict]
    input_tokens: int
    output_tokens: int
    raw_usage: dict
    # Opaque native context stays inside this run, never in public text/events
    # or financial receipts. Reasoning models need it when returning tool results.
    response_items: list[dict] | None = None


@dataclass(frozen=True)
class GatewayEvent:
    type: str
    data: Any


class GatewayError(Exception):
    def __init__(self, code: str, message: str, *, request_sent: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_sent = request_sent


class UsageUnavailable(GatewayError):
    def __init__(self, message: str = "模型网关未返回完整用量，费用等待核实。") -> None:
        super().__init__("usage_unavailable", message)


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    if type(value) is not int or value < 0:
        raise UsageUnavailable()
    return value


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        )
    return ""


def _responses_input(messages: list[dict]) -> list[dict]:
    """Replay the graph's full history without requiring stored responses."""
    items: list[dict] = []
    for message in messages:
        role = message["role"]
        content = message.get("content")
        native = message.get("platform_response_items")
        if native is not None:
            if (
                role != "assistant"
                or not isinstance(native, list)
                or not all(isinstance(item, dict) for item in native)
            ):
                raise ValueError("Invalid native response history")
            items.extend(native)
            continue
        if role == "tool":
            call_id = message["tool_call_id"]
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(content, str)
            ):
                raise ValueError("Invalid tool output")
            items.append(
                {"type": "function_call_output", "call_id": call_id, "output": content}
            )
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError("Invalid message role")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list) or (calls and role != "assistant"):
            raise ValueError("Invalid tool calls")
        if content is None and calls:
            content = ""
        if not isinstance(content, str):
            raise TypeError("Invalid message content")
        if content or not calls:
            items.append({"role": role, "content": content})
        for call in calls:
            function = call["function"]
            call_id, name, arguments = (
                call["id"],
                function["name"],
                function["arguments"],
            )
            if not all(isinstance(value, str) and value for value in (call_id, name)):
                raise ValueError("Invalid function call")
            if not isinstance(arguments, str):
                raise TypeError("Invalid function arguments")
            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
    return items


def _responses_tools(tools: list[dict]) -> list[dict]:
    converted = []
    for tool in tools:
        if tool.get("type") != "function":
            raise ValueError("Unsupported tool type")
        function = tool.get("function", tool)
        if not isinstance(function.get("name"), str) or not function["name"]:
            raise ValueError("Invalid tool name")
        if not isinstance(function.get("parameters"), dict):
            raise TypeError("Invalid tool parameters")
        converted.append(
            {
                "type": "function",
                **{
                    key: function[key]
                    for key in ("name", "description", "parameters", "strict")
                    if key in function
                },
            }
        )
    return converted


async def _responses_events(response: httpx.Response) -> AsyncIterator[dict]:
    """Read raw SSE JSON so invalid usage types cannot be coerced into counts."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            payload = "\n".join(data)
            data.clear()
            if payload == "[DONE]":
                return
            event = json.loads(payload)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Invalid Responses event")
            yield event
    if data:
        payload = "\n".join(data)
        if payload != "[DONE]":
            event = json.loads(payload)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("Invalid Responses event")
            yield event


class Gateway:
    """Only server configuration selects the upstream endpoint and credential."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        client_factory: Callable[..., Any] = create_gateway_chat_model,
        timeout: float = 90,
        local_address: str | None = None,
        protocol: str = "responses",
        reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        self.base_url = (
            base_url if base_url is not None else os.getenv("PLATFORM_LITELLM_URL", "")
        ).strip()
        self._api_key = (
            api_key if api_key is not None else os.getenv("PLATFORM_LITELLM_KEY", "")
        ).strip()
        self._client_factory = client_factory
        self.timeout = timeout
        self.protocol = protocol
        self.reasoning_effort = reasoning_effort
        self.local_address = (
            local_address
            if local_address is not None
            else os.getenv("PLATFORM_GATEWAY_LOCAL_ADDRESS", "")
        ).strip() or None

    @property
    def configured(self) -> bool:
        try:
            parsed = urlsplit(self.base_url)
            return bool(
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and self._api_key
            )
        except ValueError:
            return False

    async def stream(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict],
        *,
        attempt_id: str | None = None,
        logical_call_id: str | None = None,
    ) -> AsyncIterator[GatewayEvent]:
        if not self.configured:
            raise GatewayError(
                "gateway_not_configured",
                "请在服务端配置 PLATFORM_LITELLM_URL 和 PLATFORM_LITELLM_KEY。",
                request_sent=False,
            )
        if not model or type(max_tokens) is not int or max_tokens < 1:
            raise GatewayError(
                "invalid_model_config", "模型或输出上限无效。", request_sent=False
            )
        if self.protocol == "responses":
            stream = self._stream_responses
        elif self.protocol == "chat_completions":
            # Explicit compatibility mode for old integrations; production uses
            # native Responses and never silently retries via a different API.
            stream = self._stream_chat
        else:
            raise GatewayError(
                "invalid_gateway_protocol", "模型网关协议无效。", request_sent=False
            )
        async with aclosing(
            stream(
                messages,
                model,
                max_tokens,
                temperature,
                tools,
                attempt_id=attempt_id,
                logical_call_id=logical_call_id,
            )
        ) as events:
            async for event in events:
                yield event

    async def _stream_responses(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict],
        *,
        attempt_id: str | None,
        logical_call_id: str | None,
    ) -> AsyncIterator[GatewayEvent]:
        transport: httpx.AsyncHTTPTransport | None = None
        http_client: httpx.AsyncClient | None = None
        request_sent = False
        terminal: dict | None = None
        gateway_response_id: str | None = None
        deltas: list[str] = []
        reasoning_deltas: list[str] = []
        try:
            body: dict[str, Any] = {
                "model": model,
                "input": _responses_input(messages),
                "max_output_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
            if tools:
                body["tools"] = _responses_tools(tools)
            if self.reasoning_effort is not None:
                body["reasoning"] = {"effort": self.reasoning_effort}
            headers = {"Authorization": "Bearer " + self._api_key}
            if attempt_id:
                headers["x-litellm-call-id"] = attempt_id
                body["user"] = "agent-platform-attempt-" + attempt_id
                body["metadata"] = {
                    "platform_attempt_id": attempt_id,
                    **(
                        {"platform_logical_call_id": logical_call_id}
                        if logical_call_id
                        else {}
                    ),
                }
            transport = httpx.AsyncHTTPTransport(
                local_address=self.local_address, verify=True, retries=0
            )
            http_client = httpx.AsyncClient(
                transport=transport, timeout=self.timeout, trust_env=False
            )
            # There is exactly one provider attempt. Any failure after dispatch
            # retains the reservation until metering can establish actual usage.
            request_sent = True
            async with http_client.stream(
                "POST",
                self.base_url.rstrip("/") + "/responses",
                headers=headers,
                json=body,
            ) as response:
                response.raise_for_status()
                async for event in _responses_events(response):
                    kind = event["type"]
                    snapshot = event.get("response")
                    if isinstance(snapshot, dict):
                        provider_id = snapshot.get("id")
                        if isinstance(provider_id, str) and provider_id:
                            if (
                                gateway_response_id is not None
                                and gateway_response_id != provider_id
                            ):
                                raise UsageUnavailable(
                                    "模型网关响应标识不一致，费用等待核实。"
                                )
                            gateway_response_id = provider_id
                    if kind in {
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    }:
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ValueError("Invalid readable reasoning delta")
                        reasoning_deltas.append(delta)
                        if delta:
                            # Only the provider's readable text is public. Native
                            # item IDs and encrypted context stay in response_items.
                            yield GatewayEvent("reasoning", {"text": delta})
                    elif kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ValueError("Invalid text delta")
                        deltas.append(delta)
                        if delta:
                            yield GatewayEvent("text", {"text": delta})
                    elif kind in {"response.failed", "error"}:
                        raise GatewayError(
                            "gateway_failed", "模型网关调用失败或流中断，费用等待核实。"
                        )
                    elif kind in {"response.completed", "response.incomplete"}:
                        if (
                            not isinstance(snapshot, dict)
                            or not isinstance(snapshot.get("id"), str)
                            or not snapshot["id"]
                        ):
                            raise UsageUnavailable()
                        if kind == "response.completed":
                            if snapshot.get("status") != "completed":
                                raise UsageUnavailable()
                        elif (
                            snapshot.get("status") != "incomplete"
                            or (snapshot.get("incomplete_details") or {}).get("reason")
                            != "max_output_tokens"
                        ):
                            raise UsageUnavailable()
                        terminal = snapshot
                        break
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception as exc:
            # Neither raw upstream error bodies nor URLs/credentials/prompts
            # may cross the worker's public error boundary.
            raise GatewayError(
                "gateway_failed",
                "模型网关调用失败或流中断，费用等待核实。",
                request_sent=request_sent,
            ) from exc
        finally:
            if http_client is not None:
                await http_client.aclose()
            elif transport is not None:
                await transport.aclose()
        if terminal is None or not isinstance(terminal.get("usage"), dict):
            raise UsageUnavailable()
        usage = terminal["usage"]
        input_tokens = _count(usage, "input_tokens")
        output_tokens = _count(usage, "output_tokens")
        if (
            "total_tokens" in usage
            and _count(usage, "total_tokens") != input_tokens + output_tokens
        ):
            raise UsageUnavailable("模型网关用量不一致，费用等待核实。")
        output = terminal.get("output")
        if not isinstance(output, list):
            raise UsageUnavailable()
        content = ""
        reasoning_content = ""
        tool_calls = []
        for item in output:
            if not isinstance(item, dict):
                raise UsageUnavailable()
            if item.get("type") == "message":
                blocks = item.get("content")
                if not isinstance(blocks, list):
                    raise UsageUnavailable()
                for block in blocks:
                    if not isinstance(block, dict):
                        raise UsageUnavailable()
                    if block.get("type") in {"output_text", "text", "refusal"}:
                        text = block.get(
                            "refusal" if block["type"] == "refusal" else "text"
                        )
                        if not isinstance(text, str):
                            raise UsageUnavailable()
                        content += text
            elif item.get("type") == "reasoning":
                blocks = item.get("summary") or item.get("content") or []
                if not isinstance(blocks, list):
                    raise UsageUnavailable()
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") in {
                        "summary_text",
                        "reasoning_text",
                        "text",
                    }:
                        if not isinstance(block.get("text"), str):
                            raise UsageUnavailable()
                        reasoning_content += block["text"]
            elif item.get("type") == "function_call":
                arguments = item.get("arguments", "")
                if terminal["status"] == "completed" and item.get("status") not in {
                    "in_progress",
                    "incomplete",
                }:
                    try:
                        arguments = json.loads(arguments)
                    except (ValueError, TypeError):
                        # Never repair truncated JSON into executable arguments.
                        pass
                else:
                    # Even valid JSON is not authority to execute an unfinished
                    # function call. Keep a string for graph validation, while
                    # still settling the terminal event's verified usage.
                    arguments = str(arguments)
                tool_calls.append(
                    {
                        "id": item.get("call_id"),
                        "name": item.get("name"),
                        "args": arguments,
                    }
                )
        if reasoning_content and not reasoning_deltas:
            yield GatewayEvent("reasoning", {"text": reasoning_content})
        if content and not deltas:
            yield GatewayEvent("text", {"text": content})
        finish_reason = (
            "length"
            if terminal["status"] == "incomplete"
            else "tool_calls"
            if tool_calls
            else "stop"
        )
        yield GatewayEvent(
            "result",
            ModelReply(
                content=content,
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_usage={
                    **usage,
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "finish_reason": finish_reason,
                    "usage_source": "litellm_gateway",
                    "protocol": "responses",
                    **(
                        {
                            "attempt_id": attempt_id,
                            "logical_call_id": logical_call_id,
                            "gateway_call_id": attempt_id,
                            "gateway_response_id": gateway_response_id,
                            "gateway_response_id_source": "provider_sse",
                        }
                        if attempt_id
                        else {}
                    ),
                },
                response_items=output,
            ),
        )

    async def _stream_chat(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict],
        *,
        attempt_id: str | None,
        logical_call_id: str | None,
    ) -> AsyncIterator[GatewayEvent]:
        aggregate: AIMessageChunk | None = None
        usage: dict | None = None
        finish_reason: str | None = None
        gateway_response_id: str | None = None
        transport: httpx.AsyncHTTPTransport | None = None
        http_client: httpx.AsyncClient | None = None
        request_sent = False
        try:
            transport_options: dict[str, Any] = {}
            if self.local_address:
                # This request owns its client; never change shared SDK defaults.
                transport = httpx.AsyncHTTPTransport(
                    local_address=self.local_address, verify=True, retries=0
                )
                http_client = httpx.AsyncClient(
                    transport=transport, timeout=self.timeout
                )
                transport_options = {
                    "http_async_client": http_client,
                    "http_socket_options": (),
                }
            client = self._client_factory(
                model=model,
                base_url=self.base_url,
                api_key=self._api_key,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=self.timeout,
                use_responses_api=False,
                **(
                    {"reasoning_effort": self.reasoning_effort}
                    if self.reasoning_effort is not None
                    else {}
                ),
                **(
                    {
                        "default_headers": {"x-litellm-call-id": attempt_id},
                        "extra_body": {
                            "user": "agent-platform-attempt-" + attempt_id,
                            "metadata": {
                                "platform_attempt_id": attempt_id,
                                "platform_logical_call_id": logical_call_id,
                                "tags": ["agent-platform-attempt:" + attempt_id],
                            },
                        },
                    }
                    if attempt_id
                    else {}
                ),
                **transport_options,
            )
            runnable = client.bind_tools(tools) if tools else client
            request_sent = True
            async for chunk in runnable.astream(messages):
                if not isinstance(chunk, AIMessageChunk):
                    raise GatewayError("invalid_gateway_response", "模型返回格式无效。")
                provider_id = chunk.response_metadata.get(
                    "platform_gateway_response_id"
                )
                if isinstance(provider_id, str) and provider_id:
                    if (
                        gateway_response_id is not None
                        and gateway_response_id != provider_id
                    ):
                        raise UsageUnavailable("模型网关响应标识不一致，费用等待核实。")
                    gateway_response_id = provider_id
                raw_usage = chunk.response_metadata.get("platform_raw_usage")
                if isinstance(raw_usage, dict):
                    # Streaming usage is a cumulative snapshot, never a delta.
                    usage = dict(raw_usage)
                if reason := chunk.response_metadata.get("finish_reason"):
                    finish_reason = reason
                aggregate = chunk if aggregate is None else aggregate + chunk
                if text := _text(chunk.content):
                    yield GatewayEvent("text", {"text": text})
        except asyncio.CancelledError:
            raise
        except GatewayError:
            raise
        except Exception as exc:
            # Provider exceptions can include credentials, URLs or prompt text.
            # Only a stable safe message crosses the platform boundary.
            raise GatewayError(
                "gateway_failed",
                "模型网关调用失败或流中断，费用等待核实。",
                request_sent=request_sent,
            ) from exc
        finally:
            if http_client is not None:
                await http_client.aclose()
            elif transport is not None:
                # Client construction can fail after allocating its transport.
                await transport.aclose()
        if usage is None or aggregate is None or finish_reason is None:
            raise UsageUnavailable()
        input_tokens = _count(usage, "prompt_tokens")
        output_tokens = _count(usage, "completion_tokens")
        if (
            "total_tokens" in usage
            and _count(usage, "total_tokens") != input_tokens + output_tokens
        ):
            raise UsageUnavailable("模型网关用量不一致，费用等待核实。")
        tool_calls = []
        for call in aggregate.tool_call_chunks:
            arguments = call.get("args", "")
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                # LangChain's partial-JSON parser can accept truncated JSON.
                # Preserve invalid text for runtime rejection after settlement.
                pass
            tool_calls.append(
                {"id": call.get("id"), "name": call.get("name"), "args": arguments}
            )
        yield GatewayEvent(
            "result",
            ModelReply(
                content=_text(aggregate.content),
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_usage={
                    **usage,
                    "finish_reason": finish_reason,
                    "usage_source": "litellm_gateway",
                    **(
                        {
                            "attempt_id": attempt_id,
                            "logical_call_id": logical_call_id,
                            "gateway_call_id": attempt_id,
                            "gateway_response_id": gateway_response_id,
                            "gateway_response_id_source": "provider_sse"
                            if gateway_response_id
                            else None,
                        }
                        if attempt_id
                        else {}
                    ),
                },
            ),
        )
