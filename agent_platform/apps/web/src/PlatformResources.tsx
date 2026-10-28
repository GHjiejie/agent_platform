import { Select } from "./Select";
import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Pencil, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import { dateTime, money, write } from "./api";
import { Badge, Button, Empty, Field, Form, Modal, Panel, ResourceState, formValue, useResource, useToast } from "./components";
import { BillingReconciliation } from "./BillingReconciliation";
import type { IdentityUser, Model, Tenant, Wallet } from "./types";

type Items<T> = { items: T[] };
type ModelPolicy = { default_model_id: string | null; ordered_model_ids: string[]; version: number };
type Deployment = { id: string; name: string; internal_route: string; base_url: string; status: string; gateway_id: string };
type GrantedModel = Model & { deployment_id?: string; status?: string; sync_status?: string };
type GatewayStatus = {
  status: string; mode: string; desired_version?: number; applied_version?: number; error_code?: string;
  credentials: { id: string; generation: number; status: string; expires_at?: number | string; fingerprint: string }[];
  operations: { id: string; action: string; status: string; phase: string; error_code?: string; attempt_count: number; updated_at: string }[];
  operation_counts?: Record<string, number>;
};
const gatewayLabel = (value: string) => ({ not_enrolled: "尚未开通", legacy: "旧版共享凭据", provisioning: "正在同步", ready: "已就绪", revoking: "撤销中", revoked: "已撤销", degraded: "同步异常", failed: "同步失败", pending: "等待处理", applying: "正在应用", reconciling: "核实中", retry_wait: "等待重试", applied: "已应用", active: "有效", retiring: "退出中" }[value] ?? value);

export function ModelPolicyPanel({ models, changed }: { models: Model[]; changed: () => Promise<void> }) {
  const resource = useResource<ModelPolicy>("/model-policy");
  const [editing, setEditing] = useState(false);
  return <Panel title="组织模型偏好" detail="自动选择只使用当前组织获授权的模型，Agent 角色配置可继续复用。" action={<Button variant="secondary" disabled={!resource.data} onClick={() => setEditing(true)}>
    <Pencil size={14} />设置默认与顺序</Button>}>
    <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
      {resource.data && <div className="form-body">
        <p>默认模型：<strong>
          {models.find((item) => item.id === resource.data?.default_model_id)?.name ?? "自动选择"}</strong>
        </p>
        <p className="muted">候选顺序：{resource.data.ordered_model_ids.map((id) => models.find((item) => item.id === id)?.name ?? "已不可用模型").join(" → ") || "按授权模型的默认顺序"}</p>
      </div>}</ResourceState>
    {editing && resource.data && <ModelPolicyEditor policy={resource.data} models={models} close={() => setEditing(false)} saved={async () => { setEditing(false); await Promise.all([resource.reload(), changed()]); }} />}</Panel>;
}
function ModelPolicyEditor({ policy, models, close, saved }: { policy: ModelPolicy; models: Model[]; close: () => void; saved: () => Promise<void> }) {
  const [order, setOrder] = useState(policy.ordered_model_ids);
  const [defaultId, setDefaultId] = useState(policy.default_model_id ?? "");
  function move(index: number, direction: number) {
    setOrder((previous) => {
      const next = [...previous];
      [next[index], next[index + direction]] = [next[index + direction], next[index]];
      return next;
    });
  }
  return <Modal title="组织模型选择策略" description="本次手动选择和 Agent 指定模型优先于组织默认策略。" close={close}>
    <Form close={close} submit={async () => { await write("/model-policy", { default_model_id: defaultId || null, ordered_model_ids: order, expected_version: policy.version }, "PUT"); await saved(); }}>
      <Field label="组织默认模型">
        <Select value={defaultId} onChange={(event) => setDefaultId(event.target.value)}>
          <option value="">自动选择候选模型</option>
          {models.filter((item) => item.active || item.id === defaultId).map((item) =>
            <option value={item.id} key={item.id}>
              {item.name}{item.active ? "" : "（已停用）"}</option>)}</Select>
      </Field>
      <Field label="添加候选模型" hint="可调整自动选择时的优先顺序；不会在模型请求失败后自动再次调用。">
        <Select value="" onChange={(event) => setOrder((previous) => [...previous, event.target.value])}>
          <option value="">选择候选模型</option>
          {models.filter((item) => item.active && !order.includes(item.id)).map((item) =>
            <option value={item.id} key={item.id}>
              {item.name}</option>)}</Select>
      </Field>
      <ol className="model-priority-list">
        {order.map((id, index) =>
          <li key={id}>
            <div className="model-priority-row">
              <span>
                {models.find((item) => item.id === id)?.name ?? "已不可用模型"}</span>
              <div className="row-actions">
                <button className="icon-button" type="button" aria-label="提高优先级" disabled={index === 0} onClick={() => move(index, -1)}>
                  <ArrowUp size={14} />
                </button>
                <button className="icon-button" type="button" aria-label="降低优先级" disabled={index === order.length - 1} onClick={() => move(index, 1)}>
                  <ArrowDown size={14} />
                </button>
                <button className="icon-button" type="button" aria-label="移除候选模型" onClick={() => setOrder((previous) => previous.filter((value) => value !== id))}>
                  <X size={14} />
                </button>
              </div>
            </div>
          </li>)}</ol>
    </Form>
  </Modal>;
}

