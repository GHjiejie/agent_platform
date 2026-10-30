"""Real process exits at durable boundaries; no exception cleanup can fake safety."""

import asyncio
import json
import multiprocessing
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal as journal_table
from agent_platform.modules.collaboration.scheduler import CollaborationScheduler
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.modules.runtime.journal import DurableJournal
from agent_platform.modules.service_contexts import worker_services
from agent_platform.tests import test_collaboration_runtime as runtime_tests
from agent_platform.tests.test_collaboration_parallel import PROPOSAL, ParallelGateway


def crash_worker(settings, point, call_log):
    """Spawn constructs fresh connections and exits without running finally blocks."""

    def record_request():
        with open(call_log, "a") as output:
            output.write("provider-request\n")
            output.flush()
            os.fsync(output.fileno())

    class Gateway(runtime_tests.ScriptedGateway):
        async def stream(self, *args, **kwargs):
            await asyncio.to_thread(record_request)
            if point == "after_send":
                os._exit(73)
            if "parallel" in point:
                envelope = json.loads(args[0][-1]["content"].split("\n", 1)[1])
                if point == "parallel_unknown" and "objective" in envelope:
                    os._exit(73)
                value = (
                    json.dumps(PROPOSAL)
                    if "output_schema" in envelope
                    else json.dumps({"value": envelope["objective"]})
                )
                yield GatewayEvent("result", ModelReply(value, [], 20, 10, {}))
                return
            async for event in super().stream(*args, **kwargs):
                yield event

    original_confirm = DurableJournal.confirm
    original_complete = CollaborationScheduler.complete
    from agent_platform.modules.runtime.tools import execute_tool as original_tool

    def mark_sent(self, *args, **kwargs):
        os._exit(73)

    def confirm(self, *args, **kwargs):
        result = original_confirm(self, *args, **kwargs)
        if "observation" in point and result["kind"] != "tool":
            return result
        os._exit(73)
        return result

    def complete(self, *args, **kwargs):
        result = original_complete(self, *args, **kwargs)
        if "parallel" in point:
            if args[0].role == "planner":
                return result
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with db.read() as connection:
                    count = connection.scalar(
                        select(func.count())
                        .select_from(journal_table)
                        .where(
                            journal_table.c.root_run_id == self.run["id"],
                            journal_table.c.kind == "model",
                            journal_table.c.state == "confirmed",
                        )
                    )
                if count >= 3:
                    break
                time.sleep(0.01)
            else:
                os._exit(74)
        os._exit(73)
        return result

    def tool(name, arguments, allowed):
        if name == "current_time":
            if point == "during_observation":
                os._exit(73)
            return {"time": "2026-10-30T12:00:00+08:00", "timezone": "Asia/Shanghai"}
        return original_tool(name, arguments, allowed)

    target, name, replacement = {
        "before_send": (DurableJournal, "mark_sent", mark_sent),
        "after_response": (DurableJournal, "confirm", confirm),
        "after_confirmation": (CollaborationScheduler, "complete", complete),
        "after_observation": (DurableJournal, "confirm", confirm),
        "during_observation": (DurableJournal, "confirm", confirm),
        "after_parallel_confirmation": (CollaborationScheduler, "complete", complete),
    }.get(point, (None, None, None))
    db = Database(settings.database_url)
    replies = (
        [
            ModelReply(
                "",
                [
                    {
                        "id": "clock",
                        "name": "current_time",
                        "args": {"timezone": "Asia/Shanghai"},
                    }
                ],
                20,
                10,
                {},
            )
        ]
        if "observation" in point
        else ['{"value":"confirmed original"}']
    )
    worker = Worker(
        worker_services(db, settings),
        gateway=Gateway(replies),
    )
    run = worker.claim()
    if target:
        with (
            patch.object(target, name, replacement),
            patch("agent_platform.modules.runtime.tools.execute_tool", tool),
        ):
            asyncio.run(worker.execute(run))
    else:
        asyncio.run(worker.execute(run))


