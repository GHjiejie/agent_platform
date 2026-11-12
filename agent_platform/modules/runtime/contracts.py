"""Bounded tool schemas and supported normal execution snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_RUN_SNAPSHOT_VERSIONS = (1, 2, 7)
LEGACY_RUN_SNAPSHOT_VERSION = 1
CURRENT_RUN_SNAPSHOT_VERSION = 2


def validate_run_snapshot_version(spec: dict) -> int:
    """Missing versions identify legacy snapshots; never coerce future versions."""
    if type(spec) is not dict:
        raise ValueError("Run snapshot must be a JSON object.")
    if (
        spec.get("execution_mode", "single") != "single"
        or spec.get("remote_ref")
        or spec.get("team_snapshot")
    ):
        raise ValueError("Only normal agent execution is supported.")
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
