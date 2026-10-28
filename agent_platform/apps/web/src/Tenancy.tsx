import { Select } from "./Select";
import { useRef, useState } from "react";
import { Building2, Check, Copy, ExternalLink, Pencil, Plus, RefreshCw, ShieldCheck, Users } from "lucide-react";
import { api, dateTime, money, tenantLink, write } from "./api";
import type { Entitlements, GatewayAdmin, IdentityUser, Invitation, Membership, Model, Tenant, User } from "./types";
import { Badge, Button, Empty, Field, Form, Modal, PageTitle, Panel, ResourceState, SearchBox, formValue, useResource, useToast } from "./components";

import { PlatformDeploymentsPanel, PlatformModelGrants, TenantFinancePanel, TenantGatewayPanel } from "./PlatformResources";
import { PlatformRolesPanel } from "./Support";
import { TenantClosurePanel } from "./OperationsPanel";

type Items<T> = { items: T[] };
export const roleLabel = (role: string) => ({ owner: "组织所有者", tenant_admin: "组织管理员", member: "成员", finance_viewer: "财务查看员" }[role] ?? role);
const stateLabel = (state: string) => ({ active: "正常", suspended: "已暂停", provisioning: "开通中", closing: "关闭中", deleted: "已删除", pending: "待接受", accepted: "已接受", expired: "已过期", revoked: "已撤销", removed: "已移除" }[state] ?? state);
function State({ value }: { value: string }) {
  return <Badge status={value === "suspended" || value === "removed" ? "disabled" : value}>
    {stateLabel(value)}</Badge>;
}
function RoleOptions({ owner }: { owner: boolean }) {
  return <>
    <option value="member">成员</option>
    {owner && <option value="tenant_admin">组织管理员</option>}<option value="finance_viewer">财务查看员</option>
  </>;
}
function invitationState(item: Invitation) { return item.status ?? (item.accepted_at ? "accepted" : item.revoked_at ? "revoked" : item.expires_at && new Date(typeof item.expires_at === "number" ? item.expires_at * 1000 : item.expires_at).getTime() < Date.now() ? "expired" : "pending"); }

export function InvitationPage({ identity, accepted }: { identity: IdentityUser; accepted: () => Promise<void> }) {
  const [token, setToken] = useState(() => new URLSearchParams(location.hash.split("?")[1] ?? "").get("token") ?? "");
  const [done, setDone] = useState(false);
  return <>
    <PageTitle eyebrow="INVITATION" title="接受组织邀请" description="使用收到邀请的邮箱登录，将该组织加入你的工作空间。" />
    {!identity.memberships.some((item) => item.status === "active") && <div className="info-banner">
      <Building2 size={22} />
      <div>
        <strong>你还没有加入组织</strong>
        <p>请联系组织管理员获取邀请。已有平台运营权限的账号也可以从平台运营页开通组织。</p>
      </div>
    </div>}
    <Panel title={done ? "已加入组织" : "邀请信息"} detail={`当前账号：${identity.email}`}>
      {done ? <Empty title="邀请已接受" description="从右上角切换到新组织，即可开始使用。" action={<Button onClick={() => { location.hash = tenantLink("overview"); }}>进入工作空间</Button>} /> : <Form close={() => setToken("")} label="接受邀请" submit={async () => {
        const value = token.trim(); if (!value) throw new Error("请输入邀请令牌或邀请链接。");
        let invitationToken = value;
        if (value.includes("#")) invitationToken = new URLSearchParams(value.split("?")[1] ?? "").get("token") ?? value;
        const membership = await write<{ tenant_id: string }>("/api/v2/invitations/accept", { token: invitationToken });
        setToken(""); setDone(true); history.replaceState(null, "", tenantLink("invite")); await accepted();
        location.hash = tenantLink("overview", membership.tenant_id);
      }}>
        <Field label="邀请令牌或链接" hint="邀请仅能由指定邮箱使用；过期后请联系管理员重新邀请。">
          <textarea value={token} onChange={(event) => setToken(event.target.value)} rows={3} autoComplete="off" required />
        </Field>
      </Form>}
    </Panel>
  </>;
}

