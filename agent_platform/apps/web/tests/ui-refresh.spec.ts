import { fixture } from "./ui-fixture";
import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";

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

test.describe("流式输出体验", () => {
  test("Markdown 在流式阶段和恢复历史后保持相同结构", async ({ page }, info) => {
    const mock = await fixture(page, { controlledStream: true });
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
    await input.fill("# 这是用户原始输入 **保留标记**");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    await mock.emit({ type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" });
    const first = "## Markdown 流式回复\n\n这是 **加粗";
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: first });
    await expect(page.getByRole("heading", { level: 2, name: "Markdown 流式回复" })).toBeVisible();
    await expect(page.locator(".pg-markdown strong")).toHaveText("加粗");
    const next = '内容**和 `行内代码`。\n\n- 列表项一\n- 列表项二\n\n| 字段 | 状态 |\n| --- | --- |\n| 通知 | 连续 |\n\n```python\nprint("你好")';
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: next });
    await expect(page.locator(".pg-markdown strong")).toHaveText("加粗内容");
    await expect(page.locator(".pg-markdown ul li")).toHaveCount(2);
    await expect(page.getByRole("columnheader")).toHaveCount(2);
    await expect(page.locator(".pg-markdown pre code")).toContainText('print("你好")');
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    await page.screenshot({ path: info.outputPath("markdown-streaming.png") });
    const last = "\n```\n\n[项目链接](https://example.com)";
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: last });
    await mock.finish(first + next + last);
    await expect(page.getByRole("button", { name: "复制回复" })).toBeVisible();
    await expect(page.getByRole("link", { name: "项目链接" })).toHaveAttribute("href", "https://example.com");
    await expect(page.getByRole("article", { name: "你的消息" })).toContainText("# 这是用户原始输入 **保留标记**");
    await page.reload();
    await expect(page.getByRole("heading", { level: 2, name: "Markdown 流式回复" })).toBeVisible();
    await expect(page.locator(".pg-markdown strong")).toHaveText("加粗内容");
    await expect(page.locator(".pg-markdown pre code")).toContainText('print("你好")');
    await page.setViewportSize({ width: 390, height: 700 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath("markdown-history-mobile.png") });
    expect(mock.errors).toEqual([]);
  });

  test("Markdown 不执行原始 HTML 或危险链接，也不自动加载外部图片", async ({ page }) => {
    const mock = await fixture(page, { controlledStream: true });
    const imageRequests: string[] = [];
    page.on("request", (request) => { if (request.url().startsWith("https://tracking.example/")) imageRequests.push(request.url()); });
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
    await input.fill("检查 Markdown 安全边界");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    await mock.emit({ type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" });
    const text = '## 安全渲染\n\n<script>window.markdownExecuted = true</script>\n\n<img src="x" onerror="window.markdownExecuted = true">\n\n[危险链接](javascript:alert(1))\n\n![图片说明](https://tracking.example/image)\n\n```html\n<script>只展示代码</script>\n```';
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: text });
    await mock.finish(text);
    await expect(page.getByRole("button", { name: "复制回复" })).toBeVisible();
    await expect(page.locator(".pg-markdown script, .pg-markdown img, .pg-markdown [onerror], .pg-markdown a[href^='javascript:']")).toHaveCount(0);
    await expect(page.locator(".pg-markdown pre code")).toHaveText("<script>只展示代码</script>\n");
    await expect(page.getByRole("link", { name: "图片说明" })).toHaveAttribute("href", "https://tracking.example/image");
    expect(await page.evaluate(() => "markdownExecuted" in window)).toBe(false);
    expect(imageRequests).toEqual([]);
    expect(mock.errors).toEqual([]);
  });

  test("思考连续增长、折叠和视口缩小时保持触底", async ({ page }, info) => {
    const mock = await fixture(page, { controlledStream: true, longHistory: true });
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
    await input.fill("验证长思考的滚动跟随");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    const viewport = page.locator(".pg-messages");
    const gap = () => viewport.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
    const samples: number[] = [];
    await mock.emit({ type: "REASONING_MESSAGE_START", messageId: "reasoning:run-ui:call-1", role: "reasoning" });
    let reasoning = "这是一段用于复现视频中长思考输出的内容，包含分析步骤和代码说明。\n".repeat(1200);
    await mock.emit({ type: "REASONING_MESSAGE_CONTENT", messageId: "reasoning:run-ui:call-1", delta: reasoning });
    await expect(page.locator(".pg-reasoning-content")).toHaveText(reasoning);
    const frameSamples = page.evaluate(async () => {
      const intervals: number[] = [];
      let previous = performance.now();
      const started = previous;
      await new Promise<void>((resolve) => {
        const sample = (now: number) => {
          intervals.push(now - previous);
          previous = now;
          if (now - started < 1200) requestAnimationFrame(sample);
          else resolve();
        };
        requestAnimationFrame(sample);
      });
      return intervals;
    });
    for (let index = 0; index < 8; index++) {
      const delta = `第 ${index + 1} 批思考。\n${"分析条件与实现步骤。\n".repeat(40)}`;
      reasoning += delta;
      await mock.emit({ type: "REASONING_MESSAGE_CONTENT", messageId: "reasoning:run-ui:call-1", delta });
      await expect(page.locator(".pg-reasoning-content")).toHaveText(reasoning);
      await expect.poll(gap).toBeLessThan(4);
      samples.push(await gap());
    }
    const toggle = page.getByRole("button", { name: "正在思考" });
    // Trigger the offscreen toggle directly to isolate the fold's layout change.
    await toggle.dispatchEvent("click");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.dispatchEvent("click");
    await expect.poll(gap).toBeLessThan(4);
    await page.setViewportSize({ width: 390, height: 640 });
    await expect.poll(gap).toBeLessThan(4);
    await expect(page.getByRole("button", { name: "回到最新消息" })).toHaveCount(0);
    await mock.emit({ type: "REASONING_END", messageId: "reasoning:run-ui:call-1" });
    await mock.emit({ type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" });
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "长思考结束，继续正常回复。" });
    await mock.finish("长思考结束，继续正常回复。");
    await expect(page.getByRole("article", { name: "Agent 的回复", exact: true }).filter({ hasText: "长思考结束，继续正常回复。" }).getByRole("button", { name: "复制回复" })).toBeVisible();
    await expect.poll(gap).toBeLessThan(4);
    await page.screenshot({ path: info.outputPath("streaming-mobile-follow.png") });
    await writeFile(info.outputPath("bottom-gap-samples.json"), JSON.stringify(samples));
    await writeFile(info.outputPath("streaming-frame-intervals.json"), JSON.stringify(await frameSamples));
    expect(mock.errors).toEqual([]);
  });

  test("上翻阅读时不抢滚动，回到底部后继续跟随", async ({ page }, info) => {
    const mock = await fixture(page, { controlledStream: true, longHistory: true });
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
    await input.fill("验证阅读位置");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    const viewport = page.locator(".pg-messages");
    const gap = () => viewport.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
    await mock.emit({ type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" });
    const first = "第一段回复，保持连续输出。\n".repeat(50);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: first });
    await expect.poll(gap).toBeLessThan(4);
    await viewport.hover();
    await page.mouse.wheel(0, -480);
    await expect(page.getByRole("button", { name: "回到最新消息" })).toBeVisible();
    // Wait for the wheel gesture itself to settle before measuring reading position.
    await page.waitForTimeout(150);
    const readingTop = await viewport.evaluate((element) => element.scrollTop);
    const next = "新增内容不能把正在阅读的用户拉走。\n".repeat(70);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: next });
    await expect(page.locator(".pg-message.assistant").last()).toContainText(next.trim());
    expect(Math.abs(await viewport.evaluate((element) => element.scrollTop) - readingTop)).toBeLessThan(4);
    await page.screenshot({ path: info.outputPath("streaming-reading-position.png") });
    await page.getByRole("button", { name: "回到最新消息" }).click();
    await expect.poll(gap).toBeLessThan(4);
    const last = "恢复跟随后的最新内容。\n".repeat(30);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: last });
    await expect.poll(gap).toBeLessThan(4);
    await viewport.focus();
    await page.keyboard.press("PageUp");
    await expect(page.getByRole("button", { name: "回到最新消息" })).toBeVisible();
    await page.keyboard.press("End");
    await expect.poll(gap).toBeLessThan(4);
    await expect(page.getByRole("button", { name: "回到最新消息" })).toHaveCount(0);
    await mock.finish(first + next + last);
    await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
    await expect.poll(gap).toBeLessThan(4);
    expect(mock.errors).toEqual([]);
  });

  test("分批文本逐帧呈现，完整保留表情并即时响应减少动效", async ({ page }, info) => {
    const mock = await fixture(page, { controlledStream: true });
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
    await input.fill("验证流式呈现");
    await input.press("Enter");
    await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
    await mock.emit({ type: "TEXT_MESSAGE_START", messageId: "assistant:run-ui:call-1", role: "assistant" });
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: "回复：" });
    const reply = page.locator(".pg-message.assistant .pg-message-text").last();
    await expect(reply).toHaveText("回复：");
    await reply.evaluate((element) => {
      element.setAttribute("data-updates", "[]");
      new MutationObserver(() => {
        const updates = JSON.parse(element.getAttribute("data-updates") ?? "[]") as string[];
        updates.push(element.textContent ?? "");
        element.setAttribute("data-updates", JSON.stringify(updates));
      }).observe(element, { childList: true, characterData: true, subtree: true });
    });
    const delta = "连续文字👩🏽‍💻🌏é，".repeat(40);
    const full = "回复：" + delta;
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta });
    await expect(reply).toHaveText(full);
    const updates = await reply.evaluate((element) => JSON.parse(element.getAttribute("data-updates") ?? "[]") as string[]);
    const partial = updates.filter((value) => value.length > 3 && value.length < full.length);
    expect(partial.length).toBeGreaterThan(1);
    const boundaries = new Set([0, ...Array.from(new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(full), (part) => part.index + part.segment.length)]);
    for (const value of updates) {
      expect(full.startsWith(value)).toBe(true);
      expect(boundaries.has(value.length)).toBe(true);
    }
    const interrupted = "偏好切换前正在展开的文本。".repeat(200);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: interrupted });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect(reply).toHaveText(full + interrupted);
    const tail = "减少动效时立即显示完整新增内容。".repeat(30);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: tail });
    await expect(reply).toHaveText(full + interrupted + tail);
    // End with animation enabled and a pending batch: terminal events must
    // immediately expose all accepted text, even when paint frames are paused.
    await page.emulateMedia({ reducedMotion: "no-preference" });
    const final = "结束前的最后一批内容。".repeat(100);
    await mock.emit({ type: "TEXT_MESSAGE_CONTENT", messageId: "assistant:run-ui:call-1", delta: final });
    await page.evaluate(() => { window.requestAnimationFrame = () => 1; window.cancelAnimationFrame = () => {}; });
    await mock.finish(full + interrupted + tail + final);
    await expect(page.getByRole("button", { name: "复制回复" })).toBeVisible();
    await expect(page.getByText(full + interrupted + tail + final, { exact: true })).toHaveCount(1);
    await writeFile(info.outputPath("streaming-reveal-samples.json"), JSON.stringify(updates));
    expect(mock.errors).toEqual([]);
  });
});

