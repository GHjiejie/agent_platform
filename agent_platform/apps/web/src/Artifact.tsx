// Adapted from agent-agui-generated-ui/frontend/src/Artifact.tsx.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import { artifactPhase, finalDocument, parseGenerativeContent, previewDocument, previewUpdate, validArtifactMessage, validArtifactResizeMessage } from "./sandbox";
import type { ArtifactFailure } from "./sandbox";

type RunStatus = string;
const isRunning = (status: string) => ["queued", "running", "cancelling"].includes(status);

type RegenerateProps = { onRegenerate?: () => void; regenerateDisabled?: boolean };
type Props = { content: unknown; runStatus: RunStatus } & RegenerateProps;

export function Artifact({ content: value, runStatus, onRegenerate, regenerateDisabled }: Props) {
  const content = useMemo(() => parseGenerativeContent(value), [value]);
  const disabled = regenerateDisabled || isRunning(runStatus);
  if (!content) return <ArtifactError message="界面数据格式不正确或超过大小限制。" onRegenerate={onRegenerate} regenerateDisabled={disabled} />;
  const validated = typeof value === "object" && value !== null && "validated" in value && value.validated === true;
  return <ValidArtifact validated={validated} content={content} runStatus={runStatus} onRegenerate={onRegenerate} regenerateDisabled={disabled} />;
}

function ArtifactError({ message, failure, onRegenerate, regenerateDisabled }: { message: string; failure?: ArtifactFailure } & RegenerateProps) {
  return <div className="artifact-error" role="alert"><span>界面生成失败</span><div>{message}</div>
    <small>{onRegenerate ? "重新生成将保留原会话及其内容。" : "请在对话中重试生成。"}</small>
    {onRegenerate && <button type="button" className="retry-button" onClick={onRegenerate} disabled={regenerateDisabled}
      title={regenerateDisabled ? "请等待当前生成或模型列表加载完成" : "根据原始请求重新生成此界面"}>重新生成界面</button>}
    {failure && <details className="artifact-error-details">
      <summary>查看错误详情</summary><p>{failure.stage === "initialization" ? "初始化" : "运行中"} · {failure.name}</p><pre>{failure.message}</pre>
    </details>}
  </div>;
}

