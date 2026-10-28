# 开发者可配置的 Agent 执行策略

Agent 的模型调用与执行预算由开发者在“我的 Agent → 编辑草稿”中配置。保存后发布，新运行读取新版本；已有运行保留其创建时的执行策略、输出预算和平台限额快照。当前统一默认单次输出为 128K（131072 tokens），默认上下文窗口为 1M（1048576 tokens）；默认值只用于新建，不会在启动时覆盖开发者编辑。

## 配置项

| 编辑器字段 | API 字段 | 含义 |
| --- | --- | --- |
| 单次输出 Token 上限 | `max_tokens` | 每次模型调用的输出预算；受实际选中模型的已登记输出上限约束 |
| 最大执行步数 | `max_steps` | 本次运行的模型调用总次数，包含工具后调用和截断恢复调用 |
| 思考强度 | `execution_policy.reasoning_effort` | 留空继承网关，显式值在本次请求覆盖模型默认值 |
| 运行时限（秒） | `execution_policy.timeout_seconds` | 执行阶段的总时限，不包含排队；留空继承平台 |
| 累计 Token 预算 | `execution_policy.max_total_tokens` | 所有调用的输入和输出总预算；留空不额外设置此项限制 |
| 运行费用上限 | `execution_policy.max_cost` | 精确十进制字符串，单位 USD；留空继承平台费用上限 |
| 输出上限恢复次数 | `execution_policy.max_output_recoveries` | 0 表示截断后停止；正数允许有限次重新请求完整结果 |

平台提供 `GET /api/v2/tenants/{tenant_id}/agents/execution-options` 返回实际的运行时限、费用、调用次数和输出字段上限，编辑器据此展示限制。`PLATFORM_RUN_TIMEOUT` 默认 `0`，表示不限制总执行时间，接口的 `timeout_seconds` 返回 `null`。Agent 留空时同样不限时；显式填写正数仍会限制该 Agent。平台设置正数时，开发者只能在该上限内设置时限。费用防护来自 `PLATFORM_MAX_RUN_COST`。排队期限、Worker 心跳租约、手动停止、调用次数及费用限制独立生效；供应商和网络仍有自己的超时限制。

Playground 输入框工具栏支持选择“本次思考强度”。“自动”沿用已发布 Agent 的设置，再继承模型网关默认值；显式选择覆盖本次运行的所有模型调用，写入不可变运行快照，不修改 Agent 或共享网关。AG-UI 使用 `forwardedProps.reasoningEffort`，普通创建运行接口使用 `reasoning_effort`；均只接受现有思考等级枚举。变更该值属于不同请求，不能复用原幂等键。

已移除单次输出字段原有的 128000 限制，字段当前支持最高 2000000。模型目录仍必须登记真实输出能力和上下文大小。将 Agent 设为 1000000 不代表任何模型都能输出这么多；自动选择或在 Playground 切换模型时，运行快照使用 Agent 与实际模型上限的较小值；绑定固定模型的草稿超出模型登记上限时拒绝保存。

默认值可通过 `PLATFORM_DEFAULT_OUTPUT_TOKENS` 和 `PLATFORM_DEFAULT_CONTEXT_WINDOW` 配置。新建 Agent、模型授权、首次内置模板及前端表单读取同一组配置；显式填写的开发者值优先。`GET /api/v2/tenants/{tenant_id}/token-defaults` 返回当前默认值。累计 Token 预算保持独立，不随单次输出预算自动设置。

## API 示例

```json
{
  "max_tokens": 131072,
  "max_steps": 8,
  "execution_policy": {
    "reasoning_effort": "low",
    "timeout_seconds": 120,
    "max_total_tokens": 500000,
    "max_cost": "0.20",
    "max_output_recoveries": 2
  }
}
```

对 `/api/v2/tenants/{tenant_id}/agents/{agent_id}` 发起 PATCH 保存草稿，再 POST `/{agent_id}/publish` 发布。策略内的 PATCH 只合并明确提供的字段；传 `null` 清除一个可继承字段；`execution_policy: {}` 或 `null` 重置整份策略。旧 Agent 和旧发布版本没有策略字段时继承平台/网关默认值，不改写历史快照。

思考强度通过 Responses 的 `reasoning.effort` 或兼容 Chat Completions 的 `reasoning_effort` 请求字段传递，按请求隔离，不修改共享网关路由。是否支持 none、minimal、low、medium、high、xhigh、max 取决于供应商；显式不支持的值应由上游明确报错，平台不擅自改成另一种强度。

## 输出上限与恢复

执行器只有在取得可验证用量、确认 `finish_reason=length` 后才尝试恢复，计费在恢复前完成。每个恢复请求独立计量，仍占用 `max_steps`，且执行前重新检查运行期限、Token 预算、费用、账号和模型授权。

恢复是让模型基于已确认上下文重新生成精简、完整的结果，不是对截断 JSON 的修补，也不是供应商内部思考状态的检查点恢复。截断的工具调用即使碰巧是合法 JSON 也不会执行。每次达到上限会写入 `model.output_limit`，映射为 AG-UI `platform.output_limit`，运行详情可看到恢复或停止动作。

当恢复次数或总调用次数用尽时，运行明确失败并报告输出上限，不把不完整文本算作成功。没有完整结束帧、缺少用量、断网或未知费用的请求不自动重试，仍走现有对账机制，避免重复收费。

累计 Token 在调用前按保守输入估算与最大输出预留检查；已结算调用使用实际用量，未知用量的预留继续占用预算。费用预算同样检查已结算金额与未确认预留。平台的更严格限额可继续收紧正在排队的任务，已领取任务的 deadline 不会因后续设置变大而延长。

## 迁移与边界

数据库新增 `0011_agent_execution_policy`，仅给 `platform_agents` 增加 JSON 列，默认空策略。旧发布版本、会话、运行、账务不改写。

这次提供可配置的执行预算与有限截断恢复，没有实现多阶段文件检查点或自动判断浏览器界面是否满足业务需求。已有成功界面继续保存在运行事件中；后台进程崩溃后仍不自动重放未知执行。
