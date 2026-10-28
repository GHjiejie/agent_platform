"""Wire contract drift and finance APIs against isolated migrated storage."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update

from agent_platform.apps.api.main import create_app
from agent_platform.apps.api.responses import WalletResponse
from agent_platform.infrastructure import metering_tables as mt
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.modules.billing.service import BillingService
from agent_platform.scripts.api_contract import OUTPUT, generate
from agent_platform.tests.fixtures import PASSWORD, MultiTenantFixture, migrate


class ContractDefinitionTests(unittest.TestCase):
    def test_frontend_contract_is_generated_from_actual_routes(self):
        generated = generate()
        self.assertEqual(OUTPUT.read_text(), generated, "Run scripts.api_contract")
        self.assertIn('"GET /api/v2/tenants/{tenant_id}/runs": RunsResponse', generated)
        self.assertIn('"GET /api/v2/me": AuthResponse', generated)

    def test_money_keeps_precision_and_rejects_floats_and_invalid_values(self):
        values = {
            "balance": Decimal("900000.123456789012"),
            "reserved": "0",
            "available": "1",
            "currency": "USD",
            "blocked": False,
            "block_reason": "",
        }
        self.assertEqual(
            WalletResponse(**values).model_dump(mode="json")["balance"],
            "900000.123456789012",
        )
        for invalid in (0.1, float("nan"), "NaN", "Infinity", "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                WalletResponse(**{**values, "balance": invalid})


class FinanceAPIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="api-contract-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temp.name) / 'platform.db'}",
            admin_email="owner@contract.test",
            admin_password=PASSWORD,
            operator_emails=("owner@contract.test",),
            secret_encryption_key=Fernet.generate_key().decode(),
        )
        migrate(self.settings.database_url)
        self.app = create_app(self.settings)
        self.db = self.app.state.database
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.platform = self.app.state.platform
        self.tenant = self.fixture.tenant_id
        self.model = self.fixture.create_model()
        session = self.fixture.create_session()
        self.run = self.fixture.create_run(self.fixture.owner, session)
        self.deployment = self.model["deployment_id"]
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.login(self.settings.admin_email)

    def login(self, email):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]
        return response.json()

    def request(self, method, path, **kwargs):
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        return self.client.request(
            method, "/api/v2/platform" + path, headers=headers, **kwargs
        )

    def reserve(self, call="contract-call"):
        billing = BillingService(self.db)
        billing.credit(
            self.tenant,
            self.fixture.owner["id"],
            "10",
            "Isolated test funds",
            "contract-credit",
        )
        billing.reserve(
            self.tenant,
            self.fixture.owner["id"],
            self.model["id"],
            self.run["id"],
            call,
            10,
            10,
            "1",
            "2",
            deployment_id=self.deployment,
        )
        return billing

    def test_receipts_are_exact_and_finance_only_and_tenant_scoped(self):
        billing = self.reserve()
        billing.settle(
            self.tenant,
            "contract-call",
            10,
            5,
            "0.123456789012",
            {
                "usage_source": "litellm_gateway",
                "source_event_id": "event-a",
                "attempt_id": "attempt-a",
                "gateway_request_id": "gateway-a",
            },
        )
        path = f"/tenants/{self.tenant}/billing/calls/contract-call/receipts"
        response = self.request("GET", path)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()["items"][0]
        self.assertEqual(receipt["provider_cost"], "0.123456789012")
        self.assertEqual(receipt["attempt_id"], "attempt-a")
        other = self.request(
            "GET",
            f"/tenants/{self.fixture.tenant_b}/billing/calls/contract-call/receipts",
        )
        self.assertEqual(other.json(), {"items": []})
        self.login(self.fixture.member["email"])
        self.assertEqual(self.request("GET", path).status_code, 403)
        self.assertEqual(self.request("GET", "/costs").status_code, 403)

    def test_cost_versions_and_revoked_finance_fail_closed(self):
        response = self.request(
            "POST",
            f"/deployments/{self.deployment}/cost-prices",
            json={
                "input_price": "1.000000000001",
                "output_price": "2",
                "expected_version": 0,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["input_price"], "1.000000000001")
        stale = self.request(
            "POST",
            f"/deployments/{self.deployment}/cost-prices",
            json={"input_price": "5", "output_price": "6", "expected_version": 0},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(self.request("GET", "/costs").json()["prices"]), 1)
        finance = self.fixture.provision_user(
            "finance@contract.test", role="platform_finance"
        )
        self.login(finance["email"])
        self.assertEqual(
            self.request(
                "PUT",
                "/costs/budgets/platform/platform",
                json={"limit": "10", "expected_version": 0},
            ).status_code,
            200,
        )
        with self.db.transaction("identity:" + finance["id"]) as connection:
            connection.execute(
                update(nt.platform_roles)
                .where(nt.platform_roles.c.user_id == finance["id"])
                .values(revoked_at="2026-10-07T00:00:00+00:00")
            )
        self.assertEqual(
            self.request(
                "PUT",
                "/costs/budgets/platform/platform",
                json={"limit": "100", "expected_version": 1},
            ).status_code,
            403,
        )
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(mt.cost_budgets.c.limit)), Decimal(10)
            )

    def test_rebuild_repairs_projection_from_immutable_charge_ledger(self):
        billing = self.reserve()
        billing.settle(self.tenant, "contract-call", 10, 5)
        with (
            self.db.tenant_scope(self.tenant),
            self.db.transaction("tenant:" + self.tenant) as connection,
        ):
            connection.execute(
                update(mt.budget_totals)
                .where(mt.budget_totals.c.tenant_id == self.tenant)
                .values(spent=Decimal(999))
            )
        response = self.request(
            "POST",
            f"/tenants/{self.tenant}/billing/budget-projection/rebuild",
            json={"reason": "Verify projection repair"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(Decimal(response.json()["spent"]), Decimal("0.00002"))
        with self.db.read() as connection:
            self.assertEqual(
                set(connection.scalars(select(mt.budget_totals.c.spent))),
                {Decimal("0.00002")},
            )
        self.assertEqual(len(billing.ledger(self.tenant)), 2)

    def test_diagnostics_require_current_operator_and_finance(self):
        response = self.request("GET", f"/tenants/{self.tenant}/diagnostics")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["storage"]["visibility"], "not_available")
        self.assertNotIn("message", response.text)
        self.login(self.fixture.member["email"])
        self.assertEqual(
            self.request("GET", f"/tenants/{self.tenant}/diagnostics").status_code, 403
        )

    def test_cost_review_keeps_unknown_holds_and_requires_funded_budget(self):
        response = self.request(
            "POST",
            f"/deployments/{self.deployment}/cost-prices",
            json={
                "input_price": "100000",
                "output_price": "100000",
                "expected_version": 0,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            self.request(
                "PUT",
                "/costs/budgets/platform/platform",
                json={"limit": "10", "expected_version": 0},
            ).status_code,
            200,
        )
        billing = self.reserve()
        billing.unresolved(self.tenant, "contract-call", "Unknown supplier consumption")
        path = "/costs/budgets/platform/platform/review"
        unknown = self.request(
            "POST", path, json={"reason": "Review unknown hold", "expected_version": 1}
        )
        self.assertEqual(unknown.status_code, 409, unknown.text)
        self.assertEqual(unknown.json()["error"]["code"], "unresolved_cost")
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(mt.cost_budgets.c.reserved)), Decimal(2)
            )
        billing.resolve(
            self.tenant,
            "contract-call",
            action="write_off",
            actor_id=self.fixture.owner["id"],
            reason="Platform accepts conservative supplier bound",
            idempotency_key="contract-writeoff",
        )
        self.assertEqual(
            self.request(
                "PUT",
                "/costs/budgets/platform/platform",
                json={"limit": "1", "expected_version": 1},
            ).status_code,
            200,
        )
        exceeded = self.request(
            "POST", path, json={"reason": "Review spent cost", "expected_version": 2}
        )
        self.assertEqual(exceeded.status_code, 409, exceeded.text)
        self.assertEqual(exceeded.json()["error"]["code"], "cost_budget_exceeded")
        self.assertEqual(
            self.request(
                "PUT",
                "/costs/budgets/platform/platform",
                json={"limit": "10", "expected_version": 2},
            ).status_code,
            200,
        )
        with self.db.transaction("cost:admission") as connection:
            connection.execute(
                update(mt.cost_budgets).values(spent=Decimal(9), blocked=True)
            )
        reviewed = self.request(
            "POST",
            path,
            json={
                "reason": "Acknowledge conservative writeoff and repair projection",
                "expected_version": 3,
            },
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(Decimal(reviewed.json()["spent"]), Decimal(2))
        self.assertEqual(Decimal(reviewed.json()["reserved"]), Decimal(0))
        self.assertFalse(reviewed.json()["blocked"])
        self.assertEqual(reviewed.json()["version"], 4)
        self.assertEqual(len(billing.ledger(self.tenant)), 1)

    def test_v2_responses_drop_private_run_spec_and_identity_fields(self):
        identity = self.client.get("/api/v2/me").json()
        self.assertNotIn("password_hash", identity["user"])
        response = self.client.get(
            f"/api/v2/tenants/{self.tenant}/runs/{self.run['id']}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("spec", response.json())
        self.assertNotIn("request_hash", response.json())
        self.assertEqual(response.json()["cost"], "0")


if __name__ == "__main__":
    unittest.main()
