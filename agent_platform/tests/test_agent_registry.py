"""Registration, discovery and pinned team admission through real HTTP/storage."""

import asyncio
import copy
import unittest
from dataclasses import replace

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.modules.agent_registry.contracts import (
    AgentDeclaration,
    DataContract,
)
from agent_platform.modules.agent_registry.service import AgentRegistry
from agent_platform.modules.run_records import now, uid
from agent_platform.modules.runtime.context import ExecutionContext
from agent_platform.tests import test_api as api_tests

DATA = {
    "name": "data",
    "version": 1,
    "json_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}
CONTRACT = {"task_type": "analyze", "input": DATA, "output": DATA}


class AgentRegistryTests(unittest.TestCase):
    setUp = api_tests.APIIntegrationTest.setUp
    tearDown = api_tests.APIIntegrationTest.tearDown
    login = api_tests.APIIntegrationTest.login
    request = api_tests.APIIntegrationTest.request
    create_resources = api_tests.APIIntegrationTest.create_resources
    enqueue = api_tests.APIIntegrationTest.enqueue

    def create_member(self, *, publish=True, name="Analyst", model_id=None):
        response = self.request(
            "POST",
            "/agents",
            json={
                "name": name,
                "model_id": model_id,
                "max_tokens": 128,
                "system_prompt": "Private member prompt",
                "capability_keys": ["data.analyze"],
                "task_contracts": [CONTRACT],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        agent = response.json()
        if publish:
            response = self.request("POST", f"/agents/{agent['id']}/publish")
            self.assertEqual(response.status_code, 200, response.text)
        return agent

    def team_body(self, member):
        return {
            "name": "Analysis team",
            "execution_mode": "team",
            "max_tokens": 128,
            "collaboration_spec": {
                "members": [
                    {
                        "key": "analyst",
                        "agent_id": member["id"],
                        "version": 1,
                        "responsibility": "Analyze supplied data",
                    }
                ],
                "tasks": [
                    {
                        "key": "analysis",
                        "member_key": "analyst",
                        "task_type": "analyze",
                        "objective": "Analyze input",
                    }
                ],
            },
        }

    def create_team(self, member):
        response = self.request("POST", "/agents", json=self.team_body(member))
        self.assertEqual(response.status_code, 201, response.text)
        team = response.json()
        response = self.request("POST", f"/agents/{team['id']}/publish")
        self.assertEqual(response.status_code, 200, response.text)
        return team

    def test_discovery_only_published_authorized_sanitized_versions(self):
        member = self.create_member()
        self.create_member(publish=False, name="Draft")
        self.request(
            "PATCH",
            f"/agents/{member['id']}",
            json={"description": "PRIVATE DRAFT", "capability_keys": ["changed"]},
        )
        response = self.request(
            "GET", "/agents/discovery?capability=data.analyze&task_type=analyze"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([x["id"] for x in response.json()["items"]], [member["id"]])
        self.assertNotIn("system_prompt", response.text)
        self.assertNotIn("PRIVATE DRAFT", response.text)
        other = self.fixture.context(self.fixture.owner["id"], self.fixture.tenant_b)
        self.assertEqual(
            AgentRegistry(self.platform).discover(other, capability="data.analyze")[
                "items"
            ],
            [],
        )
        digest = DataContract.model_validate(DATA).digest
        self.assertEqual(
            len(
                self.request("GET", f"/agents/discovery?output_digest={digest}").json()[
                    "items"
                ]
            ),
            1,
        )
        self.assertEqual(
            self.request("GET", "/agents/discovery?output_digest=" + "0" * 64).json()[
                "items"
            ],
            [],
        )

    def test_discovery_pages_stably_without_duplicates(self):
        expected = {self.create_member(name=str(i))["id"] for i in range(3)}
        seen, cursor = [], None
        while True:
            path = "/agents/discovery?capability=data.analyze&limit=1"
            response = self.request(
                "GET", path + (f"&cursor={cursor}" if cursor else "")
            )
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            seen.extend(item["id"] for item in data["items"])
            cursor = data["next_cursor"]
            if not cursor:
                break
        self.assertEqual(set(seen), expected)
        self.assertEqual(seen, sorted(expected))
        self.assertEqual(
            self.request("GET", "/agents/discovery?limit=101").status_code, 422
        )

    def test_availability_is_live_and_audited(self):
        member = self.create_member()
        response = self.request(
            "PATCH", f"/agents/{member['id']}/availability", json={"available": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request("GET", "/agents/discovery?capability=data.analyze").json()[
                "items"
            ],
            [],
        )
        self.assertEqual(
            self.request(
                "POST", "/sessions", json={"agent_id": member["id"]}
            ).status_code,
            409,
        )
        with self.db.read() as connection:
            actions = list(
                connection.scalars(
                    select(t.audits.c.action).where(t.audits.c.target == member["id"])
                )
            )
        self.assertIn("agent.disable", actions)
        response = self.request(
            "PATCH", f"/agents/{member['id']}/availability", json={"available": True}
        )
        self.assertEqual(response.status_code, 200)

    def test_team_pins_versions_and_rejects_unpublished_nested_or_foreign_members(self):
        member = self.create_member()
        team = self.create_team(member)
        self.request(
            "PATCH", f"/agents/{member['id']}", json={"system_prompt": "New version"}
        )
        self.request("POST", f"/agents/{member['id']}/publish")
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.agent_versions.c.spec).where(
                    t.agent_versions.c.agent_id == team["id"]
                )
            )
        pinned = spec["team_snapshot"]["members"]["analyst"]["spec"]
        self.assertEqual(pinned["version"], 1)
        self.assertEqual(pinned["system_prompt"], "Private member prompt")
        for candidate, expected in (
            (self.create_member(publish=False), 400),
            (team, 422),
            ({"id": "missing"}, 404),
        ):
            response = self.request("POST", "/agents", json=self.team_body(candidate))
            self.assertEqual(response.status_code, 201, response.text)
            response = self.request("POST", f"/agents/{response.json()['id']}/publish")
            self.assertEqual(response.status_code, expected, response.text)
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(t.agents)
                .where(t.agents.c.id == member["id"])
                .values(available=False)
            )
        response = self.request("POST", f"/agents/{team['id']}/publish")
        self.assertEqual(response.status_code, 409)

    def test_contract_and_dependency_validation(self):
        member = self.create_member()
        body = self.team_body(member)
        task = body["collaboration_spec"]["tasks"][0]
        task["dependencies"] = [task["key"]]
        self.assertEqual(self.request("POST", "/agents", json=body).status_code, 422)
        task["dependencies"] = []
        task["task_type"] = "undeclared"
        created = self.request("POST", "/agents", json=body)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            self.request("POST", f"/agents/{created.json()['id']}/publish").status_code,
            422,
        )
        invalid = copy.deepcopy(DATA)
        invalid["json_schema"]["additionalProperties"] = True
        with self.assertRaises(ValueError):
            DataContract.model_validate(invalid)
        with self.assertRaises(ValueError):
            AgentDeclaration(execution_mode="team")

    def test_team_run_gate_and_frozen_member_model(self):
        model, _, _ = self.create_resources()
        member = self.create_member(model_id=model["id"])
        team = self.create_team(member)
        session = self.request(
            "POST", "/sessions", json={"agent_id": team["id"]}
        ).json()
        self.assertEqual(self.enqueue(session).status_code, 409)
        setting = self.request("GET", "/agents/collaboration-settings").json()
        response = self.request(
            "PATCH",
            "/agents/collaboration-settings",
            json={"enabled": True, "expected_version": setting["version"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.request(
                "PATCH",
                "/agents/collaboration-settings",
                json={"enabled": False, "expected_version": setting["version"]},
            ).status_code,
            409,
        )
        self.assertEqual(self.enqueue(session).status_code, 409)
        self.platform.settings = replace(self.settings, collaboration_enabled=True)
        response = self.enqueue(session)
        self.assertEqual(response.status_code, 202, response.text)
        with self.db.read() as connection:
            spec = connection.scalar(
                select(t.runs.c.spec).where(t.runs.c.id == response.json()["id"])
            )
        self.assertEqual(spec["snapshot_schema_version"], 3)
        pinned = spec["team_snapshot"]["members"]["analyst"]["spec"]
        self.assertEqual(pinned["model_id"], model["id"])
        self.assertIn("price_version_id", pinned)
        self.assertIn("execution_limits", pinned)

    def test_single_agent_call_rechecks_availability(self):
        _, agent, session = self.create_resources()
        self.request(
            "POST",
            "/billing/credits",
            key=uid(),
            json={"amount": "1", "reason": "test"},
        )
        queued = self.enqueue(session)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.request(
            "PATCH", f"/agents/{agent['id']}/availability", json={"available": False}
        )
        worker = Worker(self.platform, gateway=self.gateway)
        asyncio.run(worker.execute(worker.claim()))
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(
            self.request("GET", f"/runs/{queued.json()['id']}").json()["status"],
            "failed",
        )

    def test_execution_context_copies_member_and_root_configuration(self):
        _, _, session = self.create_resources()
        queued = self.enqueue(session)
        worker = Worker(self.platform, gateway=self.gateway)
        run = worker.claim()
        spec = {**run["spec"], "name": "Child", "max_tokens": 10}
        context = ExecutionContext.from_run(run, spec=spec)
        spec["max_tokens"] = 20
        view = context.invocation_run()
        view["spec"]["max_tokens"] = 30
        self.assertEqual(context.spec["max_tokens"], 10)
        self.assertEqual(context.root["spec"]["max_tokens"], run["spec"]["max_tokens"])
        self.assertEqual(context.root_run_id, queued.json()["id"])

    def test_cross_run_foreign_keys_reject_task_injection(self):
        _, agent, session = self.create_resources()
        run = self.fixture.create_run(self.fixture.owner, session)
        other = self.fixture.create_run(self.fixture.owner, session)
        plan = {
            "id": uid(),
            "tenant_id": self.tenant,
            "root_run_id": run["id"],
            "created_at": now(),
            "revision": 1,
            "plan": {},
            "digest": "0" * 64,
        }
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(insert(ct.plans).values(**plan))
        with (
            self.assertRaises(IntegrityError),
            self.db.transaction(f"tenant:{self.tenant}") as connection,
        ):
            connection.execute(
                insert(ct.tasks).values(
                    id=uid(),
                    tenant_id=self.tenant,
                    root_run_id=other["id"],
                    created_at=now(),
                    plan_id=plan["id"],
                    logical_key="inject",
                    member_key="m",
                    agent_id=agent["id"],
                    agent_version=1,
                    task_type="analyze",
                    objective="x",
                    dependencies=[],
                    required=True,
                    status="pending",
                )
            )

    def test_shared_invocation_attributes_separate_executions_without_mutating_root(
        self,
    ):
        _, agent, session = self.create_resources()
        self.platform.billing.credit(
            self.tenant, self.fixture.owner["id"], "1", "Invocation test", uid()
        )
        queued = self.enqueue(session)
        worker = Worker(self.platform, gateway=self.gateway)
        run = worker.claim()
        contexts = []
        for attempt in (1, 2):
            identifier = uid()
            context = ExecutionContext.from_run(
                run, execution_id=identifier, attempt=attempt
            )
            with self.db.transaction(f"tenant:{self.tenant}") as connection:
                connection.execute(
                    insert(ct.executions).values(
                        id=identifier,
                        tenant_id=self.tenant,
                        root_run_id=run["id"],
                        created_at=now(),
                        attempt=attempt,
                        role="coordinator",
                        agent_id=agent["id"],
                        agent_version=1,
                        spec=context.spec,
                        input={},
                        input_digest="0" * 64,
                        status="running",
                        owner=run["owner"],
                        fence=run["fence"],
                    )
                )
            contexts.append(context)

        async def invoke():
            for context in contexts:
                await worker.invocation.invoke(
                    context, [{"role": "user", "content": "Test"}], []
                )

        asyncio.run(invoke())
        with self.db.read() as connection:
            calls = list(
                connection.execute(
                    select(t.calls).where(t.calls.c.run_id == queued.json()["id"])
                ).mappings()
            )
        self.assertEqual(
            {row["execution_id"] for row in calls},
            {context.execution_id for context in contexts},
        )
        self.assertTrue(all(row["status"] == "confirmed" for row in calls))
        self.assertEqual(run["spec"], contexts[0].root["spec"])
        self.assertEqual(len(self.gateway.calls), 2)

    def test_tenant_content_purge_covers_collaboration_records(self):
        from pathlib import Path

        from agent_platform.infrastructure import operations_tables as ot
        from agent_platform.modules.operations import OperationsService

        member = self.create_member()
        session = self.fixture.create_session()
        run = self.fixture.create_run(self.fixture.owner, session)
        plan_id, execution_id = uid(), uid()
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                insert(ct.plans).values(
                    id=plan_id,
                    tenant_id=self.tenant,
                    root_run_id=run["id"],
                    created_at=now(),
                    revision=1,
                    plan={"private": "plan"},
                    digest="0" * 64,
                )
            )
            connection.execute(
                insert(ct.executions).values(
                    id=execution_id,
                    tenant_id=self.tenant,
                    root_run_id=run["id"],
                    created_at=now(),
                    attempt=1,
                    role="coordinator",
                    agent_id=member["id"],
                    agent_version=1,
                    spec={"system_prompt": "secret"},
                    input={"private": "input"},
                    input_digest="0" * 64,
                    status="succeeded",
                    output={"private": "output"},
                    fence=1,
                )
            )
            connection.execute(
                insert(ct.artifacts).values(
                    id=uid(),
                    tenant_id=self.tenant,
                    root_run_id=run["id"],
                    created_at=now(),
                    execution_id=execution_id,
                    version=1,
                    kind="data",
                    content={"private": "data"},
                    digest="0" * 64,
                    validation_status="validated",
                )
            )
        self.platform.settings = replace(
            self.settings,
            export_directory=Path(self.temporary.name) / "exports",
            tombstone_path=Path(self.temporary.name) / "tombstones",
        )
        operations = OperationsService(self.platform)
        operations.close_tenant(self.fixture.principal, self.tenant, "Fixture deletion")
        with self.db.transaction(f"tenant:{self.tenant}") as connection:
            connection.execute(
                update(ot.closures)
                .where(ot.closures.c.tenant_id == self.tenant)
                .values(status="purging", retain_until=0, tombstone_written_at=now())
            )
        # Exercise the actual fenced content-purge phases; gateway revocation is
        # covered independently and no external credential exists in this fixture.
        for _ in range(30):
            job = operations._claim_closure(self.tenant)
            if job is None:
                break
            operations._purge_page(job)
        with self.db.read() as connection:
            plan = connection.scalar(
                select(ct.plans.c.plan).where(ct.plans.c.id == plan_id)
            )
            execution = (
                connection.execute(
                    select(ct.executions).where(ct.executions.c.id == execution_id)
                )
                .mappings()
                .one()
            )
            artifact = connection.scalar(select(ct.artifacts.c.content))
        self.assertEqual(plan, {"content_deleted": True})
        self.assertEqual(execution["input"], {})
        self.assertIsNone(execution["output"])
        self.assertNotIn("system_prompt", execution["spec"])
        self.assertEqual(artifact, {"content_deleted": True})
