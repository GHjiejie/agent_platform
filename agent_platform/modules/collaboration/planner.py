"""Validate plans against immutable run members before persisting any task."""

from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.agent_registry.contracts import AgentDeclaration, TeamSpec


def validate_plan(snapshot, definition=None):
    team = TeamSpec.model_validate(definition or snapshot["definition"])
    frozen = {
        member.key: member
        for member in TeamSpec.model_validate(snapshot["definition"]).members
    }
    if any(
        member.key not in frozen or member != frozen[member.key]
        for member in team.members
    ):
        raise PlatformError(422, "unapproved_member", "计划不能变更已固定的团队成员。")
    for task in team.tasks:
        member = snapshot["members"].get(task.member_key)
        if not member:
            raise PlatformError(422, "unapproved_member", "计划引用了未授权成员。")
        declaration = AgentDeclaration.from_spec(member["spec"])
        contract = next(
            (c for c in declaration.task_contracts if c.task_type == task.task_type),
            None,
        )
        if not contract:
            raise PlatformError(422, "contract_mismatch", "任务类型未获成员声明。")
        for dependency in task.dependencies:
            previous = next(t for t in team.tasks if t.key == dependency)
            source = AgentDeclaration.from_spec(
                snapshot["members"][previous.member_key]["spec"]
            )
            output = next(
                (
                    c.output
                    for c in source.task_contracts
                    if c.task_type == previous.task_type
                ),
                None,
            )
            if output is None or output.digest != contract.input.digest:
                raise PlatformError(
                    422, "contract_mismatch", "任务依赖的输入输出契约不兼容。"
                )
    return team
