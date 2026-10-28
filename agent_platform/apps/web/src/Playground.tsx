import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertCircle, ArrowDown, ArrowUp, Bot, Brain, Check, ChevronRight, CircleStop,
  Copy, History, LoaderCircle, MessageSquare, Plus,
  Sparkles, Terminal, Wrench,
} from "lucide-react";
import { api, dateTime, getScopeRevision, getTenantId, scopeIsCurrent, tenantLink, terminal, write } from "./api";
import { Button, Modal, SearchBox, Select, useResource, useToast } from "./components";
import type { Agent, GeneratedArtifact, Model, ReasoningEffort, ReasoningTrace, Run, Session, SessionDetail } from "./types";
import type { AGUIEvent } from "./ag-ui";
import { messageRunId } from "./ag-ui";
import { useAgentRun } from "./useAgentRun";
import { Artifact } from "./Artifact";
import { Reasoning, reasoningTrace } from "./Reasoning";

const query = () => new URLSearchParams(location.hash.split("?")[1] ?? "");
const reasoningOptions: { value: ReasoningEffort; label: string }[] = [
  { value: "none", label: "不思考" },
  { value: "minimal", label: "最低" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
  { value: "xhigh", label: "很高" },
  { value: "max", label: "最高" },
];
export function Playground({ name }: { name?: string }) {
  const tenantScope = useRef(getTenantId()).current;
  const scopeRevision = useRef(getScopeRevision()).current;
  const agents = useResource<{ items: Agent[] }>("/agents");
  const models = useResource<{ items: Model[] }>("/models");
  const [modelId, setModelId] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | "">("");
  const sessions = useResource<{ items: Session[] }>("/sessions");
  const [agentId, setAgentId] = useState(query().get("agent") ?? "");
  const [sessionId, setSessionId] = useState<string | null>(
    query().get("session"),
  );
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [liveText, setLiveText] = useState("");
  const [liveReasoning, setLiveReasoning] = useState<ReasoningTrace[]>([]);
  const [liveArtifacts, setLiveArtifacts] = useState<GeneratedArtifact[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [error, setError] = useState("");
  const [showEvents, setShowEvents] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);
  const scrollFrame = useRef<number | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const following = useRef(true);
  const [showLatest, setShowLatest] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyQuery, setHistoryQuery] = useState("");
  const sendKey = useRef<{
    message: string;
    session: string;
    model: string;
    reasoning: ReasoningEffort | "";
    key: string;
    messageId: string;
  } | null>(null);
  const selection = useRef(0);
  const toast = useToast();
  const published =
    agents.data?.items.filter((agent) => agent.published_version) ?? [];
  const selectedAgent = agents.data?.items.find(
    (agent) => agent.id === (detail?.agent_id ?? agentId),
  );
  const latestRun = detail?.runs
    .slice()
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const { run: activeRun, events, connection, error: streamError, start, attach, reset, reconnect } = useAgentRun(tenantScope, {
    messages: (messages, runId) => {
      setLiveArtifacts(messages.flatMap((message) => message.role === "activity" && message.activityType === "GENERATIVE_UI"
        ? [{ id: message.id, run_id: runId, content: message.content }] : []));
      const livePrefix = `assistant:${runId}:`;
      const isLiveReply = (id: string) => id === `assistant:${runId}` || id.startsWith(livePrefix);
      setLiveReasoning(messages.flatMap((message) => {
        const trace = reasoningTrace(message);
        return trace?.run_id === runId ? [trace] : [];
      }));
      setLiveText(messages.filter((message) => message.role === "assistant" && isLiveReply(message.id))
        .map((message) => typeof message.content === "string" ? message.content : "").filter(Boolean).join("\n\n"));
      const confirmed = messages.flatMap((message) => ["user", "assistant"].includes(message.role) && typeof message.content === "string" && message.content && !isLiveReply(message.id)
        ? [{ id: message.id, run_id: messageRunId(message), role: message.role, content: message.content }] : []);
      setDetail((current) => {
        if (!current || current.messages.length === confirmed.length && current.messages.every((message, index) =>
          message.id === confirmed[index].id && message.run_id === confirmed[index].run_id && message.role === confirmed[index].role && message.content === confirmed[index].content)) return current;
        return { ...current, messages: confirmed };
      });
    },
    finished: (id, _status, message) => {
      setCanceling(false);
      if (message) setError(message);
      // Keep the last streamed text visible until its persisted replacement is
      // ready, so completion cannot briefly empty the assistant bubble.
      void loadSession(id, false).then((result) => { if (result) { setLiveText(""); setLiveArtifacts([]); setLiveReasoning([]); } });
      void sessions.reload();
    },
  });
  useEffect(() => {
    if (!agentId && published.length) setAgentId(published[0].id);
  }, [agentId, published]);
  useEffect(() => {
    const listener = () => {
      const next = query();
      setSessionId(next.get("session"));
      if (next.has("agent")) {
        setAgentId(next.get("agent")!);
        setModelId("");
        setReasoningEffort("");
        setSessionId(null);
      }
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  const loadSession = useCallback(async (id: string, updateRun = true) => {
    const current = ++selection.current;
    setLoading(true);
    setError("");
    try {
      const result = await api<SessionDetail>(`/sessions/${id}`, {}, tenantScope);
      if (current !== selection.current) return;
      setDetail(result);
      setAgentId(result.agent_id);
      if (updateRun) {
        const running = result.runs.find((run) => !terminal(run.status));
        if (running) attach(running);
      }
      return result;
    } catch (err) {
      if (current === selection.current) setError((err as Error).message);
    } finally {
      if (current === selection.current) setLoading(false);
    }
  }, [attach]);
  useEffect(() => {
    reset();
    setLiveText("");
    setLiveReasoning([]);
    setLiveArtifacts([]);
    setDetail(null);
    setError("");
    if (sessionId) void loadSession(sessionId);
    else {
      selection.current++;
      setLoading(false);
    }
    return () => {
      selection.current++;
    };
  }, [sessionId, loadSession, reset]);
  useLayoutEffect(() => {
    if (following.current && scrollFrame.current === null) scrollFrame.current = requestAnimationFrame(() => {
      scrollFrame.current = null;
      const element = viewport.current;
      if (element && following.current) element.scrollTop = element.scrollHeight;
    });
  }, [detail?.messages.length, detail?.reasoning?.length, liveText, liveReasoning, liveArtifacts, sessionId]);
  useEffect(() => () => {
    if (scrollFrame.current !== null) cancelAnimationFrame(scrollFrame.current);
    scrollFrame.current = null;
  }, []);
  useLayoutEffect(() => {
    if (!input.current) return;
    input.current.style.height = "auto";
    input.current.style.height = `${Math.min(input.current.scrollHeight, 180)}px`;
  }, [draft]);
  async function send(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!scopeIsCurrent(scopeRevision) || !message || activeRun || busy || loading || !agentId) return;
    setBusy(true);
    following.current = true;
    setShowLatest(false);
    setError("");
    try {
      let id = sessionId;
      if (!id) {
        const session = await write<Session>("/sessions", {
          agent_id: agentId,
          title: message.slice(0, 60),
        });
        if (!scopeIsCurrent(scopeRevision)) return;
        id = session.id;
        setSessionId(id);
        location.hash = tenantLink(`playground?session=${id}`, tenantScope);
        await sessions.reload();
        if (!scopeIsCurrent(scopeRevision)) return;
      }
      if (
        !sendKey.current ||
        sendKey.current.message !== message ||
        sendKey.current.session !== id ||
        sendKey.current.model !== modelId ||
        sendKey.current.reasoning !== reasoningEffort
      )
        sendKey.current = {
          message,
          session: id,
          model: modelId,
          reasoning: reasoningEffort,
          key: crypto.randomUUID(),
          messageId: crypto.randomUUID(),
        };
      const restored = await loadSession(id, false);
      if (!restored || !scopeIsCurrent(scopeRevision)) return;
      setLiveText("");
      setLiveReasoning([]);
      setLiveArtifacts([]);
      await start({
        threadId: id, runId: sendKey.current.key, protocolVersion: "1.0",
        messages: [{ id: sendKey.current.messageId, role: "user", content: message }],
        state: {}, tools: [], context: [], forwardedProps: { modelId: modelId || null, reasoningEffort: reasoningEffort || null },
      });
      if (!scopeIsCurrent(scopeRevision)) return;
      setDraft("");
      sendKey.current = null;
      setShowEvents(false);
    } catch (err) {
      if (scopeIsCurrent(scopeRevision)) setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    if (!activeRun || canceling) return;
    setCanceling(true);
    try {
      const run = await write<Run>(`/runs/${activeRun.id}/cancel`);
      toast("已请求取消，正在等待运行停止");
      // The AG-UI terminal event confirms the worker has stopped. Closing
      // the HTTP stream alone would only detach this browser.
      if (terminal(run.status)) setCanceling(false);
    } catch (err) {
      setError((err as Error).message);
      setCanceling(false);
    }
  }
  function newSession() {
    following.current = true;
    setShowLatest(false);
    setHistoryOpen(false);
    setShowEvents(false);
    setSessionId(null);
    setDetail(null);
    reset();
    selection.current++;
    setLiveText("");
    setLiveReasoning([]);
    setError("");
    setDraft("");
    setCanceling(false);
    location.hash = tenantLink(`playground${agentId ? `?agent=${agentId}` : ""}`, tenantScope);
  }
  function chooseSession(session: Session) {
    reset();
    selection.current++;
    following.current = true;
    setShowLatest(false);
    setHistoryOpen(false);
    setShowEvents(false);
    setSessionId(session.id);
    setModelId("");
    setDraft("");
    setCanceling(false);
    location.hash = tenantLink(`playground?session=${session.id}`, tenantScope);
  }
  const effectiveError = error || streamError || agents.error || models.error || (!activeRun && latestRun && ["failed", "expired", "interrupted"].includes(latestRun.status) ? latestRun.error : "");
  const hasMessages = Boolean(detail?.messages.length || activeRun || liveText || liveReasoning.length);
  const availableModels = models.data?.items.filter((model) => model.active) ?? [];
  const automaticModel = availableModels.find((model) => model.id === selectedAgent?.model_id) ?? availableModels.find((model) => model.is_default);
  const recentSessions = sessions.data?.items.filter((session) => (session.title || "新对话").toLocaleLowerCase().includes(historyQuery.toLocaleLowerCase())) ?? [];
  const suggestions = selectedAgent?.starter_prompts?.length ? selectedAgent.starter_prompts.slice(0, 3) : [
    "请介绍你可以帮我完成哪些工作", "帮我把一个想法整理成行动计划", "把复杂的问题解释得简单一些",
  ];
  const copyReply = useCallback((content: string) => {
    void navigator.clipboard.writeText(content).then(() => toast("回复已复制")).catch(() => toast("无法复制，请手动选择文本", "error"));
  }, [toast]);
  return <section className="pg-workspace" aria-label="Playground">
    <div className="pg-toolbar">
      <h1 className="sr-only">Playground</h1>
      <Select id="active-agent" className="pg-agent-picker" variant="quiet" icon={<Bot size={18} />} aria-label="当前 Agent"
        value={detail?.agent_id ?? agentId} disabled={Boolean(sessionId) || busy || Boolean(activeRun) || agents.loading}
        title={sessionId ? "新建对话后可切换 Agent" : "选择已发布的 Agent"} searchable
        onChange={(event) => { setAgentId(event.target.value); setModelId(""); setReasoningEffort(""); }}>
        <option value="" disabled>选择 Agent</option>
        {published.map((agent) => <option value={agent.id} key={agent.id} data-description={agent.description}>{agent.name}</option>)}
      </Select>
      <div className="pg-toolbar-actions">
        {(events.length > 0 || activeRun) && <button className="pg-toolbar-button" onClick={() => setShowEvents(true)} aria-label="运行详情">
          <Terminal size={17} /><span>运行详情</span>
        </button>}
        <button className="pg-toolbar-button" onClick={() => { setHistoryQuery(""); setHistoryOpen(true); void sessions.reload(); }} aria-label="历史对话">
          <History size={18} /><span>历史对话</span>
        </button>
        <button className="pg-toolbar-button pg-new-chat" onClick={newSession} disabled={busy} aria-label="新建对话">
          <Plus size={18} /><span>新建对话</span>
        </button>
      </div>
    </div>
    <div className={`pg-body ${hasMessages ? "has-messages" : "is-empty"}`}>
      <div ref={viewport} className="pg-messages" aria-live="polite" aria-busy={Boolean(activeRun) || loading}
        onScroll={(event) => {
          const element = event.currentTarget;
          following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
          setShowLatest(!following.current);
        }}>
        {loading && !detail ? <div className="pg-loading" role="status"><LoaderCircle size={21} className="spin" />正在恢复对话…</div>
          : !hasMessages ? <div className="pg-welcome">
            <div className="pg-greeting">你好{name ? `，${name}` : ""}</div>
            <h2>今天想聊些什么？</h2>
            {!agents.loading && !published.length && <p className="pg-empty-note">还没有已发布的 Agent。<a href={tenantLink("agents", tenantScope)}>先去创建并发布</a></p>}
          </div> : <div className="pg-message-list">
            <ConversationHistory messages={detail?.messages} artifacts={detail?.artifacts} runs={detail?.runs} reasoning={detail?.reasoning}
              hiddenRunId={activeRun?.id ?? liveReasoning[0]?.run_id} sessionId={sessionId} copy={copyReply} />
            {(activeRun || liveText || liveReasoning.length > 0 || liveArtifacts.length > 0) && <article className="pg-message assistant" aria-label={activeRun ? "Agent 正在回复" : "Agent 的回复"}>
              <span className="pg-assistant-icon"><Sparkles size={22} strokeWidth={1.7} /></span>
              <div className="pg-message-content">
                {liveReasoning.map((trace) => <Reasoning key={trace.id} trace={trace} />)}
                {liveText ? <div className="pg-message-text">{liveText}{activeRun && <span className="typing-cursor" />}</div>
                  : !liveReasoning.length && <div className="pg-thinking" role="status"><span className="thinking-dots"><i /><i /><i /></span>{activeRun?.status === "queued" ? "等待执行" : "正在思考"}</div>}
                {liveArtifacts.filter((artifact) => !detail?.artifacts?.some((saved) => saved.id === artifact.id)).map((artifact) =>
                  <Artifact key={artifact.id} content={artifact.content} runStatus={activeRun?.status ?? "succeeded"} />)}
              </div>
            </article>}
          </div>}
      </div>
      {showLatest && hasMessages && <button className="pg-jump-latest" aria-label="回到最新消息" onClick={() => {
        following.current = true; setShowLatest(false);
        if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
      }}><ArrowDown size={17} />最新消息</button>}
      <div className="pg-compose-area">
        {effectiveError && <div className="pg-notice error" role="alert"><AlertCircle size={17} /><span>{effectiveError}</span>
          <button onClick={() => { if (sessionId) void loadSession(sessionId); void agents.reload(); void models.reload(); }}>重新加载</button>
        </div>}
        {connection === "retrying" && <div className="pg-notice reconnect" role="status"><LoaderCircle size={16} className="spin" />
          <span>正在恢复连接，Agent 仍在运行。</span><button onClick={() => reconnect()}>立即重连</button>
        </div>}
        <form className="pg-composer" onSubmit={send}>
          <textarea ref={input} aria-label="发送给 Agent 的消息" placeholder={selectedAgent ? `向${selectedAgent.name}提问` : "选择一个 Agent，开始对话"}
            rows={2} value={draft} disabled={busy || loading || !agentId} maxLength={32000}
            onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault(); if (!activeRun) void send(event);
              }
            }} />
          <div className="pg-composer-toolbar">
            <div className="pg-run-settings">
            <Select id="run-model" variant="quiet" className="pg-model-picker" aria-label="本次使用模型" icon={<Sparkles size={15} />}
              value={modelId} disabled={busy || Boolean(activeRun) || models.loading} searchable onChange={(event) => setModelId(event.target.value)}>
              <option value="">{automaticModel ? `${automaticModel.name} · 自动` : selectedAgent?.model_id ? "跟随 Agent 设置" : "自动选择模型"}</option>
              {availableModels.map((model) => <option key={model.id} value={model.id} data-description={model.alias}>{model.name}{model.is_default ? " · 默认" : ""}</option>)}
            </Select>
            <Select id="run-reasoning" variant="quiet" className="pg-reasoning-picker" aria-label="本次思考强度" icon={<Brain size={15} />}
              value={reasoningEffort} disabled={busy || loading || Boolean(activeRun)} title="应用于下一次发送；自动沿用 Agent 或模型设置"
              onChange={(event) => {
                const value = event.target.value;
                const option = reasoningOptions.find((option) => option.value === value);
                if (value === "") setReasoningEffort("");
                else if (option) setReasoningEffort(option.value);
              }}>
              <option value="" data-description="沿用 Agent 或模型的默认思考设置">思考：自动</option>
              {reasoningOptions.map((option) => <option key={option.value} value={option.value}>思考：{option.label}</option>)}
            </Select>
            </div>
            <span className="pg-keyboard-hint">Enter 发送</span>
            {activeRun ? <Button type="button" variant="secondary" className="pg-stop" onClick={() => void cancel()} busy={canceling}
              title="停止生成不会免除已产生的费用"><CircleStop size={17} /><span>停止生成</span></Button>
              : <Button type="submit" className="pg-send" aria-label="发送消息" disabled={!draft.trim() || !agentId || loading} busy={busy}>
                {!busy && <ArrowUp size={21} />}
              </Button>}
          </div>
        </form>
        {!hasMessages && published.length > 0 && <div className="pg-suggestions" aria-label="推荐问题">
          {suggestions.map((prompt) => <button key={prompt} disabled={busy || loading} onClick={() => { setDraft(prompt); input.current?.focus(); }}>
            <MessageSquare size={15} /><span>{prompt}</span>
          </button>)}
        </div>}
        {!models.loading && models.data && !availableModels.length && <p className="pg-empty-note"><a href={tenantLink("models", tenantScope)}>暂无可用模型，请联系管理员授权</a></p>}
        <p className="pg-disclaimer">AI 的回答可能不准确，请核实关键信息。</p>
      </div>
    </div>
    {historyOpen && <Modal title="历史对话" close={() => setHistoryOpen(false)}>
      <div className="pg-history-body">
        <SearchBox className="pg-history-search" aria-label="搜索历史对话" placeholder="搜索对话" value={historyQuery} onChange={setHistoryQuery} />
        {sessions.error && <div className="pg-notice error" role="alert"><span>{sessions.error}</span><button onClick={() => void sessions.reload()}>重试</button></div>}
        {sessions.loading && !sessions.data ? <div className="pg-loading"><LoaderCircle size={20} className="spin" />正在加载…</div>
          : recentSessions.length ? <div className="pg-history-list">{recentSessions.map((session) => <button key={session.id} className={session.id === sessionId ? "selected" : ""} disabled={busy} onClick={() => chooseSession(session)}>
            <MessageSquare size={17} /><span><strong>{session.title || "新对话"}</strong><small>{dateTime(session.created_at)}</small></span><ChevronRight size={16} />
          </button>)}</div> : <div className="pg-history-empty">{historyQuery ? "没有找到相关对话，试试其他关键词。" : "开始聊天后，对话会保存在这里。"}</div>}
      </div>
    </Modal>}
    {showEvents && <Modal title="运行详情" close={() => setShowEvents(false)}>
      <div className="pg-run-details">
        <p className="pg-run-status">{activeRun ? connection === "retrying" ? "正在恢复实时连接" : "运行进行中" : "本次运行已结束"}</p>
        {events.length ? events.map((event) => <div className="pg-event" key={event.id}>
          {event.event.type.startsWith("TOOL_CALL_") ? <Wrench size={15} /> : event.event.type === "RUN_FINISHED" ? <Check size={15} /> : <span className="pg-event-dot" />}
          <span>{eventLabel(event.event)}</span>
        </div>) : <div className="pg-loading">等待 Agent 开始执行…</div>}
        <a className="pg-run-link" href={tenantLink("runs", tenantScope)}>查看运行记录<ChevronRight size={15} /></a>
      </div>
    </Modal>}
  </section>;
}
const ConversationHistory = memo(function ConversationHistory({ messages, artifacts, runs, reasoning, hiddenRunId, sessionId, copy }: {
  messages: SessionDetail["messages"] | undefined; artifacts: GeneratedArtifact[] | undefined; runs: Run[] | undefined;
  reasoning: ReasoningTrace[] | undefined; hiddenRunId: string | undefined;
  sessionId: string | null; copy: (content: string) => void;
}) {
  const runId = (message: SessionDetail["messages"][number]) => message.run_id ?? (message.id?.startsWith("assistant:") ? message.id.split(":")[1] : undefined);
  const traces = (message: SessionDetail["messages"][number]) => reasoning?.filter((trace) => trace.run_id !== hiddenRunId && trace.run_id === runId(message)) ?? [];
  return <>{messages?.filter((message) => ["user", "assistant"].includes(message.role)).map((message, index) => <Fragment
    key={message.id ?? `${sessionId}-${message.role}-${message.created_at ?? index}`}
    ><article
    className={`pg-message ${message.role}`} aria-label={message.role === "user" ? "你的消息" : "Agent 的回复"}>
    {message.role === "assistant" && <span className="pg-assistant-icon"><Sparkles size={22} strokeWidth={1.7} /></span>}
    <div className="pg-message-content">
      {message.role === "assistant" && traces(message).map((trace) => <Reasoning key={trace.id} trace={trace} />)}
      <div className="pg-message-text">{message.content}</div>
      {artifacts?.filter((artifact) => artifact.run_id === runId(message) &&
        (message.role === "assistant" || !messages.some((item) => item.role === "assistant" && runId(item) === artifact.run_id)))
        .map((artifact) => <Artifact key={artifact.id} content={artifact.content}
          runStatus={runs?.find((run) => run.id === artifact.run_id)?.status ?? "succeeded"} />)}
      {message.role === "assistant" && <button className="pg-copy icon-button" aria-label="复制回复" onClick={() => copy(message.content)}><Copy size={15} /></button>}
    </div>
  </article>
    {message.role === "user" && !messages.some((item) => item.role === "assistant" && runId(item) === runId(message)) && traces(message).map((trace) =>
      <article key={trace.id} className="pg-message assistant" aria-label="Agent 的思考"><span className="pg-assistant-icon"><Sparkles size={22} /></span>
        <div className="pg-message-content"><Reasoning trace={trace} /></div></article>)}
  </Fragment>)}</>;
});
function eventLabel(event: AGUIEvent): string {
  switch (event.type) {
    case "RUN_STARTED": return "运行已接收";
    case "RUN_FINISHED": return event.outcome?.type === "cancelled" ? "运行已取消" : "运行完成";
    case "RUN_ERROR": return "运行失败";
    case "STEP_STARTED": return "开始下一步骤";
    case "STEP_FINISHED": return "步骤结束";
    case "REASONING_START": return "开始思考";
    case "REASONING_END": return "思考结束";
    case "TOOL_CALL_START": return `正在调用：${event.toolCallName}`;
    case "TOOL_CALL_RESULT": return "工具已返回结果";
    case "STATE_SNAPSHOT": {
      const value: unknown = event.snapshot;
      if (value && typeof value === "object" && "run" in value && value.run && typeof value.run === "object" && "status" in value.run) {
        const status: Record<string, string> = { queued: "运行已进入队列", running: "Agent 开始执行", cancelling: "正在停止运行", cancelled: "运行已取消", succeeded: "运行完成", failed: "运行失败", expired: "运行已超时" };
        return status[String(value.run.status)] ?? "运行状态已更新";
      }
      return "运行状态已更新";
    }
    case "CUSTOM": {
      if (event.name === "platform.output_limit") {
        const value: unknown = event.value;
        return value && typeof value === "object" && "action" in value && value.action === "recover"
          ? "达到单次输出上限，按 Agent 配置尝试生成完整结果" : "达到单次输出上限，已停止恢复";
      }
      return ({ "platform.queued": "运行已进入队列", "platform.model": "正在调用模型", "platform.usage": "调用用量与费用已更新" } as Record<string, string>)[event.name] ?? "运行进度已更新";
    }
    default: return event.type;
  }
}
