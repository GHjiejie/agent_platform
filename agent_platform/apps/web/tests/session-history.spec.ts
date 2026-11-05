import { expect, test, type Download } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { fixture } from "./ui-fixture";
import { sessionMarkdown } from "../src/session-export";
import type { SessionDetailResponse } from "../src/api-contract.generated";

const created = "2026-11-05T02:00:00Z";
const saved: SessionDetailResponse = {
  id: "history", tenant_id: "team-a", user_id: "user", agent_id: "assistant",
  title: "产品介绍怎么写？", created_at: created, runs: [], artifacts: [], reasoning: [],
  messages: [
    { id: "question", run_id: "run-1", role: "user", content: "导出测试：你好🌏\n请保留换行。", created_at: created },
    { id: "answer", run_id: "run-1", role: "assistant", content: "## 建议\n\n```ts\nconst title = '产品介绍';\n```\n\n完整的历史回复。", created_at: created },
  ],
};

async function downloadText(download: Download) {
  expect(await download.failure()).toBeNull();
  const path = await download.path();
  if (!path) throw new Error("浏览器未生成下载文件");
  return readFile(path, "utf8");
}

test("历史对话直接导出已保存问答，保留当前草稿并可继续打开会话", async ({ page }, info) => {
  const mock = await fixture(page);
  let reads = 0;
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => { reads++; return route.fulfill({ json: saved }); });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await input.fill("尚未发送的草稿");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "历史对话", exact: true });
  await expect(dialog.getByRole("button", { name: "导出对话：产品介绍怎么写？", exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath("session-export-desktop.png"), animations: "disabled" });
  const downloaded = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "导出对话：产品介绍怎么写？", exact: true }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe("产品介绍怎么写？-history.md");
  const text = await downloadText(download);
  expect(text).toContain("# 产品介绍怎么写？");
  expect(text).toContain("- Agent：客服回复助手");
  expect(text).toContain("- 创建时间：2026");
  expect(text).toContain("- 消息数：2");
  expect(text).toContain(saved.messages[0].content);
  expect(text).toContain(saved.messages[1].content);
  expect(text.indexOf("## 1. 用户")).toBeLessThan(text.indexOf("## 2. Agent"));
  expect(text).not.toContain("尚未发送的草稿");
  expect(reads).toBe(1);
  expect(mock.writes).toEqual([]);
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/#playground\?tenant=team-a$/);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(input).toHaveValue("尚未发送的草稿");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await dialog.getByRole("button", { name: "打开对话：产品介绍怎么写？", exact: true }).click();
  await expect(page.getByRole("article", { name: "你的消息", exact: true })).toContainText("导出测试：你好🌏");
  expect(mock.errors).toEqual([]);
});

test("导出失败不下载文件，保留搜索条件并支持重新读取后重试", async ({ page }) => {
  const mock = await fixture(page);
  let attempts = 0;
  const downloads: Download[] = [];
  page.on("download", (download) => downloads.push(download));
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => {
    attempts++;
    return attempts === 1
      ? route.fulfill({ status: 503, json: { error: { code: "temporary_error", message: "服务暂时不可用" } } })
      : route.fulfill({ json: saved });
  });
  await page.goto("/#playground?tenant=team-a");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  const search = page.getByRole("textbox", { name: "搜索历史对话", exact: true });
  await search.fill("产品");
  await page.getByRole("button", { name: "导出对话：产品介绍怎么写？", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("导出失败：服务暂时不可用");
  await expect(search).toHaveValue("产品");
  expect(downloads).toHaveLength(0);
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "重试导出", exact: true }).click();
  expect(await downloadText(await downloaded)).toContain("完整的历史回复。");
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(attempts).toBe(2);
  expect(mock.errors).toEqual([]);
});

test("重复点击只发出一个导出请求，关闭历史弹窗后取消下载", async ({ page }) => {
  const mock = await fixture(page);
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let settled: () => void = () => {};
  const finished = new Promise<void>((resolve) => { settled = resolve; });
  let reads = 0;
  const downloads: Download[] = [];
  page.on("download", (download) => downloads.push(download));
  await page.route("**/api/v2/tenants/team-a/sessions/history", async (route) => {
    reads++;
    await gate;
    await route.fulfill({ json: saved });
    settled();
  });
  await page.goto("/#playground?tenant=team-a");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  const button = page.getByRole("button", { name: "导出对话：产品介绍怎么写？", exact: true });
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toHaveAttribute("aria-busy", "true");
  await button.evaluate((element: HTMLButtonElement) => element.click());
  expect(reads).toBe(1);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  release();
  await finished;
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(downloads).toHaveLength(0);
  expect(mock.errors).toEqual([]);
});

test("窄屏长标题仍可导出和搜索，不触发打开会话或横向溢出", async ({ page }, info) => {
  const mock = await fixture(page);
  const title = "一段很长的中文历史会话标题，用于确认导出按钮始终可见且不会挤出弹窗边界";
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v2/tenants/team-a/sessions", (route) => route.fulfill({ json: { items: [{ ...saved, title }] } }));
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => route.fulfill({ json: { ...saved, title } }));
  await page.goto("/#playground?tenant=team-a");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "历史对话", exact: true });
  const exportButton = dialog.getByRole("button", { name: `导出对话：${title}`, exact: true });
  await expect(exportButton).toBeVisible();
  const bounds = await exportButton.boundingBox();
  if (!bounds) throw new Error("导出按钮不可见");
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath("session-export-mobile.png"), animations: "disabled" });
  const downloaded = page.waitForEvent("download");
  await exportButton.click();
  expect(await downloadText(await downloaded)).toContain(saved.messages[0].content);
  await expect(dialog).toBeVisible();
  await page.getByRole("textbox", { name: "搜索历史对话", exact: true }).fill("没有相关内容");
  await expect(page.getByText("没有找到相关对话，试试其他关键词。", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(mock.errors).toEqual([]);
});

test("导出文件名去除路径和控制字符，正文保持原文并排除内部工具消息", () => {
  const session = { ...saved, title: '计划/草稿\\:<>"|?*\n🌏', messages: [...saved.messages, { ...saved.messages[0], id: "internal", role: "tool", content: "internal-tool-result" }] };
  const result = sessionMarkdown(session, "测试 Agent", new Date(created));
  expect(result.filename).toMatch(/🌏-history\.md$/);
  expect(result.filename).not.toMatch(/[<>:"/\\|?*\u0000-\u001f]/);
  expect(result.content).toContain("- 消息数：2");
  expect(result.content).toContain(saved.messages[1].content);
  expect(result.content).not.toContain("internal-tool-result");
});

test("空会话与运行中的会话导出有明确说明", () => {
  expect(sessionMarkdown({ ...saved, messages: [] }).content).toContain("本会话暂无已保存的消息。");
  const run = {
    id: "run-1", tenant_id: "team-a", session_id: "history", user_id: "user", agent_name: "历史 Agent",
    status: "running", model_id: null, model_alias: null, error: "", created_at: created, finished_at: null,
  };
  const result = sessionMarkdown({ ...saved, runs: [run] });
  expect(result.content).toContain("会话仍在进行中，以下为导出时已保存的消息。");
  expect(result.content).toContain("- Agent：历史 Agent");
  expect(result.content).toContain(saved.messages[0].content);
});
