"""Regression tests for logic mistakes found by reading the code rather than by running it.

Each test here corresponds to a specific wrong assumption that shipped:

* a camera match threshold stricter than the model it wraps, so a face at the documented
  boundary was a stranger forever;
* ``recognition`` meaning "somebody was matched" instead of "matching was possible", which let a
  room of strangers fall back to a stale voice label saying the owner was present;
* a single unnamed voice counting as the owner because only *two* voices counted as strangers;
* an OS camera denial not being recorded, so the interface kept retrying a refused permission;
* a scheduled run interrupted by a crash being silently replayed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import wave

from zyntharo.actions import presence as presence_tools
from zyntharo.ops.lessons import LessonBook
from zyntharo.ops.presence import (
    ASK_OWNER,
    SILENT,
    SPEAK,
    SPEAK_BRIEF,
    TEXT_ONLY,
    CameraView,
    EtiquettePolicy,
    PresenceController,
    PresenceSnapshot,
    VoiceView,
)
from zyntharo.ops.schedule import Scheduler
from zyntharo.vision.camera import CameraDenied, CameraOsDenied, CameraService, Detector, Face
from zyntharo.voice.speakers import SpeakerRegistry
from tests.helpers import Stack
from tests.test_speakers import tone


class EmbeddingDetector:
    """A detector whose embedding can be moved to an exact distance from the enrolled one."""

    def __init__(self, faces: int = 1):
        self.faces = [Face(box=(0, 0, 20, 20)) for _ in range(faces)]
        self.vector = (0.0, 0.0, 0.0)

    def detect(self, frame):
        return list(self.faces)

    def embed(self, frame, box):
        return self.vector


def service(tmp: str, detector: EmbeddingDetector, **kwargs) -> CameraService:
    return CameraService(
        tmp,
        detector=Detector(detect=detector.detect, embed=detector.embed, backend="stub"),
        frame_source=lambda: type("Frame", (), {"nbytes": 16})(),
        **kwargs,
    )


class CameraThresholdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_face_at_the_models_documented_distance_still_matches(self):
        detector = EmbeddingDetector()
        machine = service(self.tmp.name, detector)
        machine.grant()
        machine.enroll_face("Owner", kind="owner", samples=1)
        # dlib's working threshold is distance 0.6. At 0.58 the owner must still be recognised.
        detector.vector = (0.58, 0.0, 0.0)
        look = machine.look()
        self.assertEqual(look.face_count, 1)
        self.assertEqual(look.faces[0].label, "Owner", look.faces[0].to_json())
        self.assertTrue(look.owner_present)

    def test_a_face_beyond_the_threshold_is_a_stranger(self):
        detector = EmbeddingDetector()
        machine = service(self.tmp.name, detector)
        machine.grant()
        machine.enroll_face("Owner", kind="owner", samples=1)
        detector.vector = (0.75, 0.0, 0.0)
        look = machine.look()
        self.assertEqual(look.faces[0].label, "")
        self.assertEqual(look.stranger_count, 1)
        self.assertFalse(look.owner_present)

    def test_matching_being_possible_is_reported_even_when_nobody_matched(self):
        detector = EmbeddingDetector()
        machine = service(self.tmp.name, detector)
        machine.grant()
        machine.enroll_face("Owner", kind="owner", samples=1)
        detector.vector = (0.9, 0.9, 0.9)
        look = machine.look()
        self.assertFalse(look.recognition, "nobody matched")
        self.assertTrue(look.recognition_available, "but matching did run")


class CameraOsDenialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_an_os_denial_is_recorded_so_the_interface_stops_pretending(self):
        def denied():
            raise CameraOsDenied("the operating system did not give this process a camera")

        machine = CameraService(
            self.tmp.name,
            detector=Detector(detect=lambda frame: [], backend="stub"),
            frame_source=denied,
        )
        machine.grant()
        with self.assertRaises(CameraDenied):
            machine.look()
        self.assertEqual(machine.consent.os_permission, "denied")
        # Persisted: a fresh process must not believe the camera is merely idle.
        reopened = CameraService(self.tmp.name, detector=Detector(backend="stub"), frame_source=denied)
        self.assertEqual(reopened.status()["consent"]["os_permission"], "denied")
        self.assertFalse(reopened.status()["consent"]["allows_capture"])

    def test_a_plain_device_error_is_not_mistaken_for_an_os_denial(self):
        def busy():
            raise CameraDenied("the camera opened but delivered no frame")

        machine = CameraService(self.tmp.name, detector=Detector(detect=lambda frame: [], backend="stub"), frame_source=busy)
        machine.grant()
        with self.assertRaises(CameraDenied):
            machine.look()
        self.assertNotEqual(machine.consent.os_permission, "denied")


class PresenceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.policy = EtiquettePolicy()

    def test_a_stranger_only_room_overrides_stale_voice_owner_evidence(self):
        snapshot = PresenceSnapshot(
            voice=VoiceView(speaker_count=1, owner_speaking=True, owner_enrolled=True, owner_label="Ayesha"),
            camera=CameraView(
                available=True,
                consent="granted",
                face_count=1,
                owner_present=False,
                stranger_count=1,
                recognition_available=True,
            ),
        )
        self.assertFalse(snapshot.owner_present, "the camera can tell, and the owner is not in the frame")
        self.assertTrue(snapshot.strangers_present)
        decision = self.policy.decide(snapshot, text="read the bank balance", addressed=True)
        # The camera can match faces and does not see the owner, so the speaker is not verified:
        # private content waits for the owner instead of being shown to whoever is in the room.
        self.assertEqual(decision.mode, ASK_OWNER, decision.reason)
        self.assertFalse(decision.audible)

    def test_one_unnamed_voice_is_a_stranger_when_an_owner_is_enrolled(self):
        snapshot = PresenceSnapshot(
            voice=VoiceView(speaker_count=1, active_label="", owner_enrolled=True, owner_label="Ayesha")
        )
        self.assertTrue(snapshot.strangers_present)
        non_private = self.policy.decide(snapshot, text="the report is ready", addressed=True)
        self.assertEqual(non_private.mode, SPEAK_BRIEF)
        self.assertTrue(non_private.requires_owner)
        private = self.policy.decide(snapshot, text="your password is 4412", addressed=True)
        self.assertEqual(private.mode, ASK_OWNER)
        self.assertFalse(private.audible)

    def test_a_verified_owner_alone_still_gets_a_normal_answer(self):
        snapshot = PresenceSnapshot(
            voice=VoiceView(
                speaker_count=1,
                active_label="Ayesha",
                owner_enrolled=True,
                owner_label="Ayesha",
                owner_speaking=True,
                verified=True,
            )
        )
        self.assertFalse(snapshot.strangers_present)
        decision = self.policy.decide(snapshot, text="the report is ready", addressed=True)
        self.assertEqual(decision.mode, SPEAK)

    def test_no_evidence_at_all_does_not_downgrade_every_answer(self):
        # Nothing observed yet is not the same as "a stranger is present"; over-downgrading here
        # would make every reply on a fresh start a whisper.
        decision = self.policy.decide(PresenceSnapshot(), text="the report is ready", addressed=True)
        self.assertEqual(decision.mode, SPEAK)

    def test_an_empty_room_and_no_addressing_is_still_silent(self):
        decision = self.policy.decide(PresenceSnapshot(), text="anything", addressed=False)
        self.assertEqual(decision.mode, SILENT)


class SpeakerVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = Stack()
        self.addCleanup(self.stack.close)
        self.registry = SpeakerRegistry(os.path.join(self.tmp.name, "voice"))
        self.camera = CameraService(os.path.join(self.tmp.name, "cam"), detector=Detector(backend="stub"))
        presence_tools.set_services(
            presence_tools.PresenceServices(
                speakers=self.registry,
                camera=self.camera,
                presence=PresenceController(self.registry, self.camera, self.stack.bus),
                config=self.stack.config,
                bus=self.stack.bus,
            )
        )
        self.addCleanup(presence_tools.set_services, None)

    def _wav(self, name: str, f0: float) -> str:
        path = os.path.join(self.tmp.name, name)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(tone(f0, seconds=1.4))
        return path

    def test_verifying_without_an_enrolled_owner_is_refused_not_answered(self):
        result = self.stack.tool("speaker_verify", capabilities=["voice.identify"], path=self._wav("x.wav", 200.0))
        self.assertFalse(result.ok)
        self.assertIn("no owner voice", result.error)

    def test_the_owners_voice_verifies_and_another_does_not(self):
        self.stack.tool(
            "speaker_enroll",
            capabilities=["voice.enroll"],
            name="Owner",
            path=self._wav("owner.wav", 205.0),
            make_owner=True,
        )
        same = self.stack.tool("speaker_verify", capabilities=["voice.identify"], path=self._wav("same.wav", 208.0))
        self.assertTrue(same.ok, same.error)
        self.assertTrue(same.output["verified"], same.output)
        other = self.stack.tool("speaker_verify", capabilities=["voice.identify"], path=self._wav("other.wav", 110.0))
        self.assertTrue(other.ok, other.error)
        self.assertFalse(other.output["verified"])
        self.assertEqual(other.output["verdict"], "not the enrolled owner")


class SchedulerCrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "schedule.json")

    def _write(self, task: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "tasks": [task]}, handle)

    def test_a_one_shot_interrupted_by_a_crash_is_reported_and_not_replayed(self):
        now = time.time()
        self._write(
            {
                "id": "t1",
                "title": "delete the temp folder",
                "objective": "delete the temp folder",
                "next_run": now - 5,
                "running_since": now - 3,
                "enabled": True,
            }
        )
        scheduler = Scheduler(path=self.path, runner=lambda task: {"status": "done"})
        task = scheduler.entries["t1"]
        self.assertFalse(task.enabled, "a half-finished one-shot must not run again unattended")
        self.assertIn("interrupted", task.last_outcome)
        self.assertEqual(scheduler.tick(now=now)["ran"], [])

    def test_a_recurring_task_interrupted_by_a_crash_waits_for_its_next_window(self):
        now = time.time()
        self._write(
            {
                "id": "t2",
                "title": "check the inbox",
                "objective": "check the inbox",
                "next_run": now - 5,
                "every_seconds": 300,
                "running_since": now - 3,
                "enabled": True,
            }
        )
        scheduler = Scheduler(path=self.path, runner=lambda task: {"status": "done"})
        task = scheduler.entries["t2"]
        self.assertTrue(task.enabled)
        self.assertGreater(task.next_run, now)
        self.assertEqual(scheduler.tick(now=now)["ran"], [])

    def test_a_run_in_flight_is_marked_on_disk_before_it_starts(self):
        seen: list[float] = []
        scheduler = Scheduler(path=self.path, runner=lambda task: seen.append(task.running_since) or None)
        entry = scheduler.schedule("do something durable", in_seconds=1, now=1000.0)
        scheduler.tick(now=1002.0)
        self.assertTrue(seen and seen[0] > 0, "running_since was never persisted before the run")
        self.assertEqual(scheduler.entries[entry.id].running_since, 0.0)

    def test_deferral_is_bounded_so_an_impossible_entry_stops_retrying(self):
        scheduler = Scheduler(
            path=self.path,
            runner=lambda task: {"status": "done"},
            gate=lambda task: (False, "the session is locked"),
            max_deferrals=3,
            defer_seconds=10,
        )
        entry = scheduler.schedule("open the spreadsheet", in_seconds=1, needs_gui=True, now=1000.0)
        for step in range(4):
            scheduler.tick(now=1001.0 + step * 20)
        task = scheduler.entries[entry.id]
        self.assertFalse(task.enabled, task.to_json())
        self.assertIn("gave up", task.last_reason)


class LessonAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.book = LessonBook(self.tmp.name)

    def test_the_first_failure_is_quiet_and_the_repeat_is_annotated(self):
        # A documented fix that has never seen a failure: hits starts at zero, not one.
        self.book.record("office_excel", "workbook is locked", fix="close the workbook first", count=False)
        first = self.book.annotate("office_excel", "workbook is locked")
        self.assertNotIn("known fix", first)
        second = self.book.annotate("office_excel", "workbook is locked")
        self.assertIn("known fix", second)
        self.assertIn("seen 2x", second)

    def test_writing_a_fix_down_is_not_another_failure(self):
        # ``lessons_note`` records the fix; the first real failure must still be quiet, or a
        # deliberately documented caveat makes every later failure look like a repeat.
        self.book.record("browser_click", "selector matched nothing", fix="use pierce=true", count=False)
        self.assertEqual(self.book.rows()[0]["hits"], 0, "a note about a failure is not a failure")
        quiet = self.book.annotate("browser_click", "selector matched nothing")
        self.assertNotIn("known fix", quiet)
        annotated = self.book.annotate("browser_click", "selector matched nothing")
        self.assertIn("known fix", annotated)


if __name__ == "__main__":
    unittest.main()
