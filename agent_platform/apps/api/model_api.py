"""Member key management and a bounded OpenAI-compatible model API."""

# ruff: noqa: B008
import asyncio
import json
import time
from contextlib import suppress
from datetime import datetime

from anyio import CancelScope
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from agent_platform.apps.api.model_api_responses import (
    ChatCompletion,
    KeyRevoked,
    ManagementErrorResponse,
    ModelApiErrorResponse,
    ModelApiModels,
    ModelApiResponse,
)
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.member_api_keys import MemberKeys
from agent_platform.modules.model_api_contracts import (
    ChatRequest,
    KeyCreate,
    KeyCreated,
    KeyList,
    ResponsesRequest,
    arguments,
)
from agent_platform.modules.platform import STREAM_LEASE_SECONDS

TERMINAL = {"succeeded", "failed", "cancelled", "expired", "interrupted"}

KEY_ERRORS = {
    status: {"model": ManagementErrorResponse, "description": description}
    for status, description in {
        401: "登录失效。",
        403: "权限不足、组织不可写或 CSRF 校验失败。",
        404: "目标组织、模型或密钥不存在，或无权访问。",
        422: "请求参数无效，详见 error.details。",
        429: "有效个人密钥已达上限。",
    }.items()
}

MODEL_ERRORS = {
    status: {"model": ModelApiErrorResponse, "description": description}
    for status, description in {
        400: "无效的请求头。",
        401: "密钥无效、过期、已撤销或所属成员权限失效。",
        402: "可用余额或预算不足。",
        403: "密钥模型范围、组织授权或模型状态不允许调用。",
        404: "模型不存在或当前组织无权使用。",
        409: "幂等请求正在运行，或相同标识对应不同请求。",
        413: "请求体超过大小限制。",
        422: "请求字段格式无效，或使用了当前不支持的能力。",
        429: "队列、速率、并发或连接额度已用尽，参见 Retry-After。",
        502: "模型请求未完成。",
    }.items()
}

STREAM_RESPONSE = {
    200: {
        "description": "stream=false 返回 JSON；stream=true 返回 SSE 事件流。",
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
    }
}


def error_body(code, message):
    return {
        "error": {
            "type": "model_api_error",
            "code": code,
            "message": message,
            "param": None,
        }
    }


def chat_result(run, reply):
    calls = [
        {
            "id": c["id"],
            "type": "function",
            "function": {"name": c["name"], "arguments": arguments(c["args"])},
        }
        for c in reply["tool_calls"]
    ]
    message = {
        "role": "assistant",
        "content": reply["content"] or (None if calls else ""),
    }
    if calls:
        message["tool_calls"] = calls
    return {
        "id": "chatcmpl-" + run["id"],
        "object": "chat.completion",
        "created": int(datetime.fromisoformat(run["created_at"]).timestamp()),
        "model": run["spec"]["model"]["alias"],
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": reply["finish_reason"],
                "logprobs": None,
            }
        ],
        "usage": {
            "prompt_tokens": reply["input_tokens"],
            "completion_tokens": reply["output_tokens"],
            "total_tokens": reply["input_tokens"] + reply["output_tokens"],
        },
    }


def response_result(run, reply=None, status="in_progress"):
    output = []
    if reply:
        if reply["content"]:
            output.append(
                {
                    "id": "msg_" + run["id"],
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reply["content"],
                            "annotations": [],
                            "logprobs": [],
                        }
                    ],
                }
            )
        output.extend(
            {
                "id": "fc_" + str(i) + "_" + run["id"],
                "type": "function_call",
                "status": "completed",
                "call_id": c["id"],
                "name": c["name"],
                "arguments": arguments(c["args"]),
            }
            for i, c in enumerate(reply["tool_calls"])
        )
        status = "incomplete" if reply["finish_reason"] == "length" else "completed"
    return {
        "id": "resp_" + run["id"],
        "object": "response",
        "created_at": int(datetime.fromisoformat(run["created_at"]).timestamp()),
        "model": run["spec"]["model"]["alias"],
        "status": status,
        "output": output,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"}
        if status == "incomplete"
        else None,
        "usage": {
            "input_tokens": reply["input_tokens"],
            "output_tokens": reply["output_tokens"],
            "total_tokens": reply["input_tokens"] + reply["output_tokens"],
        }
        if reply
        else None,
        "store": False,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
    }


