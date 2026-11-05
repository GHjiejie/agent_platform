import { terminal } from "./api";
import type { SessionDetailResponse } from "./api-contract.generated";

function exportTime(value: string) {
  const date = new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", second: "2-digit", hour12: false, timeZoneName: "short",
  }).format(date);
}

const inlineText = (value: string) => value.replace(/\s+/g, " ").trim().replace(/[\\`*_{}\[\]<>#|]/g, "\\$&");

export function sessionMarkdown(session: SessionDetailResponse, agentName?: string, exportedAt = new Date()) {
  const title = session.title.trim() || "新对话";
  const messages = session.messages.filter((message) => message.role === "user" || message.role === "assistant");
  const lines = [
    `# ${inlineText(title)}`, "",
    `- Agent：${inlineText(agentName || session.runs[0]?.agent_name || session.agent_id)}`,
    `- 创建时间：${exportTime(session.created_at)}`,
    `- 导出时间：${exportTime(exportedAt.toISOString())}`,
    `- 消息数：${messages.length}`, "",
  ];
  if (session.runs.some((run) => !terminal(run.status))) {
    lines.push("> 会话仍在进行中，以下为导出时已保存的消息。", "");
  }
  if (!messages.length) lines.push("本会话暂无已保存的消息。", "");
  messages.forEach((message, index) => {
    lines.push(`## ${index + 1}. ${message.role === "user" ? "用户" : "Agent"}`, "");
    if (message.created_at) lines.push(`时间：${exportTime(message.created_at)}`, "");
    lines.push(message.content, "");
    if (index < messages.length - 1) lines.push("---", "");
  });
  const filenameTitle = Array.from(title.normalize("NFC").replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, "-").replace(/\s+/g, " ").trim())
    .slice(0, 80).join("") || "新对话";
  const suffix = session.id.replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 12) || "conversation";
  return { content: lines.join("\n"), filename: `${filenameTitle}-${suffix}.md` };
}

export function downloadSessionMarkdown(session: SessionDetailResponse, agentName?: string) {
  const { content, filename } = sessionMarkdown(session, agentName);
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}
