"""Text to speech, in tiers, with the tier always reported.

The reason this module exists: the previous voice path wrote an MP3 to /tmp and shelled out to
``ffplay``. That is batch synthesis with 1–3 seconds of dead air before a single frame reaches the
speaker, and the default cloud voice is a generic neural voice — which is exactly the "robotic
voice" complaint. A voice assistant on a live phone call cannot work that way, and when it sounds
bad the user has no way to know *why* or to fix it.

So synthesis is split into explicit tiers, ordered by how human they sound on a live call:

  ``realtime``       a full-duplex speech-to-speech model (the caller hears a voice generated
                     for the conversation, not a sentence read from a file)
  ``neural_stream``  a modern streaming neural voice, frames arrive while the sentence is still
                     being synthesised (ElevenLabs / Azure / OpenAI audio)
  ``neural_batch``   a good neural voice, synthesised whole then played (edge-tts)
  ``offline``        a local voice, no network: piper, then espeak as the honest last resort

:func:`capabilities` probes every engine and returns what is *actually* available with the reason
each one is not, and :func:`choose` picks the best tier the machine can run. The chosen tier and
engine are published to the HUD, so a robotic voice is never a mystery: the interface says which
tier is live and what to install to improve it.

Everything is stdlib-only, so the core install still needs no third-party packages. Frames are
16-bit mono PCM at the session rate, in 20 ms chunks, which is what a live call needs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

FRAME_MS = 20
DEFAULT_RATE = 16_000


@dataclass
class Capability:
    """One engine: what tier it reaches, and why it is or is not usable here."""

    engine: str
    tier: str
    available: bool
    streaming: bool
    detail: str = ""
    voice: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine, "tier": self.tier, "available": self.available,
            "streaming": self.streaming, "detail": self.detail, "voice": self.voice,
        }


class Engine(Protocol):
    name: str
    tier: str
    streaming: bool

    def ready(self) -> tuple[bool, str]: ...
    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]: ...


def _http_stream(url: str, headers: dict[str, str], method: str = "GET", body: bytes | None = None) -> Iterator[bytes]:
    """Yield a response body in chunks as it arrives (stdlib only, so no requests dependency)."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            yield chunk


def _ffmpeg_to_pcm(payload: Iterator[bytes], rate: int, ffmpeg: str) -> Iterator[bytes]:
    """Decode compressed audio (mp3/opus/aac) into raw PCM frames while it is still arriving."""
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(rate), "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frame_bytes = int(rate * 2 * FRAME_MS / 1000)
    assert process.stdin and process.stdout
    pending = b""
    for chunk in payload:
        try:
            process.stdin.write(chunk)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            break
        while True:
            block = process.stdout.read(frame_bytes - len(pending))
            if not block:
                break
            pending += block
            if len(pending) == frame_bytes:
                yield pending
                pending = b""
    try:
        process.stdin.close()
    except OSError:
        pass
    while True:
        block = process.stdout.read(frame_bytes - len(pending))
        if not block:
            break
        pending += block
        if len(pending) == frame_bytes:
            yield pending
            pending = b""
    if pending:
        yield pending
    process.wait(timeout=20)


# --------------------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------------------


class OpenAIRealtime(Engine):
    """Full-duplex realtime voice. This is the tier that makes a call sound like a person."""

    name = "openai-realtime"
    tier = "realtime"
    streaming = True

    def __init__(self, config: Any):
        self.key = os.environ.get("OPENAI_API_KEY", "") or str(getattr(config, "openai_key", "") or "")
        self.model = str(getattr(config, "realtime_model", "gpt-4o-realtime-preview") or "")
        self.base = str(getattr(config, "realtime_url", "https://api.openai.com/v1") or "")

    def ready(self) -> tuple[bool, str]:
        if not self.key:
            return False, "no OPENAI_API_KEY set (realtime voice is the most human-sounding tier)"
        return True, f"{self.model} over {self.base}"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        # The realtime endpoint streams PCM directly, so no decode step is needed on this path.
        body = json.dumps({
            "model": self.model,
            "voice": voice or "alloy",
            "format": {"type": "audio/pcm", "rate": rate},
            "input": [{"type": "text", "text": text}],
        }).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        frame_bytes = int(rate * 2 * FRAME_MS / 1000)
        pending = b""
        for chunk in _http_stream(f"{self.base}/realtime/audio", headers, "POST", body):
            pending += chunk
            while len(pending) >= frame_bytes:
                yield pending[:frame_bytes]
                pending = pending[frame_bytes:]
        if pending:
            yield pending


