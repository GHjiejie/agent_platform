"""Test the evaluation harness without claiming this is real model quality data."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.collaboration_evaluation import evaluate, score


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
