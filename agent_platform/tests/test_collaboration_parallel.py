"""Exercise actual overlapping model requests, capacity waits and atomic limits."""

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

from sqlalchemy import func, select

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import BillingService
from agent_platform.modules.collaboration.planner import validate_plan
from agent_platform.modules.collaboration.scheduler import CollaborationScheduler
from agent_platform.modules.run_records import uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests import test_collaboration_runtime as runtime_tests

PROPOSAL = {
    "tasks": [
        {"key": "left", "member_key": "m0", "task_type": "work", "objective": "left"},
        {"key": "right", "member_key": "m0", "task_type": "work", "objective": "right"},
        {
            "key": "join",
            "member_key": "m1",
            "task_type": "work",
            "objective": "join",
            "dependencies": ["left", "right"],
        },
    ]
}


class ParallelGateway:
    configured = True

    def __init__(self, hook=None):
        self.calls = []
        self.running = 0
        self.peak = 0
        self.hook = hook
        self.inputs = {}

    async def stream(self, messages, model, max_tokens, temperature, tools):
        envelope = json.loads(messages[-1]["content"].split("\n", 1)[1])
        key = envelope.get(
            "objective", "plan" if "output_schema" in envelope else "summary"
        )
        self.calls.append(key)
        self.inputs[key] = envelope
        self.running += 1
        self.peak = max(self.running, self.peak)
        try:
            if self.hook:
                await self.hook(key, self)
            value = (
                json.dumps(PROPOSAL)
                if key == "plan"
                else "Summary"
                if key == "summary"
                else json.dumps({"value": key})
            )
            reply = ModelReply(
                value, [], 20, 10, {"prompt_tokens": 20, "completion_tokens": 10}
            )
            yield GatewayEvent("text", {"text": value})
            yield GatewayEvent("result", reply)
        finally:
            self.running -= 1


