import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// This suite mutates an explicitly disposable local test database. It never
// connects to a user's browser profile, an upstream model, or payment service.
const password =
  process.env.PLATFORM_E2E_PASSWORD ??
  readFileSync(
    process.env.PLATFORM_E2E_PASSWORD_FILE ??
      resolve("../../.data/browser-test-password.txt"),
    "utf8",
  ).trim();
const email = process.env.PLATFORM_E2E_EMAIL ?? "admin@example.com";

async function login(
  page: Page,
  accountEmail = email,
  accountPassword = password,
) {
  await page.goto("/");
  await page.getByRole("textbox", { name: "工作邮箱" }).fill(accountEmail);
  await page.getByLabel("密码", { exact: true }).fill(accountPassword);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
}

test("管理员可通过真实 API 完成管理闭环，无网关调用明确拒绝", async ({
  page,
}, testInfo) => {
  const suffix = randomUUID().slice(0, 8);
  const modelName = `浏览器验收模型 ${suffix}`;
  const agentName = `浏览器验收 Agent ${suffix}`;
  const memberName = `浏览器测试成员 ${suffix}`;
  const memberEmail = `ui-${suffix}@example.test`;
  const memberPassword = `${randomUUID()}-test`;
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => consoleErrors.push(error.message));
  await login(page);
  await expect(page.getByText("连接模型网关，开启第一次运行")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("dashboard-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });

  await page.getByRole("link", { name: "模型目录", exact: true }).click();
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  const modelDialog = page.getByRole("dialog", { name: "添加模型" });
  await modelDialog.getByLabel("显示名称", { exact: true }).fill(modelName);
  await modelDialog
    .getByLabel("LiteLLM 模型别名", { exact: true })
    .fill(`platform-chat-e2e-${suffix}`);
  await modelDialog.getByLabel(/^输入价格/).fill("1");
  await modelDialog.getByLabel(/^输出价格/).fill("3");
  await modelDialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText(modelName, { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "我的 Agent", exact: true }).click();
  await page.getByRole("button", { name: "创建 Agent", exact: true }).click();
  const agentDialog = page.getByRole("dialog", { name: "创建 Agent" });
  await agentDialog.getByLabel("Agent 名称", { exact: true }).fill(agentName);
  await agentDialog
    .getByLabel("使用模型", { exact: true })
    .selectOption({ label: modelName });
  await agentDialog
    .getByLabel("简短描述", { exact: true })
    .fill("只用于本地 UI 管理闭环验收");
  await agentDialog.getByRole("checkbox", { name: /计算器/ }).check();
  await agentDialog.getByRole("button", { name: "保存草稿" }).click();
  const agentCard = page
    .getByRole("article")
    .filter({
      has: page.getByRole("heading", { name: agentName, exact: true }),
    });
  await expect(agentCard).toBeVisible();
  await agentCard.getByRole("button", { name: "发布", exact: true }).click();
  await expect(
    agentCard.getByText("已发布 · v1", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("agents-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });

  await agentCard.getByRole("button", { name: "运行", exact: true }).click();
  await page
    .getByRole("textbox", { name: "发送给 Agent 的消息" })
    .fill("验证没有网关时明确拒绝请求");
  const rejection = page.waitForResponse(
    (response) =>
      response.url().endsWith("/runs") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const rejected = await rejection;
  expect(rejected.status()).toBe(503);
  expect((await rejected.json()).error.code).toBe("gateway_not_configured");
  await expect(page.getByRole("alert")).toContainText(/请先配置 LiteLLM/);
  await expect(page).toHaveURL(/#playground\?session=/);
  await page.screenshot({
    path: testInfo.outputPath("gateway-unconfigured.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.reload();
  await expect(
    page.getByRole("textbox", { name: "发送给 Agent 的消息" }),
  ).toBeVisible();
  await expect(page.getByRole("textbox", { name: "工作邮箱" })).toHaveCount(0);

  await page.getByRole("link", { name: "成员管理", exact: true }).click();
  await page.getByRole("button", { name: "添加成员", exact: true }).click();
  const memberDialog = page.getByRole("dialog", { name: "添加团队成员" });
  await memberDialog.getByLabel("姓名", { exact: true }).fill(memberName);
  await memberDialog.getByLabel("邮箱", { exact: true }).fill(memberEmail);
  await memberDialog.getByLabel(/^初始密码/).fill(memberPassword);
  await memberDialog
    .getByRole("button", { name: "创建账号", exact: true })
    .click();
  const memberRow = page.getByRole("row").filter({ hasText: memberEmail });
  await expect(memberRow).toBeVisible();
  const memberContext = await page
    .context()
    .browser()!
    .newContext({ baseURL: new URL(page.url()).origin });
  const memberPage = await memberContext.newPage();
  await login(memberPage, memberEmail, memberPassword);
  await expect(
    memberPage.getByRole("link", { name: "成员管理", exact: true }),
  ).toHaveCount(0);
  await expect(
    memberPage.getByRole("link", { name: "配额与限流", exact: true }),
  ).toHaveCount(0);
  await memberPage.getByRole("link", { name: "费用中心", exact: true }).click();
  await expect(
    memberPage.getByText("资金流水由管理员查看", { exact: true }),
  ).toBeVisible();
  await expect(
    memberPage.getByRole("button", { name: "人工入账", exact: true }),
  ).toHaveCount(0);
  expect(
    (await memberPage.request.get("/api/v1/billing/ledger")).status(),
  ).toBe(403);
  await memberRow.getByRole("button", { name: "编辑", exact: true }).click();
  const editMember = page.getByRole("dialog", { name: "编辑成员" });
  await editMember.getByRole("checkbox", { name: "账号启用" }).uncheck();
  await editMember.getByRole("button", { name: "保存修改" }).click();
  await expect(memberRow.getByText("已停用", { exact: true })).toBeVisible();
  await memberPage.reload();
  await expect(
    memberPage.getByRole("textbox", { name: "工作邮箱" }),
  ).toBeVisible();
  await memberContext.close();

  await page.getByRole("link", { name: "费用中心", exact: true }).click();
  await page.getByRole("button", { name: "人工入账", exact: true }).click();
  const creditDialog = page.getByRole("dialog", { name: "人工额度入账" });
  await creditDialog
    .getByLabel("入账金额（USD）", { exact: true })
    .fill("1.25");
  await creditDialog
    .getByLabel("入账说明 / 凭证号", { exact: true })
    .fill(`浏览器验收额度 ${suffix}，不是实际付款`);
  await creditDialog.getByRole("button", { name: "确认入账" }).click();
  await expect(creditDialog).toBeHidden();
  await expect(page.getByRole("row").filter({ hasText: suffix })).toContainText(
    "1.25",
  );
  await expect(
    page.getByText("没有待人工对账的调用", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("billing-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });

  await page.getByRole("link", { name: "配额与限流", exact: true }).click();
  await page
    .getByRole("row")
    .filter({ hasText: "整个工作空间" })
    .getByRole("button", { name: "编辑", exact: true })
    .click();
  const quotaDialog = page.getByRole("dialog", { name: "配置配额策略" });
  await quotaDialog
    .getByLabel("每分钟请求数（RPM）", { exact: true })
    .fill("0");
  await quotaDialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(quotaDialog).toBeHidden();
  await expect(
    page
      .getByRole("row")
      .filter({ hasText: "整个工作空间" })
      .getByRole("cell", { name: "0", exact: true }),
  ).toBeVisible();

  await page.getByRole("link", { name: "审计日志", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "agent.publish", exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "billing.credit", exact: true }).first(),
  ).toBeVisible();
  expect(consoleErrors).toEqual([]);
});

test("390px 窄屏可以打开导航、费用页与会话选择，不横向溢出", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("textbox", { name: "工作邮箱" }).fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("button", { name: "打开导航" })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("dashboard-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("link", { name: "费用中心", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "费用中心", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("billing-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "打开导航" }).click();
  await page.getByRole("link", { name: "对话实验室", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "切换最近会话" }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("playground-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
