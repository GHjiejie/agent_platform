"""Public model API response schemas, shared by OpenAPI and frontend types."""

from typing import Literal

from pydantic import BaseModel

from agent_platform.modules.model_api_contracts import ToolCall


class KeyRevoked(BaseModel):
    id: str
    revoked: Literal[True]


class ManagementFieldError(BaseModel):
    field: str
    message: str


class ManagementError(BaseModel):
    code: str
    message: str
    details: list[ManagementFieldError] | None = None


class ManagementErrorResponse(BaseModel):
    error: ManagementError


class ModelApiModel(BaseModel):
    id: str
    object: Literal["model"]
    created: int
    owned_by: str


class ModelApiModels(BaseModel):
    object: Literal["list"]
    data: list[ModelApiModel]


class ModelApiError(BaseModel):
    type: Literal["model_api_error"]
    code: str
    message: str
    param: None


class ModelApiErrorResponse(BaseModel):
    error: ModelApiError


class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class CompletionMessage(BaseModel):
    role: Literal["assistant"]
    content: str | None
    tool_calls: list[ToolCall] | None = None


class CompletionChoice(BaseModel):
    index: int
    message: CompletionMessage
    finish_reason: str
    logprobs: None


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: CompletionUsage


class ResponseUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ResponseOutputText(BaseModel):
    type: Literal["output_text"]
    text: str
    annotations: list[dict]
    logprobs: list[dict]


class ResponseOutputMessage(BaseModel):
    id: str
    type: Literal["message"]
    status: Literal["completed"]
    role: Literal["assistant"]
    content: list[ResponseOutputText]


class ResponseFunctionCall(BaseModel):
    id: str
    type: Literal["function_call"]
    status: Literal["completed"]
    call_id: str
    name: str
    arguments: str


class IncompleteDetails(BaseModel):
    reason: Literal["max_output_tokens"]


class ModelApiResponse(BaseModel):
    id: str
    object: Literal["response"]
    created_at: int
    model: str
    status: Literal["in_progress", "completed", "incomplete", "failed"]
    output: list[ResponseOutputMessage | ResponseFunctionCall]
    error: ModelApiError | None
    incomplete_details: IncompleteDetails | None
    usage: ResponseUsage | None
    store: Literal[False]
    parallel_tool_calls: bool
    tool_choice: Literal["auto"]
    tools: list[dict]
    metadata: dict[str, str]
