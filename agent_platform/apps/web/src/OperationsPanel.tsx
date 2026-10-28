import { Select } from "./Select";
import { useCallback, useEffect, useRef, useState } from "react";
import { Download, FileDown, RefreshCw, Trash2 } from "lucide-react";
import {
  api, ApiError, apiUrl, dateTime, getScopeRevision, getTenantId,
  scopeIsCurrent, write,
} from "./api";
import {
  Badge, Button, Empty, Field, Form, Modal, Panel, ResourceState,
  formValue, useResource, useToast,
} from "./components";
import type { User } from "./types";

type ExportJob = {
  id: string;
  kind: "calls" | "runs";
  scope: "self" | "tenant";
  status: string;
  row_count: number;
  created_at: string;
  cutoff: string;
  expires_at: number;
  error_code: string;
  measurement_note: string;
};

const exportLabels: Record<string, string> = {
  queued: "等待生成", running: "生成中", ready: "可下载",
  expired: "已过期", failed: "生成失败", cancelled: "已取消",
};

function exportError(code: string) {
  if (!code) return "";
  if (code.includes("permission") || code.includes("membership") || code.includes("identity"))
    return "访问权限已变化，请重新申请。";
  if (code.includes("storage") || code.includes("file"))
    return "导出文件暂不可用，请联系管理员。";
  if (code.includes("tenant")) return "组织状态已变化，无法完成导出。";
  return "本次导出未完成，可重新申请或联系管理员。";
}