def register(app, platform, auth, leased_response):
    service = MemberKeys(platform)

    @app.get(
        "/api/v2/tenants/{tenant_id}/api-keys",
        response_model=KeyList,
        tags=["API 密钥"],
        summary="查询个人密钥",
        description="默认仅返回本人密钥；all_members=true 要求组织成员管理权限。只返回脱敏标识。",
        responses=KEY_ERRORS,
    )
    def list_keys(all_members: bool = False, user=Depends(auth)):
        return service.list(user, all_members)

    @app.post(
        "/api/v2/tenants/{tenant_id}/api-keys",
        response_model=KeyCreated,
        status_code=201,
        tags=["API 密钥"],
        summary="创建个人密钥",
        description=(
            "要求 runs.execute 权限和 X-CSRF-Token。完整密钥仅在本次响应返回。"
            "有效期 1–365 天，默认 90 天；每位成员最多 10 个有效密钥。"
            "model_ids 使用组织模型 ID；空数组表示当前及后续授权的所有可用模型。"
        ),
        responses=KEY_ERRORS,
    )
    def create_key(body: KeyCreate, user=Depends(auth)):
        return service.create(user, body)

    @app.delete(
        "/api/v2/tenants/{tenant_id}/api-keys/{key_id}",
        response_model=KeyRevoked,
        tags=["API 密钥"],
        summary="撤销密钥",
        description="本人可撤销自己的密钥；撤销他人密钥需要成员管理权限。重复撤销返回成功。",
        responses=KEY_ERRORS,
    )
    def revoke_key(key_id: str, user=Depends(auth)):
        return service.revoke(user, key_id)

    bearer_scheme = HTTPBearer(
        scheme_name="MemberApiKey",
        bearerFormat="ap_<tenant_id>.<key_id>.<secret>",
        description="使用个人 API 密钥；该密钥不能访问 Cookie 管理接口。",
        auto_error=False,
    )

    async def bearer(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ):
        header = request.headers.get("authorization", "")
        if credentials is None or len(header) > 256:
            raise PlatformError(
                401,
                "invalid_api_key",
                "请通过 Authorization: Bearer 提供个人 API 密钥。",
            )
        identity = await asyncio.to_thread(
            service.authenticate, credentials.credentials
        )
        with platform.db.tenant_scope(identity["user"]["tenant_id"]):
            yield identity

    @app.get(
        "/v1/models",
        response_model=ModelApiModels,
        tags=["模型调用"],
        summary="查询当前密钥可调用的模型",
        responses={401: MODEL_ERRORS[401]},
    )
    def model_list(identity=Depends(bearer)):
        return service.models(identity)

    async def parse(request, contract):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise PlatformError(
                    413, "request_too_large", "请求大小不得超过 256 KiB。"
                )
        try:
            return contract.model_validate_json(raw)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(v) for v in e["loc"]) or "body" for e in exc.errors()}
            )
            raise PlatformError(
                422,
                "invalid_request",
                "请求参数不受支持或格式无效：" + ", ".join(fields[:10]),
            ) from None

    async def complete(request, identity, contract, responses):
        body = await parse(request, contract)
        try:
            payload = body.normalized()
        except (ValueError, TypeError, KeyError):
            raise PlatformError(
                422,
                "invalid_request",
                "请使用文本消息与客户端函数工具；输入结构或参数不受支持。",
            ) from None
        # Stream admission and enqueue commit atomically; cancellation waits for
        # the transaction so it cannot strand an admitted run or stream slot.
        with platform.db.tenant_scope(identity["user"]["tenant_id"]):
            admission = asyncio.create_task(
                asyncio.to_thread(
                    service.enqueue,
                    identity,
                    payload,
                    request.headers.get("idempotency-key"),
                )
            )
            try:
                run = await asyncio.shield(admission)
            except asyncio.CancelledError:
                with CancelScope(shield=True):
                    admitted = await admission
                    await asyncio.to_thread(service.cancel, identity, admitted["id"])
                    await asyncio.to_thread(
                        platform.update_stream,
                        identity["user"],
                        admitted["_stream_id"],
                        release=True,
                    )
                raise
        stream_id = run.pop("_stream_id")

        async def check():
            await asyncio.to_thread(service.read, identity, run["id"], 2147483647)

        async def renew():
            await check()
            await asyncio.to_thread(platform.update_stream, identity["user"], stream_id)

        async def release():
            with (
                platform.db.tenant_scope(identity["user"]["tenant_id"]),
                CancelScope(shield=True),
            ):
                with suppress(Exception):
                    await asyncio.to_thread(service.cancel, identity, run["id"])
                with suppress(Exception):
                    await asyncio.to_thread(
                        platform.update_stream,
                        identity["user"],
                        stream_id,
                        release=True,
                    )

        async def updates():
            cursor = 0
            last_keepalive = time.monotonic()
            while True:
                if await request.is_disconnected():
                    raise asyncio.CancelledError
                current, record, events = await asyncio.to_thread(
                    service.read, identity, run["id"], cursor
                )
                for event in events:
                    cursor = event["sequence"]
                    yield event["type"], event["data"], current
                if current["status"] in TERMINAL and len(events) < 100:
                    if current["status"] != "succeeded" or record["result"] is None:
                        code = record["error_code"] or "model_request_failed"
                        status = (
                            429
                            if code
                            in {"rpm_exceeded", "tpm_exceeded", "concurrent_exceeded"}
                            else 402
                            if "budget" in code or "fund" in code or "balance" in code
                            else 502
                        )
                        raise PlatformError(
                            status, code, current["error"] or "模型请求未完成。"
                        )
                    yield "result", record["result"], current
                    return
                if time.monotonic() - last_keepalive > 10:
                    yield "keepalive", {}, current
                    last_keepalive = time.monotonic()
                await asyncio.sleep(0.1 if platform.settings.mode == "local" else 0.25)

        if not body.stream:
            last_renew = time.monotonic()
            try:
                async for kind, value, current in updates():
                    if time.monotonic() - last_renew > 10:
                        await renew()
                        last_renew = time.monotonic()
                    if kind == "result":
                        return JSONResponse(
                            response_result(current, value)
                            if responses
                            else chat_result(current, value),
                            headers={"X-Request-ID": run["id"]},
                        )
            finally:
                await release()

        async def stream():
            sequence = 0

            def frame(value):
                nonlocal sequence
                if responses:
                    value["sequence_number"] = sequence
                    sequence += 1
                encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                return (
                    f"event: {value['type']}\n" if responses else ""
                ) + f"data: {encoded}\n\n"

            def chunk(delta, finish_reason=None, usage=None):
                return {
                    "id": "chatcmpl-" + run["id"],
                    "object": "chat.completion.chunk",
                    "created": int(
                        datetime.fromisoformat(run["created_at"]).timestamp()
                    ),
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish_reason,
                            "logprobs": None,
                        }
                    ],
                    "usage": usage,
                }

            message_id = "msg_" + run["id"]
            opened = False
            text = ""
            try:
                if responses:
                    yield frame(
                        {"type": "response.created", "response": response_result(run)}
                    )
                    yield frame(
                        {
                            "type": "response.in_progress",
                            "response": response_result(run),
                        }
                    )
                else:
                    yield frame(chunk({"role": "assistant", "content": ""}))
                async for kind, value, current in updates():
                    if kind == "keepalive":
                        yield ": keepalive\n\n"
                    elif kind == "message.delta":
                        delta = value["text"]
                        text += delta
                        if responses:
                            if not opened:
                                opened = True
                                yield frame(
                                    {
                                        "type": "response.output_item.added",
                                        "output_index": 0,
                                        "item": {
                                            "id": message_id,
                                            "type": "message",
                                            "role": "assistant",
                                            "status": "in_progress",
                                            "content": [],
                                        },
                                    }
                                )
                                yield frame(
                                    {
                                        "type": "response.content_part.added",
                                        "item_id": message_id,
                                        "output_index": 0,
                                        "content_index": 0,
                                        "part": {
                                            "type": "output_text",
                                            "text": "",
                                            "annotations": [],
                                            "logprobs": [],
                                        },
                                    }
                                )
                            yield frame(
                                {
                                    "type": "response.output_text.delta",
                                    "item_id": message_id,
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": delta,
                                    "logprobs": [],
                                }
                            )
                        else:
                            yield frame(chunk({"content": delta}))
                    elif kind == "result":
                        if responses:
                            result = response_result(current, value)
                            for i, item in enumerate(result["output"]):
                                if item["type"] == "message":
                                    part = item["content"][0]
                                    yield frame(
                                        {
                                            "type": "response.output_text.done",
                                            "item_id": item["id"],
                                            "output_index": i,
                                            "content_index": 0,
                                            "text": part["text"],
                                            "logprobs": [],
                                        }
                                    )
                                    yield frame(
                                        {
                                            "type": "response.content_part.done",
                                            "item_id": item["id"],
                                            "output_index": i,
                                            "content_index": 0,
                                            "part": part,
                                        }
                                    )
                                else:
                                    yield frame(
                                        {
                                            "type": "response.output_item.added",
                                            "output_index": i,
                                            "item": {
                                                **item,
                                                "status": "in_progress",
                                                "arguments": "",
                                            },
                                        }
                                    )
                                    yield frame(
                                        {
                                            "type": "response.function_call_arguments.delta",
                                            "item_id": item["id"],
                                            "output_index": i,
                                            "delta": item["arguments"],
                                        }
                                    )
                                    yield frame(
                                        {
                                            "type": "response.function_call_arguments.done",
                                            "item_id": item["id"],
                                            "output_index": i,
                                            "arguments": item["arguments"],
                                        }
                                    )
                                yield frame(
                                    {
                                        "type": "response.output_item.done",
                                        "output_index": i,
                                        "item": item,
                                    }
                                )
                            yield frame(
                                {
                                    "type": "response.incomplete"
                                    if result["status"] == "incomplete"
                                    else "response.completed",
                                    "response": result,
                                }
                            )
                        else:
                            result = chat_result(current, value)
                            calls = result["choices"][0]["message"].get("tool_calls")
                            if calls:
                                yield frame(
                                    chunk(
                                        {
                                            "tool_calls": [
                                                {"index": i, **call}
                                                for i, call in enumerate(calls)
                                            ]
                                        }
                                    )
                                )
                            yield frame(chunk({}, value["finish_reason"]))
                            if (
                                body.stream_options
                                and body.stream_options.include_usage
                            ):
                                yield frame(
                                    {**chunk({}, usage=result["usage"]), "choices": []}
                                )
                            yield "data: [DONE]\n\n"
            except PlatformError as exc:
                if responses:
                    yield frame(
                        {
                            "type": "response.failed",
                            "response": {
                                **response_result(run, status="failed"),
                                "error": error_body(exc.code, exc.message)["error"],
                            },
                        }
                    )
                else:
                    yield frame(error_body(exc.code, exc.message))
                    yield "data: [DONE]\n\n"

        return leased_response(
            stream(),
            renew=renew,
            release=release,
            authorize=check,
            valid_until=time.monotonic() + STREAM_LEASE_SECONDS,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-Request-ID": run["id"],
            },
        )

    @app.post(
        "/v1/chat/completions",
        response_model=ChatCompletion,
        tags=["模型调用"],
        summary="创建 Chat Completion",
        description="支持文本消息和客户端函数工具；stream=true 使用 SSE，结束标识为 data: [DONE]。",
        responses={**MODEL_ERRORS, **STREAM_RESPONSE},
    )
    async def chat(request: Request, identity=Depends(bearer)):
        return await complete(request, identity, ChatRequest, False)

    @app.post(
        "/v1/responses",
        response_model=ModelApiResponse,
        tags=["模型调用"],
        summary="创建 Response",
        description="支持文本及客户端函数工具。调用方提供完整历史，store=false；stream=true 使用 SSE。",
        responses={**MODEL_ERRORS, **STREAM_RESPONSE},
    )
    async def responses(request: Request, identity=Depends(bearer)):
        return await complete(request, identity, ResponsesRequest, True)
