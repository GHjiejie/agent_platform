# 多租户部署、维护与离线切库手册

更新日期：2026-10-07。配套设计：[multi-tenant-iteration-design.md](multi-tenant-iteration-design.md)。

当前本机已采用 [OrbStack Kubernetes 部署](kubernetes-local.md)。本文的 Compose 命令用于独立 SaaS 环境；操作旧 supervisor 请使用 `make local-stop/local-status`，`make stop/status` 现用于 Kubernetes。

## 1. 使用范围与交付状态

本文描述本仓库多租户实现的部署和运维步骤。命令供运维在获准的维护窗口执行，本轮没有执行现有部署 PostgreSQL 切库或真实供应商调用；一次性数据库的隔离、角色与故障回归见[架构调整记录](architecture-adjustments.md)。生产恢复与容量仍需另行验收。设计中的 **I5 灰度与正式验收仍待执行**；SQLite 本地启动成功不能作为 SaaS 上线证据。

本机当前使用的 `k3` 模型配置继续保留。部署、迁移及恢复均不以更换模型为前提；模型上游、部署路由、租户授权和 Agent 模板分别管理。禁止在搬迁过程中用新默认模型重写历史 `run.spec`、价格快照或调用记录。

适用边界：单个 SQLite 平台库，在明确停写后，一次性搬入同版本、已完成 Alembic 结构迁移的空 PostgreSQL 平台库。不支持在线双写、增量同步、覆盖已初始化的目标库、跨版本合并或 PostgreSQL 反向同步 SQLite。

脚本动态读取当前发行版 Alembic 的唯一 head，不在操作命令中固定 revision。源和目标都必须与执行脚本的代码版本完全一致；多 head、少表、多表和字段集合不一致均停止操作。源库升级与跨引擎搬迁是两个独立步骤。

## 2. 进程与权限分工

| 组件 | 启动入口 | 数据库身份 | 需要的秘密 | 职责 |
| --- | --- | --- | --- | --- |
| 结构迁移 / 显式初始化 | `alembic ... upgrade head` / `python -m agent_platform init` | `platform_migrator`，表所有者，但无 SUPERUSER / BYPASSRLS | 独立迁移连接凭据；初始化时的管理员密码 | 受控 DDL、首次身份引导 |
| API | `python -m agent_platform serve` | `platform_api` | 运行连接凭据、持久加密 Key | 鉴权、租户上下文、业务准入、控制面操作入队 |
| Worker | `python -m agent_platform worker` | `platform_worker` | 运行连接凭据、持久加密 Key | 按租户领取任务、授权复核、推理、计费收尾 |
| Gateway Sync | `python -m agent_platform gateway-sync` | `platform_gateway_sync` | 运行连接凭据、加密 Key、网关控制 Key | 领取 Outbox、远端开通、轮换、撤销和核查 |
| Maintenance | `python -m agent_platform maintenance` | `platform_maintenance` | 运行连接凭据、加密 Key | 导出、过期文件回收、租户关闭及墓碑处理 |
| LiteLLM | Compose `litellm` | 独立 LiteLLM 库身份 | 上游 Key、LiteLLM master / salt、管理 UI 密码 | 上游调用和网关资源管理 |

API、Worker、常驻 Maintenance 不接收网关 master、上游根 Key、数据库迁移密码或初始化管理员密码。只有独立 Gateway Sync 进程接收网关控制 Key；LiteLLM 服务本身另持有其 master 和上游秘密。临时 legacy 撤销核实命令属于例外的受控运维作业，见第 7 节。

四个独立运行角色必须为非表所有者，具有 `NOSUPERUSER NOBYPASSRLS`；禁止通过 `SET ROLE` 提升为迁移角色。内容、运行、财务及租户网关表启用 FORCE RLS。全局身份、Membership、平台角色、审计/支持授权等控制元数据按应用权限访问，不能依靠它们的全局可枚举性推导出正文读取权。

每个请求/后台任务显式绑定一个 `tenant_id`，每次数据库事务使用事务级 `app.tenant_id`。后台枚举租户后逐租户处理，不持有跨租户正文查询特权。平台管理员也必须针对明确的目标租户操作。进程启动仅检查 schema；API/Worker 不建表、不自动引导账号。

## 3. 配置、密钥与存储

### 3.1 配置管理

以 `.env.example` 为字段清单，真实值只放在忽略的 `.env`、权限为 `0600` 的运维配置或秘密管理系统中。禁止提交数据库文件、报告、导出物和秘密。不要将 `docker compose config` 的完整展开结果、环境变量列表、数据库连接串或异常原文贴入工单。

