"""Independent supplier budgets and append-only metering on isolated real storage."""

from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import select

from agent_platform.infrastructure import gateway_tables as gt
from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules import metering
from agent_platform.modules.billing.service import BillingService, reservations_table
from agent_platform.modules.costs import CostService
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.run_records import now
from agent_platform.tests.fixtures import MultiTenantFixture, migrate


class LogsAdapter:
    """Controlled log evidence, never a model endpoint or real credential."""

    def __init__(self, payload, callback=None):
        self.payload, self.callback, self.requests = payload, callback, []

    def _request(self, path):
        self.requests.append(path)
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        return copy.deepcopy(self.payload)


class DateFilteredLogsAdapter(LogsAdapter):
    """LiteLLM v2 parses UTC dates and applies inclusive startTime bounds."""

    def _request(self, path):
        query = parse_qs(urlsplit(path).query)
        start, end = (
            datetime.strptime(query[name][0], "%Y-%m-%d").replace(tzinfo=UTC)
            for name in ("start_date", "end_date")
        )
        payload = super()._request(path)
        payload["data"] = [
            row
            for row in payload["data"]
            if start <= datetime.fromisoformat(row["startTime"]) <= end
        ]
        payload["total"] = len(payload["data"])
        return payload


class MeteringCostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="metering-costs-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temporary.name) / 'platform.db'}",
            admin_email="owner@example.test",
            admin_password="temporary-test-password-42",
            operator_emails=("owner@example.test",),
            litellm_url="http://127.0.0.1:1",
            secret_encryption_key=Fernet.generate_key().decode(),
            export_directory=Path(self.temporary.name) / "exports",
            web_directory=Path(self.temporary.name) / "web",
        )
        migrate(self.settings.database_url)
        self.db = Database(self.settings.database_url)
        self.addCleanup(self.db.close)
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.tenant = self.fixture.tenant_id
        self.other = self.fixture.tenant_b
        self.actor = self.fixture.owner
        self.gateway = GatewayService(self.db, self.settings)
        self.costs = CostService(
            self.db,
            authorize=lambda connection: self.gateway._authorize(
                connection, self.actor["id"], "platform.billing.manage"
            ),
        )
        self.billing = BillingService(self.db)
        self.models, self.runs = {}, {}
        for tenant in (self.tenant, self.other):
            actor = self.fixture.context(self.actor["id"], tenant)
            model = self.fixture.create_model(tenant_id=tenant, alias="financial-model")
            session = self.fixture.create_session(actor=actor)
            run = self.fixture.create_run(actor, session)
            self.models[tenant], self.runs[tenant] = model, run
            self.billing.credit(
                tenant, self.actor["id"], "100", "Isolated test funds", "test-credit"
            )
            self.billing.set_quota(tenant, "tenant", tenant, None, None, 10, None)
        self.deployment = self.models[self.tenant]["deployment_id"]
        self.route = next(
            row["internal_route"]
            for row in self.rows(gt.deployments)
            if row["id"] == self.deployment
        )
        self.costs.configure_price(
            self.deployment, "100000", "100000", self.actor["id"]
        )
        self.costs.configure_budget("platform", "platform", "10", self.actor["id"])

    def rows(self, table):
        with self.db.read() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def budget(self, scope="platform", subject="platform"):
        return next(
            row
            for row in self.rows(mt.cost_budgets)
            if row["scope"] == scope and row["subject_id"] == subject
        )

    def hold(self, call_id):
        return next(
            row for row in self.rows(mt.cost_holds) if row["call_id"] == call_id
        )

    def reserve(
        self,
        call_id="call-a",
        tenant=None,
        *,
        input_price="0",
        output_price="0",
        deployment=None,
    ):
        tenant = tenant or self.tenant
        model, run = self.models[tenant], self.runs[tenant]
        return self.billing.reserve(
            tenant,
            self.actor["id"],
            model["id"],
            run["id"],
            call_id,
            10,
            10,
            input_price,
            output_price,
            deployment_id=deployment or model["deployment_id"],
            require_cost_policy=True,
        )

    def usage(self, event="gateway-a", *, source="litellm_gateway", attempt=None):
        return {
            "usage_source": source,
            "source_event_id": event,
            "attempt_id": attempt or "attempt-call-a",
            "gateway_request_id": event,
        }

    def prepare_log_job(self, call_id="call-a", *, input_price="1", output_price="2"):
        self.reserve(call_id, input_price=input_price, output_price=output_price)
        model, run = self.models[self.tenant], self.runs[self.tenant]
        attempt = "attempt-" + call_id
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                t.calls.insert().values(
                    id=call_id,
                    tenant_id=self.tenant,
                    user_id=self.actor["id"],
                    run_id=run["id"],
                    model_id=model["id"],
                    model=model["alias"],
                    agent_name="Finance fixture",
                    user_email=self.actor["email"],
                    status="running",
                    input_tokens=0,
                    output_tokens=0,
                    cost="0",
                    price_version=model["price_version"],
                    error="",
                    created_at=now(),
                )
            )
            connection.execute(
                rt.call_attempts.insert().values(
                    id=attempt,
                    tenant_id=self.tenant,
                    run_id=run["id"],
                    logical_call_id=call_id,
                    membership_id=self.actor["membership_id"],
                    deployment_id=self.deployment,
                    config_version=1,
                    route=self.route,
                    created_at=now(),
                )
            )
            metering.prepare_reconciliation(connection, self.tenant, call_id, attempt)
            connection.execute(
                mt.reconciliation_jobs.update()
                .where(
                    mt.reconciliation_jobs.c.tenant_id == self.tenant,
                    mt.reconciliation_jobs.c.attempt_id == attempt,
                )
                .values(next_attempt_at=0)
            )
        self.billing.unresolved(self.tenant, call_id, "Stream ended without evidence")
        return attempt

    def log(self, attempt="attempt-call-a", **changes):
        return {
            "end_user": "agent-platform-attempt-" + attempt,
            "model_group": self.route,
            "status": "success",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "request_id": "recovered-gateway-a",
            "spend": "0.3",
            **changes,
        }

    def job(self):
        return self.rows(mt.reconciliation_jobs)[0]

    def reconcile(self, rows, *, total=None, callback=None):
        adapter = LogsAdapter(
            {"data": rows, "total": total if total is not None else len(rows)}, callback
        )
        reconciler = metering.MeteringReconciler(self.db, self.settings, adapter)
        self.assertTrue(reconciler.process_one())
        return adapter, reconciler

    def test_zero_customer_price_still_reserves_supplier_budget(self):
        receipt = self.reserve()
        self.assertEqual(Decimal(receipt["amount"]), 0)
        self.assertEqual(Decimal(self.billing.wallet(self.tenant)["reserved"]), 0)
        self.assertEqual(self.budget()["reserved"], Decimal(2))
        self.costs.configure_budget("platform", "platform", "2", self.actor["id"])
        with self.assertRaises(PlatformError) as rejected:
            self.reserve("call-b", tenant=self.other)
        self.assertEqual(rejected.exception.code, "provider_budget_exceeded")
        self.assertEqual(len(self.rows(reservations_table)), 1)
        self.assertEqual(len(self.rows(mt.cost_holds)), 1)

    def test_cross_tenant_concurrency_cannot_overspend_shared_platform_budget(self):
        self.costs.configure_budget("platform", "platform", "2", self.actor["id"])
        barrier = threading.Barrier(8)

        def admit(index):
            barrier.wait(timeout=5)
            try:
                self.reserve(
                    "parallel-" + str(index),
                    tenant=self.tenant if index % 2 else self.other,
                )
                return "admitted"
            except PlatformError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(admit, range(8)))
        self.assertEqual(results.count("admitted"), 1)
        self.assertEqual(results.count("provider_budget_exceeded"), 7)
        self.assertEqual(self.budget()["reserved"], Decimal(2))
        self.assertEqual(len(self.rows(mt.cost_holds)), 1)

    def test_deployment_budget_and_platform_budget_both_apply(self):
        self.costs.configure_budget(
            "deployment", self.deployment, "1", self.actor["id"]
        )
        with self.assertRaises(PlatformError):
            self.reserve()
        self.assertEqual(self.budget()["reserved"], 0)
        self.assertEqual(self.rows(reservations_table), [])
        self.costs.configure_budget(
            "deployment", self.deployment, "2", self.actor["id"]
        )
        self.reserve()
        self.assertEqual(self.budget()["reserved"], 2)
        self.assertEqual(self.budget("deployment", self.deployment)["reserved"], 2)

    def test_unknown_holds_both_cost_budget_and_customer_money(self):
        receipt = self.reserve(input_price="1", output_price="2")
        self.billing.unresolved(self.tenant, "call-a", "Usage unknown")
        self.assertEqual(self.hold("call-a")["status"], "unresolved")
        self.assertEqual(self.budget()["reserved"], 2)
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant)["reserved"]),
            Decimal(receipt["amount"]),
        )
        with self.assertRaises(PlatformError):
            self.billing.release(self.tenant, "call-a", "A timeout is not proof")

    def test_writeoff_records_platform_loss_and_cannot_repeat_budget_change(self):
        self.reserve(input_price="1", output_price="2")
        self.billing.unresolved(self.tenant, "call-a", "No obtainable usage")
        arguments = {
            "action": "write_off",
            "reason": "Platform accepts the unknown cost",
            "actor_id": self.actor["id"],
            "idempotency_key": "writeoff-a",
        }
        self.billing.resolve(self.tenant, "call-a", **arguments)
        self.billing.resolve(self.tenant, "call-a", **arguments)
        self.assertEqual(
            (self.budget()["spent"], self.budget()["reserved"]),
            (Decimal(2), Decimal(0)),
        )
        self.assertEqual(self.hold("call-a")["quality"], "written_off_bound")
        self.assertEqual(Decimal(self.billing.wallet(self.tenant)["balance"]), 100)
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)

    def test_proven_unsent_release_frees_cost_and_customer_holds(self):
        self.reserve(input_price="1", output_price="2")
        self.billing.release(self.tenant, "call-a", "Failed before key dispatch")
        self.assertEqual(
            (self.budget()["spent"], self.budget()["reserved"]),
            (Decimal(0), Decimal(0)),
        )
        self.assertEqual(self.hold("call-a")["status"], "released")
        self.assertEqual(Decimal(self.billing.wallet(self.tenant)["reserved"]), 0)

    def test_duplicate_receipts_and_numeric_money_representations_are_idempotent(self):
        self.reserve(input_price="1", output_price="2")
        first = self.billing.settle(self.tenant, "call-a", 10, 5, "0.10", self.usage())
        self.billing.settle(self.tenant, "call-a", 10, 5, "0.1", self.usage())
        self.assertEqual(len(self.rows(mt.usage_receipts)), 1)
        self.assertEqual(len(self.billing.ledger(self.tenant)), 2)
        self.assertEqual(self.budget()["spent"], Decimal("0.1"))
        self.assertEqual(
            Decimal(self.billing.wallet(self.tenant)["balance"]),
            Decimal(100) - Decimal(first["cost"]),
        )

    def test_conflicting_receipt_is_preserved_without_recharging_or_cost_rewrite(self):
        self.reserve(input_price="1", output_price="2")
        self.billing.settle(self.tenant, "call-a", 10, 5, "0.3", self.usage())
        balance = self.billing.wallet(self.tenant)["balance"]
        for _ in range(2):
            with self.assertRaises(PlatformError) as conflict:
                self.billing.settle(self.tenant, "call-a", 11, 5, "0.4", self.usage())
            self.assertEqual(conflict.exception.code, "settlement_conflict")
        self.assertEqual(len(self.rows(mt.usage_receipts)), 2)
        self.assertEqual(
            sum(row["quality"] == "conflict" for row in self.rows(mt.usage_receipts)), 1
        )
        self.assertEqual(self.billing.wallet(self.tenant)["balance"], balance)
        self.assertEqual(self.budget()["spent"], Decimal("0.3"))

    def test_late_reconciliation_cleans_stale_redis_capacity_and_adjusts_tokens(self):
        redis_url = os.getenv("PLATFORM_ACCEPTANCE_REDIS_URL")
        if not redis_url:
            self.skipTest("Real isolated Redis is required for late metering cleanup")
        if os.getenv("PLATFORM_ACCEPTANCE_REDIS_ISOLATED") != "1":
            raise RuntimeError("Redis must be explicitly isolated")
        self.billing = BillingService(self.db, redis_url)
        limiter = self.billing._limiter
        self.addCleanup(limiter.client.close)
        self.billing.set_quota(self.tenant, "tenant", self.tenant, None, None, 1, None)
        self.prepare_log_job()
        reservation = self.billing.reservations(self.tenant)[0]
        keys = limiter._keys(self.tenant, reservation["rate_scopes"])
        for index in range(2, len(keys), 3):
            self.assertIsNone(limiter.client.zscore(keys[index], "call-a"))
            # Simulate a missed cleanup from before execution/fees were separated.
            limiter.client.zadd(keys[index], {"call-a": time.time()})
        self.assertTrue(
            all(
                limiter.client.zscore(keys[index], "call-a") is not None
                for index in range(2, len(keys), 3)
            )
        )
        self.reconcile([self.log()])
        self.assertTrue(
            metering.apply_ready_evidence(
                self.db, self.tenant, "cleanup-worker", redis_url=redis_url
            )
        )
        for index in range(0, len(keys), 3):
            self.assertIsNone(limiter.client.zscore(keys[index + 2], "call-a"))
            self.assertEqual(limiter.client.hget(keys[index + 1], "call-a"), "15")
        self.assertEqual(self.job()["status"], "applied")
        self.reserve("call-b")

    def test_late_log_cost_updates_estimate_without_second_customer_charge(self):
        self.reserve(input_price="1", output_price="2")
        self.billing.settle(self.tenant, "call-a", 10, 5, None, self.usage())
        self.assertEqual(self.budget()["spent"], Decimal("1.5"))
        balance = self.billing.wallet(self.tenant)["balance"]
        self.billing.settle(
            self.tenant,
            "call-a",
            10,
            5,
            "0.3",
            self.usage("log-a", source="litellm_logs"),
        )
        self.assertEqual(self.budget()["spent"], Decimal("0.3"))
        self.assertEqual(self.billing.wallet(self.tenant)["balance"], balance)
        self.assertEqual(len(self.billing.ledger(self.tenant)), 2)
        self.assertEqual(len(self.rows(mt.usage_receipts)), 2)

    def test_supplier_price_is_frozen_at_admission(self):
        self.reserve()
        self.costs.configure_price(
            self.deployment, "900000", "900000", self.actor["id"]
        )
        self.billing.settle(self.tenant, "call-a", 10, 5, raw_usage=self.usage())
        self.assertEqual(self.budget()["spent"], Decimal("1.5"))
        self.assertEqual(self.hold("call-a")["input_price"], Decimal(100000))

    def test_supplier_overage_blocks_future_calls_while_preserving_customer_bound(self):
        reserved = self.reserve(input_price="1", output_price="2")
        receipt = self.billing.settle(self.tenant, "call-a", 10, 5, "3", self.usage())
        self.assertLessEqual(Decimal(receipt["cost"]), Decimal(reserved["amount"]))
        self.assertTrue(self.budget()["blocked"])
        with self.assertRaises(PlatformError):
            self.reserve("call-b", tenant=self.other)

    def test_duplicate_admission_cannot_change_deployment_or_required_cost_policy(self):
        self.reserve()
        with self.assertRaises(PlatformError) as conflict:
            self.reserve(deployment="different-deployment")
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(self.budget()["reserved"], 2)

    def test_missing_required_cost_price_rolls_back_customer_reservation(self):
        with self.assertRaises(PlatformError) as missing:
            self.reserve(deployment="unpriced-deployment")
        self.assertEqual(missing.exception.code, "cost_policy_missing")
        self.assertEqual(self.rows(reservations_table), [])
        self.assertEqual(Decimal(self.billing.wallet(self.tenant)["reserved"]), 0)

    def test_trusted_logs_are_durable_before_worker_settles(self):
        attempt = self.prepare_log_job()
        adapter, _ = self.reconcile([self.log(attempt)])
        self.assertEqual(self.job()["status"], "evidence_ready")
        self.assertEqual(
            self.billing.reservations(self.tenant)[0]["status"], "unresolved"
        )
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)
        query = parse_qs(urlsplit(adapter.requests[0]).query)
        self.assertEqual(query["end_user"], ["agent-platform-attempt-" + attempt])
        self.assertEqual(query["page_size"], ["100"])
        self.assertTrue(
            metering.apply_ready_evidence(self.db, self.tenant, "worker-test")
        )
        self.assertEqual(self.job()["status"], "applied")
        self.assertEqual(self.billing.reservations(self.tenant)[0]["status"], "settled")
        self.assertEqual(self.budget()["spent"], Decimal("0.3"))
        self.assertFalse(
            metering.apply_ready_evidence(self.db, self.tenant, "worker-test")
        )
        self.assertEqual(len(self.billing.ledger(self.tenant)), 2)

    def test_log_dates_recover_fractional_usage_across_utc_day_boundaries(self):
        cases = (
            (
                "2026-10-10T00:00:00.000001+08:00",
                "2026-10-09T23:59:59.999999+00:00",
                "2026-10-09T16:00:00.000001+00:00",
                "2026-10-09",
                "2026-10-10",
            ),
            (
                "2026-10-09T23:59:59.999999-07:00",
                "2026-10-10T07:00:00.000001+00:00",
                "2026-10-10T07:00:00.000001+00:00",
                "2026-10-10",
                "2026-10-11",
            ),
            (
                "2026-10-09T23:59:59.999999+00:00",
                "2026-10-10T00:00:00.000001+00:00",
                "2026-10-10T00:00:00.000001+00:00",
                "2026-10-09",
                "2026-10-11",
            ),
            (
                "2026-10-09T23:59:59.999999",
                "2026-10-10T00:00:00.000001+00:00",
                "2026-10-09T23:59:59.999999+00:00",
                "2026-10-09",
                "2026-10-11",
            ),
        )
        for index, (created, clock, log_time, start, end) in enumerate(cases):
            with self.subTest(created=created, clock=clock):
                attempt = self.prepare_log_job(f"date-boundary-{index}")
                with self.db.transaction("tenant:" + self.tenant) as connection:
                    connection.execute(
                        rt.call_attempts.update()
                        .where(rt.call_attempts.c.id == attempt)
                        .values(created_at=created)
                    )
                adapter = DateFilteredLogsAdapter(
                    {"data": [self.log(attempt, startTime=log_time)]}
                )
                reconciler = metering.MeteringReconciler(
                    self.db, self.settings, adapter
                )
                with patch.object(metering, "datetime", wraps=datetime) as frozen:
                    frozen.now.return_value = datetime.fromisoformat(clock)
                    self.assertTrue(reconciler.process_one())
                query = parse_qs(urlsplit(adapter.requests[0]).query)
                self.assertEqual(query["start_date"], [start])
                self.assertEqual(query["end_date"], [end])
                self.assertEqual(
                    query["end_user"], ["agent-platform-attempt-" + attempt]
                )
                job = next(
                    row
                    for row in self.rows(mt.reconciliation_jobs)
                    if row["attempt_id"] == attempt
                )
                self.assertEqual(job["status"], "evidence_ready")
                self.assertEqual(
                    self.billing.reservations(self.tenant)[index]["status"],
                    "unresolved",
                )
        self.assertEqual(len(self.rows(mt.usage_receipts)), len(cases))
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)

    def test_logs_with_wrong_attempt_or_route_do_not_settle(self):
        self.prepare_log_job()
        self.reconcile(
            [
                self.log(end_user="another-tenant-attempt"),
                self.log(model_group="another-route"),
            ]
        )
        self.assertEqual(self.job()["status"], "retry_wait")
        self.assertEqual(self.rows(mt.usage_receipts), [])
        self.assertEqual(self.budget()["reserved"], 2)

    def test_duplicate_or_truncated_log_result_is_not_trusted(self):
        self.prepare_log_job()
        self.reconcile([self.log(), self.log(request_id="second-response")])
        self.assertEqual(self.job()["status"], "retry_wait")
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                mt.reconciliation_jobs.update().values(next_attempt_at=0)
            )
        self.reconcile([self.log()], total=2)
        self.assertEqual(self.rows(mt.usage_receipts), [])
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)

    def test_incomplete_failed_or_inconsistent_usage_is_not_trusted(self):
        self.prepare_log_job()
        for changes in (
            {"status": "failure"},
            {"prompt_tokens": True},
            {"total_tokens": 999},
            {"request_id": ""},
        ):
            with self.subTest(changes=changes):
                with self.db.transaction("tenant:" + self.tenant) as connection:
                    connection.execute(
                        mt.reconciliation_jobs.update().values(next_attempt_at=0)
                    )
                self.reconcile([self.log(**changes)])
                self.assertEqual(self.job()["status"], "retry_wait")
                self.assertEqual(self.rows(mt.usage_receipts), [])

    def test_expired_reconciler_cannot_publish_evidence_from_remote_response(self):
        self.prepare_log_job()

        def lose_lease():
            with self.db.transaction("tenant:" + self.tenant) as connection:
                connection.execute(
                    mt.reconciliation_jobs.update().values(
                        owner="new-owner",
                        fence=99,
                        lease_until=time.time() + 60,
                    )
                )

        self.reconcile([self.log()], callback=lose_lease)
        self.assertEqual(self.rows(mt.usage_receipts), [])
        self.assertEqual(self.job()["owner"], "new-owner")
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)

    def test_recovered_conflict_preserves_both_receipts_without_recharging(self):
        self.prepare_log_job()
        self.billing.settle(self.tenant, "call-a", 10, 5, raw_usage=self.usage())
        balance = self.billing.wallet(self.tenant)["balance"]
        self.reconcile([self.log(prompt_tokens=11, total_tokens=16)])
        metering.apply_ready_evidence(self.db, self.tenant, "worker-test")
        self.assertEqual(self.job()["status"], "conflict")
        self.assertEqual(self.billing.wallet(self.tenant)["balance"], balance)
        self.assertEqual(len(self.billing.ledger(self.tenant)), 2)
        self.assertTrue(
            any(row["quality"] == "conflict" for row in self.rows(mt.usage_receipts))
        )

    def test_cost_reservation_rolls_back_if_customer_transaction_fails(self):
        # A failure after inserting the independent hold must roll back both
        # projections as part of the real customer admission transaction.
        original = CostService.reserve

        def reserve_then_fail(service, *args, **kwargs):
            original(service, *args, **kwargs)
            raise RuntimeError("post-reservation failure")

        with (
            patch.object(CostService, "reserve", reserve_then_fail),
            self.assertRaises(RuntimeError),
        ):
            self.reserve()
        self.assertEqual(self.budget()["reserved"], 0)
        self.assertEqual(self.rows(mt.cost_holds), [])
        self.assertEqual(self.rows(reservations_table), [])

    def test_long_source_event_ids_do_not_collide_at_common_prefix(self):
        self.reserve()
        prefix = "shared-source-prefix-" * 10
        self.billing.settle(
            self.tenant, "call-a", 10, 5, raw_usage=self.usage(prefix + "a")
        )
        self.billing.settle(
            self.tenant, "call-a", 10, 5, raw_usage=self.usage(prefix + "b")
        )
        self.assertEqual(len(self.rows(mt.usage_receipts)), 2)
        self.assertTrue(
            all(row["quality"] != "conflict" for row in self.rows(mt.usage_receipts))
        )
        self.assertEqual(len(self.billing.ledger(self.tenant)), 2)

    def test_revoked_financial_role_cannot_change_cost_budget(self):
        with self.db.transaction("identity:financial-test-revoke") as connection:
            connection.execute(
                nt.platform_roles.update()
                .where(
                    nt.platform_roles.c.user_id == self.actor["id"],
                    nt.platform_roles.c.role == "platform_finance",
                )
                .values(revoked_at=now())
            )
        with self.assertRaises(PlatformError):
            self.costs.configure_budget("platform", "platform", "999", self.actor["id"])
        self.assertEqual(self.budget()["limit"], Decimal(10))

    def test_worker_with_stale_fence_cannot_settle_ready_evidence(self):
        self.prepare_log_job()
        self.reconcile([self.log()])
        original = BillingService.settle

        def settle_after_takeover(service, *args, **kwargs):
            with self.db.transaction("tenant:" + self.tenant) as connection:
                connection.execute(
                    mt.reconciliation_jobs.update().values(
                        owner="replacement-worker",
                        fence=100,
                        lease_until=time.time() + 60,
                    )
                )
            return original(service, *args, **kwargs)

        with patch.object(BillingService, "settle", settle_after_takeover):
            self.assertTrue(
                metering.apply_ready_evidence(self.db, self.tenant, "stale-worker")
            )
        self.assertEqual(self.job()["owner"], "replacement-worker")
        self.assertEqual(self.budget()["reserved"], Decimal(2))
        self.assertEqual(len(self.billing.ledger(self.tenant)), 1)


if __name__ == "__main__":
    unittest.main()
