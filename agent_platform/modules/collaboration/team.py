"""Freeze published members and their contracts before any team can execute."""

from agent_platform.infrastructure.errors import PlatformError
from agent_platform.modules.agent_registry.contracts import AgentDeclaration
from agent_platform.modules.agent_registry.service import AgentRegistry


def freeze_team(platform, connection, user, spec):
    declaration = AgentDeclaration.from_spec(spec)
    if declaration.execution_mode == "single":
        return None
    registry = AgentRegistry(platform)
    team = declaration.collaboration_spec
    members = {}
    for member in team.members:
        resolved = registry.resolve(connection, user, member.agent_id, member.version)
        if resolved["execution_mode"] != "single":
            raise PlatformError(422, "nested_team", "团队成员必须是单 Agent。")
        members[member.key] = {"binding": member.model_dump(), "spec": resolved}
    for task in team.tasks:
        contract = next(
            (
                c
                for c in members[task.member_key]["spec"]["task_contracts"]
                if c["task_type"] == task.task_type
            ),
            None,
        )
        if contract is None:
            raise PlatformError(422, "contract_mismatch", "成员未声明所需任务契约。")
        target = AgentDeclaration.from_spec(members[task.member_key]["spec"])
        target_contract = next(
            c for c in target.task_contracts if c.task_type == task.task_type
        )
        for key in task.dependencies:
            dependency = next(t for t in team.tasks if t.key == key)
            source = AgentDeclaration.from_spec(members[dependency.member_key]["spec"])
            source_contract = next(
                (
                    c
                    for c in source.task_contracts
                    if c.task_type == dependency.task_type
                ),
                None,
            )
            if (
                source_contract is None
                or source_contract.output.digest != target_contract.input.digest
            ):
                raise PlatformError(
                    422, "contract_mismatch", "依赖任务的输出与成员输入契约不匹配。"
                )
    return {"definition": team.model_dump(), "members": members}
