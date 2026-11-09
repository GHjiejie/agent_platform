import { useState } from "react";
import { api } from "./api";
import { Button, Field, Select, useResource } from "./components";
import type { CollaborationSettingsResponse, DiscoveryCandidate, DiscoveryResponse, TaskContract, TeamSpec, TaskTemplate } from "./api-contract.generated";

export const textContract = { name: "request", version: 1, json_schema: { type: "string", maxLength: 32000 } };
export const dataContract = { name: "observations", version: 1, json_schema: {
  type: "object", properties: { facts: { type: "array", items: { type: "string", maxLength: 2000 }, maxItems: 50 }, missing: { type: "array", items: { type: "string", maxLength: 500 }, maxItems: 20 } }, required: ["facts", "missing"], additionalProperties: false,
} };
export const memberContracts: TaskContract[] = [{ task_type: "normalize", input: textContract, output: dataContract }];

export function TeamEditor({ value, onChange }: { value: TeamSpec; onChange: (value: TeamSpec) => void }) {
  const discovery = useResource<DiscoveryResponse>("/agents/discovery");
  const settings = useResource<CollaborationSettingsResponse>("/agents/collaboration-settings");
  const [more, setMore] = useState<DiscoveryCandidate[]>([]);
  const [cursor, setCursor] = useState<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const candidates = [...(discovery.data?.items ?? []), ...more].filter((candidate) => candidate.task_contracts.length);
  const nextCursor = cursor === undefined ? discovery.data?.next_cursor : cursor;
  const tasks = value.tasks ?? [];
  function updateTask(key: string, patch: Partial<TaskTemplate>) {
    const memberKey = tasks.find((task) => task.key === key)?.member_key;
    onChange({ ...value, members: value.members.map((member) => member.key === memberKey && patch.objective !== undefined ? { ...member, responsibility: patch.objective } : member), tasks: tasks.map((task) => task.key === key ? { ...task, ...patch } : task) });
  }
  function assign(task: TaskTemplate, id: string) {
    const candidate = candidates.find((item) => `${item.id}:${item.version}` === id);
    if (!candidate) return;
    const memberKey = task.member_key;
    const member = { key: memberKey, agent_id: candidate.id, version: candidate.version, responsibility: task.objective || candidate.description || candidate.name };
    onChange({ ...value, members: [...value.members.filter((item) => item.key !== memberKey), member], tasks: tasks.map((item) => item.key === task.key ? { ...item, task_type: candidate.task_contracts[0].task_type } : item) });
  }
  function addTask() {
    const key = `task_${crypto.randomUUID().slice(0, 8)}`;
    onChange({ ...value, tasks: [...tasks, { key, member_key: key, task_type: "", objective: "", dependencies: [], required: true }] });
  }
  return <fieldset className="agent-execution-policy">
    <legend>团队任务</legend>
    <p className="muted">选择同组织已发布的成员并固定版本。成员输出通过校验后才传给后继；团队系统指令用于规划与最终汇总。</p>
    {settings.loading && !settings.data && <p role="status">正在读取协作能力…</p>}
    {settings.error && <p role="alert">{settings.error}<Button type="button" variant="ghost" onClick={() => void settings.reload()}>重试读取协作能力</Button></p>}
    <div className="form-columns">
      <Field label="规划方式" hint={settings.data?.features?.dynamic ? "动态规划只能使用已固定成员及其任务接口，不会扩大预算。" : "动态规划尚未开放，已有配置会保留。"}><Select value={value.planning ?? "template"} onChange={(event) => onChange({ ...value, planning: event.target.value === "dynamic" ? "dynamic" : "template" })}>
        <option value="template">固定任务模板</option><option value="dynamic" disabled={!settings.data?.features?.dynamic}>按请求动态规划</option>
      </Select></Field>
      <Field label="同时执行的任务数" hint={settings.data?.features?.parallel ? "无依赖的任务可同时执行，仍共享团队预算。" : "并行执行尚未开放。"}><Select value={String(value.max_parallel ?? 1)} onChange={(event) => onChange({ ...value, max_parallel: Number(event.target.value) })}>
        <option value="1">1 个（顺序执行）</option><option value="2" disabled={!settings.data?.features?.parallel}>最多 2 个</option>
      </Select></Field>
    </div>
    {value.planning === "dynamic" && <p className="muted">下方目标用于配置候选成员的职责。实际任务和依赖会按每次请求重新生成，可能与预设不同；任务数量受上限约束。</p>}
    {discovery.loading && !discovery.data && <p role="status">正在发现可用成员…</p>}
    {discovery.error && <p role="alert">{discovery.error}<Button type="button" variant="ghost" onClick={() => void discovery.reload()}>重试发现</Button></p>}
    {!discovery.loading && !candidates.length && <p>还没有声明任务接口的已发布成员。请先创建单 Agent，配置“协作能力”并发布，或安装顺序模板。</p>}
    {tasks.map((task, index) => {
      const binding = value.members.find((member) => member.key === task.member_key);
      const candidate = candidates.find((item) => item.id === binding?.agent_id && item.version === binding.version);
      const choices = candidates;
      return <fieldset className="team-task-editor" key={task.key}><legend>任务 {index + 1} · {task.key}</legend>
        <Field label={`任务 ${index + 1} 的目标`}><input required maxLength={4000} value={task.objective} onChange={(event) => updateTask(task.key, { objective: event.target.value })} /></Field>
        <Field label={`任务 ${index + 1} 的成员`} hint="版本固定；重新选择成员才更新绑定。">
          <Select value={binding ? `${binding.agent_id}:${binding.version}` : ""} onChange={(event) => assign(task, event.target.value)}>
            <option value="">选择已发布成员</option>
            {binding && !candidate && <option value={`${binding.agent_id}:${binding.version}`}>已固定成员 {binding.agent_id} · v{binding.version}（不在当前候选页）</option>}
            {choices.map((item) => <option key={`${item.id}:${item.version}`} value={`${item.id}:${item.version}`}>{item.name} · v{item.version}</option>)}
          </Select>
        </Field>
        <Field label={`任务 ${index + 1} 的类型`}><Select value={task.task_type} onChange={(event) => updateTask(task.key, { task_type: event.target.value })}>
          <option value="">选择任务类型</option>
          {!candidate && task.task_type && <option value={task.task_type}>{task.task_type}（已固定）</option>}
          {candidate?.task_contracts.map((contract) => <option value={contract.task_type} key={contract.task_type}>{contract.task_type} · {contract.input.name} → {contract.output.name}</option>)}
        </Select></Field>
        <p>前置任务</p><div className="team-dependencies">{tasks.filter((item) => item.key !== task.key).map((item) => <label key={item.key}>
          <input type="checkbox" checked={task.dependencies?.includes(item.key) ?? false} onChange={(event) => updateTask(task.key, { dependencies: event.target.checked ? [...(task.dependencies ?? []), item.key] : task.dependencies?.filter((key) => key !== item.key) })} />{item.objective || item.key}
        </label>)}</div>
        <label className="team-dependencies"><input type="checkbox" checked={task.required !== false} onChange={(event) => updateTask(task.key, { required: event.target.checked })} />必须完成此任务</label>
        <Button type="button" variant="ghost" onClick={() => {
          const remaining = tasks.filter((item) => item.key !== task.key).map((item) => ({ ...item, dependencies: item.dependencies?.filter((key) => key !== task.key) }));
          onChange({ ...value, tasks: remaining, members: value.members.filter((member) => remaining.some((item) => item.member_key === member.key)) });
        }}>移除任务 {index + 1}</Button>
      </fieldset>;
    })}
    <Button type="button" variant="secondary" disabled={tasks.length >= 8} onClick={addTask}>添加任务</Button>
    {nextCursor && <Button type="button" variant="ghost" busy={loading} onClick={async () => {
      setLoading(true); setError("");
      try { const page = await api<DiscoveryResponse>(`/agents/discovery?cursor=${encodeURIComponent(nextCursor)}`); setMore((items) => [...items, ...page.items]); setCursor(page.next_cursor); }
      catch (cause) { setError(cause instanceof Error ? cause.message : "读取候选失败"); } finally { setLoading(false); }
    }}>加载更多成员</Button>}
    {error && <p role="alert">{error}</p>}
    <div className="form-columns">
      <Field label="团队模型调用总次数" hint="包括规划、成员、修复和最终汇总，至少为汇总留一次。"><input type="number" required min={2} max={50} value={value.max_model_calls ?? 20} onChange={(event) => onChange({ ...value, max_model_calls: Number(event.target.value) })} /></Field>
      <Field label="汇总费用预留比例" hint="成员不能消耗这部分主运行预算。"><input type="number" required min={0.05} max={0.5} step={0.05} value={value.summary_reserve_fraction ?? 0.15} onChange={(event) => onChange({ ...value, summary_reserve_fraction: Number(event.target.value) })} /></Field>
    </div>
    <Field label="任务数量上限" hint="动态规划和固定模板都不能超过此数量。"><input type="number" required min={Math.max(1, tasks.length)} max={8} value={value.max_tasks ?? 8} onChange={(event) => onChange({ ...value, max_tasks: Number(event.target.value) })} /></Field>
    <label className="team-dependencies"><input type="checkbox" checked={value.allow_partial ?? false} onChange={(event) => onChange({ ...value, allow_partial: event.target.checked })} />可选任务失败时允许部分交付</label>
  </fieldset>;
}
