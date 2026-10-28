import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function session(page: Page, role: "admin" | "member" = "admin") {
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        user: {
          id: "gateway-ui-test",
          email: "gateway@example.test",
          name: "Gateway test",
          role,
          tenant_id: "gateway-ui-tenant",
          active: true,
        },
        csrf_token: "test-csrf",
      },
    }),
  );
}

test("管理员入口使用服务端地址，在独立标签页打开且不传递凭据", async ({
  page,
}) => {
  await session(page);
  const address = "http://127.0.0.1:49117/ui";
  await page.route("**/api/v1/gateway", (route) =>
    route.fulfill({ json: { configured: true, admin_url: address } }),
  );
  await page.goto("/#gateway");
  await expect(
    page.getByRole("link", { name: "LiteLLM 网关", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("管理入口已配置", { exact: true })).toBeVisible();
  const link = page.getByRole("link", { name: "打开 LiteLLM 管理台" });
  await expect(link).toHaveAttribute("href", address);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(page.locator("iframe")).toHaveCount(0);
});

test("入口未配置时给出明确状态，刷新后可打开新地址", async ({ page }) => {
  await session(page);
  let configured = false;
  await page.route("**/api/v1/gateway", (route) =>
    route.fulfill({
      json: {
        configured,
        admin_url: configured ? "https://gateway.example.test/ui" : "",
      },
    }),
  );
  await page.goto("/#gateway");
  await expect(page.getByText("尚未配置 LiteLLM 管理入口")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "打开 LiteLLM 管理台" }),
  ).toHaveCount(0);
  configured = true;
  await page.getByRole("button", { name: "刷新配置" }).click();
  await expect(
    page.getByRole("link", { name: "打开 LiteLLM 管理台" }),
  ).toHaveAttribute("href", "https://gateway.example.test/ui");
});

test("获取入口失败时可以重试", async ({ page }) => {
  await session(page);
  let fail = true;
  await page.route("**/api/v1/gateway", (route) =>
    route.fulfill(
      fail
        ? { status: 503, json: { error: { message: "管理入口暂时不可用" } } }
        : { json: { configured: true, admin_url: "http://localhost:4002/ui" } },
    ),
  );
  await page.goto("/#gateway");
  await expect(page.getByRole("alert")).toContainText("管理入口暂时不可用");
  fail = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "打开 LiteLLM 管理台" }),
  ).toBeVisible();
});

test("普通成员看不到入口，直接访问页面也不会获取管理地址", async ({ page }) => {
  await session(page, "member");
  let gatewayRequests = 0;
  await page.route("**/api/v1/gateway", (route) => {
    gatewayRequests++;
    return route.fulfill({ status: 403, json: { error: { message: "禁止访问" } } });
  });
  await page.route("**/api/v1/dashboard", (route) =>
    route.fulfill({
      json: {
        balance: "0",
        reserved: "0",
        available: "0",
        currency: "USD",
        requests: 0,
        tokens: 0,
        cost: "0",
        success_rate: 0,
        active_runs: 0,
        daily: [],
        models: [],
        gateway_configured: true,
      },
    }),
  );
  await page.route("**/api/v1/runs", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.goto("/#gateway");
  await expect(page.getByRole("heading", { name: /你好/ })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "LiteLLM 网关", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "打开 LiteLLM 管理台" }),
  ).toHaveCount(0);
  expect(gatewayRequests).toBe(0);
});
