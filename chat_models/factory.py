"""Request-scoped chat clients without mutating the examples' global model."""

from typing import Any

from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


class UsageAwareChatOpenAI(ChatOpenAI):
    """Keep original usage evidence before LangChain fills missing counts with zero.

    This narrow adapter targets the installed langchain-openai Chat Completions
    converter. Offline transport tests exercise the actual conversion path.
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if result is not None:
            # Chat Completions conversion discards the raw response id and the
            # framework later assigns a local lc_run id. Preserve only the
            # identifier actually supplied by the provider's SSE envelope.
            provider_id = chunk.get("id")
            if isinstance(provider_id, str) and provider_id:
                result.message.response_metadata["platform_gateway_response_id"] = (
                    provider_id
                )
        if result is not None and isinstance(chunk.get("usage"), dict):
            result.message.response_metadata["platform_raw_usage"] = dict(
                chunk["usage"]
            )
        return result


def create_gateway_chat_model(
    *,
    model: str,
    api_key: str,
    base_url: str,
    max_tokens: int,
    temperature: float,
    timeout: float = 90,
    use_responses_api: bool = False,
    **transport_options: Any,
) -> ChatOpenAI:
    """Create an isolated LiteLLM client; one admission means one HTTP attempt.

    The platform currently enables Chat Completions only. The protocol switch is
    explicit for other repository callers; Responses usage must be separately
    validated before enabling it in the platform catalog.
    """
    return UsageAwareChatOpenAI(
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=0,
        streaming=True,
        stream_usage=True,
        use_responses_api=use_responses_api,
        **transport_options,
    )
