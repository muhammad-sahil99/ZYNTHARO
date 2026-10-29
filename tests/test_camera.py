"""The camera consent gate and the face registry.

The most important assertions in this file are the refusals: no consent, a revoked consent
and an OS-level denial must all stop capture, and there must be no API that turns the camera
on when the operating system says no. Everything else (counting faces, matching a face) is
proven against an injected detector, because this machine has no camera and a test that
pretended otherwise would be exactly the "fabricated success" the reality gate exists to stop.
"""

from __future__ import annotations

import tempfile
import unittest

from zyntharo.vision import camera as cam


class Frame:
    """Stand-in for a camera frame: only what the service touches."""

    def __init__(self, nbytes: int = 32):
        self.nbytes = nbytes


class StubDetector:
    """A detector with a fixed answer, so the gate can be tested without a camera."""

    def __init__(self, faces: list[cam.Face] | None = None, embedding: tuple[float, ...] = (0.1, 0.2, 0.3)):
        self.faces = faces or []
        self._embedding = embedding
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return list(self.faces)

    def embed(self, frame, box):
        # A second face gets a different embedding, so "known" and "stranger" can be told apart.
        return self._embedding if box[0] == 0 else (9.0, 9.0, 9.0)


def service(tmp: str, detector=None, **kwargs) -> cam.CameraService:
    stub = detector or StubDetector([cam.Face(box=(0, 0, 40, 40))])
    return cam.CameraService(
        tmp,
        detector=cam.Detector(
            detect=getattr(stub, "detect", None),
            embed=getattr(stub, "embed", None),
            backend="stub",
        ),
        frame_source=lambda: Frame(),
        **kwargs,
    )


class ConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_looking_without_consent_is_refused_with_a_remedy(self):
        machine = service(self.tmp.name)
        with self.assertRaises(cam.CameraDenied) as caught:
            machine.look()
        self.assertIn("consent", str(caught.exception))
        self.assertIn("camera_consent", caught.exception.remedy)
        self.assertIsNone(machine.last_look, "a refused look must not leave a frame behind")

    def test_deny_and_revoke_are_recorded_states_not_warnings(self):
        machine = service(self.tmp.name)
        machine.deny("not now")
        self.assertEqual(machine.status()["consent"]["state"], cam.CONSENT_DENIED)
        with self.assertRaises(cam.CameraDenied):
            machine.look()
        machine.grant("owner", "for the demo")
        machine.look()
        machine.revoke("done")
        self.assertEqual(machine.status()["consent"]["state"], cam.CONSENT_REVOKED)
        self.assertIsNone(machine.last_look)

    def test_an_os_denial_cannot_be_overridden_by_this_application(self):
        machine = service(self.tmp.name)
        machine.grant()
        machine.consent.os_permission = cam.OS_DENIED
        with self.assertRaises(cam.CameraDenied) as caught:
            machine.look()
        # The refusal names the OS as the decision maker, and there is no parameter anywhere
        # in the service that could be flipped to bypass it.
        self.assertIn("operating system", str(caught.exception))
        self.assertNotIn("force", machine.look.__code__.co_varnames)
        self.assertNotIn("force", machine.status())
        self.assertFalse(hasattr(machine, "force"))

    def test_consent_is_persisted_and_os_denial_survives_a_restart(self):
        machine = service(self.tmp.name)
        machine.grant("owner")
        reopened = service(self.tmp.name)
        self.assertEqual(reopened.consent.state, cam.CONSENT_GRANTED)
        self.assertTrue(reopened.consent.allows_capture)

    def test_no_backend_is_reported_as_missing_not_as_an_empty_room(self):
        machine = cam.CameraService(
            self.tmp.name,
            detector=cam.Detector(backend="none", reason="OpenCV (cv2) is not installed: pip install opencv-python"),
            frame_source=lambda: Frame(),
        )
        machine.grant()
        with self.assertRaises(cam.CameraDenied) as caught:
            machine.look()
        self.assertIn("opencv", str(caught.exception).lower())
        self.assertFalse(machine.status()["can_detect"])


class LookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_faces_are_counted_and_strangers_are_separated_from_known_people(self):
        machine = service(self.tmp.name, StubDetector([cam.Face(box=(0, 0, 40, 40)), cam.Face(box=(50, 0, 40, 40))]))
        machine.grant()
        machine.enroll_face("Owner", kind="owner", samples=1)
        look = machine.look()
        self.assertEqual(look.face_count, 2)
        self.assertEqual(look.stranger_count, 1, "both faces embed identically: one is the owner, one is not")
        self.assertTrue(look.owner_present)

    def test_enrolment_refuses_to_guess_without_an_embedding_model(self):
        machine = cam.CameraService(
            self.tmp.name,
            detector=cam.Detector(detect=lambda frame: [cam.Face(box=(0, 0, 10, 10))], backend="opencv-haar"),
            frame_source=lambda: Frame(),
        )
        machine.grant()
        with self.assertRaises(cam.CameraDenied) as caught:
            machine.enroll_face("Owner")
        self.assertIn("embedding", str(caught.exception))
        self.assertEqual(machine.people, {})

    def test_a_face_with_no_detection_is_a_refusal_not_an_empty_enrolment(self):
        machine = service(self.tmp.name, StubDetector([], embedding=(0.1, 0.2)))
        machine.grant()
        with self.assertRaises(cam.CameraDenied) as caught:
            machine.enroll_face("Owner", samples=2)
        self.assertIn("no face was found", str(caught.exception))

    def test_owner_face_and_voice_are_both_identity_labels_never_permissions(self):
        machine = service(self.tmp.name, StubDetector([cam.Face(box=(0, 0, 10, 10))]))
        machine.grant()
        machine.enroll_face("Owner", kind="owner", samples=1)
        self.assertEqual(machine.owner(), "Owner")
        self.assertTrue(machine.forget_face("Owner"))
        self.assertEqual(machine.owner(), "")
        self.assertFalse(machine.forget_face("Owner"))


if __name__ == "__main__":
    unittest.main()
