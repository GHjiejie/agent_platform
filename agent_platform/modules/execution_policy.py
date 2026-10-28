"""Published developer policy, separate from platform admission ceilings."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_OUTPUT_TOKENS = 2000000
MAX_MODEL_CALLS = 50
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reasoning_effort: ReasoningEffort | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    max_total_tokens: int | None = Field(default=None, ge=1, le=2000000000)
    max_cost: str | None = None
    max_output_recoveries: int = Field(default=0, ge=0, le=10)

    @field_validator("max_cost")
    @classmethod
    def exact_cost(cls, value):
        if value is None:
            return None
        try:
            number = Decimal(value)
            if (
                not number.is_finite()
                or not 0 < number <= 1000000000
                or number.as_tuple().exponent < -12
            ):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError("费用上限必须为正数，最多 12 位小数。") from None
        return str(number)


def read_policy(spec: dict) -> ExecutionPolicy:
    # Missing policy identifies a pre-policy published version or run snapshot.
    return ExecutionPolicy.model_validate(spec.get("execution_policy", {}))


def effective_limits(policy: ExecutionPolicy, settings) -> dict:
    return {
        "timeout_seconds": execution_timeout(
            policy.timeout_seconds, settings.run_timeout
        ),
        "max_cost": str(
            min(
                Decimal(policy.max_cost or settings.max_run_cost),
                Decimal(settings.max_run_cost),
            )
        ),
    }


def execution_timeout(*limits: float | None) -> int | float | None:
    """None/zero means unlimited; any explicit positive limit still applies."""
    return min((value for value in limits if value), default=None)
