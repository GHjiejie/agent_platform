# 阶段 D 工程验收记录

验收日期：2026-10-30。范围为设计文档第 11.4 节的 D1、D2，包括外部 Agent 注册发现、混合团队执行、跨 Worker 成员租约、费用和故障恢复。C5 真实模型效果评估独立保留，不以确定性测试替代。

## 要求与证据

| 要求 | 实现位置 | 验证证据 |
| --- | --- | --- |
| D1：独立成员领取，根任务等待不耗尽成员执行槽位 | `modules/collaboration/task_leases.py`、Worker 的独立成员任务池 | `test_distributed_collaboration.py` 的不同 Worker 执行、竞争领取、双 Worker 并行、满根任务槽及退出排空测试 |
| D1：根与成员 fence、取消和截止时间共同约束执行权 | `TaskController`、`DurableJournal.guard`、任务租约表 | 同文件的根 fence、取消、成员失联接管、原截止时间、接管上限测试；`test_remote_agent_execution.py` 另覆盖真实远程成员进程退出 |
| D1：共享预算与汇总预留 | `runtime/invocation.py`、`billing/service.py` | 跨 Worker 最后预算争用；PostgreSQL 并发钱包准入；远程报价不得消耗汇总预留 |
| D2.1：目录版本不可变、凭据加密不回显、管理权限和幂等注册 | `remote_agents/registry.py`、`0017_remote_agent_registry` | `test_remote_agent_registry.py` 的注册幂等、版本固定、未授权成员、跨租户引用、响应无密钥检查 |
| D2.1：部署服务权限、RLS 与关闭清理 | 远程目录/调用表、权限矩阵、`operations.py` | 实际 PostgreSQL 非 owner 角色检查 API 密文只写、Worker 同租户读、跨租户隔离及复合外键；关闭清理删除描述、密文和端点 |
| D2.2：健康 TTL、启停和凭据撤销影响发现与发送 | `RemoteHealthQueue`、`RemoteRegistry.eligible`、`RemoteInvocation.dispatch` | 健康过期、撤销后过期探测不能恢复健康、TLS 后撤权阻止 POST、轮换凭据并发布新版后旧运行仍用旧冻结版本 |
| D2.2：传输限制 | `remote_agents/transport.py` | 精确 HTTPS origin、私网/混合 DNS 拒绝、固定连接 IP、SNI/证书域名、禁重定向、大小限制、严格 JSON、受控路径 |
| D2.3：远程任务与本地任务共享根预算及调用数 | Scheduler、`RemoteInvocation`、`0019_remote_agent_calls` | 远程→本地→汇总混合团队、根费用与调用次数上限、未发送释放预占、终止意图不能晚到新增预占 |
| D2.3：固定报价独立费用证据，无虚假 Token | `reserve_fixed`、`settle_fixed`、`0018_fixed_remote_charges` | `test_remote_agent_billing.py` 的金额精度、重复回执、冲突证据、钱包/平台成本原子回滚、人工确认与核销 |
| D2.3：结果与财务投影可核对 | 任务费用、`remote_agents/projections.py`、用量/统计/导出 | SQLite 与 PostgreSQL 的运行、任务、用量、统计、CSV 金额一致；PostgreSQL 维护角色生成导出、API 角色下载；本人/跨租户权限与敏感字段排除 |
| D2.4：未知提交不重发，不切端点 | 稳定请求标识、加密意图、持久发送标记、只读查询 | 模拟未知响应及真实 HTTPS 收到 POST 后断连，均只按原请求标识查询；`not_found` 不释放预占、不重投 |
| D2.4：进程退出可受控恢复 | 根恢复、独立成员租约与远程日志复用 | 6 项真实子进程 `os._exit` 用例：发送前、发送后、完整输出后、输出带费用冲突、成员发送后、成员输出带费用冲突；确认原请求标识、幂等键、原截止时间不变 |
| D2.4：输出与费用独立，取消不恢复成果 | `RemoteInvocation`、`RemoteReconciler` | 只有费用不能解锁下游、无费用的完整输出可成功、错误报价保留预占；后台补证不写成果；旧补证租约不能发送取消或覆盖新持有者 |
| D2.4：查询与取消有界 | 执行查询计数、后台补证租约/计数、取消标记 | 执行查询上限及原截止时间；后台最多 12 次、30 秒间隔；取消最多一次；超过上限保留预占待人工处置 |
| D2.5：真实 HTTPS 与跨 Worker 联合执行 | 临时 HTTPS 服务、生产传输、实际 PostgreSQL 服务角色 | 4 项真实 HTTPS 测试覆盖健康发现和混合链路、未知提交及取消、关闭功能后的迟到费用、错误报价；PostgreSQL 独立成员 Worker 执行远程→本地团队 |
| D2.5：部署、回滚与运维 | Compose、Kubernetes、Helm、操作手册 | 配置透传和默认关闭测试、真实角色引导及冻结迁移权限检查；文档规定快照 6 排空、补证、密钥和访问权限退场顺序 |

测试位置统一为 `agent_platform/tests/`。真实 HTTPS 夹具在测试内把 DNS 指向临时回环服务，并信任临时证书；TLS、SNI、请求字节和响应断连经过生产传输，生产地址限制与证书校验保持不变。模型回复为确定性夹具，没有模型消费。

## 完整门禁

最终 `scripts.acceptance --mode all` 运行 **548 项测试，0 失败、0 错误、0 跳过**，耗时 227.177 秒；[机器可读报告](stage-d-acceptance.json) 中 PostgreSQL、实际角色引导、Redis 和后端工程门禁均通过。远程执行专项共 28 项，包含 6 项真实进程退出与 4 项真实 HTTPS 测试；Ruff、API 契约同步和差异格式检查通过。该脚本要求 PostgreSQL 17、Redis、Docker 和 Helm，任一跳过都会判为未通过。验收创建临时容器、数据库 schema 和非 owner 服务身份，结束后只清理这些测试资源，不访问部署数据库。

该结果证明 D1、D2 的工程实现和确定性验收完成。`release_gate_passed` 是后端验收脚本的结果字段，不包含 C5 效果评估或生产部署审批。

## 上线依赖

C5 的真实单 Agent / 固定团队 / 动态团队质量、成本和延迟对比尚未执行。当前环境没有 `COLLABORATION_EVAL_GATEWAY_URL`、`COLLABORATION_EVAL_MODEL`、`COLLABORATION_EVAL_API_KEY`、`COLLABORATION_EVAL_INPUT_PRICE`、`COLLABORATION_EVAL_OUTPUT_PRICE`、`COLLABORATION_EVAL_MAX_COST`，工作区也没有可供该评估使用的本地环境文件。需要专用测试网关、模型、凭据来源、报价及总费用上限后，使用操作手册中的评估命令完成剩余门禁。

所有高级功能继续默认关闭；工程测试通过不代表模型回答质量提升、生产部署完成或整个 A–D 目标已完成。
