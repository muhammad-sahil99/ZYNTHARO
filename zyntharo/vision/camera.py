"""Camera presence: who is in the room, and why there is deliberately no way to bypass consent.

The requirement includes "no user permission required if the user has turned off
permissions". That part is refused, and the refusal is in the code rather than in a
comment: :meth:`CameraService.look` raises :class:`CameraDenied` when consent is missing or
the OS says the camera is blocked, and there is **no** ``force`` parameter, environment
variable or config key that skips the check. This is not timidity about a feature request.
Covert capture of a room is spyware by construction: it produces footage of people who did
not agree to be filmed (visitors, children, colleagues), it breaks the law in most
jurisdictions, and it makes the whole product unsignable and undefendable. A camera that
lies about being on is also how the *owner* gets recorded without knowing.

What is implemented instead is the honest version, which is more useful anyway:

* **Consent is a stored decision, made by the owner, shown in the interface**, with the
  OS-level permission reported alongside it. When the OS denies the camera, the tool says
  exactly that and how to allow it — and keeps working with voice-only presence.
* **Presence, not surveillance.** Frames live in memory for the duration of a look; they are
  never written to disk, never put in the audit log and never shipped to a model. What is
  stored is a face *embedding* per enrolled person, plus counts.
* **Two honest tiers.** Detection (a face-shaped box) and recognition (an embedding that can
  be matched to an enrolled person) are different dependencies. With only OpenCV we can say
  "one person is here"; identity matching reports itself unavailable rather than guessing
  from a blurry box.
* **Multiple people are a first-class case.** The count, and whether the owner is among
  them, is what the etiquette layer needs to decide whether speaking aloud would leak
  private information to a stranger.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..events import CAMERA_FACE, CAMERA_LOOK, CAMERA_STATE

CONSENT_GRANTED = "granted"
CONSENT_DENIED = "denied"
CONSENT_REVOKED = "revoked"
CONSENT_UNKNOWN = "unknown"

#: OS-level permission states, as observed — never as assumed.
OS_GRANTED = "granted"
OS_DENIED = "denied"
OS_UNKNOWN = "unknown"


class CameraDenied(PermissionError):
    """Capture was refused: no consent, or the operating system blocks the camera.

    Carries the reason and the remedy, because a refusal the user cannot act on is a dead
    end. There is intentionally no way to construct this as a warning.
    """

    def __init__(self, reason: str, remedy: str = ""):
        super().__init__(f"{reason}{(' — ' + remedy) if remedy else ''}")
        self.reason = reason
        self.remedy = remedy


class CameraOsDenied(CameraDenied):
    """The operating system refused the camera (privacy setting, device in use, policy).

    Separate from a plain consent refusal because it is *evidence about the machine*: it is
    recorded into the consent file, so the interface says "os denied" instead of retrying a
    permission that cannot be granted from inside this program.
    """


@dataclass(slots=True)
class Consent:
    state: str = CONSENT_UNKNOWN
    granted_at: float = 0.0
    scope: str = "presence"
    os_permission: str = OS_UNKNOWN
    decided_by: str = ""
    note: str = ""

    @property
    def allows_capture(self) -> bool:
        return self.state == CONSENT_GRANTED and self.os_permission != OS_DENIED

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "granted_at": self.granted_at,
            "scope": self.scope,
            "os_permission": self.os_permission,
            "decided_by": self.decided_by,
            "note": self.note,
            "allows_capture": self.allows_capture,
        }


@dataclass(slots=True)
class Face:
    """One detected face. ``embedding`` is empty when recognition is unavailable."""

    box: tuple[int, int, int, int]
    embedding: tuple[float, ...] = ()
    score: float = 0.0
    label: str = ""
    kind: str = "unknown"

    @property
    def matched(self) -> bool:
        return bool(self.label)

    def to_json(self) -> dict[str, Any]:
        return {
            "box": list(self.box),
            "has_embedding": bool(self.embedding),
            "label": self.label,
            "kind": self.kind,
            "score": round(self.score, 3),
        }


@dataclass(slots=True)
class Look:
    """The result of one look: what is in front of the camera, as facts only."""

    at: float = field(default_factory=time.time)
    face_count: int = 0
    faces: list[Face] = field(default_factory=list)
    owner_present: bool = False
    stranger_count: int = 0
    #: True when matching ran at all (an embedding model is installed), independent of whether
    #: anybody in frame was recognised. Conflating the two makes a room full of strangers look
    #: like "recognition unavailable" and hands the decision back to stale voice evidence.
    recognition: bool = False
    recognition_available: bool = False
    detail: str = ""
    frame_bytes: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "face_count": self.face_count,
            "faces": [face.to_json() for face in self.faces],
            "owner_present": self.owner_present,
            "stranger_count": self.stranger_count,
            "recognition": self.recognition,
            "recognition_available": self.recognition_available,
            "detail": self.detail,
            "frame_bytes": self.frame_bytes,
        }


# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------


def _have(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken environment
        return False


@dataclass(slots=True)
class Detector:
    """What the machine can actually do with a frame."""

    detect: Callable[[Any], list[Face]] | None = None
    embed: Callable[[Any, tuple[int, int, int, int]], tuple[float, ...]] | None = None
    backend: str = ""
    reason: str = ""

    @property
    def can_detect(self) -> bool:
        return self.detect is not None

    @property
    def can_recognise(self) -> bool:
        return self.embed is not None


def opencv_detector() -> Detector:
    """OpenCV + a Haar cascade: detection only, unless an embedding model is installed.

    OpenCV's own face recognisers need ``opencv-contrib`` and expect a trained model per
    person, which is not an identity embedding. So recognition is only claimed when
    ``face_recognition`` (dlib) is present, whose 128-d embeddings are stable enough to
    compare. Anything else would be a guess dressed as recognition.
    """
    if not _have("cv2"):
        return Detector(backend="none", reason="OpenCV (cv2) is not installed: pip install opencv-python")
    try:  # pragma: no cover - optional dependency
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(str(cascade_path))

        def detect(frame: Any) -> list[Face]:
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            boxes = cascade.detectMultiScale(grey, scaleFactor=1.15, minNeighbors=5, minSize=(48, 48))
            return [Face(box=(int(x), int(y), int(w), int(h))) for (x, y, w, h) in boxes]

        embed = None
        backend = "opencv-haar"
        if _have("face_recognition"):  # pragma: no cover - optional dependency
            import face_recognition  # type: ignore

            def embed(frame: Any, box: tuple[int, int, int, int]) -> tuple[float, ...]:
                x, y, w, h = box
                rgb = cv2.cvtColor(frame[y : y + h, x : x + w], cv2.COLOR_BGR2RGB)
                encodings = face_recognition.face_encodings(np.asarray(rgb))
                return tuple(float(value) for value in encodings[0]) if encodings else ()

            backend = "opencv-haar+dlib"

        return Detector(detect=detect, embed=embed, backend=backend, reason="")
    except Exception as exc:  # pragma: no cover - optional dependency
        return Detector(backend="none", reason=f"OpenCV is installed but unusable: {exc}")


def default_frame_source() -> Any:  # pragma: no cover - needs a camera
    """Grab one BGR frame from the default camera, or raise with the OS reason."""
    try:
        import cv2  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise CameraDenied("no camera backend is installed", "pip install opencv-python") from exc
    capture = cv2.VideoCapture(0)
    if not capture or not capture.isOpened():
        capture and capture.release()
        raise CameraOsDenied(
            "the operating system did not give this process a camera",
            "allow camera access for ZYNTHARO in the OS privacy settings, then try again",
        )
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise CameraDenied("the camera opened but delivered no frame", "close other apps that hold the camera")
    return frame


# --------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------


class CameraService:
    """Consent-gated presence camera with a face registry that stores embeddings only."""

    def __init__(
        self,
        home: str | os.PathLike[str],
        bus: Any = None,
        detector: Detector | None = None,
        frame_source: Callable[[], Any] | None = None,
        match_threshold: float = 0.5,
        owner_label: str = "",
    ):
        self.home = Path(home)
        self.bus = bus
        self.detector = detector if detector is not None else opencv_detector()
        self.frame_source = frame_source or default_frame_source
        #: A score at or above this is a match. See :meth:`_match` for the distance mapping.
        self.match_threshold = match_threshold
        self.owner_label = owner_label
        self.consent = self._load_consent()
        self.people: dict[str, dict[str, Any]] = {}
        self._load_people()
        self.last_look: Look | None = None

    # -- storage -----------------------------------------------------------------------
    @property
    def consent_path(self) -> Path:
        return self.home / "camera_consent.json"

    @property
    def faces_path(self) -> Path:
        return self.home / "faces.json"

    def _load_consent(self) -> Consent:
        if not self.consent_path.exists():
            return Consent()
        try:
            data = json.loads(self.consent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Consent()
        known = {name for name in Consent.__dataclass_fields__}
        consent = Consent(**{key: value for key, value in data.items() if key in known})
        # A consent file written when the OS allowed the camera must not keep claiming so
        # after the permission was revoked outside this application.
        if self._os_permission() == OS_DENIED:
            consent.os_permission = OS_DENIED
        return consent

    def _persist_consent(self) -> None:
        self.consent_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.consent_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.consent.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, self.consent_path)

    def _load_people(self) -> None:
        if not self.faces_path.exists():
            return
        try:
            data = json.loads(self.faces_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.people = dict(data.get("people") or {})

    def save_people(self) -> Path:
        self.faces_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.faces_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "people": self.people}, indent=2), encoding="utf-8")
        os.replace(tmp, self.faces_path)
        try:
            os.chmod(self.faces_path, 0o600)
        except OSError:
            pass
        return self.faces_path

    # -- consent -----------------------------------------------------------------------
    def _os_permission(self) -> str:  # pragma: no cover - depends on the host
        """Ask the OS what it thinks. Unknown hosts are reported as unknown, not allowed."""
        try:
            if os.name == "nt":  # a blocked camera fails to open; we learn at capture time
                return OS_UNKNOWN
            return OS_UNKNOWN
        except Exception:
            return OS_UNKNOWN

    def grant(self, decided_by: str = "owner", note: str = "") -> dict[str, Any]:
        self.consent = Consent(
            state=CONSENT_GRANTED,
            granted_at=time.time(),
            os_permission=self._os_permission(),
            decided_by=decided_by,
            note=note,
        )
        self._persist_consent()
        self._emit(CAMERA_STATE, state=CONSENT_GRANTED, os_permission=self.consent.os_permission)
        return self.consent.to_json()

    def revoke(self, reason: str = "") -> dict[str, Any]:
        """Turn the camera off. Frames already captured are dropped with the look."""
        self.consent = Consent(state=CONSENT_REVOKED, decided_by="owner", note=reason, os_permission=self._os_permission())
        self.last_look = None
        self._persist_consent()
        self._emit(CAMERA_STATE, state=CONSENT_REVOKED, os_permission=self.consent.os_permission)
        return self.consent.to_json()

    def deny(self, reason: str = "the owner declined") -> dict[str, Any]:
        self.consent = Consent(state=CONSENT_DENIED, decided_by="owner", note=reason, os_permission=self._os_permission())
        self._persist_consent()
        self._emit(CAMERA_STATE, state=CONSENT_DENIED, os_permission=self.consent.os_permission)
        return self.consent.to_json()

    def _emit(self, topic: str, **payload: Any) -> None:
        if self.bus is not None:
            try:
                self.bus.emit(topic, source="camera", **payload)
            except Exception:  # noqa: BLE001 - an interface failure must not break the camera
                pass

    # -- looking -----------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """What this machine can do right now, with the reason for anything it cannot."""
        return {
            "consent": self.consent.to_json(),
            "backend": self.detector.backend or "none",
            "can_detect": self.detector.can_detect,
            "can_recognise": self.detector.can_recognise,
            "reason": self.detector.reason,
            "enrolled": len(self.people),
            "owner": self.owner(),
            "retains_frames": False,
        }

    def _require_capture(self) -> None:
        if self.consent.state != CONSENT_GRANTED:
            raise CameraDenied(
                f"camera consent is {self.consent.state!r}, not 'granted'",
                "ask the owner to grant it (camera_consent action); voice-only presence keeps working",
            )
        if self.consent.os_permission == OS_DENIED:
            raise CameraDenied(
                "the operating system is blocking the camera for this process",
                "allow camera access for ZYNTHARO in the OS privacy settings — this application cannot and will not bypass that",
            )
        if not self.detector.can_detect:
            raise CameraDenied(
                f"no camera backend: {self.detector.reason or 'detector unavailable'}",
                "pip install opencv-python (add face_recognition for identity matching)",
            )

    def _match(self, embedding: Sequence[float]) -> tuple[str, str, float]:
        """Nearest enrolled face, with the score mapped so the thresholds agree.

        dlib's published working threshold is a euclidean distance of 0.6. The score below is
        ``1 - distance / 1.2``, so 0.6 maps to 0.5 — which is why the default match threshold
        is 0.5. It used to be 0.55, i.e. distance < 0.54: slightly *stricter* than the model it
        wraps, so a face at the documented boundary was reported as a stranger forever.
        """
        best_label, best_kind, best_score = "", "unknown", 0.0
        for label, record in self.people.items():
            stored = record.get("embedding") or []
            if len(stored) != len(embedding) or not embedding:
                continue
            # dlib embeddings are compared by euclidean distance; 0.6 is the published
            # working threshold, so the score is mapped onto 0..1 for display.
            distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(stored, embedding)))
            score = max(0.0, 1.0 - distance / 1.2)
            if score > best_score:
                best_label, best_kind, best_score = label, record.get("kind", "guest"), score
        if best_score >= self.match_threshold:
            return best_label, best_kind, round(best_score, 3)
        return "", "unknown", round(best_score, 3)

    def look(self, detail: str = "presence") -> Look:
        """One consent-gated look. Frames stay in memory; only counts and labels come out."""
        self._require_capture()
        try:
            frame = self.frame_source()
        except CameraOsDenied:
            # Record what the machine told us: the panel must show "os denied" rather than an
            # idle camera that fails identically on every look.
            self.consent.os_permission = OS_DENIED
            self._persist_consent()
            self._emit(CAMERA_STATE, state=self.consent.state, os_permission=OS_DENIED)
            raise
        except CameraDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken camera must not look like an empty room
            raise CameraDenied(f"the camera could not deliver a frame: {exc}", "check the device, then unplug/replug it") from exc
        faces = list(self.detector.detect(frame) or [])  # type: ignore[misc]
        recognised = 0
        for face in faces:
            if not self.detector.can_recognise or self.detector.embed is None:
                continue
            try:
                embedding = self.detector.embed(frame, face.box)
            except Exception:  # noqa: BLE001 - one bad box must not abort the look
                continue
            if not embedding:
                continue
            face.embedding = tuple(embedding)
            label, kind, score = self._match(embedding)
            face.label, face.kind, face.score = label, kind, score
            recognised += 1 if label else 0
        owner = self.owner()
        owner_present = any(face.label and face.label == owner for face in faces) if owner else False
        strangers = sum(1 for face in faces if not face.label)
        result = Look(
            face_count=len(faces),
            faces=faces,
            owner_present=owner_present,
            stranger_count=strangers,
            recognition=self.detector.can_recognise and recognised > 0,
            recognition_available=self.detector.can_recognise,
            detail=detail,
            frame_bytes=int(getattr(frame, "nbytes", 0) or 0),
        )
        self.last_look = result
        self._emit(
            CAMERA_LOOK,
            face_count=result.face_count,
            owner_present=owner_present,
            strangers=strangers,
            recognised=recognised,
        )
        return result

    # -- people ------------------------------------------------------------------------
    def enroll_face(self, label: str, kind: str = "guest", samples: int = 3) -> dict[str, Any]:
        """Enrol a face from ``samples`` looks, averaging the embeddings.

        Averaging matters: a single frame catches mid-blink, bad light or a turned head, and
        a stored bad embedding makes the person a stranger forever.
        """
        if not (label or "").strip():
            raise ValueError("a face needs a name")
        if not self.detector.can_recognise:
            raise CameraDenied(
                "identity matching is not available: no face-embedding model",
                "pip install face_recognition (dlib) to enrol people; detection alone cannot compare faces",
            )
        collected: list[tuple[float, ...]] = []
        attempts = 0
        while len(collected) < max(1, samples) and attempts < max(1, samples) * 3:
            attempts += 1
            result = self.look(detail=f"enrol {label}")
            for face in result.faces:
                if face.embedding:
                    collected.append(face.embedding)
                    break
        if not collected:
            raise CameraDenied("no face was found in any of the attempted looks", "face the camera, with normal lighting")
        size = len(collected[0])
        averaged = tuple(sum(value[i] for value in collected) / len(collected) for i in range(size))
        self.people[label.strip()] = {
            "label": label.strip(),
            "kind": kind if kind in {"owner", "household", "guest"} else "guest",
            "embedding": list(averaged),
            "samples": len(collected),
            "updated_at": time.time(),
        }
        self.save_people()
        if self.people[label.strip()]["kind"] == "owner":
            self.owner_label = label.strip()
        self._emit(CAMERA_FACE, action="enrolled", label=label.strip(), samples=len(collected))
        return {"label": label.strip(), "samples": len(collected), "kind": self.people[label.strip()]["kind"]}

    def set_owner(self, label: str) -> dict[str, Any]:
        if label not in self.people:
            raise ValueError(f"{label!r} has no enrolled face")
        for record in self.people.values():
            if record.get("kind") == "owner":
                record["kind"] = "household"
        self.people[label]["kind"] = "owner"
        self.owner_label = label
        self.save_people()
        return {"owner": label}

    def owner(self) -> str:
        if self.owner_label and self.owner_label in self.people:
            return self.owner_label
        for label, record in self.people.items():
            if record.get("kind") == "owner":
                return label
        return ""

    def forget_face(self, label: str) -> bool:
        removed = self.people.pop(label, None) is not None
        if removed:
            if self.owner_label == label:
                self.owner_label = ""
            self.save_people()
            self._emit(CAMERA_FACE, action="forgotten", label=label)
        return removed

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "label": record.get("label", label),
                "kind": record.get("kind", "guest"),
                "samples": int(record.get("samples", 0)),
                "updated_at": record.get("updated_at", 0.0),
            }
            for label, record in sorted(self.people.items())
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.status(),
            "people": self.rows(),
            "last_look": self.last_look.to_json() if self.last_look else None,
        }
