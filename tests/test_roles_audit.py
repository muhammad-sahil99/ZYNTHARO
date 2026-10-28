"""Role audit: is every declared capability actually reachable by somebody?

This is the check that catches a specific class of mistake that has no other symptom: a tool is
declared, gated, tested — and no rank can ever call it. The Sovereign holds ``*``, so a naive
"is it held?" test always passes; the question that matters is whether a *delegable* role holds
it, because the Sovereign is exactly the agent that should be delegating rather than acting.

The remaining capabilities are listed in ``OWNER_ONLY_CAPABILITIES`` with a reason. If a new tool
is not reachable and not documented there, this test fails and the choice has to be made
deliberately.
"""

from __future__ import annotations

import unittest

from orbynth.agents import roles
from orbynth.config import CAPABILITIES, expand_capabilities


def held_by_delegable_roles() -> set[str]:
    held: set[str] = set()
    for role, template in roles.TEMPLATES.items():
        if role == "sovereign":
            continue
        held |= expand_capabilities(list(template.capabilities))
    return held


class RoleAuditTests(unittest.TestCase):
    def test_every_capability_is_reachable_or_documented_as_owner_only(self):
        reachable = held_by_delegable_roles()
        unreachable = {capability for capability in CAPABILITIES if capability not in reachable}
        self.assertEqual(
            unreachable,
            set(roles.OWNER_ONLY_CAPABILITIES),
            "a capability nobody can hold is a tool that can never run: grant it to a role or "
            "record it in OWNER_ONLY_CAPABILITIES with the reason",
        )

    def test_every_owner_only_capability_carries_a_real_reason(self):
        for capability, reason in roles.OWNER_ONLY_CAPABILITIES.items():
            self.assertIn(capability, CAPABILITIES, f"{capability} is not a declared capability")
            self.assertGreater(len(reason), 30, f"{capability} needs a reason a person can read")

    def test_no_role_can_enrol_an_identity(self):
        # Enrolment is the owner acting on their own biometrics. An agent that can enrol a voice
        # or a face can also re-enrol it for somebody else, which is impersonation with extra steps.
        for role, template in roles.TEMPLATES.items():
            if role == "sovereign":
                continue  # holds "*" by design; the owner-facing gate is what limits it
            granted = expand_capabilities(list(template.capabilities))
            self.assertNotIn("voice.enroll", granted, role)
            self.assertNotIn("camera.enroll", granted, role)

    def test_the_sovereign_can_still_reach_the_whole_action_surface(self):
        sovereign = expand_capabilities(list(roles.SOVEREIGN.capabilities))
        self.assertEqual(sovereign, set(CAPABILITIES))

    def test_the_foreman_can_run_scheduled_work_and_check_the_room(self):
        foreman = expand_capabilities(list(roles.FOREMAN.capabilities))
        for capability in ("task.schedule", "presence.decide", "presence.read", "voice.identify"):
            self.assertIn(capability, foreman)

    def test_an_operative_can_finish_its_own_file_work(self):
        operative = expand_capabilities(list(roles.OPERATIVE.capabilities))
        for capability in ("archive.zip", "archive.unzip", "filesystem.write"):
            self.assertIn(capability, operative)


if __name__ == "__main__":
    unittest.main()
