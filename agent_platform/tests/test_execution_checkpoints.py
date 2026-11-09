"""Checkpoints cannot escape execution/phase scope or bypass current authorization."""

import unittest
from dataclasses import replace

from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import select, update

from agent_platform.infrastructure import checkpoint_tables as cp
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.runtime.checkpoints import ExecutionCheckpointer
from agent_platform.modules.runtime.journal import DurableJournal
from agent_platform.tests import test_execution_journal as journal_tests


class ExecutionCheckpointTests(unittest.TestCase):
    setUp = journal_tests.ExecutionJournalTests.setUp
    tearDown = journal_tests.ExecutionJournalTests.tearDown
    login = journal_tests.ExecutionJournalTests.login
    request = journal_tests.ExecutionJournalTests.request
    create_resources = journal_tests.ExecutionJournalTests.create_resources
    enqueue = journal_tests.ExecutionJournalTests.enqueue
    setup_team = journal_tests.ExecutionJournalTests.setup_team
    journal = journal_tests.ExecutionJournalTests.journal

    def test_encrypted_round_trip_pending_writes_and_phase_isolation(self):
        journal, _ = self.journal()
        saver = ExecutionCheckpointer(journal)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {
            "messages": [{"role": "user", "content": "PRIVATE_CHECKPOINT"}]
        }
        checkpoint["channel_versions"] = {"messages": 1}
        config = saver.put(
            saver.config(),
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            {"messages": 1},
        )
        saver.put_writes(
            config,
            [("messages", [{"role": "assistant", "content": "PRIVATE_RESULT"}])],
            "graph-task",
        )
        saved = saver.get_tuple(config)
        self.assertEqual(saved.checkpoint, checkpoint)
        self.assertEqual(saved.pending_writes[0][0], "graph-task")
        self.assertIn("PRIVATE_RESULT", str(saved.pending_writes))
        self.assertIsNone(
            ExecutionCheckpointer(journal, "repair").get_tuple(saver.config())
        )
        with self.db.read() as connection:
            for table in cp.CHECKPOINT_TABLES:
                records = connection.execute(select(table)).mappings().all()
                self.assertEqual(len(records), 1)
                self.assertNotIn("PRIVATE_", records[0]["payload_ciphertext"])

    def test_wrong_execution_old_fence_and_corrupt_payload_are_rejected(self):
        journal, scheduler = self.journal()
        saver = ExecutionCheckpointer(journal)
        checkpoint = empty_checkpoint()
        config = saver.put(
            saver.config(),
            checkpoint,
            {"source": "input", "step": -1, "parents": {}},
            {},
        )
        with self.assertRaises(PlatformError):
            saver.get_tuple({"configurable": {"thread_id": "different-execution"}})
        other = ExecutionCheckpointer(
            DurableJournal(
                scheduler.worker, replace(journal.context, execution_id="another")
            )
        )
        with self.assertRaises(PlatformError):
            other.get_tuple(other.config())
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(cp.checkpoints)
                .where(cp.checkpoints.c.execution_id == journal.context.execution_id)
                .values(payload_digest="0" * 64)
            )
        with self.assertRaises(PlatformError):
            saver.get_tuple(config)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == journal.context.root_run_id)
                .values(fence=journal.context.fence + 1)
            )
        with self.assertRaises(PlatformError):
            saver.put_writes(config, [("messages", [])], "graph-task")

    def test_confirmed_checkpoint_and_pending_write_cannot_change(self):
        journal, _ = self.journal()
        saver = ExecutionCheckpointer(journal)
        checkpoint = empty_checkpoint()
        metadata = {"source": "input", "step": -1, "parents": {}}
        config = saver.put(saver.config(), checkpoint, metadata, {})
        saver.put(saver.config(), checkpoint, metadata, {})
        checkpoint["channel_values"] = {"messages": ["different"]}
        with self.assertRaises(PlatformError):
            saver.put(saver.config(), checkpoint, metadata, {})
        saver.put_writes(config, [("messages", ["first"])], "graph-task")
        saver.put_writes(config, [("messages", ["first"])], "graph-task")
        with self.assertRaises(PlatformError):
            saver.put_writes(config, [("messages", ["different"])], "graph-task")


if __name__ == "__main__":
    unittest.main()
