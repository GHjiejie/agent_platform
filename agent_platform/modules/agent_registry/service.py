"""Published capability discovery never grants authority or changes pinned versions."""

from sqlalchemy import select, update

from agent_platform.infrastructure import tables as t
from agent_platform.infrastructure.errors import PlatformError

from .contracts import AgentDeclaration


class AgentRegistry:
    def __init__(self, platform):
        self.platform = platform
        self.db = platform.db

    def resolve(self, connection, user, agent_id, version=None):
        self.platform.identity.assert_current(connection, user, "agents.read")
        row = (
            connection.execute(
                select(t.agents).where(
                    t.agents.c.tenant_id == user["tenant_id"],
                    t.agents.c.id == agent_id,
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise PlatformError(404, "no_eligible_agent", "没有可用且获授权的 Agent。")
        if not row["available"]:
            raise PlatformError(409, "agent_unavailable", "Agent 已停用。")
        published = (
            connection.execute(
                select(t.agent_versions).where(
                    t.agent_versions.c.tenant_id == user["tenant_id"],
                    t.agent_versions.c.agent_id == agent_id,
                    t.agent_versions.c.version
                    == (version if version is not None else row["published_version"]),
                )
            )
            .mappings()
            .first()
        )
        if not published:
            raise PlatformError(
                400, "agent_not_published", "指定的 Agent 版本尚未发布。"
            )
        spec = dict(published["spec"])
        spec.update(AgentDeclaration.from_spec(spec).model_dump())
        if spec.get("remote_ref"):
            from agent_platform.modules.remote_agents.registry import RemoteRegistry

            RemoteRegistry(self.platform).eligible(
                connection, user["tenant_id"], spec["remote_ref"]
            )
        return {**spec, "id": agent_id, "version": published["version"]}

    def discover(
        self,
        user,
        *,
        capability=None,
        task_type=None,
        output_digest=None,
        limit=50,
        cursor=None,
        candidates=None,
    ):
        """Candidates, when supplied, must come from a server-owned run snapshot."""
        if not 1 <= limit <= 100:
            raise PlatformError(422, "invalid_page_size", "每页最多 100 个候选。")
        with self.db.tenant_scope(user["tenant_id"]), self.db.read() as connection:
            self.platform.identity.assert_current(connection, user, "agents.read")
            query = (
                select(t.agents.c.id)
                .where(
                    t.agents.c.tenant_id == user["tenant_id"],
                    t.agents.c.available.is_(True),
                    t.agents.c.published_version > 0,
                )
                .order_by(t.agents.c.id)
            )
            if candidates is not None:
                query = query.where(t.agents.c.id.in_(candidates))
            if cursor:
                query = query.where(t.agents.c.id > cursor)
            # Bound work as well as page size. A page can be empty with a cursor.
            ids = list(connection.scalars(query.limit(201)))
            items, scanned = [], None
            for identifier in ids[:200]:
                scanned = identifier
                try:
                    spec = self.resolve(
                        connection,
                        user,
                        identifier,
                        candidates[identifier] if candidates is not None else None,
                    )
                except PlatformError as exc:
                    if exc.code in {
                        "remote_unavailable",
                        "remote_agents_disabled",
                        "remote_not_found",
                        "agent_unavailable",
                    }:
                        continue
                    raise
                declaration = AgentDeclaration.from_spec(spec)
                if declaration.execution_mode != "single":
                    continue
                if capability and capability not in declaration.capability_keys:
                    continue
                contracts = [
                    c
                    for c in declaration.task_contracts
                    if not task_type or c.task_type == task_type
                ]
                if task_type and not contracts:
                    continue
                if output_digest and not any(
                    c.output.digest == output_digest for c in contracts
                ):
                    continue
                items.append(
                    {
                        "id": identifier,
                        "version": spec["version"],
                        "name": spec["name"],
                        "description": spec["description"],
                        "capability_keys": declaration.capability_keys,
                        "task_contracts": [c.model_dump() for c in contracts],
                        "contract_digests": {
                            c.task_type: {
                                "input": c.input.digest,
                                "output": c.output.digest,
                            }
                            for c in contracts
                        },
                    }
                )
                if len(items) == limit:
                    break
            more = bool(scanned and (len(ids) > 200 or scanned != ids[-1]))
            return {"items": items, "next_cursor": scanned if more else None}

    def set_availability(self, user, agent_id, available):
        from agent_platform.modules.platform import audit

        with self.db.transaction(f"tenant:{user['tenant_id']}") as connection:
            self.platform.identity.assert_current(connection, user, "agents.manage")
            self.platform.owned(connection, t.agents, user, agent_id)
            connection.execute(
                update(t.agents)
                .where(
                    t.agents.c.tenant_id == user["tenant_id"],
                    t.agents.c.id == agent_id,
                )
                .values(available=available)
            )
            audit(
                connection,
                user,
                "agent.enable" if available else "agent.disable",
                agent_id,
            )
        return {"id": agent_id, "available": available}
