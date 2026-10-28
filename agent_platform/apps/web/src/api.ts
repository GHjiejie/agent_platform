let csrfToken = "";
let tenantId = "";
let scopeRevision = 0;
let sessionRevision = 0;
const tenantRequests = new Set<AbortController>();
const sessionRequests = new Set<AbortController>();
export const API_ROOT = "/api/v1";
export function setCsrf(token: string) { csrfToken = token; }
export function getTenantId() { return tenantId; }
export function getScopeRevision() { return scopeRevision; }
export function resetSession() {
  sessionRevision++;
  csrfToken = "";
  for (const controller of sessionRequests) controller.abort();
  sessionRequests.clear();
  setTenantId("");
}
export function setTenantId(value: string) {
  if (value === tenantId) return;
  tenantId = value;
  scopeRevision++;
  for (const controller of tenantRequests) controller.abort();
  tenantRequests.clear();
  window.dispatchEvent(new Event("tenant-scope-changed"));
}
export function scopeIsCurrent(revision: number) { return revision === scopeRevision; }
export function apiUrl(path: string, scope = tenantId) {
  if (path.startsWith("/api/")) return path;
  if (path.startsWith("/auth/")) return `${API_ROOT}${path}`;
  if (!scope) throw new ApiError("请先选择一个可访问的组织。", 403, "tenant_required");
  return `/api/v2/tenants/${encodeURIComponent(scope)}${path}`;
}
export function tenantLink(path: string, scope = tenantId) {
  const [route, raw = ""] = path.replace(/^#/, "").split("?");
  const query = new URLSearchParams(raw);
  if (scope) query.set("tenant", scope);
  return `#${route}${query.size ? `?${query}` : ""}`;
}
export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) {
    super(message);
  }
}
export async function api<T>(path: string, options: RequestInit = {}, scope = tenantId): Promise<T> {
  return withApiResponse(path, options, scope, async (response) => response.json().catch(() => null) as Promise<T>);
}
// Keep cookies, CSRF, scope cancellation and access-expiry handling identical
// for JSON APIs and long-lived AG-UI fetch streams. The response remains
// registered until consume() completes, including its streaming body.
export async function withApiResponse<T>(path: string, options: RequestInit, scope: string, consume: (response: Response) => Promise<T>): Promise<T> {
  const scoped = !path.startsWith("/auth/") && !path.startsWith("/api/");
  const revision = scopeRevision;
  const session = sessionRevision;
  if (scoped && scope !== tenantId) throw new ApiError("组织已切换，请重新操作。", 0, "scope_changed");
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  if (scoped) tenantRequests.add(controller);
  sessionRequests.add(controller);
  try {
    const response = await fetch(apiUrl(path, scope), { ...options, signal: controller.signal, headers, credentials: "same-origin" });
    if (controller.signal.aborted || session !== sessionRevision) throw new ApiError("登录状态或组织已变化，请重新操作。", 0, "scope_changed");
    if (scoped && revision !== scopeRevision) throw new ApiError("组织已切换，请重新操作。", 0, "scope_changed");
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      if (response.status === 401 && path !== "/auth/login" && path !== "/api/v2/me") window.dispatchEvent(new Event("auth-expired"));
      if (response.status === 403 && scoped) window.dispatchEvent(new Event("tenant-access-changed"));
      throw new ApiError(body?.error?.message ?? (response.status === 422 ? "请检查表单内容是否符合要求。" : `请求未完成（${response.status}）`), response.status, body?.error?.code ?? "request_failed");
    }
    const result = await consume(response);
    if (controller.signal.aborted || session !== sessionRevision || (scoped && revision !== scopeRevision)) throw new ApiError("登录状态或组织已变化，请重新操作。", 0, "scope_changed");
    return result;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) throw new ApiError("请求已取消或组织已切换。", 0, "scope_changed");
    throw new ApiError("暂时无法连接服务，请检查服务状态后重试。", 0, "network_error");
  } finally {
    tenantRequests.delete(controller);
    sessionRequests.delete(controller);
    options.signal?.removeEventListener("abort", abort);
  }
}
export function write<T>(path: string, body?: unknown, method = "POST", idempotent = false) {
  return api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body), headers: idempotent ? { "Idempotency-Key": crypto.randomUUID() } : undefined });
}
export const money = (value: string | number | null | undefined, digits = 4) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: digits,
    }).format(Number(value));
export const number = (value: number) =>
  new Intl.NumberFormat("zh-CN").format(value);
export const dateTime = (value: string | number) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(
    new Date(
      typeof value === "number" ? value * 1000 : value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
    ),
  );
export const terminal = (status: string) =>
  [
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "canceled",
    "interrupted",
    "expired",
  ].includes(status);
