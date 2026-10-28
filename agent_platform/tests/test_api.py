"""HTTP integration coverage of tenant boundaries, policy and immutable runs."""

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from agent_platform.apps.api.main import COOKIE, create_app
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.platform import append_event, now, uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.fixtures import MultiTenantFixture, migrate
from agent_platform.tests.test_execution_lifecycle import OfflineControl

PASSWORD = "Strong-password-for-tests-42"


class FakeGateway:
    """An explicit offline test dependency, never a production fallback."""

    configured = True

    def __init__(self):
        self.calls = []

    async def stream(self, messages, model, max_tokens, temperature, tools):
        self.calls.append({"messages": messages, "model": model})
        yield GatewayEvent("text", {"text": "Verified test response"})
        yield GatewayEvent(
            "result",
            ModelReply(
                "Verified test response",
                [],
                10,
                5,
                {"prompt_tokens": 10, "completion_tokens": 5},
            ),
        )


class APIIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temporary.name) / 'platform.db'}",
            admin_email="admin@example.test",
            admin_password=PASSWORD,
            operator_emails=("admin@example.test",),
            secret_encryption_key=Fernet.generate_key().decode(),
            embedded_worker=False,
            litellm_url="http://test",
            litellm_key="test",
            litellm_admin_url="http://127.0.0.1:4017/ui",
            web_directory=Path(self.temporary.name) / "web",
        )
        self.gateway = FakeGateway()
        self.app = create_app(self.settings, gateway=self.gateway)
        migrate(self.settings.database_url)
        self.db = self.app.state.database
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.platform = self.app.state.platform
        self.tenant = self.fixture.tenant_id
        self.platform.gateway = GatewayService(
            self.db, self.settings, adapter=OfflineControl()
        )
        self.deployment = self.platform.gateway.create_deployment(
            {
                "name": "Offline deployment",
                "internal_route": "offline-test-route",
                "base_url": self.settings.litellm_url,
                "capabilities": {"text": True, "tools": True},
            },
            actor_id=self.fixture.owner["id"],
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.admin = self.login()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def login(self, email="admin@example.test", password=PASSWORD):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {**response.json(), "token": response.cookies[COOKIE]}

    def test_default_model_is_visible_without_inventing_platform_prices(self):
        settings = replace(self.settings, default_model="k3")
        with TestClient(create_app(settings, gateway=self.gateway)) as client:
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": settings.admin_email,
                    "password": PASSWORD,
                },
            )
            result = client.get(f"/api/v2/tenants/{self.tenant}/models")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {"items": [], "default_model": "k3"})

    def test_builtins_are_published_without_models_and_bootstrap_preserves_edits(self):
        from agent_platform.modules.builtin_agents import BUILTIN_AGENTS

        agents = self.request("GET", "/agents").json()["items"]
        self.assertEqual(len(agents), len(BUILTIN_AGENTS))
        self.assertTrue(
            all(a["model_id"] is None and a["published_version"] == 1 for a in agents)
        )
        self.assertTrue(all(a["starter_prompts"] for a in agents))
        ui = next(a for a in agents if a["builtin_key"] == "generative_ui")
        self.assertEqual(ui["tools"], ["generate_sandboxed_ui"])
        self.assertEqual(ui["max_tokens"], self.settings.default_output_tokens)
        self.assertTrue(all(not a["tools"] for a in agents if a != ui))
        first = agents[0]
        self.request(
            "PATCH", f"/agents/{first['id']}", json={"system_prompt": "Edited draft"}
        )
        self.platform.bootstrap()
        after = self.request("GET", "/agents").json()["items"]
        self.assertEqual({a["id"] for a in after}, {a["id"] for a in agents})
        self.assertEqual(
            next(a for a in after if a["id"] == first["id"])["system_prompt"],
            "Edited draft",
        )
        member = self.create_user()
        visible = self.request(
            "GET", "/agents", auth=self.login(member["email"])
        ).json()["items"]
        self.assertEqual(
            next(a for a in visible if a["id"] == first["id"])["system_prompt"],
            first["system_prompt"],
        )
        session = self.request(
            "POST", "/sessions", json={"agent_id": first["id"]}
        ).json()
        rejected = self.enqueue(session)
        self.assertEqual(rejected.json()["error"]["code"], "no_available_model")
        self.assertEqual(
            self.request("GET", f"/sessions/{session['id']}").json()["messages"], []
        )

    def test_execution_policy_is_editable_validated_and_published(self):
        options = self.request("GET", "/agents/execution-options")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.json()["timeout_seconds"], self.settings.run_timeout)
        _, agent, session = self.create_resources()
        policy = {
            "reasoning_effort": "low",
            "timeout_seconds": 2,
            "max_total_tokens": 20000,
            "max_cost": "0.1",
            "max_output_recoveries": 2,
        }
        saved = self.request(
            "PATCH", f"/agents/{agent['id']}", json={"execution_policy": policy}
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["execution_policy"], policy)
        self.request("POST", f"/agents/{agent['id']}/publish")
        queued = self.enqueue(session).json()
        self.request(
            "PATCH",
            f"/agents/{agent['id']}",
            json={"execution_policy": {"reasoning_effort": None}},
        )
        draft = next(
            a
            for a in self.request("GET", "/agents").json()["items"]
            if a["id"] == agent["id"]
        )
        self.assertIsNone(draft["execution_policy"]["reasoning_effort"])
        self.assertEqual(draft["execution_policy"]["max_output_recoveries"], 2)
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == queued["id"])
            )
        self.assertEqual(spec["execution_policy"], policy)
        for invalid in (
            {"max_cost": "NaN"},
            {"max_cost": "0"},
            {"max_output_recoveries": -1},
            {"reasoning_effort": "unknown"},
            {"extra": True},
        ):
            rejected = self.request(
                "PATCH", f"/agents/{agent['id']}", json={"execution_policy": invalid}
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
        rejected = self.request(
            "PATCH",
            f"/agents/{agent['id']}",
            json={"execution_policy": {"timeout_seconds": 86400}},
        )
        self.assertEqual(rejected.json()["error"]["code"], "execution_policy_limit")
        reset = self.request(
            "PATCH", f"/agents/{agent['id']}", json={"execution_policy": {}}
        )
        self.assertEqual(reset.json()["execution_policy"]["max_output_recoveries"], 0)

    def test_large_output_budget_is_not_fixed_to_128000(self):
        created = self.request(
            "POST", "/agents", json={"name": "Large output", "max_tokens": 1000000}
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["max_tokens"], 1000000)

    def test_automatic_agent_resolves_new_models_each_run_and_freezes_snapshot(self):
        builtin = self.request("GET", "/agents").json()["items"][0]
        model, _, _ = self.create_resources()
        preferred = self.request(
            "POST",
            "/models",
            json={
                "name": "Preferred model",
                "alias": "preferred-model",
                "max_output_tokens": 128,
                "input_price": "4",
                "output_price": "5",
            },
        ).json()
        self.request("PUT", "/model-policy", json={"default_model_id": preferred["id"]})
        session = self.request(
            "POST", "/sessions", json={"agent_id": builtin["id"]}
        ).json()
        first = self.enqueue(session).json()
        self.assertEqual(first["model_id"], preferred["id"])
        self.request("POST", f"/runs/{first['id']}/cancel")
        self.request(
            "PATCH",
            f"/models/{preferred['id']}",
            json={"active": False, "input_price": "9"},
        )
        second = self.enqueue(session, key="next-auto").json()
        self.assertEqual(second["model_id"], model["id"])
        with self.db.read() as conn:
            frozen = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == first["id"])
            )
            self.assertEqual(frozen["max_tokens"], 128)
            self.assertEqual(frozen["model"]["input_price"], "4")
            version = conn.scalar(
                select(t.agent_versions.c.spec).where(
                    t.agent_versions.c.agent_id == builtin["id"]
                )
            )
            self.assertIsNone(version["model_id"])

    def test_run_model_override_enforces_tenant_active_status_and_idempotency(self):
        model, agent, session = self.create_resources()
        foreign = self.seed_second_tenant()
        other = self.request(
            "POST",
            "/models",
            json={"name": "Alternate", "alias": "alternate", "max_output_tokens": 64},
        ).json()
        path = f"/sessions/{session['id']}/runs"
        for selected, expected in (
            (foreign["model"]["id"], 404),
            ("unknown-model", 404),
        ):
            result = self.request(
                "POST", path, key=uid(), json={"message": "hello", "model_id": selected}
            )
            self.assertEqual(result.status_code, expected)
        self.request("PATCH", f"/models/{other['id']}", json={"active": False})
        rejected = self.request(
            "POST", path, key=uid(), json={"message": "hello", "model_id": other["id"]}
        )
        self.assertEqual(rejected.json()["error"]["code"], "model_not_granted")
        self.request("PATCH", f"/models/{other['id']}", json={"active": True})
        payload = {"message": "hello", "model_id": other["id"]}
        first = self.request("POST", path, key="override", json=payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["model_alias"], "alternate")
        self.assertEqual(
            self.request("POST", path, key="override", json=payload).json()["id"],
            first.json()["id"],
        )
        conflict = self.request(
            "POST", path, key="override", json={**payload, "model_id": model["id"]}
        )
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
        with self.db.read() as conn:
            spec = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == first.json()["id"])
            )
            self.assertEqual(spec["max_tokens"], 64)
            self.assertEqual(
                conn.scalar(
                    select(t.agents.c.model_id).where(t.agents.c.id == agent["id"])
                ),
                model["id"],
            )

    def test_model_preference_can_be_cleared_and_auto_agent_can_publish_without_model(
        self,
    ):
        agent = self.request("POST", "/agents", json={"name": "Portable assistant"})
        self.assertEqual(agent.status_code, 201, agent.text)
        self.assertEqual(
            self.request("POST", f"/agents/{agent.json()['id']}/publish").status_code,
            200,
        )
        model, pinned, _ = self.create_resources()
        omitted = self.request(
            "PATCH", f"/agents/{pinned['id']}", json={"name": "Renamed"}
        ).json()
        self.assertEqual(omitted["model_id"], model["id"])
        cleared = self.request(
            "PATCH", f"/agents/{pinned['id']}", json={"model_id": None}
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["model_id"])

    def test_builtin_bootstrap_is_tenant_scoped(self):
        foreign = self.seed_second_tenant()
        self.platform.bootstrap()
        local = self.request("GET", "/agents").json()["items"]
        self.assertTrue(all(a["tenant_id"] == self.tenant for a in local))
        with self.db.read() as conn:
            foreign_ids = list(
                conn.scalars(
                    select(t.agents.c.id).where(
                        t.agents.c.tenant_id == foreign["user"]["tenant_id"],
                        t.agents.c.builtin_key.is_not(None),
                    )
                )
            )
        from agent_platform.modules.builtin_agents import BUILTIN_AGENTS

        self.assertEqual(len(foreign_ids), len(BUILTIN_AGENTS))
        self.assertEqual(
            self.request(
                "POST", "/sessions", json={"agent_id": foreign_ids[0]}
            ).status_code,
            404,
        )

    def test_gateway_admin_entry_is_admin_only_and_does_not_expose_secrets(self):
        result = self.request("GET", "/gateway")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json(),
            {"admin_url": "http://127.0.0.1:4017/ui", "configured": True},
        )
        self.assertNotIn(self.settings.litellm_key, result.text)
        self.assertNotIn(self.settings.admin_password, result.text)
        self.assertEqual(self.request("GET", "/gateway", auth=None).status_code, 401)
        member = self.create_user()
        context = self.login(member["email"])
        self.assertEqual(self.request("GET", "/gateway", auth=context).status_code, 403)

    def test_gateway_without_admin_url_reports_unconfigured(self):
        settings = replace(self.settings, litellm_admin_url="")
        with TestClient(create_app(settings, gateway=self.gateway)) as client:
            client.post(
                "/api/v1/auth/login",
                json={"email": settings.admin_email, "password": PASSWORD},
            )
            response = client.get("/api/v2/platform/gateway")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"admin_url": "", "configured": False})

    def test_gateway_admin_url_rejects_unsafe_links_without_echoing_values(self):
        for address in (
            "javascript:alert(1)",
            "//example.test/ui",
            "/ui",
            "http:///ui",
            "https://user:secret@example.test/ui",
            "https://user@example.test/ui",
            "https://example.test/ui?key=secret",
            "https://example.test/ui#secret",
            "https://example.test/ui?",
            "https://example.test/ui#",
            "https://example.test\\@other.test/ui",
            "https://example.test/\nui",
            "https://example.test:invalid/ui",
            "https://example.test:0/ui",
            "https://example.test:99999/ui",
            "https://example%2etest/ui",
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError) as raised:
                    replace(self.settings, litellm_admin_url=address)
                self.assertNotIn(address, str(raised.exception))

    def test_gateway_admin_url_can_be_configured_with_custom_ports(self):
        address = "https://gateway.example.test:4443/ui"
        with patch.dict("os.environ", {"PLATFORM_LITELLM_ADMIN_URL": address}):
            self.assertEqual(Settings.from_env().litellm_admin_url, address)
        self.assertEqual(
            replace(
                self.settings, litellm_admin_url="http://[::1]:4001/ui"
            ).litellm_admin_url,
            "http://[::1]:4001/ui",
        )

    def test_default_model_bootstrap_is_idempotent_and_preserves_catalog_edits(self):
        settings = replace(
            self.settings,
            default_model="k3",
            default_model_input_price="2",
            default_model_output_price="5",
        )
        app = create_app(settings, gateway=self.gateway)
        app.state.platform.bootstrap()
        with TestClient(app) as client:
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": settings.admin_email,
                    "password": PASSWORD,
                },
            ).json()
            catalog = client.get(f"/api/v2/tenants/{self.tenant}/models").json()
            self.assertEqual(len(catalog["items"]), 1)
            model = catalog["items"][0]
            self.assertEqual(model["alias"], "k3")
            self.assertTrue(model["is_default"])
            self.assertEqual((model["input_price"], model["output_price"]), ("2", "5"))
            changed = self.request(
                "PATCH",
                f"/models/{model['id']}",
                json={"active": False, "input_price": "7"},
            )
            self.assertEqual(changed.status_code, 201, changed.text)
            app.state.platform.bootstrap()
            after = client.get(f"/api/v2/tenants/{self.tenant}/models").json()["items"]
            self.assertEqual(len(after), 1)
            self.assertEqual(after[0]["id"], model["id"])
            self.assertEqual(after[0]["input_price"], "7")
            self.assertFalse(after[0]["active"])

    def test_default_model_bootstrap_targets_only_configured_admin_tenant(self):
        self.seed_second_tenant()
        settings = replace(
            self.settings,
            default_model="k3",
            default_model_input_price="2",
            default_model_output_price="5",
        )
        app = create_app(settings, gateway=self.gateway)
        app.state.platform.bootstrap()
        with TestClient(app), self.db.read() as connection:
            rows = (
                connection.execute(select(t.models).where(t.models.c.alias == "k3"))
                .mappings()
                .all()
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["tenant_id"], self.tenant)

    def request(
        self, method, path, *, auth="admin", csrf=True, key=None, headers=None, **kwargs
    ):
        context = self.admin if auth == "admin" else auth
        request_headers = dict(headers or {})
        request_headers["Cookie"] = f"{COOKIE}={context['token']}" if context else ""
        if context and csrf:
            request_headers.setdefault("X-CSRF-Token", context["csrf_token"])
        if key is not None:
            request_headers["Idempotency-Key"] = key
        tenant_prefix = f"/api/v2/tenants/{self.tenant}"
        platform_prefix = f"/api/v2/platform/tenants/{self.tenant}"
        if path.startswith("/auth/"):
            target = "/api/v1" + path
        elif path == "/gateway":
            target = "/api/v2/platform/gateway"
        elif path == "/users" and method == "POST":
            target = "/api/v2/platform/users"
        elif path == "/users":
            target = tenant_prefix + "/memberships"
        elif path.startswith("/users/"):
            identifier = path.split("/")[-1]
            members = self.platform.identity.members(self.fixture.owner)
            membership = next(
                (row for row in members if row["user_id"] == identifier), None
            )
            payload = dict(kwargs.get("json") or {})
            if "active" in payload:
                payload["status"] = "active" if payload.pop("active") else "revoked"
            if payload.get("role") == "admin":
                payload["role"] = "tenant_admin"
            kwargs["json"] = payload
            target = (
                tenant_prefix
                + "/memberships/"
                + (membership["id"] if membership else identifier)
            )
        elif path == "/models" and method == "POST":
            target = platform_prefix + "/models"
            kwargs["json"] = {
                "deployment_id": self.deployment["id"],
                **(kwargs.get("json") or {}),
            }
        elif (
            path.startswith("/models/")
            and method == "PATCH"
            and set(kwargs.get("json") or {}) != {"active"}
        ):
            identifier = path.split("/")[-1]
            with self.db.read() as conn:
                row = (
                    conn.execute(
                        select(t.models).where(
                            t.models.c.tenant_id == self.tenant,
                            t.models.c.id == identifier,
                        )
                    )
                    .mappings()
                    .first()
                )
            if row:
                fields = (
                    "name",
                    "alias",
                    "active",
                    "input_price",
                    "output_price",
                    "context_window",
                    "max_output_tokens",
                )
                kwargs["json"] = {
                    "deployment_id": self.deployment["id"],
                    **{field: row[field] for field in fields},
                    **kwargs["json"],
                }
                method, target = "POST", platform_prefix + "/models"
            else:
                target = tenant_prefix + path
        elif path == "/billing/credits":
            target = platform_prefix + "/credits"
        elif path.startswith("/billing/reservations") or path == "/billing/unblock":
            target = platform_prefix + path
        else:
            target = tenant_prefix + path
        response = self.client.request(
            method, target, headers=request_headers, **kwargs
        )
        if target == platform_prefix + "/models" and response.status_code == 201:
            if (
                response.json()["active"]
                and self.platform.gateway.status(self.tenant)["status"]
                == "not_enrolled"
            ):
                self.platform.gateway.enroll(
                    self.tenant, actor_id=self.fixture.owner["id"]
                )
            while self.platform.gateway.process_one():
                pass
        return response

    def create_user(
        self, *, email="member@example.test", role="member", password=PASSWORD
    ):
        response = self.request(
            "POST",
            "/users",
            json={"email": email, "name": "Test member", "password": password},
        )
        self.assertEqual(response.status_code, 201, response.text)
        invitation = self.request(
            "POST",
            "/invitations",
            json={"email": email, "role": "tenant_admin" if role == "admin" else role},
        )
        self.assertEqual(invitation.status_code, 201, invitation.text)
        account = self.login(email, password)
        accepted = self.client.post(
            "/api/v2/invitations/accept",
            headers={
                "Cookie": f"{COOKIE}={account['token']}",
                "X-CSRF-Token": account["csrf_token"],
            },
            json={"token": invitation.json()["token"]},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        return self.platform.identity.context(response.json()["id"], self.tenant)

    def create_resources(self):
        model = self.request(
            "POST",
            "/models",
            json={
                "name": "Test model",
                "alias": "test-model",
                "input_price": "1",
                "output_price": "3",
                "context_window": 8192,
                "max_output_tokens": 1024,
            },
        )
        self.assertEqual(model.status_code, 201, model.text)
        model = model.json()
        agent = self.request(
            "POST",
            "/agents",
            json={
                "name": "Test agent",
                "model_id": model["id"],
                "system_prompt": "Original published prompt",
                "max_tokens": 512,
                "max_steps": 3,
                "tools": ["calculator"],
                "temperature": 0,
            },
        )
        self.assertEqual(agent.status_code, 201, agent.text)
        agent = agent.json()
        response = self.request("POST", f"/agents/{agent['id']}/publish")
        self.assertEqual(response.status_code, 200, response.text)
        session = self.request("POST", "/sessions", json={"agent_id": agent["id"]})
        self.assertEqual(session.status_code, 201, session.text)
        return model, agent, session.json()

    def enqueue(self, session, message="Test message", key="run-1", auth="admin"):
        return self.request(
            "POST",
            f"/sessions/{session['id']}/runs",
            auth=auth,
            key=key,
            json={"message": message},
        )

    def seed_second_tenant(self):
        tenant_id = self.fixture.tenant_b
        user = self.fixture.provision_user(
            f"{uid()}@foreign.test", role="tenant_admin", tenant_id=tenant_id
        )
        model = self.platform.gateway.grant_model(
            tenant_id,
            self.deployment["id"],
            {
                "name": "Foreign model",
                "alias": "foreign-model",
                "input_price": "1",
                "output_price": "2",
                "context_window": 8192,
                "max_output_tokens": 1024,
            },
            actor_id=self.fixture.owner["id"],
        )
        agent = self.platform.save_agent(
            user,
            {
                "name": "Foreign agent",
                "description": "",
                "temperature": 0,
                "system_prompt": "Foreign private prompt",
                "model_id": model["id"],
                "max_tokens": 512,
                "max_steps": 3,
                "tools": [],
            },
        )
        self.platform.publish(user, agent["id"])
        session = self.platform.create_session(
            user, agent["id"], "Foreign conversation"
        )
        run = self.fixture.create_run(user, session, "Foreign secret")
        with self.db.transaction(f"tenant:{tenant_id}") as conn:
            append_event(conn, run["id"], "run.completed", {"text": "Foreign secret"})
            conn.execute(
                insert(t.calls).values(
                    id=uid(),
                    tenant_id=tenant_id,
                    user_id=user["id"],
                    run_id=run["id"],
                    model_id=model["id"],
                    model=model["name"],
                    agent_name=agent["name"],
                    user_email=user["email"],
                    status="confirmed",
                    input_tokens=10,
                    output_tokens=5,
                    cost="0.00002",
                    price_version=1,
                    created_at=now(),
                    error="",
                )
            )
        self.platform.billing.credit(
            tenant_id, self.fixture.owner["id"], "99", "Foreign funds", uid()
        )
        self.platform.billing.reserve(
            tenant_id,
            user["id"],
            model["id"],
            run["id"],
            "foreign-call",
            1,
            1,
            "1",
            "1",
        )
        return {
            "tenant_id": tenant_id,
            "user": user,
            "model": model,
            "agent": agent,
            "session": session,
            "run": run,
        }

    def seed_unresolved_call(self, tenant=None):
        tenant = tenant or self.tenant
        model, _, session = self.create_resources()
        user_id = self.admin["user"]["id"]
        self.platform.billing.credit(
            tenant, user_id, "10", "Investigation funds", "credit"
        )
        run = self.enqueue(session).json()
        call_id = uid()
        self.platform.billing.reserve(
            tenant, user_id, model["id"], run["id"], call_id, 100, 100, "1000", "1000"
        )
        self.platform.billing.unresolved(tenant, call_id, "Gateway dropped final usage")
        with self.db.transaction(f"tenant:{tenant}") as conn:
            conn.execute(
                insert(t.calls).values(
                    id=call_id,
                    tenant_id=tenant,
                    user_id=user_id,
                    run_id=run["id"],
                    model_id=model["id"],
                    model="Test model",
                    agent_name="Test agent",
                    user_email=self.admin["user"]["email"],
                    status="unresolved",
                    input_tokens=0,
                    output_tokens=0,
                    cost="0",
                    price_version=1,
                    created_at=now(),
                    error="Missing usage",
                )
            )
        return call_id

    def test_login_cookie_csrf_origin_and_logout_boundary(self):
        response = self.request("GET", "/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("password_hash", response.text)
        self.assertNotIn("token", response.json()["user"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.request("GET", "/auth/me", auth=None).status_code, 401)
        response = self.request(
            "POST",
            "/billing/credits",
            csrf=False,
            key="bad",
            json={"amount": "1", "description": "Test"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        response = self.request(
            "POST", "/auth/logout", headers={"Origin": "https://attacker.invalid"}
        )
        self.assertEqual(response.json()["error"]["code"], "invalid_origin")
        self.assertEqual(self.request("POST", "/auth/logout").status_code, 200)
        self.assertEqual(self.request("GET", "/auth/me").status_code, 401)

    def test_login_response_uses_httponly_strict_cookie(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.test", "password": PASSWORD},
        )
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertNotIn("token", response.json())

    def test_validation_never_echoes_password_or_accepts_extra_tenant(self):
        before = self.request("GET", "/users").json()["items"]
        secret = "top-secret-password"
        response = self.request(
            "POST",
            "/users",
            json={
                "email": "bad",
                "name": "Invalid",
                "password": secret,
                "tenant_id": "forged-tenant",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("forged-tenant", response.text)
        self.assertEqual(self.request("GET", "/users").json()["items"], before)

    def test_login_failures_are_rate_limited(self):
        for _ in range(10):
            response = self.client.post(
                "/api/v1/auth/login",
                json={"email": "missing@example.test", "password": "Wrong"},
            )
            self.assertEqual(response.status_code, 401, response.text)
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": "missing@example.test", "password": "Wrong"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")

    def test_last_active_admin_cannot_disable_or_demote_itself(self):
        for values in ({"active": False}, {"role": "member"}):
            response = self.request(
                "PATCH", f"/users/{self.admin['user']['id']}", json=values
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "last_owner")
        self.assertEqual(self.request("GET", "/auth/me").status_code, 200)

    def test_disabling_account_revokes_existing_sessions_and_prevents_login(self):
        member = self.create_user()
        context = self.login(member["email"])
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 200)
        revoked = self.request(
            "PATCH", f"/users/{member['id']}", json={"active": False}
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.request("GET", "/agents", auth=context).status_code, 404)
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 200)
        self.login(member["email"])
        # Global suspension is identity-provider fixture state. A tenant owner
        # revoking membership cannot disable the shared login identity.
        with self.db.transaction("identity:suspension-fixture") as connection:
            connection.execute(
                update(t.users)
                .where(t.users.c.id == member["id"])
                .values(
                    active=False,
                    global_status="disabled",
                    auth_version=t.users.c.auth_version + 1,
                )
            )
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 401)
        response = self.client.post(
            "/api/v1/auth/login", json={"email": member["email"], "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 401)

    def test_role_change_recalculates_permissions_for_existing_global_session(self):
        second = self.create_user(email="second-admin@example.test", role="admin")
        context = self.login(second["email"])
        self.assertEqual(
            self.request(
                "PATCH", f"/users/{second['id']}", json={"role": "member"}
            ).status_code,
            200,
        )
        self.assertEqual(self.request("GET", "/users", auth=context).status_code, 403)
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 200)
        context = self.login(second["email"])
        self.assertEqual(self.request("GET", "/users", auth=context).status_code, 403)

    def test_password_change_revokes_all_sessions_and_preserves_password_whitespace(
        self,
    ):
        member = self.create_user(password="  Long password with spaces  ")
        context = self.login(member["email"], "  Long password with spaces  ")
        trimmed = self.client.post(
            "/api/v1/auth/login",
            json={"email": member["email"], "password": "Long password with spaces"},
        )
        self.assertEqual(trimmed.status_code, 401)
        response = self.request(
            "POST",
            "/auth/password",
            auth=context,
            json={
                "current_password": "  Long password with spaces  ",
                "new_password": "  Changed long password  ",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.request("GET", "/auth/me", auth=context).status_code, 401)
        self.login(member["email"], "  Changed long password  ")

    def test_member_cannot_manage_users_models_agents_finances_or_audit(self):
        member = self.create_user()
        context = self.login(member["email"])
        for path in (
            "/users",
            "/billing/ledger",
            "/billing/reservations",
            "/quotas",
            "/audit",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.request("GET", path, auth=context).status_code, 403
                )
        requests = [
            (
                "POST",
                "/users",
                {"email": "x@example.test", "name": "X", "password": PASSWORD},
            ),
            ("POST", "/models", {"name": "X", "alias": "x"}),
            ("POST", "/agents", {"name": "X", "model_id": "x"}),
            ("PATCH", f"/users/{member['id']}", {"role": "admin"}),
            ("POST", "/agents/unknown/publish", None),
            ("POST", "/billing/credits", {"amount": "100", "description": "Forged"}),
            ("POST", "/billing/unblock", {"reason": "Forged permission"}),
            (
                "POST",
                "/billing/reservations/unknown/resolve",
                {"action": "write_off", "reason": "Forged permission"},
            ),
            (
                "PUT",
                "/quotas",
                {"scope": "tenant", "subject_id": member["tenant_id"], "rpm": 1000},
            ),
        ]
        for method, path, payload in requests:
            with self.subTest(path=path):
                response = self.request(
                    method, path, auth=context, key="forged", json=payload
                )
                self.assertEqual(response.status_code, 403, response.text)

    def test_cross_tenant_lists_details_mutations_and_sse_are_isolated(self):
        foreign = self.seed_second_tenant()
        self.create_resources()
        for path in (
            "/users",
            "/models",
            "/agents",
            "/sessions",
            "/runs",
            "/usage/calls",
            "/billing/ledger",
            "/billing/reservations",
            "/audit",
        ):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn(foreign["tenant_id"], response.text)
                self.assertNotIn("Foreign secret", response.text)
        for path in (
            f"/sessions/{foreign['session']['id']}",
            f"/runs/{foreign['run']['id']}",
            f"/runs/{foreign['run']['id']}/events",
        ):
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 404, response.text)
        for method, path, payload in (
            ("PATCH", f"/models/{foreign['model']['id']}", {"active": False}),
            ("PATCH", f"/users/{foreign['user']['id']}", {"active": False}),
            ("POST", f"/agents/{foreign['agent']['id']}/publish", None),
            ("POST", f"/runs/{foreign['run']['id']}/cancel", None),
            ("POST", "/sessions", {"agent_id": foreign["agent"]["id"]}),
        ):
            response = self.request(method, path, json=payload)
            self.assertEqual(response.status_code, 404, response.text)
        own_wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(own_wallet["balance"]), 0)

    def test_quota_subjects_must_belong_to_the_callers_tenant(self):
        foreign = self.seed_second_tenant()
        for scope, subject, status in (
            ("tenant", foreign["tenant_id"], 400),
            ("user", foreign["user"]["id"], 404),
            ("model", foreign["model"]["id"], 404),
        ):
            response = self.request(
                "PUT", "/quotas", json={"scope": scope, "subject_id": subject, "rpm": 1}
            )
            self.assertEqual(response.status_code, status, response.text)
        response = self.request(
            "PUT",
            "/quotas",
            json={
                "scope": "tenant",
                "subject_id": self.tenant,
                "rpm": 0,
                "max_budget": "0",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rpm"], 0)
        self.assertEqual(Decimal(response.json()["max_budget"]), 0)

    def test_published_agent_and_run_model_prices_are_immutable_snapshots(self):
        model, agent, session = self.create_resources()
        self.request(
            "PATCH",
            f"/agents/{agent['id']}",
            json={"system_prompt": "New private draft", "temperature": 0.7},
        )
        member = self.create_user()
        context = self.login(member["email"])
        visible = self.request("GET", "/agents", auth=context).json()["items"]
        self.assertEqual(visible[0]["system_prompt"], "Original published prompt")
        self.assertEqual(visible[0]["published_version"], 1)
        queued = self.enqueue(session)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.request(
            "PATCH",
            f"/models/{model['id']}",
            json={"input_price": "9", "output_price": "10"},
        )
        self.request("POST", f"/agents/{agent['id']}/publish")
        with self.db.read() as conn:
            spec = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == queued.json()["id"])
            )
        self.assertEqual(spec["system_prompt"], "Original published prompt")
        self.assertEqual(spec["temperature"], 0)
        self.assertEqual(spec["version"], 1)
        self.assertEqual(Decimal(spec["model"]["input_price"]), 1)
        self.assertEqual(Decimal(spec["model"]["output_price"]), 3)
        new_session = self.request(
            "POST", "/sessions", json={"agent_id": agent["id"]}
        ).json()
        queued = self.enqueue(new_session, key="new-price-run")
        with self.db.read() as conn:
            latest = conn.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == queued.json()["id"])
            )
        self.assertEqual(latest["system_prompt"], "New private draft")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(Decimal(latest["model"]["input_price"]), 9)

    def test_run_idempotency_body_conflicts_and_session_serialization(self):
        _, _, session = self.create_resources()
        first = self.enqueue(session)
        self.assertEqual(first.status_code, 202, first.text)
        repeated = self.enqueue(session)
        self.assertEqual(repeated.json()["id"], first.json()["id"])
        conflict = self.enqueue(session, message="Different input")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
        busy = self.enqueue(session, key="another-run")
        self.assertEqual(busy.json()["error"]["code"], "session_busy")
        self.request("POST", f"/runs/{first.json()['id']}/cancel")
        self.assertEqual(
            self.enqueue(session, key="after-cancellation").status_code, 202
        )
        with self.db.read() as conn:
            messages = list(
                conn.execute(
                    select(t.messages).where(t.messages.c.session_id == session["id"])
                )
            )
        self.assertEqual(len(messages), 2)

    def test_member_cannot_read_other_members_conversation_or_impersonate_owner(self):
        _, agent, admin_session = self.create_resources()
        member = self.create_user()
        context = self.login(member["email"])
        self.assertEqual(
            self.request(
                "GET", f"/sessions/{admin_session['id']}", auth=context
            ).status_code,
            404,
        )
        member_session = self.request(
            "POST", "/sessions", auth=context, json={"agent_id": agent["id"]}
        ).json()
        response = self.enqueue(member_session)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        own_list = self.request("GET", "/sessions").json()["items"]
        self.assertEqual([row["id"] for row in own_list], [admin_session["id"]])

    def test_sse_replays_durable_events_and_respects_last_event_id(self):
        _, _, session = self.create_resources()
        response = self.enqueue(session)
        run_id = response.json()["id"]
        self.request("POST", f"/runs/{run_id}/cancel")
        full = self.request("GET", f"/runs/{run_id}/events")
        self.assertEqual(full.status_code, 200)
        self.assertIn("event: run.queued", full.text)
        self.assertIn("event: run.cancelled", full.text)
        replay = self.request(
            "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
        )
        self.assertNotIn("event: run.queued", replay.text)
        self.assertIn("event: run.cancelled", replay.text)
        malformed = self.request(
            "GET", f"/runs/{run_id}/events", headers={"Last-Event-ID": "invalid"}
        )
        self.assertEqual(malformed.status_code, 400)
        with self.db.read() as conn:
            self.assertEqual(len(list(conn.execute(select(t.runs)))), 1)

    def test_zero_balance_stays_zero_and_failed_admission_creates_no_charge(self):
        tenant = self.tenant
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["available"]), 0)
        with self.assertRaises(PlatformError) as caught:
            self.platform.billing.reserve(
                tenant,
                self.admin["user"]["id"],
                "model",
                "run",
                "call",
                100,
                100,
                "1",
                "1",
            )
        self.assertEqual(caught.exception.code, "insufficient_balance")
        self.assertEqual(
            self.request("GET", "/billing/reservations").json()["items"], []
        )
        self.assertEqual(self.request("GET", "/billing/ledger").json()["items"], [])
        self.assertEqual(self.gateway.calls, [])

    def test_credit_requires_csrf_reason_and_idempotency_and_appears_in_audit(self):
        payload = {"amount": "1.23456789", "description": "Receipt TEST-42"}
        self.assertEqual(
            self.request("POST", "/billing/credits", json=payload).status_code, 400
        )
        response = self.request(
            "POST", "/billing/credits", key="receipt-42", json=payload
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request(
                "POST", "/billing/credits", key="receipt-42", json=payload
            ).status_code,
            200,
        )
        conflict = self.request(
            "POST",
            "/billing/credits",
            key="receipt-42",
            json={**payload, "amount": "2"},
        )
        self.assertEqual(conflict.status_code, 409)
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["balance"]), Decimal(payload["amount"]))
        self.assertEqual(len(self.request("GET", "/billing/ledger").json()["items"]), 1)
        audit = self.request("GET", "/audit").json()["items"]
        financial = [row for row in audit if row["action"] == "billing.credit"]
        self.assertEqual(len(financial), 1)
        self.assertEqual(financial[0]["actor_email"], self.admin["user"]["email"])
        self.assertIn("Receipt TEST-42", financial[0]["details"])

    def test_manual_confirmation_updates_wallet_usage_and_audit_once(self):
        call_id = self.seed_unresolved_call()
        payload = {
            "action": "confirm",
            "reason": "Provider report verified #42",
            "input_tokens": 100,
            "output_tokens": 25,
        }
        response = self.request(
            "POST",
            f"/billing/reservations/{call_id}/resolve",
            key="resolution",
            json=payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request(
                "POST",
                f"/billing/reservations/{call_id}/resolve",
                key="resolution",
                json=payload,
            ).status_code,
            200,
        )
        wallet = self.request("GET", "/billing/wallet").json()
        self.assertEqual(Decimal(wallet["balance"]), Decimal("9.875"))
        self.assertEqual(Decimal(wallet["reserved"]), 0)
        calls = self.request("GET", "/usage/calls").json()["items"]
        self.assertEqual(Decimal(calls[0]["cost"]), Decimal("0.125"))
        self.assertEqual(calls[0]["status"], "confirmed")
        self.assertEqual(
            Decimal(self.request("GET", "/dashboard").json()["cost"]), Decimal("0.125")
        )
        audits = self.request("GET", "/audit").json()["items"]
        self.assertEqual(
            sum(row["action"] == "billing.resolve.confirm" for row in audits), 1
        )

    def test_manually_written_off_unknown_call_is_visible_without_fabricated_refund(
        self,
    ):
        call_id = self.seed_unresolved_call()
        response = self.request(
            "POST",
            f"/billing/reservations/{call_id}/resolve",
            key="waiver",
            json={
                "action": "write_off",
                "reason": "Waived after provider investigation",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "written_off")
        self.assertEqual(
            Decimal(self.request("GET", "/billing/wallet").json()["balance"]), 10
        )
        self.assertEqual(len(self.request("GET", "/billing/ledger").json()["items"]), 1)

    def test_expired_server_session_cannot_be_reused(self):
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            conn.execute(update(t.auth_sessions).values(expires_at=1))
        self.assertEqual(self.request("GET", "/auth/me").status_code, 401)
        self.assertEqual(
            self.request(
                "POST",
                "/billing/credits",
                key="expired",
                json={"amount": "1", "description": "Must not be accepted"},
            ).status_code,
            401,
        )

    def test_model_prices_and_agent_output_bounds_reject_invalid_configuration(self):
        for price in ("0", "-1", "NaN", "Infinity", "0.0000000000001"):
            with self.subTest(price=price):
                response = self.request(
                    "POST",
                    "/models",
                    json={
                        "name": "Invalid model",
                        "alias": "invalid-model",
                        "input_price": price,
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)
        model, agent, _ = self.create_resources()
        response = self.request(
            "PATCH", f"/agents/{agent['id']}", json={"max_tokens": 2048}
        )
        self.assertEqual(response.status_code, 400, response.text)
        response = self.request(
            "PATCH", f"/models/{model['id']}", json={"context_window": 512}
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.request("PATCH", f"/models/{model['id']}", json={"active": False})
        self.assertEqual(
            self.request("POST", f"/agents/{agent['id']}/publish").status_code, 400
        )

    def test_usage_reads_financial_facts_even_when_worker_projection_is_stale(self):
        call_id = self.seed_unresolved_call()
        # Simulate a worker committing the authoritative ledger then crashing
        # before refreshing platform_calls. Display must remain reconstructable.
        self.platform.billing.settle(self.tenant, call_id, 100, 25)
        calls = self.request("GET", "/usage/calls").json()["items"]
        self.assertEqual(Decimal(calls[0]["cost"]), Decimal("0.125"))
        self.assertEqual(calls[0]["status"], "confirmed")
        dashboard = self.request("GET", "/dashboard").json()
        self.assertEqual(Decimal(dashboard["cost"]), Decimal("0.125"))

    def test_quota_mutation_records_the_administrator_audit(self):
        before = self.request("GET", "/audit").json()["items"]
        response = self.request(
            "PUT",
            "/quotas",
            json={
                "scope": "tenant",
                "subject_id": self.tenant,
                "rpm": 10,
                "tpm": 1000,
                "concurrent": 1,
                "max_budget": "20",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.request("GET", "/audit").json()["items"]
        previous_ids = {row["id"] for row in before}
        new_entries = [row for row in after if row["id"] not in previous_ids]
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["actor_email"], self.admin["user"]["email"])


if __name__ == "__main__":
    unittest.main()
