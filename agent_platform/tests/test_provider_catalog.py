"""Provider discovery/import without static model names or exposing upstream keys."""

import time
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

from agent_platform.infrastructure import gateway_tables as g
from agent_platform.infrastructure import provider_tables as p
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.gateway_control import ControlFailure, GatewayService
from agent_platform.modules.provider_catalog import (
    ProviderCatalog,
    ProviderClient,
    parse_models,
)
from agent_platform.tests import test_api as api_tests


class ModelsClient:
    def __init__(self):
        self.calls = []
        self.rows = parse_models(
            {
                "data": [
                    {
                        "id": "vendor/model-a",
                        "name": "Model A",
                        "owned_by": "vendor",
                        "context_length": 32000,
                    }
                ]
            }
        )

    def list_models(self, base, token):
        self.calls.append((base, token))
        return self.rows


class CatalogControl:
    def __init__(self):
        self.models = {}
        self.writes = []
        self.lose_response = False
        self.discard = False

    def provider_model_info(self, identifier):
        return self.models.get(identifier)

    def create_provider_model(self, identifier, route, upstream, api_base, api_key):
        self.writes.append(identifier)
        if not self.discard:
            self.models[identifier] = {
                "model_name": route,
                "litellm_params": {"model": upstream, "api_base": api_base},
                "model_info": {"id": identifier},
            }
        if self.lose_response:
            raise ControlFailure("lost_response", unknown=True)


