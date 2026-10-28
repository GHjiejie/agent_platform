"""In-memory SQLite must serialize its shared physical connection."""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from agent_platform.infrastructure.db import Database


class MemoryStorageTests(unittest.TestCase):
    def test_reads_and_writes_do_not_rollback_other_threads(self):
        database = Database("sqlite:///:memory:")
        try:
            with database.transaction() as connection:
                connection.execute(
                    text("CREATE TABLE counter (value INTEGER NOT NULL)")
                )
                connection.execute(text("INSERT INTO counter VALUES (0)"))

            def increment(index):
                # Different logical scopes still share the same SQLite connection.
                with database.transaction(str(index)) as connection:
                    value = connection.scalar(text("SELECT value FROM counter"))
                    time.sleep(0.001)
                    connection.execute(
                        text("UPDATE counter SET value=:value"), {"value": value + 1}
                    )
                with database.read() as connection:
                    return connection.scalar(text("SELECT value FROM counter"))

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(increment, range(32)))
            with database.read() as connection:
                self.assertEqual(
                    connection.scalar(text("SELECT value FROM counter")), 32
                )
            self.assertEqual(len(results), 32)
        finally:
            database.close()
