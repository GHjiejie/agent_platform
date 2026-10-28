# 内置可视化界面助手

平台将 `agent-agui-generated-ui` 的 HTML/CSS/JS 生成契约与浏览器隔离渲染能力接入现有 Agent 运行时。模型调用仍通过平台模型网关执行，继续使用组织授权、发布快照、计费、取消与事件重放。

## 使用

更新 Web、API 和 Worker 后，API 启动时的 `Platform.bootstrap()` 会为已有组织补装 `builtin_key=generative_ui` 的“可视化界面助手”，发布为第 1 版。已安装的其他 Agent、用户编辑与已发布版本均保留。支持开发者执行策略的版本需要先迁移至 `0011_agent_execution_policy`，详见[执行策略配置](agent-execution-policy.md)。

在 Agent 列表或 Playground 中选择“可视化界面助手”，发送例如“生成一个可点击的计数器”或“用示例数据生成一个可切换月份的销售仪表盘”。生成后可继续要求修改，运行时会将本会话最近一次成功运行的界面代码作为 assistant 数据放在当前问题之前。普通问题仍可直接文字回答。

该 Agent 自动选择组织可用的模型，要求模型部署声明支持文本和工具调用。当前 128K（131072 tokens）只是首次安装的默认输出预算；开发者可以在 Agent 草稿中修改输出预算、思考强度、总预算和输出上限恢复次数，发布后生效。运行仍受实际模型和平台限额约束。缺少模型、组织网关授权或额度时，沿用平台的明确错误提示。

自定义 Agent 也可以在编辑器里勾选“生成可视化界面”工具，并自行编写相应提示词。

## 执行与恢复

1. 已发布的工具白名单授权 `generate_sandboxed_ui`，输入包含 `initialHeight`、`placeholderMessages`、`css`、`html`、`jsFunctions`、`jsExpressions`。平台使用 snake_case 工具名以符合现有工具契约；参数保持参考项目的结构。
2. 服务端校验字段、长度、初始化表达式和总计 180 KB 的 UTF-8 载荷，只确认传输，绝不执行生成代码。
3. 成功的工具调用产生持久 `artifact.created` 事件，AG-UI 投影为 `ACTIVITY_SNAPSHOT`，类型为 `GENERATIVE_UI`。正文和普通工具事件继续沿用原协议。
4. Playground 收到整份校验通过的代码后创建隔离 iframe，等待初始化成功才开放交互。此次接入采用完整代码到达后的展示，不提供工具参数逐 token 的界面预览。网络断线通过现有复合游标恢复；结束与重连的消息快照均保留 Activity。
5. 会话详情新增 `artifacts`，每项包含 `id`、`run_id` 和渲染内容；消息新增 `run_id` 用于把界面附在对应轮次。刷新和打开历史对话会从该组织会话的运行事件恢复代码。

界面代码与运行事件使用同一保留和清理机制。iframe 内的点击计数、输入值等内存状态不保存，刷新后重新初始化。继续修改只参考最近一次成功运行产生的完整版本，取消或失败运行不作为后续代码上下文。

## 浏览器边界

渲染器移植自参考项目的 `Artifact.tsx` 和 `sandbox.ts`，无需原项目后端、Node 网关、模型目录或额外服务。

- iframe 仅使用 `sandbox="allow-scripts"`，不授予 same-origin、表单、弹窗或顶层导航能力；宿主消息同时检查实际 iframe Window、随机通道与消息结构。
- HTML 先在 inert template 中净化，移除脚本、内联事件、外部链接/资源与嵌入能力；内联 SVG 只保留基础图形、文字、渐变和裁剪。
- CSP 禁止网络请求、外部脚本、frame、worker、外部字体和表单；仅运行带随机 nonce 的内联初始化代码。
- `jsFunctions` 和所有 `jsExpressions` 在同一个 async 作用域中依次运行。初始化超时或脚本异常时退回不执行生成 JS 的预览，错误详情按纯文字显示。
- 高度随内容变化，宿主接受的高度上限为 6000 px，窄屏宽度跟随容器。

iframe 不提供 CPU/内存配额，也不能视为浏览器所有导航行为的绝对隔离；不适合运行任意重负载或依赖真实凭据的应用。

## 验证

后端测试覆盖工具授权/参数大小、真实 Worker 的事件持久化、AG-UI Activity、游标恢复、历史、修改上下文和跨组织隔离。浏览器测试使用临时 SQLite 与确定性模型适配器，不连接付费模型或现有部署数据库：

```sh
.venv/bin/python -m unittest agent_platform.tests.test_generative_ui agent_platform.tests.test_ag_ui
.venv/bin/python -m uvicorn agent_platform.tests.ag_ui_smoke_app:create_smoke_app --factory --host 127.0.0.1 --port 18302
# 在另一个终端，从 agent_platform/apps/web 启动开发服务并验证
PLATFORM_API_PROXY=http://127.0.0.1:18302 npm run dev -- --port 15183 --strictPort
PLATFORM_E2E_BASE_URL=http://127.0.0.1:15183 PLATFORM_AG_UI_SMOKE=1 npm run test:e2e -- tests/ag-ui-live.spec.ts tests/ag-ui.spec.ts tests/sandbox.spec.ts
```
