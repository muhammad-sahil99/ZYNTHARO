"""Presence and etiquette: who is here, and whether speaking aloud is the right move.

The assistant this project replaces had one social mode — talk — and it talked over people,
read private things to whoever happened to be in the room, and answered strangers as if they
were the owner. That is not a personality problem, it is a missing decision, so this module
makes the decision explicit and testable.

Inputs come from two independent senses and are never merged into one guess until the
evidence agrees:

* **Voice** (``zyntharo.voice.speakers``): how many distinct voices are talking right now,
  which of them is the owner, whether they are male/female/unknown, and whether two people
  are talking over each other.
* **Camera** (``zyntharo.vision.camera``): how many faces are present and whether the owner is
  among them, *when* the owner has granted consent. Without consent this input is absent —
  and absent is a state, not a "zero strangers" reading, so it must not unlock anything.

The output is a :class:`SpeechDecision`: speak, speak briefly, write to the interface only,
ask the owner, wait, or stay silent — with a reason a person can read. A decision that says
"silent" carries the reason, because an assistant that goes quiet for no visible reason looks
broken.

Three rules encode the judgement the requirement asked for ("speak if needed, otherwise be
polite"):

1. **Privacy beats availability.** If there is a stranger in the room (a face we cannot
   name, or a voice we cannot name while another person is present), private content goes to
   the screen, not the speaker.
2. **A human conversation is not an invitation.** Two or more voices talking to each other,
   with no wake word and no name, means "wait"; barging in is the single most irritating
   behaviour a voice assistant has.
3. **Identity never buys authority.** Knowing who is speaking changes the *tone and privacy*
   of the answer, never the permissions on the action. Authorisation stays in
   :mod:`zyntharo.ops.owner`; this module only ever *reduces* what is said aloud.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from ..events import PRESENCE_DECISION, VOICE_OVERLAP, VOICE_SPEAKER

SPEAK = "speak"
SPEAK_BRIEF = "speak_brief"
TEXT_ONLY = "text_only"
ASK_OWNER = "ask_owner"
WAIT = "wait"
SILENT = "silent"

#: Anything matching these goes to the screen when somebody else is present.
_PRIVATE_PATTERNS = (
    re.compile(r"\b(password|passcode|api[ _-]?key|token|secret|2fa|one[- ]time code)\b", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # an address
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # a phone/card-shaped number
    re.compile(r"(\b[A-Za-z]:\\\\|/home/|/Users/|~/\.)", re.IGNORECASE),  # file paths
    re.compile(r"\b(balance|salary|invoice|iban|account number)\b", re.IGNORECASE),
)

#: How long a voice keeps counting as "recently speaking" for turn-taking.
RECENT_WINDOW_SECONDS = 6.0


def looks_private(text: str) -> bool:
    """Heuristic: would reading this aloud leak something to the room?"""
    sample = (text or "")[:4000]
    return any(pattern.search(sample) for pattern in _PRIVATE_PATTERNS)


@dataclass(slots=True)
class SpeechDecision:
    mode: str
    reason: str
    audible: bool
    private: bool = False
    requires_owner: bool = False
    audience: int = 0
    signals: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VoiceView:
    """What the voice sense currently knows, flattened for decisions and the interface."""

    speaker_count: int = 0
    active_track: str = ""
    active_label: str = ""
    active_gender: str = "unknown"
    owner_speaking: bool = False
    overlap: bool = False
    voice_available: bool = True
    #: Whether an owner voice has been enrolled at all. Without this, "nobody is named" and
    #: "nobody is enrolled" look identical, and one unnamed voice was treated as the owner.
    owner_enrolled: bool = False
    owner_label: str = ""
    verified: bool = False
    detail: str = "no voice analysed yet"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CameraView:
    """What the camera sense knows — with ``available`` false when consent is absent."""

    available: bool = False
    consent: str = "unknown"
    face_count: int = 0
    owner_present: bool = False
    stranger_count: int = 0
    recognition: bool = False
    recognition_available: bool = False
    detail: str = "camera not consulted"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PresenceSnapshot:
    voice: VoiceView = field(default_factory=VoiceView)
    camera: CameraView = field(default_factory=CameraView)
    at: float = field(default_factory=time.time)

    @property
    def owner_present(self) -> bool:
        """Owner evidence. The camera wins when it can actually match faces.

        The previous version only trusted the camera when *somebody* had been recognised, so a
        room containing only strangers reported "no camera evidence" and fell back to a stale
        voice label — which could say the owner was present while they were not in the room.
        Capability to match (``recognition_available``) is what decides who wins, not whether
        this particular frame produced a hit.
        """
        if self.camera.available and self.camera.face_count and self.camera.recognition_available:
            return self.camera.owner_present
        return self.voice.owner_speaking

    @property
    def strangers_present(self) -> bool:
        if self.camera.available and self.camera.face_count:
            return self.camera.stranger_count > 0
        # No camera evidence. When an owner voice is enrolled, a single unnamed voice *is* a
        # stranger: identity is not assumed from "there is only one person talking".
        if self.voice.owner_enrolled:
            return self.voice.active_label != self.voice.owner_label
        return bool(self.voice.active_label == "" and self.voice.speaker_count >= 2)

    @property
    def audience(self) -> int:
        if self.camera.available and self.camera.face_count:
            return self.camera.face_count
        return self.voice.speaker_count

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "voice": self.voice.to_json(),
            "camera": self.camera.to_json(),
            "owner_present": self.owner_present,
            "strangers_present": self.strangers_present,
            "audience": self.audience,
        }


class EtiquettePolicy:
    """Turns presence facts into one of six speaking modes, with a reason.

    The rules are ordered deliberately: each one can only make the assistant *quieter* than
    the rule before it, except the two that raise the volume again (owner alone + addressed,
    and a safety alert). No rule can ever make an action permitted — that is the owner
    gate's job.
    """

    def __init__(self, allow_ambient_alerts: bool = True):
        self.allow_ambient_alerts = allow_ambient_alerts

    def decide(
        self,
        snapshot: PresenceSnapshot,
        text: str = "",
        addressed: bool = False,
        private: bool | None = None,
        urgency: str = "normal",
        voice_enabled: bool = True,
        speaking: bool = False,
    ) -> SpeechDecision:
        private = looks_private(text) if private is None else private
        signals = {
            "addressed": addressed,
            "urgency": urgency,
            "private": private,
            "voice_enabled": voice_enabled,
            "speaking": speaking,
            "audience": snapshot.audience,
            "strangers": snapshot.strangers_present,
            "owner_present": snapshot.owner_present,
            "overlap": snapshot.voice.overlap,
            "active_label": snapshot.voice.active_label,
        }

        def decide(mode: str, reason: str, audible: bool, requires_owner: bool = False, **extra: Any) -> SpeechDecision:
            return SpeechDecision(
                mode=mode,
                reason=reason,
                audible=audible,
                private=private,
                requires_owner=requires_owner,
                audience=snapshot.audience,
                signals={**signals, **extra},
            )

        if not voice_enabled:
            return decide(TEXT_ONLY, "voice is off or muted, so the answer goes to the interface", False)
        if snapshot.voice.overlap and not addressed:
            return decide(WAIT, "two voices are talking over each other: waiting for a clear turn", False)
        if snapshot.voice.overlap and addressed:
            return decide(
                TEXT_ONLY,
                "the owner is being talked over; answering aloud now would add a third voice",
                False,
            )
        if private and snapshot.strangers_present:
            if not snapshot.owner_present:
                # The owner is not the one asking, so "put it on the screen" is still handing
                # private material to whoever is in the room. This needs the owner, not a
                # quieter delivery.
                return decide(
                    ASK_OWNER,
                    "private content, nobody verified as the owner: the owner has to confirm "
                    "before this is spoken or shown",
                    False,
                    requires_owner=True,
                )
            return decide(
                TEXT_ONLY,
                "private content and somebody else is present: shown on screen, not read aloud",
                False,
            )
        if snapshot.audience >= 2 and not addressed:
            return decide(
                SILENT,
                "a conversation is already happening between people and nobody addressed me",
                False,
            )
        unverified_speaker = bool(snapshot.voice.active_label) and not snapshot.voice.verified
        if addressed and (snapshot.strangers_present or unverified_speaker) and not snapshot.owner_present:
            # A stranger can ask, but the answer must not read as the owner's business. Private
            # content is not merely shortened here — it is held until the owner confirms.
            if private:
                return decide(
                    ASK_OWNER,
                    "private content requested by a speaker who is not verified as the owner: "
                    "the owner has to confirm before this is spoken or shown",
                    False,
                    requires_owner=True,
                )
            return decide(
                SPEAK_BRIEF,
                "addressed by somebody who is not verified as the owner: short answer, no private detail",
                True,
                requires_owner=True,
            )
        if addressed:
            return decide(SPEAK, "addressed directly, nobody else in the room", True)
        if urgency in {"danger", "critical"} and self.allow_ambient_alerts:
            return decide(
                SPEAK_BRIEF,
                f"{urgency} alert: alerts are spoken even when not addressed, because silence there is worse",
                True,
            )
        if snapshot.audience == 0:
            return decide(SILENT, "nobody is present and the message was not addressed to me", False)
        return decide(SILENT, "ambient utterance, nothing asked of me: staying quiet is the polite default", False)


class PresenceController:
    """Holds the latest evidence from both senses and answers etiquette questions.

    It is a plain object rather than a background thread: the voice session already has a
    capture thread, and a second poller fighting for the microphone is how a quiet room gets
    transcribed as speech. ``observe_voice`` is called with the analysis of each completed
    utterance; ``look`` is called when the fleet actually wants a picture.
    """

    def __init__(self, speakers: Any = None, camera: Any = None, bus: Any = None, policy: EtiquettePolicy | None = None):
        self.speakers = speakers
        self.camera = camera
        self.bus = bus
        self.policy = policy or EtiquettePolicy()
        self.voice = VoiceView()
        self._recent: list[dict[str, Any]] = []

    # -- voice -------------------------------------------------------------------------
    def observe_voice(self, analysis: dict[str, Any]) -> VoiceView:
        """Record the analysis of one utterance (see ``speakers.analyse_*``)."""
        speakers = analysis.get("speakers") or []
        timeline = [row for row in (analysis.get("timeline") or []) if row.get("seconds", 0) >= 0.4]
        active = timeline[-1] if timeline else None
        owner = getattr(self.speakers, "owner", lambda: "")() if self.speakers is not None else ""
        active_label = (active or {}).get("label", "")
        self.voice = VoiceView(
            speaker_count=len(speakers),
            active_track=(active or {}).get("track", ""),
            active_label=active_label,
            active_gender=((active or {}).get("gender") or {}).get("label", "unknown"),
            owner_speaking=bool(owner) and active_label == owner,
            overlap=bool((active or {}).get("overlap")),
            owner_enrolled=bool(owner),
            owner_label=owner,
            verified=bool(owner) and active_label == owner,
            detail=analysis.get("detail", ""),
        )
        self._recent.append({"at": time.time(), "label": self.voice.active_label, "seconds": (active or {}).get("seconds", 0)})
        self._recent = self._recent[-32:]
        if self.bus is not None:
            self.bus.emit(
                VOICE_SPEAKER,
                source="presence",
                speakers=self.voice.speaker_count,
                active=self.voice.active_label or self.voice.active_track,
                gender=self.voice.active_gender,
                owner_speaking=self.voice.owner_speaking,
                overlap=self.voice.overlap,
            )
            if self.voice.overlap:
                self.bus.emit(VOICE_OVERLAP, source="presence", speakers=self.voice.speaker_count)
        return self.voice

    # -- camera ------------------------------------------------------------------------
    def camera_view(self, consent_look: dict[str, Any] | None = None) -> CameraView:
        camera = self.camera
        if camera is None:
            return CameraView(available=False, detail="no camera service is attached")
        status = camera.status()
        consent = status.get("consent", {})
        view = CameraView(
            available=bool(consent.get("allows_capture")) and bool(status.get("can_detect")),
            consent=consent.get("state", "unknown"),
            recognition=bool(status.get("can_recognise")),
            recognition_available=bool(status.get("can_recognise")),
            detail=status.get("reason", "") or f"consent {consent.get('state', 'unknown')}",
        )
        look = consent_look or (camera.last_look.to_json() if getattr(camera, "last_look", None) else None)
        if look:
            view.face_count = int(look.get("face_count", 0))
            view.owner_present = bool(look.get("owner_present"))
            view.stranger_count = int(look.get("stranger_count", 0))
            view.recognition = bool(look.get("recognition", view.recognition))
            view.recognition_available = bool(look.get("recognition_available", view.recognition_available))
        return view

    def look(self) -> CameraView:
        """Take one look through the camera, reporting a refusal instead of raising."""
        if self.camera is None:
            return CameraView(available=False, detail="no camera service is attached")
        try:
            self.camera.look(detail="presence")
        except Exception as exc:  # noqa: BLE001 - a blocked camera is a state, not a crash
            view = self.camera_view()
            view.available = False
            view.detail = str(exc)
            return view
        return self.camera_view()

    # -- both --------------------------------------------------------------------------
    def snapshot(self, look: bool = False) -> PresenceSnapshot:
        view = self.look() if look else self.camera_view()
        return PresenceSnapshot(voice=self.voice, camera=view)

    def decide(
        self,
        text: str = "",
        addressed: bool = False,
        private: bool | None = None,
        urgency: str = "normal",
        voice_enabled: bool = True,
        speaking: bool = False,
        look: bool = False,
    ) -> SpeechDecision:
        snapshot = self.snapshot(look=look)
        decision = self.policy.decide(
            snapshot,
            text=text,
            addressed=addressed,
            private=private,
            urgency=urgency,
            voice_enabled=voice_enabled,
            speaking=speaking,
        )
        if self.bus is not None:
            self.bus.emit(
                PRESENCE_DECISION,
                source="presence",
                mode=decision.mode,
                audible=decision.audible,
                reason=decision.reason,
                audience=decision.audience,
                private=decision.private,
                addressed=addressed,
            )
        return decision

    def human_conversation(self) -> bool:
        """True when at least two named/unnamed voices spoke recently to each other."""
        cutoff = time.time() - RECENT_WINDOW_SECONDS
        recent = [row for row in self._recent if row["at"] >= cutoff]
        labels = {row["label"] for row in recent if row["label"]}
        return len(recent) >= 2 and (len(labels) >= 2 or not labels)

    def snapshot_json(self) -> dict[str, Any]:
        data = self.snapshot().to_json()
        data["human_conversation"] = self.human_conversation()
        data["recent_turns"] = [
            {"at": row["at"], "label": row["label"], "seconds": row["seconds"]}
            for row in self._recent[-8:]
        ]
        return data
