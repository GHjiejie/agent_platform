import { Select } from "./Select";
import { useEffect, useRef, useState } from "react";
import { RefreshCw, ShieldCheck, UserCog } from "lucide-react";
import { api, dateTime, write } from "./api";
import { Badge, Button, Empty, Field, Form, Modal, PageTitle, Panel, ResourceState, formValue, useResource, useToast } from "./components";
import type { IdentityUser, Message, User } from "./types";

type Items<T> = { items: T[] };
type Grant = {
  id: string; tenant_id: string; tenant_name?: string; staff_email?: string;
  reason: string; allow_content: boolean; expires_at: number; revoked_at: string | null;
};
type SupportRun = { id: string; session_id: string; status: string; created_at: string; finished_at: string | null };
type RoleUser = Pick<IdentityUser, "id" | "name" | "email" | "active" | "platform_roles">;
const roleNames: Record<string, string> = { platform_admin: "平台管理员", platform_finance: "平台财务", platform_support: "平台支持" };
const grantActive = (grant: Grant) => !grant.revoked_at && grant.expires_at * 1000 > Date.now();

export function OwnerSupportPanel({ user }: { user: User }) {
  const grants = useResource<Items<Grant>>("/support-grants");
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Grant | null>(null);
  const toast = useToast();
  return <Panel title="临时支持访问" detail="仅组织所有者可以批准。默认开放 15 分钟运行元数据；会话正文需要单独勾选批准，每次访问都会留下审计记录。" action={user.tenant_status === "active" && <Button variant="secondary" onClick={() => setCreating(true)}>
    <ShieldCheck size={16} />批准支持访问</Button>}>
    <ResourceState loading={grants.loading} error={grants.error} retry={() => void grants.reload()}>
      {grants.data?.items.length ? <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>支持人员</th>
              <th>范围 / 原因</th>
              <th>到期时间</th>
              <th>状态</th>
              <th className="align-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {grants.data.items.map((grant) => <tr key={grant.id}>
              <td>
                {grant.staff_email}</td>
              <td>
                {grant.allow_content ? "运行元数据与会话正文" : "仅运行元数据"}<small>
                  {grant.reason}</small>
              </td>
              <td>
                {dateTime(grant.expires_at)}</td>
              <td>
                <Badge status={grantActive(grant) ? "active" : "disabled"}>
                  {grant.revoked_at ? "已撤销" : grantActive(grant) ? "有效" : "已过期"}</Badge>
              </td>
              <td className="align-right">
                {grantActive(grant) && <Button variant="danger" onClick={() => setRevoking(grant)}>撤销</Button>}</td>
            </tr>)}
          </tbody>
        </table>
      </div> : <Empty title="没有支持授权" description="支持人员默认不能读取本组织的运行或会话。" />}
    </ResourceState>
    {creating && <Modal title="批准临时支持访问" description={`批准人：${user.name || user.email}。请核实支持人员邮箱和本次排查范围。`} close={() => setCreating(false)}>
      <Form close={() => setCreating(false)} label="批准访问" submit={async (form) => {
        await write("/support-grants", { staff_email: formValue(form, "staff_email"), reason: formValue(form, "reason"), minutes: Number(formValue(form, "minutes")), allow_content: (form.elements.namedItem("allow_content") as HTMLInputElement).checked });
        setCreating(false); await grants.reload(); toast("临时支持授权已批准");
      }}>
        <Field label="支持人员邮箱" hint="必须是已拥有平台支持角色的账号。">
          <input name="staff_email" type="email" required />
        </Field>
        <Field label="有效时长（分钟）">
          <input name="minutes" type="number" min={1} max={60} defaultValue={15} required />
        </Field>
        <Field label="排查原因">
          <textarea name="reason" minLength={5} maxLength={500} rows={3} required />
        </Field>
        <label className="checkbox-row">
          <input name="allow_content" type="checkbox" />允许读取本组织会话正文</label>
      </Form>
    </Modal>}
    {revoking && <Modal title="撤销支持访问" description={`${revoking.staff_email} 的后续请求将立即被拒绝。`} close={() => setRevoking(null)}>
      <Form close={() => setRevoking(null)} label="确认撤销" submit={async () => { await write(`/support-grants/${revoking.id}`, undefined, "DELETE"); setRevoking(null); await grants.reload(); }}>
        <p>
          {revoking.reason}</p>
      </Form>
    </Modal>}
  </Panel>;
}

