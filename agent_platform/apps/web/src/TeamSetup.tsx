import { useState } from "react";
import { write } from "./api";
import { Button, Modal, useResource } from "./components";
import type { CollaborationSettingsResponse } from "./api-contract.generated";

export function TeamSetup({ close, saved }: { close: () => void; saved: () => Promise<void> }) {
  const settings = useResource<CollaborationSettingsResponse>("/agents/collaboration-settings");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function perform(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败"); } finally { setBusy(false); }
  }
  return <Modal title="团队协作设置" close={close}>
    <div className="form-body">
      {settings.loading && !settings.data && <p role="status">正在读取组织设置…</p>}
      {settings.error && <p role="alert">{settings.error}<Button variant="ghost" onClick={() => void settings.reload()}>重试</Button></p>}
      {settings.data && <>
        <p>当前组织：{settings.data.enabled ? "已开启" : "未开启"}团队协作。关闭后不再接受新的团队运行，已经开始的运行继续完成或由用户取消。</p>
        {!settings.data.platform_enabled && <p>平台尚未开放团队执行。可以先准备模板与成员，运行前需由运营开启平台开关。</p>}
        <p className="muted">动态规划：{settings.data.features?.dynamic ? "已开放" : "未开放"}；双任务并行：{settings.data.features?.parallel ? "已开放" : "未开放"}；故障后续跑：{settings.data.features?.recovery ? "已开放，仅继续有确定执行证据的任务" : "未开放"}。</p>
        <Button variant="secondary" disabled={busy} onClick={() => void perform(async () => {
          if (!settings.data) return;
          await write("/agents/collaboration-settings", { enabled: !settings.data.enabled, expected_version: settings.data.version }, "PATCH");
          await settings.reload(); setMessage("组织协作设置已更新。");
        })}>{settings.data.enabled ? "关闭组织协作" : "开启组织协作"}</Button>
      </>}
      <hr />
      <h3>整理 → 分析 → 图表 → 汇总</h3>
      <p>安装三个已发布成员和一个团队入口。模板仅使用提供的数据；缺少数据时明确提示。重复安装会返回原团队，不覆盖已有编辑。</p>
      <Button busy={busy} onClick={() => void perform(async () => {
        const result = await write<{ id: string; created: boolean }>("/agents/templates/analysis-team");
        await saved(); setMessage(result.created ? "顺序模板已安装并发布，可以在 Agent 列表中编辑或运行。" : "模板已存在，可在 Agent 列表中继续编辑或运行。");
      })}>安装顺序模板</Button>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
    </div>
  </Modal>;
}
