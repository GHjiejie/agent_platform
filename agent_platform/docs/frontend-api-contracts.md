# 前端 API 契约与页面映射

> 2026-10-15 更新：Playground 已迁移到 AG-UI 1.0 POST/SSE，详见 [AG-UI 当前契约](ag-ui-protocol.md)。下文基线中的旧 Run 202 / 自定义事件 API 保留为兼容接口，不再由 Playground 使用。具体请求 schema 以 OpenAPI、官方 AG-UI SDK 和生成类型为准。

> 基线：`master` @ `db1ab95367b53d84e68cb39f59c97715ccce37a0`，2026-10-02 源码核查。本文为 [前端功能规格](frontend-functional-spec.md) 的接口参考。所有例子都是结构示意，ID须从实际响应取得，Cookie/CSRF/邀请令牌由真实会话或专用响应提供；不包含真实凭据。未执行 live API、测试或服务启动。

## 1. 通用规则与响应投影

- `T=/api/v2/tenants/{tenant_id}`；`P=/api/v2/platform`；`PT=P/tenants/{tenant_id}`；`S=/api/v2/support-grants`。完整注册 URL 见末尾附录，不能将路径缩写直接发给 fetch。
- 新租户 UI 使用显式 v2 路径。v1 业务 handler 仅在身份恰有一个 active membership 时选租户，否则409 `tenant_selection_required`。登录仍在 `/api/v1/auth/*`。
- 写请求同源 Cookie + `X-CSRF-Token`；幂等接口按真实要求传 `Idempotency-Key`。scope变化或会话退出中止旧请求和流；不得把前一租户的响应显示到新组织。
- 平台与租户能力分开；后端再次检查当前身份、成员版本和租户状态。各模块权限以逐接口说明为准。
- 金额为十进制字符串，日期为ISO字符串，明确Epoch字段使用秒；无 `response_model` 的对象以下按源码返回字典/表行核对。响应可能有额外字段，UI不应暴露内部执行或凭据证据字段。
- 业务错误 `{error:{code,message}}`；422校验附 `details:[{field,message}]`；429带 `Retry-After:60`；500安全 `internal_error`。当前 `api.ts` 只保留status/code/message，后续可补details/request ID和Retry-After解析。
- 未明确提供的query/body/分页/筛选，不得假设后端支持。未知字符串状态应保留可读fallback。

### 运行与账务 DTO 的关键差异

所有时间字段除注明 epoch seconds 外是 ISO 字符串；boolean 是 JSON boolean；金额/报价是 decimal string。下列为 UI 需要的投影，后端多数返回 dict/表行而无 response_model，可能带额外字段。

- Agent（列表/已存草稿）: id,tenant_id,created_at,name,description,system_prompt,model_id:string|null,builtin_key:string|null,temperature:number,max_steps:number,max_tokens:number,tools:string[],published_version:number；GET 另加 category,starter_prompts。draft 的 published_version 为0；成员只获得发布版 spec。POST创建直接返回构建字典，builtin_key未填时字段缺席，也不带category/starter_prompts；应随后刷新列表。main.py:379–406；platform.py:559–588。
- Model: id,tenant_id,name,alias,active,input_price,output_price,context_window,max_output_tokens,price_version:number,created_at；GET /models 增加 is_default 并返回 {items,default_model}。main.py:348–358；infrastructure/tables.py:70–83。
- Session: id,tenant_id,user_id,agent_id,title,created_at。SessionDetail 增 messages:[{id,tenant_id,session_id,run_id,role,content,created_at}],runs:PublicRun[]。platform.py:618–654。
- PublicRun: id,tenant_id,user_id,session_id,agent_name,created_at,status,error,finished_at:string|null,model_id,model_alias；create 不带 cost/message，GET 单条/列表额外 cost:string,message:string，session detail 的 runs 不带 cost。platform.py:856–883。
- RunEvent: run_id,tenant_id,sequence:number,type:string,data:object,created_at。run_records.py:19–39；前端 types.ts:64–68 只声明 sequence/type/data。
- UsageCall: id,run_id,user_id,model_id,model,agent_name,user_email,status,input_tokens:number,output_tokens:number,cost:string,created_at,...；usage_source='litellm_gateway'；raw_usage 始终剔除；provider_cost 仅 platform.costs.read 返回（其余角色字段缺席，非 null）。platform.py:885–939。
- Dashboard: requests,tokens:number,cost:string,success_rate:number(0–100),active_runs:number,daily:[{date,requests,tokens,cost}],models:[{model,requests,tokens,cost}],gateway_configured:boolean。只有 billing.read 才附钱包字段，故不能无条件 extends Wallet。platform.py:1061–1078。
- Wallet: balance,reserved,available:string,currency:'USD',blocked:boolean,block_reason:string，现有 types.ts:69–76。
- Ledger: id,type,amount,balance,description,created_at,...，现有 types.ts:99–106。
- Quota: id,tenant_id,scope:tenant|user|model,subject_id,rpm/tpm/concurrent:number|null,max_budget:string|null；实际表和PUT响应都没有version（types.ts声明version?不是后端承诺）。billing/service.py:76–88、525–569。
- Audit: id,actor_email,action,target,created_at,...（金融审计可含额外字段）。main.py:650–674；types.ts:117–123。

## 2. 工作空间、Agent、会话、运行与模型选择

### 登录、账号与工作空间（App.tsx）

- POST /api/v1/auth/login，body {email:string,password:string} → {user:IdentityUser,csrf_token} + HttpOnly session cookie；email 3–254，password 1–512。main.py:277–295；schemas.py:13–16。登录提交 busy/disabled；错误 form-error；成功 refreshIdentity（App.tsx:293–311）。
- GET /api/v2/me → {user,csrf_token}；GET T → {tenant,user}；POST /api/v1/auth/logout 无 body→{ok:true}；POST /api/v1/auth/password {current_password,new_password(12–512)}→{ok:true} 后清 session 重新登录。main.py:301–317、702–704；App.tsx:132–141、272–280。
- 不要新增前端角色硬编码鉴权：导航以 capability 为主。overview/playground=runs.execute；agents=agents.read；runs=runs.read_own；models=models.read；usage=billing.read_own 或 usage.read_all；billing=billing.read；users=members.manage；quotas=quotas.manage；audit=audit.read；support-access=ownership.manage；平台入口由平台能力门控。App.tsx:16–31、145–150。

### 工作空间概览（pages.tsx:63–277）

- GET T/dashboard→Dashboard；GET T/runs→{items:Run[],next_cursor}；趋势柱、模型分布、累计调用/Token/客户费用/余额、active_runs、近期运行和开始对话入口。
- 主资源 ResourceState；chart/model/recent run 无记录各有 Empty。gateway_configured=false 展示授权/配置提醒。wallet.blocked 展示暂停调用提醒。余额字段可能缺席，当前显示 —；新 UI 应按 billing.read 隐藏或说明。
- daily 是最多30个有调用的日期（不是补零的连续30天），models最多100个；success_rate 是已确认调用占比，非 Agent 成功率。platform.py:998–1048。

### 我的 Agent（pages.tsx:309–657）

- GET T/agents→{items:Agent[]}，GET T/models→{items:Model[],default_model}。客户端搜索 name/description/category，source=all|builtin|custom；无服务端过滤/分页。发布版为0不能“开始对话”。
- agents.manage 控制创建/编辑/发布按钮；GET 成员仅发布版，admin能看到草稿。编辑只影响后续发布，进行中的 run 已快照。
- POST T/agents(201) / PATCH T/agents/{agent_id}，body {name(1–120),description(<=2000),system_prompt(1–20000),model_id:string|null,temperature:0–2,max_steps:1–30,max_tokens:1–128000,tools:('calculator'|'current_time')[]}→Agent。PATCH 可省字段，显式 model_id:null 清偏好。schemas.py:88–113；main.py:408–418；pages.tsx:511–531。
- POST T/agents/{agent_id}/publish→{version:number}；版本单调增加，保存 published spec。无需 body。main.py:420–422；platform.py:590–616。
- 无 Agent 删除、详情 GET、历史版本 GET、复制 API；若设计出现这些操作应标为未实现或本地复制表单再创建。已发布/草稿空态与模型未就绪说明已实现。发布失败 toast，编辑失败 Form 留窗，publish busy id。pages.tsx:331–343、445–476、650–653。

### Playground（Playground.tsx）

- GET T/agents/models/sessions；GET T/sessions/{session_id}→SessionDetail；POST T/sessions(201) {agent_id(1–64),title?:string<=160}→Session；POST T/sessions/{id}/runs(202) {message:string1–32000,model_id?:string|null} + Idempotency-Key→PublicRun(status=queued)；POST T/runs/{id}/cancel→GET式 Run；GET T/runs/{id}→Run。schemas.py:116–123；main.py:424–482。
- 仅用户本人的 sessions/runs（含管理员）。一个会话最多1个 active run。重试相同会话/消息/模型保留同一 key。发布 Agent 是创建会话先决条件，模型/报价/授权在入队时检查。platform.py:618–631、668–729。
- 最近会话侧栏、移动端 select；hash 支持 agent/session/tenant；先加载会话，再找到 nonterminal run 重新连 SSE。用户可切换/新建会话时脱离当前流，服务端仍执行；不是自动取消。Playground.tsx:92–138、328–344。
- 当前 Agent 在已有session、busy或active run时 disabled；模型 busy/active run时 disabled，只列 active 模型；缺模型显示授权链接，但当前输入仍可发送，由后端给 no_available_model。模型优先级：本次手选 > Agent model_id > 组织 default > ordered candidates > 其他已授权模型，运行失败后不自动重放/切模型。platform.py:763–805；PlatformResources.tsx:44–54。
- 输入 maxLength=32000；Enter发送/Shift+Enter换行，IME组合输入不会提交。textarea busy/loading/无Agent disabled；发送空白/无Agent/loading disabled；active run显示停止按钮，canceling busy。加载会话 spinner；无消息欢迎页+starter prompts；无已发布Agent显示说明；错误条可重新加载；SSE retrying 提供手动重连。Playground.tsx:427–515、522–568、673–745。
- 对话按纯 React 文本渲染，不是 Markdown renderer；不支持附件/图片/音频/富消息/API工具授权弹窗。Playground.tsx:570–599。
- UI 最多保留80条非message.delta事件（previous.slice(-79)+new）；liveText合并所有模型步骤delta；终态后重新GET SessionDetail，用持久化最终assistant消息替换流内容。失败/取消不保存最终assistant消息，user消息会继续留在历史。Playground.tsx:163–201；worker/main.py:675–707。

### 运行记录（pages.tsx:1795–1912）

- GET T/runs→{items:Run[],next_cursor}；每行 Agent/ID、status、cost、created_at、error；复制ID；按钮回到 playground?session=...；刷新和 ResourceState/Empty。
- 当前无过滤/分页控件、无列表自动轮询、无单独 run detail 页、无本页取消按钮。前端只拉首批默认100。

### 模型目录与组织偏好（pages.tsx:728–845，PlatformResources.tsx:20–79）

- GET T/models→{items,default_model}；客户端 name/alias 搜索；展示默认、启用、context/output limits、USD/百万Token价格。
- App 实际以 admin=false 挂载，租户目录不提供“添加模型/维护报价”旧弹窗；可 models.policy 时展示 ModelPolicyPanel。
- GET T/model-policy→{tenant_id,default_model_id:string|null,ordered_model_ids:string[],version:number}；PUT T/model-policy {default_model_id,ordered_model_ids,expected_version}→更新后的同类对象。schemas.py:232–235；identity.py:1100–1120、1181。
- 偏好编辑可选择默认、添加/移除候选、上下移；边界按钮 disabled；资源未加载前配置 disabled；空候选允许自动选择其他已授权模型。expected_version 冲突应提示刷新并重做。
- PATCH T/models/{id} 仅 {active:boolean} 可用→{model_id,enabled}；当前 ModelsPage(admin=false)没有启停控件，不能凭旧写表单发送报价。main.py:366–377；gateway_control.py:751。

