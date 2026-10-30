import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { EventType } from "@ag-ui/core";
import { reduceMessages } from "../src/ag-ui";
import { memberContracts } from "../src/TeamEditor";
import { fixture } from "./ui-fixture";

const created = "2026-10-30T02:00:00Z";
const agent = { id: "team", name: "研究团队", description: "整理分析", execution_mode: "team", collaboration_spec: { members: [{ key: "m", agent_id: "member", version: 1, responsibility: "整理" }], tasks: [{ key: "normalize", member_key: "m", objective: "整理数据", task_type: "normalize", dependencies: [] }] }, task_contracts: [], capability_keys: [], model_id: null, system_prompt: "汇总", temperature: 1, max_steps: 6, max_tokens: 128, tools: [], published_version: 1, created_at: created };
const session = { id: "new-session", tenant_id: "team-a", user_id: "user", agent_id: "team", title: "团队分析", created_at: created };
const run = { id: "run-ui", session_id: session.id, tenant_id: "team-a", user_id: "user", execution_mode: "team", agent_name: "研究团队", created_at: created, finished_at: created, model_id: "luna", model_alias: "offline-model", status: "succeeded", outcome: "partial", error: "" };
const fees = { confirmed: "0.0004", pending: "0" };
const task = { id: "task1", logical_key: "normalize", member_key: "m", agent_id: "member", agent_version: 1, task_type: "normalize", objective: "整理数据", dependencies: [], required: true, status: "succeeded", error: "" };
const execution = { id: "exec1", task_id: "task1", attempt: 1, role: "member", agent_id: "member", agent_version: 1, name: "整理成员", model_alias: "offline-model", status: "succeeded", output: { facts: ["已提供数据"] }, error: "", input_digest: "input-proof", input_summary: "只接收已分配的数据", created_at: created, finished_at: created, fees };
const detail = { run_id: run.id, status: "succeeded", enabled: true, outcome: "partial", tasks: [task], executions: [execution, { ...execution, id: "summary", task_id: null, role: "summary", name: "研究团队", output: "统一答案" }], artifacts: [{ id: "artifact1", execution_id: "exec1", version: 1, kind: "task_result", content: execution.output, digest: "source-proof", validation_status: "validated" }], fees, max_cost: "1" };

async function teamFixture(page: Page, failedChart = false) {
  const mock = await fixture(page);
  let completed = failedChart;
  let settings = { enabled: false, version: 1, platform_enabled: true };
  const ui = { validated: true, generating: false, htmlComplete: true, cssComplete: true, jsFunctionsComplete: true, jsExpressionsComplete: true, initialHeight: 180, placeholderMessages: [], html: ['<button id="add">增加</button><output id="count">0</output>'], css: "body{padding:16px}", jsFunctions: "", jsExpressions: ["let n=0;document.querySelector('#add').addEventListener('click',()=>{document.querySelector('#count').textContent=String(++n)})"] };
  await page.route("**/api/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    const resource = path.replace("/api/v2/tenants/team-a", "");
    const json = (value: unknown) => route.fulfill({ json: value });
    if (resource === "/agents/discovery") return json({ items: [{ id: "member", name: "整理成员", version: 1, description: "整理", task_contracts: memberContracts, capability_keys: ["normalize"], contract_digests: {} }], next_cursor: null });
    if (resource === "/agents/collaboration-settings") {
      if (method === "PATCH") settings = { ...settings, enabled: request.postDataJSON().enabled, version: settings.version + 1 };
      return json(settings);
    }
    if (resource === "/agents/templates/analysis-team") return json({ id: "team", created: true });
    if (resource === "/agents/team/publish") return json({ version: 2 });
    if (resource === "/agents/team" && method === "PATCH") { mock.writes.push({ path, body: request.postDataJSON() }); return json(agent); }
    if (resource === "/agents" && method === "GET") return json({ items: [agent] });
    if (resource === "/sessions/new-session") return json({ ...session, messages: completed ? [{ id: "question", role: "user", content: "分析这些数据", run_id: run.id }, ...(failedChart ? [] : [{ id: "assistant:run-ui", role: "assistant", content: "统一答案", run_id: run.id }])] : [], runs: completed ? [{ ...run, status: failedChart ? "failed" : run.status }] : [], artifacts: failedChart ? [{ id: "artifact:run-ui:chart", run_id: run.id, content: ui }] : [] });
    if (resource === "/runs/run-ui/collaboration") return json(detail);
    if (resource === "/runs/run-ui/tasks/task1") return json({ ...detail, executions: [execution] });
    if (resource === "/sessions/new-session/ag-ui") {
      const body = request.postDataJSON(); completed = true;
      const events = [
        { type: "RUN_STARTED", threadId: session.id, runId: body.runId },
        { type: "STATE_SNAPSHOT", snapshot: { run: { ...run, status: "running", finished_at: null } } },
        { type: "MESSAGES_SNAPSHOT", messages: [{ id: "question", role: "user", content: "分析这些数据", metadata: { platform: { run_id: run.id } } }] },
        { type: "SUBAGENT_STARTED", subagentRunId: "exec1", name: "整理成员" },
        { type: "TEXT_MESSAGE_START", subagentRunId: "exec1", messageId: "assistant:run-ui:exec1:call", role: "assistant" },
        { type: "TEXT_MESSAGE_CONTENT", subagentRunId: "exec1", messageId: "assistant:run-ui:exec1:call", delta: "成员私有中间回复" },
        { type: "SUBAGENT_ERROR", subagentRunId: "exec2", message: "可选成员失败", code: "failed" },
        { type: "MESSAGES_SNAPSHOT", messages: [{ id: "question", role: "user", content: "分析这些数据", metadata: { platform: { run_id: run.id } } }, { id: "assistant:run-ui:exec1:call", subagentRunId: "exec1", role: "assistant", content: "成员私有中间回复" }, { id: "assistant:run-ui", role: "assistant", content: "统一答案" }] },
        { type: "STATE_SNAPSHOT", snapshot: { run } },
        { type: "RUN_FINISHED", threadId: session.id, runId: body.runId, outcome: { type: "success" } },
      ];
      return route.fulfill({ contentType: "text/event-stream", headers: { "X-Run-ID": run.id }, body: events.map((event, index) => `id: ${index + 1}.0\ndata: ${JSON.stringify(event)}\n\n`).join("") });
    }
    return route.fallback();
  });
  return mock;
}