- `PLATFORM_MODE=saas`：必须 PostgreSQL、`PLATFORM_REDIS_URL`、`PLATFORM_EMBEDDED_WORKER=false`、`PLATFORM_SECURE_COOKIES=true`。
- `PLATFORM_API_DATABASE_URL`、`PLATFORM_WORKER_DATABASE_URL`、`PLATFORM_GATEWAY_SYNC_DATABASE_URL`、`PLATFORM_MAINTENANCE_DATABASE_URL` / `PLATFORM_MIGRATION_DATABASE_URL`：每个服务使用独立身份及独立密码；如果密码含 URL 保留字符，先正确百分号编码。不要把未编码密码拼到 URL。
- `PLATFORM_SECRET_ENCRYPTION_KEY`：持久的 Fernet Key。所有需要读取租户凭据的组件使用同一代 Key；备份 Key 的引用与恢复权限，禁止在迁移时重新生成。
- `PLATFORM_OPERATOR_EMAILS`：仅列出明确授权的平台操作员。旧租户 `admin` 不会全部自动变为平台管理员。引导需要显式 `init`，不要为赋权而在运行进程反复执行初始化。
- `PLATFORM_WORKER_CONCURRENCY`：进程本地并行上限；租户套餐与持久账务预占仍分别实施全局租户上限。
- `PLATFORM_EXPORT_DIRECTORY`：私有导出目录。不得由 Web 服务器静态公开，下载仍经过当前权限与 Membership 版本校验。
- `PLATFORM_TOMBSTONE_PATH`：独立持久卷中的租户删除墓碑。必须与 PostgreSQL 备份分离，数据库恢复不能回滚墓碑事实。

本地 supervisor 首次缺少加密 Key 时，会生成 Key 并以 `0600` 持久化到忽略的应用 `.env`，不打印值。已有 Key 格式错误会停止启动，不会悄悄替换。SaaS 使用外部秘密管理流程配置 Key，不依赖本地临时生成。

### 3.2 存储与备份集合

| 数据 | 备份/保留策略 | 恢复注意事项 |
| --- | --- | --- |
| 平台 PostgreSQL | 数据、schema、迁移 head、财务不可变记录 | 与代码版本、独立角色授权一起恢复 |
| LiteLLM 数据库 | 独立备份及远端资源清单 | 不能假设与平台库的时间点原子一致 |
| 加密 Key / 秘密引用 | 秘密管理系统独立版本化 | 丢失旧 Key 会使旧密文不可解密 |
| 导出文件 | 私有卷、大小与 SHA-256 清单 | `export_artifacts` 元数据不能代替实际文件 |
| 删除墓碑 | 独立于数据库的不可回滚保留 | 恢复后先重放，再开放业务 |
| Redis | 独立维护其用途/持久化策略 | 数据库仍保留财务预占；清空 Redis 不能释放 unknown |
| 旧 SQLite | 停写快照、逻辑摘要、只读归档 | PostgreSQL 接受新写入后不能直接切回旧库 |

不要执行 `docker compose down -v` 作为常规重启手段。Compose 的墓碑卷是外部卷，但数据库和导出卷仍可能被此命令删除。

## 4. 新建 SaaS 环境

本节只用于全新环境，已有 SQLite 数据请走第 5 节。下列命令在仓库根目录执行；配置文件、代理域名、存储所有权由部署负责人先准备好。

1. 记录发行版提交号，安装锁定依赖，准备 PostgreSQL / Redis / LiteLLM 和 TLS 反向代理。SaaS 的安全 Cookie 要求浏览器通过 HTTPS 访问；内网 HTTP 健康检查不等于可通过 HTTP 登录。
2. 配置独立的 `POSTGRES_PASSWORD`、四个 `PLATFORM_<SERVICE>_DB_PASSWORD`、`PLATFORM_MIGRATION_DB_PASSWORD`、`LITELLM_DB_PASSWORD`、网关秘密、初始密码及持久加密 Key。不要复用这些秘密。
3. 显式配置初始操作员邮箱。新租户余额为零，运营应通过有审计及幂等键的充值入口授予余额，租户管理员没有自充值能力。
4. 创建独立外部墓碑卷，名称与配置一致：

   ```sh
   docker volume create agent-platform-tenant-tombstones
   docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml build
   docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml up -d postgres redis litellm
   docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml run --rm init
   ```

