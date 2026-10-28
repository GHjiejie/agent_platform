"""Current global identity/Membership fixtures backed by real temporary storage.

Callers own the Database lifecycle. No helper reads the deployment database URL,
starts an embedded worker, enrolls gateway credentials or sends model requests.
"""

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.config import Settings
from agent_platform.infrastructure.db import Database
from agent_platform.modules.platform import Platform, hasher, now, uid
from agent_platform.modules.support import SupportService

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "temporary-platform-fixture-password"


def migrate(url: str) -> None:
    """Migrate only the caller's explicitly supplied, isolated database."""
    config = Config(str(ROOT / "agent_platform/alembic.ini"))
    with patch.dict(os.environ, {"PLATFORM_DATABASE_URL": url}):
        command.upgrade(config, "head")


class MultiTenantFixture:
    def __init__(self, db: Database, settings: Settings, *, bootstrap=True):
        self.db = db
        self.settings = replace(
            settings,
            admin_password=settings.admin_password or PASSWORD,
            operator_emails=tuple(
                set(settings.operator_emails) | {settings.admin_email.strip().lower()}
            ),
            litellm_url=settings.litellm_url or "http://127.0.0.1:4000/v1",
        )
        self.platform = Platform(db, self.settings)
        if bootstrap:
            self.platform.bootstrap()
        with db.read() as connection:
            owner_id = connection.scalar(
                select(t.users.c.id).where(
                    t.users.c.email == self.settings.admin_email.strip().lower()
                )
            )
        self.principal = self.platform.identity.principal(owner_id)
        self.tenant_id = self.principal["memberships"][0]["tenant_id"]
        self.owner = self.context(owner_id)
        self.tenant_b = self.platform.identity.create_tenant(
            self.principal,
            name="Fixture tenant B",
            owner_user_id=owner_id,
            idempotency_key="fixture-tenant-b",
        )["id"]
        self.member = self.provision_user("member@fixture.test")
        self.finance = self.provision_user(
            "finance@fixture.test", role="finance_viewer"
        )
        self.support = self.provision_user(
            "support@fixture.test", role="platform_support"
        )
        self._deployment_id = None

    def context(self, user_id: str, tenant_id: str | None = None) -> dict:
        return self.platform.identity.context(user_id, tenant_id or self.tenant_id)

    def provision_user(
        self, email: str, role: str | None = "member", tenant_id: str | None = None
    ) -> dict:
        email = email.strip().lower()
        with self.db.transaction("identity:fixture-provision") as connection:
            user_id = connection.scalar(
                select(t.users.c.id).where(t.users.c.email == email)
            )
            if not user_id:
                user_id = uid()
                connection.execute(
                    insert(t.users).values(
                        id=user_id,
                        tenant_id=self.tenant_id,
                        email=email,
                        name=email.split("@")[0],
                        password_hash=hasher.hash(PASSWORD),
                        role="member",
                        active=True,
                        global_status="active",
                        auth_version=1,
                        created_at=now(),
                    )
                )
        principal = self.platform.identity.principal(user_id)
        if role and role.startswith("platform_"):
            SupportService(self.platform).set_role(
                self.principal,
                user_id,
                role=role,
                active=True,
                reason="Explicit temporary fixture role",
            )
            return self.platform.identity.principal(user_id)
        if role:
            chosen_tenant = tenant_id or self.tenant_id
            invitation = self.platform.identity.create_invitation(
                self.context(self.owner["id"], chosen_tenant), email=email, role=role
            )
            self.platform.identity.accept_invitation(principal, invitation["token"])
            return self.context(user_id, chosen_tenant)
        return principal

    def create_model(
        self, tenant_id: str | None = None, alias="offline-model", **values
    ) -> dict:
        if self._deployment_id is None:
            deployment = self.platform.gateway.create_deployment(
                {
                    "name": "Fixture deployment",
                    "internal_route": "fixture-offline-route",
                    "base_url": self.settings.litellm_url,
                    "capabilities": {"text": True, "tools": True},
                },
                actor_id=self.owner["id"],
            )
            self._deployment_id = deployment["id"]
        return self.platform.gateway.grant_model(
            tenant_id or self.tenant_id,
            self._deployment_id,
            {
                "name": "Offline model",
                "alias": alias,
                "input_price": "1",
                "output_price": "2",
                "context_window": 32000,
                "max_output_tokens": 256,
                **values,
            },
            actor_id=self.owner["id"],
        )

    def create_session(self, actor: dict | None = None) -> dict:
        actor = actor or self.owner
        with self.db.tenant_scope(actor["tenant_id"]), self.db.read() as connection:
            agent_id = connection.scalar(
                select(t.agents.c.id)
                .where(t.agents.c.tenant_id == actor["tenant_id"])
                .order_by(t.agents.c.id)
                .limit(1)
            )
        return self.platform.create_session(actor, agent_id, "Fixture private session")

    def create_run(
        self, actor: dict, session: dict, content="Fixture private content"
    ) -> dict:
        run = {
            "id": uid(),
            "tenant_id": actor["tenant_id"],
            "user_id": actor["id"],
            "session_id": session["id"],
            "created_at": now(),
            "finished_at": now(),
            "agent_name": "Fixture completed run",
            "spec": {},
            "message": content,
            "status": "succeeded",
            "idempotency_key": uid(),
            "request_hash": uid(),
        }
        with self.db.transaction("tenant:" + actor["tenant_id"]) as connection:
            connection.execute(insert(t.runs).values(**run))
            connection.execute(
                insert(t.messages).values(
                    id=uid(),
                    tenant_id=actor["tenant_id"],
                    session_id=session["id"],
                    run_id=run["id"],
                    role="user",
                    content=content,
                    created_at=now(),
                )
            )
        return run
