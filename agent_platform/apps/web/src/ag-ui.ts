import { EventType } from "@ag-ui/core";
import type { AGUIEvent, Message, RunAgentInput } from "@ag-ui/core";
import { EventSchema } from "@ag-ui/core/schemas";
import { z } from "zod";
import { ApiError, withApiResponse } from "./api";
import type { Run } from "./types";

export type { AGUIEvent, Message as AgentMessage, RunAgentInput };
export type AgentTrace = { id: string; event: AGUIEvent };
export type AgentFrame = { cursor: string | null; event: AGUIEvent };

export function messageRunId(message: Message): string | undefined {
  const platform: unknown = message.metadata?.platform;
  if (platform && typeof platform === "object" && "run_id" in platform && typeof platform.run_id === "string") return platform.run_id;
  return message.id.startsWith("assistant:") ? message.id.split(":")[1] : undefined;
}

const runState = z.object({ run: z.object({
  id: z.string(), session_id: z.string(), tenant_id: z.string(), user_id: z.string(),
  agent_name: z.string(), created_at: z.string(), status: z.string(), error: z.string(),
  model_id: z.string().nullable(), model_alias: z.string().nullable(), finished_at: z.string().nullable(),
}) });

export function platformRun(event: AGUIEvent): Run | null {
  if (event.type !== EventType.STATE_SNAPSHOT) return null;
  const result = runState.safeParse(event.snapshot);
  if (!result.success) throw new ApiError("运行状态格式无效，请重新连接。", 502, "ag_ui_invalid_event");
  return result.data.run;
}

// SSE data can span network chunks, UTF-8 code points and multiple data lines.
// No provider-specific event name or JSON envelope is accepted here.
export async function* decodeAgentEvents(body: ReadableStream<Uint8Array>, received?: () => void): AsyncGenerator<AgentFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "", data: string[] = [], cursor: string | null = null;
  const parse = (): AgentFrame | null => {
    if (!data.length) { cursor = null; return null; }
    let value: unknown;
    try { value = JSON.parse(data.join("\n")); }
    catch { throw new ApiError("收到无效的 AG-UI 数据，请重新连接。", 502, "ag_ui_invalid_event"); }
    const parsed = EventSchema.safeParse(value);
    if (!parsed.success) throw new ApiError("收到不符合 AG-UI 协议的事件。", 502, "ag_ui_invalid_event");
    const frame = { cursor, event: parsed.data };
    data = []; cursor = null;
    return frame;
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (!done) received?.();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      while (true) {
        const lineEnd = buffer.search(/[\r\n]/);
        if (lineEnd < 0 || (!done && buffer[lineEnd] === "\r" && lineEnd === buffer.length - 1)) break;
        const line = buffer.slice(0, lineEnd);
        const separator = buffer[lineEnd] === "\r" && buffer[lineEnd + 1] === "\n" ? 2 : 1;
        buffer = buffer.slice(lineEnd + separator);
        if (!line) { const frame = parse(); if (frame) yield frame; continue; }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":"), field = colon < 0 ? line : line.slice(0, colon);
        const content = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "data") data.push(content);
        if (field === "id") {
          if (!/^\d+\.\d+$/.test(content)) throw new ApiError("事件游标格式无效。", 502, "ag_ui_invalid_event");
          cursor = content;
        }
      }
      // An unterminated frame is not committed. Resume from the last fully
      // decoded cursor instead of treating a truncated response as success.
      if (done) return;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export async function agentStream(options: {
  path: string; tenant: string; signal: AbortSignal; input?: RunAgentInput; cursor?: string;
  opened: (runId: string) => void; receive: (frame: AgentFrame) => void;
}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal.addEventListener("abort", abort, { once: true });
  if (options.signal.aborted) controller.abort();
  let idle = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const pulse = () => {
    clearTimeout(timer);
    // The server sends a heartbeat every 10 seconds. A silent connection is
    // retried even when the network never reports a clean EOF or TCP error.
    timer = setTimeout(() => { idle = true; controller.abort(); }, 30000);
  };
  pulse();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (options.cursor) headers["Last-Event-ID"] = options.cursor;
  if (options.input) headers["Idempotency-Key"] = options.input.runId;
  try {
    await withApiResponse(options.path, {
      method: options.input ? "POST" : "GET", headers, signal: controller.signal,
      body: options.input ? JSON.stringify(options.input) : undefined,
    }, options.tenant, async (response) => {
      if (!response.headers.get("content-type")?.includes("text/event-stream") || !response.body) {
        throw new ApiError("服务未返回 AG-UI 事件流。", 502, "ag_ui_invalid_event");
      }
      const runId = response.headers.get("x-run-id");
      if (!runId) throw new ApiError("服务未确认运行标识。", 502, "ag_ui_invalid_event");
      options.opened(runId);
      let started = false;
      for await (const frame of decodeAgentEvents(response.body, pulse)) {
        if ((!started && frame.event.type !== EventType.RUN_STARTED) || (started && frame.event.type === EventType.RUN_STARTED)) {
          throw new ApiError("AG-UI 运行生命周期顺序无效。", 502, "ag_ui_invalid_event");
        }
        started = true;
        options.receive(frame);
        if (frame.event.type === EventType.RUN_FINISHED || frame.event.type === EventType.RUN_ERROR) return;
      }
      throw new ApiError("实时连接已中断，正在恢复。", 0, "ag_ui_eof");
    });
  } catch (error) {
    if (idle && !options.signal.aborted) throw new ApiError("实时连接超时，正在恢复。", 0, "ag_ui_timeout");
    throw error;
  } finally {
    clearTimeout(timer);
    options.signal.removeEventListener("abort", abort);
  }
}

