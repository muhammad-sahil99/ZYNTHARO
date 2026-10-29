"""Connected accounts: the parts that would break a customer's trust if they drifted.

Three classes of behaviour are asserted here, each because a naive version is wrong in a way that
looks fine until it matters:

* **The vault** must protect what it can, say so honestly when it cannot, and never leak a value.
* **A connector** must refuse an undeclared operation, keep the provider's own error text, and mark
  the mail provider that Microsoft has made impossible instead of failing at login.
* **The gate** must stop a read-only agent from sending mail by naming a different operation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from zyntharo.actions import connectors as connector_actions
from zyntharo.actions.registry import ActionContext, ActionRegistry
from zyntharo.connectors import base as cbase
from zyntharo.connectors.mail import PROVIDERS, MailConnector, provider_list
from zyntharo.connectors.registry import ConnectorRegistry, UNSUPPORTED
from zyntharo.connectors.saas import SLACK, SaasConnector
from zyntharo.connectors.store import Vault, redact


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.vault = Vault(self.dir)

    def test_save_load_remove_roundtrip(self):
        self.vault.save(cbase.Connection(name="slack", values={"token": "xoxb-secret"}, meta={"account": "acme"}))
        loaded = self.vault.get("slack")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.values["token"], "xoxb-secret")
        self.assertEqual(loaded.meta["account"], "acme")
        self.assertTrue(self.vault.remove("slack"))
        self.assertIsNone(self.vault.get("slack"))
        self.assertFalse(self.vault.remove("slack"))

    def test_summary_lists_keys_never_values(self):
        self.vault.save(cbase.Connection(name="github", values={"token": "ghp_verysecret"}))
        summary = self.vault.summary()
        self.assertEqual(summary[0]["keys"], ["token"])
        self.assertNotIn("ghp_verysecret", json.dumps(summary))

    def test_protection_is_reported_not_assumed(self):
        # On Windows DPAPI is used; elsewhere the file is 0600 and the object must say so.
        self.assertEqual(self.vault.at_rest_protected, sys.platform == "win32")
        note = self.vault.protection_note
        self.assertTrue("DPAPI" in note or "0600" in note)

    def test_file_permissions_are_restricted(self):
        self.vault.save(cbase.Connection(name="notion", values={"token": "secret"}))
        if os.name != "nt":
            self.assertEqual(self.vault.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_store_reports_the_fix(self):
        self.vault.path.parent.mkdir(parents=True, exist_ok=True)
        self.vault.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(OSError) as caught:
            Vault(self.dir).load()
        self.assertIn("Move it aside", str(caught.exception))

    def test_redact_keeps_only_the_tail(self):
        self.assertEqual(redact(""), "")
        self.assertNotIn("secret", redact("supersecret"))
        self.assertTrue(redact("supersecret").endswith("cret"))


class MailConnectorTests(unittest.TestCase):
    def test_unconfigured_status_is_an_instruction(self):
        status = MailConnector(None, provider="gmail").status()
        self.assertEqual(status.state, "needs_setup")
        self.assertIn("app password", status.detail.lower())

    def test_outlook_is_declared_impossible_not_broken(self):
        entry = next(p for p in provider_list() if p["key"] == "outlook")
        self.assertFalse(entry["supported_here"])
        self.assertIn("microsoft", entry["note"].lower())
        connector = MailConnector(cbase.Connection(name="mail", values={"address": "a@b.c", "password": "x"}, meta={"provider": "outlook"}))
        self.assertEqual(connector.status().state, "needs_setup")
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector.call("list_folders")
        self.assertIn("Microsoft", str(caught.exception))

    def test_generic_provider_without_hosts_says_what_is_missing(self):
        connector = MailConnector(cbase.Connection(name="mail", values={"address": "a@b.c", "password": "x"}, meta={"provider": "generic"}))
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector.call("list_folders")
        self.assertIn("imap_host", str(caught.exception))

    def test_every_operation_is_declared_and_routable(self):
        connector = MailConnector(None)
        names = {op.name for op in connector.operations()}
        self.assertIn("send", names)
        self.assertIn("search", names)
        for name in names:
            self.assertTrue(callable(getattr(connector, f"_op_{name}", None)), name)

    def test_send_is_a_send_capability(self):
        op = next(op for op in MailConnector(None).operations() if op.name == "send")
        self.assertEqual(op.capability, "connector.send")
        self.assertTrue(op.mutates)

    def test_unknown_operation_is_refused_with_the_list(self):
        connector = MailConnector(None)
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector._operation("delete_everything")
        self.assertIn("list_folders", str(caught.exception))

    def test_html_only_mail_decodes_to_text(self):
        import email as email_module

        raw = (
            "From: a@b.c\r\nSubject: Hi\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello <b>there</b></p>"
        )
        message = email_module.message_from_string(raw)
        from zyntharo.connectors.mail import _text_body

        body = _text_body(message)
        self.assertIn("Hello", body)
        self.assertNotIn("<b>", body)


class SaasConnectorTests(unittest.TestCase):
    def test_operations_are_declared_and_unknown_is_refused(self):
        connector = SaasConnector(SLACK, None)
        names = {op.name for op in connector.operations()}
        self.assertTrue({"auth_test", "post_message", "history"} <= names)
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector.call("delete_workspace")
        self.assertIn("post_message", str(caught.exception))

    def test_unconnected_is_needs_setup_with_the_where(self):
        status = SaasConnector(SLACK, None).status()
        self.assertEqual(status.state, "needs_setup")
        self.assertIn("Bot User OAuth Token", SaasConnector(SLACK, None).where)

    def test_call_builds_the_documented_request(self):
        connection = cbase.Connection(name="slack", values={"token": "xoxb-abc"})
        connector = SaasConnector(SLACK, connection)
        with mock.patch("zyntharo.connectors.saas.request_json", return_value={"ok": True, "ts": "1"}) as patched:
            result = connector.call("post_message", channel="C1", text="hello")
        self.assertEqual(result["ts"], "1")
        method, url = patched.call_args.args[0], patched.call_args.args[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(patched.call_args.kwargs["headers"]["Authorization"], "Bearer xoxb-abc")
        self.assertEqual(patched.call_args.kwargs["body"], {"channel": "C1", "text": "hello"})

    def test_slack_ok_false_surfaces_the_platform_error(self):
        connector = SaasConnector(SLACK, cbase.Connection(name="slack", values={"token": "x"}))
        with mock.patch(
            "zyntharo.connectors.saas.request_json",
            return_value={"ok": False, "error": "channel_not_found"},
        ):
            with self.assertRaises(cbase.ConnectorError) as caught:
                connector.call("history", channel="C9")
        self.assertIn("channel_not_found", str(caught.exception))

    def test_key_and_token_style_goes_into_the_query(self):
        from zyntharo.connectors.saas import TRELLO

        connection = cbase.Connection(name="trello", values={"key": "k", "token": "t"})
        connector = SaasConnector(TRELLO, connection)
        with mock.patch("zyntharo.connectors.saas.request_json", return_value={"id": "me"}) as patched:
            connector.call("member")
        params = patched.call_args.kwargs["params"]
        self.assertEqual(params["key"], "k")
        self.assertEqual(params["token"], "t")

    def test_path_placeholders_are_required(self):
        from zyntharo.connectors.saas import GITHUB

        connector = SaasConnector(GITHUB, cbase.Connection(name="github", values={"token": "t"}))
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector.call("issues")
        self.assertIn("owner", str(caught.exception))

    def test_missing_required_value_is_named(self):
        connector = SaasConnector(SLACK, cbase.Connection(name="slack", values={}))
        with self.assertRaises(cbase.ConnectorError) as caught:
            connector.call("auth_test")
        self.assertIn("token", str(caught.exception))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.registry = ConnectorRegistry(Vault(self.dir))

    def test_every_declared_connector_reports_state(self):
        rows = self.registry.to_json()
        self.assertGreaterEqual(len(rows), 17)
        for row in rows:
            self.assertIn(row["state"], {"ready", "configured", "needs_setup", "unavailable"})
            self.assertTrue(row["detail"], row["name"])

    def test_gated_platforms_quote_the_business_gate(self):
        for name in UNSUPPORTED:
            with self.assertRaises(cbase.Unsupported) as caught:
                self.registry.get(name)
            self.assertIn(UNSUPPORTED[name][:40], str(caught.exception))

    def test_unknown_connector_lists_the_real_ones(self):
        with self.assertRaises(KeyError) as caught:
            self.registry.get("myspace")
        self.assertIn("slack", str(caught.exception))

    def test_failed_verify_keeps_the_credential_for_a_retry(self):
        with mock.patch.object(SaasConnector, "verify", side_effect=cbase.ConnectorError("bad token")):
            status = self.registry.connect("slack", {"token": "xoxb-wrong"}, {})
        self.assertEqual(status.state, "needs_setup")
        self.assertIn("bad token", status.detail)
        self.assertIsNotNone(self.registry.vault.get("slack"))

    def test_successful_connect_is_recorded_as_verified(self):
        with mock.patch.object(SaasConnector, "verify", return_value=cbase.ConnectorStatus("slack", "ready", "ok", "api_token", "acme")):
            status = self.registry.connect("slack", {"token": "xoxb-right"})
        self.assertEqual(status.state, "ready")
        self.assertGreater(self.registry.vault.get("slack").verified_at, 0)
        self.assertIn("slack", self.registry.ready())

    def test_call_refuses_before_the_credential_is_proven(self):
        self.registry.vault.save(cbase.Connection(name="slack", values={"token": "x"}))
        with self.assertRaises(cbase.ConnectorError) as caught:
            self.registry.call("slack", "auth_test")
        self.assertIn("not ready", str(caught.exception))


class GateTests(unittest.TestCase):
    """A read-only agent must not be able to send mail by renaming the operation."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.registry = ConnectorRegistry(Vault(self.dir))
        self.registry.vault.save(
            cbase.Connection(
                name="slack",
                values={"token": "xoxb-real"},
                meta={"account": "acme"},
                verified_at=1.0,
            )
        )
        connector_actions.set_services(connector_actions.ConnectorServices(self.registry))
        self.actions = ActionRegistry()
        self.actions.register_all(connector_actions.all_actions())

    def tearDown(self):
        connector_actions.set_services(None)

    def test_read_cannot_reach_a_send_operation(self):
        ctx = ActionContext(capabilities=frozenset({"connector.read"}))
        with mock.patch.object(SaasConnector, "call") as called:
            result = self.actions.invoke("connector_read", ctx, connector="slack", operation="post_message", args={"channel": "C", "text": "hi"})
        self.assertFalse(result.ok)
        self.assertIn("connector.send", result.error)
        called.assert_not_called()

    def test_read_reaches_a_read_operation(self):
        ctx = ActionContext(capabilities=frozenset({"connector.read"}))
        with mock.patch.object(SaasConnector, "call", return_value={"ok": True}) as called:
            result = self.actions.invoke("connector_read", ctx, connector="slack", operation="history", args={"channel": "C"})
        self.assertTrue(result.ok, result.error)
        called.assert_called_once()

    def test_send_requires_confirmation(self):
        ctx = ActionContext(capabilities=frozenset({"connector.send"}))
        result = self.actions.invoke("connector_send", ctx, connector="slack", operation="post_message", args={"channel": "C", "text": "hi"})
        self.assertFalse(result.ok)
        self.assertIn("confirmation required", result.error)

    def test_send_works_once_confirmed(self):
        ctx = ActionContext(capabilities=frozenset({"connector.send"}), confirmed=("connector_send",))
        with mock.patch.object(SaasConnector, "call", return_value={"ok": True, "ts": "2"}) as called:
            result = self.actions.invoke("connector_send", ctx, connector="slack", operation="post_message", args={"channel": "C", "text": "hi"})
        self.assertTrue(result.ok, result.error)
        called.assert_called_once()

    def test_connector_send_is_owner_only_and_irreversible(self):
        from zyntharo.ops.risk import is_irreversible

        self.assertTrue(is_irreversible("connector.send"))
        self.assertFalse(is_irreversible("connector.read"))

    def test_capabilities_are_declared(self):
        from zyntharo.config import CAPABILITIES

        for capability in ("connector.read", "connector.write", "connector.send", "connector.admin"):
            self.assertIn(capability, CAPABILITIES)

    def test_catalogue_reports_counts_and_vault_honesty(self):
        data = self.registry.catalogue()
        self.assertGreater(data["total"], 10)
        self.assertIn("at_rest_protected", data["vault"])
        self.assertTrue(data["mail_providers"])


if __name__ == "__main__":
    unittest.main()
