import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, LoaderCircle, MessageSquare, Trash2 } from "lucide-react";
import { api, dateTime, getScopeRevision, scopeIsCurrent } from "./api";
import { Button, Modal, SearchBox, useToast } from "./components";
import { downloadSessionMarkdown } from "./session-export";
import type { SessionDeleteResponse, SessionDetailResponse } from "./api-contract.generated";
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
  deleted: (sessionId: string) => void;
  close: () => void;
};

export function SessionHistory({ sessions, loading, error, reload, currentSessionId, agents, tenant, busy, choose, deleted, close }: Props) {
  const [query, setQuery] = useState("");
  const [exporting, setExporting] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ session: Session; message: string } | null>(null);
  const [confirming, setConfirming] = useState<Session | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const request = useRef<AbortController | null>(null);
  const revision = useRef(getScopeRevision()).current;
  const toast = useToast();
  useEffect(() => () => request.current?.abort(), []);
  const items = sessions?.filter((session) => (session.title || "新对话").toLocaleLowerCase().includes(query.toLocaleLowerCase())) ?? [];

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

  async function deleteSession(session: Session) {
    if (request.current || !scopeIsCurrent(revision)) return;
    const controller = new AbortController();
    request.current = controller;
    setDeleting(true);
    setDeleteError("");
    try {
      const result = await api<SessionDeleteResponse>(`/sessions/${encodeURIComponent(session.id)}`, { method: "DELETE", signal: controller.signal }, tenant);
      if (controller.signal.aborted || !scopeIsCurrent(revision)) return;
      setConfirming(null);
      deleted(result.id);
      toast("对话已删除");
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

  if (confirming) return <Modal key="delete" title="删除对话" close={() => { if (!deleting) setConfirming(null); }}>
    <div className="pg-history-delete-body">
      <p>确定删除「<strong>{confirming.title || "新对话"}</strong>」吗？</p>
      <p className="muted">删除后将从历史记录中移除，无法再打开或导出。已有运行和费用记录会保留。</p>
      {deleteError && <div className="pg-notice error" role="alert"><span>{deleteError}</span></div>}
    </div>
    <div className="modal-footer">
      <Button variant="secondary" disabled={deleting} onClick={() => setConfirming(null)}>取消</Button>
      <Button variant="danger" busy={deleting} onClick={() => void deleteSession(confirming)}>
        {!deleting && <Trash2 size={15} />}确认删除
      </Button>
    </div>
  </Modal>;

  return <Modal key="history" title="历史对话" description="可导出已保存的问答内容，或删除不再需要的历史对话。" close={close}>
    <div className="pg-history-body">
      <SearchBox className="pg-history-search" aria-label="搜索历史对话" placeholder="搜索对话" value={query} onChange={setQuery} />
      {error && <div className="pg-notice error" role="alert"><span>{error}</span><button onClick={() => void reload()}>重试</button></div>}
      {failure && <div className="pg-notice error" role="alert"><span>导出失败：{failure.message}</span>
        <button disabled={exporting !== null} onClick={() => void exportSession(failure.session)}>重试导出</button>
      </div>}
      {loading && !sessions ? <div className="pg-loading" role="status"><LoaderCircle size={20} className="spin" />正在加载…</div>
        : items.length ? <div className="pg-history-list" role="list" aria-label="历史对话列表">{items.map((session) => {
          const title = session.title || "新对话";
          return <div key={session.id} className="pg-history-row" role="listitem">
            <button className={`pg-history-open${session.id === currentSessionId ? " selected" : ""}`} disabled={busy}
              aria-label={`打开对话：${title}`} onClick={() => choose(session)}>
              <MessageSquare size={17} /><span><strong>{title}</strong><small>{dateTime(session.created_at)}</small></span><ChevronRight size={16} />
            </button>
            <Button className="pg-history-export" variant="ghost" busy={exporting === session.id} disabled={exporting !== null}
              aria-label={`导出对话：${title}`} title="导出为 Markdown" onClick={() => void exportSession(session)}>
              {exporting !== session.id && <Download size={15} />}导出
            </Button>
            <Button className="pg-history-delete" variant="ghost" disabled={busy || exporting !== null}
              aria-label={`删除对话：${title}`} title="删除对话" onClick={() => { setDeleteError(""); setConfirming(session); }}>
              <Trash2 size={15} /><span>删除</span>
            </Button>
          </div>;
        })}</div> : <div className="pg-history-empty">{query ? "没有找到相关对话，试试其他关键词。" : "开始聊天后，对话会保存在这里。"}</div>}
    </div>
  </Modal>;
}
