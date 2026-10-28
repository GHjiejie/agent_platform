import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// Isolated browser coverage; API behavior is covered by test_api.py.
async function loginFixture(page: Page) {
  const requests: { email: string; password: string }[] = [];
  const errors: string[] = [];
  let signedIn = false;
  const user = {
    id: "registered-user",
    email: "new-user@example.test",
    name: "new-user",
    active: true,
    global_status: "active",
    auth_version: 1,
    created_at: "2026-10-28T00:00:00Z",
    memberships: [],
    platform_roles: [],
    capabilities: [],
  };
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/login" && request.method() === "POST") {
      const body = request.postDataJSON() as { email: string; password: string };
      requests.push(body);
      if (body.password === "Wrong-password-42") {
        await route.fulfill({ status: 401, json: { error: { code: "invalid_credentials", message: "邮箱或密码不正确。" } } });
        return;
      }
      signedIn = true;
      await route.fulfill({ json: { user, csrf_token: "mock-registration-csrf" } });
      return;
    }
    if (path === "/api/v2/me") {
      await route.fulfill(signedIn
        ? { json: { user, csrf_token: "mock-registration-csrf" } }
        : { status: 401, json: { error: { code: "unauthorized", message: "请先登录。" } } });
      return;
    }
    errors.push(`Unexpected request: ${request.method()} ${path}`);
    await route.fulfill({ status: 501, json: { error: { message: "Unexpected mock route" } } });
  });
  return { requests, errors };
}

test("统一登录入口说明自动注册，错误后可重试并保留原邀请", async ({ page }, info) => {
  const mock = await loginFixture(page);
  const token = "mock-invitation-token-for-registration";
  await page.goto(`/#invite?token=${token}`);
  await expect(page.getByRole("heading", { name: "登录或注册" })).toBeVisible();
  await expect(page.getByText("未注册的邮箱将自动创建账号并登录。", { exact: false })).toBeVisible();
  await page.screenshot({ path: info.outputPath("login-registration-desktop.png"), fullPage: true });
  await page.getByRole("textbox", { name: "工作邮箱" }).fill("new-user@example.test");
  const password = page.getByLabel("密码", { exact: true });
  await password.fill("Wrong-password-42");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("alert")).toContainText("邮箱或密码不正确。");
  await expect(password).toHaveValue("Wrong-password-42");
  await password.fill("  Registration password with spaces  ");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("heading", { name: "接受组织邀请", exact: true })).toBeVisible();
  await expect(page.getByLabel("邀请令牌或链接", { exact: true })).toHaveValue(token);
  await expect(page.getByText("你还没有加入组织", { exact: true })).toBeVisible();
  expect(mock.requests.at(-1)).toEqual({ email: "new-user@example.test", password: "  Registration password with spaces  " });
  expect(mock.errors).toEqual([]);
});

test("移动端新用户登录后进入无组织状态，账号与邀请入口可用", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await loginFixture(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录或注册" })).toBeVisible();
  await expect(page.getByText("首次使用请设置至少 12 位密码，已有账号请输入原密码。", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("login-registration-mobile.png"), fullPage: true });
  await page.getByRole("textbox", { name: "工作邮箱" }).fill("new-user@example.test");
  await page.getByLabel("密码", { exact: true }).fill("Registration-password-42");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("heading", { name: "接受组织邀请", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "账号设置", exact: true })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "切换组织", exact: true })).toBeDisabled();
  expect(mock.errors).toEqual([]);
});
