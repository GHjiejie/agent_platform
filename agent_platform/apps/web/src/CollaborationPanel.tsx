import { useEffect, useState } from "react";
import { money } from "./api";
import { Button, useResource } from "./components";
import type { CollaborationDetail, CollaborationExecution } from "./api-contract.generated";

const statuses: Record<string, string> = {
  pending: "等待前置任务", ready: "准备执行", running: "执行中", succeeded: "已完成",
  failed: "失败", cancelled: "已取消", blocked: "前置任务未完成", waiting_capacity: "等待可用容量",
  waiting_evidence: "等待结果确认",
};
function Execution({ execution }: { execution: CollaborationExecution }) {
  return <div className="collaboration-execution">
    <p>{execution.name} · v{execution.agent_version} · {execution.model_alias || "尚未调用模型"}</p>
    <p>已确认费用 {money(execution.fees.confirmed, 8)} · 待确认预留 {money(execution.fees.pending, 8)}</p>
    {execution.error && <p role="alert">{execution.error}</p>}
    {execution.output != null && <pre aria-label={`${execution.name}的结果`}>{typeof execution.output === "string" ? execution.output : JSON.stringify(execution.output, null, 2)}</pre>}
  </div>;
}
function TaskDetails({ runId, taskId }: { runId: string; taskId: string }) {
  const resource = useResource<CollaborationDetail>(`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}`);
  return <div>
    {resource.loading && !resource.data && <p role="status">正在读取任务输入…</p>}
    {resource.error && <p role="alert">{resource.error}<Button variant="ghost" onClick={() => void resource.reload()}>重试</Button></p>}
    {resource.data?.executions.map((execution) => <div key={execution.id}>
      <p>输入摘要（最多 2,000 字）</p><pre>{execution.input_summary}</pre><small>输入摘要校验值：{execution.input_digest}</small>
    </div>)}
  </div>;
}
export function CollaborationPanel({ runId, refreshKey = "", live = false }: { runId: string; refreshKey?: string; live?: boolean }) {
  const [expanded, setExpanded] = useState(live);
  const [selected, setSelected] = useState<string | null>(null);
  const resource = useResource<CollaborationDetail>(`/runs/${encodeURIComponent(runId)}/collaboration`, expanded);
  const { reload } = resource;
  useEffect(() => {
    if (!expanded || !refreshKey) return;
    const timer = setTimeout(() => void reload(), 100);
    return () => clearTimeout(timer);
  }, [expanded, refreshKey, reload]);
  const detail = resource.data;
  return <section className="collaboration-panel" aria-label="团队协作">
    <button type="button" className="collaboration-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      {expanded ? "收起" : "查看"}团队协作过程{detail?.outcome === "partial" ? " · 部分完成" : ""}
    </button>
    {expanded && <div className="collaboration-body">
      {resource.loading && !detail && <p role="status">正在读取协作任务…</p>}
      {resource.error && <p role="alert">{resource.error}<Button variant="ghost" onClick={() => void reload()}>重试</Button></p>}
      {detail && <>
        <p>已确认 {money(detail.fees.confirmed, 8)} · 待确认预留 {money(detail.fees.pending, 8)} · 总费用上限 {money(detail.max_cost, 8)}</p>
        {!detail.tasks.length && <p>等待生成任务计划。</p>}
        <ol className="collaboration-tasks">{detail.tasks.map((task) => <li key={task.id}>
          <details><summary>{task.objective} · {statuses[task.status] ?? task.status}</summary>
            <p>任务：{task.logical_key} · {task.required ? "必选" : "可选"} · 前置任务：{task.dependencies.join("、") || "无"}</p>
            {task.error && <p role="alert">{task.error}</p>}
            {detail.executions.filter((execution) => execution.task_id === task.id).map((execution) => <Execution key={execution.id} execution={execution} />)}
            <Button type="button" variant="ghost" onClick={() => setSelected(selected === task.id ? null : task.id)}>查看输入来源</Button>
            {selected === task.id && <TaskDetails key={task.id} runId={runId} taskId={task.id} />}
          </details>
        </li>)}</ol>
        {detail.executions.filter((execution) => !execution.task_id).map((execution) => <details key={execution.id}><summary>{execution.role === "summary" ? "统一汇总" : "任务规划"} · {statuses[execution.status] ?? execution.status}</summary><Execution execution={execution} /></details>)}
        {!!detail.artifacts.length && <details><summary>已确认成果与来源（{detail.artifacts.length}）</summary>
          {detail.artifacts.map((artifact) => <p key={artifact.id}>成果 {artifact.id} · v{artifact.version} · {artifact.kind}<br />执行：{artifact.execution_id}<br />校验值：{artifact.digest}</p>)}
        </details>}
        <Button type="button" variant="ghost" onClick={() => void reload()}>刷新任务详情</Button>
      </>}
    </div>}
  </section>;
}
