# 前端接口接入与 API 密钥管理

前端通过平台同源地址调用后端。组织业务使用显式的 `/api/v2/tenants/{tenant_id}` 路径；模型调用使用 `/v1`。Vite 开发代理和 Web 服务都会将这两组路径转发给 API，流式响应保持实时输出。

## 在线接口文档

| 地址 | 用途 |
| --- | --- |
| `/api/docs` | Swagger 交互文档，包含请求参数、响应结构和鉴权方式 |
| `/api/redoc` | 按接口阅读文档 |
| `/api/openapi.json` | 当前运行版本的 OpenAPI 契约，可用于生成前端类型 |

本机默认 Web 地址为 `http://127.0.0.1:8010`，例如打开 `http://127.0.0.1:8010/api/docs`。开发时使用 Vite 地址下的相同路径。直接访问 API 服务时，旧 `/docs`、`/redoc` 会跳转到新地址，`/openapi.json` 仍返回同一份契约。

OpenAPI 与当前后端路由和 Pydantic 校验模型同步。两种模型调用接口同时声明 JSON 和 `text/event-stream` 响应；`stream` 字段决定本次采用的响应格式。

### Swagger 中统一配置 CSRF Token

1. 先在同源控制台登录，或通过文档中的 `POST /api/v1/auth/login` 登录，浏览器会保存会话 Cookie。
2. 从登录响应或 `GET /api/v2/me` 响应复制 `csrf_token`。
3. 点击 Swagger 右上角的 **Authorize**，在 **CsrfToken (apiKey)** 中粘贴该值并确认授权。
4. 后续执行需要登录的写接口时，Swagger 自动添加 `X-CSRF-Token` 请求头，无需逐个填写。各接口的 Parameters 中不再重复显示该输入框。

配置在当前文档页面内复用。重新登录后更新为新会话的 Token；刷新文档页面后需要重新填写。浏览器自动携带登录 Cookie，SessionCookie 一栏无需手动填写。GET 查询、公开登录和 `/v1` Bearer 模型接口不要求此项。

## 登录与权限

前端管理页面使用登录会话。`POST /api/v1/auth/login` 接收 `{email, password}`，成功后返回 `{user, csrf_token}` 并设置 HttpOnly 会话 Cookie。页面刷新后通过 `GET /api/v2/me` 恢复身份和 CSRF Token；组织列表来自 `user.memberships`，可操作能力来自当前组织身份的 `capabilities`。

所有请求保留同源 Cookie，写请求额外传递 `X-CSRF-Token`。已有前端的请求封装和组织上下文会处理这些信息，接入页面时复用 `apps/web/src/api.ts` 与 `useResource`。切换组织后使用新的 `tenant_id`，并取消上一组织尚未结束的请求。

API 密钥管理接口需要当前组织的 `runs.execute` 权限。owner、tenant_admin、member 可管理自己的密钥；查看或撤销其他成员的密钥还需要 `members.manage`。finance_viewer 不具有模型执行权限。个人 Bearer 密钥仅供 `/v1` 模型接口使用，不能替代管理页面的登录会话。

## API 密钥管理接口

| 方法 | 完整路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/api/v2/tenants/{tenant_id}/api-keys` | 可选 `all_members=true`，要求成员管理权限 | 200 `{items: KeyView[]}` |
| POST | `/api/v2/tenants/{tenant_id}/api-keys` | `KeyCreate` JSON | 201 `KeyCreated`，包含一次性 `secret` |
| DELETE | `/api/v2/tenants/{tenant_id}/api-keys/{key_id}` | 无请求体 | 200 `{id, revoked: true}`；重复撤销仍成功 |

创建参数：

```json
{
  "name": "业务应用",
  "expires_in_days": 90,
  "model_ids": []
}
```

- `name`：必填，1–120 字符，不能全部为空白。
- `expires_in_days`：整数，1–365，省略时为 90。
- `model_ids`：最多 100 项，使用当前组织模型目录中的内部 `id`。空数组或省略表示允许组织当前及后续授权的所有可用模型。指定模型必须属于当前组织且已启用。
- 每位成员在每个组织最多保留 10 个未过期、未撤销的密钥。

`KeyView` 包含 `id`、`user_id`、`name`、`prefix`、`model_ids`、`created_at`、`expires_at`、`revoked_at` 和 `last_used_at`。时间为 ISO 字符串；`revoked_at`、`last_used_at` 可以为 `null`。`KeyCreated` 在这些字段外增加 `secret`。列表及撤销响应都不会返回密钥原文或摘要。

前端以 `revoked_at` 是否为空和 `expires_at` 判断展示状态，`last_used_at=null` 表示尚未调用。创建成功后立即展示一次性密钥；关闭展示后清除原文，不写入浏览器持久存储。撤销成功后重新查询列表。

下面的浏览器示例假定用户已经登录，并已从组织列表取得 `tenantId`。项目页面实现应使用现有请求封装。

```javascript
const meResponse = await fetch("/api/v2/me", { credentials: "same-origin" });
if (!meResponse.ok) throw new Error("请先登录");
const { csrf_token: csrfToken } = await meResponse.json();
const base = `/api/v2/tenants/${encodeURIComponent(tenantId)}/api-keys`;

