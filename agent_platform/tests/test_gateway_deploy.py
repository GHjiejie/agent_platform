"""Deployment key lifecycle regression tests; never contact a real gateway."""

import copy
import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_platform.deploy import bootstrap_gateway_key as gateway


class GatewayBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / ".env.gateway"
        self.environment = patch.dict(
            os.environ,
            {
                "GATEWAY_KEY_OUTPUT": str(self.output),
                "LITELLM_MASTER_KEY": "sk-test-master",
                "LITELLM_MODEL_ALIAS": "test-model",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.info = {
            "expires": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "models": ["test-model"],
            "allowed_routes": list(gateway.ALLOWED_ROUTES),
            "user_id": "agent-platform-runtime-test",
            "max_budget": 100,
            "rpm_limit": 60,
            "tpm_limit": 120000,
            "max_parallel_requests": 4,
            "spend": 1,
        }
        self.owner = {
            "user_info": {"user_role": "internal_user", "models": ["test-model"]}
        }

    def save_existing(self):
        self.output.write_text("PLATFORM_LITELLM_KEY=sk-test-runtime\n")
        self.output.chmod(0o600)

    def test_new_key_is_scoped_private_and_does_not_receive_management_type(self):
        with patch.object(
            gateway, "control_request", side_effect=[{}, {"key": "sk-test-runtime"}]
        ) as control:
            self.assertEqual(gateway.bootstrap(), self.output)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(
            self.output.read_text(), "PLATFORM_LITELLM_KEY=sk-test-runtime\n"
        )
        user_request = control.call_args_list[0].args[3]
        key_request = control.call_args_list[1].args[3]
        self.assertEqual(user_request["user_role"], "internal_user")
        self.assertFalse(user_request["auto_create_key"])
        self.assertEqual(key_request["user_id"], user_request["user_id"])
        self.assertEqual(key_request["key_type"], "default")
        self.assertEqual(key_request["models"], ["test-model"])
        self.assertEqual(key_request["allowed_routes"], gateway.ALLOWED_ROUTES)
        self.assertEqual(key_request["duration"], "30d")

    def test_multiple_models_scope_both_new_user_and_key(self):
        with (
            patch.dict(os.environ, {"GATEWAY_KEY_MODELS": "test-model, gpt-5.5"}),
            patch.object(
                gateway, "control_request", side_effect=[{}, {"key": "sk-test-runtime"}]
            ) as control,
        ):
            self.assertEqual(gateway.bootstrap(), self.output)
        for call in control.call_args_list:
            self.assertEqual(call.args[3]["models"], ["test-model", "gpt-5.5"])
        key_request = control.call_args_list[1].args[3]
        self.assertEqual(key_request["allowed_routes"], gateway.ALLOWED_ROUTES)
        self.assertEqual(key_request["max_budget"], 100)
        self.assertEqual(key_request["duration"], "30d")

    def test_invalid_model_allowlists_rejected_before_side_effects(self):
        for configured in (
            "",
            " ",
            "test-model,",
            ",test-model",
            "test-model,,gpt-5.5",
            "test-model, ,gpt-5.5",
            "test-model,*",
            "test-model,gpt-*",
            "test-model,test-model",
            "test-model, test-model",
            "gpt-5.5",
            "test-model,invalid name",
            "test-model,invalid?name",
            "test-model,invalid;name",
            "test-model,\ngpt-5.5",
            "test-model,\x7fgpt-5.5",
            "test-model," + "x" * 201,
        ):
            with (
                self.subTest(configured=configured),
                patch.dict(os.environ, {"GATEWAY_KEY_MODELS": configured}),
                patch.object(gateway, "control_request") as control,
            ):
                with self.assertRaisesRegex(ValueError, "GATEWAY_KEY_MODELS"):
                    gateway.bootstrap()
                control.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_invalid_default_model_alias_rejected_before_side_effects(self):
        for alias in ("", "*", "invalid name", "test-model,second", "x" * 201):
            with (
                self.subTest(alias=alias),
                patch.dict(os.environ, {"LITELLM_MODEL_ALIAS": alias}),
                patch.object(gateway, "control_request") as control,
            ):
                with self.assertRaisesRegex(ValueError, "model alias"):
                    gateway.bootstrap()
                control.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_default_refuses_existing_file_without_network_or_overwrite(self):
        self.save_existing()
        with patch.object(gateway, "control_request") as control:
            with self.assertRaisesRegex(RuntimeError, "reuse-existing"):
                gateway.bootstrap()
            control.assert_not_called()
        self.assertEqual(
            self.output.read_text(), "PLATFORM_LITELLM_KEY=sk-test-runtime\n"
        )

    def test_reuse_checks_owner_and_key_without_creating_or_rewriting(self):
        self.save_existing()
        previous = self.output.stat().st_mtime_ns
        with patch.object(
            gateway, "control_request", side_effect=[{"info": [self.info]}, self.owner]
        ) as control:
            self.assertEqual(gateway.bootstrap(reuse_existing=True), self.output)
        self.assertEqual(self.output.stat().st_mtime_ns, previous)
        self.assertEqual(control.call_args_list[0].args[2], "/v2/key/info")
        self.assertNotIn("sk-test-runtime", control.call_args_list[0].args[2])
        self.assertEqual(control.call_count, 2)
        self.assertTrue(control.call_args_list[1].args[2].startswith("/user/info?"))

    def test_multiple_model_reuse_accepts_order_only_changes_without_rewriting(self):
        self.save_existing()
        previous = self.output.stat().st_mtime_ns
        info = {**self.info, "models": ["gpt-5.5", "test-model"]}
        owner = {
            "user_info": {
                "user_role": "internal_user",
                "models": ["test-model", "gpt-5.5"],
            }
        }
        with (
            patch.dict(os.environ, {"GATEWAY_KEY_MODELS": "test-model,gpt-5.5"}),
            patch.object(
                gateway, "control_request", side_effect=[{"info": [info]}, owner]
            ) as control,
        ):
            self.assertEqual(gateway.bootstrap(reuse_existing=True), self.output)
        self.assertEqual(control.call_count, 2)
        self.assertEqual(self.output.stat().st_mtime_ns, previous)

    def test_multiple_model_reuse_rejects_invalid_key_or_owner_scopes(self):
        self.save_existing()
        expected = ["test-model", "gpt-5.5"]
        invalid_models = (
            None,
            "test-model,gpt-5.5",
            [],
            ["test-model"],
            ["test-model", "gpt-5.5", "extra-model"],
            ["test-model", "gpt-5.5", "gpt-5.5"],
            ["test-model", "*"],
            ["test-model", {"model": "gpt-5.5"}],
        )
        for invalid in invalid_models:
            for target in ("key", "owner"):
                info = {
                    **self.info,
                    "models": invalid if target == "key" else expected,
                }
                owner = {
                    "user_info": {
                        "user_role": "internal_user",
                        "models": invalid if target == "owner" else expected,
                    }
                }
                with (
                    self.subTest(target=target, models=invalid),
                    patch.dict(os.environ, {"GATEWAY_KEY_MODELS": ",".join(expected)}),
                    patch.object(
                        gateway,
                        "control_request",
                        side_effect=[{"info": [info]}, owner],
                    ) as control,
                ):
                    with self.assertRaisesRegex(RuntimeError, "permissions"):
                        gateway.bootstrap(reuse_existing=True)
                    self.assertEqual(control.call_count, 1 if target == "key" else 2)
        self.assertEqual(
            self.output.read_text(), "PLATFORM_LITELLM_KEY=sk-test-runtime\n"
        )

    def test_reuse_rejects_expired_missing_unrestricted_or_exhausted_keys(self):
        self.save_existing()
        changes = [
            {"expires": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            {"expires": None},
            {"models": ["*"]},
            {"models": ["another-model"]},
            {"allowed_routes": ["llm_api_routes"]},
            {"allowed_routes": ["/chat/completions", "/v1/chat/completions"]},
            {
                "allowed_routes": [
                    "/responses",
                    "/v1/responses",
                    "/key/generate",
                ]
            },
            {"blocked": True},
            {"permissions": {"arbitrary": True}},
            {"max_budget": None},
            {"max_budget": 101},
            {"rpm_limit": 0},
            {"spend": 100},
            {"user_id": "admin"},
        ]
        for changeset in changes:
            with self.subTest(changes=changeset):
                record = {**copy.deepcopy(self.info), **changeset}
                with patch.object(
                    gateway, "control_request", return_value={"info": [record]}
                ) as control:
                    with self.assertRaises(RuntimeError):
                        gateway.bootstrap(reuse_existing=True)
                    self.assertEqual(control.call_count, 1)
                    self.assertEqual(control.call_args.args[2], "/v2/key/info")
        self.assertEqual(
            self.output.read_text(), "PLATFORM_LITELLM_KEY=sk-test-runtime\n"
        )

    def test_reuse_rejects_privileged_owner_and_does_not_create_key(self):
        self.save_existing()
        owner = {"user_info": {"user_role": "proxy_admin", "models": ["test-model"]}}
        with patch.object(
            gateway, "control_request", side_effect=[{"info": [self.info]}, owner]
        ) as control:
            with self.assertRaisesRegex(RuntimeError, "owner"):
                gateway.bootstrap(reuse_existing=True)
            self.assertEqual(control.call_count, 2)

    def test_reuse_rejects_master_and_missing_key_without_rotation(self):
        self.output.write_text("PLATFORM_LITELLM_KEY=sk-test-master\n")
        with patch.object(gateway, "control_request") as control:
            with self.assertRaisesRegex(RuntimeError, "master key"):
                gateway.bootstrap(reuse_existing=True)
            control.assert_not_called()
        self.save_existing()
        with patch.object(
            gateway, "control_request", return_value={"info": []}
        ) as control:
            with self.assertRaisesRegex(RuntimeError, "not found"):
                gateway.bootstrap(reuse_existing=True)
            self.assertEqual(control.call_count, 1)

    def test_invalid_limit_rejected_before_side_effects(self):
        with (
            patch.dict(os.environ, {"GATEWAY_KEY_MAX_BUDGET": "nan"}),
            patch.object(gateway, "control_request") as control,
        ):
            with self.assertRaisesRegex(ValueError, "finite positive"):
                gateway.bootstrap()
            control.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
