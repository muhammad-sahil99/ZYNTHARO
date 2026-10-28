"""The fleet tools, exercised through the capability gate like any other action.

``tests/test_comms.py`` proves the store; this proves the *tools* — that an agent holding
``agent.messaging`` can ask for help and one without it is refused and audited, and that
``model_add`` refuses a nonsense envelope as an action failure the model can read rather
than a traceback.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from orbynth.actions import fleet
from orbynth.agents.comms import AgentComms
from orbynth.llm.reserve import ReservePool
from tests.helpers import Stack


class FleetToolTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.stack.close)
        self.comms = AgentComms(
            os.path.join(self.tmp.name, "comms.db"),
            self.stack.bus,
            roster=lambda: [
                {"agent_id": "sove-1", "name": "Sovereign", "role": "sovereign", "rank": 0},
                {"agent_id": "cart-1", "name": "Cartographer", "role": "cartographer", "rank": 4},
            ],
        )
        self.addCleanup(self.comms.close)
        self.reserves = ReservePool(
            self.stack.config,
            None,
            self.stack.bus,
            path=os.path.join(self.tmp.name, "reserves.json"),
            persist=False,
        )
        fleet.set_services(
            fleet.FleetServices(
                comms=self.comms,
                reserves=self.reserves,
                config=self.stack.config,
                bus=self.stack.bus,
            )
        )
        self.addCleanup(fleet.set_services, None)

    def test_asking_for_help_opens_a_request_the_target_can_read(self):
        result = self.stack.tool(
            "agent_ask",
            capabilities=["agent.messaging"],
            to="role:cartographer",
            question="How do I click a shadow-DOM button?",
            context="Selector matches nothing at the top level.",
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.output["kind"], "request")
        inbox = self.stack.tool("agent_inbox", capabilities=["agent.messaging"], limit=5)
        self.assertTrue(inbox.ok, inbox.error)
        # The request is open (nobody has answered it) and addressed to the specialist role.
        self.assertEqual(inbox.output["stats"]["open_requests"], 1)
        self.assertEqual(len(self.comms.open_requests()), 1)
        self.assertEqual(len(self.comms.inbox("cart-1")), 1)

    def test_asking_without_the_capability_is_denied_and_audited(self):
        result = self.stack.tool(
            "agent_ask",
            capabilities=["filesystem.read"],
            to="role:cartographer",
            question="sneaky",
        )
        self.assertFalse(result.ok)
        self.assertIn("capability denied", result.error)
        self.assertEqual(self.comms.stats()["messages"], 0)

    def test_unknown_recipient_becomes_a_readable_action_failure(self):
        result = self.stack.tool("agent_ask", capabilities=["agent.messaging"], to="ghost", question="anyone?")
        self.assertFalse(result.ok)
        self.assertIn("unknown recipient", result.error)
        self.assertIn("Cartographer", result.error)

    def test_handing_over_a_file_stores_a_copy(self):
        source = os.path.join(self.tmp.name, "evidence.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("dom snapshot\n")
        result = self.stack.tool(
            "agent_send_file",
            capabilities=["agent.messaging"],
            to="sove-1",
            paths=[source],
            note="for the report",
        )
        self.assertTrue(result.ok, result.error)
        stored = result.output["files"][0]
        self.assertTrue(os.path.exists(stored["stored_at"]))
        self.assertEqual(len(stored["sha256"]), 64)

    def test_model_add_records_a_reserve_with_its_weekly_limits(self):
        result = self.stack.tool(
            "model_add",
            capabilities=["model.manage"],
            provider="groq",
            model_id="llama-test",
            rpm=30,
            rpd=14_400,
            rpw=100_000,
            tpm=6_000,
            tpd=500_000,
            tpw=3_000_000,
            base_url="https://api.groq.test/v1",
            api_key_env="GROQ_API_KEY",
            tags=["fast"],
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.output["reserve"])
        spec = self.stack.config.model("groq:llama-test")
        self.assertEqual(spec.rpw, 100_000)
        self.assertEqual(spec.tpw, 3_000_000)

    def test_model_add_refuses_a_contradictory_envelope(self):
        result = self.stack.tool(
            "model_add",
            capabilities=["model.manage"],
            provider="weird",
            model_id="m",
            rpm=10,
            rpd=5000,
            rpw=100,
            base_url="https://weird.test/v1",
            api_key_env="WEIRD_KEY",
        )
        self.assertFalse(result.ok)
        self.assertIn("smaller than the daily cap", result.error)

    def test_activating_a_reserve_is_a_gated_manager_action(self):
        self.stack.tool(
            "model_add",
            capabilities=["model.manage"],
            provider="groq",
            model_id="llama-test",
            rpm=30,
            base_url="https://api.groq.test/v1",
            api_key_env="GROQ_API_KEY",
        )
        denied = self.stack.tool("model_activate", capabilities=["model.assign"], model="groq:llama-test")
        self.assertFalse(denied.ok)
        allowed = self.stack.tool("model_activate", capabilities=["model.manage"], model="groq:llama-test", reason="primary out")
        self.assertTrue(allowed.ok, allowed.error)
        self.assertTrue(allowed.output["activated"])
        bench = self.stack.tool("model_reserves", capabilities=["model.manage"], only_reserves=True)
        self.assertEqual(bench.output["count"], 0)


if __name__ == "__main__":
    unittest.main()
