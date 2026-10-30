# 固定团队协作操作与验收

本文对应阶段 B：同组织已发布成员、固定任务模板、一个 Worker 按依赖顺序执行。一次提问创建一个主 Run，成员与汇总使用独立执行记录，主会话只保存一条最终答案。动态规划、并行及自动续跑尚未开放。

## 开启与准备

1. 先运行数据库迁移到 `0013_collaboration_outcome`，再更新 API 和 Worker。迁移必须使用专用迁移身份，服务启动不修改 schema。
2. API 的平台开关 `PLATFORM_COLLABORATION_ENABLED=true` 与组织开关必须同时开启。环境示例、Compose 和 Kubernetes 模板默认关闭。Helm 的 API / Worker Chart 对应 `config.collaborationEnabled`，默认 `false`。这不是部署指令，本次实现没有修改正在运行的部署。
3. 组织管理员打开“我的 Agent → 团队协作设置”，安装“整理 → 分析 → 图表 → 汇总”顺序模板。安装创建三个成员及一个团队并发布；创建配额不足时整批回滚。重复安装不覆盖已经编辑的模板。
4. 为组织授权支持文本和工具的模型，完成网关配置并入账。模板不绑定具体供应商，入队时按现有模型选择规则为每个成员冻结路由、报价与限制。
5. 开启组织协作开关，然后在 Playground 选择“整理分析图表团队”。模板不会提供真实业务数据，只有用户提交的信息才可作为事实；没有数据时，成员必须返回缺失信息。

需要自建团队时，先创建单 Agent，在编辑器的“协作能力”中填写能力标签与任务接口并发布；再创建执行方式为“团队协作”的 Agent，为每项任务选择成员、版本、任务类型、目标与前置任务。发布时拒绝环、缺失依赖、未发布或停用成员、嵌套团队、输入输出契约不一致。任务类型和输入输出契约采用 `TaskContract`；其 `input/output` 包含 `name`、`version`、`json_schema`，必须与依赖成果的契约摘要完全一致。

第一个任务可用字符串输入接收用户文本；后继任务读取已确认的前置输出。多个前置输出按 `inputs[]` 分别提供，保留任务键、成果标识和版本，不隐式合并或复制其他成员的提示词。结构化输出不合规时仅允许一次修复，仍消耗主预算、成员调用次数与团队调用次数。

## 使用和排查

- Playground 的团队入口显示“协调模型”，单次选择只覆盖协调者，成员继续使用各自的冻结模型。调整团队成员版本需要重新保存并发布团队。
- 展开“团队协作过程”，查看任务依赖、成员版本、实际模型、结构化输出、输入摘要及校验值、成果来源、已确认费用与待确认预留。费用是十进制字符串，展示四舍五入不改变账本值。
- 详情接口：`GET /api/v2/tenants/{tenant}/runs/{run}/collaboration`，单任务为 `GET .../runs/{run}/tasks/{task}`。二者均要求当前成员资格和该 Run 的本人访问权；即使管理员也不能直接读取他人会话。
- 模板安装接口：`POST /api/v2/tenants/{tenant}/agents/templates/analysis-team`。组织开关为 `GET/PATCH .../agents/collaboration-settings`；PATCH 传 `enabled` 与读取到的 `expected_version`，并发修改会冲突，不覆盖其他设置。
- 成员错误产生 AG-UI `SUBAGENT_ERROR`，不终结根流；`subagentRunId` 标识具体执行尝试。客户端只订阅根流，以服务端事件游标重连，不重新提交模型请求。
- 必选任务失败会阻断依赖并令主运行失败。只有发布配置允许部分交付且所有失败分支均为可选时，才允许汇总，Run 的 `outcome=partial`。成功的完整交付为 `outcome=complete`。
- 已确认成果不因汇总失败消失。完整通过校验的图表仍在隔离 iframe 内交互；不完整内容或浏览器初始化失败不会被标记可用。
- 成员预占受主费用上限和任务上限约束，默认为汇总预留主费用额度的 15%，至少预留一次模型调用。规划阶段 B 使用固定模板，没有规划模型费用；最终汇总、成员及修复调用全部计入根 Run。
- 用户停止后不派发新任务或模型请求，已确认用量仍结算，未知用量维持待确认预留。停用成员或撤销授权会阻止继续调用；不能通过修改已发布快照绕过实时资格检查。
- Worker 失联时明确失败，成功任务和成果保留，未完成任务结束为阻断或取消；旧 fence 无权写入。**阶段 B 不自动续跑或重发未知请求**。SSE 断线重连仅恢复展示。

## 关闭和回滚

关闭组织开关或 API 平台开关只阻止新的团队准入，已经入队的运行继续使用冻结版本完成，或由用户显式取消。回滚到旧 Worker 前先排空团队运行；不能将包含团队快照的 Run 交给不支持该版本的 Worker。迁移的降级不会静默删除运行、成果和费用证据，数据库回滚需采用经过核对的备份恢复方案。

## 确定性验证

以下命令在项目根目录执行，使用已安装依赖的 Python 与 Node。测试使用临时数据库、模拟模型和拦截的 HTTP，不读取线上账号、不产生模型消费。

```bash
python -m unittest agent_platform.tests.test_collaboration_runtime agent_platform.tests.test_agent_registry agent_platform.tests.test_ag_ui -v
python -m agent_platform.scripts.api_contract --check
npm --prefix agent_platform/apps/web run build
# 在另一个终端启动专用前端，浏览器测试拦截 API 与 SSE。
npm --prefix agent_platform/apps/web run dev -- --port 5181 --strictPort
PLATFORM_E2E_BASE_URL=http://127.0.0.1:5181 agent_platform/apps/web/node_modules/.bin/playwright test --config agent_platform/apps/web/playwright.config.ts collaboration.spec.ts ag-ui.spec.ts
```

真实前后端验收使用另一个一次性应用，自动创建临时 SQLite、测试组织和模拟余额，使用真实 API、Worker 和 SSE，仅模型输出由确定性模拟网关提供。先完成前端构建，再在两个终端分别运行：

```bash
python -m uvicorn agent_platform.tests.collaboration_smoke_app:create_team_smoke_app --factory --host 127.0.0.1 --port 5183
PLATFORM_COLLABORATION_SMOKE=1 PLATFORM_E2E_BASE_URL=http://127.0.0.1:5183 agent_platform/apps/web/node_modules/.bin/playwright test --config agent_platform/apps/web/playwright.config.ts collaboration-live.spec.ts
```

此测试覆盖三成员与汇总的真实运行、执行中刷新重连、只有一条主答案、五次调用不重复、成果来源及图表点击。该测试身份只属于临时应用，不能用于部署实例。

PostgreSQL 需要一次性验收数据库 `agent_platform_acceptance[_suffix]`，并显式设置 `PLATFORM_ACCEPTANCE_POSTGRES_ISOLATED=1` 和 `PLATFORM_ACCEPTANCE_POSTGRES_URL`。`test_postgres_isolation.PostgreSQLIsolationTests.test_sequential_team_executes_with_deployed_service_roles` 用生产权限矩阵的非 owner 服务角色完成模板安装、准入、三成员执行、图表及汇总，验证成果与费用。没有配置时显示跳过，不能将跳过声明为 PostgreSQL 已通过。

质量评估和单 Agent 对比属于阶段 C：确定性模拟测试只能证明协议、隔离和故障行为，不能据此宣称团队回答质量优于单 Agent。阶段 B 模板保持用户显式选择，不默认将普通对话切为团队。
