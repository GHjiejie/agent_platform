import "./provider-catalog.css";
import { useEffect, useRef, useState } from "react";
import { Download, Plus, RefreshCw } from "lucide-react";
import { api, dateTime, getTenantId, write } from "./api";
import { Button, Empty, Field, Form, Modal, ResourceState, SearchBox, formValue, useResource, useToast } from "./components";
import { Select } from "./Select";
import type { ProviderListResponse, ProviderModel, ProviderOperationResponse, ProviderResponse } from "./api-contract.generated";

const errorMessage = (code: string) => ({
  provider_auth_failed: "服务商拒绝了 API Key，请检查密钥及模型查询权限。",
  provider_address_invalid: "服务商地址不可用，请使用公网 HTTPS API 地址。",
  provider_response_invalid: "服务商未返回兼容的模型列表，请检查 API 地址。",
  provider_http_failed: "服务商暂不可用，请稍后重新获取。",
  provider_import_unknown: "接入结果正在核实，请稍后刷新；系统不会重复创建模型。",
  provider_gateway_failed: "网关接入未完成，请检查网关配置后重试。",
  platform_role_required: "当前账号已没有平台模型管理权限。",
}[code] ?? "操作未完成，请检查服务商连接或网关状态。");

export function ProviderCatalog({ close, canPrice, changed }: { close: () => void; canPrice: boolean; changed: () => Promise<void> }) {
  const resource = useResource<ProviderListResponse>("/api/v2/platform/providers");
  const [selected, setSelected] = useState("");
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const [job, setJob] = useState<ProviderOperationResponse | null>(null);
  const [failure, setFailure] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [model, setModel] = useState<ProviderModel | null>(null);
  const [deployment, setDeployment] = useState("");
  const [tools, setTools] = useState(false);
  const toast = useToast();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const provider = resource.data?.items.find((item) => item.id === selected) ?? (!selected ? resource.data?.items[0] : undefined);
  const busy = submitting || !!job && (["queued", "running"].includes(job.status) || job.status === "unknown" && job.attempts < 3);
  useEffect(() => {
    if (!job?.id) return;
    let stopped = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const result = await api<ProviderOperationResponse>(`/api/v2/platform/provider-operations/${job.id}`, { signal: controller.signal });
        if (stopped) return;
        setJob(result);
        if (["queued", "running"].includes(result.status) || result.status === "unknown" && result.attempts < 3) {
          timer = setTimeout(() => void poll(), 1500);
        } else if (result.status === "succeeded") {
          await resource.reload();
          if (stopped) return;
          if (result.kind === "import" && result.result.deployment_id) {
            setDeployment(result.result.deployment_id);
            toast(canPrice ? "模型已接入，请确认报价并授权给当前组织" : "模型已接入，可在平台运营中授权给组织");
          } else toast(`已获取 ${result.result.model_count ?? 0} 个服务商模型`);
        } else setFailure(errorMessage(result.error_code));
      } catch (error) {
        if (!stopped) { setFailure(error instanceof Error ? error.message : "读取状态失败"); setJob(null); }
      }
    };
    void poll();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [job?.id, resource.reload, toast, canPrice]);

  async function discover(item: ProviderResponse) {
    setFailure(""); setJob(null); setSubmitting(true); setDeployment(""); setModel(null);
    try {
      const result = await write<ProviderOperationResponse>(`/api/v2/platform/providers/${item.id}/discover`);
      if (alive.current) setJob(result);
    } catch (error) { if (alive.current) setFailure(error instanceof Error ? error.message : "获取失败"); }
    finally { if (alive.current) setSubmitting(false); }
  }
  async function importModel(item: ProviderModel) {
    if (!provider) return;
    setFailure(""); setJob(null); setSubmitting(true); setDeployment(""); setModel(item);
    try {
      const result = await write<ProviderOperationResponse>(`/api/v2/platform/providers/${provider.id}/imports`, { model_id: item.id, tools });
      if (alive.current) setJob(result);
    } catch (error) { if (alive.current) setFailure(error instanceof Error ? error.message : "接入失败"); }
    finally { if (alive.current) setSubmitting(false); }
  }
  const items = provider?.catalog.filter((item) => `${item.id} ${item.name}`.toLowerCase().includes(query.toLowerCase())) ?? [];
  return <Modal className="provider-dialog" title="从服务商获取模型" description="连接兼容 OpenAI 模型列表接口的服务商，获取模型列表，选择支持文本对话的模型接入。" close={close}>
    {creating ? <Form close={() => setCreating(false)} label="保存并获取模型" submit={async (form) => {
      const item = await write<ProviderResponse>("/api/v2/platform/providers", { name: formValue(form, "name"), api_base: formValue(form, "api_base"), api_key: formValue(form, "api_key") });
      if (!alive.current) return;
      setSelected(item.id); setCreating(false); await resource.reload(); await discover(item);
    }}>
      <Field label="服务商名称"><input name="name" placeholder="例如：我的模型服务商" maxLength={120} required /></Field>
      <Field label="API 地址" hint="填写 API 基础地址，例如 https://api.example.com/v1，系统会请求其 /models 接口。"><input name="api_base" type="url" placeholder="https://api.example.com/v1" maxLength={500} required /></Field>
      <Field label="API Key" hint="密钥加密保存在服务端，保存后不回显。"><input name="api_key" type="password" autoComplete="new-password" maxLength={8192} required /></Field>
    </Form> : <div className="form-body provider-catalog">
      <ResourceState loading={resource.loading && !resource.data} error={resource.error} retry={() => void resource.reload()}>
        <section className="provider-connection">
        <div className="provider-toolbar">
          <Field label="服务商"><Select value={provider?.id ?? ""} disabled={busy} onChange={(event) => { setSelected(event.target.value); setQuery(""); setFailure(""); setModel(null); setDeployment(""); setJob(null); }}>
            {!resource.data?.items.length && <option value="">请先添加服务商</option>}
            {resource.data?.items.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </Select></Field>
          <Button variant="secondary" disabled={busy} onClick={() => setCreating(true)}><Plus size={15} />添加服务商</Button>
          <Button disabled={!provider || busy} busy={busy && job?.kind === "discover"} onClick={() => { if (provider) void discover(provider); }}><RefreshCw size={15} />获取模型</Button>
        </div>
        {provider && <p className="provider-meta muted">{provider.api_base} · {provider.catalog_updated_at ? `更新于 ${dateTime(provider.catalog_updated_at)}` : "尚未获取模型"}</p>}
        </section>
        {failure && <div className="form-error" role="alert">{failure}</div>}
        {busy && <p role="status">{job?.kind === "import" ? "正在接入网关，请稍候…" : "正在从服务商获取模型…"}</p>}
        <section className="provider-models" aria-label="服务商模型列表">
        {!!provider?.catalog.length && <div className="table-toolbar provider-search-toolbar"><SearchBox value={query} onChange={setQuery} placeholder="搜索服务商模型" /><span>{items.length} 个模型</span></div>}
        {!!items.length && <label className="checkbox-row provider-model-option"><input type="checkbox" checked={tools} disabled={busy} onChange={(event) => setTools(event.target.checked)} />所选模型支持工具调用</label>}
        {items.length ? <div className="table-scroll provider-model-table"><table><colgroup><col /><col className="provider-owner-column" /><col className="provider-action-column" /></colgroup><thead><tr><th>服务商模型</th><th>提供方</th><th className="align-right">操作</th></tr></thead><tbody>
          {items.map((item) => <tr key={item.id}><td><strong>{item.name}</strong><small>{item.id}</small></td><td>{item.owned_by || "—"}</td><td className="align-right"><Button variant="secondary" disabled={busy} onClick={() => void importModel(item)}><Download size={14} />接入模型</Button></td></tr>)}
        </tbody></table></div> : !busy && <Empty title={!provider ? "尚未连接服务商" : provider.catalog_updated_at ? query ? "没有匹配的模型" : "服务商未提供可用模型" : "等待获取模型列表"} description={!provider ? "添加服务商地址和 API Key 后即可获取模型，无需修改配置文件。" : "模型列表来自服务商接口，可点击“获取模型”刷新。"} />}
        </section>
        {deployment && model && canPrice && <ModelAuthorization key={deployment} deployment={deployment} model={model} changed={changed} close={() => { setDeployment(""); setModel(null); }} saved={() => { setDeployment(""); setModel(null); toast("模型已授权，正在同步组织网关权限"); }} />}
      </ResourceState>
    </div>}
  </Modal>;
}

