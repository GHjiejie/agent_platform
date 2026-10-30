"""Model proposals are untrusted tasks, never execution authority."""

import copy
import json
import unittest
from dataclasses import replace

from sqlalchemy import select

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.collaboration.planner import parse_proposal, validate_plan
from agent_platform.tests import test_collaboration_runtime as runtime_tests


class DynamicPlanningTests(unittest.TestCase):
    setUp = runtime_tests.CollaborationRuntimeTests.setUp
    tearDown = runtime_tests.CollaborationRuntimeTests.tearDown
    login = runtime_tests.CollaborationRuntimeTests.login
    request = runtime_tests.CollaborationRuntimeTests.request
    create_resources = runtime_tests.CollaborationRuntimeTests.create_resources
    enqueue = runtime_tests.CollaborationRuntimeTests.enqueue
    setup_team = runtime_tests.CollaborationRuntimeTests.setup_team
    execute = runtime_tests.CollaborationRuntimeTests.execute
    detail = runtime_tests.CollaborationRuntimeTests.detail

    def setup_dynamic(self):
        run, members = self.setup_team(planning="dynamic", dynamic_enabled=True)
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == run["id"])
            )
        self.snapshot = spec["team_snapshot"]
        self.proposal = {"tasks": self.snapshot["definition"]["tasks"]}
        return run, members

    def test_dynamic_plan_is_metered_isolated_and_validated_before_execution(self):
        run, _ = self.setup_dynamic()
        gateway, _, _ = self.execute(
            run,
            [
                json.dumps(self.proposal),
                '{"value":"input"}',
                '{"value":"analysis"}',
                "Summary",
            ],
        )
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(len(detail["tasks"]), 2)
        self.assertEqual(len(detail["executions"]), 4)
        planner = next(e for e in detail["executions"] if e["role"] == "planner")
        self.assertNotEqual(planner["fees"]["confirmed"], "0")
        self.assertEqual(gateway.calls[0]["tools"], [])
        self.assertNotIn("Private role", json.dumps(gateway.calls[0]))
        self.assertNotIn("output_schema", json.dumps(gateway.calls[1]))

    def test_one_invalid_proposal_can_be_corrected_once(self):
        run, _ = self.setup_dynamic()
        gateway, _, _ = self.execute(
            run,
            [
                json.dumps({**self.proposal, "max_model_calls": 50}),
                json.dumps(self.proposal),
                '{"value":"input"}',
                '{"value":"analysis"}',
                "Summary",
            ],
        )
        self.assertEqual(self.detail(run)["status"], "succeeded")
        self.assertEqual(len(gateway.calls), 5)

    def test_two_invalid_proposals_never_create_tasks_or_invoke_members(self):
        run, _ = self.setup_dynamic()
        self.proposal["tasks"][0]["member_key"] = "intruder"
        gateway, _, _ = self.execute(run, [json.dumps(self.proposal)] * 2)
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["tasks"], [])
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(len(detail["executions"]), 1)

    def test_no_live_candidate_fails_before_planning_call(self):
        run, members = self.setup_dynamic()
        for member in members:
            self.request(
                "PATCH",
                f"/agents/{member['agent_id']}/availability",
                json={"available": False},
            )
        gateway, _, _ = self.execute(run, [])
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        with self.db.read() as connection:
            error = connection.scalar(
                select(t.runs.c.error).where(t.runs.c.id == run["id"])
            )
        self.assertIn("没有可用", error)
        self.assertEqual(gateway.calls, [])
        self.assertEqual(detail["tasks"], [])

    def test_kill_switch_stops_already_queued_dynamic_run(self):
        run, _ = self.setup_dynamic()
        self.platform.settings = replace(
            self.platform.settings, collaboration_dynamic_enabled=False
        )
        gateway, _, _ = self.execute(run, [])
        self.assertEqual(self.detail(run)["status"], "failed")
        self.assertEqual(gateway.calls, [])

    def test_new_dynamic_runs_require_explicit_feature_gate(self):
        run, _ = self.setup_dynamic()
        self.execute(run, ["invalid", "invalid"])
        self.platform.settings = replace(
            self.platform.settings, collaboration_dynamic_enabled=False
        )
        response = self.enqueue(
            {"id": run["session_id"]}, message="New request", key="new-dynamic"
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "dynamic_planning_disabled")

    def test_rejects_privilege_changes_cycles_limits_and_contract_mismatch(self):
        self.setup_dynamic()
        for field, value in (
            ("max_model_calls", 50),
            ("max_parallel", 2),
            ("allow_partial", True),
            ("summary_reserve_fraction", 0.05),
            ("max_tasks", 7),
            ("members", []),
        ):
            definition = copy.deepcopy(self.snapshot["definition"])
            definition[field] = value
            with (
                self.subTest(field=field),
                self.assertRaises((PlatformError, ValueError)),
            ):
                validate_plan(self.snapshot, definition)
        invalid = []
        cycle = copy.deepcopy(self.proposal)
        cycle["tasks"][0]["dependencies"] = ["analyze"]
        invalid.append(cycle)
        mismatch = copy.deepcopy(self.proposal)
        mismatch["tasks"][1]["member_key"] = "m0"
        invalid.append(mismatch)
        invalid.extend(
            [
                {"tasks": []},
                {"tasks": self.proposal["tasks"] * 5},
                {**self.proposal, "tools": ["shell"]},
            ]
        )
        for proposal in invalid:
            with self.subTest(proposal=proposal), self.assertRaises(PlatformError):
                parse_proposal(
                    self.snapshot, json.dumps(proposal), candidates={"m0", "m1"}
                )
        with self.assertRaises(PlatformError):
            parse_proposal(self.snapshot, json.dumps(self.proposal), candidates={"m0"})


if __name__ == "__main__":
    unittest.main()
