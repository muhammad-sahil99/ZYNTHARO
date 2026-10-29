"""Speaker recognition: who is talking, how many people, and how sure we are.

The requirement is "differentiate between humans that are talking, and tell male from
female". Three things have to be separated, because conflating them is how this feature
becomes a lie:

1. **Diarization** — how many distinct voices are in this recording, and which segments
   belong to which. This is solvable with cheap features (pitch, spectral shape, energy
   envelope) and is what makes "two people are talking" possible at all.
2. **Identification** — is *this* voice one we have enrolled (the owner, a colleague)?
   That is a comparison against stored signatures, never a guess from a name list.
3. **Gender estimate** — a *heuristic over fundamental frequency*. It is reported with a
   confidence and can say ``unknown``. It is never used to grant or refuse permission, and
   the docstring on :func:`describe_gender` says why: pitch ranges overlap heavily between
   people, and children, whispering, shouting and singing all move the estimate without
   the person changing.

What this module deliberately does **not** claim: separating two people who speak at the
same time on one microphone. That needs a separation model (a neural source separator).
Here overlap is *detected and flagged* — segments where pitch and spectrum jump inside a
single energy envelope — and those segments are marked ``uncertain`` rather than attributed
to somebody, because a wrong attribution is worse than an admitted gap.

Everything is stdlib-only so the core install still has no dependencies: the live path
uses the energy VAD already in :mod:`zyntharo.voice.session`, and this module analyses the
buffered utterance (``analyse_pcm``) or a real recording (``analyse_wav``).
"""

from __future__ import annotations

import array
import json
import math
import os
import sys
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SAMPLE_RATE = 16_000
FRAME_MS = 30
#: Below this RMS (16-bit full scale) a frame is silence, not a quiet speaker.
SILENCE_RMS = 180.0
MIN_SEGMENT_SECONDS = 0.30
MIN_PITCH_HZ = 60.0
MAX_PITCH_HZ = 420.0
#: Cosine distance under which two segments are the same voice. Tuned on the synthetic
#: corpus in the tests; a real deployment should record its own and keep this configurable.
SAME_SPEAKER_DISTANCE = 0.22


# --------------------------------------------------------------------------------------
# PCM plumbing
# --------------------------------------------------------------------------------------


