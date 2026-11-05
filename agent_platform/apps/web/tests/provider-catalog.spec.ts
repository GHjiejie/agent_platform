import { expect, test, type Page } from "@playwright/test";
import { fixture } from "./ui-fixture";

async function catalogFixture(page: Page, options: { admin?: boolean; failed?: boolean; connected?: boolean } = {}) {
  const base = await fixture(page);
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const model = { id: "vendor/model-from-api", name: "服务商实时模型", owned_by: "Vendor", context_window: 32768, max_output_tokens: 4096 };
  let provider = { id: "provider-one", name: "我的服务商", api_base: "https://provider.example.test/v1", catalog: options.connected ? [model] : [], catalog_updated_at: options.connected ? "2026-11-05T02:00:00Z" : null, created_at: "2026-11-05T02:00:00Z" };
  let exists = !!options.connected;
  let kind = "discover";
  let providerReads = 0;
  await page.route("**/api/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    const json = (value: unknown) => route.fulfill({ json: value });
    if (path === "/api/v2/me" && options.admin !== false) return json({ user: { id: "user", name: "管理员", email: "ui@example.test", active: true, global_status: "active", auth_version: 1, created_at: "2026-11-05T02:00:00Z", memberships: [{ id: "member-a", tenant_id: "team-a", tenant_name: "我的组织", role: "owner", status: "active", tenant_status: "active" }], capabilities: ["platform.models.manage", "platform.pricing.manage", "platform.gateway.manage"], platform_roles: ["platform_admin", "platform_finance"] }, csrf_token: "ui-csrf" });
    if (path === "/api/v2/platform/providers" && method === "GET") { providerReads++; return json({ items: exists ? [provider] : [] }); }
    if (path === "/api/v2/platform/providers" && method === "POST") { writes.push({ path, body: request.postDataJSON() }); exists = true; return json(provider); }
    if (path.endsWith("/discover") || path.endsWith("/imports")) {
      kind = path.endsWith("/discover") ? "discover" : "import";
      writes.push({ path, body: request.postDataJSON() ?? {} });
      return json({ id: `op-${kind}`, provider_id: provider.id, kind, status: "queued", attempts: 0, error_code: "", result: {}, updated_at: provider.created_at });
    }
    if (path.startsWith("/api/v2/platform/provider-operations/")) {
      if (!options.failed && kind === "discover") provider = { ...provider, catalog: [model], catalog_updated_at: provider.created_at };
      return json({ id: `op-${kind}`, provider_id: provider.id, kind, status: options.failed ? "failed" : "succeeded", attempts: 1, error_code: options.failed ? "provider_auth_failed" : "", result: kind === "discover" ? { model_count: 1 } : { deployment_id: "deployment-from-api", internal_route: "provider-generated-route" }, updated_at: provider.created_at });
    }
    if (path === "/api/v2/platform/token-defaults") return json({ context_window: 1048576, output_tokens: 131072 });
    if (path === "/api/v2/platform/tenants/team-a/models" && method === "POST" || path === "/api/v2/platform/tenants/team-a/gateway/enroll") { writes.push({ path, body: request.postDataJSON() ?? {} }); return json({ id: "granted-model" }); }
    return route.fallback();
  });
  return { ...base, writes, reads: () => providerReads };
}

test("从空连接创建服务商、获取模型、接入并填写报价授权", async ({ page }) => {
  const mock = await catalogFixture(page);
  await page.goto("/#models?tenant=team-a");
  await page.getByRole("button", { name: "获取服务商模型", exact: true }).click();
  await expect(page.getByText("尚未连接服务商", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "添加服务商", exact: true }).click();
  await page.getByRole("textbox", { name: "服务商名称", exact: true }).fill("我的服务商");
  await page.getByRole("textbox", { name: "API 地址", exact: true }).fill("https://provider.example.test/v1");
  await page.getByLabel("API Key", { exact: true }).fill("mock-upstream-secret");
  await page.getByRole("button", { name: "保存并获取模型", exact: true }).click();
  await expect(page.getByText("服务商实时模型", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "接入模型", exact: true }).click();
  await expect(page.getByRole("heading", { name: "确认报价并授权给当前组织", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "输入价格（USD / 百万 Token）", exact: true }).fill("0.3");
  await page.getByRole("textbox", { name: "输出价格（USD / 百万 Token）", exact: true }).fill("1.2");
  await page.getByRole("button", { name: "授权给当前组织", exact: true }).click();
  await expect(page.getByText("模型已授权，正在同步组织网关权限", { exact: true })).toBeVisible();
  const grant = mock.writes.find(item => item.path.endsWith("/team-a/models"));
  expect(grant?.body).toMatchObject({ deployment_id: "deployment-from-api", alias: "vendor/model-from-api-deployment-f", input_price: "0.3", output_price: "1.2", context_window: 32768, max_output_tokens: 4096 });
  expect(mock.writes.some(item => item.path.endsWith("/gateway/enroll"))).toBeTruthy();
  await page.getByRole("button", { name: "接入模型", exact: true }).click();
  await expect(page.getByRole("heading", { name: "确认报价并授权给当前组织", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  expect(mock.writes.filter(item => item.path.endsWith("/team-a/models"))).toHaveLength(1);
  expect(mock.errors).toEqual([]);
});

test("获取失败保留服务商并提供重新获取", async ({ page }) => {
  await catalogFixture(page, { connected: true, failed: true });
  await page.goto("/#models?tenant=team-a");
  await page.getByRole("button", { name: "获取服务商模型", exact: true }).click();
  await page.getByRole("button", { name: "获取模型", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("服务商拒绝了 API Key");
  await expect(page.getByRole("button", { name: "获取模型", exact: true })).toBeEnabled();
  await expect(page.getByRole("textbox", { name: "搜索服务商模型", exact: true })).toBeVisible();
});

test("普通组织成员不会看到服务商管理入口或请求平台连接", async ({ page }) => {
  const mock = await catalogFixture(page, { admin: false });
  await page.goto("/#models?tenant=team-a");
  await expect(page.getByRole("heading", { name: "模型目录", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "获取服务商模型", exact: true })).toHaveCount(0);
  expect(mock.reads()).toBe(0);
});

test("窄屏可搜索模型并展示无匹配状态", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const mock = await catalogFixture(page, { connected: true });
  await page.goto("/#models?tenant=team-a");
  await page.getByRole("button", { name: "获取服务商模型", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索服务商模型", exact: true }).fill("not-found");
  await expect(page.getByText("没有匹配的模型", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: info.outputPath("provider-mobile.png"), fullPage: true });
  expect(mock.errors).toEqual([]);
});