## 3. 分页、SSE 与执行错误

### 分页

- GET T/sessions、T/runs、T/usage/calls 支持 limit=1..200（default100）、cursor:string<=300 → {items,next_cursor:string|null}。按 created_at DESC,id DESC keyset，cursor为base64 JSON [created_at,id]；无效400 invalid_cursor。main.py:424–439、460–474、551–563；platform.py:383–448。
- 当前Web没有消费next_cursor，三个列表只首批100；Agent/Models/Audit至多200；支持运行200、正文500(has_more)；后台导出列表200；网关operations50。前端搜索/筛选只对当前返回批次，不能把“搜索无结果”解释为全库无结果。
- Ledger/reservations/quotas/memberships/invitations等没有通用cursor/分页API，不应添加假分页总数。billing/service.py:1192–1216；identity.py:701–717、779–789。

### SSE

- GET T/runs/{run_id}/events?after=N，N>=0；浏览器同源Cookie，EventSource withCredentials=true。可 Last-Event-ID header；服务器取max(after,header)，非法header400 invalid_cursor。main.py:484–499。
- 每条：id:sequence；event:type；data:完整RunEvent JSON。按sequence升序，单批200，轮询DB0.25秒；约10秒 :heartbeat 注释；终态最终尾读后关闭；X-Accel-Buffering:no、Cache-Control:no-store。main.py:501–549。
- 服务端重查登录、成员、runs.read_own，权限失效关闭连接；连接配额租约 acquire/update/release。浏览器自动重连，after避免重放，sequence去重。Playground.tsx:159–234；platform.py:450–498。
- 实际生产事件：
  - run.queued {status:'queued'}（platform.py:852）
  - run.started {status:'running'}（worker/main.py:728–730）
  - run.cancelling {status:'cancelling'} 或 run.cancelled {status:'cancelled'}（platform.py:945–957）
  - step.started {step}；step.finished {step,input_tokens,output_tokens}（engine.py:106、144–150）
  - model.started {call_id,model:alias}（worker/main.py:561–566）
  - message.delta {text,call_id}（worker/main.py:579–585、613–619）
  - tool.started {id,name}；tool.finished {id,name,ok:true,...output} 或 {id,name,ok:false,error}（engine.py:165–178）
  - usage.updated {call_id,input_tokens,output_tokens,cost,status:'confirmed'}（worker/main.py:620–630）
  - run.completed {status:'succeeded',content:final,error:''}；run.failed/run.cancelled/run.expired {status,content:'',error}（worker/main.py:697–707）。
- UI注册了额外兼容名称run.canceled/run.interrupted/run.succeeded/step.completed/model.completed，当前生产不发这些；反而漏注册run.cancelling。source.onmessage只接默认event，不会兜底带命名run.cancelling事件，取消中Badge不会及时更新，最终会靠终态/5秒poll更新。
- SSE不是直接转发LangGraph内部流；平台拥有稳定持久事件协议，graph.astream_events v3被内部消费。engine.py:203–210。
- 没有 WebSocket/SSE POST/模型工具参数流/明确重试次数API，不能为现有backend臆造。

### 常见失败状态（业务异常）

- 认证：authentication_required/session_expired 401；csrf_failed/invalid_origin 403。
- 资源：not_found 404（隐藏跨租户/他人资源）；tenant_selection_required 409（旧v1多成员需v2）。
- Agent：agent_limit429，output_limit_exceeded400，model_disabled400，agent_not_published400。
- 入队：idempotency_required400，idempotency_conflict409，session_busy409，queue_full429，model_capability_required400，no_available_model400；授权同步失败还会抛gateway service错误。
- Worker：context_limit400、run_budget_exceeded402、model_grant_revoked403、deployment_changed403，取消/超时/网关流错误最终通过Run.error和run.*事件给UI，而非原POST的HTTP错误。
- 模型配置无效、step_limit_exceeded、tool_limit_exceeded、invalid_tool_call在执行期令run failed。工具参数失败tool.finished ok=false会返回模型继续，而非立即失败。engine.py:77–85、101–125、171–186。
- 网关usage缺失或不一致→usage_unavailable；上游可能执行但未知计费→unresolved保留预占；发送前失败→release。gateway.py:188–196；worker/main.py:633–661。
- 402余额/预算、429限流、503依赖不可用应保留code用于准确UI处理；现有通用客户端只显示message，部分billing错误是英文。

## 4. 身份、成员、平台管理、支持、部署和网关

### 通用调用约定

