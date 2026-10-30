"""Explicit wire contracts for identity, execution and exact financial responses.

Database/private fields are dropped at this boundary. Monetary values are JSON
strings; accepting floats would silently discard supplier/customer precision.
"""

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from agent_platform.modules.agent_registry.contracts import TaskContract


def money_string(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    try:
        valid = isinstance(value, str) and Decimal(value).is_finite()
    except InvalidOperation:
        valid = False
    if not valid:
        raise ValueError("Money must be an exact decimal string")
    return value


Money = Annotated[str, BeforeValidator(money_string)]


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CollaborationFeatures(ResponseModel):
    dynamic: bool
    parallel: bool
    durable: bool
    recovery: bool


class CollaborationSettingsResponse(ResponseModel):
    enabled: bool
    version: int
    platform_enabled: bool
    features: CollaborationFeatures


class MembershipResponse(ResponseModel):
    id: str
    tenant_id: str
    user_id: str
    role: str
    status: str
    authz_version: int
    joined_at: str
    revoked_at: str | None
    tenant_name: str
    tenant_status: str


class PrincipalResponse(ResponseModel):
    id: str
    email: str
    name: str
    active: bool
    global_status: str
    created_at: str
    auth_version: int
    platform_roles: list[str]
    capabilities: list[str]
    memberships: list[MembershipResponse]


class AuthResponse(ResponseModel):
    user: PrincipalResponse
    csrf_token: str


class WalletResponse(ResponseModel):
    balance: Money
    reserved: Money
    available: Money
    currency: str
    blocked: bool
    block_reason: str


class RunResponse(ResponseModel):
    execution_mode: Literal["single", "team"] = "single"
    outcome: str | None = None
    id: str
    tenant_id: str
    session_id: str
    user_id: str
    agent_name: str
    created_at: str
    status: str
    error: str
    finished_at: str | None
    model_id: str | None
    model_alias: str | None
    cost: Money = "0"
    message: str = ""


class RunsResponse(ResponseModel):
    items: list[RunResponse]
    next_cursor: str | None


class SessionResponse(ResponseModel):
    id: str
    tenant_id: str
    user_id: str
    agent_id: str
    title: str
    created_at: str


class SessionsResponse(ResponseModel):
    items: list[SessionResponse]
    next_cursor: str | None


class MessageResponse(ResponseModel):
    id: str
    run_id: str
    role: str
    content: str
    created_at: str


class ArtifactResponse(ResponseModel):
    id: str
    run_id: str
    content: dict[str, Any]


class ReasoningResponse(ResponseModel):
    id: str
    run_id: str
    content: str
    status: Literal["streaming", "complete", "interrupted"]
    created_at: str


class SessionDetailResponse(SessionResponse):
    messages: list[MessageResponse]
    runs: list[RunResponse]
    artifacts: list[ArtifactResponse]
    reasoning: list[ReasoningResponse] = Field(default_factory=list)


class CallResponse(ResponseModel):
    id: str
    tenant_id: str
    user_id: str
    run_id: str
    model_id: str
    model: str
    agent_name: str
    user_email: str
    status: str
    input_tokens: int
    output_tokens: int
    cost: Money
    provider_cost: Money | None = None
    price_version: int
    usage_source: str
    error: str
    created_at: str
    finished_at: str | None


class CallsResponse(ResponseModel):
    items: list[CallResponse]
    next_cursor: str | None


class UsageReceiptResponse(ResponseModel):
    id: str
    tenant_id: str
    call_id: str
    attempt_id: str | None
    source: str
    source_event_id: str
    gateway_request_id: str | None
    quality: str
    input_tokens: int
    output_tokens: int
    provider_cost: Money | None
    evidence: dict[str, Any]
    fingerprint: str
    created_at: str


class UsageReceiptsResponse(ResponseModel):
    items: list[UsageReceiptResponse]


class CostBudgetResponse(ResponseModel):
    scope: str
    subject_id: str
    limit: Money
    spent: Money
    reserved: Money
    blocked: bool
    version: int
    updated_by: str
    updated_at: str


class CostPriceResponse(ResponseModel):
    id: str
    deployment_id: str
    version: int
    input_price: Money
    output_price: Money
    currency: str
    created_by: str
    created_at: str


class CostsResponse(ResponseModel):
    budgets: list[CostBudgetResponse]
    prices: list[CostPriceResponse]


class BudgetRebuildResponse(ResponseModel):
    tenant_id: str
    spent: Money
    dimensions: int


class CollaborationFees(ResponseModel):
    confirmed: Money
    pending: Money


class CollaborationTask(ResponseModel):
    id: str
    logical_key: str
    member_key: str
    agent_id: str
    agent_version: int
    task_type: str
    objective: str
    dependencies: list[str]
    required: bool
    status: str
    error: str = ""


class CollaborationExecution(ResponseModel):
    id: str
    task_id: str | None
    attempt: int
    role: str
    agent_id: str
    agent_version: int
    name: str
    model_alias: str
    status: str
    output: Any
    error: str
    input_digest: str
    input_summary: str | None
    created_at: str
    finished_at: str | None
    fees: CollaborationFees


class CollaborationArtifact(ResponseModel):
    id: str
    execution_id: str
    version: int
    kind: str
    content: Any
    digest: str
    validation_status: str


class CollaborationDetail(ResponseModel):
    run_id: str
    status: str
    outcome: str | None
    enabled: bool
    tasks: list[CollaborationTask]
    executions: list[CollaborationExecution]
    artifacts: list[CollaborationArtifact]
    fees: CollaborationFees
    max_cost: Money | None


class DiscoveryCandidate(ResponseModel):
    id: str
    version: int
    name: str
    description: str
    capability_keys: list[str]
    task_contracts: list[TaskContract]
    contract_digests: dict[str, dict[str, str]]


class DiscoveryResponse(ResponseModel):
    items: list[DiscoveryCandidate]
    next_cursor: str | None


class RemoteCredentialResponse(ResponseModel):
    id: str
    tenant_id: str
    name: str
    active: bool
    created_at: str


class RemoteCredentialList(ResponseModel):
    items: list[RemoteCredentialResponse]
    next_cursor: str | None


class RemoteEndpointResponse(ResponseModel):
    id: str
    url: str
    credential_id: str
    health_status: str
    health_until: float
    health_error: str


class RemoteRegistrationResponse(ResponseModel):
    id: str
    agent_id: str
    version: int
    descriptor: dict[str, Any]
    digest: str
    enabled: bool
    created_at: str
    endpoints: list[RemoteEndpointResponse]


class RemoteRegistrationList(ResponseModel):
    items: list[RemoteRegistrationResponse]
    next_cursor: str | None


class RemoteAvailabilityResponse(ResponseModel):
    id: str
    enabled: bool


class RemoteProbeResponse(ResponseModel):
    id: str
    status: Literal["pending"]
