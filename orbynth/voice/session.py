"""Voice session: wake word, streaming STT, TTS and barge-in.

Voice is the default interface; typing is the option. That ordering shapes this module:

* The microphone is live on launch. A wake word (``orbynth``/``orb`` by default) gates
  attention, but the user can also hold push-to-talk or speak after the assistant finishes.
* **Barge-in** is real: while ORBYNTH is speaking, incoming voice energy above the VAD
  threshold immediately stops playback and starts listening. Interrupting the assistant must
  never require a click.
* Everything degrades honestly. With no microphone, no STT model or no TTS voice installed,
  the session reports precisely which piece is missing, keeps the HUD fully usable in text
  mode, and never pretends it heard something.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..events import (
    VOICE_LANGUAGE,
    VOICE_LISTENING,
    VOICE_SPEAKING,
    VOICE_TRANSCRIPT,
    VOICE_WAKE,
    EventBus,
    LOG,
)


@dataclass
class VoiceState:
    enabled: bool = True
    muted: bool = False
    listening: bool = False
    speaking: bool = False
    push_to_talk: bool = False
    last_transcript: str = ""
    last_wake: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "muted": self.muted,
            "listening": self.listening,
            "speaking": self.speaking,
            "push_to_talk": self.push_to_talk,
            "last_transcript": self.last_transcript[:200],
            "errors": self.errors[-5:],
        }


class VoiceSession:
    """Wires wake word → STT → agent → TTS, with interruption."""

    def __init__(
        self,
        config: Any,
        bus: EventBus,
        on_utterance: Callable[[str], Any] | None = None,
        sample_rate: int = 16_000,
        frame_ms: int = 30,
    ):
        self.config = config
        self.bus = bus
        self.on_utterance = on_utterance
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.state = VoiceState(enabled=bool(getattr(config, "enabled", True)))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._playback_stop = threading.Event()
        self._tts: Any = None
        self._stt: Any = None
        self._wake: Any = None
        self._available = self._probe()

    # ----------------------------------------------------------------------------------
    # Capability probing (honest reporting instead of silent failure)
    # ----------------------------------------------------------------------------------
    def _probe(self) -> dict[str, Any]:
        available: dict[str, Any] = {}
        stt_engine = getattr(self.config, "stt", "faster-whisper")
        if stt_engine in {"faster-whisper", "faster_whisper"}:
            try:  # pragma: no cover - optional dependency
                from faster_whisper import WhisperModel  # type: ignore

                self._stt = WhisperModel(getattr(self.config, "stt_model", "base.en"), device="auto", compute_type="int8")
                available["stt"] = "faster-whisper"
            except Exception as exc:  # pragma: no cover
                self.state.errors.append(f"STT unavailable: {exc}")
                available["stt"] = None
        elif stt_engine == "cloud":
            available["stt"] = "cloud (configure a provider)"
        else:
            available["stt"] = None
        tts_engine = getattr(self.config, "tts", "edge-tts")
        if tts_engine in {"edge-tts", "edge_tts"}:
            try:  # pragma: no cover - optional dependency
                import edge_tts  # type: ignore  # noqa: F401

                available["tts"] = "edge-tts"
            except Exception as exc:  # pragma: no cover
                self.state.errors.append(f"TTS unavailable: {exc}")
                available["tts"] = None
        elif tts_engine == "piper":
            available["tts"] = "piper (binary required)"
        else:
            available["tts"] = None
        try:  # pragma: no cover - optional dependency
            import sounddevice  # type: ignore  # noqa: F401

            available["audio"] = True
        except Exception:
            available["audio"] = False
            self.state.errors.append("no audio device layer (pip install sounddevice)")
        try:  # pragma: no cover
            import openwakeword  # type: ignore  # noqa: F401

            available["wake"] = True
        except Exception:
            available["wake"] = False
        return available

    @property
    def available(self) -> dict[str, Any]:
        return self._available

    # ----------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------
    def start(self) -> dict[str, Any]:
        if not self._available.get("audio"):
            self.bus.emit(LOG, source="voice", severity="warning", message="voice disabled: no audio device")
            return {"started": False, "reason": "no audio device", "details": self._available}
        self._stop.clear()
        for target, name in ((self._capture_loop, "voice-capture"), (self._wake_loop, "voice-wake")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        self.bus.emit(LOG, source="voice", severity="info", message="voice session started", **self._available)
        return {"started": True, "details": self._available}

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads.clear()

    def set_muted(self, muted: bool) -> None:
        self.state.muted = muted
        if muted:
            self.state.listening = False

    def push_to_talk(self, down: bool) -> dict[str, Any]:
        self.state.push_to_talk = down
        if down and not self.state.listening:
            self.state.listening = True
            self.bus.emit(VOICE_LISTENING, source="voice", trigger="push_to_talk")
        return {"push_to_talk": down, "listening": self.state.listening}

    # ----------------------------------------------------------------------------------
    # Transcribing (the WhatsApp call bridge hands raw call frames in here)
    # ----------------------------------------------------------------------------------
    def transcribe_pcm(self, pcm: bytes, rate: int = 16_000) -> str:
        """Turn captured 16-bit mono PCM into words.

        The call bridge captures the live call as raw frames while the STT engine wants a file,
        so the frames are wrapped in a WAV header. With no STT installed this returns an empty
        string instead of inventing a transcript.
        """
        if not pcm or self._stt is None:
            return ""
        import tempfile
        import wave
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "call.wav"
            try:
                with wave.open(str(path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(rate)
                    wav.writeframes(pcm)
                segments, _info = self._stt.transcribe(str(path), beam_size=1, vad_filter=True)
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:  # pragma: no cover - depends on the local model
                self.state.errors.append(f"call transcription failed: {exc}")
                return ""

    # ----------------------------------------------------------------------------------
    # Speaking
    # ----------------------------------------------------------------------------------
    def say(self, text: str, block: bool = False) -> dict[str, Any]:
        """Speak text, unless muted or the barge-in monitor detects the user talking."""
        text = (text or "").strip()
        if not text:
            return {"spoken": False, "reason": "empty"}
        if self.state.muted:
            self.bus.emit(VOICE_SPEAKING, source="voice", text=text, muted=True)
            return {"spoken": False, "reason": "muted"}
        self.state.speaking = True
        self.bus.emit(VOICE_SPEAKING, source="voice", text=text[:500])
        self._playback_stop.clear()
        engine = self._available.get("tts")

        def run() -> None:
            try:
                if engine == "edge-tts":
                    self._speak_edge(text)
                elif engine == "piper":
                    self._speak_piper(text)
                else:
                    self.bus.emit(
                        LOG,
                        source="voice",
                        severity="warning",
                        message="no TTS engine installed; text is shown in the HUD only",
                    )
            except Exception as exc:  # noqa: BLE001 - never let audio break the agent loop
                self.state.errors.append(f"TTS failed: {exc}")
            finally:
                self.state.speaking = False

        thread = threading.Thread(target=run, name="voice-tts", daemon=True)
        thread.start()
        if block:
            thread.join()
        return {"spoken": True, "engine": engine, "chars": len(text)}

    def _speak_edge(self, text: str) -> None:  # pragma: no cover - needs network + audio
        import asyncio

        import edge_tts  # type: ignore

        from ..ops.languages import tts_voice

        # Speak in the configured language: replying to an Urdu question in an English voice
        # is the kind of detail that makes a voice assistant unusable in half the world.
        configured_voice = getattr(self.config, "tts_voice", "") or "en-US-AriaNeural"
        default_voice = "en-US-AriaNeural"
        language = getattr(self.config, "language", "en")
        voice = configured_voice if configured_voice != default_voice or language == "en" else tts_voice(language, configured_voice)
        output = "/tmp/orbynth-tts.mp3"
        asyncio.run(edge_tts.Communicate(text, voice).save(output))
        if self._playback_stop.is_set():
            return
        import subprocess
        import shutil

        player = shutil.which("ffplay") or shutil.which("mpv") or shutil.which("afplay")
        if player:
            subprocess.run([player, "-nodisp", "-autoexit", "-loglevel", "quiet", output], check=False)
        else:
            self.bus.emit(LOG, source="voice", severity="warning", message="no audio player found (ffplay/mpv/afplay)")

    def _speak_piper(self, text: str) -> None:  # pragma: no cover - needs the piper binary
        import shutil
        import subprocess

        binary = shutil.which("piper")
        if not binary:
            raise RuntimeError("piper not found on PATH")
        subprocess.run([binary, "--output_file", "/tmp/orbynth-tts.wav"], input=text.encode(), check=False)

    def interrupt(self) -> None:
        """Barge-in: stop speaking immediately."""
        self._playback_stop.set()
        self.state.speaking = False

    # ----------------------------------------------------------------------------------
    # Listening
    # ----------------------------------------------------------------------------------
    def listen_once(self, timeout: float = 12.0, silence_ms: int | None = None) -> dict[str, Any]:
        """Record one utterance (until silence) and transcribe it."""
        if self._stt is None:
            raise RuntimeError("speech-to-text is not available on this machine")
        silence = silence_ms or getattr(self.config, "vad_silence_ms", 700)
        self.state.listening = True
        self.bus.emit(VOICE_LISTENING, source="voice", trigger="listen_once")
        frames: list[bytes] = []
        deadline = time.time() + timeout
        last_voice = time.time()
        while time.time() < deadline:
            try:
                frame = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            frames.append(frame)
            if _energy(frame) > 320:
                last_voice = time.time()
                if self.state.speaking:  # barge-in
                    self.interrupt()
            elif (time.time() - last_voice) * 1000 >= silence and frames:
                break
        self.state.listening = False
        audio = b"".join(frames)
        if not audio:
            return {"text": "", "reason": "no audio captured"}
        text = self._transcribe(audio)
        self.state.last_transcript = text
        self.bus.emit(VOICE_TRANSCRIPT, source="voice", text=text, role="user", final=True)
        if text and self.on_utterance is not None:
            try:
                self.on_utterance(text)
            except Exception as exc:  # noqa: BLE001
                self.state.errors.append(f"utterance handler failed: {exc}")
        return {"text": text, "seconds": round(len(audio) / (self.sample_rate * 2), 2)}

    def _transcribe(self, audio: bytes) -> str:  # pragma: no cover - needs the STT model
        import numpy as np  # type: ignore

        from ..ops.languages import detect as detect_language
        from ..ops.languages import stt_language

        samples = np.frombuffer(audio, dtype=np.int16).astype("float32") / 32768.0
        # The configured language, not a hard-coded "en": with "en" an Urdu or Arabic
        # utterance transcribes to nonsense, and the agent then acts on the nonsense.
        configured = getattr(self.config, "language", "en")
        segments, _info = self._stt.transcribe(samples, language=stt_language(configured), vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        # A user may switch language mid-session; report what was actually heard, and remember
        # it so TTS, OCR hints and the HUD follow the conversation instead of fighting it.
        heard = detect_language(text)
        if text and heard != configured:
            self.bus.emit(
                VOICE_LANGUAGE,
                source="voice",
                severity="notice",
                detected=heard,
                configured=configured,
                text=text[:120],
            )
        return text

    # ----------------------------------------------------------------------------------
    # Background loops
    # ----------------------------------------------------------------------------------
    def _capture_loop(self) -> None:  # pragma: no cover - needs audio hardware
        try:
            import sounddevice as sd  # type: ignore
        except Exception:
            return
        blocksize = int(self.sample_rate * self.frame_ms / 1000)

        def callback(indata: Any, _frames: int, _time_info: Any, _status: Any) -> None:
            if self.state.muted or self._stop.is_set():
                return
            try:
                self._audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

        with sd.RawInputStream(
            samplerate=self.sample_rate, blocksize=blocksize, dtype="int16", channels=1, callback=callback
        ):
            while not self._stop.is_set():
                time.sleep(0.1)

    def _wake_loop(self) -> None:  # pragma: no cover - needs the wake-word model
        """Always-on wake word detection; falls through to listening when triggered."""
        if not self._available.get("wake"):
            return
        try:
            import numpy as np  # type: ignore
            from openwakeword.model import Model  # type: ignore

            model = Model()
        except Exception:
            return
        words = [word.lower() for word in getattr(self.config, "wake_words", ["orbynth", "orb"])]
        buffer = b""
        while not self._stop.is_set():
            try:
                buffer += self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if len(buffer) < 1280 * 2:
                continue
            chunk, buffer = buffer[: 1280 * 2], buffer[1280 * 2 :]
            try:
                scores = model.predict(np.frombuffer(chunk, dtype=np.int16))
            except Exception:
                continue
            for name, score in (scores or {}).items():
                if score > 0.6 and any(word in name.lower() for word in words):
                    self.state.last_wake = time.time()
                    self.bus.emit(VOICE_WAKE, source="voice", word=name, score=round(float(score), 3))
                    self.listen_once()


def _energy(frame: bytes) -> float:
    if not frame:
        return 0.0
    try:
        import numpy as np  # type: ignore

        return float(np.abs(np.frombuffer(frame, dtype=np.int16)).mean())
    except Exception:
        return float(sum(frame[: min(len(frame), 512)]) / max(1, min(len(frame), 512)))


class TextOnlyVoice:
    """Fallback used when voice is disabled: keeps the same interface, never pretends.

    The HUD stays usable in text mode and every call site keeps working unchanged.
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.state = VoiceState(enabled=False)

    def start(self) -> dict[str, Any]:
        return {"started": False, "reason": "voice disabled in config"}

    def stop(self) -> None:
        return None

    def set_muted(self, muted: bool) -> None:
        self.state.muted = muted

    def push_to_talk(self, down: bool) -> dict[str, Any]:
        return {"push_to_talk": down, "listening": False}

    def say(self, text: str, block: bool = False) -> dict[str, Any]:
        self.bus.emit(VOICE_SPEAKING, source="voice", text=(text or "")[:500], muted=True)
        return {"spoken": False, "reason": "voice disabled"}

    def listen_once(self, timeout: float = 12.0, silence_ms: int | None = None) -> dict[str, Any]:
        return {"text": "", "reason": "voice disabled"}

    def interrupt(self) -> None:
        return None
