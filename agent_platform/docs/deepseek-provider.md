# 本机 DeepSeek 接入

接入日期：2026-10-26。本次配置的是本机 managed LiteLLM 与 `agent_platform` 的现有单组织实例。随后已新增 Wylon 并将其设为默认；本文记录保留的 DeepSeek 直连模型，当前默认配置见 [Wylon 接入](wylon-provider.md)。

## 当前配置

| 项目 | 值 |
| --- | --- |
| 平台地址 | <http://127.0.0.1:8010> |
| 模型服务商 | DeepSeek（保留直连） |
| 上游 API 地址 | `https://api.deepseek.com/v1` |
| 直连 Flash 模型 | `deepseek-flash`，界面名称为 DeepSeek V4.1 Flash，目前为可选模型 |
| 可选模型 | `deepseek-v4-pro`，界面名称为 DeepSeek V4 Pro |
| 平台到网关的协议 | 原生 Responses API，流式输出 |
| 本地网关 | `http://127.0.0.1:4000/v1` |
| 本地输出上限 | 两个模型均配置为每次最多 128K（131072 tokens），上下文为 1M（1048576 tokens）；Agent 可以设置更小上限 |
| 默认思考设置 | 本机两个路由均配置 `reasoning_effort: xhigh`；DeepSeek 官方将该兼容值映射为 high，详见[推理显示与等级](reasoning.md) |

模型名称和上下文上限来自接入时使用实际密钥请求的 `/models`：两个模型的上下文容量均为 1048576 tokens，上游最大输出为 393216 tokens。本机每次输出上限独立设置，没有直接采用上游最大值。[DeepSeek 模型发现接口](https://api-docs.deepseek.com/api/list-models/)、[Responses API](https://api-docs.deepseek.com/api/create-response/)。

前端 Playground 当前默认使用“DeepSeek V4.1 Flash（Wylon）”，仍可手动选择直连 Flash 和 Pro。原固定使用 `k3` 的 `normal` Agent 已发布新版本，改为跟随组织默认模型。

## 密钥保存与调用边界

- DeepSeek 直连密钥的私有来源文件为 `.data/providers/deepseek.env`。该文件为 `0600`，被 Git 忽略；本文不记录密钥。
- 当前 `agent_platform/.env` 保存 Wylon 默认上游和三个允许的模型。DeepSeek 直连 Flash、Pro 都已通过 LiteLLM 管理 API 注册，配置持久化在现有 LiteLLM 数据库中；默认 Wylon 路由通过 [本地 LiteLLM YAML](../deploy/litellm.yaml) 与环境变量加载。
- API 和 Worker 只收到 LiteLLM 受限调用凭据，不收到 DeepSeek 上游密钥或网关 master key。
- 受限凭据当前允许 Wylon Flash、DeepSeek 直连 Flash 和 Pro，以及 `/responses`、`/v1/responses` 路径，沿用原有预算、RPM、TPM 和并发上限。
- 原受限凭据已过期，本次已撤销，并更新为新的 30 天凭据，有效期到北京时间 **2026-11-25 11:39**。到期前需要显式轮换；启动流程不会自动延长凭据。

DeepSeek 直连路由的上游配置示意如下；当前默认路由的环境变量见 Wylon 接入说明：

```dotenv
UPSTREAM_MODEL='openai/deepseek-flash'
UPSTREAM_API_BASE='https://api.deepseek.com/v1'
```

`openai/` 是 LiteLLM 的协议适配器前缀；实际请求的服务商为 DeepSeek，上游模型名仍是 `deepseek-flash`。

## 模型目录与历史数据

首次接入新建两个直连模型目录条目、对应部署、组织授权和独立价格版本。随后新增 Wylon 时保留了这两个模型，并把默认及候选顺序改为 Wylon Flash → 直连 Flash → 直连 Pro。旧 `k3`、`gpt-6.1-sol` 条目仍停用，原 ID、价格版本、会话、历史运行和账务记录保留。网关受限调用凭据不再允许旧模型。

没有给现有账号新增平台角色，没有重置登录密码，也没有调整组织钱包额度或人工核销历史账务。

## 本机费用显示

新模型使用以下固定参考单价生成平台价格版本，单位为 **USD / 百万 tokens**：

| 模型 | 输入 | 输出 |
| --- | --- | --- |
| DeepSeek V4.1 Flash | 0.30 | 1.20 |
| DeepSeek V4 Pro | 1.32 | 3.96 |

这些数值取接入当日官方价格表的**高峰、缓存未命中**费率，作为本机费用显示的固定参考。DeepSeek 实际上游费用会受空闲时段、缓存命中和后续价格调整影响；本次没有实现按时间及缓存命中动态计价，也没有把这些固定参考值当作真实供应商账单。[官方模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)。

## 验证记录

- 使用提供的密钥查询 DeepSeek `/models`，返回 HTTP 200，确认两个实际可用模型。
- 两个模型直连 Responses 请求均返回 HTTP 200 和 `OK`。
- 两个模型经 LiteLLM 受限凭据和平台 `Gateway` 流式适配器调用，均返回完整文本与 token 用量。
- 平台实际运行：默认 Flash 返回“DeepSeek 接入成功”，显式选择 Pro 返回“Pro 接入成功”；两个运行状态均为 `succeeded`，调用状态为 `confirmed`，资金预占均为 `settled`。
- 使用实际 Flash 模型、LangGraph v3 执行器和 `calculator` 工具完成模型 → 工具 → 模型的完整回合，计算 `17 * 19` 返回 `323`。
- 独立 Chromium 登录实际前端，检查 Playground 默认模型和两个可选模型；旧模型不出现在可用选项中，未发现页面脚本错误。
- SQLite 完整性检查为 `ok`，外键检查无错误。
- `uv run --no-sync python -m unittest agent_platform.tests.test_smoke_provider agent_platform.tests.test_local_gateway -q`：22 项通过。
- `uv run --no-sync python -m unittest agent_platform.tests.test_gateway_responses -q`：14 项通过，相关回归合计 36 项通过。

详细运行 ID、当前模型 ID、浏览器截图和后续验证结果保存在 Git 忽略的 `.data/providers/deepseek-integration.json` 与 `.data/providers/deepseek-playground.png`。

## 启动与恢复

本次检查未发现 `agent-platform` Kubernetes 部署或迁移标记，实际保留的是本地 Docker Compose 网关与 SQLite 数据，因此使用现有本机 supervisor 启动于 8010。当前仓库 `make start` 的默认目标是 Kubernetes，不等同于本次本机启动方式。

```bash
make -C agent_platform local-status
make -C agent_platform local-start PORT=8010
```

应用已经运行时无需再次执行 `local-start`。本机 supervisor 的停启命令为 `local-stop` / `local-start`；LiteLLM 运行期间不要删除现有 PostgreSQL、Redis 数据卷，两个直连模型的路由配置均保存在网关数据库。

修改前的 `.env`、本地网关配置、原受限凭据和 SQLite 在线备份位于 `.data/backups/before-deepseek-20261026-113750/`，该目录含私密配置，已经设为本机私有且被 Git 忽略。正在使用的数据库有新运行后，恢复旧备份会丢失后续写入；应先停写并备份当前数据，再核对恢复范围。