export function TenantExportsPanel({ user }: { user: User }) {
  const allowed = !!user.capabilities?.includes("exports.create");
  const organizationAllowed = !!user.capabilities?.includes("usage.read_all");
  const resource = useResource<{ items: ExportJob[] }>("/exports", allowed);
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState<"calls" | "runs">("calls");
  const [scope, setScope] = useState<"self" | "tenant">("self");
  const [downloading, setDownloading] = useState<string | null>(null);
  const requestKey = useRef<{ parameters: string; key: string } | null>(null);
  const downloadRequest = useRef<AbortController | null>(null);
  const toast = useToast();
  const active = resource.data?.items.some((job) => ["queued", "running"].includes(job.status));

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => { void resource.reload(); }, 4000);
    return () => window.clearInterval(timer);
  }, [active, resource.reload]);

  useEffect(() => {
    const cancel = () => downloadRequest.current?.abort();
    window.addEventListener("tenant-scope-changed", cancel);
    window.addEventListener("auth-expired", cancel);
    return () => {
      cancel();
      window.removeEventListener("tenant-scope-changed", cancel);
      window.removeEventListener("auth-expired", cancel);
    };
  }, []);

  async function download(job: ExportJob) {
    const tenant = getTenantId();
    const revision = getScopeRevision();
    const controller = new AbortController();
    downloadRequest.current?.abort();
    downloadRequest.current = controller;
    setDownloading(job.id);
    try {
      const response = await fetch(apiUrl(`/exports/${encodeURIComponent(job.id)}/download`, tenant), {
        credentials: "same-origin", signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("auth-expired"));
        if (response.status === 403) window.dispatchEvent(new Event("tenant-access-changed"));
        const body = await response.json().catch(() => null);
        throw new Error(body?.error?.message ?? "下载失败，请刷新后重试。");
      }
      const blob = await response.blob();
      if (controller.signal.aborted || !scopeIsCurrent(revision) || getTenantId() !== tenant) return;
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `${job.kind}-${job.id}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      if (!controller.signal.aborted && scopeIsCurrent(revision))
        toast(error instanceof Error ? error.message : "下载失败，请重试。", "error");
    } finally {
      if (downloadRequest.current === controller) {
        downloadRequest.current = null;
        setDownloading(null);
      }
    }
  }

  if (!allowed) return null;
  return <>
    <Panel title="数据导出" detail="导出调用或运行元数据。每份文件仅申请人可下载，不包含对话正文、提示词或供应商成本。"
      action={<div className="row-actions">
        <Button variant="ghost" onClick={() => void resource.reload()} aria-label="刷新导出任务"><RefreshCw size={16} /></Button>
        <Button variant="secondary" onClick={() => setCreating(true)}><FileDown size={16} />申请导出</Button>
      </div>}>
      <ResourceState loading={resource.loading && !resource.data} error={resource.error} retry={resource.reload}>
        {resource.data?.items.length ? <div className="table-scroll"><table>
          <thead><tr><th>申请时间</th><th>数据范围</th><th>状态</th><th>记录数</th><th>文件有效期</th><th>操作</th></tr></thead>
          <tbody>{resource.data.items.map((job) => <tr key={job.id}>
            <td>{dateTime(job.created_at)}<small>截止 {dateTime(job.cutoff)}</small></td>
            <td>{job.kind === "calls" ? "调用元数据" : "运行元数据"}<small>{job.scope === "tenant" ? "当前组织" : "仅本人"}</small></td>
            <td><Badge status={job.status === "ready" ? "completed" : job.status}>{exportLabels[job.status] ?? job.status}</Badge>
              {job.error_code && <small>{exportError(job.error_code)}</small>}</td>
            <td>{job.row_count}</td>
            <td>{dateTime(job.expires_at)}</td>
            <td><Button variant="ghost" busy={downloading === job.id}
              disabled={job.status !== "ready" || job.expires_at * 1000 <= Date.now()}
              onClick={() => void download(job)}><Download size={16} />下载 CSV</Button></td>
          </tr>)}</tbody>
        </table></div> : <Empty title="还没有导出文件" description="提交申请后将在后台生成，可稍后回来下载。" />}
      </ResourceState>
      <p className="tenant-detail-summary">记录范围固定到申请时间；费用、用量与状态取各分页生成时的账务记录。后续结算不会改写已生成文件，查看最新账务请重新申请。</p>
    </Panel>
    {creating && <Modal title="申请数据导出" close={() => setCreating(false)} description="文件仅自己可见，下载时会重新检查当前组织权限。">
      <Form close={() => setCreating(false)} label="提交导出申请" submit={async () => {
        const actualScope = kind === "runs" ? "self" : scope;
        const parameters = JSON.stringify({ kind, scope: actualScope, tenant: getTenantId() });
        if (requestKey.current?.parameters !== parameters) requestKey.current = { parameters, key: crypto.randomUUID() };
        await api<ExportJob>("/exports", { method: "POST", body: JSON.stringify({ kind, scope: actualScope }), headers: { "Idempotency-Key": requestKey.current.key } });
        requestKey.current = null;
        setCreating(false);
        toast("导出申请已提交。");
        await resource.reload();
      }}>
        <Field label="数据类型"><Select value={kind} onChange={(event) => {
          const value = event.target.value as "calls" | "runs";
          setKind(value);
          if (value === "runs") setScope("self");
        }}><option value="calls">调用元数据与费用</option><option value="runs">运行元数据</option></Select></Field>
        <Field label="数据范围" hint="组织范围仅导出调用元数据；运行记录只可导出自己的。"><Select value={kind === "runs" ? "self" : scope}
          onChange={(event) => setScope(event.target.value as "self" | "tenant")}>
          <option value="self">仅本人</option>
          {kind === "calls" && organizationAllowed && <option value="tenant">当前组织全部调用</option>}
        </Select></Field>
      </Form>
    </Modal>}
  </>;
}

type Closure = {
  id: string;
  status: string;
  reason: string;
  created_at: string;
  retain_until: number;
  finished_at: string | null;
  error_code: string;
};

const closureLabels: Record<string, string> = {
  waiting: "等待关闭条件满足", retaining: "内容保留期", purging: "内容清理中", deleted: "关闭完成",
};
const closureBlockers: Record<string, string> = {
  billing_unresolved: "仍有预占或待核实账单，请先完成账务对账。",
  runs_pending: "仍有执行中的任务，等待任务终止。",
  gateway_revocation_pending: "网关凭据正在撤销，等待外部状态确认。",
  legacy_gateway_revocation_required: "旧版共享网关凭据需要运维人员撤销并核实。",
  tombstone_write_failed: "删除记录尚未可靠保存，内容清理已停止，请检查存储。",
  tombstone_storage_required: "请先配置删除记录存储，再执行内容清理。",
};

export function TenantClosurePanel({ tenantId, tenantName, onChanged }: {
  tenantId: string;
  tenantName: string;
  onChanged?: () => Promise<void>;
}) {
  const [closure, setClosure] = useState<Closure | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);
  const toast = useToast();
  const base = `/api/v2/platform/tenants/${encodeURIComponent(tenantId)}`;

  const reload = useCallback(async () => {
    const revision = ++generation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    try {
      const value = await api<Closure>(`${base}/closure`, { signal: controller.signal });
      if (generation.current === revision) setClosure(value);
    } catch (failure) {
      if (generation.current !== revision || controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 404) setClosure(null);
      else setError(failure instanceof Error ? failure.message : "关闭状态暂不可用。");
    } finally {
      if (generation.current === revision) setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    setClosure(null);
    setConfirming(false);
    void reload();
    return () => { generation.current++; request.current?.abort(); };
  }, [reload]);

  useEffect(() => {
    if (!closure || closure.status === "deleted") return;
    const timer = window.setInterval(() => { void reload(); }, 10000);
    return () => window.clearInterval(timer);
  }, [closure?.status, reload]);

  return <>
    <Panel title="关闭组织" detail="停止新任务，等待账务终结与凭据撤销；至少保留 30 天内容后清理。账务与审计记录继续保留。"
      action={<Button variant="ghost" onClick={() => void reload()} aria-label="刷新关闭状态"><RefreshCw size={16} /></Button>}>
      <ResourceState loading={loading && !closure} error={error} retry={reload}>
        {closure ? <div className="tenant-detail-summary">
          <div><Badge status={closure.status === "deleted" ? "completed" : "pending"}>{closureLabels[closure.status] ?? closure.status}</Badge>
            <p>申请于 {dateTime(closure.created_at)} · 最早清理时间 {dateTime(closure.retain_until)}</p>
            <p>关闭原因：{closure.reason}</p>
            {closure.error_code && <p role="status">{closureBlockers[closure.error_code] ?? "关闭条件尚未全部满足，请联系运维人员检查。"}</p>}
            {closure.finished_at && <p>完成时间：{dateTime(closure.finished_at)}</p>}
          </div>
        </div> : <div className="tenant-detail-summary"><div><strong>尚未申请关闭</strong><p>关闭后立即停止受理新任务，清理完成后无法恢复组织内容。</p></div>
          <Button variant="danger" onClick={() => setConfirming(true)}><Trash2 size={16} />关闭此组织</Button></div>}
      </ResourceState>
    </Panel>
    {confirming && <Modal title={`关闭「${tenantName}」`} close={() => setConfirming(false)} description="请确认目标组织。此操作取消排队任务并停止新调用，之后按保留期限清理内容。">
      <Form label="确认关闭组织" close={() => setConfirming(false)} submit={async (form) => {
        if (formValue(form, "confirmation") !== tenantName) throw new Error("请输入完整组织名称以确认目标。");
        const revision = generation.current;
        const value = await write<Closure>(`${base}/close`, { reason: formValue(form, "reason").trim() });
        if (generation.current !== revision) return;
        setClosure(value);
        setConfirming(false);
        toast("组织已进入关闭流程。");
        await onChanged?.();
      }}>
        <Field label="关闭原因"><textarea name="reason" required minLength={1} maxLength={1000} rows={3} /></Field>
        <Field label="输入完整组织名称" hint={tenantName}><input name="confirmation" required autoComplete="off" /></Field>
        <p>待核实费用、执行中的任务或尚未撤销的网关凭据会延后清理。至少 30 天保留期结束后删除对话正文、提示词及导出文件，保留账务与审计元数据。</p>
      </Form>
    </Modal>}
  </>;
}
