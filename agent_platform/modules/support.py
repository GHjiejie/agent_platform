"""Dedicated support access. No impersonation and no ordinary tenant API bypass."""

import time

from sqlalchemy import func, insert, select, update

from agent_platform.infrastructure import (
    support_tables as st,
)
from agent_platform.infrastructure import (
    tables as t,
)
from agent_platform.infrastructure import (
    tenancy_tables as nt,
)
from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.platform import now, uid


class SupportService:
    def __init__(self, platform):
        self.platform = platform
        self.db = platform.db
        self.identity = platform.identity

    def _audit(
        self, connection, actor, action, target, tenant_id=None, reason="", **details
    ):
        self.identity._audit(
            connection, actor["id"], action, target, tenant_id, reason, details
        )

    def _owner(self, connection, actor):
        current = self.identity.assert_current(connection, actor, "tenant.read")
        if current["tenant_role"] != "owner":
            raise PlatformError(403, "owner_required", "支持访问必须由组织所有者批准。")
        return current

    @staticmethod
    def _lock_users(connection, *user_ids):
        # Support reads and role writes may involve the same two identities.
        # Lock all identities in one order before taking role/grant row locks.
        connection.execute(
            select(t.users.c.id)
            .where(t.users.c.id.in_(set(user_ids)))
            .order_by(t.users.c.id)
            .with_for_update()
        ).all()

    @staticmethod
    def _approval_conditions(grant, approver):
        return (
            nt.memberships.c.id == grant["approver_membership_id"],
            nt.memberships.c.tenant_id == grant["tenant_id"],
            nt.memberships.c.user_id == grant["approved_by"],
            nt.memberships.c.role == "owner",
            nt.memberships.c.status == "active",
            nt.memberships.c.authz_version == grant["approver_membership_version"],
            approver.c.id == grant["approved_by"],
            approver.c.active.is_(True),
            approver.c.global_status == "active",
        )

    @staticmethod
    def _staff(connection, user_id):
        # Locks coordinate with platform role changes until the access audit commits.
        user = connection.execute(
            select(t.users.c.id)
            .where(
                t.users.c.id == user_id,
                t.users.c.active.is_(True),
                t.users.c.global_status == "active",
            )
            .with_for_update()
        ).first()
        role = connection.execute(
            select(nt.platform_roles.c.role)
            .where(
                nt.platform_roles.c.user_id == user_id,
                nt.platform_roles.c.role == "platform_support",
                nt.platform_roles.c.revoked_at.is_(None),
            )
            .with_for_update()
        ).first()
        if not user or not role:
            raise PlatformError(
                403, "support_role_required", "需要当前有效的支持人员身份。"
            )

    def create(self, actor, *, staff_email, reason, minutes=15, allow_content=False):
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            owner = self._owner(connection, actor)
            if owner["tenant_status"] != "active":
                raise PlatformError(
                    403, "tenant_suspended", "仅正常组织可以批准新的支持访问。"
                )
            staff_id = connection.scalar(
                select(t.users.c.id).where(
                    t.users.c.email == staff_email.strip().lower()
                )
            )
            if not staff_id:
                raise PlatformError(
                    404, "support_staff_not_found", "未找到指定支持人员。"
                )
            self._lock_users(connection, actor["id"], staff_id)
            # Recheck after locking: an identity can be disabled outside this tenant.
            owner = self._owner(connection, actor)
            self._staff(connection, staff_id)
            stamp = now()
            grant = {
                "id": uid(),
                "tenant_id": owner["tenant_id"],
                "staff_user_id": staff_id,
                "approved_by": owner["id"],
                "approver_membership_id": owner["membership_id"],
                "approver_membership_version": owner["membership_version"],
                "reason": reason,
                "allow_content": allow_content,
                "created_at": stamp,
                "expires_at": time.time() + minutes * 60,
                "revoked_at": None,
                "revoked_by": None,
            }
            connection.execute(insert(st.support_grants).values(**grant))
            self._audit(
                connection,
                owner,
                "support.approve",
                grant["id"],
                owner["tenant_id"],
                reason,
                grant_id=grant["id"],
                staff_user_id=staff_id,
                allow_content=allow_content,
                expires_at=grant["expires_at"],
            )
            return {**grant, "staff_email": staff_email.strip().lower()}

    def tenant_grants(self, actor):
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            owner = self._owner(connection, actor)
            rows = connection.execute(
                select(st.support_grants, t.users.c.email.label("staff_email"))
                .join(t.users, t.users.c.id == st.support_grants.c.staff_user_id)
                .where(st.support_grants.c.tenant_id == owner["tenant_id"])
                .order_by(st.support_grants.c.created_at.desc())
                .limit(200)
            ).mappings()
            return [dict(row) for row in rows]

    def revoke(self, actor, grant_id):
        with self.db.transaction(f"tenant:{actor['tenant_id']}") as connection:
            owner = self._owner(connection, actor)
            grant = (
                connection.execute(
                    select(st.support_grants)
                    .where(
                        st.support_grants.c.id == grant_id,
                        st.support_grants.c.tenant_id == owner["tenant_id"],
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not grant:
                raise PlatformError(404, "not_found", "支持授权不存在。")
            if grant["revoked_at"]:
                return dict(grant)
            changes = {"revoked_at": now(), "revoked_by": owner["id"]}
            connection.execute(
                update(st.support_grants)
                .where(st.support_grants.c.id == grant_id)
                .values(**changes)
            )
            self._audit(
                connection,
                owner,
                "support.revoke",
                grant_id,
                owner["tenant_id"],
                grant_id=grant_id,
            )
            return {**dict(grant), **changes}

    def my_grants(self, actor):
        with self.db.transaction("support:list") as connection:
            approver_ids = connection.scalars(
                select(st.support_grants.c.approved_by)
                .where(
                    st.support_grants.c.staff_user_id == actor["id"],
                    st.support_grants.c.revoked_at.is_(None),
                    st.support_grants.c.expires_at > time.time(),
                )
                .distinct()
            ).all()
            self._lock_users(connection, actor["id"], *approver_ids)
            self._staff(connection, actor["id"])
            approver = t.users.alias("support_approver")
            rows = list(
                connection.execute(
                    select(st.support_grants, t.tenants.c.name.label("tenant_name"))
                    .join(t.tenants, t.tenants.c.id == st.support_grants.c.tenant_id)
                    .join(
                        nt.tenant_settings,
                        nt.tenant_settings.c.tenant_id == st.support_grants.c.tenant_id,
                    )
                    .join(
                        nt.memberships,
                        nt.memberships.c.id
                        == st.support_grants.c.approver_membership_id,
                    )
                    .join(approver, approver.c.id == nt.memberships.c.user_id)
                    .where(
                        st.support_grants.c.staff_user_id == actor["id"],
                        # A newly approved identity is included on the next request,
                        # after its user row can join the ordered lock set.
                        st.support_grants.c.approved_by.in_(approver_ids),
                        st.support_grants.c.revoked_at.is_(None),
                        st.support_grants.c.expires_at > time.time(),
                        nt.tenant_settings.c.status.in_(["active", "suspended"]),
                        *self._approval_conditions(st.support_grants.c, approver),
                    )
                    .order_by(st.support_grants.c.expires_at)
                    .limit(200)
                    .with_for_update(of=[nt.memberships, st.support_grants])
                ).mappings()
            )
            self._audit(
                connection,
                actor,
                "support.list",
                actor["id"],
                grant_ids=[row["id"] for row in rows],
            )
            return [dict(row) for row in rows]

    def _tenant_for_grant(self, actor, grant_id):
        with self.db.read() as connection:
            tenant_id = connection.scalar(
                select(st.support_grants.c.tenant_id).where(
                    st.support_grants.c.id == grant_id,
                    st.support_grants.c.staff_user_id == actor["id"],
                )
            )
        if not tenant_id:
            raise PlatformError(404, "not_found", "支持授权不存在。")
        return tenant_id

    def _authorized(self, connection, actor, grant_id, tenant_id, *, content=False):
        approver_id = connection.scalar(
            select(st.support_grants.c.approved_by).where(
                st.support_grants.c.id == grant_id,
                st.support_grants.c.tenant_id == tenant_id,
                st.support_grants.c.staff_user_id == actor["id"],
            )
        )
        self._lock_users(
            connection, actor["id"], *([approver_id] if approver_id else [])
        )
        self._staff(connection, actor["id"])
        grant = (
            connection.execute(
                select(st.support_grants)
                .where(
                    st.support_grants.c.id == grant_id,
                    st.support_grants.c.tenant_id == tenant_id,
                    st.support_grants.c.staff_user_id == actor["id"],
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        status = connection.scalar(
            select(nt.tenant_settings.c.status).where(
                nt.tenant_settings.c.tenant_id == tenant_id
            )
        )
        if (
            not grant
            or grant["revoked_at"]
            or grant["expires_at"] <= time.time()
            or status not in {"active", "suspended"}
        ):
            raise PlatformError(
                403, "support_grant_inactive", "支持授权已过期、撤销或组织已关闭。"
            )
        approval = connection.execute(
            select(nt.memberships.c.id)
            .join(t.users, t.users.c.id == nt.memberships.c.user_id)
            .where(*self._approval_conditions(grant, t.users))
            .with_for_update(of=nt.memberships)
        ).first()
        if not approval:
            raise PlatformError(
                403, "support_grant_inactive", "支持授权的审批依据已失效，请重新申请。"
            )
        if content and not grant["allow_content"]:
            raise PlatformError(
                403, "support_content_forbidden", "该授权仅允许查看运行元数据。"
            )
        return grant

    def usage(self, actor, grant_id):
        tenant_id = self._tenant_for_grant(actor, grant_id)
        with (
            self.db.tenant_scope(tenant_id),
            self.db.transaction(f"tenant:{tenant_id}") as connection,
        ):
            grant = self._authorized(connection, actor, grant_id, tenant_id)
            # Select an explicit metadata projection: no prompts, errors, names or provider cost.
            rows = [
                dict(row)
                for row in connection.execute(
                    select(
                        t.runs.c.id,
                        t.runs.c.session_id,
                        t.runs.c.status,
                        t.runs.c.created_at,
                        t.runs.c.finished_at,
                    )
                    .where(t.runs.c.tenant_id == tenant_id)
                    .order_by(t.runs.c.created_at.desc(), t.runs.c.id)
                    .limit(200)
                ).mappings()
            ]
            self._audit(
                connection,
                actor,
                "support.usage_read",
                grant_id,
                tenant_id,
                grant_id=grant_id,
                row_count=len(rows),
                allow_content=grant["allow_content"],
            )
            return {"items": rows, "limit": 200, "tenant_id": tenant_id}

    def session(self, actor, grant_id, session_id):
        tenant_id = self._tenant_for_grant(actor, grant_id)
        with (
            self.db.tenant_scope(tenant_id),
            self.db.transaction(f"tenant:{tenant_id}") as connection,
        ):
            self._authorized(connection, actor, grant_id, tenant_id, content=True)
            session = (
                connection.execute(
                    select(t.sessions.c.id, t.sessions.c.created_at).where(
                        t.sessions.c.tenant_id == tenant_id,
                        t.sessions.c.id == session_id,
                    )
                )
                .mappings()
                .first()
            )
            if not session:
                raise PlatformError(404, "not_found", "会话不存在。")
            messages = list(
                connection.execute(
                    select(
                        t.messages.c.id,
                        t.messages.c.role,
                        t.messages.c.content,
                        t.messages.c.created_at,
                    )
                    .where(
                        t.messages.c.tenant_id == tenant_id,
                        t.messages.c.session_id == session_id,
                    )
                    .order_by(t.messages.c.created_at, t.messages.c.id)
                    .limit(501)
                ).mappings()
            )
            self._audit(
                connection,
                actor,
                "support.content_read",
                session_id,
                tenant_id,
                grant_id=grant_id,
                message_count=min(len(messages), 500),
            )
            return {
                **dict(session),
                "messages": [dict(row) for row in messages[:500]],
                "has_more": len(messages) > 500,
            }

    def identities(self, actor, email):
        with self.db.read() as connection:
            self.platform.require_platform(connection, actor, "platform.roles.manage")
            rows = [
                dict(row)
                for row in connection.execute(
                    select(
                        t.users.c.id,
                        t.users.c.email,
                        t.users.c.name,
                        t.users.c.active,
                        t.users.c.global_status,
                    )
                    .where(t.users.c.email == email.strip().lower())
                    .limit(1)
                ).mappings()
            ]
            for row in rows:
                row["platform_roles"] = list(
                    connection.scalars(
                        select(nt.platform_roles.c.role).where(
                            nt.platform_roles.c.user_id == row["id"],
                            nt.platform_roles.c.revoked_at.is_(None),
                        )
                    )
                )
            return rows

    def set_role(self, actor, user_id, *, role, active, reason):
        # All platform role writes serialize; two admins cannot concurrently remove the last two.
        with self.db.transaction("identity:platform-roles") as connection:
            self._lock_users(connection, actor["id"], user_id)
            self.platform.require_platform(connection, actor, "platform.roles.manage")
            target = (
                connection.execute(
                    select(t.users).where(t.users.c.id == user_id).with_for_update()
                )
                .mappings()
                .first()
            )
            if not target:
                raise PlatformError(404, "not_found", "账号不存在。")
            if active and (not target["active"] or target["global_status"] != "active"):
                raise PlatformError(
                    409, "identity_inactive", "不能给停用账号授予平台角色。"
                )
            previous = (
                connection.execute(
                    select(nt.platform_roles)
                    .where(
                        nt.platform_roles.c.user_id == user_id,
                        nt.platform_roles.c.role == role,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not active
                and role == "platform_admin"
                and previous
                and previous["revoked_at"] is None
            ):
                others = connection.scalar(
                    select(func.count())
                    .select_from(
                        nt.platform_roles.join(
                            t.users, t.users.c.id == nt.platform_roles.c.user_id
                        )
                    )
                    .where(
                        nt.platform_roles.c.role == "platform_admin",
                        nt.platform_roles.c.revoked_at.is_(None),
                        nt.platform_roles.c.user_id != user_id,
                        t.users.c.active.is_(True),
                        t.users.c.global_status == "active",
                    )
                )
                if not others:
                    raise PlatformError(
                        409, "last_platform_admin", "必须保留至少一位有效平台管理员。"
                    )
            stamp = now()
            values = {"revoked_at": None if active else stamp}
            if active:
                values.update(granted_by=actor["id"], granted_at=stamp)
            if previous:
                connection.execute(
                    update(nt.platform_roles)
                    .where(
                        nt.platform_roles.c.user_id == user_id,
                        nt.platform_roles.c.role == role,
                    )
                    .values(**values)
                )
            else:
                connection.execute(
                    insert(nt.platform_roles).values(
                        user_id=user_id,
                        role=role,
                        granted_by=actor["id"],
                        granted_at=stamp,
                        revoked_at=None if active else stamp,
                    )
                )
            if role == "platform_support" and not active:
                # Re-granting the role must not revive past tenant approvals.
                connection.execute(
                    update(st.support_grants)
                    .where(
                        st.support_grants.c.staff_user_id == user_id,
                        st.support_grants.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=stamp, revoked_by=actor["id"])
                )
            self._audit(
                connection,
                actor,
                "platform.role_grant" if active else "platform.role_revoke",
                user_id,
                reason=reason,
                role=role,
            )
            return {"user_id": user_id, "role": role, "active": active}
