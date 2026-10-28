"""Native Responses HTTP/SSE contracts on an entirely offline transport."""

from __future__ import annotations

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from agent_platform.modules.runtime.engine import RuntimeFailure, execute_agent
from agent_platform.modules.runtime.gateway import (
    Gateway,
    GatewayError,
    ModelReply,
    UsageUnavailable,
)


def text_item(text="canonical reply", item_id="msg-item-a"):
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_item(arguments='{"expression":"2+3"}', call_id="call-a"):
    return {
        "type": "function_call",
        "id": "fc-item-a",
        "call_id": call_id,
        "name": "calculator",
        "arguments": arguments,
        "status": "completed",
    }


def response(*, response_id="resp-a", output=None, usage=None, status="completed"):
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": status,
        "error": None,
        "incomplete_details": None,
        "output": [text_item()] if output is None else output,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if usage is None
        else usage,
        "metadata": {},
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }


def event(kind, sequence, **data):
    return {"type": kind, "sequence_number": sequence, **data}


def completed_events(final=None):
    final = response() if final is None else final
    pending = {
        **copy.deepcopy(final),
        "status": "in_progress",
        "output": [],
        "usage": None,
    }
    return [
        event("response.created", 0, response=copy.deepcopy(pending)),
        event("response.in_progress", 1, response=copy.deepcopy(pending)),
        event(
            "response.output_item.added",
            2,
            output_index=0,
            item={**text_item(""), "status": "in_progress"},
        ),
        event(
            "response.output_text.delta",
            3,
            item_id="msg-item-a",
            output_index=0,
            content_index=0,
            delta="streamed draft",
            logprobs=[],
        ),
        event("response.completed", 4, response=final),
    ]


def sse_bytes(events, *, multiline=False):
    frames = []
    for value in events:
        data = json.dumps(value, indent=2 if multiline else None)
        frames.append(
            f": keepalive\nevent: {value['type']}\n"
            + "\n".join("data: " + line for line in data.splitlines())
            + "\n\n"
        )
    return "".join(frames).encode()


class ClosingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.close_count = 0

    async def aclose(self):
        self.close_count += 1
        await super().aclose()


class BlockingSSEStream(httpx.AsyncByteStream):
    def __init__(self, started):
        self.started = started
        self.close_count = 0

    async def __aiter__(self):
        yield sse_bytes(completed_events()[:-1])
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.close_count += 1


class GatewayResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def run_sse(
        self,
        events,
        *,
        messages=None,
        tools=None,
        multiline=False,
        attempt_id="attempt-a",
        logical_call_id="logical-a",
        reasoning_effort=None,
    ):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_bytes(events, multiline=multiline),
            )

        transport = ClosingTransport(handler)
        with patch(
            "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
            return_value=transport,
        ):
            gateway = Gateway(
                base_url="http://gateway/v1",
                api_key="private-key",
                reasoning_effort=reasoning_effort,
            )
            values = [
                value
                async for value in gateway.stream(
                    messages or [{"role": "user", "content": "offline prompt"}],
                    "test-model",
                    64,
                    0.25,
                    tools or [],
                    attempt_id=attempt_id,
                    logical_call_id=logical_call_id,
                )
            ]
        self.assertEqual(transport.close_count, 1)
        self.assertEqual(len(requests), 1)
        return values, requests[0]

    def result(self, events):
        replies = [value.data for value in events if value.type == "result"]
        self.assertEqual(len(replies), 1)
        self.assertIsInstance(replies[0], ModelReply)
        return replies[0]

    async def test_developer_reasoning_override_is_request_scoped(self):
        _, low = await self.run_sse(completed_events(), reasoning_effort="low")
        self.assertEqual(json.loads(low.content)["reasoning"], {"effort": "low"})
        _, inherited = await self.run_sse(completed_events())
        self.assertNotIn("reasoning", json.loads(inherited.content))

    async def test_default_responses_request_converts_messages_tools_and_evidence_ids(
        self,
    ):
        messages = [
            {"role": "system", "content": "offline instruction"},
            {"role": "user", "content": "offline prompt"},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Calculate an expression",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        events, request = await self.run_sse(
            completed_events(), messages=messages, tools=tools
        )
        body = json.loads(request.content)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/responses")
        self.assertEqual(request.headers["authorization"], "Bearer private-key")
        self.assertEqual(request.headers["x-litellm-call-id"], "attempt-a")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["input"], messages)
        self.assertEqual(body["max_output_tokens"], 64)
        self.assertEqual(body["temperature"], 0.25)
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertNotIn("messages", body)
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("stream_options", body)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "calculator")
        self.assertEqual(
            body["tools"][0]["parameters"], tools[0]["function"]["parameters"]
        )
        self.assertNotIn("function", body["tools"][0])
        self.assertEqual(body["user"], "agent-platform-attempt-attempt-a")
        self.assertEqual(body["metadata"]["platform_attempt_id"], "attempt-a")
        self.assertEqual(body["metadata"]["platform_logical_call_id"], "logical-a")
        reply = self.result(events)
        self.assertEqual(reply.raw_usage["attempt_id"], "attempt-a")
        self.assertEqual(reply.raw_usage["logical_call_id"], "logical-a")

    async def test_stable_response_id_accepts_distinct_item_ids_and_canonical_final_output(
        self,
    ):
        final = response(
            output=[text_item("canonical A"), text_item("canonical B", "msg-item-b")]
        )
        wire = completed_events(final)
        wire.insert(
            -1,
            event(
                "response.output_text.delta",
                4,
                item_id="msg-item-b",
                output_index=1,
                content_index=0,
                delta=" plus another draft",
                logprobs=[],
            ),
        )
        wire[-1]["sequence_number"] = 5
        events, _ = await self.run_sse(wire)
        self.assertEqual(
            [value.data["text"] for value in events if value.type == "text"],
            ["streamed draft", " plus another draft"],
        )
        reply = self.result(events)
        self.assertEqual(reply.content, "canonical Acanonical B")
        self.assertEqual(reply.tool_calls, [])
        self.assertEqual((reply.input_tokens, reply.output_tokens), (12, 4))
        self.assertEqual(reply.raw_usage["input_tokens"], 12)
        self.assertEqual(reply.raw_usage["output_tokens"], 4)
        self.assertEqual(reply.raw_usage["prompt_tokens"], 12)
        self.assertEqual(reply.raw_usage["completion_tokens"], 4)
        self.assertEqual(reply.raw_usage["gateway_response_id"], "resp-a")
        self.assertEqual(reply.raw_usage["finish_reason"], "stop")

    async def test_changed_response_id_rejects_evidence(self):
        for position in (1, -1):
            with self.subTest(position=position):
                wire = completed_events()
                wire[position]["response"]["id"] = "resp-changed"
                with self.assertRaises(UsageUnavailable):
                    await self.run_sse(wire)

    async def test_missing_negative_boolean_or_conflicting_usage_never_produces_result(
        self,
    ):
        cases = [
            None,
            {},
            {"input_tokens": 12, "total_tokens": 16},
            {"input_tokens": -1, "output_tokens": 4, "total_tokens": 3},
            {"input_tokens": 12, "output_tokens": True, "total_tokens": 13},
            {"input_tokens": False, "output_tokens": 4, "total_tokens": 4},
            {"input_tokens": 12, "output_tokens": 4, "total_tokens": 99},
            {"input_tokens": 12, "output_tokens": 4, "total_tokens": True},
        ]
        for usage in cases:
            with self.subTest(usage=usage):
                final = response()
                final["usage"] = usage
                with self.assertRaises(UsageUnavailable):
                    await self.run_sse(completed_events(final))

    async def test_eof_after_delta_without_terminal_rejects_usage(self):
        with self.assertRaises(UsageUnavailable):
            await self.run_sse(completed_events()[:-1])

    async def test_invalid_terminal_status_id_or_incomplete_reason_rejects_usage(self):
        for changes, terminal_type in (
            ({"status": "in_progress"}, "response.completed"),
            ({"id": ""}, "response.completed"),
            (
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "content_filter"},
                },
                "response.incomplete",
            ),
        ):
            with self.subTest(changes=changes):
                final = {**response(), **changes}
                wire = completed_events(final)
                wire[-1]["type"] = terminal_type
                with self.assertRaises(UsageUnavailable):
                    await self.run_sse(wire)

    async def test_failed_and_error_events_raise_safe_failure_without_a_result(self):
        sensitive = "provider-private-failure-detail"
        for failure in (
            event(
                "response.failed",
                4,
                response={
                    **response(status="failed"),
                    "error": {"code": "server_error", "message": sensitive},
                },
            ),
            event("error", 4, code="server_error", message=sensitive, param=None),
        ):
            with self.subTest(kind=failure["type"]):
                wire = [*completed_events()[:-1], failure]
                with self.assertRaises(GatewayError) as caught:
                    await self.run_sse(wire)
                self.assertEqual(caught.exception.code, "gateway_failed")
                self.assertNotIn(sensitive, str(caught.exception))

    async def test_incomplete_max_output_keeps_usage_and_unrepaired_tool_arguments(
        self,
    ):
        truncated = '{"expression":"2+3"'
        final = response(output=[function_item(truncated)], status="incomplete")
        final["incomplete_details"] = {"reason": "max_output_tokens"}
        wire = completed_events(final)
        wire[-1]["type"] = "response.incomplete"
        events, _ = await self.run_sse(wire)
        reply = self.result(events)
        self.assertEqual(reply.raw_usage["finish_reason"], "length")
        self.assertEqual((reply.input_tokens, reply.output_tokens), (12, 4))
        self.assertEqual(
            reply.tool_calls,
            [{"id": "call-a", "name": "calculator", "args": truncated}],
        )

    async def test_incomplete_function_calls_never_execute_even_with_valid_json(self):
        arguments = '{"expression":"2+3"}'
        for response_status, item_status in (
            ("incomplete", "completed"),
            ("completed", "incomplete"),
            ("completed", "in_progress"),
        ):
            with self.subTest(response_status=response_status, item_status=item_status):
                item = {**function_item(arguments), "status": item_status}
                final = response(output=[item], status=response_status)
                if response_status == "incomplete":
                    final["incomplete_details"] = {"reason": "max_output_tokens"}
                wire = completed_events(final)
                wire[-1]["type"] = "response." + response_status
                events, _ = await self.run_sse(wire)
                reply = self.result(events)
                self.assertEqual((reply.input_tokens, reply.output_tokens), (12, 4))
                self.assertEqual(reply.tool_calls[0]["args"], arguments)
                self.assertIsInstance(reply.tool_calls[0]["args"], str)

                invocations = []

                async def invoke(
                    messages, tools, *, invocations=invocations, reply=reply
                ):
                    invocations.append(messages)
                    if len(invocations) == 1:
                        return reply
                    self.assertEqual(messages[-1]["role"], "tool")
                    self.assertFalse(json.loads(messages[-1]["content"])["ok"])
                    return ModelReply("safe rejection", [], 1, 1, {})

                async def emit(name, data):
                    pass

                async def cancelled():
                    return False

                with patch(
                    "agent_platform.modules.runtime.engine.execute_tool"
                ) as execute:
                    if response_status == "incomplete":
                        with self.assertRaises(RuntimeFailure) as caught:
                            await execute_agent(
                                {"max_steps": 3, "tools": ["calculator"]},
                                [{"role": "user", "content": "offline calculate"}],
                                invoke,
                                emit,
                                cancelled,
                            )
                        self.assertEqual(caught.exception.code, "output_limit_exceeded")
                    else:
                        result = await execute_agent(
                            {"max_steps": 3, "tools": ["calculator"]},
                            [{"role": "user", "content": "offline calculate"}],
                            invoke,
                            emit,
                            cancelled,
                        )
                execute.assert_not_called()
                if response_status != "incomplete":
                    self.assertEqual(result, "safe rejection")
                self.assertEqual(
                    len(invocations), 1 if response_status == "incomplete" else 2
                )

    async def test_tool_call_uses_call_id_and_second_round_function_call_output(self):
        events, _ = await self.run_sse(
            completed_events(response(output=[function_item()]))
        )
        reply = self.result(events)
        self.assertEqual(reply.raw_usage["finish_reason"], "tool_calls")
        self.assertEqual(
            reply.tool_calls,
            [{"id": "call-a", "name": "calculator", "args": {"expression": "2+3"}}],
        )
        messages = [
            {"role": "user", "content": "offline calculate"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-a",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"2+3"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-a",
                "name": "calculator",
                "content": '{"ok":true,"result":5}',
            },
        ]
        second, request = await self.run_sse(completed_events(), messages=messages)
        inputs = json.loads(request.content)["input"]
        calls = [item for item in inputs if item.get("type") == "function_call"]
        outputs = [
            item for item in inputs if item.get("type") == "function_call_output"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["call_id"], "call-a")
        self.assertEqual(calls[0]["name"], "calculator")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"expression": "2+3"})
        self.assertEqual(
            outputs,
            [
                {
                    "type": "function_call_output",
                    "call_id": "call-a",
                    "output": '{"ok":true,"result":5}',
                }
            ],
        )
        self.assertEqual(self.result(second).raw_usage["finish_reason"], "stop")

    async def test_tool_loop_replays_private_context_and_exposes_only_readable_reasoning(
        self,
    ):
        opaque = "synthetic-encrypted-reasoning-context"
        reasoning_text = "synthetic-readable-reasoning-summary"
        reasoning = {
            "type": "reasoning",
            "id": "rs-item-a",
            "summary": [{"type": "summary_text", "text": reasoning_text}],
            "encrypted_content": opaque,
            "status": "completed",
        }
        function = function_item(call_id="tool-call-a")
        first = response(response_id="resp-tool-round", output=[reasoning, function])
        second = response(response_id="resp-final-round", output=[text_item("5")])
        replies = []
        requests = []
        public_events = []
        transports = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                wire = [
                    event(
                        "response.created",
                        0,
                        response={**first, "status": "in_progress", "output": []},
                    ),
                    event(
                        "response.reasoning_summary_text.delta",
                        1,
                        item_id=reasoning["id"],
                        output_index=0,
                        summary_index=0,
                        delta=reasoning_text,
                    ),
                    event("response.completed", 2, response=first),
                ]
            else:
                self.assertEqual(len(requests), 2)
                wire = [event("response.completed", 0, response=second)]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_bytes(wire),
            )

        def transport_factory(**_options):
            transport = ClosingTransport(handler)
            transports.append(transport)
            return transport

        async def invoke(messages, tools):
            values = []
            async for value in gateway.stream(messages, "test-model", 64, 0.25, tools):
                values.append(value)
                if value.type == "text":
                    public_events.append(("message.delta", value.data))
                elif value.type == "reasoning":
                    public_events.append(("reasoning.delta", value.data))
            reply = self.result(values)
            replies.append(reply)
            return reply

        async def emit(name, data):
            public_events.append((name, data))

        async def cancelled():
            return False

        with patch(
            "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
            side_effect=transport_factory,
        ):
            gateway = Gateway(base_url="http://gateway/v1", api_key="private-key")
            result = await execute_agent(
                {"max_steps": 3, "tools": ["calculator"]},
                [{"role": "user", "content": "offline calculate"}],
                invoke,
                emit,
                cancelled,
            )

        self.assertEqual(result, "5")
        self.assertEqual(len(requests), 2)
        self.assertEqual([transport.close_count for transport in transports], [1, 1])
        self.assertEqual(replies[0].response_items, [reasoning, function])
        self.assertEqual(replies[0].content, "")
        body = json.loads(requests[1].content)
        inputs = body["input"]
        self.assertEqual(inputs[0], {"role": "user", "content": "offline calculate"})
        self.assertEqual(inputs[1:3], [reasoning, function])
        self.assertEqual(len(inputs), 4)
        self.assertEqual(inputs[3]["type"], "function_call_output")
        self.assertEqual(inputs[3]["call_id"], function["call_id"])
        self.assertEqual(json.loads(inputs[3]["output"]), {"ok": True, "result": 5})
        self.assertEqual(
            [item.get("type") for item in inputs[1:]],
            ["reasoning", "function_call", "function_call_output"],
        )
        for request in requests:
            request_body = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertIs(request_body["store"], False)
            self.assertEqual(request_body["include"], ["reasoning.encrypted_content"])
            self.assertNotIn("previous_response_id", request_body)
        evidence = json.dumps(
            {"events": public_events, "usage": [reply.raw_usage for reply in replies]}
        )
        for private_value in (opaque, reasoning["id"]):
            self.assertNotIn(private_value, evidence)
        self.assertEqual(
            [data for name, data in public_events if name == "reasoning.delta"],
            [{"text": reasoning_text}],
        )
        self.assertNotIn(
            reasoning_text, json.dumps([reply.raw_usage for reply in replies])
        )
        self.assertEqual(
            [data["text"] for name, data in public_events if name == "message.delta"],
            ["5"],
        )

    async def test_reasoning_text_and_summary_fallback_are_not_duplicated(self):
        readable = "A public reasoning summary."
        for field, block in (
            ("summary", "summary_text"),
            ("content", "reasoning_text"),
        ):
            for streamed in (False, True):
                with self.subTest(field=field, streamed=streamed):
                    reasoning = {
                        "type": "reasoning",
                        "id": "rs-private",
                        field: [{"type": block, "text": readable}],
                        "encrypted_content": "private-ciphertext",
                    }
                    final = response(output=[reasoning, text_item("Answer")])
                    wire = []
                    if streamed:
                        wire.append(
                            event(
                                "response.reasoning_summary_text.delta"
                                if field == "summary"
                                else "response.reasoning_text.delta",
                                0,
                                delta=readable,
                                item_id="rs-private",
                            )
                        )
                    wire.append(event("response.completed", 1, response=final))
                    values, _ = await self.run_sse(wire)
                    self.assertEqual(
                        [value.data for value in values if value.type == "reasoning"],
                        [{"text": readable}],
                    )
                    self.assertEqual(self.result(values).content, "Answer")
                    public = json.dumps(
                        [value.data for value in values if value.type != "result"]
                    )
                    self.assertNotIn("rs-private", public)
                    self.assertNotIn("private-ciphertext", public)

    async def test_malformed_reasoning_delta_fails_safely(self):
        with self.assertRaises(GatewayError) as caught:
            await self.run_sse(
                [
                    event(
                        "response.reasoning_text.delta",
                        0,
                        delta={"encrypted_content": "private-value"},
                    ),
                    event("response.completed", 1, response=response()),
                ]
            )
        self.assertNotIn("private-value", str(caught.exception))

    async def test_sse_comments_and_multiline_data_preserve_terminal_reply(self):
        events, _ = await self.run_sse(completed_events(), multiline=True)
        self.assertEqual(self.result(events).content, "canonical reply")

    async def test_http_500_is_not_retried_and_error_message_is_safe(self):
        requests = []
        sensitive = "provider-private-message-and-key"

        def handler(request):
            requests.append(request)
            return httpx.Response(
                500,
                json={"error": {"message": sensitive, "type": "server_error"}},
            )

        transport = ClosingTransport(handler)
        with patch(
            "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
            return_value=transport,
        ):
            gateway = Gateway(base_url="http://gateway/v1", api_key="private-key")
            with self.assertRaises(GatewayError) as caught:
                _ = [
                    value
                    async for value in gateway.stream(
                        [{"role": "user", "content": sensitive}],
                        "test-model",
                        64,
                        0.25,
                        [],
                    )
                ]
        self.assertEqual(caught.exception.code, "gateway_failed")
        self.assertTrue(caught.exception.request_sent)
        self.assertNotIn(sensitive, str(caught.exception))
        self.assertNotIn("private-key", str(caught.exception))
        self.assertEqual(len(requests), 1)
        self.assertEqual(transport.close_count, 1)

    async def test_cancellation_closes_response_stream_and_owned_transport(self):
        started = asyncio.Event()
        stream = BlockingSSEStream(started)

        def handler(request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        transport = ClosingTransport(handler)
        with patch(
            "agent_platform.modules.runtime.gateway.httpx.AsyncHTTPTransport",
            return_value=transport,
        ):
            gateway = Gateway(base_url="http://gateway/v1", api_key="private-key")

            async def consume():
                return [
                    value
                    async for value in gateway.stream(
                        [{"role": "user", "content": "offline prompt"}],
                        "test-model",
                        64,
                        0.25,
                        [],
                    )
                ]

            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        self.assertEqual(stream.close_count, 1)
        self.assertEqual(transport.close_count, 1)


if __name__ == "__main__":
    unittest.main()
