"""Remote members share real task, budget, journal and result projections."""

import asyncio
import copy
import csv
import io
import json
import multiprocessing
import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import collaboration_tables as ct
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.db import Database
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.infrastructure.journal_tables import journal
from agent_platform.infrastructure.remote_call_tables import calls
from agent_platform.modules.billing.service import reservations_table
from agent_platform.modules.costs import CostService
from agent_platform.modules.operations import OperationsService
from agent_platform.modules.remote_agents.invocation import RemoteInvocation
from agent_platform.modules.remote_agents.reconciliation import RemoteReconciler
from agent_platform.modules.runtime.contracts import canonical_digest
from agent_platform.modules.runtime.journal import DurableJournal
from agent_platform.modules.service_contexts import worker_services
from agent_platform.tests import test_remote_agent_registry as registry_tests
from agent_platform.tests.test_collaboration_runtime import DATA, ScriptedGateway


class TaskTransport:
    def __init__(self, mode="complete"):
        self.mode = mode
        self.requests = []
        self.submissions = []
        self.tokens = []
        self.on_send = None
        self.after_submit = None
        self.result_value = {"value": "remote-result"}

    async def request(
        self,
        url,
        route,
        *,
        method="GET",
        token,
        body=None,
        authorize_send=None,
        **kwargs,
    ):
        if self.on_send:
            callback, self.on_send = self.on_send, None
            callback()
        if authorize_send:
            await asyncio.to_thread(authorize_send)
        self.requests.append((method, url, route))
        self.tokens.append(token)
        if route == "/tasks":
            self.submissions.append(copy.deepcopy(body))
            if self.after_submit:
                callback, self.after_submit = self.after_submit, None
                callback()
            if self.mode == "lost":
                raise PlatformError(
                    502, "remote_transport_failed", "response lost after send"
                )
        request = self.submissions[-1]
        identifier = request["request_id"]
        fee = {
            "receipt_id": "fee-1",
            "request_id": identifier,
            "quote_id": request["quote_id"],
            "amount": "0.01",
            "final": True,
        }
        result = {
            "value": self.result_value,
            "digest": canonical_digest(self.result_value),
        }
        if route.endswith("/cancel"):
            if self.mode == "cancel_unknown":
                return {"request_id": identifier, "state": "running"}
            return {"request_id": identifier, "state": "cancelled", "fee": fee}
        if self.mode in {"unknown", "cancel_unknown"}:
            return {"request_id": identifier, "state": "not_found"}
        if self.mode == "fee_only":
            return {"request_id": identifier, "state": "succeeded", "fee": fee}
        if self.mode == "output_only":
            return {"request_id": identifier, "state": "succeeded", "result": result}
        if self.mode == "wrong_quote":
            fee["quote_id"] = "other-quote"
        if self.mode == "invalid_output":
            result = {"value": 42, "digest": canonical_digest(42)}
        if self.mode == "wrong_request":
            return {"request_id": "other-call", "state": "succeeded", "result": result}
        return {
            "request_id": identifier,
            "state": "succeeded",
            "fee": fee,
            "result": result,
        }


def crash_remote_worker(settings, point, path, *, distributed=False):
    """Exit a separate process without running exception or financial cleanup."""
    transport = TaskTransport("wrong_quote" if point == "fee_conflict" else "complete")

    def submitted():
        with open(path, "w") as output:
            json.dump(transport.submissions, output)
            output.flush()
            os.fsync(output.fileno())
        if point == "after_send":
            os._exit(73)

    transport.after_submit = submitted
    original_confirm = DurableJournal.confirm

    def confirmed(self, *args, **kwargs):
        result = original_confirm(self, *args, **kwargs)
        if result["kind"] == "remote":
            os._exit(73)
        return result

    def before_send(*args, **kwargs):
        os._exit(73)

    worker = Worker(
        worker_services(Database(settings.database_url), settings),
        gateway=ScriptedGateway([]),
        remote_transport=transport,
    )
    queue = worker.member_queue if distributed else worker
    claim = queue.claim()
    if claim is None:
        os._exit(74)
    target, method, replacement = (
        (RemoteInvocation, "dispatch", before_send)
        if point == "before_send"
        else (DurableJournal, "confirm", confirmed)
    )
    with patch.object(target, method, replacement):
        asyncio.run(queue.execute(claim))
    os._exit(75)


