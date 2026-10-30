"""Independent worker claims and member fencing under one root run."""

import asyncio
import multiprocessing
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.task_lease_tables import leases
from agent_platform.modules.collaboration.scheduler import CollaborationScheduler
from agent_platform.modules.collaboration.task_leases import TaskController, dispatch
from agent_platform.modules.runtime.context import ExecutionContext
from agent_platform.modules.runtime.journal import DurableJournal
from agent_platform.modules.service_contexts import worker_services
from agent_platform.tests import test_collaboration_runtime as rt


def crash_member(settings, point, path):
    def record_request():
        with open(path, "a") as output:
            output.write("request\n")
            output.flush()
            os.fsync(output.fileno())

    class Gateway(rt.ScriptedGateway):
        async def stream(self, *args, **kwargs):
            await asyncio.to_thread(record_request)
            if point == "after_send":
                os._exit(73)
            async for event in super().stream(*args, **kwargs):
                yield event

    original_confirm = DurableJournal.confirm
    original_complete = CollaborationScheduler.complete

    def before_send(*args, **kwargs):
        os._exit(73)

    def after_response(*args, **kwargs):
        original_confirm(*args, **kwargs)
        os._exit(73)

    def after_confirmation(*args, **kwargs):
        original_complete(*args, **kwargs)
        os._exit(73)

    worker = Worker(
        worker_services(Database(settings.database_url), settings),
        gateway=Gateway(['{"value":"from child process"}']),
    )
    claim = worker.member_queue.claim()
    if claim is None:
        os._exit(74)
    target, method, replacement = {
        "before_send": (DurableJournal, "mark_sent", before_send),
        "after_response": (DurableJournal, "confirm", after_response),
        "after_confirmation": (CollaborationScheduler, "complete", after_confirmation),
        "after_send": (DurableJournal, "confirm", after_response),
    }[point]
    with patch.object(target, method, replacement):
        asyncio.run(worker.member_queue.execute(claim))