5. 核实初始化退出成功。`deploy/bootstrap-roles.sh` 只在全新 PostgreSQL 数据卷初始化时运行；既有数据卷不会自动改变旧角色、所有者或密码。已有部署必须由数据库负责人制定显式角色/所有权迁移，不能为通过启动检查改用超级用户。
6. 确认导出卷与墓碑卷对容器 UID `10001` 可读写，私有文件不被其他服务公开。挂载已存在的外部卷时尤其需要检查其所有权。
7. 启动运行进程：

   ```sh
   docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml up -d --no-deps api worker gateway-sync maintenance
   ```

8. 内部验证 `/api/v1/ready`。其检查 schema、运行角色、Redis（配置时）及 Worker / Maintenance / SaaS Gateway Sync 心跳。它是依赖存活准入；不证明网关 Outbox 无积压、模型可用、权限正确或财务对账通过。
9. 配置平台部署、租户模型授权和受限网关凭据。新租户仅有 Agent 模板并不代表已有模型调用权限。绑定未 ready、模型未授权、余额不足或超套餐都会拒绝新运行。
10. 完成 I5 验收与发布审批后，再经 TLS 代理开放外部租户。

## 5. SQLite → PostgreSQL 离线迁移

### 5.1 工具契约

入口：`python -m agent_platform.scripts.migrate_sqlite`。

| 参数 | 含义 |
| --- | --- |
| `--source PATH` | 必填，SQLite 源库或已停止的只读快照；工具用 `mode=ro` 和一致读事务访问 |
| `--backup PATH` | 独立、自包含 SQLite 备份；apply 必填，不得与 source 是同一文件/硬链接 |
| `--target-env NAME` | PostgreSQL 迁移连接串所在环境变量名，默认 `PLATFORM_MIGRATION_DATABASE_URL`；命令行不传秘密值 |
| `--key-env NAME` | Fernet Key 所在环境变量名，默认 `PLATFORM_SECRET_ENCRYPTION_KEY` |
| `--manifest PATH` | 外部状态核实清单，apply 必填，规范见 5.4 |
| `--apply` | 显式开启目标写入；不带此参数是只读 dry-run |
| `--source-stopped` | 运维确认已停止源写入、旧执行器及其自动重启；不替代实际停机 |
| `--batch-size N` | 单批插入行数，默认 500，允许 1–2000；仍为一个整体事务 |
| `--report PATH` | 独占新建 `0600` JSON 报告，已有同名文件会拒绝；不指定则仅输出安全 JSON |

工具不会读取 `.env`。由受控 runner 注入迁移连接凭据和加密 Key，或使用 `uv run --env-file /secure/migration.env --no-sync ...`。这个配置只包含迁移所需字段，不应包含供应商或网关控制 Key。

**强制前提：** 源无 queued / running / cancelling 或其他非终态 Run，无有效服务心跳；源外键检查通过；所有业务表与当前发行版一致；钱包预占等于有效预占金额合计，分录逐交易逐币种平衡，钱包余额与 `customer_wallet` 分录相等。工具不会通过丢行、修改金额或猜测使用量修复差异。

目标必须只执行过结构迁移，没有 `init`、租户/模型引导或业务流量。空库检测使用 PostgreSQL 主 heap 大小，因此 FORCE RLS 隐藏行不会被误判为空。检查比 `COUNT(*)=0` 严格：用过再 `DELETE` 的库也会拒绝。失败重跑时创建新空库并迁移结构，不用清表、关 RLS 或授予 BYPASSRLS 绕过检查。

### 5.2 冻结源端

1. 进入维护窗口，代理阻止新业务写入与新运行；冻结身份、Membership、角色、模型、价格、套餐和充值变更。记录冻结时间及发行版提交号。
2. 等待运行结束，或通过业务取消入口受控结束 queued / running 任务。保留取消事件与账务结果。不要直接把运行状态批量改为 succeeded/failed。
3. 关停旧 API、Worker、Gateway Sync、Maintenance、cron/恢复作业和本地 supervisor，关闭系统级自动重启。若由本地脚本管理，可使用：

   ```sh
   make -C agent_platform local-stop
   make -C agent_platform local-status
   ```

   同时确认其他部署方式启动的进程已停止。脚本只管理自身记录的进程，不会停止其他服务。
