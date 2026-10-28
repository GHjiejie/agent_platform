# Wylon 接入与默认模型

接入日期：2026-10-26。当前本机平台为 <http://127.0.0.1:8010>。

## 当前三个模型

| 前端名称 | 网关/平台别名 | 上游模型 | 服务商 | 默认 |
| --- | --- | --- | --- | --- |
| DeepSeek V4.1 Flash（Wylon） | `wylon-deepseek-v4.1-flash` | `deepseek-ai/DeepSeek-V4.1-Flash` | `https://api.wylon.cn/v1` | 是 |
| DeepSeek V4.1 Flash | `deepseek-flash` | `deepseek-flash` | `https://api.deepseek.com/v1` | 否，可选 |
| DeepSeek V4 Pro | `deepseek-v4-pro` | `deepseek-v4-pro` | `https://api.deepseek.com/v1` | 否，可选 |

Wylon 模型使用独立别名，避免与直连 Flash 混淆。组织默认与候选顺序为 Wylon Flash → DeepSeek 直连 Flash → DeepSeek 直连 Pro；顺序只参与运行创建时的可用模型选择，不会在已经发出的调用失败后自动重新收费重试。

本次查询 Wylon `/models` 确认模型名、1048576 tokens 上下文与 393216 tokens 上游最大输出。本机当前默认每次最多输出 128K（131072 tokens），上下文窗口为 1M（1048576 tokens），实际还受 Agent 配置限制。

## 路由与密钥

- Wylon 默认路由由 `deploy/litellm.yaml` 和 `agent_platform/.env` 加载，模型适配器为 `openai/deepseek-ai/DeepSeek-V4.1-Flash`，使用原生 Responses API 流式输出。
- 思考等级已设为 `reasoning_effort: xhigh`（extra high），Playground 显示可读推理并保留历史；兼容性与验证见[推理显示](reasoning.md)。
- Wylon 密钥来源保存在 `.data/providers/wylon.env`，运行配置写入 `.env`。两个文件均为 `0600` 且被 Git 忽略，本文不记录密钥。
- 原有 DeepSeek 密钥继续保存在 `.data/providers/deepseek.env`，直连 Flash 和 Pro 路由持久化在 LiteLLM 数据库中。
- API 和 Worker 只接收受限网关调用凭据；只有 LiteLLM 接收服务商密钥。
- 已将现有受限网关凭据及其所有者的模型白名单扩展为上述三个模型，保留原预算、RPM、TPM、并发上限与 Responses 路径限制。凭据沿用原有效期：北京时间 **2026-11-25 11:39**。

当前 `.env` 中的非敏感配置为：

```dotenv
UPSTREAM_MODEL='openai/deepseek-ai/DeepSeek-V4.1-Flash'
UPSTREAM_API_BASE='https://api.wylon.cn/v1'
LITELLM_MODEL_ALIAS='wylon-deepseek-v4.1-flash'
PLATFORM_DEFAULT_MODEL='wylon-deepseek-v4.1-flash'
GATEWAY_KEY_MODELS='wylon-deepseek-v4.1-flash,deepseek-flash,deepseek-v4-pro'
```

已为 Wylon 创建独立部署、模型目录、组织授权和价格版本；两个直连模型的 ID、价格及启用状态保留。现有自动选择模型的 Agent 会使用新的组织默认模型；用户显式选择直连模型时仍走对应的直连路由。

## 本机费用显示

Wylon 模型沿用现有 Flash 的本机固定参考价：输入 **0.30 USD / 百万 tokens**、输出 **1.20 USD / 百万 tokens**。这只是本机费用显示的参考，并非已核实的 Wylon 对外报价；本次没有获取 Wylon 的供应商计价合同，也没有实现其动态成本结算。

组织钱包额度、历史预占、旧模型价格版本和历史会话均保留。

## 验证与备份

已完成以下实际验证：

- Wylon `/models`、Chat Completions、Responses 接口均返回 HTTP 200。
- 平台未显式指定模型的运行实际选择 `wylon-deepseek-v4.1-flash`，返回“Wylon 默认模型接入成功”，运行状态为 `succeeded`；调用状态为 `confirmed`，资金预占为 `settled`。
- 原有 DeepSeek 直连 Flash 和 Pro 均通过更新后的受限凭据返回 `OK`。
- 实际 Wylon 模型通过 LangGraph v3 执行器完成计算器工具回合，`17 * 19` 返回 `323`。
- 独立 Chromium 检查 Playground：自动模型为 Wylon，三个模型均可选；未发现页面脚本错误。
- 本机前端重建通过，平台 `/ready` 返回 HTTP 200，SQLite 完整性及外键检查通过。

实际验证结果及运行 ID 保存在 `.data/providers/wylon-integration.json`，浏览器截图保存在 `.data/providers/wylon-playground.png`。

修改前的本地 `.env`、网关路由信息、受限调用凭据及 SQLite 在线备份位于 `.data/backups/before-wylon-20261026-143431/`，文件均处于 Git 忽略的私有目录。恢复前应先停写并保存当前数据，以免覆盖接入后的新运行和账务。

本机服务使用现有 supervisor 管理：

```bash
make -C agent_platform local-status
make -C agent_platform local-start PORT=8010
```

应用已经运行时无需再次启动。两个直连模型的持久化配置依赖现有 LiteLLM PostgreSQL 数据卷。
