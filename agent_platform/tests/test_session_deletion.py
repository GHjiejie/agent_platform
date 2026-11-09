"""History deletion uses real migrated storage and preserves execution records."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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

    def test_batch_delete_reports_busy_and_private_items_without_blocking_other_deletions(
        self,
    ):
        first = self.fixture.create_session()
        second = self.fixture.create_session()
        busy = self.fixture.create_session()
        other = self.fixture.create_session(self.fixture.member)
        run = self.fixture.create_run(self.fixture.owner, busy)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs).where(t.runs.c.id == run["id"]).values(status="running")
            )
        response = self.request(
            "POST",
            "/sessions/batch-delete",
            json={
                "session_ids": [
                    first["id"],
                    busy["id"],
                    second["id"],
                    other["id"],
                    "unknown",
                    first["id"],
                ]
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["deleted_ids"], [first["id"], second["id"]])
        self.assertEqual(
            [(item["id"], item["code"]) for item in result["failed"]],
            [
                (busy["id"], "session_busy"),
                (other["id"], "not_found"),
                ("unknown", "not_found"),
            ],
        )
        self.assertEqual(result["failed"][1]["message"], result["failed"][2]["message"])
        self.assertEqual(
            self.request("GET", f"/sessions/{first['id']}").status_code, 404
        )
        self.assertEqual(
            self.request("GET", f"/sessions/{busy['id']}").status_code, 200
        )
        self.assertEqual(
            self.platform.session_detail(self.fixture.member, other["id"])["id"],
            other["id"],
        )
        with self.db.read() as connection:
            audits = list(
                connection.scalars(
                    select(t.audits.c.target).where(
                        t.audits.c.action == "session.delete"
                    )
                )
            )
            self.assertEqual(set(audits), {first["id"], second["id"]})
            self.assertEqual(len(audits), 2)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(status="succeeded")
            )
        retry = self.request(
            "POST", "/sessions/batch-delete", json={"session_ids": [busy["id"]]}
        )
        self.assertEqual(retry.json(), {"deleted_ids": [busy["id"]], "failed": []})

    def test_batch_retries_are_idempotent_and_cross_tenant_items_remain_private(self):
        sessions = [self.fixture.create_session() for _ in range(2)]
        identifiers = [item["id"] for item in sessions]
        denied = self.request(
            "POST",
            "/sessions/batch-delete",
            tenant=self.fixture.tenant_b,
            json={"session_ids": identifiers},
        )
        self.assertEqual(denied.status_code, 200, denied.text)
        self.assertEqual(denied.json()["deleted_ids"], [])
        self.assertTrue(
            all(item["code"] == "not_found" for item in denied.json()["failed"])
        )
        for _ in range(2):
            response = self.request(
                "POST", "/sessions/batch-delete", json={"session_ids": identifiers}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(), {"deleted_ids": identifiers, "failed": []}
            )
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(t.audits)
                    .where(t.audits.c.action == "session.delete")
                ),
                2,
            )

    def test_batch_bounds_csrf_and_role_are_checked_before_writing(self):
        session = self.fixture.create_session()
        for values in ([], ["unknown"] * 201, ["x" * 65], [""]):
            with self.subTest(values_length=len(values)):
                result = self.request(
                    "POST", "/sessions/batch-delete", json={"session_ids": values}
                )
                self.assertEqual(result.status_code, 422, result.text)
        no_csrf = self.client.post(
            f"/api/v2/tenants/{self.tenant}/sessions/batch-delete",
            json={"session_ids": [session["id"]]},
        )
        self.assertEqual(no_csrf.status_code, 403)
        self.login(self.fixture.finance["email"])
        denied = self.request(
            "POST", "/sessions/batch-delete", json={"session_ids": [session["id"]]}
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(
            self.platform.session_detail(self.fixture.owner, session["id"])["id"],
            session["id"],
        )

    def test_unexpected_batch_failure_rolls_back_deletions_and_audits(self):
        first, second = [self.fixture.create_session() for _ in range(2)]
        remove = self.platform._delete_session

        def fail_second(connection, actor, session_id):
            if session_id == second["id"]:
                raise RuntimeError("Explicit isolated storage failure")
            return remove(connection, actor, session_id)

        with (
            patch.object(self.platform, "_delete_session", side_effect=fail_second),
            self.assertRaises(RuntimeError),
        ):
            self.platform.delete_sessions(
                self.fixture.owner, [first["id"], second["id"]]
            )
        for item in (first, second):
            self.assertEqual(
                self.platform.session_detail(self.fixture.owner, item["id"])["id"],
                item["id"],
            )
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(t.audits)
                    .where(t.audits.c.action == "session.delete")
                ),
                0,
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