export function SupportPage() {
  const grants = useResource<Items<Grant>>("/api/v2/support-grants");
  const [selected, setSelected] = useState("");
  const [clock, setClock] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setClock(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => { const timer = setInterval(() => void grants.reload(), 15000); return () => clearInterval(timer); }, [grants.reload]);
  const live = grants.data?.items.filter((grant) => !grant.revoked_at && grant.expires_at * 1000 > clock) ?? [];
  const current = grants.error ? undefined : live.find((grant) => grant.id === selected);
  return <>
    <PageTitle eyebrow="SUPPORT ACCESS" title="已批准的支持访问" description="按组织所有者授予的范围排查问题。支持身份独立留痕，无法代替成员运行 Agent 或修改组织配置。" action={<Button variant="secondary" onClick={() => void grants.reload()}>
      <RefreshCw size={16} />刷新授权</Button>} />
    <Panel title="当前有效授权">
      <ResourceState loading={grants.loading} error={grants.error} retry={() => void grants.reload()}>
        {live.length ? <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>组织</th>
                <th>排查范围</th>
                <th>到期时间</th>
                <th className="align-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {live.map((grant) => <tr key={grant.id}>
                <td>
                  {grant.tenant_name}<small>
                    {grant.reason}</small>
                </td>
                <td>
                  {grant.allow_content ? "元数据与会话正文" : "仅运行元数据"}</td>
                <td>
                  {dateTime(grant.expires_at)}</td>
                <td className="align-right">
                  <Button variant="secondary" onClick={() => setSelected(grant.id)}>查看运行</Button>
                </td>
              </tr>)}</tbody>
          </table>
        </div> : <Empty title="暂无有效支持授权" description="请组织所有者从支持访问授权页批准你的支持人员邮箱。" />}
      </ResourceState>
    </Panel>
    {current && <SupportDetail key={current.id} grant={current} />}
  </>;
}

function SupportDetail({ grant }: { grant: Grant }) {
  const prefix = `/api/v2/support-grants/${grant.id}`;
  const usage = useResource<Items<SupportRun>>(`${prefix}/usage`);
  const [content, setContent] = useState<{ id: string; messages: Message[]; has_more: boolean } | null>(null);
  const request = useRef<AbortController | null>(null);
  const toast = useToast();
  useEffect(() => () => { request.current?.abort(); }, []);
  async function openSession(id: string) {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    try { const value = await api<{ id: string; messages: Message[]; has_more: boolean }>(`${prefix}/sessions/${encodeURIComponent(id)}`, { signal: controller.signal }); if (!controller.signal.aborted) setContent(value); }
    catch (error) { if (!controller.signal.aborted) { setContent(null); toast((error as Error).message, "error"); } }
  }
  return <Panel title={`运行记录：${grant.tenant_name ?? grant.tenant_id}`} detail="显示最近 200 条运行元数据；每次读取都记录授权编号及真实操作人。" action={<Button variant="ghost" onClick={() => { setContent(null); void usage.reload(); }}>
    <RefreshCw size={16} />
  </Button>}>
    <ResourceState loading={usage.loading} error={usage.error} retry={() => void usage.reload()}>
      {usage.data?.items.length ? <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>运行 ID</th>
              <th>状态</th>
              <th>时间</th>
              <th className="align-right">会话</th>
            </tr>
          </thead>
          <tbody>
            {usage.data.items.map((run) => <tr key={run.id}>
              <td>
                {run.id}<small>
                  {run.session_id}</small>
              </td>
              <td>
                <Badge status={run.status} />
              </td>
              <td>
                {dateTime(run.created_at)}</td>
              <td className="align-right">
                {grant.allow_content ? <Button variant="ghost" onClick={() => void openSession(run.session_id)}>读取正文</Button> : "未授权正文"}</td>
            </tr>)}</tbody>
        </table>
      </div> : <Empty title="暂无运行记录" />}</ResourceState>
    {content && <Modal title="支持访问 · 会话正文" description={`会话 ${content.id} · 本次读取已审计`} close={() => setContent(null)}>
      <div className="form-body">
        {content.messages.map((message, index) => <div className="support-message" key={index}>
          <strong>
            {message.role}</strong>
          <pre>
            {message.content}</pre>
        </div>)}{content.has_more && <p className="inline-note">记录较长，本次仅返回前 500 条消息。</p>}{!content.messages.length && <Empty title="暂无消息" />}</div>
    </Modal>}
  </Panel>;
}

