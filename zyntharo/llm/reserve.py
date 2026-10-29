"""Reserve models and their limits — the Control Manager's bench.

The fleet needs more than "which models exist": it needs models that are *known but not
spending*, ready to be called up when the active ones run out of headroom, plus the limits
each one must be held to. Without that, adding a provider is a leap of faith: the system
learns the model's rpm/rpd/tpm by getting a 429.

So a model is added with its envelope written down — per minute, per day **and per week** —
and it can be added *as a reserve*: registered, quota-tracked, and deliberately excluded
from ordinary assignment until someone with authority activates it.

Who activates is not this module's business. It exposes ``activate``/``retire``; the
Steward (rank 1) holds the ``model.manage`` capability, so the judgement lives with the
manager role, and every activation is published as an event rather than happening quietly.

Limits are validated on the way in. A negative cap, or a weekly cap smaller than the daily
one, is refused with the reason — a nonsense envelope would make the Steward's arithmetic
wrong in a way nobody would notice until the money was gone.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import AppConfig, ModelSpec, ProviderConfig
from ..events import MODEL_LIMITS, MODEL_RESERVE, EventBus

#: Provider kinds the adapter layer can actually speak.
PROVIDER_KINDS: tuple[str, ...] = ("openai_compatible", "gemini", "anthropic", "echo")


@dataclass(slots=True)
class ModelLimits:
    """The envelope a model is held to. Every field is optional except rpm/rpd sanity."""

    rpm: int | None = None
    rpd: int | None = None
    rpw: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    tpw: int | None = None
    context_window: int = 128_000

    LIMIT_FIELDS = ("rpm", "rpd", "rpw", "tpm", "tpd", "tpw")

    def validate(self) -> None:
        for name in self.LIMIT_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be a whole number of requests/tokens, got {value!r}")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero (or omitted entirely), got {value}")
        if self.context_window <= 0:
            raise ValueError("context_window must be greater than zero")
        # A weekly cap below the daily cap can never be reached by design: the daily one
        # binds first, and the weekly number is then a lie the Steward would plan against.
        for daily, weekly, label in (
            (self.rpd, self.rpw, "requests"),
            (self.tpd, self.tpw, "tokens"),
        ):
            if daily is not None and weekly is not None and weekly < daily:
                raise ValueError(
                    f"weekly {label} cap ({weekly}) is smaller than the daily cap ({daily}); "
                    "the daily limit would bind first and the weekly figure would be misleading"
                )
        if self.tpm is not None and self.tpw is not None and self.tpw < self.tpm:
            raise ValueError(f"weekly token cap ({self.tpw}) is smaller than the per-minute cap ({self.tpm})")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ModelLimits":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def describe(self) -> str:
        parts = []
        for name, label in (("rpm", "req/min"), ("rpd", "req/day"), ("rpw", "req/wk"), ("tpm", "tok/min"), ("tpd", "tok/day"), ("tpw", "tok/wk")):
            value = getattr(self, name)
            if value is not None:
                parts.append(f"{value} {label}")
        return ", ".join(parts) or "no limits declared"


class ReservePool:
    """Adds, holds and calls up models; the only writer of the provider bench."""

    def __init__(
        self,
        config: AppConfig,
        quotas: Any = None,
        bus: EventBus | None = None,
        path: str | Path | None = None,
        persist: bool = True,
    ):
        self.config = config
        self.quotas = quotas
        self.bus = bus or EventBus()
        self.persist = persist
        self.path = Path(path) if path else None
        #: ``provider:model`` ids that are held out of ordinary assignment.
        self._held: set[str] = set()
        self._added: dict[str, dict[str, Any]] = {}
        self.load()

    # -- persistence ---------------------------------------------------------------------
    def load(self) -> None:
        if not self.path or not self.path.exists():
            # A model declared as a reserve in config is a reserve on restart too; the file
            # only exists for models added at runtime.
            for spec in self.config.all_models():
                if getattr(spec, "reserve", False):
                    self._held.add(spec.qualified)
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.bus.emit(
                MODEL_LIMITS,
                source="reserve_pool",
                severity="warning",
                message=f"reserve file unreadable, starting from config: {exc!r}",
            )
            return
        self._held = set(payload.get("reserve", []))
        self._added = dict(payload.get("added", {}))
        for qualified in self._held:
            spec = self.config.model(qualified)
            if spec is not None:
                spec.reserve = True

    def save(self) -> None:
        if not self.path or not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(),            "reserve": sorted(self._held), "added": self._added}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- adding --------------------------------------------------------------------------
    def add_model(
        self,
        provider: str,
        model_id: str,
        limits: ModelLimits,
        kind: str = "openai_compatible",
        base_url: str = "",
        api_key_env: str = "",
        tags: Sequence[str] = (),
        reserve: bool = True,
        priority: int = 40,
        supports_vision: bool = True,
        supports_tools: bool = True,
        supports_audio_live: bool = False,
    ) -> dict[str, Any]:
        provider_name = (provider or "").strip()
        model_name = (model_id or "").strip()
        if not provider_name or not model_name:
            raise ValueError("both provider and model_id are required")
        limits.validate()

        existing_provider = self.config.provider(provider_name)
        created: list[str] = []
        if existing_provider is None:
            if not base_url:
                raise ValueError(
                    f"provider {provider_name!r} does not exist yet, so base_url is required "
                    "(e.g. https://api.example.com/v1 for an OpenAI-compatible endpoint)"
                )
            if kind not in PROVIDER_KINDS:
                raise ValueError(f"kind must be one of {PROVIDER_KINDS}, got {kind!r}")
            if not api_key_env:
                raise ValueError(
                    "api_key_env is required for a new provider — name the environment variable that "
                    "holds the key so the key itself never enters configuration"
                )
            existing_provider = ProviderConfig(
                name=provider_name,
                kind=kind,
                base_url=base_url.rstrip("/"),
                api_key_env=api_key_env,
                priority=priority,
                enabled=True,
            )
            self.config.providers.append(existing_provider)
            created.append(f"provider:{provider_name}")
        else:
            # Silently repointing an existing provider would send a working key to a new host.
            if base_url and existing_provider.base_url.rstrip("/") != base_url.rstrip("/"):
                raise ValueError(
                    f"provider {provider_name!r} already points at {existing_provider.base_url!r}; "
                    f"refusing to redirect it to {base_url!r}. Add a second provider instead."
                )
            if kind and existing_provider.kind != kind and existing_provider.models:
                raise ValueError(
                    f"provider {provider_name!r} speaks {existing_provider.kind!r}, not {kind!r}; "
                    "adding a differently-shaped model under one provider will fail at the wire"
                )

        spec = existing_provider.model(model_name)
        updated = spec is not None
        if spec is None:
            spec = ModelSpec(model_id=model_name, provider=provider_name)
            existing_provider.models.append(spec)
            created.append(f"model:{provider_name}:{model_name}")
        spec.rpm, spec.rpd, spec.rpw = limits.rpm, limits.rpd, limits.rpw
        spec.tpm, spec.tpd, spec.tpw = limits.tpm, limits.tpd, limits.tpw
        spec.context_window = limits.context_window
        spec.tags = sorted({*spec.tags, *tags})
        spec.supports_vision = supports_vision
        spec.supports_tools = supports_tools
        spec.supports_audio_live = supports_audio_live
        spec.reserve = bool(reserve)
        if self.quotas is not None:
            self.quotas.register_model(spec)

        qualified = f"{provider_name}:{model_name}"
        if reserve:
            self._held.add(qualified)
        else:
            self._held.discard(qualified)
        self._added[qualified] = {
            "limits": limits.to_json(),
            "tags": list(spec.tags),
            "added_at": time.time(),
            "reserve": bool(reserve),
        }
        self.save()
        if self.persist:
            try:
                self.config.save()
            except OSError as exc:
                self.bus.emit(
                    MODEL_LIMITS,
                    source="reserve_pool",
                    severity="warning",
                    message=f"limits applied in memory but config could not be written: {exc!r}",
                )
        self.bus.emit(
            MODEL_LIMITS,
            source="reserve_pool",
            severity="notice",
            **{
                "model": qualified,
                "limits": limits.to_json(),
                "describe": limits.describe(),
                "reserve": bool(reserve),
                "created": created,
                "updated": updated,
            },
        )
        return {
            "model": qualified,
            "limits": limits.to_json(),
            "describe": limits.describe(),
            "reserve": bool(reserve),
            "created": created,
            "updated": updated,
            "note": (
                "reserve: registered and quota-tracked, but not assigned to any role until "
                "model_activate is called"
                if reserve
                else "active: available for ordinary assignment"
            ),
        }

    # -- activation ----------------------------------------------------------------------
    def activate(self, qualified: str, by: str = "steward", reason: str = "") -> dict[str, Any]:
        spec = self.config.model(qualified)
        if spec is None:
            raise ValueError(f"unknown model {qualified!r}; add it first with model_add")
        if not spec.reserve and qualified not in self._held:
            return {"model": qualified, "activated": False, "reason": "already active"}
        spec.reserve = False
        self._held.discard(qualified)
        if qualified in self._added:
            self._added[qualified]["reserve"] = False
        self.save()
        self._persist_config()
        headroom = self.headroom(qualified)
        self.bus.emit(
            MODEL_RESERVE,
            source=by,
            severity="notice",
            **{"model": qualified, "action": "activate", "reason": reason, "headroom": round(headroom, 3), "by": by},
        )
        return {
            "model": qualified,
            "activated": True,
            "by": by,
            "reason": reason,
            "headroom": round(headroom, 3),
            "limits": self.limits(qualified),
        }

    def retire(self, qualified: str, by: str = "steward", reason: str = "") -> dict[str, Any]:
        spec = self.config.model(qualified)
        if spec is None:
            raise ValueError(f"unknown model {qualified!r}")
        spec.reserve = True
        self._held.add(qualified)
        if qualified in self._added:
            self._added[qualified]["reserve"] = True
        self.save()
        self._persist_config()
        self.bus.emit(
            MODEL_RESERVE,
            source=by,
            severity="info",
            **{"model": qualified, "action": "retire", "reason": reason, "by": by},
        )
        return {"model": qualified, "reserve": True, "by": by, "reason": reason}

    def _persist_config(self) -> None:
        if not self.persist:
            return
        try:
            self.config.save()
        except OSError:
            pass

    # -- reading -------------------------------------------------------------------------
    def is_reserve(self, qualified: str) -> bool:
        return qualified in self._held

    def limits(self, qualified: str) -> dict[str, Any]:
        spec = self.config.model(qualified)
        if spec is None:
            raise ValueError(f"unknown model {qualified!r}")
        return {
            "rpm": spec.rpm,
            "rpd": spec.rpd,
            "rpw": spec.rpw,
            "tpm": spec.tpm,
            "tpd": spec.tpd,
            "tpw": spec.tpw,
            "context_window": spec.context_window,
        }

    def headroom(self, qualified: str) -> float:
        """0.0 = nothing left in at least one window, 1.0 = untouched."""
        if self.quotas is None:
            return 1.0
        quota = self.quotas.models.get(qualified)
        return self.quotas.headroom(quota) if quota is not None else 1.0

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in self.config.all_models():
            rows.append(
                {
                    "model": spec.qualified,
                    "reserve": spec.qualified in self._held or bool(getattr(spec, "reserve", False)),
                    "provider": spec.provider,
                    "tags": list(spec.tags),
                    "limits": self.limits(spec.qualified),
                    "headroom": round(self.headroom(spec.qualified), 3),
                    "audio_live": spec.supports_audio_live,
                }
            )
        return sorted(rows, key=lambda r: (r["reserve"], r["model"]))

    def reserves(self) -> list[dict[str, Any]]:
        return [row for row in self.rows() if row["reserve"]]

    def best_reserve(self, tags: Iterable[str] = (), need_tokens: int = 0) -> dict[str, Any] | None:
        """The reserve with the most headroom that can serve this need, or None.

        Returns ``None`` rather than an arbitrary model when every reserve is exhausted: the
        caller then knows to defer or escalate instead of spending a call that will 429.
        """
        wanted = {t.lower() for t in tags}
        candidates: list[dict[str, Any]] = []
        for row in self.reserves():
            spec = self.config.model(row["model"])
            if spec is None:
                continue
            if wanted and not wanted & {t.lower() for t in spec.tags}:
                continue
            if need_tokens and spec.tpm is not None and need_tokens > spec.tpm:
                continue
            quota = self.quotas.models.get(row["model"]) if self.quotas is not None else None
            if quota is not None and self.quotas is not None:
                if not self.quotas.check(row["model"], need_tokens).allowed:
                    continue
            candidates.append(row)
        if not candidates:
            return None
        return max(candidates, key=lambda r: r["headroom"])
