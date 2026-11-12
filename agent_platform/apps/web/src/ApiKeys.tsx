import { useState } from "react";
import { Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { dateTime, write } from "./api";
import { Button, Empty, Field, Form, Modal, PageTitle, Panel, ResourceState, Select, formValue, useResource, useToast } from "./components";
import type { KeyCreated, KeyList, KeyView } from "./api-contract.generated";
import type { Model } from "./types";
import "./api-keys.css";

export function ApiKeysPage() {
  const resource = useResource<KeyList>("/api-keys");
  const models = useResource<{ items: Model[] }>("/models");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<KeyCreated | null>(null);
  const [revoking, setRevoking] = useState<KeyView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [protocol, setProtocol] = useState("responses");
  const toast = useToast();
  const baseURL = `${location.origin}/v1`;
  const model = models.data?.items.find((item) => item.active)?.alias ?? "从模型目录复制模型别名";
  const example = protocol === "responses"
    ? `from openai import OpenAI\nimport os\n\nclient = OpenAI(base_url="${baseURL}", api_key=os.environ["AGENT_PLATFORM_API_KEY"], max_retries=0)\nresponse = client.responses.create(\n    model="${model}",\n    input="你好，请介绍一下你能做什么。",\n    store=False,\n    max_output_tokens=2048,\n)\nprint(response.output_text)`
    : `from openai import OpenAI\nimport os\n\nclient = OpenAI(base_url="${baseURL}", api_key=os.environ["AGENT_PLATFORM_API_KEY"], max_retries=0)\nstream = client.chat.completions.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "你好"}],\n    max_completion_tokens=2048,\n    stream=True,\n    stream_options={"include_usage": True},\n)\nfor chunk in stream:\n    if chunk.choices:\n        print(chunk.choices[0].delta.content or "", end="")`;
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); toast("已复制"); }
    catch { toast("无法复制，请手动选择文本", "error"); }
  }
  return <>
    <PageTitle eyebrow="API ACCESS" title="API 密钥" description="为自己的应用创建密钥，调用当前组织授权的模型。用量计入本人及组织的配额与费用。"
      action={<Button onClick={() => setCreating(true)}><Plus size={16} />创建密钥</Button>} />
    <div className="info-banner"><KeyRound size={20} /><div><strong>个人密钥仅在当前组织内有效</strong>
      <p>完整密钥只在创建时显示。成员资格或角色变化会使原密钥失效；已发送请求的用量仍会结算。</p></div></div>
    <ResourceState loading={resource.loading} error={resource.error} retry={() => void resource.reload()}>
      {resource.data?.items.length ? <div className="table-wrap api-key-table"><table><thead><tr><th>名称 / 密钥标识</th><th>模型范围</th><th>状态</th><th>到期时间</th><th>最近调用</th><th className="align-right">操作</th></tr></thead>
        <tbody>{resource.data.items.map((key) => {
          const expired = Date.parse(key.expires_at) <= Date.now();
          return <tr key={key.id}><td><strong>{key.name}</strong><small className="api-key-prefix">{key.prefix}</small></td>
            <td>{key.model_ids.length ? `${key.model_ids.length} 个指定模型` : "组织所有可用模型"}</td>
            <td>{key.revoked_at ? "已撤销" : expired ? "已过期" : "有效"}</td><td>{dateTime(key.expires_at)}</td><td>{key.last_used_at ? dateTime(key.last_used_at) : "尚未使用"}</td>
            <td className="align-right"><Button variant="ghost" disabled={Boolean(key.revoked_at)} onClick={() => { setError(""); setRevoking(key); }} aria-label={`撤销密钥：${key.name}`}><Trash2 size={15} />撤销</Button></td></tr>;
        })}</tbody></table></div> : <Empty title="还没有 API 密钥" description="创建密钥后，可从自己的应用调用模型。" />}
    </ResourceState>
    <Panel title="接入模型服务" detail="支持文本、客户端函数工具及流式输出；工具由调用方执行。使用 GET /v1/models 获取当前密钥可用的模型。">
      <div className="api-key-guide"><label>Base URL <code>{baseURL}</code></label><Button variant="secondary" onClick={() => void copy(baseURL)}><Copy size={14} />复制地址</Button></div>
      <p className="muted">Responses 使用完整 input 历史和 store=false。当前不支持托管会话、图片、音频或内置联网工具。模型 ID 使用模型目录中的别名。</p>
      <Select aria-label="调用示例接口" value={protocol} onChange={(event) => setProtocol(event.target.value)}><option value="responses">Responses</option><option value="chat">Chat Completions（流式）</option></Select>
      <pre className="api-key-example"><code>{example}</code></pre><Button variant="secondary" onClick={() => void copy(example)}><Copy size={14} />复制示例</Button>
    </Panel>
    {creating && <Modal title="创建 API 密钥" close={() => setCreating(false)}>
      <Form close={() => setCreating(false)} submit={async (form) => {
        const result = await write<KeyCreated>("/api-keys", { name: formValue(form, "name"), expires_in_days: Number(formValue(form, "expires_in_days")), model_ids: new FormData(form).getAll("model_ids") });
        setCreating(false); setCreated(result); await resource.reload();
      }}>
        <Field label="密钥名称"><input name="name" required maxLength={120} placeholder="例如：本地开发应用" /></Field>
        <Field label="有效期"><Select name="expires_in_days" defaultValue="90"><option value="7">7 天</option><option value="30">30 天</option><option value="90">90 天</option><option value="365">365 天</option></Select></Field>
        <Field label="模型范围" hint="不选择具体模型时，可调用组织当前及后续授权的所有可用模型。">
          <ResourceState loading={models.loading} error={models.error} retry={() => void models.reload()}>
            <div className="api-key-models">{models.data?.items.filter((item) => item.active).map((item) => <label key={item.id}><input type="checkbox" name="model_ids" value={item.id} />{item.name}<small>{item.alias}</small></label>)}</div>
          </ResourceState>
        </Field>
      </Form>
    </Modal>}
    {created && <Modal title="保存你的 API 密钥" description="完整密钥仅展示这一次，关闭后无法再次查看。" close={() => setCreated(null)}>
      <div className="form-body"><p>密钥名称：{created.name}</p><pre className="api-key-secret" aria-label="新创建的 API 密钥">{created.secret}</pre>
        <Button variant="secondary" onClick={() => void copy(created.secret)}><Copy size={14} />复制密钥</Button>
        <p className="muted">请保存在应用的环境变量 AGENT_PLATFORM_API_KEY 中。</p></div>
      <div className="form-footer"><Button onClick={() => setCreated(null)}>已保存，关闭</Button></div>
    </Modal>}
    {revoking && <Modal title="撤销 API 密钥" close={() => { if (!busy) setRevoking(null); }}>
      <div className="form-body"><p>撤销「{revoking.name}」后，该密钥将无法继续调用模型。需要时可创建新密钥。</p>{error && <p role="alert" className="error-text">{error}</p>}</div>
      <div className="form-footer"><Button variant="secondary" disabled={busy} onClick={() => setRevoking(null)}>取消</Button><Button variant="danger" busy={busy} onClick={async () => {
        setBusy(true); setError("");
        try { await write(`/api-keys/${revoking.id}`, undefined, "DELETE"); setRevoking(null); await resource.reload(); toast("密钥已撤销"); }
        catch (cause) { setError(cause instanceof Error ? cause.message : "撤销失败"); }
        finally { setBusy(false); }
      }}>确认撤销</Button></div>
    </Modal>}
  </>;
}
