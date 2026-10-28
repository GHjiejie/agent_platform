"""Explicit capabilities; organization roles never confer platform authority."""

from agent_platform.infrastructure.errors import PlatformError

MEMBER_CAPABILITIES = frozenset(
    {
        "tenant.read",
        "agents.read",
        "models.read",
        "runs.execute",
        "runs.read_own",
        "billing.read_own",
        "exports.create",
    }
)
TENANT_ROLE_CAPABILITIES = {
    "member": MEMBER_CAPABILITIES,
    "finance_viewer": frozenset(
        {"tenant.read", "billing.read", "usage.read_all", "exports.create"}
    ),
    "tenant_admin": MEMBER_CAPABILITIES
    | {
        "members.read",
        "members.manage",
        "agents.manage",
        "models.policy",
        "billing.read",
        "usage.read_all",
        "quotas.manage",
        "audit.read",
    },
}
TENANT_ROLE_CAPABILITIES["owner"] = TENANT_ROLE_CAPABILITIES["tenant_admin"] | {
    "ownership.manage",
    "tenant.close",
}
PLATFORM_ROLE_CAPABILITIES = {
    "platform_admin": frozenset(
        {
            "platform.tenants.manage",
            "platform.entitlements.manage",
            "platform.models.manage",
            "platform.pricing.manage",
            "platform.gateway.manage",
            "platform.roles.manage",
            "platform.audit.read",
        }
    ),
    "platform_finance": frozenset(
        {"platform.billing.manage", "platform.costs.read", "platform.audit.read"}
    ),
    # A support role alone is deliberately insufficient to read tenant content.
    "platform_support": frozenset({"platform.support.request"}),
}
READ_CAPABILITIES = frozenset(
    {
        "tenant.read",
        "members.read",
        "agents.read",
        "models.read",
        "runs.read_own",
        "billing.read",
        "billing.read_own",
        "usage.read_all",
        "audit.read",
        "exports.create",
    }
)


def capabilities(tenant_role: str | None, platform_roles=()) -> frozenset[str]:
    result = set(TENANT_ROLE_CAPABILITIES.get(tenant_role, ()))
    for role in platform_roles:
        result.update(PLATFORM_ROLE_CAPABILITIES.get(role, ()))
    return frozenset(result)


def require_capability(context: dict, capability: str) -> None:
    if capability not in context.get("capabilities", ()):
        raise PlatformError(403, "capability_required", "当前身份无权执行此操作。")


def can_read_content(context: dict, owner_user_id: str) -> bool:
    """First release: private conversations, including for tenant operators."""
    return context.get("id") == owner_user_id and "runs.read_own" in context.get(
        "capabilities", ()
    )
