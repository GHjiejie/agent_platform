import { useId, useState } from "react";
import { Brain, ChevronDown } from "lucide-react";
import type { AgentMessage } from "./ag-ui";
import type { ReasoningTrace } from "./types";

export function reasoningTrace(message: AgentMessage): ReasoningTrace | null {
  if (message.role !== "reasoning") return null;
  const runId = /^reasoning:([^:]+):/.exec(message.id)?.[1];
  if (!runId) return null;
  const platform: unknown = message.metadata?.platform;
  const status = platform && typeof platform === "object" && "reasoning_status" in platform ? platform.reasoning_status : undefined;
  return {
    id: message.id, run_id: runId, content: message.content, created_at: "",
    status: status === "complete" || status === "interrupted" ? status : "streaming",
  };
}

export function Reasoning({ trace }: { trace: ReasoningTrace }) {
  const id = useId();
  const [expanded, setExpanded] = useState(trace.status === "streaming");
  const title = trace.status === "streaming" ? "正在思考" : trace.status === "interrupted" ? "思考已中断" : "思考过程";
  return <section className={`pg-reasoning ${trace.status}`} aria-label={title}>
    <button type="button" className="pg-reasoning-toggle" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded((value) => !value)}>
      <Brain size={16} /><span>{title}</span><ChevronDown size={15} className={expanded ? "expanded" : ""} />
    </button>
    <div id={id} className="pg-reasoning-content" hidden={!expanded}>
      {trace.content || (trace.status === "streaming" ? "正在分析问题…" : "未返回可展示的推理内容。")}
    </div>
  </section>;
}
