# 第一版实现契约

> 历史基线文档：下述单组织 API、角色及财务权限描述属于第一版。当前多租户契约见 [multi-tenant-implementation.md](multi-tenant-implementation.md)，部署步骤见 [multi-tenant-operations.md](multi-tenant-operations.md)。

本文是 `architecture.md` 的第一版可运行实现契约。正式设计保留在架构文档，实际完成范围与验证记录写入 README。

## 开发约定

- Python 包：`agent_platform`，使用仓库根 `uv` 环境。
- PostgreSQL 为部署数据库，SQLite 用于零基础设施本地启动和确定性测试；所有余额变动必须串行事务。
- 公共入口 `agent_platform.apps.api.main:create_app`；命令 `uv run python -m agent_platform`。
- 同源 Cookie 会话：登录返回 `csrf_token`，所有后续写请求带 `X-CSRF-Token`。
- API 前缀 `/api/v1`；列表统一 `{ "items": [...] }`；金额为 USD 十进制字符串。
- 成员角色第一版为 `admin`（当前租户管理）和 `member`；所有业务资源带 `tenant_id`。平台初始管理员只能通过服务端引导创建。
- 错误 `{ "error": { "code": "...", "message": "..." } }`。

## 前端接口

| 请求 | 输入/返回 |
| --- | --- |
| `POST /auth/login` | `{email,password}` → `{user,csrf_token}` |
| `GET /auth/me` | `{user,csrf_token}`；user=`{id,email,name,role,tenant_id,active}` |
| `POST /auth/logout` | `{ok:true}` |
| `POST /auth/password` | `{current_password,new_password}` |
| `GET /dashboard` | `{balance,reserved,available,currency,requests,tokens,cost,success_rate,active_runs,daily:[{date,requests,tokens,cost}],models:[{model,requests,tokens,cost}],gateway_configured}` |
| `GET /users` | 用户列表，仅管理员 |
| `POST /users` | `{email,name,password,role}` → user |
| `PATCH /users/{id}` | `{name?,role?,active?}` → user |
| `GET /models` | `{items:[{id,name,alias,active,input_price,output_price,context_window,max_output_tokens}...]}`；价格每百万 token |
| `POST /models` | 同上字段，id 自动生成；仅管理员 |
| `PATCH /models/{id}` | 更新模型字段与价格；仅管理员 |
| `GET /agents` | `{items:[{id,name,description,system_prompt,model_id,builtin_key,category,starter_prompts,temperature,max_steps,max_tokens,tools,published_version,created_at}...]}`；`model_id=null` 为运行时自动选择，`builtin_key` 标记内置角色 |
| `POST /agents` | Agent 配置；`model_id` 可空（自动选择），tools 为 `calculator` / `current_time` 的列表 |
| `PATCH /agents/{id}` | 更新草稿 |
| `POST /agents/{id}/publish` | `{version:1}` |
| `GET /sessions` | `{items:[{id,title,agent_id,created_at}...]}` |
| `POST /sessions` | `{agent_id,title?}` → session |
| `GET /sessions/{id}` | `{...session,messages:[{role,content,created_at}],runs:[...]}` |
| `POST /sessions/{id}/runs` | `{message,model_id?}` + `Idempotency-Key` → `{id,status,session_id,model_id,model_alias}`；模型覆盖参与幂等校验，只可选择本组织已启用模型 |
| `GET /runs` | `{items:[{id,session_id,agent_name,status,created_at,error,cost}...]}` |
| `GET /runs/{id}` | Run 详情 |
| `GET /runs/{id}/events` | SSE，支持 Last-Event-ID 或 `?after=sequence`；data=`{sequence,type,data}` |
| `POST /runs/{id}/cancel` | Run |
| `GET /usage/calls` | `{items:[{id,model,agent_name,user_email,status,input_tokens,output_tokens,cost,provider_cost,created_at}...]}` |
| `GET /billing/wallet` | `{balance,reserved,available,currency}` |
| `GET /billing/ledger` | `{items:[{id,type,amount,balance,description,created_at}...]}` |
| `POST /billing/credits` | `{amount,description}` + `Idempotency-Key`；管理员 |
| `GET /quotas` | `{items:[{id,scope,subject_id,rpm,tpm,concurrent,max_budget}...]}`；scope tenant/user/model |
| `PUT /quotas` | `{scope,subject_id,rpm,tpm,concurrent,max_budget}`；0 表示禁止，null 表示不限制 |
| `GET /audit` | `{items:[{id,actor_email,action,target,created_at}...]}` |

租户初始钱包余额为零，需显式入账。服务未配置 LiteLLM 时如实显示，并拒绝模型调用，不提供伪造模型回复。

## Runtime 适配契约

运行配置使用普通字典。`agent_platform.modules.runtime.engine.execute_agent(spec, messages, invoke, emit, cancelled)` 是 async 函数，返回最终 assistant 文本。`spec` 含 system_prompt、temperature、max_steps、max_tokens、tools；messages 为历史 role/content。

- `invoke(messages, tools)`：async 函数，返回 `ModelReply(content: str, tool_calls: list[dict], input_tokens: int, output_tokens: int, raw_usage: dict)`。每次 invoke 的权限、限流、费用预占和结算由平台负责。
- `emit(type: str, data: dict)`：async 函数，写持久运行事件。
- `cancelled()`：async 函数，返回 bool。
- `Gateway.stream(messages, model, max_tokens, temperature, tools)` 为 async iterator，输出 `GatewayEvent(type, data)`：`text`（text 字段）、`result`（ModelReply）。真实网关只读取 `PLATFORM_LITELLM_URL` 与 `PLATFORM_LITELLM_KEY`，没有测试 fallback。
- Runtime 使用 LangGraph 的 v3 流式执行，Gateway 使用共享模型工厂创建客户端，不修改全局 chat_model。

## 存储与计费模块契约

`Database` 提供 `engine`、`read()` 上下文（SQLAlchemy Connection）、`transaction(scope: str)` 上下文。SQLite 写事务使用 BEGIN IMMEDIATE，PostgreSQL 使用基于 scope 的事务 advisory lock。`metadata` 为 `agent_platform.infrastructure.db.metadata`。

计费/限流模块在 `agent_platform.modules.billing.service` 定义 `BillingService(db, redis_url=None)`，方法：

- `wallet(tenant_id)` → balance/reserved/available/currency 字典；`credit(tenant_id, actor_id, amount, description, idempotency_key)` → 钱包。
- `set_quota(tenant_id, scope, subject_id, rpm, tpm, concurrent, max_budget)`；`quotas(tenant_id)` → 列表。
- `reserve(tenant_id,user_id,model_id,run_id,call_id,input_tokens,max_output_tokens,input_price,output_price)` → reservation 字典（id、amount、价格快照）。
- `settle(tenant_id,call_id,input_tokens,output_tokens,provider_cost=None,raw_usage=None)`；`release(tenant_id,call_id,reason)`；`unresolved(tenant_id,call_id,reason)`。
- `ledger(tenant_id)` → 列表；`reservations(tenant_id)` → 列表。
- 计费表统一挂在 shared metadata，使用十进制金额。业务拒绝抛 `PlatformError(status,code,message)`，定义于 `agent_platform.infrastructure.errors`。
- 额度/钱包/限流原子检查，拒绝不能留下半完成预占；待确认调用保持冻结。默认每租户 RPM 60、TPM 120000、并发 4，可由管理员更改。
- 用户 max_budget 表示当前累计费用上限；周期预算完整 UI 留到后续，文档明确语义，不能伪称自然月预算。
