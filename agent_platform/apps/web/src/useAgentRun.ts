import { useCallback, useEffect, useRef, useState } from "react";
import { EventType } from "@ag-ui/core";
import type { AgentMessage, AgentTrace, RunAgentInput } from "./ag-ui";
import { agentStream, platformRun, reduceMessages } from "./ag-ui";
import { ApiError } from "./api";
import type { Run } from "./types";

type Target = {
  sessionId: string; runId?: string; input?: RunAgentInput; generation: number;
  accepted?: { resolve: (run: Run) => void; reject: (error: Error) => void };
};
type Connection = "closed" | "connecting" | "open" | "retrying";

function later(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
    const timer = setTimeout(finish, ms);
    signal.addEventListener("abort", finish, { once: true });
  });
}

function cursorAfter(next: string, previous: string) {
  const [row, part] = next.split(".").map(Number), [oldRow, oldPart] = previous.split(".").map(Number);
  return row > oldRow || (row === oldRow && part > oldPart);
}

export function useAgentRun(tenant: string, callbacks: {
  messages: (messages: AgentMessage[], runId: string) => void;
  finished: (sessionId: string, status: string, message?: string) => void;
}) {
  const handlers = useRef(callbacks);
  handlers.current = callbacks;
  const generation = useRef(0);
  const lastCursor = useRef("0.-1");
  const currentRunId = useRef("");
  const [target, setTarget] = useState<Target | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<AgentTrace[]>([]);
  const [connection, setConnection] = useState<Connection>("closed");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const reset = useCallback(() => {
    generation.current++;
    currentRunId.current = "";
    lastCursor.current = "0.-1";
    setTarget(null); setRun(null); setEvents([]); setConnection("closed"); setError("");
  }, []);
  const attach = useCallback((active: Run) => {
    const next = ++generation.current;
    lastCursor.current = "0.-1";
    currentRunId.current = active.id;
    setRun(active); setEvents([]); setError("");
    setTarget({ sessionId: active.session_id, runId: active.id, generation: next });
  }, []);
  const start = useCallback((input: RunAgentInput) => new Promise<Run>((resolve, reject) => {
    const next = ++generation.current;
    lastCursor.current = "0.-1";
    currentRunId.current = "";
    setRun(null); setEvents([]); setError("");
    setTarget({ sessionId: input.threadId, input, generation: next, accepted: { resolve, reject } });
  }), []);

  useEffect(() => {
    if (!target) return;
    const controller = new AbortController();
    const valid = () => !controller.signal.aborted && generation.current === target.generation;
    let acknowledged = !target.accepted;
    let messages: AgentMessage[] = [];
    let messagesDirty = false;
    let messageFrame: number | null = null;
    let status = "running";
    let protocolRunId = "";
    const seen = new Set<string>();
    const flushMessages = () => {
      if (messageFrame !== null) cancelAnimationFrame(messageFrame);
      messageFrame = null;
      if (messagesDirty && valid()) {
        messagesDirty = false;
        handlers.current.messages(messages, currentRunId.current);
      }
    };
    const scheduleMessages = () => {
      messagesDirty = true;
      // Decode and validate every event/cursor immediately; publish only the
      // latest reduced snapshot per paint, including bursts after reconnect.
      messageFrame ??= requestAnimationFrame(flushMessages);
    };
    const stopForScope = () => controller.abort();
    window.addEventListener("tenant-scope-changed", stopForScope);
    const consume = async () => {
      let retries = 0;
      setConnection("connecting");
      while (valid()) {
        try {
          const runId = currentRunId.current || target.runId;
          await agentStream({
            path: runId ? `/runs/${encodeURIComponent(runId)}/ag-ui` : `/sessions/${encodeURIComponent(target.sessionId)}/ag-ui`,
            input: runId ? undefined : target.input,
            cursor: lastCursor.current === "0.-1" ? undefined : lastCursor.current,
            tenant, signal: controller.signal,
            opened: (id) => {
              if (!valid()) return;
              if (currentRunId.current && currentRunId.current !== id) throw new ApiError("运行标识不一致。", 502, "ag_ui_invalid_event");
              currentRunId.current = id;
              if (valid()) setConnection("open");
            },
            receive: ({ cursor, event }) => {
              if (!valid()) return;
              if (cursor && !cursorAfter(cursor, lastCursor.current)) return;
              if (event.type === EventType.RUN_STARTED) {
                if (event.threadId !== target.sessionId || (target.input && event.runId !== target.input.runId) || (protocolRunId && event.runId !== protocolRunId)) throw new ApiError("AG-UI 运行上下文不一致。", 502, "ag_ui_invalid_event");
                protocolRunId = event.runId;
              }
              if (event.type === EventType.RUN_FINISHED && (event.threadId !== target.sessionId || event.runId !== protocolRunId)) throw new ApiError("AG-UI 终结事件不属于当前运行。", 502, "ag_ui_invalid_event");
              // Advance only after validation and reduction succeed. A failed
              // frame must be replayable rather than silently lost.
              const nextRun = platformRun(event);
              if (nextRun) {
                if (nextRun.id !== currentRunId.current || nextRun.tenant_id !== tenant || nextRun.session_id !== target.sessionId) throw new ApiError("运行上下文不一致。", 502, "ag_ui_invalid_event");
                status = nextRun.status;
                setRun(nextRun);
                if (!acknowledged) { acknowledged = true; target.accepted?.resolve(nextRun); }
              }
              const updated = reduceMessages(messages, event);
              const changed = updated !== messages;
              messages = updated;
              if (changed) scheduleMessages();
              if (cursor) { lastCursor.current = cursor; retries = 0; }
              setConnection("open"); setError("");
              const key = cursor ?? (event.type === EventType.STATE_SNAPSHOT ? `state:${status}` : event.type);
              if (![EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_START, EventType.TEXT_MESSAGE_END,
                EventType.REASONING_MESSAGE_CONTENT, EventType.REASONING_MESSAGE_START, EventType.REASONING_MESSAGE_END,
                EventType.MESSAGES_SNAPSHOT, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END].includes(event.type) && !seen.has(key)) {
                seen.add(key);
                setEvents((previous) => [...previous.filter((item) => item.id !== key).slice(-79), { id: key, event }]);
              }
              if (event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR) {
                if (!acknowledged) throw new ApiError("运行缺少状态快照。", 502, "ag_ui_invalid_event");
                // Do not leave the final characters waiting for a background
                // tab's animation frame or discard them on completion.
                flushMessages();
                setRun(null); setConnection("closed");
                if (event.type === EventType.RUN_ERROR) { status = "failed"; setError(event.message); }
                else if (event.outcome?.type === "cancelled") status = "cancelled";
                handlers.current.finished(target.sessionId, status, event.type === EventType.RUN_ERROR ? event.message : undefined);
              }
            },
          });
          return;
        } catch (cause) {
          if (!valid()) return;
          flushMessages();
          const failure = cause instanceof Error ? cause : new Error("实时连接未完成。");
          // A rejected initial request keeps the draft. Once the server has
          // acknowledged a run, reconnect with GET; never submit it again.
          const retryable = failure instanceof ApiError && (failure.status === 0 || failure.status === 429 || failure.status >= 500) && failure.code !== "ag_ui_invalid_event" && failure.code !== "scope_changed";
          if (!currentRunId.current || !retryable) {
            setError(failure.message); setConnection("closed");
            if (!acknowledged) { acknowledged = true; target.accepted?.reject(failure); }
            return;
          }
          setConnection("retrying");
          await later(Math.min(1000 * 2 ** retries++, 10000), controller.signal);
        }
      }
    };
    void consume();
    return () => {
      controller.abort();
      if (messageFrame !== null) cancelAnimationFrame(messageFrame);
      window.removeEventListener("tenant-scope-changed", stopForScope);
      if (!acknowledged) target.accepted?.reject(new ApiError("请求已取消或组织已切换。", 0, "scope_changed"));
    };
  }, [target, attempt, tenant]);

  const reconnect = useCallback(() => { setError(""); setAttempt((value) => value + 1); }, []);
  return { run, events, connection, error, start, attach, reset, reconnect };
}