- `GET /api/v2/me` 与 `GET /api/v1/auth/me` 返回相同 `{user: GlobalIdentity, csrf_token: string}`。前者是新 UI 建议入口。路径从 [apps/api/main.py:297–299,702–704](../apps/api/main.py#L297) 核实。
- Cookie 名 `agent_platform_session`；`HttpOnly=true`、`SameSite=strict`、`Path=/`，`secure` 由服务端配置决定，session_hours 默认24。浏览器不需要、也拿不到登录会话 token 字段（login将该字段放Cookie后移除）。`main.py:41,277–295`; `config.py:38–40`。
- 非 GET/HEAD/OPTIONS 的认证请求必须带 `X-CSRF-Token: <会话响应的值>`；Cookie同源发送。登录不要求 CSRF。来源 Origin 的 netloc 若不等于 Host 则403 `invalid_origin`。`main.py:65–93,174–180`。
- `Idempotency-Key` 仅在声明需要的接口使用；有效字符串长度1–160。缺少/过长→400 `idempotency_required`。应在打开确认表单时生成一次并在同一提交重试中复用，不能每次重试生成新值。`main.py:240–245`。
- 请求模型默认 `extra="forbid"`，多数字符串自动 strip；Login/PasswordChange/TenantCreate/PlatformUserCreate 禁止extra但不自动strip。Email在业务层小写。`schemas.py:9–22,151–172`。
- 业务错误格式 `{error:{code:string,message:string}}`；校验错误422 `{error:{code:"validation_error",message:string,details:{field:string,message:string}[]}}`；429带 `Retry-After: 60`；未知异常500 `internal_error`。`main.py:134–172`。
- 所有认证接口共通：401 `authentication_required|session_expired|identity_inactive`；写请求403 `csrf_failed`；角色不够403 `capability_required|platform_role_required`。UI依据当前 `capabilities` 隐藏/禁用，但服务端仍复核。
- 无租户 membership 返回404 `not_found`，刻意不区分租户不存在或无权限。租户非active时普通读请求可进入 suspended/closing/provisioning 上下文，写请求403 `tenant_suspended`；具体服务还可能更严格。`identity.py:298–339`; `main.py:191–194`。
- 所列接口除明确query外没有分页/query参数。不要让UI假定后端具有搜索、筛选、排序、分页等能力。
- 当前前端已有 `api.ts` 同源cookie/CSRF、组织切换中止旧请求和scope revision检查（[apps/web/src/api.ts:1–81](../apps/web/src/api.ts#L1)）；相对 `/memberships` 会变成 `/api/v2/tenants/{tenantId}/memberships`，完整 `/api/...` 保持原样；`/auth/...` 仍映射v1认证。

### 真实响应形状与类型

下面 `Id=string`（目前通常32位hex，但API字段一般接受1–64），`ISO=string`（UTC ISO时间），`Epoch=number`（秒，不是毫秒）；`?`是响应可能缺少，`|null`是字段显式nullable。

```ts
type TenantRole = "owner"|"tenant_admin"|"member"|"finance_viewer";
type PlatformRole = "platform_admin"|"platform_finance"|"platform_support";
type Membership = {
  id: string; tenant_id: string; user_id: string; role: TenantRole;
  status: "active"|"revoked"; authz_version: number; joined_at: string;
  revoked_at: string|null;
};
type IdentityMembership = Membership & {tenant_name: string; tenant_status: string};
type GlobalIdentity = {
  id: string; email: string; name: string; active: true; global_status: "active";
  created_at: string; auth_version: number; platform_roles: PlatformRole[];
  capabilities: string[]; memberships: IdentityMembership[];
};
type TenantContext = Omit<GlobalIdentity,"memberships"> & {
  tenant_id: string; tenant_name: string; role: "admin"|"member";
  tenant_role: TenantRole; membership_id: string; identity_version: number;
  membership_version: number; tenant_status: string; tenant_policy_version: number;
  _allow_suspended: boolean;
};
type TenantSettings = {
  tenant_id: string; status: "provisioning"|"active"|"suspended"|"closing"|"deleted";
  version: number; default_model_id: string|null; ordered_model_ids: string[];
  owner_unavailable: boolean; suspension_reason: string; created_at: string; updated_at: string;
};
type PlatformTenant = TenantSettings & {id:string; name:string; created_at_1?:string};
type MemberListRow = Membership & {email:string; name:string; global_status:string};
type Invitation = {
  id:string; tenant_id:string; email:string; role:Exclude<TenantRole,"owner">;
  expires_at:number; created_at:string; invited_by:string;
  accepted_at:string|null; accepted_by:string|null; revoked_at:string|null;
};
type Entitlements = {
  tenant_id:string; version:number; max_members:number; max_agents:number;
  max_sse_connections:number; max_export_jobs:number; max_queued_runs:number;
  max_concurrent_runs:number; rpm:number; tpm:number; max_budget:string|null;
  updated_at:string; updated_by:string|null;
};
type ModelPolicy = {tenant_id:string; default_model_id:string|null; ordered_model_ids:string[]; version:number};
type Model = {
  id:string; tenant_id:string; created_at:string; name:string; alias:string; active:boolean;
  input_price:string; output_price:string; context_window:number; max_output_tokens:number; price_version:number;
};
type Deployment = {
  id:string; created_at:string; name:string; owner_scope:"platform"|"tenant";
  owner_tenant_id:string|null; gateway_id:string; internal_route:string; base_url:string;
  protocol:string; capabilities:{text:boolean;tools:boolean}; status:string; config_version:number;
};
type SupportGrant = {
  id:string; tenant_id:string; staff_user_id:string; approved_by:string;
  approver_membership_id:string; approver_membership_version:number; reason:string;
  allow_content:boolean; created_at:string; expires_at:number;
  revoked_at:string|null; revoked_by:string|null;
};
type PlatformAudit = {
  id:string; actor_user_id:string|null; tenant_id:string|null; action:string;
  target:string; reason:string; details:Record<string,unknown>; created_at:string;
};
```

出处：`identity.py:281–339,507–517,755–777`; [infrastructure/tenancy_tables.py:20–40,59–140](../infrastructure/tenancy_tables.py#L20); [infrastructure/tables.py:28–84](../infrastructure/tables.py#L28); `gateway_tables.py:29–48`; `support_tables.py:18–45`。**GlobalIdentity 没有 tenant_id / tenant_role / legacy role**，必须通过选择membership后读取TenantContext；现 `types.ts:11` 的旧 User.tenant_id 为必填与GlobalIdentity不能混用。平台目录 `select(t.tenants, tenant_settings)` 有同名created_at，SQLAlchemy映射会带重复列别名，UI仅依赖id/name/status/version等唯一字段；第一次tenant创建与幂等回读可能存在额外别名差异。

### 角色与页面/按钮 gating

|主体|代码能力|对应UI|
|---|---|---|
|member|tenant.read, agents.read, models.read, runs.execute, runs.read_own, billing.read_own, exports.create|组织切换、个人运行/会话、模型只读；不能管理成员|
|finance_viewer|tenant.read, billing.read, usage.read_all, exports.create|用量/账务元数据；无models.read、运行或正文能力|
|tenant_admin|member全部 + members.read/manage, agents.manage, models.policy, billing.read, usage.read_all, quotas.manage, audit.read|组织管理；不能提升/修改owner或tenant_admin人员|
|owner|tenant_admin全部 + ownership.manage, tenant.close|高权限成员、所有权转移、批准/撤销支持访问；tenant.close能力当前无owner关闭HTTP入口|
|platform_admin|platform.tenants/entitlements/models/pricing/gateway/roles.manage, platform.audit.read|平台组织、套餐、部署、售价、网关、角色管理|
|platform_finance|platform.billing.manage, platform.costs.read, platform.audit.read|平台入账、对账、解冻及供应商成本|
|platform_support|platform.support.request|支持工作台；仍须有效support grant|

依据 [modules/authorization.py:5–54](../modules/authorization.py#L5)。能力是多个平台角色的并集；**platform_admin不自动拥有billing.manage或costs.read**。租户context把平台capabilities与租户capabilities合并，但platform操作再单独查询真实当前platform role。首批普通会话正文只属于本人，tenant admin/owner不默认读取其他人会话。`authorization.py:71–87`; `platform.py:360–422`。

### A. 登录与全局身份 UI

|method/path|body/query/headers|success shape|授权与错误|
|---|---|---|---|
|POST `/api/v1/auth/login`|`{email:string(3–254),password:string(1–512)}`；无CSRF要求|200 `{user:GlobalIdentity,csrf_token:string}` + set-cookie|401 invalid_credentials；5分钟窗口email10次/IP50次后429 login_rate_limited|
|GET `/api/v2/me`|无|200 `{user:GlobalIdentity,csrf_token:string}`|已登录；可无membership仍返回|
|GET `/api/v1/auth/me`|无|同上|旧兼容入口|
|POST `/api/v1/auth/logout`|无body，CSRF|200 `{ok:true}` +删除当前cookie/session|只注销本会话|
|POST `/api/v1/auth/password`|`{current_password:string(1–512),new_password:string(12–512)}`；CSRF|200 `{ok:true}`|400 invalid_password；变更auth_version并删除该用户所有sessions；成功后导航重新登录|

出处 `main.py:277–317,702–704`; `schemas.py:13–22`; `platform.py:251–352,499–539`。

不存在：自助注册、验证码登录、邮箱验证操作、忘记密码/管理员重置密码、SSO接口。`email_verified_at`虽然存在于users表，以上未找到对应路由。平台创建用户是人工provisioning，不能当公开注册。

### B. 组织选择、成员与邀请 UI

所有 `/api/v2/tenants/{tenant_id}` 路径固定选择目标租户，不能在body发送tenant_id或role假冒身份；无membership时403/404，先从GlobalIdentity.memberships取tenant_id。

|method/path|请求|响应|角色/限制/错误|
|---|---|---|---|
|GET `/api/v2/tenants/{tenant_id}`|无|`{tenant:{id,name,created_at,status,version},user:TenantContext}`|任一active membership；允许suspended/closing/provisioning只读上下文；404 not_found|
|GET `/api/v2/tenants/{tenant_id}/memberships`|无|`{items:MemberListRow[]}`|members.read（owner/admin）；包含revoked历史行；按joined_at,id，无分页|
|PATCH `/api/v2/tenants/{tenant_id}/memberships/{membership_id}`|`{role?:"tenant_admin"\|"member"\|"finance_viewer",status?:"active"\|"revoked",expected_version?:number>=1}`；至少role/status之一非null|更新后的Membership|members.manage且active租户；expected_version对目标authz_version；owner才能更改现owner/admin或目标admin；空body422 invalid_membership；404 not_found；409 configuration_conflict/last_owner/ownership_transfer_required；403 owner_required；重激活先检查目标账号有效与seat上限|
|GET `/api/v2/tenants/{tenant_id}/invitations`|无|`{items:Invitation[]}`|members.read；倒序所有历史；**没有token、status、accept_url**|
|POST `/api/v2/tenants/{tenant_id}/invitations`|`{email:string(regex,3–254),role?:"tenant_admin"\|"member"\|"finance_viewer"=member,expires_hours?:int(1–168)=72}`|201 `Invitation & {token:string}`|members.manage；只有owner能邀请admin；同邮箱尚未接受/撤销邀请先撤销；没有发送邮件；不预占seat|
|POST `/api/v2/invitations/accept`|`{token:string(20–512)}`|200 Membership|全局登录身份，无需先有membership；邮箱须与invite匹配403 invitation_identity_mismatch；404 invalid_invitation；410 invitation_expired；409 invitation_used/entitlement_exceeded；403 tenant_suspended；接受时复核邀请人仍有对应授权；已active成员接受不会更改原role|
|POST `/api/v2/tenants/{tenant_id}/ownership-transfer`|`{membership_id:string(1–64),expected_version?:int>=1}`|新owner的Membership；若选自身则返回当前Membership|ownership.manage（owner）；expected_version对**当前owner** authz_version；目标必须是本租户active membership且全局账号有效；前owner降tenant_admin；409 configuration_conflict，404 not_found|
|GET `/api/v2/tenants/{tenant_id}/entitlements`|无|Entitlements|tenant.read；仅只读套餐上限|

出处：[apps/api/tenancy.py:65–116](../apps/api/tenancy.py#L65); `schemas.py:190–225`; `identity.py:701–717,720–896,899–1038,1040–1059`。

UI操作规则：

- tenant selector展示membership.tenant_name/role/tenant_status，选后加载上述tenant detail；租户切换时清除本租户缓存、表单选中IDs、SSE并中止旧请求（已有api.ts实现）。owner/成员变更后同时刷新me和tenant context。
- suspended租户读可用、管理写禁用；closing/deleted不允许恢复。支持撤销的特殊读/删除路径见D。
- 邀请成功token只有这一次响应可展示。页面可生成`#invite?token=...`链接（当前`Tenancy.tsx:145`如此实现），但不要期待服务端accept_url，也不要在审计/日志展示token。邀请状态从accepted_at/revoked_at/expires_at计算。
- 不存在邀请单独撤销/删除或重发邮件接口；再次创建同邮箱邀请是现有撤销旧邀请并发新token的唯一操作。成员的global name/email/password/active不能由组织管理员更改。
- MembershipPatch的expected_version可选（后端并不强制），UI应始终提交当前authz_version；409后刷新再编辑。
- owner/admin管理成员页面不能显示“把他设为owner”的普通role选项，应单独所有权交接。admin对现admin/owner任何权限变更均禁用。最后owner不能降级或撤销。
- membership角色/状态有变更会取消该成员queued run、将running/cancelling置cancelling，包含**角色提升**也触发该行为（`identity.py:961–977,644–679`）。需在确认文案明确现有运行会收到取消请求。

合法普通请求示例（示例ID仅展示字段类型，不代表实例实际记录；不含凭据）：
```json
{"email":"colleague@example.test","role":"finance_viewer","expires_hours":24}
```
```json
{"role":"member","status":"revoked","expected_version":2}
```
```json
{"membership_id":"member_demo_02","expected_version":3}
```

旧接口：`GET /api/v2/tenants/{id}/users`仍是memberships列表的旧别名，要求admin；POST `/users`→410 `use_invitations`；PATCH `/users/{user_id}`→410 `use_memberships`。v1同样如此。`main.py:330–346,676–690`。

### C. 平台组织、身份与角色 UI

平台路由使用全局principal，不要求操作者加入目标租户。

|method/path|请求|响应|能力/错误|
|---|---|---|---|
|GET `/api/v2/platform/tenants`|无query|`{items:PlatformTenant[]}`，created_at倒序/id|任一platform.tenants/billing/entitlements/models/gateway.manage；support角色单独无目录权限|
|POST `/api/v2/platform/tenants`|`{name:string(1–120),owner_email:string(regex,3–254),owner_name?:string(1–120)\|null,owner_password?:string(12–512)\|null}`；**Idempotency-Key**+CSRF|201 PlatformTenant；首次shape `{id,name,...TenantSettings}`|platform.tenants.manage；owner账号已存在无需password，账号不存在必须给password→400 owner_password_required；inactive owner→409 owner_inactive；同key不同tenant请求409 idempotency_conflict；初始余额0、默认套餐、内置Agent同租户事务创建|
|POST `/api/v2/platform/tenants/{tenant_id}/suspend`|`{reason:string(5–500),expected_version?:int>=1}`|TenantSettings更新结果|platform.tenants.manage；expected_version对tenant_settings.version；409 configuration_conflict/invalid_transition；停用同时取消/要求停止runs|
|POST `/api/v2/platform/tenants/{tenant_id}/resume`|同上|TenantSettings更新结果|同cap；关闭/删除无法直接恢复409 invalid_transition；没有有效owner409 owner_unavailable|
|GET `/api/v2/platform/tenants/{tenant_id}/entitlements`|无|Entitlements|platform.entitlements.manage；404 not_found|
|PUT `/api/v2/platform/tenants/{tenant_id}/entitlements`|见下方字段|Entitlements更新结果|platform.entitlements.manage；409 configuration_conflict；422 conflicting_limits/invalid_entitlement|
|POST `/api/v2/platform/users`|`{email:string(regex,3–254),name:string(1–120),password:string(12–512)}`|201 `{id:string,email:string,name:string,active:true}`|platform.tenants.manage；仅创建global账号，**没有membership或platform role**；409 identity_exists/platform_not_initialized；name.strip空422 invalid_name|
|GET `/api/v2/platform/users`|必须query `email=...`，string3–254|`{items:{id,email,name,active,global_status,platform_roles:PlatformRole[]}[]}`，0或1行|platform.roles.manage；这是**精确邮箱查询**而非全量目录/模糊搜索|
|POST `/api/v2/platform/users/{user_id}/roles`|`{role:PlatformRole,active:boolean,reason:string(5–500)}`|`{user_id:string,role:PlatformRole,active:boolean}`|platform.roles.manage；每个角色独立授予/撤销；404 not_found；inactive账号授予409 identity_inactive；撤最后有效admin409 last_platform_admin；撤support角色会撤该staff全部未撤support grants|
|GET `/api/v2/platform/audit`|无query|`{items:PlatformAudit[]}`，最新200条|platform.audit.read；没有tenant/action/date分页筛选契约|

出处 `tenancy.py:126–266,535–537`; `support.py(API):40–48`; `schemas.py:151–187,213–225`; `identity.py:369–584,586–679,1040–1098,1183–1196`; `support.py(module):321–444`。

Entitlements PUT所有字段可选；除max_budget明确null外，null被排除、代表不改。限制：max_members1–100000；max_agents/max_queued_runs/max_sse_connections0–100000；max_export_jobs0–1000；max_concurrent_runs0–10000；rpm0–1000000；tpm0–1000000000；max_budget为非负十进制金额string、最多12位小数、不超过1000000000，或null不限；expected_version>=1。接口兼容max_running_runs/concurrent别名，同body提供不同并发值422 conflicting_limits；统一存储/响应字段是**max_concurrent_runs**。0表示禁止；预算是累计预算，不是账期/月度额度。`schemas.py:213–225`; `tenancy.py:249–266`; `identity.py:394–424`。

合法请求示例：
```json
{"max_members":30,"max_agents":50,"max_queued_runs":20,"max_concurrent_runs":4,"rpm":60,"tpm":120000,"max_budget":"100.00","expected_version":2}
```
```json
{"reason":"客户要求暂停组织使用","expected_version":3}
```
```json
{"role":"platform_support","active":true,"reason":"授权本周客户排障支持"}
```

创建组织schema允许owner_email已注册，不存在公开注册流程。global账号provision先在独立事务完成，再调用create_tenant事务，所以tenant创建后续失败可能留下无membership账号；UI重试应使用同email/同幂等key，不能宣称整个“新账号+新组织”单一事务回滚。`tenancy.py:183–224`。

平台账务入口存在：POST `/platform/tenants/{id}/credits`（Idempotency-Key、Credit amount/description）、GET `/billing/wallet`、GET `/billing/reservations`、POST `/billing/reservations/{call_id}/resolve`（Idempotency-Key）、POST `/billing/unblock`。这里所有入口require platform.billing.manage，不是platform_admin；账务响应由BillingService真实返回，细节由账务规格负责。`tenancy.py:268–324`。

### D. 支持访问 UI

所有支持操作以实际staff身份审计；grant不是membership，不能冒用tenant普通API。Owner授权面板与staff工作台必须分开。

|method/path|请求|响应|scope/gating/errors|
|---|---|---|---|
|GET `/api/v2/tenants/{tenant_id}/support-grants`|无|`{items:(SupportGrant & {staff_email:string})[]}`；最新最多200|owner；调用owner_context allow_suspended=true；suspended/closing/provisioning仍可查看（deleted无membership上下文）；非owner403 owner_required|
|POST `/api/v2/tenants/{tenant_id}/support-grants`|`{staff_email:string(regex,3–254),reason:string(5–500),minutes?:int(1–60)=15,allow_content?:boolean=false}`|201 `SupportGrant & {staff_email:string}`|当前owner且tenant active；staff必须global active +有效platform_support；404 support_staff_not_found；403 support_role_required/tenant_suspended|
|DELETE `/api/v2/tenants/{tenant_id}/support-grants/{grant_id}`|无body，CSRF|SupportGrant（**无staff_email字段**）|当前owner；allow_suspended=true，停用仍可撤；404 not_found；已撤返回原grant（可重复）|
|GET `/api/v2/support-grants`|无|`{items:(SupportGrant & {tenant_name:string})[]}`，最多200，expiry升序|当前有效platform_support；只返回归属自己、未撤、未到期、tenant active/suspended；会记录support.list审计；没有staff_email|
|GET `/api/v2/support-grants/{grant_id}/usage`|无|`{items:{id,session_id,status,created_at,finished_at:string\|null}[],limit:200,tenant_id:string}`|支持角色+自己的有效grant；tenant active/suspended；403 support_grant_inactive/support_role_required；非本人/不存在404 not_found|
|GET `/api/v2/support-grants/{grant_id}/sessions/{session_id}`|无|`{id:string,created_at:string,messages:{id:string,role:string,content:string,created_at:string}[],has_more:boolean}`|同上且allow_content=true，否则403 support_content_forbidden；session必须同tenant，否则404 not_found；最多500 messages，取501判断has_more；**没有后续分页接口**|

出处 [apps/api/support.py:13–38](../apps/api/support.py#L13); `schemas.py:175–182`; [modules/support.py:33–319](../modules/support.py#L33)。

UI说明：

- expires_at是epoch秒；渲染倒计时要乘1000。失效状态从revoked_at和expires_at计算，服务端没有status字段。
- Owner创建表单默认15分钟，allow_content默认不勾；仅tenant active显示批准按钮。撤销按钮可在suspended显示。Grant重复审批不会替换旧grant，也没有extension/update接口。
- Staff panel以grant selection切换support scope，横幅显示tenant_name、reason、expiry、正文授权；不要调用 `setTenantId(grant.tenant_id)` 冒充membership。专用API使用完整 `/api/v2/support-grants/...` URL。
- Metadata表不给用户名字/邮箱、prompt、错误详情、provider cost；session列表不是一个额外接口，使用usage返回的session_id进入授权正文视图。前端不得把普通Session DTO强制套入支持响应（后者缺title/agent/user/run等）。
- 403 support_grant_inactive立即清正文和metadata，返回grant列表；没有授权/过期时显示空态。支持grant列表最多200，无搜索/分页。
- Staff role撤销会撤所有未撤grant；再授予support角色不会复活旧grant。`support.py:426–434`。
- **代码/文档缺口**：grant保留approver_membership_id/version快照，但是 `_authorized` 未校验该approval membership是否仍active/owner或version相等。`support.py:88–89,201–234` 与 `docs/multi-tenant-implementation.md:73`（声称每次复核审批成员关系）不一致。Owner transfer/revoke对既有grant的失效行为不能承诺；当前代码仅复核staff、grant期限/撤销、tenant状态和allow_content。

合法请求示例：
```json
{"staff_email":"support@example.test","reason":"排查任务重复进入重试状态","minutes":15,"allow_content":false}
```

### E. 租户模型策略与平台模型/部署 UI

|method/path|请求|响应|授权/错误|
|---|---|---|---|
|GET `/api/v2/tenants/{tenant_id}/models`|无|`{items:(Model & {is_default:boolean})[],default_model:string}`|models.read；default_model只有legacy role=admin时返回服务配置值，否则""；不可当真实tenant默认选择，真实默认读policy|
|PATCH `/api/v2/tenants/{tenant_id}/models/{model_id}`|仅 `{active:boolean}`|`{model_id:string,enabled:boolean}`|admin（owner/tenant_admin）+models.policy；model须已有grant，否则404 model_not_granted；包含name/alias/price/limits等即403 use_platform_models|
|GET `/api/v2/tenants/{tenant_id}/model-policy`|无|ModelPolicy|models.read（finance_viewer无此cap）|
|PUT `/api/v2/tenants/{tenant_id}/model-policy`|`{default_model_id?:string(max64)\|null,ordered_model_ids?:string[](max200)\|null,expected_version?:int>=1}`|ModelPolicy + `{updated_at:string}`|models.policy；对tenant_settings.version CAS；每个模型须本tenant active，否则404 model_not_authorized；重复ID去重保持顺序；409 configuration_conflict|
|GET `/api/v2/platform/models`|无|`{items:Model[]}`|platform.models.manage；扫描各租户，仅选平台归属active deployment、enabled grant、active model；**不是独立全局model表**，可能出现同alias的多租户源条目|
|GET `/api/v2/platform/tenants/{tenant_id}/model-grants`|无|`{items:(Model & {deployment_id:string,enabled:boolean,policy_version:number,price_version_id:string,sync_status:string})[],gateway:{status:string,desired_version:number,applied_version:number}}`|platform.models.manage，tenant存在；未enroll gateway={status:"not_enrolled",desired_version:0,applied_version:0}|
|POST `/api/v2/platform/tenants/{tenant_id}/model-grants`|`{source_model_id:string(1–64)}`|201 Model + `{deployment_id:string,price_version_id:string}`|platform.models.manage + platform.pricing.manage；从全租户源目录查active平台deployment grant；无源404 not_found；目标同alias模型会upsert，price_version增加|
|POST `/api/v2/platform/tenants/{tenant_id}/models`|`{deployment_id:string(1–64),name:string(1–120),alias:string(1–200,pattern),active?:boolean=true,input_price?:string="1",output_price?:string="3",context_window?:int(256–2000000)=32768,max_output_tokens?:int(1–128000)=2048}`|201 Model + deployment_id/price_version_id|platform.models.manage+pricing.manage；价格>0、<=1000000、最多12位小数；max_output_tokens<=context_window；404 deployment_not_found；409 model_configuration_conflict/gateway_state_conflict|
|GET `/api/v2/platform/deployments`|无|`{items:Deployment[]}`|platform.models.manage；仅owner_scope=platform；按created_at,id|
|POST `/api/v2/platform/deployments`|`{name:string(1–120),internal_route:string(1–200,pattern),base_url:string(1–500),gateway_id?:"primary"=primary,capabilities?:("chat"\|"tools")[]=["chat"]}`|201 Deployment|platform.models.manage；base_url必须等于服务配置的litellm_url（URL规整后比较），否则422 unsupported_gateway；不能配置upstream key；route已相同归属时返回原record，不更新name/url/caps；route不同归属409 deployment_route_owned|

pattern=`^[a-zA-Z0-9_.:/-]+$`。价格、IDs、scope真实schema：`schemas.py:41–85,228–247`; handlers `main.py:348–377,676–690`; `tenancy.py:326–474,513–533`; service `identity.py:1100–1181`; `gateway_control.py:472–539,585–751`。

关键UI语义：

- Model-policy PUT是**完整覆盖**，省略default_model_id会设置null，省略ordered_model_ids会设置[]。送完整选择+顺序+current version。GET response没有updated_at，PUT多updated_at。
- policy验证只看本tenant active model，未检查grant enabled/远端已同步（`identity.py:1146–1158`）；UI不能据保存policy成功推断模型已ready。
- 授予模型和启用/禁用模型不会自动enroll，也不会增加binding.desired_version（`gateway_control.py:665–751`）。用户须在网关面板点enroll同步route集合；runtime.resolve还检查当前credential routes包含deployment.internal_route（`gateway_control.py:943–950`）。现 model-grants.sync_status是共享binding.status，不是逐model readiness证据。
- 新建模型/重授模型同alias是upsert并新增price version，接口没有Idempotency-Key或expected_version；不要无提示自动重放POST。UI标明价格按每百万tokens的USD金额，内部价格版本通过_add_price固定unit/currency（`gateway_control.py:541–584`）。
- Deployment API只是登记**已经在primary网关配置好的路由**；没有供应商凭据输入、上游测试或部署HTTP健康检查；名称列表不代表真实网关route已经存在。无部署PATCH/DELETE/enable/status更新API；tenant-owned deployment在服务层有支持但公开DeploymentCreate schema没有owner字段，属于未公开能力。
- 旧 `/models` POST返回410 use_platform_models；tenant修改价格/部署403。旧v1 gateway入口要求租户admin+platform.gateway.manage，不应作为平台UI入口。

合法请求示例：
```json
{"default_model_id":"model_demo_01","ordered_model_ids":["model_demo_01","model_demo_02"],"expected_version":4}
```
```json
{"source_model_id":"model_demo_01"}
```
```json
{"name":"团队文本模型","alias":"team-text","deployment_id":"deployment_demo_01","input_price":"1.20","output_price":"3.60","context_window":32768,"max_output_tokens":2048,"active":true}
```
```json
{"name":"平台文本路由","internal_route":"platform-text-route","base_url":"http://litellm:4000","gateway_id":"primary","capabilities":["chat","tools"]}
```
最后示例仅在服务端litellm_url确为该base时合法，UI建议从实际已知primary配置提示用户，不发任意地址。

### F. 网关管理 UI（仅平台运营）

```ts
type GatewayBindingSummary = {
  status:string; mode:"none"|"legacy"|"enrolled";
  id?:string; tenant_id?:string; gateway_id?:string; purpose?:string;
  desired_version?:number; applied_version?:number; error_code?:string;
};
type GatewayCredentialSummary = {
  id:string; generation:number; status:string; expires_at:string|null;
  created_at:string; revoked_at:string|null; fingerprint:string; //只12位摘要
};
type GatewayOperationSummary = {
  id:string; action:string; status:string; phase:string; error_code:string;
  attempt_count:number; updated_at:string;
};
type GatewayStatus = GatewayBindingSummary & {
  credentials:GatewayCredentialSummary[]; operations:GatewayOperationSummary[];
  operation_counts:Record<string,number>;
};
```

|method/path|request|response|gating/errors|
|---|---|---|---|
|GET `/api/v2/platform/gateway`|无|`{configured:boolean,admin_url:string}`|platform.gateway.manage；configured仅bool(settings.litellm_admin_url)，未测外部连接|
|GET `/api/v2/platform/tenants/{tenant_id}/gateway`|无|GatewayStatus|platform.gateway.manage，租户存在；not_enrolled时仅status/mode+空credentials/operations+counts，无versions/id；credentials按generation倒序，operations最新50；counts聚合全部outbox|
|POST `/api/v2/platform/tenants/{tenant_id}/gateway/enroll`|**无body/query**，CSRF|GatewayBindingSummary + `operation_id?:string`|platform.gateway.manage；生成代际并入outbox；503 secret_store_not_configured；400 no_available_model；存在live jobs返回现binding，不新建且无operation_id；已ready相同route/limits/未过期返回现binding|
|POST `/api/v2/platform/tenants/{tenant_id}/gateway/rotate`|无body/query|同enroll|强制新代际；有pending仍仅返现binding；新代际核验后旧代际延迟(model_timeout+30s)撤销|
|POST `/api/v2/platform/tenants/{tenant_id}/gateway/revoke`|无body/query|GatewayBindingSummary|立即本地revoking禁止runtime，新建撤销outbox；无binding返回 `{status:"revoked",mode:"none"}`；多次可重复，不能把200视为已远端删除|
|POST `/api/v2/platform/tenants/{tenant_id}/gateway/operations/{operation_id}/reconcile`|无body/query|`{operation_id:string,status:string}`，status仍原retry_wait/reconciling|仅retry_wait/reconciling可用；改变next_attempt_at=0，不重放未知创建；其它状态/找不到job409 operation_not_reconcilable|

未识别 `/gateway/{operation}`→404 not_found；`gateway_state_conflict`409涵盖DB关联/重复冲突。出处 `tenancy.py:476–511`; `gateway_control.py:259–303,366–446,991–1133,1143–1325,1327–1357`。

状态展示规则：

- Binding常见status：not_enrolled / legacy / provisioning / ready / degraded / failed / revoking / revoked；mode独立none / legacy / enrolled。状态列为String，UI应保留未知状态文本，不假定穷尽enum。
- Outbox常见status：pending / applying / retry_wait / reconciling / applied / failed / cancelled；action provision/revoke；phase展示原服务值（user_pending/user_sent/key_pending/key_sent/.../verified），不要把不同phase当最终成功。
- live jobs=pending/applying/retry_wait/reconciling（`gateway_control.py:43`）；处理过程中禁用重复enroll/rotate按钮可避免无效果提交。reconcile只对retry_wait/reconciling显示。
- Credential常见pending/active/retiring/revoked；只展示id/generation/status/expiry/fingerprint。没有明文key取回、下载或copy-key API；不要展示任何secret_ciphertext、external_key_id或master key输入。
- 轮换中binding可仍ready、desired_version>applied_version，旧凭据继续可用；新代际verified后applied_version追上，旧凭据retiring可继续后台撤销。若未知写变reconciling则binding可degraded、runtime拒绝新调用。`gateway_control.py:1103–1110,1529–1613,1774–1827`。
- 轮询GatewayStatus即可追踪最新operations；后端没有网关SSE接口，也无poll间隔承诺。UI可用现有刷新按钮+有live jobs时短间隔轮询，失败停止自动放大写操作。
- enroll/rotate接口没有网关limits表单输入；虽然服务enroll支持limits参数，公开route没传。当前默认gateway limits budget100/rpm60/tpm120000/parallel4与tenant entitlements不是同一个自动同步限额（`gateway_control.py:37–42,991–1042`）。UI不能宣称套餐编辑自动同步LiteLLM限额。
- `/platform/gateway.configured`只表示有admin_url；可链接独立LiteLLM UI，不证明gateway-control URL/key/encryption key或worker配置完成。网页登录与LiteLLM登录不同会话。
- SaaS gateway-sync startup要求有效Fernet密钥、control URL与控制凭据；API lifespan显式拒绝持有gateway_control_key（`main.py:112–114`; `gateway_control.py:1860–1866`）。enroll HTTP提交只要求服务端加密配置，并不现场调用/验证control配置；真正失败可稍后出现在operation/error_code。
- local legacy只允许local模式且全平台tenant总数恰1，新增第二租户后会503 gateway_enrollment_required（`gateway_control.py:913–925`）。不能把legacy状态当multi-tenant可用。

安全普通响应示例（结构示意、没有真实凭据）：
```json
{"id":"binding_demo_01","tenant_id":"tenant_demo_01","gateway_id":"primary","purpose":"inference","mode":"enrolled","status":"provisioning","desired_version":1,"applied_version":0,"error_code":"","operation_id":"operation_demo_01"}
```
```json
{"operation_id":"operation_demo_01","status":"reconciling"}
```

## 5. 调用、账务、配额、审计、导出和关闭

### 页面与功能：调用统计

#### GET T/usage/calls（以及 GET /api/v1/usage/calls）

源码 [apps/api/main.py:551](../apps/api/main.py#L551) → [modules/platform.py:885](../modules/platform.py#L885) → [infrastructure/tables.py:196](../infrastructure/tables.py#L196)。

- query：`limit:int=100`（1..200），`cursor:string|null`（最长 300）。只有这两个外部参数。响应 `{items:Call[],next_cursor:string|null}`；倒序 `created_at,id`，长度恰好 limit 时仍生成下一游标，下一次可能空。游标必须原样回传；损坏 400 `invalid_cursor`（[modules/platform.py:426](../modules/platform.py#L426)）。
- Call 字段：`id,tenant_id,created_at,user_id,run_id,model_id,model,agent_name,user_email,status,cost,error:string`；`price_version,input_tokens,output_tokens:int`；`finished_at:string|null`；`usage_source:string="litellm_gateway"`；`provider_cost?:string|null` 仅 `platform.costs.read` 返回；永不返回 raw_usage。created_at/finished_at 是 ISO 字符串。money 为十进制字符串，显示可 format，提交/累计不能用 JS 浮点制造金额事实。
- 数据 scope：含 `usage.read_all` 返回当前组织所有 calls，否则仅 user.id。auth 路由识别 usage 时要求 `tenant.read`，服务层通过 capability 决定 own/all（不是“管理员能读全部对话”）。调用列表不返回对话或 prompt。
- 财务事实覆盖：reservation `settled→confirmed`，`reserved→running`，其他状态保留；cost、tokens、provider_cost 以 reservation 为准（[modules/platform.py:923](../modules/platform.py#L923)）。正常 possible statuses：admitting、rejected、running、confirmed、released、unresolved、written_off；UI 需未知状态 fallback。
- 无服务器 model/status/search/date 筛选参数；前端可做已加载数据本地筛选并明确范围。分页应该实际使用 next_cursor，避免默认 100 条当完整统计。
- UI：表格模型/Agent/用户/状态/input-output tokens/客户费用/创建时间；加载下一页状态独立；空状态“暂无调用”；筛选后空“当前已加载记录无匹配”；权限不足不显示供应商成本字段。
- 示例：`GET T/usage/calls?limit=50`；合法形状 `{"items":[{"id":"call_demo","tenant_id":"tenant_demo","created_at":"2026-10-02T08:00:00+00:00","user_id":"user_demo","run_id":"run_demo","model_id":"model_demo","model":"chat-demo","agent_name":"示例 Agent","user_email":"demo@example.invalid","status":"confirmed","input_tokens":100,"output_tokens":40,"cost":"0.00014","price_version":1,"error":"","finished_at":"2026-10-02T08:00:02+00:00","usage_source":"litellm_gateway"}],"next_cursor":null}`；示例是结构演示，不是 live 数据。
- 可复用 [apps/web/src/pages.tsx:1149](../apps/web/src/pages.tsx#L1149) UsagePage。现有搜索/状态筛选仅当前 items，第 1161 行 CSV 也是当前返回数组的本地导出，第 1303 行明确“当前返回”；不能写成服务端全量导出。真正异步导出见第 7 节。

#### GET T/dashboard

源码 [apps/api/main.py:319](../apps/api/main.py#L319)、[modules/platform.py:977](../modules/platform.py#L977)。响应：`requests,tokens,active_runs:int`、`cost:string`、`success_rate:number`、`daily:[{date:string,requests:int,tokens:int,cost:string}]`、`models:[{model:string,requests:int,tokens:int,cost:string}]`、`gateway_configured:boolean`。有 billing.read 时合并 Wallet 字段；member 无 Wallet 字段。scope own/all 与 calls 相同。无 query。daily 是最近最多 30 个有数据日期的分组，日期固定 Asia/Shanghai；totals 为全记录 aggregate，不能声称“最近 30 天总数”，models 最多 100 组（第 1003/1028/1039 行）。`gateway_configured` 是组织网关 ready/legacy 判定，不代表 live inference 验收已通过。

### 页面与功能：费用中心 / 平台组织财务

#### 钱包读取

`GET T/billing/wallet`：租户 `billing.read`；`GET PT/billing/wallet`：平台 `platform.billing.manage`。无 query/body。源码 [apps/api/main.py:565](../apps/api/main.py#L565)、[apps/api/tenancy.py:284](../apps/api/tenancy.py#L284)、[modules/billing/service.py:269](../modules/billing/service.py#L269)、第 368 行。

Wallet = `{balance:string,reserved:string,available:string,currency:string,blocked:boolean,block_reason:string}`；`available=balance-reserved`，实际初始化 USD，没有记录时返回零钱包而非 404（第 280 行）。UI 卡片余额/预占/可用；blocked 警示 block_reason；零额度显示联系平台人员入账。没有在线支付、绑定银行卡或自动充值接口。

#### 流水读取

`GET T/billing/ledger`：billing.read；响应 `{items:Ledger[]}`；无 query/pagination/filter。[apps/api/main.py:569](../apps/api/main.py#L569)、[modules/billing/service.py:1192](../modules/billing/service.py#L1192)、第 130 行。Ledger = `id,tenant_id,type,amount,balance,description,fingerprint,created_at:string`；`actor_id,call_id,idempotency_key:string|null`。金额为 decimal string；created_at 降序。当前服务生成 type 只有 credit/charge（第 426、912 行），虽然现有前端标签还列 refund/adjustment/debit/consumption，不代表有这些 mutation。无 PT/ledger route；平台财务如果没有目标会员权限，不能假设可读全租户流水。

UI：流水表时间/类型/金额/交易后余额/说明；空“还没有资金流水”；搜索若做仅客户端；不得提供账本编辑、删除、退款、发票、下载账单 API。可复用 `pages.tsx:1311` BillingPage，Nav `billing.read`（`App.tsx:23`）。

#### 平台人工入账

- 首选 `POST PT/credits`（注意 path 不含 billing）；[apps/api/tenancy.py:268](../apps/api/tenancy.py#L268)。平台 billing.manage，目标租户存在；不要求 membership/active 租户。
- 兼容 `POST T/billing/credits` / v1：billing.manage + tenant owner/admin + active 租户（`main.py:573`）。
- body `{amount:string,description:string}`；description 1..500；amount >0、≤1,000,000,000、最多 12 位小数、有限十进制；description 作为审计说明。`Idempotency-Key` required 1..160（route），底层服务最多 200（`schemas.py:126`、`main.py:240`、`service.py:372`）。全 route 成功 200，响应 Wallet。
- 相同 actor+amount+description+key 重试不再入账，返回当前钱包；改变请求内容复用 key →409 `idempotency_conflict`。422 `invalid_amount`/`audit_required`/`balance_limit`，400 `idempotency_required`，404 `not_found`，403 `platform_role_required`。
- 示例 body `{"amount":"25.50","description":"示例内部额度凭证 DEMO-001"}`；header `Idempotency-Key: demo-credit-001`。这是人工平台额度记录，不发起支付。
- UI：平台组织详情里金额和凭证号/说明 modal；提交中禁用；同一次提交失败可用同 key 重试，用户修改 body 应产生新 key；成功刷新 Wallet，不自动恢复 blocked。
- 可复用 `Tenancy.tsx:311` modal、`PlatformResources.tsx:181` TenantFinancePanel、`Tenancy.tsx:272` capability gate。当前 modal body 改动时不重建 key，应在实现里处理幂等冲突反馈。

### 页面与功能：预占、人工对账和钱包阻断

#### GET T/billing/reservations / GET PT/billing/reservations

T：billing.read，全当前租户；没有 own-reservations endpoint。PT：platform.billing.manage。无 query/分页/状态参数；响应 `{items:Reservation[]}`，created_at 降序、不限制数量（`main.py:587`、`tenancy.py:290`、`service.py:1205`）。

Reservation 完整已实现字段（`service.py:89`、第 241 行）：

- string：`id,tenant_id,user_id,model_id,run_id,call_id,fingerprint,status,reason,created_at,updated_at`。
- decimal string：`amount,input_price,output_price,cost,overage`；`actual_cost,provider_cost:string|null`。
- int：`estimated_input_tokens,max_output_tokens,rate_tokens`；`input_tokens,output_tokens:int|null`。
- `rate_scopes:string[]`；`raw_usage:object|null`（JSON 解码）；T 端点在无 platform.costs.read 时删除 provider_cost/raw_usage；PT 端点返回底层完整 reservation，无额外 projection。UI 应仅显示必要字段，不展示 fingerprint/rate scopes 当业务信息。
- 状态 `reserved|unresolved|settled|released|written_off`。unresolved 是未知真实用量而非免费；仍持有钱包/预算金额和原限流窗口内的用量，但释放执行并发名额；amount 为预占，cost 为客户已扣费用，actual_cost 为计算实际金额，overage 为平台承担差额。cost 不能替代 amount，estimated tokens 不能当 confirmed tokens。
- 正常 worker 结算把 provider_cost 参数传 None（`worker/main.py:597`），所以有 platform.costs.read 也不保证供应商成本非空；不能凭成本空值推出毛利或成本为零。
- UI：“待对账/全部”tab 可本地筛选；待对账只 unresolved；行的人工处理按钮仅 unresolved 且平台财务有权，其他只读；loading/empty/error/refresh 明确。可复用 `BillingReconciliation.tsx:42`，目前仅装在平台组织财务（`PlatformResources.tsx:209`），不要向租户 finance_viewer 展示不可调用的 mutation。

#### POST PT/billing/reservations/{call_id}/resolve

兼容 T/v1 同 suffix；PT platform.billing.manage；T 还要求 active 租户且 owner/admin。源码 `tenancy.py:296`、`main.py:601`、`schemas.py:140`、`service.py:995`。

- body `action:"confirm"|"write_off",reason:string`（strip，5..500），`input_tokens:int|null=null,output_tokens:int|null=null`。
- confirm 必须同时给 input/output 非负整数，服务上限各 2,000,000,000；write_off 不得携带非 null tokens。Header Idempotency-Key 1..160。成功 200 Reservation；人工确认记录来源 `manual_reconciliation` 及 actor/reason/action，不当作供应商自动回执。
- confirm 示例 `{"action":"confirm","reason":"已核对示例网关用量回执","input_tokens":100,"output_tokens":40}`；write_off 示例 `{"action":"write_off","reason":"示例人工核查无法恢复最终回执"}`。
- only unresolved；不存在/其他租户 404 `reservation_not_found`；非待核实 409 `resolution_not_pending`；同 key 改 body/actor/call 409 `idempotency_conflict`；422 `invalid_usage`/`invalid_resolution`/`audit_required`；503 `accounting_invariant`。精确同请求 replay 返回已有 reservation（第 1033 行）。
- confirm 按调用时价格快照算实际金额，客户扣款 `min(actual,amount)`；超过预占时平台承担差额、blocked=true 并记录 overage，不能在 UI 索要额外扣款（第 855 行）。write_off 只释放预占，保留估算 rate token，不创造 charge/refund（第 1083 行）。
- API 在财务事务完成后另调用 `worker.sync_call_receipt` 更新 calls（`main.py:615`，`tenancy.py:306`）；两步不是同一个原子事务，第二步失败可能 response error 但钱已结算，必须按相同 key 重试并 reload，不能重建 key 重扣。calls 读取会以财务事实覆盖 stale 投影。
- UI modal：显式选择处理方式，不预选；confirm 需整数框，write_off 不传 token；reason 必填；请求中禁用/保存原 key；成功 reload reservations+wallet+usage；保留 stale 状态变动 409 的可恢复提示。可复用 `BillingReconciliation.tsx:181`。

#### POST PT/billing/unblock

兼容 T/v1同 suffix。body `{reason:string}`（5..500），无需 Idempotency-Key；200 Wallet。源码 `tenancy.py:319`、`main.py:620`、`schemas.py:147`、`service.py:1146`。

- already unblocked 直接返回 Wallet；blocked 且任何 reserved 或 unresolved →409 `unresolved_billing`。解除仅审计“承认已记录超额”，不抹账、不退款，不绕过余额/配额/预算。
- UI仅 blocked 时显示，财务权限必需；加载/错误/存在 reserved 或 unresolved 禁用；原因 modal；成功 reload Wallet。现有 `BillingReconciliation.tsx:71` 只禁止 unresolved，没有禁止 reserved，需修正 UI gate 保持与服务一致。

### 页面与功能：配额 / 组织审计

#### GET / PUT T/quotas

源码 `main.py:624` → `schemas.py:131` → `service.py:448`、第 503 行。租户 `quotas.manage` 且 owner/admin；无 PT equivalent。GET 无 query，返回 `{items:Quota[]}`。Quota = `{id:string,tenant_id:string,scope:"tenant"|"user"|"model",subject_id:string,rpm:int|null,tpm:int|null,concurrent:int|null,max_budget:string|null}`。没有 version/etag/updated_at；相同 tenant+scope+subject upsert。若没有 tenant policy，GET 添加默认 id `default:{tenant_id}`，rpm 60/tpm 120000/concurrent 4，max_budget null。

PUT body 同字段去 id/tenant_id：scope required；subject_id 1..64；rpm int|null ≤1,000,000；tpm≤1,000,000,000；concurrent≤10,000；max_budget decimal string|null（0..1e9、12位）。后四项默认 null：提交 omitted 也置 null，并非 PATCH 保留。0 禁止，null 只是不添加本策略限制，平台 entitlements 仍生效；max_budget 是累计预算，并非月/日 reset（第 696 行）。200 返回 Quota。

- tenant subject 必须当前 tenant_id，否则 400 `invalid_subject`；user 必须当前 active membership user_id（不是 membership id），否则404 `not_found`；model 必须当前组织模型，否则404。422 `entitlement_exceeded`：不能超平台硬上限；403 `entitlement_missing`：没有套餐；其余 schema/invalid_amount/invalid_quota。
- 示例 `{"scope":"tenant","subject_id":"tenant_demo","rpm":60,"tpm":120000,"concurrent":4,"max_budget":"100"}`；保留 null 须用真实 null。
- UI列表+新建/编辑 modal；user/model下拉分别取真实 memberships/models，依赖加载时禁用、没有目标时提示；不提供删除策略按钮（无 DELETE），无 optimistic version claim；执行 PUT 时应保留完整四个字段。现有 `pages.tsx:1477` 可复用，编辑锁 scope，第 1605 行 payload，用户显示映射第 1492 行。

#### GET T/audit

`main.py:650`、[infrastructure/tables.py:226](../infrastructure/tables.py#L226)、`service.py:157`；租户 audit.read + owner/admin。无 query，响应 `{items:Audit[]}`。先读取最多200条 platform_audits，再追加当前租户所有 billing_audit，按 created_at 倒序截断200条。不是可访问全历史审计的分页 API。

共同字段：`id,tenant_id,actor_id,actor_email,action,target,created_at:string`；财务条目 actor_id可null，details为JSON string，不是自动解码 object；系统 actor_email fallback `system`。非财务平台表条目不一定有details。UI表格actor/action/target/time，details可按需安全JSON解析，搜索只本地200条；不提供编辑、删除、恢复操作。可复用 `pages.tsx:1713`。

另 `GET /api/v2/platform/audit`（`tenancy.py:535` → `identity.py:1183`）要求 platform.audit.read，是独立平台审计来源，不等于跨租户 billing_ledger API。

### 页面与功能：后台元数据 CSV 导出

源码 [apps/api/operations.py:13](../apps/api/operations.py#L13)、第 35 行；[modules/operations.py:104](../modules/operations.py#L104)、第 127 行；[infrastructure/operations_tables.py:18](../infrastructure/operations_tables.py#L18)。

#### POST T/exports

body `{kind:"calls"|"runs"="calls",scope:"self"|"tenant"="self"}`；Header `Idempotency-Key` 1..160 required；202 ExportJob。exports.create；calls tenant scope另需 usage.read_all；runs 只 self，即使管理员也不导出别人的运行。same applicant+tenant+key+kind/scope replay同job；跨参数复用409 `idempotency_conflict`。

ExportJob 公共字段：`id:string,kind:"calls"|"runs",scope:"self"|"tenant",status:string,row_count:int,created_at:string,updated_at:string,expires_at:number,error_code:string,cutoff:string,measurement_note:string`。expires_at 是 Unix seconds float，其他时间 ISO string。measurement_note 明确：范围在申请前创建，费用/用量/status按每页生成当时财务事实，后续结算不会改写文件。不是一个全财务一致性时间点快照。

- 403 `private_export_only`（runs tenant），`capability_required`，`export_access_revoked`（membership/identity版本变），`tenant_deleting`（closure purging/deleted）。400 `idempotency_required`；429 `export_limit`（组织 queued/running job数达max_export_jobs或entitlement缺失）；422 `invalid_export`或schema。
- status 实际：queued↔running，经分页 ready，错误 failed，过期 expired；后续维护会将 failed（以及已存在的 cancelled）清理为 expired并保留error_code（第539行），因此expired也应显示失败原因。现有前端还映射cancelled，但当前服务没有取消路由/状态mutation，不得提供取消。
- 示例 `{"kind":"calls","scope":"self"}`，成功示例 `{"id":"export_demo","kind":"calls","scope":"self","status":"queued","row_count":0,"created_at":"2026-10-02T08:00:00+00:00","updated_at":"2026-10-02T08:00:00+00:00","expires_at":1791014400,"error_code":"","cutoff":"2026-10-02T08:00:00+00:00","measurement_note":"示例：范围截止申请时间，用量与费用取生成各页当时账务事实。"}`。

#### GET T/exports

响应 `{items:ExportJob[]}`；无query/分页/过滤，固定最近最多200条，created_at降序、id升序；**始终仅当前申请人**，即使tenant scope任务也不向同组织其他管理员共享（`operations.py:232`）。同exports.create current check，purging时连列表也403。UI pending job每4秒轮询可复用现有实现；停在queued不等于API失败，可能maintenance缺席，展示后台生成中/刷新即可。

#### GET T/exports/{job_id}/download

response raw streamed CSV，`Content-Type: text/csv; charset=utf-8`，`Content-Disposition: attachment; filename="{kind}-{job_id}.csv"`、Cache-Control no-store、nosniff（API第49行）。这是HTTP字节流，非SSE/WS；无progress事件和百分比。

- 404 `export_not_found`（非申请人也404）；409 `export_not_ready`（非ready或超过expires_at）；403 revoked/tenant_deleting；410 `export_expired`（artifact已清理）；503 `export_storage_invalid`（目录/文件/size/hash异常）。每chunk重新授权。发生在StreamingResponse generator开始/中途的异常可能表现为中断/截断下载，不保证仍是JSON错误，UI不得只凭开始200说完整文件成功；下载客户端catch需覆盖网络中断。
- calls CSV列固定 `id,created_at,model,agent_name,user_email,status,input_tokens,output_tokens,cost,price_version,finished_at`；runs CSV列 `id,created_at,agent_name,status,finished_at`（`operations.py:45`）。不包含message/prompt/provider_cost/raw_usage。无receipt的历史call导出可能pending_evidence/零tokens/零cost（第402行），与普通calls页面fallback旧投影存在差别，应如实解释。
- UI modal类型/scope，runs 强制self；仅有usage.read_all才显示tenant calls scope；任务表状态/row_count/截止/过期；ready且未过期才能download；下载busy/AbortController；empty/error重试；failed/expired重新申请用新key，无retry/cancel/delete路由。不要设计“全组织会话导出”“自定义字段”“日期导出”表单。
- 可复用 `OperationsPanel.tsx:41` TenantExportsPanel（poll第54行、download第71行、form第134行）；在 `App.tsx:261` UsagePage下加载，仅exports.create。

### 页面与功能：关闭组织

#### POST PT/close

`operations.py` route第64行 → service第649行。platform.tenants.manage（platform_admin）且目标存在；不要求目标membership；tenant.close capability虽赋予owner，**没有实现owner关闭端点**，不可创建组织owner关闭按钮。

body `{reason:string}`（1..1000；service strip且不允许空白）；202 Closure。无Idempotency-Key；同tenant已有任务直接返回已有Closure，不改变reason；不是可重启/撤销关闭。立即tenant status=closing、version+1，取消queued runs，再尝试gateway revoke，异常不会撤销已记录关闭（第677行）。不存在/deleted且无job404 `tenant_not_found`，422 `reason_required`；无platform role403。

#### GET PT/closure

platform.tenants.manage；无query；200完整Closure；无job404 `closure_not_found`（UI作为未申请，不是整体页面错误）。Closure目前没有公共projection，完整字段（`operations_tables.py:72`）为：

`tenant_id,id,requested_by,reason,status,error_code,created_at,updated_at:string`；`retain_until:number`（Unix秒）；`purge_phase,fence:int`；`cursor_id,owner,legacy_key_fingerprint,tombstone_written_at,finished_at:string|null`；`lease_until:number|null`；`legacy_revocation_confirmed:boolean`。UI只显示status/reason/times/error，不曝光internal owner/fence/cursor/fingerprint。

- 状态 waiting→retaining→purging→deleted；waiting错误/阻塞 `billing_unresolved`（reserved或unresolved）、`runs_pending`（queued/running/cancelling）、`legacy_gateway_revocation_required`、`gateway_revocation_pending`（`operations.py:838`）。保留期至少30天；代码 `max(30, configured_days)*86400`（第694行）。retain_until未来时保持retaining；先可靠写外部tombstone再清理（第1184行）。存储错误可为 tombstone_storage_required/invalid、tombstone_write_failed、closure_storage_unavailable，状态可能原值并error_code持续。
- 清理包含export artifacts，删除/遮盖message、session/run内容、agent prompt/tool配置等；保留financial金额/identifiers/entries/audit；calls/financial raw evidence脱敏，membership最后revoked、tenant deleted（第987/1105/1142行）。UI不得声称账单或审计被删除，也不得声称可恢复。
- legacy gateway撤销“确认”只有 privileged maintenance CLI `--confirm-legacy-revocation TENANT_ID`，会真实gateway key_info read-back，没有HTTP endpoint（第1221行；[apps/maintenance/main.py:29](../apps/maintenance/main.py#L29)）。不得给UI一个由用户勾选“我已撤销”即可推进按钮。
- UI平台组织详情内，no job empty显示关闭按钮；名称确认仅前端（后端只reason），busy时禁用；有任务显示阻塞+retain_until+finished_at，10秒轮询直到deleted；没有取消/恢复/强制清理/缩短保留期endpoint。可复用 `OperationsPanel.tsx:182` TenantClosurePanel；`Tenancy.tsx:345` 只canManage显示。

## 6. 安全的请求与事件示例

以下 ID 为示意，实施时从实际列表/创建响应取得；示例不是运行记录。写请求除登录外携带真实会话的 CSRF，Run 再带一次提交固定的 `Idempotency-Key`。

`POST T/agents`（201；组织 agents.manage + admin 兼容门控）：

```json
{"name":"计算助手","description":"示例白名单工具 Agent","system_prompt":"准确回答并使用计算器核对算术。","model_id":null,"temperature":1,"max_steps":8,"max_tokens":1024,"tools":["calculator"]}
```

AgentCreate 只有 name 必需；description 默认空，system_prompt 默认源码的助手提示，model_id 默认null，temperature默认1、max_steps默认8、max_tokens默认1024、tools默认[]。PATCH所有字段可选，显式model_id:null清除偏好，其他null在handler中被排除。保存时显式绑定model_id的Agent若max_tokens超过模型max_output_tokens会400 output_limit_exceeded；本次手选模型或自动选模型时，入队会取两者最小值作为执行快照。见 [platform.py:570](../modules/platform.py#L570)、[platform.py:809](../modules/platform.py#L809)。

随后先发布 Agent，再 `POST T/sessions`（201）：

```json
{"agent_id":"agent_demo_01","title":"示例算术会话"}
```

`POST T/sessions/session_demo_01/runs`（202；`Idempotency-Key: demo-run-001`）：

```json
{"message":"计算 12 × 8，并解释结果。","model_id":null}
```

结构示意响应，创建 Run 不包含 cost/message；详情读取才增加这两项：

```json
{"id":"run_demo_01","tenant_id":"tenant_demo_01","session_id":"session_demo_01","user_id":"user_demo_01","agent_name":"计算助手","created_at":"2026-10-02T08:00:00+00:00","status":"queued","error":"","finished_at":null,"model_id":"model_demo_01","model_alias":"team-text"}
```

`GET T/runs/run_demo_01/events?after=3` 的平台 SSE 帧形状：

```text
id: 4
event: message.delta
data: {"run_id":"run_demo_01","tenant_id":"tenant_demo_01","sequence":4,"type":"message.delta","data":{"text":"结果","call_id":"call_demo_01"},"created_at":"2026-10-02T08:00:01+00:00"}

```

取消是 POST，不发新的模型消息；SSE断线通过after/Last-Event-ID恢复，不能把重新连接等同重建Run。列表分页的后续请求必须回传服务端next_cursor原值，不能自行生成offset或total_count。

## 7. 诊断接口

- `GET /api/v1/health` 无认证，200 `{status:"ok",gateway_configured:boolean,rate_limit_storage:"redis+database"|"database"}`，只是配置存在，不是网关call通过（`main.py:247`）。
- `GET /api/v1/ready` 无认证：200/503 `{status:"ready"|"unavailable",service:"api"}`；异常仅`{status:"unavailable"}`。检查schema、配置有Redis时ping；不检查worker/maintenance/gateway可调用（`main.py:257`、[modules/health.py:80](../modules/health.py#L80)）。
- `GET /api/v1/platform-status` platform.tenants.manage：`{status:"ready"|"degraded",missing_services:string[],live_services:string[]}`，heartbeat聚合；local要求worker+maintenance，SaaS另gateway-sync。无任务进度、队列操作或后台重启HTTP能力（`main.py:269`、`health.py:108`）。

这些接口没有自动启动/恢复服务的mutation。现有App尚未挂载独立诊断页，可在平台运营中按权限展示；具体判定边界见 [架构核查](architecture-source-audit.md)。

## 完整注册接口附录（AST 静态提取）

本附录只列注册事实。成功状态码是路由声明；410/403固定拒绝及业务错误优先于该声明，见上文。`main.py:676–690` 在显式 tenancy/operations/support 注册之前复制 v1 业务路由为 v2 租户路由。未导入应用、未调用数据库或网络。

| Method | 完整 Path | 声明成功码 | Handler / 源码 | 注册类型 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/health` | 200 | `health` · [main.py:247](../apps/api/main.py#L247) | 显式 |
| GET | `/api/v1/ready` | 200 | `ready` · [main.py:257](../apps/api/main.py#L257) | 显式 |
| GET | `/api/v1/platform-status` | 200 | `service_status` · [main.py:269](../apps/api/main.py#L269) | 显式 |
| POST | `/api/v1/auth/login` | 200 | `login` · [main.py:277](../apps/api/main.py#L277) | 显式 |
| GET | `/api/v1/auth/me` | 200 | `me` · [main.py:297](../apps/api/main.py#L297) | 显式 |
| POST | `/api/v1/auth/logout` | 200 | `logout` · [main.py:301](../apps/api/main.py#L301) | 显式 |
| POST | `/api/v1/auth/password` | 200 | `password` · [main.py:314](../apps/api/main.py#L314) | 显式 |
| GET | `/api/v1/dashboard` | 200 | `dashboard` · [main.py:319](../apps/api/main.py#L319) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/dashboard` | 200 | `dashboard` · [main.py:319](../apps/api/main.py#L319) | 复用 v1 handler |
| GET | `/api/v1/gateway` | 200 | `gateway_admin` · [main.py:323](../apps/api/main.py#L323) | 显式 |
| GET | `/api/v1/users` | 200 | `users` · [main.py:330](../apps/api/main.py#L330) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/users` | 200 | `users` · [main.py:330](../apps/api/main.py#L330) | 复用 v1 handler |
| POST | `/api/v1/users` | 201 | `create_user` · [main.py:334](../apps/api/main.py#L334) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/users` | 201 | `create_user` · [main.py:334](../apps/api/main.py#L334) | 复用 v1 handler |
| PATCH | `/api/v1/users/{user_id}` | 200 | `patch_user` · [main.py:342](../apps/api/main.py#L342) | 显式 |
| PATCH | `/api/v2/tenants/{tenant_id}/users/{user_id}` | 200 | `patch_user` · [main.py:342](../apps/api/main.py#L342) | 复用 v1 handler |
| GET | `/api/v1/models` | 200 | `models` · [main.py:348](../apps/api/main.py#L348) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/models` | 200 | `models` · [main.py:348](../apps/api/main.py#L348) | 复用 v1 handler |
| POST | `/api/v1/models` | 201 | `create_model` · [main.py:360](../apps/api/main.py#L360) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/models` | 201 | `create_model` · [main.py:360](../apps/api/main.py#L360) | 复用 v1 handler |
| PATCH | `/api/v1/models/{model_id}` | 200 | `patch_model` · [main.py:366](../apps/api/main.py#L366) | 显式 |
| PATCH | `/api/v2/tenants/{tenant_id}/models/{model_id}` | 200 | `patch_model` · [main.py:366](../apps/api/main.py#L366) | 复用 v1 handler |
| GET | `/api/v1/agents` | 200 | `agents` · [main.py:379](../apps/api/main.py#L379) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/agents` | 200 | `agents` · [main.py:379](../apps/api/main.py#L379) | 复用 v1 handler |
| POST | `/api/v1/agents` | 201 | `create_agent` · [main.py:408](../apps/api/main.py#L408) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/agents` | 201 | `create_agent` · [main.py:408](../apps/api/main.py#L408) | 复用 v1 handler |
| PATCH | `/api/v1/agents/{agent_id}` | 200 | `patch_agent` · [main.py:412](../apps/api/main.py#L412) | 显式 |
| PATCH | `/api/v2/tenants/{tenant_id}/agents/{agent_id}` | 200 | `patch_agent` · [main.py:412](../apps/api/main.py#L412) | 复用 v1 handler |
| POST | `/api/v1/agents/{agent_id}/publish` | 200 | `publish` · [main.py:420](../apps/api/main.py#L420) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/agents/{agent_id}/publish` | 200 | `publish` · [main.py:420](../apps/api/main.py#L420) | 复用 v1 handler |
| GET | `/api/v1/sessions` | 200 | `sessions` · [main.py:424](../apps/api/main.py#L424) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/sessions` | 200 | `sessions` · [main.py:424](../apps/api/main.py#L424) | 复用 v1 handler |
| POST | `/api/v1/sessions` | 201 | `create_session` · [main.py:441](../apps/api/main.py#L441) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/sessions` | 201 | `create_session` · [main.py:441](../apps/api/main.py#L441) | 复用 v1 handler |
| GET | `/api/v1/sessions/{session_id}` | 200 | `session_detail` · [main.py:445](../apps/api/main.py#L445) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/sessions/{session_id}` | 200 | `session_detail` · [main.py:445](../apps/api/main.py#L445) | 复用 v1 handler |
| POST | `/api/v1/sessions/{session_id}/runs` | 202 | `create_run` · [main.py:449](../apps/api/main.py#L449) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/sessions/{session_id}/runs` | 202 | `create_run` · [main.py:449](../apps/api/main.py#L449) | 复用 v1 handler |
| GET | `/api/v1/runs` | 200 | `runs` · [main.py:460](../apps/api/main.py#L460) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/runs` | 200 | `runs` · [main.py:460](../apps/api/main.py#L460) | 复用 v1 handler |
| GET | `/api/v1/runs/{run_id}` | 200 | `run` · [main.py:476](../apps/api/main.py#L476) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/runs/{run_id}` | 200 | `run` · [main.py:476](../apps/api/main.py#L476) | 复用 v1 handler |
| POST | `/api/v1/runs/{run_id}/cancel` | 200 | `cancel` · [main.py:480](../apps/api/main.py#L480) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/runs/{run_id}/cancel` | 200 | `cancel` · [main.py:480](../apps/api/main.py#L480) | 复用 v1 handler |
| GET | `/api/v1/runs/{run_id}/events` | 200 | `events` · [main.py:484](../apps/api/main.py#L484) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/runs/{run_id}/events` | 200 | `events` · [main.py:484](../apps/api/main.py#L484) | 复用 v1 handler |
| GET | `/api/v1/usage/calls` | 200 | `calls` · [main.py:551](../apps/api/main.py#L551) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/usage/calls` | 200 | `calls` · [main.py:551](../apps/api/main.py#L551) | 复用 v1 handler |
| GET | `/api/v1/billing/wallet` | 200 | `wallet` · [main.py:565](../apps/api/main.py#L565) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/billing/wallet` | 200 | `wallet` · [main.py:565](../apps/api/main.py#L565) | 复用 v1 handler |
| GET | `/api/v1/billing/ledger` | 200 | `ledger` · [main.py:569](../apps/api/main.py#L569) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/billing/ledger` | 200 | `ledger` · [main.py:569](../apps/api/main.py#L569) | 复用 v1 handler |
| POST | `/api/v1/billing/credits` | 200 | `credit` · [main.py:573](../apps/api/main.py#L573) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/billing/credits` | 200 | `credit` · [main.py:573](../apps/api/main.py#L573) | 复用 v1 handler |
| GET | `/api/v1/billing/reservations` | 200 | `reservations` · [main.py:587](../apps/api/main.py#L587) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/billing/reservations` | 200 | `reservations` · [main.py:587](../apps/api/main.py#L587) | 复用 v1 handler |
| POST | `/api/v1/billing/reservations/{call_id}/resolve` | 200 | `reconcile` · [main.py:601](../apps/api/main.py#L601) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/billing/reservations/{call_id}/resolve` | 200 | `reconcile` · [main.py:601](../apps/api/main.py#L601) | 复用 v1 handler |
| POST | `/api/v1/billing/unblock` | 200 | `unblock` · [main.py:620](../apps/api/main.py#L620) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/billing/unblock` | 200 | `unblock` · [main.py:620](../apps/api/main.py#L620) | 复用 v1 handler |
| GET | `/api/v1/quotas` | 200 | `quotas` · [main.py:624](../apps/api/main.py#L624) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/quotas` | 200 | `quotas` · [main.py:624](../apps/api/main.py#L624) | 复用 v1 handler |
| PUT | `/api/v1/quotas` | 200 | `quota` · [main.py:628](../apps/api/main.py#L628) | 显式 |
| PUT | `/api/v2/tenants/{tenant_id}/quotas` | 200 | `quota` · [main.py:628](../apps/api/main.py#L628) | 复用 v1 handler |
| GET | `/api/v1/audit` | 200 | `audits` · [main.py:650](../apps/api/main.py#L650) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/audit` | 200 | `audits` · [main.py:650](../apps/api/main.py#L650) | 复用 v1 handler |
| GET | `/api/v2/me` | 200 | `me_v2` · [main.py:702](../apps/api/main.py#L702) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}` | 200 | `tenant` · [tenancy.py:65](../apps/api/tenancy.py#L65) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/memberships` | 200 | `members` · [tenancy.py:87](../apps/api/tenancy.py#L87) | 显式 |
| PATCH | `/api/v2/tenants/{tenant_id}/memberships/{membership_id}` | 200 | `member_update` · [tenancy.py:91](../apps/api/tenancy.py#L91) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/invitations` | 200 | `invitations` · [tenancy.py:98](../apps/api/tenancy.py#L98) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/invitations` | 201 | `invite` · [tenancy.py:102](../apps/api/tenancy.py#L102) | 显式 |
| POST | `/api/v2/invitations/accept` | 200 | `accept` · [tenancy.py:106](../apps/api/tenancy.py#L106) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/ownership-transfer` | 200 | `transfer` · [tenancy.py:110](../apps/api/tenancy.py#L110) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/entitlements` | 200 | `limits` · [tenancy.py:114](../apps/api/tenancy.py#L114) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/model-policy` | 200 | `model_policy` · [tenancy.py:118](../apps/api/tenancy.py#L118) | 显式 |
| PUT | `/api/v2/tenants/{tenant_id}/model-policy` | 200 | `model_policy_update` · [tenancy.py:122](../apps/api/tenancy.py#L122) | 显式 |
| GET | `/api/v2/platform/tenants` | 200 | `tenants` · [tenancy.py:126](../apps/api/tenancy.py#L126) | 显式 |
| POST | `/api/v2/platform/users` | 201 | `create_global_user` · [tenancy.py:130](../apps/api/tenancy.py#L130) | 显式 |
| POST | `/api/v2/platform/tenants` | 201 | `create_tenant` · [tenancy.py:174](../apps/api/tenancy.py#L174) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/suspend` | 200 | `suspend` · [tenancy.py:226](../apps/api/tenancy.py#L226) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/resume` | 200 | `resume` · [tenancy.py:233](../apps/api/tenancy.py#L233) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/entitlements` | 200 | `platform_limits` · [tenancy.py:240](../apps/api/tenancy.py#L240) | 显式 |
| PUT | `/api/v2/platform/tenants/{tenant_id}/entitlements` | 200 | `platform_limit_update` · [tenancy.py:245](../apps/api/tenancy.py#L245) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/credits` | 200 | `credit` · [tenancy.py:268](../apps/api/tenancy.py#L268) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/billing/wallet` | 200 | `platform_wallet` · [tenancy.py:284](../apps/api/tenancy.py#L284) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/billing/reservations` | 200 | `reservations` · [tenancy.py:290](../apps/api/tenancy.py#L290) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/billing/reservations/{call_id}/resolve` | 200 | `resolve` · [tenancy.py:296](../apps/api/tenancy.py#L296) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/billing/unblock` | 200 | `unblock` · [tenancy.py:319](../apps/api/tenancy.py#L319) | 显式 |
| GET | `/api/v2/platform/models` | 200 | `platform_models` · [tenancy.py:326](../apps/api/tenancy.py#L326) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/model-grants` | 200 | `model_grants` · [tenancy.py:360](../apps/api/tenancy.py#L360) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/models` | 201 | `create_tenant_model` · [tenancy.py:409](../apps/api/tenancy.py#L409) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/model-grants` | 201 | `grant` · [tenancy.py:421](../apps/api/tenancy.py#L421) | 显式 |
| GET | `/api/v2/platform/gateway` | 200 | `gateway` · [tenancy.py:476](../apps/api/tenancy.py#L476) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/gateway` | 200 | `gateway_status` · [tenancy.py:484](../apps/api/tenancy.py#L484) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/gateway/{operation}` | 200 | `gateway_operation` · [tenancy.py:490](../apps/api/tenancy.py#L490) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/gateway/operations/{operation_id}/reconcile` | 200 | `gateway_reconcile` · [tenancy.py:503](../apps/api/tenancy.py#L503) | 显式 |
| GET | `/api/v2/platform/deployments` | 200 | `deployments` · [tenancy.py:513](../apps/api/tenancy.py#L513) | 显式 |
| POST | `/api/v2/platform/deployments` | 201 | `deployment` · [tenancy.py:528](../apps/api/tenancy.py#L528) | 显式 |
| GET | `/api/v2/platform/audit` | 200 | `audits` · [tenancy.py:535](../apps/api/tenancy.py#L535) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/exports` | 202 | `export_create` · [operations.py:35](../apps/api/operations.py#L35) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/exports` | 200 | `exports` · [operations.py:45](../apps/api/operations.py#L45) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/exports/{job_id}/download` | 200 | `download` · [operations.py:49](../apps/api/operations.py#L49) | 显式 |
| POST | `/api/v2/platform/tenants/{tenant_id}/close` | 202 | `close` · [operations.py:64](../apps/api/operations.py#L64) | 显式 |
| GET | `/api/v2/platform/tenants/{tenant_id}/closure` | 200 | `closure` · [operations.py:68](../apps/api/operations.py#L68) | 显式 |
| GET | `/api/v2/tenants/{tenant_id}/support-grants` | 200 | `tenant_grants` · [support.py:16](../apps/api/support.py#L16) | 显式 |
| POST | `/api/v2/tenants/{tenant_id}/support-grants` | 201 | `approve` · [support.py:20](../apps/api/support.py#L20) | 显式 |
| DELETE | `/api/v2/tenants/{tenant_id}/support-grants/{grant_id}` | 200 | `revoke` · [support.py:24](../apps/api/support.py#L24) | 显式 |
| GET | `/api/v2/support-grants` | 200 | `my_grants` · [support.py:28](../apps/api/support.py#L28) | 显式 |
| GET | `/api/v2/support-grants/{grant_id}/usage` | 200 | `usage` · [support.py:32](../apps/api/support.py#L32) | 显式 |
| GET | `/api/v2/support-grants/{grant_id}/sessions/{session_id}` | 200 | `content` · [support.py:36](../apps/api/support.py#L36) | 显式 |
| GET | `/api/v2/platform/users` | 200 | `identities` · [support.py:40](../apps/api/support.py#L40) | 显式 |
| POST | `/api/v2/platform/users/{user_id}/roles` | 200 | `set_role` · [support.py:46](../apps/api/support.py#L46) | 显式 |

统计：113 个 method/path 组合（84 个显式声明 + 29 个 v2 租户别名），不包含 SPA catch-all、FastAPI自动文档与 OpenAPI 路由。固定拒绝、缺少角色或运行依赖的组合不代表可操作功能。
