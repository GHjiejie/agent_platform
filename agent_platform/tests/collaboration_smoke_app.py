"""Disposable API/Worker for a real browser team run, with zero provider traffic."""

import asyncio

from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.ag_ui_smoke_app import create_smoke_app
from agent_platform.tests.generative_ui_fixture import COUNTER_UI


class TeamSmokeGateway:
    async def stream(self, messages, _model, _max_tokens, _temperature, tools):
        # Independent deterministic responses selected by explicit task envelopes;
        # no deployment credentials, external model requests or production data.
        await asyncio.sleep(0.5)
        if (
            any(tool["function"]["name"] == "generate_sandboxed_ui" for tool in tools)
            and messages[-1]["role"] != "tool"
        ):
            reply = ModelReply(
                "",
                [{"id": "chart", "name": "generate_sandboxed_ui", "args": COUNTER_UI}],
                20,
                10,
                {},
            )
        elif "请根据以下已确认任务成果" in messages[-1]["content"]:
            reply = ModelReply("协作验收完成：A=10，B=20。", [], 20, 10, {})
        else:
            reply = ModelReply('{"facts":["A=10","B=20"],"missing":[]}', [], 20, 10, {})
        yield GatewayEvent("reasoning", {"text": "仅在成员内部展示的推理。"})
        if reply.content:
            yield GatewayEvent("text", {"text": reply.content})
        yield GatewayEvent("result", reply)


def create_team_smoke_app():
    return create_smoke_app(TeamSmokeGateway(), collaboration=True)