class ParallelCollaborationTests(unittest.TestCase):
    setUp = runtime_tests.CollaborationRuntimeTests.setUp
    tearDown = runtime_tests.CollaborationRuntimeTests.tearDown
    login = runtime_tests.CollaborationRuntimeTests.login
    request = runtime_tests.CollaborationRuntimeTests.request
    create_resources = runtime_tests.CollaborationRuntimeTests.create_resources
    enqueue = runtime_tests.CollaborationRuntimeTests.enqueue
    setup_team = runtime_tests.CollaborationRuntimeTests.setup_team
    detail = runtime_tests.CollaborationRuntimeTests.detail

    def parallel_team(self):
        return self.setup_team(
            planning="dynamic",
            dynamic_enabled=True,
            max_parallel=2,
            parallel_enabled=True,
        )[0]

    def run_gateway(self, run, gateway):
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        self.assertEqual(claimed["id"], run["id"])
        asyncio.run(worker.execute(claimed))

    def test_two_requests_overlap_and_join_waits_for_both_confirmed_outputs(self):
        run = self.parallel_team()

        async def overlap(key, gateway):
            if key in {"left", "right"}:
                async with asyncio.timeout(3):
                    while not {"left", "right"}.issubset(gateway.calls):
                        await asyncio.sleep(0.01)
            elif key == "join":
                detail = self.detail(run)
                states = {
                    task["logical_key"]: task["status"] for task in detail["tasks"]
                }
                self.assertEqual(states["left"], "succeeded")
                self.assertEqual(states["right"], "succeeded")

        gateway = ParallelGateway(overlap)
        self.run_gateway(run, gateway)
        self.assertEqual(self.detail(run)["status"], "succeeded")
        self.assertEqual(gateway.peak, 2)
        self.assertEqual(gateway.calls[0], "plan")
        self.assertEqual(gateway.calls[-2:], ["join", "summary"])
        self.assertEqual(
            {item["value"]["value"] for item in gateway.inputs["join"]["inputs"]},
            {"left", "right"},
        )
        self.assertNotIn("right", json.dumps(gateway.inputs["left"]["inputs"]))
        self.assertNotIn("left", json.dumps(gateway.inputs["right"]["inputs"]))

    def test_capacity_wait_does_not_create_execution_or_call_and_resumes(self):
        run = self.parallel_team()
        self.platform.settings = replace(
            self.platform.settings, collaboration_max_active_members=1
        )
        seen_wait = []

        async def inspect_wait(key, gateway):
            if key in {"left", "right"} and len(gateway.calls) == 2:
                async with asyncio.timeout(3):
                    while True:
                        with self.db.read() as connection:
                            states = list(
                                connection.scalars(
                                    select(ct.tasks.c.status).where(
                                        ct.tasks.c.root_run_id == run["id"]
                                    )
                                )
                            )
                            if "waiting_capacity" in states:
                                self.assertEqual(
                                    connection.scalar(
                                        select(func.count())
                                        .select_from(ct.executions)
                                        .where(ct.executions.c.root_run_id == run["id"])
                                    ),
                                    2,
                                )
                                self.assertEqual(
                                    connection.scalar(
                                        select(func.count())
                                        .select_from(t.calls)
                                        .where(t.calls.c.run_id == run["id"])
                                    ),
                                    2,
                                )
                                seen_wait.append(True)
                                break
                        await asyncio.sleep(0.01)

        gateway = ParallelGateway(inspect_wait)
        self.run_gateway(run, gateway)
        self.assertEqual(self.detail(run)["status"], "succeeded")
        self.assertEqual(seen_wait, [True])
        self.assertEqual(gateway.peak, 1)

    def test_capacity_wait_is_bounded_and_never_calls_rejected_member(self):
        run = self.parallel_team()
        self.platform.settings = replace(
            self.platform.settings,
            collaboration_max_active_members=1,
            collaboration_capacity_wait_seconds=0.1,
        )

        async def hold(key, gateway):
            if key in {"left", "right"}:
                await asyncio.sleep(0.3)

        gateway = ParallelGateway(hold)
        self.run_gateway(run, gateway)
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(len(gateway.calls), 2)
        states = sorted(task["status"] for task in detail["tasks"])
        self.assertEqual(states, ["blocked", "failed", "succeeded"])
        self.assertTrue(any("容量超时" in task["error"] for task in detail["tasks"]))

    def test_cancel_during_overlapping_calls_joins_children_and_stops_join(self):
        run = self.parallel_team()
        cancelled = []

        async def cancel(key, gateway):
            if key in {"left", "right"}:
                async with asyncio.timeout(3):
                    while not {"left", "right"}.issubset(gateway.calls):
                        await asyncio.sleep(0.01)
                if not cancelled:
                    cancelled.append(True)
                    self.platform.cancel_run(self.fixture.owner, run["id"])

        gateway = ParallelGateway(cancel)
        self.run_gateway(run, gateway)
        detail = self.detail(run)
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(gateway.running, 0)
        self.assertNotIn("join", gateway.calls)
        self.assertFalse(any(e["status"] == "running" for e in detail["executions"]))

    def test_parallel_gate_is_frozen_and_can_be_disabled_for_queued_runs(self):
        run = self.parallel_team()
        self.platform.settings = replace(
            self.platform.settings, collaboration_parallel_enabled=False
        )
        gateway = ParallelGateway()
        self.run_gateway(run, gateway)
        self.assertEqual(self.detail(run)["status"], "failed")
        self.assertNotIn("left", gateway.calls)
        response = self.enqueue({"id": run["session_id"]}, key="new-parallel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"], "parallel_execution_disabled"
        )

    def test_parallel_reservations_compete_atomically_for_last_root_budget(self):
        run, _ = self.setup_team(
            planning="dynamic",
            dynamic_enabled=True,
            max_parallel=2,
            parallel_enabled=True,
            summary_policy={"max_cost": "0.0007"},
        )
        worker = Worker(self.platform, gateway=ParallelGateway())
        claimed = worker.claim()
        scheduler = CollaborationScheduler(worker, claimed)
        scheduler.team = validate_plan(
            scheduler.snapshot,
            {
                **scheduler.snapshot["definition"],
                "tasks": PROPOSAL["tasks"],
            },
        )
        scheduler.initialize()
        tasks = [task for task in scheduler.tasks() if not task["dependencies"]]
        contexts = []
        for task in tasks:
            scheduler.transition(task, "ready")
            contexts.append(
                scheduler.begin(
                    scheduler.snapshot["members"]["m0"]["spec"], [], task=task
                )
            )
        barrier = Barrier(2)

        def reserve(context):
            invocation = context.invocation_run()
            call_id = uid()
            with self.db.tenant_scope(self.tenant):
                worker.invocation.register_call(invocation, call_id)
                billing = BillingService(
                    self.db,
                    authorize=lambda connection: worker.invocation.authorize_call(
                        connection, invocation, 100, Decimal("0.000484")
                    ),
                )
                barrier.wait(timeout=3)
                try:
                    return billing.reserve(
                        self.tenant,
                        invocation["user_id"],
                        invocation["spec"]["model_id"],
                        run["id"],
                        call_id,
                        100,
                        128,
                        "1",
                        "3",
                    )["status"]
                except PlatformError as exc:
                    return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, contexts))
        self.assertEqual(sorted(results), ["reserved", "run_budget_exceeded"])
        summary = scheduler.begin(claimed["spec"], [], role="summary")
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            worker.invocation.authorize_call(
                connection, summary.invocation_run(), 100, Decimal("0.00015")
            )
            with self.assertRaises(PlatformError):
                worker.invocation.authorize_call(
                    connection, contexts[0].invocation_run(), 100, Decimal("0.00015")
                )


if __name__ == "__main__":
    unittest.main()