export function TenantGatewayPanel({ tenant }: { tenant: Tenant }) {
  const prefix = `/api/v2/platform/tenants/${tenant.id}/gateway`;
  const resource = useResource<GatewayStatus>(prefix);
  const [operation, setOperation] = useState<"enroll" | "rotate" | "revoke" | null>(null);
  const [reconciling, setReconciling] = useState("");
  const toast = useToast();
  const status = resource.data?.status;
  const pending = !!resource.data && (["pending", "applying", "reconciling", "retry_wait"].some((state) => (resource.data?.operation_counts?.[state] ?? 0) > 0) || resource.data.operations.some((item) => ["pending", "applying", "reconciling", "retry_wait"].includes(item.status)));
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => { void resource.reload(); }, 5000);
    return () => clearInterval(timer);
  }, [pending, resource.reload]);
  return <Panel title="组织网关授权" detail="凭据在服务端加密保存，控制台只显示同步状态与代际。" action={<Button variant="ghost" aria-label="刷新网关同步" onClick={() => void resource.reload()}>
    <RefreshCw size={16} />
  </Button>}>
    <ResourceState loading={resource.loading && !resource.data} error={resource.error} retry={() => void resource.reload()}>
      {resource.data && <>
        <div className="tenant-detail-summary">
          <div>
            <Badge status={status === "ready" ? "active" : status === "failed" || status === "degraded" ? "failed" : "pending"}>
              {gatewayLabel(status ?? "not_enrolled")}</Badge>
            <p className="muted">期望版本 {resource.data.desired_version ?? 0} · 已应用 {resource.data.applied_version ?? 0}</p>
          </div>
          <div className="row-actions">
            <Button variant="secondary" onClick={() => setOperation("enroll")} disabled={pending}>开通 / 同步授权</Button>
            <Button variant="secondary" onClick={() => setOperation("rotate")} disabled={pending || resource.data.mode !== "enrolled"}>轮换凭据</Button>
            <Button variant="danger" onClick={() => setOperation("revoke")} disabled={["not_enrolled", "revoked"].includes(status ?? "")}>撤销凭据</Button>
          </div>
        </div>
        {resource.data.error_code && <div className="form-error" role="alert">同步尚未完成：{resource.data.error_code}。请检查平台配置和操作记录后重试。</div>}
        {resource.data.credentials.length > 0 && <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>凭据代际</th>
                <th>指纹</th>
                <th>状态</th>
                <th>过期时间</th>
              </tr>
            </thead>
            <tbody>
              {resource.data.credentials.map((item) =>
                <tr key={item.id}>
                  <td>v{item.generation}</td>
                  <td className="mono">
                    {item.fingerprint}</td>
                  <td>
                    {gatewayLabel(item.status)}</td>
                  <td>
                    {item.expires_at ? dateTime(item.expires_at) : "—"}</td>
                </tr>)}</tbody>
          </table>
        </div>}
        {resource.data.operations.length > 0 && <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>操作</th>
                <th>状态</th>
                <th>处理阶段</th>
                <th>重试次数</th>
                <th>更新时间</th>
                <th className="align-right">核查</th>
              </tr>
            </thead>
            <tbody>
              {resource.data.operations.map((item) =>
                <tr key={item.id}>
                  <td>
                    {item.action}</td>
                  <td>
                    {gatewayLabel(item.status)}{item.error_code && <small>
                      {item.error_code}</small>}</td>
                  <td>
                    {item.phase}</td>
                  <td>
                    {item.attempt_count}</td>
                  <td>
                    {dateTime(item.updated_at)}</td>
                  <td className="align-right">{["reconciling", "retry_wait"].includes(item.status) && <Button variant="ghost" busy={reconciling === item.id} onClick={async () => {
                    setReconciling(item.id);
                    try { await write(`${prefix}/operations/${item.id}/reconcile`); await resource.reload(); toast("已请求重新核查网关状态"); }
                    catch (error) { toast((error as Error).message, "error"); }
                    finally { setReconciling(""); }
                  }}>重新核查</Button>}</td>
                </tr>)}</tbody>
          </table>
        </div>}
      </>}</ResourceState>
    {operation && <Modal title={`${{ enroll: "同步授权", rotate: "轮换凭据", revoke: "撤销凭据" }[operation]}：${tenant.name}`} close={() => setOperation(null)}>
      <Form close={() => setOperation(null)} label="确认操作" submit={async () => { await write(`${prefix}/${operation}`); setOperation(null); await resource.reload(); toast("网关操作已提交，请关注同步结果"); }}>
        <div className="inline-note">
          <ShieldCheck size={18} />
          {operation === "revoke" ? "撤销后此组织不能发起新的模型调用，已有调用仍会完成费用处理。" : "操作由后台同步到网关，只有已应用的授权可用于模型调用。"}</div>
      </Form>
    </Modal>}
  </Panel>;
}

