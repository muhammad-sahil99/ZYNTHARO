"""Etiquette and the presence tools, exercised end to end through the capability gate.

Two levels are tested on purpose. ``EtiquettePolicy`` is pure logic and is checked on every
rule, including the ones that make the assistant *quieter* — those are the rules a naive
implementation forgets. Then the actions are driven through the real registry with real
temporary storage, so "speak only when appropriate" is proven to be reachable by an agent and
not just a well-written docstring.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import wave

from orbynth.actions import presence as tools
from orbynth.ops.presence import (
    ASK_OWNER,
    SILENT,
    SPEAK,
    SPEAK_BRIEF,
    TEXT_ONLY,
    WAIT,
    CameraView,
    EtiquettePolicy,
    PresenceController,
    PresenceSnapshot,
    VoiceView,
    looks_private,
)
from orbynth.vision.camera import CameraService, Detector, Face
from orbynth.voice.speakers import SpeakerRegistry
from tests.helpers import Stack
from tests.test_speakers import tone


def snapshot(voice: VoiceView | None = None, camera: CameraView | None = None) -> PresenceSnapshot:
    return PresenceSnapshot(voice=voice or VoiceView(), camera=camera or CameraView())


def camera_view(faces: int = 1, owner: bool = True, strangers: int = 0, recognition: bool = True) -> CameraView:
    return CameraView(
        available=True,
        consent="granted",
        face_count=faces,
        owner_present=owner,
        stranger_count=strangers,
        recognition=recognition,
        # A stub camera that can match faces: without this the snapshot would fall back to
        # voice evidence, which is a different scenario from the one these tests describe.
        recognition_available=recognition,
        detail="stub",
    )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = EtiquettePolicy()

    def decide(self, snap, **kwargs):
        kwargs.setdefault("addressed", True)
        kwargs.setdefault("text", "the report is ready")
        return self.policy.decide(snap, **kwargs)

    def test_owner_alone_and_addressed_gets_a_spoken_answer(self):
        decision = self.decide(snapshot(camera=camera_view(faces=1, owner=True)))
        self.assertEqual(decision.mode, SPEAK)
        self.assertTrue(decision.audible)

    def test_muted_voice_sends_the_answer_to_the_screen(self):
        decision = self.decide(snapshot(), voice_enabled=False)
        self.assertEqual(decision.mode, TEXT_ONLY)
        self.assertFalse(decision.audible)

    def test_private_content_with_a_stranger_present_is_never_read_aloud(self):
        decision = self.decide(
            snapshot(camera=camera_view(faces=2, owner=True, strangers=1)),
            text="your password is in the vault, the code is 4412",
        )
        self.assertEqual(decision.mode, TEXT_ONLY)
        self.assertTrue(decision.private)
        self.assertIn("somebody else is present", decision.reason)

    def test_two_people_talking_to_each_other_are_not_interrupted(self):
        decision = self.decide(
            snapshot(voice=VoiceView(speaker_count=2, active_label="", detail="two voices")),
            addressed=False,
        )
        self.assertEqual(decision.mode, SILENT)
        self.assertIn("conversation", decision.reason)

    def test_speaking_over_each_other_makes_the_assistant_wait(self):
        decision = self.decide(
            snapshot(voice=VoiceView(speaker_count=2, overlap=True)),
            addressed=False,
        )
        self.assertEqual(decision.mode, WAIT)
        # Addressed while being talked over: answering aloud would add a third voice.
        addressed = self.decide(snapshot(voice=VoiceView(speaker_count=2, overlap=True)), addressed=True)
        self.assertEqual(addressed.mode, TEXT_ONLY)

    def test_a_stranger_can_ask_but_gets_a_short_unprivileged_answer(self):
        decision = self.decide(
            snapshot(voice=VoiceView(speaker_count=1, active_label=""), camera=camera_view(owner=False, strangers=1)),
            addressed=True,
            text="what is on the calendar",
        )
        self.assertEqual(decision.mode, SPEAK_BRIEF)
        self.assertTrue(decision.requires_owner)

    def test_a_critical_alert_is_spoken_even_when_not_addressed(self):
        decision = self.decide(snapshot(camera=camera_view()), addressed=False, urgency="critical")
        self.assertEqual(decision.mode, SPEAK_BRIEF)
        self.assertTrue(decision.audible)

    def test_ambient_speech_with_nobody_around_stays_silent(self):
        decision = self.decide(snapshot(), addressed=False, text="nothing needed here")
        self.assertEqual(decision.mode, SILENT)

    def test_private_detection_catches_the_obvious_leaks(self):
        for text in ("password: hunter2", "mail me at a@b.com", "/home/ayesha/keys", "the IBAN is ready"):
            self.assertTrue(looks_private(text), text)
        self.assertFalse(looks_private("the invoices are filed"))


class FusionTests(unittest.TestCase):
    def test_camera_evidence_is_absent_without_consent_and_never_unlocks_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            camera = CameraService(
                tmp,
                detector=Detector(detect=lambda frame: [Face(box=(0, 0, 4, 4))], backend="stub"),
                frame_source=lambda: type("F", (), {"nbytes": 4})(),
            )
            controller = PresenceController(None, camera)
            view = controller.camera_view()
        self.assertFalse(view.available)
        self.assertEqual(view.consent, "unknown")
        self.assertFalse(controller.snapshot().owner_present)

    def test_voice_analysis_becomes_the_active_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SpeakerRegistry(tmp)
            controller = PresenceController(registry, None)
            view = controller.observe_voice(
                {
                    "speakers": [{"track": "speaker_1", "label": "", "gender": {"label": "male"}}],
                    "timeline": [
                        {"track": "speaker_1", "label": "", "seconds": 1.4, "overlap": False, "gender": {"label": "male"}}
                    ],
                    "detail": "one voice",
                }
            )
        self.assertEqual(view.speaker_count, 1)
        self.assertEqual(view.active_track, "speaker_1")
        self.assertEqual(view.active_gender, "male")
        self.assertFalse(view.owner_speaking)

    def test_a_registered_owner_voice_is_reported_as_owner_speaking(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SpeakerRegistry(tmp)
            features = __import__("orbynth.voice.speakers", fromlist=["x"]).frame_features(tone(210.0))
            from orbynth.voice.speakers import signature_from_frames

            registry.enroll("Owner", signature_from_frames(features), kind="owner")
            controller = PresenceController(registry, None)
            view = controller.observe_voice(
                {
                    "speakers": [{"track": "speaker_1", "label": "Owner"}],
                    "timeline": [
                        {"track": "speaker_1", "label": "Owner", "seconds": 1.2, "overlap": False, "gender": {"label": "unknown"}}
                    ],
                }
            )
        self.assertTrue(view.owner_speaking)
        self.assertTrue(controller.snapshot().owner_present)


class PresenceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = Stack()
        self.addCleanup(self.stack.close)
        self.registry = SpeakerRegistry(os.path.join(self.tmp.name, "voice"))
        self.camera = CameraService(
            os.path.join(self.tmp.name, "cam"),
            self.stack.bus,
            detector=Detector(detect=lambda frame: [Face(box=(0, 0, 12, 12))], backend="stub"),
            frame_source=lambda: type("Frame", (), {"nbytes": 16})(),
        )
        self.presence = PresenceController(self.registry, self.camera, self.stack.bus)
        tools.set_services(
            tools.PresenceServices(
                speakers=self.registry,
                camera=self.camera,
                presence=self.presence,
                config=self.stack.config,
                bus=self.stack.bus,
            )
        )
        self.addCleanup(tools.set_services, None)
        self.wav = os.path.join(self.tmp.name, "owner.wav")
        with wave.open(self.wav, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(tone(205.0, seconds=1.4))

    def test_the_camera_cannot_be_looked_through_before_consent(self):
        result = self.stack.tool("camera_look", capabilities=["camera.view"])
        self.assertFalse(result.ok)
        self.assertIn("consent", result.error)

    def test_granting_consent_needs_the_enrolment_capability(self):
        denied = self.stack.tool("camera_consent", capabilities=["camera.view"], decision="grant")
        self.assertFalse(denied.ok)
        granted = self.stack.tool("camera_consent", capabilities=["camera.enroll"], decision="grant")
        self.assertTrue(granted.ok, granted.error)
        look = self.stack.tool("camera_look", capabilities=["camera.view"])
        self.assertTrue(look.ok, look.error)
        self.assertEqual(look.output["face_count"], 1)
        self.assertEqual(look.output["stranger_count"], 1, "nobody is enrolled, so the face is a stranger")

    def test_face_enrolment_without_an_embedding_model_refuses_instead_of_guessing(self):
        self.stack.tool("camera_consent", capabilities=["camera.enroll"], decision="grant")
        result = self.stack.tool("camera_enroll", capabilities=["camera.enroll"], name="Owner", kind="owner")
        self.assertFalse(result.ok)
        self.assertIn("embedding", result.error)
        self.assertEqual(self.camera.people, {})

    def test_a_voice_is_enrolled_named_and_analysed_back(self):
        enrolled = self.stack.tool(
            "speaker_enroll", capabilities=["voice.enroll"], name="Owner", path=self.wav, make_owner=True
        )
        self.assertTrue(enrolled.ok, enrolled.error)
        self.assertEqual(self.registry.owner(), "Owner")
        analysis = self.stack.tool("speaker_analyse", capabilities=["voice.identify"], path=self.wav)
        self.assertTrue(analysis.ok, analysis.error)
        self.assertEqual(analysis.output["speaker_count"], 1)
        self.assertEqual(analysis.output["named"], 1)
        self.assertEqual(analysis.output["speakers"][0]["label"], "Owner")

    def test_analysis_without_the_capability_is_denied(self):
        denied = self.stack.tool("speaker_analyse", capabilities=["camera.view"], path=self.wav)
        self.assertFalse(denied.ok)
        self.assertIn("capability denied", denied.error)

    def test_presence_observation_updates_the_live_view(self):
        observed = self.stack.tool("presence_observe", capabilities=["presence.read"], path=self.wav)
        self.assertTrue(observed.ok, observed.error)
        self.assertEqual(observed.output["voice"]["speaker_count"], 1)
        report = self.stack.tool("presence_report", capabilities=["presence.read"])
        self.assertTrue(report.ok, report.error)
        self.assertIn("camera_status", report.output)

    def test_the_decide_tool_refuses_private_speech_in_front_of_a_stranger(self):
        self.stack.tool("camera_consent", capabilities=["camera.enroll"], decision="grant")
        self.stack.tool("camera_look", capabilities=["camera.view"])
        decision = self.stack.tool(
            "presence_decide",
            capabilities=["presence.decide"],
            text="your password is 4412-tango",
            addressed=True,
        )
        self.assertTrue(decision.ok, decision.error)
        # Nobody is verified as the owner here (the stub camera cannot match faces and no voice
        # is enrolled), so private content is not merely quieter — it waits for the owner.
        self.assertEqual(decision.output["mode"], ASK_OWNER, decision.output)
        self.assertFalse(decision.output["audible"])

    def test_deciding_without_the_capability_is_denied(self):
        denied = self.stack.tool("presence_decide", capabilities=["presence.read"], text="hello")
        self.assertFalse(denied.ok)
        self.assertIn("capability denied", denied.error)


class ModeVocabularyTests(unittest.TestCase):
    def test_every_mode_the_policy_returns_is_a_documented_constant(self):
        # A mode string invented at a call site would be invisible to the interface and to the
        # tests, which is how a "stay silent" answer gets rendered as a normal reply.
        self.assertEqual({SPEAK, SPEAK_BRIEF, TEXT_ONLY, ASK_OWNER, WAIT, SILENT}, {
            "speak", "speak_brief", "text_only", "ask_owner", "wait", "silent",
        })


if __name__ == "__main__":
    unittest.main()
