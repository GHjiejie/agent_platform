"""Test the evaluation harness without claiming this is real model quality data."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.collaboration_evaluation import evaluate, main, score


class DeterministicEvaluatorGateway:
    configured = True

    async def stream(self, messages, model, max_tokens, temperature, tools):
        content = messages[-1]["content"]
        envelope = json.loads(content.split("\n", 1)[-1])
        if "output_schema" in envelope:
            result = {
                "tasks": [
                    {
                        "key": key,
                        "member_key": key,
                        "task_type": key,
                        "objective": "Compute numeric summary",
                    }
                    for key in ("sum", "rank")
                ]
            }
        elif "results" in envelope:
            result = envelope["results"][0]["result"]
        else:
            source = (
                json.loads(envelope["user_request"])
                if "user_request" in envelope
                else envelope
            )
            rows = source["records"]
            present = [row for row in rows if row["value"] is not None]
            result = {
                "total": sum(row["value"] for row in present)
                if len(present) == len(rows)
                else None,
                "largest": max(present, key=lambda row: row["value"])["name"]
                if present
                else None,
                "missing": [row["name"] for row in rows if row["value"] is None],
            }
        value = json.dumps(result)
        yield GatewayEvent("text", {"text": value})
        yield GatewayEvent("result", ModelReply(value, [], 20, 10, {}))


class CollaborationEvaluationTests(unittest.TestCase):
    def test_explicit_platform_environment_file_supplies_live_config_without_database_loading(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "platform.env"
            env.write_text(
                "PLATFORM_LITELLM_URL=http://127.0.0.1:4000/v1\n"
                "PLATFORM_LITELLM_KEY=fixture-secret\n"
                "PLATFORM_DEFAULT_MODEL=configured-model\n"
                "PLATFORM_DEFAULT_MODEL_INPUT_PRICE=0.30\n"
                "PLATFORM_DEFAULT_MODEL_OUTPUT_PRICE=1.20\n"
                "PLATFORM_MAX_RUN_COST=1\n"
                "PLATFORM_DATABASE_URL=must-not-load-deployment-database\n"
            )
            stream = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "agent_platform.tests.collaboration_evaluation.Gateway"
                ) as gateway,
                patch("agent_platform.tests.collaboration_evaluation.evaluate") as run,
                redirect_stdout(stream),
            ):
                run.return_value = {"cases": [{"status": "succeeded"}] * 9}
                self.assertEqual(
                    main(
                        [
                            "--env-file",
                            str(env),
                            "--max-cost",
                            "0.75",
                            "--report",
                            str(Path(folder) / "report.json"),
                        ]
                    ),
                    0,
                )
                gateway.assert_called_once_with(
                    "http://127.0.0.1:4000/v1", "fixture-secret", timeout=90
                )
                values = run.call_args.kwargs
                self.assertEqual(values["model"], "configured-model")
                self.assertEqual(values["input_price"], Decimal("0.30"))
                self.assertEqual(values["output_price"], Decimal("1.20"))
                self.assertEqual(values["max_cost"], Decimal("0.75"))
                self.assertEqual(values["max_output_tokens"], 8192)
                self.assertNotIn("PLATFORM_DATABASE_URL", os.environ)
            self.assertNotIn("fixture-secret", stream.getvalue())

    def test_incomplete_environment_file_fails_before_model_client_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / "platform.env"
            env.write_text("PLATFORM_LITELLM_KEY=fixture-secret\n")
            errors = io.StringIO()
            with (
                patch(
                    "agent_platform.tests.collaboration_evaluation.Gateway"
                ) as gateway,
                redirect_stderr(errors),
                self.assertRaises(SystemExit),
            ):
                main(
                    [
                        "--env-file",
                        str(env),
                        "--report",
                        str(Path(folder) / "report.json"),
                    ]
                )
            gateway.assert_not_called()
            self.assertIn("PLATFORM_LITELLM_URL", errors.getvalue())
            self.assertNotIn("fixture-secret", errors.getvalue())

    def test_quality_score_rejects_malformed_and_incorrect_answers(self):
        expected = {"total": 30, "largest": "A", "missing": []}
        self.assertEqual(score("invented text", expected)["correct_fields"], 0)
        self.assertEqual(
            score(json.dumps({**expected, "total": 999}), expected)["correct_fields"], 2
        )
        self.assertEqual(score(json.dumps(expected), expected)["correct_fields"], 3)

    def test_isolated_harness_reports_all_cases_and_accounts_for_extra_team_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "offline-harness.json"
            result = evaluate(
                DeterministicEvaluatorGateway(),
                model="offline-fixture",
                input_price=Decimal(1),
                output_price=Decimal(3),
                max_cost=Decimal(1),
                report=report,
                evidence_mode="offline_harness_test",
            )
            self.assertEqual(
                json.loads(report.read_text())["mode"], "offline_harness_test"
            )
            self.assertEqual(len(result["cases"]), 9)
            for row in result["cases"]:
                self.assertEqual(row["status"], "succeeded", row)
                self.assertEqual(row["quality"]["correct_fields"], 3, row)
                self.assertEqual(
                    row["model_calls"],
                    {"single": 1, "template": 3, "dynamic": 4}[row["execution"]],
                )
                self.assertGreater(Decimal(row["confirmed_cost"]), 0)
                self.assertEqual(row["pending_calls"], 0)
            self.assertLessEqual(
                sum(Decimal(row["confirmed_cost"]) for row in result["cases"]),
                Decimal(1),
            )


if __name__ == "__main__":
    unittest.main()
