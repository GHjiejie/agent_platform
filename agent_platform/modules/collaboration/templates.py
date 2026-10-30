"""Explicitly installed, versioned sequential example; no fake source observations."""

from sqlalchemy import select

from agent_platform.infrastructure import tables as t
from agent_platform.modules.agent_registry.contracts import AgentDeclaration

TEMPLATE_KEY = "collaboration_analysis_v1"
TEXT = {
    "name": "request",
    "version": 1,
    "json_schema": {"type": "string", "maxLength": 32000},
}
DATA = {
    "name": "observations",
    "version": 1,
    "json_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {"type": "string", "maxLength": 2000},
                "maxItems": 50,
            },
            "missing": {
                "type": "array",
                "items": {"type": "string", "maxLength": 500},
                "maxItems": 20,
            },
        },
        "required": ["facts", "missing"],
        "additionalProperties": False,
    },
}


def install(platform, user):
    # The same tenant lock covers quota checks, creation, publication and retry.
    # A failed installation rolls back all four agents and their audit records.
    with platform.db.transaction(f"tenant:{user['tenant_id']}") as connection:
        platform.require_current(connection, user, admin=True)
        existing = connection.scalar(
            select(t.agents.c.id).where(
                t.agents.c.tenant_id == user["tenant_id"],
                t.agents.c.builtin_key == TEMPLATE_KEY,
            )
        )
        if existing:
            return {"id": existing, "created": False}
        common = {
            "description": "顺序协作模板：仅基于用户提供的数据，缺少信息时明确说明。",
            "model_id": None,
            "temperature": 1,
            "max_steps": 4,
            "max_tokens": 2048,
            "tools": [],
            "execution_policy": {},
        }
        members, tasks = [], []
        for key, name, objective, tools in (
            (
                "normalize",
                "数据整理成员",
                "从用户输入提取事实和缺失信息，禁止补造数据。",
                [],
            ),
            (
                "analyze",
                "数据分析成员",
                "分析已确认事实，写出依据与局限，保留缺失信息。",
                [],
            ),
            (
                "chart",
                "图表成员",
                "基于已确认分析调用界面工具生成图表或信息缺失提示，再返回结构化成果。",
                ["generate_sandboxed_ui"],
            ),
        ):
            declaration = AgentDeclaration.model_validate(
                {
                    "capability_keys": [key],
                    "task_contracts": [
                        {
                            "task_type": key,
                            "input": TEXT if not members else DATA,
                            "output": DATA,
                        }
                    ],
                }
            )
            agent = platform._save_agent(
                connection,
                user,
                {
                    **common,
                    **declaration.model_dump(),
                    "name": name,
                    "system_prompt": objective,
                    "tools": tools,
                },
            )
            published = platform._publish(connection, user, agent["id"])
            members.append(
                {
                    "key": key,
                    "agent_id": agent["id"],
                    "version": published["version"],
                    "responsibility": objective,
                }
            )
            tasks.append(
                {
                    "key": key,
                    "member_key": key,
                    "task_type": key,
                    "objective": objective,
                    "dependencies": [tasks[-1]["key"]] if tasks else [],
                    "required": True,
                }
            )
        declaration = AgentDeclaration.model_validate(
            {
                "execution_mode": "team",
                "collaboration_spec": {"members": members, "tasks": tasks},
            }
        )
        root = platform._save_agent(
            connection,
            user,
            {
                **common,
                **declaration.model_dump(),
                "name": "整理分析图表团队",
                "builtin_key": TEMPLATE_KEY,
                "system_prompt": "根据已确认的任务成果给出统一答案。引用事实和缺失信息，不得把示例当成真实业务结果。不重复输出界面代码。",
            },
        )
        platform._publish(connection, user, root["id"])
        return {"id": root["id"], "created": True}
