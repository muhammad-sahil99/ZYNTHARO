"""Owner identity (contract 22.7).

Voice verification and PIN approvals only mean something if there is a *known* owner, a way
to prove it, a way to recover when it is lost, and rules for machines that more than one
person uses. This module is that bookkeeping.

Three rules it enforces, all of them consequences of how these systems actually fail:

1. **A voice is not a key.** Speaker verification can be attacked with a recording or a
   cloned voice, so voice may start a low-risk action but never authorises an irreversible
   one. Risky actions need the PIN (or biometric *plus* PIN) even inside an unlocked voice
   session. This is stated in the data model, not left to the caller's judgement.
2. **Recovery is a code, not a person.** A lost PIN is recovered with the recovery code
   generated at first run and shown exactly once; using it rotates it.
3. **Shared PCs are isolated per OS user.** The profile lives under the OS user's own data
   directory, so two Windows accounts on one machine have two owners and two memories. The
   code refuses to run owner-gated work when it detects a profile owned by a different OS
   user.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..events import EventBus, OWNER_DENIED, OWNER_SETUP, OWNER_VERIFIED

PBKDF2_ROUNDS = 200_000
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusable glyphs
#: Capabilities that always require the owner's PIN, whatever the session says.
IRREVERSIBLE_CAPABILITIES: tuple[str, ...] = (
    "filesystem.delete",
    "shell.exec",
    "office.macro",
    "browser.eval",
    "plugin.install",
    "plugin.uninstall",
    "whatsapp.call",
    "process.kill",
    "model.assign",
    "memory.admin",
)


def _hash(secret: str, salt: bytes = b"", rounds: int = PBKDF2_ROUNDS) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, rounds)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def _verify(secret: str, salt_b64: str, digest_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return hmac.compare_digest(candidate, expected)


def make_recovery_code(groups: int = 4, size: int = 4) -> str:
    return "-".join("".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(size)) for _ in range(groups))


@dataclass
class VoicePrint:
    """One enrolled voice sample. Stored as a hash of the embedding, never as audio."""

    label: str
    sample_hash: str
    enrolled_at: float = field(default_factory=time.time)
    quality: float = 0.0


@dataclass
class AuthDecision:
    allow: bool
    method: str  # session | pin | biometric | recovery | owner_absent | locked_out
    reason: str = ""
    capabilities: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"allow": self.allow, "method": self.method, "reason": self.reason}


@dataclass
class OwnerProfile:
    owner_name: str = ""
    created_at: float = 0.0
    pin_salt: str = ""
    pin_hash: str = ""
    recovery_salt: str = ""
    recovery_hash: str = ""
    voice_prints: list[VoicePrint] = field(default_factory=list)
    biometric_available: bool = False
    os_user: str = ""
    failed_attempts: int = 0
    locked_until: float = 0.0
    last_verified_at: float = 0.0
    #: Free text from first-run: "this is my personal laptop" vs "shared family PC".
    machine_kind: str = "personal"

    @property
    def configured(self) -> bool:
        return bool(self.pin_hash)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["voice_prints"] = [asdict(v) for v in self.voice_prints]
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OwnerProfile":
        data = dict(data)
        prints = [VoicePrint(**p) for p in data.pop("voice_prints", [])]
        known = {f for f in cls.__dataclass_fields__}
        profile = cls(**{k: v for k, v in data.items() if k in known})
        profile.voice_prints = prints
        return profile


class OwnerRegistry:
    """First-run setup, verification, recovery, and the risky-action gate."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        bus: EventBus | None = None,
        session_minutes: int = 30,
        max_attempts: int = 5,
        lockout_seconds: float = 300.0,
        voice_matcher: Any = None,
        voice_threshold: float = 0.75,
    ):
        self.home = Path(home)
        self.bus = bus or EventBus()
        self.session_minutes = session_minutes
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        #: Injected speaker-verification function: (print, sample) -> similarity 0..1.
        #: Without one, voice verification reports itself unavailable instead of guessing.
        self.voice_matcher = voice_matcher
        self.voice_threshold = voice_threshold
        self.profile = self._load()

    # -- storage -----------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.home / "owner.json"

    def _load(self) -> OwnerProfile:
        if self.path.exists():
            try:
                return OwnerProfile.from_json(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return OwnerProfile()
        return OwnerProfile()

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.profile.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:  # the profile holds PIN material: keep it owner-readable only
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    # -- first run ---------------------------------------------------------------------
    def setup(self, owner_name: str, pin: str, machine_kind: str = "personal", biometric_available: bool = False) -> str:
        """Create the profile. Returns the recovery code, which is shown exactly once."""
        if len(pin) < 6:
            raise ValueError("PIN must be at least 6 characters")
        recovery = make_recovery_code()
        salt, digest = _hash(pin)
        rsalt, rdigest = _hash(recovery)
        self.profile = OwnerProfile(
            owner_name=owner_name.strip() or "Owner",
            created_at=time.time(),
            pin_salt=salt,
            pin_hash=digest,
            recovery_salt=rsalt,
            recovery_hash=rdigest,
            biometric_available=biometric_available,
            os_user=self.os_user(),
            machine_kind=machine_kind,
        )
        self.save()
        self.bus.emit(OWNER_SETUP, source="owner", owner=self.profile.owner_name, machine_kind=machine_kind)
        return recovery

    @staticmethod
    def os_user() -> str:
        try:
            return getpass.getuser()
        except Exception:  # pragma: no cover - exotic environments
            return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"

    def foreign_profile(self) -> bool:
        """True when the stored profile belongs to a different OS user (shared PC)."""
        stored = self.profile.os_user
        return bool(stored) and stored != self.os_user()

    # -- verification ------------------------------------------------------------------
    def locked_out(self) -> bool:
        return self.profile.locked_until > time.time()

    def verify_pin(self, pin: str, long_enough: bool = True) -> bool:
        if not self.profile.configured:
            return False
        if self.locked_out():
            self.bus.emit(OWNER_DENIED, source="owner", severity="warning", reason="locked out")
            return False
        ok = _verify(pin, self.profile.pin_salt, self.profile.pin_hash)
        self._record_attempt(ok)
        if ok:
            self._open_session("pin")
        return ok

    def verify_recovery(self, code: str, new_pin: str) -> bool:
        """Recover from a lost PIN. Consumes the recovery code and issues a fresh one."""
        if not self.profile.configured or self.locked_out():
            return False
        normalized = code.strip().upper()
        if not _verify(normalized, self.profile.recovery_salt, self.profile.recovery_hash):
            self._record_attempt(False)
            return False
        if len(new_pin) < 6:
            raise ValueError("PIN must be at least 6 characters")
        self.profile.failed_attempts = 0
        self.profile.locked_until = 0.0
        self.enroll_pin(new_pin)
        self._open_session("recovery")
        return True

    def rotate_recovery_code(self) -> str:
        """Issue a new recovery code (shown once). Invalidates the previous one."""
        if not self.profile.configured:
            raise RuntimeError("owner profile is not configured")
        code = make_recovery_code()
        self.profile.recovery_salt, self.profile.recovery_hash = _hash(code)
        self.save()
        return code

    def enroll_pin(self, pin: str) -> None:
        if len(pin) < 6:
            raise ValueError("PIN must be at least 6 characters")
        self.profile.pin_salt, self.profile.pin_hash = _hash(pin)
        self.save()

    def enroll_voice(self, label: str, sample_hash: str, quality: float = 0.8) -> None:
        self.profile.voice_prints.append(VoicePrint(label=label, sample_hash=sample_hash, quality=quality))
        self.save()

    def verify_voice(self, sample_hash: str) -> tuple[bool, float]:
        """Speaker check. Without a matcher this is honestly reported as unavailable."""
        if self.voice_matcher is None or not self.profile.voice_prints:
            return False, 0.0
        best = 0.0
        for enrolled in self.profile.voice_prints:
            try:
                score = float(self.voice_matcher(enrolled.sample_hash, sample_hash))
            except Exception:
                continue
            best = max(best, score)
        return best >= self.voice_threshold, best

    def _record_attempt(self, ok: bool) -> None:
        if ok:
            self.profile.failed_attempts = 0
            self.profile.last_verified_at = time.time()
            self.bus.emit(OWNER_VERIFIED, source="owner", owner=self.profile.owner_name)
            return
        self.profile.failed_attempts += 1
        if self.profile.failed_attempts >= self.max_attempts:
            self.profile.locked_until = time.time() + self.lockout_seconds
            self.profile.failed_attempts = 0
        self.save()
        self.bus.emit(OWNER_DENIED, source="owner", severity="warning", reason="bad PIN", locked_out=self.locked_out())

    # -- session -----------------------------------------------------------------------
    def _open_session(self, method: str) -> None:
        self._session_until = time.time() + self.session_minutes * 60
        self._session_method = method
        self.save()

    def session_valid(self) -> bool:
        return getattr(self, "_session_until", 0.0) > time.time()

    def session_remaining(self) -> float:
        return max(0.0, getattr(self, "_session_until", 0.0) - time.time())

    def session_method(self) -> str:
        """How the current session was opened (``pin``/``recovery``), or ``""`` if none.

        Callers must ask this rather than asserting a method: the gate records what actually
        happened, so a claimed method cannot buy authorisation it did not earn.
        """
        return getattr(self, "_session_method", "") if self.session_valid() else ""

    def end_session(self) -> None:
        self._session_until = 0.0
        self._session_method = ""

    # -- the gate ----------------------------------------------------------------------
    @staticmethod
    def risk_of(capability: str) -> str:
        return "irreversible" if capability in IRREVERSIBLE_CAPABILITIES else "normal"

    def require(self, capabilities: Iterable[str], method: str = "") -> AuthDecision:
        """Decide whether the owner's authorisation is present for these capabilities.

        ``method`` defaults to how this session was actually opened; passing "voice" asks the
        harder question "may a voice match alone authorise this?", which for irreversible work
        is always no.
        """
        caps = tuple(capabilities)
        if method in {"", "session"}:
            method = self.session_method() or "session"
        if not self.profile.configured:
            return AuthDecision(False, "owner_absent", "first-run owner setup has not been completed")
        if self.foreign_profile():
            return AuthDecision(
                False,
                "owner_absent",
                f"profile belongs to OS user {self.profile.os_user!r}; each user on a shared PC has their own owner",
            )
        if self.locked_out():
            return AuthDecision(False, "locked_out", f"PIN entry locked for {self.profile.locked_until - time.time():.0f}s more")
        irreversible = tuple(c for c in caps if c in IRREVERSIBLE_CAPABILITIES)
        if irreversible:
            if method in {"pin", "recovery"} and self.session_valid():
                return AuthDecision(True, method, "PIN verified in this session", irreversible)
            return AuthDecision(
                False,
                "pin",
                "irreversible actions need the PIN even in an unlocked voice session "
                "(a cloned voice must not be able to delete files or place calls)",
                irreversible,
            )
        if self.session_valid():
            return AuthDecision(True, "session", f"owner session valid for {self.session_remaining():.0f}s more")
        if method == "voice":
            return AuthDecision(False, "pin", "voice alone does not authorise actions; enter the PIN to start a session")
        return AuthDecision(False, "pin", "no active owner session")
