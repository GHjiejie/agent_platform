import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, LoaderCircle, MessageSquare } from "lucide-react";
import { api, dateTime, getScopeRevision, scopeIsCurrent } from "./api";
import { Button, Modal, SearchBox, useToast } from "./components";
import { downloadSessionMarkdown } from "./session-export";
import type { SessionDetailResponse } from "./api-contract.generated";
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
  close: () => void;
};

export function SessionHistory({ sessions, loading, error, reload, currentSessionId, agents, tenant, busy, choose, close }: Props) {
  const [query, setQuery] = useState("");
  const [exporting, setExporting] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ session: Session; message: string } | null>(null);
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

  return <Modal title="历史对话" description="可将每条对话已保存的问答内容导出为 Markdown 文件。" close={close}>
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
          </div>;
        })}</div> : <div className="pg-history-empty">{query ? "没有找到相关对话，试试其他关键词。" : "开始聊天后，对话会保存在这里。"}</div>}
    </div>
  </Modal>;
}
