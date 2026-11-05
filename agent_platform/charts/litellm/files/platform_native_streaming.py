"""Keep native Responses streaming for dynamically added OpenAI deployments.

LiteLLM 1.83.0 registers hot-loaded model metadata under the deployment ID,
but its Responses streaming check looks up the upstream model name. Unknown
names otherwise select fake_stream and buffer the entire upstream response.
Remove this compatibility hook once the pinned gateway fixes that lookup.
"""

import litellm
from litellm.integrations.custom_logger import CustomLogger


def register_native_streaming(deployments, model_name):
    for deployment in deployments:
        if deployment.get("model_name") != model_name:
            continue
        info = deployment.get("model_info", {})
        upstream = deployment.get("litellm_params", {}).get("model")
        if (
            info.get("mode") != "responses"
            or info.get("supports_native_streaming") is not True
            or not isinstance(upstream, str)
            or not upstream.startswith("openai/")
        ):
            continue
        current = litellm.model_cost.get(upstream, {})
        if current.get("supports_native_streaming") is True:
            continue
        # Preserve cost/context metadata; no credential or request data enters
        # the capability map. Only server-declared streaming models opt in.
        litellm.register_model(
            model_cost={
                upstream: {
                    **current,
                    "mode": "responses",
                    "litellm_provider": "openai",
                    "supports_native_streaming": True,
                }
            }
        )


class NativeResponsesStreaming(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type == "aresponses" and data.get("stream") is True:
            from litellm.proxy.proxy_server import llm_router

            if llm_router is not None:
                register_native_streaming(llm_router.model_list, data.get("model"))
        return data


proxy_handler_instance = NativeResponsesStreaming()