4. 停机后等待残留心跳到期；正常退出会删除心跳，非正常退出的心跳有效期通常为 30 秒。不要为通过校验而随意删除活跃心跳。
5. 记录所有外部网关资源和未知操作，封锁旧进程继续访问数据库/网关的能力。旧 SQLite Worker 不受新 PostgreSQL 的 fence 约束；必须通过停进程、停止自动重启、主机/网络隔离及凭据轮换或撤销完成隔离。
6. `reserved` / `unresolved` 不要求伪装成成功或清零后才能迁移。无活动 Run 的旧 reserved 将由目标 Worker 恢复为待核实；金额继续冻结，后续凭证对账。若远端调用是否完成未知，禁止重发相同模型请求。
7. 如源库尚未升级到本次发行版，先保留升级前快照，再用对应发行版的 Alembic 升级源库；根据身份/网关迁移要求显式完成初始化。源与迁移程序 schema 不一致时先停止切库，不能 `stamp head` 掩盖差异。
8. 完成源端变更后重新制作最终冻结备份。最终备份需与 source 所有表的逻辑摘要一致，包括即将不复制的登录会话、流租约和心跳表；备份之后不能继续修改源数据。

### 5.3 制作只读快照与准备目标

优先使用 SQLite backup API，避免遗漏 WAL。以下示例只在源已冻结后执行；目录应为 `0700`，存储有足够空间，文件必须不存在：

```sh
umask 077
mkdir -p agent_platform/.data/cutover
uv run --no-sync python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

source = Path("agent_platform/.data/platform.db").resolve()
directory = Path("agent_platform/.data/cutover")
os.chmod(directory, 0o700)
for name in ("source.db", "backup.db"):
    path = directory / name
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect("file:" + quote(str(source), safe="/") + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(path)) as target:
            origin.backup(target)
    path.chmod(0o400)
PY
```

这里生成 source 快照与独立 backup；另应把 backup 复制到独立故障域。不能只复制正被写入的 `.db` 主文件。工具拒绝携带 `backup.db-wal` 的备份，要求备份自包含。保留升级前、升级后两个阶段的备份，勿互相覆盖。

目标基础设施按第 4 节建立，但**跳过 `init` 引导**。只执行结构迁移：

```sh
docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml up -d postgres redis litellm
docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml run --rm --no-deps init alembic -c agent_platform/alembic.ini upgrade head
```

`init` 是 Compose 服务名称，此处覆盖其命令为 Alembic；不会执行 `python -m agent_platform init`。源平台密码哈希、角色、租户、模型及账务将直接搬迁，因此目标导入前不要创建管理员/租户。迁移角色不能是 PostgreSQL superuser 或 BYPASSRLS。

### 5.4 外部状态清单

在私有目录创建 `manifest.json`，仅在实际完成对应动作后将布尔值改为 `true`。不填写任何原始 API Key、数据库 URL、密码、对话正文或请求内容。`evidence_reference` 指向受控运维工单，详细资源清单留在该记录里。

```json
{
  "schema_version": 1,
  "source_stopped": false,
  "target_services_stopped": false,
  "old_executors_fenced": false,
  "gateway_inventory_reviewed": false,
  "gateway_sync_held": false,
  "artifacts_preserved": false,
  "tombstones_preserved": false,
  "same_encryption_key_provisioned": false,
  "backup_sha256": "待填备份文件SHA256",
  "encryption_key_sha256": "待填Fernet Key文本的SHA256",
  "reviewed_at": "待填带时区的ISO时间",
  "reviewed_by": "运维负责人标识",
  "evidence_reference": "变更工单编号"
}
```

清单核实内容：

- 网关实例/版本、租户绑定、external user/key ID、别名、凭据指纹/代际、授权路由、限额和实际状态。
- Outbox 的 action / phase / status；已发送但响应未知的开通、撤销、轮换必须与远端资源核对，不能仅根据本地 pending 重建资源。
- 旧进程掌握的凭据已被隔离，目标运行凭据已经安全配置。若外部凭据在窗口内轮换，相关数据库记录必须在最终快照之前完成更新或保持待核查；不得把已经撤销的密文标记成 ready。
- 平台网关密文与目标环境使用相同 Fernet Key。工具会尝试解密每条现存密文验证 Key，但不会输出或调用解密得到的秘密。
- 所有 `export_artifacts` 对应文件、相对路径及 SHA-256 已复制；无文件时也应明确核实“无”。容器 UID 可读取，文件继续私有。
- 独立墓碑文件保留完整，覆盖所有已发生删除；其恢复时间点不能早于数据库。网关数据库、外部资源和删除墓碑的时间点差异有处理记录。

用受控 runner 生成两个摘要和时间，可以避免把秘密放进命令历史。下面的程序只修改清单中的摘要/时间，不替运维勾选确认项；运行环境预先注入 Key：

```sh
uv run --env-file /secure/migration.env --no-sync python - <<'PY'
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

directory = Path("agent_platform/.data/cutover")
path = directory / "manifest.json"
manifest = json.loads(path.read_text())
with (directory / "backup.db").open("rb") as stream:
    manifest["backup_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
key = os.environ["PLATFORM_SECRET_ENCRYPTION_KEY"].strip()
manifest["encryption_key_sha256"] = hashlib.sha256(key.encode()).hexdigest()
manifest["reviewed_at"] = datetime.now(UTC).isoformat()
path.chmod(0o600)
path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
PY
```

