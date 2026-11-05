import { expect, test, type Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";
import { fixture } from "./ui-fixture";

const created = "2026-11-05T02:00:00Z";
const reasoning = Array.from({ length: 12 }, (_, index) => `分析阶段 ${index + 1}：这是用于检查长内容展开和折叠的示例文字。`).join("\n\n");
const run = {
  id: "run-motion", session_id: "history", tenant_id: "team-a", user_id: "user", agent_name: "客服回复助手",
  status: "succeeded", created_at: created, finished_at: created, error: "", model_id: "luna", model_alias: "gpt-6-luna",
};
const user = { id: "motion-question", run_id: run.id, role: "user", content: "检查思考区域的过渡", created_at: created };
const history = {
  id: "history", tenant_id: "team-a", user_id: "user", agent_id: "assistant", title: "思考过渡验证", created_at: created,
  messages: [user, { id: "motion-answer", run_id: run.id, role: "assistant", created_at: created,
    content: Array.from({ length: 18 }, (_, index) => `回复段落 ${index + 1}：展开上方的思考过程时，这段回复应随着真实高度移动。`).join("\n\n") }],
  runs: [run], artifacts: [], reasoning: [{ id: `reasoning:${run.id}:call-1`, run_id: run.id, status: "complete", content: reasoning, created_at: created }],
};

async function historyFixture(page: Page) {
  const mock = await fixture(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => route.fulfill({ json: history }));
  await page.goto("/#playground?session=history&tenant=team-a");
  const toggle = page.getByRole("button", { name: "思考过程", exact: true });
  await expect(toggle).toBeVisible();
  await toggle.scrollIntoViewIfNeeded();
  await page.evaluate(() => document.fonts.ready);
  return mock;
}

test("思考内容双向过渡真实高度，回复随布局移动且阅读位置稳定", async ({ page }, info) => {
  const mock = await historyFixture(page);
  const panel = page.getByRole("region", { name: "思考过程", exact: true });
  const content = panel.locator(".pg-reasoning-collapse");
  await expect(content).toHaveAttribute("aria-hidden", "true");
  await expect(content).toHaveJSProperty("inert", true);
  const measured = await page.evaluate(async () => {
    const panel = document.querySelector<HTMLElement>(".pg-reasoning");
    const toggle = panel?.querySelector<HTMLButtonElement>(".pg-reasoning-toggle");
    const reply = panel?.nextElementSibling;
    if (!panel || !toggle || !reply) throw new Error("Missing reasoning or adjacent reply");
    const rows: { phase: string; t: number; height: number; width: number; top: number; gap: number }[] = [];
    const initial = panel.getBoundingClientRect().height;
    async function sample(phase: string) {
      const started = performance.now();
      toggle?.click();
      await new Promise<void>((resolve) => {
        function frame(now: number) {
          if (!panel || !reply) return resolve();
          const rect = panel.getBoundingClientRect();
          rows.push({ phase, t: now - started, height: rect.height, width: rect.width, top: rect.top, gap: reply.getBoundingClientRect().top - rect.bottom });
          if (now - started < 340) requestAnimationFrame(frame); else resolve();
        }
        requestAnimationFrame(frame);
      });
    }
    await sample("expand");
    const expanded = panel.getBoundingClientRect().height;
    await sample("collapse");
    return { initial, expanded, collapsed: panel.getBoundingClientRect().height, rows };
  });
  expect(measured.expanded).toBeGreaterThan(measured.initial + 150);
  expect(measured.collapsed).toBeCloseTo(measured.initial, 0);
  for (const phase of ["expand", "collapse"]) {
    expect(measured.rows.filter((row) => row.phase === phase).some((row) => row.height > measured.initial + 2 && row.height < measured.expanded - 2)).toBe(true);
  }
  expect(measured.rows.every((row) => Math.abs(row.width - measured.rows[0].width) < 1)).toBe(true);
  expect(measured.rows.every((row) => Math.abs(row.gap - measured.rows[0].gap) < 1)).toBe(true);
  expect(Math.max(...measured.rows.map((row) => row.top)) - Math.min(...measured.rows.map((row) => row.top))).toBeLessThan(2);
  await writeFile(info.outputPath("reasoning-layout-frames.json"), JSON.stringify(measured, null, 2));
  await expect(content).toHaveCSS("visibility", "hidden");
  await expect(panel.locator(".pg-reasoning-content")).not.toBeVisible();
  await panel.getByRole("button", { name: "思考过程", exact: true }).click();
  await expect(content).toHaveAttribute("aria-hidden", "false");
  await expect(content).toHaveJSProperty("inert", false);
  await expect(panel.locator(".pg-reasoning-content")).toContainText("分析阶段 12");
  await page.waitForTimeout(70);
  await page.screenshot({ path: info.outputPath("reasoning-expanding-midframe.png"), animations: "allow" });
  await page.screenshot({ path: info.outputPath("reasoning-expanded.png"), animations: "disabled" });
  expect(mock.errors).toEqual([]);
});

test("连续反向点击从当前高度继续，最后一次操作决定最终状态", async ({ page }, info) => {
  const mock = await historyFixture(page);
  const result = await page.evaluate(async () => {
    const panel = document.querySelector<HTMLElement>(".pg-reasoning");
    const toggle = panel?.querySelector<HTMLButtonElement>(".pg-reasoning-toggle");
    if (!panel || !toggle) throw new Error("Missing reasoning toggle");
    const rows: { t: number; height: number }[] = [];
    const reversals: { before: number; after: number }[] = [];
    const started = performance.now();
    const initial = panel.getBoundingClientRect().height;
    toggle.click();
    for (const at of [70, 120, 190, 245]) setTimeout(() => {
      const before = panel.getBoundingClientRect().height;
      toggle.click();
      reversals.push({ before, after: panel.getBoundingClientRect().height });
    }, at);
    await new Promise<void>((resolve) => {
      function frame(now: number) {
        if (!panel) return resolve();
        rows.push({ t: now - started, height: panel.getBoundingClientRect().height });
        if (now - started < 620) requestAnimationFrame(frame); else resolve();
      }
      requestAnimationFrame(frame);
    });
    return { initial, rows, reversals, expanded: toggle.getAttribute("aria-expanded"), final: panel.getBoundingClientRect().height };
  });
  expect(result.reversals).toHaveLength(4);
  expect(result.reversals.every((row) => Math.abs(row.before - row.after) < 1)).toBe(true);
  expect(result.expanded).toBe("true");
  expect(result.final).toBeGreaterThan(result.initial + 150);
  expect(result.rows.every((row) => row.height >= result.initial - 1 && row.height <= result.final + 1)).toBe(true);
  await writeFile(info.outputPath("reasoning-reversal-frames.json"), JSON.stringify(result, null, 2));
  expect(mock.errors).toEqual([]);
});

test("窄屏键盘开合可用，运行中切换减少动态效果立即到达正确终态", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await historyFixture(page);
  const panel = page.getByRole("region", { name: "思考过程", exact: true });
  const toggle = panel.getByRole("button", { name: "思考过程", exact: true });
  const content = panel.locator(".pg-reasoning-collapse");
  const initial = await panel.evaluate((element) => element.getBoundingClientRect().height);
  await toggle.focus();
  await toggle.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(content).toHaveCSS("transition-property", "none");
  await expect(content).toHaveCSS("visibility", "visible");
  await expect(content).toHaveJSProperty("inert", false);
  await toggle.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(content).toHaveCSS("visibility", "hidden");
  await expect(content).toHaveJSProperty("inert", true);
  expect(await panel.evaluate((element) => element.getBoundingClientRect().height)).toBeCloseTo(initial, 0);
  await expect(toggle).toBeFocused();
  await toggle.press("Enter");
  await expect(content).toHaveAttribute("aria-hidden", "false");
  await page.screenshot({ path: info.outputPath("reasoning-mobile-reduced-motion.png"), animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(mock.errors).toEqual([]);
});

test("折叠期间接收思考增量不会自动展开，再次展开可看到完整新内容", async ({ page }) => {
  const mock = await fixture(page);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  const first = "最初的分析内容。";
  const added = "新增思考片段。".repeat(100);
  let releaseUpdate: () => void = () => {};
  let releaseEnd: () => void = () => {};
  const update = new Promise<void>((resolve) => { releaseUpdate = resolve; });
  const end = new Promise<void>((resolve) => { releaseEnd = resolve; });
  let reads = 0;
  await page.route("**/api/v2/tenants/team-a/sessions/history", (route) => route.fulfill({ json: { ...history, messages: [user], reasoning: [], runs: [{ ...run, status: "running", finished_at: null }] } }));
  await page.route("**/api/v2/tenants/team-a/runs/run-motion/ag-ui", async (route) => {
    const attempt = ++reads;
    if (attempt === 2) await update;
    if (attempt > 2) await end;
    const id = `reasoning:${run.id}:call-1`;
    const events = [
      { type: "RUN_STARTED", threadId: "history", runId: run.id, protocolVersion: "1.0" },
      { type: "STATE_SNAPSHOT", snapshot: { run: { ...run, status: "running", finished_at: null } } },
      { type: "MESSAGES_SNAPSHOT", messages: [user, { id, role: "reasoning", content: first, metadata: { platform: { reasoning_status: "streaming" } } }] },
    ].map((event) => `data: ${JSON.stringify(event)}\n\n`);
    if (attempt > 1) events.push(`id: 2.0\ndata: ${JSON.stringify({ type: "REASONING_MESSAGE_CONTENT", messageId: id, delta: added })}\n\n`);
    await route.fulfill({ contentType: "text/event-stream", headers: { "X-Run-ID": run.id, "X-AG-UI-Protocol-Version": "1.0" }, body: events.join("") });
  });
  try {
    await page.goto("/#playground?session=history&tenant=team-a");
    const toggle = page.getByRole("button", { name: "正在思考", exact: true });
    const panel = page.getByRole("region", { name: "正在思考", exact: true });
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(panel.locator(".pg-reasoning-content")).toHaveText(first);
    await toggle.click();
    releaseUpdate();
    await expect(panel.locator(".pg-reasoning-content")).toContainText(added);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(panel.locator(".pg-reasoning-collapse")).toHaveJSProperty("inert", true);
    await toggle.click();
    await expect(panel.locator(".pg-reasoning-content")).toContainText(first + added);
    await expect(panel.locator(".pg-reasoning-content")).toBeVisible();
    await expect(panel.locator(".pg-reasoning-collapse")).toHaveJSProperty("inert", false);
    await page.getByRole("button", { name: "新建对话", exact: true }).click();
  } finally {
    releaseUpdate();
    releaseEnd();
  }
  expect(mock.errors).toEqual([]);
});
