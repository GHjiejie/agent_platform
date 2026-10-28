"""Offline tests of money, concurrency and all-or-nothing admission invariants."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import (
    BillingService,
    RedisLimiter,
    billing_audit,
    ledger_entries,
    reconciliations,
    reservations_table,
)
from agent_platform.tests.fixtures import MultiTenantFixture, migrate


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temporary.name) / 'billing.db'}",
            admin_email="owner@billing.test",
            admin_password="temporary-test-password-42",
            operator_emails=("owner@billing.test",),
            litellm_url="http://127.0.0.1:1",
            export_directory=Path(self.temporary.name) / "exports",
            web_directory=Path(self.temporary.name) / "web",
        )
        migrate(self.settings.database_url)
        self.db = Database(self.settings.database_url)
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.tenant_a, self.tenant_b = self.fixture.tenant_id, self.fixture.tenant_b
        self.user_a, self.user_b = self.fixture.owner["id"], self.fixture.member["id"]
        self.admin, self.other_admin = self.user_a, self.user_b
        self.finance_admin = self.fixture.finance["id"]
        self.models, self.runs = {}, {}
        for tenant in (self.tenant_a, self.tenant_b):
            actor = self.fixture.context(self.user_a, tenant)
            self.models[tenant] = self.fixture.create_model(tenant, alias="model-a")[
                "id"
            ]
            session = self.fixture.create_session(actor)
            self.runs[(tenant, self.user_a)] = self.fixture.create_run(actor, session)[
                "id"
            ]
        self.model_a = self.models[self.tenant_a]
        self.model_b = self.fixture.create_model(alias="model-b")["id"]
        session = self.fixture.create_session(self.fixture.member)
        self.runs[(self.tenant_a, self.user_b)] = self.fixture.create_run(
            self.fixture.member, session
        )["id"]
        self.billing = BillingService(self.db)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def credit(self, amount="10", tenant=None, key="credit-1"):
        return self.billing.credit(
            tenant or self.tenant_a, self.admin, amount, "Manual credit receipt", key
        )

    def reserve(
        self,
        call="call-1",
        tenant=None,
        user=None,
        model=None,
        input_tokens=100,
        output_tokens=100,
        price="1000",
    ):
        tenant, user = tenant or self.tenant_a, user or self.user_a
        return self.billing.reserve(
            tenant,
            user,
            model or self.models[tenant],
            self.runs[(tenant, user)],
            call,
            input_tokens,
            output_tokens,
            price,
            price,
        )

    def quota(
        self,
        scope="tenant",
        subject=None,
        rpm=60,
        tpm=120000,
        concurrent=4,
        budget=None,
    ):
        return self.billing.set_quota(
            self.tenant_a, scope, subject or self.tenant_a, rpm, tpm, concurrent, budget
        )

    def error(self, code, operation):
        with self.assertRaises(PlatformError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_wallet_starts_at_zero_and_requires_explicit_credit(self):
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 0)
        self.error("insufficient_balance", self.reserve)
        self.assertEqual(self.billing.reservations(self.tenant_a), [])
        self.assertEqual(self.billing.ledger(self.tenant_a), [])

    def test_credit_idempotency_binds_actor_amount_and_description(self):
        self.credit()
        self.credit()
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 10)
        self.error("idempotency_conflict", lambda: self.credit("11"))
        self.error(
            "idempotency_conflict",
            lambda: self.billing.credit(
                self.tenant_a,
                self.other_admin,
                "10",
                "Manual credit receipt",
                "credit-1",
            ),
        )
        self.error(
            "idempotency_conflict",
            lambda: self.billing.credit(
                self.tenant_a, self.admin, "10", "Changed receipt", "credit-1"
            ),
        )
        with self.db.read() as conn:
            audits = list(conn.execute(select(billing_audit)).mappings())
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["actor_id"], self.admin)

    def test_money_remains_exact_below_float_precision(self):
        self.credit("999999999.123456789012")
        self.credit("0.000000000001", key="tiny-credit")
        self.assertEqual(
            self.billing.wallet(self.tenant_a)["balance"], "999999999.123456789013"
        )

    def test_concurrent_requests_cannot_spend_the_same_balance(self):
        self.credit("0.2")
        barrier = Barrier(2)

        def attempt(call):
            barrier.wait()
            try:
                return self.reserve(call)["status"]
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, ["one", "two"]))
        self.assertCountEqual(outcomes, ["reserved", "insufficient_balance"])
        wallet = self.billing.wallet(self.tenant_a)
        self.assertEqual(Decimal(wallet["reserved"]), Decimal("0.2"))
        self.assertEqual(Decimal(wallet["available"]), 0)
        self.assertEqual(len(self.billing.reservations(self.tenant_a)), 1)

    def test_duplicate_reservation_and_settlement_never_double_charge(self):
        self.credit()
        first = self.reserve()
        self.assertEqual(self.reserve()["id"], first["id"])
        self.error("idempotency_conflict", lambda: self.reserve(output_tokens=99))
        first = self.billing.settle(self.tenant_a, "call-1", 100, 25, "0.001")
        second = self.billing.settle(self.tenant_a, "call-1", 100, 25, "0.001")
        self.assertEqual(first, second)
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["balance"]), Decimal("9.875")
        )
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 2)
        self.error(
            "settlement_conflict",
            lambda: self.billing.settle(self.tenant_a, "call-1", 100, 26),
        )

    def test_postings_balance_and_wallet_projection_can_be_rebuilt(self):
        self.credit()
        self.reserve()
        self.billing.settle(self.tenant_a, "call-1", 100, 50)
        with self.db.read() as conn:
            entries = list(conn.execute(select(ledger_entries)).mappings())
        transactions = {}
        for entry in entries:
            transactions.setdefault(entry["transaction_id"], Decimal(0))
            transactions[entry["transaction_id"]] += entry["amount"]
        self.assertTrue(all(total == 0 for total in transactions.values()))
        projected = sum(
            (e["amount"] for e in entries if e["account"] == "customer_wallet"),
            Decimal(0),
        )
        self.assertEqual(
            projected, Decimal(self.billing.wallet(self.tenant_a)["balance"])
        )

    def test_tenant_user_and_model_quotas_all_apply_without_partial_admission(self):
        self.credit()
        self.quota(scope="user", subject=self.user_a, rpm=1)
        self.quota(scope="model", subject=self.model_a, tpm=199)
        self.error("tpm_limit", self.reserve)
        self.assertEqual(self.billing.reservations(self.tenant_a), [])
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.quota(scope="model", subject=self.model_a, tpm=200)
        self.reserve()
        self.error("rpm_limit", lambda: self.reserve("call-2", model=self.model_b))
        self.error("tpm_limit", lambda: self.reserve("call-3", user=self.user_b))
        self.assertEqual(len(self.billing.reservations(self.tenant_a)), 1)

    def test_zero_limits_deny_even_zero_priced_calls(self):
        for field in ("rpm", "tpm", "concurrent", "budget"):
            with self.subTest(field=field):
                self.quota(**{field: 0})
                expected = "budget_exceeded" if field == "budget" else f"{field}_limit"
                self.error(
                    expected,
                    lambda: self.reserve(price="0", input_tokens=0, output_tokens=0),
                )
        self.assertEqual(self.billing.reservations(self.tenant_a), [])

    def test_user_and_model_budgets_are_cumulative_and_count_inflight_money(self):
        self.credit()
        self.quota(scope="user", subject=self.user_a, budget="0.3")
        self.quota(scope="model", subject=self.model_a, budget="0.2")
        self.reserve()
        self.error(
            "budget_exceeded", lambda: self.reserve("same-user", model=self.model_b)
        )
        self.error(
            "budget_exceeded", lambda: self.reserve("same-model", user=self.user_b)
        )
        self.billing.settle(self.tenant_a, "call-1", 100, 0)
        self.reserve("fits-budget", input_tokens=50, output_tokens=50)
        self.error(
            "budget_exceeded",
            lambda: self.reserve("over-budget", input_tokens=1, output_tokens=0),
        )

    def test_unknown_usage_keeps_money_but_releases_execution_capacity(self):
        self.credit()
        self.quota(concurrent=1)
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Gateway disconnected")
        self.billing.unresolved(self.tenant_a, "call-1", "Duplicate notification")
        old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        with self.db.transaction(f"tenant:{self.tenant_a}") as conn:
            conn.execute(reservations_table.update().values(created_at=old))
        self.error(
            "unresolved_call",
            lambda: self.billing.release(self.tenant_a, "call-1", "TTL expired"),
        )
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.2")
        )
        self.reserve("next")
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.4")
        )
        self.error("concurrent_limit", lambda: self.reserve("over-limit"))
        self.billing.settle(self.tenant_a, "call-1", 100, 10)
        # Late settlement of the old call cannot release a different active call.
        self.error("concurrent_limit", lambda: self.reserve("still-over-limit"))
        self.billing.settle(self.tenant_a, "next", 100, 10)
        self.reserve("reconciled")

    def test_unknown_usage_preserves_budgets_and_capacity_limits_in_every_scope(self):
        self.credit()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Usage not confirmed")
        for scope, subject in (
            ("tenant", self.tenant_a),
            ("user", self.user_a),
            ("model", self.model_a),
        ):
            with self.subTest(scope=scope):
                self.quota(scope=scope, subject=subject, concurrent=1, budget="0.3")
                self.error(
                    "budget_exceeded",
                    lambda scope=scope: self.reserve(f"{scope}-denied"),
                )
                self.quota(scope=scope, subject=subject, concurrent=1, budget="0.4")
                self.reserve(f"{scope}-accepted")
                self.error(
                    "concurrent_limit",
                    lambda scope=scope: self.reserve(f"{scope}-excess"),
                )
                self.billing.release(
                    self.tenant_a, f"{scope}-accepted", "Request never sent"
                )
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.2")
        )

    def test_unknown_usage_does_not_consume_entitlement_execution_capacity(self):
        self.credit()
        self.fixture.platform.identity.set_entitlements(
            self.fixture.principal, self.tenant_a, {"max_concurrent_runs": 1}
        )
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Usage not confirmed")
        self.reserve("next")
        self.error("concurrent_limit", lambda: self.reserve("over-limit"))

    def test_token_adjustment_releases_unused_tokens_in_original_window(self):
        self.credit()
        self.quota(tpm=250)
        self.reserve()
        self.error("tpm_limit", lambda: self.reserve("next"))
        self.billing.settle(self.tenant_a, "call-1", 50, 0)
        self.reserve("next")

    def test_release_is_idempotent_and_returns_all_unsent_capacity(self):
        self.credit()
        self.quota(rpm=1, tpm=200, concurrent=1)
        self.reserve()
        self.billing.release(self.tenant_a, "call-1", "Permission revoked before send")
        self.billing.release(self.tenant_a, "call-1", "Repeated")
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.reserve("next")
        self.error(
            "reservation_released",
            lambda: self.billing.settle(self.tenant_a, "call-1", 100, 50),
        )

    def test_invalid_usage_becomes_unresolved_without_refunding(self):
        self.credit()
        for i, value in enumerate((-1, True, 1.5, None, 2_000_000_001)):
            call = f"bad-{i}"
            # Disable concurrency limit only for this validation test.
            self.quota(concurrent=None)
            self.reserve(call)
            self.error(
                "invalid_usage",
                lambda call=call, value=value: self.billing.settle(
                    self.tenant_a, call, value, 10
                ),
            )
        self.assertTrue(
            all(
                r["status"] == "unresolved"
                for r in self.billing.reservations(self.tenant_a)
            )
        )
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 1)

    def test_cached_and_reasoning_details_do_not_duplicate_token_charges(self):
        self.credit()
        self.reserve()
        result = self.billing.settle(
            self.tenant_a,
            "call-1",
            100,
            40,
            raw_usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 30},
            },
        )
        self.assertEqual(Decimal(result["cost"]), Decimal("0.14"))
        self.assertEqual(
            result["raw_usage"]["completion_tokens_details"]["reasoning_tokens"], 30
        )

    def test_actual_cost_over_reservation_never_overdraws_wallet(self):
        self.credit("0.2")
        self.reserve()
        result = self.billing.settle(self.tenant_a, "call-1", 200, 200)
        self.assertEqual(Decimal(result["cost"]), Decimal("0.2"))
        self.assertEqual(Decimal(result["overage"]), Decimal("0.2"))
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 0)
        self.assertTrue(self.billing.wallet(self.tenant_a)["blocked"])
        self.error("billing_review_required", lambda: self.reserve("next", price="0"))

    def test_tenant_isolation_covers_money_receipts_and_idempotency(self):
        self.credit()
        self.credit("20", tenant=self.tenant_b)
        self.reserve()
        self.reserve(tenant=self.tenant_b)
        self.billing.settle(self.tenant_b, "call-1", 10, 10)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 10)
        self.assertEqual(
            self.billing.reservations(self.tenant_a)[0]["status"], "reserved"
        )
        self.error(
            "reservation_not_found",
            lambda: self.billing.settle("tenant-c", "call-1", 10, 10),
        )
        self.assertEqual(self.billing.ledger("tenant-c"), [])

    def test_redis_failure_fails_closed_and_does_not_hold_wallet(self):
        self.credit()
        limiter = Mock()
        limiter.admit.side_effect = PlatformError(
            503, "rate_limiter_unavailable", "Offline"
        )
        self.billing._limiter = limiter
        self.error("rate_limiter_unavailable", self.reserve)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.assertEqual(self.billing.reservations(self.tenant_a), [])
        limiter.finish.assert_called_once()

    def test_database_commit_failure_compensates_redis_admission(self):
        self.credit()
        limiter = Mock()
        self.billing._limiter = limiter
        original_transaction = self.db.transaction

        @contextmanager
        def fail_commit(scope):
            with original_transaction(scope) as conn:
                yield conn
                raise RuntimeError("Simulated commit failure")

        self.db.transaction = fail_commit
        with self.assertRaisesRegex(RuntimeError, "commit failure"):
            self.reserve()
        self.db.transaction = original_transaction
        self.assertEqual(self.billing.reservations(self.tenant_a), [])
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        limiter.admit.assert_called_once()
        limiter.finish.assert_called_once_with(
            self.tenant_a,
            "call-1",
            [f"entitlement:{self.tenant_a}", f"tenant:{self.tenant_a}"],
            0,
            rollback=True,
        )

    def test_every_mutation_rechecks_authority_inside_the_tenant_transaction(self):
        self.credit()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Missing receipt")
        before = self.billing.wallet(self.tenant_a)
        checks = []

        def denied(conn):
            self.assertTrue(conn.in_transaction())
            checks.append(conn.execute(select(reservations_table)).first())
            raise PlatformError(403, "permission_revoked", "Role changed")

        protected = BillingService(self.db, authorize=denied)
        operations = [
            lambda: protected.credit(
                self.tenant_a, self.admin, "1", "New credit", "new"
            ),
            lambda: protected.set_quota(
                self.tenant_a, "tenant", self.tenant_a, None, None, None, None
            ),
            lambda: protected.reserve(
                self.tenant_a,
                self.user_a,
                self.model_a,
                self.runs[(self.tenant_a, self.user_a)],
                "new",
                1,
                1,
                "1",
                "1",
            ),
            lambda: protected.settle(self.tenant_a, "call-1", 100, 10),
            lambda: protected.release(self.tenant_a, "call-1", "Do not release"),
            lambda: protected.unresolved(self.tenant_a, "call-1", "Do not update"),
            lambda: protected.resolve(
                self.tenant_a,
                "call-1",
                action="write_off",
                reason="Do not waive",
                actor_id=self.admin,
                idempotency_key="reconcile",
            ),
            lambda: protected.unblock(self.tenant_a, self.admin, "Do not unblock"),
        ]
        for operation in operations:
            self.error("permission_revoked", operation)
        self.assertEqual(len(checks), len(operations))
        self.assertEqual(before, self.billing.wallet(self.tenant_a))
        self.assertEqual(
            self.billing.reservations(self.tenant_a)[0]["status"], "unresolved"
        )
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 1)

    def test_manual_confirmation_is_audited_and_binds_idempotency_content(self):
        self.credit()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Missing final usage")
        arguments = {
            "input_tokens": 100,
            "output_tokens": 25,
            "action": "confirm",
            "reason": "Verified gateway report #42",
            "actor_id": self.finance_admin,
            "idempotency_key": "resolution-1",
        }
        result = self.billing.resolve(self.tenant_a, "call-1", **arguments)
        self.assertEqual(
            self.billing.resolve(self.tenant_a, "call-1", **arguments), result
        )
        self.assertEqual(result["raw_usage"]["source"], "manual_reconciliation")
        self.assertEqual(result["raw_usage"]["reason"], arguments["reason"])
        self.assertEqual(Decimal(result["cost"]), Decimal("0.125"))
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 2)
        arguments["output_tokens"] = 30
        self.error(
            "idempotency_conflict",
            lambda: self.billing.resolve(self.tenant_a, "call-1", **arguments),
        )
        with self.db.read() as conn:
            audits = list(
                conn.execute(
                    select(billing_audit).where(
                        billing_audit.c.action == "billing.resolve.confirm"
                    )
                ).mappings()
            )
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["actor_id"], self.finance_admin)

    def test_manual_write_off_releases_money_without_erasing_usage_or_creating_a_refund(
        self,
    ):
        self.credit()
        self.quota(tpm=200, concurrent=1)
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Provider cannot supply usage")
        arguments = {
            "action": "write_off",
            "reason": "Waived after provider investigation #19",
            "actor_id": self.finance_admin,
            "idempotency_key": "write-off-1",
        }
        result = self.billing.resolve(self.tenant_a, "call-1", **arguments)
        self.assertEqual(
            self.billing.resolve(self.tenant_a, "call-1", **arguments), result
        )
        self.assertEqual(result["status"], "written_off")
        self.assertEqual(result["rate_tokens"], 200)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 10)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 1)
        self.error("tpm_limit", lambda: self.reserve("next"))
        self.error(
            "reservation_released",
            lambda: self.billing.settle(self.tenant_a, "call-1", 100, 100),
        )
        self.quota(tpm=400, concurrent=1)
        self.reserve("next")

    def test_manual_resolution_rejects_wrong_tenant_incomplete_evidence_and_final_calls(
        self,
    ):
        self.credit()
        self.reserve()
        arguments = {
            "action": "write_off",
            "reason": "Investigation",
            "actor_id": self.admin,
            "idempotency_key": "resolution",
        }
        self.error(
            "resolution_not_pending",
            lambda: self.billing.resolve(self.tenant_a, "call-1", **arguments),
        )
        self.error(
            "reservation_not_found",
            lambda: self.billing.resolve(self.tenant_b, "call-1", **arguments),
        )
        self.billing.unresolved(self.tenant_a, "call-1", "Provider lost receipt")
        self.error(
            "invalid_usage",
            lambda: self.billing.resolve(
                self.tenant_a,
                "call-1",
                action="confirm",
                reason="Missing tokens",
                actor_id=self.admin,
                idempotency_key="confirm",
            ),
        )
        self.error(
            "invalid_resolution",
            lambda: self.billing.resolve(
                self.tenant_a, "call-1", input_tokens=0, **arguments
            ),
        )
        self.error(
            "audit_required",
            lambda: self.billing.resolve(
                self.tenant_a,
                "call-1",
                action="write_off",
                reason=" ",
                actor_id=self.admin,
                idempotency_key="confirm",
            ),
        )
        self.assertEqual(
            self.billing.reservations(self.tenant_a)[0]["status"], "unresolved"
        )

    def test_concurrent_manual_confirmation_cannot_charge_twice(self):
        self.credit()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Missing final usage")
        barrier = Barrier(2)

        def resolve(_index):
            barrier.wait()
            return self.billing.resolve(
                self.tenant_a,
                "call-1",
                100,
                50,
                action="confirm",
                reason="Report verified",
                actor_id=self.admin,
                idempotency_key="shared-key",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(resolve, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["balance"]), Decimal("9.85")
        )
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 2)

    def test_manual_reconciliation_and_audit_share_one_atomic_transaction(self):
        self.credit()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "call-1", "Missing final usage")
        self.billing._audit = Mock(side_effect=RuntimeError("Audit storage failed"))
        with self.assertRaisesRegex(RuntimeError, "Audit storage failed"):
            self.billing.resolve(
                self.tenant_a,
                "call-1",
                100,
                50,
                action="confirm",
                reason="Report verified",
                actor_id=self.admin,
                idempotency_key="attempt",
            )
        self.assertEqual(
            self.billing.reservations(self.tenant_a)[0]["status"], "unresolved"
        )
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.2")
        )
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 1)
        with self.db.read() as conn:
            self.assertEqual(list(conn.execute(select(reconciliations))), [])

    def test_unblock_requires_resolution_and_records_review_without_erasing_overage(
        self,
    ):
        self.credit()
        self.reserve()
        self.reserve("unknown")
        self.billing.unresolved(self.tenant_a, "unknown", "Incomplete usage")
        self.billing.settle(self.tenant_a, "call-1", 200, 200)
        self.error(
            "unresolved_billing",
            lambda: self.billing.unblock(self.tenant_a, self.admin, "Review"),
        )
        self.billing.resolve(
            self.tenant_a,
            "unknown",
            action="write_off",
            reason="Waiver approved",
            actor_id=self.admin,
            idempotency_key="waiver",
        )
        self.billing.unblock(
            self.tenant_a, self.admin, "Accepted excess cost; pricing corrected"
        )
        self.assertFalse(self.billing.wallet(self.tenant_a)["blocked"])
        settled = next(
            row
            for row in self.billing.reservations(self.tenant_a)
            if row["call_id"] == "call-1"
        )
        self.assertEqual(Decimal(settled["overage"]), Decimal("0.2"))
        self.assertEqual(len(self.billing.ledger(self.tenant_a)), 2)
        with self.db.read() as conn:
            audit = (
                conn.execute(
                    select(billing_audit).where(
                        billing_audit.c.action == "billing.unblock"
                    )
                )
                .mappings()
                .one()
            )
        self.assertIn("Accepted excess cost", audit["details"])
        self.assertIn("call-1", audit["details"])
        self.reserve("resumed")


class RedisBillingTest(unittest.TestCase):
    """Exercise real Lua atomics when the local redis-server executable exists."""

    @classmethod
    def setUpClass(cls):
        isolated_url = os.getenv("PLATFORM_ACCEPTANCE_REDIS_URL")
        if isolated_url:
            if os.getenv("PLATFORM_ACCEPTANCE_REDIS_ISOLATED") != "1":
                raise RuntimeError(
                    "Acceptance Redis must be explicitly marked isolated"
                )
            cls.temporary = tempfile.TemporaryDirectory(
                prefix="platform-redis-", dir="/tmp"
            )
            cls.process = None
            cls.url = isolated_url
            cls.limiter = RedisLimiter(cls.url)
            cls.limiter.client.ping()
            return
        executable = shutil.which("redis-server")
        if not executable:
            raise unittest.SkipTest("redis-server is not installed")
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="platform-redis-", dir="/tmp"
        )
        socket_path = str(Path(cls.temporary.name) / "redis.sock")
        cls.url = f"unix://{socket_path}"
        cls.process = subprocess.Popen(
            [
                executable,
                "--port",
                "0",
                "--unixsocket",
                socket_path,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                cls.temporary.name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.limiter = RedisLimiter(cls.url)
        for _ in range(100):
            try:
                cls.limiter.client.ping()
                return
            except RedisConnectionError:
                time.sleep(0.02)
        cls.tearDownClass()
        raise RuntimeError("Local isolated Redis did not start")

    @classmethod
    def tearDownClass(cls):
        cls.limiter.client.close()
        if cls.process is not None:
            cls.process.terminate()
            cls.process.wait(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.tenant = uuid.uuid4().hex

    def policy(self, scope="tenant", subject=None, rpm=None, tpm=None, concurrent=None):
        return {
            "scope": scope,
            "subject_id": subject or self.tenant,
            "rpm": rpm,
            "tpm": tpm,
            "concurrent": concurrent,
        }

    def test_lua_rejection_does_not_partially_consume_other_dimensions(self):
        policies = [self.policy(rpm=2), self.policy("user", "user-a", rpm=0)]
        with self.assertRaises(PlatformError):
            self.limiter.admit(self.tenant, "denied", 100, policies)
        tenant_keys = self.limiter._keys(self.tenant, [f"tenant:{self.tenant}"])
        self.assertEqual(self.limiter.client.zcard(tenant_keys[0]), 0)
        policies[1]["rpm"] = 1
        self.limiter.admit(self.tenant, "accepted", 100, policies)
        with self.assertRaises(PlatformError):
            self.limiter.admit(self.tenant, "denied-again", 100, policies)
        self.assertEqual(self.limiter.client.zcard(tenant_keys[0]), 1)

    def test_lua_concurrent_admissions_have_an_atomic_capacity_bound(self):
        barrier = Barrier(8)
        policies = [self.policy(concurrent=2)]

        def attempt(index):
            barrier.wait()
            try:
                self.limiter.admit(self.tenant, f"call-{index}", 100, policies)
                return "accepted"
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertEqual(results.count("accepted"), 2)
        self.assertEqual(results.count("concurrent_limit"), 6)

    def test_late_usage_does_not_refund_tokens_to_a_new_window(self):
        policy = self.policy(tpm=100)
        scopes = [f"tenant:{self.tenant}"]
        keys = self.limiter._keys(self.tenant, scopes)
        self.limiter.admit(self.tenant, "old", 100, [policy])
        self.limiter.client.zadd(keys[0], {"old": time.time() - 70})
        self.limiter.admit(self.tenant, "current", 100, [policy])
        self.limiter.finish(self.tenant, "old", scopes, 0)
        self.assertEqual(self.limiter.client.hgetall(keys[1]), {"current": "100"})
        with self.assertRaises(PlatformError) as caught:
            self.limiter.admit(self.tenant, "excess", 1, [policy])
        self.assertEqual(caught.exception.code, "tpm_limit")

    def test_unknown_usage_releases_redis_capacity_without_refunding_rate_tokens(self):
        settings = Settings(
            database_url=f"sqlite:///{Path(self.temporary.name) / (self.tenant + '.db')}",
            admin_email="owner@redis-billing.test",
            admin_password="temporary-test-password-42",
            operator_emails=("owner@redis-billing.test",),
            litellm_url="http://127.0.0.1:1",
            export_directory=Path(self.temporary.name) / "exports",
            web_directory=Path(self.temporary.name) / "web",
        )
        migrate(settings.database_url)
        db = Database(settings.database_url)
        try:
            fixture = MultiTenantFixture(db, settings)
            tenant, actor = fixture.tenant_id, fixture.owner
            model = fixture.create_model()
            session = fixture.create_session(actor)
            run = fixture.create_run(actor, session)
            billing = BillingService(db, redis_url=self.url)
            billing.credit(tenant, actor["id"], "1", "Test funds", "credit")
            billing.set_quota(tenant, "tenant", tenant, None, None, 1, None)
            billing.reserve(
                tenant, actor["id"], model["id"], run["id"], "old", 100, 100, "1", "1"
            )
            billing.unresolved(tenant, "old", "Unknown remote state")
            scopes = [f"entitlement:{tenant}", f"tenant:{tenant}"]
            keys = self.limiter._keys(tenant, scopes)
            for index in range(0, len(keys), 3):
                self.assertIsNone(self.limiter.client.zscore(keys[index + 2], "old"))
                self.assertIsNotNone(self.limiter.client.zscore(keys[index], "old"))
                self.assertEqual(
                    self.limiter.client.hget(keys[index + 1], "old"), "200"
                )
            billing.reserve(
                tenant, actor["id"], model["id"], run["id"], "new", 100, 100, "1", "1"
            )
            # Losing Redis state still cannot admit a second executing call.
            self.limiter.client.delete(*keys)
            self.assertTrue(all(self.limiter.client.exists(key) == 0 for key in keys))
            with self.assertRaises(PlatformError) as caught:
                billing.reserve(
                    tenant,
                    actor["id"],
                    model["id"],
                    run["id"],
                    "excess",
                    100,
                    100,
                    "1",
                    "1",
                )
            self.assertEqual(caught.exception.code, "concurrent_limit")
            self.assertEqual(
                Decimal(billing.wallet(tenant)["reserved"]), Decimal("0.0004")
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