function ValidArtifact({ content, runStatus, validated, onRegenerate, regenerateDisabled }: { validated: boolean; content: NonNullable<ReturnType<typeof parseGenerativeContent>>; runStatus: RunStatus } & RegenerateProps) {
  const phase = artifactPhase(content);
  const frame = useRef<HTMLIFrameElement>(null);
  const html = content.html?.join("") ?? "";
  const expressions = JSON.stringify(content.jsExpressions ?? []);
  // A revised complete artifact is a new execution, with a new Window/token.
  // A delayed status from the previous document must not unlock its successor.
  const executionVersion = phase === "initialize" ? JSON.stringify([html, content.css, content.jsFunctions, expressions]) : "";
  const executionChannel = useMemo(() => crypto.randomUUID().replaceAll("-", ""), [executionVersion]);
  const [previewChannel] = useState(() => crypto.randomUUID().replaceAll("-", ""));
  type FrameStatus = "loading" | "ready" | "error" | "timeout";
  const [frameState, setFrameState] = useState<{ channel: string; status: FrameStatus; error?: ArtifactFailure }>({ channel: executionChannel, status: "loading" });
  const frameStatus = frameState.channel === executionChannel ? frameState.status : "loading";
  const failure = frameState.channel === executionChannel ? frameState.error : undefined;
  // The last measured size belongs to this artifact, not to one streamed
  // revision. Keep it while changing documents so the page never resets to 320px.
  const [frameHeight, setFrameHeight] = useState<number | null>(null);
  const final = useMemo(() => phase === "initialize" ? finalDocument(content, executionChannel) : "", [executionVersion, executionChannel]);
  const stopped = !(validated && phase === "initialize") && ["cancelled", "canceled", "failed", "expired", "interrupted", "incomplete"].includes(runStatus);
  const error = phase === "empty" || frameStatus === "error" || frameStatus === "timeout" ||
    (["failed", "incomplete"].includes(runStatus) && phase !== "initialize") ||
    (runStatus === "succeeded" && phase !== "initialize");
  const executing = phase === "initialize" && !error && !stopped;
  const channel = executing ? executionChannel : previewChannel;

  useEffect(() => {
    const receive = (event: MessageEvent<unknown>) => {
      if (event.source !== frame.current?.contentWindow || !validArtifactResizeMessage(event.data, channel)) return;
      const height = Math.min(6000, event.data.height);
      setFrameHeight(current => current === height ? current : height);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [channel]);

  const sendViewport = useCallback(() => frame.current?.contentWindow?.postMessage({
    type: "artifact-viewport", channel, height: Math.max(1, Math.round(window.innerHeight)),
  }, "*"), [channel]);
  useEffect(() => {
    sendViewport();
    window.addEventListener("resize", sendViewport);
    return () => window.removeEventListener("resize", sendViewport);
  }, [sendViewport]);

  // Restored artifacts can execute as soon as their iframe mounts. Subscribe
  // before passive effects so an immediate failure still exposes regeneration.
  useLayoutEffect(() => {
    if (phase !== "initialize" || stopped) return;
    setFrameState({ channel: executionChannel, status: "loading" });
    const receive = (event: MessageEvent<unknown>) => {
      // A sandboxed srcdoc frame has opaque origin. Compare the actual window,
      // a per-instance token, and the exact envelope; origin alone is insufficient.
      if (event.source !== frame.current?.contentWindow || !validArtifactMessage(event.data, executionChannel)) return;
      const status = event.data.status;
      const details = event.data.error;
      setFrameState(current => current.channel === executionChannel && (current.status === "error" || current.status === "timeout") ? current : { channel: executionChannel, status, ...(details ? { error: details } : {}) });
    };
    window.addEventListener("message", receive);
    const timer = window.setTimeout(() => setFrameState(current => current.channel === executionChannel && current.status === "loading" ? { channel: executionChannel, status: "timeout" } : current), 10_000);
    return () => { window.removeEventListener("message", receive); window.clearTimeout(timer); };
  }, [phase, final, executionChannel, stopped]);

  const ready = phase === "initialize" && frameStatus === "ready" && !stopped;
  // Generation deadlines belong to the request. A partial artifact can still
  // finish after a long reasoning phase; only initialization has its own timer.
  const label = error ? "界面生成失败" : stopped ? "预览已停止" : phase === "placeholder" ? "正在准备界面" : phase === "preview" ? "流式预览 · 交互准备中" : "正在初始化交互";
  const message = phase === "empty" ? "模型未返回可显示的界面。" : frameStatus === "error" ? "生成的界面脚本执行失败。" :
    frameStatus === "timeout" ? "界面初始化超时，未确认交互就绪。" : "界面内容不完整。";
  const height = frameHeight ?? Math.max(1, content.initialHeight ?? 320);

  return <section className={`artifact ${ready ? "artifact-ready" : ""}`} aria-label="生成式界面">
    <div className="artifact-body" style={{ minHeight: ready ? height : Math.max(200, height) }}>
      {executing ? <iframe
        ref={frame}
        key={`final-${executionChannel}`}
        aria-label="生成的交互界面"
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        srcDoc={final}
        onLoad={sendViewport}
        style={{ height }}
      /> : phase !== "placeholder" && phase !== "empty" && <PreviewFrame content={content} channel={previewChannel} frame={frame} height={height} onLoad={sendViewport} />}
      {!ready && <div className={`artifact-overlay ${phase === "placeholder" ? "opaque" : ""}`}>
        {error ? <ArtifactError message={message} failure={failure} onRegenerate={onRegenerate} regenerateDisabled={regenerateDisabled} /> : stopped ?
          <div className="artifact-loading">{label}，保留已收到的预览。</div> : <div className="artifact-loading"><span className="spinner" />{label}<small>收到完整内容并初始化成功后开放交互。</small></div>}
      </div>}
    </div>
  </section>;
}

function PreviewFrame({ content, channel, frame, height, onLoad }: {
  content: NonNullable<ReturnType<typeof parseGenerativeContent>>;
  channel: string;
  frame: RefObject<HTMLIFrameElement | null>;
  height: number;
  onLoad: () => void;
}) {
  // srcDoc is deliberately fixed for this mount: editing it navigates the
  // iframe, flashes empty content, and loses its last height on every token.
  const [source] = useState(() => previewDocument(content, channel));
  const html = content.html?.join("") ?? "";
  const css = content.css ?? "";
  const update = useMemo(() => previewUpdate({ html: [html], css }, channel), [html, css, channel]);
  const latest = useRef(update);
  latest.current = update;
  const pending = useRef<number | undefined>(undefined);
  const send = useCallback(() => {
    if (pending.current !== undefined) window.clearTimeout(pending.current);
    pending.current = undefined;
    frame.current?.contentWindow?.postMessage(latest.current, "*");
  }, [frame]);
  useEffect(() => {
    // Coalesce bursts, but still publish during a continuous token stream.
    if (pending.current === undefined) pending.current = window.setTimeout(send, 100);
  }, [update, send]);
  useEffect(() => () => {
    if (pending.current !== undefined) window.clearTimeout(pending.current);
  }, []);
  return <iframe ref={frame} aria-label="生成界面预览" sandbox="allow-scripts" referrerPolicy="no-referrer"
    srcDoc={source} style={{ height }} onLoad={() => { onLoad(); send(); }} />;
}