`reviewed_at` 必须在执行 apply 前 24 小时内，清单字段必须完整。工具只能验证清单结构、摘要和确认项，不能远程证明源进程已停止或网关资源已核实，这些由变更负责人承担。

### 5.5 Dry-run 与正式导入

在能连接目标 PostgreSQL 私网的受控迁移主机执行。`/secure/migration.env` 仅包含迁移连接串和持久加密 Key，文件权限为 `0600`：

```sh
uv run --env-file /secure/migration.env --no-sync python -m agent_platform.scripts.migrate_sqlite \
  --source agent_platform/.data/cutover/source.db \
  --backup agent_platform/.data/cutover/backup.db \
  --report agent_platform/.data/cutover/dry-run.json
```

返回 `status=dry_run_ready` 表示源数据、备份和空目标的结构/财务前置检查通过；不表示已经搬迁，也不代替 manifest 和加密 Key 的 apply 校验。报告中的 `target` 是预计变换后的摘要。保存报告，逐表行数及精确金额必须经负责人确认。

正式执行：

```sh
uv run --env-file /secure/migration.env --no-sync python -m agent_platform.scripts.migrate_sqlite \
  --source agent_platform/.data/cutover/source.db \
  --backup agent_platform/.data/cutover/backup.db \
  --manifest agent_platform/.data/cutover/manifest.json \
  --apply --source-stopped \
  --report agent_platform/.data/cutover/applied.json
```

如果迁移主机不能直连 Compose 私网，可使用与服务相同的容器网络。先将私有快照目录准备为当前运维 UID 可读；运行临时容器时指定该 UID，避免为了容器读取而公开文件：

```sh
umask 077
docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD/agent_platform/.data/cutover:/cutover:ro" \
  init python -m agent_platform.scripts.migrate_sqlite \
  --source /cutover/source.db --backup /cutover/backup.db \
  --target-env PLATFORM_DATABASE_URL \
  > agent_platform/.data/cutover/container-dry-run.json
```

这是 dry-run。正式执行同样需要追加 `--manifest /cutover/manifest.json --apply --source-stopped`，输出到新的报告路径，并核对退出码。Compose `init` 服务在这种命令覆盖下只是注入迁移身份的临时 runner；不启动依赖、不运行初始化默认命令。

### 5.6 导入语义与报告解释

1. 工具对全部目标业务表获取独占锁，再次确认空库。锁等待最多 5 秒，发现并发运行进程应停下排查，不能无限等待或杀死未知数据库会话。
2. 按 SQLAlchemy metadata 外键依赖顺序导入；每张含租户字段的表再按租户分区，通过事务级 `set_config('app.tenant_id', ..., true)` 写入，FORCE RLS 保持启用。
3. ID、用户密码哈希、Membership、owner/平台角色、支持授权证据、Agent 发布版本、模型授权、immutable price version、`run.spec`、事件序号、幂等键、账务流水、预占及 unknown 状态原样保留。
4. `platform_auth_sessions`、`platform_stream_leases`、`platform_service_heartbeats` 不复制。用户重新登录，旧流连接失效，后台服务以新心跳加入。
5. 带 owner/lease/fence 的执行记录清除 owner、fence 加一。普通租约清空；`gateway_outbox.applying` 和 `export_jobs.running` 使用 `lease_until=0` 表示已到期，让恢复器能领取。它们的 status / phase 不重写为“成功”或“未发送”。
6. `reserved` / `unresolved` 保留原金额和状态。相关钱包设置 `blocked=true`，新加上的原因是 `migration_pending_financial_review`；本来已 blocked 的原因保留。冻结不阻止已发生消费的可信结算。处理全部待核实预占后，平台财务以理由和审计显式解除。
7. 每批最多加载 `--batch-size` 行到插入列表。摘要逐行计算，金额使用 `Decimal`，不走 SQLite 浮点 `SUM`；每表包含行数、顺序无关的 SHA-256 多重集合摘要及各金额字段精确合计。字段级字符串保持原文本；数值金额统一规范化后比较。
8. 每表写入后立即在同一事务中按租户回读，对比预计变换后的摘要和金额；差异或约束失败导致整个目标事务回滚。没有分段提交、自动重试或断点覆盖功能。
9. 工具只访问 SQLite / PostgreSQL，不联系 LiteLLM / 模型上游，不重发 Outbox，不复制导出文件或墓碑，不启动服务。manifest 是这些外部步骤的证据入口。
10. `status=applied` 且 `committed=true` 才表示完成。`status=commit_outcome_unknown` 表示 COMMIT 期间连接故障，结果需要核实；保持两端停写，不根据退出码断言已经回滚，也不自动重跑。由 DBA 对当前目标行数/摘要及提交事实核实，再决定恢复流程。

