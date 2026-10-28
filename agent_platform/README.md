# Agent Platform

Python / FastAPI、React / TypeScript、LangGraph 与 LiteLLM 组成的多租户 Agent 平台。一个账号可以加入多个组织，组织成员权限与平台运营权限分开；Agent 角色与供应商模型解耦。

## 文档

- [AG-UI 交互协议](docs/ag-ui-protocol.md)：Playground 的流式运行、工具事件、取消、幂等与断线恢复。
- [可视化界面助手](docs/generative-ui-agent.md)：内置 Agent、HTML/CSS/JS 工具、隔离交互界面、历史恢复与继续修改。
- [Agent 执行策略](docs/agent-execution-policy.md)：开发者配置思考强度、调用和 Token/费用预算、运行时限及输出上限后的有限恢复。
- [架构调整与验收](docs/architecture-adjustments.md)：八项架构改进、证据及升级契约。
- [多租户实现说明](docs/multi-tenant-implementation.md)：当前架构、权限、数据库、网关、账务和交付状态。
- [账号与前端角色权限](docs/roles-and-permissions.md)：7 种角色的菜单、可操作功能、当前本机账号与本地凭据文件位置。
- [本机 DeepSeek 接入](docs/deepseek-provider.md)：当前上游、默认/可选模型、密钥位置与接入验证。
- [Wylon 接入与默认模型](docs/wylon-provider.md)：新增 Wylon Flash 并设为默认，保留 DeepSeek 直连模型。
- [Playground 流式输出优化](docs/streaming-performance.md)：本机跨进程通知、前端更新合并和延迟实测。
- [推理显示与思考等级](docs/reasoning.md)：AG-UI 推理流、历史恢复、取消状态和 extra high 配置。
- [多租户运行手册](docs/multi-tenant-operations.md)：部署、密钥、切库、备份、关闭与恢复。
- [Kubernetes 本地部署](docs/kubernetes-local.md)：OrbStack 集群、数据迁移、更新、停启及恢复。
- [迭代设计方案](docs/multi-tenant-iteration-design.md)：设计依据和正式验收矩阵。
- [历史第一版记录](docs/implementation-status.md)：旧版本验证记录，不作为多租户上线依据。

当前功能已实现。一次性 PostgreSQL/RLS、独立角色、Redis 和故障回归的验证记录见架构调整文档；本轮没有更新现有部署数据库。真实 LiteLLM/供应商兼容性、恢复演练和生产容量仍需部署环境验收。

## 已实现功能

| 领域 | 当前能力 |
| --- | --- |
| 身份 | 全局账号、多组织成员、邀请、撤销、所有权转移、按标签页切换组织 |
| 权限 | owner、tenant_admin、member、finance_viewer；独立平台管理/财务/支持角色 |
| 内容 | 会话和运行正文本人私有；支持访问需 owner 限时审批并留审计 |
| Agent | 9 个内置助手（含可视化界面生成）、草稿和不可变发布、受控工具、多轮会话 |
| 模型 | 组织模型授权、默认及候选顺序、部署与显示别名分离、价格快照 |
| 网关 | 每组织加密凭据、代际轮换、Outbox、未知外部写入核查、独立同步进程 |
| 执行 | AG-UI 1.0 流式交互、独立排队/执行期限、租户轮转、持久事件与提交唤醒、SSE独立租约及撤权检查 |
| 账务 | 精确钱包、独立供应商成本预算、追加用量证据、后台补证、未知冻结、可重建累计预算 |
| 容量 | 套餐硬上限、内部配额、成员/Agent/队列/运行/SSE/导出限制 |
| 数据 | 分页接口、私有异步导出、关闭组织、保留期、独立持久删除记录 |
| 部署 | PostgreSQL FORCE RLS、迁移与每服务独立运行角色、隔离文件卷、10 个 Helm Charts |

未配置可用模型、组织凭据或额度时会明确拒绝调用，不生成模拟答案。

## 本地 Kubernetes 部署

前置依赖：`uv`、`make`、Docker、`kubectl`、Helm，默认使用已有的 `orbstack` 集群。Python 共用根目录的 `pyproject.toml`、`uv.lock` 和 `.venv`；前端有独立 Dockerfile 和镜像。

```bash
make -C agent_platform start
make -C agent_platform deploy SERVICE=worker
make -C agent_platform status
make -C agent_platform stop SERVICE=worker
make -C agent_platform resume SERVICE=worker
```

当前实例在 `agent-platform` 命名空间由 9 个独立 Helm releases 管理：6 个 Deployment（Web、API、Worker、gateway-sync、maintenance、LiteLLM）和 2 个 StatefulSet（PostgreSQL、Redis），存储独立管理。`charts/` 下为这些服务及手动 migration 提供 10 个 Chart，没有自动部署其他服务的 Chart dependencies。每个角色有独立配置、探针、资源及镜像；后台不再加载 HTTP facade。

`make start` 逐个构建并更新服务，`make deploy SERVICE=worker` 只更新 Worker。schema 变更显式执行 `make migrate`，本地完成后 `make resume`；日常发布不隐式迁移。`make stop` 关闭准入、等运行收尾并缩容，保留 releases、PVC 和 Secret。