export function MembersPage({ user, changed }: { user: User; changed: () => Promise<void> }) {
  const members = useResource<Items<Membership>>("/memberships");
  const invitations = useResource<Items<Invitation>>("/invitations");
  const [editing, setEditing] = useState<Membership | null>(null);
  const [invite, setInvite] = useState(false);
  const [createdLink, setCreatedLink] = useState("");
  const [transfer, setTransfer] = useState<Membership | null>(null);
  const [search, setSearch] = useState("");
  const toast = useToast();
  const isOwner = user.tenant_role === "owner";
  const items = (members.data?.items ?? []).filter((item) => `${item.name} ${item.email}`.toLowerCase().includes(search.toLowerCase()));
  async function reload() { await Promise.all([members.reload(), invitations.reload(), changed()]); }
  return <>
    <PageTitle eyebrow="MEMBERS" title="成员与邀请" description="角色仅在当前组织生效；移除成员不会删除其历史运行或账务记录。" action={<Button onClick={() => { setCreatedLink(""); setInvite(true); }}>
      <Plus size={16} />邀请成员</Button>} />
    <div className="inline-note">
      <ShieldCheck size={18} />组织所有者负责所有权转移；财务查看员只能访问费用与用量，不能运行 Agent。</div>
    <Panel title="组织成员" action={<Button variant="ghost" aria-label="刷新成员" onClick={() => void reload()}>
      <RefreshCw size={16} />
    </Button>}>
      <div className="toolbar">
        <SearchBox value={search} onChange={setSearch} placeholder="搜索姓名或邮箱" />
      </div>
      <ResourceState loading={members.loading} error={members.error} retry={() => void members.reload()}>
        {items.length ? <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>成员</th>
                <th>角色</th>
                <th>状态</th>
                <th className="align-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => <tr key={item.id}>
                <td>
                  <strong>
                    {item.name || item.email}</strong>
                  <small>
                    {item.email}{item.user_id === user.id ? " · 你" : ""}</small>
                </td>
                <td>
                  {roleLabel(item.role)}</td>
                <td>
                  <State value={item.status} />
                </td>
                <td className="align-right">
                  <div className="row-actions">
                    {item.role !== "owner" && (isOwner || item.role !== "tenant_admin") && <Button variant="ghost" onClick={() => setEditing(item)}>
                      <Pencil size={14} />编辑</Button>}{isOwner && item.role !== "owner" && item.status === "active" && <Button variant="ghost" onClick={() => setTransfer(item)}>转移所有权</Button>}</div>
                </td>
              </tr>)}</tbody>
          </table>
        </div> : <Empty title="没有匹配成员" />}</ResourceState>
    </Panel>
    <Panel title="组织邀请" detail="创建邀请后复制链接并自行分享；新邮箱需由平台运营人员先开通账号，平台不会自动发送邮件。">
      <ResourceState loading={invitations.loading} error={invitations.error} retry={() => void invitations.reload()}>
        {invitations.data?.items.length ? <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>受邀邮箱</th>
                <th>角色</th>
                <th>状态</th>
                <th>过期时间</th>
              </tr>
            </thead>
            <tbody>
              {invitations.data.items.map((item) => <tr key={item.id}>
                <td>
                  {item.email}</td>
                <td>
                  {roleLabel(item.role)}</td>
                <td>
                  <State value={invitationState(item)} />
                </td>
                <td>
                  {item.expires_at ? dateTime(item.expires_at) : "—"}</td>
              </tr>)}</tbody>
          </table>
        </div> : <Empty title="暂无邀请" description="邀请成员使用现有账号加入组织。" />}</ResourceState>
    </Panel>
    {invite && <Modal title={createdLink ? "邀请已创建" : "邀请组织成员"} close={() => setInvite(false)}>
      {createdLink ? <div className="form-body">
        <Field label="邀请链接" hint="请发送给指定邮箱的成员。令牌仅在创建时展示，请妥善分享。">
          <textarea readOnly value={createdLink} rows={4} />
        </Field>
        <Button onClick={async () => { try { await navigator.clipboard.writeText(createdLink); toast("邀请链接已复制"); } catch { toast("请选中邀请链接后手动复制。", "error"); } }}>
          <Copy size={16} />复制链接</Button>
      </div> : <Form close={() => setInvite(false)} label="创建邀请" submit={async (form) => { const result = await write<Invitation>("/invitations", { email: formValue(form, "email"), role: formValue(form, "role") }); const value = result.accept_url ?? (result.token ? `${location.origin}${location.pathname}#invite?token=${encodeURIComponent(result.token)}` : ""); if (value) setCreatedLink(value); else { setInvite(false); toast("邀请已创建，请从组织邀请记录中查看状态"); } await invitations.reload(); }}>
        <Field label="成员邮箱" hint="请确认对方已有登录账号；新账号请联系平台运营人员开通后再接受邀请。">
          <input name="email" type="email" required autoComplete="off" />
        </Field>
        <Field label="加入后的角色">
          <Select name="role" defaultValue="member">
            <RoleOptions owner={isOwner} />
          </Select>
        </Field>
      </Form>}</Modal>}
    {editing && <Modal title="编辑组织成员" description={editing.email} close={() => setEditing(null)}>
      <Form close={() => setEditing(null)} submit={async (form) => { await write(`/memberships/${editing.id}`, { role: formValue(form, "role"), status: formValue(form, "status"), expected_version: editing.version ?? editing.authz_version ?? 1 }, "PATCH"); setEditing(null); toast("成员权限已更新"); await reload(); }}>
        <Field label="组织角色">
          <Select name="role" defaultValue={editing.role}>
            <RoleOptions owner={isOwner} />
          </Select>
        </Field>
        <Field label="成员状态" hint="停用后不能继续访问本组织，其他组织的成员身份不受影响。">
          <Select name="status" defaultValue={editing.status}>
            <option value="active">正常</option>
            <option value="revoked">移除</option>
          </Select>
        </Field>
        {editing.user_id === user.id && <div className="form-error">正在修改你自己的成员身份。降低权限后部分管理入口将立即关闭。</div>}</Form>
    </Modal>}
    {transfer && <Modal title="转移组织所有权" description="转移后，对方成为组织所有者，你将保留管理员身份。" close={() => setTransfer(null)}>
      <Form close={() => setTransfer(null)} label="确认转移" submit={async () => { await write("/ownership-transfer", { membership_id: transfer.id, expected_version: user.membership_version }); setTransfer(null); await reload(); toast("所有权已转移"); }}>
        <div className="inline-note">
          <Users size={20} />
          {transfer.name} · {transfer.email}</div>
      </Form>
    </Modal>}
  </>;
}

