import { RunAgentInputSchema } from "@ag-ui/core/schemas";
import type { Page } from "@playwright/test";

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

export async function fixture(page: Page, options: { failRun?: boolean; holdRun?: boolean; longHistory?: boolean; empty?: boolean; dropStream?: boolean; burst?: boolean; delayFinalSnapshot?: boolean } = {}) {
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
    if (resource === "/agents") return json({ items: options.empty ? [] : agents });
    if (resource === "/agents/execution-options") return json({ timeout_seconds: 300, max_cost: "1", max_steps: 50, max_output_tokens: 2000000, default_output_tokens: 131072, default_context_window: 1048576 });
    if (resource === "/token-defaults") return json({ output_tokens: 131072, context_window: 1048576 });
    if (resource === "/models") return json({ items: options.empty ? [] : models });
    if (resource === "/sessions" && method === "POST") { newSession.title = String(request.postDataJSON().title); return json(newSession, 201); }
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
