"""Helm orchestration safety regressions using only offline command doubles."""

import argparse
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_platform.scripts import helm as h


def resource(kind, name, *, selector=None, annotations=None):
    result = {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "annotations": annotations or {}},
        "spec": {},
    }
    if selector is not None:
        result["spec"]["selector"] = selector
    return result


def pvc(name, *, mode="Filesystem", size="5Gi"):
    result = resource("PersistentVolumeClaim", name)
    result["spec"] = {
        "storageClassName": "local-path",
        "accessModes": ["ReadWriteOnce"],
        "volumeMode": mode,
        "resources": {"requests": {"storage": size}},
    }
    return result


class HelmDeploymentTests(unittest.TestCase):
    def cluster(self, service="api", *, skip_build=True):
        cluster = object.__new__(h.HelmCluster)
        cluster.args = argparse.Namespace(
            service=service,
            skip_build=skip_build,
            values=None,
            env_file=Path("/nonexistent/.env"),
        )
        cluster.context = "orbstack"
        cluster.namespace = "agent-platform-test"
        cluster.image = "agent-platform:regression"
        cluster.build_id = "20261001T010203Z"
        cluster.helm = Mock(
            side_effect=lambda *args, **kwargs: (
                "platform-release\n" if args[0] == "list" else ""
            )
        )
        cluster.kube = Mock(return_value="secret/existing\n")
        cluster.get = Mock(return_value=None)
        cluster.render = Mock(return_value=[])
        cluster.retain_volumes = Mock()
        cluster.drain = Mock()
        cluster.settings = Mock(side_effect=AssertionError("credentials read"))
        return cluster

    def test_single_service_deploy_does_not_drain_migrate_or_touch_other_releases(self):
        cluster = self.cluster(skip_build=False)
        cluster.preflight = Mock()
        cluster.build_services = Mock()
        cluster.upgrade = Mock()
        cluster.migrate = Mock()
        with patch.object(h, "private_json"):
            cluster.deploy_releases()
        cluster.preflight.assert_called_once_with(("api",), adopt=False)
        cluster.build_services.assert_called_once_with(("api",))
        cluster.upgrade.assert_called_once_with("api", adopt=False)
        cluster.drain.assert_not_called()
        cluster.migrate.assert_not_called()
        cluster.settings.assert_not_called()

    def test_all_deploy_excludes_migration_build_and_release(self):
        self.assertNotIn("migration", h.selected_services("all"))
        self.assertEqual(h.selected_services("worker"), ("worker",))

    def test_image_targets_are_independent_and_registry_ports_are_preserved(self):
        self.assertEqual(
            h.service_image("registry.example:5000/team/platform:v2", "worker"),
            ("registry.example:5000/team/platform-worker", "v2"),
        )
        for invalid in ("platform", "platform:", "platform@sha256:123", ":tag"):
            with self.subTest(image=invalid), self.assertRaises(ValueError):
                h.service_image(invalid, "api")

    def test_values_file_cannot_accidentally_configure_every_service(self):
        cluster = self.cluster("all")
        cluster.args.values = Path("tenant-overrides.yaml")
        with self.assertRaisesRegex(RuntimeError, "一个"):
            cluster.values("worker")
        cluster.helm.assert_not_called()

    def test_build_only_requested_target_without_reading_credentials(self):
        cluster = self.cluster("worker")
        with patch.object(h.subprocess, "run") as command:
            cluster.build_services(("worker",))
        command.assert_called_once()
        args = command.call_args.args[0]
        self.assertEqual(args[args.index("--target") + 1], "worker")
        self.assertEqual(args[args.index("-t") + 1], "agent-platform-worker:regression")
        cluster.settings.assert_not_called()

    def test_preflight_checks_secret_presence_using_names_only(self):
        cluster = self.cluster()
        workload = resource("Deployment", "api")
        workload["spec"]["template"] = {
            "spec": {
                "containers": [
                    {
                        "envFrom": [{"secretRef": {"name": "runtime-secret"}}],
                        "env": [
                            {
                                "name": "PLATFORM_DATABASE_URL",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": "database-secret",
                                        "key": "connection",
                                    }
                                },
                            }
                        ],
                    }
                ],
                "volumes": [
                    {
                        "name": "exports",
                        "persistentVolumeClaim": {
                            "claimName": "private-exports",
                        },
                    }
                ],
            },
        }
        cluster.render.return_value = [workload]
        cluster.get.side_effect = lambda kind, name: (
            pvc(name) if kind == "pvc" else None
        )
        cluster.preflight(("api",))
        secret_calls = [call.args for call in cluster.kube.call_args_list]
        self.assertCountEqual(
            secret_calls,
            [
                ("get", "secret", "runtime-secret", "--ignore-not-found", "-o", "name"),
                (
                    "get",
                    "secret",
                    "database-secret",
                    "--ignore-not-found",
                    "-o",
                    "name",
                ),
            ],
        )
        self.assertFalse(
            any(call.args[0].lower() == "secret" for call in cluster.get.call_args_list)
        )
        cluster.settings.assert_not_called()

    def test_missing_secret_aborts_before_upgrading_workload(self):
        cluster = self.cluster()
        workload = resource("Deployment", "api")
        workload["spec"]["template"] = {
            "spec": {
                "containers": [
                    {
                        "env": [
                            {
                                "name": "KEY",
                                "valueFrom": {
                                    "secretKeyRef": {"name": "missing", "key": "KEY"}
                                },
                            }
                        ]
                    }
                ]
            }
        }
        cluster.render.return_value = [workload]
        cluster.kube.return_value = ""
        with (
            patch.object(h, "private_json"),
            self.assertRaisesRegex(RuntimeError, "Secret/missing"),
        ):
            cluster.deploy_releases()
        self.assertFalse(
            any(call.args[0] == "upgrade" for call in cluster.helm.call_args_list)
        )

    def test_adoption_cannot_steal_resource_owned_by_other_release_or_namespace(self):
        cluster = self.cluster()
        desired = resource(
            "Deployment", "api", selector={"matchLabels": {"app": "platform-api"}}
        )
        for owner, namespace in (
            ("other-release", cluster.namespace),
            ("platform-api", "other-namespace"),
        ):
            existing = copy.deepcopy(desired)
            existing["metadata"]["annotations"] = {
                "meta.helm.sh/release-name": owner,
                "meta.helm.sh/release-namespace": namespace,
            }
            with (
                self.subTest(owner=owner, namespace=namespace),
                self.assertRaisesRegex(RuntimeError, "其他"),
            ):
                cluster.check_ownership("api", desired, existing, adopt=True)

    def test_unmanaged_resource_requires_explicit_adoption(self):
        cluster = self.cluster()
        desired = resource(
            "Deployment", "api", selector={"matchLabels": {"app": "platform-api"}}
        )
        with self.assertRaisesRegex(RuntimeError, "首次"):
            cluster.check_ownership("api", desired, copy.deepcopy(desired), adopt=False)
        cluster.check_ownership("api", desired, copy.deepcopy(desired), adopt=True)

    def test_adoption_rejects_workload_and_service_selector_changes(self):
        cluster = self.cluster()
        for kind in ("Deployment", "StatefulSet", "Service"):
            selector = {"app": "platform-api"}
            if kind != "Service":
                selector = {"matchLabels": selector}
            desired = resource(kind, "api", selector=selector)
            existing = copy.deepcopy(desired)
            existing["spec"]["selector"] = {"app": "unrelated"}
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(RuntimeError, "selector"),
            ):
                cluster.check_ownership("api", desired, existing, adopt=True)

    def test_adoption_protects_existing_pvc_capacity_access_and_volume_mode(self):
        cluster = self.cluster("storage")
        desired = pvc("platform-data")
        for field, value in (
            ("volumeMode", "Block"),
            ("storageClassName", "another-class"),
            ("accessModes", ["ReadWriteMany"]),
            ("resources", {"requests": {"storage": "1Gi"}}),
        ):
            existing = copy.deepcopy(desired)
            existing["spec"][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                cluster.check_ownership("storage", desired, existing, adopt=True)

    def test_adoption_rejects_changed_statefulset_claim_template(self):
        cluster = self.cluster("postgres")
        desired = resource(
            "StatefulSet",
            "postgres",
            selector={"matchLabels": {"app": "platform-postgres"}},
        )
        desired["spec"].update(
            {"serviceName": "postgres", "volumeClaimTemplates": [pvc("data")]}
        )
        existing = copy.deepcopy(desired)
        existing["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"][
            "storage"
        ] = "1Gi"
        with self.assertRaisesRegex(RuntimeError, "持久卷"):
            cluster.check_ownership("postgres", desired, existing, adopt=True)

    def test_single_service_stop_preserves_values_and_does_not_drain_globally(self):
        cluster = self.cluster("worker")
        cluster.stop_releases()
        cluster.helm.assert_called_once()
        args = cluster.helm.call_args.args
        self.assertEqual(args[0], "upgrade")
        self.assertIn("platform-worker", args)
        self.assertIn("--reuse-values", args)
        self.assertIn("replicaCount=0", args)
        self.assertNotIn("-f", args)
        cluster.drain.assert_not_called()

    def test_start_restores_replica_without_resetting_release_configuration(self):
        cluster = self.cluster("worker")
        cluster.start_releases()
        cluster.helm.assert_called_once()
        args = cluster.helm.call_args.args
        self.assertIn("platform-worker", args)
        self.assertIn("--reuse-values", args)
        self.assertIn("replicaCount=1", args)
        self.assertNotIn("-f", args)
        self.assertFalse(any(str(arg).startswith("image.") for arg in args))
        cluster.drain.assert_not_called()
        cluster.settings.assert_not_called()

    def test_migration_preflight_matches_unique_release_and_job_version(self):
        cluster = self.cluster()
        cluster.migrate()
        job_calls = [
            call.args
            for call in cluster.helm.call_args_list
            if "--wait-for-jobs" in call.args
        ]
        self.assertEqual(len(job_calls), 1)
        execution = job_calls[0]
        execution_release = execution[execution.index("install") + 1]
        version = next(arg for arg in execution if str(arg).startswith("job.version="))
        self.assertTrue(execution_release.startswith("platform-migration-"))
        cluster.render.assert_called_once()
        preview = cluster.render.call_args
        self.assertEqual(preview.args[0], "migration")
        self.assertEqual(preview.kwargs.get("release"), execution_release)
        self.assertIn(version, preview.kwargs.get("extra", ()))
        cluster.drain.assert_called_once()

    def test_adoption_rechecks_ownership_immediately_before_takeover(self):
        cluster = self.cluster()
        cluster.preflight = Mock(side_effect=RuntimeError("ownership changed"))
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            cluster.upgrade("api", adopt=True)
        cluster.preflight.assert_called_once_with(("api",), adopt=True, snapshot=False)
        cluster.helm.assert_not_called()

    def test_redeploy_reuses_custom_release_values(self):
        cluster = self.cluster()
        cluster.upgrade("api")
        args = cluster.helm.call_args.args
        self.assertIn("--reuse-values", args)
        self.assertNotIn("--reset-values", args)
        self.assertNotIn("--take-ownership", args)

    def test_volume_retention_only_changes_claims_used_by_selected_service(self):
        cluster = self.cluster("api")
        workload = resource("Deployment", "api")
        workload["spec"]["template"] = {
            "spec": {
                "volumes": [
                    {
                        "name": "exports",
                        "persistentVolumeClaim": {"claimName": "api-exports"},
                    }
                ]
            }
        }
        cluster.render.return_value = [workload]

        def get(kind, name):
            if (kind, name) == ("pvc", "api-exports"):
                return {"spec": {"volumeName": "api-volume"}}
            if (kind, name) == ("pv", "api-volume"):
                return {"spec": {"persistentVolumeReclaimPolicy": "Delete"}}
            raise AssertionError(f"unrelated volume accessed: {kind}/{name}")

        cluster.get.side_effect = get
        cluster.retain_service_volumes(("api",))
        cluster.render.assert_called_once_with("api")
        self.assertEqual(cluster.get.call_count, 2)
        cluster.kube.assert_called_once()
        args = cluster.kube.call_args.args
        self.assertEqual(args[:3], ("patch", "pv", "api-volume"))
        cluster.retain_volumes.assert_not_called()

    def test_wrong_context_is_rejected_before_commands_or_credential_reads(self):
        with patch.object(h, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "orbstack"):
                h.HelmCluster(argparse.Namespace(context="production"))
            command.assert_not_called()

    def test_render_refuses_secret_manifests(self):
        cluster = self.cluster()
        cluster.render = h.HelmCluster.render.__get__(cluster)
        cluster.helm.side_effect = None
        cluster.helm.return_value = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: unsafe\n"
        )
        with self.assertRaisesRegex(RuntimeError, "Secret"):
            cluster.render("api")


if __name__ == "__main__":
    unittest.main()