export function TenantFinancePanel({ tenant, refresh }: { tenant: Tenant; refresh: number }) {
  const prefix = `/api/v2/platform/tenants/${tenant.id}`;
  const resource = useResource<Wallet>(`${prefix}/billing/wallet`);
  useEffect(() => { void resource.reload(); }, [refresh, resource.reload]);
  return <>
    <Panel title={`组织钱包 · ${tenant.name}`}>
      <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
        {resource.data && <>
          <div className="tenant-limits-grid">
            <div>
              <span>账面余额</span>
              <strong>
                {money(resource.data.balance, 6)}</strong>
            </div>
            <div>
              <span>预占金额</span>
              <strong>
                {money(resource.data.reserved, 6)}</strong>
            </div>
            <div>
              <span>可用余额</span>
              <strong>
                {money(resource.data.available, 6)}</strong>
            </div>
          </div>
          {resource.data.blocked && <div className="form-error">
            {resource.data.block_reason || "钱包已暂停新的模型准入，请先核实费用。"}</div>}</>}</ResourceState>
    </Panel>
    <BillingReconciliation basePath={prefix} wallet={resource.data} changed={resource.reload} />
  </>;
}

export function PlatformDeploymentsPanel() {
  const resource = useResource<Items<Deployment>>("/api/v2/platform/deployments");
  const [creating, setCreating] = useState(false);
  return <Panel title="平台模型部署" detail="先登记已经在网关配置好的部署，再给组织授权与设置报价。" action={<Button variant="secondary" onClick={() => setCreating(true)}>
    <Plus size={15} />登记部署</Button>}>
    <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
      {resource.data?.items.length ? <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>部署名称</th>
              <th>内部路由</th>
              <th>网关</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {resource.data.items.map((item) =>
              <tr key={item.id}>
                <td>
                  {item.name}</td>
                <td className="mono">
                  {item.internal_route}</td>
                <td>
                  {item.gateway_id}</td>
                <td>
                  <Badge status={item.status} />
                </td>
              </tr>)}</tbody>
        </table>
      </div> : <Empty title="尚未登记模型部署" description="网关部署与租户看到的模型名称分别维护。" />}</ResourceState>
    {creating && <Modal title="登记平台模型部署" description="请先在网关配置上游服务与密钥。这里登记可授权的部署，不接收上游 API Key。" close={() => setCreating(false)}>
      <Form close={() => setCreating(false)} submit={async (form) => { await write("/api/v2/platform/deployments", { name: formValue(form, "name"), internal_route: formValue(form, "internal_route"), base_url: formValue(form, "base_url"), gateway_id: "primary", capabilities: (form.elements.namedItem("tools") as HTMLInputElement).checked ? ["chat", "tools"] : ["chat"] }); setCreating(false); await resource.reload(); }}>
        <Field label="部署名称">
          <input name="name" maxLength={120} required />
        </Field>
        <Field label="网关内部路由" hint="对应 LiteLLM 已配置的 model_name。仅平台运营界面显示此信息。">
          <input name="internal_route" maxLength={200} required />
        </Field>
        <Field label="网关 API 地址" hint="例如 https://gateway.example.com/v1；必须在服务端允许的地址范围内。">
          <input name="base_url" type="url" maxLength={500} required />
        </Field>
        <label className="checkbox-row">
          <input name="tools" type="checkbox" />支持工具调用</label>
      </Form>
    </Modal>}
  </Panel>;
}

