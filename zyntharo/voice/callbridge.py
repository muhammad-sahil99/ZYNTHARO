"""Call audio bridge: put ZYNTHARO's voice into a real WhatsApp call, and hear the caller.

A voice call is an audio loop, and this is the missing half of it. WhatsApp (like Zoom, Teams or
Meet) asks the operating system for a microphone and a speaker. To *talk* in that call, ZYNTHARO has
to appear as a microphone: its synthesised frames must be written into an audio device that
WhatsApp is using as its input. To *hear* the other person, it captures the call's output. Nothing
short of that can hold a conversation — which is why the previous implementation, which merely
recorded the phrase it was asked to say into a result dictionary, produced no speech at all.

The routing therefore has three honest states:

  ``virtual_cable``  a loopback device exists (VB-Audio Cable on Windows, BlackHole on macOS, a
                     PulseAudio/PipeWire null sink on Linux) and is used as the call microphone.
  ``system``         no loopback device: ZYNTHARO can speak aloud through the speakers and listen
                     through the microphone, so it works in a real room with speakerphone.
  ``none``           neither is available. Reported as a driver problem with the install step,
                     never faked.

Two behaviours matter as much as the routing:

* **Frames, not files.** Speech is written in 20 ms frames so it starts immediately and can be
  stopped mid-sentence.
* **Barge-in that actually stops.** The write loop checks the stop flag between frames, so when
  the caller starts talking the current utterance really ends — the previous code only checked
  before playback began, which meant interrupting did nothing until the sentence finished.

Audio backends are injectable, so all of this is exercised in tests without a sound card.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .tts import FRAME_MS, StreamingVoice

#: Name fragments that identify a loopback device that can act as a virtual microphone.
VIRTUAL_MIC_MARKERS = (
    "cable output",            # VB-Audio Virtual Cable (Windows)
    "voicemeeter out",         # VoiceMeeter
    "blackhole",               # macOS
    "loopback",                # macOS Loopback app
    "null sink", "monitor of",  # PulseAudio / PipeWire
    "virtual cable", "vac",
)

#: Inputs that are real microphones — used for the system (speakerphone) fallback.
REAL_MIC_MARKERS = ("microphone", "mic", "input", "array", "headset")


@dataclass
class AudioRoute:
    """Where speech goes out and where the call is captured from."""

    kind: str = "none"                 # virtual_cable | system | none
    output_index: int = -1             # device the frames are written to (the call's microphone)
    output_name: str = ""
    capture_index: int = -1            # device the call's audio is read from
    capture_name: str = ""
    reason: str = ""
    speakers_index: int = -1           # for the system fallback: audible playback
    speakers_name: str = ""

    @property
    def can_talk(self) -> bool:
        return self.kind in {"virtual_cable", "system"} and self.output_index >= 0

    @property
    def can_hear(self) -> bool:
        return self.capture_index >= 0

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "output": self.output_name or None, "capture": self.capture_name or None,
            "can_talk": self.can_talk, "can_hear": self.can_hear, "reason": self.reason,
            "speakers": self.speakers_name or None,
        }


class AudioBackend(Protocol):
    """The slice of ``sounddevice`` this module needs (so tests can inject a fake)."""

    def query_devices(self) -> list[dict[str, Any]]: ...
    def open_output(self, index: int, rate: int) -> Any: ...
    def open_input(self, index: int, rate: int) -> Any: ...


class SoundDeviceBackend:
    """Real backend. Reports precisely what to install when the library is missing."""

    def __init__(self) -> None:
        import sounddevice as sd  # type: ignore

        self.sd = sd

    def query_devices(self) -> list[dict[str, Any]]:
        rows = []
        for index, device in enumerate(self.sd.query_devices()):
            rows.append({
                "index": index,
                "name": str(device.get("name", "")),
                "inputs": int(device.get("max_input_channels", 0)),
                "outputs": int(device.get("max_output_channels", 0)),
            })
        return rows

    def open_output(self, index: int, rate: int) -> Any:
        return self.sd.RawOutputStream(samplerate=rate, device=index, channels=1, dtype="int16")

    def open_input(self, index: int, rate: int) -> Any:
        return self.sd.RawInputStream(samplerate=rate, device=index, channels=1, dtype="int16")


def backend() -> AudioBackend:
    """The active backend. Overridable in tests and by the packaged installer."""
    global _BACKEND
    if _BACKEND is None:
        try:
            _BACKEND = SoundDeviceBackend()
        except Exception as exc:  # pragma: no cover - depends on the machine
            raise RuntimeError(
                "no audio backend: pip install sounddevice (and PortAudio). "
                f"Details: {exc}"
            ) from exc
    return _BACKEND


_BACKEND: AudioBackend | None = None


def set_backend(value: AudioBackend | None) -> None:
    global _BACKEND
    _BACKEND = value


def _matches(name: str, markers: Iterable[str]) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in markers)


def find_route(audio: AudioBackend | None = None) -> AudioRoute:
    """Decide how ZYNTHARO can join a call, preferring a virtual microphone.

    A virtual cable is what makes ZYNTHARO indistinguishable from a person on the other end: the
    call receives clean synthesised audio instead of a room recording of a speaker.
    """
    audio = audio or backend()
    devices = audio.query_devices()
    outputs = [row for row in devices if row["outputs"] > 0]
    inputs = [row for row in devices if row["inputs"] > 0]

    virtual_output = next((row for row in outputs if _matches(row["name"], VIRTUAL_MIC_MARKERS)), None)
    if virtual_output is not None:
        capture = next((row for row in inputs if _matches(row["name"], VIRTUAL_MIC_MARKERS)), None)
        return AudioRoute(
            kind="virtual_cable",
            output_index=int(virtual_output["index"]),
            output_name=str(virtual_output["name"]),
            capture_index=int(capture["index"]) if capture else -1,
            capture_name=str(capture["name"]) if capture else "",
            reason=(
                "loopback device used as the call microphone"
                if capture else
                "loopback output found; set Windows/macOS default input to it so the call hears ZYNTHARO"
            ),
        )

    real_mic = next((row for row in inputs if _matches(row["name"], REAL_MIC_MARKERS)) or None, None)
    speakers = next((row for row in outputs if _matches(row["name"], ("speaker", "headphone", "output"))), None)
    if real_mic is None and not inputs:
        return AudioRoute(kind="none", reason="no audio input or output devices are present")
    if real_mic is None and speakers is None:
        return AudioRoute(kind="none", reason="no usable microphone or speaker was found")
    return AudioRoute(
        kind="system",
        capture_index=int(real_mic["index"]) if real_mic else -1,
        capture_name=str(real_mic["name"]) if real_mic else "",
        speakers_index=int(speakers["index"]) if speakers else -1,
        speakers_name=str(speakers["name"]) if speakers else "",
        output_index=int(speakers["index"]) if speakers else -1,
        output_name=str(speakers["name"]) if speakers else "",
        reason=(
            "no loopback device installed: ZYNTHARO will speak through the speakers, which works on "
            "speakerphone but the caller also hears the room. Install VB-Audio Cable (Windows), "
            "BlackHole (macOS) or a PipeWire null sink (Linux) for clean call audio."
        ),
    )


@dataclass
class CallStats:
    frames_out: int = 0
    frames_in: int = 0
    underruns: int = 0
    interrupts: int = 0
    last_first_frame_ms: int = 0
    talking: bool = False
    listening: bool = False
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "frames_out": self.frames_out, "frames_in": self.frames_in,
            "underruns": self.underruns, "interrupts": self.interrupts,
            "first_frame_ms": self.last_first_frame_ms,
            "talking": self.talking, "listening": self.listening,
            "errors": self.errors[-5:],
        }


class CallAudioBridge:
    """Duplex audio for a live call: speak frames out, capture frames in, interrupt for real."""

    def __init__(
        self,
        voice: StreamingVoice,
        bus: Any = None,
        audio: AudioBackend | None = None,
        route: AudioRoute | None = None,
        sample_rate: int = 16_000,
    ):
        self.voice = voice
        self.bus = bus
        self.sample_rate = sample_rate
        self._audio = audio
        self.route = route or self._discover()
        self.stats = CallStats()
        self._stop_all = threading.Event()
        self._stop_speaking = threading.Event()
        self._lock = threading.Lock()

    def _discover(self) -> AudioRoute:
        try:
            return find_route(self._audio)
        except Exception as exc:
            return AudioRoute(kind="none", reason=f"audio backend unavailable: {exc}")

    # -- capability -----------------------------------------------------------------
    def require_talking(self) -> AudioRoute:
        """Raise with the exact fix when there is no way to be heard, instead of pretending."""
        if self.route.can_talk:
            return self.route
        from ..actions.registry import DriverUnavailable

        hint = {
            "windows": "Install VB-Audio Virtual Cable, then set WhatsApp's microphone to 'CABLE Output'.",
            "darwin": "Install BlackHole (brew install blackhole-2ch) and pick it as the call's input.",
            "linux": "Create a null sink: pactl load-module module-null-sink sink_name=zyntharo_mic",
        }.get(_platform(), "Install a loopback audio device so a call can receive synthesised speech.")
        raise DriverUnavailable("whatsapp.call audio", "a loopback or speaker output device", f"{self.route.reason} {hint}")

    def status(self) -> dict[str, Any]:
        return {
            "route": self.route.to_json(),
            "voice": self.voice.status(),
            "stats": self.stats.to_json(),
            "can_hold_conversation": bool(self.route.can_talk and self.route.can_hear),
            "note": (
                "clean call audio (loopback microphone)"
                if self.route.kind == "virtual_cable" else
                "speakerphone mode: the caller will also hear the room"
                if self.route.kind == "system" else
                "no audio path: ZYNTHARO cannot be heard in a call on this machine"
            ),
        }

    # -- speaking -------------------------------------------------------------------
    def speak(self, text: str, voice: str = "", timeout: float = 60.0) -> dict[str, Any]:
        """Write one utterance into the call, frame by frame, interruptible at any frame."""
        self.require_talking()
        started = time.time()
        self._stop_speaking.clear()
        self.stats.talking = True
        emitted = 0
        first_frame_ms = 0
        try:
            stream = self._audio.open_output(self.route.output_index, self.sample_rate) if self._audio else None
            if stream is not None:
                stream.start()
            for frame in self.voice.frames(text, voice):
                if self._stop_speaking.is_set() or self._stop_all.is_set():
                    self.stats.interrupts += 1
                    break
                if not first_frame_ms:
                    first_frame_ms = round((time.time() - started) * 1000)
                    self.stats.last_first_frame_ms = first_frame_ms
                if stream is not None:
                    stream.write(frame)
                emitted += 1
                self.stats.frames_out += 1
                if time.time() - started > timeout:
                    self.stats.errors.append(f"utterance exceeded {timeout}s and was cut")
                    break
            if stream is not None:
                stream.stop()
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
        except Exception as exc:
            self.stats.errors.append(f"audio write failed: {exc}")
            raise
        finally:
            self.stats.talking = False
        return {
            "spoken": True, "frames": emitted,
            "seconds": round(emitted * FRAME_MS / 1000, 2),
            "first_frame_ms": first_frame_ms,
            "tier": self.voice.tier,
            "interrupted": self._stop_speaking.is_set(),
            "route": self.route.kind,
        }

    def barge_in(self) -> dict[str, Any]:
        """Interrupt the current utterance. Checked between frames, so it stops immediately."""
        self._stop_speaking.set()
        return {"interrupted": True, "frames_written": self.stats.frames_out}

    # -- listening ------------------------------------------------------------------
    def listen(self, seconds: float = 5.0, voice_energy_threshold: int = 300) -> dict[str, Any]:
        """Capture the call's audio and return what was said (needs STT on the session)."""
        if not self.route.can_hear:
            return {"text": "", "reason": self.route.reason or "no capture device"}
        self.stats.listening = True
        captured = bytearray()
        deadline = time.time() + seconds
        heard_voice = False
        try:
            stream = self._audio.open_input(self.route.capture_index, self.sample_rate) if self._audio else None
            if stream is None:
                return {"text": "", "reason": "no audio backend available for capture"}
            stream.start()
            frame_bytes = int(self.sample_rate * 2 * FRAME_MS / 1000)
            while time.time() < deadline and not self._stop_all.is_set():
                frame = stream.read(frame_bytes)
                if not frame:
                    continue
                frame = frame[0] if isinstance(frame, tuple) else frame
                captured += frame
                self.stats.frames_in += 1
                if _energy(frame) > voice_energy_threshold:
                    heard_voice = True
                    if self.stats.talking:
                        # the caller spoke over us: this is barge-in, and it is automatic
                        self.barge_in()
            stream.stop()
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        finally:
            self.stats.listening = False
        return {
            "frames": len(captured) // max(1, int(self.sample_rate * 2 * FRAME_MS / 1000)),
            "seconds": round(len(captured) / (self.sample_rate * 2), 2),
            "heard_voice": heard_voice,
            "pcm": bytes(captured[: self.sample_rate * 2 * 2]),  # 2 seconds max, for STT
        }

    def stop(self) -> None:
        self._stop_all.set()
        self._stop_speaking.set()


def _energy(frame: bytes) -> float:
    if not frame:
        return 0.0
    sample_count = min(len(frame) // 2, 256)
    if not sample_count:
        return 0.0
    total = 0
    for index in range(sample_count):
        low = frame[index * 2]
        high = frame[index * 2 + 1]
        value = int.from_bytes(bytes((low, high)), "little", signed=True)
        total += abs(value)
    return total / sample_count


def _platform() -> str:
    import sys

    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"