报告不包含连接地址、秘密、用户邮箱、对话正文或单条账务内容；它仍含业务规模与金额合计，应按内部运维材料保管。任何异常输出仅包含固定错误码或异常类型，底层 SQL/参数不打印。失败报告可用于定位阶段，不能用于绕过既有一致性门禁。

## 6. 切流和回滚

### 6.1 开放目标的顺序

1. 导入成功后继续保持源只读归档，旧进程和凭据隔离措施保留；不能新旧双写。
2. 目标仍处维护网络。核对报告、角色/权限、实际部署路由、文件与 Key、外部网关资源。确认目标配置仍指向现有可用模型，本机 `k3` 不因迁移被替换。
3. 挂载独立墓碑卷，先执行墓碑重放（见第 7 节）。旧数据库快照可能包含已经删除的租户，必须先重新关闭这些访问，不能先开放登录再补删除。
4. 审核 Outbox 中 pending / retry / reconciling / applying 的外部事实后，才允许 Gateway Sync 工作。控制面调用失败与未知响应分别处理；不要把全部未知操作改成 pending 强制重试。
5. 在内部维护窗口启动 Worker / Maintenance，让已终态 Run 关联的 reserved 收尾为待核实，导出/关闭任务接管到期租约。模型新运行入口仍关闭。有关未知预占须先逐笔取得证据，再进行财务确认或明确核销。
6. 受限 API 内部开放，原用户重新登录。完成权限矩阵、RLS、账务、文件、运行流程验收；仅起 API 而不启动后台服务时 `/ready` 为 degraded 是预期结果。
7. 完成 I5 记录、批准灰度租户清单后，逐步开放新业务写入和新运行。监控拒绝率、跨租户权限拒绝、Outbox/导出/关闭积压、未知账务和钱款差异。

不要在已经导入数据的目标重新执行无目的的 `init`；若需要显式引导新增平台操作员，另走受控初始化变更，记录新增授权和审计后再放行。

### 6.2 回滚决策

| 故障时点 | 处理 |
| --- | --- |
| Dry-run 失败 | 保持源备份不变，修复源数据/目标结构或运维条件后重新预检 |
| Apply 在提交前失败 | 工具尝试事务回滚；确认目标状态，准备新的空库后重跑。源不会被工具改写 |
| COMMIT 结果未知 | 双端维持停写，由 DBA 证明目标提交事实；不能盲目重复导入 |
| 目标已提交但未产生任何新业务/外部副作用 | 可以在确认目标运行进程完全停止、外部凭据状态与源一致后，按审批恢复冻结源。新登录会话仍应失效 |
| 目标产生新消息、充值、消费、对账、删除或网关副作用 | 保留目标为事实来源；回退兼容应用版本或前向修复。直接切回旧 SQLite 会漏账/复活权限，禁止这样处理 |
| 权限/RLS 回归 | 关闭受影响入口和新准入，修复权限，保留财务收尾；禁止临时关 RLS、给 runtime 迁移身份 |
| 密文不可解密 | 恢复正确的原 Key 或执行有版本证据的轮换流程；不能以重新生成 Key 当成恢复 |
| 模型/网关异常 | 暂停受影响租户新调用，保持未知资金冻结，核实绑定/代际；不要退回共享高权限 Key |

fence 仅防止连接同一数据库的新旧任务所有者相互覆盖，不能阻止仍连旧 SQLite 的旧 Worker，也不能撤回已经发送到模型供应商的请求。外部进程隔离和凭据失效必须有单独证据。结构 downgrade 也不等于应用回滚；遇到多租户新语义或财务证据时应保留新增表列，按相应 migration 的限制处理。

## 7. 常见维护操作

### 7.1 租户、成员和平台权限

- owner、tenant_admin、member、finance_viewer 是租户 Membership 角色；platform_admin、platform_finance、platform_support 是独立平台角色。
- 租户暂停需要理由和 `expected_version`，使用 `/api/v2/platform/tenants/{tenant_id}/suspend`；恢复用同路径下 `/resume`。冲突后重新获取状态，不覆盖他人的并发变更。
- owner 转移必须走 ownership-transfer，保留审计；不能直接删除最后一个有效 owner。紧急停用与恢复由专门的身份流程处理，不能修改旧 `users.role` 绕过 Membership。
- 多标签页的租户上下文在请求中明确给出；切换浏览器页面不意味着已经发出的 SSE/导出自动获得新租户权限。
- 正文默认本人访问。支持人员访问租户需要有效、限时、经授权的 support grant；正文还要求明确的正文权限，审计保留真实 staff 身份。

