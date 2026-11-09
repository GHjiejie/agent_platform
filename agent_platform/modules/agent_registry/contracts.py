"""Versioned, bounded declarations shared by registration and orchestration."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_platform.modules.runtime.contracts import _schema, canonical_digest

Identifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
]


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataContract(Declaration):
    name: Identifier
    version: int = Field(ge=1, le=1000000)
    json_schema: dict

    @model_validator(mode="after")
    def validate_schema(self):
        _schema(self.json_schema)
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump())


class TaskContract(Declaration):
    task_type: Identifier
    input: DataContract
    output: DataContract


class TeamMember(Declaration):
    key: Identifier
    agent_id: Identifier
    version: int = Field(ge=1)
    responsibility: str = Field(min_length=1, max_length=2000)
    priority: int = Field(default=0, ge=0, le=1000)


class TaskTemplate(Declaration):
    key: Identifier
    member_key: Identifier
    task_type: Identifier
    objective: str = Field(min_length=1, max_length=4000)
    dependencies: list[Identifier] = Field(default_factory=list, max_length=8)
    required: bool = True


class TeamSpec(Declaration):
    members: list[TeamMember] = Field(min_length=1, max_length=8)
    tasks: list[TaskTemplate] = Field(default_factory=list, max_length=8)
    planning: Literal["template", "dynamic"] = "template"
    max_parallel: int = Field(default=1, ge=1, le=2)
    max_model_calls: int = Field(default=20, ge=2, le=50)
    max_tasks: int = Field(default=8, ge=1, le=8)
    allow_partial: bool = False
    summary_reserve_fraction: float = Field(default=0.15, ge=0.05, le=0.5)

    @model_validator(mode="after")
    def validate_plan(self):
        members = {m.key for m in self.members}
        tasks = {t.key: t for t in self.tasks}
        if len(members) != len(self.members) or len(tasks) != len(self.tasks):
            raise ValueError("成员和任务标识不能重复。")
        if len(self.tasks) > self.max_tasks:
            raise ValueError("任务数量超过团队限制。")
        if self.planning == "template" and not tasks:
            raise ValueError("固定模板至少包含一个任务。")
        for task in self.tasks:
            if task.member_key not in members:
                raise ValueError("任务引用了未声明的成员。")
            if len(set(task.dependencies)) != len(task.dependencies):
                raise ValueError("任务依赖不能重复。")
            if set(task.dependencies) - tasks.keys():
                raise ValueError("任务依赖不存在。")
        visited, active = set(), set()

        def visit(key):
            if key in active:
                raise ValueError("任务依赖不能形成循环。")
            if key in visited:
                return
            active.add(key)
            for dependency in tasks[key].dependencies:
                visit(dependency)
            active.remove(key)
            visited.add(key)

        for key in tasks:
            visit(key)
        return self


class AgentDeclaration(Declaration):
    execution_mode: Literal["single", "team"] = "single"
    capability_keys: list[Identifier] = Field(default_factory=list, max_length=32)
    task_contracts: list[TaskContract] = Field(default_factory=list, max_length=16)
    collaboration_spec: TeamSpec | None = None

    @model_validator(mode="after")
    def validate_declaration(self):
        if len(set(self.capability_keys)) != len(self.capability_keys):
            raise ValueError("能力标识不能重复。")
        kinds = [contract.task_type for contract in self.task_contracts]
        if len(set(kinds)) != len(kinds):
            raise ValueError("任务类型不能重复。")
        if (self.execution_mode == "team") != (self.collaboration_spec is not None):
            raise ValueError("协作模式必须包含团队配置；单 Agent 不能包含团队配置。")
        return self

    @classmethod
    def from_spec(cls, spec: dict) -> AgentDeclaration:
        return cls.model_validate(
            {key: spec[key] for key in cls.model_fields if key in spec}
        )