class RemoteExecutionTests(unittest.TestCase):
    setUp = registry_tests.RemoteAgentRegistryTests.setUp
    tearDown = registry_tests.RemoteAgentRegistryTests.tearDown
    login = registry_tests.RemoteAgentRegistryTests.login
    request = registry_tests.RemoteAgentRegistryTests.request
    create_resources = registry_tests.RemoteAgentRegistryTests.create_resources
    enqueue = registry_tests.RemoteAgentRegistryTests.enqueue
    credential = registry_tests.RemoteAgentRegistryTests.credential
    body = registry_tests.RemoteAgentRegistryTests.body
    register = registry_tests.RemoteAgentRegistryTests.register
    probe = registry_tests.RemoteAgentRegistryTests.probe

    def setup_run(
        self,
        *,
        mixed=False,
        max_calls=20,
        summary_policy=None,
        recovery=False,
        distributed=False,
    ):
        credential, _, registration = self.register()
        self.probe(registration)
        model, _, _ = self.create_resources()
        members = [
            {
                "key": "remote",
                "agent_id": registration["agent_id"],
                "version": 1,
                "responsibility": "Analyze only assigned input",
            }
        ]
        tasks = [
            {
                "key": "remote-work",
                "member_key": "remote",
                "task_type": "work",
                "objective": "Normalize the provided facts",
            }
        ]
        if mixed:
            response = self.request(
                "POST",
                "/agents",
                json={
                    "name": "Local analyst",
                    "model_id": model["id"],
                    "max_tokens": 128,
                    "system_prompt": "PRIVATE-LOCAL-PROMPT",
                    "task_contracts": [
                        {"task_type": "work", "input": DATA, "output": DATA}
                    ],
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            agent = response.json()
            self.request("POST", f"/agents/{agent['id']}/publish")
            members.append(
                {
                    "key": "local",
                    "agent_id": agent["id"],
                    "version": 1,
                    "responsibility": "Check remote output",
                }
            )
            tasks.append(
                {
                    "key": "local-work",
                    "member_key": "local",
                    "task_type": "work",
                    "objective": "Check normalized facts",
                    "dependencies": ["remote-work"],
                }
            )
        body = {
            "name": "Mixed team",
            "model_id": model["id"],
            "execution_mode": "team",
            "max_tokens": 128,
            "system_prompt": "PRIVATE-ROOT-PROMPT",
            "collaboration_spec": {
                "members": members,
                "tasks": tasks,
                "max_model_calls": max_calls,
            },
        }
        if summary_policy:
            body["execution_policy"] = summary_policy
        response = self.request("POST", "/agents", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        team = response.json()
        self.request("POST", f"/agents/{team['id']}/publish")
        config = self.request("GET", "/agents/collaboration-settings").json()
        self.request(
            "PATCH",
            "/agents/collaboration-settings",
            json={"enabled": True, "expected_version": config["version"]},
        )
        self.platform.settings = replace(
            self.platform.settings,
            collaboration_recovery_enabled=recovery,
            collaboration_distributed_enabled=distributed,
        )
        self.platform.billing.credit(
            self.tenant,
            self.fixture.owner["id"],
            "2",
            "Remote execution fixture",
            "credit",
        )
        costs = CostService(self.db, authorize=lambda conn: None)
        costs.configure_budget("platform", "platform", "10", self.fixture.owner["id"])
        costs.configure_price(self.deployment["id"], "1", "2", self.fixture.owner["id"])
        session = self.request(
            "POST", "/sessions", json={"agent_id": team["id"]}
        ).json()
        response = self.enqueue(session, message="A=10")
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), registration, credential

    def execute(self, run, transport, *, mixed=False):
        gateway = ScriptedGateway(
            ['{"value":"local-result"}', "Final combined answer"]
            if mixed
            else ["Final combined answer"]
        )
        worker = Worker(self.platform, gateway=gateway, remote_transport=transport)
        claim = worker.claim()
        self.assertEqual(claim["id"], run["id"])
        asyncio.run(worker.execute(claim))
        detail = self.request("GET", f"/runs/{run['id']}/collaboration")
        self.assertEqual(detail.status_code, 200, detail.text)
        return worker, gateway, detail.json()

    def test_mixed_team_runs_remote_then_local_with_shared_fees_and_private_input_boundary(
        self,
    ):
        run, registration, _ = self.setup_run(mixed=True)
        transport = TaskTransport()
        _worker, gateway, detail = self.execute(run, transport, mixed=True)
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"],
            "succeeded",
            detail,
        )
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(len(gateway.calls), 2)
        request = transport.submissions[0]
        self.assertEqual(request["inputs"], [{"task_key": None, "value": "A=10"}])
        self.assertNotIn("PRIVATE", str(request))
        self.assertNotIn("tenant_id", request)
        self.assertNotIn("root_run_id", request)
        self.assertNotIn("credential", str(request))
        self.assertEqual(request["descriptor_digest"], registration["digest"])
        with self.db.read() as conn:
            row = conn.execute(select(calls)).mappings().one()
            self.assertEqual(row["status"], "confirmed")
            evidence = (
                conn.execute(select(journal).where(journal.c.kind == "remote"))
                .mappings()
                .one()
            )
            self.assertEqual(evidence["state"], "confirmed")
            self.assertNotIn("A=10", evidence["request_ciphertext"])
            spec = conn.scalar(select(t.runs.c.spec).where(t.runs.c.id == run["id"]))
            self.assertEqual(spec["snapshot_schema_version"], 6)
            remote = (
                conn.execute(
                    select(reservations_table).where(
                        reservations_table.c.charge_kind == "fixed"
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(remote["cost"], Decimal("0.01"))
        remote_execution = next(
            e for e in detail["executions"] if e["agent_id"] == registration["agent_id"]
        )
        self.assertEqual(
            Decimal(remote_execution["fees"]["confirmed"]), Decimal("0.01")
        )
        self.assertGreaterEqual(Decimal(detail["fees"]["confirmed"]), Decimal("0.01"))

    def test_lost_post_response_uses_original_request_lookup_without_resubmission(self):
        run, _, _ = self.setup_run()
        transport = TaskTransport("lost")
        _, _, detail = self.execute(run, transport)
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"],
            "succeeded",
            detail,
        )
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(
            transport.requests[-1][2],
            "/tasks/" + transport.submissions[0]["request_id"],
        )

    def test_not_found_never_proves_unsent_and_reconciliation_cancels_without_replaying(
        self,
    ):
        run, _, _ = self.setup_run()
        transport = TaskTransport("unknown")
        with patch(
            "agent_platform.modules.remote_agents.invocation.MAX_EXECUTION_POLLS", 1
        ):
            worker, gateway, _ = self.execute(run, transport)
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(len(gateway.calls), 0)
        with self.db.read() as conn:
            row = (
                conn.execute(
                    select(reservations_table).where(
                        reservations_table.c.charge_kind == "fixed"
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(row["status"], "unresolved")
        queue = RemoteReconciler(worker, transport=transport)
        claim = queue.claim()
        self.assertTrue(claim["cancel_due"])
        asyncio.run(queue.execute(claim))
        self.assertEqual(
            sum(route.endswith("/cancel") for _, _, route in transport.requests), 1
        )
        self.assertEqual(len(transport.submissions), 1)
        with self.db.read() as conn:
            self.assertEqual(conn.scalar(select(calls.c.cancel_sent)), True)
            self.assertEqual(
                conn.scalar(select(reservations_table.c.status)), "settled"
            )

    def test_fee_without_complete_output_cannot_unlock_following_member(self):
        run, _, _ = self.setup_run(mixed=True)
        with patch(
            "agent_platform.modules.remote_agents.invocation.MAX_EXECUTION_POLLS", 1
        ):
            _, gateway, detail = self.execute(
                run, TaskTransport("fee_only"), mixed=True
            )
        self.assertEqual(len(gateway.calls), 0)
        self.assertEqual(Decimal(detail["fees"]["confirmed"]), Decimal("0.01"))
        self.assertFalse(any(task["status"] == "succeeded" for task in detail["tasks"]))

    def test_complete_output_can_succeed_while_late_fee_is_reconciled_separately(self):
        run, _, _ = self.setup_run()
        transport = TaskTransport("output_only")
        worker, _, detail = self.execute(run, transport)
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"],
            "succeeded",
            detail,
        )
        self.assertEqual(Decimal(detail["fees"]["pending"]), Decimal("0.02"))
        transport.mode = "complete"
        self.platform.settings = replace(
            self.platform.settings, remote_agents_enabled=False
        )
        worker = Worker(self.platform, remote_transport=transport)
        queue = RemoteReconciler(worker, transport=transport)
        claim = queue.claim()
        self.assertIsNotNone(claim)
        asyncio.run(queue.execute(claim))
        with self.db.read() as conn:
            self.assertEqual(conn.scalar(select(calls.c.reconciliation)), "done")
        self.assertEqual(len(transport.submissions), 1)
        self.assertFalse(
            any(route.endswith("/cancel") for _, _, route in transport.requests)
        )
        detail = self.request("GET", f"/runs/{run['id']}/collaboration").json()
        self.assertEqual(Decimal(detail["fees"]["pending"]), 0)

    def test_revocation_after_connect_prevents_dispatch_and_releases_unsent_hold(self):
        run, _, credential = self.setup_run()
        transport = TaskTransport()
        transport.on_send = lambda: self.request(
            "PATCH",
            f"/remote-agent-credentials/{credential['id']}",
            json={"enabled": False},
        )
        _, gateway, detail = self.execute(run, transport)
        self.assertEqual(transport.submissions, [])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(Decimal(detail["fees"]["pending"]), 0)
        with self.db.read() as conn:
            self.assertEqual(
                conn.scalar(select(reservations_table.c.status)), "released"
            )

    def test_remote_request_consumes_team_call_limit_and_reserves_summary_slot(self):
        run, _, _ = self.setup_run(mixed=True, max_calls=2)
        transport = TaskTransport()
        _, gateway, detail = self.execute(run, transport, mixed=True)
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(len(gateway.calls), 0)
        self.assertEqual(
            next(task for task in detail["tasks"] if task["member_key"] == "local")[
                "status"
            ],
            "failed",
        )

    def test_invalid_remote_output_retains_fee_but_never_creates_artifact(self):
        run, _, _ = self.setup_run()
        _, gateway, _ = self.execute(run, TaskTransport("invalid_output"))
        self.assertEqual(gateway.calls, [])
        with self.db.read() as conn:
            self.assertEqual(conn.execute(select(ct.artifacts)).all(), [])
            self.assertEqual(
                conn.scalar(select(reservations_table.c.cost)), Decimal("0.01")
            )

    def test_quote_cannot_consume_root_summary_reserve(self):
        run, _, _ = self.setup_run(summary_policy={"max_cost": "0.02"})
        transport = TaskTransport()
        _, gateway, detail = self.execute(run, transport)
        self.assertEqual(transport.submissions, [])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(Decimal(detail["fees"]["pending"]), 0)
        self.assertEqual(Decimal(detail["fees"]["confirmed"]), 0)

    def test_root_cancellation_prevents_output_commit_but_keeps_valid_fee(self):
        run, _, _ = self.setup_run()
        transport = TaskTransport()
        transport.after_submit = lambda: self.platform.cancel_run(
            self.fixture.owner, run["id"]
        )
        worker, gateway, detail = self.execute(run, transport)
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"], "cancelled"
        )
        self.assertEqual(gateway.calls, [])
        self.assertEqual(detail["artifacts"], [])
        self.assertEqual(Decimal(detail["fees"]["confirmed"]), Decimal("0.01"))
        queue = RemoteReconciler(worker, transport=transport)
        asyncio.run(queue.execute(queue.claim()))
        self.assertEqual(
            sum(route.endswith("/cancel") for _, _, route in transport.requests), 1
        )
        self.assertEqual(len(transport.submissions), 1)

    def test_new_remote_version_and_rotated_credential_do_not_change_queued_task(self):
        from agent_platform.modules.run_records import uid

        run, registration, _ = self.setup_run()
        response = self.request(
            "POST",
            "/remote-agent-credentials",
            json={"name": "Rotated credential", "token": "rotated-remote-test-secret"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = self.body(response.json())
        body.update(
            idempotency_key=uid(), agent_id=registration["agent_id"], expected_version=1
        )
        body["descriptor"].update(remote_version="v2", quote_id="q2")
        response = self.request("POST", "/remote-agents", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.probe(response.json())
        transport = TaskTransport()
        self.execute(run, transport)
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(transport.submissions[0]["remote_version"], "v1")
        self.assertEqual(transport.submissions[0]["quote_id"], "q1")
        self.assertEqual(set(transport.tokens), {"temporary-remote-test-secret"})

    def test_aborted_prepared_intent_cannot_create_a_late_financial_hold(self):
        run, _, _ = self.setup_run()
        original = RemoteInvocation.reserve

        def delayed_admission(invocation, record, intent):
            invocation.abort(record)
            return original(invocation, record, intent)

        transport = TaskTransport()
        with patch.object(RemoteInvocation, "reserve", delayed_admission):
            self.execute(run, transport)
        self.assertEqual(transport.submissions, [])
        with self.db.read() as conn:
            self.assertEqual(conn.execute(select(reservations_table)).all(), [])
            self.assertEqual(conn.execute(select(calls)).all(), [])

    def test_root_takeover_queries_sent_remote_task_using_same_identity(self):
        class ProcessLost(BaseException):
            pass

        run, _, _ = self.setup_run(recovery=True)
        transport = TaskTransport()

        def lose_process():
            raise ProcessLost()

        transport.after_submit = lose_process
        worker = Worker(
            self.platform, gateway=ScriptedGateway([]), remote_transport=transport
        )
        claim = worker.claim()
        with self.assertRaises(ProcessLost):
            asyncio.run(worker.execute(claim))
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            conn.execute(
                update(t.runs).where(t.runs.c.id == run["id"]).values(lease_until=0)
            )
            before = dict(
                conn.execute(select(journal).where(journal.c.kind == "remote"))
                .mappings()
                .one()
            )
        worker = Worker(
            self.platform,
            gateway=ScriptedGateway(["Recovered summary"]),
            remote_transport=transport,
        )
        worker.recover()
        resumed = worker.claim()
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed["recovery_count"], 1)
        asyncio.run(worker.execute(resumed))
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"], "succeeded"
        )
        self.assertEqual(len(transport.submissions), 1)
        with self.db.read() as conn:
            after = (
                conn.execute(select(journal).where(journal.c.kind == "remote"))
                .mappings()
                .one()
            )
            self.assertEqual(after["call_id"], before["call_id"])
            self.assertEqual(after["idempotency_key"], before["idempotency_key"])
            self.assertEqual(after["state"], "confirmed")

    def test_reconciliation_is_bounded_and_cancel_is_not_repeated(self):
        run, _, _ = self.setup_run()
        transport = TaskTransport("cancel_unknown")
        with patch(
            "agent_platform.modules.remote_agents.invocation.MAX_EXECUTION_POLLS", 1
        ):
            worker, _, _ = self.execute(run, transport)
        queue = RemoteReconciler(worker, transport=transport)
        with patch(
            "agent_platform.modules.remote_agents.reconciliation.MAX_RECONCILE_POLLS", 2
        ):
            for _ in range(2):
                with self.db.transaction(f"tenant:{self.tenant}") as conn:
                    conn.execute(update(calls).values(next_poll_at=0))
                queue.cursor = ""
                claim = queue.claim()
                self.assertIsNotNone(claim)
                asyncio.run(queue.execute(claim))
            queue.cursor = ""
            self.assertIsNone(queue.claim())
        with self.db.read() as conn:
            self.assertEqual(conn.scalar(select(calls.c.reconciliation)), "exhausted")
            self.assertEqual(
                conn.scalar(select(reservations_table.c.status)), "unresolved"
            )
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(
            sum(route.endswith("/cancel") for _, _, route in transport.requests), 1
        )

    @contextmanager
    def https_run(self, *, mixed=False):
        from agent_platform.modules.remote_agents.contracts import RemoteDescriptor
        from agent_platform.modules.remote_agents.registry import RemoteHealthQueue
        from agent_platform.modules.remote_agents.transport import RemoteTransport
        from agent_platform.tests.remote_https_fixture import https_peer

        with https_peer() as peer:
            self.platform.settings = replace(
                self.platform.settings, remote_agent_allowed_origins=(peer["origin"],)
            )
            original_body = self.body

            def body(credential):
                values = original_body(credential)
                values["endpoints"][0]["url"] = peer["origin"] + "/agent"
                peer["descriptor"] = RemoteDescriptor.model_validate(
                    values["descriptor"]
                )
                return values

            transport = RemoteTransport((peer["origin"],))

            def probe(registration):
                worker = Worker(
                    self.platform, gateway=self.gateway, remote_transport=transport
                )
                queue = RemoteHealthQueue(worker, transport=transport)
                claim = queue.claim()
                self.assertIsNotNone(claim)
                asyncio.run(queue.execute(claim))

            self.body, self.probe = body, probe
            run, _, _ = self.setup_run(mixed=mixed)
            yield peer, run, transport

    def test_real_https_health_lost_post_and_mixed_team_complete_without_replay(self):
        with self.https_run(mixed=True) as (peer, run, transport):
            _, gateway, detail = self.execute(run, transport, mixed=True)
            self.assertEqual(
                self.platform.get_run(self.fixture.owner, run["id"])["status"],
                "succeeded",
                detail,
            )
            self.assertEqual(len(peer["submissions"]), 1)
            self.assertEqual(len(gateway.calls), 2)
            self.assertIn(("GET", "/agent/health"), peer["requests"])
            self.assertIn(
                ("GET", "/agent/tasks/" + peer["submissions"][0]["request_id"]),
                peer["requests"],
            )
            self.assertEqual(set(peer["sni"]), {"remote.example.test"})
            self.assertNotIn("PRIVATE", str(peer["submissions"]))

    def test_real_https_unknown_submission_is_cancelled_once_without_artifacts(self):
        with self.https_run() as (peer, run, transport):
            peer["status_hook"] = lambda payload: {
                "request_id": payload["request_id"],
                "state": "not_found",
            }
            with patch(
                "agent_platform.modules.remote_agents.invocation.MAX_EXECUTION_POLLS", 1
            ):
                worker, gateway, detail = self.execute(run, transport)
            self.assertEqual(detail["artifacts"], [])
            self.assertEqual(gateway.calls, [])
            self.assertEqual(Decimal(detail["fees"]["pending"]), Decimal("0.02"))
            queue = RemoteReconciler(worker, transport=transport)
            asyncio.run(queue.execute(queue.claim()))
            queue.cursor = ""
            self.assertIsNone(queue.claim())
            self.assertEqual(len(peer["submissions"]), 1)
            self.assertEqual(
                sum(route.endswith("/cancel") for _, route in peer["requests"]), 1
            )
            detail = self.request("GET", f"/runs/{run['id']}/collaboration").json()
            self.assertEqual(detail["artifacts"], [])
            self.assertEqual(Decimal(detail["fees"]["pending"]), 0)
            self.assertEqual(Decimal(detail["fees"]["confirmed"]), Decimal("0.01"))

    def test_real_https_late_fee_after_feature_shutdown_keeps_completed_output(self):
        with self.https_run() as (peer, run, transport):
            peer["status_hook"] = lambda payload: {
                key: value for key, value in payload.items() if key != "fee"
            }
            _, _, detail = self.execute(run, transport)
            artifacts = detail["artifacts"]
            self.assertTrue(artifacts)
            self.assertEqual(Decimal(detail["fees"]["pending"]), Decimal("0.02"))
            peer["status_hook"] = None
            self.platform.settings = replace(
                self.platform.settings, remote_agents_enabled=False
            )
            queue = RemoteReconciler(Worker(self.platform, remote_transport=transport))
            asyncio.run(queue.execute(queue.claim()))
            self.assertEqual(len(peer["submissions"]), 1)
            detail = self.request("GET", f"/runs/{run['id']}/collaboration").json()
            self.assertEqual(detail["artifacts"], artifacts)
            self.assertEqual(Decimal(detail["fees"]["pending"]), 0)
            self.assertEqual(
                self.platform.get_run(self.fixture.owner, run["id"])["status"],
                "succeeded",
            )

    def test_real_https_wrong_quote_preserves_valid_output_and_financial_hold(self):
        with self.https_run() as (peer, run, transport):

            def wrong_quote(payload):
                return {**payload, "fee": {**payload["fee"], "quote_id": "wrong-quote"}}

            peer["status_hook"] = wrong_quote
            _, _, detail = self.execute(run, transport)
            self.assertEqual(len(peer["submissions"]), 1)
            self.assertTrue(detail["artifacts"])
            self.assertEqual(Decimal(detail["fees"]["pending"]), Decimal("0.02"))
            self.assertEqual(
                self.platform.get_run(self.fixture.owner, run["id"])["status"],
                "succeeded",
            )

    def test_wrong_quote_does_not_turn_valid_output_into_fee_confirmation(self):
        run, _, _ = self.setup_run()
        _, _, detail = self.execute(run, TaskTransport("wrong_quote"))
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"],
            "succeeded",
            detail,
        )
        self.assertEqual(Decimal(detail["fees"]["pending"]), Decimal("0.02"))

    def process_recovery(self, point, *, distributed=False):
        from agent_platform.infrastructure.task_lease_tables import leases
        from agent_platform.modules.collaboration.scheduler import (
            CollaborationScheduler,
        )
        from agent_platform.modules.collaboration.task_leases import dispatch

        run, _, _ = self.setup_run(recovery=True, distributed=distributed)
        transport = TaskTransport()
        root_worker = Worker(
            self.platform,
            gateway=ScriptedGateway(["Recovered summary"]),
            remote_transport=transport,
        )
        if distributed:
            root = root_worker.claim()
            scheduler = CollaborationScheduler(root_worker, root)
            scheduler.initialize()
            dispatch(scheduler, scheduler.tasks()[0])
        path = Path(self.temporary.name) / "remote-submissions.json"
        process = multiprocessing.get_context("spawn").Process(
            target=crash_remote_worker,
            args=(self.platform.settings, point, str(path)),
            kwargs={"distributed": distributed},
        )
        process.start()
        process.join(15)
        if process.is_alive():
            process.kill()
            process.join(5)
            self.fail("Remote worker failed to reach crash boundary")
        self.assertEqual(process.exitcode, 73)
        transport.submissions = json.loads(path.read_text()) if path.exists() else []
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            before = dict(
                conn.execute(select(journal).where(journal.c.kind == "remote"))
                .mappings()
                .one()
            )
            self.assertEqual(
                before["state"],
                "prepared"
                if point == "before_send"
                else "sent"
                if point == "after_send"
                else "confirmed",
            )
            if distributed:
                old_lease = dict(conn.execute(select(leases)).mappings().one())
                conn.execute(update(leases).values(lease_until=0))
            else:
                conn.execute(
                    update(t.runs).where(t.runs.c.id == run["id"]).values(lease_until=0)
                )
        if distributed:
            root_worker.renew(root)
            member = Worker(
                self.platform, gateway=ScriptedGateway([]), remote_transport=transport
            )
            claim = member.member_queue.claim()
            self.assertIsNotNone(claim)
            self.assertEqual(claim["lease"]["execution_id"], old_lease["execution_id"])
            self.assertEqual(claim["lease"]["deadline"], old_lease["deadline"])
            self.assertGreater(claim["lease"]["fence"], old_lease["fence"])
            asyncio.run(member.member_queue.execute(claim))
            asyncio.run(root_worker.execute(root))
        else:
            root_worker.recover()
            root = root_worker.claim()
            self.assertIsNotNone(root)
            asyncio.run(root_worker.execute(root))
        self.assertEqual(
            self.platform.get_run(self.fixture.owner, run["id"])["status"], "succeeded"
        )
        self.assertEqual(len(transport.submissions), 1)
        self.assertEqual(len(root_worker.gateway.calls), 1)
        if point != "before_send":
            self.assertFalse(
                any(route == "/tasks" for _, _, route in transport.requests)
            )
        if point in {"after_result", "fee_conflict"}:
            self.assertEqual(transport.requests, [])
        with self.db.read() as conn:
            after = (
                conn.execute(select(journal).where(journal.c.kind == "remote"))
                .mappings()
                .one()
            )
            self.assertEqual(after["state"], "confirmed")
            self.assertEqual(after["call_id"], before["call_id"])
            self.assertEqual(after["idempotency_key"], before["idempotency_key"])
            receipt = (
                conn.execute(
                    select(reservations_table).where(
                        reservations_table.c.charge_kind == "fixed"
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(
                receipt["status"],
                "unresolved" if point == "fee_conflict" else "settled",
            )
        return run

    def test_process_exit_before_remote_send_reuses_prepared_identity(self):
        self.process_recovery("before_send")

    def test_process_exit_after_remote_send_only_queries_original_request(self):
        self.process_recovery("after_send")

    def test_process_exit_after_remote_result_reuses_output_without_network(self):
        self.process_recovery("after_result")

    def test_process_exit_after_output_with_fee_conflict_preserves_independent_result(
        self,
    ):
        self.process_recovery("fee_conflict")

    def test_member_process_exit_after_send_preserves_request_and_original_deadline(
        self,
    ):
        self.process_recovery("after_send", distributed=True)

    def test_member_process_exit_after_result_with_fee_conflict_reuses_output(self):
        self.process_recovery("fee_conflict", distributed=True)

    def test_remote_usage_dashboard_and_export_match_confirmed_fees(self):
        run, _, _ = self.setup_run(mixed=True)
        _, _, detail = self.execute(run, TaskTransport(), mixed=True)
        self.platform.settings = replace(
            self.platform.settings,
            export_directory=Path(self.temporary.name) / "exports",
            tombstone_path=Path(self.temporary.name) / "tombstones",
        )
        records = self.platform.call_records(self.fixture.owner, run_id=run["id"])
        total = sum(Decimal(item["cost"]) for item in records)
        self.assertEqual(total, Decimal(detail["fees"]["confirmed"]))
        self.assertEqual(
            total, Decimal(self.platform.dashboard(self.fixture.owner)["cost"])
        )
        self.assertEqual(
            total, Decimal(self.platform.get_run(self.fixture.owner, run["id"])["cost"])
        )
        remote = next(item for item in records if item["call_kind"] == "remote")
        self.assertEqual(remote["input_tokens"], 0)
        self.assertEqual(remote["output_tokens"], 0)
        self.assertEqual(Decimal(remote["cost"]), Decimal("0.01"))
        service = OperationsService(self.platform)
        job = service.request_export(
            self.fixture.owner, kind="calls", idempotency_key="remote-export"
        )
        outcome = service.process_batch()
        self.assertEqual(outcome["errors"], 0)
        content = b"".join(
            service.download(self.fixture.owner, job["id"])["chunks"]
        ).decode()
        rows = list(csv.DictReader(io.StringIO(content)))
        self.assertEqual(
            {item["id"] for item in rows}, {item["id"] for item in records}
        )
        self.assertEqual(sum(Decimal(item["cost"]) for item in rows), total)
        for secret in ("PRIVATE", "remote.example", "Bearer", "credential", "A=10"):
            self.assertNotIn(secret, content)
        self.assertEqual(self.platform.call_records(self.fixture.member), [])
        with self.assertRaises(PlatformError):
            service.download(self.fixture.member, job["id"])
        other = self.fixture.context(self.fixture.owner["id"], self.fixture.tenant_b)
        with self.db.tenant_scope(self.fixture.tenant_b):
            self.assertEqual(self.platform.call_records(other), [])
            with self.assertRaises(PlatformError):
                service.download(other, job["id"])

    def test_redacted_remote_execution_remains_readable_in_usage_response(self):
        from agent_platform.apps.api.responses import CallResponse

        run, _, _ = self.setup_run()
        self.execute(run, TaskTransport())
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            for row in conn.execute(select(ct.executions)).mappings():
                conn.execute(
                    update(ct.executions)
                    .where(ct.executions.c.id == row["id"])
                    .values(spec=OperationsService._redacted_spec(row["spec"]))
                )
        remote = next(
            item
            for item in self.platform.call_records(self.fixture.owner)
            if item["call_kind"] == "remote"
        )
        self.assertEqual(CallResponse.model_validate(remote).agent_name, "[已删除]")

    def test_stale_reconciliation_owner_cannot_send_cancel_or_overwrite_successor(self):
        run, _, _ = self.setup_run()
        transport = TaskTransport("unknown")
        with patch(
            "agent_platform.modules.remote_agents.invocation.MAX_EXECUTION_POLLS", 1
        ):
            worker, _, _ = self.execute(run, transport)
        old = RemoteReconciler(worker, transport=transport)
        claim = old.claim()
        with self.db.transaction(f"tenant:{self.tenant}") as conn:
            conn.execute(update(calls).values(lease_until=0))
        replacement = RemoteReconciler(
            Worker(self.platform, remote_transport=transport)
        )
        successor = replacement.claim()
        self.assertGreater(successor["fence"], claim["fence"])
        prior_requests = list(transport.requests)
        asyncio.run(old.execute(claim))
        self.assertEqual(transport.requests, prior_requests)
        with self.db.read() as conn:
            row = conn.execute(select(calls)).mappings().one()
            self.assertEqual(row["owner"], successor["owner"])
            self.assertEqual(row["fence"], successor["fence"])
            self.assertFalse(row["cancel_sent"])
        asyncio.run(replacement.execute(successor))
        self.assertEqual(
            sum(route.endswith("/cancel") for _, _, route in transport.requests), 1
        )


if __name__ == "__main__":
    unittest.main()
