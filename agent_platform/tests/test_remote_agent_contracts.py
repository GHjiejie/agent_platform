"""Output, fee, identity and descriptor evidence have independent strict contracts."""

import unittest

from pydantic import ValidationError

from agent_platform.modules.remote_agents.contracts import (
    RemoteDescriptor,
    RemoteFee,
    RemoteRequest,
    RemoteResult,
    RemoteStatus,
)
from agent_platform.modules.runtime.contracts import canonical_digest
from agent_platform.tests.test_collaboration_runtime import DATA, TEXT


class RemoteAgentContractTests(unittest.TestCase):
    def descriptor(self):
        return {
            "remote_agent_id": "remote",
            "remote_version": "v1",
            "name": "External analysis",
            "task_contracts": [{"task_type": "work", "input": TEXT, "output": DATA}],
            "quote_id": "quote-v1",
            "max_charge": "0.02",
            "idempotency": True,
            "status_lookup": True,
            "cancellation": True,
        }

    def test_descriptor_is_bounded_and_digest_pins_contract_and_quote(self):
        values = self.descriptor()
        descriptor = RemoteDescriptor.model_validate(values)
        changed = RemoteDescriptor.model_validate({**values, "quote_id": "quote-v2"})
        self.assertNotEqual(descriptor.digest, changed.digest)
        for patch in (
            {"max_charge": "NaN"},
            {"max_charge": "-1"},
            {"max_charge": 0.02},
            {"max_charge": "1000000001"},
            {"idempotency": False},
            {"status_lookup": False},
            {"task_contracts": [*values["task_contracts"], *values["task_contracts"]]},
            {"timeout_seconds": 301},
            {"endpoint": "https://arbitrary.test"},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                RemoteDescriptor.model_validate({**values, **patch})

    def test_request_contains_only_task_input_and_cannot_expand_privileges(self):
        descriptor = RemoteDescriptor.model_validate(self.descriptor())
        values = {
            "request_id": "request-1",
            "idempotency_key": "stable-1",
            "remote_agent_id": "remote",
            "remote_version": "v1",
            "descriptor_digest": descriptor.digest,
            "task_type": "work",
            "objective": "Analyze supplied facts",
            "inputs": "A=10",
            "input_digest": canonical_digest("A=10"),
            "quote_id": "quote-v1",
            "max_charge": "0.02",
            "deadline_ms": 1793361000000,
        }
        self.assertEqual(RemoteRequest.model_validate(values).inputs, "A=10")
        for patch in (
            {"input_digest": "0" * 64},
            {"tools": ["write"]},
            {"system_prompt": "private"},
            {"credential": "secret"},
            {"tenant_id": "another-tenant"},
            {"deadline_ms": "1793361000000"},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                RemoteRequest.model_validate({**values, **patch})

    def test_terminal_state_output_and_fee_remain_independent_evidence(self):
        fee = {
            "receipt_id": "receipt-1",
            "request_id": "request-1",
            "quote_id": "quote-v1",
            "amount": "0.01",
            "final": True,
        }
        fee_only = RemoteStatus.model_validate(
            {"request_id": "request-1", "state": "succeeded", "fee": fee}
        )
        self.assertIsNone(fee_only.result)
        output = {
            "value": {"value": "answer"},
            "digest": canonical_digest({"value": "answer"}),
        }
        output_only = RemoteStatus.model_validate(
            {"request_id": "request-1", "state": "succeeded", "result": output}
        )
        self.assertIsNone(output_only.fee)
        cancelled = RemoteStatus.model_validate(
            {"request_id": "request-1", "state": "cancelled"}
        )
        self.assertIsNone(cancelled.fee)
        with self.assertRaises(ValidationError):
            RemoteStatus.model_validate(
                {"request_id": "different", "state": "succeeded", "fee": fee}
            )
        with self.assertRaises(ValidationError):
            RemoteStatus.model_validate(
                {"request_id": "request-1", "state": "running", "result": output}
            )
        with self.assertRaises(ValidationError):
            RemoteResult.model_validate({**output, "digest": "0" * 64})
        with self.assertRaises(ValidationError):
            RemoteFee.model_validate({**fee, "final": False})
