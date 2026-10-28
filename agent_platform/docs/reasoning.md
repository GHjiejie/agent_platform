# Playground 推理显示与思考等级

2026-10-26。当前默认模型为 `wylon-deepseek-v4.1-flash`，上游为 Wylon 的 `deepseek-ai/DeepSeek-V4.1-Flash`。本机 LiteLLM 默认路由配置 `reasoning_effort: xhigh`，对应用户要求的 extra high。

## 推理如何显示

网关读取 Responses 流中的 `response.reasoning_text.delta` 和 `response.reasoning_summary_text.delta`，仅转发上游明确返回的可读文本。若上游仅在最终响应的 reasoning.content 或 reasoning.summary 中返回文本，则补发一次；已经收到流式推理时不重复补发。

Worker 将这些内容缓冲为 `reasoning.started`、`reasoning.delta` 和 `reasoning.finished` 事件；API 使用 AG-UI 1.0 的 `REASONING_START`、`REASONING_MESSAGE_START/CONTENT/END` 和 `REASONING_END`。推理与正文各有消息 ID；每个工具回合的推理独立保存。

Playground 在推理期间展开“正在思考”，正文继续在同一回复中显示。持久化后的“思考过程”默认折叠，可用鼠标或键盘展开；运行取消、失败或超时会闭合未完成阶段，并显示“思考已中断”。用户手动向上阅读时保留阅读位置，沿用逐帧批量更新。

推理从持久运行事件恢复，刷新、历史会话和断线重连无需再次调用模型。会话详情的 reasoning 字段与 AG-UI 快照都遵守原有组织及会话所有者权限。历史上没有保存的推理无法补回。

仅展示上游提供的可读推理；加密推理上下文、供应商内部 ID、原生响应项和密钥不进入前端。可读推理不拼入最终答案，也不拼入下一轮用户输入；原生工具调用所需的上下文仍按原有机制仅在服务端传回上游。

## 等级与供应商兼容性

| 路由 | 本机请求等级 | 供应商行为 |
| --- | --- | --- |
| Wylon Flash（默认） | `xhigh` | 实测 Responses HTTP 200，响应回显 `reasoning.effort=xhigh`，并返回推理增量 |
| DeepSeek 直连 Flash / Pro | `xhigh` | 官方兼容值，按供应商约定映射为 `high` |

Wylon 对 `max` 的实测为 HTTP 400；当前使用已确认可用的 `xhigh`。DeepSeek 直连接口原生等级为 none、low、high、max，官方明确说明 xhigh 是 high 的兼容别名；因此不将直连的 xhigh 描述为其原生 max。[DeepSeek Responses 参数](https://api-docs.deepseek.com/api/create-response/)

思考等级控制请求意图，实际推理长度由模型和问题决定。推理和正文共用 `max_output_tokens`；本机模型和 Agent 的默认输出上限为 128K（131072 tokens），上下文窗口为 1M（1048576 tokens），已发布 Agent 可以有更小的上限。修改等级不会自动提高所有 Agent 的输出预算。

当前固定版本的 LiteLLM 对两个直连路由会合并上游流，在最终响应中返回可读 reasoning.content；平台会从该响应补发思考内容。默认 Wylon 路由已实测为真正的推理增量流。直连路由也已登记其原生推理和流式能力，以供网关能力查询使用。

## 配置、验证和恢复

默认 Wylon 路由的持久来源为 [deploy/litellm.yaml](../deploy/litellm.yaml)；两个直连路由的参数保存于现有 LiteLLM 数据库。供应商密钥仍只保存在 Git 忽略的本机配置中。

验证包含：原生推理/摘要解析及去重、加密上下文隔离、Worker 持久化、AG-UI 各阶段游标恢复、历史及权限隔离、前端网络分片、真实浏览器实时显示/键盘展开/刷新/取消/窄屏/减少动态效果，以及实际 Wylon xhigh 请求。私有验证记录位于 `.data/reasoning/`，其中兼容性结果 `effort-compatibility.json` 只记录等级、事件计数与用量，不记录推理正文或密钥。

实际 Wylon 浏览器验证运行 `003896d0b7d24e128db3b8fc5672f52e` 使用组织默认模型并成功完成，产生 6 个 AG-UI 推理增量、136 字可读推理和 1 次模型调用。刷新前后调用数均为 1，未出现页面脚本错误；记录见 `.data/reasoning/real-browser.json`。两个直连模型经平台 Gateway 的最终推理补发分别验证通过。

本次提交独立验证：119 项后端测试通过，2 项因未配置隔离 Redis 而跳过；20 项浏览器测试、前端构建、Ruff 和 API 契约检查通过。本机实际服务的 Redis 通知继续启用。

部署前的配置、网关路由信息和 SQLite 在线备份位于 `.data/backups/before-reasoning-20261026-154840/`。恢复旧数据库前须先停写并保存现有数据，避免覆盖新增会话与账务。
