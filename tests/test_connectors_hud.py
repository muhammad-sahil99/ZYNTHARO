"""The connected-accounts screen: declared fields, the owner-only commands, and sign-in sessions.

The screen is where a customer's trust is won or lost, so the tests here assert the things that
would quietly make it dishonest:

* every connectable account declares the inputs the interface must render — a form that does not
  match the calls it will make is a screen that lies;
* the mailbox form follows the chosen provider (Gmail must not ask for IMAP hosts);
* connecting stores the value and reports ``needs_setup`` when the provider refuses it, rather than
  turning green because a string was non-empty;
* a session for an interactive sign-in carries instructions and never a token;
* the interface actually has the controls, checked statically like the rest of the HUD tests.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from orbynth.connectors import base as cbase
from orbynth.connectors.registry import ConnectorRegistry
from orbynth.connectors.signin import DONE, FAILED, WAITING, SignInManager
from orbynth.connectors.store import Vault
from orbynth.hud.service import HudService
from tests.helpers import Stack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeclaredFieldsTests(unittest.TestCase):
    def setUp(self):
        self.registry = ConnectorRegistry(Vault(tempfile.mkdtemp()))

    def test_every_offered_account_declares_what_it_needs(self):
        """A connector with no fields would render an empty form and look broken."""
        missing = []
        for name in self.registry.names():
            if name in {"instagram", "facebook", "x", "linkedin", "tiktok", "snapchat", "apple_notes"}:
                continue
            connector = self.registry.get(name)
            fields = connector.fields()
            # A sign-in-only connector still declares the id that starts the flow.
            if not fields:
                missing.append(name)
            for field in fields:
                if not field.name or not field.label:
                    missing.append(name + ":unnamed")
        self.assertEqual(missing, [], f"connectors with no declared input: {missing}")

    def test_the_mailbox_form_follows_the_chosen_provider(self):
        gmail = self.registry.get("mail", "gmail")
        self.assertEqual([f.name for f in gmail.fields()], ["address", "password"])
        generic = self.registry.get("mail", "generic")
        names = [f.name for f in generic.fields()]
        self.assertEqual(names[:2], ["address", "password"])
        self.assertIn("imap_host", names)
        self.assertIn("smtp_host", names)
        # Hostnames are the one thing an unknown provider cannot be guessed for.
        self.assertTrue(all(not f.required for f in generic.fields() if f.name.endswith("_port")))

    def test_saas_fields_match_the_auth_style_that_is_actually_used(self):
        self.assertEqual([f.name for f in self.registry.get("slack").fields()], ["token"])
        trello = [f.name for f in self.registry.get("trello").fields()]
        self.assertEqual(trello, ["key", "token"])
        jira = [f.name for f in self.registry.get("jira").fields()]
        self.assertEqual(jira, ["email", "api_token"])
        # The email is a label, not a secret; the token is always masked.
        jira_connector = self.registry.get("jira")
        secrets = {f.name: f.secret for f in jira_connector.fields()}
        self.assertFalse(secrets["email"])
        self.assertTrue(secrets["api_token"])

    def test_the_oauth_accounts_declare_a_signin_instead_of_a_token_field(self):
        for name in ("google", "microsoft"):
            connector = self.registry.get(name)
            signin = [f for f in connector.fields() if f.signin]
            self.assertTrue(signin, name)
            self.assertEqual(signin[0].name, "refresh_token")
            self.assertFalse(signin[0].required)
        profiles = self.registry.signin.profiles("google")
        self.assertTrue(profiles)
        self.assertTrue(any(p["restricted"] for p in profiles), "Gmail read must be labelled restricted")

    def test_a_platform_that_cannot_be_reached_is_not_given_a_form(self):
        catalogue = self.registry.catalogue()
        self.assertIn("instagram", catalogue["unavailable"])
        self.assertIn("reviewed Meta app", catalogue["unavailable"]["instagram"])
        rows = [row for entries in catalogue["categories"].values() for row in entries]
        self.assertFalse([row for row in rows if row["name"] == "instagram"])


class HudConnectorCommandTests(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.registry = ConnectorRegistry(Vault(self.stack.config.home_path))
        self.service = HudService(
            config=self.stack.config,
            bus=self.stack.bus,
            memory=self.stack.memory,
            registry=self.stack.actions,
            runtime=self.stack.runtime,
            quotas=self.stack.quotas,
            orchestrator=self.stack.orchestrator,
            plugins=self.stack.plugins,
            connectors=self.registry,
        )

    def tearDown(self):
        self.stack.close()

    def test_the_snapshot_carries_accounts_without_making_a_network_call(self):
        """Painting the screen must never call a provider: statuses are local vault facts."""
        with mock.patch("orbynth.connectors.transport.request_json") as transport:
            snapshot = self.service.snapshot()["connectors"]
        transport.assert_not_called()
        self.assertTrue(snapshot["attached"])
        self.assertGreaterEqual(len(snapshot["rows"]), 17)
        self.assertIn("google", snapshot["signin_profiles"])
        # The operation lists are deliberately not in this payload.
        self.assertNotIn("operations_list", json.dumps(snapshot))

    def test_the_spec_returns_the_fields_and_operations_for_one_account(self):
        spec = self.service.route("connector.spec", {"connector": "slack"}, None)
        self.assertEqual(spec["connector"], "slack")
        self.assertEqual([f["name"] for f in spec["fields"]], ["token"])
        self.assertGreater(len(spec["operations"]), 3)
        self.assertTrue(any(op["capability"] == "connector.send" for op in spec["operations"]))

    def test_the_spec_for_an_unreachable_platform_gives_the_gate_not_an_empty_list(self):
        spec = self.service.route("connector.spec", {"connector": "instagram"}, None)
        self.assertEqual(spec["state"], "unavailable")
        self.assertIn("Meta", spec["detail"])
        self.assertEqual(spec["operations"], [])

    def test_connecting_stores_the_value_and_reports_the_providers_refusal(self):
        """A stored string is not a working account: only the provider can say it works."""
        with mock.patch(
            "orbynth.connectors.saas.request_json",
            side_effect=cbase.ConnectorError("invalid_auth"),
        ):
            result = self.service.route(
                "connector.connect",
                {"connector": "slack", "values": {"token": "xoxb-not-real"}},
                None,
            )
        self.assertEqual(result["state"], "needs_setup")
        self.assertIn("invalid_auth", result["detail"])
        # The credential is kept so a typo can be fixed without retyping every field...
        stored = self.registry.vault.get("slack")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.values["token"], "xoxb-not-real")
        # ...and it is never echoed back to the interface.
        self.assertNotIn("xoxb-not-real", json.dumps(result))

    def test_connecting_with_no_value_is_refused_before_anything_is_stored(self):
        with self.assertRaises(ValueError) as caught:
            self.service.route("connector.connect", {"connector": "slack", "values": {"token": "   "}}, None)
        self.assertIn("field value", str(caught.exception))
        self.assertIsNone(self.registry.vault.get("slack"))

    def test_an_undeclared_operation_is_refused_and_names_the_declared_ones(self):
        self.registry.vault.save(cbase.Connection(name="slack", values={"token": "xoxb-test"}))
        with self.assertRaises(ValueError) as caught:
            self.service.route(
                "connector.call", {"connector": "slack", "operation": "delete_workspace"}, None
            )
        message = str(caught.exception)
        self.assertIn("delete_workspace", message)
        self.assertIn("auth_test", message)

    def test_disconnecting_removes_the_credential_from_the_vault(self):
        self.registry.vault.save(cbase.Connection(name="github", values={"token": "ghp_x"}))
        result = self.service.route("connector.disconnect", {"connector": "github"}, None)
        self.assertTrue(result["disconnected"])
        self.assertEqual(self.registry.vault.load(), {})
        again = self.service.route("connector.disconnect", {"connector": "github"}, None)
        self.assertFalse(again["disconnected"])

    def test_a_build_without_connectors_says_so_instead_of_showing_zero(self):
        service = HudService(
            config=self.stack.config,
            bus=self.stack.bus,
            memory=self.stack.memory,
            registry=self.stack.actions,
            runtime=self.stack.runtime,
            quotas=self.stack.quotas,
            orchestrator=self.stack.orchestrator,
            plugins=self.stack.plugins,
        )
        snapshot = service.snapshot()["connectors"]
        self.assertFalse(snapshot["attached"])
        self.assertIn("no connector registry", snapshot["note"])
        with self.assertRaises(ValueError):
            service.route("connector.spec", {"connector": "slack"}, None)


class SignInSessionTests(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(tempfile.mkdtemp())
        self.registry = ConnectorRegistry(self.vault)

    def _manager(self, device=None, poll=None, browser=None):
        manager = SignInManager(
            self.registry, device_flow=device, poll_flow=poll, browser_flow=browser
        )
        self.registry.signin = manager
        return manager

    def _wait(self, manager, name, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            session = manager.status(name)
            if session is not None and session.done():
                return session
            time.sleep(0.02)
        self.fail("the sign-in session never finished")

    def test_microsoft_session_writes_tokens_and_never_reports_them(self):
        code = mock.Mock(
            user_code="ABCD-1234",
            verification_uri="https://microsoft.com/devicelogin",
            device_code="device-1",
            interval=1.0,
            expires_in=60.0,
            instruction=lambda: "Open the page and enter ABCD-1234.",
        )
        self.vault.save(cbase.Connection(name="microsoft", values={"client_id": "client-1"}))

        def poll(client_id, tenant, device_code, interval, expires_in):
            return {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_at": 4102444800.0}

        manager = self._manager(device=lambda *a: code, poll=poll)
        started = manager.start("microsoft", "desk")
        # The flow starts on its own thread, so the first snapshot is legitimately 'preparing'.
        self.assertIn(started.stage, {"preparing", WAITING, DONE})

        with mock.patch(
            "orbynth.connectors.microsoft.MicrosoftConnector.verify",
            autospec=True,
            side_effect=lambda self: self._ready("Microsoft 365 verified as me@example.com", "me@example.com"),
        ):
            session = self._wait(manager, "microsoft")

        self.assertEqual(session.stage, DONE, session.detail)
        stored = self.vault.get("microsoft")
        self.assertEqual(stored.values["access_token"], "access-secret")
        self.assertEqual(stored.values["refresh_token"], "refresh-secret")
        # The session payload is what the interface renders: instructions and an outcome.
        payload = json.dumps(session.to_json())
        self.assertIn("me@example.com", payload)
        self.assertNotIn("access-secret", payload)
        self.assertNotIn("refresh-secret", payload)

    def test_a_signin_without_its_client_id_says_what_is_missing(self):
        manager = self._manager(device=lambda *a: None, poll=lambda *a: {})
        manager.start("microsoft", "desk")
        session = self._wait(manager, "microsoft")
        self.assertEqual(session.stage, FAILED)
        self.assertIn("client) id", session.detail)

    def test_a_flow_that_raises_lands_as_a_failed_session_not_a_crash(self):
        self.vault.save(cbase.Connection(name="google", values={"client_id": "client-2"}))

        def browser(client_id, scopes, timeout=0, on_url=None):
            if on_url:
                on_url("https://accounts.google.com/o/oauth2/v2/auth?x=1")
            raise cbase.ConnectorError("Google refused the authorization: access_denied")

        manager = self._manager(browser=browser)
        manager.start("google", "mail_read")
        session = self._wait(manager, "google")
        self.assertEqual(session.stage, FAILED)
        self.assertIn("access_denied", session.detail)
        self.assertIn("accounts.google.com", session.url)
        # Nothing was written on a failed consent.
        self.assertEqual(self.vault.get("google").values, {"client_id": "client-2"})

    def test_a_connector_without_a_signin_is_told_to_paste_its_token(self):
        manager = self._manager()
        with self.assertRaises(cbase.ConnectorError) as caught:
            manager.start("slack")
        self.assertIn("paste", str(caught.exception).lower())

    def test_cancelling_marks_the_session_failed_for_the_interface(self):
        self.vault.save(cbase.Connection(name="google", values={"client_id": "client-3"}))

        def browser(client_id, scopes, timeout=0, on_url=None):
            time.sleep(0.5)
            return {"access_token": "late"}

        manager = self._manager(browser=browser)
        manager.start("google", "mail_read")
        self.assertTrue(manager.cancel("google"))
        session = manager.status("google")
        self.assertEqual(session.stage, FAILED)
        self.assertIn("cancelled", session.detail)

    def test_the_hud_route_starts_and_reports_a_session(self):
        stack = Stack()
        registry = ConnectorRegistry(Vault(stack.config.home_path))
        service = HudService(
            config=stack.config,
            bus=stack.bus,
            memory=stack.memory,
            registry=stack.actions,
            runtime=stack.runtime,
            quotas=stack.quotas,
            orchestrator=stack.orchestrator,
            plugins=stack.plugins,
            connectors=registry,
        )
        try:
            registry.vault.save(cbase.Connection(name="microsoft", values={"client_id": "client-4"}))
            code = mock.Mock(
                user_code="WXYZ-9876",
                verification_uri="https://microsoft.com/devicelogin",
                device_code="d",
                interval=0.01,
                expires_in=5.0,
                instruction=lambda: "enter WXYZ-9876",
            )
            registry.signin = SignInManager(
                registry,
                device_flow=lambda *a: code,
                poll_flow=lambda *a: {"access_token": "t", "refresh_token": "r", "expires_at": 4102444800.0},
            )
            # The mock has to be in place before the thread starts: the fake poll returns
            # instantly, so verification can happen before the first poll of this test.
            with mock.patch(
                "orbynth.connectors.microsoft.MicrosoftConnector.verify",
                autospec=True,
                side_effect=lambda self: self._ready("verified", "me@example.com"),
            ):
                started = service.route(
                    "connector.signin",
                    {"connector": "microsoft", "action": "start", "profile": "desk"},
                    None,
                )
                self.assertEqual(started["connector"], "microsoft")
                deadline = time.time() + 5
                status = started
                while time.time() < deadline:
                    status = service.route("connector.signin", {"connector": "microsoft", "action": "status"}, None)
                    if status.get("stage") in {DONE, FAILED}:
                        break
                    time.sleep(0.02)
            self.assertEqual(status["stage"], DONE, status.get("detail"))
            self.assertIn("microsoft", [row["name"] for row in service.snapshot()["connectors"]["rows"]])
        finally:
            stack.close()


class InterfaceTests(unittest.TestCase):
    def test_the_panel_exists_with_its_controls_and_rides_the_bridge(self):
        with open(os.path.join(ROOT, "hud", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(ROOT, "hud", "app.js"), encoding="utf-8") as handle:
            app = handle.read()
        for fragment in ('data-tab="connectors"', 'data-panel="connectors"', 'id="connectors"', 'id="connector-form"'):
            self.assertIn(fragment, html)
        for command in ("connector.spec", "connector.connect", "connector.verify", "connector.disconnect", "connector.signin"):
            self.assertIn(command, app)
        # The form is built from the connector's declared fields, not a hardcoded input list.
        self.assertIn("data-conn-field", app)
        self.assertIn("(spec.fields || []).forEach", app)


if __name__ == "__main__":
    unittest.main()