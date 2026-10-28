"""Helm contracts: adoption, data retention, external credentials and endpoints."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
CHARTS = PLATFORM / "charts"


@unittest.skipUnless(shutil.which("helm"), "Helm is needed to render chart contracts")
class InfrastructureChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = yaml.safe_load(
            (PLATFORM / "deploy/kubernetes/stack.yaml").read_text()
        )["items"]

    def render(self, service, *, local=False, overrides=()):
        chart = CHARTS / service
        command = ["helm", "template", f"platform-{service}", str(chart)]
        if local:
            command += ["-f", str(chart / "values.local.yaml")]
        for override in overrides:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def resource(self, items, kind, name):
        return next(
            item
            for item in items
            if item["kind"] == kind and item["metadata"]["name"] == name
        )

    def test_local_statefulsets_preserve_adopted_immutable_contract(self):
        for service in ("postgres", "redis"):
            with self.subTest(service=service):
                current = self.resource(
                    self.render(service, local=True), "StatefulSet", service
                )
                old = self.resource(self.baseline, "StatefulSet", service)
                for field in ("serviceName", "selector", "volumeClaimTemplates"):
                    self.assertEqual(current["spec"][field], old["spec"][field])
                self.assertEqual(
                    current["spec"]["persistentVolumeClaimRetentionPolicy"],
                    {"whenDeleted": "Retain", "whenScaled": "Retain"},
                )

    def test_storage_adoption_keeps_both_existing_claim_specs(self):
        rendered = self.render("storage", local=True)
        self.assertEqual(
            {item["metadata"]["name"] for item in rendered},
            {"platform-data", "tenant-tombstones"},
        )
        for current in rendered:
            old = self.resource(
                self.baseline, "PersistentVolumeClaim", current["metadata"]["name"]
            )
            self.assertEqual(current["spec"], old["spec"])
            self.assertEqual(
                current["metadata"]["annotations"]["helm.sh/resource-policy"],
                "keep",
            )

    def test_generic_storage_separates_exports_from_sqlite(self):
        rendered = self.render("storage")
        self.assertEqual(
            {item["metadata"]["name"] for item in rendered},
            {"platform-exports", "tenant-tombstones"},
        )
        for item in rendered:
            self.assertEqual(item["spec"]["accessModes"], ["ReadWriteMany"])

    def test_releases_never_render_secrets_or_schedule_on_a_fixed_node(self):
        for service in ("postgres", "redis", "litellm", "storage"):
            with self.subTest(service=service):
                rendered = self.render(service)
                self.assertFalse(any(item["kind"] == "Secret" for item in rendered))
                for item in rendered:
                    if item["kind"] in ("Deployment", "StatefulSet"):
                        pod = item["spec"]["template"]["spec"]
                        self.assertNotIn("nodeSelector", pod)
                        self.assertFalse(pod["automountServiceAccountToken"])
                manifest = yaml.safe_load((CHARTS / service / "Chart.yaml").read_text())
                self.assertNotIn("dependencies", manifest)

    def test_gateway_redis_is_external_and_config_checksum_changes(self):
        original = self.render("litellm")
        updated = self.render(
            "litellm",
            overrides=(
                "redis.host=external-redis",
                "redis.port=6380",
                "redis.database=2",
            ),
        )
        config = self.resource(updated, "ConfigMap", "litellm-config")
        data = yaml.safe_load(config["data"]["litellm.yaml"])
        self.assertEqual(data["router_settings"]["redis_host"], "external-redis")
        self.assertEqual(data["router_settings"]["redis_port"], 6380)
        self.assertEqual(data["router_settings"]["redis_db"], 2)
        self.assertEqual(
            data["general_settings"]["database_url"], "os.environ/DATABASE_URL"
        )
        original_annotation = self.resource(original, "Deployment", "litellm")["spec"][
            "template"
        ]["metadata"]["annotations"]["checksum/config"]
        updated_annotation = self.resource(updated, "Deployment", "litellm")["spec"][
            "template"
        ]["metadata"]["annotations"]["checksum/config"]
        self.assertNotEqual(original_annotation, updated_annotation)
        self.assertFalse(any(item["kind"] == "StatefulSet" for item in updated))

    def test_redis_auth_uses_secret_refs_and_error_sensitive_probes(self):
        rendered = self.render(
            "redis", overrides=("auth.existingSecret=redis-credentials",)
        )
        container = self.resource(rendered, "StatefulSet", "redis")["spec"]["template"][
            "spec"
        ]["containers"][0]
        for variable in container["env"]:
            self.assertEqual(
                variable["valueFrom"]["secretKeyRef"]["name"], "redis-credentials"
            )
            self.assertNotIn("value", variable)
        self.assertEqual(container["command"], ["sh", "-ec"])
        self.assertIn(
            'test "$(redis-cli ping)" = PONG',
            container["readinessProbe"]["exec"]["command"],
        )
        gateway = self.render(
            "litellm", overrides=("redis.auth.existingSecret=redis-credentials",)
        )
        config = self.resource(gateway, "ConfigMap", "litellm-config")
        self.assertEqual(
            yaml.safe_load(config["data"]["litellm.yaml"])["router_settings"][
                "redis_password"
            ],
            "os.environ/REDIS_PASSWORD",
        )

    def test_redis_password_cannot_inject_configuration_directives(self):
        rendered = self.render(
            "redis", overrides=("auth.existingSecret=redis-credentials",)
        )
        container = self.resource(rendered, "StatefulSet", "redis")["spec"]["template"][
            "spec"
        ]["containers"][0]
        script = container["args"][0]
        with tempfile.TemporaryDirectory() as folder:
            # Exercise the config writer only; no server or user credentials are used.
            path = str(Path(folder) / "redis.conf")
            script = script.replace("/tmp/redis.conf", path).replace(
                f"exec redis-server {path}", f"cat {path}"
            )
            result = subprocess.run(
                ["sh", "-ec", script],
                env={
                    "PATH": os.environ["PATH"],
                    "REDIS_PASSWORD": 'a"b\\c\nreplicaof evil 123',
                },
                capture_output=True,
                text=True,
                check=True,
            )
        self.assertEqual(
            result.stdout,
            'appendonly yes\nmaxmemory-policy noeviction\nrequirepass "a\\"b\\\\c\\nreplicaof evil 123"\n',
        )

    def test_postgres_bootstrap_can_be_disabled_without_secret_scope(self):
        rendered = self.render(
            "postgres", overrides=("bootstrap.litellm.enabled=false",)
        )
        self.assertFalse(any(item["kind"] == "ConfigMap" for item in rendered))
        container = self.resource(rendered, "StatefulSet", "postgres")["spec"][
            "template"
        ]["spec"]["containers"][0]
        self.assertNotIn(
            "LITELLM_DB_PASSWORD", {item["name"] for item in container["env"]}
        )

    def test_schema_rejects_unknown_fields_and_unsafe_identifier(self):
        chart = CHARTS / "postgres"
        for override in (
            "credential.existingSecret=typo",
            "bootstrap.litellm.user=unsafe;value",
        ):
            with self.subTest(override=override):
                result = subprocess.run(
                    [
                        "helm",
                        "template",
                        "platform-postgres",
                        str(chart),
                        "--set",
                        override,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema", result.stderr)


if __name__ == "__main__":
    unittest.main()