export function PlatformRolesPanel({ changed }: { changed: () => Promise<void> }) {
  const [user, setUser] = useState<RoleUser | null>(null);
  const [lookup, setLookup] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [searching, setSearching] = useState(false);
  const lookupController = useRef<AbortController | null>(null);
  const toast = useToast();
  useEffect(() => () => { lookupController.current?.abort(); }, []);
  async function find(email: string) {
    lookupController.current?.abort(); const controller = new AbortController(); lookupController.current = controller;
    setSearching(true); setError(""); setUser(null); setLookup(false);
    try { const result = await api<Items<RoleUser>>(`/api/v2/platform/users?email=${encodeURIComponent(email)}`, { signal: controller.signal }); if (!controller.signal.aborted) { setUser(result.items[0] ?? null); setLookup(true); } }
    catch (err) { if (!controller.signal.aborted) setError((err as Error).message); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  return <Panel title="平台角色" detail="平台管理员、财务和支持角色与组织成员角色分别管理。最后一位有效平台管理员不能被撤销。">
    <form className="toolbar" onSubmit={(event) => { event.preventDefault(); void find(formValue(event.currentTarget, "email")); }}>
      <input name="email" type="email" aria-label="查询平台账号邮箱" placeholder="按完整邮箱查询账号" maxLength={254} required />
      <Button type="submit" disabled={searching}>
        {searching ? "查询中…" : "查询账号"}</Button>
    </form>
    {error && <div className="form-error">
      {error}</div>}
    {lookup && !user && <Empty title="未找到账号" description="请先开通该邮箱的个人账号。" />}
    {user && <div className="tenant-detail-summary">
      <div>
        <strong>
          {user.name} · {user.email}</strong>
        <p>
          {user.platform_roles.map((role) => roleNames[role] ?? role).join("、") || "未授予平台角色"}</p>
      </div>
      <Button variant="secondary" onClick={() => setEditing(true)}>
        <UserCog size={16} />管理平台角色</Button>
    </div>}
    {editing && user && <Modal title={`平台角色：${user.email}`} close={() => setEditing(false)}>
      <Form close={() => setEditing(false)} label="确认变更" submit={async (form) => { await write(`/api/v2/platform/users/${user.id}/roles`, { role: formValue(form, "role"), active: formValue(form, "active") === "true", reason: formValue(form, "reason") }); setEditing(false); await Promise.all([find(user.email), changed()]); toast("平台角色已更新"); }}>
        <Field label="平台角色">
          <Select name="role" defaultValue="platform_support">
            {Object.entries(roleNames).map(([role, label]) => <option key={role} value={role}>
              {label}</option>)}</Select>
        </Field>
        <Field label="操作">
          <Select name="active" defaultValue="true">
            <option value="true">授予角色</option>
            <option value="false">撤销角色</option>
          </Select>
        </Field>
        <Field label="变更原因">
          <textarea name="reason" minLength={5} maxLength={500} rows={3} required />
        </Field>
      </Form>
    </Modal>}
  </Panel>;
}
