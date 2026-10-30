import { expect, test } from "@playwright/test";
import { RunAgentInputSchema } from "@ag-ui/core/schemas";
import type { Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";

// Every API and SSE request is intercepted. These UI checks never mutate a real
// tenant, invoke a paid model, or read a user's login credentials.
const capabilities = ["runs.execute", "runs.read_own", "agents.read", "agents.manage", "models.read", "models.policy", "billing.read", "billing.read_own", "members.manage", "quotas.manage", "audit.read", "ownership.manage"];
const created = "2026-10-15T02:00:00Z";
const memberships = [
  { id: "member-a", tenant_id: "team-a", tenant_name: "我的组织", role: "owner", status: "active", tenant_status: "active" },
  { id: "member-b", tenant_id: "team-b", tenant_name: "设计团队", role: "owner", status: "active", tenant_status: "active" },
];
const models = [
  { id: "luna", name: "GPT-6 Luna", alias: "gpt-6-luna", is_default: true },
  { id: "k3", name: "k3", alias: "k3", is_default: false },
  ...Array.from({ length: 6 }, (_, index) => ({ id: `model-${index}`, name: `测试模型 ${index + 1}`, alias: `model-${index}`, is_default: false })),
].map((model) => ({ ...model, active: true, input_price: "1", output_price: "3", context_window: 32768, max_output_tokens: 2048 }));
const agents = [
  { id: "assistant", name: "客服回复助手", description: "帮助你写出清晰、友好的回复。", builtin_key: "support" },
  { id: "writer", name: "写作助手", description: "整理想法，完善文章。", builtin_key: null },
].map((agent) => ({ ...agent, model_id: null, system_prompt: "", temperature: 0.5, max_steps: 6, max_tokens: 2048, tools: [], published_version: 1, starter_prompts: ["帮我写一段产品介绍", "整理一份工作计划"], created_at: created }));

async function fixture(page: Page, options: { failRun?: boolean; holdRun?: boolean; longHistory?: boolean; empty?: boolean; dropStream?: boolean; burst?: boolean; delayFinalSnapshot?: boolean; reducedAgents?: boolean } = {}) {
  const writes: { path: string; body: Record<string, unknown>; key?: string }[] = [];
  const errors: string[] = [];
  const resumes: string[] = [];
  let wireRunId = "client-run";
  page.on("pageerror", (error) => errors.push(error.message));
  let resolveEvents: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { resolveEvents = resolve; });
  let runStatus = "queued";
  const messages = options.longHistory ? Array.from({ length: 50 }, (_, index) => ({ id: `message-${index}`, role: index % 2 ? "assistant" : "user", content: `历史消息 ${index + 1}\n${"这是一段用于验证长对话阅读位置的内容。".repeat(8)}`, created_at: created })) : [
    { id: "question", role: "user", content: "帮我写一段产品介绍", created_at: created },
    { id: "answer", role: "assistant", content: "当然。我们可以从用户的问题出发，说明产品如何帮助他们完成工作。\n你希望面向哪一类用户？", created_at: created },
  ];
  const session = { id: "history", tenant_id: "team-a", user_id: "user", agent_id: "assistant", title: "产品介绍怎么写？", created_at: created };
  const newSession = { ...session, id: "new-session", title: "新对话" };
  const run = () => ({ id: "run-ui", session_id: newSession.id, tenant_id: "team-a", user_id: "user", agent_name: "客服回复助手", created_at: created, finished_at: ["queued", "running"].includes(runStatus) ? null : created, model_id: "luna", model_alias: "gpt-6-luna", status: runStatus, error: "", cost: "0.001" });
  let newMessages: typeof messages = [];
  let hasRun = false;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const json = (value: unknown, status = 200) => route.fulfill({ status, json: value });
    if (path === "/api/v2/me") return json({ user: { id: "user", name: "管理员", email: "ui@example.test", active: true, global_status: "active", auth_version: 1, created_at: created, memberships, capabilities: [], platform_roles: [] }, csrf_token: "ui-csrf" });
    const match = path.match(/^\/api\/v2\/tenants\/(team-[ab])(.*)$/);
    if (!match) return json({ error: { message: "Unexpected mock route", code: "unexpected_mock_route" } }, 501);
    const [, tenantId, resource] = match;
    if (!resource) return json({ tenant: { id: tenantId, name: memberships.find((item) => item.tenant_id === tenantId)?.tenant_name, status: "active" }, user: { id: "user", name: "管理员", email: "ui@example.test", tenant_id: tenantId, role: "admin", tenant_role: "owner", capabilities, active: true } });
    if (method !== "GET") {
      const body = request.postDataJSON() as Record<string, unknown> | null;
      writes.push({ path, body: body ?? {}, key: request.headers()["idempotency-key"] });
    }
    if (resource === "/agents" && method === "POST") return json({ ...request.postDataJSON(), id: "created-agent" }, 201);
    if (resource === "/agents") return json({ items: options.empty ? [] : options.reducedAgents ? [
      { ...agents[0], name: "可视化界面助手", builtin_key: "generative_ui", tools: ["generate_sandboxed_ui"] },
      { ...agents[1], id: "normal", name: "normal", builtin_key: "normal" },
    ] : agents });
    if (resource === "/agents/execution-options") return json({ timeout_seconds: 300, max_cost: "1", max_steps: 50, max_output_tokens: 2000000, default_output_tokens: 131072, default_context_window: 1048576 });
    if (resource === "/token-defaults") return json({ output_tokens: 131072, context_window: 1048576 });
    if (resource === "/models") return json({ items: options.empty ? [] : models });
    if (resource === "/sessions" && method === "POST") { newSession.title = String(request.postDataJSON().title); newSession.agent_id = String(request.postDataJSON().agent_id); return json(newSession, 201); }
    if (resource === "/sessions") return json({ items: options.empty ? [] : [session, ...(hasRun ? [newSession] : [])] });
    if (resource === "/sessions/history") return json({ ...session, messages, runs: [] });
    if (resource === "/sessions/new-session") {
      if (options.delayFinalSnapshot && runStatus === "succeeded") await gate;
      return json({ ...newSession, messages: newMessages, runs: hasRun ? [run()] : [] });
    }
    const wire = (events: { id?: string; event: Record<string, unknown> }[]) => route.fulfill({ contentType: "text/event-stream", headers: { "X-Run-ID": "run-ui", "X-AG-UI-Protocol-Version": "1.0" }, body: events.map(({ id, event }) => `${id ? `id: ${id}\n` : ""}data: ${JSON.stringify(event)}\n\n`).join("") });
    const startFrames = () => [
      { event: { type: "RUN_STARTED", threadId: newSession.id, runId: wireRunId, protocolVersion: "1.0" } },
      { event: { type: "STATE_SNAPSHOT", snapshot: { run: run() } } },
      { event: { type: "MESSAGES_SNAPSHOT", messages: newMessages.map((message) => ({ ...message })) } },
    ];
    if (resource.endsWith("/ag-ui") && method === "POST") {
      if (options.failRun) return json({ error: { code: "gateway_not_configured", message: "网关暂时不可用，请稍后重试。" } }, 503);
      const body = RunAgentInputSchema.parse(request.postDataJSON());
      wireRunId = body.runId;
      hasRun = true; runStatus = "running";
      const latest = body.messages.at(-1)!;
      newMessages = [...(options.longHistory ? messages : []), { id: latest.id, role: "user", content: String(latest.content), created_at: created }];
      const prefix = startFrames();
      if (options.holdRun) return wire([...prefix, { id: "3.0", event: { type: "CUSTOM", name: "platform.model", value: { model: "gpt-6-luna" } } }]);
      if (options.dropStream) return wire([...prefix,
        { id: "4.0", event: { type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" } },
        { id: "4.1", event: { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "先接收第一段，" } },
      ]);
      if (options.burst) {
        const chunks = Array.from({ length: 240 }, (_, index) => `片段${index}🌏，`);
        runStatus = "succeeded";
        newMessages.push({ id: "assistant:run-ui", role: "assistant", content: chunks.join(""), created_at: created });
        return wire([...prefix,
          { id: "3.0", event: { type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" } },
          ...chunks.map((delta, index) => ({ id: `${index + 4}.0`, event: { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta } })),
          { id: "244.0", event: { type: "TEXT_MESSAGE_END", messageId: "assistant:run-ui:call-1" } },
          { id: "245.0", event: { type: "RUN_FINISHED", threadId: newSession.id, runId: wireRunId, outcome: { type: "success" } } },
        ]);
      }
      runStatus = "succeeded";
      newMessages.push({ id: "assistant:run-ui", role: "assistant", content: "已收到，我们一起把想法变成清晰的行动。", created_at: created });
      return wire([...prefix,
        { id: "3.0", event: { type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" } },
        { id: "3.1", event: { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "已收到，我们一起把想法变成清晰的行动。" } },
        { id: "4.0", event: { type: "TEXT_MESSAGE_END", messageId: "assistant:run-ui:call-1" } },
        { id: "5.0", event: { type: "MESSAGES_SNAPSHOT", messages: newMessages } },
        { id: "5.1", event: { type: "STATE_SNAPSHOT", snapshot: { run: run() } } },
        { id: "5.2", event: { type: "RUN_FINISHED", threadId: newSession.id, runId: wireRunId, outcome: { type: "success" }, result: { status: "succeeded", runId: "run-ui" } } },
      ]);
    }
    if (resource === "/runs/run-ui/cancel") { runStatus = "cancelled"; resolveEvents?.(); return json(run()); }
    if (resource === "/runs/run-ui/ag-ui") {
      resumes.push(request.headers()["last-event-id"] ?? "");
      if (options.holdRun) await gate;
      const cancelled = String(runStatus) === "cancelled";
      runStatus = cancelled ? "cancelled" : "succeeded";
      if (options.dropStream) {
        const prefix = [...newMessages, { id: "assistant:run-ui:call-1", role: "assistant", content: "先接收第一段，", created_at: created }];
        newMessages.push({ id: "assistant:run-ui", role: "assistant", content: "先接收第一段，再接收第二段。", created_at: created });
        return wire([
          ...startFrames().slice(0, 2), { event: { type: "MESSAGES_SNAPSHOT", messages: prefix } },
          { id: "4.1", event: { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "这段重复事件必须忽略" } },
          { id: "5.0", event: { type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "再接收第二段。" } },
          { id: "6.0", event: { type: "MESSAGES_SNAPSHOT", messages: newMessages } },
          { id: "6.1", event: { type: "RUN_FINISHED", threadId: newSession.id, runId: wireRunId, outcome: { type: "success" } } },
        ]);
      }
      if (!cancelled) newMessages.push({ id: "assistant:run-ui", role: "assistant", content: "已收到，我们一起把想法变成清晰的行动。", created_at: created });
      return wire([...startFrames(), { id: "6.0", event: { type: "RUN_FINISHED", threadId: newSession.id, runId: wireRunId, outcome: { type: cancelled ? "cancelled" : "success" } } }]);
    }
    if (resource === "/runs/run-ui") return json(run());
    if (resource === "/runs") return json({ items: [] });
    if (resource === "/dashboard") return json({ balance: "100", reserved: "0", available: "100", currency: "USD", requests: 30, tokens: 34741, cost: "0.0579", success_rate: 80, active_runs: 0, gateway_configured: true, daily: [{ date: "2026-10-15", requests: 10 }], models: [{ model: "gpt-6-luna", requests: 13 }, { model: "k3", requests: 17 }] });
    if (resource === "/memberships") return json({ items: [{ id: "member-a", user_id: "user", name: "管理员", email: "ui@example.test", role: "owner", status: "active" }] });
    if (resource === "/invitations" && method === "POST") return json({ id: "invitation", accept_url: "http://127.0.0.1/#invite?token=mock-only", role: request.postDataJSON().role });
    if (resource === "/invitations") return json({ items: [] });
    if (resource === "/model-policy") return json({ default_model_id: "luna", ordered_model_ids: ["luna", "k3"], version: 1 });
    if (resource === "/quotas" || resource === "/usage/calls" || resource === "/audit") return json({ items: [] });
    if (resource === "/entitlements") return json({ rpm: null, tpm: null, concurrent: null, max_concurrent_runs: 10, max_queued_runs: 20, max_members: 10, max_agents: 20, max_sse_connections: 10, max_export_jobs: 3, max_budget: null });
    return json({ error: { message: `Unhandled UI fixture: ${resource}` } }, 501);
  });
  return { writes, errors, resumes, release: () => resolveEvents?.() };
}

test("组织移到右上角，侧栏真实占位连续变化并记住折叠状态", async ({ page }, info) => {
  const mock = await fixture(page);
  await page.goto("/#overview");
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  const picker = page.getByRole("combobox", { name: "切换组织", exact: true });
  await expect(picker).toBeVisible();
  expect(await picker.evaluate((node) => Boolean(node.closest(".topbar-right")))).toBe(true);
  await expect(page.locator(".sidebar .select-control")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Playground", exact: true })).toBeVisible();
  await expect.poll(() => page.locator(".sidebar").evaluate((element) => element.getBoundingClientRect().width)).toBe(248);
  await page.screenshot({ path: info.outputPath("overview-expanded.png"), fullPage: true });
  const samples = await page.evaluate(async () => {
    const rows: { t: number; sidebar: number; left: number; right: number; width: number }[] = [];
    const sidebar = document.querySelector(".sidebar")!;
    const main = document.querySelector(".main-shell")!;
    const toggle = document.querySelector<HTMLButtonElement>(".navigation-toggle")!;
    const start = performance.now();
    toggle.click();
    await new Promise<void>((resolve) => {
      function frame(now: number) {
        const side = sidebar.getBoundingClientRect(), rect = main.getBoundingClientRect();
        rows.push({ t: now - start, sidebar: side.width, left: rect.left, right: rect.right, width: rect.width });
        if (now - start < 340) requestAnimationFrame(frame); else resolve();
      }
      requestAnimationFrame(frame);
    });
    return rows;
  });
  await writeFile(info.outputPath("sidebar-layout-samples.json"), JSON.stringify(samples, null, 2));
  await info.attach("sidebar-layout-samples.json", { path: info.outputPath("sidebar-layout-samples.json"), contentType: "application/json" });
  expect(samples.some((row) => row.sidebar > 85 && row.sidebar < 235)).toBe(true);
  expect(samples.every((row) => row.sidebar >= 76 && row.sidebar <= 248)).toBe(true);
  expect(samples.every((row) => Math.abs(row.sidebar - row.left) < 1 && Math.abs(row.right - 1440) < 1)).toBe(true);
  await page.screenshot({ path: info.outputPath("overview-collapsed.png"), fullPage: true });
  await page.reload();
  await expect(page.getByRole("button", { name: "展开侧边栏" })).toBeVisible();
  const reversal = await page.evaluate(async () => {
    const rows: { t: number; sidebar: number; left: number }[] = [];
    const start = performance.now();
    const toggle = document.querySelector<HTMLButtonElement>(".navigation-toggle")!;
    toggle.click();
    setTimeout(() => toggle.click(), 85);
    await new Promise<void>((resolve) => {
      function frame(now: number) {
        rows.push({ t: now - start, sidebar: document.querySelector(".sidebar")!.getBoundingClientRect().width, left: document.querySelector(".main-shell")!.getBoundingClientRect().left });
        if (now - start < 420) requestAnimationFrame(frame); else resolve();
      }
      requestAnimationFrame(frame);
    });
    return rows;
  });
  expect(reversal.some((row) => row.sidebar > 100)).toBe(true);
  expect(reversal.at(-1)!.sidebar).toBeCloseTo(76, 0);
  expect(reversal.every((row) => row.sidebar >= 76 && row.sidebar <= 248)).toBe(true);
  expect(reversal.every((row) => Math.abs(row.sidebar - row.left) < 1)).toBe(true);
  await writeFile(info.outputPath("sidebar-reversal-samples.json"), JSON.stringify(reversal, null, 2));
  await info.attach("sidebar-reversal-samples.json", { path: info.outputPath("sidebar-reversal-samples.json"), contentType: "application/json" });
  await page.getByRole("button", { name: "展开侧边栏" }).click();
  await page.waitForTimeout(65);
  // Full-page capture can resize the viewport during a layout transition.
  await page.screenshot({ path: info.outputPath("sidebar-expanding-midframe.png"), animations: "allow" });
  await page.getByRole("button", { name: "收起侧边栏" }).evaluate((button: HTMLButtonElement) => button.click());
  await page.waitForTimeout(40);
  await page.screenshot({ path: info.outputPath("sidebar-reversing-midframe.png"), animations: "allow" });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect.poll(() => page.locator(".sidebar").evaluate((element) => element.getBoundingClientRect().width)).toBe(76);
  await picker.click();
  await page.getByRole("combobox", { name: "搜索选项" }).fill("设计");
  await page.getByRole("option", { name: /设计团队/ }).click();
  await expect(page).toHaveURL(/tenant=team-b/);
  await expect(picker).toContainText("设计团队");
  expect(mock.errors).toEqual([]);
});

test("突发流式片段在动画帧暂停和最终快照迟到时仍完整显示", async ({ page }) => {
  const mock = await fixture(page, { burst: true, delayFinalSnapshot: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toBeEnabled();
  // Background tabs may defer animation frames indefinitely. Completion must
  // flush accepted text independently, without waiting for another paint tick.
  await page.evaluate(() => { window.requestAnimationFrame = () => 1; window.cancelAnimationFrame = () => {}; });
  await input.fill("验证突发片段");
  await input.press("Enter");
  const answer = Array.from({ length: 240 }, (_, index) => `片段${index}🌏，`).join("");
  await expect(page.getByText(answer, { exact: true })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  mock.release();
  await expect(page.getByRole("button", { name: "复制回复" })).toHaveCount(1);
  await expect(page.getByText(answer, { exact: true })).toHaveCount(1);
  await expect(input).toBeEnabled();
  expect(mock.errors).toEqual([]);
});

test("切换新对话后迟到的流式快照不会恢复旧回复", async ({ page }) => {
  const mock = await fixture(page, { burst: true, delayFinalSnapshot: true });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toBeEnabled();
  await input.fill("验证切换对话");
  await input.press("Enter");
  await expect(page.getByText(/片段239🌏，$/)).toHaveCount(1);
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  mock.release();
  await expect(page.getByRole("heading", { name: "今天想聊些什么？" })).toBeVisible();
  await expect(page.locator(".pg-message-text")).toHaveCount(0);
  await expect(input).toBeEnabled();
  expect(mock.errors).toEqual([]);
});

test("新对话默认 normal，切换助手后新建仍重置为 normal", async ({ page }) => {
  const mock = await fixture(page, { reducedAgents: true });
  await page.goto("/#playground?tenant=team-a");
  const picker = page.getByRole("combobox", { name: "当前 Agent", exact: true });
  await expect(picker).toContainText("normal");
  await picker.click();
  await page.getByRole("option", { name: /^可视化界面助手/ }).click();
  await expect(picker).toContainText("可视化界面助手");
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(picker).toContainText("normal");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await input.fill("普通聊天");
  await input.press("Enter");
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toBeVisible();
  expect(mock.writes.find((request) => request.path.endsWith("/sessions"))?.body.agent_id).toBe("normal");
  expect(mock.errors).toEqual([]);
});

test("显式选择和历史会话保留原助手，失效助手链接回退 normal", async ({ page }) => {
  const mock = await fixture(page, { reducedAgents: true });
  const picker = page.getByRole("combobox", { name: "当前 Agent", exact: true });
  await page.goto("/#playground?tenant=team-a&agent=assistant");
  await expect(picker).toContainText("可视化界面助手");
  await page.goto("/#playground?tenant=team-a&session=history");
  await expect(picker).toContainText("可视化界面助手");
  await expect(picker).toBeDisabled();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(picker).toContainText("normal");
  await page.goto("/#playground?tenant=team-a&agent=removed-agent");
  await expect(picker).toContainText("normal");
  expect(mock.errors).toEqual([]);
});

test("Playground 简洁空态、搜索模型、键盘选择和真实消息交互", async ({ page }, info) => {
  const mock = await fixture(page);
  await page.goto("/#playground?tenant=team-a");
  await expect(page.getByRole("heading", { name: "今天想聊些什么？" })).toBeVisible();
  await expect(page.locator(".session-sidebar, .chat-model-bar, .run-events, .playground-page + .footer")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("playground-empty-desktop.png"), fullPage: true });
  const model = page.getByRole("combobox", { name: "本次使用模型", exact: true });
  await model.click();
  const search = page.getByRole("combobox", { name: "搜索选项" });
  await expect(search).toBeFocused();
  await search.fill("not-a-model");
  await expect(page.getByText("没有匹配的选项")).toBeVisible();
  await search.fill("k3");
  await page.screenshot({ path: info.outputPath("model-picker-desktop.png"), fullPage: true });
  await search.press("Enter");
  await expect(model).toContainText("k3");
  await expect(model).toBeFocused();
  await model.press("Enter");
  await search.press("Escape");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(model).toBeFocused();
  await page.getByRole("button", { name: "帮我写一段产品介绍", exact: true }).click();
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toHaveValue("帮我写一段产品介绍");
  await input.dispatchEvent("keydown", { key: "Enter", isComposing: true });
  expect(mock.writes).toHaveLength(0);
  await input.press("Enter");
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toBeVisible();
  const sent = mock.writes.find((request) => request.path.endsWith("/ag-ui"))!;
  expect(sent.body).toMatchObject({ threadId: "new-session", messages: [{ role: "user", content: "帮我写一段产品介绍" }], state: {}, tools: [], context: [], forwardedProps: { modelId: "k3" } });
  expect(sent.body.runId).toEqual(sent.key);
  expect(sent.key).toBeTruthy();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("playground-conversation-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "运行详情", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "运行详情" })).toContainText("运行完成");
  expect(mock.errors).toEqual([]);
});

test("发送失败保留草稿，只显示一条错误，重试复用幂等标识", async ({ page }) => {
  const mock = await fixture(page, { failRun: true });
  await page.goto("/#playground");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toBeEnabled();
  await input.fill("请帮我整理需求");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(1);
  await expect(page.getByRole("alert")).toContainText("网关暂时不可用");
  await expect(input).toHaveValue("请帮我整理需求");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect.poll(() => mock.writes.filter((request) => request.path.endsWith("/ag-ui")).length).toBe(2);
  const calls = mock.writes.filter((request) => request.path.endsWith("/ag-ui"));
  expect(calls[0].key).toEqual(calls[1].key);
  expect(calls[0].body).toEqual(calls[1].body);
  expect(mock.errors).toEqual([]);
});

test("生成中可停止，运行详情按需打开，不抢占对话空间", async ({ page }) => {
  const mock = await fixture(page, { holdRun: true });
  await page.goto("/#playground");
  await expect(page.getByRole("textbox", { name: "发送给 Agent 的消息" })).toBeEnabled();
  await page.getByRole("textbox", { name: "发送给 Agent 的消息" }).fill("请开始");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "本次使用模型", exact: true })).toBeDisabled();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  expect(mock.writes.filter((request) => request.path.endsWith("/cancel"))).toHaveLength(1);
  expect(mock.errors).toEqual([]);
});

test("AG-UI 断流后用同一运行和游标恢复，重复事件不会重复显示", async ({ page }) => {
  const mock = await fixture(page, { dropStream: true });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toBeEnabled();
  await input.fill("验证断线恢复");
  await input.press("Enter");
  await expect(page.getByText("先接收第一段，", { exact: false })).toBeVisible();
  await expect(page.getByText("先接收第一段，再接收第二段。", { exact: true })).toBeVisible();
  await expect(page.getByText("这段重复事件必须忽略", { exact: false })).toHaveCount(0);
  expect(mock.resumes).toEqual(["4.1"]);
  expect(mock.writes.filter((request) => request.path.endsWith("/ag-ui"))).toHaveLength(1);
  await expect(input).toHaveValue("");
  expect(mock.errors).toEqual([]);
});

test("切换组织后停止旧 AG-UI 连接，旧运行结果不会覆盖新组织", async ({ page }) => {
  const mock = await fixture(page, { holdRun: true });
  await page.goto("/#playground?tenant=team-a");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await expect(input).toBeEnabled();
  await input.fill("旧组织的运行");
  await input.press("Enter");
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  // Allow the first stream to enter its reconnect GET before changing scope.
  await expect.poll(() => mock.resumes.length).toBe(1);
  await page.getByRole("combobox", { name: "切换组织", exact: true }).click();
  await page.getByRole("option", { name: /设计团队/ }).click();
  await expect(page).toHaveURL(/tenant=team-b/);
  mock.release();
  await page.getByRole("link", { name: "Playground", exact: true }).click();
  await expect(page.getByRole("heading", { name: "今天想聊些什么？" })).toBeVisible();
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  expect(mock.writes.filter((request) => request.path.endsWith("/cancel"))).toHaveLength(0);
  expect(mock.errors).toEqual([]);
});

test("长对话更新保持阅读位置，可主动回到最新消息", async ({ page }) => {
  const mock = await fixture(page, { holdRun: true, longHistory: true });
  await page.goto("/#playground?session=history&tenant=team-a");
  await expect(page.getByRole("article", { name: "你的消息" })).toHaveCount(25);
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await page.getByRole("textbox", { name: "发送给 Agent 的消息" }).fill("继续刚才的话题");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await page.locator(".pg-messages").evaluate((element) => { element.scrollTop = 0; element.dispatchEvent(new Event("scroll")); });
  await expect(page.getByRole("button", { name: "回到最新消息" })).toBeVisible();
  mock.release();
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toBeAttached();
  expect(await page.locator(".pg-messages").evaluate((element) => element.scrollTop)).toBeLessThan(10);
  await page.getByRole("button", { name: "回到最新消息" }).click();
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toBeVisible();
  expect(mock.errors).toEqual([]);
});

test("表单下拉保持默认角色与 FormData，Escape 只关闭当前浮层", async ({ page }) => {
  const mock = await fixture(page);
  await page.goto("/#users");
  await page.getByRole("button", { name: "邀请成员", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "邀请组织成员" });
  const role = dialog.getByRole("combobox", { name: "加入后的角色", exact: true });
  await expect(role).toContainText("成员");
  await role.click();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(role).toBeFocused();
  await role.click();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "取消", exact: true })).toBeFocused();
  await role.click();
  await page.getByRole("option", { name: "财务查看员", exact: true }).click();
  await dialog.getByLabel("成员邮箱").fill("invited@example.test");
  await dialog.getByRole("button", { name: "创建邀请" }).click();
  await expect(page.getByRole("dialog", { name: "邀请已创建" })).toBeVisible();
  expect(mock.writes.find((request) => request.path.endsWith("/invitations"))?.body).toEqual({ email: "invited@example.test", role: "finance_viewer" });
  expect(mock.errors).toEqual([]);
});

test("开发者在 Agent 编辑器配置执行策略并提交完整配置", async ({ page }, info) => {
  const mock = await fixture(page);
  await page.goto("/#agents");
  await page.getByRole("button", { name: "创建 Agent", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "创建 Agent", exact: true });
  await expect(dialog.getByLabel("单次输出 Token 上限", { exact: true })).toHaveValue("131072");
  await dialog.getByLabel("Agent 名称", { exact: true }).fill("可配置可视化助手");
  await dialog.getByLabel("单次输出 Token 上限", { exact: true }).fill("1000000");
  await dialog.getByLabel("思考强度", { exact: true }).click();
  await page.getByRole("option", { name: "低（low）", exact: true }).click();
  await dialog.getByLabel("运行时限（秒）", { exact: true }).fill("120");
  await dialog.getByLabel("累计 Token 预算", { exact: true }).fill("1500000");
  await dialog.getByLabel("运行费用上限（USD）", { exact: true }).fill("0.20");
  await dialog.getByLabel("输出上限恢复次数", { exact: true }).fill("2");
  await expect(dialog.getByLabel("运行时限（秒）", { exact: true })).toHaveAttribute("max", "300");
  await page.screenshot({ path: info.outputPath("agent-execution-policy.png"), fullPage: true });
  await dialog.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const saved = mock.writes.find((request) => request.path.endsWith("/agents") && request.body.name === "可配置可视化助手");
  expect(saved?.body).toMatchObject({ max_tokens: 1000000, execution_policy: { reasoning_effort: "low", timeout_seconds: 120, max_total_tokens: 1500000, max_cost: "0.20", max_output_recoveries: 2 } });
  expect(mock.errors).toEqual([]);
});

test("受控候选模型选择保持占位值，不会把空值或重复模型加入策略", async ({ page }) => {
  const mock = await fixture(page);
  await page.goto("/#models");
  await page.getByRole("button", { name: "设置默认与顺序" }).click();
  const dialog = page.getByRole("dialog", { name: "组织模型选择策略" });
  const candidate = dialog.getByRole("combobox", { name: "添加候选模型", exact: true });
  await candidate.click();
  await page.getByRole("option", { name: "选择候选模型", exact: true }).click();
  await expect(dialog.locator("ol li")).toHaveCount(2);
  await candidate.click();
  await page.getByRole("option", { name: "测试模型 1", exact: true }).click();
  await expect(dialog.locator("ol li")).toHaveCount(3);
  await expect(candidate).toContainText("选择候选模型");
  await candidate.click();
  await expect(page.getByRole("option", { name: "测试模型 1", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(mock.writes.find((request) => request.path.endsWith("/model-policy"))?.body).toMatchObject({ default_model_id: "luna", ordered_model_ids: ["luna", "k3", "model-0"] });
  expect(mock.errors).toEqual([]);
});

test("必填下拉不可空提交，选中目标后保留实际表单值", async ({ page }) => {
  const mock = await fixture(page);
  await page.goto("/#quotas");
  await page.getByRole("button", { name: "配置策略", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "配置配额策略" });
  await dialog.getByRole("combobox", { name: "作用范围", exact: true }).click();
  await page.getByRole("option", { name: "指定模型", exact: true }).click();
  const target = dialog.getByRole("combobox", { name: "模型", exact: true });
  await expect(target).toContainText("选择目标");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(target).toHaveAttribute("aria-invalid", "true");
  await expect(dialog.getByText("请选择一项", { exact: true })).toBeVisible();
  expect(mock.writes).toHaveLength(0);
  await expect(target).toBeFocused();
  await target.click();
  await page.getByRole("combobox", { name: "搜索选项" }).fill("Luna");
  await page.getByRole("option", { name: "GPT-6 Luna", exact: true }).click();
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(mock.writes.find((request) => request.path.endsWith("/quotas"))?.body).toMatchObject({ scope: "model", subject_id: "luna" });
  expect(mock.errors).toEqual([]);
});

test("移动端导航、历史对话、下拉浮层均可用且不横向溢出", async ({ page }, info) => {
  const mock = await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#playground");
  await expect(page.getByRole("heading", { name: "今天想聊些什么？" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("playground-empty-mobile.png"), fullPage: true });
  await expect(page.getByRole("navigation", { name: "主导航" })).not.toBeVisible();
  const toggle = page.getByRole("button", { name: "打开导航", exact: true });
  await expect(page.getByRole("button", { name: "退出登录", exact: true })).toBeVisible();
  await toggle.click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(toggle).toBeFocused();
  await page.getByRole("combobox", { name: "本次使用模型", exact: true }).click();
  const bounds = await page.locator(".select-menu").boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "历史对话", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索历史对话" }).fill("产品介绍");
  await page.getByRole("dialog", { name: "历史对话" }).getByRole("button", { name: /产品介绍怎么写/ }).click();
  await expect(page.getByRole("article", { name: "Agent 的回复" })).toBeVisible();
  await page.screenshot({ path: info.outputPath("playground-conversation-mobile.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(mock.errors).toEqual([]);
});

test("空资源有恢复入口，减少动效下交互和焦点结果保持一致", async ({ page }, info) => {
  const mock = await fixture(page, { empty: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#playground");
  await expect(page.getByText("还没有已发布的 Agent。")).toBeVisible();
  await expect(page.getByRole("textbox", { name: "发送给 Agent 的消息" })).toBeDisabled();
  await page.getByRole("button", { name: "收起侧边栏" }).click();
  expect(await page.locator(".sidebar").evaluate((element) => getComputedStyle(element).transitionDuration)).toBe("0s");
  await page.getByRole("combobox", { name: "切换组织", exact: true }).click();
  expect(await page.locator(".select-menu").evaluate((element) => getComputedStyle(element).animationName)).toBe("none");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("combobox", { name: "切换组织", exact: true })).toBeFocused();
  await page.screenshot({ path: info.outputPath("playground-empty-resources.png"), fullPage: true });
  expect(mock.errors).toEqual([]);
});
