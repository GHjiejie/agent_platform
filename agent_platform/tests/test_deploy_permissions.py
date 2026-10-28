"""Resolve Compose inheritance and verify the resulting service storage access."""

import unittest
from importlib import import_module
from pathlib import Path

import yaml

from agent_platform.deploy.bootstrap_gateway_key import (
    ALLOWED_ROUTES as BOOTSTRAP_ALLOWED_ROUTES,
)
from agent_platform.deploy.database_permissions import ROLE_NAMES, ROLE_PRIVILEGES
from agent_platform.modules.gateway_control import ALLOWED_ROUTES


class ComposeStoragePermissionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "deploy/compose.yml"
        cls.compose = yaml.safe_load(path.read_text())
        cls.services = cls.compose["services"]

    def test_bootstrap_and_managed_runtime_keys_only_allow_responses(self):
        expected = ["/responses", "/v1/responses"]
        self.assertEqual(BOOTSTRAP_ALLOWED_ROUTES, expected)
        self.assertEqual(ALLOWED_ROUTES, expected)

    def test_api_reads_same_export_volume_that_maintenance_writes(self):
        api_mounts = self.services["api"]["volumes"]
        maintenance_mounts = self.services["maintenance"]["volumes"]
        self.assertEqual(len(api_mounts), 1)
        source, target, mode = api_mounts[0].split(":")
        self.assertEqual(mode, "ro")
        self.assertIn(f"{source}:{target}", maintenance_mounts)
        api_exports = self.services["api"]["environment"]["PLATFORM_EXPORT_DIRECTORY"]
        maintenance_exports = self.services["maintenance"]["environment"][
            "PLATFORM_EXPORT_DIRECTORY"
        ]
        self.assertEqual(api_exports, maintenance_exports)
        self.assertTrue(api_exports.startswith(target + "/"))

    def test_only_maintenance_mounts_independent_deletion_evidence(self):
        tombstone = self.services["maintenance"]["environment"][
            "PLATFORM_TOMBSTONE_PATH"
        ]
        expected = "tenant-tombstones:/app/agent_platform/.tombstones"
        self.assertIn(expected, self.services["maintenance"]["volumes"])
        self.assertTrue(tombstone.startswith(expected.split(":")[1] + "/"))
        for role in ("api", "worker", "gateway-sync", "init"):
            with self.subTest(role=role):
                self.assertFalse(
                    any(
                        "tenant-tombstones:" in mount
                        for mount in self.services[role].get("volumes", ())
                    )
                )
        self.assertTrue(self.compose["volumes"]["tenant-tombstones"]["external"])

    def test_litellm_bootstrap_default_excludes_platform_data_and_control_directory(
        self,
    ):
        mount = next(
            value
            for value in self.services["litellm"]["volumes"]
            if value.endswith(":/bootstrap")
        )
        self.assertIn("../.data/gateway-bootstrap", mount)
        self.assertNotIn("${GATEWAY_BOOTSTRAP_DIR:-../.data}:", mount)

    def test_background_execution_and_init_have_no_artifact_mounts(self):
        for role in ("worker", "gateway-sync", "init"):
            with self.subTest(role=role):
                self.assertEqual(self.services[role].get("volumes", []), [])

    def test_application_processes_use_independent_database_credentials(self):
        passwords = set()
        for service, name in (
            ("api", "API"),
            ("worker", "WORKER"),
            ("gateway-sync", "GATEWAY_SYNC"),
            ("maintenance", "MAINTENANCE"),
        ):
            with self.subTest(service=service):
                environment = self.services[service]["environment"]
                url = environment["PLATFORM_DATABASE_URL"]
                role = "platform_" + service.replace("-", "_")
                password = "PLATFORM_" + name + "_DB_PASSWORD"
                self.assertIn(f"postgresql+psycopg://{role}:", url)
                self.assertIn("${" + password + ":?", url)
                self.assertIn(password, self.services["postgres"]["environment"])
                self.assertNotIn("platform_runtime", url)
                passwords.add(password)
        self.assertEqual(len(passwords), 4)
        migration_url = self.services["init"]["environment"]["PLATFORM_DATABASE_URL"]
        self.assertIn("postgresql+psycopg://platform_migrator:", migration_url)
        self.assertIn("${PLATFORM_MIGRATION_DB_PASSWORD:?", migration_url)

    def test_existing_deployments_upgrade_to_the_same_service_permissions(self):
        # Keep new installs and the frozen upgrade contract aligned for this release.
        migration = import_module(
            "agent_platform.migrations.versions.0010_service_permissions"
        )
        self.assertEqual(migration.ROLE_NAMES, ROLE_NAMES)
        self.assertEqual(migration.PRIVILEGES, ROLE_PRIVILEGES)


if __name__ == "__main__":
    unittest.main()
