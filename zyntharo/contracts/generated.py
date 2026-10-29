"""Generated from contracts/*.schema.json by tools/contracts.py. Do not edit.

Every contract is a dataclass plus the constants that name it. The runtime imports
these to build records whose field names cannot drift from the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any



@dataclass
class PluginManifest:
    """Plugin manifest (contracts/interfaces.schema.json#/$defs/plugin-manifest)."""

    name: str = ""
    version: str = ""
    description: str | None = None
    author: str | None = None
    entry: str = ""
    kind: str | None = None
    capabilities: list[Any] = field(default_factory=list)
    actions: list[Any] | None = None
    depends_on: list[Any] | None = None
    created_at: float | None = None
    schema_version: int = 0
    source: str | None = None
    task: str | None = None
    runtime: str | None = None
    contracts: dict[str, Any] | None = None

    CONTRACT = 'plugin-manifest'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class NativeRpcRequest:
    """Native helper RPC request (contracts/interfaces.schema.json#/$defs/native-rpc-request)."""

    id: str = ""
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    deadline_ms: int | None = None
    requires_uninjected: bool | None = None

    CONTRACT = 'native-rpc-request'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NativeRpcRequest":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class NativeRpcResponse:
    """Native helper RPC response (contracts/interfaces.schema.json#/$defs/native-rpc-response)."""

    id: str = ""
    ok: bool = False
    result: str | None = None
    error: str | None = None
    channel: str | None = None
    observed: dict[str, Any] | None = None
    duration_ms: int | None = None

    CONTRACT = 'native-rpc-response'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NativeRpcResponse":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Receipt:
    """Evidence receipt (spec 4.2) (contracts/records.schema.json#/$defs/receipt)."""

    receipt_id: str = ""
    tool: str = ""
    agent_id: str | None = None
    capability: str | None = None
    group: str | None = None
    risk_tier: str | None = None
    args: dict[str, Any] | None = None
    target: str | None = None
    rung: int = 0
    rung_name: str | None = None
    method: str | None = None
    channels: list[Any] | None = None
    started_at: float = 0.0
    ended_at: float | None = None
    duration: float | None = None
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    outcome: str = ""
    verified: bool | None = None
    error: str | None = None
    output_digest: str | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    notes: str | None = None

    CONTRACT = 'receipt'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Receipt":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class JournalEntry:
    """Action journal entry (spec 4.5) (contracts/records.schema.json#/$defs/journal-entry)."""

    entry_id: int = 0
    task_id: str | None = None
    agent_id: str | None = None
    action: str = ""
    capability: str | None = None
    target: str | None = None
    risk: int | None = None
    mode: str | None = None
    reversible: bool | None = None
    idempotency_key: str | None = None
    state: str = ""
    outcome: str | None = None
    error: str | None = None
    notes: str | None = None
    started_at: float = 0.0
    ended_at: float | None | None = None
    evidence: dict[str, Any] | None = None

    CONTRACT = 'journal-entry'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JournalEntry":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Criterion:
    """Success criterion (spec 4.8) (contracts/records.schema.json#/$defs/criterion)."""

    kind: str = ""
    target: str | None = None
    expected: str | None = None
    note: str | None = None
    weight: float | None = None

    CONTRACT = 'criterion'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Criterion":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class TaskContract:
    """Task contract (spec 4.8) (contracts/records.schema.json#/$defs/task-contract)."""

    task_id: str = ""
    request: str = ""
    criteria: list[Any] = field(default_factory=list)
    intended_capabilities: list[Any] | None = None
    requested_by: str | None = None
    created_at: float = 0.0

    CONTRACT = 'task-contract'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskContract":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Event:
    """Bus event (spec 3.6) (contracts/records.schema.json#/$defs/event)."""

    topic: str = ""
    payload: dict[str, Any] | None = None
    source: str | None = None
    severity: str = ""
    ts: float = 0.0
    ts_iso: str | None = None
    id: str = ""

    CONTRACT = 'event'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Tool:
    """Tool catalog entry (spec 3.4) (contracts/records.schema.json#/$defs/tool)."""

    name: str = ""
    description: str = ""
    capability: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    requires_confirm: bool | None = None
    danger: str = ""
    group: str = ""
    risk_tier: str | None = None

    CONTRACT = 'tool'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tool":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Scene:
    """HUD scene request (spec 3.6, 17) (contracts/records.schema.json#/$defs/scene)."""

    kind: str = ""
    title: str | None = None
    payload: dict[str, Any] | None = None

    CONTRACT = 'scene'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scene":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class MemoryRecord:
    """Memory record (spec 3.4, memory format) (contracts/records.schema.json#/$defs/memory-record)."""

    id: int | None = None
    agent_id: str = ""
    kind: str = ""
    text: str = ""
    tags: list[Any] | None = None
    importance: float | None = None
    ts: float = 0.0
    age_s: float | None = None
    score: float | None = None

    CONTRACT = 'memory-record'

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        """Unknown keys are dropped on purpose: a newer minor may add fields,
        and a reader that cannot understand them must not smuggle them onward."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

