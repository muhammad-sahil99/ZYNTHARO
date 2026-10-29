"""Presence actions: who is speaking, who is in the room, and whether to say it out loud.

Four groups of tools, all capability-gated:

* ``speaker_analyse`` / ``speaker_enroll`` / ``speaker_list`` / ``speaker_forget`` — voice
  identification over a real recording (WAV), with enrolment that refuses thin audio.
* ``camera_status`` / ``camera_consent`` / ``camera_look`` / ``camera_enroll`` /
  ``camera_forget`` — the consent-gated camera. Changing consent needs ``camera.enroll``,
  which only the owner-facing ranks hold: an Operative cannot switch a camera on.
* ``presence_observe`` — feed one recorded conversation through the voice sense so the
  etiquette layer has something real to reason about.
* ``presence_report`` / ``presence_decide`` — the fused view, and the decision an agent must
  ask for before speaking aloud.

A refusal here is always actionable and always reports *why*: "no consent", "the OS blocks
the camera", "no face-embedding model", "not enough voiced audio". That is the difference
between a feature that is missing and a feature that lies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..events import SPEAKER_ENROLLED
from ..ops.presence import looks_private
from .registry import ActionContext, ActionFailed, action


@dataclass
class PresenceServices:
    speakers: Any = None
    camera: Any = None
    presence: Any = None
    voice: Any = None
    config: Any = None
    bus: Any = None


SERVICES: PresenceServices | None = None


def set_services(services: PresenceServices | None) -> None:
    global SERVICES
    SERVICES = services


def _services() -> PresenceServices:
    if SERVICES is None:
        raise ActionFailed(
            "presence is not wired yet",
            {"hint": "call actions.presence.set_services(...) during bootstrap"},
        )
    return SERVICES


def _speakers() -> Any:
    speakers = _services().speakers
    if speakers is None:
        raise ActionFailed("no speaker registry is attached to this build")
    return speakers


def _camera() -> Any:
    camera = _services().camera
    if camera is None:
        raise ActionFailed("no camera service is attached to this build")
    return camera


def _presence() -> Any:
    presence = _services().presence
    if presence is None:
        raise ActionFailed("no presence controller is attached to this build")
    return presence


# --------------------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------------------


@action(
    name="speaker_analyse",
    description=(
        "Analyse a WAV recording: how many distinct voices, which segments belong to which "
        "speaker, male/female/unknown per voice, and whether anyone enrolled is in it. "
        "Use before acting on a conversation so you know who you are answering."
    ),
    capability="voice.identify",
    group="voice",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "WAV file (16-bit PCM, mono or stereo)."},
            "max_speakers": {"type": "integer", "description": "Upper bound on distinct voices (default 8)."},
        },
        "required": ["path"],
    },
)
def speaker_analyse(ctx: ActionContext, path: str, max_speakers: int = 8) -> Any:
    from ..voice import speakers as speaker_engine

    try:
        return speaker_engine.analyse_wav(path, registry=_speakers(), max_speakers=max_speakers)
    except ValueError as exc:
        raise ActionFailed(str(exc), {"path": path}) from exc
    except OSError as exc:
        raise ActionFailed(f"could not read {path}: {exc}", {"path": path}) from exc


@action(
    name="speaker_enroll",
    description=(
        "Enrol a person's voice from a WAV recording so they are recognised by name later. "
        "Refuses thin audio (under ~1s of clear speech) instead of storing a bad signature."
    ),
    capability="voice.enroll",
    group="voice",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "path": {"type": "string", "description": "A clear recording of this person speaking."},
            "kind": {"type": "string", "enum": ["owner", "household", "guest"]},
            "make_owner": {"type": "boolean", "description": "Mark this person as the owner voice."},
        },
        "required": ["name", "path"],
    },
)
def speaker_enroll(ctx: ActionContext, name: str, path: str, kind: str = "guest", make_owner: bool = False) -> Any:
    from ..voice import speakers as speaker_engine

    try:
        samples, rate = speaker_engine.read_wav(path)
    except (ValueError, OSError) as exc:
        raise ActionFailed(f"could not read {path}: {exc}", {"path": path}) from exc
    features = speaker_engine.frame_features(
        b"".join(int(max(-32768, min(32767, round(v)))).to_bytes(2, "little", signed=True) for v in samples), rate
    )
    signature = speaker_engine.signature_from_frames(features)
    registry = _speakers()
    try:
        result = registry.enroll(name, signature, kind="owner" if make_owner else kind)
    except ValueError as exc:
        raise ActionFailed(
            str(exc),
            {"path": path, "voiced_ratio": signature.voiced_ratio, "pitch_hz": signature.pitch_hz},
        ) from exc
    if make_owner:
        result.update(registry.set_owner(name))
    services = _services()
    if services.bus is not None:
        services.bus.emit(
            SPEAKER_ENROLLED, source="presence", label=name, kind=result.get("kind"), samples=result.get("samples")
        )
    return result


@action(
    name="speaker_verify",
    description=(
        "Is the owner speaking in this recording? Returns the match score, the enrolled name it "
        "compared against, and an explicit verdict. Refuses when no owner voice is enrolled, "
        "because 'not the owner' and 'nobody enrolled' are different answers."
    ),
    capability="voice.identify",
    group="voice",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "WAV file of one speaker."}},
        "required": ["path"],
    },
)
def speaker_verify(ctx: ActionContext, path: str) -> Any:
    from ..voice import speakers as speaker_engine

    registry = _speakers()
    owner = registry.owner()
    if not owner:
        raise ActionFailed(
            "no owner voice is enrolled, so there is nothing to verify against",
            {"enrolled": [row["label"] for row in registry.rows()], "hint": "speaker_enroll with make_owner=true first"},
        )
    try:
        samples, rate = speaker_engine.read_wav(path)
    except (ValueError, OSError) as exc:
        raise ActionFailed(f"could not read {path}: {exc}", {"path": path}) from exc
    pcm = b"".join(int(max(-32768, min(32767, round(v)))).to_bytes(2, "little", signed=True) for v in samples)
    signature = speaker_engine.signature_from_frames(speaker_engine.frame_features(pcm, rate))
    if not signature.usable:
        raise ActionFailed(
            "not enough clear speech in this recording to verify anybody",
            {"voiced_ratio": signature.voiced_ratio, "seconds": signature.seconds},
        )
    owner_record = registry.people.get(owner) or {}
    owner_signature = speaker_engine.VoiceSignature.from_json(owner_record.get("signature") or {})
    distance = signature.distance(owner_signature)
    verified = distance <= registry.threshold
    return {
        "owner": owner,
        "verified": verified,
        "distance": distance,
        "threshold": registry.threshold,
        "pitch_hz": signature.pitch_hz,
        "verdict": "owner" if verified else "not the enrolled owner",
        "note": "a voice match changes tone and privacy; it never grants a capability",
    }


@action(
    name="speaker_list",
    description="List enrolled voices with their kind, pitch estimate and how many samples back each one.",
    capability="voice.identify",
    group="voice",
    parameters={"type": "object", "properties": {}},
)
def speaker_list(ctx: ActionContext) -> Any:
    registry = _speakers()
    return {
        "count": len(registry.people),
        "owner": registry.owner(),
        "people": registry.rows(),
        "note": "signatures are stored, never audio; a voice name never grants a permission",
    }


@action(
    name="speaker_forget",
    description="Delete an enrolled voice signature (biometric data) permanently.",
    capability="voice.enroll",
    group="voice",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def speaker_forget(ctx: ActionContext, name: str) -> Any:
    registry = _speakers()
    if not registry.forget(name):
        raise ActionFailed(f"{name!r} was not enrolled", {"enrolled": [row["label"] for row in registry.rows()]})
    return {"forgotten": name, "remaining": len(registry.people)}


# --------------------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------------------


@action(
    name="camera_status",
    description=(
        "Report the camera state: consent, OS permission, whether detection and identity "
        "matching are available, and why not when they are not. Never captures anything."
    ),
    capability="camera.view",
    group="camera",
    parameters={"type": "object", "properties": {}},
)
def camera_status(ctx: ActionContext) -> Any:
    return _camera().status()


@action(
    name="camera_consent",
    description=(
        "Record the owner's decision about the camera: grant, revoke or deny. There is no "
        "override for OS-level denial — the tool reports it and voice-only presence continues."
    ),
    capability="camera.enroll",
    group="camera",
    parameters={
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["grant", "revoke", "deny"]},
            "note": {"type": "string"},
        },
        "required": ["decision"],
    },
)
def camera_consent(ctx: ActionContext, decision: str, note: str = "") -> Any:
    camera = _camera()
    who = getattr(getattr(ctx, "identity", None), "name", "") or ctx.agent_id or "owner"
    if decision == "grant":
        return camera.grant(decided_by=who, note=note)
    if decision == "revoke":
        return camera.revoke(note or "revoked by the owner")
    if decision == "deny":
        return camera.deny(note or "the owner declined the camera")
    raise ActionFailed(f"unknown decision {decision!r}", {"allowed": ["grant", "revoke", "deny"]})


@action(
    name="camera_look",
    description=(
        "Take one consent-gated look: how many faces, whether the enrolled owner is among "
        "them, and how many are unrecognised. Frames stay in memory and are not stored."
    ),
    capability="camera.view",
    group="camera",
    parameters={"type": "object", "properties": {"detail": {"type": "string"}}},
)
def camera_look(ctx: ActionContext, detail: str = "presence") -> Any:
    camera = _camera()
    try:
        look = camera.look(detail=detail)
    except PermissionError as exc:  # CameraDenied
        raise ActionFailed(str(exc), {"consent": camera.status().get("consent"), "refused": True}) from exc
    data = look.to_json()
    data["people"] = camera.rows()
    return data


@action(
    name="camera_enroll",
    description="Enrol a face (averaged over several looks) and optionally mark it as the owner's face.",
    capability="camera.enroll",
    group="camera",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["owner", "household", "guest"]},
            "samples": {"type": "integer"},
        },
        "required": ["name"],
    },
)
def camera_enroll(ctx: ActionContext, name: str, kind: str = "guest", samples: int = 3) -> Any:
    camera = _camera()
    try:
        result = camera.enroll_face(name, kind=kind, samples=samples)
    except PermissionError as exc:
        raise ActionFailed(str(exc), {"refused": True, "status": camera.status()}) from exc
    except ValueError as exc:
        raise ActionFailed(str(exc), {"name": name}) from exc
    if kind == "owner":
        result.update(camera.set_owner(name))
    return result


@action(
    name="camera_forget",
    description="Delete an enrolled face embedding permanently.",
    capability="camera.enroll",
    group="camera",
    parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def camera_forget(ctx: ActionContext, name: str) -> Any:
    camera = _camera()
    if not camera.forget_face(name):
        raise ActionFailed(f"{name!r} has no enrolled face", {"enrolled": [row["label"] for row in camera.rows()]})
    return {"forgotten": name, "remaining": len(camera.people)}


# --------------------------------------------------------------------------------------
# Fused presence
# --------------------------------------------------------------------------------------


@action(
    name="presence_observe",
    description=(
        "Feed one recorded conversation through the voice sense: updates who is present, "
        "which voice is active, and whether people are talking over each other."
    ),
    capability="presence.read",
    group="presence",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}, "max_speakers": {"type": "integer"}},
        "required": ["path"],
    },
)
def presence_observe(ctx: ActionContext, path: str, max_speakers: int = 8) -> Any:
    from ..voice import speakers as speaker_engine

    try:
        analysis = speaker_engine.analyse_wav(path, registry=_speakers(), max_speakers=max_speakers)
    except (ValueError, OSError) as exc:
        raise ActionFailed(f"could not analyse {path}: {exc}", {"path": path}) from exc
    view = _presence().observe_voice(analysis)
    return {
        "voice": view.to_json(),
        "speakers": analysis.get("speakers", []),
        "overlap": analysis.get("overlaps", 0),
        "presence": _presence().snapshot_json(),
    }


@action(
    name="presence_report",
    description=(
        "Who is here and who is talking, fused from voice and (consented) camera evidence, "
        "including whether a stranger is present and whether private speech is safe."
    ),
    capability="presence.read",
    group="presence",
    parameters={"type": "object", "properties": {"look": {"type": "boolean", "description": "Consult the camera too."}}},
)
def presence_report(ctx: ActionContext, look: bool = False) -> Any:
    controller = _presence()
    data = controller.snapshot_json() if not look else controller.snapshot(look=True).to_json()
    data["camera_status"] = _camera().status() if _services().camera is not None else None
    return data


@action(
    name="presence_decide",
    description=(
        "Ask whether this reply should be spoken, shortened, shown on screen, or held. Call "
        "before speaking aloud when anyone else may be present. Returns the mode and why."
    ),
    capability="presence.decide",
    group="presence",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What you intend to say."},
            "addressed": {"type": "boolean", "description": "Was the user talking to you specifically?"},
            "private": {"type": "boolean", "description": "Override the private-content heuristic."},
            "urgency": {"type": "string", "enum": ["normal", "notice", "danger", "critical"]},
            "look": {"type": "boolean", "description": "Consult the camera before deciding."},
        },
        "required": ["text"],
    },
)
def presence_decide(
    ctx: ActionContext,
    text: str,
    addressed: bool = False,
    private: bool | None = None,
    urgency: str = "normal",
    look: bool = False,
) -> Any:
    voice = _services().voice
    enabled = bool(getattr(getattr(voice, "state", None), "enabled", True))
    speaking = bool(getattr(getattr(voice, "state", None), "speaking", False))
    decision = _presence().decide(
        text=text,
        addressed=addressed,
        private=looks_private(text) if private is None else private,
        urgency=urgency,
        voice_enabled=enabled,
        speaking=speaking,
        look=look,
    )
    return decision.to_json()
