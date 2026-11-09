"""Real API registration, encrypted credentials, leased probes and frozen discovery."""

import asyncio
import unittest
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import remote_agent_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.remote_agents.registry import (
    RemoteHealthQueue,
    RemoteRegistry,
)
from agent_platform.modules.run_records import uid
from agent_platform.tests import test_api as api_tests
from agent_platform.tests.test_collaboration_runtime import DATA, TEXT


class HealthTransport:
    def __init__(self, registration, *, healthy=True):
        self.registration, self.healthy, self.calls = registration, healthy, []

    async def request(self, url, route, **kwargs):
        self.calls.append((url, route))
        descriptor = self.registration["descriptor"]
        return {
            "remote_agent_id": descriptor["remote_agent_id"],
            "remote_version": descriptor["remote_version"],
            "descriptor_digest": self.registration["digest"],
            "healthy": self.healthy,
        }


class RemoteAgentRegistryTests(unittest.TestCase):
    tearDown = api_tests.APIIntegrationTest.tearDown
    login = api_tests.APIIntegrationTest.login
    request = api_tests.APIIntegrationTest.request
    create_resources = api_tests.APIIntegrationTest.create_resources
    enqueue = api_tests.APIIntegrationTest.enqueue

    def setUp(self):
        api_tests.APIIntegrationTest.setUp(self)
        self.platform.settings = replace(
            self.settings,
            remote_agents_enabled=True,
            remote_agent_allowed_origins=("https://remote.example.test",),
            collaboration_enabled=True,
            collaboration_durable_enabled=True,
        )

    def credential(self):
        response = self.request(
            "POST",
            "/remote-agent-credentials",
            json={"name": "External service", "token": "temporary-remote-test-secret"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("temporary-remote-test-secret", response.text)
        return response.json()

    def body(self, credential):
        return {
            "idempotency_key": uid(),
            "descriptor": {
                "remote_agent_id": "remote",
                "remote_version": "v1",
                "name": "Remote analyst",
                "description": "Only task inputs",
                "capability_keys": ["remote.analysis"],
                "task_contracts": [
                    {"task_type": "work", "input": TEXT, "output": DATA}
                ],
                "quote_id": "q1",
                "max_charge": "0.02",
                "idempotency": True,
                "status_lookup": True,
                "cancellation": True,
            },
            "endpoints": [
                {
                    "url": "https://remote.example.test/agent",
                    "credential_id": credential["id"],
                }
            ],
        }

    def register(self):
        credential = self.credential()
        body = self.body(credential)
        response = self.request("POST", "/remote-agents", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return credential, body, response.json()

    def probe(self, registration):
        worker = Worker(self.platform, gateway=self.gateway)
        queue = RemoteHealthQueue(worker, transport=HealthTransport(registration))
        claim = queue.claim()
        self.assertIsNotNone(claim)
        asyncio.run(queue.execute(claim))
        return queue, claim

    def test_tenant_purge_scrubs_remote_secrets_descriptors_and_endpoints(self):
        from agent_platform.infrastructure import operations_tables as ot
        from agent_platform.modules.operations import OperationsService
        from agent_platform.modules.run_records import now

        credential, _, registration = self.register()
        self.probe(registration)
        self.platform.settings = replace(
            self.platform.settings,
            export_directory=Path(self.temporary.name) / "exports",
            tombstone_path=Path(self.temporary.name) / "tombstones",
        )
        operations = OperationsService(self.platform)
        operations.close_tenant(self.fixture.principal, self.tenant, "Fixture deletion")
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            conn.execute(
                update(ot.closures)
                .where(ot.closures.c.tenant_id == self.tenant)
                .values(
                    status="purging",
                    retain_until=0,
                    tombstone_written_at=now(),
                )
            )
        for _ in range(35):
            job = operations._claim_closure(self.tenant)
            if job is None:
                break
            operations._purge_page(job)
        with self.db.read() as conn:
            secret = (
                conn.execute(
                    select(rt.secrets).where(rt.secrets.c.id == credential["id"])
                )
                .mappings()
                .one()
            )
            metadata = (
                conn.execute(
                    select(rt.credentials).where(
                        rt.credentials.c.id == credential["id"]
                    )
                )
                .mappings()
                .one()
            )
            version = (
                conn.execute(
                    select(rt.versions).where(rt.versions.c.id == registration["id"])
                )
                .mappings()
                .one()
            )
            endpoint = conn.execute(select(rt.endpoints)).mappings().one()
        self.assertEqual((secret["ciphertext"], secret["digest"]), ("", ""))
        self.assertFalse(metadata["active"])
        self.assertEqual(metadata["name"], "[已删除]")
        self.assertFalse(version["enabled"])
        self.assertEqual(version["descriptor"], {"content_deleted": True})
        self.assertEqual(endpoint["url"], "")
        self.assertEqual(endpoint["health_status"], "disabled")
        self.assertIsNone(endpoint["probe_owner"])

    def test_registration_is_idempotent_versioned_and_secrets_never_appear_in_api(self):
        credential, body, registered = self.register()
        repeated = self.request("POST", "/remote-agents", json=body)
        self.assertEqual(repeated.json()["id"], registered["id"])
        conflict = self.request(
            "POST",
            "/remote-agents",
            json={**body, "descriptor": {**body["descriptor"], "quote_id": "q2"}},
        )
        self.assertEqual(conflict.status_code, 409)
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(rt.versions)), 1
            )
            secret = connection.execute(select(rt.secrets)).mappings().one()
            self.assertNotIn("temporary-remote-test-secret", secret["ciphertext"])
            self.assertEqual(
                RemoteRegistry(self.platform).credential(
                    connection, self.tenant, credential["id"]
                ),
                "temporary-remote-test-secret",
            )
        for path in (
            "/remote-agent-credentials",
            "/remote-agents",
            f"/remote-agents/{registered['id']}",
            "/agents",
        ):
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("temporary-remote-test-secret", response.text)
            self.assertNotIn("ciphertext", response.text)
        self.assertEqual(
            self.request(
                "PATCH",
                f"/agents/{registered['agent_id']}",
                json={"system_prompt": "new prompt"},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.request(
                "POST", f"/agents/{registered['agent_id']}/publish"
            ).status_code,
            409,
        )
        self.assertEqual(
            self.request(
                "POST", "/sessions", json={"agent_id": registered["agent_id"]}
            ).status_code,
            409,
        )

    def test_discovery_requires_health_and_filters_revoked_or_expired_endpoints(self):
        credential, _, registered = self.register()
        path = "/agents/discovery?capability=remote.analysis"
        self.assertEqual(self.request("GET", path).json()["items"], [])
        self.probe(registered)
        found = self.request("GET", path)
        self.assertEqual(
            [item["id"] for item in found.json()["items"]], [registered["agent_id"]]
        )
        self.assertNotIn("remote.example.test", found.text)
        self.assertNotIn(credential["id"], found.text)
        self.request(
            "PATCH",
            f"/remote-agent-credentials/{credential['id']}",
            json={"enabled": False},
        )
        self.assertEqual(self.request("GET", path).json()["items"], [])
        self.request(
            "PATCH",
            f"/remote-agent-credentials/{credential['id']}",
            json={"enabled": True},
        )
        self.probe(registered)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(rt.endpoints)
                .where(rt.endpoints.c.registration_id == registered["id"])
                .values(health_until=0)
            )
        self.assertEqual(self.request("GET", path).json()["items"], [])

    def test_probe_cannot_restore_health_after_credential_revocation(self):
        credential, _, registered = self.register()
        worker = Worker(self.platform, gateway=self.gateway)
        queue = RemoteHealthQueue(worker, transport=HealthTransport(registered))
        claim = queue.claim()
        self.assertIsNone(
            RemoteHealthQueue(Worker(self.platform, gateway=self.gateway)).claim()
        )
        self.request(
            "PATCH",
            f"/remote-agent-credentials/{credential['id']}",
            json={"enabled": False},
        )
        self.assertFalse(queue.finish(claim, True, ""))
        self.assertEqual(
            self.request("GET", f"/remote-agents/{registered['id']}").json()[
                "endpoints"
            ][0]["health_status"],
            "unhealthy",
        )

    def test_registration_rejects_cross_tenant_credentials_and_disallowed_origins(self):
        credential, body, registered = self.register()
        bad = {
            **body,
            "idempotency_key": uid(),
            "endpoints": [
                {
                    "url": "https://private.example.test",
                    "credential_id": credential["id"],
                }
            ],
        }
        self.assertEqual(
            self.request("POST", "/remote-agents", json=bad).status_code, 403
        )
        other = self.fixture.context(self.fixture.owner["id"], self.fixture.tenant_b)
        registry = RemoteRegistry(self.platform)
        with self.assertRaises(PlatformError) as error:
            registry.register(other, {**body, "idempotency_key": uid()})
        self.assertEqual(error.exception.status, 404)
        with self.assertRaises(PlatformError):
            registry.get(other, registered["id"])
        with self.assertRaises(PlatformError):
            registry.create_credential(
                self.fixture.member, {"name": "Bad", "token": "test"}
            )

    def test_new_version_does_not_mutate_old_registration_or_inherit_health(self):
        _, body, registered = self.register()
        self.probe(registered)
        response = self.request(
            "POST",
            "/remote-agents",
            json={
                **body,
                "idempotency_key": uid(),
                "agent_id": registered["agent_id"],
                "expected_version": 1,
                "descriptor": {
                    **body["descriptor"],
                    "remote_version": "v2",
                    "quote_id": "q2",
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        new = response.json()
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["endpoints"][0]["health_status"], "pending")
        self.assertEqual(
            self.request("GET", f"/remote-agents/{registered['id']}").json()[
                "descriptor"
            ]["remote_version"],
            "v1",
        )
        with self.db.read() as connection:
            old = connection.scalar(
                select(t.agent_versions.c.spec).where(
                    t.agent_versions.c.agent_id == registered["agent_id"],
                    t.agent_versions.c.version == 1,
                )
            )
        self.assertEqual(old["remote_ref"]["id"], registered["id"])
        stale = self.request(
            "POST",
            "/remote-agents",
            json={
                **body,
                "idempotency_key": uid(),
                "agent_id": registered["agent_id"],
                "expected_version": 1,
            },
        )
        self.assertEqual(stale.status_code, 409)

    def test_team_freezes_remote_version_endpoints_and_contract_without_model_route(
        self,
    ):
        _, _, registered = self.register()
        self.probe(registered)
        self.create_resources()
        team = self.request(
            "POST",
            "/agents",
            json={
                "name": "Remote team",
                "execution_mode": "team",
                "collaboration_spec": {
                    "members": [
                        {
                            "key": "remote",
                            "agent_id": registered["agent_id"],
                            "version": 1,
                            "responsibility": "Analyze input",
                        }
                    ],
                    "tasks": [
                        {
                            "key": "analysis",
                            "member_key": "remote",
                            "task_type": "work",
                            "objective": "Analyze input",
                        }
                    ],
                },
            },
        )
        self.assertEqual(team.status_code, 201, team.text)
        identifier = team.json()["id"]
        self.assertEqual(
            self.request("POST", f"/agents/{identifier}/publish").status_code, 200
        )
        setting = self.request("GET", "/agents/collaboration-settings").json()
        self.request(
            "PATCH",
            "/agents/collaboration-settings",
            json={"enabled": True, "expected_version": setting["version"]},
        )
        session = self.request(
            "POST", "/sessions", json={"agent_id": identifier}
        ).json()
        response = self.enqueue(session, message="A=10")
        self.assertEqual(response.status_code, 202, response.text)
        with self.db.read() as connection:
            root = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == response.json()["id"])
            )
        remote = root["team_snapshot"]["members"]["remote"]["spec"]
        self.assertNotIn("model", remote)
        self.assertNotIn("route", remote)
        self.assertEqual(remote["remote_snapshot"]["digest"], registered["digest"])
        self.assertEqual(
            remote["remote_snapshot"]["endpoint_ids"],
            [registered["endpoints"][0]["id"]],
        )
        self.request(
            "PATCH",
            f"/remote-agents/{registered['id']}/availability",
            json={"enabled": False},
        )
        denied = self.enqueue(session, message="A=10", key="after-disable")
        self.assertEqual(denied.status_code, 409, denied.text)
