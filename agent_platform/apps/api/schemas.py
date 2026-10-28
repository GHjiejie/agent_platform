"""Validated, deliberately bounded API requests."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_platform.infrastructure.token_defaults import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_OUTPUT_TOKENS,
)
from agent_platform.modules.execution_policy import (
    MAX_MODEL_CALLS,
    MAX_OUTPUT_TOKENS,
    ExecutionPolicy,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


class PasswordChange(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class UserCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=512)
    role: Literal["admin", "member"] = "member"


class UserPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Literal["admin", "member"] | None = None
    active: bool | None = None


class ModelCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    alias: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$")
    active: bool = True
    input_price: str = "1"
    output_price: str = "3"
    context_window: int = Field(default=DEFAULT_CONTEXT_WINDOW, ge=256, le=2000000)
    max_output_tokens: int = Field(
        default=DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS
    )

    @field_validator("input_price", "output_price")
    @classmethod
    def price(cls, value: str) -> str:
        try:
            number = Decimal(value)
            if (
                not number.is_finite()
                or number <= 0
                or number > 1000000
                or number.as_tuple().exponent < -12
            ):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError("第一版价格必须为大于零、最多12位小数的金额") from None
        return str(number)


class PlatformModelCreate(ModelCreate):
    deployment_id: str = Field(min_length=1, max_length=64)


class ModelPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    alias: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$"
    )
    active: bool | None = None
    input_price: str | None = None
    output_price: str | None = None
    context_window: int | None = Field(default=None, ge=256, le=2000000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)

    @field_validator("input_price", "output_price")
    @classmethod
    def price(cls, value: str | None) -> str | None:
        return ModelCreate.price(value) if value is not None else None


class AgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(
        default="你是一位专业、可靠的助手。", min_length=1, max_length=20000
    )
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    temperature: float = Field(default=1, ge=0, le=2)
    max_steps: int = Field(default=8, ge=1, le=MAX_MODEL_CALLS)
    max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=1, le=MAX_OUTPUT_TOKENS)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    tools: list[Literal["calculator", "current_time", "generate_sandboxed_ui"]] = Field(
        default_factory=list, max_length=3
    )


class AgentPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    model_id: str | None = Field(default=None, min_length=1, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_steps: int | None = Field(default=None, ge=1, le=MAX_MODEL_CALLS)
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    execution_policy: ExecutionPolicy | None = None
    tools: (
        list[Literal["calculator", "current_time", "generate_sandboxed_ui"]] | None
    ) = Field(default=None, max_length=3)


class SessionCreate(StrictModel):
    agent_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=160)


class RunCreate(StrictModel):
    message: str = Field(min_length=1, max_length=32000)
    model_id: str | None = Field(default=None, min_length=1, max_length=64)


class Credit(StrictModel):
    amount: str
    description: str = Field(min_length=1, max_length=500)


class Quota(StrictModel):
    scope: Literal["tenant", "user", "model"]
    subject_id: str = Field(min_length=1, max_length=64)
    rpm: int | None = Field(default=None, ge=0, le=1000000)
    tpm: int | None = Field(default=None, ge=0, le=1000000000)
    concurrent: int | None = Field(default=None, ge=0, le=10000)
    max_budget: str | None = None


class Reconcile(StrictModel):
    action: Literal["confirm", "write_off"]
    reason: str = Field(min_length=5, max_length=500)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Unblock(StrictModel):
    reason: str = Field(min_length=5, max_length=500)


class TenantCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=120)
    owner_email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    owner_name: str | None = Field(default=None, min_length=1, max_length=120)
    owner_password: str | None = Field(default=None, min_length=12, max_length=512)


class StatusChange(StrictModel):
    reason: str = Field(min_length=5, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class PlatformUserCreate(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=512)


class SupportGrantCreate(StrictModel):
    staff_email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    reason: str = Field(min_length=5, max_length=500)
    minutes: int = Field(default=15, ge=1, le=60)
    allow_content: bool = False


class PlatformRoleChange(StrictModel):
    role: Literal["platform_admin", "platform_finance", "platform_support"]
    active: bool
    reason: str = Field(min_length=5, max_length=500)


class InvitationCreate(StrictModel):
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    role: Literal["tenant_admin", "member", "finance_viewer"] = "member"
    expires_hours: int = Field(default=72, ge=1, le=168)


class InvitationAccept(StrictModel):
    token: str = Field(min_length=20, max_length=512)


class MembershipPatch(StrictModel):
    role: Literal["tenant_admin", "member", "finance_viewer"] | None = None
    status: Literal["active", "revoked"] | None = None
    expected_version: int | None = Field(default=None, ge=1)


class OwnershipTransfer(StrictModel):
    membership_id: str = Field(min_length=1, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)


class EntitlementPatch(StrictModel):
    max_members: int | None = Field(default=None, ge=1, le=100000)
    max_agents: int | None = Field(default=None, ge=0, le=100000)
    max_queued_runs: int | None = Field(default=None, ge=0, le=100000)
    max_running_runs: int | None = Field(default=None, ge=0, le=10000)
    max_concurrent_runs: int | None = Field(default=None, ge=0, le=10000)
    concurrent: int | None = Field(default=None, ge=0, le=10000)
    max_sse_connections: int | None = Field(default=None, ge=0, le=100000)
    max_export_jobs: int | None = Field(default=None, ge=0, le=1000)
    rpm: int | None = Field(default=None, ge=0, le=1000000)
    tpm: int | None = Field(default=None, ge=0, le=1000000000)
    max_budget: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class ModelGrant(StrictModel):
    source_model_id: str = Field(min_length=1, max_length=64)


class ModelPolicy(StrictModel):
    default_model_id: str | None = Field(default=None, max_length=64)
    ordered_model_ids: list[str] | None = Field(default=None, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)


class DeploymentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    internal_route: str = Field(
        min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$"
    )
    base_url: str = Field(min_length=1, max_length=500)
    gateway_id: Literal["primary"] = "primary"
    capabilities: list[Literal["chat", "tools"]] = Field(
        default_factory=lambda: ["chat"], max_length=2
    )


class DeploymentPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    internal_route: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:/-]+$"
    )
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["active", "disabled"] | None = None


class CostBudget(StrictModel):
    limit: str
    expected_version: int | None = Field(default=None, ge=0)


class CostPrice(StrictModel):
    input_price: str
    output_price: str
    expected_version: int | None = Field(default=None, ge=0)


class ProjectionRebuild(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class CostReview(ProjectionRebuild):
    expected_version: int = Field(ge=1)