test("发现成员、配置团队、保存发布与安装模板", async ({ page }) => {
  const mock = await teamFixture(page);
  await page.goto("/#agents?tenant=team-a");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByRole("button", { name: "添加任务", exact: true }).click();
  await page.getByRole("textbox", { name: "任务 2 的目标", exact: true }).fill("分析资料");
  await page.getByRole("combobox", { name: "任务 2 的成员", exact: true }).click();
  await page.getByRole("option", { name: "整理成员 · v1", exact: true }).click();
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const saved = mock.writes.find((item) => item.path.endsWith("/agents/team"));
  expect(saved?.body.execution_mode).toBe("team");
  expect(saved?.body.collaboration_spec).toMatchObject({ members: expect.arrayContaining([{ key: expect.any(String), agent_id: "member", version: 1, responsibility: "分析资料" }]) });
  await page.getByRole("button", { name: "发布新版", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已发布");
  await page.getByRole("button", { name: "团队协作设置", exact: true }).click();
  await page.getByRole("button", { name: "开启组织协作", exact: true }).click();
  await expect(page.getByText("组织协作设置已更新。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "安装顺序模板", exact: true }).click();
  await expect(page.getByText("顺序模板已安装并发布，可以在 Agent 列表中编辑或运行。", { exact: true })).toBeVisible();
  expect(mock.errors).toEqual([]);
});

test("成员错误不会终止主流，主对话只显示统一答案并可追溯费用", async ({ page }) => {
  const mock = await teamFixture(page);
  await page.goto("/#playground?tenant=team-a&agent=team");
  await page.getByRole("textbox", { name: "发送给 Agent 的消息" }).fill("分析这些数据");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".pg-message-text").filter({ hasText: "统一答案" })).toHaveCount(1);
  await expect(page.getByText("成员私有中间回复", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查看团队协作过程", exact: true }).click();
  await expect(page.getByRole("button", { name: "收起团队协作过程 · 部分完成", exact: true })).toBeVisible();
  await page.getByText("整理数据 · 已完成", { exact: true }).click();
  await expect(page.getByText("整理成员 · v1 · offline-model", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看输入来源", exact: true }).click();
  await expect(page.getByText("只接收已分配的数据", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "主导航" })).not.toBeVisible();
  await expect.poll(() => page.locator(".sidebar").evaluate((node) => node.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: "/tmp/agent-platform-collaboration-mobile.png", fullPage: true });
  expect(mock.errors).toEqual([]);
});

test("汇总失败后已校验图表仍可交互", async ({ page }) => {
  const mock = await teamFixture(page, true);
  await page.goto("/#playground?tenant=team-a&session=new-session");
  const chart = page.frameLocator('iframe[aria-label="生成的交互界面"]');
  await expect(chart.getByRole("button", { name: "增加", exact: true })).toBeVisible();
  await chart.getByRole("button", { name: "增加", exact: true }).click();
  await expect(chart.locator("#count")).toHaveText("1");
  expect(mock.errors).toEqual([]);
});

test("交错成员文本、工具和快照保留 subagentRunId", () => {
  let messages: Parameters<typeof reduceMessages>[0] = [];
  for (const id of ["a", "b"]) {
    messages = reduceMessages(messages, { type: EventType.TEXT_MESSAGE_START, messageId: id, subagentRunId: id, role: "assistant" });
    messages = reduceMessages(messages, { type: EventType.TEXT_MESSAGE_CONTENT, messageId: id, subagentRunId: id, delta: id });
  }
  messages = reduceMessages(messages, { type: EventType.TOOL_CALL_START, toolCallId: "tool", parentMessageId: "a", subagentRunId: "a", toolCallName: "calculator" });
  messages = reduceMessages(messages, { type: EventType.ACTIVITY_SNAPSHOT, messageId: "ui", subagentRunId: "b", activityType: "GENERATIVE_UI", content: { validated: true }, replace: true });
  expect(messages.map((item) => item.subagentRunId)).toEqual(["a", "b", "b"]);
  expect(reduceMessages([], { type: EventType.MESSAGES_SNAPSHOT, messages })).toEqual(messages);
});
