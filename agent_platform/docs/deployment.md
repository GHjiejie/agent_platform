# Agent 平台：运行、部署与迁移

> 多租户版本的独立进程、数据库角色、凭据和切库操作以[多租户运行手册](multi-tenant-operations.md)为准；下文为旧版部署参考。

本文对应第一版实现。完整目标见 [架构设计](architecture.md)，模块约定见 [实现契约](implementation-contract.md)。Python 统一使用仓库根目录的 `uv` 环境；前端依赖在 `agent_platform/apps/web` 内管理。

部署前可对照[当前实现架构图](architecture-diagrams.md)中的本地启动与完整 Compose 拓扑，确认 API / Worker 的运行位置、数据存储和 LiteLLM 管理入口。

## 本地启动

安装 `uv`、Node.js/npm 与 make 并启动 Docker 后，从仓库根目录执行一条命令：

```bash
make -C agent_platform start
```

在 `agent_platform` 内执行 `make`、`make start` 或 `make run` 等价。该入口串行完成依赖准备、LiteLLM / PostgreSQL / Redis 启动、受限调用密钥引导、前端构建、平台数据库迁移和账号初始化，健康检查通过后才报告就绪。Python 使用 `uv sync --frozen --all-groups` 保留仓库共用环境。React 由 API 同源提供，无需额外启动 Vite。

缺少 `.env` 时，从模板自动创建权限为0600的配置，并生成随机初始密码；默认邮箱 `admin@example.com`。密码在 `.env` 中查看，不会打印到启动日志。已有文件完整保留；已有账号不会因引导变量改变而重置。显式设置的环境变量优先于平台文件，平台文件优先于仓库根目录 `.env`；如果已有配置的密码为空且数据库尚无管理员，初始化会明确报错，需要自行填写至少12位密码。

一键启动默认使用 `GATEWAY=managed`，读取根目录 `.env` 中的 `OPENAI_BASE_URL`、`OPENAI_API_KEY` 和 `MODEL`，将上游模型配置为 `openai/<MODEL>`，对平台提供同名别名。有显式 `UPSTREAM_API_KEY` 时使用 `UPSTREAM_MODEL` / `UPSTREAM_API_BASE` / `UPSTREAM_API_KEY`。平台运行进程只接收本机 LiteLLM 地址与受限 key；master key、UI 密码和上游 key 不进入平台运行进程。

官方管理页面默认是 `http://127.0.0.1:4000/ui`，平台管理员从侧栏「LiteLLM 网关」进入。首次启动自动生成 `.data/local/gateway/.env.control`（权限0600），其中 `UI_USERNAME` 默认为 `admin`，`UI_PASSWORD` 是独立管理页密码；数据库密码、master key、salt 也在该文件中持久保存。运行密钥保存在同目录 `.env.gateway`，仅允许明确授权模型的 Responses 调用，并有预算、RPM、TPM、并发与30天过期限制。重启验证后复用该密钥，不重置有效期，不自动轮换；无效、过期或协议权限不匹配时启动失败并明确提示。请不要删除控制文件后复用旧数据库卷，否则保存的加密数据和数据库认证将无法匹配。

本地容器归属于由 `STATE_DIR` 派生的独立 Compose 项目，平台仍使用现有 SQLite 数据。`make stop` / Ctrl+C 停止该实例的网关容器并保留卷；不会停止其他 Compose 项目。`LITELLM_PORT` 控制本机管理页与调用端口。Docker 不可用时不会偷偷回退直连上游；已有外部服务可显式使用 `GATEWAY=external`，此时读取 `PLATFORM_LITELLM_URL` / `PLATFORM_LITELLM_KEY`，空值回退根 `OPENAI_*`，并可通过 `PLATFORM_LITELLM_ADMIN_URL` 指定外部管理页。

默认模型通过 `PLATFORM_DEFAULT_MODEL` 配置，一键启动也会从根 `MODEL` 回退读取。模型目录显示默认标记，自动模式的 Agent 在每次运行时优先选择已启用的默认模型。首次注册默认模型时可设置 `PLATFORM_DEFAULT_MODEL_INPUT_PRICE` 和 `PLATFORM_DEFAULT_MODEL_OUTPUT_PRICE`（平台报价，USD / 百万 Token），启动时会为配置管理员所属组织创建模型；重启不覆盖已存在的模型。没有报价时仅在页面提示和预填，不自动生成收费价格。

