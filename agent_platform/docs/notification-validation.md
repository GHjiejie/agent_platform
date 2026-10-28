# SSE 提交通知与隔离验证

本次验证使用迁移到当前 head 的临时 SQLite、真实租户/成员/会话授权，以及 API 的 `stream_response`。Redis 场景使用显式标记为隔离、关闭持久化的本地 Docker Redis。没有连接部署数据库，没有发出模型调用。

## 行为

- `append_event` 在业务事务内登记 tenant/run/sequence；提交、释放连接后才唤醒。回滚不发送通知，同一事务内同 run 合并通知。
- Redis 只携带唤醒标识。正文、序号和终态始终从数据库游标回放；订阅先于读取，关闭 read/wait 窗口；通知丢失或 Redis 不可用时每 2 秒检查数据库。
- Redis I/O 在独立后台线程执行，连接/请求超时 100ms，失败退避最长 5 秒。待通知 run 上限 4096；本进程 callback 与待发布通知均合并，自己发布的 Redis 回声不会触发重复回放。
- SSE 的 session、成员版本和 tenant 状态每 250ms 独立检查，身份查询超时 250ms 时关闭流。检查与发送独立，慢客户端也不能阻止撤权；租约每 10 秒续签。正常调度下授权失效会在约 500ms 的检查窗口内结束流；事件循环完全阻塞时不作墙钟硬实时保证。
- 诊断只读聚合 queue、unresolved、Outbox、metering、供应商冻结金额。调用方必须提供同连接授权 callback；金额保持 Decimal 精度，不返回正文或凭据。PostgreSQL advisory waiter 是当前 DB 角色可见会话的瞬时数量；没有锁等待历史分位数。Metering 的年龄以现有 `updated_at` 表示当前状态年龄。

## SQL 调用量

8 个连接，每个场景观测 4 秒；旧方案使用原 250ms 循环，新方案使用当前 API。保留相同授权检查频率。

| 隔离场景 | 旧 SELECT | 新 SELECT | SELECT 减少 | 旧事件回放 | 新事件回放 | 事件回放减少 | 发送帧旧/新 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 空闲，无 Redis | 1476 | 672 | 54.47% | 123 | 16 | 86.99% | 0 / 0 |
| 每秒 delta，隔离 Redis | 1542 | 720 | 53.31% | 128 | 37 | 71.09% | 24 / 24 |

原始数据：[空闲](validation/sse-idle.json)、[delta](validation/sse-deltas.json)。时序调度会影响短窗口内查询次数；此表只证明本机隔离比较的调用量变化，不代表生产吞吐、PostgreSQL 锁竞争或网络容量。

可复现入口：

```sh
python -m agent_platform.scripts.notification_probe --clients 8 --seconds 4
PLATFORM_ACCEPTANCE_REDIS_ISOLATED=1 PLATFORM_ACCEPTANCE_REDIS_URL=redis://127.0.0.1:<isolated-port>/0 \
  python -m agent_platform.scripts.notification_probe --clients 8 --seconds 4 --redis --delta-interval 1
```

通知回归覆盖提交/回滚、合并与 read/wait 竞争、Redis 故障退避与真实 pub/sub 重连、丢通知后的持久回放、Last-Event-ID 分页、慢客户端撤权、成员权限变化、租户停用和诊断金额/授权。原执行生命周期用例继续验证租约失效、阻塞续签与终态多页回放。
