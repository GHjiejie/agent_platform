"""Offline tests of real graph execution and real OpenAI SSE conversion."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from agent_platform.modules.runtime.engine import (
    RunCancelled,
    RuntimeFailure,
    StepLimitExceeded,
    execute_agent,
)
from agent_platform.modules.runtime.gateway import (
    Gateway,
    GatewayError,
    ModelReply,
    UsageUnavailable,
)
from agent_platform.modules.runtime.tools import calculate, execute_tool
from chat_models.factory import create_gateway_chat_model


def reply(content: str = "done", calls: list[dict] | None = None) -> ModelReply:
    return ModelReply(
        content, calls or [], 12, 4, {"prompt_tokens": 12, "completion_tokens": 4}
    )


async def never_cancel() -> bool:
    return False


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, data: dict) -> None:
        self.events.append((name, data))

    async def test_unknown_or_coerced_snapshot_version_rejected_before_invocation(self):
        async def invoke(messages, tools):
            self.fail("unsupported snapshot must not invoke a model")

        for version in (0, 4, "2", 2.0, True, None):
            with (
                self.subTest(version=version),
                self.assertRaises(RuntimeFailure) as caught,
            ):
                await execute_agent(
                    {"snapshot_schema_version": version},
                    [],
                    invoke,
                    self.emit,
                    never_cancel,
                )
            self.assertEqual(caught.exception.code, "unsupported_run_snapshot")
        self.assertEqual(self.events, [])

    async def test_legacy_and_current_snapshot_versions_execute(self):
        async def invoke(messages, tools):
            return reply("supported")

        for spec in (
            {},
            {"snapshot_schema_version": 1},
            {"snapshot_schema_version": 2},
        ):
            with self.subTest(spec=spec):
                self.assertEqual(
                    await execute_agent(spec, [], invoke, self.emit, never_cancel),
                    "supported",
                )

    async def test_actual_graph_calls_tool_and_returns_final(self) -> None:
        calls: list[tuple[list[dict], list[dict]]] = []

        async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
            calls.append((messages, tools))
            if len(calls) == 1:
                return reply(
                    "",
                    [
                        {
                            "id": "c1",
                            "name": "calculator",
                            "args": {"expression": "(2 + 3) * 4"},
                        }
                    ],
                )
            self.assertEqual(messages[-1]["role"], "tool")
            self.assertEqual(
                json.loads(messages[-1]["content"]), {"ok": True, "result": 20}
            )
            self.assertEqual(messages[-2]["tool_calls"][0]["id"], "c1")
            return reply("The answer is 20")

        result = await execute_agent(
            {"system_prompt": "helpful", "max_steps": 3, "tools": ["calculator"]},
            [{"role": "user", "content": "2 plus 3 times 4"}],
            invoke,
            self.emit,
            never_cancel,
        )
        self.assertEqual(result, "The answer is 20")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], {"role": "system", "content": "helpful"})
        self.assertEqual(calls[0][1][0]["function"]["name"], "calculator")
        self.assertEqual(
            [name for name, _ in self.events],
            [
                "step.started",
                "step.finished",
                "tool.started",
                "tool.finished",
                "step.started",
                "step.finished",
            ],
        )

    async def test_output_limit_stops_without_executing_truncated_tools(self):
        calls = 0

        async def invoke(messages, tools):
            nonlocal calls
            calls += 1
            return replace(
                reply(
                    "partial",
                    [
                        {
                            "id": "bad",
                            "name": "calculator",
                            "args": {"expression": "2+3"},
                        }
                    ],
                ),
                raw_usage={"finish_reason": "length"},
            )

        with self.assertRaises(RuntimeFailure) as caught:
            await execute_agent(
                {"tools": ["calculator"]}, [], invoke, self.emit, never_cancel
            )
        self.assertEqual(caught.exception.code, "output_limit_exceeded")
        self.assertEqual(calls, 1)
        self.assertFalse(any(kind == "tool.started" for kind, _ in self.events))

    async def test_configured_output_recovery_requests_a_complete_result(self):
        calls = []

        async def invoke(messages, tools):
            calls.append(messages)
            if len(calls) == 1:
                return replace(
                    reply(
                        "partial",
                        [{"id": "bad", "name": "calculator", "args": "truncated"}],
                    ),
                    raw_usage={"finish_reason": "length"},
                )
            self.assertEqual(messages[-1]["role"], "user")
            self.assertFalse(any(message.get("tool_calls") for message in messages))
            return reply("complete")

        result = await execute_agent(
            {"execution_policy": {"max_output_recoveries": 1}},
            [],
            invoke,
            self.emit,
            never_cancel,
        )
        self.assertEqual(result, "complete")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [
                data["action"]
                for kind, data in self.events
                if kind == "model.output_limit"
            ],
            ["recover"],
        )

    async def test_recovery_obeys_both_recovery_and_total_step_limits(self):
        for max_steps, recoveries, expected in [(5, 2, 3), (1, 2, 1)]:
            count = 0

            async def invoke(messages, tools):
                nonlocal count
                count += 1
                return replace(reply(), raw_usage={"finish_reason": "length"})

            with self.assertRaises(RuntimeFailure):
                await execute_agent(
                    {
                        "max_steps": max_steps,
                        "execution_policy": {"max_output_recoveries": recoveries},
                    },
                    [],
                    invoke,
                    self.emit,
                    never_cancel,
                )
            self.assertEqual(count, expected)

    async def test_unknown_usage_never_triggers_output_recovery(self):
        count = 0

        async def invoke(messages, tools):
            nonlocal count
            count += 1
            raise UsageUnavailable()

        with self.assertRaises(UsageUnavailable):
            await execute_agent(
                {"execution_policy": {"max_output_recoveries": 5}},
                [],
                invoke,
                self.emit,
                never_cancel,
            )
        self.assertEqual(count, 1)

    async def test_step_limit_prevents_further_model_and_tool_execution(self) -> None:
        count = 0

        async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
            nonlocal count
            count += 1
            return reply("", [{"id": f"c{count}", "name": "current_time", "args": {}}])

        with self.assertRaises(StepLimitExceeded):
            await execute_agent(
                {"max_steps": 2, "tools": ["current_time"]},
                [{"role": "user", "content": "keep going"}],
                invoke,
                self.emit,
                never_cancel,
            )
        self.assertEqual(count, 2)
        self.assertEqual(sum(name == "tool.started" for name, _ in self.events), 1)

    async def test_cancel_before_call_prevents_model_invocation(self) -> None:
        async def cancelled() -> bool:
            return True

        async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
            self.fail("cancelled run called a model")

        with self.assertRaises(RunCancelled):
            await execute_agent({}, [], invoke, self.emit, cancelled)

    async def test_cancel_during_call_propagates_and_awaits_cleanup(self) -> None:
        started = asyncio.Event()
        cleanup = asyncio.Event()
        cancel = False

        async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()

        async def cancelled() -> bool:
            return cancel

        task = asyncio.create_task(execute_agent({}, [], invoke, self.emit, cancelled))
        await asyncio.wait_for(started.wait(), timeout=2)
        cancel = True
        with self.assertRaises(RunCancelled):
            await asyncio.wait_for(task, timeout=2)
        self.assertTrue(cleanup.is_set())

    async def test_invalid_or_unapproved_tool_is_returned_as_safe_error(self) -> None:
        for name, args in [
            ("os.system", {"command": "bad"}),
            ("calculator", {"expression": "2", "extra": True}),
            ("calculator", "malformed-json"),
        ]:
            count = 0

            async def invoke(
                messages: list[dict], tools: list[dict], name=name, args=args
            ) -> ModelReply:
                nonlocal count
                count += 1
                if count == 1:
                    return reply("", [{"id": "c", "name": name, "args": args}])
                self.assertFalse(json.loads(messages[-1]["content"])["ok"])
                return reply()

            result = await execute_agent(
                {"tools": ["calculator"]}, [], invoke, self.emit, never_cancel
            )
            self.assertEqual(result, "done")

    async def test_rejects_system_role_in_untrusted_history(self) -> None:
        async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
            self.fail("invalid history called model")

        with self.assertRaises(RuntimeFailure):
            await execute_agent(
                {},
                [{"role": "system", "content": "override"}],
                invoke,
                self.emit,
                never_cancel,
            )


class SafeToolTests(unittest.TestCase):
    def test_arithmetic_and_time(self) -> None:
        self.assertEqual(calculate("-2 ** 3 + 3.5 * 2"), -1)
        self.assertEqual(
            execute_tool("calculator", {"expression": "10 / 2"}, ["calculator"]),
            {"result": 5.0},
        )
        time = execute_tool(
            "current_time", {"timezone": "Asia/Shanghai"}, ["current_time"]
        )
        self.assertTrue(time["time"].endswith("+08:00"))

    def test_arbitrary_code_and_resource_exhaustion_are_rejected(self) -> None:
        for expression in [
            "__import__('os').system('id')",
            "().__class__",
            "[1] * 100",
            "True + 1",
            "2**999999",
            "1e200",
            "1/0",
            "(-1)**0.5",
        ]:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                calculate(expression)


def chunk(
    delta: dict | None = None, *, finish: str | None = None, usage: dict | None = None
) -> dict:
    value = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        value.update({"usage": usage, "choices": []})
    return value


class ClosingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1
        await super().aclose()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def collect(
        self, chunks: list[dict], *, status: int = 200
    ) -> tuple[list, list[dict]]:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            self.assertEqual(
                str(request.url), "https://litellm.test/v1/chat/completions"
            )
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            data = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
            data += "data: [DONE]\n\n"
            return httpx.Response(
                status, headers={"content-type": "text/event-stream"}, text=data
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            def factory(**options):
                self.assertNotIn("http_async_client", options)
                self.assertNotIn("http_socket_options", options)
                return create_gateway_chat_model(
                    **options, http_async_client=client, http_socket_options=()
                )

            gateway = Gateway(
                "https://litellm.test/v1",
                "test-key",
                protocol="chat_completions",
                client_factory=factory,
            )
            events = [
                event
                async for event in gateway.stream(
                    [{"role": "user", "content": "hello"}], "test", 16, 0, []
                )
            ]
        return events, requests

    async def test_bound_transport_uses_one_attempt_and_closes_after_stream(
        self,
    ) -> None:
        for status in (200, 500):
            with self.subTest(status=status):
                requests = []
                clients = []
                chunks = [
                    chunk({"content": "hello"}),
                    chunk(finish="stop"),
                    chunk(
                        usage={
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        }
                    ),
                ]

                def handler(request, requests=requests, status=status, chunks=chunks):
                    requests.append(request)
                    return httpx.Response(
                        status,
                        headers={"content-type": "text/event-stream"},
                        text="".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
                        + "data: [DONE]\n\n",
                    )

                def factory(clients=clients, **options):
                    clients.append(options["http_async_client"])
                    self.assertEqual(options["http_socket_options"], ())
                    return create_gateway_chat_model(**options)

                transport = ClosingTransport(handler)
                with patch(
                    "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
                    return_value=transport,
                ) as transport_factory:
                    gateway = Gateway(
                        "https://litellm.test/v1",
                        "test-key",
                        protocol="chat_completions",
                        client_factory=factory,
                        local_address="0.0.0.0",
                    )
                    if status == 200:
                        events = [
                            event
                            async for event in gateway.stream([], "test", 20, 0, [])
                        ]
                        self.assertEqual(events[-1].data.content, "hello")
                    else:
                        with self.assertRaises(GatewayError):
                            _ = [
                                event
                                async for event in gateway.stream([], "test", 20, 0, [])
                            ]
                transport_factory.assert_called_once_with(
                    local_address="0.0.0.0", verify=True, retries=0
                )
                self.assertEqual(len(requests), 1)
                self.assertEqual(transport.close_count, 1)
                self.assertTrue(clients[0].is_closed)

    async def test_bound_transport_closes_when_request_is_cancelled(self) -> None:
        started = asyncio.Event()
        clients = []

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        def factory(**options):
            clients.append(options["http_async_client"])
            return create_gateway_chat_model(**options)

        transport = ClosingTransport(handler)
        with patch(
            "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
            return_value=transport,
        ):
            gateway = Gateway(
                "https://litellm.test/v1",
                "test-key",
                protocol="chat_completions",
                client_factory=factory,
                local_address="0.0.0.0",
            )

            async def consume():
                return [event async for event in gateway.stream([], "test", 20, 0, [])]

            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(transport.close_count, 1)
        self.assertTrue(clients[0].is_closed)

    async def test_bound_transport_closes_on_client_construction_failure(self) -> None:
        for failure_stage in ("http", "model"):
            with self.subTest(failure_stage=failure_stage):
                clients = []
                transport = ClosingTransport(
                    lambda request: self.fail("construction failure sent a request")
                )

                def failing_factory(clients=clients, **options):
                    clients.append(options["http_async_client"])
                    raise ValueError("secret initialization details")

                gateway = Gateway(
                    "https://litellm.test/v1",
                    "test-key",
                    protocol="chat_completions",
                    client_factory=failing_factory,
                    local_address="0.0.0.0",
                )
                with (
                    patch(
                        "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
                        return_value=transport,
                    ),
                    patch(
                        "agent_platform.modules.runtime.gateway.httpx.AsyncClient",
                        wraps=httpx.AsyncClient,
                        side_effect=ValueError("secret initialization details")
                        if failure_stage == "http"
                        else None,
                    ),
                    self.assertRaises(GatewayError) as caught,
                ):
                    _ = [event async for event in gateway.stream([], "test", 20, 0, [])]
                self.assertFalse(caught.exception.request_sent)
                self.assertNotIn("secret initialization", str(caught.exception))
                self.assertEqual(transport.close_count, 1)
                if clients:
                    self.assertTrue(clients[0].is_closed)

    async def test_real_sdk_sse_text_usage_and_request_options(self) -> None:
        events, requests = await self.collect(
            [
                chunk({"role": "assistant", "content": "hello "}),
                chunk({"content": "world"}),
                chunk(finish="stop"),
                chunk(
                    usage={
                        "prompt_tokens": 12,
                        "completion_tokens": 2,
                        "total_tokens": 14,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    }
                ),
            ]
        )
        self.assertEqual([event.type for event in events], ["text", "text", "result"])
        result = events[-1].data
        self.assertEqual(result.content, "hello world")
        self.assertEqual((result.input_tokens, result.output_tokens), (12, 2))
        self.assertEqual(result.raw_usage["prompt_tokens_details"]["cached_tokens"], 4)
        self.assertEqual(result.raw_usage["usage_source"], "litellm_gateway")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["stream_options"], {"include_usage": True})
        self.assertEqual(requests[0]["max_completion_tokens"], 16)

    async def test_real_sdk_accumulates_fragmented_tool_arguments(self) -> None:
        events, _ = await self.collect(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression":',
                                },
                            }
                        ],
                    }
                ),
                chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": '"2+2"}'}}]}
                ),
                chunk(finish="tool_calls"),
                chunk(
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    }
                ),
            ]
        )
        self.assertEqual(events[-1].data.tool_calls[0]["args"], {"expression": "2+2"})

    async def test_missing_or_partial_usage_never_becomes_zero_cost(self) -> None:
        for usage in [
            None,
            {"prompt_tokens": 4},
            {"completion_tokens": 3},
            {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 5},
        ]:
            chunks = [chunk({"content": "hello"}), chunk(finish="stop")]
            if usage is not None:
                chunks.append(chunk(usage=usage))
            with self.subTest(usage=usage), self.assertRaises(UsageUnavailable):
                await self.collect(chunks)

    async def test_truncated_tool_json_is_not_silently_repaired(self) -> None:
        events, _ = await self.collect(
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression":"2+2"',
                                },
                            }
                        ]
                    }
                ),
                chunk(finish="length"),
                chunk(
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    }
                ),
            ]
        )
        self.assertIsInstance(events[-1].data.tool_calls[0]["args"], str)

    async def test_gateway_stream_inside_real_v3_graph(self) -> None:
        requests: list[dict] = []
        events: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertEqual(body["tools"][0]["function"]["name"], "calculator")
                chunks = [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "calculator",
                                        "arguments": '{"expression":"2+2"}',
                                    },
                                }
                            ]
                        }
                    ),
                    chunk(finish="tool_calls"),
                ]
            else:
                self.assertEqual(
                    json.loads(body["messages"][-1]["content"])["result"], 4
                )
                chunks = [chunk({"content": "Answer: 4"}), chunk(finish="stop")]
            chunks.append(
                chunk(
                    usage={
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    }
                )
            )
            data = (
                "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=data
            )

        async def emit(name: str, data: dict) -> None:
            events.append((name, data))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                "https://litellm.test/v1",
                "test-key",
                protocol="chat_completions",
                client_factory=lambda **options: create_gateway_chat_model(
                    **options, http_async_client=client, http_socket_options=()
                ),
            )

            async def invoke(messages: list[dict], tools: list[dict]) -> ModelReply:
                result = None
                async for event in gateway.stream(messages, "test", 20, 0, tools):
                    if event.type == "text":
                        await emit("message.delta", event.data)
                    else:
                        result = event.data
                self.assertIsNotNone(result)
                return result

            result = await execute_agent(
                {"max_steps": 3, "tools": ["calculator"]},
                [{"role": "user", "content": "calculate 2+2"}],
                invoke,
                emit,
                never_cancel,
            )
        self.assertEqual(result, "Answer: 4")
        self.assertEqual(len(requests), 2)
        self.assertIn(("message.delta", {"text": "Answer: 4"}), events)

    async def test_unknown_completion_is_not_settled(self) -> None:
        with self.assertRaises(UsageUnavailable):
            await self.collect(
                [
                    chunk({"content": "partial"}),
                    chunk(usage={"prompt_tokens": 1, "completion_tokens": 1}),
                ]
            )

    async def test_http_error_does_not_retry_or_expose_provider_message(self) -> None:
        count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal count
            count += 1
            return httpx.Response(
                500,
                json={
                    "error": {
                        "message": "secret-provider-prompt",
                        "type": "internal_error",
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                "https://litellm.test/v1",
                "test-key",
                protocol="chat_completions",
                client_factory=lambda **options: create_gateway_chat_model(
                    **options, http_async_client=client, http_socket_options=()
                ),
            )
            with self.assertRaises(GatewayError) as caught:
                _ = [event async for event in gateway.stream([], "test", 20, 0, [])]
        self.assertEqual(count, 1)
        self.assertNotIn("secret-provider-prompt", str(caught.exception))
        self.assertTrue(caught.exception.request_sent)

    async def test_no_fallback_to_other_environment_credentials(self) -> None:
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "not-platform", "OPENAI_BASE_URL": "https://wrong.test"},
            clear=True,
        ):
            gateway = Gateway(protocol="chat_completions")
            self.assertFalse(gateway.configured)
            with self.assertRaises(GatewayError) as caught:
                _ = [event async for event in gateway.stream([], "test", 20, 0, [])]
            self.assertFalse(caught.exception.request_sent)


if __name__ == "__main__":
    unittest.main()
