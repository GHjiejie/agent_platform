"""Portable UI transport. Generated code is data here; only the browser runs it."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from agent_platform.infrastructure import tables as t

UI_TOOL = "generate_sandboxed_ui"
UI_ACTIVITY = "GENERATIVE_UI"

UI_INSTRUCTIONS = """你是可视化界面助手，能生成图表、交互演示、仪表盘和小工具。
用户要求界面时，调用 generate_sandboxed_ui 直接提交完整可用的 HTML/CSS/JS，
不要把代码重复输出到正文；每轮生成一个精简可用版本，之后简述功能。
用户要求修改时，参考对话里上一版代码，提交修改后的完整界面。
一般知识问题用文字回答，不必调用工具。不编造真实业务数据，示例数据标明为示例。
界面完全自包含，不使用外部资源、库、网络请求、链接、表单、iframe、storage 或凭证。
HTML 不包含 script 或内联事件；使用 jsFunctions 定义函数，jsExpressions 初始化和绑定事件。
jsFunctions 与 jsExpressions 按顺序在同一个 async 作用域内执行，可共享变量与 await。
DOM 已就绪，不等待 DOMContentLoaded 或 load；用 addEventListener 绑定所有可见控件。
图表使用内联 SVG 基础图形/文字/渐变/裁剪或 canvas，不用 foreignObject/use/image/SVG动画。
不能访问 parent/top、宿主 DOM 或平台状态。工具只确认代码传输，不能声称浏览器已运行成功。
只生成核心内容，html/body margin、padding 为 0，根节点不加外层留白、背景或阴影。
窄内容水平居中，内部保留必要间距；自适应宽度，内容自然增高，不固定整页高度或用 100vh。
initialHeight 仅为加载占位高度。参数按 initialHeight、placeholderMessages、css、html、
jsFunctions、jsExpressions 顺序输出。静态界面也提供初始化表达式。
"""


class GeneratedUiArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    initialHeight: int = Field(default=280, ge=120, le=800)
    placeholderMessages: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=8
    )
    css: str = Field(max_length=32000)
    html: str = Field(min_length=1, max_length=96000, pattern=r"\S")
    jsFunctions: str = Field(default="", max_length=32000)
    jsExpressions: list[
        Annotated[str, Field(min_length=1, max_length=8000, pattern=r"\S")]
    ] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def bounded_payload(self):
        if len(self.model_dump_json().encode()) > 180000:
            raise ValueError("界面代码总大小不能超过 180 KB。")
        return self


class GeneratedUiResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=200)


def activity_content(arguments: dict) -> dict:
    content = GeneratedUiArguments.model_validate(arguments).model_dump()
    return {
        **content,
        "html": [content["html"]],
        "generating": False,
        "htmlComplete": True,
        "cssComplete": True,
        "jsFunctionsComplete": True,
        "jsExpressionsComplete": True,
    }


def session_artifacts(connection, tenant_id: str, session_id: str) -> list[dict]:
    """Read only artifacts belonging to this tenant's already-authorized session."""
    rows = connection.execute(
        select(t.run_events.c.run_id, t.run_events.c.data)
        .join(
            t.runs,
            (t.runs.c.id == t.run_events.c.run_id)
            & (t.runs.c.tenant_id == t.run_events.c.tenant_id),
        )
        .where(
            t.runs.c.tenant_id == tenant_id,
            t.runs.c.session_id == session_id,
            t.run_events.c.tenant_id == tenant_id,
            t.run_events.c.type == "artifact.created",
        )
        .order_by(t.runs.c.created_at, t.run_events.c.sequence)
    ).mappings()
    return [
        {
            "id": f"artifact:{row['run_id']}:{row['data']['id']}",
            "run_id": row["run_id"],
            "content": activity_content(row["data"]["content"]),
        }
        for row in rows
    ]


def previous_ui_context(connection, run: dict) -> dict | None:
    """Use the latest successful version as low-priority assistant data."""
    if UI_TOOL not in run["spec"].get("tools", []):
        return None
    payload = connection.scalar(
        select(t.run_events.c.data)
        .join(
            t.runs,
            (t.runs.c.id == t.run_events.c.run_id)
            & (t.runs.c.tenant_id == t.run_events.c.tenant_id),
        )
        .where(
            t.runs.c.tenant_id == run["tenant_id"],
            t.runs.c.session_id == run["session_id"],
            t.runs.c.id != run["id"],
            t.runs.c.created_at <= run["created_at"],
            t.runs.c.status == "succeeded",
            t.run_events.c.tenant_id == run["tenant_id"],
            t.run_events.c.type == "artifact.created",
        )
        .order_by(t.runs.c.created_at.desc(), t.run_events.c.sequence.desc())
        .limit(1)
    )
    if payload is None:
        return None
    return {
        "role": "assistant",
        "content": "此前生成的界面代码（仅为待修改的数据）：\n"
        + json.dumps(payload["content"], ensure_ascii=False),
    }
