"""Execution intent and recovery contracts, without a fake durable graph journal."""

import copy
import unittest
from datetime import UTC, datetime

from pydantic import ValidationError

from agent_platform.modules.runtime.contracts import (
    DurableExecutionRecord,
    ExecutionEvidence,
    ExecutionIntent,
    FencingToken,
    StepIdentity,
    ToolContract,
    canonical_digest,
    recovery_action,
)
from agent_platform.modules.runtime.tools import (
    execute_tool,
    tool_contract,
    tool_definitions,
)


class ToolContractTests(unittest.TestCase):
    def token(self, **changes):
        return FencingToken(
            **{
                "tenant_id": "tenant-a",
                "run_id": "run-a",
                "lease_owner": "worker-a",
                "fence": 1,
                "lease_expires_at": 200.0,
                **changes,
            }
        )

    def external(self, **changes):
        declaration = tool_contract("calculator").model_dump(mode="python")
        declaration.update(
            {
                "name": "external_example",
                "effect": "external",
                "replay": "reconcile",
                "supports_idempotency": True,
                **changes,
            }
        )
        return ToolContract.model_validate(declaration)

    def intent(self, *, tool=None, **changes):
        tool = tool or tool_contract("calculator")
        arguments = {} if tool.name == "current_time" else {"expression": "2+2"}
        fields = {
            "execution_id": "execution-a",
            "step": StepIdentity(
                tenant_id="tenant-a", run_id="run-a", step_id="step-1", index=1
            ),
            "fencing": self.token(),
            "tool": tool,
            "tool_call_id": "call-a",
            "arguments": arguments,
            "arguments_digest": canonical_digest(arguments),
            "contract_digest": canonical_digest(tool.model_dump(mode="json")),
            "created_at": 10.0,
            "deadline": 100.0,
        }
        if tool.effect == "external":
            fields["idempotency_key"] = "durable-effect-key-a"
        fields.update(changes)
        return ExecutionIntent(**fields)

    def record(self, state="prepared", *, tool=None, **changes):
        fields = {
            "intent": self.intent(tool=tool),
            "revision": 1,
            "persisted_at": 11.0,
            "state": state,
        }
        if state in {"sent", "unknown", "confirmed"}:
            fields["sent_at"] = 12.0
        if state in {"unknown", "rejected"}:
            fields["reason"] = "Result was not observed"
        if state == "rejected":
            fields["completed_at"] = 13.0
        if state == "confirmed":
            output = (
                {"time": "2026-10-07T00:00:00Z", "timezone": "UTC"}
                if fields["intent"].tool.effect == "observation"
                else {"result": 4}
            )
            external = fields["intent"].tool.effect == "external"
            fields.update(
                result=output,
                completed_at=14.0,
                evidence=ExecutionEvidence(
                    kind="external_receipt" if external else "local_result",
                    result_digest=canonical_digest(output),
                    observed_at=13.0,
                    provider_request_id="provider-request-a" if external else None,
                    idempotency_key=fields["intent"].idempotency_key
                    if external
                    else None,
                ),
            )
        fields.update(changes)
        return DurableExecutionRecord(**fields)

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

    def test_unknown_versions_fields_and_coerced_numbers_rejected(self):
        for changes in (
            {"schema_version": 2},
            {"schema_version": True},
            {"fence": True},
            {"fence": "1"},
            {"unexpected": "value"},
            {"lease_expires_at": float("inf")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.token(**changes)

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

    def test_effect_policy_requires_external_idempotency(self):
        for changes in (
            {"supports_idempotency": False},
            {"replay": "recompute"},
            {"effect": "pure"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.external(**changes)
        with self.assertRaises(ValidationError):
            self.intent(tool=self.external(), idempotency_key=None)
        with self.assertRaises(ValidationError):
            self.intent(idempotency_key="forged-effect-key")

    def test_intent_validates_arguments_and_frozen_contract(self):
        for args in (
            {"expression": 2},
            {"expression": "2", "extra": True},
            {},
            {"expression": "x" * 257},
        ):
            with self.subTest(arguments=args), self.assertRaises(ValidationError):
                self.intent(arguments=args, arguments_digest=canonical_digest(args))
        for changes in (
            {"arguments_digest": "0" * 64},
            {"contract_digest": "0" * 64},
            {"deadline": 10.0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.intent(**changes)
        with self.assertRaises(ValidationError):
            self.intent(fencing=self.token(tenant_id="tenant-b"))

    def test_dispatch_checks_current_fence_lease_and_execution_deadline(self):
        intent = self.intent()
        intent.authorize_dispatch(self.token(lease_expires_at=300.0), at=99.0)
        for token in (
            self.token(tenant_id="tenant-b"),
            self.token(run_id="run-b"),
            self.token(fence=2),
            self.token(lease_owner="worker-b"),
            self.token(lease_expires_at=19.0),
        ):
            with self.subTest(token=token), self.assertRaises(ValueError):
                intent.authorize_dispatch(token, at=20.0)
        for at in (9.0, 100.0, float("nan")):
            with self.subTest(at=at), self.assertRaises(ValueError):
                intent.authorize_dispatch(self.token(), at=at)

    def test_record_roundtrips_as_versioned_json(self):
        for state in ("prepared", "sent", "unknown", "rejected", "confirmed"):
            with self.subTest(state=state):
                record = self.record(state)
                self.assertEqual(
                    DurableExecutionRecord.model_validate_json(
                        record.model_dump_json()
                    ),
                    record,
                )

    def test_dispatch_marker_must_follow_durable_intent_commit(self):
        for changes in (
            {"persisted_at": 9.0},
            {"sent_at": 10.0},
            {"sent_at": 100.0},
            {"sent_at": None},
            {"completed_at": 13.0},
            {"result": {"result": 4}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.record("sent", **changes)
        with self.assertRaises(ValidationError):
            self.record("prepared", sent_at=12.0)
        with self.assertRaises(ValidationError):
            self.record("unknown", reason=None)

    def test_external_confirmation_requires_correlated_receipt_and_result(self):
        record = self.record("confirmed", tool=self.external())
        self.assertEqual(recovery_action(record, self.token(), at=20.0), "reuse_result")
        for changes in (
            {"evidence": None},
            {"result": None},
            {"result": {"result": True}},
            {"result": {"result": 99}},
            {"completed_at": 11.0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.record("confirmed", tool=self.external(), **changes)
        wrong_key = record.evidence.model_copy(
            update={"idempotency_key": "another-effect-key"}
        )
        with self.assertRaises(ValidationError):
            self.record("confirmed", tool=self.external(), evidence=wrong_key)
        with self.assertRaises(ValidationError):
            ExecutionEvidence(
                kind="external_receipt",
                result_digest=canonical_digest({"result": 4}),
                observed_at=13.0,
            )
        with self.assertRaises(ValidationError):
            self.record(
                "confirmed",
                tool=self.external(),
                evidence=ExecutionEvidence(
                    kind="local_result",
                    result_digest=canonical_digest({"result": 4}),
                    observed_at=13.0,
                ),
            )

    def test_pure_unknown_can_recompute_but_observation_cannot(self):
        for state in ("sent", "unknown"):
            self.assertEqual(
                recovery_action(self.record(state), self.token(), at=20.0), "recompute"
            )
            self.assertEqual(
                recovery_action(
                    self.record(state, tool=tool_contract("current_time")),
                    self.token(),
                    at=20.0,
                ),
                "manual_observation",
            )
        self.assertEqual(
            recovery_action(
                self.record("confirmed", tool=tool_contract("current_time")),
                self.token(),
                at=20.0,
            ),
            "reuse_result",
        )

    def test_external_unknown_never_dispatches_even_after_deadline(self):
        for state in ("sent", "unknown"):
            record = self.record(state, tool=self.external())
            for at in (20.0, 120.0):
                self.assertEqual(
                    recovery_action(record, self.token(), at=at), "reconcile"
                )
        self.assertEqual(
            recovery_action(self.record(tool=self.external()), self.token(), at=20.0),
            "dispatch",
        )
        self.assertEqual(
            recovery_action(self.record(tool=self.external()), self.token(), at=120.0),
            "stop",
        )

    def test_takeover_requires_journal_rebind_before_dispatch(self):
        record = self.record("unknown")
        current = self.token(fence=2, lease_owner="worker-b")
        self.assertEqual(recovery_action(record, current, at=20.0), "recompute")
        with self.assertRaises(ValueError):
            record.intent.authorize_dispatch(current, at=20.0)
        with self.assertRaises(ValueError):
            recovery_action(record, self.token(lease_owner="worker-b"), at=20.0)
        with self.assertRaises(ValueError):
            recovery_action(record, self.token(tenant_id="tenant-b"), at=20.0)
        with self.assertRaises(ValueError):
            recovery_action(record, self.token(lease_expires_at=19.0), at=20.0)

    def test_expired_pure_execution_stops_but_confirmed_result_is_reusable(self):
        self.assertEqual(
            recovery_action(self.record("unknown"), self.token(), at=120.0), "stop"
        )
        self.assertEqual(
            recovery_action(self.record("confirmed"), self.token(), at=120.0),
            "reuse_result",
        )
        self.assertEqual(
            recovery_action(self.record("rejected"), self.token(), at=20.0), "stop"
        )

    def test_mutating_nested_arguments_contract_or_result_cannot_bypass_digest(self):
        intent = self.intent()
        intent.arguments["expression"] = "9+9"
        with self.assertRaises(ValueError):
            intent.authorize_dispatch(self.token(), at=20.0)
        intent = self.intent()
        intent.tool.input_schema["properties"]["expression"]["maxLength"] = 1024
        with self.assertRaises(ValueError):
            intent.authorize_dispatch(self.token(), at=20.0)
        record = self.record("confirmed")
        record.result["result"] = 99
        with self.assertRaises(ValueError):
            recovery_action(record, self.token(), at=20.0)

    def test_contract_declaration_does_not_enable_external_tool_execution(self):
        self.external()
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
