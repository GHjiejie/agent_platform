"""Durable output proof is private, fenced and independent of financial proof."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from sqlalchemy import select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal as journal_table
from agent_platform.modules.collaboration.scheduler import CollaborationScheduler
from agent_platform.modules.runtime.context import ExecutionContext
from agent_platform.modules.runtime.journal import DurableJournal, EvidenceCodec
from agent_platform.tests import test_collaboration_runtime as runtime_tests


class ExecutionJournalTests(unittest.TestCase):
    setUp = runtime_tests.CollaborationRuntimeTests.setUp
    tearDown = runtime_tests.CollaborationRuntimeTests.tearDown
    login = runtime_tests.CollaborationRuntimeTests.login
    request = runtime_tests.CollaborationRuntimeTests.request
    create_resources = runtime_tests.CollaborationRuntimeTests.create_resources
    enqueue = runtime_tests.CollaborationRuntimeTests.enqueue
    setup_team = runtime_tests.CollaborationRuntimeTests.setup_team

    def journal(self, *, tools=()):
        self.setup_team()
        worker = Worker(self.platform, gateway=runtime_tests.ScriptedGateway([]))
        run = worker.claim()
        scheduler = CollaborationScheduler(worker, run)
        scheduler.initialize()
        task = scheduler.tasks()[0]
        scheduler.transition(task, "ready")
        spec = {**scheduler.snapshot["members"]["m0"]["spec"], "tools": list(tools)}
        context = scheduler.begin(spec, [], task=task)
        return DurableJournal(worker, context), scheduler

    def test_intent_is_idempotent_and_encrypted_with_stable_call_identifiers(self):
        log, _ = self.journal()
        request = {
            "messages": [{"role": "user", "content": "PRIVATE_INPUT_123"}],
            "tools": [],
        }
        first = log.prepare("initial:model:1", "model", request)
        retry = log.prepare("initial:model:1", "model", request)
        self.assertEqual(first, retry)
        self.assertIsNotNone(first["call_id"])
        self.assertNotIn("PRIVATE_INPUT_123", first["request_ciphertext"])
        self.assertEqual(
            log.codec.decode(first["request_ciphertext"], first["request_digest"]),
            request,
        )
        self.assertEqual(log.recovery_action(first["id"]), "dispatch")
        with self.assertRaises(PlatformError):
            log.prepare("initial:model:1", "model", {"messages": []})
        with self.assertRaises(ValueError):
            log.prepare("initial:model:2", "model", {}, effect="pure")

    def test_sent_without_full_output_never_authorizes_model_resend(self):
        log, _ = self.journal()
        record = log.prepare("initial:model:1", "model", {})
        sent = log.mark_sent(record["id"], expected_revision=record["revision"])
        self.assertEqual(log.recovery_action(record["id"]), "stop")
        with self.assertRaises(PlatformError):
            log.mark_sent(record["id"], expected_revision=sent["revision"])
        unknown = log.mark_unknown(sent["id"], expected_revision=sent["revision"])
        self.assertEqual(log.recovery_action(unknown["id"]), "stop")
        with self.assertRaises(PlatformError):
            log.result(unknown["id"])
        with self.assertRaises(PlatformError):
            log.reject(unknown["id"], expected_revision=unknown["revision"])

    def test_confirmation_is_idempotent_and_keeps_complete_private_native_items(self):
        log, _ = self.journal()
        record = log.prepare("initial:model:1", "model", {})
        with self.assertRaises(PlatformError):
            log.confirm(record["id"], {}, expected_revision=0)
        sent = log.mark_sent(record["id"], expected_revision=0)
        reply = {
            "content": "Private answer",
            "response_items": [
                {"type": "reasoning", "encrypted_content": "NATIVE_PRIVATE"}
            ],
            "raw_usage": {"tokens": 10},
        }
        confirmed = log.confirm(sent["id"], reply, expected_revision=sent["revision"])
        self.assertEqual(
            log.confirm(sent["id"], reply, expected_revision=sent["revision"]),
            confirmed,
        )
        self.assertEqual(log.result(sent["id"]), reply)
        self.assertEqual(log.recovery_action(sent["id"]), "reuse_result")
        self.assertNotIn("NATIVE_PRIVATE", confirmed["result_ciphertext"])
        with self.assertRaises(PlatformError):
            log.confirm(
                sent["id"],
                {"content": "different"},
                expected_revision=confirmed["revision"],
            )
        with self.db.read() as connection:
            self.assertEqual(
                connection.execute(
                    select(t.calls).where(t.calls.c.run_id == log.context.root_run_id)
                ).all(),
                [],
            )
        # A complete output may exist before settlement; finance must reconcile
        # independently rather than interpreting this state as paid.

    def test_observation_is_reused_and_uncertain_observation_is_not_reobserved(self):
        log, _ = self.journal()
        observation = log.prepare(
            "initial:tool:clock",
            "tool",
            {"tool": "current_time", "version": 1, "arguments": {}},
            effect="observation",
        )
        sent = log.mark_sent(observation["id"], expected_revision=0)
        self.assertEqual(log.recovery_action(sent["id"]), "stop")
        log.confirm(
            sent["id"],
            {"time": "frozen observation"},
            expected_revision=sent["revision"],
        )
        self.assertEqual(log.result(sent["id"]), {"time": "frozen observation"})
        pure = log.prepare(
            "initial:tool:calc",
            "tool",
            {"tool": "calculator", "arguments": {"expression": "1+2"}},
            effect="pure",
        )
        log.mark_sent(pure["id"], expected_revision=0)
        self.assertEqual(log.recovery_action(pure["id"]), "recompute")

    def test_takeover_preserves_unknown_state_and_rejects_old_owner(self):
        log, _ = self.journal()
        record = log.prepare("initial:model:1", "model", {})
        sent = log.mark_sent(record["id"], expected_revision=0)
        old = log.context
        run = {**old.root, "fence": old.fence + 1, "owner": "replacement"}
        with self.db.transaction(f"tenant:{old.tenant_id}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == old.root_run_id)
                .values(fence=run["fence"], owner=run["owner"])
            )
            connection.execute(
                update(ct.executions)
                .where(ct.executions.c.id == old.execution_id)
                .values(fence=run["fence"], owner=run["owner"])
            )
        context = ExecutionContext.from_run(
            run,
            spec=old.spec,
            task_id=old.task_id,
            execution_id=old.execution_id,
            role=old.role,
        )
        replacement = Worker(self.platform, gateway=runtime_tests.ScriptedGateway([]))
        replacement.owner = "replacement"
        current = DurableJournal(replacement, context)
        adopted = current.adopt(sent["id"], expected_revision=sent["revision"])
        self.assertEqual(adopted["state"], "sent")
        self.assertEqual(adopted["call_id"], sent["call_id"])
        self.assertEqual(adopted["idempotency_key"], sent["idempotency_key"])
        self.assertEqual(current.recovery_action(sent["id"]), "stop")
        with self.assertRaises(PlatformError):
            log.confirm(sent["id"], {}, expected_revision=sent["revision"])
        with self.assertRaises(PlatformError):
            current.adopt(sent["id"], expected_revision=sent["revision"])

    def test_wrong_execution_revocation_and_tampering_fail_closed(self):
        log, scheduler = self.journal()
        record = log.prepare("initial:model:1", "model", {})
        log.mark_sent(record["id"], expected_revision=0)
        log.confirm(record["id"], {"content": "answer"}, expected_revision=1)
        wrong = DurableJournal(
            scheduler.worker, replace(log.context, execution_id="another-execution")
        )
        with self.assertRaises(PlatformError):
            wrong.get(record["id"])
        with self.db.transaction(f"tenant:{log.context.tenant_id}") as connection:
            connection.execute(
                update(journal_table)
                .where(journal_table.c.id == record["id"])
                .values(result_digest="0" * 64)
            )
        with self.assertRaises(PlatformError):
            log.recovery_action(record["id"])
        self.request(
            "PATCH",
            f"/agents/{log.context.agent_id}/availability",
            json={"available": False},
        )
        with self.assertRaises(PlatformError):
            log.get(record["id"])

    def test_missing_or_wrong_key_cannot_silently_discard_evidence(self):
        with self.assertRaises(PlatformError):
            EvidenceCodec("")
        log, _ = self.journal()
        digest, encrypted = log.codec.encode({"private": True})
        with self.assertRaises(PlatformError):
            log.codec.decode(encrypted[:-5] + "00000", digest)

    def test_real_model_call_has_sent_marker_and_reuses_full_reply_without_rebilling(
        self,
    ):
        log, _ = self.journal()
        messages = [{"role": "user", "content": "request"}]

        def reply():
            with self.db.read() as connection:
                row = (
                    connection.execute(
                        select(journal_table).where(
                            journal_table.c.execution_id == log.context.execution_id
                        )
                    )
                    .mappings()
                    .one()
                )
            self.assertEqual(row["state"], "sent")
            return "durable response"

        gateway = runtime_tests.ScriptedGateway([reply])
        log.worker.invocation.gateway = gateway

        async def invoke():
            return await log.worker.invocation.invoke(
                log.context, messages, [], journal=log, operation_key="initial:model:1"
            )

        first = asyncio.run(invoke())
        second = asyncio.run(invoke())
        self.assertEqual(first, second)
        self.assertEqual(len(gateway.calls), 1)
        with self.db.read() as connection:
            calls = (
                connection.execute(
                    select(t.calls).where(t.calls.c.run_id == log.context.root_run_id)
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "confirmed")

    def test_usage_settlement_without_output_confirmation_cannot_authorize_retry(self):
        log, _ = self.journal()
        gateway = runtime_tests.ScriptedGateway(["reply already billed"])
        log.worker.invocation.gateway = gateway

        async def invoke():
            return await log.worker.invocation.invoke(
                log.context,
                [{"role": "user", "content": "request"}],
                [],
                journal=log,
                operation_key="initial:model:1",
            )

        with (
            patch.object(
                log,
                "confirm",
                side_effect=PlatformError(409, "test", "Lost output persistence"),
            ),
            self.assertRaises(PlatformError),
        ):
            asyncio.run(invoke())
        with self.assertRaises(PlatformError) as failure:
            asyncio.run(invoke())
        self.assertEqual(failure.exception.code, "model_output_unknown")
        self.assertEqual(len(gateway.calls), 1)
        with self.db.read() as connection:
            call = (
                connection.execute(
                    select(t.calls).where(t.calls.c.run_id == log.context.root_run_id)
                )
                .mappings()
                .one()
            )
            record = (
                connection.execute(
                    select(journal_table).where(
                        journal_table.c.execution_id == log.context.execution_id
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(call["status"], "confirmed")
        self.assertEqual(record["state"], "unknown")
        self.assertIsNone(record["result_ciphertext"])

    def test_tool_observation_is_executed_once_and_pure_result_is_validated(self):
        log, _ = self.journal(tools=["current_time", "calculator"])
        with patch(
            "agent_platform.modules.runtime.tools.execute_tool",
            return_value={
                "time": "2026-10-30T12:00:00+08:00",
                "timezone": "Asia/Shanghai",
            },
        ) as execute:
            first = log.execute_tool(
                "initial:tool:1:clock", "current_time", {"timezone": "Asia/Shanghai"}
            )
            second = log.execute_tool(
                "initial:tool:1:clock", "current_time", {"timezone": "Asia/Shanghai"}
            )
            self.assertEqual(first, second)
            execute.assert_called_once()
        self.assertEqual(
            log.execute_tool(
                "initial:tool:1:calc", "calculator", {"expression": "1+2"}
            ),
            {"ok": True, "result": 3},
        )

    def test_team_runtime_journals_every_model_execution_only_when_opted_in(self):
        run, _ = self.setup_team(durable_enabled=True)
        gateway = runtime_tests.ScriptedGateway(
            ['{"value":"n"}', '{"value":"a"}', "Summary"]
        )
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        self.assertEqual(claimed["spec"]["snapshot_schema_version"], 4)
        asyncio.run(worker.execute(claimed))
        with self.db.read() as connection:
            rows = (
                connection.execute(
                    select(journal_table).where(
                        journal_table.c.root_run_id == run["id"]
                    )
                )
                .mappings()
                .all()
            )
            status = connection.scalar(
                select(t.runs.c.status).where(t.runs.c.id == run["id"])
            )
        self.assertEqual(status, "succeeded")
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["state"] for row in rows}, {"confirmed"})
        self.assertEqual(len({row["execution_id"] for row in rows}), 3)


if __name__ == "__main__":
    unittest.main()
