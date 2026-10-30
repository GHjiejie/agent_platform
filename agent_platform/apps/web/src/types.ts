import type { AgentCreate, CallResponse, ExecutionPolicy, PrincipalResponse, ReasoningResponse, RunResponse, SessionResponse, WalletResponse } from "./api-contract.generated";

export type User = {
  id: string;
  email: string;
  name: string;
  role: "admin" | "member" | "finance_viewer";
  tenant_role?: TenantRole;
  membership_version?: number;
  tenant_status?: string;
  capabilities?: string[];
  platform_roles?: string[];
  tenant_id: string;
  active: boolean;
};
export type Model = {
  id: string;
  name: string;
  alias: string;
  is_default?: boolean;
  active: boolean;
  input_price: string;
  output_price: string;
  context_window: number;
  max_output_tokens: number;
};
export type GatewayAdmin = {
  admin_url: string;
  configured: boolean;
};
export type Agent = Pick<AgentCreate, "execution_mode" | "capability_keys" | "task_contracts" | "collaboration_spec"> & {
  available?: boolean;
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  model_id: string | null;
  builtin_key?: string | null;
  category?: string;
  starter_prompts?: string[];
  temperature: number;
  max_steps: number;
  max_tokens: number;
  execution_policy?: ExecutionPolicy;
  tools: string[];
  published_version: number | null;
  created_at: string;
};
export type Session = SessionResponse;
export type TokenDefaults = { output_tokens: number; context_window: number };
export type ReasoningEffort = NonNullable<ExecutionPolicy["reasoning_effort"]>;
export type ExecutionOptions = { timeout_seconds: number | null; max_cost: string; max_steps: number; max_output_tokens: number; default_output_tokens?: number; default_context_window?: number };
export type Run = RunResponse;
export type Message = { id?: string; run_id?: string; role: string; content: string; created_at?: string };
export type GeneratedArtifact = { id: string; run_id: string; content: unknown };
export type ReasoningTrace = ReasoningResponse;
export type SessionDetail = Session & { messages: Message[]; runs: Run[]; artifacts?: GeneratedArtifact[]; reasoning?: ReasoningTrace[] };
export type RunEvent = {
  sequence: number;
  type: string;
  data: Record<string, unknown>;
};
export type Wallet = WalletResponse;
export type Dashboard = Wallet & {
  requests: number;
  tokens: number;
  cost: string;
  success_rate: number;
  active_runs: number;
  daily: { date: string; requests: number; tokens: number; cost: string }[];
  models: { model: string; requests: number; tokens: number; cost: string }[];
  gateway_configured: boolean;
};
export type Usage = CallResponse;
export type Ledger = {
  id: string;
  type: string;
  amount: string;
  balance: string;
  description: string;
  created_at: string;
};
export type Quota = {
  id: string;
  scope: string;
  subject_id: string;
  rpm: number | null;
  tpm: number | null;
  concurrent: number | null;
  max_budget: string | null;
  version?: number;
};
export type Audit = {
  id: string;
  actor_email: string;
  action: string;
  target: string;
  created_at: string;
};

export type TenantRole = "owner" | "tenant_admin" | "member" | "finance_viewer";
export type Membership = {
  id: string; user_id: string; tenant_id: string; tenant_name?: string;
  name?: string; email?: string; role: TenantRole; status: string;
  tenant_status?: string; version?: number; authz_version?: number;
};
export type IdentityUser = PrincipalResponse;
export type Tenant = { id: string; name: string; status: string; version?: number; authz_version?: number; created_at?: string; };
export type Invitation = { id: string; email: string; role: TenantRole; status?: string; expires_at?: string | number; accepted_at?: string | null; revoked_at?: string | null; token?: string; accept_url?: string; };
export type Entitlements = {
  rpm: number | null; tpm: number | null; concurrent: number | null;
  max_queued_runs: number; max_running_runs?: number; max_concurrent_runs?: number;
  max_members: number; max_agents: number; max_sse_connections: number; max_export_jobs: number;
  max_budget: string | null;
  version?: number;
};
