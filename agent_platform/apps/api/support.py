"""Support routes deliberately depend on global identity, never impersonation."""

# ruff: noqa: B008
from fastapi import Depends, Query

from agent_platform.apps.api import schemas as s
from agent_platform.modules.support import SupportService


def register(app, platform, principal, auth):
    service = SupportService(platform)

    def owner_context(actor, tenant_id):
        return platform.identity.context(actor["id"], tenant_id, allow_suspended=True)

    @app.get("/api/v2/tenants/{tenant_id}/support-grants")
    def tenant_grants(tenant_id: str, actor=Depends(principal)):
        return {"items": service.tenant_grants(owner_context(actor, tenant_id))}

    @app.post("/api/v2/tenants/{tenant_id}/support-grants", status_code=201)
    def approve(body: s.SupportGrantCreate, user=Depends(auth)):
        return service.create(user, **body.model_dump())

    @app.delete("/api/v2/tenants/{tenant_id}/support-grants/{grant_id}")
    def revoke(tenant_id: str, grant_id: str, actor=Depends(principal)):
        return service.revoke(owner_context(actor, tenant_id), grant_id)

    @app.get("/api/v2/support-grants")
    def my_grants(actor=Depends(principal)):
        return {"items": service.my_grants(actor)}

    @app.get("/api/v2/support-grants/{grant_id}/usage")
    def usage(grant_id: str, actor=Depends(principal)):
        return service.usage(actor, grant_id)

    @app.get("/api/v2/support-grants/{grant_id}/sessions/{session_id}")
    def content(grant_id: str, session_id: str, actor=Depends(principal)):
        return service.session(actor, grant_id, session_id)

    @app.get("/api/v2/platform/users")
    def identities(
        email: str = Query(min_length=3, max_length=254), actor=Depends(principal)
    ):
        return {"items": service.identities(actor, email)}

    @app.post("/api/v2/platform/users/{user_id}/roles")
    def set_role(user_id: str, body: s.PlatformRoleChange, actor=Depends(principal)):
        return service.set_role(actor, user_id, **body.model_dump())
