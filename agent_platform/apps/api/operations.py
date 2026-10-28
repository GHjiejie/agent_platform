"""Explicit tenant export routes and platform-only tenant closure."""
# ruff: noqa: B008

from typing import Literal

from fastapi import Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_platform.modules.operations import OperationsService


class ExportRequest(BaseModel):
    kind: Literal["calls", "runs"] = "calls"
    scope: Literal["self", "tenant"] = "self"


class CloseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


def register(app, platform, principal, auth):
    operations = OperationsService(platform)
    db = platform.db

    async def export_user(tenant_id: str, actor=Depends(principal)):
        # Closing/suspended organizations retain authorized metadata exports.
        # CSRF remains enforced by the principal dependency for every mutation.
        with db.tenant_scope(tenant_id):
            user = platform.identity.context(
                actor["id"], tenant_id, allow_suspended=True
            )
            yield user

    @app.post("/api/v2/tenants/{tenant_id}/exports", status_code=202)
    def export_create(
        body: ExportRequest,
        user=Depends(export_user),
        idempotency_key: str | None = Header(default=None),
    ):
        return operations.request_export(
            user, body.kind, body.scope, idempotency_key=idempotency_key
        )

    @app.get("/api/v2/tenants/{tenant_id}/exports")
    def exports(user=Depends(export_user)):
        return {"items": operations.list_exports(user)}

    @app.get("/api/v2/tenants/{tenant_id}/exports/{job_id}/download")
    def download(job_id: str, user=Depends(export_user)):
        result = operations.download(user, job_id)
        return StreamingResponse(
            result["chunks"],
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="'
                + result["filename"]
                + '"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/v2/platform/tenants/{tenant_id}/close", status_code=202)
    def close(tenant_id: str, body: CloseRequest, actor=Depends(principal)):
        return operations.close_tenant(actor, tenant_id, body.reason)

    @app.get("/api/v2/platform/tenants/{tenant_id}/closure")
    def closure(tenant_id: str, actor=Depends(principal)):
        return operations.lifecycle_status(actor, tenant_id)
