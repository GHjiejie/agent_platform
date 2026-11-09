import { expect, test, type Page } from "@playwright/test";
import { fixture } from "./ui-fixture";

const created = "2026-11-05T02:00:00Z";
const session = { id: "history", tenant_id: "team-a", user_id: "user", agent_id: "assistant", title: "Markdown 显示", created_at: created };
const user = { id: "md-question", run_id: "md-run", role: "user", content: "# 用户原文", created_at: created };
const formatted = [
  "# 操作说明", "", "**重点**：保留格式。", "", "- 第一项", "- 第二项", "",
  "| 指标 | 数值 |", "| --- | --- |", "| 销售额 | 128 |", "",
  "```ts", "const sales = 128;", "```", "",
  "[正常链接](https://example.test/docs)", "[危险链接](javascript:alert(1))", "",
  '<img src="https://images.example.test/tracking.png" onerror="window.__markdownExecuted = true">',
  "<script>window.__markdownExecuted = true</script>", "",
  "![示意图](https://images.example.test/chart.png)",
].join("\n");

async function markdownFixture(page: Page, text = formatted) {
  const mock = await fixture(page);
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => route.fulfill({ json: {
    ...session, messages: [user, { id: "md-answer", run_id: "md-run", role: "assistant", content: text, created_at: created }], runs: [], artifacts: [],
    reasoning: [{ id: "reasoning:md-run:call", run_id: "md-run", status: "complete", content: "## 核对步骤\n\n- 校验输入\n- 整理结果", created_at: created }],
  } }));
  await page.goto("/#playground?session=history&tenant=team-a");
  return mock;
}

test("回复和思考渲染 Markdown，提示保留原文且不执行 HTML 或自动加载图片", async ({ page }) => {
  const images: string[] = [];
  page.on("request", (request) => { if (request.url().startsWith("https://images.example.test/")) images.push(request.url()); });
  const mock = await markdownFixture(page);
  const answer = page.getByRole("article", { name: "Agent 的回复", exact: true });
  await expect(answer.getByRole("heading", { name: "操作说明", exact: true })).toBeVisible();
  await expect(answer.locator("strong")).toHaveText("重点");
  await expect(answer.getByRole("listitem")).toHaveCount(2);
  await expect(answer.getByRole("table")).toContainText("销售额");
  await expect(answer.locator("pre code")).toHaveText("const sales = 128;\n");
  const link = answer.getByRole("link", { name: "正常链接", exact: true });
  await expect(link).toHaveAttribute("href", "https://example.test/docs");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(answer.getByRole("link", { name: "危险链接", exact: true })).toHaveCount(0);
  await expect(answer.locator("img")).toHaveCount(0);
  await expect(answer.getByRole("link", { name: "示意图", exact: true })).toHaveAttribute("href", "https://images.example.test/chart.png");
  expect(await page.evaluate(() => "__markdownExecuted" in window)).toBe(false);
  expect(images).toEqual([]);
  const question = page.getByRole("article", { name: "你的消息", exact: true });
  await expect(question).toHaveText("# 用户原文");
  await expect(question.getByRole("heading")).toHaveCount(0);
  await page.getByRole("button", { name: "思考过程", exact: true }).click();
  await expect(page.getByRole("region", { name: "思考过程", exact: true }).getByRole("heading", { name: "核对步骤", exact: true })).toBeVisible();
  expect(mock.errors).toEqual([]);
});

test("窄屏长代码使用内部横向滚动，表格不会撑宽会话页面", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const code = "const channel = '" + "long-channel-".repeat(60) + "';";
  const mock = await markdownFixture(page, "## 代码与表格\n\n```ts\n" + code + "\n```\n\n| 渠道 | 销售额 |\n| --- | --- |\n| 官网 | 128 |\n");
  const answer = page.getByRole("article", { name: "Agent 的回复", exact: true });
  await expect(answer.locator("pre code")).toContainText(code);
  expect(await answer.locator("pre").evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await expect(answer.getByRole("table")).toContainText("官网");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("markdown-mobile.png"), animations: "disabled" });
  expect(mock.errors).toEqual([]);
});

test("未闭合代码块在流式输出时可读，终态快照保留完整 Unicode 与 Markdown", async ({ page }) => {
  const mock = await fixture(page);
  let complete = false;
  let wireRunId = "";
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const partial = "## 实时回复\n\n**重点**\n\n```ts\nconst family = '👨‍👩‍👧‍👦';\n";
  const final = partial + "```\n\n已完成。";
  const run = { id: "md-live", session_id: "new-session", tenant_id: "team-a", user_id: "user", agent_name: "客服回复助手", status: "running", created_at: created, finished_at: null, error: "", model_id: "luna", model_alias: "gpt-6-luna" };
  const wire = (events: unknown[]) => events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
  await page.route("**/api/v2/tenants/team-a/sessions/new-session", (route) => route.fulfill({ json: {
    ...session, id: "new-session", messages: complete ? [{ ...user, run_id: run.id }, { id: "md-final", run_id: run.id, role: "assistant", content: final, created_at: created }] : [],
    runs: complete ? [{ ...run, status: "succeeded", finished_at: created }] : [], artifacts: [], reasoning: [],
  } }));
  await page.route("**/api/v2/tenants/team-a/sessions/new-session/ag-ui", (route) => {
    const input: { runId: string } = route.request().postDataJSON();
    wireRunId = input.runId;
    return route.fulfill({ contentType: "text/event-stream", headers: { "X-Run-ID": run.id, "X-AG-UI-Protocol-Version": "1.0" }, body: wire([
      { type: "RUN_STARTED", threadId: "new-session", runId: wireRunId, protocolVersion: "1.0" },
      { type: "STATE_SNAPSHOT", snapshot: { run } },
      { type: "TEXT_MESSAGE_START", messageId: `assistant:${run.id}:call`, role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: `assistant:${run.id}:call`, delta: partial },
    ]) });
  });
  await page.route("**/api/v2/tenants/team-a/runs/md-live/ag-ui", async (route) => {
    await gate;
    complete = true;
    await route.fulfill({ contentType: "text/event-stream", headers: { "X-Run-ID": run.id, "X-AG-UI-Protocol-Version": "1.0" }, body: wire([
      { type: "RUN_STARTED", threadId: "new-session", runId: wireRunId, protocolVersion: "1.0" },
      { type: "STATE_SNAPSHOT", snapshot: { run: { ...run, status: "succeeded", finished_at: created } } },
      { type: "MESSAGES_SNAPSHOT", messages: [{ id: "md-final", role: "assistant", content: final }] },
      { type: "RUN_FINISHED", threadId: "new-session", runId: wireRunId, outcome: { type: "success" } },
    ]) });
  });
  try {
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息", exact: true });
    await expect(input).toBeEnabled();
    await input.fill("检查流式 Markdown");
    await input.press("Enter");
    const live = page.getByRole("article", { name: "Agent 正在回复", exact: true });
    await expect(live.getByRole("heading", { name: "实时回复", exact: true })).toBeVisible();
    await expect(live.locator("pre code")).toContainText("👨‍👩‍👧‍👦");
    release();
    const saved = page.getByRole("article", { name: "Agent 的回复", exact: true });
    await expect(saved.getByRole("heading", { name: "实时回复", exact: true })).toBeVisible();
    await expect(saved.locator("pre code")).toContainText("const family = '👨‍👩‍👧‍👦';");
    await expect(saved).toContainText("已完成。");
    await expect(page.getByRole("button", { name: "停止生成", exact: true })).toHaveCount(0);
  } finally { release(); }
  expect(mock.errors).toEqual([]);
});
