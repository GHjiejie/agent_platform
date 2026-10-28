import { Select } from "./Select";
import { useState } from "react";
import {
  Activity,
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  Bot,
  Boxes,
  CircleDollarSign,
  Clock3,
  Copy,
  CreditCard,
  Download,
  ExternalLink,
  Layers,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Wallet,
  Zap,
} from "lucide-react";
import { dateTime, money, number, write } from "./api";
import type {
  Agent,
  Audit,
  Dashboard,
  GatewayAdmin,
  Ledger,
  Model,
  Membership,
  Quota,
  Run,
  Usage,
  User,
  Wallet as WalletData,
} from "./types";
import {
  Badge,
  Button,
  Empty,
  Field,
  Form,
  Modal,
  PageTitle,
  Panel,
  ResourceState,
  SearchBox,
  TextLink,
  formValue,
  useResource,
  useToast,
  statusLabel,
} from "./components";

import { ModelPolicyPanel } from "./PlatformResources";

type Items<T> = { items: T[] };

export function DashboardPage({
  user,
  navigate,
}: {
  user: User;
  navigate: (path: string) => void;
}) {
  const resource = useResource<Dashboard>("/dashboard");
  const runs = useResource<Items<Run>>("/runs");
  const value = resource.data;
  const maxRequests = Math.max(
    1,
    ...(value?.daily.map((day) => day.requests) ?? []),
  );
  return (
    <>
      <PageTitle
        eyebrow="OVERVIEW"
        title={`你好，${user.name || "欢迎回来"} 👋`}
        description="查看工作空间运行状态，掌握每一次智能调用。"
        action={
          <Button onClick={() => navigate("playground")}>
            <Sparkles size={16} />
            开始对话
            <ArrowUpRight size={15} />
          </Button>
        }
      />
      <ResourceState
        loading={resource.loading}
        error={resource.error}
        retry={() => void resource.reload()}
      >
        {value && (
          <>
            {!value.gateway_configured && (
              <div className="info-banner">
                <Boxes size={20} />
                <div>
                  <strong>连接模型网关，开启第一次运行</strong>
                  <p>
                    LiteLLM
                    模型尚未就绪。请联系平台运营人员授予模型并完成连接配置。
                  </p>
                </div>
                <Button variant="secondary" onClick={() => navigate("models")}>
                  查看模型
                  <ArrowRight size={15} />
                </Button>
              </div>
            )}
            <WalletAlert wallet={value} />
            <div className="stat-grid">
              <Stat
                label="累计模型调用"
                value={number(value.requests)}
                unit="次"
                icon={<Activity size={19} />}
                detail={`${value.active_runs} 个 Agent 任务正在运行`}
              />
              <Stat
                label="累计 Token 用量"
                value={number(value.tokens)}
                unit="tokens"
                icon={<Zap size={19} />}
                detail="输入与输出 Token 合计"
              />
              <Stat
                label="累计调用费用"
                value={money(value.cost)}
                icon={<CircleDollarSign size={19} />}
                detail="已确认的客户费用 · USD"
              />
              <Stat
                label="可用余额"
                value={money(value.available)}
                icon={<Wallet size={19} />}
                detail={`预占中 ${money(value.reserved)}`}
                accent
              />
            </div>
            <div className="dashboard-columns">
              <Panel
                title="调用趋势"
                detail="按日期查看模型调用量"
                action={
                  <span className="chart-legend">
                    <i />
                    模型调用
                  </span>
                }
              >
                <div
                  className={`bar-chart ${value.daily.length > 14 ? "dense" : ""}`}
                  role="img"
                  aria-label="每日模型调用趋势"
                >
                  {value.daily.length ? (
                    value.daily.map((day) => (
                      <div className="bar-column" key={day.date}>
                        <div className="bar-value">
                          {day.requests > 0 ? number(day.requests) : ""}
                        </div>
                        <div className="bar-track">
                          <div
                            className="bar-fill"
                            style={{
                              height: `${(day.requests / maxRequests) * 100}%`,
                              minHeight: day.requests ? 4 : 0,
                            }}
                            title={`${day.date}：${number(day.requests)} 次调用，${number(day.tokens)} tokens，${money(day.cost)}`}
                          />
                        </div>
                        <span>{day.date.slice(5)}</span>
                      </div>
                    ))
                  ) : (
                    <Empty
                      title="等待第一次调用"
                      description="开始运行 Agent 后，调用趋势会显示在这里。"
                    />
                  )}
                </div>
                <div className="chart-footer">
                  <span>所有用量均来自实际调用记录</span>
                  <TextLink onClick={() => navigate("usage")}>
                    查看明细
                  </TextLink>
                </div>
              </Panel>
              <Panel
                title="模型分布"
                detail="按累计调用次数统计"
                className="model-distribution"
              >
                {value.models.length ? (
                  <div className="distribution-list">
                    {value.models.slice(0, 5).map((model, index) => (
                      <div key={model.model} className="distribution-item">
                        <div className={`model-icon color-${index % 4}`}>
                          <Boxes size={18} />
                        </div>
                        <div>
                          <strong>{model.model}</strong>
                          <div className="progress">
                            <i
                              style={{
                                width: `${(model.requests / Math.max(value.requests, 1)) * 100}%`,
                              }}
                            />
                          </div>
                        </div>
                        <span>
                          {number(model.requests)}
                          <small>次调用</small>
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty
                    title="暂无模型用量"
                    description="模型调用完成后生成统计。"
                  />
                )}
                <div className="model-footer">
                  <ShieldCheck size={15} />
                  <span>成功率</span>
                  <strong>
                    {value.requests
                      ? `${Number(value.success_rate).toFixed(1)}%`
                      : "—"}
                  </strong>
                </div>
              </Panel>
            </div>
            <Panel
              title="最近运行"
              detail="最新的 Agent 执行记录"
              action={
                <TextLink onClick={() => navigate("runs")}>全部记录</TextLink>
              }
            >
              <ResourceState
                loading={runs.loading}
                error={runs.error}
                retry={() => void runs.reload()}
              >
                {runs.data?.items.length ? (
                  <RunTable
                    items={runs.data.items.slice(0, 5)}
                    navigate={navigate}
                  />
                ) : (
                  <Empty
                    title="从一次对话开始"
                    description="配置并发布你的 Agent，让第一个想法运行起来。"
                    action={
                      <Button
                        variant="secondary"
                        onClick={() => navigate("agents")}
                      >
                        查看 Agent
                        <ArrowRight size={15} />
                      </Button>
                    }
                  />
                )}
              </ResourceState>
            </Panel>
          </>
        )}
      </ResourceState>
    </>
  );
}
function Stat({
  label,
  value,
  unit,
  icon,
  detail,
  accent = false,
}: {
  label: string;
  value: string;
  unit?: string;
  icon: React.ReactNode;
  detail: string;
  accent?: boolean;
}) {
  return (
    <div className={`stat-card ${accent ? "accent" : ""}`}>
      <div className="stat-top">
        <span>{label}</span>
        <div className="stat-icon">{icon}</div>
      </div>
      <div className="stat-value">
        {value}
        <small>{unit}</small>
      </div>
      <div className="stat-detail">{detail}</div>
    </div>
  );
}

export function AgentsPage({
  admin,
  navigate,
}: {
  admin: boolean;
  navigate: (path: string) => void;
}) {
  const agents = useResource<Items<Agent>>("/agents");
  const models = useResource<Items<Model>>("/models");
  const executionOptions = useResource<import("./types").ExecutionOptions>("/agents/execution-options");
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [editing, setEditing] = useState<Agent | "new" | null>(null);
  const [publishing, setPublishing] = useState<string | null>(null);
  const toast = useToast();
  const items =
    agents.data?.items.filter((agent) =>
      (source === "all" ||
        (source === "builtin" ? Boolean(agent.builtin_key) : !agent.builtin_key)) &&
      `${agent.name} ${agent.description} ${agent.category ?? ""}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) ?? [];
  async function publish(agent: Agent) {
    setPublishing(agent.id);
    try {
      const result = await write<{ version: number }>(
        `/agents/${agent.id}/publish`,
      );
      toast(`「${agent.name}」已发布为 v${result.version}`);
      await agents.reload();
    } catch (error) {
      toast((error as Error).message, "error");
    } finally {
      setPublishing(null);
    }
  }
  return (
    <>
      <PageTitle
        eyebrow="AGENTS"
        title="我的 Agent"
        description="内置实用助手，选择任务即可开始。模型可随时切换，角色与工作方式保持独立。"
        action={
          admin && (
            <Button onClick={() => setEditing("new")}>
              <Plus size={17} />
              创建 Agent
            </Button>
          )
        }
      />
      <div className="list-toolbar">
        <SearchBox
          value={search}
          onChange={setSearch}
          placeholder="搜索 Agent 名称或描述"
        />
        <Select
          className="agent-source-filter"
          aria-label="筛选 Agent 来源"
          value={source}
          onChange={(event) => setSource(event.target.value)}
        >
          <option value="all">全部 Agent</option>
          <option value="builtin">内置 Agent</option>
          <option value="custom">自建 Agent</option>
        </Select>
        <span>{items.length} 个 Agent</span>
        <Button
          variant="ghost"
          onClick={() => void agents.reload()}
          aria-label="刷新 Agent"
        >
          <RefreshCw size={16} />
        </Button>
      </div>
      <ResourceState
        loading={agents.loading}
        error={agents.error}
        retry={() => void agents.reload()}
      >
        {items.length ? (
          <div className="agent-grid">
            {items.map((agent, index) => (
              <article className="agent-card" key={agent.id}>
                <div className="agent-card-top">
                  <div className={`agent-icon color-${index % 4}`}>
                    <Bot size={25} strokeWidth={1.7} />
                  </div>
                  {agent.published_version ? (
                    <Badge status="active">
                      已发布 · v{agent.published_version}
                    </Badge>
                  ) : (
                    <Badge status="draft" />
                  )}
                </div>
                <h2>{agent.name}</h2>
                {agent.builtin_key && (
                  <span className="agent-origin">内置 · {agent.category}</span>
                )}
                <p>{agent.description || "还没有填写 Agent 描述。"}</p>
                <div className="agent-model">
                  <Boxes size={14} />
                  {!agent.model_id
                    ? "自动选择可用模型"
                    : models.data?.items.find(
                        (model) => model.id === agent.model_id,
                      )?.name ?? agent.model_id}
                </div>
                <div className="agent-tags">
                  <span>
                    <Layers size={12} />
                    最多 {agent.max_steps} 步
                  </span>
                  <span>{agent.tools.length} 个工具</span>
                  <span>{number(agent.max_tokens)} 输出 tokens</span>
                </div>
                <div className="agent-card-actions">
                  {admin && (
                    <>
                      <Button variant="ghost" onClick={() => setEditing(agent)}>
                        <Pencil size={14} />
                        编辑
                      </Button>
                      <Button
                        variant="ghost"
                        busy={publishing === agent.id}
                        onClick={() => void publish(agent)}
                      >
                        发布{agent.published_version ? "新版" : ""}
                      </Button>
                    </>
                  )}
                  <Button
                    variant="secondary"
                    disabled={!agent.published_version}
                    onClick={() => navigate(`playground?agent=${agent.id}`)}
                  >
                    <Play size={13} />
                    运行
                  </Button>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <Panel>
            <Empty
              title={search ? "未找到匹配的 Agent" : "创建你的第一个 Agent"}
              description={
                search
                  ? "试试其他名称或关键词。"
                  : "定义角色、选择模型并发布，即可开始多轮对话。"
              }
              action={
                admin && !search ? (
                  <Button onClick={() => setEditing("new")}>
                    <Plus size={16} />
                    创建 Agent
                  </Button>
                ) : undefined
              }
            />
          </Panel>
        )}
      </ResourceState>
      {editing && (
        <AgentEditor
          agent={editing === "new" ? null : editing}
          models={models.data?.items ?? []}
          executionOptions={executionOptions.data}
          close={() => setEditing(null)}
          saved={async () => {
            setEditing(null);
            toast("Agent 草稿已保存，发布后会用于后续运行");
            await agents.reload();
          }}
        />
      )}
    </>
  );
}
function AgentEditor({
  agent,
  models,
  executionOptions,
  close,
  saved,
}: {
  agent: Agent | null;
  models: Model[];
  executionOptions: import("./types").ExecutionOptions | null;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [selectedModel, setSelectedModel] = useState<string>(
    agent?.model_id ?? "",
  );
  const policy = agent?.execution_policy;
  const modelLimit = models.find((model) => model.id === selectedModel)?.max_output_tokens;
  return (
    <Modal
      title={agent ? "编辑 Agent 草稿" : "创建 Agent"}
      description="修改先保存为草稿；发布后，后续运行使用新版本。"
      close={close}
    >
      <Form
        close={close}
        label="保存草稿"
        submit={async (form) => {
          const data = new FormData(form);
          const payload = {
            name: formValue(form, "name"),
            description: formValue(form, "description"),
            system_prompt: formValue(form, "system_prompt"),
            model_id: formValue(form, "model_id") || null,
            temperature: Number(formValue(form, "temperature")),
            max_steps: Number(formValue(form, "max_steps")),
            max_tokens: Number(formValue(form, "max_tokens")),
            execution_policy: {
              reasoning_effort: formValue(form, "reasoning_effort") || null,
              timeout_seconds: formValue(form, "timeout_seconds") ? Number(formValue(form, "timeout_seconds")) : null,
              max_total_tokens: formValue(form, "max_total_tokens") ? Number(formValue(form, "max_total_tokens")) : null,
              max_cost: formValue(form, "max_cost") || null,
              max_output_recoveries: Number(formValue(form, "max_output_recoveries")),
            },
            tools: data.getAll("tools"),
          };
          await write(
            agent ? `/agents/${agent.id}` : "/agents",
            payload,
            agent ? "PATCH" : "POST",
          );
          await saved();
        }}
      >
        <div className="form-columns">
          <Field label="Agent 名称">
            <input
              name="name"
              required
              maxLength={100}
              defaultValue={agent?.name}
              placeholder="例如：研究助手"
            />
          </Field>
          <Field
            label="使用模型"
            hint="自动模式优先使用默认模型，也可在对话中指定其他已启用模型。"
          >
            <Select
              name="model_id"
              value={selectedModel}
              onChange={(event) => setSelectedModel(event.target.value)}
            >
              <option value="">
                自动选择可用模型
              </option>
              {models
                .filter((model) => model.active || model.id === agent?.model_id)
                .map((model) => (
                  <option value={model.id} key={model.id}>
                    {model.name}
                    {!model.active ? "（已停用）" : ""}
                  </option>
                ))}
            </Select>
          </Field>
        </div>
        <Field label="简短描述">
          <input
            name="description"
            defaultValue={agent?.description}
            maxLength={500}
            placeholder="这个 Agent 擅长什么？"
          />
        </Field>
        <Field
          label="系统指令"
          hint="描述角色、目标与输出要求。请不要填写服务端密钥。"
        >
          <textarea
            name="system_prompt"
            rows={6}
            maxLength={20000}
            required
            defaultValue={
              agent?.system_prompt ??
              "你是一位严谨、友好的智能助手。请用中文清晰地回答用户的问题。"
            }
          />
        </Field>
        <div className="form-columns three">
          <Field label="Temperature">
            <input
              name="temperature"
              type="number"
              min={0}
              max={2}
              step={0.1}
              defaultValue={agent?.temperature ?? 1}
              required
            />
          </Field>
          <Field label="最大执行步数" hint="模型调用总次数，包含输出上限后的恢复调用。">
            <input
              name="max_steps"
              type="number"
              min={1}
              max={executionOptions?.max_steps}
              defaultValue={agent?.max_steps ?? 6}
              required
            />
          </Field>
          <Field label="单次输出 Token 上限" hint={modelLimit ? `所选模型最多 ${number(modelLimit)} tokens。` : "运行时与实际所选模型的输出上限取较小值。"}>
            <input
              name="max_tokens"
              type="number"
              min={1}
              max={modelLimit ?? executionOptions?.max_output_tokens}
              defaultValue={agent?.max_tokens ?? executionOptions?.default_output_tokens ?? 131072}
              required
            />
          </Field>
        </div>
        <fieldset className="agent-execution-policy">
          <legend>执行策略</legend>
          <p className="muted">留空的限制继承平台配置。修改发布后用于新运行，正在执行的任务保持原版本。</p>
          <div className="form-columns">
            <Field label="思考强度" hint="继承时沿用模型网关默认值；显式值需要所选供应商支持。">
              <Select name="reasoning_effort" defaultValue={policy?.reasoning_effort ?? ""}>
                <option value="">继承模型默认</option>
                <option value="none">关闭（none）</option>
                <option value="minimal">最少（minimal）</option>
                <option value="low">低（low）</option>
                <option value="medium">中（medium）</option>
                <option value="high">高（high）</option>
                <option value="xhigh">很高（xhigh）</option>
                <option value="max">最高（max）</option>
              </Select>
            </Field>
            <Field label="运行时限（秒）" hint={executionOptions ? `平台上限：${executionOptions.timeout_seconds} 秒。` : "留空继承平台运行时限。"}>
              <input name="timeout_seconds" type="number" min={1} max={executionOptions?.timeout_seconds} step={1} defaultValue={policy?.timeout_seconds ?? ""} placeholder="继承平台" />
            </Field>
            <Field label="累计 Token 预算" hint="覆盖本次运行全部模型调用的输入和输出；调用前按保守输入估算及输出预留检查。">
              <input name="max_total_tokens" type="number" min={1} max={2000000000} step={1} defaultValue={policy?.max_total_tokens ?? ""} placeholder="不另设累计 Token 限制" />
            </Field>
            <Field label="运行费用上限（USD）" hint={executionOptions ? `平台上限：${executionOptions.max_cost} USD，调用前检查费用预留。` : "留空继承平台费用上限。"}>
              <input name="max_cost" type="number" min="0.000000000001" max={executionOptions?.max_cost} step="any" defaultValue={policy?.max_cost ?? ""} placeholder="继承平台" />
            </Field>
            <Field label="输出上限恢复次数" hint="0 表示停止；大于 0 时，请求新的完整结果。仅恢复用量已确认的输出截断，不重试费用未知的请求。">
              <input name="max_output_recoveries" type="number" min={0} max={10} step={1} required defaultValue={policy?.max_output_recoveries ?? 0} />
            </Field>
          </div>
        </fieldset>
        <div className="field">
          <span>允许的工具</span>
          <div className="tool-choices">
            <label>
              <input
                type="checkbox"
                name="tools"
                value="calculator"
                defaultChecked={agent?.tools.includes("calculator")}
              />
              <span>
                计算器<small>执行受控算术运算</small>
              </span>
            </label>
            <label>
              <input
                type="checkbox"
                name="tools"
                value="current_time"
                defaultChecked={agent?.tools.includes("current_time")}
              />
              <span>
                当前时间<small>查询当前日期与时间</small>
              </span>
            </label>
            <label>
              <input type="checkbox" name="tools" value="generate_sandboxed_ui"
                defaultChecked={agent?.tools.includes("generate_sandboxed_ui")} />
              <span>生成可视化界面<small>在对话中生成交互图表、演示和小工具</small></span>
            </label>
          </div>
        </div>
        {!models.some((model) => model.active) && (
          <div className="inline-note">
            可以先保存并发布 Agent；运行前请在模型目录添加并启用模型。
          </div>
        )}
      </Form>
    </Modal>
  );
}

export function GatewayPage() {
  const resource = useResource<GatewayAdmin>("/gateway");
  const gateway = resource.data;
  return (
    <>
      <PageTitle
        eyebrow="MODEL GATEWAY"
        title="LiteLLM 网关"
        description="进入 LiteLLM 管理台，管理上游模型、访问密钥与网关调用用量。"
        action={
          <Button variant="secondary" onClick={() => void resource.reload()}>
            <RefreshCw size={16} />
            刷新配置
          </Button>
        }
      />
      <ResourceState
        loading={resource.loading}
        error={resource.error}
        retry={() => void resource.reload()}
      >
        {gateway?.configured && gateway.admin_url ? (
          <Panel
            title="LiteLLM Admin UI"
            detail="管理入口已配置"
            className="gateway-panel"
          >
            <div className="gateway-details">
              <p>
                管理台将在新标签页中打开，请使用 LiteLLM 管理员账号登录。
                在这里配置模型供应商、虚拟密钥、网关限流和调用记录。
              </p>
              <span className="gateway-address">{gateway.admin_url}</span>
              <a
                className="button primary"
                href={gateway.admin_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                打开 LiteLLM 管理台
                <ExternalLink size={16} />
              </a>
              <p className="gateway-help">
                如果页面无法打开，请检查 LiteLLM 服务是否已启动。
                此处显示的是入口配置状态。
              </p>
            </div>
          </Panel>
        ) : gateway ? (
          <Panel>
            <Empty
              title="尚未配置 LiteLLM 管理入口"
              description="启动 LiteLLM 管理服务并配置管理入口后，刷新此页即可打开管理台。"
            />
          </Panel>
        ) : null}
      </ResourceState>
      <div className="inline-note gateway-note">
        <ShieldCheck size={17} />
        <span>
          Agent 平台的模型报价、成员余额和 Agent 运行记录仍在本平台管理。
          LiteLLM 的调用费用反映网关侧的统计口径。
        </span>
      </div>
    </>
  );
}

export function ModelsPage({ admin, canPolicy = false }: { admin: boolean; canPolicy?: boolean }) {
  const resource = useResource<Items<Model> & { default_model?: string }>(
    "/models",
  );
  const tokenDefaults = useResource<import("./types").TokenDefaults>("/token-defaults");
  const [editing, setEditing] = useState<Model | "new" | null>(null);
  const [search, setSearch] = useState("");
  const toast = useToast();
  const defaultToAdd = resource.data?.items.some(
    (model) => model.alias === resource.data?.default_model,
  ) ? "" : resource.data?.default_model ?? "";
  const items =
    resource.data?.items.filter((model) =>
      `${model.name} ${model.alias}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) ?? [];
  return (
    <>
      <PageTitle
        eyebrow="MODEL CATALOG"
        title="模型目录"
        description="查看当前组织获授权的模型与调用价格，选择合适的模型运行 Agent。"
        action={
          admin && (
            <Button onClick={() => setEditing("new")}>
              <Plus size={17} />
              添加模型
            </Button>
          )
        }
      />
      <div className="inline-note">
        <ShieldCheck size={17} />
        模型由平台运营人员授权。价格单位：USD / 百万 Token；每次运行保存所用价格。
      </div>
      {admin && defaultToAdd && (
          <div className="inline-note">
            已配置默认模型 {defaultToAdd}。添加模型并填写平台报价后即可使用，
            名称和别名已预填。
          </div>
        )}
      {canPolicy && <ModelPolicyPanel models={resource.data?.items ?? []} changed={resource.reload} />}
      <Panel>
        <div className="table-toolbar">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="搜索模型名称或别名"
          />
          <span>{items.length} 个模型</span>
        </div>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>状态</th>
                    <th>输入价格</th>
                    <th>输出价格</th>
                    <th>上下文 / 输出上限</th>
                    {admin && <th className="align-right">操作</th>}
                  </tr>
                </thead>
                <tbody>
                  {items.map((model) => (
                    <tr key={model.id}>
                      <td>
                        <div className="table-identity">
                          <div className="model-icon">
                            <Boxes size={19} />
                          </div>
                          <div>
                            <strong>
                              {model.name}{model.is_default ? "（默认）" : ""}
                            </strong>
                            <small>{model.alias}</small>
                          </div>
                        </div>
                      </td>
                      <td>
                        <Badge status={model.active ? "active" : "disabled"} />
                      </td>
                      <td className="numeric">{money(model.input_price)}</td>
                      <td className="numeric">{money(model.output_price)}</td>
                      <td className="muted">
                        {number(model.context_window)} /{" "}
                        {number(model.max_output_tokens)}
                      </td>
                      {admin && (
                        <td className="align-right">
                          <Button
                            variant="ghost"
                            onClick={() => setEditing(model)}
                          >
                            <Pencil size={14} />
                            编辑
                          </Button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="暂无模型"
              description="管理员添加模型后，即可在 Agent 中选择使用。"
            />
          )}
        </ResourceState>
      </Panel>
      {editing && (
        <Modal
          title={editing === "new" ? "添加模型" : "编辑模型"}
          description="配置平台展示与报价；上游凭据仅在服务端管理。"
          close={() => setEditing(null)}
        >
          <Form
            close={() => setEditing(null)}
            submit={async (form) => {
              const model = editing === "new" ? null : editing;
              await write(
                model ? `/models/${model.id}` : "/models",
                {
                  name: formValue(form, "name"),
                  alias: formValue(form, "alias"),
                  input_price: formValue(form, "input_price"),
                  output_price: formValue(form, "output_price"),
                  context_window: Number(formValue(form, "context_window")),
                  max_output_tokens: Number(
                    formValue(form, "max_output_tokens"),
                  ),
                  active: new FormData(form).has("active"),
                },
                model ? "PATCH" : "POST",
              );
              setEditing(null);
              toast("模型配置已保存");
              await resource.reload();
            }}
          >
            <Field label="显示名称">
              <input
                name="name"
                required
                defaultValue={
                  editing === "new" ? defaultToAdd : editing.name
                }
                placeholder="例如：团队通用模型"
              />
            </Field>
            <Field label="LiteLLM 模型别名">
              <input
                name="alias"
                required
                defaultValue={
                  editing === "new" ? defaultToAdd : editing.alias
                }
                placeholder="与网关 model_name 保持一致"
              />
            </Field>
            <div className="form-columns">
              <Field
                label="输入价格（USD / 百万 Token）"
                hint="第一版需大于零，最多 12 位小数。"
              >
                <input
                  name="input_price"
                  type="number"
                  min="0.000000000001"
                  max={1000000}
                  step="any"
                  required
                  defaultValue={editing === "new" ? "" : editing.input_price}
                />
              </Field>
              <Field
                label="输出价格（USD / 百万 Token）"
                hint="第一版需大于零，最多 12 位小数。"
              >
                <input
                  name="output_price"
                  type="number"
                  min="0.000000000001"
                  max={1000000}
                  step="any"
                  required
                  defaultValue={editing === "new" ? "" : editing.output_price}
                />
              </Field>
              <Field label="上下文窗口（Token）">
                <input
                  name="context_window"
                  type="number"
                  min={256}
                  max={2000000}
                  required
                  defaultValue={
                    editing === "new" ? tokenDefaults.data?.context_window ?? 1048576 : editing.context_window
                  }
                />
              </Field>
              <Field label="最大输出（Token）">
                <input
                  name="max_output_tokens"
                  type="number"
                  min={1}
                  required
                  defaultValue={
                    editing === "new" ? tokenDefaults.data?.output_tokens ?? 131072 : editing.max_output_tokens
                  }
                />
              </Field>
            </div>
            <label className="checkbox-row">
              <input
                name="active"
                type="checkbox"
                defaultChecked={editing === "new" || editing.active}
              />
              启用模型
            </label>
          </Form>
        </Modal>
      )}
    </>
  );
}

export function UsersPage({ currentUser }: { currentUser: User }) {
  const resource = useResource<Items<User>>("/users");
  const [editing, setEditing] = useState<User | "new" | null>(null);
  const [search, setSearch] = useState("");
  const toast = useToast();
  const items =
    resource.data?.items.filter((user) =>
      `${user.name} ${user.email}`.toLowerCase().includes(search.toLowerCase()),
    ) ?? [];
  return (
    <>
      <PageTitle
        eyebrow="TEAM MEMBERS"
        title="成员管理"
        description="管理团队账号、角色和访问状态。"
        action={
          <Button onClick={() => setEditing("new")}>
            <Plus size={17} />
            添加成员
          </Button>
        }
      />
      <Panel>
        <div className="table-toolbar">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="搜索姓名或邮箱"
          />
          <span>{items.length} 位成员</span>
        </div>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>成员</th>
                    <th>角色</th>
                    <th>账号状态</th>
                    <th className="align-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((user) => (
                    <tr key={user.id}>
                      <td>
                        <div className="table-identity">
                          <span className="avatar">
                            {(user.name || user.email)
                              .slice(0, 1)
                              .toUpperCase()}
                          </span>
                          <div>
                            <strong>
                              {user.name}
                              {user.id === currentUser.id && (
                                <span className="you">你</span>
                              )}
                            </strong>
                            <small>{user.email}</small>
                          </div>
                        </div>
                      </td>
                      <td>
                        <span className={`role-pill ${user.role}`}>
                          {user.role === "admin" ? "管理员" : "成员"}
                        </span>
                      </td>
                      <td>
                        <Badge status={user.active ? "active" : "disabled"} />
                      </td>
                      <td className="align-right">
                        <Button
                          variant="ghost"
                          onClick={() => setEditing(user)}
                        >
                          <Pencil size={14} />
                          编辑
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="没有匹配的成员" />
          )}
        </ResourceState>
      </Panel>
      {editing && (
        <Modal
          title={editing === "new" ? "添加团队成员" : "编辑成员"}
          description="管理员可以管理资源与额度；成员可以运行 Agent 并查看自己的记录。"
          close={() => setEditing(null)}
        >
          <Form
            close={() => setEditing(null)}
            label={editing === "new" ? "创建账号" : "保存修改"}
            submit={async (form) => {
              const user = editing === "new" ? null : editing;
              const payload = {
                name: formValue(form, "name"),
                role: formValue(form, "role"),
                ...(user
                  ? { active: new FormData(form).has("active") }
                  : {
                      email: formValue(form, "email"),
                      password: formValue(form, "password"),
                    }),
              };
              await write(
                user ? `/users/${user.id}` : "/users",
                payload,
                user ? "PATCH" : "POST",
              );
              setEditing(null);
              toast(user ? "成员信息已更新" : "成员账号已创建");
              await resource.reload();
            }}
          >
            <Field label="姓名">
              <input
                name="name"
                required
                maxLength={100}
                defaultValue={editing === "new" ? "" : editing.name}
              />
            </Field>
            {editing === "new" && (
              <>
                <Field label="邮箱">
                  <input
                    name="email"
                    type="email"
                    autoComplete="off"
                    required
                  />
                </Field>
                <Field
                  label="初始密码"
                  hint="至少 12 个字符；请通过安全渠道交给成员，登录后可自行修改。"
                >
                  <input
                    name="password"
                    type="password"
                    autoComplete="new-password"
                    required
                    minLength={12}
                  />
                </Field>
              </>
            )}
            <Field label="角色">
              <Select
                name="role"
                defaultValue={editing === "new" ? "member" : editing.role}
              >
                <option value="member">成员</option>
                <option value="admin">管理员</option>
              </Select>
            </Field>
            {editing !== "new" && (
              <label className="checkbox-row">
                <input
                  name="active"
                  type="checkbox"
                  defaultChecked={editing.active}
                />
                账号启用
              </label>
            )}
          </Form>
        </Modal>
      )}
    </>
  );
}

export function UsagePage() {
  const resource = useResource<Items<Usage>>("/usage/calls");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const items =
    resource.data?.items.filter(
      (call) =>
        `${call.model} ${call.agent_name} ${call.user_email}`
          .toLowerCase()
          .includes(search.toLowerCase()) &&
        (status === "all" || call.status === status),
    ) ?? [];
  function download() {
    const rows = [
      [
        "时间",
        "模型",
        "Agent",
        "用户",
        "状态",
        "输入 tokens",
        "输出 tokens",
        "客户费用 USD",
      ],
      ...items.map((call) => [
        call.created_at,
        call.model,
        call.agent_name,
        call.user_email,
        call.status,
        call.input_tokens,
        call.output_tokens,
        call.cost,
      ]),
    ];
    const csv =
      "\uFEFF" +
      rows
        .map((row) =>
          row
            .map(
              (value) =>
                `"${String(value)
                  .replace(/^(?:\s*[=+\-@]|[\t\r\n])/, "'$&")
                  .replaceAll('"', '""')}"`,
            )
            .join(","),
        )
        .join("\r\n");
    const url = URL.createObjectURL(
      new Blob([csv], { type: "text/csv;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `agent-usage-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  return (
    <>
      <PageTitle
        eyebrow="USAGE & ANALYTICS"
        title="调用统计"
        description="追踪每次模型调用的用量、结果与费用。"
        action={
          <Button
            variant="secondary"
            onClick={download}
            disabled={!items.length}
          >
            <Download size={16} />
            导出当前结果
          </Button>
        }
      />
      <Panel>
        <div className="table-toolbar">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="搜索模型、Agent 或用户"
          />
          <Select
            aria-label="调用状态"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="all">全部状态</option>
            {Array.from(
              new Set(resource.data?.items.map((call) => call.status)),
            ).map((value) => (
              <option value={value} key={value}>
                {statusLabel(value)}
              </option>
            ))}
          </Select>
          <Button
            variant="ghost"
            aria-label="刷新调用统计"
            onClick={() => void resource.reload()}
          >
            <RefreshCw size={16} />
          </Button>
        </div>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>模型 / Agent</th>
                    <th>用户</th>
                    <th>状态</th>
                    <th>输入 / 输出 Token</th>
                    <th>客户费用</th>
                    <th>调用时间</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((call) => (
                    <tr key={call.id}>
                      <td>
                        <strong>{call.model}</strong>
                        <small>{call.agent_name || "—"}</small>
                      </td>
                      <td>{call.user_email}</td>
                      <td>
                        <Badge status={call.status} />
                      </td>
                      <td className="numeric">
                        {number(call.input_tokens)}{" "}
                        <span className="muted">/</span>{" "}
                        {number(call.output_tokens)}
                      </td>
                      <td className="numeric">{money(call.cost, 6)}</td>
                      <td className="nowrap muted">
                        {dateTime(call.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="暂无调用明细"
              description="实际调用发生后，将显示 Token、费用与结算状态。"
            />
          )}
        </ResourceState>
        <div className="table-footer">
          显示当前返回的 {items.length} 条记录 · 费用按调用时的价格快照计算
        </div>
      </Panel>
    </>
  );
}

export function BillingPage({ admin }: { admin: boolean }) {
  const wallet = useResource<WalletData>("/billing/wallet");
  const ledger = useResource<Items<Ledger>>("/billing/ledger", admin);
  const ledgerLabels: Record<string, string> = {
    credit: "额度入账",
    debit: "调用消费",
    charge: "调用消费",
    consumption: "调用消费",
    refund: "退款",
    adjustment: "账务调整",
  };
  return (
    <>
      <PageTitle
        eyebrow="BILLING"
        title="费用中心"
        description="管理可用额度，查看预占金额与不可变账本记录。"
      />
      <ResourceState
        loading={wallet.loading}
        error={wallet.error}
        retry={() => void wallet.reload()}
      >
        {wallet.data && (
          <div className="wallet-grid">
            <div className="wallet-hero">
              <div className="wallet-top">
                <span>
                  <Wallet size={19} />
                  工作空间钱包
                </span>
                <span>USD</span>
              </div>
              <p>可用余额</p>
              <strong>{money(wallet.data.available, 6)}</strong>
              <div>
                <ShieldCheck size={15} />
                调用前预占 · 用量确认后结算
              </div>
              <div className="wallet-art" />
            </div>
            <div className="wallet-detail">
              <span className="detail-icon">
                <CreditCard size={20} />
              </span>
              <p>账面余额</p>
              <strong>{money(wallet.data.balance, 6)}</strong>
              <small>入账减去已结算费用</small>
            </div>
            <div className="wallet-detail">
              <span className="detail-icon amber">
                <Clock3 size={20} />
              </span>
              <p>预占金额</p>
              <strong>{money(wallet.data.reserved, 6)}</strong>
              <small>在途调用与待对账占用</small>
            </div>
          </div>
        )}
      </ResourceState>
      <WalletAlert wallet={wallet.data} />
      <div className="inline-note">
        <Boxes size={17} />
        Token 用量来自模型网关；客户费用按调用时的平台价格快照计算。
        “用量已结算”表示本次平台费用已记账。
      </div>
      <div className="inline-note">
        <ShieldCheck size={17} />
        超时或取消的调用可能仍产生费用，结果未确认时保留预占。额度入账与费用核销由平台财务人员处理。
      </div>
      {admin ? (
        <Panel
          title="资金流水"
          detail="所有金额均以 USD 计价，历史账本采用追加记录"
          action={
            <Button
              variant="ghost"
              aria-label="刷新账本"
              onClick={() => {
                void ledger.reload();
                void wallet.reload();
              }}
            >
              <RefreshCw size={16} />
            </Button>
          }
        >
          <ResourceState
            loading={ledger.loading}
            error={ledger.error}
            retry={() => void ledger.reload()}
          >
            {ledger.data?.items.length ? (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>类型</th>
                      <th>说明</th>
                      <th>金额</th>
                      <th>账面余额</th>
                      <th>时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ledger.data.items.map((entry) => (
                      <tr key={entry.id}>
                        <td>
                          <div className="ledger-type">
                            <span
                              className={
                                Number(entry.amount) >= 0
                                  ? "ledger-positive"
                                  : "ledger-negative"
                              }
                            >
                              {Number(entry.amount) >= 0 ? (
                                <ArrowDownLeft size={16} />
                              ) : (
                                <ArrowUpRight size={16} />
                              )}
                            </span>
                            {ledgerLabels[entry.type] ?? entry.type}
                          </div>
                        </td>
                        <td>{entry.description || "—"}</td>
                        <td
                          className={`numeric ${Number(entry.amount) >= 0 ? "positive-text" : ""}`}
                        >
                          {Number(entry.amount) > 0 ? "+" : ""}
                          {money(entry.amount, 6)}
                        </td>
                        <td className="numeric">{money(entry.balance, 6)}</td>
                        <td className="muted nowrap">
                          {dateTime(entry.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="还没有资金流水"
                description={
                  admin
                    ? "首次使用前，请联系平台运营人员为组织分配额度。"
                    : "请联系管理员为工作空间分配使用额度。"
                }
              />
            )}
          </ResourceState>
        </Panel>
      ) : (
        <Panel>
          <Empty
            title="资金流水由管理员查看"
            description="你可以在调用统计中查看自己的用量与费用，工作空间额度请联系管理员分配。"
          />
        </Panel>
      )}

    </>
  );
}

export function QuotasPage({ tenantId }: { tenantId: string }) {
  const resource = useResource<Items<Quota>>("/quotas");
  const users = useResource<Items<Membership>>("/memberships");
  const models = useResource<Items<Model>>("/models");
  const [editing, setEditing] = useState<Quota | "new" | null>(null);
  const [scope, setScope] = useState("tenant");
  const toast = useToast();
  const scopeNames: Record<string, string> = {
    tenant: "工作空间",
    user: "用户",
    model: "模型",
  };
  const targetName = (quota: Quota) =>
    quota.scope === "tenant"
      ? "整个工作空间"
      : quota.scope === "user"
        ? (users.data?.items.find((user) => user.user_id === quota.subject_id)
            ?.email ?? quota.subject_id)
        : (models.data?.items.find((model) => model.id === quota.subject_id)
            ?.name ?? quota.subject_id);
  const show = (quota: Quota | "new") => {
    setScope(quota === "new" ? "tenant" : quota.scope);
    setEditing(quota);
  };
  return (
    <>
      <PageTitle
        eyebrow="QUOTAS & LIMITS"
        title="配额与限流"
        description="在工作空间、用户和模型层面控制调用速度与费用。"
        action={
          <Button onClick={() => show("new")}>
            <Plus size={17} />
            配置策略
          </Button>
        }
      />
      <div className="policy-explainer">
        <div>
          <Activity size={20} />
          <strong>RPM / TPM</strong>
          <p>每分钟请求数与 Token 数</p>
        </div>
        <div>
          <Layers size={20} />
          <strong>并发限制</strong>
          <p>同时进行的模型调用数</p>
        </div>
        <div>
          <CircleDollarSign size={20} />
          <strong>累计预算</strong>
          <p>累计费用上限，不按月重置</p>
        </div>
      </div>
      <div className="inline-note">
        <SlidersHorizontal size={17} />
        各层限制同时生效。留空表示不限制，填写 0
        表示禁止；下级配置不能放大上级额度。
      </div>
      <Panel title="生效策略">
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {resource.data?.items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>作用范围</th>
                    <th>目标</th>
                    <th>RPM</th>
                    <th>TPM</th>
                    <th>并发数</th>
                    <th>累计预算（USD）</th>
                    <th className="align-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {resource.data.items.map((quota) => (
                    <tr key={quota.id}>
                      <td>
                        <span className="scope-pill">
                          {scopeNames[quota.scope]}
                        </span>
                      </td>
                      <td>{targetName(quota)}</td>
                      <td>{quota.rpm == null ? "不限" : number(quota.rpm)}</td>
                      <td>{quota.tpm == null ? "不限" : number(quota.tpm)}</td>
                      <td>{quota.concurrent ?? "不限"}</td>
                      <td>
                        {quota.max_budget == null
                          ? "不限"
                          : money(quota.max_budget)}
                      </td>
                      <td className="align-right">
                        <Button variant="ghost" onClick={() => show(quota)}>
                          <Pencil size={14} />
                          编辑
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="暂无策略"
              description="为不同层级配置明确的调用限制。"
            />
          )}
        </ResourceState>
      </Panel>
      {editing && (
        <Modal
          title="配置配额策略"
          description="相同范围与目标的策略会被更新；0 禁止，留空不限。"
          close={() => setEditing(null)}
        >
          <Form
            close={() => setEditing(null)}
            submit={async (form) => {
              const optional = (key: string) =>
                formValue(form, key) === ""
                  ? null
                  : Number(formValue(form, key));
              await write(
                "/quotas",
                {
                  scope,
                  subject_id:
                    scope === "tenant"
                      ? tenantId
                      : formValue(form, "subject_id"),
                  rpm: optional("rpm"),
                  tpm: optional("tpm"),
                  concurrent: optional("concurrent"),
                  max_budget: formValue(form, "max_budget") || null,
                },
                "PUT",
              );
              setEditing(null);
              toast("配额策略已保存");
              await resource.reload();
            }}
          >
            <Field label="作用范围">
              <Select
                value={scope}
                onChange={(event) => setScope(event.target.value)}
                disabled={editing !== "new"}
              >
                <option value="tenant">整个工作空间</option>
                <option value="user">指定用户</option>
                <option value="model">指定模型</option>
              </Select>
            </Field>
            {scope !== "tenant" && (
              <Field label={scope === "user" ? "用户" : "模型"}>
                <Select
                  name="subject_id"
                  required
                  defaultValue={editing === "new" ? "" : editing.subject_id}
                >
                  <option value="" disabled>
                    选择目标
                  </option>
                  {scope === "user"
                    ? users.data?.items.map((user) => (
                        <option value={user.user_id} key={user.id}>
                          {user.name} · {user.email}
                        </option>
                      ))
                    : models.data?.items.map((model) => (
                        <option value={model.id} key={model.id}>
                          {model.name}
                        </option>
                      ))}
                </Select>
              </Field>
            )}
            <div className="form-columns">
              <Field label="每分钟请求数（RPM）">
                <input
                  name="rpm"
                  type="number"
                  min={0}
                  step={1}
                  placeholder="不限"
                  defaultValue={editing === "new" ? "" : (editing.rpm ?? "")}
                />
              </Field>
              <Field label="每分钟 Token 数（TPM）">
                <input
                  name="tpm"
                  type="number"
                  min={0}
                  step={1}
                  placeholder="不限"
                  defaultValue={editing === "new" ? "" : (editing.tpm ?? "")}
                />
              </Field>
              <Field label="同时并发调用数">
                <input
                  name="concurrent"
                  type="number"
                  min={0}
                  step={1}
                  placeholder="不限"
                  defaultValue={
                    editing === "new" ? "" : (editing.concurrent ?? "")
                  }
                />
              </Field>
              <Field label="累计预算上限（USD）">
                <input
                  name="max_budget"
                  type="number"
                  min={0}
                  step="any"
                  placeholder="不限"
                  defaultValue={
                    editing === "new" ? "" : (editing.max_budget ?? "")
                  }
                />
              </Field>
            </div>
          </Form>
        </Modal>
      )}
    </>
  );
}

export function AuditPage() {
  const resource = useResource<Items<Audit>>("/audit");
  const [search, setSearch] = useState("");
  const items =
    resource.data?.items.filter((entry) =>
      `${entry.actor_email} ${entry.action} ${entry.target}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) ?? [];
  return (
    <>
      <PageTitle
        eyebrow="AUDIT LOG"
        title="审计日志"
        description="关键管理操作的完整记录，帮助团队了解每一次变更。"
        action={
          <Button variant="secondary" onClick={() => void resource.reload()}>
            <RefreshCw size={16} />
            刷新
          </Button>
        }
      />
      <Panel>
        <div className="table-toolbar">
          <SearchBox
            value={search}
            onChange={setSearch}
            placeholder="搜索操作者、动作或对象"
          />
          <span>{items.length} 条记录</span>
        </div>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {items.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>操作人</th>
                    <th>动作</th>
                    <th>操作对象</th>
                    <th>发生时间</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((entry) => (
                    <tr key={entry.id}>
                      <td>
                        <div className="table-identity">
                          <span className="audit-icon">
                            <ShieldCheck size={16} />
                          </span>
                          {entry.actor_email}
                        </div>
                      </td>
                      <td>
                        <code className="action-code">{entry.action}</code>
                      </td>
                      <td className="muted mono">{entry.target}</td>
                      <td className="muted nowrap">
                        {dateTime(entry.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title="暂无审计记录"
              description="成员、模型、Agent 与额度的关键变更将记录在这里。"
            />
          )}
        </ResourceState>
      </Panel>
    </>
  );
}

export function RunsPage({ navigate }: { navigate: (path: string) => void }) {
  const resource = useResource<Items<Run>>("/runs");
  return (
    <>
      <PageTitle
        eyebrow="RUN HISTORY"
        title="运行记录"
        description="查看 Agent 任务状态，回到会话继续探索。"
        action={
          <Button variant="secondary" onClick={() => void resource.reload()}>
            <RefreshCw size={16} />
            刷新
          </Button>
        }
      />
      <Panel>
        <ResourceState
          loading={resource.loading}
          error={resource.error}
          retry={() => void resource.reload()}
        >
          {resource.data?.items.length ? (
            <RunTable items={resource.data.items} navigate={navigate} />
          ) : (
            <Empty
              title="暂无运行记录"
              description="发布 Agent 并发送消息后，运行记录会出现在这里。"
              action={
                <Button
                  variant="secondary"
                  onClick={() => navigate("playground")}
                >
                  打开 Playground
                  <ArrowRight size={15} />
                </Button>
              }
            />
          )}
        </ResourceState>
      </Panel>
    </>
  );
}
function RunTable({
  items,
  navigate,
}: {
  items: Run[];
  navigate: (path: string) => void;
}) {
  const toast = useToast();
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Agent / 运行 ID</th>
            <th>状态</th>
            <th>费用</th>
            <th>创建时间</th>
            <th className="align-right">操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((run) => (
            <tr key={run.id}>
              <td>
                <div className="table-identity">
                  <div className="mini-agent">
                    <Bot size={18} />
                  </div>
                  <div>
                    <strong>{run.agent_name || "Agent 运行"}</strong>
                    <button
                      className="copy-id"
                      title="复制运行 ID"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(run.id);
                          toast("运行 ID 已复制");
                        } catch {
                          toast("浏览器暂不支持复制", "error");
                        }
                      }}
                    >
                      {run.id.slice(0, 12)}
                      <Copy size={11} />
                    </button>
                  </div>
                </div>
                {run.error && (
                  <small className="error-text" title={run.error}>
                    {run.error}
                  </small>
                )}
              </td>
              <td>
                <Badge status={run.status} />
              </td>
              <td className="numeric">{money(run.cost, 6)}</td>
              <td className="muted nowrap">{dateTime(run.created_at)}</td>
              <td className="align-right">
                <Button
                  variant="ghost"
                  onClick={() =>
                    navigate(`playground?session=${run.session_id}`)
                  }
                >
                  查看会话
                  <ExternalLink size={14} />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WalletAlert({ wallet }: { wallet: WalletData | null }) {
  return wallet?.blocked ? (
    <div className="form-error" role="alert">
      <ShieldCheck size={17} />
      <span>
        钱包已暂停新的模型调用。
        {wallet.block_reason || "请联系管理员核对账务后处理。"}
      </span>
    </div>
  ) : null;
}
