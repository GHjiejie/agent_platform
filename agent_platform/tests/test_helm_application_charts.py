"""Render application Helm contracts offline, including unsafe-value rejection."""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

CHARTS = Path(__file__).resolve().parents[1] / "charts"
ROLES = ("api", "worker", "gateway-sync", "maintenance", "migration")
DEPLOYMENTS = tuple(role for role in ROLES if role != "migration")
CONFIG_KEYS = {
    "api": {
        "PLATFORM_MODE",
        "PLATFORM_REDIS_URL",
        "PLATFORM_SECURE_COOKIES",
        "PLATFORM_EMBEDDED_WORKER",
        "PLATFORM_LITELLM_URL",
        "PLATFORM_LITELLM_ADMIN_URL",
        "PLATFORM_MAX_RUN_COST",
        "PLATFORM_EXPORT_DIRECTORY",
    },
    "worker": {
        "PLATFORM_MODE",
        "PLATFORM_REDIS_URL",
        "PLATFORM_LITELLM_URL",
        "PLATFORM_WORKER_CONCURRENCY",
        "PLATFORM_RUN_TIMEOUT",
        "PLATFORM_MODEL_TIMEOUT",
        "PLATFORM_MAX_RUN_COST",
    },
    "gateway-sync": {
        "PLATFORM_MODE",
        "PLATFORM_GATEWAY_CONTROL_URL",
        "PLATFORM_GATEWAY_SYNC_INTERVAL",
    },
    "maintenance": {
        "PLATFORM_MODE",
        "PLATFORM_MAINTENANCE_INTERVAL",
        "PLATFORM_MAINTENANCE_BATCH_SIZE",
        "PLATFORM_EXPORT_TTL_SECONDS",
        "PLATFORM_DELETION_RETENTION_DAYS",
        "PLATFORM_EXPORT_DIRECTORY",
        "PLATFORM_TOMBSTONE_PATH",
    },
}


class ApplicationChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helm = shutil.which("helm")
        if not cls.helm:
            raise unittest.SkipTest(
                "Helm is required for offline chart contract checks"
            )
        cls.rendered = {}
        for role in ROLES:
            for local in (False, True):
                result = cls.command(role, local=local)
                if result.returncode:
                    raise AssertionError(result.stderr)
                cls.rendered[role, local] = list(yaml.safe_load_all(result.stdout))

    @classmethod
    def command(cls, role, *, local=False, overrides=(), skip_schema=False):
        command = [cls.helm, "template", f"platform-{role}", str(CHARTS / role)]
        if local:
            command.extend(("-f", str(CHARTS / role / "values.local.yaml")))
        if skip_schema:
            command.append("--skip-schema-validation")
        for value in overrides:
            command.extend(("--set", value))
        return subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=30
        )

    def resources(self, role, *, local=False):
        return self.rendered[role, local]

    def workload(self, role, *, local=False):
        kind = "Job" if role == "migration" else "Deployment"
        return next(
            item for item in self.resources(role, local=local) if item["kind"] == kind
        )

    def pod(self, role, *, local=False):
        return self.workload(role, local=local)["spec"]["template"]["spec"]

    def secret_refs(self, role, *, local=False):
        container = self.pod(role, local=local)["containers"][0]
        self.assertFalse(
            any("secretRef" in item for item in container.get("envFrom", []))
        )
        return {
            variable["name"]: variable["valueFrom"]["secretKeyRef"]
            for variable in container.get("env", [])
            if "secretKeyRef" in variable.get("valueFrom", {})
        }

    def test_each_chart_deploys_only_its_service_without_dependencies_or_secrets(self):
        for role in ROLES:
            with self.subTest(role=role):
                chart = yaml.safe_load((CHARTS / role / "Chart.yaml").read_text())
                self.assertFalse(chart.get("dependencies"))
                kinds = {item["kind"] for item in self.resources(role)}
                expected = (
                    {"Job"} if role == "migration" else {"Deployment", "ConfigMap"}
                )
                if role == "api":
                    expected.add("Service")
                self.assertEqual(kinds, expected)
                self.assertEqual(len(self.pod(role)["containers"]), 1)
                self.assertFalse(self.pod(role)["automountServiceAccountToken"])

    def test_defaults_project_only_role_specific_secret_keys(self):
        for role in ROLES:
            with self.subTest(role=role):
                expected = {
                    "PLATFORM_DATABASE_URL": {
                        "name": "platform-migrator-secret"
                        if role == "migration"
                        else f"platform-{role}-database-secret",
                        "key": "PLATFORM_DATABASE_URL",
                    },
                }
                if role != "migration":
                    expected["PLATFORM_SECRET_ENCRYPTION_KEY"] = {
                        "name": "platform-runtime-secret",
                        "key": "PLATFORM_SECRET_ENCRYPTION_KEY",
                    }
                if role == "gateway-sync":
                    expected["PLATFORM_GATEWAY_CONTROL_KEY"] = {
                        "name": "gateway-control-secret",
                        "key": "PLATFORM_GATEWAY_CONTROL_KEY",
                    }
                self.assertEqual(self.secret_refs(role), expected)
                env = self.pod(role)["containers"][0]["env"]
                self.assertFalse(
                    any(
                        "ADMIN_PASSWORD" in variable["name"]
                        or "UPSTREAM" in variable["name"]
                        or "MASTER_KEY" in variable["name"]
                        for variable in env
                    )
                )

    def test_each_service_configmap_contains_only_its_configuration(self):
        for role, expected in CONFIG_KEYS.items():
            with self.subTest(role=role):
                config = next(
                    item for item in self.resources(role) if item["kind"] == "ConfigMap"
                )
                self.assertEqual(set(config["data"]), expected)
                self.assertNotIn("PLATFORM_DATABASE_URL", config["data"])
                self.assertNotIn("PLATFORM_SECRET_ENCRYPTION_KEY", config["data"])
                self.assertNotIn("PLATFORM_GATEWAY_CONTROL_KEY", config["data"])

    def test_default_storage_is_limited_to_export_reader_and_maintenance_writer(self):
        for role in ("worker", "gateway-sync", "migration"):
            with self.subTest(role=role):
                self.assertFalse(self.pod(role).get("volumes"))
                self.assertFalse(self.pod(role)["containers"][0].get("volumeMounts"))
        api = self.pod("api")
        self.assertEqual(
            api["volumes"],
            [
                {
                    "name": "exports",
                    "persistentVolumeClaim": {"claimName": "platform-exports"},
                }
            ],
        )
        self.assertTrue(api["containers"][0]["volumeMounts"][0]["readOnly"])
        maintenance = self.pod("maintenance")
        claims = {
            item["persistentVolumeClaim"]["claimName"]
            for item in maintenance["volumes"]
        }
        self.assertEqual(claims, {"platform-exports", "tenant-tombstones"})
        self.assertFalse(
            next(
                item
                for item in maintenance["containers"][0]["volumeMounts"]
                if item["name"] == "exports"
            )["readOnly"]
        )
        for role in ROLES:
            with self.subTest(role=role):
                self.assertFalse(self.pod(role).get("nodeSelector"))

    def test_local_sqlite_profile_retains_existing_claim_and_single_node_strategy(self):
        for role in ROLES:
            with self.subTest(role=role):
                pod = self.pod(role, local=True)
                self.assertEqual(
                    pod["nodeSelector"], {"kubernetes.io/hostname": "orbstack"}
                )
                self.assertIn(
                    {
                        "name": "sqlite",
                        "persistentVolumeClaim": {"claimName": "platform-data"},
                    },
                    pod["volumes"],
                )
                self.assertFalse(
                    any(item["name"] == "exports" for item in pod["volumes"])
                )
                env = pod["containers"][0]["env"]
                self.assertIn(
                    {
                        "name": "PLATFORM_DATABASE_URL",
                        "value": "sqlite:////app/agent_platform/.data/platform.db",
                    },
                    env,
                )
                if role != "migration":
                    self.assertEqual(
                        self.workload(role, local=True)["spec"]["strategy"]["type"],
                        "Recreate",
                    )
                    self.assertEqual(
                        self.workload(role, local=True)["spec"]["replicas"], 1
                    )
                secret_names = set(self.secret_refs(role, local=True))
                self.assertEqual(
                    "PLATFORM_LITELLM_KEY" in secret_names, role in {"api", "worker"}
                )

    def test_background_probes_use_this_pod_uid_instead_of_other_replicas(self):
        for role in ("worker", "gateway-sync", "maintenance"):
            with self.subTest(role=role):
                container = self.pod(role)["containers"][0]
                self.assertIn(
                    {
                        "name": "PLATFORM_SERVICE_INSTANCE",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                    },
                    container["env"],
                )
                for name in ("startupProbe", "readinessProbe", "livenessProbe"):
                    self.assertEqual(
                        container[name]["exec"]["command"],
                        ["python", "-m", "agent_platform.scripts.service_probe", role],
                    )

    def test_manual_migration_has_no_hooks_runtime_key_or_initialization(self):
        job = self.workload("migration")
        self.assertEqual(job["metadata"]["name"], "migration-manual-1")
        self.assertEqual(self.pod("migration")["restartPolicy"], "Never")
        self.assertEqual(
            self.pod("migration")["containers"][0]["command"],
            ["alembic", "-c", "agent_platform/alembic.ini", "upgrade", "head"],
        )
        for item in self.resources("migration"):
            self.assertNotIn("helm.sh/hook", item["metadata"].get("annotations", {}))
        self.assertEqual(set(self.secret_refs("migration")), {"PLATFORM_DATABASE_URL"})

    def test_sqlite_rejects_multiple_replicas_even_without_schema_validation(self):
        for role in DEPLOYMENTS:
            for skip_schema in (False, True):
                with self.subTest(role=role, skip_schema=skip_schema):
                    result = self.command(
                        role,
                        local=True,
                        overrides=("replicaCount=2",),
                        skip_schema=skip_schema,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("replicaCount", result.stderr)

    def test_sqlite_rejects_rolling_updates_even_without_schema_validation(self):
        for role in DEPLOYMENTS:
            with self.subTest(role=role):
                result = self.command(
                    role,
                    local=True,
                    overrides=("strategy.type=RollingUpdate",),
                    skip_schema=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Recreate", result.stderr)

    def test_sqlite_stopped_replica_zero_is_allowed(self):
        for role in DEPLOYMENTS:
            with self.subTest(role=role):
                result = self.command(role, local=True, overrides=("replicaCount=0",))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_sqlite_url_and_volume_must_be_enabled_together(self):
        for role in ROLES:
            for overrides in (("sqliteStorage.enabled=false",), ("database.url=",)):
                with self.subTest(role=role, overrides=overrides):
                    result = self.command(
                        role, local=True, overrides=overrides, skip_schema=True
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("sqliteStorage", result.stderr)

    def test_plain_postgresql_url_is_rejected_instead_of_becoming_release_values(self):
        for role in ROLES:
            with self.subTest(role=role):
                result = self.command(
                    role, overrides=("database.url=postgresql://database/platform",)
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("database", result.stderr)

    def test_configmap_cannot_inject_database_or_control_credentials(self):
        for role in ROLES:
            with self.subTest(role=role):
                result = self.command(
                    role,
                    overrides=(
                        "config.PLATFORM_DATABASE_URL=postgresql://database/platform",
                    ),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("config", result.stderr)

    def test_worker_requires_timeout_plus_thirty_seconds_before_kubernetes_kill(self):
        for overrides in (
            ("config.runTimeout=300", "terminationGracePeriodSeconds=329"),
            ("config.runTimeout=400",),
        ):
            with self.subTest(overrides=overrides):
                result = self.command("worker", overrides=overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("runTimeout + 30", result.stderr)
        result = self.command(
            "worker",
            overrides=("config.runTimeout=400", "terminationGracePeriodSeconds=430"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_accepts_unlimited_execution_with_independent_shutdown_grace(self):
        result = self.command("worker", overrides=("config.runTimeout=0",))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
