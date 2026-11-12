"""Frontend consumers can discover and use real key and model API contracts."""

import unittest

from fastapi.testclient import TestClient

from agent_platform.apps.api.main import create_app
from agent_platform.apps.api.model_api_responses import (
    ChatCompletion,
    KeyRevoked,
    ManagementErrorResponse,
    ModelApiErrorResponse,
    ModelApiModels,
    ModelApiResponse,
)
from agent_platform.infrastructure.config import Settings
from agent_platform.scripts.api_contract import generate
from agent_platform.tests import test_api as base
from agent_platform.tests import test_member_api_keys as model_tests
from agent_platform.tests.fixtures import PASSWORD


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Settings(database_url="sqlite:///:memory:"))
        self.addCleanup(self.app.state.database.close)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_docs_and_schema_are_available_under_the_frontend_api_proxy(self):
        for path in ("/api/docs", "/api/redoc"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("/api/openapi.json", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
        document = self.client.get("/api/openapi.json")
        self.assertEqual(document.status_code, 200)
        self.assertIn("/v1/chat/completions", document.json()["paths"])
        self.assertEqual(self.client.get("/openapi.json").json(), document.json())
        for old, new in (("/docs", "/api/docs"), ("/redoc", "/api/redoc")):
            response = self.client.get(old, follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], new)

    def test_management_and_model_calls_declare_separate_credentials(self):
        schema = self.app.openapi()
        schemes = schema["components"]["securitySchemes"]
        self.assertEqual(schemes["SessionCookie"]["in"], "cookie")
        self.assertEqual(schemes["CsrfToken"]["in"], "header")
        self.assertEqual(schemes["CsrfToken"]["name"], "X-CSRF-Token")
        self.assertEqual(schemes["MemberApiKey"]["scheme"], "bearer")
        keys = schema["paths"]["/api/v2/tenants/{tenant_id}/api-keys"]
        self.assertEqual(
            keys["post"]["security"], [{"SessionCookie": [], "CsrfToken": []}]
        )
        self.assertEqual(keys["get"]["security"], [{"SessionCookie": []}])
        self.assertEqual(
            schema["paths"]["/v1/models"]["get"]["security"],
            [{"MemberApiKey": []}],
        )

    def test_csrf_is_configured_once_and_applies_only_to_session_writes(self):
        schema = self.app.openapi()
        writes = reads = 0
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                with self.subTest(path=path, method=method):
                    self.assertFalse(
                        any(
                            parameter["in"] == "header"
                            and parameter["name"].lower() == "x-csrf-token"
                            for parameter in operation.get("parameters", [])
                        ),
                        "CSRF must appear in Authorize, not individual parameters",
                    )
                    for requirement in operation.get("security", []):
                        if "SessionCookie" in requirement:
                            if method in {"get", "head", "options"}:
                                reads += 1
                                self.assertNotIn("CsrfToken", requirement)
                            else:
                                writes += 1
                                self.assertEqual(requirement["CsrfToken"], [])
                        else:
                            self.assertNotIn("CsrfToken", requirement)
        self.assertGreater(writes, 0)
        self.assertGreater(reads, 0)
        self.assertNotIn("security", schema["paths"]["/api/v1/auth/login"]["post"])

    def test_manually_bounded_requests_and_both_response_formats_are_documented(self):
        schema = self.app.openapi()
        for path, contract, response in (
            ("/v1/chat/completions", "ChatRequest", "ChatCompletion"),
            ("/v1/responses", "ResponsesRequest", "ModelApiResponse"),
        ):
            with self.subTest(path=path):
                operation = schema["paths"][path]["post"]
                body = operation["requestBody"]
                self.assertTrue(body["required"])
                self.assertEqual(
                    body["content"]["application/json"]["schema"]["$ref"],
                    "#/components/schemas/" + contract,
                )
                content = operation["responses"]["200"]["content"]
                self.assertIn("text/event-stream", content)
                self.assertEqual(
                    content["application/json"]["schema"]["$ref"],
                    "#/components/schemas/" + response,
                )
                self.assertIn("401", operation["responses"])
                self.assertIn("429", operation["responses"])
                self.assertEqual(operation["parameters"][0]["name"], "Idempotency-Key")
        self.assertFalse(
            schema["components"]["schemas"]["ChatRequest"]["additionalProperties"]
        )

        def check_references(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    target = schema
                    for part in value["$ref"].removeprefix("#/").split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    self.assertIsInstance(target, dict)
                for nested in value.values():
                    check_references(nested)
            elif isinstance(value, list):
                for nested in value:
                    check_references(nested)

        check_references(schema)

    def test_frontend_types_include_request_and_response_operation_contracts(self):
        generated = generate()
        for contract in (
            "export type ApiRequestByOperation = {",
            '"POST /v1/chat/completions": ChatRequest;',
            '"POST /v1/responses": ResponsesRequest;',
            '"POST /api/v2/tenants/{tenant_id}/api-keys": KeyCreate;',
            '"DELETE /api/v2/tenants/{tenant_id}/api-keys/{key_id}": KeyRevoked;',
            '"GET /v1/models": ModelApiModels;',
        ):
            self.assertIn(contract, generated)


class ApiKeyFrontendTests(unittest.TestCase):
    setUp = base.APIIntegrationTest.setUp
    tearDown = base.APIIntegrationTest.tearDown
    login = base.APIIntegrationTest.login
    request = base.APIIntegrationTest.request

    def test_global_swagger_configuration_preserves_session_csrf_validation(self):
        for headers in ({}, {"X-CSRF-Token": "incorrect"}):
            denied = self.request(
                "POST", "/api-keys", csrf=False, headers=headers, json={"name": "App"}
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], "csrf_failed")
        self.assertEqual(self.request("GET", "/api-keys", csrf=False).status_code, 200)
        other_session = self.login()
        denied = self.request(
            "POST",
            "/api-keys",
            csrf=False,
            headers={"X-CSRF-Token": other_session["csrf_token"]},
            json={"name": "App"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        created = self.request("POST", "/api-keys", json={"name": "App"})
        self.assertEqual(created.status_code, 201, created.text)
        unauthenticated = self.request(
            "POST",
            "/api-keys",
            auth=None,
            csrf=False,
            headers={"X-CSRF-Token": self.admin["csrf_token"]},
            json={"name": "App"},
        )
        self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)

    def test_administrator_can_list_and_revoke_member_keys_without_secret_access(self):
        member = self.login("member@fixture.test", PASSWORD)
        created = self.request("POST", "/api-keys", auth=member, json={"name": "App"})
        self.assertEqual(created.status_code, 201, created.text)
        key = created.json()
        self.assertEqual(
            self.request("GET", "/api-keys?all_members=true", auth=member).status_code,
            403,
        )
        self.admin = self.login()
        self.assertEqual(self.request("GET", "/api-keys").json(), {"items": []})
        listed = self.request("GET", "/api-keys?all_members=true")
        self.assertEqual([item["id"] for item in listed.json()["items"]], [key["id"]])
        self.assertNotIn(key["secret"], listed.text)
        self.assertNotIn("secret_hash", listed.text)
        for _ in range(2):
            result = self.request("DELETE", "/api-keys/" + key["id"])
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(KeyRevoked.model_validate(result.json()).id, key["id"])
        denied = self.client.get(
            "/v1/models", headers={"Authorization": "Bearer " + key["secret"]}
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["www-authenticate"], "Bearer")
        self.assertEqual(
            ModelApiErrorResponse.model_validate(denied.json()).error.code,
            "invalid_api_key",
        )

    def test_wrong_tenant_and_invalid_create_requests_cannot_create_keys(self):
        for body in (
            {"name": " "},
            {"name": "App", "expires_in_days": 0},
            {"name": "App", "expires_in_days": 366},
            {"name": "App", "expires_in_days": "30"},
            {"name": "App", "unknown": True},
        ):
            with self.subTest(body=body):
                response = self.request("POST", "/api-keys", json=body)
                self.assertEqual(response.status_code, 422, response.text)
                error = ManagementErrorResponse.model_validate(response.json()).error
                self.assertTrue(error.code)
                self.assertTrue(error.message)
        member = self.login("member@fixture.test", PASSWORD)
        denied = self.client.post(
            f"/api/v2/tenants/{self.fixture.tenant_b}/api-keys",
            headers={"X-CSRF-Token": member["csrf_token"]},
            json={"name": "Foreign"},
        )
        self.assertEqual(denied.status_code, 404, denied.text)
        self.assertEqual(
            self.request("GET", "/api-keys", auth=member).json(), {"items": []}
        )


class ModelApiWireTests(unittest.TestCase):
    setUp = model_tests.MemberApiKeyTests.setUp
    tearDown = model_tests.MemberApiKeyTests.tearDown
    login = model_tests.MemberApiKeyTests.login
    request = model_tests.MemberApiKeyTests.request
    create_resources = model_tests.MemberApiKeyTests.create_resources
    key = model_tests.MemberApiKeyTests.key
    headers = staticmethod(model_tests.MemberApiKeyTests.headers)
    worker = model_tests.MemberApiKeyTests.worker
    chat = model_tests.MemberApiKeyTests.chat

    def test_boundary_errors_use_the_documented_model_error_format(self):
        for headers, status, code in (
            ({"Origin": "https://another-origin.test"}, 403, "invalid_origin"),
            ({"Content-Length": "invalid"}, 400, "invalid_length"),
            ({"Content-Length": "262145"}, 413, "body_too_large"),
        ):
            with self.subTest(code=code):
                response = self.client.post(
                    "/v1/responses", headers=headers, content=b"{}"
                )
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(
                    ModelApiErrorResponse.model_validate(response.json()).error.code,
                    code,
                )
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.gateway.calls, [])

    def test_live_json_responses_match_the_frontend_documented_schemas(self):
        key = self.key()
        models = self.client.get("/v1/models", headers=self.headers(key))
        self.assertEqual(models.status_code, 200)
        self.assertEqual(
            ModelApiModels.model_validate(models.json()).data[0].id,
            self.model["alias"],
        )
        with self.worker():
            chat = self.chat(key)
            self.assertEqual(chat.status_code, 200, chat.text)
            self.assertEqual(
                ChatCompletion.model_validate(chat.json()).choices[0].message.content,
                "Verified test response",
            )
            response = self.client.post(
                "/v1/responses",
                headers=self.headers(key),
                json={
                    "model": self.model["alias"],
                    "input": "Hello",
                    "max_output_tokens": 128,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                ModelApiResponse.model_validate(response.json()).status, "completed"
            )


if __name__ == "__main__":
    unittest.main()