function ModelAuthorization({ deployment, model, changed, saved, close }: { deployment: string; model: ProviderModel; changed: () => Promise<void>; saved: () => void; close: () => void }) {
  const [granted, setGranted] = useState(false);
  const tenantId = getTenantId();
  const defaults = useResource<import("./types").TokenDefaults>("/api/v2/platform/token-defaults");
  const context = model.context_window ?? defaults.data?.context_window;
  const output = Math.min(model.max_output_tokens ?? defaults.data?.output_tokens ?? 0, context ?? Infinity);
  return <section className="provider-authorization"><h3>确认报价并授权给当前组织</h3><p className="muted">服务商模型列表不包含可靠的调用价格，请填写平台报价。授权成功后自动同步网关权限。</p>
    <ResourceState loading={defaults.loading} error={defaults.error} retry={() => void defaults.reload()}>
      {context && output > 0 && <Form label={granted ? "重试同步网关" : "授权给当前组织"} close={close} submit={async (form) => {
        const prefix = `/api/v2/platform/tenants/${tenantId}`;
        if (!granted) { await write(`${prefix}/models`, { deployment_id: deployment, name: formValue(form, "name"), alias: formValue(form, "alias"), input_price: formValue(form, "input_price"), output_price: formValue(form, "output_price"), context_window: Number(formValue(form, "context_window")), max_output_tokens: Number(formValue(form, "max_output_tokens")), active: true }); setGranted(true); await changed(); }
        await write(`${prefix}/gateway/enroll`);
        await changed(); saved();
      }}>
        <div className="provider-form-grid"><Field label="显示名称"><input name="name" defaultValue={model.name.slice(0, 120)} maxLength={120} required /></Field>
        <Field label="模型别名"><input name="alias" defaultValue={`${model.id.replace(/[^a-zA-Z0-9_.:/-]/g, "-").slice(0, 175)}-${deployment.slice(0, 12)}`} maxLength={200} pattern="[a-zA-Z0-9_.:/-]+" required /></Field></div>
        <div className="provider-form-grid"><Field label="输入价格（USD / 百万 Token）"><input name="input_price" inputMode="decimal" placeholder="请填写" required /></Field><Field label="输出价格（USD / 百万 Token）"><input name="output_price" inputMode="decimal" placeholder="请填写" required /></Field></div>
        <div className="provider-form-grid"><Field label="上下文上限"><input name="context_window" type="number" min={256} max={2000000} defaultValue={context} required /></Field><Field label="输出上限"><input name="max_output_tokens" type="number" min={1} max={2000000} defaultValue={output} required /></Field></div>
      </Form>}
    </ResourceState>
  </section>;
}
