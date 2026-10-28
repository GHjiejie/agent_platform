"""UI transport validation must never execute model-authored browser code."""

import unittest

from pydantic import ValidationError

from agent_platform.modules.runtime.generative_ui import UI_TOOL, GeneratedUiArguments
from agent_platform.modules.runtime.tools import execute_tool, tool_contract
from agent_platform.tests.generative_ui_fixture import COUNTER_UI


class GeneratedUiTests(unittest.TestCase):
    def test_transport_is_authorized_bounded_and_has_a_closed_contract(self):
        code = {**COUNTER_UI, "jsFunctions": "throw new Error('never run on server')"}
        self.assertIn("message", execute_tool(UI_TOOL, code, [UI_TOOL]))
        with self.assertRaises(ValueError):
            execute_tool(UI_TOOL, code, [])
        contract = tool_contract(UI_TOOL)
        self.assertEqual(contract.effect, "pure")
        self.assertFalse(contract.input_schema["additionalProperties"])
        self.assertFalse(contract.output_schema["additionalProperties"])
        for changes in (
            {"html": " "},
            {"html": "x" * 96001},
            {"jsExpressions": []},
            {"jsExpressions": [" "]},
            {"initialHeight": True},
            {"unknown": "value"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                GeneratedUiArguments.model_validate({**COUNTER_UI, **changes})
        with self.assertRaises(ValidationError):
            GeneratedUiArguments.model_validate({**COUNTER_UI, "html": "界" * 70000})
