"""Disposable, zero-provider-I/O app for browser AG-UI integration checks.

Run with uvicorn agent_platform.tests.ag_ui_smoke_app:create_smoke_app --factory
on loopback only. All identities, money and runs live in a temporary database.
"""

import asyncio
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from agent_platform.apps.api.main import create_app
from agent_platform.infrastructure.config import Settings
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.fixtures import PASSWORD, MultiTenantFixture, migrate
from agent_platform.tests.generative_ui_fixture import COUNTER_UI
from agent_platform.tests.test_execution_lifecycle import OfflineControl


class SmokeGateway:
    async def stream(self, messages, _model, _max_tokens, _temperature, _tools):
        last = messages[-1]
        if last["role"] == "user" and "展示思考" in last["content"]:
            yield GatewayEvent("reasoning", {"text": "先核对输入条件。"})
            await asyncio.sleep(0.2)
            yield GatewayEvent("reasoning", {"text": "再验证计算结果。"})
            # Keep this phase open long enough to exercise refresh and cancel.
            await asyncio.sleep(0.4)
            if "等待取消" in last["content"]:
                await asyncio.Event().wait()
            yield GatewayEvent("text", {"text": "验证完成，结果为 300。"})
            yield GatewayEvent(
                "result", ModelReply("验证完成，结果为 300。", [], 20, 25, {})
            )
            return
        if last["role"] == "user" and any(
            word in last["content"] for word in ("界面", "计数器")
        ):
            yield GatewayEvent(
                "result",
                ModelReply(
                    "",
                    [
                        {
                            "id": "smoke-ui",
                            "name": "generate_sandboxed_ui",
                            "args": COUNTER_UI,
                        }
                    ],
                    20,
                    10,
                    {},
                ),
            )
            return
        if last["role"] == "tool" and "界面代码已传输" in last["content"]:
            yield GatewayEvent("text", {"text": "计数器界面已生成。"})
            yield GatewayEvent(
                "result", ModelReply("计数器界面已生成。", [], 20, 5, {})
            )
            return
        if last["role"] == "user" and "计算" in last["content"]:
            yield GatewayEvent(
                "result",
                ModelReply(
                    "",
                    [
                        {
                            "id": "smoke-calculator",
                            "name": "calculator",
                            "args": {"expression": "2+3"},
                        }
                    ],
                    20,
                    5,
                    {},
                ),
            )
            return
        waiting = last["role"] == "user" and "等待取消" in last["content"]
        text = "正在等待取消。" if waiting else "计算结果是 5。"
        yield GatewayEvent("text", {"text": text[:4]})
        await asyncio.sleep(0.2)
        yield GatewayEvent("text", {"text": text[4:]})
        if waiting:
            await asyncio.Event().wait()
        yield GatewayEvent("result", ModelReply(text, [], 20, 5, {}))


def create_smoke_app():
    directory = tempfile.TemporaryDirectory(prefix="ag-ui-browser-")
    settings = Settings(
        database_url=f"sqlite:///{Path(directory.name) / 'platform.db'}",
        admin_email="ag-ui-browser@example.test",
        admin_password=PASSWORD,
        secret_encryption_key=Fernet.generate_key().decode(),
        litellm_url="http://127.0.0.1:1",
        embedded_worker=True,
        worker_poll_seconds=0.05,
        run_timeout=60,
        export_directory=Path(directory.name) / "exports",
        tombstone_path=Path(directory.name) / "tombstones.jsonl",
    )
    migrate(settings.database_url)
    app = create_app(settings, gateway=SmokeGateway())
    app.state.smoke_directory = directory
    fixture = MultiTenantFixture(app.state.database, settings)
    platform = app.state.platform
    platform.gateway = GatewayService(
        app.state.database, settings, adapter=OfflineControl()
    )
    fixture.platform.gateway = platform.gateway
    model = fixture.create_model(alias="ag-ui-offline")
    platform.gateway.enroll(fixture.tenant_id, actor_id=fixture.owner["id"])
    while platform.gateway.process_one():
        pass
    agent = platform.save_agent(
        fixture.owner,
        {
            "name": "AG-UI 联调助手",
            "description": "免费的本地协议验证模型",
            "system_prompt": "Use the calculator when asked to calculate.",
            "model_id": model["id"],
            "temperature": 0,
            "max_steps": 3,
            "max_tokens": 64,
            "tools": ["calculator"],
        },
    )
    platform.publish(fixture.owner, agent["id"])
    platform.billing.credit(
        fixture.tenant_id,
        fixture.owner["id"],
        "10",
        "Disposable test balance",
        "smoke-credit",
    )
    return app
