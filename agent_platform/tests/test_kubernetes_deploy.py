"""Deployment boundary regressions; no cluster, credentials or model traffic."""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_platform.scripts import kubernetes as k


class KubernetesDeploymentTests(unittest.TestCase):
    def cluster(self):
        cluster = object.__new__(k.Cluster)
        cluster.namespace = "agent-platform-test"
        cluster.image = "agent-platform:test"
        cluster.args = argparse.Namespace(env_file=Path("/nonexistent/.env"))
        cluster.kube = Mock(return_value="")
        cluster.apply = Mock()
        return cluster

    def test_other_context_is_rejected_before_any_cluster_command(self):
        with patch.object(k, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "orbstack"):
                k.Cluster(argparse.Namespace(context="production"))
            command.assert_not_called()

    def test_roles_receive_only_their_secret_scopes(self):
        resources = self.cluster().resources()
        deployments = {
            r["metadata"]["name"]: r for r in resources if r["kind"] == "Deployment"
        }
        for role in k.ROLES:
            resource = deployments[role]
            self.assertEqual(resource["spec"]["replicas"], 0)
            pod = resource["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            container = pod["containers"][0]
            secret_names = {
                ref["secretRef"]["name"]
                for ref in container["envFrom"]
                if "secretRef" in ref
            }
            expected = {"platform-runtime-secret"}
            if role == "gateway-sync":
                expected.add("gateway-control-secret")
            self.assertEqual(secret_names, expected)

    def test_migration_selects_deployment_not_same_named_service(self):
        cluster = self.cluster()
        cluster.migrate_schema()
        job = cluster.apply.call_args.args[0][0]
        self.assertEqual(job["kind"], "Job")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["command"][0], "alembic")
        self.assertNotIn("readinessProbe", container)

    def test_redeploy_does_not_read_or_overwrite_old_credentials(self):
        cluster = self.cluster()
        cluster.settings = Mock(side_effect=AssertionError("legacy credentials read"))

        def existing(kind, name):
            if kind == "statefulset":
                return {"spec": {"replicas": 1}}
            if kind == "configmap":
                return {"data": {"PLATFORM_DEFAULT_MODEL": "existing-model"}}
            return {"metadata": {"name": name}}

        cluster.get = Mock(side_effect=existing)
        cluster.install_config(initial=False)
        cluster.settings.assert_not_called()
        applied = [r for call in cluster.apply.call_args_list for r in call.args[0]]
        self.assertFalse(any(r["kind"] == "Secret" for r in applied))
        for resource in applied:
            if resource["kind"] == "StatefulSet":
                self.assertEqual(resource["spec"]["replicas"], 1)

    def test_import_refuses_existing_destination(self):
        cluster = self.cluster()
        cluster.get = Mock(return_value={"metadata": {"name": "platform-data"}})
        cluster.settings = Mock()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(k, "STATE", Path(folder)),
            self.assertRaisesRegex(RuntimeError, "不会覆盖"),
        ):
            cluster.import_local()
        cluster.settings.assert_not_called()
        cluster.apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
