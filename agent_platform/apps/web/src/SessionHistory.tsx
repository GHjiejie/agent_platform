import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, LoaderCircle, MessageSquare, Trash2 } from "lucide-react";
import { api, dateTime, getScopeRevision, scopeIsCurrent } from "./api";
import { Button, Modal, SearchBox, useToast } from "./components";
import { downloadSessionMarkdown } from "./session-export";
import type { SessionBatchDeleteResponse, SessionDeleteResponse, SessionDetailResponse } from "./api-contract.generated";
import type { Agent, Session } from "./types";
import "./session-history.css";

type Props = {
  sessions: Session[] | undefined;
  loading: boolean;
  error: string;
  reload: () => Promise<unknown>;
  currentSessionId: string | null;
  agents: Agent[] | undefined;
  tenant: string;
  busy: boolean;
  choose: (session: Session) => void;
  deleted: (sessionIds: string[], options?: { keepHistoryOpen: boolean }) => void;
  close: () => void;
};

type DeleteTargets = { kind: "single" | "batch"; sessions: Session[] };
type BatchResult = SessionBatchDeleteResponse & { sessions: Session[] };

export function SessionHistory({ sessions, loading, error, reload, currentSessionId, agents, tenant, busy, choose, deleted, close }: Props) {
  const [query, setQuery] = useState("");
  const [exporting, setExporting] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ session: Session; message: string } | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [confirming, setConfirming] = useState<DeleteTargets | null>(null);
  const [batchResult, setBatchResult] = useState<BatchResult | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const request = useRef<AbortController | null>(null);
  const selectAll = useRef<HTMLInputElement>(null);
  const revision = useRef(getScopeRevision()).current;
  const toast = useToast();
  useEffect(() => () => request.current?.abort(), []);
  const items = sessions?.filter((session) => (session.title || "新对话").toLocaleLowerCase().includes(query.toLocaleLowerCase())) ?? [];
  const selected = items.filter((session) => selectedIds.includes(session.id));
  const allSelected = items.length > 0 && selected.length === items.length;
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = selected.length > 0 && !allSelected;
  }, [selected.length, allSelected, confirming]);

  function confirmDeletion(targets: DeleteTargets) {
    if (!targets.sessions.length) return;
    setDeleteError("");
    setBatchResult(null);
    setConfirming(targets);
  }

  async function exportSession(session: Session) {
    if (request.current || !scopeIsCurrent(revision)) return;
    const controller = new AbortController();
    request.current = controller;
    setExporting(session.id);
    setFailure(null);
    try {
      const detail = await api<SessionDetailResponse>(`/sessions/${encodeURIComponent(session.id)}`, { signal: controller.signal }, tenant);
      if (controller.signal.aborted || !scopeIsCurrent(revision)) return;
      downloadSessionMarkdown(detail, agents?.find((agent) => agent.id === detail.agent_id)?.name);
      toast("已生成对话导出文件");
    } catch (error) {
      if (!controller.signal.aborted && scopeIsCurrent(revision)) {
        setFailure({ session, message: error instanceof Error ? error.message : "请稍后重试。" });
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) setExporting(null);
      }
    }
  }

  async function deleteSessions(targets: DeleteTargets) {
    const first = targets.sessions[0];
    if (!first || request.current || !scopeIsCurrent(revision)) return;
    const controller = new AbortController();
    request.current = controller;
    setDeleting(true);
    setDeleteError("");
    try {
      const result: SessionBatchDeleteResponse = targets.kind === "batch"
        ? await api<SessionBatchDeleteResponse>("/sessions/batch-delete", {
          method: "POST", signal: controller.signal, body: JSON.stringify({ session_ids: targets.sessions.map((session) => session.id) }),
        }, tenant)
        : { deleted_ids: [(await api<SessionDeleteResponse>(`/sessions/${encodeURIComponent(first.id)}`, { method: "DELETE", signal: controller.signal }, tenant)).id], failed: [] };
      if (controller.signal.aborted || !scopeIsCurrent(revision)) return;
      setConfirming(null);
      if (targets.kind === "batch") {
        setBatchResult({ ...result, sessions: targets.sessions });
        setSelectedIds(result.failed.map((item) => item.id));
        deleted(result.deleted_ids, { keepHistoryOpen: true });
      } else {
        setSelectedIds((current) => current.filter((id) => !result.deleted_ids.includes(id)));
        deleted(result.deleted_ids);
        toast("对话已删除");
      }
      void reload();
    } catch (error) {
      if (!controller.signal.aborted && scopeIsCurrent(revision)) {
        setDeleteError(error instanceof Error ? error.message : "删除失败，请稍后重试。");
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) setDeleting(false);
      }
    }
  }

  if (confirming) return <Modal key="delete" title={confirming.kind === "batch" ? "批量删除对话" : "删除对话"} close={() => { if (!deleting) setConfirming(null); }}>
    <div className="pg-history-delete-body">
      {confirming.kind === "single" ? <p>确定删除「<strong>{confirming.sessions[0]?.title || "新对话"}</strong>」吗？</p>
        : <><p>确定删除所选的 <strong>{confirming.sessions.length}</strong> 条对话吗？</p>
          <ul className="pg-history-confirm-list">{confirming.sessions.map((session) => <li key={session.id}>
            {session.title || "新对话"}{session.id === currentSessionId && <small>当前会话</small>}
          </li>)}</ul>
          <p className="muted">运行中或已无访问权限的会话会保留，并显示未删除的原因。</p>
        </>}
      <p className="muted">删除后将从历史记录中移除，无法再打开或导出。已有运行和费用记录会保留。</p>
      {deleteError && <div className="pg-notice error" role="alert"><span>{deleteError}</span></div>}
    </div>
    <div className="modal-footer">
      <Button variant="secondary" disabled={deleting} onClick={() => setConfirming(null)}>取消</Button>
      <Button variant="danger" busy={deleting} onClick={() => void deleteSessions(confirming)}>
        {!deleting && <Trash2 size={15} />}{confirming.kind === "batch" ? `确认删除 ${confirming.sessions.length} 条` : "确认删除"}
      </Button>
    </div>
  </Modal>;

  return <Modal key="history" title="历史对话" description="可导出已保存的问答内容，或删除不再需要的历史对话。" close={close}>
    <div className="pg-history-body">
      <SearchBox className="pg-history-search" aria-label="搜索历史对话" placeholder="搜索对话" value={query}
        onChange={(value) => { setQuery(value); setSelectedIds([]); setBatchResult(null); }} />
      {error && <div className="pg-notice error" role="alert"><span>{error}</span><button onClick={() => void reload()}>重试</button></div>}
      {failure && <div className="pg-notice error" role="alert"><span>导出失败：{failure.message}</span>
        <button disabled={exporting !== null} onClick={() => void exportSession(failure.session)}>重试导出</button>
      </div>}
      {batchResult && <section className="pg-history-batch-result" aria-label="批量删除结果">
        <p role="status">已删除 {batchResult.deleted_ids.length} 条对话{batchResult.failed.length ? `，${batchResult.failed.length} 条未删除。` : "。"}</p>
        {!!batchResult.failed.length && <>
          <ul>{batchResult.failed.map((item) => <li key={item.id}><strong>{batchResult.sessions.find((session) => session.id === item.id)?.title || "新对话"}</strong>
            <span>{item.message}</span></li>)}</ul>
          <Button variant="secondary" onClick={() => confirmDeletion({ kind: "batch", sessions: batchResult.sessions.filter((session) => batchResult.failed.some((item) => item.id === session.id)) })}>重试未删除项</Button>
        </>}
      </section>}
      {!!sessions?.length && <div className="pg-history-bulk-actions">
        <label className="pg-history-select-all"><input ref={selectAll} className="pg-history-selection" type="checkbox"
          aria-label="全选当前结果" checked={allSelected} disabled={!items.length || busy || exporting !== null}
          onChange={(event) => setSelectedIds(event.target.checked ? items.map((session) => session.id) : [])} />全选当前结果</label>
        <div><span className="pg-history-selected-count">已选 {selected.length} 条</span>
          <Button variant="danger" disabled={!selected.length || busy || exporting !== null}
            onClick={() => confirmDeletion({ kind: "batch", sessions: selected })}><Trash2 size={14} />批量删除</Button>
        </div>
      </div>}
      {loading && !sessions ? <div className="pg-loading" role="status"><LoaderCircle size={20} className="spin" />正在加载…</div>
        : items.length ? <div className="pg-history-list" role="list" aria-label="历史对话列表">{items.map((session) => {
          const title = session.title || "新对话";
          return <div key={session.id} className="pg-history-row" role="listitem">
            <input className="pg-history-selection" type="checkbox" aria-label={`选择对话：${title}`} checked={selectedIds.includes(session.id)}
              disabled={busy || exporting !== null} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...current, session.id] : current.filter((id) => id !== session.id))} />
            <button className={`pg-history-open${session.id === currentSessionId ? " selected" : ""}`} disabled={busy}
              aria-label={`打开对话：${title}`} onClick={() => choose(session)}>
              <MessageSquare size={17} /><span><strong>{title}</strong><small>{dateTime(session.created_at)}</small></span><ChevronRight size={16} />
            </button>
            <Button className="pg-history-export" variant="ghost" busy={exporting === session.id} disabled={exporting !== null}
              aria-label={`导出对话：${title}`} title="导出为 Markdown" onClick={() => void exportSession(session)}>
              {exporting !== session.id && <Download size={15} />}导出
            </Button>
            <Button className="pg-history-delete" variant="ghost" disabled={busy || exporting !== null}
              aria-label={`删除对话：${title}`} title="删除对话" onClick={() => confirmDeletion({ kind: "single", sessions: [session] })}>
              <Trash2 size={15} /><span>删除</span>
            </Button>
          </div>;
        })}</div> : <div className="pg-history-empty">{query ? "没有找到相关对话，试试其他关键词。" : "开始聊天后，对话会保存在这里。"}</div>}
    </div>
  </Modal>;
}