class ProviderCatalogTests(unittest.TestCase):
    setUp = api_tests.APIIntegrationTest.setUp
    tearDown = api_tests.APIIntegrationTest.tearDown
    login = api_tests.APIIntegrationTest.login

    def request(self, method, path, **kwargs):
        if not path.startswith("/api/"):
            return api_tests.APIIntegrationTest.request(self, method, path, **kwargs)
        return self.client.request(
            method, path, headers={"X-CSRF-Token": self.admin["csrf_token"]}, **kwargs
        )

    def catalog(self):
        client, control = ModelsClient(), CatalogControl()
        service = ProviderCatalog(
            GatewayService(self.db, self.settings, adapter=control), client=client
        )
        return service, client, control

    def create(self):
        response = self.request(
            "POST",
            "/api/v2/platform/providers",
            json={
                "name": "Example provider",
                "api_base": "https://provider.example.test/v1",
                "api_key": "provider-fixture-secret",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn("provider-fixture-secret", response.text)
        return response.json()

    def discover(self, service, provider):
        job = service.enqueue(self.fixture.owner["id"], provider["id"], "discover")
        self.assertTrue(service.process_one())
        self.assertEqual(
            service.operation(self.fixture.owner["id"], job["id"])["status"],
            "succeeded",
        )
        return job

    def test_api_stores_encrypted_secret_and_never_returns_it(self):
        provider = self.create()
        response = self.request("GET", "/api/v2/platform/providers")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("provider-fixture-secret", response.text)
        self.assertNotIn("ciphertext", response.text)
        with self.db.read() as conn:
            secret = conn.scalar(select(p.secrets.c.ciphertext))
        self.assertNotIn("provider-fixture-secret", secret)
        self.assertEqual(
            GatewayService(self.db, self.settings)._decrypt(secret),
            "provider-fixture-secret",
        )
        self.assertEqual(response.json()["items"][0]["id"], provider["id"])

    def test_ordinary_tenant_member_cannot_manage_or_read_provider_connections(self):
        service, _, _ = self.catalog()
        for action in (
            lambda: service.list(self.fixture.member["id"]),
            lambda: service.create(
                self.fixture.member["id"],
                name="Denied",
                api_base="https://example.test/v1",
                api_key="secret",
            ),
        ):
            with self.assertRaises(PlatformError) as e:
                action()
            self.assertEqual(e.exception.status, 403)

    def test_private_or_ambiguous_addresses_rejected_before_saving(self):
        service, _, _ = self.catalog()
        for address in (
            "http://provider.example/v1",
            "https://127.0.0.1/v1",
            "https://user:pass@example.com/v1",
            "https://example.com/v1?token=x",
        ):
            with self.assertRaises(PlatformError):
                service.create(
                    self.fixture.owner["id"],
                    name="Bad",
                    api_base=address,
                    api_key="secret",
                )
        with self.db.read() as conn:
            self.assertEqual(conn.execute(select(p.providers)).all(), [])

    def test_discovery_uses_current_provider_response_and_refreshes_without_configuration(
        self,
    ):
        provider = self.create()
        service, client, _ = self.catalog()
        first = service.enqueue(self.fixture.owner["id"], provider["id"], "discover")
        second = service.enqueue(self.fixture.owner["id"], provider["id"], "discover")
        self.assertEqual(first["id"], second["id"])
        service.process_one()
        self.assertEqual(
            client.calls,
            [("https://provider.example.test/v1", "provider-fixture-secret")],
        )
        client.rows = parse_models({"data": [{"id": "entirely-new-model"}]})
        self.discover(service, provider)
        self.assertEqual(
            service.list(self.fixture.owner["id"])["items"][0]["catalog"][0]["id"],
            "entirely-new-model",
        )

    def test_empty_provider_catalog_is_successful_empty_state(self):
        provider = self.create()
        service, client, _ = self.catalog()
        client.rows = []
        job = self.discover(service, provider)
        self.assertEqual(
            service.operation(self.fixture.owner["id"], job["id"])["result"],
            {"model_count": 0},
        )

    def test_only_discovered_models_can_be_imported(self):
        provider = self.create()
        service, _, control = self.catalog()
        with self.assertRaises(PlatformError):
            service.enqueue(
                self.fixture.owner["id"], provider["id"], "import", "arbitrary/model"
            )
        self.assertEqual(control.writes, [])

    def test_import_registers_gateway_route_and_deduplicates_repeated_selection(self):
        provider = self.create()
        service, _, control = self.catalog()
        self.discover(service, provider)
        first = service.enqueue(
            self.fixture.owner["id"], provider["id"], "import", "vendor/model-a"
        )
        self.assertEqual(
            service.enqueue(
                self.fixture.owner["id"], provider["id"], "import", "vendor/model-a"
            )["id"],
            first["id"],
        )
        service.process_one()
        result = service.operation(self.fixture.owner["id"], first["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(control.writes), 1)
        with self.db.read() as conn:
            deployment = (
                conn.execute(
                    select(g.deployments).where(
                        g.deployments.c.id == result["result"]["deployment_id"]
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(deployment["status"], "active")
        self.assertTrue(deployment["internal_route"].startswith("provider-"))
        self.assertNotIn("provider-fixture-secret", str(result))

    def test_uncertain_gateway_write_is_read_back_without_repeating_post(self):
        provider = self.create()
        service, _, control = self.catalog()
        self.discover(service, provider)
        control.lose_response = True
        job = service.enqueue(
            self.fixture.owner["id"], provider["id"], "import", "vendor/model-a"
        )
        service.process_one()
        self.assertEqual(
            service.operation(self.fixture.owner["id"], job["id"])["status"], "unknown"
        )
        with self.db.transaction("gateway:providers") as conn:
            conn.execute(
                update(p.operations)
                .where(p.operations.c.id == job["id"])
                .values(next_poll_at=0)
            )
        service.process_one()
        self.assertEqual(
            service.operation(self.fixture.owner["id"], job["id"])["status"],
            "succeeded",
        )
        self.assertEqual(len(control.writes), 1)

    def test_unknown_import_without_gateway_evidence_is_bounded_and_never_granted(self):
        provider = self.create()
        service, _, control = self.catalog()
        self.discover(service, provider)
        control.lose_response, control.discard = True, True
        job = service.enqueue(
            self.fixture.owner["id"], provider["id"], "import", "vendor/model-a"
        )
        for _ in range(3):
            with self.db.transaction("gateway:providers") as conn:
                conn.execute(
                    update(p.operations)
                    .where(p.operations.c.id == job["id"])
                    .values(next_poll_at=0)
                )
            service.process_one()
        self.assertFalse(service.process_one())
        result = service.operation(self.fixture.owner["id"], job["id"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(control.writes), 1)
        self.assertEqual(result["result"], {})

    def test_revoked_platform_operator_cannot_trigger_queued_network_work(self):
        provider = self.create()
        service, client, _ = self.catalog()
        service.enqueue(self.fixture.owner["id"], provider["id"], "discover")
        with self.db.transaction("identity") as conn:
            conn.execute(
                update(nt.platform_roles)
                .where(nt.platform_roles.c.user_id == self.fixture.owner["id"])
                .values(revoked_at="revoked")
            )
        service.process_one()
        self.assertEqual(client.calls, [])
        with self.db.read() as conn:
            self.assertEqual(
                conn.scalar(select(p.operations.c.error_code)), "platform_role_required"
            )

    def test_expired_worker_is_fenced_from_publishing_catalog(self):
        provider = self.create()
        first, _, _ = self.catalog()
        first.enqueue(self.fixture.owner["id"], provider["id"], "discover")
        stale = first.claim()
        with self.db.transaction("gateway:providers") as conn:
            conn.execute(update(p.operations).values(lease_until=time.time() - 1))
        second, _, _ = self.catalog()
        successor = second.claim()
        with self.assertRaises(PlatformError):
            first.execute(stale)
        self.assertGreater(successor["fence"], stale["fence"])

    def test_transport_checks_dns_and_does_not_send_key_to_private_destination(self):
        with (
            patch(
                "agent_platform.modules.provider_catalog.public_addresses",
                side_effect=PlatformError(403, "remote_address_blocked", "blocked"),
            ),
            patch(
                "agent_platform.modules.provider_catalog.PinnedHTTPSConnection"
            ) as connection,
        ):
            with self.assertRaises(PlatformError) as e:
                ProviderClient().list_models(
                    "https://provider.example.test/v1", "secret"
                )
            self.assertEqual(e.exception.code, "provider_address_invalid")
            connection.assert_not_called()

    def test_real_https_model_list_handles_connection_close_with_certificate_validation(
        self,
    ):
        from agent_platform.tests.remote_https_fixture import https_peer

        with https_peer() as peer:
            peer["models"] = [{"id": "model-from-live-peer", "owned_by": "fixture"}]
            with patch(
                "agent_platform.modules.provider_catalog.public_addresses",
                return_value=["127.0.0.1"],
            ):
                models = ProviderClient().list_models(
                    peer["origin"] + "/agent", "temporary-remote-test-secret"
                )
            self.assertEqual([m["id"] for m in models], ["model-from-live-peer"])
            self.assertEqual(peer["requests"], [("GET", "/agent/models")])
            self.assertEqual(set(peer["sni"]), {"remote.example.test"})

    def test_response_parser_projects_metadata_and_rejects_invalid_identifiers(self):
        rows = parse_models(
            {
                "data": [
                    {"id": "z", "api_key": "must-not-project", "context_length": 1000},
                    {"id": "bad\nmodel"},
                    {"id": "a"},
                ]
            }
        )
        self.assertEqual([r["id"] for r in rows], ["a", "z"])
        self.assertNotIn("must-not-project", str(rows))
        self.assertEqual(rows[1]["context_window"], 1000)
        with self.assertRaises(PlatformError):
            parse_models({"data": [{"id": "bad id"}]})


if __name__ == "__main__":
    unittest.main()
