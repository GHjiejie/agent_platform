import { expect, test, type Download, type Page } from "@playwright/test";
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

async function deletionFixture(page: Page, options: { pending?: Promise<void>; failOnce?: boolean } = {}) {
  const mock = await fixture(page);
  let exists = true;
  let deletes = 0;
  await page.route("**/api/v2/tenants/team-a/sessions", (route) => route.fulfill({ json: { items: exists ? [saved] : [] } }));
  await page.route("**/api/v2/tenants/team-a/sessions/history", async (route) => {
    if (route.request().method() === "DELETE") {
      deletes++;
      await options.pending;
      if (options.failOnce && deletes === 1) return route.fulfill({ status: 409, json: { error: {
        code: "session_busy", message: "会话仍在运行，请先停止生成或等待运行结束后再删除。",
      } } });
      exists = false;
      return route.fulfill({ json: { id: saved.id, deleted: true } });
    }
    return route.fulfill({ status: exists ? 200 : 404, json: exists ? saved : { error: { code: "not_found", message: "会话不存在。" } } });
  });
  return { ...mock, deleteCount: () => deletes };
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
  const deleteButton = dialog.getByRole("button", { name: `删除对话：${title}`, exact: true });
  await expect(exportButton).toBeVisible();
  const bounds = await exportButton.boundingBox();
  if (!bounds) throw new Error("导出按钮不可见");
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  const deleteBounds = await deleteButton.boundingBox();
  if (!deleteBounds) throw new Error("删除按钮不可见");
  expect(deleteBounds.x + deleteBounds.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath("session-export-mobile.png"), animations: "disabled" });
  const downloaded = page.waitForEvent("download");
  await exportButton.click();
  expect(await downloadText(await downloaded)).toContain(saved.messages[0].content);
  await expect(dialog).toBeVisible();
  await deleteButton.click();
  await expect(page.getByRole("dialog", { name: "删除对话", exact: true })).toContainText(title);
  await page.screenshot({ path: info.outputPath("session-delete-mobile.png"), animations: "disabled" });
  await page.getByRole("button", { name: "取消", exact: true }).click();
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

test("取消删除保留历史会话、搜索条件和未发送草稿", async ({ page }, info) => {
  const mock = await deletionFixture(page);
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息", exact: true });
  await input.fill("保留这份草稿");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索历史对话", exact: true }).fill("产品");
  await page.getByRole("button", { name: "删除对话：产品介绍怎么写？", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "删除对话", exact: true });
  await expect(confirmation).toContainText("产品介绍怎么写？");
  await expect(confirmation).toContainText("已有运行和费用记录会保留");
  await page.screenshot({ path: info.outputPath("session-delete-desktop.png"), animations: "disabled" });
  await confirmation.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "搜索历史对话", exact: true })).toHaveValue("产品");
  await expect(page.getByRole("button", { name: "打开对话：产品介绍怎么写？", exact: true })).toBeVisible();
  expect(mock.deleteCount()).toBe(0);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(input).toHaveValue("保留这份草稿");
  expect(mock.errors).toEqual([]);
});

test("确认删除只提交一次，成功后移除历史记录并保留其他会话草稿", async ({ page }) => {
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => { release = resolve; });
  const mock = await deletionFixture(page, { pending });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息", exact: true });
  await input.fill("其他会话的草稿");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await page.getByRole("button", { name: "删除对话：产品介绍怎么写？", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "删除对话", exact: true });
  const confirm = confirmation.getByRole("button", { name: "确认删除", exact: true });
  await confirm.click();
  await expect(confirm).toBeDisabled();
  await expect(confirm).toHaveAttribute("aria-busy", "true");
  await expect(confirmation.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await confirm.evaluate((element: HTMLButtonElement) => element.click());
  await confirmation.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(confirmation).toBeVisible();
  expect(mock.deleteCount()).toBe(1);
  release();
  await expect(page.getByText("对话已删除", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开对话：产品介绍怎么写？", exact: true })).toHaveCount(0);
  await expect(page.getByText("开始聊天后，对话会保存在这里。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(input).toHaveValue("其他会话的草稿");
  await page.reload();
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await expect(page.getByText("开始聊天后，对话会保存在这里。", { exact: true })).toBeVisible();
  expect(mock.errors).toEqual([]);
});

test("删除当前会话后回到新对话并清理已显示的消息", async ({ page }) => {
  const mock = await deletionFixture(page);
  await page.goto("/#playground?session=history&tenant=team-a");
  await expect(page.getByRole("article", { name: "你的消息", exact: true })).toContainText("导出测试：你好🌏");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await page.getByRole("button", { name: "删除对话：产品介绍怎么写？", exact: true }).click();
  await page.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByRole("heading", { name: "今天想聊些什么？", exact: true })).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(0);
  expect(new URLSearchParams(new URL(page.url()).hash.split("?")[1]).has("session")).toBe(false);
  await expect(page.getByRole("textbox", { name: "发送给 Agent 的消息", exact: true })).toBeEnabled();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(mock.deleteCount()).toBe(1);
  expect(mock.errors).toEqual([]);
});

test("运行中拒绝删除时保留确认弹窗，运行结束后可重试", async ({ page }) => {
  const mock = await deletionFixture(page, { failOnce: true });
  await page.goto("/#playground?tenant=team-a");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await page.getByRole("button", { name: "删除对话：产品介绍怎么写？", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "删除对话", exact: true });
  await confirmation.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(confirmation.getByRole("alert")).toContainText("会话仍在运行");
  await expect(confirmation.getByRole("button", { name: "确认删除", exact: true })).toBeEnabled();
  await confirmation.getByRole("button", { name: "确认删除", exact: true }).click();
  await expect(page.getByText("对话已删除", { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "历史对话", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开对话：产品介绍怎么写？", exact: true })).toHaveCount(0);
  expect(mock.deleteCount()).toBe(2);
  expect(mock.errors).toEqual([]);
});
