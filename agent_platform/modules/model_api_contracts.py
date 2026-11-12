"""Bounded OpenAI-compatible text and client-executed function requests."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_platform.modules.execution_policy import ReasoningEffort


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class KeyCreate(Strict):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int = Field(default=90, ge=1, le=365)
    model_ids: list[str] = Field(default_factory=list, max_length=100)


class KeyView(BaseModel):
    id: str
    user_id: str
    name: str
    prefix: str
    model_ids: list[str]
    created_at: str
    expires_at: str
    revoked_at: str | None
    last_used_at: str | None


class KeyCreated(KeyView):
    secret: str


class KeyList(BaseModel):
    items: list[KeyView]


class Function(Strict):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(default="", max_length=4000)
    parameters: dict = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    strict: bool | None = None


class Tool(Strict):
    type: Literal["function"] = "function"
    function: Function


class CallFunction(Strict):
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(max_length=64000)


class ToolCall(Strict):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"] = "function"
    function: CallFunction


class Message(Strict):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=128000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_message(self):
        if self.role == "tool":
            if not self.tool_call_id or self.content is None or self.tool_calls:
                raise ValueError("Tool messages need tool_call_id and text content")
        elif self.tool_call_id or (self.tool_calls and self.role != "assistant"):
            raise ValueError("Tool calls belong to assistant messages")
        if self.content is None and not self.tool_calls:
            raise ValueError("Text content is required")
        return self


class StreamOptions(Strict):
    include_usage: bool = False


class ChatRequest(Strict):
    model: str = Field(min_length=1, max_length=200)
    messages: list[Message] = Field(min_length=1, max_length=100)
    tools: list[Tool] = Field(default_factory=list, max_length=16)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=2000000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=2000000)
    temperature: float = Field(default=1.0, ge=0, le=2)
    reasoning_effort: ReasoningEffort | None = None
    n: Literal[1] = 1
    tool_choice: Literal["auto"] = "auto"

    @model_validator(mode="after")
    def bounded(self):
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Use only one output token limit")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if len(self.model_dump_json().encode()) > 262144:
            raise ValueError("Request exceeds 256 KiB")
        pending = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("Tool output has no matching call")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("Supply all tool outputs before the next message")
                for call in message.tool_calls:
                    if call.id in pending:
                        raise ValueError("Duplicate tool call ID")
                    pending.add(call.id)
        if pending:
            raise ValueError("Tool outputs are required before continuing")
        names = [tool.function.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("Tool names must be unique")
        return self

    def normalized(self):
        return {
            "model": self.model,
            "messages": [
                message.model_dump(exclude_none=True) for message in self.messages
            ],
            "tools": [tool.model_dump(exclude_none=True) for tool in self.tools],
            "max_tokens": self.max_completion_tokens or self.max_tokens,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }


class Reasoning(Strict):
    effort: ReasoningEffort | None = None


class ResponsesRequest(Strict):
    model: str = Field(min_length=1, max_length=200)
    input: str | list[dict]
    instructions: str | None = Field(default=None, max_length=128000)
    tools: list[dict] = Field(default_factory=list, max_length=16)
    stream: bool = False
    max_output_tokens: int | None = Field(default=None, ge=1, le=2000000)
    temperature: float = Field(default=1.0, ge=0, le=2)
    reasoning: Reasoning | None = None
    store: Literal[False] = False
    previous_response_id: None = None
    tool_choice: Literal["auto"] = "auto"

    def normalized(self):
        messages = []
        if self.instructions:
            messages.append({"role": "system", "content": self.instructions})
        if isinstance(self.input, str):
            messages.append({"role": "user", "content": self.input})
        else:
            if len(self.input) > 100:
                raise ValueError("At most 100 input items are supported")
            for item in self.input:
                kind = item.get("type", "message")
                if kind == "function_call":
                    if set(item) - {
                        "type",
                        "id",
                        "status",
                        "call_id",
                        "name",
                        "arguments",
                    }:
                        raise ValueError("Unsupported function call fields")
                    call = {
                        "id": item.get("call_id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name"),
                            "arguments": item.get("arguments"),
                        },
                    }
                    if (
                        messages
                        and messages[-1]["role"] == "assistant"
                        and messages[-1].get("tool_calls")
                    ):
                        messages[-1]["tool_calls"].append(call)
                    else:
                        messages.append({"role": "assistant", "tool_calls": [call]})
                elif kind == "function_call_output":
                    if set(item) - {"type", "id", "call_id", "output"}:
                        raise ValueError("Unsupported function output fields")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.get("call_id"),
                            "content": item.get("output"),
                        }
                    )
                elif kind == "message":
                    if set(item) - {"type", "id", "status", "role", "content"}:
                        raise ValueError("Unsupported message fields")
                    content = item.get("content")
                    if isinstance(content, list):
                        parts = []
                        for part in content:
                            if (
                                not isinstance(part, dict)
                                or set(part)
                                - {"type", "text", "annotations", "logprobs"}
                                or part.get("type") not in {"input_text", "output_text"}
                                or not isinstance(part.get("text"), str)
                            ):
                                raise ValueError("Only text input is supported")
                            parts.append(part["text"])
                        content = "".join(parts)
                    messages.append({"role": item.get("role"), "content": content})
                else:
                    raise ValueError(
                        "Only messages and client function calls are supported"
                    )
        converted = []
        for tool in self.tools:
            if tool.get("type") != "function":
                raise ValueError("Only client function tools are supported")
            converted.append(
                {
                    "type": "function",
                    "function": {k: v for k, v in tool.items() if k != "type"},
                }
            )
        return ChatRequest.model_validate(
            {
                "model": self.model,
                "messages": messages,
                "tools": converted,
                "max_completion_tokens": self.max_output_tokens,
                "temperature": self.temperature,
                "reasoning_effort": self.reasoning.effort if self.reasoning else None,
            }
        ).normalized()


def serialize_reply(reply):
    return {
        "content": reply.content,
        "tool_calls": reply.tool_calls,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        "finish_reason": reply.raw_usage.get("finish_reason", "stop"),
    }


def arguments(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
