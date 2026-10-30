# 团队协作操作与验收

本文对应阶段 B：同组织已发布成员、固定任务模板、一个 Worker 按依赖顺序执行。一次提问创建一个主 Run，成员与汇总使用独立执行记录，主会话只保存一条最终答案。阶段 C 已提供动态规划、并行及受控续跑实现，默认关闭，尚待真实模型质量、费用和耗时评估。下文先说明阶段 B 的默认行为，再列出阶段 C 的启用条件。

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


## 阶段 C：受控启用与恢复边界

本次提交包含 C1–C4 实现与 C5 的确定性验证、评估脚本；不代表 C5 已全部验收，也没有修改部署环境。启用前必须使用迁移身份升级到 `0015_execution_checkpoints`，同时更新 API、Worker 和权限矩阵。新增加密日志与检查点仅允许 Worker 读写及维护角色执行规定的清理操作，API 与 gateway-sync 不可读取其原文。

API 与 Worker 使用一致的以下配置；组织协作开关仍需开启：

| 配置 | 默认 | 用途 |
| --- | --- | --- |
| `PLATFORM_COLLABORATION_DYNAMIC_ENABLED` | `false` | 在冻结成员候选中生成并校验计划，最多一次修复 |
| `PLATFORM_COLLABORATION_PARALLEL_ENABLED` | `false` | 同一个 Worker 内最多两个成员任务并行 |
| `PLATFORM_COLLABORATION_DURABLE_ENABLED` | `false` | 保存加密模型/工具调用证据与执行检查点 |
| `PLATFORM_COLLABORATION_RECOVERY_ENABLED` | `false` | 对具备完整恢复证据的运行允许租约接管 |
| `PLATFORM_COLLABORATION_MAX_ACTIVE_MEMBERS` | `8` | 每组织活跃成员执行上限，独立于根 Run 并发 |
| `PLATFORM_COLLABORATION_CAPACITY_WAIT_SECONDS` | `30` | 容量等待秒数，允许范围大于 0 且不超过 300 |

持久执行还要求有效的 `PLATFORM_SECRET_ENCRYPTION_KEY`。API 冻结运行功能配置；持久执行快照为版本 4，原有版本 3 团队继续遵循保守失败策略。持久化开关关闭阻止后续运行采用该能力，已冻结的持久运行仍需保留证据写入能力和解密密钥。动态、并行和恢复能力还会在执行前检查当前服务开关；不要将关闭所有开关等同于主动取消所有运行。停止具体运行使用取消接口，回滚旧程序前排空新快照运行。

团队编辑器读取组织设置返回的 `features`，仅在平台开放时允许选择动态规划及双任务并行。规划只能改变任务计划，不能扩大成员白名单、模型、工具或预算。规划、修复、成员与汇总费用全部归根 Run。容量等待不创建模型预占；预算竞争采用原子准入，汇总预留继续生效。供应商 429 不会触发无限重试。

恢复依据如下，账务凭据本身不能代替完整输出：

| 中断位置 / 证据 | 恢复行为 |
| --- | --- |
| 已准备、尚未标记发送 | 可用原调用标识继续发送，复用已存在的合法预占 |
| 完整输出已确认 | 校验加密输出摘要并复用，不再发起同一模型或观察调用 |
| 模型或观察工具已标记发送，完整输出未知 | 停止执行，不自动重发；费用保持待确认 |
| 纯函数工具结果未知 | 在相同冻结请求与当前执行权限下允许重新计算 |
| 任务及成果已确认 | 保留任务、执行与成果标识，仅继续剩余依赖 |
| 当前权限撤销、取消、超时或证据损坏 | 不继续派发；已确认用量仍照常结算 |

最多允许三次接管，接管不延长原执行截止时间。新持有者必须持有效根租约与 fence；旧持有者不能更新执行日志、检查点或任务。检查点按租户、根运行、执行及初始/修复阶段隔离，仅保存执行进度，不独立授予重放权限。SSE 重连恢复展示，Worker 接管恢复执行，两者分别验收。阶段 C 尚不支持跨 Worker 独立子任务领取或远程 Agent，这些仍属于阶段 D。

新增确定性验证命令：

```bash
python -m unittest agent_platform.tests.test_collaboration_dynamic agent_platform.tests.test_collaboration_parallel agent_platform.tests.test_execution_journal agent_platform.tests.test_execution_checkpoints agent_platform.tests.test_collaboration_recovery agent_platform.tests.test_collaboration_evaluation -v
```

