"""The workflow contract: one repeatable business process, with evidence.

A customer does not buy tools; they buy "the Monday report exists by 9am". A workflow is that
promise in code: it takes the customer's own folders and accounts, does the steps, and returns a
result that says exactly what it touched and whether the outcome was verified. A workflow never
claims success because its steps ran — every one of them checks the artifact it produced.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

DONE = "done"
PARTIAL = "partial"
FAILED = "failed"

OK = "ok"
SKIPPED = "skipped"


class WorkflowError(RuntimeError):
    """The workflow cannot start or cannot continue. The message is for the customer."""


@dataclass(slots=True)
class Step:
    name: str
    status: str = OK
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkflowResult:
    workflow: str
    status: str
    summary: str
    steps: list[Step] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    problems: list[str] = field(default_factory=list)

    def finish(self, status: str, summary: str) -> "WorkflowResult":
        self.status = status
        self.summary = summary
        self.finished = time.time()
        return self

    def step(self, name: str, status: str = OK, detail: str = "", artifacts: list[str] | None = None) -> Step:
        entry = Step(name=name, status=status, detail=detail, artifacts=list(artifacts or []))
        self.steps.append(entry)
        self.artifacts.extend(entry.artifacts)
        return entry

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["elapsed_s"] = round((self.finished or time.time()) - self.started, 2)
        data["steps"] = [step.to_json() for step in self.steps]
        return data


class Workflow:
    """Base class. ``run`` must be safe to call twice on the same input."""

    name = ""
    title = ""
    description = ""
    #: What the customer must supply before it can run, in their words.
    needs = ""

    def run(self, **params: Any) -> WorkflowResult:
        raise NotImplementedError