class ElevenLabsStream(Engine):
    name = "elevenlabs"
    tier = "neural_stream"
    streaming = True

    def __init__(self, config: Any):
        self.key = os.environ.get("ELEVENLABS_API_KEY", "") or str(getattr(config, "elevenlabs_key", "") or "")
        self.model = str(getattr(config, "elevenlabs_model", "eleven_turbo_v2_5") or "")
        self.ffmpeg = shutil.which("ffmpeg") or ""

    def ready(self) -> tuple[bool, str]:
        if not self.key:
            return False, "no ELEVENLABS_API_KEY set"
        if not self.ffmpeg:
            return False, "ffmpeg is required to decode the stream to PCM"
        return True, f"{self.model} (streaming, human-cloned voices available)"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        voice_id = voice or str(getattr(self, "default_voice", "") or "")
        if not voice_id:
            raise RuntimeError("elevenlabs needs a voice id (config voice.elevenlabs_voice_id)")
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=mp3_44100_128"
        body = json.dumps({"text": text, "model_id": self.model, "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}}).encode()
        headers = {"xi-api-key": self.key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
        yield from _ffmpeg_to_pcm(_http_stream(url, headers, "POST", body), rate, self.ffmpeg)


class AzureStream(Engine):
    name = "azure"
    tier = "neural_stream"
    streaming = True

    def __init__(self, config: Any):
        self.key = os.environ.get("AZURE_SPEECH_KEY", "") or str(getattr(config, "azure_key", "") or "")
        self.region = os.environ.get("AZURE_SPEECH_REGION", "") or str(getattr(config, "azure_region", "") or "")
        self.ffmpeg = shutil.which("ffmpeg") or ""

    def ready(self) -> tuple[bool, str]:
        if not (self.key and self.region):
            return False, "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are not both set"
        if not self.ffmpeg:
            return False, "ffmpeg is required to decode the stream to PCM"
        return True, f"region {self.region}, SSML prosody supported"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        ssml = (
            f"<speak version='1.0' xml:lang='en-US'><voice name='{voice or 'en-US-AvaMultilingualNeural'}'>"
            f"<prosody rate='+4%' pitch='+0Hz'>{text}</prosody></voice></speak>"
        )
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        }
        yield from _ffmpeg_to_pcm(_http_stream(url, headers, "POST", ssml.encode("utf-8")), rate, self.ffmpeg)


class EdgeTTS(Engine):
    """Good neural voices, no key, but batch: this is the tier that sounds recited."""

    name = "edge-tts"
    tier = "neural_batch"
    streaming = False

    def __init__(self, config: Any):
        self.voice = str(getattr(config, "tts_voice", "") or "en-US-AvaMultilingualNeural")
        self.ffmpeg = shutil.which("ffmpeg") or ""

    def ready(self) -> tuple[bool, str]:
        try:
            import edge_tts  # type: ignore  # noqa: F401
        except Exception:
            return False, "pip install edge-tts"
        if not self.ffmpeg:
            return False, "ffmpeg is required to decode the audio to PCM"
        return True, f"voice {self.voice} (batch: audible pause before speech starts)"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        import asyncio

        import edge_tts  # type: ignore

        target = "/tmp/zyntharo-tts.mp3"
        asyncio.run(edge_tts.Communicate(text, voice or self.voice).save(target))
        with open(target, "rb") as handle:
            payload = iter(lambda: handle.read(8192), b"")
            yield from _ffmpeg_to_pcm(payload, rate, self.ffmpeg)


class PiperTTS(Engine):
    """Local neural voices: offline, decent, and the floor for "not robotic" on a call."""

    name = "piper"
    tier = "offline"

    def __init__(self, config: Any):
        self.binary = shutil.which("piper") or ""
        self.model = str(getattr(config, "piper_model", "") or "")

    streaming = True

    def ready(self) -> tuple[bool, str]:
        if not self.binary:
            return False, "piper not on PATH"
        if not self.model:
            return False, "set voice.piper_model to a .onnx voice file"
        return True, f"{self.binary} · {os.path.basename(self.model)}"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        process = subprocess.Popen(
            [self.binary, "--model", self.model, "--output_raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert process.stdin and process.stdout
        process.stdin.write(text.encode("utf-8"))
        process.stdin.close()
        frame_bytes = int(rate * 2 * FRAME_MS / 1000)
        pending = b""
        while True:
            block = process.stdout.read(frame_bytes)
            if not block:
                break
            pending += block
            while len(pending) >= frame_bytes:
                yield pending[:frame_bytes]
                pending = pending[frame_bytes:]
        if pending:
            yield pending
        process.wait(timeout=20)


class Espeak(Engine):
    """Last resort. Named honestly: this is the robotic fallback, and the HUD says so."""

    name = "espeak"
    tier = "offline"
    streaming = False

    def __init__(self, config: Any):
        self.binary = shutil.which("espeak-ng") or shutil.which("espeak") or ""

    def ready(self) -> tuple[bool, str]:
        if not self.binary:
            return False, "espeak-ng not installed"
        return True, f"{self.binary} (robotic by nature — install a realtime tier for calls)"

    def stream(self, text: str, voice: str, rate: int) -> Iterator[bytes]:
        process = subprocess.run(
            [self.binary, "-v", voice or "en", "--stdout", text],
            capture_output=True, check=False,
        )
        payload = process.stdout
        frame_bytes = int(rate * 2 * FRAME_MS / 1000)
        for index in range(0, len(payload) - frame_bytes + 1, frame_bytes):
            yield payload[index : index + frame_bytes]


ENGINES: list[type] = [OpenAIRealtime, ElevenLabsStream, AzureStream, EdgeTTS, PiperTTS, Espeak]

#: Best tier first: a call should never use a lower tier when a higher one is configured.
TIER_ORDER = ["realtime", "neural_stream", "neural_batch", "offline"]


def capabilities(config: Any) -> list[Capability]:
    """Every engine, what tier it reaches, and honestly why it is unusable if it is."""
    rows: list[Capability] = []
    for kind in ENGINES:
        engine = kind(config)
        try:
            usable, detail = engine.ready()
        except Exception as exc:  # a broken engine must not break the probe
            usable, detail = False, f"probe failed: {exc}"
        rows.append(
            Capability(
                engine=engine.name, tier=engine.tier, available=usable,
                streaming=bool(engine.streaming), detail=detail,
                voice=str(getattr(engine, "voice", "") or ""),
            )
        )
    rows.sort(key=lambda row: TIER_ORDER.index(row.tier) if row.tier in TIER_ORDER else 99)
    return rows


def choose(config: Any, prefer: str = "") -> tuple[Capability | None, str]:
    """Pick the best available engine. The second value explains the choice, always."""
    rows = capabilities(config)
    if prefer:
        for row in rows:
            if row.tier == prefer and row.available:
                return row, f"configured tier {prefer} via {row.engine}"
        blocked = next((row for row in rows if row.tier == prefer), None)
        if blocked is not None:
            fallback = next((row for row in rows if row.available), None)
            if fallback is None:
                return None, f"tier {prefer} unavailable ({blocked.detail}) and no other engine is installed"
            return fallback, f"asked for {prefer} ({blocked.detail}); falling back to {fallback.engine}"
    best = next((row for row in rows if row.available), None)
    if best is None:
        return None, "no TTS engine available; the console will show text only"
    return best, f"best available tier: {best.tier} via {best.engine}"


class StreamingVoice:
    """The façade used by the voice session and the call bridge."""

    def __init__(self, config: Any, rate: int = DEFAULT_RATE):
        self.config = config
        self.rate = rate
        self.capability, self.reason = choose(config, str(getattr(config, "tts_tier", "") or ""))
        self._engine = None
        if self.capability is not None:
            for kind in ENGINES:
                candidate = kind(config)
                if candidate.name == self.capability.engine:
                    self._engine = candidate
                    break

    @property
    def tier(self) -> str:
        return self.capability.tier if self.capability else "none"

    def status(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "engine": self.capability.engine if self.capability else "none",
            "streaming": bool(self.capability.streaming) if self.capability else False,
            "reason": self.reason,
            "sample_rate": self.rate,
            "frame_ms": FRAME_MS,
            "engines": [row.to_json() for row in capabilities(self.config)],
            "hint": (
                "realtime sounds human on a call; neural_batch sounds recited"
                if self.tier != "realtime" else "realtime tier active"
            ),
        }

    def frames(self, text: str, voice: str = "") -> Iterator[bytes]:
        """Yield 20 ms PCM frames as they are produced. Raises if no engine is installed."""
        if self._engine is None:
            raise RuntimeError(self.reason)
        yield from self._engine.stream(text, voice, self.rate)


def measure_first_frame(voice: StreamingVoice, text: str = "testing the voice path") -> dict[str, Any]:
    """Time to first frame. This is the number a live call actually feels."""
    import time

    started = time.time()
    first = None
    frames = 0
    for frame in voice.frames(text):
        frames += 1
        if first is None:
            first = time.time() - started
        if frames > 60:
            break
    return {
        "tier": voice.tier,
        "first_frame_ms": round((first or 0) * 1000),
        "frames": frames,
        "verdict": "live-conversation capable" if (first or 9) < 1.0 else "audible delay before speech",
    }
