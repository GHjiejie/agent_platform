"""History deletion uses real migrated storage and preserves execution records."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from agent_platform.apps.api.main import create_app
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.tests.fixtures import PASSWORD, MultiTenantFixture, migrate


class SessionDeletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="session-deletion-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(
            database_url=f"sqlite:///{Path(temporary.name) / 'platform.db'}",
            admin_email="owner@session.test",
            admin_password=PASSWORD,
            embedded_worker=False,
        )
        migrate(self.settings.database_url)
        self.app = create_app(self.settings)
        self.db = self.app.state.database
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.platform = self.app.state.platform
        self.tenant = self.fixture.tenant_id
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.login(self.fixture.owner["email"])

    def login(self, email):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]

    def request(self, method, path, *, tenant=None, **kwargs):
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        return self.client.request(
            method,
            f"/api/v2/tenants/{tenant or self.tenant}{path}",
            headers=headers,
            **kwargs,
        )

    def test_deleted_history_is_hidden_and_cannot_be_read_or_used_for_new_work(self):
        session = self.fixture.create_session()
        run = self.fixture.create_run(self.fixture.owner, session)
        result = self.request("DELETE", f"/sessions/{session['id']}")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"id": session["id"], "deleted": True})
        self.assertEqual(self.request("GET", "/sessions").json()["items"], [])
        self.assertEqual(
            self.request("GET", f"/sessions/{session['id']}").status_code, 404
        )
        denied = self.request(
            "POST",
            f"/sessions/{session['id']}/runs",
            json={"message": "Do not recreate deleted history"},
            headers={"Idempotency-Key": "deleted-history-run"},
        )
        self.assertEqual(denied.status_code, 404, denied.text)
        with self.db.read() as connection:
            self.assertIsNotNone(
                connection.scalar(
                    select(t.sessions.c.deleted_at).where(
                        t.sessions.c.id == session["id"]
                    )
                )
            )
            retained = (
                connection.execute(select(t.runs).where(t.runs.c.id == run["id"]))
                .mappings()
                .one()
            )
            self.assertEqual(retained["status"], "succeeded")
            self.assertEqual(retained["message"], run["message"])
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.messages)), 1
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.runs)), 1
            )

    def test_repeated_concurrent_delete_is_idempotent_and_audited_once(self):
        session = self.fixture.create_session()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.platform.delete_session(
                        self.fixture.owner, session["id"]
                    ),
                    range(2),
                )
            )
        self.assertEqual(results, [{"id": session["id"], "deleted": True}] * 2)
        with self.db.read() as connection:
            entries = list(
                connection.execute(
                    select(t.audits).where(
                        t.audits.c.action == "session.delete",
                        t.audits.c.target == session["id"],
                    )
                ).mappings()
            )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor_id"], self.fixture.owner["id"])

    def test_other_members_and_other_tenants_cannot_delete_or_probe_history(self):
        own = self.fixture.create_session()
        member_session = self.fixture.create_session(self.fixture.member)
        for identifier, tenant in (
            (member_session["id"], self.tenant),
            (own["id"], self.fixture.tenant_b),
            ("unknown-session", self.tenant),
        ):
            with self.subTest(identifier=identifier, tenant=tenant):
                result = self.request(
                    "DELETE", f"/sessions/{identifier}", tenant=tenant
                )
                self.assertEqual(result.status_code, 404, result.text)
                self.assertEqual(result.json()["error"]["code"], "not_found")
        self.login(self.fixture.member["email"])
        self.assertEqual(
            self.request("DELETE", f"/sessions/{own['id']}").status_code, 404
        )
        result = self.request("DELETE", f"/sessions/{member_session['id']}")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            self.platform.session_detail(self.fixture.owner, own["id"])["id"], own["id"]
        )

    def test_queued_running_and_cancelling_work_must_finish_before_deletion(self):
        for status in ("queued", "running", "cancelling"):
            with self.subTest(status=status):
                session = self.fixture.create_session()
                run = self.fixture.create_run(self.fixture.owner, session)
                with self.db.transaction(f"tenant:{self.tenant}") as connection:
                    connection.execute(
                        update(t.runs)
                        .where(t.runs.c.id == run["id"])
                        .values(status=status)
                    )
                result = self.request("DELETE", f"/sessions/{session['id']}")
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(result.json()["error"]["code"], "session_busy")
                self.assertEqual(
                    self.request("GET", f"/sessions/{session['id']}").status_code, 200
                )
                with self.db.transaction(f"tenant:{self.tenant}") as connection:
                    connection.execute(
                        update(t.runs)
                        .where(t.runs.c.id == run["id"])
                        .values(status="cancelled")
                    )
                self.assertEqual(
                    self.request("DELETE", f"/sessions/{session['id']}").status_code,
                    200,
                )

    def test_delete_rechecks_authority_and_requires_csrf(self):
        session = self.fixture.create_session(self.fixture.member)
        denied = self.client.delete(
            f"/api/v2/tenants/{self.tenant}/sessions/{session['id']}"
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "csrf_failed")
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(nt.memberships)
                .where(
                    nt.memberships.c.tenant_id == self.tenant,
                    nt.memberships.c.user_id == self.fixture.member["id"],
                )
                .values(status="revoked")
            )
        with self.assertRaises(PlatformError) as rejected:
            self.platform.delete_session(self.fixture.member, session["id"])
        self.assertEqual(rejected.exception.status, 404)
        self.assertEqual(rejected.exception.code, "not_found")
        with self.db.read() as connection:
            self.assertIsNone(
                connection.scalar(
                    select(t.sessions.c.deleted_at).where(
                        t.sessions.c.id == session["id"]
                    )
                )
            )

    def test_pagination_filters_deleted_rows_before_selecting_each_page(self):
        sessions = [self.fixture.create_session() for _ in range(5)]
        for session in sessions[1::2]:
            self.platform.delete_session(self.fixture.owner, session["id"])
        found = []
        cursor = None
        for _ in range(3):
            response = self.request(
                "GET",
                "/sessions",
                params={"limit": 2, **({"cursor": cursor} if cursor else {})},
            )
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            found.extend(row["id"] for row in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(set(found), {session["id"] for session in sessions[::2]})
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
