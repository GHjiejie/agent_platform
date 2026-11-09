import { expect, test } from "@playwright/test";
import type { CollaborationDetail } from "../src/api-contract.generated";

test.skip(process.env.PLATFORM_COLLABORATION_SMOKE !== "1", "Requires the disposable collaboration_smoke_app, never a deployment");

test("真实 API/Worker 团队执行、刷新重连、成果来源与交互图表", async ({ page, context }, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const login = await context.request.post("/api/v1/auth/login", { data: { email: "ag-ui-browser@example.test", password: "temporary-platform-fixture-password" } });
  expect(login.status()).toBe(200);
  const auth = await login.json();
  const tenant = auth.user.memberships.find((item: { role: string }) => item.role === "owner").tenant_id as string;
  const root = `/api/v2/tenants/${tenant}`;
  const agents = await (await context.request.get(`${root}/agents`)).json();
  const team = agents.items.find((item: { execution_mode: string }) => item.execution_mode === "team");
  await page.goto(`/#playground?tenant=${tenant}&agent=${team.id}`);
  await expect(page.getByRole("combobox", { name: "本次协调模型", exact: true })).toBeVisible();
  await page.getByRole("textbox", { name: "发送给 Agent 的消息" }).fill("已提供事实：A=10，B=20。请整理、分析并生成图表。");
  const sent = page.waitForResponse((response) => response.request().method() === "POST" && response.url().endsWith("/ag-ui"));
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const response = await sent;
  const runId = response.headers()["x-run-id"];
  expect(runId).toBeTruthy();
  await expect.poll(async () => {
    const detail: CollaborationDetail = await (await context.request.get(`${root}/runs/${runId}/collaboration`)).json();
    return detail.tasks.filter((task) => task.status === "succeeded").length;
  }).toBeGreaterThanOrEqual(1);
  await page.reload();
  await expect(page.locator(".pg-message-text").filter({ hasText: "协作验收完成：A=10，B=20。" })).toHaveCount(1);
  await expect(page.getByText("仅在成员内部展示的推理。", { exact: true })).toHaveCount(0);
  const detail: CollaborationDetail = await (await context.request.get(`${root}/runs/${runId}/collaboration`)).json();
  expect(detail.status).toBe("succeeded");
  expect(detail.tasks.map((task) => task.status)).toEqual(["succeeded", "succeeded", "succeeded"]);
  expect(detail.executions).toHaveLength(4);
  expect(detail.artifacts).toHaveLength(4);
  const calls = await (await context.request.get(`${root}/usage/calls`)).json();
  expect(calls.items.filter((call: { run_id: string }) => call.run_id === runId)).toHaveLength(5);
  await page.getByRole("button", { name: "查看团队协作过程", exact: true }).click();
  await expect(page.getByRole("region", { name: "团队协作" })).toContainText("已确认");
  const frame = page.frameLocator('iframe[aria-label="生成的交互界面"]');
  await frame.getByRole("button", { name: "增加", exact: true }).click();
  await expect(frame.locator("#count")).toHaveText("1");
  await page.screenshot({ path: info.outputPath("team-live-desktop.png"), fullPage: true });
  expect(errors).toEqual([]);
});
