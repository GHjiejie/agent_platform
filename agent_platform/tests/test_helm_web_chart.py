"""Frontend release contracts: streaming, identity forwarding and no Python runtime."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
CHART = PLATFORM / "charts/web"


@unittest.skipUnless(shutil.which("helm"), "Helm is needed to render web contracts")
class WebChartTests(unittest.TestCase):
    def render(self, *, local=False, overrides=()):
        command = ["helm", "template", "platform-web", str(CHART)]
        if local:
            command += ["-f", str(CHART / "values.local.yaml")]
        for override in overrides:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return [item for item in yaml.safe_load_all(result.stdout) if item]

    def resource(self, items, kind):
        return next(item for item in items if item["kind"] == kind)

    def test_frontend_has_no_data_or_credential_dependencies(self):
        rendered = self.render()
        self.assertEqual(
            {item["kind"] for item in rendered}, {"ConfigMap", "Deployment", "Service"}
        )
        pod = self.resource(rendered, "Deployment")["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("nodeSelector", pod)
        self.assertEqual(
            {volume["name"] for volume in pod["volumes"]}, {"config", "tmp"}
        )
        for volume in pod["volumes"]:
            self.assertTrue("configMap" in volume or "emptyDir" in volume)
        container = pod["containers"][0]
        self.assertNotIn("env", container)
        self.assertNotIn("envFrom", container)
        self.assertNotIn(
            "dependencies", yaml.safe_load((CHART / "Chart.yaml").read_text())
        )

    def test_runtime_is_nonroot_readonly_and_probes_are_local(self):
        pod = self.resource(self.render(), "Deployment")["spec"]["template"]["spec"]
        self.assertEqual(pod["securityContext"]["runAsUser"], 101)
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(container["ports"][0]["containerPort"], 8080)
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"]["path"], "/healthz")

    def test_streaming_and_identity_headers_are_preserved(self):
        config = self.resource(self.render(), "ConfigMap")["data"]["server.conf"]
        for directive in (
            "proxy_buffering off;",
            "proxy_request_buffering off;",
            "proxy_cache off;",
            "gzip off;",
            "proxy_http_version 1.1;",
            'proxy_set_header Connection "";',
            "proxy_set_header Host $http_host;",
            "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "proxy_read_timeout 3600s;",
        ):
            self.assertIn(directive, config)
        for directive in (
            "proxy_hide_header Set-Cookie",
            "proxy_cookie_domain",
            "proxy_cookie_path",
        ):
            self.assertNotIn(directive, config)

    def test_changed_upstream_rolls_only_web_config(self):
        original = self.resource(self.render(), "Deployment")["spec"]["template"][
            "metadata"
        ]["annotations"]["checksum/config"]
        updated = self.resource(
            self.render(overrides=("config.apiUpstream=http://external-api:8080",)),
            "Deployment",
        )["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        self.assertNotEqual(original, updated)
        local = self.render(local=True)
        self.assertEqual(
            self.resource(local, "Service")["spec"]["ports"][0]["port"], 8010
        )
        self.assertEqual(
            self.resource(local, "Service")["spec"]["type"], "LoadBalancer"
        )

    def test_schema_rejects_nginx_injection_bad_ports_and_unknown_fields(self):
        schema = json.loads((CHART / "values.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        for override in (
            "config.apiUpstream=http://api;return 200 evil",
            "config.apiUpstream=http://api:65536",
            "config.apiUpstream=http://api:0",
            "config.maxBodySize=10m;return 200 evil",
            "config.streamTimeoutSeconds=300;return 200 evil",
            "config.apiUpsream=http://typo",
            "podAnnotations.checksum/config=override",
        ):
            with self.subTest(override=override):
                result = subprocess.run(
                    ["helm", "template", "platform-web", str(CHART), "--set", override],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema", result.stderr)

    def test_image_build_and_nginx_config_are_independent_of_python(self):
        dockerfile = (PLATFORM / "deploy/Dockerfile.web").read_text()
        self.assertIn("FROM node:", dockerfile)
        self.assertIn("FROM nginx:", dockerfile)
        self.assertNotIn("FROM python:", dockerfile)
        self.assertNotIn("uv sync", dockerfile)
        self.assertNotIn("pyproject.toml", dockerfile)
        self.assertIn("USER 101:101", dockerfile)
        self.assertIn('ENTRYPOINT ["nginx"]', dockerfile)
        config = (PLATFORM / "deploy/nginx.conf").read_text()
        self.assertIn("pid /tmp/nginx.pid;", config)
        self.assertIn("include /etc/nginx/platform/server.conf;", config)
        for line in config.splitlines():
            if "_temp_path" in line:
                self.assertIn("/tmp/", line)
        self.assertNotIn("$request_uri", config)


if __name__ == "__main__":
    unittest.main()