### 7.2 钱包、限额和未知消费

平台财务路径：`/api/v2/platform/tenants/{tenant_id}/billing/wallet`、`/billing/reservations`、`/billing/reservations/{call_id}/resolve`、`/billing/unblock`；充值走该租户 `/credits`。写操作必须使用权限、理由及接口要求的幂等键，不能直接改余额表。

`unresolved` 表示是否发生费用仍未证实，不代表失败且免费。仅凭超时、取消请求或 Worker 重启不能释放预占。对账要记录供应商/网关调用标识、可验证用量、冻结价格版本和确认人；无法证明实际用量时用明确核销流程，保留原始事实与补偿记录。

执行并发与费用预占分别计算：Worker 按 `running` / `cancelling` Run 限制执行数；账务调用准入只将 `reserved` 调用计入并发。调用结束并转为 `unresolved` 后，释放 SQL / Redis 执行名额，继续持有钱包和客户/供应商预算金额，并保留原限流窗口内的请求数与预估 token。历史待核实调用不会一直占满执行并发；余额或预算不足仍会拒绝新调用。

暂停租户、余额冻结、套餐减少不阻止已发生的可信消费结算。迁移冻结钱包需要先处理全部 reserved / unresolved，再由平台财务显式解除；无证据不得清空预占。套餐硬上限与租户可调 quota 分别约束，tenant_admin 无权增加平台价格、套餐或给自己充值。

### 7.3 网关开通、轮换与撤销

API 将开通/轮换/撤销作为持久 Outbox 入队，由 Gateway Sync 处理。观察绑定 desired/applied version、credential generation、operation phase 和状态；异步操作受理不等于远端已经成功。

未知 key 创建响应先核查 external ID / alias / fingerprint，不直接生成另一把 Key；撤销须远端读回确认。缺控制 Key、部署不一致或密文不可解密会保持不可运行状态。API/Worker 不应为排障临时挂载 master。

只有遗留共享 Key 撤销证据确认需要一次性特权作业：

```sh
uv run --env-file /secure/legacy-revocation.env --no-sync python -m agent_platform.apps.maintenance.main \
  --confirm-legacy-revocation TENANT_ID
```

该私有环境使用非 owner 运行数据库身份、持久加密 Key 和仅为此次核实注入的控制 Key；命令从远端读回检查已撤销事实。任务完成后销毁临时环境，常驻 Maintenance 不继承该 Key。不能把“已在后台点过删除”直接改成已确认。

### 7.4 导出与关闭任务

独立维护入口支持单批处理：

```sh
docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml run --rm --no-deps maintenance \
  python -m agent_platform.apps.maintenance.main --once
```

单批处理会推进实际维护任务，并非只读状态查询。诊断时先看 API 状态与受限指标。导出使用固定截止时间和分页游标，产物经过私有路径、SHA-256 和有效期约束；生成权限和下载权限都会复核当前用户/成员状态。

租户关闭为异步状态机，先阻止运行/撤销网关，再等待财务待核实项目清零与保留期结束，最后按阶段清理正文。`billing_unresolved`、`runs_pending`、`gateway_revocation_pending`、`legacy_gateway_revocation_required` 是阻塞原因，按其事实处理，不能强制把任务状态改成 deleted。

### 7.5 数据库恢复后重放墓碑

保持公开流量、Worker 和 Gateway Sync 停止；挂载最新独立墓碑后执行：

```sh
docker compose --env-file agent_platform/.env -f agent_platform/deploy/compose.yml run --rm --no-deps maintenance \
  python -m agent_platform.apps.maintenance.main --replay-tombstones
```

墓碑会重新关闭从旧备份恢复出的已删除租户，并推进必要清理。它不能补回备份之后的财务消费、网关远端变更或供应商状态。恢复负责人还必须核对这些新事实，再按第 6 节开放目标；不得为了恢复旧租户而回滚独立墓碑卷。

## 8. 发布验收与值班记录

以下项目是待执行的上线门禁，不是本次已完成测试的声明：