class DistributedCollaborationTests(unittest.TestCase):
    setUp = rt.CollaborationRuntimeTests.setUp
    tearDown = rt.CollaborationRuntimeTests.tearDown
    login = rt.CollaborationRuntimeTests.login
    request = rt.CollaborationRuntimeTests.request
    create_resources = rt.CollaborationRuntimeTests.create_resources
    enqueue = rt.CollaborationRuntimeTests.enqueue
    setup_team = rt.CollaborationRuntimeTests.setup_team
    detail = rt.CollaborationRuntimeTests.detail

    def prepare(self):
        run, _ = self.setup_team(
            durable_enabled=True, recovery_enabled=True, distributed_enabled=True
        )
        root_worker = Worker(self.platform, gateway=rt.ScriptedGateway(["Summary"]))
        root = root_worker.claim()
        scheduler = CollaborationScheduler(root_worker, root)
        scheduler.initialize()
        tasks = scheduler.tasks()
        first = next(task for task in tasks if task["logical_key"] == "normalize")
        dispatch(scheduler, first)
        return run, root_worker, root, scheduler, first

    def context(self, claim, worker):
        controller = TaskController(worker, claim)
        return controller, ExecutionContext.from_run(
            controller.run,
            spec=claim["spec"],
            task_id=claim["task"]["id"],
            execution_id=claim["lease"]["execution_id"],
            role="member",
        )

    def test_distinct_worker_executes_children_without_new_runs_or_root_ownership(self):
        run, root_worker, root, _, _ = self.prepare()
        gateway = rt.ScriptedGateway(['{"value":"normalized"}', '{"value":"analysis"}'])
        member_worker = Worker(self.platform, gateway=gateway)

        async def execute():
            parent = asyncio.create_task(root_worker.execute(root))
            async with asyncio.timeout(8):
                while not parent.done():
                    claim = await asyncio.to_thread(member_worker.member_queue.claim)
                    if claim:
                        self.assertNotEqual(claim["lease"]["owner"], root["owner"])
                        await member_worker.member_queue.execute(claim)
                    await asyncio.sleep(0.01)
            await parent

        asyncio.run(execute())
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(len(root_worker.gateway.calls), 1)
        with self.db.read() as connection:
            current = (
                connection.execute(select(t.runs).where(t.runs.c.id == run["id"]))
                .mappings()
                .one()
            )
            self.assertEqual(current["owner"], root_worker.owner)
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.runs)), 1
            )
            owners = set(
                connection.scalars(
                    select(ct.executions.c.owner).where(
                        ct.executions.c.role == "member"
                    )
                )
            )
            self.assertEqual(owners, {member_worker.owner})

    def test_competing_claims_have_one_owner_and_no_model_reservations(self):
        _, _, _, _, first = self.prepare()
        workers = [
            Worker(self.platform, gateway=rt.ScriptedGateway([])) for _ in range(2)
        ]
        with ThreadPoolExecutor(2) as pool:
            results = list(
                pool.map(lambda worker: worker.member_queue.claim(), workers)
            )
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.calls)), 0
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(ct.executions)), 1
            )
            self.assertEqual(connection.scalar(select(leases.c.task_id)), first["id"])

    def test_draining_member_claim_is_limited_to_its_local_root_ids(self):
        run, _, _, _, _ = self.prepare()
        worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        self.assertIsNone(worker.member_queue.claim(root_ids=()))
        self.assertIsNone(worker.member_queue.claim(root_ids=("another-root",)))
        claimed = worker.member_queue.claim(root_ids=(run["id"],))
        self.assertEqual(claimed["root"]["id"], run["id"])

    def test_expired_child_reuses_prepared_identity_and_fences_old_owner(self):
        _, _, _, _, first = self.prepare()
        old = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        claim = old.member_queue.claim()
        controller, context = self.context(claim, old)
        journal = DurableJournal(controller, context)
        record = journal.prepare("initial:0", "model", {"messages": ["fixture"]})
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(leases)
                .where(leases.c.task_id == first["id"])
                .values(lease_until=0)
            )
        new = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        adopted = new.member_queue.claim()
        self.assertEqual(
            adopted["lease"]["execution_id"], claim["lease"]["execution_id"]
        )
        self.assertGreater(adopted["lease"]["fence"], claim["lease"]["fence"])
        self.assertEqual(adopted["lease"]["deadline"], claim["lease"]["deadline"])
        with self.assertRaises(PlatformError):
            journal.mark_sent(record["id"], expected_revision=record["revision"])
        new_controller, new_context = self.context(adopted, new)
        restored = DurableJournal(new_controller, new_context).prepare(
            "initial:0", "model", {"messages": ["fixture"]}
        )
        self.assertEqual(restored["call_id"], record["call_id"])

    def test_unknown_sent_request_is_never_reclaimed(self):
        run, _, _, _, first = self.prepare()
        worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        claim = worker.member_queue.claim()
        controller, context = self.context(claim, worker)
        journal = DurableJournal(controller, context)
        record = journal.prepare("initial:0", "model", {"messages": ["fixture"]})
        journal.mark_sent(record["id"], expected_revision=record["revision"])
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(leases)
                .where(leases.c.task_id == first["id"])
                .values(lease_until=0)
            )
        self.assertIsNone(Worker(self.platform).member_queue.claim())
        self.assertEqual(
            next(
                task for task in self.detail(run)["tasks"] if task["id"] == first["id"]
            )["status"],
            "failed",
        )

    def test_root_fence_and_cancellation_revoke_child_evidence_access(self):
        run, _, _, _, _ = self.prepare()
        worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        claim = worker.member_queue.claim()
        controller, context = self.context(claim, worker)
        journal = DurableJournal(controller, context)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(cancel_requested=True)
            )
        with self.assertRaises(PlatformError):
            journal.prepare("initial:0", "model", {})
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(cancel_requested=False, fence=t.runs.c.fence + 1)
            )
        self.assertTrue(controller.is_cancelled(context.invocation_run()))
        with self.assertRaises(PlatformError):
            journal.prepare("initial:0", "model", {})

    def test_full_root_slots_do_not_starve_children_and_shutdown_drains(self):
        run, _ = self.setup_team(
            durable_enabled=True, recovery_enabled=True, distributed_enabled=True
        )
        self.platform.settings = replace(
            self.platform.settings, worker_concurrency=1, worker_poll_seconds=0.01
        )
        worker = Worker(
            self.platform,
            gateway=rt.ScriptedGateway(
                ['{"value":"normalized"}', '{"value":"analysis"}', "Summary"]
            ),
        )

        async def execute():
            stop = asyncio.Event()
            serving = asyncio.create_task(worker.serve(stop))
            async with asyncio.timeout(8):
                while self.detail(run)["status"] == "queued":
                    await asyncio.sleep(0.01)
                stop.set()  # Local root must still obtain members while draining.
                await serving

        asyncio.run(execute())
        self.assertEqual(self.detail(run)["status"], "succeeded")
        self.assertEqual(len(worker.gateway.calls), 3)

    def process_recovery(self, point):
        run, root_worker, root, _, first = self.prepare()
        path = Path(self.temporary.name) / "child-requests.log"
        process = multiprocessing.get_context("spawn").Process(
            target=crash_member, args=(self.platform.settings, point, str(path))
        )
        process.start()
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join(5)
            self.fail("Member process failed to reach fault boundary")
        self.assertEqual(process.exitcode, 73)
        sent = len(path.read_text().splitlines()) if path.exists() else 0
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            former = dict(
                connection.execute(
                    select(leases).where(leases.c.task_id == first["id"])
                )
                .mappings()
                .one()
            )
            connection.execute(
                update(leases)
                .where(leases.c.task_id == first["id"])
                .values(lease_until=0)
            )
        root_worker.renew(root)
        gateway = rt.ScriptedGateway(
            (['{"value":"first"}'] if point == "before_send" else [])
            + ['{"value":"second"}']
        )
        member = Worker(self.platform, gateway=gateway)

        async def resume():
            parent = asyncio.create_task(root_worker.execute(root))
            async with asyncio.timeout(8):
                while not parent.done():
                    claim = await asyncio.to_thread(member.member_queue.claim)
                    if claim:
                        if claim["task"]["id"] == first["id"]:
                            self.assertEqual(
                                claim["lease"]["execution_id"], former["execution_id"]
                            )
                            self.assertEqual(
                                claim["lease"]["deadline"], former["deadline"]
                            )
                        await member.member_queue.execute(claim)
                    await asyncio.sleep(0.01)
            await parent

        asyncio.run(resume())
        if point == "after_send":
            self.assertEqual(self.detail(run)["status"], "failed")
            self.assertEqual(len(gateway.calls), 0)
            self.assertEqual(len(root_worker.gateway.calls), 0)
        else:
            self.assertEqual(self.detail(run)["status"], "succeeded", self.detail(run))
            self.assertEqual(sent + len(gateway.calls), 2)
            self.assertEqual(len(root_worker.gateway.calls), 1)
            self.assertEqual(len(self.detail(run)["executions"]), 3)
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(
                    select(t.runs.c.fence).where(t.runs.c.id == run["id"])
                ),
                root["fence"],
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.runs)), 1
            )

    def test_child_process_exit_before_send_resumes_original_call(self):
        self.process_recovery("before_send")

    def test_child_process_exit_after_send_stops_unknown_call(self):
        self.process_recovery("after_send")

    def test_child_process_exit_after_response_reuses_full_output(self):
        self.process_recovery("after_response")

    def test_child_process_exit_after_confirmation_preserves_successful_task(self):
        self.process_recovery("after_confirmation")

    def test_parallel_tasks_on_two_workers_overlap_and_unlock_join_once(self):
        from agent_platform.tests.test_collaboration_parallel import ParallelGateway

        run, _ = self.setup_team(
            planning="dynamic",
            dynamic_enabled=True,
            parallel_enabled=True,
            max_parallel=2,
            durable_enabled=True,
            recovery_enabled=True,
            distributed_enabled=True,
        )

        async def overlap(key, gateway):
            if key in {"left", "right"}:
                async with asyncio.timeout(3):
                    while not {"left", "right"}.issubset(gateway.calls):
                        await asyncio.sleep(0.01)

        gateway = ParallelGateway(overlap)
        root_worker = Worker(self.platform, gateway=gateway)
        root = root_worker.claim()
        members = [Worker(self.platform, gateway=gateway) for _ in range(2)]

        async def execute():
            parent = asyncio.create_task(root_worker.execute(root))

            async def serve_member(worker):
                while not parent.done():
                    claim = await asyncio.to_thread(worker.member_queue.claim)
                    if claim:
                        await worker.member_queue.execute(claim)
                    await asyncio.sleep(0.01)

            async with asyncio.timeout(8):
                await asyncio.gather(
                    parent, *(serve_member(worker) for worker in members)
                )

        asyncio.run(execute())
        self.assertEqual(self.detail(run)["status"], "succeeded", self.detail(run))
        self.assertEqual(gateway.peak, 2)
        self.assertEqual(gateway.calls[-2:], ["join", "summary"])
        with self.db.read() as connection:
            owners = set(
                connection.scalars(
                    select(ct.executions.c.owner).where(
                        ct.executions.c.role == "member"
                    )
                )
            )
            self.assertEqual(owners, {worker.owner for worker in members})

    def test_cross_worker_budget_race_keeps_summary_reserve(self):
        from decimal import Decimal
        from threading import Barrier

        from agent_platform.modules.billing.service import BillingService
        from agent_platform.modules.collaboration.planner import validate_plan
        from agent_platform.modules.run_records import uid
        from agent_platform.tests.test_collaboration_parallel import PROPOSAL

        run, _ = self.setup_team(
            planning="dynamic",
            dynamic_enabled=True,
            parallel_enabled=True,
            max_parallel=2,
            durable_enabled=True,
            recovery_enabled=True,
            distributed_enabled=True,
            summary_policy={"max_cost": "0.0007"},
        )
        root_worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        root = root_worker.claim()
        scheduler = CollaborationScheduler(root_worker, root)
        scheduler.team = validate_plan(
            scheduler.snapshot,
            {**scheduler.snapshot["definition"], "tasks": PROPOSAL["tasks"]},
        )
        scheduler.initialize()
        for task in scheduler.tasks():
            if not task["dependencies"]:
                dispatch(scheduler, task)
        workers = [
            Worker(self.platform, gateway=rt.ScriptedGateway([])) for _ in range(2)
        ]
        controllers = [
            self.context(worker.member_queue.claim(), worker) for worker in workers
        ]
        barrier = Barrier(2)

        def reserve(pair):
            controller, context = pair
            run_data = context.invocation_run()
            call_id = uid()
            with self.db.tenant_scope(self.tenant):
                controller.invocation.register_call(run_data, call_id)
                billing = BillingService(
                    self.db,
                    authorize=lambda connection: controller.invocation.authorize_call(
                        connection, run_data, 100, Decimal("0.000484")
                    ),
                )
                barrier.wait(timeout=3)
                try:
                    return billing.reserve(
                        self.tenant,
                        run_data["user_id"],
                        run_data["spec"]["model_id"],
                        run["id"],
                        call_id,
                        100,
                        128,
                        "1",
                        "3",
                    )["status"]
                except PlatformError as exc:
                    return exc.code

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(
                sorted(pool.map(reserve, controllers)),
                ["reserved", "run_budget_exceeded"],
            )
        summary = scheduler.begin(root["spec"], [], role="summary")
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            root_worker.invocation.authorize_call(
                connection, summary.invocation_run(), 100, Decimal("0.00015")
            )

    def test_root_takeover_revokes_child_and_preserves_execution_identity(self):
        from agent_platform.modules.collaboration.recovery import requeue_safe_run

        _run, root_worker, root, _, first = self.prepare()
        child_worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        child = child_worker.member_queue.claim()
        controller, context = self.context(child, child_worker)
        evidence = DurableJournal(controller, context).prepare("initial:0", "model", {})
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            self.assertTrue(requeue_safe_run(root_worker, connection, root))
        replacement = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        new_root = replacement.claim()
        self.assertGreater(new_root["fence"], root["fence"])
        with self.assertRaises(PlatformError):
            DurableJournal(controller, context).mark_sent(
                evidence["id"], expected_revision=evidence["revision"]
            )
        scheduler = CollaborationScheduler(replacement, new_root)
        scheduler.restore_plan()
        scheduler.initialize()
        task = next(task for task in scheduler.tasks() if task["id"] == first["id"])
        dispatch(scheduler, task)
        new_child = child_worker.member_queue.claim()
        self.assertEqual(
            new_child["lease"]["execution_id"], child["lease"]["execution_id"]
        )
        self.assertGreater(new_child["lease"]["fence"], child["lease"]["fence"])
        self.assertEqual(new_child["lease"]["deadline"], child["lease"]["deadline"])
        self.assertEqual(new_child["lease"]["root_fence"], new_root["fence"])

    def test_capacity_timeout_and_recovery_limit_are_bounded(self):
        run, _, _, _, first = self.prepare()
        worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(leases).where(leases.c.task_id == first["id"]).values(ready_at=0)
            )
        self.assertIsNone(worker.member_queue.claim())
        self.assertEqual(
            next(
                task for task in self.detail(run)["tasks"] if task["id"] == first["id"]
            )["status"],
            "failed",
        )
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.calls)), 0
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(ct.executions)), 0
            )

    def test_recovery_limit_stops_fourth_child_takeover(self):
        run, _, _, _, first = self.prepare()
        worker = Worker(self.platform, gateway=rt.ScriptedGateway([]))
        original = worker.member_queue.claim()
        for count in range(1, 5):
            with self.db.transaction(f"tenant:{self.tenant}") as connection:
                connection.execute(
                    update(leases)
                    .where(leases.c.task_id == first["id"])
                    .values(lease_until=0)
                )
            child = Worker(
                self.platform, gateway=rt.ScriptedGateway([])
            ).member_queue.claim()
            if count == 4:
                self.assertIsNone(child)
            else:
                self.assertEqual(child["lease"]["recovery_count"], count)
                self.assertEqual(
                    child["lease"]["deadline"], original["lease"]["deadline"]
                )
        self.assertEqual(
            next(
                task for task in self.detail(run)["tasks"] if task["id"] == first["id"]
            )["status"],
            "failed",
        )

    def test_root_cancel_during_two_remote_worker_calls_never_dispatches_join(self):
        from agent_platform.tests.test_collaboration_parallel import ParallelGateway

        run, _ = self.setup_team(
            planning="dynamic",
            dynamic_enabled=True,
            parallel_enabled=True,
            max_parallel=2,
            durable_enabled=True,
            recovery_enabled=True,
            distributed_enabled=True,
        )
        cancelled = False

        async def cancel_at_boundary(key, gateway):
            nonlocal cancelled
            if key in {"left", "right"}:
                async with asyncio.timeout(3):
                    while not {"left", "right"}.issubset(gateway.calls):
                        await asyncio.sleep(0.01)
                if not cancelled:
                    cancelled = True
                    self.platform.cancel_run(self.fixture.owner, run["id"])

        gateway = ParallelGateway(cancel_at_boundary)
        root_worker = Worker(self.platform, gateway=gateway)
        root = root_worker.claim()
        members = [Worker(self.platform, gateway=gateway) for _ in range(2)]

        async def execute():
            parent = asyncio.create_task(root_worker.execute(root))

            async def serve_member(worker):
                while not parent.done():
                    claim = await asyncio.to_thread(worker.member_queue.claim)
                    if claim:
                        await worker.member_queue.execute(claim)
                    await asyncio.sleep(0.01)

            async with asyncio.timeout(8):
                await asyncio.gather(
                    parent, *(serve_member(worker) for worker in members)
                )

        asyncio.run(execute())
        detail = self.detail(run)
        self.assertEqual(detail["status"], "cancelled")
        self.assertNotIn("join", gateway.calls)
        self.assertNotIn("summary", gateway.calls)
        self.assertFalse(
            any(
                task["status"] in {"ready", "running", "pending"}
                for task in detail["tasks"]
            )
        )
        self.assertTrue(all(task["status"] == "cancelled" for task in detail["tasks"]))
        self.assertEqual(gateway.running, 0)
