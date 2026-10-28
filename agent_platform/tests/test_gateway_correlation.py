"""Check correlation on the actual HTTP/SDK SSE path, with no provider network."""

import json
import unittest

import httpx

from agent_platform.modules.runtime.gateway import Gateway, GatewayError
from agent_platform.tests.test_runtime import chunk
from chat_models.factory import create_gateway_chat_model


class GatewayCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def stream_attempts(
        self, attempts, *, status=200, response_id="chatcmpl-provider-response-42"
    ):
        requests = []

        def handler(request):
            requests.append(request)
            if status != 200:
                return httpx.Response(
                    status,
                    json={
                        "error": {
                            "message": "provider-secret",
                            "type": "internal_error",
                        }
                    },
                )
            body = [
                chunk({"content": "Offline correlated reply"}),
                chunk(finish="stop"),
                chunk(
                    usage={
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    }
                ),
            ]
            for item in body:
                if response_id is None:
                    item.pop("id", None)
                else:
                    item["id"] = response_id
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-litellm-call-id": "response-header-id",
                },
                text="".join(f"data: {json.dumps(item)}\n\n" for item in body)
                + "data: [DONE]\n\n",
            )

        replies = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                "https://offline-gateway.test/v1",
                "offline-inference-key",
                protocol="chat_completions",
                client_factory=lambda **options: create_gateway_chat_model(
                    **options, http_async_client=client, http_socket_options=()
                ),
            )
            for attempt, logical in attempts:
                events = [
                    event
                    async for event in gateway.stream(
                        [{"role": "user", "content": "Test input"}],
                        "offline-route",
                        16,
                        0,
                        [],
                        attempt_id=attempt,
                        logical_call_id=logical,
                    )
                ]
                replies.append(events[-1].data)
        return requests, replies

    async def test_attempt_correlation_reaches_http_header_user_metadata_and_reply(
        self,
    ):
        requests, replies = await self.stream_attempts(
            [("attempt-1", "logical-call-a"), ("attempt-2", "logical-call-a")]
        )
        self.assertEqual(len(requests), 2)
        for request, reply, attempt in zip(
            requests, replies, ("attempt-1", "attempt-2"), strict=True
        ):
            self.assertEqual(
                str(request.url), "https://offline-gateway.test/v1/chat/completions"
            )
            self.assertEqual(
                request.headers["authorization"], "Bearer offline-inference-key"
            )
            self.assertEqual(request.headers["x-litellm-call-id"], attempt)
            body = json.loads(request.content)
            self.assertEqual(body["user"], "agent-platform-attempt-" + attempt)
            self.assertEqual(body["metadata"]["platform_attempt_id"], attempt)
            self.assertEqual(
                body["metadata"]["platform_logical_call_id"], "logical-call-a"
            )
            self.assertEqual(
                body["metadata"]["tags"], ["agent-platform-attempt:" + attempt]
            )
            self.assertEqual(body["model"], "offline-route")
            self.assertEqual(reply.raw_usage["attempt_id"], attempt)
            self.assertEqual(reply.raw_usage["logical_call_id"], "logical-call-a")
            self.assertEqual(reply.raw_usage["gateway_call_id"], attempt)
            self.assertEqual(
                reply.raw_usage["gateway_response_id"], "chatcmpl-provider-response-42"
            )
            self.assertEqual(
                reply.raw_usage["gateway_response_id_source"], "provider_sse"
            )
            self.assertEqual((reply.input_tokens, reply.output_tokens), (7, 3))

    async def test_missing_provider_id_remains_unknown_instead_of_using_framework_id(
        self,
    ):
        _, replies = await self.stream_attempts(
            [("attempt-no-id", "logical-no-id")], response_id=None
        )
        self.assertIsNone(replies[0].raw_usage["gateway_response_id"])
        self.assertIsNone(replies[0].raw_usage["gateway_response_id_source"])
        self.assertEqual(replies[0].raw_usage["gateway_call_id"], "attempt-no-id")

    async def test_legacy_uncorrelated_calls_do_not_invent_identifiers(self):
        requests, replies = await self.stream_attempts([(None, None)])
        request = requests[0]
        self.assertNotIn("x-litellm-call-id", request.headers)
        body = json.loads(request.content)
        self.assertNotIn("user", body)
        self.assertNotIn("metadata", body)
        self.assertNotIn("attempt_id", replies[0].raw_usage)
        self.assertNotIn("gateway_response_id", replies[0].raw_usage)

    async def test_correlated_gateway_failure_is_one_http_attempt(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                503,
                json={
                    "error": {"message": "provider-secret", "type": "internal_error"}
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = Gateway(
                "https://offline-gateway.test/v1",
                "offline-inference-key",
                protocol="chat_completions",
                client_factory=lambda **options: create_gateway_chat_model(
                    **options, http_async_client=client, http_socket_options=()
                ),
            )
            with self.assertRaises(GatewayError) as caught:
                _ = [
                    event
                    async for event in gateway.stream(
                        [],
                        "offline-route",
                        16,
                        0,
                        [],
                        attempt_id="attempt-error",
                        logical_call_id="logical-error",
                    )
                ]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["x-litellm-call-id"], "attempt-error")
        self.assertTrue(caught.exception.request_sent)
        self.assertNotIn("provider-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
