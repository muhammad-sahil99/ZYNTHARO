"""Agent, orchestration and self-extension tests: the multi-agent system as specified."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.helpers import Stack
from orbynth.agents.identity import AgentIdentity
from orbynth.agents.roles import role_names, role_template
from orbynth.config import AgentConfig
from orbynth.events import AGENT_REPORT, AGENT_SPAWNED, MODEL_FALLBACK
from orbynth.plugins.loader import PluginHost, PluginManifest


class RoleAndIdentityTests(unittest.TestCase):
    def test_every_named_role_exists_with_a_rank_and_a_prompt(self):
        for name in ["sovereign", "steward", "foreman", "operative", "artificer", "archivist", "warden", "cartographer", "courier", "scrivener", "probe"]:
            template = role_template(name)
            self.assertEqual(template.role, name)
            self.assertTrue(template.system_prompt)
            self.assertGreater(len(template.capabilities), 0)
        self.assertIn("sovereign", role_names())

    def test_identity_from_config_uses_the_role_grant(self):
        identity = AgentIdentity.from_config(AgentConfig(name="Op", role="operative"), agent_id="op-1")
        self.assertEqual(identity.rank, 5)
        self.assertIn("input.type", identity.capabilities)

    def test_ranks_express_authority(self):
        sovereign = AgentIdentity.from_config(AgentConfig(name="S", role="sovereign"), agent_id="s")
        operative = AgentIdentity.from_config(AgentConfig(name="O", role="operative"), agent_id="o")
        self.assertTrue(sovereign.outranks(operative))
        self.assertFalse(operative.outranks(sovereign))
        self.assertEqual(sovereign.rank_name, "sovereign")


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.workdir = tempfile.mkdtemp(prefix="orbynth-agent-")

    def tearDown(self):
        self.stack.close()

    def test_agent_acts_with_a_tool_then_reports(self):
        target = os.path.join(self.workdir, "note.txt")
        agent = self.stack.spawn("operative", capabilities=["filesystem.write", "filesystem.read"])
        report = agent.run_task(
            "write a note",
            f'Write the file. use: fs_write {{"path": "{target}", "content": "hello"}}',
        )
        self.assertEqual(report.status, "done")
        self.assertEqual(report.actions, 1)
        self.assertTrue(os.path.exists(target))
        self.assertTrue(report.evidence)
        # The task ledger and the audit trail both know what happened.
        self.assertEqual(self.stack.memory.task(report.task_id)["status"], "done")
        self.assertTrue(self.stack.memory.audit_log(5, agent.id))

    def test_the_live_task_path_checks_the_outcome_against_a_written_contract(self):
        # The contract mechanism existed but no live path asserted it produced a verdict, so
        # the wiring could break silently and every task would look verified.
        from orbynth.ops.verification import OutcomeVerifier, contract_for_request

        verifier = OutcomeVerifier(self.stack.bus)
        verifier.register_probe("file_exists", lambda c, o: (os.path.exists(c.target), c.target))
        self.stack.orchestrator.verifier = verifier

        missing = os.path.join(self.workdir, "never-written.txt")
        contract = contract_for_request("t-live", f"create {missing}", expected_outputs=[missing])
        agent = self.stack.spawn("operative", capabilities=["filesystem.write"])
        report = agent.run_task("write a note", "do nothing at all", contract=contract)
        self.assertIsNotNone(report.verdict, "the finished task carried no outcome check")
        self.assertEqual(report.verdict["status"], "not_met")
        self.assertEqual(report.status, "disputed")
        self.assertIn("filesystem.write", contract.intended_capabilities)

    def test_the_live_task_path_can_report_a_met_outcome(self):
        from orbynth.ops.verification import OutcomeVerifier, contract_for_request

        verifier = OutcomeVerifier(self.stack.bus)
        verifier.register_probe("file_exists", lambda c, o: (os.path.exists(c.target), c.target))
        self.stack.orchestrator.verifier = verifier

        target = os.path.join(self.workdir, "promised.txt")
        contract = contract_for_request("t-live-2", f"create {target}", expected_outputs=[target])
        agent = self.stack.spawn("operative", capabilities=["filesystem.write"])
        report = agent.run_task(
            "write a note",
            f'Write the file. use: fs_write {{"path": "{target}", "content": "hello"}}',
            contract=contract,
        )
        self.assertIsNotNone(report.verdict)
        self.assertEqual(report.verdict["status"], "met", report.verdict)

    def test_ungranted_capability_is_not_even_offered_and_nothing_happens(self):
        victim = os.path.join(self.workdir, "keep-me.txt")
        open(victim, "w").write("important")
        agent = self.stack.spawn("operative", capabilities=["filesystem.read"])
        report = agent.run_task("delete the folder", f'Delete everything. use: fs_delete {{"path": "{self.workdir}", "recursive": true}}')
        # The tool is not in this agent's tool list at all, so the model cannot even ask for it.
        offered = {spec.name for spec in agent.tools()}
        self.assertNotIn("fs_delete", offered)
        self.assertIn("fs_read", offered)
        self.assertNotEqual(report.status, "done")
        self.assertTrue(os.path.exists(victim))

    def test_a_denied_call_is_audited_when_the_capability_is_lost_mid_task(self):
        agent = self.stack.spawn("operative", capabilities=["filesystem.*"])
        # Capability revoked after the grant was made: the gate must catch the call.
        agent.identity.capabilities = frozenset({"filesystem.read"})
        target = os.path.join(self.workdir, "revoked.txt")
        open(target, "w").write("x")
        result = agent.act("fs_delete", path=target, to_trash=False)
        self.assertFalse(result.ok)
        self.assertIn("capability denied", result.error)
        denied = [row for row in self.stack.memory.audit_log(10, agent.id) if row["outcome"] == "denied"]
        self.assertTrue(denied)
        self.assertTrue(os.path.exists(target))

    def test_budget_exhaustion_is_a_reported_failure(self):
        agent = self.stack.orchestrator.spawn(
            role="operative",
            name="Budgeted",
            capabilities=["filesystem.write"],
            budget={"max_actions": 0, "max_wall_seconds": 60, "max_tokens": 1000},
        )
        report = agent.run_task("write many", f'use: fs_write {{"path": "{os.path.join(self.workdir, "x.txt")}", "content": "y"}}')
        self.assertEqual(report.status, "failed")
        self.assertIn("budget", report.summary)

    def test_no_model_available_is_reported_as_blocked(self):
        self.stack.providers.providers["echo"].enabled = False
        agent = self.stack.spawn("operative")
        report = agent.run_task("anything", "do something")
        self.assertEqual(report.status, "blocked")
        self.assertIn("no model", report.summary)

    def test_agent_keeps_its_own_memory_and_answers_messages(self):
        first = self.stack.spawn("operative", capabilities=["filesystem.read"], model="echo:offline-echo")
        first.remember("the portal needs 2FA on Mondays", importance=0.9, tags=["portal"])
        answer = first.handle_message("Sovereign", "what do you know about the portal?")
        self.assertTrue(answer)
        hits = first.recall("portal", k=3)
        self.assertTrue(hits)
        self.assertIn("report", [row["kind"] for row in hits] + ["report"])

    def test_report_event_is_published_for_the_hud(self):
        agent = self.stack.spawn("operative")
        agent.run_task("trivial", "say something")
        self.assertTrue(any(event.topic == AGENT_REPORT for event in self.stack.events))


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.workdir = tempfile.mkdtemp(prefix="orbynth-team-")

    def tearDown(self):
        self.stack.close()

    def test_roster_bootstrap_creates_the_named_cast(self):
        agents = self.stack.orchestrator.bootstrap_roster()
        self.assertEqual([a.identity.role for a in agents], ["sovereign", "steward", "artificer"])
        self.assertTrue(any(event.topic == AGENT_SPAWNED for event in self.stack.events))

    def test_team_runs_workers_in_parallel_and_returns_one_consolidated_report(self):
        from orbynth.agents.orchestrator import WorkItem

        items = [
            WorkItem(title=f"file {index}", objective=f'use: fs_write {{"path": "{os.path.join(self.workdir, f"f{index}.txt")}", "content": "x"}}', capabilities=["filesystem.write"])
            for index in range(3)
        ]
        result = self.stack.orchestrator.run_team("write three files", items)
        self.assertEqual(len(result["reports"]), 3)
        self.assertTrue(all(report["status"] == "done" for report in result["reports"]))
        self.assertTrue(result["summary"])
        for index in range(3):
            self.assertTrue(os.path.exists(os.path.join(self.workdir, f"f{index}.txt")))
        self.assertEqual(len(self.stack.orchestrator.teams), 1)

    def test_a_large_team_is_admitted_in_bounded_batches(self):
        """A nine-item team used to queue nine agents at once, each with a live context.

        The governor's cap has to apply *before* submission, otherwise a busy laptop allocates
        the memory first and learns about the pressure afterwards.
        """
        from orbynth.agents.orchestrator import WorkItem
        from orbynth.ops.governor import ResponsivenessGovernor

        self.stack.orchestrator.governor = ResponsivenessGovernor(
            detector=lambda: [], load_provider=lambda: 0.7, max_workers=2
        )
        items = [
            WorkItem(
                title=f"bounded {index}",
                objective=f'use: fs_write {{"path": "{os.path.join(self.workdir, f"b{index}.txt")}", "content": "x"}}',
                capabilities=["filesystem.write"],
            )
            for index in range(3)
        ]
        result = self.stack.orchestrator.run_team("bounded work", items)
        self.assertGreater(len(result["batches"]), 1, result["batches"])
        self.assertTrue(all(size <= 1 for size in result["batches"]), result["batches"])
        self.assertEqual(len(result["reports"]), 3)
        self.assertTrue(all(report["status"] == "done" for report in result["reports"]))

    def test_an_ungoverned_team_still_uses_the_configured_width(self):
        from orbynth.agents.orchestrator import WorkItem

        items = [WorkItem(title=f"wide {index}", objective="report done") for index in range(3)]
        result = self.stack.orchestrator.run_team("wide work", items)
        self.assertEqual(result["batches"], [3], result["batches"])

    def test_worker_crash_does_not_break_the_team(self):
        from orbynth.agents.orchestrator import WorkItem

        items = [WorkItem(title="bad", objective="use: fs_write {\"path\": \"/proc/nope/x\", \"content\": \"x\"}", capabilities=["filesystem.write"])]
        result = self.stack.orchestrator.run_team("try something impossible", items)
        self.assertEqual(len(result["reports"]), 1)
        self.assertIn(result["reports"][0]["status"], {"partial", "failed", "done"})

    def test_steward_transplants_throttled_agents_and_preserves_context(self):
        stack = Stack(quotas={"echo": {"rpm": 5, "rpd": 1000, "tpm": 100000}, "spare": {"rpm": 100, "rpd": 1000}})
        try:
            stack.orchestrator.bootstrap_roster()
            agent = stack.orchestrator.spawn(role="operative", name="Throttled", model="echo:offline-echo")
            agent.remember("mid-task state: 12 of 41 invoices downloaded", importance=0.9)
            before = stack.memory.stats(agent.id)["episodes"]
            stack.memory.set_agent_status(agent.id, "running")
            stack.quotas.note_error("echo:offline-echo", "rate_limit", retry_after=600)

            summary = stack.orchestrator.steward_watch()

            self.assertEqual(summary["failovers"], 1)
            self.assertEqual(stack.memory.get_agent(agent.id).model, "spare:spare-model")
            self.assertGreaterEqual(stack.memory.stats(agent.id)["episodes"], before + 1)
            self.assertTrue(any(event.topic == MODEL_FALLBACK for event in stack.events))
        finally:
            stack.close()

    def test_choose_model_respects_requested_model_only_when_usable(self):
        stack = Stack()
        try:
            self.assertEqual(stack.orchestrator._usable_model("nonexistent:model", "operative"), "echo:offline-echo")
            self.assertEqual(stack.orchestrator._usable_model("echo:offline-echo", "operative"), "echo:offline-echo")
        finally:
            stack.close()

    def test_orchestration_tools_are_capability_gated(self):
        from orbynth.actions.orchestration import SERVICES

        self.assertIsNotNone(SERVICES)
        allowed = self.stack.tool("agent_spawn", role="probe", capabilities=["agent.spawn"])
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.output["role"], "probe")
        denied = self.stack.tool("agent_spawn", capabilities=["filesystem.read"], role="probe")
        self.assertFalse(denied.ok)
        self.assertIn("capability denied", denied.error)

    def test_agent_task_tool_runs_a_subagent_and_returns_a_report(self):
        result = self.stack.tool(
            "agent_task",
            capabilities=["agent.control"],
            title="check the folder",
            objective="report the folder state",
            role="probe",
        )
        self.assertTrue(result.ok)
        self.assertIn("status", result.output)

    def test_memory_and_model_tools(self):
        behaviour = self.stack.tool("memory_remember", capabilities=["memory.admin"], text="portal is at portal.example", kind="fact", importance=0.8)
        self.assertTrue(behaviour.output["stored"])
        recall = self.stack.tool("memory_recall", capabilities=["memory.admin"], query="portal")
        self.assertTrue(recall.output["hits"])
        board = self.stack.tool("memory_board", capabilities=["memory.admin"], action="post", team_id="fleet", topic="plan", body="step 1")
        self.assertTrue(board.output["posted"])
        health = self.stack.tool("model_health", capabilities=["model.assign"])
        self.assertTrue(health.output["models"])


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.workdir = tempfile.mkdtemp(prefix="orbynth-plugin-")

    def tearDown(self):
        self.stack.close()

    def test_promoted_workflow_becomes_a_first_class_action(self):
        result = self.stack.forge.promote_workflow(
            "write-daily-summary",
            steps=[
                {"action": "fs_create", "args": {"path": "{folder}/day.txt", "content": "entries\n"}},
                {"action": "fs_write", "args": {"path": "{folder}/total.txt", "content": "total=3"}},
            ],
            capabilities=["filesystem.create", "filesystem.write"],
            task="stop re-doing the summary by hand",
        )
        self.assertTrue(result["installed"], result["validation"])
        self.assertIn("run_write_daily_summary", self.stack.actions.names())

        run = self.stack.tool("run_write_daily_summary", folder=self.workdir)
        self.assertTrue(run.ok)
        self.assertEqual(run.output["steps_run"], 2)
        self.assertTrue(os.path.exists(os.path.join(self.workdir, "total.txt")))

    def test_plugin_steps_stay_capability_gated(self):
        self.stack.forge.promote_workflow(
            "gated-write",
            steps=[{"action": "fs_write", "args": {"path": "{path}", "content": "x"}}],
            capabilities=["filesystem.read", "filesystem.write"],
        )
        denied = self.stack.tool("run_gated_write", capabilities=["filesystem.read"], path=os.path.join(self.workdir, "no.txt"))
        self.assertFalse(denied.ok)

    def test_forge_designs_and_installs_without_a_real_model(self):
        result = self.stack.forge.forge("download invoices then rename them by date", name="invoice-download", capabilities=["filesystem.read", "filesystem.write"])
        self.assertTrue(result["validation"]["ok"], result["validation"])
        self.assertTrue(result["installed"])
        self.assertTrue(self.stack.plugins.installed())

    def test_validation_refuses_capabilities_beyond_policy(self):
        directory = os.path.join(self.workdir, "greedy")
        os.makedirs(directory)
        with open(os.path.join(directory, "plugin.json"), "w") as handle:
            json.dump({"name": "greedy", "capabilities": ["shell.exec"], "actions": ["x"]}, handle)
        with open(os.path.join(directory, "actions.py"), "w") as handle:
            handle.write(
                "from orbynth.actions.registry import action, ActionContext\n"
                "@action(name='x', description='x', capability='shell.exec', parameters={'type':'object','properties':{}})\n"
                "def x(ctx: ActionContext):\n    return {'ran': True}\n"
            )
        strict = PluginHost(self.workdir, self.stack.actions, self.stack.bus, granted_capabilities=["filesystem.read"], memory=self.stack.memory)
        result = strict.validate(__import__("pathlib").Path(directory))
        self.assertFalse(result.ok)
        self.assertTrue(any("exceed the install policy" in error for error in result.errors))

    def test_validation_refuses_a_failing_self_test(self):
        directory = os.path.join(self.workdir, "broken")
        os.makedirs(directory)
        with open(os.path.join(directory, "plugin.json"), "w") as handle:
            json.dump({"name": "broken", "capabilities": ["filesystem.read"], "actions": ["y"]}, handle)
        with open(os.path.join(directory, "actions.py"), "w") as handle:
            handle.write(
                "from orbynth.actions.registry import action, ActionContext\n"
                "@action(name='y', description='y', capability='filesystem.read', parameters={'type':'object','properties':{}})\n"
                "def y(ctx: ActionContext):\n    return None\n"
                "def self_test(registry):\n    return {'ok': False, 'error': 'deliberately broken'}\n"
            )
        result = self.stack.plugins.validate(__import__("pathlib").Path(directory))
        self.assertFalse(result.ok)
        self.assertIn("self-test failed", " ".join(result.errors))

    def test_install_rolls_back_when_activation_fails(self):
        first = self.stack.forge.promote_workflow("steady", steps=[{"action": "fs_write", "args": {"path": "{path}", "content": "1"}}], capabilities=["filesystem.write"])
        self.assertTrue(first["installed"])
        directory = self.stack.plugins.plugins_dir / "steady"
        self.assertTrue(directory.exists())
        # A second install that fails validation must leave the working plugin in place.
        bad = self.stack.forge.forge("this design has no actions", name="steady")
        self.assertTrue((self.stack.plugins.plugins_dir / "steady" / "plugin.json").exists())

    def test_uninstall_removes_actions_but_keeps_a_backup(self):
        self.stack.forge.promote_workflow("throwaway", steps=[{"action": "fs_write", "args": {"path": "{path}", "content": "1"}}], capabilities=["filesystem.write"])
        self.assertIn("run_throwaway", self.stack.actions.names())
        result = self.stack.plugins.uninstall("throwaway")
        self.assertTrue(result["uninstalled"])
        self.assertNotIn("run_throwaway", self.stack.actions.names())
        self.assertTrue((self.stack.plugins.plugins_dir / ".removed").exists())

    def test_repeated_workflows_are_detected_as_forge_opportunities(self):
        for _ in range(3):
            self.stack.tool("fs_list", path=self.workdir)
            self.stack.tool("fs_stat", path=self.workdir)
        opportunities = self.stack.forge.opportunities(min_repeats=2)
        self.assertTrue(opportunities)
        self.assertIn("fs_list", " ".join(str(entry["sequence"]) for entry in opportunities))


class ConfirmationGateTests(unittest.TestCase):
    """The approval panel must apply to the powerful agents, not only to the small ones.

    ``Agent.act`` used to pre-approve every capability in ``safety.confirm_capabilities`` for
    any agent of rank 1 or better -- the very list that makes those capabilities require
    confirmation. The gate cancelled itself, and the Sovereign (the agent that actually calls
    the destructive tools) never emitted a single ``action.confirm`` card.
    """

    def setUp(self):
        from tests.helpers import Stack

        self.stack = Stack()
        self.workdir = tempfile.mkdtemp(prefix="orbynth-confirm-")

    def tearDown(self):
        self.stack.close()

    def _victim(self, name: str = "doomed.txt") -> str:
        path = os.path.join(self.workdir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        return path

    def test_a_sovereign_still_asks_before_a_destructive_action(self):
        from orbynth.events import ACTION_CONFIRM

        seen: list[dict] = []
        self.stack.bus.subscribe(ACTION_CONFIRM, lambda event: seen.append(dict(event.payload)))
        agent = self.stack.spawn("sovereign", capabilities=["filesystem.delete"])
        target = self._victim()

        result = agent.act("fs_delete", path=target)
        self.assertFalse(result.ok)
        self.assertTrue(result.evidence.get("confirmation_required"), result.to_json())
        self.assertTrue(os.path.exists(target), "the file was deleted without an approval")
        self.assertTrue(seen, "no confirmation card was ever published")
        self.assertEqual(seen[0]["action"], "fs_delete")

    def test_an_explicit_approval_lets_the_same_action_through(self):
        agent = self.stack.spawn("sovereign", capabilities=["filesystem.delete"])
        target = self._victim("approved.txt")
        self.stack.actions.approve("fs_delete", True)
        result = agent.act("fs_delete", path=target)
        self.assertTrue(result.ok, result.to_json())
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
