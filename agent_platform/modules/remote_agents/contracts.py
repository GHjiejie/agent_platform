"""Platform Task v1 wire contract, independent of any vendor's Agent protocol.

Full output and final fee are separate evidence. A remote terminal state alone
never authorizes local success, release of a hold, or replay of a request.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from agent_platform.modules.agent_registry.contracts import Identifier, TaskContract
from agent_platform.modules.runtime.contracts import canonical_digest

Money = Annotated[
    str, Field(pattern=r"^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,12})?$", max_length=23)
]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RemoteDescriptor(WireModel):
    protocol: Literal["agent-platform-task-v1"] = "agent-platform-task-v1"
    remote_agent_id: Identifier
    remote_version: Identifier
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    capability_keys: list[Identifier] = Field(default_factory=list, max_length=32)
    task_contracts: list[TaskContract] = Field(min_length=1, max_length=16)
    quote_id: Identifier
    max_charge: Money
    currency: Literal["USD"] = "USD"
    idempotency: Literal[True]
    status_lookup: Literal[True]
    cancellation: bool
    timeout_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator("max_charge")
    @classmethod
    def bounded_charge(cls, value):
        if Decimal(value) > Decimal(1000000000):
            raise ValueError("Remote charge exceeds platform amount bound")
        return value

    @model_validator(mode="after")
    def unique_contracts(self):
        if len(set(self.capability_keys)) != len(self.capability_keys):
            raise ValueError("Remote capabilities must be unique")
        if len({contract.task_type for contract in self.task_contracts}) != len(
            self.task_contracts
        ):
            raise ValueError("Remote task contracts must be unique")
        return self

    @property
    def digest(self):
        return canonical_digest(self.model_dump())


class RemoteRequest(WireModel):
    protocol: Literal["agent-platform-task-v1"] = "agent-platform-task-v1"
    request_id: Identifier
    idempotency_key: Identifier
    remote_agent_id: Identifier
    remote_version: Identifier
    descriptor_digest: Digest
    task_type: Identifier
    objective: str = Field(min_length=1, max_length=4000)
    inputs: JsonValue
    input_digest: Digest
    quote_id: Identifier
    max_charge: Money
    currency: Literal["USD"] = "USD"
    # Unix time in milliseconds, fixed when the local task is first admitted.
    deadline_ms: int = Field(ge=1)

    @field_validator("max_charge")
    @classmethod
    def bounded_charge(cls, value):
        return RemoteDescriptor.bounded_charge(value)

    @model_validator(mode="after")
    def verify_input(self):
        if canonical_digest(self.inputs) != self.input_digest:
            raise ValueError("Remote input digest mismatch")
        return self


class RemoteResult(WireModel):
    value: JsonValue
    digest: Digest

    @model_validator(mode="after")
    def verify_output(self):
        if canonical_digest(self.value) != self.digest:
            raise ValueError("Remote output digest mismatch")
        return self


class RemoteFee(WireModel):
    receipt_id: Identifier
    request_id: Identifier
    quote_id: Identifier
    amount: Money
    currency: Literal["USD"] = "USD"
    final: Literal[True]

    @field_validator("amount")
    @classmethod
    def bounded_amount(cls, value):
        return RemoteDescriptor.bounded_charge(value)


class RemoteStatus(WireModel):
    protocol: Literal["agent-platform-task-v1"] = "agent-platform-task-v1"
    request_id: Identifier
    remote_task_id: Identifier | None = None
    state: Literal["not_found", "running", "succeeded", "failed", "cancelled"]
    result: RemoteResult | None = None
    fee: RemoteFee | None = None
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def coherent_evidence(self):
        if self.result is not None and self.state != "succeeded":
            raise ValueError(
                "Only a successful remote task can provide result evidence"
            )
        if self.fee is not None and self.fee.request_id != self.request_id:
            raise ValueError("Fee evidence belongs to a different request")
        # A success without output remains insufficient to complete a local task.
        # A missing fee remains pending, even when output is complete or cancelled.
        return self


class RemoteHealth(WireModel):
    protocol: Literal["agent-platform-task-v1"] = "agent-platform-task-v1"
    remote_agent_id: Identifier
    remote_version: Identifier
    descriptor_digest: Digest
    healthy: bool
