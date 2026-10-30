"""Immutable execution identity, separated from the root run's control state."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    tenant_id: str
    user_id: str
    session_id: str
    root_run_id: str
    agent_id: str | None
    agent_version: int
    task_id: str | None
    execution_id: str | None
    attempt: int
    owner: str | None
    fence: int
    deadline: float | None
    _root_json: str
    _spec_json: str

    @classmethod
    def from_run(cls, run, *, spec=None, task_id=None, execution_id=None, attempt=1):
        effective = spec if spec is not None else run["spec"]
        return cls(
            run["tenant_id"],
            run["user_id"],
            run["session_id"],
            run["id"],
            effective.get("id"),
            effective.get("version", 1),
            task_id,
            execution_id,
            attempt,
            run.get("owner"),
            run.get("fence", 0),
            run.get("deadline"),
            json.dumps(run, default=str),
            json.dumps(effective, default=str),
        )

    @property
    def spec(self):
        return json.loads(self._spec_json)

    @property
    def root(self):
        return json.loads(self._root_json)

    def invocation_run(self):
        root = self.root
        return {
            **root,
            "spec": self.spec,
            "root_spec": root["spec"],
            "agent_name": self.spec.get("name", root.get("agent_name", "")),
            "task_id": self.task_id,
            "execution_id": self.execution_id,
        }