| 门禁 | 要保存的证据 |
| --- | --- |
| 实际 PostgreSQL runtime 角色的隔离矩阵 | A/B 租户读写、联合外键、RLS WITH CHECK、异常/连接池上下文复位 |
| 身份与正文权限 | 多 Membership、多标签页、owner 变更、全局停用、邀请重放、support grant 过期/撤销 |
| 长连接与导出 | SSE 中途撤权、重连、连接上限、下载权限变更、过期与路径隔离 |
| 网关控制面 | 固定 LiteLLM 版本、开通/轮换/撤销读回、未知响应核查、旧 Key 实际失效 |
| 运行与计费 | 多 Worker 租户并发、RPM/TPM、套餐缩减、取消与故障收尾、unknown 不重复收费 |
| 迁移与回滚 | 代表性脱敏数据、历史价格/未知预占保留、dry-run/apply 报告、COMMIT 不确定性处理 |
| 恢复与删除 | 最新墓碑重放、导出文件、旧备份恢复后租户不复活、账务与网关差异核对 |
| 容量与真实模型 | 计划中的持续负载、积压和延迟；真实上游兼容验收需单独明确费用预算 |

每次变更记录至少包括：负责人/审批、代码提交号、动态 schema head、维护窗口、源/目标角色类型、备份位置引用、密钥版本引用、外部状态工单、报告校验结果、切流时间、回滚界限、待核实财务项的负责人及后续截止时间。记录引用，不记录秘密值。

运行过程中至少关注：readiness 缺失服务、租户队列等待时间、最长未决网关操作、导出/关闭积压、reserved/unresolved 数量与金额、钱包/分录差异、授权拒绝和墓碑写入失败。心跳健康并不掩盖业务任务持续失败；这些指标需要结合运营界面和告警接入，不能只用进程存活作为验收结论。

## 本轮架构调整的上线顺序

现有数据库不会被新 bootstrap 隐式修改。本发行版运行时要求 Alembic 的唯一当前 head（0010），不能与迁移前二进制混合滚动发布；本轮需明确维护窗口。独立 Helm release 仅允许在 schema 相同且快照/事件兼容的情况下单独更新。

1. 备份平台库、LiteLLM 库、加密材料和独立删除记录；停止新 Run、控制修改与财务写入，排空或核实外部请求，保留 unknown。
2. 数据库管理员预先创建四个 `platform_<service>` 登录角色及不同密码，均无 owner、SUPERUSER、BYPASSRLS 或向 migrator 提权的角色成员关系。新数据卷使用 `deploy/bootstrap-roles.sh`；已有库由负责人审核该脚本的角色/所有权步骤，不能用重新初始化数据卷替代升级。
3. 以原 schema 所有者迁移到 0010；0010 对存在的标准服务角色应用冻结权限。自定义角色使用 `python -m agent_platform.deploy.database_permissions --role worker --schema public --database platform` 等生成 SQL，经数据库负责人应用；角色命名可由 SQL 审核时调整。迁移角色不能代替运行身份。
4. 更新 Compose 各服务 URL，或准备 `platform-api-database-secret`、`platform-worker-database-secret`、`platform-gateway-sync-database-secret`、`platform-maintenance-database-secret`，键均为 `PLATFORM_DATABASE_URL`。旧 `platform_runtime` 不再用于新发布；切换完成、确认没有旧连接后由负责人撤销旧角色权限和登录凭据，避免遗留广泛授权。
5. 平台财务设置 `/api/v2/platform/costs/budgets/platform/platform` 及每个部署的 `cost-prices`。冻结价格是 USD/百万 token 的供应商成本估算，独立于客户报价和网关 Key。缺预算或价格时 SaaS 拒绝新的模型发送。
6. 发布同版本 API/Worker/gateway-sync/maintenance，检查各自 readiness、授权、Outbox、补证与未知预占；验证后恢复准入。历史不可变账本/证据不能用 UPDATE 修补，投影故障走有原因及审计的重建接口。

累计客户预算重建：`POST /api/v2/platform/tenants/{tenant_id}/billing/budget-projection/rebuild`，body 为 `reason`；从不可变 charge 账本重新汇总，不改变钱包或交易。供应商成本核实：`POST /api/v2/platform/costs/budgets/{scope}/{subject_id}/review`，body 为 `reason` 与 `expected_version`；有未处置预占或已发生成本超预算时拒绝解封。只读 `receipts` 保留每个调用的追加证据及冲突，`diagnostics` 用于观察积压和冻结。

未来 schema 变更采用 expand → 兼容代码 → 回填 → contract。开始支持跨 head 的滚动发布前，先发布明确 reader/schema 范围的验证机制，并对旧/新四进程与在途快照、Outbox、补证作兼容验收；本版本的严格 head 校验不承诺该能力。0010 权限收紧和0009财务不可变记录没有自动广泛授权或破坏性降级。
