"""Support approvals remain valid only while their current owner evidence holds."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from agent_platform.apps.api.main import create_app
from agent_platform.infrastructure import support_tables as st
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.modules.platform import now

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "temporary-password-for-tests"


class SupportAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings(
            database_url=f"sqlite:///{temporary.name}/platform.db",
            admin_email="owner@example.test",
            admin_password=PASSWORD,
            operator_emails=("owner@example.test",),
            export_directory=Path(temporary.name) / "exports",
            web_directory=Path(temporary.name) / "web",
        )
        with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": settings.database_url}):
            command.upgrade(Config(str(ROOT / "agent_platform/alembic.ini")), "head")
        self.app = create_app(settings)
        self.db = self.app.state.database
        self.platform = self.app.state.platform
        self.platform.bootstrap()
        self.client = self.enterContext(TestClient(self.app))
        owner = self.login("owner")
        self.owner_id = owner["id"]
        self.tenant_id = owner["memberships"][0]["tenant_id"]
        self.owner_membership = owner["memberships"][0]["id"]
        self.member_id = self.provision("member")
        self.staff_id = self.provision("staff")
        self.provision("otherstaff")
        self.role(self.staff_id, True)
        with self.db.read() as connection:
            otherstaff_id = connection.scalar(
                select(t.users.c.id).where(t.users.c.email == "otherstaff@example.test")
            )
        self.role(otherstaff_id, True)
        invitation = self.write(
            "post",
            f"/api/v2/tenants/{self.tenant_id}/invitations",
            {"email": "member@example.test", "role": "member"},
        ).json()
        self.login("member")
        accepted = self.write(
            "post", "/api/v2/invitations/accept", {"token": invitation["token"]}
        )
        self.member_membership = accepted.json()["id"]
        self.login("owner")
        other = self.write(
            "post",
            "/api/v2/platform/tenants",
            {"name": "Another tenant", "owner_email": "owner@example.test"},
            idempotency_key="support-test-second-tenant",
        )
        self.other_tenant_id = other.json()["id"]
        self.session_id = self.create_session(self.tenant_id)
        self.other_session_id = self.create_session(self.other_tenant_id)
        with self.db.transaction() as connection:
            for tenant_id, session_id, run_id, content in (
                (self.tenant_id, self.session_id, "run-a", "Tenant A private content"),
                (
                    self.other_tenant_id,
                    self.other_session_id,
                    "run-b",
                    "Tenant B private content",
                ),
            ):
                connection.execute(
                    insert(t.runs).values(
                        id=run_id,
                        tenant_id=tenant_id,
                        created_at=now(),
                        session_id=session_id,
                        user_id=self.owner_id,
                        agent_name="Support test",
                        spec={},
                        message=content,
                        status="succeeded",
                        idempotency_key=run_id,
                        request_hash=run_id,
                    )
                )
                connection.execute(
                    insert(t.messages).values(
                        id="message-" + run_id,
                        tenant_id=tenant_id,
                        created_at=now(),
                        session_id=session_id,
                        run_id=run_id,
                        role="user",
                        content=content,
                    )
                )
        self.grant_id = self.approve()["id"]
        self.login("staff")

    def login(self, name):
        self.client.cookies.clear()
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": f"{name}@example.test", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.headers = {"x-csrf-token": result["csrf_token"]}
        return result["user"]

    def write(self, method, path, body, *, idempotency_key=None):
        headers = dict(self.headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = getattr(self.client, method)(path, json=body, headers=headers)
        self.assertIn(response.status_code, (200, 201, 202), response.text)
        return response

    def provision(self, name):
        response = self.write(
            "post",
            "/api/v2/platform/users",
            {"email": f"{name}@example.test", "name": name, "password": PASSWORD},
        )
        return response.json()["id"]

    def role(self, user_id, active):
        return self.write(
            "post",
            f"/api/v2/platform/users/{user_id}/roles",
            {
                "role": "platform_support",
                "active": active,
                "reason": "Support regression test",
            },
        )

    def create_session(self, tenant_id):
        agents = self.client.get(f"/api/v2/tenants/{tenant_id}/agents")
        self.assertEqual(agents.status_code, 200, agents.text)
        return self.write(
            "post",
            f"/api/v2/tenants/{tenant_id}/sessions",
            {"agent_id": agents.json()["items"][0]["id"], "title": "Private session"},
        ).json()["id"]

    def approve(self, *, tenant_id=None, content=True):
        return self.write(
            "post",
            f"/api/v2/tenants/{tenant_id or self.tenant_id}/support-grants",
            {
                "staff_email": "staff@example.test",
                "reason": "Inspect a support incident",
                "allow_content": content,
            },
        ).json()

    def assert_inactive(self):
        for suffix in ("usage", "sessions/" + self.session_id):
            response = self.client.get(
                f"/api/v2/support-grants/{self.grant_id}/{suffix}"
            )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "support_grant_inactive")
        grants = self.client.get("/api/v2/support-grants")
        self.assertEqual(grants.status_code, 200, grants.text)
        self.assertNotIn(self.grant_id, [row["id"] for row in grants.json()["items"]])

    def transfer_owner(self):
        self.login("owner")
        self.write(
            "post",
            f"/api/v2/tenants/{self.tenant_id}/ownership-transfer",
            {"membership_id": self.member_membership, "expected_version": 1},
        )

    def test_valid_grant_reads_only_approved_tenant_and_audits_real_staff(self):
        usage = self.client.get(f"/api/v2/support-grants/{self.grant_id}/usage")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual([row["id"] for row in usage.json()["items"]], ["run-a"])
        content = self.client.get(
            f"/api/v2/support-grants/{self.grant_id}/sessions/{self.session_id}"
        )
        self.assertEqual(content.status_code, 200, content.text)
        self.assertEqual(
            content.json()["messages"][0]["content"], "Tenant A private content"
        )
        other = self.client.get(
            f"/api/v2/support-grants/{self.grant_id}/sessions/{self.other_session_id}"
        )
        self.assertEqual(other.status_code, 404, other.text)
        with self.db.read() as connection:
            audit = (
                connection.execute(
                    select(nt.operation_audits).where(
                        nt.operation_audits.c.action == "support.content_read"
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(audit["actor_user_id"], self.staff_id)
        self.assertEqual(audit["tenant_id"], self.tenant_id)
        self.assertEqual(audit["details"]["grant_id"], self.grant_id)

    def test_owner_transfer_invalidates_old_approval(self):
        self.transfer_owner()
        self.login("staff")
        self.assert_inactive()

    def test_new_owner_approval_is_not_hidden_by_old_invalid_grant(self):
        self.transfer_owner()
        self.login("member")
        fresh_id = self.approve()["id"]
        self.login("staff")
        listed = self.client.get("/api/v2/support-grants").json()["items"]
        self.assertEqual([row["id"] for row in listed], [fresh_id])
        response = self.client.get(f"/api/v2/support-grants/{fresh_id}/usage")
        self.assertEqual(response.status_code, 200, response.text)

    def test_owner_membership_revocation_invalidates_approval(self):
        # A second valid owner makes revoking the approver a permitted real operation.
        with self.db.transaction("tenant:" + self.tenant_id) as connection:
            connection.execute(
                update(nt.memberships)
                .where(nt.memberships.c.id == self.member_membership)
                .values(role="owner", authz_version=2)
            )
        self.login("owner")
        self.write(
            "patch",
            f"/api/v2/tenants/{self.tenant_id}/memberships/{self.owner_membership}",
            {"status": "revoked", "expected_version": 1},
        )
        self.login("staff")
        self.assert_inactive()

    def test_owner_account_active_and_global_status_are_both_required(self):
        for changes in ({"active": False}, {"global_status": "disabled"}):
            with self.subTest(changes=changes):
                with self.db.transaction("identity:" + self.owner_id) as connection:
                    connection.execute(
                        update(t.users)
                        .where(t.users.c.id == self.owner_id)
                        .values(**changes)
                    )
                self.assert_inactive()
                with self.db.transaction("identity:" + self.owner_id) as connection:
                    connection.execute(
                        update(t.users)
                        .where(t.users.c.id == self.owner_id)
                        .values(active=True, global_status="active")
                    )

    def test_membership_version_drift_invalidates_approval_even_if_still_owner(self):
        with self.db.transaction("tenant:" + self.tenant_id) as connection:
            connection.execute(
                update(nt.memberships)
                .where(nt.memberships.c.id == self.owner_membership)
                .values(authz_version=2)
            )
        self.assert_inactive()

    def test_other_tenant_and_other_member_changes_do_not_invalidate_approval(self):
        with self.db.transaction("tenant:" + self.other_tenant_id) as connection:
            connection.execute(
                update(nt.memberships)
                .where(nt.memberships.c.tenant_id == self.other_tenant_id)
                .values(authz_version=2)
            )
        self.login("owner")
        self.write(
            "patch",
            f"/api/v2/tenants/{self.tenant_id}/memberships/{self.member_membership}",
            {"role": "finance_viewer", "expected_version": 1},
        )
        self.login("staff")
        response = self.client.get(f"/api/v2/support-grants/{self.grant_id}/usage")
        self.assertEqual(response.status_code, 200, response.text)

    def test_support_role_revocation_does_not_revive_old_grants_on_regrant(self):
        self.login("owner")
        self.role(self.staff_id, False)
        self.login("staff")
        denied = self.client.get(f"/api/v2/support-grants/{self.grant_id}/usage")
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "support_role_required")
        self.login("owner")
        self.role(self.staff_id, True)
        self.login("staff")
        self.assert_inactive()

    def test_other_support_identity_cannot_use_grant(self):
        self.login("otherstaff")
        response = self.client.get(f"/api/v2/support-grants/{self.grant_id}/usage")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.client.get("/api/v2/support-grants").json()["items"], [])

    def test_metadata_only_grant_cannot_read_content(self):
        self.login("owner")
        grant_id = self.approve(content=False)["id"]
        self.login("staff")
        metadata = self.client.get(f"/api/v2/support-grants/{grant_id}/usage")
        self.assertEqual(metadata.status_code, 200, metadata.text)
        content = self.client.get(
            f"/api/v2/support-grants/{grant_id}/sessions/{self.session_id}"
        )
        self.assertEqual(content.status_code, 403, content.text)
        self.assertEqual(content.json()["error"]["code"], "support_content_forbidden")

    def test_expired_or_explicitly_revoked_grant_is_not_listed_or_readable(self):
        for changes in ({"expires_at": time.time() - 1}, {"revoked_at": now()}):
            with self.subTest(changes=changes):
                with self.db.transaction("tenant:" + self.tenant_id) as connection:
                    connection.execute(
                        update(st.support_grants)
                        .where(st.support_grants.c.id == self.grant_id)
                        .values(
                            **{
                                "expires_at": time.time() + 900,
                                "revoked_at": None,
                                **changes,
                            }
                        )
                    )
                self.assert_inactive()

    def test_suspended_tenant_keeps_existing_grant_but_closing_tenant_does_not(self):
        self.login("owner")
        self.write(
            "post",
            f"/api/v2/platform/tenants/{self.tenant_id}/suspend",
            {"reason": "Temporary maintenance", "expected_version": 1},
        )
        self.login("staff")
        response = self.client.get(f"/api/v2/support-grants/{self.grant_id}/usage")
        self.assertEqual(response.status_code, 200, response.text)
        with self.db.transaction("tenant:" + self.tenant_id) as connection:
            connection.execute(
                update(nt.tenant_settings)
                .where(nt.tenant_settings.c.tenant_id == self.tenant_id)
                .values(status="closing")
            )
        self.assert_inactive()


if __name__ == "__main__":
    unittest.main()
