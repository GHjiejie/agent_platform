"""Versioned execution records for a future durable journal.

These declarations do not enable graph checkpoints or external tools. A journal
must atomically persist an intent before dispatch and compare the current run
lease/fence when changing its state. Event streams alone are not that journal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_RUN_SNAPSHOT_VERSIONS = (1, 2)
LEGACY_RUN_SNAPSHOT_VERSION = 1
CURRENT_RUN_SNAPSHOT_VERSION = 2


def validate_run_snapshot_version(spec: dict) -> int:
    """Missing versions identify legacy snapshots; never coerce future versions."""
    if type(spec) is not dict:
        raise ValueError("Run snapshot must be a JSON object.")
    version = spec.get("snapshot_schema_version", LEGACY_RUN_SNAPSHOT_VERSION)
    if type(version) is not int or version not in SUPPORTED_RUN_SNAPSHOT_VERSIONS:
        raise ValueError("Unsupported run snapshot version.")
    return version


_SHA256 = r"^[0-9a-f]{64}$"
_SCHEMA_KEYS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
    "enum",
    "title",
    "description",
    "default",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minItems",
    "maxItems",
}


def json_value(value, *, depth=0):
    """Accept JSON values without coercing tuples, objects, keys, or nonfinite numbers."""
    if depth > 32:
        raise ValueError("Execution JSON exceeds the nesting limit.")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        for item in value:
            json_value(item, depth=depth + 1)
        return value
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            json_value(item, depth=depth + 1)
        return value
    raise ValueError("Execution data must contain finite JSON values.")


def canonical_digest(value) -> str:
    json_value(value)
    serialized = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(serialized.encode()) > 262144:
        raise ValueError("Execution JSON exceeds the size limit.")
    return hashlib.sha256(serialized.encode()).hexdigest()


def _schema(schema):
    """The v1 dialect is a closed, bounded subset of JSON Schema."""
    json_value(schema)
    if type(schema) is not dict or set(schema) - _SCHEMA_KEYS:
        raise ValueError("Unsupported execution schema keyword.")
    annotations = {"title", "description", "default", "enum"}
    for name in ("title", "description"):
        if name in schema and type(schema[name]) is not str:
            raise ValueError("Schema annotations must be strings.")
    kind = schema.get("type")
    if "anyOf" in schema:
        choices = schema["anyOf"]
        if (
            type(choices) is not list
            or not choices
            or set(schema) - annotations - {"anyOf"}
        ):
            raise ValueError("Schema alternatives must be nonempty and unambiguous.")
        for choice in choices:
            _schema(choice)
    else:
        permitted = {
            "object": {"properties", "required", "additionalProperties"},
            "array": {"items", "minItems", "maxItems"},
            "string": {"minLength", "maxLength", "pattern"},
            "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
            "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
            "boolean": set(),
            "null": set(),
        }
        if (
            type(kind) is not str
            or kind not in permitted
            or set(schema) - annotations - {"type"} - permitted[kind]
        ):
            raise ValueError(
                "Execution schema requires a supported type and constraints."
            )
        if kind == "object":
            properties = schema.get("properties")
            required = schema.get("required", [])
            if (
                type(properties) is not dict
                or schema.get("additionalProperties") is not False
                or type(required) is not list
                or any(
                    type(key) is not str or key not in properties for key in required
                )
                or len(set(required)) != len(required)
            ):
                raise ValueError("Object schemas must reject unknown fields.")
            for child in properties.values():
                _schema(child)
        if kind == "array":
            _schema(schema.get("items"))
        for name in ("minLength", "maxLength", "minItems", "maxItems"):
            if name in schema and (type(schema[name]) is not int or schema[name] < 0):
                raise ValueError("Schema length bounds must be nonnegative integers.")
        for minimum, maximum in (("minLength", "maxLength"), ("minItems", "maxItems")):
            if (
                minimum in schema
                and maximum in schema
                and schema[minimum] > schema[maximum]
            ):
                raise ValueError("Schema length bounds must be ordered.")
        for name in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if name in schema and type(schema[name]) not in {int, float}:
                raise ValueError("Schema numeric bounds must be finite numbers.")
        if (
            "minimum" in schema
            and "maximum" in schema
            and schema["minimum"] > schema["maximum"]
        ):
            raise ValueError("Schema numeric bounds must be ordered.")
        if "pattern" in schema:
            if type(schema["pattern"]) is not str:
                raise ValueError("Schema patterns must be strings.")
            try:
                re.compile(schema["pattern"])
            except re.error:
                raise ValueError("Schema regular expression is invalid.") from None
    if "enum" in schema:
        if type(schema["enum"]) is not list or not schema["enum"]:
            raise ValueError("Schema enumerations must be nonempty arrays.")
        unconstrained_enum = {
            key: value for key, value in schema.items() if key != "enum"
        }
        for value in schema["enum"]:
            validate_value(unconstrained_enum, value)
    if "default" in schema:
        validate_value(schema, schema["default"])
    canonical_digest(schema)


def validate_value(schema, value):
    """Validate the v1 schema without depending on a permissive JSON encoder."""
    json_value(value)
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            try:
                validate_value(option, value)
                return value
            except ValueError:
                pass
        raise ValueError("Execution value does not match its declared schema.")
    kind = schema["type"]
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "number": type(value) in {int, float},
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]
    if not valid or (
        "enum" in schema
        and not any(
            type(value) is type(option) and value == option for option in schema["enum"]
        )
    ):
        raise ValueError("Execution value does not match its declared schema.")
    if kind == "object":
        if (
            set(value) - schema["properties"].keys()
            or set(schema.get("required", [])) - value.keys()
        ):
            raise ValueError(
                "Execution object fields do not match its declared schema."
            )
        for key, item in value.items():
            validate_value(schema["properties"][key], item)
    elif kind == "array":
        for item in value:
            validate_value(schema["items"], item)
    if kind in {"string", "array"}:
        minimum, maximum = (
            ("minLength", "maxLength") if kind == "string" else ("minItems", "maxItems")
        )
        if len(value) < schema.get(minimum, 0) or len(value) > schema.get(
            maximum, math.inf
        ):
            raise ValueError("Execution value exceeds its declared bounds.")
    if (
        kind == "string"
        and "pattern" in schema
        and not re.search(schema["pattern"], value)
    ):
        raise ValueError("Execution string does not match its declared pattern.")
    if kind in {"number", "integer"}:
        for key, predicate in (
            ("minimum", lambda bound: value >= bound),
            ("maximum", lambda bound: value <= bound),
            ("exclusiveMinimum", lambda bound: value > bound),
            ("exclusiveMaximum", lambda bound: value < bound),
        ):
            if key in schema and not predicate(schema[key]):
                raise ValueError("Execution number exceeds its declared bounds.")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported execution contract version.")
        return value


class ToolContract(ContractModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    implementation_version: int = Field(ge=1)
    effect: Literal["pure", "observation", "external"]
    replay: Literal["recompute", "recorded_result", "reconcile"]
    supports_idempotency: bool = False
    input_schema: dict
    output_schema: dict

    @field_validator("input_schema", "output_schema", mode="before")
    @classmethod
    def strict_schema(cls, value):
        _schema(value)
        if value.get("type") != "object":
            raise ValueError("Tool arguments and results must be JSON objects.")
        return value

    @model_validator(mode="after")
    def effect_policy(self):
        expected = {
            "pure": "recompute",
            "observation": "recorded_result",
            "external": "reconcile",
        }[self.effect]
        if self.replay != expected or self.supports_idempotency != (
            self.effect == "external"
        ):
            raise ValueError(
                "Tool replay policy must match its effects and idempotency support."
            )
        return self


class FencingToken(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    lease_owner: str = Field(min_length=1, max_length=128)
    fence: int = Field(ge=1)
    lease_expires_at: float = Field(gt=0)

    def require_current(self, current: FencingToken, *, at: float):
        if (
            type(at) not in {int, float}
            or not math.isfinite(at)
            or at < 0
            or at >= current.lease_expires_at
        ):
            raise ValueError("The current execution lease has expired.")
        if (self.tenant_id, self.run_id, self.lease_owner, self.fence) != (
            current.tenant_id,
            current.run_id,
            current.lease_owner,
            current.fence,
        ):
            raise ValueError("The execution fencing token is stale.")


class StepIdentity(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    index: int = Field(ge=1)


class ExecutionIntent(ContractModel):
    execution_id: str = Field(min_length=1, max_length=128)
    step: StepIdentity
    fencing: FencingToken
    tool: ToolContract
    tool_call_id: str = Field(min_length=1, max_length=128)
    arguments: dict
    arguments_digest: str = Field(pattern=_SHA256)
    contract_digest: str = Field(pattern=_SHA256)
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=256)
    created_at: float = Field(gt=0)
    deadline: float = Field(gt=0)

    @field_validator("arguments", mode="before")
    @classmethod
    def serializable_arguments(cls, value):
        canonical_digest(value)
        return value

    @model_validator(mode="after")
    def coherent_intent(self):
        if (self.step.tenant_id, self.step.run_id) != (
            self.fencing.tenant_id,
            self.fencing.run_id,
        ):
            raise ValueError(
                "Step and fencing token must belong to the same tenant and run."
            )
        if (
            self.deadline <= self.created_at
            or self.created_at >= self.fencing.lease_expires_at
        ):
            raise ValueError(
                "Intent must be prepared under a live execution lease and deadline."
            )
        if canonical_digest(self.arguments) != self.arguments_digest:
            raise ValueError("Execution arguments do not match their frozen digest.")
        if canonical_digest(self.tool.model_dump(mode="json")) != self.contract_digest:
            raise ValueError("Tool declaration does not match its frozen digest.")
        validate_value(self.tool.input_schema, self.arguments)
        if (self.tool.effect == "external") != (self.idempotency_key is not None):
            raise ValueError("External effects require one durable idempotency key.")
        return self

    def authorize_dispatch(self, current: FencingToken, *, at: float):
        self.fencing.require_current(current, at=at)
        if (
            at < self.created_at
            or at >= self.deadline
            or canonical_digest(self.arguments) != self.arguments_digest
            or canonical_digest(self.tool.model_dump(mode="json"))
            != self.contract_digest
        ):
            raise ValueError("Execution intent has expired or changed.")


class ExecutionEvidence(ContractModel):
    kind: Literal["local_result", "external_receipt"]
    result_digest: str = Field(pattern=_SHA256)
    observed_at: float = Field(gt=0)
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=256)

    @model_validator(mode="after")
    def receipt_proof(self):
        if self.kind == "external_receipt" and (
            not self.provider_request_id or not self.idempotency_key
        ):
            raise ValueError(
                "External confirmation requires provider and idempotency evidence."
            )
        if self.kind == "local_result" and (
            self.provider_request_id or self.idempotency_key
        ):
            raise ValueError("Local execution cannot claim an external receipt.")
        return self


class DurableExecutionRecord(ContractModel):
    """Prospective journal row, not proof that the current graph persisted a step."""

    intent: ExecutionIntent
    revision: int = Field(ge=1)
    persisted_at: float = Field(gt=0)
    state: Literal["prepared", "sent", "confirmed", "rejected", "unknown"]
    sent_at: float | None = Field(default=None, gt=0)
    completed_at: float | None = Field(default=None, gt=0)
    result: dict | None = None
    evidence: ExecutionEvidence | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("result", mode="before")
    @classmethod
    def serializable_result(cls, value):
        if value is not None:
            canonical_digest(value)
        return value

    @model_validator(mode="after")
    def state_evidence(self):
        if self.persisted_at < self.intent.created_at:
            raise ValueError("Journal commit cannot precede intent preparation.")
        if self.sent_at is not None and self.sent_at >= self.intent.deadline:
            raise ValueError("Dispatch must precede the execution deadline.")
        if self.sent_at is not None and self.sent_at < self.persisted_at:
            raise ValueError("Dispatch must follow the durable intent commit.")
        if self.completed_at is not None and self.completed_at < (
            self.sent_at or self.persisted_at
        ):
            raise ValueError("Completion cannot precede dispatch or commit.")
        if self.state == "prepared" and any(
            value is not None
            for value in (
                self.sent_at,
                self.completed_at,
                self.result,
                self.evidence,
                self.reason,
            )
        ):
            raise ValueError("Prepared intents cannot claim dispatch or a result.")
        if self.state in {"sent", "unknown", "confirmed"} and self.sent_at is None:
            raise ValueError("This execution state requires a durable dispatch marker.")
        if self.state in {"sent", "unknown", "rejected"} and (
            self.result is not None or self.evidence is not None
        ):
            raise ValueError("Unconfirmed executions cannot claim a result.")
        if self.state in {"sent", "unknown"} and self.completed_at is not None:
            raise ValueError("Unresolved executions cannot claim completion.")
        if self.state in {"unknown", "rejected"} and not self.reason:
            raise ValueError("Unresolved or rejected executions require a reason.")
        if self.state == "rejected" and self.completed_at is None:
            raise ValueError("Rejected executions require a completion time.")
        if self.state == "confirmed":
            if (
                self.result is None
                or self.evidence is None
                or self.completed_at is None
            ):
                raise ValueError(
                    "Confirmed executions require a result and receipt evidence."
                )
            if not self.sent_at <= self.evidence.observed_at <= self.completed_at:
                raise ValueError(
                    "Receipt observation must follow dispatch and precede completion."
                )
            validate_value(self.intent.tool.output_schema, self.result)
            if canonical_digest(self.result) != self.evidence.result_digest:
                raise ValueError("Execution result does not match its evidence.")
            external = self.intent.tool.effect == "external"
            if external != (self.evidence.kind == "external_receipt") or (
                external
                and self.evidence.idempotency_key != self.intent.idempotency_key
            ):
                raise ValueError(
                    "Receipt must prove this execution's effect and idempotency key."
                )
        return self


def recovery_action(
    record: DurableExecutionRecord, current: FencingToken, *, at: float
) -> Literal[
    "dispatch", "reuse_result", "recompute", "reconcile", "manual_observation", "stop"
]:
    """Classify recovery; never perform I/O or replay an external effect.

    Recompute/dispatch after lease takeover requires journal CAS to replace the
    old fencing token, preserving execution identity and its idempotency key.
    Confirmed evidence can be read after expiry; new dispatch remains forbidden.
    """
    record = DurableExecutionRecord.model_validate(record.model_dump(mode="python"))
    previous = record.intent.fencing
    current.require_current(current, at=at)
    if (
        (previous.tenant_id, previous.run_id) != (current.tenant_id, current.run_id)
        or current.fence < previous.fence
        or (
            current.fence == previous.fence
            and current.lease_owner != previous.lease_owner
        )
    ):
        raise ValueError("Recovery requires the current owner of the same tenant run.")
    if record.state == "confirmed":
        return "reuse_result"
    if record.state in {"sent", "unknown"} and record.intent.tool.effect == "external":
        return "reconcile"
    if record.state == "rejected" or at >= record.intent.deadline:
        return "stop"
    if record.state == "prepared":
        return "dispatch"
    return {
        "pure": "recompute",
        "observation": "manual_observation",
        "external": "reconcile",
    }[record.intent.tool.effect]


class ExecutionJournal(Protocol):
    """Persistence adapter obligations before enabling external tools.

    Uniqueness is tenant + run + step + tool_call_id. prepare is idempotent only
    for the identical contract/version, arguments digest and idempotency key.
    Dispatch and completion require a transaction validating the current lease,
    fencing token and expected revision; a stale owner cannot write any result.
    A durable sent marker precedes external I/O. An ambiguous response becomes
    unknown; reconciliation records evidence without resending the effect.
    """

    def prepare(
        self, intent: ExecutionIntent, current: FencingToken, *, at: float
    ) -> DurableExecutionRecord: ...

    def adopt_for_recovery(
        self,
        execution_id: str,
        current: FencingToken,
        *,
        expected_revision: int,
        at: float,
    ) -> DurableExecutionRecord:
        """CAS ownership only; preserve intent identity, state, digest and key.

        It must not turn sent/unknown external effects back into prepared work.
        Only the recovery policy can authorize a pure re-computation.
        """
        ...

    def mark_sent(
        self,
        execution_id: str,
        current: FencingToken,
        *,
        expected_revision: int,
        at: float,
    ) -> DurableExecutionRecord: ...

    def confirm(
        self,
        execution_id: str,
        current: FencingToken,
        result: dict,
        evidence: ExecutionEvidence,
        *,
        expected_revision: int,
        at: float,
    ) -> DurableExecutionRecord: ...

    def mark_unknown(
        self,
        execution_id: str,
        current: FencingToken,
        reason: str,
        *,
        expected_revision: int,
        at: float,
    ) -> DurableExecutionRecord: ...

    def get(
        self, tenant_id: str, execution_id: str
    ) -> DurableExecutionRecord | None: ...
