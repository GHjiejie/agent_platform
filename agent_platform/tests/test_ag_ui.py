"""AG-UI wire contracts against isolated SQL, HTTP and a real offline worker."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from ag_ui.core import Event
from cryptography.fernet import Fernet
from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, update

from agent_platform.apps.api.main import COOKIE, create_app
from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules import ag_ui
from agent_platform.modules.gateway_control import GatewayService
from agent_platform.modules.run_records import append_event, now, uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests.fixtures import PASSWORD, MultiTenantFixture, migrate
from agent_platform.tests.generative_ui_fixture import COUNTER_UI
from agent_platform.tests.test_event_notifications import RequestFixture, eventually
from agent_platform.tests.test_execution_lifecycle import (
    OfflineControl,
    OfflineInference,
)


def frames(text):
    result = []
    for block in text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if not data:
            continue
        value = json.loads("\n".join(data))
        TypeAdapter(Event).validate_python(value)
        cursor = next(
            (line[4:] for line in block.splitlines() if line.startswith("id: ")), None
        )
        result.append((cursor, value))
    return result


class ToolInference:
    def __init__(self):
        self.calls = 0

    async def stream(self, messages, _model, _max_tokens, _temperature, _tools):
        self.calls += 1
        if self.calls == 1:
            reply = ModelReply(
                "",
                [{"id": "tool-1", "name": "calculator", "args": {"expression": "2+3"}}],
                10,
                5,
                {"private": "not-for-ui"},
            )
        else:
            assert messages[-1]["tool_call_id"] == "tool-1"
            yield GatewayEvent("text", {"text": "结果是 5。"})
            reply = ModelReply("结果是 5。", [], 15, 5, {"private": "not-for-ui"})
        yield GatewayEvent("result", reply)


class UiInference:
    def __init__(self, arguments=None):
        self.arguments = COUNTER_UI if arguments is None else arguments
        self.messages = []

    async def stream(self, messages, _model, _max_tokens, _temperature, _tools):
        self.messages.append(messages)
        if messages[-1]["role"] == "user":
            reply = ModelReply(
                "",
                [
                    {
                        "id": "ui-1",
                        "name": "generate_sandboxed_ui",
                        "args": self.arguments,
                    }
                ],
                20,
                10,
                {},
            )
        else:
            yield GatewayEvent("text", {"text": "已生成计数器。"})
            reply = ModelReply("已生成计数器。", [], 20, 5, {})
        yield GatewayEvent("result", reply)


class ReasoningInference:
    text = "核对输入条件。" * 20 + "已确认计算方法。"

    async def stream(self, messages, _model, _max_tokens, _temperature, _tools):
        yield GatewayEvent("reasoning", {"text": self.text[:140]})
        yield GatewayEvent("reasoning", {"text": self.text[140:]})
        yield GatewayEvent("text", {"text": "结果是 5。"})
        yield GatewayEvent("result", ModelReply("结果是 5。", [], 15, 25, {}))


class AgUIContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ag-ui-contract-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(
            database_url=f"sqlite:///{Path(self.temp.name) / 'platform.db'}",
            admin_email="owner@ag-ui.test",
            admin_password=PASSWORD,
            secret_encryption_key=Fernet.generate_key().decode(),
            litellm_url="http://127.0.0.1:1",
            run_timeout=10,
            export_directory=Path(self.temp.name) / "exports",
            web_directory=Path(self.temp.name) / "web",
        )
        migrate(self.settings.database_url)
        self.app = create_app(self.settings)
        self.db = self.app.state.database
        self.addCleanup(self.db.close)
        self.fixture = MultiTenantFixture(self.db, self.settings)
        self.platform = self.app.state.platform
        self.platform.gateway = GatewayService(
            self.db, self.settings, adapter=OfflineControl()
        )
        self.fixture.platform.gateway = self.platform.gateway
        self.user, self.tenant = self.fixture.owner, self.fixture.tenant_id
        self.model = self.fixture.create_model()
        self.platform.gateway.enroll(self.tenant, actor_id=self.user["id"])
        while self.platform.gateway.process_one():
            pass
        self.agent = self.platform.save_agent(
            self.user,
            {
                "name": "AG-UI offline agent",
                "system_prompt": "Answer briefly.",
                "description": "Protocol contract fixture",
                "model_id": self.model["id"],
                "temperature": 0,
                "max_steps": 3,
                "max_tokens": 64,
                "tools": ["calculator"],
            },
        )
        self.platform.publish(self.user, self.agent["id"])
        self.session = self.platform.create_session(self.user, self.agent["id"], None)
        self.platform.billing.credit(
            self.tenant, self.user["id"], "10", "Offline test funds", uid()
        )
        self.login = self.platform.login(self.settings.admin_email, PASSWORD, "offline")
        self.headers = {"X-CSRF-Token": self.login["csrf_token"]}
        self.body = {
            "threadId": self.session["id"],
            "runId": "client-run-1",
            "protocolVersion": "1.0",
            "messages": [
                {
                    "id": "client-message-1",
                    "role": "user",
                    "content": "Offline question",
                }
            ],
            "state": {},
            "tools": [],
            "context": [],
            "forwardedProps": {"modelId": self.model["id"]},
        }

    def client(self, token=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://isolated",
            cookies={COOKIE: token or self.login["token"]},
        )

    def path(self, resource):
        return f"/api/v2/tenants/{self.tenant}{resource}"

    def count(self, table):
        with self.db.read() as connection:
            return connection.scalar(select(func.count()).select_from(table))

    def protocol_run(self):
        run = ag_ui.enqueue(
            self.platform,
            self.user,
            self.session["id"],
            ag_ui.PlatformRunInput.model_validate(self.body),
        )
        self.platform.update_stream(self.user, run.pop("_stream_id"), release=True)
        return run

    def complete(
        self, run, *, content="Completed fixture", status="succeeded", error=""
    ):
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(status=status, finished_at=now(), error=error)
            )
            append_event(
                connection,
                run["id"],
                "run.completed" if status == "succeeded" else f"run.{status}",
                {"status": status, "content": content, "error": error},
            )

    async def execute_request(self, gateway):
        worker = Worker(self.platform, gateway=gateway)
        async with self.client() as client:
            request = asyncio.create_task(
                client.post(
                    self.path(f"/sessions/{self.session['id']}/ag-ui"),
                    json=self.body,
                    headers=self.headers,
                )
            )
            claimed = None
            until = time.monotonic() + 3
            while claimed is None and time.monotonic() < until:
                claimed = await asyncio.to_thread(worker.claim)
                if claimed is None:
                    if request.done():
                        response = await request
                        self.fail(
                            f"Run rejected: {response.status_code} {response.text}"
                        )
                    await asyncio.sleep(0.01)
            self.assertIsNotNone(claimed)
            await worker.execute(claimed)
            response = await asyncio.wait_for(request, 3)
        self.assertEqual(response.status_code, 200, response.text)
        return claimed, response, frames(response.text)

    async def test_composer_reasoning_override_is_frozen_and_does_not_edit_agent(self):
        self.platform.save_agent(
            self.user,
            {"execution_policy": {"reasoning_effort": "high"}},
            self.agent["id"],
        )
        self.platform.publish(self.user, self.agent["id"])
        self.body["forwardedProps"]["reasoningEffort"] = "low"
        run = self.protocol_run()
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == run["id"])
            )
            self.assertEqual(spec["execution_policy"]["reasoning_effort"], "low")
            inference, _ = Worker(self.platform).invocation._resolve_inference(
                {"tenant_id": self.tenant, "spec": spec}
            )
            self.assertEqual(inference.reasoning_effort, "low")
            published = connection.scalar(
                select(t.agent_versions.c.spec).where(
                    t.agent_versions.c.agent_id == self.agent["id"],
                    t.agent_versions.c.version == 2,
                )
            )
            self.assertEqual(published["execution_policy"]["reasoning_effort"], "high")
        self.assertEqual(self.protocol_run()["id"], run["id"])
        self.body["forwardedProps"]["reasoningEffort"] = "medium"
        with self.assertRaises(PlatformError) as caught:
            self.protocol_run()
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.complete(run)
        self.body["runId"] = "inherit-reasoning"
        self.body["messages"][0]["id"] = "inherit-reasoning-message"
        self.body["forwardedProps"]["reasoningEffort"] = None
        inherited = self.protocol_run()
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == inherited["id"])
            )
            self.assertEqual(spec["execution_policy"]["reasoning_effort"], "high")

    async def test_readable_reasoning_streams_persists_and_resumes_at_each_phase(self):
        run, response, events = await self.execute_request(ReasoningInference())
        reasoning = [
            (cursor, event)
            for cursor, event in events
            if event["type"].startswith("REASONING_")
        ]
        self.assertEqual(
            [event["type"] for _, event in reasoning],
            [
                "REASONING_START",
                "REASONING_MESSAGE_START",
                "REASONING_MESSAGE_CONTENT",
                "REASONING_MESSAGE_CONTENT",
                "REASONING_MESSAGE_END",
                "REASONING_END",
            ],
        )
        identifier = reasoning[0][1]["messageId"]
        self.assertTrue(identifier.startswith(f"reasoning:{run['id']}:"))
        self.assertEqual(
            "".join(event["delta"] for _, event in reasoning if "delta" in event),
            ReasoningInference.text,
        )
        types = [event["type"] for _, event in events]
        self.assertLess(types.index("REASONING_END"), types.index("TEXT_MESSAGE_START"))
        final = next(
            event
            for _, event in reversed(events)
            if event["type"] == "MESSAGES_SNAPSHOT"
        )
        self.assertEqual(
            [
                message["content"]
                for message in final["messages"]
                if message["role"] == "reasoning"
            ],
            [ReasoningInference.text],
        )
        async with self.client() as client:
            detail = (
                await client.get(self.path(f"/sessions/{self.session['id']}"))
            ).json()
            self.assertEqual(len(detail["reasoning"]), 1)
            self.assertEqual(detail["reasoning"][0]["id"], identifier)
            self.assertEqual(detail["reasoning"][0]["status"], "complete")
            self.assertEqual(detail["reasoning"][0]["content"], ReasoningInference.text)
            for cursor, _ in reasoning:
                resumed = frames(
                    (
                        await client.get(
                            self.path(f"/runs/{run['id']}/ag-ui"),
                            headers={"Last-Event-ID": cursor},
                        )
                    ).text
                )
                self.assertEqual(
                    [key for key, _ in resumed if key],
                    [
                        key
                        for key, _ in events
                        if key and ag_ui.parse_cursor(key) > ag_ui.parse_cursor(cursor)
                    ],
                )
                snapshot = next(
                    event
                    for _, event in reversed(resumed)
                    if event["type"] == "MESSAGES_SNAPSHOT"
                )
                traces = [
                    message
                    for message in snapshot["messages"]
                    if message["role"] == "reasoning"
                ]
                self.assertEqual(len(traces), 1)
                self.assertEqual(traces[0]["content"], ReasoningInference.text)
        self.body["runId"] = "client-run-2"
        self.body["messages"] = [
            {"id": "client-message-2", "role": "user", "content": "Follow up"}
        ]
        next_run = self.protocol_run()
        projection = ag_ui.read_projection(self.platform, self.user, next_run["id"])
        self.assertEqual(
            [
                message["content"]
                for message in projection.messages
                if message["role"] == "reasoning"
            ],
            [ReasoningInference.text],
        )
        self.assertTrue(
            all(
                message["metadata"]["platform"]["run_id"]
                for message in projection.messages
                if message["role"] in {"user", "assistant"}
            )
        )
        self.assertNotIn("encrypted_content", response.text)

    async def test_cancelled_reasoning_is_closed_and_only_its_owner_can_read_it(self):
        from agent_platform.modules.reasoning import session_reasoning

        run = self.protocol_run()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            for kind, data in [
                ("reasoning.started", {"call_id": "call-1"}),
                (
                    "reasoning.delta",
                    {"call_id": "call-1", "text": "已收到的推理片段。"},
                ),
            ]:
                append_event(connection, run["id"], kind, data)
        self.complete(run, status="cancelled")
        async with self.client() as client:
            replay = frames(
                (await client.get(self.path(f"/runs/{run['id']}/ag-ui"))).text
            )
            detail = (
                await client.get(self.path(f"/sessions/{self.session['id']}"))
            ).json()
        end = next(event for _, event in replay if event["type"] == "REASONING_END")
        self.assertEqual(end["metadata"]["platform"]["reasoning_status"], "interrupted")
        self.assertEqual(detail["reasoning"][0]["status"], "interrupted")
        self.assertEqual(detail["reasoning"][0]["content"], "已收到的推理片段。")
        with self.db.read() as connection:
            self.assertEqual(
                session_reasoning(
                    connection,
                    {**self.user, "id": "different-user"},
                    self.session["id"],
                ),
                [],
            )
            self.assertEqual(
                session_reasoning(
                    connection,
                    {**self.user, "tenant_id": self.fixture.tenant_b},
                    self.session["id"],
                ),
                [],
            )

    async def test_post_runs_real_worker_and_emits_official_events(self):
        gateway = OfflineInference()
        run, response, events = await self.execute_request(gateway)
        self.assertEqual(response.headers["x-run-id"], run["id"])
        self.assertEqual(response.headers["x-ag-ui-protocol-version"], "1.0")
        self.assertNotIn("event: ", response.text)
        self.assertEqual(events[0][1]["type"], "RUN_STARTED")
        self.assertEqual(events[0][1]["runId"], self.body["runId"])
        types = [event["type"] for _, event in events]
        self.assertLess(
            types.index("TEXT_MESSAGE_START"), types.index("TEXT_MESSAGE_CONTENT")
        )
        self.assertLess(
            types.index("TEXT_MESSAGE_CONTENT"), types.index("TEXT_MESSAGE_END")
        )
        self.assertEqual(types[-1], "RUN_FINISHED")
        self.assertEqual(events[-1][1]["outcome"], {"type": "success"})
        snapshots = [
            event for _, event in events if event["type"] == "MESSAGES_SNAPSHOT"
        ]
        self.assertEqual(snapshots[0]["messages"][-1]["id"], "client-message-1")
        self.assertEqual(snapshots[-1]["messages"][-1]["content"], "Offline answer")
        self.assertEqual(self.count(rt.stream_leases), 0)
        self.assertEqual(self.count(t.calls), 1)
        async with self.client() as client:
            duplicate = await client.post(
                self.path(f"/sessions/{self.session['id']}/ag-ui"),
                json=self.body,
                headers=self.headers,
            )
            conflict = await client.post(
                self.path(f"/sessions/{self.session['id']}/ag-ui"),
                json={
                    **self.body,
                    "messages": [
                        {
                            "id": "client-message-1",
                            "role": "user",
                            "content": "Changed request",
                        }
                    ],
                },
                headers=self.headers,
            )
        self.assertEqual(duplicate.headers["x-run-id"], run["id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.count(t.runs), 1)
        self.assertEqual(self.count(t.messages), 2)
        self.assertEqual(gateway.calls, 1)

    async def test_generated_ui_is_durable_replayable_and_revisable(self):
        self.platform.save_agent(
            self.user, {"tools": ["generate_sandboxed_ui"]}, self.agent["id"]
        )
        self.platform.publish(self.user, self.agent["id"])
        gateway = UiInference()
        run, _, events = await self.execute_request(gateway)
        activities = [
            (cursor, event)
            for cursor, event in events
            if event["type"] == "ACTIVITY_SNAPSHOT"
        ]
        self.assertEqual(len(activities), 1)
        cursor, activity = activities[0]
        self.assertEqual(activity["activityType"], "GENERATIVE_UI")
        self.assertEqual(activity["content"]["html"], [COUNTER_UI["html"]])
        self.assertFalse(activity["content"]["generating"])
        async with self.client() as client:
            detail = (
                await client.get(self.path(f"/sessions/{self.session['id']}"))
            ).json()
            replay = await client.get(
                self.path(f"/runs/{run['id']}/ag-ui"), headers={"Last-Event-ID": cursor}
            )
        self.assertEqual(detail["artifacts"][0]["id"], activity["messageId"])
        self.assertEqual(detail["artifacts"][0]["run_id"], run["id"])
        self.assertTrue(
            all(message["run_id"] == run["id"] for message in detail["messages"])
        )
        snapshots = [
            event
            for _, event in frames(replay.text)
            if event["type"] == "MESSAGES_SNAPSHOT"
        ]
        for snapshot in snapshots:
            self.assertEqual(
                sum(message["role"] == "activity" for message in snapshot["messages"]),
                1,
            )
        self.body["runId"] = "client-run-2"
        self.body["messages"] = [
            {"id": "client-message-2", "role": "user", "content": "把按钮改成蓝色"}
        ]
        await self.execute_request(gateway)
        followup = gateway.messages[2]
        self.assertEqual(followup[-1]["content"], "把按钮改成蓝色")
        self.assertEqual(followup[-2]["role"], "assistant")
        self.assertEqual(
            json.loads(followup[-2]["content"].split("\n", 1)[1])["html"],
            COUNTER_UI["html"],
        )
        from agent_platform.modules.runtime.generative_ui import (
            previous_ui_context,
            session_artifacts,
        )

        with self.db.read() as connection:
            self.assertEqual(
                session_artifacts(
                    connection, self.fixture.tenant_b, self.session["id"]
                ),
                [],
            )
            self.assertIsNone(
                previous_ui_context(
                    connection,
                    {
                        **run,
                        "tenant_id": self.fixture.tenant_b,
                        "spec": {"tools": ["generate_sandboxed_ui"]},
                    },
                )
            )

    async def test_invalid_or_unauthorized_ui_does_not_emit_an_artifact(self):
        for tools, arguments in [
            ([], COUNTER_UI),
            (["generate_sandboxed_ui"], {**COUNTER_UI, "jsExpressions": []}),
        ]:
            self.platform.save_agent(self.user, {"tools": tools}, self.agent["id"])
            self.platform.publish(self.user, self.agent["id"])
            self.body["runId"] = uid()
            self.body["messages"] = [
                {"id": uid(), "role": "user", "content": "生成界面"}
            ]
            _, _, events = await self.execute_request(UiInference(arguments))
            self.assertFalse(
                any(event["type"] == "ACTIVITY_SNAPSHOT" for _, event in events)
            )
            results = [
                json.loads(event["content"])
                for _, event in events
                if event["type"] == "TOOL_CALL_RESULT"
            ]
            self.assertFalse(results[0]["ok"])

    async def test_tools_have_start_args_end_result_and_stable_ids(self):
        gateway = ToolInference()
        _, response, frames_ = await self.execute_request(gateway)
        events = [event for _, event in frames_]
        tools = [event for event in events if event["type"].startswith("TOOL_CALL_")]
        self.assertEqual(
            [event["type"] for event in tools],
            ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"],
        )
        identifiers = {event["toolCallId"] for event in tools}
        self.assertEqual(len(identifiers), 1)
        self.assertTrue(next(iter(identifiers)).endswith(":tool-1"))
        self.assertEqual(json.loads(tools[1]["delta"]), {"expression": "2+3"})
        self.assertEqual(json.loads(tools[-1]["content"]), {"ok": True, "result": 5})
        self.assertEqual(gateway.calls, 2)
        self.assertNotIn("not-for-ui", response.text)
        self.assertNotIn("system_prompt", response.text)
        self.assertNotIn("provider_cost", response.text)

    async def test_composite_cursor_resumes_inside_a_tool_row_without_dropping_parts(
        self,
    ):
        run = self.protocol_run()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            append_event(
                connection,
                run["id"],
                "model.started",
                {"call_id": "call-1", "model": "offline-model"},
            )
            append_event(
                connection,
                run["id"],
                "message.delta",
                {"call_id": "call-1", "text": "你好"},
            )
            row = append_event(
                connection,
                run["id"],
                "tool.started",
                {"id": "tool-1", "name": "calculator", "args": {"expression": "2+3"}},
            )
            append_event(
                connection,
                run["id"],
                "tool.finished",
                {"id": "tool-1", "name": "calculator", "ok": True, "result": 5},
            )
        self.complete(run)
        async with self.client() as client:
            full = frames(
                (await client.get(self.path(f"/runs/{run['id']}/ag-ui"))).text
            )
            # close-text is part 0, tool start part 1: resume before its args.
            cursor = f"{row['sequence']}.1"
            resumed = await client.get(
                self.path(f"/runs/{run['id']}/ag-ui?after={row['sequence']}.0"),
                headers={"Last-Event-ID": cursor},
            )
        replayed = frames(resumed.text)
        self.assertEqual(
            [identifier for identifier, _ in replayed if identifier],
            [
                identifier
                for identifier, _ in full
                if identifier
                and ag_ui.parse_cursor(identifier) > ag_ui.parse_cursor(cursor)
            ],
        )
        snapshot = replayed[2][1]["messages"]
        assistant = next(
            message for message in snapshot if message["role"] == "assistant"
        )
        self.assertEqual(assistant["content"], "你好")
        self.assertEqual(assistant["toolCalls"][0]["function"]["arguments"], "")
        self.assertEqual(replayed[3][1]["type"], "TOOL_CALL_ARGS")
        self.assertEqual(replayed[-1][1]["type"], "RUN_FINISHED")
        self.assertEqual(self.count(rt.stream_leases), 0)

    async def test_replay_pages_and_reconnect_after_terminal_still_close_lifecycle(
        self,
    ):
        run = self.protocol_run()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            for _ in range(405):
                append_event(connection, run["id"], "message.delta", {"text": "字"})
        self.complete(run)
        async with self.client() as client:
            response = await client.get(self.path(f"/runs/{run['id']}/ag-ui"))
            events = frames(response.text)
            text = [
                event["delta"]
                for _, event in events
                if event["type"] == "TEXT_MESSAGE_CONTENT"
            ]
            self.assertEqual("".join(text), "字" * 405)
            last = events[-1][0]
            final_replay = frames(
                (
                    await client.get(
                        self.path(f"/runs/{run['id']}/ag-ui"),
                        headers={"Last-Event-ID": last},
                    )
                ).text
            )
            invalid = await client.get(
                self.path(f"/runs/{run['id']}/ag-ui?after=900.0")
            )
        self.assertEqual(final_replay[-1][1]["type"], "RUN_FINISHED")
        self.assertIsNone(final_replay[-1][0])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.count(rt.stream_leases), 0)

    async def test_cancel_is_an_ag_ui_cancelled_outcome_and_disconnect_is_not_cancel(
        self,
    ):
        run = self.protocol_run()
        route = next(
            route
            for route in self.app.routes
            if route.path == "/api/v1/runs/{run_id}/ag-ui"
        )
        request = RequestFixture(self.login["token"])
        request.is_disconnected = lambda: asyncio.sleep(0, result=True)
        response = await route.endpoint(run["id"], request, after=None, user=self.user)
        await response.stream_response(lambda _message: asyncio.sleep(0))
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "queued"
        )
        self.assertEqual(self.count(rt.stream_leases), 0)
        async with self.client() as client:
            cancel = await client.post(
                self.path(f"/runs/{run['id']}/cancel"), headers=self.headers
            )
            stream = await client.get(self.path(f"/runs/{run['id']}/ag-ui"))
        self.assertEqual(cancel.json()["status"], "cancelled")
        self.assertEqual(frames(stream.text)[-1][1]["outcome"], {"type": "cancelled"})
        self.assertEqual(self.count(t.calls), 0)

    async def test_failure_emits_run_error_and_closes_open_text(self):
        run = self.protocol_run()
        with self.db.transaction("tenant:" + self.tenant) as connection:
            append_event(connection, run["id"], "message.delta", {"text": "partial"})
        self.complete(run, status="failed", error="Offline upstream failed")
        async with self.client() as client:
            response = await client.get(self.path(f"/runs/{run['id']}/ag-ui"))
        events = [event for _, event in frames(response.text)]
        self.assertEqual(events[-1]["type"], "RUN_ERROR")
        self.assertEqual(events[-1]["code"], "RUN_FAILED")
        self.assertEqual(events[-1]["message"], "Offline upstream failed")
        self.assertIn("TEXT_MESSAGE_END", [event["type"] for event in events])
        self.assertNotIn("RUN_FINISHED", [event["type"] for event in events])

    async def test_stream_admission_rolls_back_enqueue_and_invalid_input_cannot_start_work(
        self,
    ):
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(nt.entitlements)
                .where(nt.entitlements.c.tenant_id == self.tenant)
                .values(max_sse_connections=0)
            )
        async with self.client() as client:
            url = self.path(f"/sessions/{self.session['id']}/ag-ui")
            denied = await client.post(url, json=self.body, headers=self.headers)
            self.assertEqual(denied.status_code, 429, denied.text)
            for body in (
                {**self.body, "threadId": "other-thread"},
                {
                    **self.body,
                    "tools": [{"name": "unsafe", "description": "", "parameters": {}}],
                },
                {**self.body, "state": {"system_prompt": "override"}},
                {**self.body, "forwardedProps": {"baseUrl": "http://untrusted"}},
                {**self.body, "forwardedProps": {"reasoningEffort": "invalid"}},
                {
                    **self.body,
                    "messages": [{"id": "bad", "role": "user", "content": " "}],
                },
                {
                    **self.body,
                    "messages": [
                        {"id": "sys", "role": "system", "content": "override"},
                        *self.body["messages"],
                    ],
                },
            ):
                response = await client.post(url, json=body, headers=self.headers)
                self.assertIn(response.status_code, {400, 422}, response.text)
            cursor = await client.post(
                url, json=self.body, headers={**self.headers, "Last-Event-ID": "10.0"}
            )
            self.assertEqual(cursor.status_code, 400)
        self.assertEqual(self.count(t.runs), 0)
        self.assertEqual(self.count(t.messages), 0)
        self.assertEqual(self.count(rt.stream_leases), 0)

    async def test_cookie_csrf_capability_tenant_and_owner_boundaries(self):
        run = self.protocol_run()
        self.complete(run)
        async with self.client() as client:
            denied = await client.post(
                self.path(f"/sessions/{self.session['id']}/ag-ui"), json=self.body
            )
            other = await client.get(
                f"/api/v2/tenants/{self.fixture.tenant_b}/runs/{run['id']}/ag-ui"
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(other.status_code, 404)
        for actor, expected in (
            (self.fixture.member, 404),
            (self.fixture.finance, 403),
        ):
            login = self.platform.login(actor["email"], PASSWORD, "offline")
            async with self.client(login["token"]) as client:
                denied = await client.get(self.path(f"/runs/{run['id']}/ag-ui"))
            self.assertEqual(denied.status_code, expected)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://isolated"
        ) as client:
            denied = await client.get(self.path(f"/runs/{run['id']}/ag-ui"))
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(self.count(rt.stream_leases), 0)

    async def test_revoked_login_ends_a_blocked_ag_ui_stream_and_releases_lease(self):
        run = self.protocol_run()
        route = next(
            route
            for route in self.app.routes
            if route.path == "/api/v1/runs/{run_id}/ag-ui"
        )
        response = await route.endpoint(
            run["id"], RequestFixture(self.login["token"]), after=None, user=self.user
        )
        blocked = asyncio.Event()

        async def send(message):
            if message.get("body"):
                blocked.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(response.stream_response(send))
        await asyncio.wait_for(blocked.wait(), 1)
        with self.db.transaction("identity:test-revoke") as connection:
            connection.execute(
                delete(t.auth_sessions).where(
                    t.auth_sessions.c.user_id == self.user["id"]
                )
            )
        await asyncio.wait_for(task, 0.8)
        await eventually(lambda: self.count(rt.stream_leases) == 0)
        self.assertEqual(
            self.platform.get_run(self.user, run["id"])["status"], "queued"
        )