启动自动为各组织安装 8 个模型无关的内置 Agent，首次安装即发布，后续启动保留修改。`0002` 迁移允许 Agent 不指定固定模型，并保留已有版本和会话；SQLite 迁移在重建表后检查外键完整性。若要降回 `0001`，需先为所有自动模式 Agent 指定模型，降级不会自动删除助手。

当本地默认网络路径无法与上游建立 TLS、但 IPv4 可用时，可设置 `PLATFORM_GATEWAY_LOCAL_ADDRESS=0.0.0.0`。该选项只影响模型调用的网络客户端，保留证书验证及零自动重试策略，客户端随调用结束关闭。

```bash
make -C agent_platform start PORT=8010
make -C agent_platform start PORT=8010 LITELLM_PORT=4001
make -C agent_platform start PORT=8010 GATEWAY=external
make -C agent_platform status
make -C agent_platform stop
```

默认访问 [本地控制台](http://127.0.0.1:8000)，设置 `PORT` 后使用相应端口。启动在前台运行，`Ctrl+C` 或另一终端的 `make stop` 会停止关联进程。状态放在 `.data/local`，通过PID、启动时间和命令核验所有权；不会终止其他端口占用者。需要自定义配置或多个隔离实例时，可传入 `ENV_FILE=... STATE_DIR=... PORT=...`；`stop/status` 必须使用对应 `STATE_DIR`，相对路径按执行 make 的目录解释。

开发前端可另开终端运行 `make -C agent_platform web PORT=8010`，访问 [Vite 开发界面](http://127.0.0.1:5173)，API 代理会使用同一个 `PORT`。原有 `install / migrate / init / build / dev / worker` 分步命令仍然可用。

本地默认使用 `agent_platform/.data/platform.db` 和 API 内嵌 Worker；未配置 Redis 时采用数据库中的共享准入记录。这个方式便于运行和测试，不代表生产容量已经验证。初始钱包余额是零，管理员需要在费用中心显式入账。

每个 Run 默认最高预占/消费合计 1 USD，可通过 `PLATFORM_MAX_RUN_COST` 设置有限正数。`PLATFORM_RUN_TIMEOUT` 默认 `0`，不限制执行总时长；设置正数时仅限制执行阶段。排队期限由 `PLATFORM_QUEUE_TIMEOUT` 独立控制，默认 300 秒。`PLATFORM_MODEL_TIMEOUT` 控制网关请求的连接与流式读取等待，默认 90 秒，并非整个 Agent 的总执行时间。

已有 LiteLLM Proxy、使用 `GATEWAY=external` 启动时，设置：

```dotenv
PLATFORM_LITELLM_URL=http://127.0.0.1:4000/v1
PLATFORM_LITELLM_KEY=受限的LiteLLM虚拟密钥
```

地址必须是 Responses 接口所在的服务器地址，通常以 `/v1` 结尾；平台统一调用该地址下的 `/responses`。模型目录中的 `alias` 必须与 LiteLLM 的 `model_name` 对应。平台没有模型模拟回复：缺少网关配置时允许管理操作，但拒绝模型调用。

一键启动通过 dotenv 解析文件，不作为 shell 脚本执行；分步 Python 命令用 `uv run --env-file` 加载配置。直接执行 Python 或 Uvicorn 时需要自行加载环境变量。设置 `PLATFORM_EMBEDDED_WORKER=false` 时，一键启动会自动管理独立 Worker；使用 `make dev` 分步启动时则要另启 `make worker`。

## Docker Compose

需要 Docker Compose 2.24 或更高版本，支持可选 `env_file`。构建上下文是仓库根目录；构建使用根 `uv.lock`，并在构建阶段生成 React 静态资源。不要移动部署目录后直接构建。

默认在 `127.0.0.1:8000` 暴露平台 API/静态页面，并在 `127.0.0.1:4000` 暴露 LiteLLM 管理页和网关接口。PostgreSQL、Redis 只在 Compose 网络内开放。部署包含：

| 服务 | 用途 |
| --- | --- |
| `postgres` | 同一实例内创建独立 `platform`、`litellm` 数据库和登录角色；撤销跨库公共连接权限 |
| `redis` | 平台使用 DB 0、LiteLLM 使用 DB 1；AOF 持久化、禁止内存驱逐 |
| `litellm` | 唯一模型出口，配置模型别名、受限密钥和网关预算 |
| `init` | 一次性执行平台 Alembic 迁移和管理员初始化 |
| `api` | FastAPI API 与 React 静态文件，同源 Cookie 登录 |
| `worker` | 独立领取和执行持久 Run |

在 `agent_platform/.env` 填写本地启动所需管理员信息，以及以下部署值：

| 环境变量 | 配置 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 管理员密码，仅数据库容器使用 |
| `PLATFORM_DB_PASSWORD` | 平台数据库角色密码，推荐独立随机十六进制字符串 |
| `LITELLM_DB_PASSWORD` | LiteLLM 数据库角色密码，推荐独立随机十六进制字符串 |
| `LITELLM_MASTER_KEY` | LiteLLM 管理密钥，以 `sk-` 开头；不注入 API 和 Worker |
| `LITELLM_SALT_KEY` | 独立随机加密盐；首次部署后保留，不随重启更改 |
| `UI_USERNAME` / `UI_PASSWORD` | LiteLLM 管理页账号与独立密码；密码必填，不能使用平台运行 key |
| `LITELLM_HTTP_PORT` | LiteLLM 本机端口，默认4000 |
| `PLATFORM_LITELLM_ADMIN_URL` | 外部模式 / 完整容器部署的浏览器可访问管理地址 |
| `UPSTREAM_MODEL` | LiteLLM provider/model，例如 `openai/gpt-4o-mini` |
| `UPSTREAM_API_BASE` | 上游接口地址，例如 `https://api.openai.com/v1` |
| `UPSTREAM_API_KEY` | 真实上游模型密钥，仅 LiteLLM 持有 |
| `LITELLM_MODEL_ALIAS` | 默认 `platform-chat`，平台模型目录也使用该别名 |
| `GATEWAY_KEY_MODELS` | 可选的逗号分隔模型白名单，默认仅 `LITELLM_MODEL_ALIAS`；必须包含默认别名，不能有重复、空项或通配符 |

数据库密码直接用于连接 URL，因此推荐十六进制随机串，避免 URL 特殊字符的编码歧义。所有密码和密钥都应分别生成，不能复用示例文本。

```bash
make -C agent_platform infra
make -C agent_platform gateway-key
make -C agent_platform up
```

`infra` 启动数据库、Redis 和 LiteLLM。`gateway-key` 在 LiteLLM 容器内运行受限密钥引导脚本，创建一个 `internal_user` 身份，为明确列出的模型生成仅允许 `/responses` 与 `/v1/responses` 的虚拟密钥。Master key 不离开该容器环境；生成的运行密钥写入 `agent_platform/.data/.env.gateway`，文件权限为 0600，归当前宿主用户所有。Compose 只把该运行密钥交给 API、Worker 和初始化服务，脚本不打印密钥。

默认运行密钥有效期 30 天，生命周期成本上限 100 USD、RPM 60、TPM 120000、并发 4，可在 `.env` 中通过 `GATEWAY_KEY_*` 调整。这里的网关预算是第二层上游保护，和平台钱包分别维护；生成密钥不会给平台充值。生产应建立到期前轮换流程。脚本发现输出文件已存在会拒绝再生成，避免重复创建凭据；轮换要在 LiteLLM 管理侧显式创建新 key、更新文件并重建 API/Worker，然后撤销旧 key。

`up` 构建并启动平台服务。Compose 的数据库地址、Redis 地址、内嵌 Worker 开关均显式覆盖本地 `.env` 值。不要把 master key 填入 `PLATFORM_LITELLM_KEY`。LiteLLM 允许在管理页中新增模型并持久化至其数据库；支持更多模型时需配置 `GATEWAY_KEY_MODELS`，同步更新运行用户、运行密钥的模型白名单和平台模型目录。平台目录中的报价与 LiteLLM 上游成本配置各自维护，不会自动互相同步。

从旧版 Chat Completions 切换时，先通过 LiteLLM 管理接口显式将已有运行密钥的 `allowed_routes` 更新为 `["/responses", "/v1/responses"]`，保留原模型范围、预算、限流与有效期。受管租户密钥也须更新或通过已有轮换流程更换。引导脚本与网关同步器都会校验精确路由权限，发现旧协议权限会拒绝复用，不会自动扩大权限或重置密钥。已有部署记录的 `protocol` 元数据应在维护窗口同步为 `responses`；新建部署默认使用该协议。

`agent_platform/.env` 中的 `PLATFORM_LITELLM_KEY` 供本地运行使用；Compose 中的运行 key 由 `.data/.env.gateway` 单独注入。已有受限 key 的管理员可直接创建该 0600 文件，内容为单行 `PLATFORM_LITELLM_KEY=...`，无需重复引导。

停止服务用 `make -C agent_platform down`。默认保留数据库和 Redis 卷；删除卷会丢失账本、用量、运行与网关密钥，不能作为普通重启步骤。

## 固定 LiteLLM 版本与调用语义

2026-09-22 查询官方 GHCR registry，精确 `1.83.0` 可用镜像标签为 `v1.83.0-nightly`。`v1.83.0`、`v1.83.0-stable`、`main-v1.83.0` 不存在，不能把不存在的标签写成可用配置。Compose 固定以下多架构 digest：

```text
ghcr.io/berriai/litellm:v1.83.0-nightly@sha256:3a8f981557838dea1340d318ba22225bfc2f2e86e4b415d6b1bc9cbe8dd26b72
```

这是用于本版兼容验证的 nightly 镜像，不是已经通过生产验收的稳定版。后续变更版本时，需要更新镜像 digest、Python 依赖与兼容验证记录，不能直接换成 `latest`。版本来源可在 [LiteLLM 官方仓库](https://github.com/BerriAI/litellm) 和 registry 查询；功能说明参考 [官方 Virtual Keys 文档](https://docs.litellm.ai/docs/proxy/virtual_keys)。

LiteLLM 配置将 `router_settings.num_retries`、`litellm_settings.num_retries`、每个模型客户端 `max_retries` 均设为零，fallback 列表为空。平台 OpenAI 客户端也设置 `max_retries=0`。一次准入对应一个实际尝试；修改这些设置会破坏当前预占和结算假设。

平台使用 Responses 流式事件，保存完成响应中的用量；网关完整输入/输出 token 和结束信息缺失时进入待核实，不能按零费用结算。即使浏览器已看到部分文字，也不能视为成功完成。取消已发出的请求同样可能留下待核实费用。

**当前结算规则是按 LiteLLM 网关计量结算，记录 `usage_source=litellm_gateway`。** 此前的 Chat Completions 兼容测试已验证 LiteLLM 1.83.0 会为缺失供应商 usage 的流自动估算 token：本地模拟供应商没有发送任何 usage 时，网关仍返回了输入 11、输出 4，协议没有附带 estimated 标记。切换 Responses 后仍需验证各路由的缺失用量行为，不能仅凭网关 usage 将其称为供应商确认的原始费用证据。平台的 confirmed 表示按网关计量完成平台结算；严格按供应商实际 token 收费需要另加可信取证与对账适配。

## 数据库迁移

平台迁移与 LiteLLM 自身数据库迁移完全分开。平台版本放在 `agent_platform/migrations/versions`，初版 `0001` 是独立 schema 快照，不会随业务模块导入而改变历史结构。

```bash
uv run --env-file agent_platform/.env alembic -c agent_platform/alembic.ini upgrade head
uv run --env-file agent_platform/.env alembic -c agent_platform/alembic.ini current
uv run --env-file agent_platform/.env alembic -c agent_platform/alembic.ini check
```

`upgrade head` 为新库创建完整平台与财务表；`check` 对比实际数据库和当前模型定义，检测未迁移变更。金额字段在 PostgreSQL 使用 `NUMERIC(30,12)`，在 SQLite 使用精确十进制字符串，避免二进制浮点误差。

生成后续迁移前，先保证目标数据库在当前 head，再用 `revision --autogenerate` 生成并人工审查。生产执行迁移前备份；迁移失败应停止服务升级。初版支持 `downgrade base` 用于空测试库验证，这会删除全部平台表，不能用于有业务数据的常规回滚。

一键启动识别到旧 `create_all` 数据库时，会比较全部模型结构并额外检查每张表的主键，完全符合初版结构后才建立 `0001` 迁移基线；缺表、结构或主键不匹配会停止，不会补表掩盖差异。已有版本记录的数据库正常执行升级。独立 `make migrate` 不执行旧库自动识别；手动处理旧库时不能未经结构核对就 `stamp head`。应用保留 `create_schema()` 作为开发便利入口，当前尚未把数据库迁移角色和运行角色细分为不同凭据。

## 离线和容器验证

普通离线测试：

```bash
uv run python -m unittest agent_platform.tests.test_runtime -v
```

测试真实 LangGraph v3 和 OpenAI SDK 的 SSE 解析，但用 HTTP mock 替代供应商，不产生真实模型费用。

部署协议验证：

```bash
uv run python -m agent_platform.deploy.smoke_test
```

这个脚本创建唯一名称的临时 Compose 项目，启动真实 PostgreSQL、Redis、固定版本 LiteLLM 以及一个独立的本地供应商 fixture。它使用临时生成的测试凭据，验证实际镜像版本、受限 virtual key、流式 token 用量、工具分片、LangGraph 工具闭环、HTTP 500 单次尝试、PostgreSQL 迁移升降级，并复现上游缺少 usage 时 LiteLLM 自动估算的边界。随后从当前代码重建应用镜像，独立启动初始化服务、API 和 Worker，完成真实 HTTP 操作与财务结算。无论成功还是失败都会删除仅属于该临时项目的容器和卷，已有部署不受影响。fixture 只存在于该测试流程，不是平台的模型降级路径。

2026-09-22 验证结果：容器中的 LiteLLM 包版本为 `1.83.0`；真实网关的文本与工具流通过；模型白名单和管理接口拒绝通过；供应商 500 仅收到一次请求；PostgreSQL 和 SQLite 均完成初版迁移升级、结构检查、降级及再次升级；应用 Docker 镜像从仓库根依赖成功构建，React 生产构建通过。

最终独立容器验收通过以下完整路径：管理员登录 → 创建模型 → 配置并发布 Agent → 验证初始余额为零 → 管理员入账 10 USD → 创建会话和运行 → 独立 Worker 经真实 LiteLLM 完成计算器工具的两轮模型调用 → SSE 输出及游标重放 → 用量、账本、会话历史和总览核对。两次调用分别为输入 12、输出 4 token，按输入 1 USD/百万、输出 2 USD/百万计价，共扣除 `0.000040 USD`，最终余额 `9.999960 USD`、冻结金额为零。两条调用和消费流水由真实 Worker/计费模块产生，没有预先插入统计记录；API 的内嵌 Worker 关闭，API 和 Worker 均不持有 LiteLLM master key。

真实供应商和具体模型仍需单独验证：usage 支持、输出上限语义、工具能力、取消/断流的计费证据、网关 RPM/TPM 的实际限制。未完成这些验证前，不应把本地协议测试等同于正式计费上线验收。

## 当前运维边界

Compose 面向自托管开发与集成验证。对外部署需要在同源入口终止 HTTPS，并把 `PLATFORM_SECURE_COOKIES=true`；默认 localhost HTTP 配置用于本地运行。当前尚未交付自动证书、备份调度、供应商账单自动对账、完整密钥自动轮换、SSO、PostgreSQL RLS 或性能容量承诺。

应备份平台 PostgreSQL、LiteLLM PostgreSQL 和部署密钥配置。Redis 持久化不能替代财务数据库备份。恢复后先核对钱包、账本、冻结预占和待核实调用，再允许新模型请求；Worker 中断或超时不应自动把冻结费用释放成可用余额。