export function PlatformModelGrants({ tenant, identity }: { tenant: Tenant; identity: IdentityUser }) {
  const prefix = `/api/v2/platform/tenants/${tenant.id}`;
  const resource = useResource<Items<GrantedModel>>(`${prefix}/model-grants`);
  const deployments = useResource<Items<Deployment>>("/api/v2/platform/deployments");
  const tokenDefaults = useResource<import("./types").TokenDefaults>("/api/v2/platform/token-defaults");
  const [editing, setEditing] = useState<GrantedModel | "new" | null>(null);
  const canPrice = identity.capabilities.includes("platform.pricing.manage");
  return <Panel title="组织模型与报价" detail="修改价格仅影响后续运行。新增或更换部署后，请在组织网关面板同步授权。" action={canPrice && <Button variant="secondary" onClick={() => { void deployments.reload(); setEditing("new"); }}>
    <Plus size={15} />添加模型授权</Button>}>
    <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
      {resource.data?.items.length ? <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>状态</th>
              <th>输入 / 输出价格</th>
              <th>输出上限</th>
              <th className="align-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {resource.data.items.map((item) =>
              <tr key={item.id}>
                <td>
                  <strong>
                    {item.name}</strong>
                  <small>
                    {item.alias}</small>
                </td>
                <td>
                  <Badge status={item.active ? "active" : "disabled"} />
                  <small>{gatewayLabel(item.sync_status ?? "not_enrolled")}</small>
                </td>
                <td>
                  {money(item.input_price)} / {money(item.output_price)}<small>USD / 百万 Token</small>
                </td>
                <td>
                  {item.max_output_tokens}</td>
                <td className="align-right">
                  {canPrice && <Button variant="ghost" onClick={() => { void deployments.reload(); setEditing(item); }}>
                    <Pencil size={14} />维护报价</Button>}</td>
              </tr>)}</tbody>
        </table>
      </div> : <Empty title="尚未授予模型" description="添加模型授权，或从已有平台模型复制授权。" />}</ResourceState>
    {editing && <Modal title={`${editing === "new" ? "添加模型授权" : "维护模型报价"}：${tenant.name}`} close={() => setEditing(null)}>
      <ResourceState loading={deployments.loading} error={deployments.error} retry={() => void deployments.reload()}>
        <Form close={() => setEditing(null)} submit={async (form) => { await write(`${prefix}/models`, { deployment_id: formValue(form, "deployment_id"), name: formValue(form, "name"), alias: formValue(form, "alias"), input_price: formValue(form, "input_price"), output_price: formValue(form, "output_price"), context_window: Number(formValue(form, "context_window")), max_output_tokens: Number(formValue(form, "max_output_tokens")), active: (form.elements.namedItem("active") as HTMLInputElement).checked }); setEditing(null); await resource.reload(); }}>
          <Field label="平台部署">
            <Select name="deployment_id" required defaultValue={editing === "new" ? "" : editing.deployment_id ?? ""}>
              <option value="" disabled>选择部署</option>
              {deployments.data?.items.filter((item) => item.status === "active").map((item) =>
                <option key={item.id} value={item.id}>
                  {item.name} · {item.internal_route}</option>)}</Select>
          </Field>
          <Field label="组织显示名称">
            <input name="name" maxLength={120} required defaultValue={editing === "new" ? "" : editing.name} />
          </Field>
          <Field label="组织内模型别名" hint="组织内唯一。编辑相同别名会更新此组织的模型与报价。">
            <input name="alias" maxLength={200} required readOnly={editing !== "new"} defaultValue={editing === "new" ? "" : editing.alias} />
          </Field>
          <div className="form-columns">
            <Field label="输入单价（USD / 百万 Token）">
              <input name="input_price" type="number" min="0.000000000001" max="1000000" step="any" required defaultValue={editing === "new" ? "" : editing.input_price} />
            </Field>
            <Field label="输出单价（USD / 百万 Token）">
              <input name="output_price" type="number" min="0.000000000001" max="1000000" step="any" required defaultValue={editing === "new" ? "" : editing.output_price} />
            </Field>
            <Field label="上下文窗口">
              <input name="context_window" type="number" min="256" max="2000000" required defaultValue={editing === "new" ? tokenDefaults.data?.context_window ?? 1048576 : editing.context_window} />
            </Field>
            <Field label="最大输出 Token">
              <input name="max_output_tokens" type="number" min="1" required defaultValue={editing === "new" ? tokenDefaults.data?.output_tokens ?? 131072 : editing.max_output_tokens} />
            </Field>
          </div>
          <label className="checkbox-row">
            <input name="active" type="checkbox" defaultChecked={editing === "new" || editing.active} />启用模型</label>
        </Form>
      </ResourceState>
    </Modal>}
  </Panel>;
}
