"""Configuration, provider registry specs, agent roster and secret resolution.

Everything the operative knows about *itself* (models, quotas, agents, capabilities,
safety policy, voice/HUD preferences) is declared here and persisted as JSON under the
ZYNTHARO home directory. Secrets are never written to that file: config stores the *name*
of an environment variable or a keychain reference, and :class:`Secrets` resolves it.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Capabilities: the vocabulary the permission gate understands.
# --------------------------------------------------------------------------------------

#: Ordered, granular capability strings. ``*`` suffixes are wildcards.
CAPABILITIES: tuple[str, ...] = (
    "input.mouse",
    "input.keyboard",
    "input.type",
    "input.hotkey",
    "screen.control",
    "vision.capture",
    "vision.ocr",
    "vision.ground",
    "windows.list",
    "windows.launch",
    "windows.focus",
    "windows.uia",
    "filesystem.read",
    "filesystem.write",
    "filesystem.create",
    "filesystem.delete",
    "filesystem.move",
    "archive.zip",
    "archive.unzip",
    "shell.exec",
    "process.list",
    "process.kill",
    "hardware.read",
    "hardware.tune",
    "network.http",
    "browser.navigate",
    "browser.tabs",
    "browser.icons",
    "browser.click",
    "browser.type",
    "browser.scroll",
    "browser.extract",
    "browser.forms",
    "browser.eval",
    "browser.download",
    "browser.cookies",
    "browser.history",
    "office.word",
    "office.excel",
    "office.access",
    "office.powerpoint",
    "office.macro",
    "office.print",
    # Outbound messaging through a connected platform. Sending is irreversible because a sent
    # message cannot be recalled, so ``channel.send`` is owner-only (see ops/risk.py).
    "channel.read",
    "channel.send",
    # A packaged business process (invoice intake, reply desk, recurring report).
    "workflow.run",
    # A customer's outside account: mail, drives, calendar, chat, CRM, code, payments.
    # ``connector.send`` is separate from ``connector.write`` because a delivered email is
    # irreversible in a way that editing a spreadsheet cell is not.
    "connector.read",
    "connector.write",
    "connector.send",
    "connector.admin",
    "whatsapp.pair",
    "whatsapp.chat",
    "whatsapp.media",
    "whatsapp.call",
    "voice.identify",
    "voice.enroll",
    "camera.view",
    "camera.enroll",
    "presence.read",
    "presence.decide",
    "task.schedule",
    # MCP (spec 15.1). ``mcp.read`` reaches tools a server declares read-only; ``mcp.call``
    # reaches everything else and is confirmation-gated, because the annotation is the server's
    # own claim. ``mcp.admin`` adds or removes a stored command line, which is executable content.
    "mcp.read",
    "mcp.call",
    "mcp.admin",
    "plugin.install",
    "plugin.author",
    "plugin.uninstall",
    "agent.spawn",
    "agent.control",
    "agent.messaging",
    "model.assign",
    "model.manage",
    "memory.admin",
)


def expand_capabilities(specs: list[str] | tuple[str, ...]) -> frozenset[str]:
    """Expand wildcard capability specs (``filesystem.*``, ``*``) into concrete names."""
    out: set[str] = set()
    for spec in specs:
        spec = spec.strip()
        if spec == "*":
            out.update(CAPABILITIES)
        elif spec.endswith(".*"):
            prefix = spec[:-1]  # keep the dot
            out.update(c for c in CAPABILITIES if c.startswith(prefix))
        elif spec in CAPABILITIES:
            out.add(spec)
        else:
            # Unknown capability: keep it so plugins can declare their own, but record it.
            out.add(spec)
    return frozenset(out)


# --------------------------------------------------------------------------------------
# Models & providers
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ModelSpec:
    """One model on one provider, with the quota envelope the Steward must respect.

    ``provider`` is filled in automatically from the enclosing :class:`ProviderConfig`, so
    config files stay readable: declare a provider once, list its models plainly.
    """

    model_id: str
    provider: str = ""
    rpm: int | None = None  # requests per minute
    rpd: int | None = None  # requests per day
    tpm: int | None = None  # tokens per minute
    tpd: int | None = None  # tokens per day
    #: Weekly envelopes. Providers publish these as "weekly" caps; without them a model
    #: with a generous daily limit could be spent flat by Tuesday and the Steward would
    #: only find out from a 429.
    rpw: int | None = None  # requests per week
    tpw: int | None = None  # tokens per week
    #: Holds this model out of ordinary assignment until the Control Manager activates it.
    reserve: bool = False
    context_window: int = 128_000
    supports_tools: bool = True
    supports_vision: bool = True
    supports_audio_live: bool = False  # full-duplex live audio (Sovereign class)
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
    tags: list[str] = field(default_factory=list)  # e.g. ["reasoning", "cheap", "coding"]

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.model_id}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ModelSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class ProviderConfig:
    """An LLM provider. ``kind`` selects the wire protocol adapter."""

    name: str
    kind: str = "openai_compatible"  # openai_compatible | gemini | anthropic | echo
    base_url: str = ""
    api_key_env: str = ""
    api_key_ref: str = ""  # keychain reference, used when no env var is set
    priority: int = 0  # lower = preferred when all else is equal
    models: list[ModelSpec] = field(default_factory=list)
    enabled: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A model always knows which provider serves it; declare that once, here.
        for spec in self.models:
            if not spec.provider:
                spec.provider = self.name

    def model(self, model_id: str) -> ModelSpec | None:
        for m in self.models:
            if m.model_id == model_id:
                return m
        return None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ProviderConfig":
        data = dict(data)
        data["models"] = [ModelSpec.from_json(m) for m in data.get("models", [])]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AgentConfig:
    """Roster entry for one agent. ``role`` selects a template in ``agents/roles.py``."""

    name: str
    role: str = "operative"
    model: str = ""  # "provider:model" — empty means "resolve by role"
    capabilities: list[str] = field(default_factory=list)
    max_actions: int = 200
    max_wall_seconds: int = 1800
    max_tokens: int = 2_000_000
    autostart: bool = False
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AgentConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------------------
# App config
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class VoiceConfig:
    enabled: bool = True
    wake_words: list[str] = field(default_factory=lambda: ["zyntharo", "orb"])
    stt: str = "faster-whisper"  # faster-whisper | cloud | none
    stt_model: str = "base.en"
    tts: str = "edge-tts"  # edge-tts | piper | none
    tts_voice: str = "en-US-AriaNeural"
    barge_in: bool = True
    vad_silence_ms: int = 700
    #: Primary language for speech recognition and synthesis ("en", "ur", "ar", ...).
    #: Hard-coding English here made the language layer unreachable, so it is configuration.
    language: str = "en"
    #: Extra languages the user speaks or reads; OCR packs and hints are derived from both.
    secondary_languages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HudConfig:
    host: str = "127.0.0.1"
    port: int = 8760
    theme: str = "zyntharo-core"
    particle_budget: int = 6000
    wires: bool = True
    start_voice_muted: bool = False
    chat_input_visible: bool = False  # voice-first by default; toggled from the menu
    webview: bool = True


@dataclass(slots=True)
class BillingConfig:
    """Who pays for model calls (contract 22.6). BYOK is the default and spends nothing."""

    mode: str = "byok"  # byok | gateway | local_only
    gateway_endpoint: str = ""
    legal_review_reference: str = ""
    per_user_daily_requests: int = 200
    per_user_daily_tokens: int = 2_000_000
    global_daily_requests: int = 5_000
    global_daily_spend_usd: float = 50.0
    kill_switch: bool = False
    resale_acknowledged: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class CatalogConfig:
    """Signed model catalogue settings (contract 22.10)."""

    path: str = ""  # the catalogue file applied last
    public_key_path: str = ""  # pinned Ed25519 public key (PEM); HMAC is development-only
    hmac_key_env: str = "ZYNTHARO_CATALOG_HMAC"
    allow_unsigned: bool = False
    drift_check_hours: int = 24


@dataclass(slots=True)
class OpsConfig:
    """Operational switches: owner gate, supervisor, governor, languages."""

    require_owner_for_irreversible: bool = True
    supervisor_enabled: bool = True
    reconcile_on_start: bool = True
    governor_enabled: bool = True
    low_priority: bool = True
    #: Maximum a single agent step may be delayed by the responsiveness governor.
    governor_max_defer_s: int = 90
    #: An isolated session/VM is available for unattended GUI work. Off by default: with no
    #: such thing configured, the honest answer to "run this at 3am with the screen locked" is
    #: to defer, and the preflight says so instead of pretending it ran (contract 22.2).
    isolated_session_available: bool = False
    #: Hardware sensing (spec 16). The collector runs on its own low-priority thread and its
    #: own cost is measured; the rules are deterministic and never call a model.
    sensors_enabled: bool = True
    sensor_interval: float = 1.0
    #: Hibernating or powering the machine down on a critical alert is off unless the owner
    #: turns it on: an assistant that can shut down the computer is a different risk class.
    allow_power_down: bool = False


@dataclass(slots=True)
class SafetyConfig:
    confirm_capabilities: list[str] = field(
        default_factory=lambda: [
            "filesystem.delete",
            "office.macro",
            "browser.eval",
            "shell.exec",
            "plugin.install",
            "plugin.uninstall",
            "whatsapp.call",
            "process.kill",
            "channel.send",
        ]
    )
    audit_keep_days: int = 90
    # Two active workers is the safe default for a desktop product. The governor can reduce
    # this further under pressure; owners who have measured spare capacity can raise it.
    max_parallel_agents: int = 2
    allow_network: bool = True
    allowed_fs_roots: list[str] = field(default_factory=list)  # empty = whole user profile


@dataclass(slots=True)
class AppConfig:
    home: str = ""
    providers: list[ProviderConfig] = field(default_factory=list)
    roster: list[AgentConfig] = field(default_factory=list)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    hud: HudConfig = field(default_factory=HudConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    billing: BillingConfig = field(default_factory=BillingConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    ops: OpsConfig = field(default_factory=OpsConfig)
    default_team_size: int = 2

    # -- persistence -------------------------------------------------------------------
    @property
    def home_path(self) -> Path:
        return Path(self.home or default_home())

    @property
    def db_path(self) -> Path:
        return self.home_path / "zyntharo.db"

    @property
    def config_path(self) -> Path:
        return self.home_path / "config.json"

    def to_json(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "providers": [p.to_json() for p in self.providers],
            "roster": [a.to_json() for a in self.roster],
            "voice": asdict(self.voice),
            "hud": asdict(self.hud),
            "safety": asdict(self.safety),
            "billing": asdict(self.billing),
            "catalog": asdict(self.catalog),
            "ops": asdict(self.ops),
            "default_team_size": self.default_team_size,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AppConfig":
        cfg = cls(
            home=data.get("home", ""),
            providers=[ProviderConfig.from_json(p) for p in data.get("providers", [])],
            roster=[AgentConfig.from_json(a) for a in data.get("roster", [])],
            default_team_size=int(data.get("default_team_size", 2)),
        )
        if "voice" in data:
            cfg.voice = VoiceConfig(**{k: v for k, v in data["voice"].items() if k in VoiceConfig.__dataclass_fields__})
        if "hud" in data:
            cfg.hud = HudConfig(**{k: v for k, v in data["hud"].items() if k in HudConfig.__dataclass_fields__})
        if "safety" in data:
            cfg.safety = SafetyConfig(
                **{k: v for k, v in data["safety"].items() if k in SafetyConfig.__dataclass_fields__}
            )
        for name, cls in (("billing", BillingConfig), ("catalog", CatalogConfig), ("ops", OpsConfig)):
            if name in data and isinstance(data[name], dict):
                setattr(cfg, name, cls(**{k: v for k, v in data[name].items() if k in cls.__dataclass_fields__}))
        return cfg

    def save(self) -> Path:
        path = self.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def model(self, qualified: str) -> ModelSpec | None:
        if ":" not in qualified:
            return None
        provider_name, model_id = qualified.split(":", 1)
        for provider in self.providers:
            if provider.name == provider_name:
                return provider.model(model_id)
        return None

    def all_models(self) -> list[ModelSpec]:
        return [m for p in self.providers if p.enabled for m in p.models]

    def provider(self, name: str) -> ProviderConfig | None:
        for p in self.providers:
            if p.name == name:
                return p
        return None


def default_home() -> Path:
    """ZYNTHARO_HOME wins, else a per-user data directory."""
    env = os.environ.get("ZYNTHARO_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ZYNTHARO"


# --------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------


class Secrets:
    """Resolve provider keys without ever writing them into ``config.json``.

    Order: process environment → OS keychain (``keyring`` when installed) → local
    ``secrets.json`` with 0600 permissions (last-resort, desktop convenience).
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self._cache: dict[str, str] = {}

    @property
    def _file(self) -> Path:
        return self.home / "secrets.json"

    def _load_file(self) -> dict[str, str]:
        if not self._file.exists():
            return {}
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, env_name: str = "", ref: str = "") -> str | None:
        key = env_name or ref
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        value = ""
        if env_name:
            value = os.environ.get(env_name, "")
        if not value and ref:
            try:  # pragma: no cover - optional dependency
                import keyring  # type: ignore

                value = keyring.get_password("zyntharo", ref) or ""
            except Exception:
                value = ""
        if not value:
            value = self._load_file().get(key, "")
        if value:
            self._cache[key] = value
        return value or None

    def set(self, key: str, value: str, use_keychain: bool = True) -> None:
        """Store a secret. Keychain first; 0600 file as fallback."""
        stored = False
        if use_keychain:
            try:  # pragma: no cover - optional dependency
                import keyring  # type: ignore

                keyring.set_password("zyntharo", key, value)
                stored = True
            except Exception:
                stored = False
        if not stored:
            data = self._load_file()
            data[key] = value
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                os.chmod(self._file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:  # pragma: no cover - platform dependent
                pass
        self._cache[key] = value

    def has(self, env_name: str = "", ref: str = "") -> bool:
        return bool(self.get(env_name, ref))


# --------------------------------------------------------------------------------------
# Defaults / bootstrap
# --------------------------------------------------------------------------------------


def default_config() -> AppConfig:
    """A usable first-run config.

    Includes the offline ``echo`` provider deliberately: a fresh install must be able to
    drive the whole pipeline (agents, quotas, tools, HUD, plugins) with zero keys, and only
    then be pointed at a real model. It performs no reasoning — it exists so nothing is
    blocked on a key, and ``zyntharo doctor`` says so plainly.
    """
    return AppConfig(
        providers=[
            ProviderConfig(
                name="echo",
                kind="echo",
                priority=999,
                models=[
                    ModelSpec(
                        model_id="offline-echo",
                        rpm=1000,
                        rpd=100_000,
                        tpm=500_000,
                        tags=["offline", "fast", "general", "coding", "reasoning", "vision", "cheap"],
                    )
                ],
            ),
            ProviderConfig(
                name="custom",
                kind="openai_compatible",
                base_url="https://api.example.com/v1",
                api_key_env="ZYNTHARO_CUSTOM_API_KEY",
                priority=50,
                models=[
                    ModelSpec(model_id="your-model-id", rpm=60, rpd=1000, tpm=90_000, tags=["general"]),
                ],
            ),
            ProviderConfig(
                name="gemini",
                kind="gemini",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key_env="GEMINI_API_KEY",
                priority=10,
                models=[
                    ModelSpec(
                        model_id="gemini-2.5-flash",
                        rpm=10,
                        rpd=250,
                        tpm=250_000,
                        context_window=1_000_000,
                        supports_audio_live=False,
                        tags=["fast", "general"],
                    ),
                    ModelSpec(
                        model_id="gemini-2.5-pro",
                        rpm=5,
                        rpd=100,
                        tpm=250_000,
                        tags=["reasoning"],
                    ),
                ],
            ),
        ],
        roster=[
            AgentConfig(name="Sovereign", role="sovereign", model="gemini:gemini-2.5-flash", capabilities=["*"]),
            AgentConfig(
                name="Steward",
                role="steward",
                model="gemini:gemini-2.5-flash",
                capabilities=[
                    "process.list",
                    "model.assign",
                    "agent.control",
                    "agent.spawn",
                    "memory.admin",
                    "network.http",
                ],
            ),
            AgentConfig(
                name="Artificer",
                role="artificer",
                model="gemini:gemini-2.5-flash",
                capabilities=[
                    "filesystem.read",
                    "filesystem.write",
                    "filesystem.create",
                    "plugin.author",
                    "plugin.install",
                    "shell.exec",
                ],
            ),
        ],
    )


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load config, creating the default one on first run."""
    if path is None:
        candidate = default_home() / "config.json"
    else:
        candidate = Path(path)
    if candidate.exists():
        try:
            return AppConfig.from_json(json.loads(candidate.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - corrupt config
            raise RuntimeError(f"config at {candidate} is unreadable: {exc}") from exc
    cfg = default_config()
    cfg.home = str(candidate.parent)
    return cfg