恢复测试使用独立子进程在发送前、发送后、收到响应后及任务确认后强制退出，并验证并行分支恢复、观察结果复用、撤权、取消和 SSE 游标。PostgreSQL 的 `test_durable_team_journal_uses_worker_only_permissions_and_rls` 另验证真实服务身份下的持久执行与权限隔离。

## 阶段 C：真实模型评估（待执行）

评估脚本在临时测试组织中使用同一模型比较单 Agent、固定团队和动态团队，覆盖完整数据、缺失数据及含干扰指令的输入，共九次运行。所有运行共享一个总费用预算；记录结构化答案得分、模型调用次数、配置报价下的费用、待确认调用及耗时。出现未知费用时停止继续测试。确定性网关仅检验评估脚本本身，不能替代真实质量结果。

配置测试网关与专用密钥环境变量后，在项目根目录执行（报价按每百万 token，金额单位与网关账务约定一致）：

```bash
python -m agent_platform.tests.collaboration_evaluation \
  --gateway-url "$COLLABORATION_EVAL_GATEWAY_URL" \
  --model "$COLLABORATION_EVAL_MODEL" \
  --api-key-env COLLABORATION_EVAL_API_KEY \
  --input-price "$COLLABORATION_EVAL_INPUT_PRICE" \
  --output-price "$COLLABORATION_EVAL_OUTPUT_PRICE" \
  --max-cost "$COLLABORATION_EVAL_MAX_COST" \
  --report /tmp/collaboration-evaluation.json
```

报告路径必须不存在，避免覆盖验收证据。测试仅发送合成任务数据；总费用约束按配置报价计算，不等同于供应商最终发票保证。当前未运行真实模型评估，团队继续由用户显式选择，四个高级功能开关保持默认关闭。完成评估并核对结果前，不将阶段 C 标记为全部完成。


## 阶段 D1：跨 Worker 成员任务（已通过工程验证，默认关闭）

`PLATFORM_COLLABORATION_DISTRIBUTED_ENABLED=false` 控制跨 Worker 调度。API 与 Worker 必须同时使用支持快照版本 5 的程序，并完成 `0016_member_task_leases` 迁移；开启时还必须开启持久执行日志。该开关由平台配置，组织协作权限与根运行预算仍生效。C5 和阶段 D 全部验收前不得作为默认部署配置启用。

根 Worker 继续持有主 Run 租约、生成计划、释放依赖和汇总结果；成员 Worker 从 `agent_tasks` / `agent_task_leases` 领取就绪任务，不创建子 Session 或子 Run。每台 Worker 的根任务池和成员池分别最多 `PLATFORM_WORKER_CONCURRENCY` 个任务，等待成员的根任务不会占用成员池。每个团队仍最多两个并行成员，组织活跃成员总上限仍统一限制本地与跨 Worker 执行。

任务租约包含成员 owner/fence、所属根 fence、执行标识、独立截止时间与接管次数。任务每三秒续租，租约最长二十秒，且不得超过任务截止时间。写检查点、执行日志、事件和成果时必须同时满足当前根租约与当前任务租约；根取消、根 fence 变化或根租约失效都会使原成员失去写权限。任务完成和成果保存仍在同一事务中提交。

任务失联最多接管三次，复用原执行和调用标识，不延长原任务截止时间。已发送但缺少完整输出的模型/观察请求停止续跑；完整输出可以复用。根进程失联则先按阶段 C 的证据规则决定能否恢复，再撤销旧成员租约、重新领取未完成任务。用户取消后不派发新请求；已确认费用仍结算，未知费用保留待确认。

普通进程停止信号停止领取新根 Run，但为本机尚未结束的根任务继续处理成员，避免退出时死锁。强制终止仍按日志与租约恢复；这不保证供应商已收到的请求能被撤回。关闭分布式开关会阻止新领取，并使受该开关管理的运行停止继续执行；回滚前应先排空或明确取消版本 5 的运行。

专项验证：

```bash
python -m unittest agent_platform.tests.test_distributed_collaboration -v
```

测试包括不同 Worker 的任务领取、重复领取竞争、双 Worker 真正重叠执行、共享最后预算、根与任务 fence 撤销、取消、容量等待、接管上限和停机排空。独立子进程在发送前、发送后、响应确认后及任务确认后退出，验证跨进程恢复不会重发未知请求。PostgreSQL 验证使用 `test_distributed_team_uses_independent_worker_leases_and_rls`，执行过程中使用不同数据库连接及实际 Worker 服务角色。