export function reduceMessages(messages: Message[], event: AGUIEvent): Message[] {
  switch (event.type) {
    case EventType.MESSAGES_SNAPSHOT:
      // These roles may exist only in the stream. Omission says nothing about
      // them; an explicit set for that role replaces its previous messages.
      return [...event.messages, ...messages.filter((message) =>
        (message.role === "reasoning" || message.role === "activity") && !event.messages.some((incoming) => incoming.role === message.role))];
    case EventType.REASONING_MESSAGE_START: {
      const existing = messages.find((message) => message.id === event.messageId);
      if (existing && existing.role !== "reasoning") throw new ApiError("推理消息标识与其他消息冲突。", 502, "ag_ui_invalid_event");
      return existing ? messages : [...messages, {
        id: event.messageId, role: "reasoning", content: "", metadata: event.metadata ?? { platform: { reasoning_status: "streaming" } },
      }];
    }
    case EventType.REASONING_MESSAGE_CONTENT:
      if (!messages.some((message) => message.id === event.messageId && message.role === "reasoning")) throw new ApiError("推理内容缺少开始事件。", 502, "ag_ui_invalid_event");
      return messages.map((message) => message.id === event.messageId && message.role === "reasoning"
        ? { ...message, content: message.content + event.delta } : message);
    case EventType.REASONING_MESSAGE_END:
    case EventType.REASONING_END:
      return messages.map((message) => message.id === event.messageId && message.role === "reasoning" ? {
        ...message, metadata: { ...message.metadata, ...event.metadata, platform: event.metadata?.platform ?? { reasoning_status: "complete" } },
      } : message);
    case EventType.ACTIVITY_SNAPSHOT:
      return [...messages.filter((message) => message.id !== event.messageId), {
        id: event.messageId, role: "activity", activityType: event.activityType, content: event.content,
      }];
    case EventType.TEXT_MESSAGE_START:
      return messages.some((message) => message.id === event.messageId) ? messages : [
        ...messages, { id: event.messageId, role: event.role ?? "assistant", content: "" },
      ];
    case EventType.TEXT_MESSAGE_CONTENT:
      if (!messages.some((message) => message.id === event.messageId)) throw new ApiError("消息内容缺少开始事件。", 502, "ag_ui_invalid_event");
      return messages.map((message) => {
        if (message.id !== event.messageId) return message;
        if (message.role === "activity" || message.role === "tool" || message.role === "reasoning" || (message.content != null && typeof message.content !== "string")) throw new ApiError("消息类型与文本事件不一致。", 502, "ag_ui_invalid_event");
        return { ...message, content: `${message.content ?? ""}${event.delta}` };
      });
    case EventType.TOOL_CALL_START: {
      const parent = event.parentMessageId ?? `assistant:tool:${event.toolCallId}`;
      const list = messages.some((message) => message.id === parent) ? messages : [...messages, { id: parent, role: "assistant" as const, content: "" }];
      return list.map((message) => message.id === parent && message.role === "assistant" ? {
        ...message, toolCalls: [...(message.toolCalls ?? []), { id: event.toolCallId, type: "function", function: { name: event.toolCallName, arguments: "" } }],
      } : message);
    }
    case EventType.TOOL_CALL_ARGS:
      return messages.map((message) => message.role === "assistant" ? { ...message, toolCalls: message.toolCalls?.map((call) => call.id === event.toolCallId ? {
        ...call, function: { ...call.function, arguments: call.function.arguments + event.delta },
      } : call) } : message);
    case EventType.TOOL_CALL_RESULT:
      return [...messages.filter((message) => message.id !== event.messageId), { id: event.messageId, role: "tool", toolCallId: event.toolCallId, content: event.content }];
    default: return messages;
  }
}