class ProcessRecoveryTests(unittest.TestCase):
    setUp = runtime_tests.CollaborationRuntimeTests.setUp
    tearDown = runtime_tests.CollaborationRuntimeTests.tearDown
    login = runtime_tests.CollaborationRuntimeTests.login
    request = runtime_tests.CollaborationRuntimeTests.request
    create_resources = runtime_tests.CollaborationRuntimeTests.create_resources
    enqueue = runtime_tests.CollaborationRuntimeTests.enqueue
    setup_team = runtime_tests.CollaborationRuntimeTests.setup_team
    detail = runtime_tests.CollaborationRuntimeTests.detail

    def crash(self, point):
        run, _ = self.setup_team(
            durable_enabled=True,
            recovery_enabled=True,
            summary_policy={"timeout_seconds": 60},
            member_tools=["current_time"] if "observation" in point else [],
            planning="dynamic" if "parallel" in point else "template",
            dynamic_enabled="parallel" in point,
            parallel_enabled="parallel" in point,
            max_parallel=2 if "parallel" in point else 1,
        )
        log = Path(self.temporary.name) / "provider-requests.log"
        process = multiprocessing.get_context("spawn").Process(
            target=crash_worker, args=(self.platform.settings, point, str(log))
        )
        process.start()
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join(5)
            self.fail("Fault-injection worker did not exit at its durable boundary")
        self.assertEqual(process.exitcode, 73)
        sent = len(log.read_text().splitlines()) if log.exists() else 0
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            stored = dict(
                connection.execute(select(t.runs).where(t.runs.c.id == run["id"]))
                .mappings()
                .one()
            )
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(lease_until=time.time() - 1)
            )
        return run, stored, sent

    def recover(self, run, previous, replies, *, expected_calls=3):
        gateway = runtime_tests.ScriptedGateway(replies)
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        claimed = worker.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], run["id"])
        self.assertEqual(claimed["deadline"], previous["deadline"])
        self.assertEqual(claimed["recovery_count"], 1)
        self.assertGreater(claimed["fence"], previous["fence"])
        old = Worker(self.platform, gateway=gateway)
        old.owner = previous["owner"]
        with self.db.read() as connection, self.assertRaises(PlatformError):
            old._assert_lease(connection, previous)
        asyncio.run(worker.execute(claimed))
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(len(detail["tasks"]), 2)
        self.assertEqual(len(detail["executions"]), 3)
        self.assertEqual(len(detail["artifacts"]), 2)
        with self.db.read() as connection:
            calls = (
                connection.execute(select(t.calls).where(t.calls.c.run_id == run["id"]))
                .mappings()
                .all()
            )
        self.assertEqual(len(calls), expected_calls)
        self.assertEqual({call["status"] for call in calls}, {"confirmed"})
        return gateway, detail

    def test_exit_before_send_continues_same_reserved_call(self):
        run, previous, sent = self.crash("before_send")
        self.assertEqual(sent, 0)
        gateway, _ = self.recover(
            run, previous, ['{"value":"source"}', '{"value":"analysis"}', "Summary"]
        )
        self.assertEqual(len(gateway.calls), 3)

    def test_exit_after_send_without_response_stops_without_retry(self):
        run, _, sent = self.crash("after_send")
        self.assertEqual(sent, 1)
        gateway = runtime_tests.ScriptedGateway([])
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        self.assertIsNone(worker.claim())
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(gateway.calls, [])
        self.assertFalse(any(task["status"] == "running" for task in detail["tasks"]))
        self.assertNotEqual(detail["fees"]["pending"], "0")

    def test_exit_after_reply_before_fee_and_checkpoint_reuses_output(self):
        run, previous, sent = self.crash("after_response")
        self.assertEqual(sent, 1)
        gateway, _ = self.recover(run, previous, ['{"value":"analysis"}', "Summary"])
        self.assertEqual(len(gateway.calls), 2)
        self.assertIn("confirmed original", str(gateway.calls[0]["messages"]))
        self.assert_sse_replay(run)

    def test_exit_after_task_confirmation_does_not_repeat_successful_task(self):
        run, previous, sent = self.crash("after_confirmation")
        self.assertEqual(sent, 1)
        before = self.detail(run)
        artifact_id = before["artifacts"][0]["id"]
        gateway, after = self.recover(
            run, previous, ['{"value":"analysis"}', "Summary"]
        )
        self.assertEqual(len(gateway.calls), 2)
        self.assertIn(artifact_id, [artifact["id"] for artifact in after["artifacts"]])

    def test_confirmed_observation_is_reused_after_process_exit(self):
        run, previous, sent = self.crash("after_observation")
        self.assertEqual(sent, 1)
        with patch(
            "agent_platform.modules.runtime.tools.execute_tool",
            side_effect=AssertionError("must not reobserve"),
        ):
            gateway, _ = self.recover(
                run,
                previous,
                ['{"value":"source"}', '{"value":"analysis"}', "Summary"],
                expected_calls=4,
            )
        self.assertEqual(len(gateway.calls), 3)
        self.assertIn("2026-10-30T12:00:00+08:00", str(gateway.calls[0]["messages"]))

    def test_incomplete_observation_is_not_reobserved_after_exit(self):
        run, _, sent = self.crash("during_observation")
        self.assertEqual(sent, 1)
        worker = Worker(self.platform, gateway=runtime_tests.ScriptedGateway([]))
        worker.recover()
        self.assertIsNone(worker.claim())
        self.assertEqual(self.detail(run)["status"], "failed")

    def test_dynamic_parallel_recovery_preserves_plan_and_completed_branches(self):
        run, previous, sent = self.crash("after_parallel_confirmation")
        self.assertEqual(sent, 3)
        gateway = ParallelGateway()
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        claimed = worker.claim()
        self.assertEqual(claimed["deadline"], previous["deadline"])
        asyncio.run(worker.execute(claimed))
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(gateway.calls, ["join", "summary"])
        self.assertEqual(len(detail["tasks"]), 3)
        self.assertEqual(len(detail["executions"]), 5)
        self.assertEqual(len(detail["artifacts"]), 3)

    def test_parallel_takeover_stops_when_a_branch_has_unknown_output(self):
        run, _, sent = self.crash("parallel_unknown")
        self.assertGreaterEqual(sent, 2)
        gateway = ParallelGateway()
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        self.assertIsNone(worker.claim())
        self.assertEqual(self.detail(run)["status"], "failed")
        self.assertEqual(gateway.calls, [])

    def assert_sse_replay(self, run):
        from agent_platform.modules.ag_ui import parse_cursor
        from agent_platform.tests.test_ag_ui import frames

        original = frames(self.request("GET", f"/runs/{run['id']}/ag-ui").text)
        final_messages = [
            event["messages"]
            for _, event in original
            if event["type"] == "MESSAGES_SNAPSHOT"
        ][-1]
        main = [
            message
            for message in final_messages
            if message["role"] == "assistant" and not message.get("subagentRunId")
        ]
        self.assertEqual([message["content"] for message in main], ["Summary"])
        originals = [
            message
            for message in final_messages
            if message.get("content") == '{"value":"confirmed original"}'
        ]
        self.assertEqual(len(originals), 1)
        self.assertTrue(originals[0].get("subagentRunId"))
        for cursor, _ in original:
            if not cursor:
                continue
            replay = frames(
                self.request(
                    "GET", f"/runs/{run['id']}/ag-ui", headers={"Last-Event-ID": cursor}
                ).text
            )
            self.assertEqual(
                [(c, event) for c, event in replay if c],
                [
                    (c, event)
                    for c, event in original
                    if c and parse_cursor(c) > parse_cursor(cursor)
                ],
            )
            self.assertEqual(
                [
                    event["messages"]
                    for _, event in replay
                    if event["type"] == "MESSAGES_SNAPSHOT"
                ][-1],
                final_messages,
            )

    def test_cancellation_while_requeued_cleans_children_and_releases_unsent_hold(self):
        run, _, _ = self.crash("before_send")
        worker = Worker(self.platform, gateway=runtime_tests.ScriptedGateway([]))
        worker.recover()
        self.platform.cancel_run(self.fixture.owner, run["id"])
        worker.recover()
        self.assertIsNone(worker.claim())
        detail = self.detail(run)
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["fees"]["pending"], "0")
        self.assertFalse(any(e["status"] == "running" for e in detail["executions"]))

    def test_revoked_active_member_cannot_use_checkpoint_to_continue(self):
        run, previous, _ = self.crash("after_response")
        member_id = self.detail(run)["executions"][0]["agent_id"]
        self.request(
            "PATCH", f"/agents/{member_id}/availability", json={"available": False}
        )
        gateway = runtime_tests.ScriptedGateway([])
        worker = Worker(self.platform, gateway=gateway)
        worker.recover()
        claimed = worker.claim()
        self.assertEqual(claimed["deadline"], previous["deadline"])
        asyncio.run(worker.execute(claimed))
        self.assertEqual(self.detail(run)["status"], "cancelled")
        self.assertEqual(gateway.calls, [])
        self.assertNotEqual(self.detail(run)["fees"]["confirmed"], "0")
        self.assertEqual(self.detail(run)["fees"]["pending"], "0")

    def test_takeover_limit_is_bounded_without_extending_original_deadline(self):
        run, previous, _ = self.crash("before_send")
        for count in range(1, 4):
            worker = Worker(self.platform, gateway=runtime_tests.ScriptedGateway([]))
            worker.recover()
            claimed = worker.claim()
            self.assertEqual(claimed["recovery_count"], count)
            self.assertEqual(claimed["deadline"], previous["deadline"])
            with self.db.transaction(f"tenant:{self.tenant}") as connection:
                connection.execute(
                    update(t.runs)
                    .where(t.runs.c.id == run["id"])
                    .values(lease_until=time.time() - 1)
                )
        worker.recover()
        self.assertIsNone(worker.claim())
        self.assertEqual(self.detail(run)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
