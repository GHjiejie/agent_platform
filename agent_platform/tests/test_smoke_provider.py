"""Exercise the native Responses smoke fixture without Docker or paid model I/O."""

import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import httpx
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig

from agent_platform.deploy import mock_provider
from agent_platform.modules.runtime.engine import execute_agent
from agent_platform.modules.runtime.gateway import (
    Gateway,
    GatewayError,
    UsageUnavailable,
)


class NativeSmokeProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class RecordingHandler(mock_provider.Handler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.path, json.loads(raw)))
                self.rfile = io.BytesIO(raw)
                super().do_POST()

        mock_provider.COUNTS.update(calls=0, retry_test=0)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.gateway = Gateway(self.base + "/v1", "fixture-only-key")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def invoke(self, messages, tools):
        events = [
            event
            async for event in self.gateway.stream(
                messages, "fixture-model", 64, 0, tools
            )
        ]
        return events[-1].data

    async def test_native_text_and_tool_loop_preserve_usage_and_call_identity(self):
        text_reply = await self.invoke([{"role": "user", "content": "hello"}], [])
        self.assertEqual(text_reply.content, "Answer: 4")
        self.assertEqual((text_reply.input_tokens, text_reply.output_tokens), (12, 4))

        async def emit(_name, _data):
            pass

        async def cancelled():
            return False

        answer = await execute_agent(
            {"max_steps": 3, "tools": ["calculator"]},
            [{"role": "user", "content": "calculate 2+2"}],
            self.invoke,
            emit,
            cancelled,
        )
        self.assertEqual(answer, "Answer: 4")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual({path for path, _body in self.requests}, {"/v1/responses"})
        for _path, body in self.requests:
            self.assertIs(body["stream"], True)
            self.assertIs(body["store"], False)
            self.assertEqual(body["max_output_tokens"], 64)
            self.assertNotIn("messages", body)
        tool_request = self.requests[1][1]
        self.assertEqual(tool_request["tools"][0]["type"], "function")
        self.assertEqual(tool_request["tools"][0]["name"], "calculator")
        followup = self.requests[2][1]["input"]
        call = next(item for item in followup if item.get("type") == "function_call")
        output = next(
            item for item in followup if item.get("type") == "function_call_output"
        )
        self.assertEqual(call["call_id"], "smoke-tool")
        self.assertEqual(output["call_id"], call["call_id"])
        self.assertEqual(json.loads(call["arguments"]), {"expression": "2+2"})
        self.assertEqual(json.loads(output["output"])["result"], 4)

    async def test_missing_native_usage_is_not_settled(self):
        with self.assertRaises(UsageUnavailable):
            await self.invoke([{"role": "user", "content": "missing-usage-test"}], [])
        self.assertEqual(len(self.requests), 1)

    async def test_provider_error_receives_one_attempt(self):
        with self.assertRaises(GatewayError):
            await self.invoke([{"role": "user", "content": "retry-test"}], [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(mock_provider.COUNTS["retry_test"], 1)

    async def test_fixture_rejects_chat_route_without_inference(self):
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.base + "/v1/chat/completions",
                json={"model": "fixture-model", "messages": []},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(mock_provider.COUNTS["calls"], 0)

    def test_fixture_uses_one_response_id_and_independent_tool_item_id(self):
        body = {"model": "fixture-model", "input": "hello", "tools": [{}]}
        first = mock_provider.response_events(body)
        second = mock_provider.response_events(body)
        created = first[0]["response"]
        completed = first[-1]["response"]
        self.assertEqual(created["id"], completed["id"])
        self.assertNotEqual(created["id"], second[0]["response"]["id"])
        call = completed["output"][0]
        self.assertNotEqual(call["id"], call["call_id"])
        deltas = [
            event["delta"]
            for event in first
            if event["type"] == "response.function_call_arguments.delta"
        ]
        self.assertEqual("".join(deltas), call["arguments"])
        self.assertEqual(completed["usage"]["total_tokens"], 16)

    def test_fixture_events_validate_with_pinned_litellm_responses_models(self):
        for tools in ([], [{"type": "function", "name": "calculator"}]):
            events = mock_provider.response_events({"input": "hello", "tools": tools})
            for event in events:
                with self.subTest(event=event["type"], tools=bool(tools)):
                    event_class = OpenAIResponsesAPIConfig.get_event_model_class(
                        event["type"]
                    )
                    parsed = event_class(**event)
                    self.assertEqual(parsed.type, event["type"])


if __name__ == "__main__":
    unittest.main()