首次从旧 supervisor 导入使用 `make -C agent_platform k8s-import`；已有 Kubernetes 资源首次接管使用 `make helm-adopt`。当前环境两项均已完成。边界、独立 Charts、外部依赖、备份和迁移步骤见 [服务与 Helm 部署](docs/kubernetes-local.md)。通用 Chart 默认使用外部 PostgreSQL；本地 values 保留原单节点 SQLite，应用固定单副本，核心领域库和平台数据库仍共享。

访问 [本地控制台](http://127.0.0.1:8010)。本地默认账号为 `admin@example.com`，密码在 `agent_platform/.env` 的 `PLATFORM_ADMIN_PASSWORD`。初始密码只用于开户，修改配置不会重置已存在账号。

旧实例首次初始化时：

```dotenv
PLATFORM_OPERATOR_EMAILS=admin@example.com
```

显式初始化会为该邮箱引导平台管理和财务角色；普通组织管理员不会自动取得平台角色。已撤销的角色也不会被重启恢复。登录入口支持自动注册：未注册邮箱首次使用至少 12 位密码提交后，会创建全局账号并登录；已有账号继续验证原密码。新账号没有组织成员身份或平台角色，接受相同邮箱的组织邀请后才能访问组织。平台管理员也可以预先开通账号。

邀请链接仍由组织管理员复制并自行分享。当前未接入邮件发送或邮箱验证，自动注册不会填写 `email_verified_at`，也不提供邮箱验证码登录。

`PLATFORM_ADMIN_EMAIL` 和 `PLATFORM_OPERATOR_EMAILS` 中尚未开通的邮箱为预留管理账号，由初始化或平台管理员开通，不能通过公开入口抢先注册；这些邮箱的已有账号正常登录。

迁移保留原来的 `PLATFORM_SECRET_ENCRYPTION_KEY`，运行时由 Kubernetes Secret 注入。后续部署不会重新生成或覆盖集群内已有凭据。

### 模型与 LiteLLM

首次导入使用旧 managed 配置的上游与受限运行 Key，并恢复 LiteLLM PostgreSQL 数据库；运行时使用集群内 `litellm` Service。上游模型配置保存在 LiteLLM 专用 Secret。

LiteLLM 管理页是 [本地 LiteLLM](http://127.0.0.1:4000/ui)。原登录凭据已迁入 `litellm-secret`，旧配置备份仍在 `.data/local/gateway/.env.control`。平台与 LiteLLM 使用不同登录会话；只有 gateway-sync 收到用于控制网关的 master key。

首次历史单组织本地部署可采用旧受限 Key；**新增第二个组织之前，先在平台运营的网关面板将原组织纳管**，再分别为新组织授权模型和同步凭据。多租户运行没有全局 Key 回退。

尚未迁入 Kubernetes 的旧开发实例，可使用外部 LiteLLM：

```bash
make -C agent_platform local-start PORT=8010 GATEWAY=external
```

显式填写 `PLATFORM_LITELLM_URL` 和受限 `PLATFORM_LITELLM_KEY`；组织网关同步另需配置 `PLATFORM_GATEWAY_CONTROL_URL/KEY`，只传入同步器。外部模式不把根 `OPENAI_API_KEY` 当作平台运行 Key。

## 开通组织并运行

1. 平台运营开通组织并指定 owner；会自动安装 8 个已发布内置 Agent，钱包初始为零。
2. 在 LiteLLM 配置上游模型，然后在平台登记对应部署路由。
3. 向组织授权模型，指定平台报价及上下文/输出限制。
4. 在组织网关面板发起同步，待状态变为就绪。
5. 平台财务显式入账；组织按需配置更严格的内部配额。
6. 切换进入该组织，在 Playground 运行 Agent。

内置角色包括写作润色、翻译与本地化、长文总结、会议纪要、任务规划、代码排错、数据解读、客服回复。角色定义只包含指令和示例，不包含模型名称、供应商或密钥。

模型解析顺序为本次指定 → Agent 偏好 → 组织默认/候选/其他可用模型。明确指定的模型不可用时失败，不偷偷替换。运行创建后冻结模型、价格和部署，调用过程中不自动换模型或重试产生新消费。

## 运维

- SaaS 必须使用 PostgreSQL、非 owner 运行角色、HTTPS secure Cookie 和持久加密密钥。
- 迁移命令与 `init` 使用迁移身份；服务启动只核对版本，不修改 schema。
- `GET /api/v1/health` 检查 API 存活；`GET /api/v1/ready` 只检查自身数据库/Redis；鉴权的 `/api/v1/platform-status` 提供聚合状态。后台探针检查各自 Pod UID 心跳。
- 未结费用必须有证据确认或明确核销，不能通过清空预占恢复服务。
- 关闭组织的删除记录放在独立持久卷，数据库恢复前必须重放；不随 PostgreSQL 回滚。

当前 Kubernetes 参数和恢复方式见 [Kubernetes 本地部署](docs/kubernetes-local.md)。离线 SQLite → PostgreSQL 工具和 SaaS 验收见[运行手册](docs/multi-tenant-operations.md)。集群部署不代表已完成 SaaS 隔离与容量验收。

关键接口的 TS 类型由 FastAPI OpenAPI 生成：`make -C agent_platform api-contract`；`api-contract-check` 和自动验收检查漂移。严格发行验收使用 `python -m agent_platform.scripts.acceptance`，仅接受显式的一次性 PostgreSQL 17/Redis 配置和 Docker 隔离环境，任一跳过均失败；具体命令见[架构调整记录](docs/architecture-adjustments.md)。