test("我的 Agent 只展示 normal，编辑器不提供已移除的执行方式", async ({ page }, info) => {
  const mock = await fixture(page, { reducedAgents: true });
  await page.goto("/#agents?tenant=team-a");
  await expect(page.locator(".agent-card")).toHaveCount(1);
  await expect(page.locator(".agent-card h2")).toHaveText("normal");
  await expect(page.getByRole("button", { name: "团队协作设置" })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("normal-only-agents.png"), fullPage: true });
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(page.getByText("执行方式", { exact: true })).toHaveCount(0);
  await expect(page.getByText("协作能力（供团队发现和调用）", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: /生成可视化界面/ })).toHaveCount(0);
  expect(mock.errors).toEqual([]);
});

test("新对话只提供 normal，新建和发送消息使用 normal", async ({ page }) => {
  const mock = await fixture(page, { reducedAgents: true });
  await page.goto("/#playground?tenant=team-a");
  const picker = page.getByRole("combobox", { name: "当前 Agent", exact: true });
  await expect(picker).toContainText("normal");
  await picker.click();
  await expect(page.getByRole("listbox", { name: "当前 Agent" }).locator('[role="option"]:not([aria-disabled="true"])')).toHaveCount(1);
  await page.getByRole("option", { name: /^normal/ }).click();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(picker).toContainText("normal");
  const input = page.getByRole("textbox", { name: "发送给 Agent 的消息" });
  await input.fill("普通聊天");
  await input.press("Enter");
  await expect(page.getByText("已收到，我们一起把想法变成清晰的行动。", { exact: true })).toBeVisible();
  expect(mock.writes.find((request) => request.path.endsWith("/sessions"))?.body.agent_id).toBe("normal");
  expect(mock.errors).toEqual([]);
});

test("历史会话使用 normal，失效助手链接回退 normal", async ({ page }) => {
  const mock = await fixture(page, { reducedAgents: true });
  const picker = page.getByRole("combobox", { name: "当前 Agent", exact: true });
  await page.goto("/#playground?tenant=team-a&agent=assistant");
  await expect(picker).toContainText("normal");
  await page.goto("/#playground?tenant=team-a&session=history");
  await expect(picker).toContainText("normal");
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
  await page.getByRole("dialog", { name: "历史对话" }).getByRole("button", { name: "打开对话：产品介绍怎么写？", exact: true }).click();
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
