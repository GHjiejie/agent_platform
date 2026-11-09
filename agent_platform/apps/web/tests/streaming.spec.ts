import { createServer, type ServerResponse } from "node:http";
import { expect, test } from "@playwright/test";
import { fixture } from "./ui-fixture";

test("运行结束前逐段显示真实 HTTP 流中的文字，完成后完整交接历史回复", async ({ page }) => {
  const mock = await fixture(page);
  const created = "2026-11-05T10:00:00Z";
  const parts = ["第一段已到达。", "第二段继续输出🌏。", "第三段完整结束。"];
  const answer = parts.join("");
  const run = { id: "paced-run", session_id: "new-session", tenant_id: "team-a", user_id: "user", agent_name: "客服回复助手", status: "running", created_at: created, finished_at: null, error: "", model_id: "luna", model_alias: "gpt-6-luna" };
  let response: ServerResponse | undefined;
  let wireRunId = "";
  let complete = false;
  const send = (event: unknown) => {
    if (!response) throw new Error("SSE connection has not opened");
    response.write(`data: ${JSON.stringify(event)}\n\n`);
  };
  const server = createServer((request, stream) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk: string) => { body += chunk; });
    request.on("end", () => {
      const input: { runId: string } = JSON.parse(body);
      wireRunId = input.runId;
      response = stream;
      stream.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Run-ID": run.id, "X-AG-UI-Protocol-Version": "1.0", "Access-Control-Allow-Origin": request.headers.origin ?? "*", "Access-Control-Allow-Credentials": "true" });
      send({ type: "RUN_STARTED", threadId: "new-session", runId: wireRunId, protocolVersion: "1.0" });
      send({ type: "STATE_SNAPSHOT", snapshot: { run } });
      send({ type: "TEXT_MESSAGE_START", messageId: `assistant:${run.id}:call`, role: "assistant" });
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Missing streaming fixture address");
  await page.route("**/api/v2/tenants/team-a/sessions/new-session/ag-ui", (route) => route.continue({ url: `http://127.0.0.1:${address.port}/stream` }));
  await page.route("**/api/v2/tenants/team-a/sessions/new-session", (route) => route.fulfill({ json: {
    id: "new-session", tenant_id: "team-a", user_id: "user", agent_id: "assistant", title: "逐段输出", created_at: created,
    messages: complete ? [{ id: "paced-final", run_id: run.id, role: "assistant", content: answer, created_at: created }] : [],
    runs: complete ? [{ ...run, status: "succeeded", finished_at: created }] : [], artifacts: [], reasoning: [],
  } }));
  try {
    await page.goto("/#playground?tenant=team-a");
    const input = page.getByRole("textbox", { name: "发送给 Agent 的消息", exact: true });
    await expect(input).toBeEnabled();
    await input.fill("检查逐段输出");
    await input.press("Enter");
    await expect.poll(() => Boolean(response)).toBe(true);
    const live = page.getByRole("article", { name: "Agent 正在回复", exact: true });
    for (let index = 0; index < parts.length; index++) {
      send({ type: "TEXT_MESSAGE_CONTENT", messageId: `assistant:${run.id}:call`, delta: parts[index] });
      await expect(live.locator(".pg-message-text")).toHaveText(parts.slice(0, index + 1).join(""));
      await expect(page.getByRole("button", { name: "停止生成", exact: true })).toBeVisible();
      if (index < parts.length - 1) await expect(live).not.toContainText(parts[index + 1]);
    }
    complete = true;
    send({ type: "TEXT_MESSAGE_END", messageId: `assistant:${run.id}:call` });
    send({ type: "RUN_FINISHED", threadId: "new-session", runId: wireRunId, outcome: { type: "success" } });
    response?.end();
    const saved = page.getByRole("article", { name: "Agent 的回复", exact: true });
    await expect(saved.locator(".pg-message-text")).toHaveText(answer);
    await expect(saved).toHaveCount(1);
    await expect(page.getByRole("button", { name: "停止生成", exact: true })).toHaveCount(0);
    expect(mock.errors).toEqual([]);
  } finally {
    response?.end();
    server.closeAllConnections();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
