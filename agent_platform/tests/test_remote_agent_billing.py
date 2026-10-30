"""Remote money is admitted and settled as money, with independent fee evidence."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from sqlalchemy import func, select

from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.billing.service import (
    BillingService,
    ledger_entries,
    ledger_transactions,
    reservations_table,
)
from agent_platform.modules.costs import CostService
from agent_platform.tests import test_billing as fixtures


class RemoteBillingTests(unittest.TestCase):
    setUp = fixtures.BillingTest.setUp
    tearDown = fixtures.BillingTest.tearDown
    credit = fixtures.BillingTest.credit

    def budget(self, amount="10"):
        CostService(self.db, authorize=lambda conn: None).configure_budget(
            "platform",
            "platform",
            amount,
            self.admin,
        )

    def reserve(self, call="remote-1", amount="0.2", tenant=None):
        tenant = tenant or self.tenant_a
        return self.billing.reserve_fixed(
            tenant,
            self.user_a,
            "remote-registration",
            self.runs[(tenant, self.user_a)],
            call,
            quote_id="quote-v1",
            max_charge=amount,
        )

    @staticmethod
    def fee(call="remote-1", amount="0.125", **changes):
        return {
            "receipt_id": "receipt-1",
            "request_id": call,
            "quote_id": "quote-v1",
            "amount": amount,
            "currency": "USD",
            "final": True,
            **changes,
        }

    def ready(self):
        self.credit()
        self.budget()

    def cost_budget(self):
        with self.db.read() as conn:
            return dict(conn.execute(select(mt.cost_budgets)).mappings().one())

    def test_fixed_hold_and_settlement_use_exact_money_without_token_usage(self):
        self.ready()
        reserved = self.reserve(amount="0.200000000001")
        self.assertEqual(reserved["charge_kind"], "fixed")
        self.assertEqual(reserved["quote_id"], "quote-v1")
        self.assertEqual(reserved["rate_tokens"], 0)
        self.assertEqual(Decimal(reserved["amount"]), Decimal("0.200000000001"))
        self.assertEqual(self.cost_budget()["reserved"], Decimal("0.200000000001"))
        result = self.billing.settle_fixed(
            self.tenant_a, "remote-1", self.fee(amount="0.125000000001")
        )
        self.assertEqual(Decimal(result["cost"]), Decimal("0.125000000001"))
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (0, 0))
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["balance"]),
            Decimal("9.874999999999"),
        )
        self.assertEqual(self.cost_budget()["reserved"], Decimal(0))
        self.assertEqual(self.cost_budget()["spent"], Decimal("0.125000000001"))
        with self.db.read() as conn:
            receipt = conn.execute(select(mt.usage_receipts)).mappings().one()
            self.assertEqual(
                (receipt["source"], receipt["quality"]),
                ("remote_agent", "remote_final"),
            )
            self.assertEqual(receipt["evidence"]["fee"]["amount"], "0.125000000001")
            hold = conn.execute(select(mt.cost_holds)).mappings().one()
            self.assertIsNone(hold["deployment_id"])
            self.assertIsNone(hold["price_version_id"])
            self.assertEqual(hold["quality"], "remote_final")
            self.assertEqual(sum(conn.scalars(select(ledger_entries.c.amount))), 0)

    def test_quote_bound_idempotency_and_concurrent_receipt_delivery(self):
        self.ready()
        first = self.reserve()
        self.assertEqual(first, self.reserve(amount="0.200"))
        with self.assertRaises(PlatformError) as failure:
            self.reserve(amount="0.3")
        self.assertEqual(failure.exception.code, "idempotency_conflict")
        barrier = Barrier(2)

        def settle(_):
            barrier.wait()
            return self.billing.settle_fixed(self.tenant_a, "remote-1", self.fee())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(settle, range(2)))
        self.assertTrue(all(row["status"] == "settled" for row in results))
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["balance"]), Decimal("9.875")
        )
        with self.db.read() as conn:
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(mt.usage_receipts)), 1
            )
            self.assertEqual(
                conn.scalar(
                    select(func.count())
                    .select_from(ledger_transactions)
                    .where(ledger_transactions.c.type == "charge")
                ),
                1,
            )

    def test_wrong_request_quote_and_excessive_fee_keep_holds_and_evidence(self):
        self.ready()
        for index, change in enumerate(
            (
                {"request_id": "another-request"},
                {"quote_id": "changed-quote"},
                {"amount": "0.3"},
            )
        ):
            call = f"remote-{index}"
            with self.subTest(change=change):
                self.reserve(call)
                with self.assertRaises(PlatformError) as failure:
                    self.billing.settle_fixed(
                        self.tenant_a, call, self.fee(call, **change)
                    )
                self.assertEqual(failure.exception.code, "settlement_conflict")
                with self.db.read() as conn:
                    row = (
                        conn.execute(
                            select(reservations_table).where(
                                reservations_table.c.call_id == call
                            )
                        )
                        .mappings()
                        .one()
                    )
                    self.assertEqual(row["status"], "unresolved")
                with self.assertRaises(PlatformError):
                    self.billing.release(self.tenant_a, call, "cannot assume not sent")
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.6")
        )
        self.assertEqual(self.cost_budget()["reserved"], Decimal("0.6"))
        with self.db.read() as conn:
            self.assertEqual(
                set(conn.scalars(select(mt.usage_receipts.c.quality))), {"conflict"}
            )

    def test_changed_receipt_never_recharges_or_rewrites_confirmed_amount(self):
        self.ready()
        self.reserve()
        self.billing.settle_fixed(self.tenant_a, "remote-1", self.fee())
        for fee in (
            self.fee(amount="0.1"),
            self.fee(amount="0.19", receipt_id="new-receipt"),
        ):
            with self.assertRaises(PlatformError):
                self.billing.settle_fixed(self.tenant_a, "remote-1", fee)
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["balance"]), Decimal("9.875")
        )
        self.assertEqual(self.cost_budget()["spent"], Decimal("0.125"))
        with self.db.read() as conn:
            self.assertEqual(
                conn.scalar(select(func.count()).select_from(mt.usage_receipts)), 3
            )

    def test_model_settlement_and_manual_tokens_cannot_zero_out_remote_hold(self):
        self.ready()
        self.reserve()
        with self.assertRaises(PlatformError) as failure:
            self.billing.settle(self.tenant_a, "remote-1", 0, 0)
        self.assertEqual(failure.exception.code, "charge_kind_mismatch")
        self.billing.unresolved(self.tenant_a, "remote-1", "remote response lost")
        with self.assertRaises(PlatformError) as failure:
            self.billing.resolve(
                self.tenant_a,
                "remote-1",
                0,
                0,
                action="confirm",
                reason="checked response",
                actor_id=self.admin,
                idempotency_key="manual-1",
            )
        self.assertEqual(failure.exception.code, "charge_kind_mismatch")
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.2")
        )
        self.billing.settle_fixed(self.tenant_a, "remote-1", self.fee())
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)

    def test_unknown_write_off_keeps_supplier_bound_but_releases_customer_hold(self):
        self.ready()
        self.reserve()
        self.billing.unresolved(self.tenant_a, "remote-1", "lost status")
        self.billing.resolve(
            self.tenant_a,
            "remote-1",
            action="write_off",
            reason="operator accepts unknown supplier expense",
            actor_id=self.admin,
            idempotency_key="waive-1",
        )
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["balance"]), 10)
        self.assertEqual(self.cost_budget()["spent"], Decimal("0.2"))
        with self.assertRaises(PlatformError):
            self.billing.settle_fixed(self.tenant_a, "remote-1", self.fee())

    def test_manual_amount_confirmation_is_bounded_audited_and_idempotent(self):
        self.ready()
        self.reserve()
        self.billing.unresolved(
            self.tenant_a, "remote-1", "operator must check invoice"
        )
        arguments = {
            "action": "confirm",
            "reason": "verified final invoice amount",
            "actor_id": self.admin,
            "idempotency_key": "manual-fixed",
        }
        with self.assertRaises(PlatformError) as failure:
            self.billing.resolve(
                self.tenant_a, "remote-1", fixed_amount="0.3", **arguments
            )
        self.assertEqual(failure.exception.code, "quote_exceeded")
        first = self.billing.resolve(
            self.tenant_a, "remote-1", fixed_amount="0.15", **arguments
        )
        second = self.billing.resolve(
            self.tenant_a, "remote-1", fixed_amount="0.150", **arguments
        )
        self.assertEqual(first, second)
        self.assertEqual(Decimal(first["cost"]), Decimal("0.15"))
        self.assertEqual(first["raw_usage"]["quote_id"], "quote-v1")
        self.assertEqual(self.cost_budget()["spent"], Decimal("0.15"))
        with self.assertRaises(PlatformError):
            self.billing.resolve(
                self.tenant_a, "remote-1", fixed_amount="0.14", **arguments
            )
        with self.db.read() as conn:
            self.assertEqual(
                conn.execute(select(mt.usage_receipts.c.quality)).scalar_one(), "manual"
            )

    def test_missing_cost_budget_rolls_back_customer_reservation(self):
        self.credit()
        with self.assertRaises(PlatformError) as failure:
            self.reserve()
        self.assertEqual(failure.exception.code, "cost_budget_missing")
        self.assertEqual(Decimal(self.billing.wallet(self.tenant_a)["reserved"]), 0)
        self.assertEqual(self.billing.reservations(self.tenant_a), [])

    def test_two_tenants_compete_for_last_platform_budget_atomically(self):
        self.credit()
        self.credit(tenant=self.tenant_b)
        self.budget("0.3")
        barrier = Barrier(2)

        def reserve(tenant):
            barrier.wait()
            try:
                return self.reserve(call=tenant, tenant=tenant)["status"]
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (self.tenant_a, self.tenant_b)))
        self.assertCountEqual(results, ["reserved", "provider_budget_exceeded"])
        self.assertEqual(self.cost_budget()["reserved"], Decimal("0.2"))
        self.assertEqual(
            sum(
                Decimal(self.billing.wallet(tenant)["reserved"])
                for tenant in (self.tenant_a, self.tenant_b)
            ),
            Decimal("0.2"),
        )

    def test_unsent_release_returns_both_holds_and_zero_fee_is_final(self):
        self.ready()
        self.reserve()
        self.billing.release(
            self.tenant_a, "remote-1", "failed before transport authorization"
        )
        self.assertEqual(self.cost_budget()["reserved"], 0)
        self.reserve("free", amount="0")
        result = self.billing.settle_fixed(self.tenant_a, "free", self.fee("free", "0"))
        self.assertEqual(result["status"], "settled")
        self.assertEqual(self.cost_budget()["spent"], 0)

    def test_cross_tenant_fee_cannot_settle_another_wallet(self):
        self.ready()
        self.reserve()
        with self.assertRaises(PlatformError):
            self.billing.settle_fixed(self.tenant_b, "remote-1", self.fee())
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant_a)["reserved"]), Decimal("0.2")
        )

    def test_remote_fee_cannot_settle_model_reservation(self):
        self.credit()
        fixtures.BillingTest.reserve(self, call="model-call")
        with self.assertRaises(PlatformError) as failure:
            self.billing.settle_fixed(
                self.tenant_a, "model-call", self.fee("model-call")
            )
        self.assertEqual(failure.exception.code, "charge_kind_mismatch")

    def test_authorization_failure_precedes_any_financial_mutation(self):
        self.ready()

        def revoked(conn):
            raise PlatformError(403, "access_revoked", "revoked")

        guarded = BillingService(self.db, authorize=revoked)
        with self.assertRaises(PlatformError):
            guarded.reserve_fixed(
                self.tenant_a,
                self.user_a,
                "remote-registration",
                self.runs[(self.tenant_a, self.user_a)],
                "denied",
                quote_id="quote-v1",
                max_charge="0.2",
            )
        self.assertEqual(self.billing.reservations(self.tenant_a), [])
        self.assertEqual(self.cost_budget()["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
