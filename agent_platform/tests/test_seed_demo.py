"""Local demo generation must preserve authorization and accounting boundaries."""

import tempfile
import unittest
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.billing import service as billing
from agent_platform.modules.platform import Platform, hasher
from agent_platform.scripts.seed_demo import ROLE_GROUPS, seed_demo
from agent_platform.tests.fixtures import migrate


class DemoSeedTest(unittest.TestCase):
    def test_roles_history_finances_and_repeatability(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                database_url=f"sqlite:///{Path(directory) / 'demo.db'}",
                admin_email="admin@example.test",
                admin_password="Admin-fixture-password-42",
                operator_emails=("admin@example.test",),
            )
            migrate(settings.database_url)
            db = Database(settings.database_url)
            self.addCleanup(db.close)
            platform = Platform(db, settings)
            platform.bootstrap()
            with db.read() as conn:
                original_agents = conn.execute(select(t.agents)).all()
                original_users = conn.execute(select(t.users)).all()
                original_wallets = conn.execute(select(billing.wallets)).all()
                original_roles = conn.execute(select(nt.platform_roles)).all()
            password = "Demo-fixture-password-42"
            report = seed_demo(platform, settings.admin_email, password)
            tenant = report["tenant_id"]
            self.assertEqual(report["counts"]["platform_users"], 50)
            self.assertEqual(report["counts"]["platform_sessions"], 500)
            self.assertEqual(report["counts"]["platform_runs"], 2500)
            self.assertEqual(report["counts"]["platform_messages"], 4750)
            self.assertEqual(
                report["run_statuses"],
                {"failed": 125, "cancelled": 125, "succeeded": 2250},
            )
            self.assertEqual(
                Counter(a["role"] for a in report["accounts"]),
                {role: count for role, _, count, _ in ROLE_GROUPS},
            )
            samples = {}
            for account in report["accounts"]:
                samples.setdefault(account["role"], account)
            for role, account in samples.items():
                principal = platform.identity.principal(account["id"])
                self.assertEqual(
                    principal["platform_roles"],
                    [role] if role.startswith("platform_") else [],
                )
                self.assertEqual(
                    [m["tenant_id"] for m in principal["memberships"]], [tenant]
                )
                context = platform.identity.context(account["id"], tenant)
                self.assertEqual(context["tenant_role"], account["membership_role"])
                self.assertEqual(
                    "runs.execute" in context["capabilities"],
                    account["membership_role"] != "finance_viewer",
                )
            with db.read() as conn:
                self.assertEqual(
                    conn.execute(text("PRAGMA foreign_key_check")).all(), []
                )
                for table, original in (
                    (t.agents, original_agents),
                    (t.users, original_users),
                    (billing.wallets, original_wallets),
                ):
                    self.assertEqual(
                        conn.execute(
                            select(table).where(table.c.tenant_id != tenant)
                        ).all(),
                        original,
                    )
                self.assertEqual(
                    conn.scalar(
                        select(func.count())
                        .select_from(t.runs)
                        .where(t.runs.c.status.in_(("queued", "running", "cancelling")))
                    ),
                    0,
                )
                user_hash = conn.scalar(
                    select(t.users.c.password_hash).where(
                        t.users.c.id == samples["member"]["id"]
                    )
                )
                self.assertTrue(hasher.verify(user_hash, password))
                postings = defaultdict(Decimal)
                wallet_sum = Decimal(0)
                for entry in conn.execute(
                    select(billing.ledger_entries).where(
                        billing.ledger_entries.c.tenant_id == tenant
                    )
                ).mappings():
                    postings[entry["transaction_id"]] += entry["amount"]
                    if entry["account"] == "customer_wallet":
                        wallet_sum += entry["amount"]
                self.assertTrue(all(value == 0 for value in postings.values()))
                self.assertEqual(wallet_sum, Decimal(report["simulated_balance"]))
                charged = sum(
                    (
                        Decimal(value)
                        for value in conn.scalars(
                            select(t.calls.c.cost).where(t.calls.c.tenant_id == tenant)
                        )
                    ),
                    Decimal(0),
                )
                self.assertEqual(charged, Decimal(report["simulated_cost"]))
                count = conn.scalar(select(func.count()).select_from(t.runs))
                self.assertEqual(
                    conn.scalar(select(func.count()).select_from(nt.platform_roles)),
                    len(original_roles) + 15,
                )
            self.assertEqual(
                seed_demo(platform, settings.admin_email, password), report
            )
            with self.assertRaises(ValueError):
                seed_demo(platform, settings.admin_email, password, rounds=2)
            with db.read() as conn:
                self.assertEqual(
                    conn.scalar(select(func.count()).select_from(t.runs)), count
                )


if __name__ == "__main__":
    unittest.main()
