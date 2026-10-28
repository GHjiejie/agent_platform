# 架构调整与验收契约

更新：2026-10-07。本文承接架构审查的八项建议，记录已完成的代码调整、验证证据及部署边界。继续采用模块化单体和独立 API、Worker、gateway-sync、maintenance 进程。

## 本次要求

| 项目 | 已实现的行为 | 实现与验证证据 |
| --- | --- | --- |
| 支持授权失效 | 每次读取复核审批人账号、当前 owner Membership 与版本；撤权、交接、停用后不能读正文或元数据 | [support.py](../modules/support.py)、[授权生命周期测试](../tests/test_support_access.py)、[PG 锁顺序与隔离测试](../tests/test_postgres_isolation.py) |
| 超时与租约 | Run 固定排队期限；领取时关闭过期任务；持久执行 deadline 统一约束执行、续租和恢复；SSE 独立续租与鉴权，慢发送也能断流 | [0008 迁移](../migrations/versions/0008_execution_deadlines.py)、[Worker](../apps/worker/main.py)、[生命周期测试](../tests/test_execution_lifecycle.py) |
| 网关自动收敛 | 模型授权/启停、部署和套餐变化在同一事务生成期望配置与 Outbox；逐路由检查已生效配置；已发送且结果未知的操作先核查 | [gateway_control.py](../modules/gateway_control.py)、[收敛状态机测试](../tests/test_gateway_convergence.py) |
| 成本与证据 | 客户预占和供应商成本独立准入；调用、attempt、网关关联 ID 可追溯；证据追加保存；后台补证和 Worker 结算保持幂等 | [costs.py](../modules/costs.py)、[metering.py](../modules/metering.py)、[财务与补证测试](../tests/test_metering_costs.py)、[SDK 关联测试](../tests/test_gateway_correlation.py) |
| 最小权限 | 四个运行进程采用独立数据库身份；限制文件卷；账本和财务证据禁止直接 UPDATE/DELETE；LiteLLM 仅挂载独立 bootstrap 目录 | [权限矩阵](../deploy/database_permissions.py)、[0010 迁移](../migrations/versions/0010_service_permissions.py)、[部署权限测试](../tests/test_deploy_permissions.py)、[真实 PG 测试](../tests/test_postgres_isolation.py) |
| 扩容热路径 | 累计预算使用同事务投影并可从账本重建；提交后通知唤醒 SSE，数据库支持断线回放及兜底；提供业务诊断 | [events.py](../infrastructure/events.py)、[业务诊断](../modules/operational_metrics.py)、[通知测试](../tests/test_event_notifications.py)、[隔离负载记录](notification-validation.md) |
| 持久步骤契约 | 定义版本化工具、步骤、执行意图、fence、证据和 journal 接口，明确可重算、复用结果和核查边界 | [contracts.py](../modules/runtime/contracts.py)、[工具契约测试](../tests/test_tool_contracts.py) |
| 契约与发布门槛 | 关键响应有显式模型和生成 TS 类型；拒绝契约漂移；历史测试改用当前多租户 fixtures；发布门槛禁止跳过 PG、Redis 和角色 bootstrap 验证 | [响应模型](../apps/api/responses.py)、[生成器](../scripts/api_contract.py)、[验收入口](../scripts/acceptance.py)、[CI](../../.github/workflows/agent-platform-acceptance.yml)、[发布规则](multi-tenant-operations.md#本轮架构调整的上线顺序) |

## 实施与验证记录

八项代码调整已完成。最终 `scripts.acceptance` 的 `all` 模式在一次性 PostgreSQL 17、Redis 和新建数据库角色环境中执行 **331 项测试，90.042 秒，失败 0、错误 0、跳过 0**。发布门槛、PostgreSQL、Redis、实际角色 bootstrap 四项均通过，安全元数据保存于 [architecture-acceptance.json](validation/architecture-acceptance.json)。验收环境与现有平台数据库、业务数据隔离，完成后已清理。

真实 PostgreSQL 验证包含 FORCE RLS、无租户/错误租户上下文、连接池上下文复位、复合外键、并发钱包准入、撤权、支持授权锁顺序，以及四个真实服务角色的允许和拒绝权限。财务测试覆盖重复/冲突证据、晚到供应商成本、长关联 ID、未知消费保留、核销，以及 Redis 并发名额在晚到结算后的回收。

API 契约漂移检查、目标后端文件的 Ruff 检查和格式检查、依赖锁校验、前端 TypeScript 与 Vite 构建均通过。Compose 配置及 smoke 请求路径/字段完成静态校验；未执行完整 LiteLLM 容器与付费供应商 smoke。现有部署没有执行迁移或切库，生产恢复、真实网关兼容性及持续容量仍需部署阶段验证。

通知负载证据仅来自 8 个连接、4 秒、本地 SQLite 的隔离实验：空闲场景事件回放查询减少 86.99%；每秒增量场景使用隔离 Redis，回放查询减少 71.09%，新旧方案均收到 24 帧。详见[负载记录与限制](notification-validation.md)。这些结果不代表生产 PostgreSQL 容量。

## 财务恢复与权限边界

SaaS 调用必须具备平台供应商成本预算及部署成本价格快照，不能以客户零售价绕过供应商成本上限。未知消费继续占用成本额度。晚到证据可修正供应商成本投影，不会再次向客户扣费；证据冲突追加保存并阻止自动重复结算。超出预占的供应商费用会冻结后续准入。

成本预算解除冻结须通过授权、带原因及版本检查的复核入口；存在 reserved/unresolved 成本占用或已花费超过限额时拒绝复核。累计客户预算投影可通过授权审计入口从不可变收费分录重建，不修改钱包、账本或原始证据。

API、Worker、gateway-sync、maintenance 的对象权限由冻结矩阵授予；gateway-sync 只读取控制与补证所需信息。迁移角色与四个运行身份分离。导出卷在 API 中只读，维护进程负责导出回收及独立删除墓碑；Worker 和 gateway-sync 不挂载导出卷。本地 LiteLLM bootstrap 文件与服务控制秘密目录分开，旧 Key 兼容复制且不自动轮换。

## 必须保留的约束

- 客户钱包预占与账本保持精确小数和事务一致性；余额不能静默透支。
- 执行权丢失阻止旧 Worker 写结果，但不能阻止已有消费的晚到证据结算。
- 发送结果未知的模型或工具请求不盲目重放；未知消费不按 TTL 自动释放。
- Runtime、Proxy 和 SDK 默认仍是一笔准入一次外部尝试。
- Redis 通知不是可靠事件存储；断线回放以平台数据库为准。
- 部署迁移显式执行；本地 SQLite 不能充当生产 PostgreSQL/RLS 验收。

## 未来外部工具与持久步骤边界

当前已实现严格的 v1 数据契约与验证，尚未启用持久图 checkpoint、journal 存储或外部副作用工具。`calculator` 为纯函数，可重新计算；`current_time` 属于观察工具，恢复时要求复用已记录结果。Run 读取器支持快照版本 1、2，未知版本在执行前拒绝。

步骤身份包含 tenant/run/step/tool-call；`ExecutionIntent.execution_id` 标识执行意图，并冻结输入/请求摘要、deadline、fence 和幂等信息。模型计量另外持有独立 attempt_id。步骤状态为 prepared、sent、confirmed、rejected、unknown；发送结果不明进入 unknown。核查状态、复用已有结果与重算纯函数采用不同恢复动作，sent/unknown 外部步骤不能自动重发。

真正接入持久 journal 或外部工具时，还必须实现发送前原子保存意图、结果与步骤推进同事务、fence 比较以及外部状态核查，并完成工具取消、超时、授权和保留策略验收；本轮契约声明不构成这些存储能力已经上线的证据。

## 共享数据库发布规则

本发行版最新 head 为 `0010_service_permissions`，运行时要求 schema 与代码的唯一当前 head 一致。因此本轮升级需要维护窗口，不支持跨 head 混合滚动发布。先准备独立角色并停止旧写入进程，再执行迁移和权限切换，配置供应商成本政策，最后启动同版本四个服务；既有数据库不会被 bootstrap 脚本自动改造。完整顺序见[运维手册的上线顺序](multi-tenant-operations.md#本轮架构调整的上线顺序)。

未来跨 head 滚动发布采用 expand → 兼容代码 → 回填 → contract，并先引入明确 reader/schema 兼容范围验证。移除字段和收紧约束前确认旧/新四进程、在途 Run 快照、Outbox 和补证均兼容。迁移不能隐式触发外部网关写入。未知契约版本明确拒绝。灾备恢复仍先重放独立删除墓碑、核查未知消费与网关凭据，然后开放业务。
