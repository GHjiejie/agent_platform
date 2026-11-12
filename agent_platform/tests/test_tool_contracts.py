"""Bounded schema declarations for normal conversation tools."""

import copy
import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from agent_platform.modules.runtime.contracts import ToolContract, canonical_digest
from agent_platform.modules.runtime.tools import (
    execute_tool,
    tool_contract,
    tool_definitions,
)


class ToolContractTests(unittest.TestCase):
    def test_declarations_version_schemas_and_time_observation(self):
        calculator, clock = tool_contract("calculator"), tool_contract("current_time")
        self.assertEqual(
            (calculator.implementation_version, calculator.effect, calculator.replay),
            (1, "pure", "recompute"),
        )
        self.assertEqual(
            (clock.effect, clock.replay), ("observation", "recorded_result")
        )
        for declaration in (calculator, clock):
            self.assertEqual(
                ToolContract.model_validate_json(declaration.model_dump_json()),
                declaration,
            )
            self.assertFalse(declaration.input_schema["additionalProperties"])
            self.assertFalse(declaration.output_schema["additionalProperties"])
            self.assertEqual(
                tool_definitions([declaration.name])[0]["function"]["parameters"],
                declaration.input_schema,
            )

    def test_registry_returns_isolated_schema_snapshots(self):
        declaration = tool_contract("calculator")
        declaration.input_schema["additionalProperties"] = True
        self.assertFalse(
            tool_contract("calculator").input_schema["additionalProperties"]
        )

    def test_contract_json_is_strict_finite_and_bounded(self):
        for value in (
            (1, 2),
            {1: "key"},
            {"n": float("nan")},
            float("inf"),
            b"binary",
            datetime.now(UTC),
            {"object": object()},
        ):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                canonical_digest(value)
        nested = None
        for _ in range(34):
            nested = [nested]
        with self.assertRaises(ValueError):
            canonical_digest(nested)
        with self.assertRaises(ValueError):
            canonical_digest("x" * 262145)
        self.assertEqual(
            canonical_digest({"a": 1, "b": 2}), canonical_digest({"b": 2, "a": 1})
        )

    def test_open_or_unsupported_schema_is_rejected(self):
        for invalid in (
            {"type": "object", "properties": {}, "additionalProperties": True},
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
                "required": ["missing"],
            },
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
                "$ref": "unsafe",
            },
            {"type": "string"},
        ):
            fields = tool_contract("calculator").model_dump(mode="python")
            fields["input_schema"] = invalid
            with self.subTest(schema=invalid), self.assertRaises(ValidationError):
                ToolContract.model_validate(fields)

    def test_schema_constraints_are_typed_and_serializable_before_use(self):
        for mutation in (
            {"type": []},
            {"maxLength": "256"},
            {"maxLength": True},
            {"minLength": -1},
            {"pattern": "["},
            {"pattern": 1},
            {"default": 10},
            {"enum": []},
            {"enum": [True]},
            {"minimum": 0},
        ):
            fields = tool_contract("calculator").model_dump(mode="python")
            fields["input_schema"]["properties"]["expression"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                ToolContract.model_validate(fields)

    def test_contract_declaration_does_not_enable_external_tool_execution(self):
        with self.assertRaises(ValueError):
            tool_contract("external_example")
        with self.assertRaises(ValueError):
            tool_definitions(["external_example"])
        with self.assertRaises(ValueError):
            execute_tool("external_example", {}, ["external_example"])
        output = execute_tool("calculator", {"expression": "2+2"}, ["calculator"])
        self.assertEqual(output, {"result": 4})
        schema = copy.deepcopy(tool_contract("calculator").output_schema)
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