export function PlatformPage({ identity, changed }: { identity: IdentityUser; changed: () => Promise<void> }) {
  const tenants = useResource<Items<Tenant>>("/api/v2/platform/tenants");
  const [selected, setSelected] = useState("");
  const [creating, setCreating] = useState(false);
  const [creatingUser, setCreatingUser] = useState(false);
  const toast = useToast();
  const createKey = useRef("");
  const [search, setSearch] = useState("");
  const canManage = identity.capabilities.includes("platform.tenants.manage");
  const items = (tenants.data?.items ?? []).filter((item) => `${item.name} ${item.id}`.toLowerCase().includes(search.toLowerCase()));
  const activeTenant = tenants.data?.items.find((item) => item.id === selected);
  return <>
    <PageTitle eyebrow="PLATFORM OPERATIONS" title="平台运营" description="管理组织生命周期、模型授权与平台额度。运营权限与组织内角色分别授予。" action={canManage && <div className="row-actions"><Button variant="secondary" onClick={() => setCreatingUser(true)}><Users size={16} />开通成员账号</Button><Button onClick={() => { createKey.current = crypto.randomUUID(); setCreating(true); }}>
      <Plus size={16} />开通组织</Button></div>} />
    <Panel title="全部组织" action={<Button variant="ghost" aria-label="刷新组织" onClick={() => void tenants.reload()}>
      <RefreshCw size={16} />
    </Button>}>
      <div className="toolbar">
        <SearchBox value={search} onChange={setSearch} placeholder="搜索组织名称或 ID" />
      </div>
      <ResourceState loading={tenants.loading} error={tenants.error} retry={() => void tenants.reload()}>
        {items.length ? <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>组织</th>
                <th>状态</th>
                <th>创建时间</th>
                <th className="align-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => <tr key={item.id} className={item.id === selected ? "selected-row" : ""}>
                <td>
                  <strong>
                    {item.name}</strong>
                  <small>
                    {item.id}</small>
                </td>
                <td>
                  <State value={item.status} />
                </td>
                <td>
                  {item.created_at ? dateTime(item.created_at) : "—"}</td>
                <td className="align-right">
                  <Button variant={item.id === selected ? "secondary" : "ghost"} onClick={() => setSelected(item.id)}>
                    {item.id === selected ? "当前选中" : "管理组织"}</Button>
                </td>
              </tr>)}</tbody>
          </table>
        </div> : <Empty title="暂无匹配的组织" description="平台运营人员可以开通新的组织。" />}</ResourceState>
    </Panel>
    {identity.capabilities.includes("platform.roles.manage") && <PlatformRolesPanel changed={changed} />}
    {identity.capabilities.includes("platform.models.manage") && <PlatformDeploymentsPanel />}
    {activeTenant && <PlatformTenantDetail key={activeTenant.id} tenant={activeTenant} identity={identity} changed={async () => { await Promise.all([tenants.reload(), changed()]); }} />}
    {creatingUser && <Modal title="开通成员账号" description="创建可登录的个人账号。账号需要接受组织邀请后才能访问该组织，请自行向成员交付初始登录信息。" close={() => setCreatingUser(false)}>
      <Form close={() => setCreatingUser(false)} label="创建账号" submit={async (form) => {
        await write("/api/v2/platform/users", { email: formValue(form, "email"), name: formValue(form, "name"), password: formValue(form, "password") });
        setCreatingUser(false); toast("账号已开通，请使用受邀邮箱登录并接受组织邀请。");
      }}>
        <Field label="邮箱" hint="必须与组织邀请中填写的邮箱一致。"><input name="email" type="email" autoComplete="off" maxLength={254} required /></Field>
        <Field label="姓名"><input name="name" maxLength={120} required /></Field>
        <Field label="初始密码" hint="至少 12 个字符；请让成员登录后在账号设置中修改。"><input name="password" type="password" autoComplete="new-password" minLength={12} maxLength={512} required /></Field>
      </Form>
    </Modal>}
    {creating && <Modal title="开通组织" description="为组织创建独立成员关系、钱包和内置 Agent，随后可授予模型与额度。" close={() => setCreating(false)}>
      <Form close={() => setCreating(false)} label="创建组织" submit={async (form) => { const ownerPassword = formValue(form, "owner_password"); const result = await api<Tenant | { tenant: Tenant }>("/api/v2/platform/tenants", { method: "POST", body: JSON.stringify({ name: formValue(form, "name"), owner_email: formValue(form, "owner_email"), owner_name: formValue(form, "owner_name") || undefined, ...(ownerPassword ? { owner_password: ownerPassword } : {}) }), headers: { "Idempotency-Key": createKey.current } }); setCreating(false); setSelected("tenant" in result ? result.tenant.id : result.id); await Promise.all([tenants.reload(), changed()]); }}>
        <Field label="组织名称">
          <input name="name" maxLength={120} required />
        </Field>
        <Field label="所有者邮箱">
          <input name="owner_email" type="email" required />
        </Field>
        <Field label="所有者姓名">
          <input name="owner_name" maxLength={120} />
        </Field>
        <Field label="新账号初始密码" hint="仅创建新账号时填写，已有账号沿用现有密码。至少 12 个字符。">
          <input name="owner_password" type="password" minLength={12} autoComplete="new-password" />
        </Field>
      </Form>
    </Modal>}
  </>;
}
const entitlementFields: { key: keyof Entitlements; label: string; optional?: boolean }[] = [
  { key: "rpm", label: "每分钟请求数" }, { key: "tpm", label: "每分钟 Token 数" },
  { key: "max_queued_runs", label: "排队任务上限" }, { key: "max_running_runs", label: "运行任务上限" }, { key: "max_members", label: "成员上限" }, { key: "max_agents", label: "Agent 上限" }, { key: "max_sse_connections", label: "实时连接上限" }, { key: "max_export_jobs", label: "导出任务上限" }, { key: "max_budget", label: "累计预算上限（USD）", optional: true },
];
function PlatformTenantDetail({ tenant, identity, changed }: { tenant: Tenant; identity: IdentityUser; changed: () => Promise<void> }) {
  const prefix = `/api/v2/platform/tenants/${tenant.id}`;
  const caps = identity.capabilities;
  const canManage = caps.includes("platform.tenants.manage");
  const canGrant = caps.includes("platform.models.manage");
  const canCredit = caps.includes("platform.billing.manage");
  const canEntitle = caps.includes("platform.entitlements.manage");
  const canGateway = caps.includes("platform.gateway.manage");
  const [resourceRevision, setResourceRevision] = useState(0);
  const entitlements = useResource<Entitlements>(`${prefix}/entitlements`, canEntitle);
  const models = useResource<Items<Model>>("/api/v2/platform/models", canGrant);
  const [dialog, setDialog] = useState<"suspend" | "resume" | "credit" | "grant" | "entitlements" | null>(null);
  const creditKey = useRef("");
  const toast = useToast();
  const quotas = entitlements.data;
  return <>
    <Panel title={`管理：${tenant.name}`} detail={`组织 ID：${tenant.id}`}>
      <div className="tenant-detail-summary">
        <State value={tenant.status} />
        <div className="row-actions">
          {canManage && tenant.status === "active" && <Button variant="danger" onClick={() => setDialog("suspend")}>暂停组织</Button>}
          {canManage && tenant.status === "suspended" && <Button variant="secondary" onClick={() => setDialog("resume")}>恢复组织</Button>}
          {canGrant && caps.includes("platform.pricing.manage") && <Button variant="secondary" onClick={() => setDialog("grant")}>授予模型</Button>}
          {canCredit && <Button variant="secondary" onClick={() => { creditKey.current = crypto.randomUUID(); setDialog("credit"); }}>额度入账</Button>}
          {canEntitle && <Button variant="secondary" disabled={!quotas} onClick={() => setDialog("entitlements")}>配置套餐上限</Button>}
        </div>
      </div>
      {canEntitle && <ResourceState loading={entitlements.loading} error={entitlements.error} retry={() => void entitlements.reload()}>
        {quotas && <div className="tenant-limits-grid">
          {entitlementFields.map(({ key, label }) => <div key={key}>
            <span>
              {label}</span>
            <strong>
              {key === "max_budget" ? (quotas[key] == null ? "不限" : money(quotas[key])) : quotas[key] ?? (key === "max_running_runs" ? quotas.max_concurrent_runs : undefined) ?? "不限"}</strong>
          </div>)}</div>}</ResourceState>}
      <div className="inline-note">
        <ShieldCheck size={17} />组织暂停会阻止新的运行和模型调用；已经发生的费用仍按证据结算。模型授权同步完成后才可用于新调用。</div>
      {(dialog === "suspend" || dialog === "resume") && <Modal title={dialog === "suspend" ? `暂停 ${tenant.name}` : `恢复 ${tenant.name}`} close={() => setDialog(null)}>
        <Form close={() => setDialog(null)} label="确认操作" submit={async (form) => { await write(`${prefix}/${dialog}`, { reason: formValue(form, "reason"), expected_version: tenant.version ?? tenant.authz_version }); setDialog(null); await changed(); toast("组织状态已更新"); }}>
          <Field label="操作原因">
            <textarea name="reason" rows={3} minLength={5} maxLength={500} required />
          </Field>
        </Form>
      </Modal>}
      {dialog === "credit" && <Modal title={`为 ${tenant.name} 入账`} description="此操作记录平台使用额度，不会发起支付。请核对组织及凭证。" close={() => setDialog(null)}>
        <Form close={() => setDialog(null)} label="确认入账" submit={async (form) => { await api(`${prefix}/credits`, { method: "POST", body: JSON.stringify({ amount: formValue(form, "amount"), description: formValue(form, "description") }), headers: { "Idempotency-Key": creditKey.current } }); setDialog(null); setResourceRevision((value) => value + 1); toast(`已为 ${tenant.name} 入账`); }}>
          <Field label="金额（USD）">
            <input name="amount" type="number" min="0.000001" step="any" required />
          </Field>
          <Field label="入账说明 / 凭证号">
            <textarea name="description" rows={3} minLength={3} maxLength={500} required />
          </Field>
        </Form>
      </Modal>}
      {dialog === "grant" && <Modal title={`授予模型：${tenant.name}`} close={() => setDialog(null)}>
        <ResourceState loading={models.loading} error={models.error} retry={() => void models.reload()}>
          <Form close={() => setDialog(null)} label="授予模型" submit={async (form) => { await write(`${prefix}/model-grants`, { source_model_id: formValue(form, "source_model_id") }); setDialog(null); setResourceRevision((value) => value + 1); toast("模型已授权，请在组织网关授权面板同步"); }}>
            <Field label="平台模型">
              <Select name="source_model_id" required defaultValue="">
                <option value="" disabled>选择已配置的平台模型</option>
                {models.data?.items.filter((model) => model.active).map((model) => <option value={model.id} key={model.id}>
                  {model.name} · {model.alias}</option>)}</Select>
            </Field>
            {!models.data?.items.some((model) => model.active) && <div className="inline-note">暂无可授权模型，请先配置平台模型目录。</div>}</Form>
        </ResourceState>
      </Modal>}
      {dialog === "entitlements" && quotas && <Modal title={`套餐上限：${tenant.name}`} description="平台上限对组织内所有策略生效。累计预算可留空，其余限制需填写；0 表示禁止。" close={() => setDialog(null)}>
        <Form close={() => setDialog(null)} submit={async (form) => { const payload: Record<string, number | string | null> = {}; for (const { key, optional } of entitlementFields) { const value = formValue(form, key); payload[key] = value === "" && optional ? null : key === "max_budget" ? value : Number(value); } await write(`${prefix}/entitlements`, { ...payload, expected_version: quotas.version }, "PUT"); setDialog(null); await entitlements.reload(); toast("套餐上限已更新"); }}>
          <div className="form-columns">
            {entitlementFields.map(({ key, label, optional }) => <Field key={key} label={label}>
              <input name={key} type="number" min={key === "max_members" ? 1 : 0} step={key === "max_budget" ? "any" : 1} required={!optional} placeholder={optional ? "不限" : undefined} defaultValue={quotas[key] ?? (key === "max_running_runs" ? quotas.max_concurrent_runs : undefined) ?? ""} />
            </Field>)}</div>
        </Form>
      </Modal>}
    </Panel>
    {canGrant && <PlatformModelGrants key={`models-${resourceRevision}`} tenant={tenant} identity={identity} />}
    {canGateway && <TenantGatewayPanel key={`gateway-${resourceRevision}`} tenant={tenant} />}
    {canCredit && <TenantFinancePanel tenant={tenant} refresh={resourceRevision} />}
    {canManage && <TenantClosurePanel tenantId={tenant.id} tenantName={tenant.name} onChanged={changed} />}
  </>;
}
export function PlatformGatewayPage() {
  const resource = useResource<GatewayAdmin>("/api/v2/platform/gateway");
  return <>
    <PageTitle eyebrow="PLATFORM GATEWAY" title="网关管理" description="网关全局管理仅对获得专用平台权限的运营人员开放。" />
    <Panel title="LiteLLM 管理台">
      <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
        {resource.data?.configured && resource.data.admin_url ? <div className="form-body">
          <div className="inline-note">
            <Check size={18} />管理台使用独立账号；租户模型使用权限仍需通过平台授权。</div>
          <a className="button secondary" href={resource.data.admin_url} target="_blank" rel="noopener noreferrer">打开网关管理台<ExternalLink size={16} />
          </a>
        </div> : <Empty title="尚未配置管理台地址" description="请由平台部署人员配置网关管理入口。" />}</ResourceState>
    </Panel>
  </>;
}
