"""Speaker recognition: pitch, diarization, the gender heuristic and enrolment.

The synthetic voices here are harmonic stacks — a fundamental plus two overtones — which is
the crudest signal that still has a measurable f0 and a measurable spectral shape. That is
enough to prove the three things that would break silently in production: pitch tracking,
speaker separation, and refusal (thin audio, ambiguous pitch, unknown voice).
"""

from __future__ import annotations

import array
import math
import os
import tempfile
import unittest
import wave

from zyntharo.voice import speakers


def tone(f0: float, seconds: float = 1.2, amp: int = 9000, rate: int = 16_000) -> bytes:
    """A voiced-ish signal: fundamental plus two overtones, amplitude-normalised."""
    out = array.array("h")
    for index in range(int(seconds * rate)):
        t = index / rate
        value = (
            math.sin(2 * math.pi * f0 * t)
            + 0.5 * math.sin(2 * math.pi * 2 * f0 * t)
            + 0.25 * math.sin(2 * math.pi * 3 * f0 * t)
        )
        out.append(int(max(-32768, min(32767, amp * value / 1.75))))
    return out.tobytes()


def silence(seconds: float = 0.4, rate: int = 16_000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def write_wav(path: str, pcm: bytes, rate: int = 16_000) -> str:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


class FeatureTests(unittest.TestCase):
    def test_pitch_tracks_the_fundamental_of_a_real_signal(self):
        features = speakers.frame_features(tone(110.0))
        voiced = [item.pitch_hz for item in features if item.voiced]
        self.assertTrue(voiced, "no frame was detected as voiced")
        median = sorted(voiced)[len(voiced) // 2]
        self.assertAlmostEqual(median, 110.0, delta=14.0, msg=f"pitch estimate was {median}")

    def test_silence_is_not_reported_as_a_voice(self):
        features = speakers.frame_features(silence())
        self.assertTrue(all(not item.voiced for item in features), "silence produced a pitch")

    def test_signature_of_thin_audio_is_not_usable(self):
        signature = speakers.signature_from_frames(speakers.frame_features(tone(150.0, seconds=0.06)))
        self.assertFalse(signature.usable, "60 ms of audio must not be enough to identify somebody")

    def test_gender_estimate_admits_the_overlap_band(self):
        male = speakers.describe_gender(speakers.VoiceSignature(pitch_hz=110.0, voiced_ratio=0.9))
        female = speakers.describe_gender(speakers.VoiceSignature(pitch_hz=240.0, voiced_ratio=0.9))
        ambiguous = speakers.describe_gender(speakers.VoiceSignature(pitch_hz=166.0, voiced_ratio=0.9))
        self.assertEqual(male.label, "male")
        self.assertEqual(female.label, "female")
        self.assertEqual(ambiguous.label, "unknown")
        self.assertIn("overlap", ambiguous.basis)

    def test_gender_estimate_is_honest_without_voiced_audio(self):
        guess = speakers.describe_gender(speakers.VoiceSignature(pitch_hz=0.0, voiced_ratio=0.0))
        self.assertEqual((guess.label, guess.confidence), ("unknown", 0.0))


class DiarizationTests(unittest.TestCase):
    def test_two_people_are_two_tracks_with_their_own_genders(self):
        pcm = tone(110.0, seconds=1.0) + silence(0.5) + tone(260.0, seconds=1.0)
        result = speakers.analyse_pcm(pcm)
        self.assertEqual(result["speaker_count"], 2, result)
        genders = sorted(row["gender"]["label"] for row in result["speakers"])
        self.assertEqual(genders, ["female", "male"])
        self.assertEqual(result["segment_count"], 2, result["timeline"])
        self.assertEqual(result["overlaps"], 0)

    def test_one_person_speaking_twice_is_one_track(self):
        pcm = tone(120.0, seconds=0.9) + silence(0.5) + tone(122.0, seconds=0.9)
        result = speakers.analyse_pcm(pcm)
        self.assertEqual(result["speaker_count"], 1, result)
        self.assertEqual(result["speakers"][0]["segments"], 2)

    def test_short_noise_bursts_are_skipped_not_labelled(self):
        pcm = tone(180.0, seconds=0.08) + silence(0.5) + tone(180.0, seconds=1.0)
        result = speakers.analyse_pcm(pcm)
        self.assertGreaterEqual(result["skipped_short_segments"], 1)
        self.assertEqual(result["segment_count"], 1)

    def test_overlapping_speech_is_flagged_rather_than_attributed(self):
        # Two voices inside one continuous envelope: pitch jumps an octave and back with no
        # pause. The honest output is "uncertain", not a confident name.
        pcm = tone(110.0, seconds=0.15) + tone(250.0, seconds=0.15) + tone(110.0, seconds=0.15)
        result = speakers.analyse_pcm(pcm)
        self.assertGreaterEqual(result["overlaps"], 1, result["timeline"])
        self.assertTrue(any(row["uncertain"] for row in result["timeline"]))

    def test_a_recording_can_be_analysed_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(os.path.join(tmp, "conversation.wav"), tone(130.0) + silence(0.4) + tone(230.0))
            result = speakers.analyse_wav(path)
        self.assertEqual(result["speaker_count"], 2)
        self.assertEqual(result["path"], path)

    def test_a_non_pcm_file_is_refused_instead_of_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.wav")
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(1)  # 8-bit: not enough resolution for these features
                handle.setframerate(16_000)
                handle.writeframes(b"\x80" * 1600)
            with self.assertRaises(ValueError) as caught:
                speakers.read_wav(path)
        self.assertIn("16-bit", str(caught.exception))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = speakers.SpeakerRegistry(self.tmp.name)

    def _signature(self, f0: float) -> speakers.VoiceSignature:
        return speakers.signature_from_frames(speakers.frame_features(tone(f0)))

    def test_an_enrolled_voice_is_recognised_and_a_different_one_is_not(self):
        self.registry.enroll("Ayesha", self._signature(210.0), kind="owner")
        label, distance = self.registry.identify(self._signature(212.0))
        self.assertEqual(label, "Ayesha", distance)
        stranger, _ = self.registry.identify(self._signature(105.0))
        self.assertEqual(stranger, "")

    def test_enrolment_refuses_audio_it_cannot_use(self):
        with self.assertRaises(ValueError) as caught:
            self.registry.enroll("Nobody", self._signature(150.0).__class__(pitch_hz=0.0, voiced_ratio=0.0))
        self.assertIn("not enough voiced audio", str(caught.exception))

    def test_the_registry_survives_a_restart(self):
        self.registry.enroll("Bilal", self._signature(120.0))
        reopened = speakers.SpeakerRegistry(self.tmp.name)
        self.assertIn("Bilal", reopened.people)
        self.assertEqual(reopened.rows()[0]["label"], "Bilal")

    def test_there_is_only_ever_one_owner_and_it_can_be_moved(self):
        self.registry.enroll("One", self._signature(120.0), kind="owner")
        self.registry.enroll("Two", self._signature(220.0))
        self.registry.set_owner("Two")
        self.assertEqual(self.registry.owner(), "Two")
        self.assertEqual(self.registry.people["One"]["kind"], "household")

    def test_forgetting_a_voice_removes_it_permanently(self):
        self.registry.enroll("Guest", self._signature(190.0))
        self.assertTrue(self.registry.forget("Guest"))
        self.assertNotIn("Guest", speakers.SpeakerRegistry(self.tmp.name).people)
        self.assertFalse(self.registry.forget("Guest"))


if __name__ == "__main__":
    unittest.main()
