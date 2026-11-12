"""Same-origin API documentation, including bounded manually parsed requests."""

from fastapi.responses import JSONResponse, RedirectResponse
from pydantic.json_schema import models_json_schema

from agent_platform.modules.model_api_contracts import ChatRequest, ResponsesRequest


def register(app):
    generate = app.openapi

    def openapi():
        if app.openapi_schema is None:
            document = generate()
            # Runtime dependencies are cumulative. Cookie-authenticated writes
            # require both credentials; reads need only the session cookie.
            for methods in document["paths"].values():
                for method, operation in methods.items():
                    if not any(
                        "SessionCookie" in requirement
                        for requirement in operation.get("security", [])
                    ):
                        continue
                    requirement = {"SessionCookie": []}
                    if method not in {"get", "head", "options"}:
                        requirement["CsrfToken"] = []
                    operation["security"] = [requirement]
            # These bodies are parsed incrementally by the route so chunked
            # requests have the same size limit as requests with Content-Length.
            contracts = (ChatRequest, ResponsesRequest)
            _, definitions = models_json_schema(
                [(contract, "validation") for contract in contracts],
                ref_template="#/components/schemas/{model}",
            )
            document["components"]["schemas"].update(definitions["$defs"])
            for path, contract in zip(
                ("/v1/chat/completions", "/v1/responses"), contracts, strict=True
            ):
                operation = document["paths"][path]["post"]
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": f"#/components/schemas/{contract.__name__}"
                            }
                        }
                    },
                }
                operation["parameters"] = [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "description": (
                            "可选的密钥内幂等标识；相同请求完成后复用原结果，"
                            "参数冲突或尚在执行时返回 409。"
                        ),
                        "schema": {"type": "string", "minLength": 1, "maxLength": 160},
                    }
                ]
            app.openapi_schema = document
        return app.openapi_schema

    app.openapi = openapi

    @app.get("/docs", include_in_schema=False)
    def legacy_docs():
        return RedirectResponse("/api/docs")

    @app.get("/redoc", include_in_schema=False)
    def legacy_redoc():
        return RedirectResponse("/api/redoc")

    @app.get("/openapi.json", include_in_schema=False)
    def legacy_openapi():
        return JSONResponse(app.openapi())
