"""A real root lease runs a fixed team against a deterministic offline gateway."""

import asyncio
import json
import unittest
from dataclasses import replace

from sqlalchemy import select

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import tables as t
from agent_platform.modules.run_records import uid
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests import test_agent_registry as registry_tests

TEXT = {
    "name": "request",
    "version": 1,
    "json_schema": {"type": "string", "maxLength": 16000},
}
DATA = registry_tests.DATA


class ScriptedGateway:
    configured = True

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def stream(self, messages, model, max_tokens, temperature, tools):
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        if isinstance(value, ModelReply):
            reply = value
        else:
            reply = ModelReply(
                value, [], 20, 10, {"prompt_tokens": 20, "completion_tokens": 10}
            )
        if reply.content:
            yield GatewayEvent("text", {"text": reply.content})
        yield GatewayEvent("result", reply)


class CollaborationRuntimeTests(unittest.TestCase):
    setUp = registry_tests.AgentRegistryTests.setUp
    tearDown = registry_tests.AgentRegistryTests.tearDown
    login = registry_tests.AgentRegistryTests.login
    request = registry_tests.AgentRegistryTests.request
    create_resources = registry_tests.AgentRegistryTests.create_resources
    enqueue = registry_tests.AgentRegistryTests.enqueue

    def setup_team(
        self,
        *,
        optional=False,
        summary_policy=None,
        planning="template",
        dynamic_enabled=False,
        max_parallel=1,
        parallel_enabled=False,
        durable_enabled=False,
        distributed_enabled=False,
        recovery_enabled=False,
        member_tools=(),
    ):
        model, _, _ = self.create_resources()
        members = []
        for index in range(2):
            response = self.request(
                "POST",
                "/agents",
                json={
                    "name": f"Member {index}",
                    "model_id": model["id"],
                    "max_tokens": 128,
                    "max_steps": 3,
                    "system_prompt": f"Private role {index}",
                    "tools": list(member_tools) if index == 0 else [],
                    "task_contracts": [
                        {
                            "task_type": "work",
                            "input": TEXT if index == 0 else DATA,
                            "output": DATA,
                        }
                    ],
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            member = response.json()
            self.assertEqual(
                self.request("POST", f"/agents/{member['id']}/publish").status_code, 200
            )
            members.append(
                {
                    "key": f"m{index}",
                    "agent_id": member["id"],
                    "version": 1,
                    "responsibility": "Fixture task",
                }
            )
        body = {
            "name": "Test team",
            "execution_mode": "team",
            "max_tokens": 128,
            "collaboration_spec": {
                "members": members,
                "planning": planning,
                "max_parallel": max_parallel,
                "allow_partial": optional,
                "tasks": [
                    {
                        "key": "normalize",
                        "member_key": "m0",
                        "task_type": "work",
                        "objective": "Normalize data",
                    },
                    {
                        "key": "analyze",
                        "member_key": "m1",
                        "task_type": "work",
                        "objective": "Analyze normalized data",
                        "dependencies": ["normalize"],
                        "required": not optional,
                    },
                ],
            },
        }
        if summary_policy:
            body["execution_policy"] = summary_policy
        response = self.request("POST", "/agents", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        agent = response.json()
        response = self.request("POST", f"/agents/{agent['id']}/publish")
        self.assertEqual(response.status_code, 200, response.text)
        session = self.request("POST", "/sessions", json={"agent_id": agent["id"]})
        self.assertEqual(session.status_code, 201, session.text)
        setting = self.request("GET", "/agents/collaboration-settings").json()
        self.request(
            "PATCH",
            "/agents/collaboration-settings",
            json={"enabled": True, "expected_version": setting["version"]},
        )
        self.platform.settings = replace(
            self.settings,
            collaboration_enabled=True,
            collaboration_dynamic_enabled=dynamic_enabled,
            collaboration_parallel_enabled=parallel_enabled,
            collaboration_durable_enabled=durable_enabled,
            collaboration_distributed_enabled=distributed_enabled,
            collaboration_recovery_enabled=recovery_enabled,
        )
        self.platform.billing.credit(
            self.tenant, self.fixture.owner["id"], "1", "Team test", uid()
        )
        response = self.enqueue(session.json(), message="Analyze provided fixture data")
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), members

    def execute(self, run, replies):
        gateway = ScriptedGateway(replies)
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        self.assertEqual(claimed["id"], run["id"])
        asyncio.run(worker.execute(claimed))
        return gateway, worker, claimed

    def detail(self, run):
        response = self.request("GET", f"/runs/{run['id']}/collaboration")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_members_exchange_validated_data_and_only_summary_enters_history(self):
        run, _ = self.setup_team()
        gateway, _, _ = self.execute(
            run, ['{"value":"normalized"}', '{"value":"analysis"}', "Final summary"]
        )
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(detail["outcome"], "complete")
        self.assertEqual(
            [task["status"] for task in detail["tasks"]], ["succeeded", "succeeded"]
        )
        self.assertEqual(len(detail["executions"]), 3)
        self.assertEqual(len(detail["artifacts"]), 2)
        self.assertEqual(len(gateway.calls), 3)
        second = gateway.calls[1]["messages"]
        self.assertIn('"value": "normalized"', second[-1]["content"])
        self.assertNotIn("Private role 0", json.dumps(second))
        self.assertNotIn("system_prompt", json.dumps(detail))
        self.assertTrue(
            all(
                execution["fees"]["confirmed"] != "0"
                for execution in detail["executions"]
            )
        )
        with self.db.read() as connection:
            messages = list(
                connection.scalars(
                    select(t.messages.c.content).where(
                        t.messages.c.run_id == run["id"],
                        t.messages.c.role == "assistant",
                    )
                )
            )
        self.assertEqual(messages, ["Final summary"])

    def test_required_failure_blocks_dependencies_without_fabricating_answer(self):
        run, _ = self.setup_team()
        gateway, _, _ = self.execute(run, ["invalid", "still invalid"])
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(
            [task["status"] for task in detail["tasks"]], ["failed", "blocked"]
        )
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(detail["artifacts"], [])

    def test_optional_failure_preserves_result_and_marks_partial(self):
        run, _ = self.setup_team(optional=True)
        gateway, _, _ = self.execute(
            run,
            [
                '{"value":"normalized"}',
                "invalid",
                "invalid",
                "Partial summary: analysis unavailable",
            ],
        )
        detail = self.detail(run)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(detail["outcome"], "partial")
        self.assertEqual(len(detail["artifacts"]), 1)
        self.assertEqual(len(gateway.calls), 4)
        self.assertIn(
            '"incomplete": ["analyze"]', gateway.calls[-1]["messages"][-1]["content"]
        )

    def test_cancel_after_member_output_prevents_following_model_call(self):
        run, _ = self.setup_team()

        def cancel():
            self.platform.cancel_run(self.fixture.owner, run["id"])
            return '{"value":"completed upstream"}'

        gateway, _, _ = self.execute(run, [cancel])
        detail = self.detail(run)
        self.assertEqual(detail["status"], "cancelled", detail)
        self.assertEqual(len(gateway.calls), 1)
        self.assertFalse(any(task["status"] == "running" for task in detail["tasks"]))
        self.assertNotEqual(detail["fees"]["confirmed"], "0")

    def test_revoked_member_does_not_send_a_request(self):
        run, members = self.setup_team()
        self.request(
            "PATCH",
            f"/agents/{members[0]['agent_id']}/availability",
            json={"available": False},
        )
        gateway, _, _ = self.execute(run, [])
        detail = self.detail(run)
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(gateway.calls, [])

    def test_task_query_rejects_other_user_and_missing_task(self):
        run, _ = self.setup_team()
        self.execute(run, ['{"value":"n"}', '{"value":"a"}', "Summary"])
        detail = self.detail(run)
        task_id = detail["tasks"][0]["id"]
        response = self.request("GET", f"/runs/{run['id']}/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["executions"][0]["input_summary"])
        self.assertEqual(
            self.request("GET", f"/runs/{run['id']}/tasks/missing").status_code, 404
        )
        from agent_platform.infrastructure.errors import PlatformError
        from agent_platform.modules.collaboration.queries import detail as query

        with self.assertRaises(PlatformError):
            query(self.platform, self.fixture.member, run["id"])

    def test_every_sse_cursor_restores_members_and_does_not_repeat_calls(self):
        from agent_platform.modules import ag_ui
        from agent_platform.tests.test_ag_ui import frames

        run, _ = self.setup_team(optional=True)
        gateway, _, _ = self.execute(
            run, ['{"value":"source"}', "invalid", "invalid", "Partial answer"]
        )
        response = self.request("GET", f"/runs/{run['id']}/ag-ui")
        self.assertEqual(response.status_code, 200, response.text)
        original = frames(response.text)
        child_errors = [e for _, e in original if e["type"] == "SUBAGENT_ERROR"]
        self.assertEqual(len(child_errors), 1)
        self.assertEqual(original[-1][1]["type"], "RUN_FINISHED")
        children = {
            e["subagentRunId"] for _, e in original if e["type"] == "SUBAGENT_STARTED"
        }
        self.assertEqual(len(children), 3)
        final_messages = [
            e["messages"] for _, e in original if e["type"] == "MESSAGES_SNAPSHOT"
        ][-1]
        main = [
            m
            for m in final_messages
            if m["role"] == "assistant" and not m.get("subagentRunId")
        ]
        self.assertEqual([m["content"] for m in main], ["Partial answer"])
        self.assertTrue(
            all(
                e.get("subagentRunId") in children
                for _, e in original
                if e["type"] == "TEXT_MESSAGE_CONTENT"
            )
        )
        for cursor, _ in original:
            if not cursor:
                continue
            resumed = self.request(
                "GET", f"/runs/{run['id']}/ag-ui", headers={"Last-Event-ID": cursor}
            )
            self.assertEqual(resumed.status_code, 200, cursor)
            restored = frames(resumed.text)
            remaining = [
                (c, e)
                for c, e in original
                if c and ag_ui.parse_cursor(c) > ag_ui.parse_cursor(cursor)
            ]
            self.assertEqual([(c, e) for c, e in restored if c], remaining, cursor)
            snapshots = [
                e["messages"] for _, e in restored if e["type"] == "MESSAGES_SNAPSHOT"
            ]
            self.assertEqual(snapshots[-1], final_messages, cursor)
            self.assertEqual(restored[-1][1]["type"], "RUN_FINISHED")
        self.assertEqual(len(gateway.calls), 4)

    def test_worker_loss_preserves_confirmed_tasks_and_rejects_old_fence(self):
        import time

        from sqlalchemy import update

        from agent_platform.infrastructure.errors import PlatformError
        from agent_platform.modules.collaboration.scheduler import (
            CollaborationScheduler,
        )

        run, _ = self.setup_team()
        gateway = ScriptedGateway(['{"value":"saved"}'])
        worker = Worker(self.platform, gateway=gateway)
        claimed = worker.claim()
        scheduler = CollaborationScheduler(worker, claimed)
        scheduler.initialize()
        scheduler.initialize()
        tasks = scheduler.tasks()
        self.assertEqual(len(tasks), 2)
        scheduler.transition(tasks[0], "ready")
        asyncio.run(scheduler.execute_task(tasks[0], tasks))
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(lease_until=time.time() - 1)
            )
        replacement = Worker(self.platform, gateway=gateway)
        replacement.recover()
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(
            [task["status"] for task in detail["tasks"]], ["succeeded", "blocked"]
        )
        self.assertEqual(len(detail["artifacts"]), 1)
        self.assertIsNone(replacement.claim())
        with self.assertRaises(PlatformError):
            scheduler.transition(tasks[1], "ready")
        self.assertEqual(len(gateway.calls), 1)

    def test_root_budget_and_call_limit_retain_summary_reserve(self):
        from decimal import Decimal

        from agent_platform.infrastructure.errors import PlatformError
        from agent_platform.modules.collaboration.scheduler import (
            CollaborationScheduler,
        )

        self.setup_team()
        worker = Worker(self.platform, gateway=ScriptedGateway([]))
        claimed = worker.claim()
        scheduler = CollaborationScheduler(worker, claimed)
        scheduler.initialize()
        task = scheduler.tasks()[0]
        scheduler.transition(task, "ready")
        context = scheduler.begin(
            scheduler.snapshot["members"]["m0"]["spec"], [], task=task
        )
        maximum = Decimal(claimed["spec"]["execution_limits"]["max_cost"])
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            with self.assertRaises(PlatformError) as failure:
                worker.invocation.authorize_call(
                    connection, context.invocation_run(), 1, maximum * Decimal("0.9")
                )
            self.assertEqual(failure.exception.code, "run_budget_exceeded")
        summary = scheduler.begin(claimed["spec"], [], role="summary")
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            worker.invocation.authorize_call(
                connection, summary.invocation_run(), 1, maximum * Decimal("0.9")
            )
        # Two total calls permit only one member call and one final summary.
        member_run = context.invocation_run()
        member_run["root_spec"]["team_snapshot"]["definition"]["max_model_calls"] = 2
        worker.invocation.register_call(member_run, uid())
        with self.assertRaises(PlatformError) as failure:
            worker.invocation.register_call(member_run, uid())
        self.assertEqual(failure.exception.code, "team_call_limit")
        summary_run = summary.invocation_run()
        summary_run["root_spec"]["team_snapshot"]["definition"]["max_model_calls"] = 2
        worker.invocation.register_call(summary_run, uid())

    def test_template_install_is_atomic_idempotent_and_graph_survives_summary_failure(
        self,
    ):
        from sqlalchemy import func, update

        from agent_platform.infrastructure import tenancy_tables as nt
        from agent_platform.tests.generative_ui_fixture import COUNTER_UI
        from agent_platform.tests.test_ag_ui import frames

        self.create_resources()
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            count = connection.scalar(
                select(func.count())
                .select_from(t.agents)
                .where(t.agents.c.tenant_id == self.tenant)
            )
            maximum = connection.scalar(
                select(nt.entitlements.c.max_agents).where(
                    nt.entitlements.c.tenant_id == self.tenant
                )
            )
            connection.execute(
                update(nt.entitlements)
                .where(nt.entitlements.c.tenant_id == self.tenant)
                .values(max_agents=count + 2)
            )
        response = self.request("POST", "/agents/templates/analysis-team")
        self.assertEqual(response.status_code, 429, response.text)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(t.agents)
                    .where(t.agents.c.tenant_id == self.tenant)
                ),
                count,
            )
            connection.execute(
                update(nt.entitlements)
                .where(nt.entitlements.c.tenant_id == self.tenant)
                .values(max_agents=maximum)
            )
        response = self.request("POST", "/agents/templates/analysis-team")
        self.assertEqual(response.status_code, 200, response.text)
        team = response.json()
        self.assertTrue(team["created"])
        self.assertEqual(
            self.request("POST", "/agents/templates/analysis-team").json(),
            {"id": team["id"], "created": False},
        )
        self.platform.settings = replace(self.settings, collaboration_enabled=True)
        setting = self.request("GET", "/agents/collaboration-settings").json()
        self.request(
            "PATCH",
            "/agents/collaboration-settings",
            json={"enabled": True, "expected_version": setting["version"]},
        )
        session = self.request(
            "POST", "/sessions", json={"agent_id": team["id"]}
        ).json()
        self.platform.billing.credit(
            self.tenant, self.fixture.owner["id"], "1", "Template test", uid()
        )
        response = self.enqueue(session, message="Only use provided facts")
        self.assertEqual(response.status_code, 202, response.text)
        run = response.json()
        output = '{"facts":[],"missing":["No observations supplied"]}'
        tool = ModelReply(
            "",
            [{"id": "ui", "name": "generate_sandboxed_ui", "args": COUNTER_UI}],
            20,
            10,
            {},
        )
        gateway, _, _ = self.execute(
            run, [output, output, tool, output, RuntimeError("Summary failed")]
        )
        detail = self.detail(run)
        self.assertEqual(detail["status"], "failed", detail)
        self.assertEqual(
            [task["status"] for task in detail["tasks"]], ["succeeded"] * 3
        )
        self.assertEqual(len(detail["artifacts"]), 4)
        saved = self.request("GET", f"/sessions/{session['id']}").json()["artifacts"]
        events = frames(self.request("GET", f"/runs/{run['id']}/ag-ui").text)
        activity = next(
            event for _, event in events if event["type"] == "ACTIVITY_SNAPSHOT"
        )
        self.assertEqual(saved[0]["id"], activity["messageId"])
        self.assertTrue(saved[0]["content"]["validated"])
        self.assertTrue(activity["content"]["validated"])
        self.assertEqual(len(gateway.calls), 5)
