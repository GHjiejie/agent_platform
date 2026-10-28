# Playground 的 AG-UI 协议

2026-10-15。浏览器与 Agent Runtime 的交互使用 **AG-UI 1.0 + HTTP SSE**。后端使用官方 `ag-ui-protocol==1.0.0`，前端使用 `@ag-ui/core@1.0.2` 的类型和事件校验器；依赖版本已写入锁文件。协议依据：[AG-UI 架构](https://docs.ag-ui.com/concepts/architecture)、[事件](https://docs.ag-ui.com/concepts/events)、[RunAgentInput](https://docs.ag-ui.com/sdk/python/core/types)。

管理台的模型、Agent、组织和费用资源仍通过 REST 管理。Worker 与 LiteLLM 之间继续使用原生 Responses API；AG-UI 不改变模型配置、供应商协议或计费方式。

## 发起运行

先通过 `POST /api/v2/tenants/{tenant_id}/sessions` 创建自己的会话，再向会话的 AG-UI 地址提交官方 `RunAgentInput`：

```http
POST /api/v2/tenants/{tenant_id}/sessions/{session_id}/ag-ui
Accept: text/event-stream
Content-Type: application/json
X-CSRF-Token: <当前登录会话的 CSRF token>
```

```json
{
  "threadId": "session_demo",
  "runId": "client-generated-uuid",
  "protocolVersion": "1.0",
  "messages": [{"id": "message-generated-uuid", "role": "user", "content": "请计算 2+3"}],
  "state": {},
  "tools": [],
  "context": [],
  "forwardedProps": {"modelId": "model_demo"}
}
```

`threadId` 必须等于 URL 中的会话 ID。`runId` 为 1–64 字符的字母、数字、下划线或连字符，在同一组织、同一用户范围内作为幂等标识。相同 runId、会话、最新用户消息（含消息 ID）及模型选择会复用同一次运行；改变这些参数返回 409。前端重试失败提交时复用 runId 和消息 ID。`Idempotency-Key` 可随请求发送，AG-UI 路由的权威幂等标识是 body.runId。

最后一条消息必须是非空用户文本，长度最多 32000 字符；消息 ID 非空、请求内不重复、最多 128 字符。可只传最新用户消息，也可携带已有用户/助手/工具消息。旧消息仅视为浏览器缓存，模型使用服务端持久化历史，首个 `MESSAGES_SNAPSHOT` 会替换客户端缓存。客户端不能通过历史消息覆盖已发布的系统提示。

`forwardedProps.modelId` 可省略或为 null，沿用已发布 Agent 与组织模型选择策略。非空的客户端 tools、context、state、parentRunId、resume，以及自定义供应商地址、密钥或系统/开发者/推理消息不在此 profile 的支持范围，会明确拒绝。工具由服务端已发布 Agent 的白名单决定。目前支持文本、calculator、current_time 和 generate_sandboxed_ui；工具参数在模型完成参数生成后作为一个 `TOOL_CALL_ARGS.delta` JSON 字符串输出。可视化工具校验成功后另发 `ACTIVITY_SNAPSHOT`（`activityType=GENERATIVE_UI`），完整代码通过隔离 iframe 显示，Activity 随结束和恢复快照保留，详见[可视化界面助手](generative-ui-agent.md)。

成功为 `200 text/event-stream`，响应头 `X-Run-ID` 为平台内部运行 ID，`X-AG-UI-Protocol-Version: 1.0`。AG-UI 事件中的 runId 保留客户端 runId，REST 运行详情和取消使用 X-Run-ID。两种 ID 在服务端冻结的关联数据中对应。鉴权、CSRF、会话归属、组织状态、发布版本、模型授权、会话串行、队列及 SSE 名额在服务端检查；新运行与 SSE 名额在同一事务中确认，连接限额拒绝不会遗留已入队的新运行。

## 事件与快照

事件放在 SSE 的 JSON `data` 中，不使用自定义 SSE `event:` 名称：

```text
id: 8.1
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"assistant:run_demo:call_demo","delta":"结果是 5。","timestamp":1792065600000}

```

| AG-UI 事件 | 用途 |
| --- | --- |
| RUN_STARTED | 每个连接的运行生命周期开始，包含 threadId / runId / protocolVersion |
| STATE_SNAPSHOT | `snapshot.run` 为调用者可见的运行投影，表达 queued / running / cancelling / 终态 |
| MESSAGES_SNAPSHOT | 已授权会话历史、精确重连点的部分内容或最终持久化结果 |
| TEXT_MESSAGE_START / CONTENT / END | 每轮模型文本开始、增量和结束，使用稳定 messageId |
| REASONING_START / REASONING_MESSAGE_START / CONTENT / END / REASONING_END | 上游明确返回的可读推理，独立于正文，使用稳定 messageId |
| STEP_STARTED / STEP_FINISHED | 有界执行步骤 |
| TOOL_CALL_START / ARGS / END / RESULT | 工具身份、参数生成完成及服务端执行结果；END 表示参数结束 |
| CUSTOM | `platform.queued`、`platform.model`、`platform.usage`，用于排队、模型展示和客户用量凭据 |
| RUN_FINISHED | 正常完成使用 `outcome.type=success`；用户取消使用官方 `outcome.type=cancelled` |
| RUN_ERROR | 失败、超时或中断，带安全 message / code |

每个连接先输出 RUN_STARTED、STATE_SNAPSHOT、MESSAGES_SNAPSHOT，再输出持久事件的映射。成功终态用最终助手消息快照替换中间步骤文本及工具消息，并保留推理和 Activity；失败或取消保留用户消息和已经收到的推理。不会向浏览器输出 Agent 系统提示、模型/网关密钥、供应商成本或加密推理项。

原生 Responses 的 `response.reasoning_text.delta` 与 `response.reasoning_summary_text.delta` 转为可读推理，只有最终响应返回推理文本时补发一次。Worker 按正文相同的缓冲阈值持久化 `reasoning.started/delta/finished`，API 再转为上述标准事件，不使用已弃用的 THINKING 事件。推理消息 ID 为 `reasoning:{platform_run_id}:{call_id}`；`metadata.platform.reasoning_status` 为 `streaming`、`complete` 或 `interrupted`。正文、工具回合和运行结束都会正确闭合推理阶段。

`GET sessions/{id}` 的 `reasoning` 字段从同一用户、同一组织、同一会话的持久事件恢复 `{id, run_id, content, status, created_at}`。Playground 实时展开推理，历史默认折叠，取消时显示“思考已中断”；刷新和重连只恢复已有事件。原生加密上下文、供应商 reasoning item ID 留在运行时；可读推理不混入助手答案或客户用量凭据。设置与兼容性见[推理显示与思考等级](reasoning.md)。

## 断线、刷新和取消

恢复地址为 `GET /api/v2/tenants/{tenant_id}/runs/{platform_run_id}/ag-ui`。这是平台提供的 AG-UI 连接扩展，只重放已存在运行的事件，不创建运行、不调用模型。支持 `Last-Event-ID` 或 `?after=sequence.part`；两者同时提供取较大值。

游标对应持久事件序号与该事件转换后的第几个 AG-UI 子事件，子事件从 0 开始。服务端恢复该游标之前的消息快照和转换状态，并输出剩余子事件。因此在 TOOL_CALL_START 与 ARGS 之间断线也不会丢失参数。越界或格式无效的游标返回 400。首连不携带游标；刷新页面时读取自己会话中的非终态 Run，从起点重放。已收到终结游标的重连仍输出完整生命周期和终结事件。

前端按游标去重，处理 UTF-8 分片、CRLF、多行 data、心跳及截断帧；只在完整事件通过官方 schema 和消息状态校验后提交游标。断流后用 GET 连接同一运行，重试间隔从 1 秒逐渐增加到 10 秒；服务器约每 10 秒发心跳，30 秒无任何数据时重连。无新持久事件的重复连接不会重置退避。切换组织、会话、退出或组件卸载会中止旧流，旧响应不能写入新界面；新消息保留用户正在阅读的位置，沿用减少动态效果规范。

`POST /api/v2/tenants/{tenant_id}/runs/{platform_run_id}/cancel` 是服务端取消扩展，继续执行原有权限与取消状态机，并通过 AG-UI 终结事件确认结果。关闭 fetch、浏览器或切换会话仅脱离接收，不会取消 Worker，也不会免除已产生的费用。所有连接继续使用独立的名额租约、登录/成员撤权检查与提交后通知机制；Redis 仅做唤醒，数据库事件是重放依据。

旧的 `POST sessions/{id}/runs` 和 `GET runs/{id}/events` 保留给已有调用者；Playground 已迁移到 AG-UI。内部 Worker 的 `message.delta` 等持久记录保持原结构，由 API 边界转换，避免改变 Worker fencing、账务凭据及已有历史。

## 验证

```bash
uv run --no-sync python -m unittest agent_platform.tests.test_ag_ui
make -C agent_platform api-contract-check
cd agent_platform/apps/web
npm run build
PLATFORM_E2E_BASE_URL=http://127.0.0.1:8010 npx playwright test tests/ag-ui.spec.ts tests/ui-refresh.spec.ts
```

真实浏览器联调使用独立临时数据库和免费模拟网关，不读取生产凭据或调用供应商。在仓库根目录运行临时应用，另一个终端执行浏览器检查，完成后结束临时应用：

```bash
uv run --no-sync uvicorn agent_platform.tests.ag_ui_smoke_app:create_smoke_app --factory --host 127.0.0.1 --port 18011
```

```bash
cd agent_platform/apps/web
PLATFORM_AG_UI_SMOKE=1 PLATFORM_E2E_BASE_URL=http://127.0.0.1:18011 npx playwright test tests/ag-ui-live.spec.ts
```

该 smoke app 验证真实 POST/SSE、工具参数/结果、可读推理、官方 JavaScript schema 消费真实 Python 事件、刷新恢复且调用数不增加、取消与费用凭据。它只用于临时本地验证。
