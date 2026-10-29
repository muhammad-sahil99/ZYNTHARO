"""Change over time (contract 22.10).

Providers retire model IDs, rename them, change rate limits and revise their live-audio
APIs. A desktop app that hard-codes a model list starts rotting the day it ships, and it
cannot be fixed without a new installer.

So model knowledge lives in a **catalog** that is separate from the release:

* it carries a version, an issue date and a signature;
* it can add, update, deprecate and retire models without touching the code;
* it is applied only between actions -- never mid-task, because swapping a model underneath
  a running agent loses its context;
* every apply snapshots the previous configuration, so a bad catalog is one command away
  from being rolled back.

Signature verification uses Ed25519 when ``cryptography`` is installed (the production
path). Without it, an HMAC is accepted only when the operator supplies the shared key, and
that is labelled for what it is: symmetric, so it protects against tampering in transit, not
against forgery by whoever holds the key. An unsigned catalog is refused unless explicitly
allowed in development. The verification method is reported, never guessed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import AppConfig, ModelSpec, ProviderConfig
from ..events import EventBus, UPDATE_APPLIED, UPDATE_BLOCKED, UPDATE_STAGED

ACTIVE = "active"
DEPRECATED = "deprecated"
RETIRED = "retired"


@dataclass
class CatalogEntry:
    """One model as the vendor currently describes it."""

    provider: str
    model_id: str
    status: str = ACTIVE
    replaces: str = ""  # the model id this one supersedes, when a vendor renames
    context_window: int = 128_000
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    supports_tools: bool = True
    supports_vision: bool = True
    supports_audio_live: bool = False
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.model_id}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CatalogEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Verification:
    ok: bool
    method: str  # ed25519 | hmac-shared | unsigned
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def canonical(payload: Mapping[str, Any]) -> bytes:
    """Deterministic bytes for signing/verifying, with the signature field removed."""
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_catalog(payload: Mapping[str, Any], hmac_key: str = "", ed25519_private: Any = None) -> str:
    """Produce a signature. Used by the release tooling; the app only verifies."""
    if ed25519_private is not None:  # pragma: no cover - requires cryptography
        return ed25519_private.sign(canonical(payload)).hex()
    if hmac_key:
        return hmac.new(hmac_key.encode("utf-8"), canonical(payload), hashlib.sha256).hexdigest()
    raise ValueError("no signing key provided")


def verify_catalog(payload: Mapping[str, Any], signature: str = "", public_key_pem: str = "", hmac_key: str = "", allow_unsigned: bool = False) -> Verification:
    if not signature:
        if allow_unsigned:
            return Verification(True, "unsigned", "development mode: unsigned catalog accepted")
        return Verification(False, "unsigned", "catalog is not signed; refusing to apply it")
    if public_key_pem:
        try:  # pragma: no cover - requires the cryptography package
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            key = load_pem_public_key(public_key_pem.encode("utf-8"))
            if not isinstance(key, Ed25519PublicKey):
                return Verification(False, "ed25519", "the pinned key is not an Ed25519 public key")
            key.verify(bytes.fromhex(signature), canonical(payload))
            return Verification(True, "ed25519", "signed catalog verified against the pinned key")
        except ImportError:
            pass
        except Exception as exc:
            return Verification(False, "ed25519", f"signature does not verify: {exc}")
    if hmac_key:
        expected = hmac.new(hmac_key.encode("utf-8"), canonical(payload), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return Verification(True, "hmac-shared", "HMAC matches (symmetric: note this in the audit log)")
        return Verification(False, "hmac-shared", "HMAC does not match")
    return Verification(False, "ed25519", "a signature was supplied but no key is pinned to check it with")


@dataclass
class CatalogDelta:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def empty(self) -> bool:
        return not (self.added or self.updated or self.retired or self.deprecated or self.renamed)

    def one_line(self) -> str:
        return (
            f"+{len(self.added)} ~{len(self.updated)} -{len(self.retired)} "
            f"(deprecated {len(self.deprecated)}, renamed {len(self.renamed)})"
        )


@dataclass
class ModelCatalog:
    """A signed statement about which models exist, and under what limits."""

    version: str = "0"
    issued_at: float = 0.0
    entries: list[CatalogEntry] = field(default_factory=list)
    signature: str = ""
    signer: str = ""

    # -- serialisation -----------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "issued_at": self.issued_at,
            "signer": self.signer,
            "entries": [e.to_json() for e in self.entries],
        }

    def to_json(self) -> dict[str, Any]:
        return {**self.payload(), "signature": self.signature}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "ModelCatalog":
        return cls(
            version=str(data.get("version", "0")),
            issued_at=float(data.get("issued_at") or 0.0),
            entries=[CatalogEntry.from_json(e) for e in data.get("entries", [])],
            signature=str(data.get("signature", "")),
            signer=str(data.get("signer", "")),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ModelCatalog":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    def verify(self, public_key_pem: str = "", hmac_key: str = "", allow_unsigned: bool = False) -> Verification:
        return verify_catalog(self.to_json(), self.signature, public_key_pem, hmac_key, allow_unsigned)

    # -- queries -----------------------------------------------------------------------
    def entry(self, provider: str, model_id: str) -> CatalogEntry | None:
        for e in self.entries:
            if e.provider == provider and e.model_id == model_id:
                return e
        return None

    def retired_ids(self) -> set[str]:
        return {e.qualified for e in self.entries if e.status == RETIRED}

    def deprecated_ids(self) -> set[str]:
        return {e.qualified for e in self.entries if e.status == DEPRECATED}

    def delta_against(self, config: AppConfig) -> CatalogDelta:
        delta = CatalogDelta()
        for entry in self.entries:
            spec = config.model(entry.qualified)
            if entry.status == RETIRED:
                if spec is not None:
                    delta.retired.append(entry.qualified)
                continue
            if entry.status == DEPRECATED:
                delta.deprecated.append(entry.qualified)
            if spec is None:
                delta.added.append(entry.qualified)
                continue
            if (
                spec.context_window != entry.context_window
                or spec.rpm != entry.rpm
                or spec.rpd != entry.rpd
                or spec.tpm != entry.tpm
                or spec.supports_audio_live != entry.supports_audio_live
            ):
                delta.updated.append(entry.qualified)
            if entry.replaces:
                delta.renamed.append(f"{entry.replaces} -> {entry.model_id}")
        return delta

    def apply_to_config(self, config: AppConfig) -> CatalogDelta:
        """Merge catalog knowledge into a config, in place. Retired models are removed.

        Removal, not a flag, is deliberate: a retired model ID is a call that will fail, and
        leaving it in the list means an agent eventually picks it and stalls.
        """
        delta = self.delta_against(config)
        retired = self.retired_ids()
        for provider in config.providers:
            provider.models = [m for m in provider.models if f"{provider.name}:{m.model_id}" not in retired]
        for entry in self.entries:
            if entry.status == RETIRED:
                continue
            provider = config.provider(entry.provider)
            if provider is None:
                provider = ProviderConfig(name=entry.provider, kind="openai_compatible", base_url="", enabled=True)
                config.providers.append(provider)
            spec = provider.model(entry.model_id)
            if spec is None:
                spec = ModelSpec(model_id=entry.model_id)
                provider.models.append(spec)
            spec.provider = provider.name
            spec.context_window = entry.context_window
            spec.rpm = entry.rpm if entry.rpm is not None else spec.rpm
            spec.rpd = entry.rpd if entry.rpd is not None else spec.rpd
            spec.tpm = entry.tpm if entry.tpm is not None else spec.tpm
            spec.supports_tools = entry.supports_tools
            spec.supports_vision = entry.supports_vision
            spec.supports_audio_live = entry.supports_audio_live
            for tag in entry.tags:
                if tag not in spec.tags:
                    spec.tags.append(tag)
        return delta


class UpdateGate:
    """Applies catalog/config changes only between actions, with a rollback snapshot."""

    def __init__(self, home: str | os.PathLike[str], bus: EventBus | None = None):
        self.home = Path(home)
        self.bus = bus or EventBus()
        self.staged: ModelCatalog | None = None
        self.snapshots: list[Path] = []

    @property
    def snapshots_dir(self) -> Path:
        return self.home / "updates"

    def stage(self, catalog: ModelCatalog, verification: Verification | None = None) -> None:
        self.staged = catalog
        self.bus.emit(
            UPDATE_STAGED,
            source="catalog",
            version=catalog.version,
            entries=len(catalog.entries),
            method=(verification.method if verification else "unverified"),
            signer=catalog.signer,
        )

    def can_apply(self, in_flight: int = 0) -> tuple[bool, str]:
        """Between actions, not during one. Swapping a model mid-task loses the task."""
        if self.staged is None:
            return False, "nothing is staged"
        if in_flight > 0:
            return False, f"{in_flight} action(s) or agent(s) still in flight; apply when idle"
        return True, ""

    def apply(self, config: AppConfig, in_flight: int = 0, verification: Verification | None = None) -> CatalogDelta:
        ok, reason = self.can_apply(in_flight)
        if not ok:
            self.bus.emit(UPDATE_BLOCKED, source="catalog", severity="notice", reason=reason)
            raise RuntimeError(f"update not applied: {reason}")
        assert self.staged is not None
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshots_dir / f"config-before-{int(time.time())}-{self.staged.version}.json"
        snapshot.write_text(json.dumps(config.to_json(), indent=2), encoding="utf-8")
        self.snapshots.append(snapshot)
        delta = self.staged.apply_to_config(config)
        config.save()
        self.bus.emit(
            UPDATE_APPLIED,
            source="catalog",
            version=self.staged.version,
            method=(verification.method if verification else "unverified"),
            delta=delta.to_json(),
        )
        self.staged = None
        return delta

    def rollback(self, config: AppConfig) -> CatalogDelta | None:
        """Restore the configuration captured before the last apply.

        The snapshot is read from disk, not from memory: an apply and its rollback usually
        happen in different processes (a nightly update, then a bad morning), and an
        in-memory-only rollback would silently do nothing exactly when it is needed.
        """
        snapshot: Path | None = self.snapshots.pop() if self.snapshots else None
        if snapshot is None:
            candidates = sorted(self.snapshots_dir.glob("config-before-*.json"))
            snapshot = candidates[-1] if candidates else None
        if snapshot is None:
            return None
        try:
            previous = AppConfig.from_json(json.loads(snapshot.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None
        delta = CatalogDelta()
        before = {m.qualified for p in config.providers for m in p.models}
        after = {m.qualified for p in previous.providers for m in p.models}
        delta.added = sorted(after - before)
        delta.retired = sorted(before - after)
        config.home = str(self.home)
        config.providers = previous.providers
        config.save()
        # Consume the snapshot so a second rollback walks further back instead of repeating.
        try:
            Path(snapshot).unlink()
        except OSError:
            pass
        self.bus.emit(UPDATE_APPLIED, source="catalog", severity="notice", rollback=True, snapshot=str(snapshot))
        return delta


def check_provider_models(
    config: AppConfig,
    fetch: Callable[..., list[str]] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Nightly drift check: does each provider still serve the model IDs we have?

    ``fetch(base_url, api_key) -> [model_id]`` is injectable so this is testable offline.
    """
    fetch = fetch or _fetch_models
    report: dict[str, Any] = {"checked_at": time.time(), "providers": {}, "missing": [], "unreachable": []}
    for provider in config.providers:
        if not provider.enabled or provider.kind in {"echo"}:
            continue
        if provider.kind == "anthropic":
            # No public list-models endpoint worth trusting: report as unchecked rather than fake it.
            report["providers"][provider.name] = "unchecked (no list endpoint)"
            continue
        key = os.environ.get(provider.api_key_env, "") if provider.api_key_env else ""
        try:
            available = set(fetch(provider.base_url, key, provider.kind))
        except Exception as exc:
            report["unreachable"].append({"provider": provider.name, "error": str(exc)[:200]})
            continue
        if not available:
            report["providers"][provider.name] = "no models reported"
            continue
        known = {m.model_id for m in provider.models}
        missing = sorted(known - available)
        report["providers"][provider.name] = f"{len(available)} served, {len(missing)} configured id(s) no longer served"
        for model_id in missing:
            report["missing"].append({"provider": provider.name, "model_id": model_id})
    return report


def _fetch_models(base_url: str, api_key: str, kind: str = "openai_compatible", timeout: float = 20.0) -> list[str]:  # pragma: no cover - network
    """List the models a provider currently serves. Each wire protocol has its own URL."""
    base = (base_url or "").rstrip("/")
    if not base:
        return []
    if kind == "gemini":
        url = f"{base}/models" + (f"?key={api_key}" if api_key else "")
        headers: dict[str, str] = {}
    else:
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
        data = json.loads(response.read().decode("utf-8"))
    items: Sequence[Mapping[str, Any]] = data.get("data") or data.get("models") or []
    out: list[str] = []
    for item in items:
        raw = str(item.get("id") or item.get("name") or "")
        # Gemini returns names like "models/gemini-2.5-flash".
        out.append(raw.split("/", 1)[1] if raw.startswith("models/") else raw)
    return [name for name in out if name]
