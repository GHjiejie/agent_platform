"""Real HTTP, worker, authorization and billing; no provider traffic."""

import asyncio
import threading
import unittest
from contextlib import contextmanager

from sqlalchemy import func, select, update

from agent_platform.apps.worker.main import Worker
from agent_platform.infrastructure import api_key_tables as ak
from agent_platform.infrastructure import runtime_tables as rt
from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure import tenancy_tables as nt
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.member_api_keys import MemberKeys
from agent_platform.modules.model_api_contracts import KeyCreate
from agent_platform.modules.runtime.gateway import GatewayEvent, ModelReply
from agent_platform.tests import test_api as base
from agent_platform.tests.fixtures import PASSWORD


class MemberApiKeyTests(unittest.TestCase):
    login = base.APIIntegrationTest.login
    request = base.APIIntegrationTest.request
    create_resources = base.APIIntegrationTest.create_resources
    enqueue = base.APIIntegrationTest.enqueue

    def setUp(self):
        base.APIIntegrationTest.setUp(self)
        self.model, self.agent, self.session = self.create_resources()
        self.platform.billing.credit(
            self.tenant, self.fixture.owner["id"], "10", "API fixture", "api-credit"
        )
        self.keys = MemberKeys(self.platform)

    tearDown = base.APIIntegrationTest.tearDown

    def key(self, user=None, **changes):
        return self.keys.create(
            user or self.fixture.owner, KeyCreate(name="Local test", **changes)
        )

    @staticmethod
    def headers(key):
        return {"Authorization": "Bearer " + key["secret"]}

    @contextmanager
    def worker(self, gateway=None):
        worker = Worker(self.platform, gateway=gateway or self.gateway)
        stop = threading.Event()
        failures = []

        def loop():
            try:
                while not stop.is_set():
                    if run := worker.claim():
                        asyncio.run(worker.execute(run))
                    else:
                        stop.wait(0.01)
            except Exception as exc:  # noqa: BLE001 - surface background test failures
                failures.append(exc)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            yield worker
        finally:
            stop.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "Worker did not stop")
            self.assertEqual(failures, [])

    def chat(self, key, **fields):
        return self.client.post(
            "/v1/chat/completions",
            headers=self.headers(key),
            json={
                "model": self.model["alias"],
                "messages": [{"role": "user", "content": "hello"}],
                "max_completion_tokens": 128,
                **fields,
            },
        )

    def test_member_self_service_csrf_hash_and_private_listing(self):
        member = self.login("member@fixture.test", PASSWORD)
        result = self.request(
            "POST",
            "/api-keys",
            auth=member,
            json={"name": "My app", "expires_in_days": 7},
        )
        self.assertEqual(result.status_code, 201, result.text)
        key = result.json()
        self.assertEqual(result.headers["cache-control"], "no-store")
        listed = self.request("GET", "/api-keys", auth=member)
        self.assertNotIn(key["secret"], listed.text)
        self.assertNotIn("secret_hash", listed.text)
        self.assertEqual(len(listed.json()["items"]), 1)
        self.assertEqual(self.request("GET", "/api-keys").json()["items"], [])
        self.assertEqual(
            self.request(
                "POST", "/api-keys", auth=member, csrf=False, json={"name": "No CSRF"}
            ).status_code,
            403,
        )
        with self.db.read() as connection:
            row = connection.execute(select(ak.keys)).mappings().one()
            self.assertNotIn(key["secret"], str(dict(row)))
        self.assertEqual(
            self.client.get("/v1/models", headers=self.headers(key)).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                f"/api/v2/tenants/{self.tenant}/api-keys",
                headers={**self.headers(key), "Cookie": ""},
            ).status_code,
            401,
        )

    def test_bad_expired_revoked_keys_and_finance_cannot_create(self):
        key = self.key()
        for token in (
            "",
            "random",
            key["secret"][:-1] + ("x" if key["secret"][-1] != "x" else "y"),
        ):
            response = self.client.get(
                "/v1/models", headers={"Authorization": "Bearer " + token}
            )
            self.assertEqual(response.status_code, 401, response.text)
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(ak.keys)
                .where(ak.keys.c.id == key["id"])
                .values(expires_at="2000-01-01T00:00:00+00:00")
            )
        self.assertEqual(self.chat(key).status_code, 401)
        key = self.key()
        self.keys.revoke(self.fixture.owner, key["id"])
        self.assertEqual(self.chat(key).status_code, 401)
        finance = self.login("finance@fixture.test", PASSWORD)
        self.assertEqual(
            self.request(
                "POST", "/api-keys", auth=finance, json={"name": "No"}
            ).status_code,
            403,
        )

    def test_model_scope_member_revocation_and_foreign_key_management(self):
        second = self.request(
            "POST",
            "/models",
            json={
                "name": "Second",
                "alias": "second",
                "input_price": "1",
                "output_price": "3",
            },
        ).json()
        key = self.key(self.fixture.member, model_ids=[self.model["id"]])
        response = self.client.get("/v1/models", headers=self.headers(key))
        self.assertEqual(
            [m["id"] for m in response.json()["data"]], [self.model["alias"]]
        )
        self.assertEqual(self.chat(key, model=second["alias"]).status_code, 403)
        other = self.fixture.provision_user("other-api@fixture.test")
        with self.assertRaises(PlatformError):
            self.keys.revoke(other, key["id"])
        self.platform.identity.update_member(
            self.fixture.owner,
            self.fixture.member["membership_id"],
            {"status": "revoked"},
        )
        self.assertEqual(self.chat(key).status_code, 401)
        self.assertEqual(self.chat(self.key(), model="foreign-model").status_code, 404)

    def test_both_nonstream_protocols_bill_members_without_new_agents_or_sessions(self):
        key = self.key(self.fixture.member)
        with self.db.read() as connection:
            before = [
                connection.scalar(select(func.count()).select_from(table))
                for table in (t.agents, t.sessions)
            ]
        with self.worker():
            response = self.chat(key)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                "Verified test response",
            )
            responses = self.client.post(
                "/v1/responses",
                headers=self.headers(key),
                json={
                    "model": self.model["alias"],
                    "input": "Hi",
                    "store": False,
                    "max_output_tokens": 128,
                },
            )
            self.assertEqual(responses.status_code, 200, responses.text)
            self.assertEqual(responses.json()["status"], "completed")
            self.assertEqual(responses.json()["usage"]["input_tokens"], 10)
        with self.db.read() as connection:
            after = [
                connection.scalar(select(func.count()).select_from(table))
                for table in (t.agents, t.sessions)
            ]
            self.assertEqual(after, before)
            calls = list(connection.execute(select(t.calls)).mappings())
            self.assertEqual(len(calls), 2)
            self.assertTrue(
                all(
                    c["user_id"] == self.fixture.member["id"]
                    and c["status"] == "confirmed"
                    for c in calls
                )
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(rt.stream_leases)), 0
            )

    def test_streaming_both_protocols_and_client_function_roundtrip(self):
        key = self.key()
        with self.worker():
            chat = self.chat(key, stream=True, stream_options={"include_usage": True})
            self.assertIn("Verified test response", chat.text)
            self.assertIn('"choices":[],"usage"', chat.text)
            self.assertIn("data: [DONE]", chat.text)
            response = self.client.post(
                "/v1/responses",
                headers=self.headers(key),
                json={
                    "model": self.model["alias"],
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Hi"}],
                        }
                    ],
                    "stream": True,
                    "max_output_tokens": 128,
                },
            )
            self.assertIn("event: response.output_text.delta", response.text)
            self.assertIn("event: response.completed", response.text)

        class FunctionGateway:
            async def stream(self, messages, model, max_tokens, temperature, tools):
                self_test.assertEqual(tools[0]["function"]["name"], "calculator")
                yield GatewayEvent(
                    "result",
                    ModelReply(
                        "",
                        [
                            {
                                "id": "c1",
                                "name": "calculator",
                                "args": {"expression": "1+2"},
                            }
                        ],
                        10,
                        5,
                        {"finish_reason": "tool_calls"},
                    ),
                )

        self_test = self
        with self.worker(FunctionGateway()):
            response = self.chat(
                key,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )
            self.assertEqual(
                response.json()["choices"][0]["finish_reason"], "tool_calls"
            )
            self.assertEqual(
                response.json()["choices"][0]["message"]["tool_calls"][0]["id"], "c1"
            )
        self.assertNotIn("calculator", str(self.gateway.calls[0]["messages"]))

    def test_openai_python_sdk_accepts_both_protocols_and_response_event_accumulation(
        self,
    ):
        from openai import OpenAI

        key = self.key()
        sdk = OpenAI(
            api_key=key["secret"],
            base_url="http://testserver/v1",
            http_client=self.client,
            max_retries=0,
        )
        with self.worker():
            self.assertEqual(sdk.models.list().data[0].id, self.model["alias"])
            chat = sdk.chat.completions.create(
                model=self.model["alias"],
                messages=[{"role": "user", "content": "Hi"}],
                max_completion_tokens=128,
            )
            self.assertEqual(chat.choices[0].message.content, "Verified test response")
            response = sdk.responses.create(
                model=self.model["alias"],
                input="Hi",
                store=False,
                max_output_tokens=128,
            )
            self.assertEqual(response.output_text, "Verified test response")
            with sdk.responses.stream(
                model=self.model["alias"],
                input="Hi",
                store=False,
                max_output_tokens=128,
            ) as stream:
                final = stream.get_final_response()
                self.assertEqual(final.output_text, "Verified test response")

    def test_idempotency_stream_limit_and_invalid_requests_never_call_provider(self):
        key = self.key()
        headers = {**self.headers(key), "Idempotency-Key": "request-1"}
        body = {
            "model": self.model["alias"],
            "input": "Hello",
            "max_output_tokens": 128,
        }
        with self.worker():
            first = self.client.post("/v1/responses", headers=headers, json=body)
            second = self.client.post("/v1/responses", headers=headers, json=body)
            self.assertEqual(first.json()["id"], second.json()["id"])
            conflict = self.client.post(
                "/v1/responses", headers=headers, json={**body, "input": "Different"}
            )
            self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.gateway.calls), 1)
        for change in (
            {"n": 2},
            {"unknown": True},
            {"max_tokens": 2000},
            {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
        ):
            self.assertEqual(self.chat(key, **change).status_code, 422)
        rejected = self.client.post(
            "/v1/responses",
            headers=self.headers(key),
            json={**body, "previous_response_id": "resp_other"},
        )
        self.assertEqual(rejected.status_code, 422)
        with self.db.transaction("tenant:" + self.tenant) as connection:
            connection.execute(
                update(nt.entitlements)
                .where(nt.entitlements.c.tenant_id == self.tenant)
                .values(max_sse_connections=0)
            )
            before = connection.scalar(select(func.count()).select_from(t.runs))
        self.assertEqual(self.chat(key).status_code, 429)
        with self.db.read() as connection:
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(t.runs)), before
            )
        self.assertEqual(len(self.gateway.calls), 1)

    def test_multiple_keys_share_member_budget_and_key_count_limit(self):
        keys = [self.key(self.fixture.member) for _ in range(10)]
        with self.assertRaises(PlatformError) as limit:
            self.key(self.fixture.member)
        self.assertEqual(limit.exception.code, "api_key_limit")
        self.platform.billing.set_quota(
            self.tenant, "user", self.fixture.member["id"], None, None, None, "0.000001"
        )
        with self.worker():
            for key in keys[:2]:
                response = self.chat(key)
                self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(self.gateway.calls, [])

    def test_revocation_during_execution_stops_request_and_keeps_unknown_fee(self):
        entered = threading.Event()

        class WaitingGateway:
            async def stream(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
                yield

        key = self.key()
        errors = []

        def revoke():
            if not entered.wait(5):
                errors.append("no model call")
                return
            self.keys.revoke(self.fixture.owner, key["id"])

        revoker = threading.Thread(target=revoke, daemon=True)
        with self.worker(WaitingGateway()):
            revoker.start()
            result = self.chat(key)
            self.assertIn(result.status_code, (401, 403))
        revoker.join(timeout=5)
        self.assertEqual(errors, [])
        with self.db.read() as connection:
            self.assertEqual(connection.scalar(select(t.calls.c.status)), "unresolved")


if __name__ == "__main__":
    unittest.main()