async function call(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message ?? "请求失败");
  return result;
}

const { items } = await call(base);
const created = await call(base, "POST", {
  name: "业务应用",
  expires_in_days: 30,
  model_ids: [],
});
// 此时展示 created.secret；关闭展示后清除原文。
// 用户确认撤销后：
// await call(`${base}/${encodeURIComponent(created.id)}`, "DELETE");
```

管理接口错误形如 `{error: {code, message}}`，字段校验失败时还包含 `details`。常见状态如下：

| 状态 | 前端处理 |
| --- | --- |
| 401 | 登录失效，重新登录 |
| 403 | 权限不足或 CSRF 校验失败；提示错误，必要时刷新身份 |
| 404 | 组织、模型或密钥不存在，或当前用户无权访问目标 |
| 422 | 显示参数错误并保留表单输入 |
| 429 | 有效密钥达到上限，提示先撤销已有密钥；其他额度错误按返回的 code 处理 |

## 模型调用接口

| 方法 | 路径 | 鉴权与作用 |
| --- | --- | --- |
| GET | `/v1/models` | `Authorization: Bearer <个人密钥>`；返回该密钥当前可调用的模型 |
| POST | `/v1/chat/completions` | 文本 Chat Completions，支持客户端函数工具和流式输出 |
| POST | `/v1/responses` | 文本 Responses，调用方传递完整历史，`store=false`，支持客户端函数工具和流式输出 |

这里请求体中的 `model` 使用 `/v1/models` 返回的 `id`，也就是组织模型的**别名**；创建密钥时的 `model_ids` 则使用模型目录的内部 ID。

模型接口只接受个人密钥。成员资格或权限版本失效、密钥过期或撤销后会拒绝继续调用；组织授权、模型状态、余额和配额也会在执行时再次检查。费用和用量仍进入原有成员及组织统计。

非流式错误形如 `{error: {type: "model_api_error", code, message, param: null}}`。401 同时返回 `WWW-Authenticate: Bearer`，429 返回 `Retry-After`。流式响应已经开始后的失败通过 SSE 失败事件表达，调用方需要同时处理 HTTP 错误和流内错误。

协议字段、流式事件和 SDK 示例见[成员 API 密钥与模型服务](member-api-keys.md)。Playground 的 Agent 运行接口与恢复协议继续见 [AG-UI 交互协议](ag-ui-protocol.md)；其他页面对应的后端接口见[前端 API 契约与页面映射](frontend-api-contracts.md)。

## 前端类型与更新

执行 `make -C agent_platform api-contract` 从当前后端生成 `apps/web/src/api-contract.generated.ts`，其中包括请求和响应类型，以及按完整方法和路径索引的 `ApiRequestByOperation`、`ApiResponseByOperation`。

```typescript
import type {
  ApiRequestByOperation,
  ApiResponseByOperation,
} from "./api-contract.generated";

type CreateKeyBody = ApiRequestByOperation[
  "POST /api/v2/tenants/{tenant_id}/api-keys"
];
type CreateKeyResult = ApiResponseByOperation[
  "POST /api/v2/tenants/{tenant_id}/api-keys"
];
```

`make -C agent_platform api-contract-check` 检查类型是否与当前后端契约一致。接口字段变更时同时更新生成类型和消费页面。

这组接口依赖 `0022_member_api_keys` 迁移及配套 API、Worker、maintenance 版本。上线顺序、数据保留与回滚限制见[成员 API 密钥与模型服务](member-api-keys.md#实现与部署)。
