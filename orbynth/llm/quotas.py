"""Quota accounting — the numbers the Steward manages the fleet with.

The user's requirement is explicit: every provider/model carries its own **rpm, rpd and
tpm** limits, the system must *know* them, must notice when it is close, and must swap to
a fallback model (same provider or a different one) while carrying the *same memory and
context* before work is interrupted.

This module is the arithmetic half of that promise:

* sliding windows for requests/minute, requests/day, tokens/minute, tokens/day
* pre-flight ``check()`` so a call is never fired into a wall
* ``note_error()`` converts provider failures (429/quota/5xx) into escalating cooldowns
* persistent counters so a restart does not forget the day's spend
* ``snapshot()`` produces the HUD quota panel rows
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import AppConfig, ModelSpec
from ..events import QUOTA_EXHAUSTED, QUOTA_WARNING, EventBus

MINUTE = 60.0
DAY = 86_400.0
WEEK = 604_800.0


@dataclass(slots=True)
class QuotaDecision:
    allowed: bool
    reason: str = "ok"
    retry_after: float = 0.0
    qualified: str = ""

    def __bool__(self) -> bool:  # ``if runtime.quotas.check(m)`` reads naturally
        return self.allowed


@dataclass
class ModelQuota:
    qualified: str
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    rpw: int | None = None
    tpw: int | None = None
    requests_minute: deque[float] = field(default_factory=deque)
    requests_day: deque[float] = field(default_factory=deque)
    requests_week: deque[float] = field(default_factory=deque)
    tokens_minute: deque[tuple[float, int]] = field(default_factory=deque)
    tokens_day: deque[tuple[float, int]] = field(default_factory=deque)
    tokens_week: deque[tuple[float, int]] = field(default_factory=deque)
    cooldown_until: float = 0.0
    consecutive_errors: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    last_error: str = ""

    # -- windows -----------------------------------------------------------------------
    def prune(self, now: float) -> None:
        for window, span in (
            (self.requests_minute, MINUTE),
            (self.requests_day, DAY),
            (self.requests_week, WEEK),
        ):
            while window and now - window[0] > span:
                window.popleft()
        for window, span in (
            (self.tokens_minute, MINUTE),
            (self.tokens_day, DAY),
            (self.tokens_week, WEEK),
        ):
            while window and now - window[0][0] > span:
                window.popleft()

    def _retry_after(self, window: str, now: float, span: float) -> float:
        """Seconds until the named window frees a slot (how long the Steward must wait)."""
        request_windows = {"rpm": self.requests_minute, "rpd": self.requests_day, "rpw": self.requests_week}
        token_windows = {"tpm": self.tokens_minute, "tpd": self.tokens_day, "tpw": self.tokens_week}
        if window.startswith("t"):
            entries = token_windows.get(window, self.tokens_minute)
            oldest = entries[0][0] if entries else now
        else:
            requests = request_windows.get(window, self.requests_minute)
            oldest = requests[0] if requests else now
        return max(0.0, oldest + span - now)

    def usage(self) -> dict[str, int]:
        return {
            "requests_minute": len(self.requests_minute),
            "requests_day": len(self.requests_day),
            "requests_week": len(self.requests_week),
            "tokens_minute": sum(n for _, n in self.tokens_minute),
            "tokens_day": sum(n for _, n in self.tokens_day),
            "tokens_week": sum(n for _, n in self.tokens_week),
        }

    def to_json(self, now: float) -> dict[str, Any]:
        self.prune(now)
        return {
            "requests_day": list(self.requests_day),
            "requests_week": list(self.requests_week),
            "tokens_day": [list(t) for t in self.tokens_day],
            "tokens_week": [list(t) for t in self.tokens_week],
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
        }

    def load(self, data: dict[str, Any], now: float) -> None:
        self.requests_day = deque(t for t in data.get("requests_day", []) if now - t <= DAY)
        self.requests_week = deque(t for t in data.get("requests_week", []) if now - t <= WEEK)
        self.tokens_day = deque((t, int(n)) for t, n in data.get("tokens_day", []) if now - t <= DAY)
        self.tokens_week = deque((t, int(n)) for t, n in data.get("tokens_week", []) if now - t <= WEEK)
        self.total_requests = int(data.get("total_requests", 0))
        self.total_tokens = int(data.get("total_tokens", 0))


class QuotaManager:
    """Tracks spend per ``provider:model`` and decides whether a call may proceed."""

    def __init__(
        self,
        config: AppConfig | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], float] = time.time,
        persist_path: str | Path | None = None,
        warn_at: float = 0.8,
    ):
        self.bus = bus or EventBus()
        self.clock = clock
        self.persist_path = Path(persist_path) if persist_path else None
        self.warn_at = warn_at
        self.models: dict[str, ModelQuota] = {}
        if config is not None:
            self.register_config(config)
        self.load()

    # -- registration ------------------------------------------------------------------
    def register_model(self, spec: ModelSpec) -> ModelQuota:
        quota = self.models.get(spec.qualified) or ModelQuota(qualified=spec.qualified)
        quota.rpm, quota.rpd, quota.tpm, quota.tpd = spec.rpm, spec.rpd, spec.tpm, spec.tpd
        quota.rpw, quota.tpw = spec.rpw, spec.tpw
        self.models[spec.qualified] = quota
        return quota

    def register_config(self, config: AppConfig) -> None:
        for spec in config.all_models():
            self.register_model(spec)

    # -- decisions ---------------------------------------------------------------------
    def check(self, qualified: str, tokens: int = 0) -> QuotaDecision:
        quota = self.models.get(qualified)
        if quota is None:
            return QuotaDecision(True, "unmanaged", 0.0, qualified)
        now = self.clock()
        quota.prune(now)
        if quota.cooldown_until > now:
            return QuotaDecision(
                False,
                "cooldown",
                quota.cooldown_until - now,
                qualified,
            )
        used = quota.usage()
        checks = (
            ("rpm", used["requests_minute"], quota.rpm, MINUTE),
            ("rpd", used["requests_day"], quota.rpd, DAY),
            ("rpw", used["requests_week"], quota.rpw, WEEK),
            ("tpm", used["tokens_minute"] + tokens, quota.tpm, MINUTE),
            ("tpd", used["tokens_day"] + tokens, quota.tpd, DAY),
            ("tpw", used["tokens_week"] + tokens, quota.tpw, WEEK),
        )
        for name, current, limit, span in checks:
            if limit is not None and current >= limit:
                retry_after = quota._retry_after(name, now, span)
                self._publish(QUOTA_EXHAUSTED, quota, name, current, limit, retry_after)
                return QuotaDecision(False, f"{name} limit reached", retry_after, qualified)
            if limit is not None and current >= limit * self.warn_at:
                self._publish(QUOTA_WARNING, quota, name, current, limit, 0.0)
        return QuotaDecision(True, "ok", 0.0, qualified)

    def _publish(self, topic: str, quota: ModelQuota, window: str, current: int, limit: int, retry: float) -> None:
        self.bus.emit(
            topic,
            source="quota_manager",
            severity="error" if topic == QUOTA_EXHAUSTED else "warning",
            model=quota.qualified,
            window=window,
            used=current,
            limit=limit,
            retry_after=round(retry, 1),
        )

    # -- recording ---------------------------------------------------------------------
    def record(self, qualified: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        quota = self.models.get(qualified)
        if quota is None:
            return
        now = self.clock()
        tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        quota.requests_minute.append(now)
        quota.requests_day.append(now)
        quota.requests_week.append(now)
        if tokens:
            quota.tokens_minute.append((now, tokens))
            quota.tokens_day.append((now, tokens))
            quota.tokens_week.append((now, tokens))
        quota.total_requests += 1
        quota.total_tokens += tokens
        quota.consecutive_errors = 0
        quota.prune(now)
        self.save()

    def note_error(self, qualified: str, kind: str, retry_after: float = 0.0, detail: str = "") -> float:
        """Register a failure. Returns the cooldown that was applied (seconds)."""
        quota = self.models.get(qualified)
        if quota is None:
            return 0.0
        quota.consecutive_errors += 1
        quota.last_error = f"{kind}: {detail}"[:300]
        if kind in {"quota", "rate_limit"}:
            # Honour the provider's retry-after when given, else back off 5s,10s,20s… capped 15m.
            base = retry_after or min(5.0 * (2 ** (quota.consecutive_errors - 1)), 900.0)
            quota.cooldown_until = max(quota.cooldown_until, self.clock() + base)
        elif kind in {"server", "network"}:
            quota.cooldown_until = max(quota.cooldown_until, self.clock() + min(2.0 ** quota.consecutive_errors, 60.0))
        elif kind == "auth":
            quota.cooldown_until = max(quota.cooldown_until, self.clock() + 3600.0)
        self.save()
        return max(0.0, quota.cooldown_until - self.clock())

    def clear_cooldown(self, qualified: str) -> None:
        if quota := self.models.get(qualified):
            quota.cooldown_until = 0.0
            quota.consecutive_errors = 0

    # -- introspection -----------------------------------------------------------------
    def available(self, qualified: str, tokens: int = 0) -> bool:
        return self.check(qualified, tokens).allowed

    def snapshot(self) -> list[dict[str, Any]]:
        now = self.clock()
        rows: list[dict[str, Any]] = []
        for qualified, quota in sorted(self.models.items()):
            quota.prune(now)
            used = quota.usage()
            rows.append(
                {
                    "model": qualified,
                    "status": self._status(quota, used),
                    "cooldown_s": round(max(0.0, quota.cooldown_until - now), 1),
                    "rpm": {"used": used["requests_minute"], "limit": quota.rpm},
                    "rpd": {"used": used["requests_day"], "limit": quota.rpd},
                    "rpw": {"used": used["requests_week"], "limit": quota.rpw},
                    "tpm": {"used": used["tokens_minute"], "limit": quota.tpm},
                    "tpd": {"used": used["tokens_day"], "limit": quota.tpd},
                    "tpw": {"used": used["tokens_week"], "limit": quota.tpw},
                    "errors": quota.consecutive_errors,
                    "last_error": quota.last_error,
                    "total_requests": quota.total_requests,
                    "total_tokens": quota.total_tokens,
                }
            )
        return rows

    def _status(self, quota: ModelQuota, used: dict[str, int]) -> str:
        if quota.cooldown_until > self.clock():
            return "cooling"
        pairs = (
            (used["requests_minute"], quota.rpm),
            (used["requests_day"], quota.rpd),
            (used["requests_week"], quota.rpw),
            (used["tokens_minute"], quota.tpm),
            (used["tokens_day"], quota.tpd),
            (used["tokens_week"], quota.tpw),
        )
        if any(limit and current >= limit for current, limit in pairs):
            return "exhausted"
        if any(limit and current >= limit * self.warn_at for current, limit in pairs):
            return "warning"
        return "ok"

    # -- persistence -------------------------------------------------------------------
    def save(self) -> None:
        if not self.persist_path:
            return
        now = self.clock()
        payload = {"saved_at": now, "models": {q: m.to_json(now) for q, m in self.models.items()}}
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.persist_path)
        except OSError:
            pass

    def load(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = self.clock()
        for qualified, data in (payload.get("models") or {}).items():
            quota = self.models.setdefault(qualified, ModelQuota(qualified=qualified))
            quota.load(data, now)

    # -- planning helpers used by the Steward -------------------------------------------
    @staticmethod
    def headroom(quota: ModelQuota) -> float:
        """0.0 = no headroom left, 1.0 = completely unused."""
        used = quota.usage()
        ratios: list[float] = []
        for current, limit in (
            (used["requests_minute"], quota.rpm),
            (used["requests_day"], quota.rpd),
            (used["requests_week"], quota.rpw),
            (used["tokens_minute"], quota.tpm),
            (used["tokens_day"], quota.tpd),
            (used["tokens_week"], quota.tpw),
        ):
            if limit:
                ratios.append(min(1.0, current / limit))
        return 1.0 if not ratios else max(0.0, 1.0 - max(ratios))

    def ranked_by_headroom(self, candidates: Iterable[str]) -> list[tuple[str, float]]:
        rows = [(q, self.headroom(self.models[q])) if q in self.models else (q, 1.0) for q in candidates]
        return sorted(rows, key=lambda r: r[1], reverse=True)