def pcm_to_samples(pcm: bytes) -> array.array:
    """16-bit little-endian mono PCM -> samples. Truncates a trailing odd byte."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":  # pragma: no cover - big-endian hosts are exotic but real
        samples.byteswap()
    return samples


def _resample(samples: Sequence[float], rate: int, target: int = SAMPLE_RATE) -> list[float]:
    """Linear resampling. Adequate for pitch and spectral shape, not for hi-fi playback."""
    if rate == target or not samples:
        return list(samples)
    ratio = target / float(rate)
    out: list[float] = []
    last = len(samples) - 1
    for index in range(int(len(samples) * ratio)):
        position = index / ratio
        left = int(position)
        frac = position - left
        a = samples[min(left, last)]
        b = samples[min(left + 1, last)]
        out.append(a + (b - a) * frac)
    return out


def read_wav(path: str | os.PathLike[str]) -> tuple[list[float], int]:
    """Read a PCM WAV into mono float samples at :data:`SAMPLE_RATE`.

    Refuses formats it cannot honestly analyse instead of pretending: a float or 24-bit
    file would produce meaningless features, and meaningless features produce a confident
    wrong answer about who is speaking.
    """
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"only 16-bit PCM is supported (this file is {width * 8}-bit)")
    samples = pcm_to_samples(raw)
    mono: list[float] = []
    if channels == 2:
        for index in range(0, len(samples) - 1, 2):
            mono.append((samples[index] + samples[index + 1]) / 2.0)
    else:
        mono = [float(value) for value in samples]
    return _resample(mono, rate), SAMPLE_RATE


# --------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class FrameFeatures:
    rms: float = 0.0
    zcr: float = 0.0
    pitch_hz: float = 0.0  # 0.0 means unvoiced
    centroid_hz: float = 0.0

    @property
    def voiced(self) -> bool:
        return self.pitch_hz > 0.0


def _rms(frame: Sequence[float]) -> float:
    if not frame:
        return 0.0
    return math.sqrt(sum(value * value for value in frame) / len(frame))


def _zcr(frame: Sequence[float]) -> float:
    if len(frame) < 2:
        return 0.0
    crossings = sum(1 for a, b in zip(frame, frame[1:]) if (a < 0.0) != (b < 0.0))
    return crossings / (len(frame) - 1)


def _pitch(frame: Sequence[float], rate: int) -> float:
    """Autocorrelation pitch on a decimated copy of the frame.

    The decimation (2x) and the coarse lag step are what keep this usable in pure Python:
    a naive full-rate autocorrelation costs millions of multiplies per second of audio.
    The estimate is refined by one sample either side, which is ~3 Hz at 16 kHz — enough
    to separate a 110 Hz voice from a 220 Hz one and to track pitch movement.
    """
    decimated = frame[::2]
    effective = rate / 2.0
    mean = sum(decimated) / max(1, len(decimated))
    centred = [value - mean for value in decimated]
    energy = sum(value * value for value in centred)
    if energy <= 0.0:
        return 0.0
    min_lag = max(2, int(effective / MAX_PITCH_HZ))
    max_lag = min(len(centred) - 2, int(effective / MIN_PITCH_HZ))
    if max_lag <= min_lag:
        return 0.0
    best_lag, best_score = 0, 0.0
    for lag in range(min_lag, max_lag + 1, 2):
        score = 0.0
        limit = len(centred) - lag
        for index in range(0, limit, 2):
            score += centred[index] * centred[index + lag]
        if score > best_score:
            best_lag, best_score = lag, score
    if best_lag == 0:
        return 0.0
    refined = best_lag
    for candidate in (best_lag - 1, best_lag + 1):
        if candidate <= 0 or candidate >= len(centred):
            continue
        score = 0.0
        limit = len(centred) - candidate
        for index in range(0, limit, 2):
            score += centred[index] * centred[index + candidate]
        if score > best_score:
            best_score, refined = score, candidate
    # Normalised correlation: a weak periodic signal must not be reported as a clear pitch.
    normaliser = energy * (1.0 if refined == 0 else 1.0)
    if best_score / normaliser < 0.20:
        return 0.0
    return round(effective / refined, 1)


def _centroid(frame: Sequence[float], rate: int, bins: int = 16) -> float:
    """Spectral centroid over log-spaced bins of a short window (256 samples max)."""
    window = frame[:256]
    if len(window) < 32:
        return 0.0
    size = len(window)
    low, high = 80.0, min(rate / 2.0, 6000.0)
    total_weight = 0.0
    weighted = 0.0
    for index in range(bins):
        fraction = index / (bins - 1)
        frequency = low * ((high / low) ** fraction)
        omega = 2.0 * math.pi * frequency / rate
        real = 0.0
        imaginary = 0.0
        for n, sample in enumerate(window):
            angle = omega * n
            real += sample * math.cos(angle)
            imaginary += sample * math.sin(angle)
        magnitude = math.hypot(real, imaginary)
        total_weight += magnitude
        weighted += magnitude * frequency
    if total_weight <= 0.0:
        return 0.0
    return round(weighted / total_weight, 1)


def frame_features(pcm: bytes, rate: int = SAMPLE_RATE) -> list[FrameFeatures]:
    """Split PCM into :data:`FRAME_MS` frames and measure each one."""
    samples = pcm_to_samples(pcm)
    if rate != SAMPLE_RATE:
        samples = array.array("h", [int(round(v)) for v in _resample([float(s) for s in samples], rate)])
    size = int(rate * FRAME_MS / 1000)
    features: list[FrameFeatures] = []
    for start in range(0, len(samples) - size + 1, size):
        frame = [float(value) for value in samples[start : start + size]]
        rms = _rms(frame)
        if rms < SILENCE_RMS:
            features.append(FrameFeatures(rms=rms))
            continue
        features.append(
            FrameFeatures(rms=rms, zcr=_zcr(frame), pitch_hz=_pitch(frame, rate), centroid_hz=_centroid(frame, rate))
        )
    return features


# --------------------------------------------------------------------------------------
# Signatures
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class VoiceSignature:
    """A compact description of one voice, stable across sentences.

    ``pitch_hz`` and ``centroid_hz`` carry the identity; ``pitch_spread`` and ``energy_db``
    are context and are excluded from the distance vector so a quiet sentence is not
    mistaken for a different person.
    """

    pitch_hz: float = 0.0
    pitch_spread: float = 0.0
    centroid_hz: float = 0.0
    zcr: float = 0.0
    energy_db: float = -60.0
    voiced_ratio: float = 0.0
    seconds: float = 0.0
    frames: int = 0

    def vector(self) -> tuple[float, ...]:
        # log-pitch and log-centroid: pitch perception and spectral shape are both
        # multiplicative, so a linear vector makes a 200 Hz mismatch dominate everything.
        return (
            math.log2(max(self.pitch_hz, 40.0) / 100.0),
            math.log2(max(self.centroid_hz, 100.0) / 800.0),
            self.zcr * 2.0,
        )

    def distance(self, other: "VoiceSignature") -> float:
        """0.0 = identical voice. Weighted distance on the identity vector.

        Cosine similarity was the obvious choice and it is the wrong one here: two voices an
        octave apart can point in nearly the same direction, so a 130 Hz speaker and a 230 Hz
        speaker came back as one person. The components below are weighted by how much they
        actually distinguish people — pitch first, spectral shape second, zero-crossing last —
        and each term is capped so one wild reading cannot dominate.
        """
        return _weighted_distance(self.vector(), other.vector())

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "VoiceSignature":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})

    @property
    def usable(self) -> bool:
        # Duration matters as much as voicing: 60 ms of clear speech has a pitch and a shape,
        # and is still nowhere near enough to tell two people apart.
        return self.voiced_ratio >= 0.25 and self.pitch_hz > 0.0 and self.seconds >= MIN_SEGMENT_SECONDS


def _weighted_distance(left: Sequence[float], right: Sequence[float]) -> float:
    """Compare two identity vectors: pitch dominates, spectrum refines, zcr breaks ties."""
    spans = (0.5, 0.8, 0.2)
    weights = (0.6, 0.25, 0.15)
    total = 0.0
    for a, b, span, weight in zip(left, right, spans, weights):
        total += weight * min(1.0, abs(a - b) / span)
    return round(min(1.0, total), 4)


def signature_from_frames(features: Sequence[FrameFeatures], frame_ms: int = FRAME_MS) -> VoiceSignature:
    """Aggregate voiced frames into one signature (median pitch, mean spectral shape)."""
    voiced = [item for item in features if item.voiced]
    if not voiced:
        return VoiceSignature(frames=len(features), seconds=round(len(features) * frame_ms / 1000.0, 2))
    pitches = sorted(item.pitch_hz for item in voiced)
    median = pitches[len(pitches) // 2]
    mean = sum(pitches) / len(pitches)
    spread = math.sqrt(sum((value - mean) ** 2 for value in pitches) / len(pitches))
    energies = [item.rms for item in features if item.rms > 0]
    loudest = max(energies) if energies else 0.0
    return VoiceSignature(
        pitch_hz=round(median, 1),
        pitch_spread=round(spread, 1),
        centroid_hz=round(sum(item.centroid_hz for item in voiced) / len(voiced), 1),
        zcr=round(sum(item.zcr for item in voiced) / len(voiced), 4),
        energy_db=round(20.0 * math.log10(max(loudest, 1e-9) / 32768.0), 1),
        voiced_ratio=round(len(voiced) / max(1, len(features)), 3),
        seconds=round(len(features) * frame_ms / 1000.0, 2),
        frames=len(features),
    )


# --------------------------------------------------------------------------------------
# Gender estimate
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class GenderGuess:
    label: str  # male | female | unknown
    confidence: float
    basis: str

    def to_json(self) -> dict[str, Any]:
        return {"label": self.label, "confidence": self.confidence, "basis": self.basis}


def describe_gender(signature: VoiceSignature) -> GenderGuess:
    """Estimate male/female from median pitch — and say ``unknown`` when it cannot.

    Adult male speech sits roughly at 85-180 Hz and adult female speech at 165-255 Hz, so
    the two bands *overlap*. Inside the overlap the honest answer is "unknown", and the
    whole estimate is worthless for a child, a whisper, a shout or singing. It is therefore
    reported as a guess with a basis string, is never stored as a fact about a person, and
    is **never** consulted by the capability gate or the owner gate: an assistant that
    decides who may do what from a pitch estimate would be trivially fooled and would hurt
    the people whose voices it measures badly.
    """
    pitch = signature.pitch_hz
    if pitch <= 0.0 or signature.voiced_ratio < 0.25:
        return GenderGuess("unknown", 0.0, "no stable voiced audio to measure pitch on")
    if pitch < 150.0:
        margin = (150.0 - pitch) / 150.0
        return GenderGuess("male", round(min(0.95, 0.5 + margin * 0.6), 2), f"median f0 {pitch:.0f} Hz is inside the adult male band")
    if pitch > 180.0:
        margin = (pitch - 180.0) / 180.0
        return GenderGuess("female", round(min(0.95, 0.5 + margin * 0.6), 2), f"median f0 {pitch:.0f} Hz is inside the adult female band")
    return GenderGuess(
        "unknown",
        0.0,
        f"median f0 {pitch:.0f} Hz sits in the male/female overlap: this reading cannot decide it",
    )


# --------------------------------------------------------------------------------------
# Diarization
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Segment:
    index: int
    start: float
    end: float
    signature: VoiceSignature
    gender: GenderGuess
    track: str = ""
    label: str = ""  # enrolled name, when the voice matches somebody we know
    distance: float = 1.0
    uncertain: bool = False
    reason: str = ""
    overlap: bool = False

    @property
    def seconds(self) -> float:
        return round(self.end - self.start, 2)

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "seconds": self.seconds,
            "track": self.track,
            "label": self.label,
            "gender": self.gender.to_json(),
            "pitch_hz": self.signature.pitch_hz,
            "centroid_hz": self.signature.centroid_hz,
            "distance": self.distance,
            "uncertain": self.uncertain,
            "overlap": self.overlap,
            "reason": self.reason,
        }


class Diarizer:
    """Online clustering of voiced segments into speaker tracks.

    Online, not batch: a live conversation has to be labelled as it arrives, and a batch
    pass over the whole recording is not available mid-sentence. A new segment joins the
    nearest existing track when the cosine distance is under :data:`SAME_SPEAKER_DISTANCE`,
    otherwise it starts a new track — up to ``max_speakers``, after which the nearest track
    is used and the segment is marked uncertain (refusing to invent a ninth speaker).
    """

    def __init__(self, threshold: float = SAME_SPEAKER_DISTANCE, max_speakers: int = 8):
        self.threshold = threshold
        self.max_speakers = max_speakers
        self._tracks: dict[str, list[float]] = {}
        self._counts: dict[str, int] = {}

    def _distance_to(self, vector: Sequence[float], centroid: Sequence[float]) -> float:
        return _weighted_distance(vector, centroid)

    def assign(self, signature: VoiceSignature) -> tuple[str, float]:
        vector = signature.vector()
        best, best_distance = "", 1.0
        for label, centroid in self._tracks.items():
            distance = self._distance_to(vector, centroid)
            if distance < best_distance:
                best, best_distance = label, distance
        if best and best_distance <= self.threshold:
            count = self._counts[best]
            self._tracks[best] = [(old * count + new) / (count + 1) for old, new in zip(self._tracks[best], vector)]
            self._counts[best] = count + 1
            return best, round(best_distance, 3)
        if len(self._tracks) >= self.max_speakers:
            return best or "speaker_?", round(best_distance, 3)
        label = f"speaker_{len(self._tracks) + 1}"
        self._tracks[label] = list(vector)
        self._counts[label] = 1
        return label, 0.0

    def tracks(self) -> dict[str, int]:
        return dict(self._counts)


# --------------------------------------------------------------------------------------
# Enrolled people
# --------------------------------------------------------------------------------------


class SpeakerRegistry:
    """People whose voice we know, stored as signatures — never as audio.

    The owner is a *label* here, not a permission: enrolment says "this sounds like Ayesha",
    and the owner gate in :mod:`zyntharo.ops.owner` still decides what Ayesha may do. Keeping
    the two apart means a cloned voice can at most be misaddressed, not authorised.
    """

    def __init__(self, home: str | os.PathLike[str], bus: Any = None, threshold: float = 0.20):
        self.home = Path(home)
        self.bus = bus
        self.threshold = threshold
        self.people: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self.home / "speakers.json"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.people = dict(data.get("people") or {})

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "people": self.people, "updated_at": time.time()}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)  # biometric-adjacent data: owner-readable only
        except OSError:
            pass
        return self.path

    def enroll(self, label: str, signature: VoiceSignature, kind: str = "guest", note: str = "") -> dict[str, Any]:
        name = (label or "").strip()
        if not name:
            raise ValueError("a speaker needs a name")
        if not signature.usable:
            raise ValueError(
                "not enough voiced audio to enrol: record at least a second of clear speech "
                f"(voiced ratio was {signature.voiced_ratio:.2f})"
            )
        existing = self.people.get(name, {})
        samples = int(existing.get("samples", 0)) + 1
        gender = describe_gender(signature)
        self.people[name] = {
            "label": name,
            "kind": kind if kind in {"owner", "guest", "household"} else "guest",
            "signature": signature.to_json(),
            "gender_guess": gender.label,
            "samples": samples,
            "updated_at": time.time(),
            "note": note or existing.get("note", ""),
        }
        self.save()
        return {"label": name, "kind": self.people[name]["kind"], "samples": samples, "gender_guess": gender.label}

    def identify(self, signature: VoiceSignature) -> tuple[str, float]:
        best, best_distance = "", 1.0
        for label, record in self.people.items():
            distance = signature.distance(VoiceSignature.from_json(record.get("signature") or {}))
            if distance < best_distance:
                best, best_distance = label, distance
        if best and best_distance <= self.threshold:
            return best, round(best_distance, 3)
        return "", round(best_distance, 3)

    def set_owner(self, label: str) -> dict[str, Any]:
        if label not in self.people:
            raise ValueError(f"{label!r} is not enrolled")
        for record in self.people.values():
            if record.get("kind") == "owner":
                record["kind"] = "household"
        self.people[label]["kind"] = "owner"
        self.save()
        return {"owner": label}

    def owner(self) -> str:
        for label, record in self.people.items():
            if record.get("kind") == "owner":
                return label
        return ""

    def forget(self, label: str) -> bool:
        removed = self.people.pop(label, None) is not None
        if removed:
            self.save()
        return removed

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "label": record.get("label", label),
                "kind": record.get("kind", "guest"),
                "gender_guess": record.get("gender_guess", "unknown"),
                "samples": int(record.get("samples", 0)),
                "pitch_hz": VoiceSignature.from_json(record.get("signature") or {}).pitch_hz,
                "updated_at": record.get("updated_at", 0.0),
            }
            for label, record in sorted(self.people.items())
        ]


# --------------------------------------------------------------------------------------
# Analysis of a real recording
# --------------------------------------------------------------------------------------


def _segment_bounds(features: Sequence[FrameFeatures], silence_frames: int = 4) -> list[tuple[int, int]]:
    """Group voiced frames into runs, split by ~120 ms of silence."""
    segments: list[tuple[int, int]] = []
    start: int | None = None
    silence = 0
    for index, item in enumerate(features):
        if item.voiced:
            if start is None:
                start = index
            silence = 0
        elif start is not None:
            silence += 1
            if silence >= silence_frames:
                segments.append((start, index - silence + 1))
                start, silence = None, 0
    if start is not None:
        segments.append((start, len(features)))
    return segments


def _probable_overlap(features: Sequence[FrameFeatures]) -> bool:
    """Flag a segment whose pitch jumps inside one continuous energy envelope.

    This detects that *something* is off, not who is speaking: a clean hand-off between two
    people usually has a pause, while talking over each other keeps the energy up and moves
    the pitch by an octave and back. Attributing such a segment to one person would be a
    fabrication, so callers mark it uncertain.
    """
    voiced = [item.pitch_hz for item in features if item.voiced]
    if len(voiced) < 8:
        return False
    jumps = 0
    for previous, current in zip(voiced, voiced[1:]):
        if previous <= 0 or current <= 0:
            continue
        ratio = max(previous, current) / min(previous, current)
        if ratio >= 1.6:
            jumps += 1
    return jumps >= 2


def analyse_pcm(
    pcm: bytes,
    rate: int = SAMPLE_RATE,
    registry: SpeakerRegistry | None = None,
    max_speakers: int = 8,
) -> dict[str, Any]:
    """Diarize a buffer of speech and name whichever voices are enrolled."""
    features = frame_features(pcm, rate)
    frame_seconds = FRAME_MS / 1000.0
    diarizer = Diarizer(max_speakers=max_speakers)
    timeline: list[Segment] = []
    too_short = 0
    for index, (begin, end) in enumerate(_segment_bounds(features)):
        chunk = features[begin:end]
        signature = signature_from_frames(chunk)
        seconds = len(chunk) * frame_seconds
        if seconds < MIN_SEGMENT_SECONDS or not signature.usable:
            too_short += 1
            continue
        gender = describe_gender(signature)
        track, distance = diarizer.assign(signature)
        label, match_distance = (registry.identify(signature) if registry is not None else ("", 1.0))
        overlap = _probable_overlap(chunk)
        timeline.append(
            Segment(
                index=index,
                start=round(begin * frame_seconds, 2),
                end=round(end * frame_seconds, 2),
                signature=signature,
                gender=gender,
                track=track,
                label=label,
                distance=match_distance,
                uncertain=overlap,
                reason="probable overlapping speech: not attributed to one person" if overlap else "",
                overlap=overlap,
            )
        )
    speakers: list[dict[str, Any]] = []
    for track in sorted({row.track for row in timeline}):
        rows = [row for row in timeline if row.track == track]
        named = [row for row in rows if row.label]
        longest = max(rows, key=lambda row: row.seconds)
        label = named[0].label if named else ""
        kind = ""
        if label and registry is not None:
            kind = (registry.people.get(label) or {}).get("kind", "guest")
        speakers.append(
            {
                "track": track,
                "label": label,
                "kind": kind,
                "gender": longest.gender.to_json(),
                "seconds": round(sum(row.seconds for row in rows), 2),
                "segments": len(rows),
                "pitch_hz": longest.signature.pitch_hz,
                "centroid_hz": longest.signature.centroid_hz,
                "uncertain_segments": sum(1 for row in rows if row.uncertain),
            }
        )
    voiced_ratio = sum(1 for item in features if item.voiced) / max(1, len(features))
    return {
        "detail": "text",
        "seconds": round(len(features) * frame_seconds, 2),
        "voiced_ratio": round(voiced_ratio, 3),
        "segment_count": len(timeline),
        "skipped_short_segments": too_short,
        "speaker_count": len(speakers),
        "speakers": speakers,
        "timeline": [row.to_json() for row in timeline],
        "overlaps": sum(1 for row in timeline if row.overlap),
        "named": sum(1 for row in speakers if row["label"]),
        "note": (
            "Genders are pitch heuristics and can say unknown; overlapping speech is flagged "
            "rather than split (separation needs a dedicated model)."
        ),
    }


def analyse_wav(path: str | os.PathLike[str], registry: SpeakerRegistry | None = None, max_speakers: int = 8) -> dict[str, Any]:
    samples, rate = read_wav(path)
    pcm = array.array("h", [int(max(-32768, min(32767, round(value)))) for value in samples]).tobytes()
    if not pcm:
        raise ValueError(f"{path} contains no audio frames")
    result = analyse_pcm(pcm, rate, registry=registry, max_speakers=max_speakers)
    result["path"] = str(path)
    return result
