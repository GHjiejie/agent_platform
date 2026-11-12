import { expect, test } from "@playwright/test";
import { fixture } from "./ui-fixture";

test("个人密钥创建仅展示一次、模型范围、示例与撤销", async ({ page }, info) => {
  const mock = await fixture(page);
  const secret = "ap_test-credential-only-visible-at-creation";
  let key: null | { id: string; user_id: string; name: string; prefix: string; model_ids: string[]; created_at: string; expires_at: string; revoked_at: string | null; last_used_at: null } = null;
  let created: Record<string, unknown> | undefined;
  await page.route("**/api/v2/tenants/team-a/api-keys**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      created = request.postDataJSON();
      key = { id: "key1", user_id: "user", name: String(created?.name), prefix: "ap_key1…only", model_ids: created?.model_ids as string[], created_at: "2026-11-12T00:00:00Z", expires_at: "2027-02-10T00:00:00Z", revoked_at: null, last_used_at: null };
      return route.fulfill({ status: 201, json: { ...key, secret } });
    }
    if (request.method() === "DELETE" && key) key.revoked_at = "2026-11-12T01:00:00Z";
    return route.fulfill({ json: request.method() === "DELETE" ? { id: "key1", revoked: true } : { items: key ? [key] : [] } });
  });
  await page.goto("/#api-keys?tenant=team-a");
  await expect(page.getByText("还没有 API 密钥", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "创建密钥", exact: true }).click();
  await page.getByRole("textbox", { name: "密钥名称", exact: true }).fill("开发脚本");
  await page.getByRole("checkbox", { name: "GPT-6 Luna", exact: false }).check();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByLabel("新创建的 API 密钥")).toHaveText(secret);
  expect(created).toMatchObject({ name: "开发脚本", expires_in_days: 90, model_ids: ["luna"] });
  await page.getByRole("button", { name: "已保存，关闭", exact: true }).click();
  await expect(page.getByText(secret, { exact: true })).toHaveCount(0);
  await expect(page.locator("tbody tr")).toContainText("1 个指定模型");
  await expect(page.locator(".api-key-example")).toContainText("client.responses.create");
  await page.getByRole("combobox", { name: "调用示例接口" }).click();
  await page.getByRole("option", { name: "Chat Completions（流式）" }).click();
  await expect(page.locator(".api-key-example")).toContainText("stream=True");
  await page.screenshot({ path: info.outputPath("api-keys-desktop.png"), fullPage: true, animations: "disabled" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("heading", { name: "API 密钥", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("api-keys-mobile.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "撤销密钥：开发脚本", exact: true }).click();
  await page.getByRole("button", { name: "确认撤销", exact: true }).click();
  await expect(page.locator("tbody tr")).toContainText("已撤销");
  await page.reload();
  await expect(page.getByText(secret, { exact: true })).toHaveCount(0);
  expect(mock.errors).toEqual([]);
});
