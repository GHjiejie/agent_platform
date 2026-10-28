"""AG-UI 1.0 boundary over the platform's durable, fenced execution log.

The protocol describes UI events, not provider requests. Published agent specs,
conversation history, tool authorization and billing remain server owned.
"""

import copy
import json
import re
from datetime import datetime

from ag_ui.core import (
    ActivitySnapshotEvent,
    BaseEvent,
    CustomEvent,
    Message,
    MessagesSnapshotEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.execution_policy import ReasoningEffort
from agent_platform.modules.reasoning import reasoning_message, session_reasoning
from agent_platform.modules.runtime.generative_ui import UI_ACTIVITY, activity_content

PROTOCOL_VERSION = "1.0"
TERMINAL_EVENTS = {
    "run.completed",
    "run.succeeded",
    "run.cancelled",
    "run.canceled",
    "run.failed",
    "run.expired",
    "run.interrupted",
}
CURSOR = re.compile(r"^(0|[1-9][0-9]{0,11})\.(0|[1-9][0-9]{0,2})$")


class ForwardedProps(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    model_id: str | None = Field(
        default=None, alias="modelId", min_length=1, max_length=64
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None, alias="reasoningEffort"
    )


class PlatformRunInput(RunAgentInput):
    """The supported AG-UI profile, also published in OpenAPI."""

    model_config = ConfigDict(extra="forbid")
    thread_id: str = Field(alias="threadId", min_length=1, max_length=64)
    run_id: str = Field(alias="runId", min_length=1, max_length=64, pattern=r"^[\w-]+$")
    messages: list[Message] = Field(min_length=1, max_length=2000)
    forwarded_props: ForwardedProps = Field(
        default_factory=ForwardedProps, alias="forwardedProps"
    )

    @model_validator(mode="after")
    def supported_input(self):
        if self.protocol_version not in (None, PROTOCOL_VERSION):
            raise ValueError("只支持 AG-UI 1.0。")
        if (
            self.tools
            or self.context
            or self.state
            or self.resume
            or self.parent_run_id
        ):
            raise ValueError(
                "此 Agent 使用服务端工具、上下文和状态，不接受客户端覆盖或中断恢复。"
            )
        return self


def enqueue(
    platform,
    user: dict,
    session_id: str,
    body: PlatformRunInput,
    resume_sequence: int = 0,
) -> dict:
    if body.thread_id != session_id:
        raise PlatformError(400, "thread_mismatch", "threadId 必须与当前会话一致。")
    # Prior client messages are a UI cache, never authoritative model context.
    # The first snapshot replaces them with the persisted, authorized history.
    messages = body.messages
    last = messages[-1]
    role = last.role
    content = getattr(last, "content", None)
    if (
        role != "user"
        or not isinstance(content, str)
        or not content.strip()
        or len(content) > 32000
    ):
        raise PlatformError(
            422, "invalid_message", "最后一条消息必须为 1–32000 字的用户文本。"
        )
    if any(
        message.role in {"system", "developer", "reasoning"} for message in messages
    ):
        raise PlatformError(
            422, "server_owned_context", "系统提示和推理上下文由已发布的 Agent 管理。"
        )
    ids = [message.id for message in messages]
    if any(not identifier or len(identifier) > 128 for identifier in ids) or len(
        set(ids)
    ) != len(ids):
        raise PlatformError(
            422, "invalid_message_id", "消息标识不能为空、重复或超过 128 字符。"
        )
    return platform.enqueue(
        user,
        session_id,
        content.strip(),
        f"ag-ui:{body.run_id}",
        body.forwarded_props.model_id,
        reasoning_effort=body.forwarded_props.reasoning_effort,
        ag_ui={"run_id": body.run_id, "message_id": last.id},
        admit_stream=True,
        resume_sequence=resume_sequence,
    )


def read_projection(platform, user: dict, run_id: str):
    """Read only owned history up to this run, excluding later conversation turns."""
    with platform.db.tenant_scope(user["tenant_id"]), platform.db.read() as connection:
        run = platform.owned(connection, t.runs, user, run_id, own=True)
        rows = connection.execute(
            select(t.messages, t.runs.c.spec.label("run_spec"))
            .join(
                t.runs,
                (t.runs.c.id == t.messages.c.run_id)
                & (t.runs.c.tenant_id == t.messages.c.tenant_id),
            )
            .where(
                t.messages.c.tenant_id == user["tenant_id"],
                t.messages.c.session_id == run["session_id"],
                t.runs.c.created_at <= run["created_at"],
            )
            .order_by(t.messages.c.created_at, t.messages.c.id)
        ).mappings()
        history = []
        history_order = {}
        for row in rows:
            if row["role"] not in {"user", "assistant"}:
                continue
            if row["run_id"] == run_id and row["role"] == "assistant":
                continue
            correlation = row["run_spec"].get("ag_ui", {})
            identifier = (
                correlation.get("message_id", row["id"])
                if row["role"] == "user"
                else f"assistant:{row['run_id']}"
            )
            history.append(
                {
                    "id": identifier,
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": {"platform": {"run_id": row["run_id"]}},
                }
            )
            history_order[identifier] = row["created_at"]
        for trace in session_reasoning(
            connection,
            user,
            run["session_id"],
            before=run["created_at"],
            exclude_run_id=run_id,
        ):
            history.append(reasoning_message(trace))
            history_order[trace["id"]] = trace["created_at"]
        history.sort(key=lambda message: (history_order[message["id"]], message["id"]))
    return Projection(run, platform.public_run(run), history)


def parse_cursor(value: str | None) -> tuple[int, int]:
    if not value or value == "0":
        return (0, -1)
    if not CURSOR.fullmatch(value):
        raise PlatformError(400, "invalid_cursor", "AG-UI 事件游标无效。")
    sequence, part = value.split(".")
    return int(sequence), int(part)


def encode(event: BaseEvent, cursor: str | None = None) -> str:
    # AG-UI uses unnamed SSE data frames; transport ids are replay cursors.
    frame = EventEncoder().encode(event)
    return f"id: {cursor}\n{frame}" if cursor is not None else frame


class Projection:
    def __init__(self, raw_run: dict, public_run: dict, history: list[dict]):
        self.run_id = (
            raw_run.get("spec", {}).get("ag_ui", {}).get("run_id", raw_run["id"])
        )
        self.run = public_run
        self.history = copy.deepcopy(history)
        self.messages = copy.deepcopy(history)
        self.open_message: str | None = None
        self.open_reasoning: str | None = None
        self.call_id = "output"
        self.step: str | None = None
        self.terminal: BaseEvent | None = None

    def prefix(self) -> list[BaseEvent]:
        return [
            RunStartedEvent(
                thread_id=self.run["session_id"],
                run_id=self.run_id,
                protocol_version=PROTOCOL_VERSION,
            ),
            self.state(),
            MessagesSnapshotEvent(messages=self.messages),
        ]

    def state(self) -> StateSnapshotEvent:
        return StateSnapshotEvent(snapshot={"run": self.run})

    def close_text(self) -> list[BaseEvent]:
        if not self.open_message:
            return []
        identifier, self.open_message = self.open_message, None
        return [TextMessageEndEvent(message_id=identifier)]

    def close_reasoning(self, status: str = "complete") -> list[BaseEvent]:
        if not self.open_reasoning:
            return []
        identifier, self.open_reasoning = self.open_reasoning, None
        return [
            ReasoningMessageEndEvent(message_id=identifier),
            ReasoningEndEvent(
                message_id=identifier,
                metadata={"platform": {"reasoning_status": status}},
            ),
        ]

    def project(self, row: dict) -> list[BaseEvent]:
        kind, data = row["type"], row["data"]
        events: list[BaseEvent] = []
        if kind == "run.queued":
            events = [CustomEvent(name="platform.queued", value={"status": "queued"})]
        elif kind in {"run.started", "run.cancelling"}:
            self.run = {**self.run, "status": data.get("status", "running")}
            events = [self.state()]
        elif kind == "step.started":
            self.step = f"step-{data['step']}"
            events = [StepStartedEvent(step_name=self.step)]
        elif kind == "step.finished":
            events = self.close_text() + self.close_reasoning()
            events.append(StepFinishedEvent(step_name=f"step-{data['step']}"))
            self.step = None
        elif kind == "model.started":
            events = self.close_text() + self.close_reasoning()
            self.call_id = data.get("call_id", "output")
            events.append(
                CustomEvent(name="platform.model", value={"model": data.get("model")})
            )
        elif kind in {"reasoning.started", "reasoning.delta"}:
            identifier = (
                f"reasoning:{self.run['id']}:{data.get('call_id', self.call_id)}"
            )
            if self.open_reasoning != identifier:
                events = self.close_text() + self.close_reasoning()
                self.open_reasoning = identifier
                events.extend(
                    [
                        ReasoningStartEvent(message_id=identifier),
                        ReasoningMessageStartEvent(
                            message_id=identifier,
                            role="reasoning",
                            metadata={"platform": {"reasoning_status": "streaming"}},
                        ),
                    ]
                )
            if kind == "reasoning.delta" and data.get("text"):
                events.append(
                    ReasoningMessageContentEvent(
                        message_id=identifier, delta=data["text"]
                    )
                )
        elif kind == "reasoning.finished":
            events = self.close_reasoning()
        elif kind == "message.delta" and data.get("text"):
            events = self.close_reasoning()
            identifier = (
                f"assistant:{self.run['id']}:{data.get('call_id', self.call_id)}"
            )
            if self.open_message != identifier:
                events += self.close_text()
                self.open_message = identifier
                events.append(
                    TextMessageStartEvent(message_id=identifier, role="assistant")
                )
            events.append(
                TextMessageContentEvent(message_id=identifier, delta=data["text"])
            )
        elif kind == "tool.started":
            events = self.close_text() + self.close_reasoning()
            parent = f"assistant:{self.run['id']}:{self.call_id}"
            tool_id = f"{self.run['id']}:{self.call_id}:{data['id']}"
            events.extend(
                [
                    ToolCallStartEvent(
                        tool_call_id=tool_id,
                        tool_call_name=data["name"],
                        parent_message_id=parent,
                    ),
                    ToolCallArgsEvent(
                        tool_call_id=tool_id,
                        delta=json.dumps(data.get("args", {}), ensure_ascii=False),
                    ),
                    ToolCallEndEvent(tool_call_id=tool_id),
                ]
            )
        elif kind == "tool.finished":
            output = {
                key: value for key, value in data.items() if key not in {"id", "name"}
            }
            tool_id = f"{self.run['id']}:{self.call_id}:{data['id']}"
            events = [
                ToolCallResultEvent(
                    message_id=f"tool:{tool_id}",
                    tool_call_id=tool_id,
                    content=json.dumps(output, ensure_ascii=False),
                    role="tool",
                )
            ]
        elif kind == "artifact.created":
            events = [
                ActivitySnapshotEvent(
                    message_id=f"artifact:{self.run['id']}:{data['id']}",
                    activity_type=UI_ACTIVITY,
                    content=activity_content(data["content"]),
                    replace=True,
                )
            ]
        elif kind == "model.output_limit":
            events = [CustomEvent(name="platform.output_limit", value=data)]
        elif kind == "usage.updated":
            # Customer-visible receipts only; provider cost and raw/encrypted
            # response items are never part of the UI protocol.
            events = [
                CustomEvent(
                    name="platform.usage",
                    value={
                        key: data[key]
                        for key in (
                            "call_id",
                            "input_tokens",
                            "output_tokens",
                            "cost",
                            "status",
                        )
                        if key in data
                    },
                )
            ]
        elif kind in TERMINAL_EVENTS:
            status = data.get("status", kind.removeprefix("run."))
            if status == "completed":
                status = "succeeded"
            reasoning_status = "complete" if status == "succeeded" else "interrupted"
            events = self.close_text() + self.close_reasoning(reasoning_status)
            self.run = {**self.run, "status": status, "error": data.get("error", "")}
            if self.step:
                events.append(
                    StepFinishedEvent(
                        step_name=self.step, metadata={"platform": {"status": status}}
                    )
                )
                self.step = None
            messages = copy.deepcopy(self.history)
            prior_ids = {message["id"] for message in messages}
            for message in self.messages:
                if message["role"] == "reasoning" and message["id"] not in prior_ids:
                    visible = copy.deepcopy(message)
                    if ((visible.get("metadata") or {}).get("platform") or {}).get(
                        "reasoning_status"
                    ) != "complete":
                        visible["metadata"] = {
                            "platform": {"reasoning_status": reasoning_status}
                        }
                    messages.append(visible)
            messages.extend(
                copy.deepcopy(
                    [
                        message
                        for message in self.messages
                        if message["role"] == "activity"
                    ]
                )
            )
            if status == "succeeded":
                messages.append(
                    {
                        "id": f"assistant:{self.run['id']}",
                        "role": "assistant",
                        "content": data.get("content", ""),
                        "metadata": {"platform": {"run_id": self.run["id"]}},
                    }
                )
            events.extend([MessagesSnapshotEvent(messages=messages), self.state()])
            if status in {"succeeded", "cancelled", "canceled"}:
                self.terminal = RunFinishedEvent(
                    thread_id=self.run["session_id"],
                    run_id=self.run_id,
                    outcome={
                        "type": "cancelled"
                        if status in {"cancelled", "canceled"}
                        else "success"
                    },
                    result={"runId": self.run["id"], "status": status},
                )
            else:
                self.terminal = RunErrorEvent(
                    message=data.get("error") or "Agent 运行未完成。",
                    code=f"RUN_{status.upper()}",
                )
            events.append(self.terminal)
        if row.get("created_at"):
            stamp = int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)
            for event in events:
                event.timestamp = stamp
        return events

    def apply(self, event: BaseEvent):
        """Reconstruct the snapshot at an exact sub-event cursor on reconnect."""
        if isinstance(event, ActivitySnapshotEvent):
            self.messages = [m for m in self.messages if m["id"] != event.message_id]
            self.messages.append(
                {
                    "id": event.message_id,
                    "role": "activity",
                    "activityType": event.activity_type,
                    "content": copy.deepcopy(event.content),
                }
            )
        elif isinstance(event, MessagesSnapshotEvent):
            self.messages = [
                message.model_dump(by_alias=True) for message in event.messages
            ]
        elif isinstance(event, ReasoningMessageStartEvent):
            if not any(message["id"] == event.message_id for message in self.messages):
                self.messages.append(
                    {
                        "id": event.message_id,
                        "role": "reasoning",
                        "content": "",
                        "metadata": event.metadata or {},
                    }
                )
        elif isinstance(event, ReasoningMessageContentEvent):
            message = next(
                message
                for message in self.messages
                if message["id"] == event.message_id
            )
            message["content"] += event.delta
        elif isinstance(event, ReasoningEndEvent):
            for message in self.messages:
                if message["id"] == event.message_id and message["role"] == "reasoning":
                    message["metadata"] = event.metadata or {
                        "platform": {"reasoning_status": "complete"}
                    }
        elif isinstance(event, TextMessageStartEvent):
            if not any(message["id"] == event.message_id for message in self.messages):
                self.messages.append(
                    {"id": event.message_id, "role": event.role, "content": ""}
                )
        elif isinstance(event, TextMessageContentEvent):
            message = next(
                message
                for message in self.messages
                if message["id"] == event.message_id
            )
            message["content"] += event.delta
        elif isinstance(event, ToolCallStartEvent):
            message = next(
                (
                    message
                    for message in self.messages
                    if message["id"] == event.parent_message_id
                ),
                None,
            )
            if message is None:
                message = {
                    "id": event.parent_message_id,
                    "role": "assistant",
                    "content": "",
                }
                self.messages.append(message)
            message.setdefault("toolCalls", []).append(
                {
                    "id": event.tool_call_id,
                    "type": "function",
                    "function": {"name": event.tool_call_name, "arguments": ""},
                }
            )
        elif isinstance(event, ToolCallArgsEvent):
            for message in self.messages:
                for call in message.get("toolCalls", []):
                    if call["id"] == event.tool_call_id:
                        call["function"]["arguments"] += event.delta
        elif isinstance(event, ToolCallResultEvent):
            self.messages.append(
                {
                    "id": event.message_id,
                    "role": "tool",
                    "toolCallId": event.tool_call_id,
                    "content": event.content,
                }
            )
