# 历史对话导出与删除

Playground → 历史对话 → 对应行的「导出」，下载 UTF-8 Markdown 文件。导出不会切换当前会话，也不会清空正在编辑的草稿。

文件包含会话标题、Agent 名称、创建与导出时间，以及按服务端顺序排列的用户消息和 Agent 回复；保留正文换行、代码块和 Unicode 字符。文件名由清理过的会话标题和会话标识组成。空会话输出明确说明；存在未结束运行时标注「导出时已保存的消息」。

点击导出时，通过现有 `GET /api/v2/tenants/{tenant_id}/sessions/{session_id}` 重新读取完整会话详情，沿用登录态、组织作用域与本人会话鉴权，然后在浏览器生成文件。导出内容为已持久化问答，不含尚未发送的草稿、尚未保存的流式片段、工具内部消息、推理记录或生成界面代码。此功能独立于调用统计中的后台元数据 CSV 导出。

导出中显示进度并阻止重复提交；请求失败保留搜索条件并支持重试；关闭历史对话弹窗、切换组织或退出登录时取消未完成请求。

实现入口：`apps/web/src/SessionHistory.tsx`；文件格式：`apps/web/src/session-export.ts`；验证：`apps/web/tests/session-history.spec.ts`。

## 删除历史对话

历史对话每行提供「删除」按钮；确认弹窗显示会话标题与影响，取消后保留搜索条件。只有服务端确认删除成功，列表才移除对应会话。删除当前正在查看的会话后回到新对话；删除其他会话不影响当前会话和草稿。请求中禁用重复提交与取消，失败保留确认弹窗并允许重试。

`DELETE /api/v2/tenants/{tenant_id}/sessions/{session_id}`（兼容 v1 单组织路径）要求有效登录、CSRF、活跃组织与 `runs.execute`，只允许操作当前用户在该组织中的会话。成功返回 `{id, deleted: true}`，重复删除幂等且仅记录一次 `session.delete` 审计；不存在、其他成员或跨组织会话统一返回 404。存在 queued/running/cancelling Run 时返回 409 `session_busy`，需先停止或等待结束。

删除是会话级逻辑删除：迁移 `0021_session_history_deletion` 新增 `platform_sessions.deleted_at` 及历史查询索引。列表在分页前过滤已删除行，会话详情、继续运行和支持内容读取均拒绝已删除会话；导出因重新读取详情也无法访问已删除会话。保留底层运行、消息与费用记录，不提供恢复入口；此操作不是数据库内容清除。删除与创建 Run 共用租户事务锁，避免新运行与删除互相穿透。

部署前运行 Alembic 升级至 head。后端验证：`tests/test_session_deletion.py`；迁移兼容性：`tests/test_agent_migration.py`。
