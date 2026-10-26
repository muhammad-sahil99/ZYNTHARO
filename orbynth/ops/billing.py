"""Who pays for the model calls (contract 22.6).

Billing is metered inside ORBYNTH (every call is counted), but metering is not financing.
Three launch modes are modelled, and the default for the first release is the one with no
legal exposure and no operating cost:

``byok``        the user brings their own provider keys. ORBYNTH spends nothing and resells
                nothing. This is the default.
``gateway``     ORBYNTH hosts models behind one endpoint. This is *opt-in* and refuses to
                arm itself until the operator has acknowledged, per provider, that resale is
                permitted, and until abuse controls are configured: per-user daily caps, a
                global spend ceiling and a kill switch. An unguarded gateway is how a
                product is drained or rate-limited out of existence in a weekend.
``local_only``  no third-party endpoints at all: a local runtime (llama.cpp, Ollama, vLLM)
                on loopback. Nothing leaves the machine.

The module is deliberately a gate, not a payment processor: it decides whether a call is
allowed under the configured mode and records enough to bill or to throttle honestly.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from ..config import AppConfig, ProviderConfig

BYOK = "byok"
GATEWAY = "gateway"
LOCAL_ONLY = "local_only"
MODES = (BYOK, GATEWAY, LOCAL_ONLY)

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")


@dataclass
class ComplianceIssue:
    severity: str  # blocking | warning
    code: str
    message: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GatewayPolicy:
    """Abuse control. Without all of these, the gateway stays disarmed."""

    endpoint: str = ""
    global_daily_requests: int = 5_000
    per_user_daily_requests: int = 200
    per_user_daily_tokens: int = 2_000_000
    global_daily_spend_usd: float = 50.0
    kill_switch: bool = False
    #: provider name -> the operator has confirmed resale is allowed by that provider's terms
    resale_acknowledged: dict[str, bool] = field(default_factory=dict)
    legal_review_reference: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CallDecision:
    allow: bool
    reason: str = ""
    mode: str = BYOK

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageMeter:
    """Per-user and global counters, in-memory plus a caller-supplied persistence hook."""

    requests: int = 0
    tokens: int = 0
    spend_usd: float = 0.0
    day: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    def roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.requests = 0
            self.tokens = 0
            self.spend_usd = 0.0
            self.day = today


class BillingGate:
    """Decides whether a model call may proceed under the configured launch mode."""

    def __init__(self, mode: str = BYOK, gateway: GatewayPolicy | None = None, local_only: bool = False):
        self.mode = mode if mode in MODES else BYOK
        self.gateway = gateway or GatewayPolicy()
        self.local_only_configured = local_only
        self.global_usage = UsageMeter()
        self.user_usage: dict[str, UsageMeter] = {}
        self._last_prices: dict[str, tuple[float, float]] = {}

    # -- configuration audit -----------------------------------------------------------
    def audit(self, config: AppConfig) -> list[ComplianceIssue]:
        issues: list[ComplianceIssue] = []
        providers = [p for p in config.providers if p.enabled and p.kind != "echo"]
        if self.mode == BYOK:
            keyed = [p for p in providers if p.api_key_env or p.api_key_ref]
            if not keyed:
                issues.append(
                    ComplianceIssue(
                        "warning",
                        "byok_no_key",
                        "BYOK mode is selected but no provider has a key configured: only the offline echo "
                        "provider will answer. Add a key from Settings, never into the installer or a repo.",
                    )
                )
            for provider in providers:
                if provider.name.lower() in {"gateway", "orbynth"}:
                    issues.append(
                        ComplianceIssue(
                            "blocking",
                            "byok_hosted_provider",
                            f"provider {provider.name!r} looks like a hosted gateway; BYOK mode must point at the "
                            "user's own endpoints so no resale is implied.",
                        )
                    )
        elif self.mode == GATEWAY:
            if not self.gateway.endpoint:
                issues.append(ComplianceIssue("blocking", "gateway_no_endpoint", "gateway mode needs an endpoint"))
            if self.gateway.kill_switch:
                issues.append(ComplianceIssue("blocking", "gateway_kill_switch", "the gateway kill switch is engaged"))
            if not self.gateway.legal_review_reference:
                issues.append(
                    ComplianceIssue(
                        "blocking",
                        "gateway_no_legal_review",
                        "hosted resale needs a recorded legal review reference for each provider's terms",
                    )
                )
            unacked = [p.name for p in providers if not self.gateway.resale_acknowledged.get(p.name)]
            if unacked:
                issues.append(
                    ComplianceIssue(
                        "blocking",
                        "gateway_resale_unconfirmed",
                        f"resale has not been confirmed as permitted for: {', '.join(unacked)}. "
                        "A lawyer must check each provider's terms before launch.",
                    )
                )
            if self.gateway.per_user_daily_requests <= 0 or self.gateway.per_user_daily_tokens <= 0:
                issues.append(
                    ComplianceIssue("blocking", "gateway_no_caps", "per-user daily request and token caps are required")
                )
            if self.gateway.global_daily_spend_usd <= 0:
                issues.append(ComplianceIssue("blocking", "gateway_no_spend_cap", "a global daily spend cap is required"))
        else:  # local_only
            remote = [p for p in providers if not _is_local(p)]
            if remote:
                issues.append(
                    ComplianceIssue(
                        "blocking",
                        "local_only_remote_provider",
                        f"local-only mode still has remote providers configured: {', '.join(p.name for p in remote)}",
                    )
                )
            if not self.local_only_configured:
                issues.append(
                    ComplianceIssue(
                        "warning",
                        "local_only_no_runtime",
                        "local-only mode needs a local runtime (llama.cpp / Ollama / vLLM) on loopback",
                    )
                )
        return issues

    def armable(self, config: AppConfig) -> bool:
        return not any(i.severity == "blocking" for i in self.audit(config))

    # -- per-call decision -------------------------------------------------------------
    def authorize(self, user_id: str, provider: str = "", est_tokens: int = 0, est_cost_usd: float = 0.0) -> CallDecision:
        if self.mode == GATEWAY:
            if self.gateway.kill_switch:
                return CallDecision(False, "kill switch engaged", self.mode)
            self.global_usage.roll()
            meter = self.user_usage.setdefault(user_id, UsageMeter())
            meter.roll()
            if meter.requests + 1 > self.gateway.per_user_daily_requests:
                return CallDecision(False, f"per-user daily request cap reached ({self.gateway.per_user_daily_requests})", self.mode)
            if meter.tokens + est_tokens > self.gateway.per_user_daily_tokens:
                return CallDecision(False, "per-user daily token cap reached", self.mode)
            if self.global_usage.requests + 1 > self.gateway.global_daily_requests:
                return CallDecision(False, "global daily request cap reached", self.mode)
            if self.global_usage.spend_usd + est_cost_usd > self.gateway.global_daily_spend_usd:
                return CallDecision(False, "global daily spend cap reached", self.mode)
            return CallDecision(True, "within caps", self.mode)
        if self.mode == LOCAL_ONLY and provider and not provider.startswith("local"):
            return CallDecision(False, f"local-only mode refuses provider {provider!r}", self.mode)
        return CallDecision(True, "", self.mode)

    def record(self, user_id: str, tokens: int = 0, cost_usd: float = 0.0) -> None:
        for meter in (self.global_usage, self.user_usage.setdefault(user_id, UsageMeter())):
            meter.roll()
            meter.requests += 1
            meter.tokens += tokens
            meter.spend_usd += cost_usd

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "global": asdict(self.global_usage),
            "users": {u: asdict(m) for u, m in self.user_usage.items()},
            "gateway": self.gateway.to_json(),
        }


def _is_local(provider: ProviderConfig) -> bool:
    url = (provider.base_url or "").lower()
    return any(host in url for host in LOCAL_HOSTS) or provider.kind == "echo"


def launch_mode_summary(config: AppConfig, mode: str = BYOK, gateway: GatewayPolicy | None = None) -> str:
    gate = BillingGate(mode, gateway)
    issues = gate.audit(config)
    if not issues:
        return f"launch mode {mode}: ready"
    lines: Sequence[str] = [f"launch mode {mode}:"] + [f"  [{i.severity}] {i.message}" for i in issues]
    return "\n".join(lines)
