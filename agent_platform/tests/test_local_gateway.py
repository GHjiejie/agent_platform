"""Exercise managed gateway lifecycle without Docker, credentials, or an upstream."""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dotenv import dotenv_values

from agent_platform.scripts.gateway import CONTROL_SECRETS, SERVICES, LocalGateway
from agent_platform.scripts.local import Supervisor, load_runtime_environment


class LocalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = {
            "OPENAI_BASE_URL": "https://upstream.example/v1",
            "OPENAI_API_KEY": "test-upstream-key",
            "MODEL": "k3",
            "PLATFORM_ADMIN_EMAIL": "owner@example.com",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def successful_commands(self, gateway, runtime_key="sk-test-runtime-only"):
        """Mimic the bootstrap command's file output; never execute a process."""
        calls = []

        def run(args, env):
            calls.append((args, dict(env)))
            if "exec" in args:
                gateway.key_file.write_text(f"PLATFORM_LITELLM_KEY={runtime_key}\n")

        return run, calls

    @staticmethod
    def docker_ready(args, **kwargs):
        output = "127.0.0.1:16379\n" if args[-3:] == ["port", "redis", "6379"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def test_controls_are_random_private_persistent_and_separate(self):
        first = LocalGateway(self.root / "first", self.env, 4000)
        second = LocalGateway(self.root / "second", self.env, 4000)
        first.prepare()
        second.prepare()
        contents = first.control_file.read_bytes()
        controls = dotenv_values(first.control_file)
        other_controls = dotenv_values(second.control_file)

        self.assertEqual(stat.S_IMODE(first.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(first.control_file.stat().st_mode), 0o600)
        self.assertEqual(controls["UI_USERNAME"], "admin")
        self.assertTrue(controls["LITELLM_MASTER_KEY"].startswith("sk-"))
        for name in CONTROL_SECRETS:
            if name != "UI_USERNAME":
                self.assertGreaterEqual(len(controls[name]), 24)
                self.assertNotEqual(controls[name], other_controls[name])
        self.assertEqual(
            len({controls[name] for name in CONTROL_SECRETS}), len(CONTROL_SECRETS)
        )
        self.assertNotIn(self.env["OPENAI_API_KEY"].encode(), contents)

        restarted = LocalGateway(
            self.root / "first",
            {**self.env, "UI_PASSWORD": "new-password-must-not-replace-existing"},
            4001,
        )
        restarted.prepare()
        self.assertEqual(restarted.control_file.read_bytes(), contents)
        self.assertEqual(restarted.compose_env["UI_PASSWORD"], controls["UI_PASSWORD"])

    def test_existing_incomplete_controls_are_rejected_without_replacement(self):
        gateway = LocalGateway(self.root, self.env, 4000)
        gateway.directory.mkdir()
        contents = "UI_USERNAME=preserve-me\n# do not rotate database credentials\n"
        gateway.control_file.write_text(contents)
        with self.assertRaisesRegex(RuntimeError, "控制配置不完整"):
            gateway.prepare()
        self.assertEqual(gateway.control_file.read_text(), contents)

    def test_admin_password_cannot_reuse_master_key(self):
        gateway = LocalGateway(
            self.root,
            {**self.env, "UI_PASSWORD": "same-key", "LITELLM_MASTER_KEY": "same-key"},
            4000,
        )
        with self.assertRaisesRegex(RuntimeError, "必须独立"):
            gateway.prepare()

    def test_root_model_and_credentials_reach_compose_without_persisting_upstream(self):
        root_file = self.root / "root.env"
        platform_file = self.root / "platform.env"
        root_file.write_text(
            "OPENAI_API_KEY=root-test-key\n"
            "OPENAI_BASE_URL=https://root.example/v1\n"
            "MODEL=k3\n"
        )
        platform_file.write_text("PLATFORM_LITELLM_URL=\nPLATFORM_LITELLM_KEY=\n")
        env = load_runtime_environment(platform_file, root_file, {})
        gateway = LocalGateway(self.root / "state", env, 4010)
        gateway.prepare()

        self.assertEqual(gateway.compose_env["LITELLM_MODEL_ALIAS"], "k3")
        self.assertEqual(gateway.compose_env["UPSTREAM_MODEL"], "openai/k3")
        self.assertEqual(
            gateway.compose_env["UPSTREAM_API_BASE"], "https://root.example/v1"
        )
        self.assertEqual(gateway.compose_env["UPSTREAM_API_KEY"], "root-test-key")
        self.assertEqual(gateway.compose_env["LITELLM_HTTP_PORT"], "4010")
        self.assertNotIn("root-test-key", gateway.control_file.read_text())
        self.assertNotIn("root-test-key", platform_file.read_text())

    def test_explicit_upstream_and_alias_override_root_defaults(self):
        gateway = LocalGateway(
            self.root,
            {
                **self.env,
                "PLATFORM_DEFAULT_MODEL": "team-chat",
                "UPSTREAM_API_KEY": "explicit-upstream-key",
                "UPSTREAM_API_BASE": "https://explicit.example/v1",
                "UPSTREAM_MODEL": "openai/provider-model",
            },
            4000,
        )
        gateway.prepare()
        self.assertEqual(gateway.compose_env["LITELLM_MODEL_ALIAS"], "team-chat")
        self.assertEqual(gateway.compose_env["UPSTREAM_MODEL"], "openai/provider-model")
        self.assertEqual(
            gateway.compose_env["UPSTREAM_API_BASE"], "https://explicit.example/v1"
        )
        self.assertEqual(
            gateway.compose_env["UPSTREAM_API_KEY"], "explicit-upstream-key"
        )

    def test_compose_project_is_stable_per_resolved_state_directory(self):
        first = LocalGateway(self.root / "first", self.env, 4000)
        same = LocalGateway(self.root / "first" / ".." / "first", self.env, 4001)
        other = LocalGateway(self.root / "second", self.env, 4000)
        self.assertEqual(first.project, same.project)
        self.assertNotEqual(first.project, other.project)
        self.assertEqual(
            first.command[first.command.index("--project-name") + 1], first.project
        )
        self.assertEqual(
            first.command[first.command.index("--env-file") + 1],
            str(first.control_file),
        )

    def test_service_database_credentials_are_private_distinct_and_persistent(self):
        from agent_platform.scripts.gateway import (
            SERVICE_DATABASE_SECRETS,
            platform_environment,
        )

        gateway = LocalGateway(self.root, self.env, 4010)
        gateway.prepare()
        values = {key: gateway.compose_env[key] for key in SERVICE_DATABASE_SECRETS}
        self.assertEqual(len(set(values.values())), len(values))
        gateway.prepare()
        self.assertEqual(
            values, {key: gateway.compose_env[key] for key in SERVICE_DATABASE_SECRETS}
        )
        runtime = platform_environment(
            {
                **gateway.compose_env,
                "PLATFORM_API_DATABASE_URL": "postgresql://private-api-url",
            }
        )
        for key in (*SERVICE_DATABASE_SECRETS, "PLATFORM_API_DATABASE_URL"):
            self.assertNotIn(key, runtime)

    def test_bootstrap_mount_cannot_read_control_secrets_and_preserves_legacy_key(self):
        gateway = LocalGateway(self.root, self.env, 4010)
        gateway.directory.mkdir(parents=True)
        legacy = gateway.directory / ".env.gateway"
        legacy.write_text("PLATFORM_LITELLM_KEY=sk-disposable-legacy-only\n")
        gateway.prepare()
        mount = Path(gateway.compose_env["GATEWAY_BOOTSTRAP_DIR"])
        self.assertEqual(mount, gateway.bootstrap_directory)
        self.assertFalse((mount / ".env.control").exists())
        self.assertEqual(gateway.key_file.read_bytes(), legacy.read_bytes())
        self.assertEqual(stat.S_IMODE(gateway.key_file.stat().st_mode), 0o600)
        self.assertNotEqual(mount, gateway.directory)

    def test_start_passes_only_runtime_key_and_public_gateway_settings_to_platform(
        self,
    ):
        env = {
            **self.env,
            **{name: f"test-{name}" for name in CONTROL_SECRETS},
            "UPSTREAM_API_KEY": "explicit-upstream-key",
            "UPSTREAM_API_BASE": "https://explicit.example/v1",
            "UPSTREAM_MODEL": "openai/model",
            "PLATFORM_LITELLM_KEY": "previous-direct-upstream-key",
        }
        gateway = LocalGateway(self.root, env, 4010)
        run, commands = self.successful_commands(gateway)
        check_port = Mock()
        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch(
                "agent_platform.scripts.gateway.subprocess.run",
                side_effect=self.docker_ready,
            ),
        ):
            runtime = gateway.start(run, check_port)

        self.assertEqual(runtime["PLATFORM_LITELLM_KEY"], "sk-test-runtime-only")
        self.assertEqual(runtime["PLATFORM_LITELLM_URL"], "http://127.0.0.1:4010/v1")
        self.assertEqual(
            runtime["PLATFORM_LITELLM_ADMIN_URL"], "http://127.0.0.1:4010/ui"
        )
        self.assertEqual(runtime["PLATFORM_DEFAULT_MODEL"], "k3")
        self.assertEqual(runtime["PLATFORM_REDIS_URL"], "redis://127.0.0.1:16379/0")
        self.assertEqual(runtime["PLATFORM_ADMIN_EMAIL"], "owner@example.com")
        for name in (
            *CONTROL_SECRETS,
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "UPSTREAM_API_KEY",
            "UPSTREAM_API_BASE",
            "UPSTREAM_MODEL",
        ):
            self.assertNotIn(name, runtime)
        self.assertNotIn("previous-direct-upstream-key", runtime.values())
        self.assertEqual(stat.S_IMODE(gateway.key_file.stat().st_mode), 0o600)
        check_port.assert_called_once_with("127.0.0.1", 4010)
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0][0][:3], ["docker", "volume", "create"])
        self.assertIn("up", commands[1][0])
        self.assertIn("--reuse-existing", commands[2][0])
        self.assertEqual(commands[1][0][: len(gateway.command)], gateway.command)

    def test_explicit_redis_is_preserved_without_discovering_a_host_port(self):
        gateway = LocalGateway(
            self.root,
            {**self.env, "PLATFORM_REDIS_URL": "redis://custom-redis:6380/5"},
            4010,
        )
        run, _ = self.successful_commands(gateway)
        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch(
                "agent_platform.scripts.gateway.subprocess.run",
                side_effect=self.docker_ready,
            ) as docker,
        ):
            runtime = gateway.start(run, Mock())
        self.assertEqual(runtime["PLATFORM_REDIS_URL"], "redis://custom-redis:6380/5")
        self.assertFalse(
            any(
                call.args[0][-3:] == ["port", "redis", "6379"]
                for call in docker.call_args_list
            )
        )

    def test_managed_redis_discovery_rejects_missing_or_non_loopback_bindings(self):
        for output in ("", "0.0.0.0:16379\n", "127.0.0.1:0\n", "127.0.0.1:65536\n"):
            with self.subTest(binding=output):
                gateway = LocalGateway(self.root, self.env, 4010)
                run, _ = self.successful_commands(gateway)

                def docker(args, binding=output, **kwargs):
                    if args[-3:] == ["port", "redis", "6379"]:
                        return subprocess.CompletedProcess(
                            args, 0, stdout=binding, stderr=""
                        )
                    return self.docker_ready(args, **kwargs)

                with (
                    patch(
                        "agent_platform.scripts.gateway.shutil.which",
                        return_value="docker",
                    ),
                    patch(
                        "agent_platform.scripts.gateway.subprocess.run",
                        side_effect=docker,
                    ),
                    self.assertRaisesRegex(RuntimeError, "Redis"),
                ):
                    gateway.start(run, Mock())

    def test_occupied_port_is_rejected_before_up_and_stop_does_not_touch_docker(self):
        gateway = LocalGateway(self.root, self.env, 4000)
        run = Mock()
        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch(
                "agent_platform.scripts.gateway.subprocess.run",
                side_effect=self.docker_ready,
            ) as docker,
            self.assertRaisesRegex(RuntimeError, "LITELLM_PORT=4001"),
        ):
            gateway.start(run, Mock(side_effect=RuntimeError("occupied")))
        run.assert_not_called()
        self.assertFalse(gateway.started)
        before_stop = docker.call_count
        with patch("agent_platform.scripts.gateway.subprocess.run", docker):
            gateway.stop()
        self.assertEqual(docker.call_count, before_stop)

    def test_owned_gateway_can_restart_on_its_existing_port(self):
        gateway = LocalGateway(self.root, self.env, 4000)
        run, _ = self.successful_commands(gateway)
        check_port = Mock(side_effect=RuntimeError("port held by owned container"))

        def docker(args, **kwargs):
            output = ""
            if "ps" in args:
                output = "owned-container-id\n"
            elif "port" in args:
                output = "127.0.0.1:4000\n"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch("agent_platform.scripts.gateway.subprocess.run", side_effect=docker),
        ):
            gateway.start(run, check_port)
        check_port.assert_not_called()

    def test_bootstrap_rejects_master_key_as_platform_runtime_key(self):
        gateway = LocalGateway(
            self.root, {**self.env, "LITELLM_MASTER_KEY": "sk-test-master"}, 4000
        )
        run, _ = self.successful_commands(gateway, "sk-test-master")
        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch(
                "agent_platform.scripts.gateway.subprocess.run",
                side_effect=self.docker_ready,
            ),
            self.assertRaisesRegex(RuntimeError, "受限调用密钥"),
        ):
            gateway.start(run, Mock())
        self.assertTrue(gateway.started)

    def test_partial_start_cleanup_stops_only_owned_services_and_preserves_volumes(
        self,
    ):
        gateway = LocalGateway(self.root, self.env, 4000)
        with (
            patch("agent_platform.scripts.gateway.shutil.which", return_value="docker"),
            patch(
                "agent_platform.scripts.gateway.subprocess.run",
                side_effect=self.docker_ready,
            ) as docker,
        ):
            with self.assertRaisesRegex(RuntimeError, "up failed"):
                gateway.start(Mock(side_effect=RuntimeError("up failed")), Mock())
            self.assertTrue(gateway.started)
            gateway.stop()
            args, kwargs = docker.call_args
            self.assertEqual(
                args[0], [*gateway.command, "stop", "--timeout", "10", *SERVICES]
            )
            self.assertEqual(kwargs["env"], gateway.compose_env)
            self.assertNotIn("down", args[0])
            self.assertNotIn("--volumes", args[0])
            self.assertFalse(gateway.started)
            calls_after_stop = docker.call_count
            gateway.stop()
            self.assertEqual(docker.call_count, calls_after_stop)
        self.assertTrue(gateway.control_file.exists())

    def test_docker_stop_failure_does_not_interrupt_supervisor_cleanup(self):
        errors = (
            subprocess.TimeoutExpired(["docker", "compose", "stop"], timeout=45),
            OSError("Docker executable became unavailable"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                supervisor = Supervisor(self.root / type(error).__name__)
                gateway = LocalGateway(supervisor.state_dir, self.env, 4000)
                gateway.prepare()
                controls = gateway.control_file.read_bytes()
                gateway.started = True
                supervisor.gateway = gateway
                process_state = supervisor.state_dir / "process.json"
                process_state.write_text('{"phase":"running"}')
                with (
                    patch(
                        "agent_platform.scripts.gateway.subprocess.run",
                        side_effect=error,
                    ) as docker,
                    patch("builtins.print") as warning,
                ):
                    supervisor.cleanup()
                    self.assertFalse(gateway.started)
                    self.assertFalse(process_state.exists())
                    self.assertEqual(gateway.control_file.read_bytes(), controls)
                    warning.assert_called_once()
                    self.assertIn("数据库卷已保留", warning.call_args.args[0])
                    gateway.stop()
                    docker.assert_called_once()

    def test_identical_managed_ports_fail_before_files_or_processes_are_created(self):
        supervisor = Supervisor(self.root / "must-not-be-created")
        with (
            patch("agent_platform.scripts.local.check_port") as check_port,
            patch("agent_platform.scripts.gateway.LocalGateway") as gateway,
            patch.object(supervisor, "run") as run,
            patch.object(supervisor, "spawn") as spawn,
            self.assertRaisesRegex(RuntimeError, "必须使用不同端口"),
        ):
            supervisor.start(
                self.root / "platform.env",
                "127.0.0.1",
                4000,
                gateway_mode="managed",
                gateway_port=4000,
            )
        self.assertFalse(supervisor.state_dir.exists())
        check_port.assert_not_called()
        gateway.assert_not_called()
        run.assert_not_called()
        spawn.assert_not_called()

    def test_external_mode_uses_configured_gateway_without_creating_local_gateway(self):
        supervisor = Supervisor(self.root / "state")
        (self.root / "platform.env").write_text("# existing local configuration\n")
        external_env = {
            "PLATFORM_LITELLM_URL": "https://external.example/v1",
            "PLATFORM_LITELLM_KEY": "sk-test-external-runtime",
        }
        with (
            patch("agent_platform.scripts.local.check_port"),
            patch(
                "agent_platform.scripts.local.shutil.which", return_value="available"
            ),
            patch(
                "agent_platform.scripts.local.identity", return_value="test-supervisor"
            ),
            patch("agent_platform.scripts.local.prepare_config", return_value=False),
            patch(
                "agent_platform.scripts.local.load_runtime_environment",
                return_value=external_env,
            ),
            patch("agent_platform.scripts.local.say"),
            patch("agent_platform.scripts.gateway.LocalGateway") as gateway,
            patch.object(supervisor, "run"),
            patch.object(
                supervisor, "spawn", side_effect=RuntimeError("stop before process")
            ) as spawn,
            self.assertRaisesRegex(RuntimeError, "stop before process"),
        ):
            supervisor.start(
                self.root / "platform.env", "127.0.0.1", 8010, gateway_mode="external"
            )
        gateway.assert_not_called()
        self.assertIsNone(supervisor.gateway)
        runtime_env = spawn.call_args.args[1]
        for key, value in external_env.items():
            self.assertEqual(runtime_env[key], value)
        self.assertTrue(runtime_env["PLATFORM_SECRET_ENCRYPTION_KEY"])
        self.assertEqual(runtime_env["PLATFORM_MODE"], "local")
        self.assertEqual(runtime_env["PLATFORM_EMBEDDED_WORKER"], "false")
        self.assertFalse((supervisor.state_dir / "gateway").exists())


if __name__ == "__main__":
    unittest.main()
