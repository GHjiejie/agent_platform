"""Small, schema-validated tools without code execution or network access."""

from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ToolContract
from .generative_ui import UI_TOOL, GeneratedUiArguments, GeneratedUiResult


class CalculatorArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str = Field(min_length=1, max_length=256)


class CurrentTimeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


class CalculatorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    result: int | float


class CurrentTimeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    time: str = Field(min_length=1, max_length=64)
    timezone: str = Field(min_length=1, max_length=64)


_TOOL_CONTRACTS = {
    UI_TOOL: ToolContract(
        name=UI_TOOL,
        implementation_version=1,
        effect="pure",
        replay="recompute",
        input_schema=GeneratedUiArguments.model_json_schema(),
        output_schema=GeneratedUiResult.model_json_schema(),
    ),
    "calculator": ToolContract(
        name="calculator",
        implementation_version=1,
        effect="pure",
        replay="recompute",
        input_schema=CalculatorArguments.model_json_schema(),
        output_schema=CalculatorResult.model_json_schema(),
    ),
    # Time is an observation, even though it performs no network or write I/O.
    # A recovered conversation must reuse its recorded value, not observe again.
    "current_time": ToolContract(
        name="current_time",
        implementation_version=1,
        effect="observation",
        replay="recorded_result",
        input_schema=CurrentTimeArguments.model_json_schema(),
        output_schema=CurrentTimeResult.model_json_schema(),
    ),
}


def tool_contract(name: str) -> ToolContract:
    """Return an isolated, serializable versioned declaration for a known tool."""
    if name not in _TOOL_CONTRACTS:
        raise ValueError("工具未获此 Agent 授权。")
    return ToolContract.model_validate_json(_TOOL_CONTRACTS[name].model_dump_json())


_ARGUMENTS = {
    UI_TOOL: GeneratedUiArguments,
    "calculator": CalculatorArguments,
    "current_time": CurrentTimeArguments,
}
_DESCRIPTIONS = {
    UI_TOOL: (
        "传输自包含的 HTML/CSS/JS 交互界面到浏览器沙箱。HTML 不含 script、内联事件或外部资源；"
        "jsFunctions 定义函数，jsExpressions 在 DOM 已就绪后按顺序初始化并绑定事件，"
        "共享 async 作用域，不等待 load。图表使用内联 SVG 或 canvas。"
        "禁止网络、外部库、storage、凭证及 parent/top 访问。工具仅确认传输。"
    ),
    "calculator": "计算基本四则运算，允许括号、小数、余数和绝对值不超过 12 的指数。",
    "current_time": "返回给定 IANA 时区（如 Asia/Shanghai）的当前 ISO 8601 时间。",
}
_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def tool_definitions(names: list[str]) -> list[dict]:
    unknown = set(names) - _ARGUMENTS.keys()
    if unknown:
        raise ValueError("Agent 包含未授权工具。")
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _DESCRIPTIONS[name],
                "parameters": tool_contract(name).input_schema,
            },
        }
        for name in dict.fromkeys(names)
    ]


def calculate(expression: str) -> int | float:
    """Evaluate a bounded arithmetic AST, never names, calls or attributes."""
    if len(expression) > 256:
        raise ValueError("算式过长。")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("算式语法无效。") from exc
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("算式过于复杂。")

    def bounded(value: Any) -> int | float:
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("只支持有限实数。")
        if abs(value) > 1e100:
            raise ValueError("计算结果超出范围。")
        return value

    def visit(node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant):
            return bounded(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return bounded(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("指数绝对值不能超过 12。")
            try:
                return bounded(_BINARY[type(node.op)](left, right))
            except ArithmeticError as exc:
                raise ValueError("该运算没有有效的有限实数结果。") from exc
        raise ValueError("算式只允许数字、括号和基本算术运算符。")

    return visit(tree.body)


def execute_tool(name: str, arguments: dict, allowed: list[str]) -> dict:
    if name not in allowed or name not in _ARGUMENTS:
        raise ValueError("工具未获此 Agent 授权。")
    params = _ARGUMENTS[name].model_validate(arguments)
    if name == UI_TOOL:
        return {"message": "界面代码已传输，预览和交互就绪由浏览器独立确认。"}
    if name == "calculator":
        return {"result": calculate(params.expression)}
    try:
        timezone = ZoneInfo(params.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("未知 IANA 时区。") from exc
    return {"time": datetime.now(timezone).isoformat(), "timezone": params.timezone}
