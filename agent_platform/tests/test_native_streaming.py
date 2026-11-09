"""Exercise the pinned SDK's real capability lookup without model requests."""

import unittest
import uuid
from unittest.mock import patch

import litellm
from litellm import Router
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.types.router import Deployment

from agent_platform.charts.litellm.files.platform_native_streaming import (
    NativeResponsesStreaming,
    register_native_streaming,
)
from agent_platform.modules.gateway_control import LiteLLMControlAdapter


class NativeStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.name = "platform-stream-test-" + uuid.uuid4().hex
        self.upstream = "openai/" + self.name
        self.route = "provider-" + self.name
        self.identifier = uuid.uuid4().hex
        self.info = {
            "id": self.identifier,
            "mode": "responses",
            "litellm_provider": "openai",
            "supports_native_streaming": True,
        }
        self.deployment = {
            "model_name": self.route,
            "litellm_params": {
                "model": self.upstream,
                "api_base": "http://127.0.0.1:1/v1",
                "api_key": "offline-fixture-key",
            },
            "model_info": self.info,
        }
        self.addCleanup(self.clear_models)

    def clear_models(self):
        for name in list(litellm.model_cost):
            if self.name in name or name == self.identifier:
                litellm.model_cost.pop(name)

    def fake_stream(self):
        return OpenAIResponsesAPIConfig().should_fake_stream(
            model=self.name, stream=True, custom_llm_provider="openai"
        )

    async def test_hot_loaded_deployment_streams_without_restarting_router(self):
        router = Router(model_list=[], num_retries=0)
        router.add_deployment(Deployment(**self.deployment))
        # Reproduce 1.83.0: even explicit metadata on a hot-loaded deployment
        # is invisible to the native streaming lookup by upstream model name.
        self.assertTrue(self.fake_stream())
        request = {"model": self.route, "stream": True, "input": "private prompt"}
        with patch("litellm.proxy.proxy_server.llm_router", router):
            result = await NativeResponsesStreaming().async_pre_call_hook(
                None, None, request, "aresponses"
            )
        self.assertIs(result, request)
        self.assertEqual(request["input"], "private prompt")
        self.assertFalse(self.fake_stream())
        self.assertNotIn("api_key", litellm.model_cost[self.upstream])
        self.assertNotIn("input", litellm.model_cost[self.upstream])

    def test_unknown_or_non_streaming_deployments_keep_their_capabilities(self):
        for changes in (
            {"supports_native_streaming": False},
            {"supports_native_streaming": None},
            {"mode": "chat"},
        ):
            with self.subTest(changes=changes):
                deployment = {
                    **self.deployment,
                    "model_info": {**self.info, **changes},
                }
                register_native_streaming([deployment], self.route)
                self.assertTrue(self.fake_stream())
        register_native_streaming([self.deployment], "other-route")
        self.assertTrue(self.fake_stream())

    def test_registration_preserves_prices_and_context_and_is_idempotent(self):
        litellm.model_cost[self.upstream] = {
            "mode": "responses",
            "litellm_provider": "openai",
            "supports_native_streaming": False,
            "input_cost_per_token": 0.000003,
            "max_input_tokens": 12345,
        }
        register_native_streaming([self.deployment], self.route)
        info = litellm.model_cost[self.upstream]
        self.assertEqual(info["input_cost_per_token"], 0.000003)
        self.assertEqual(info["max_input_tokens"], 12345)
        self.assertFalse(self.fake_stream())
        with patch.object(litellm, "register_model") as register:
            register_native_streaming([self.deployment], self.route)
        register.assert_not_called()

    async def test_non_streaming_requests_do_not_change_capabilities(self):
        request = {"model": self.route, "stream": False}
        with patch(
            "agent_platform.charts.litellm.files.platform_native_streaming.register_native_streaming"
        ) as register:
            result = await NativeResponsesStreaming().async_pre_call_hook(
                None, None, request, "aresponses"
            )
        self.assertIs(result, request)
        register.assert_not_called()

    def test_provider_registration_declares_native_responses_capabilities(self):
        adapter = LiteLLMControlAdapter("http://127.0.0.1:1", "offline-master")
        with patch.object(adapter, "_request") as request:
            adapter.create_provider_model(
                self.identifier,
                self.route,
                self.upstream,
                "https://provider.example.test/v1",
                "offline-fixture-key",
            )
        self.assertEqual(request.call_args.args[1]["model_info"], self.info)
        self.assertTrue(request.call_args.kwargs["mutation"])
