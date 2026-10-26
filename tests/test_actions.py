"""Action layer tests: filesystem, shell, the capability gate, anti-mistake input guarantees,
browser control and WhatsApp — all without a desktop, a browser or a phone."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
import zipfile

from tests.helpers import Stack
from orbynth.actions.browser import BrowserController, TabInfo, set_controller
from orbynth.actions.input_control import FocusGuard, RecordingBackend, WindowRef, set_backend, type_verified
from orbynth.actions.registry import DriverUnavailable
from orbynth.actions.whatsapp import MockWhatsApp, set_backend as set_whatsapp_backend
from orbynth.config import expand_capabilities


class ActionTestCase(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.workdir = tempfile.mkdtemp(prefix="orbynth-work-")

    def tearDown(self):
        self.stack.close()

    def path(self, name: str) -> str:
        return os.path.join(self.workdir, name)


class RegistryGateTests(ActionTestCase):
    def test_capability_denied_and_audited(self):
        result = self.stack.tool("fs_write", capabilities=["filesystem.read"], path=self.path("x.txt"), content="hi")
        self.assertFalse(result.ok)
        self.assertIn("capability denied", result.error)
        audit = self.stack.memory.audit_log(1)[0]
        self.assertEqual(audit["outcome"], "denied")
        self.assertFalse(os.path.exists(self.path("x.txt")))

    def test_confirm_level_action_requires_approval_then_runs(self):
        target = self.path("doomed.txt")
        open(target, "w").write("bye")
        blocked = self.stack.tool("fs_delete", capabilities=["filesystem.*"], confirmed=(), path=target)
        self.assertFalse(blocked.ok)
        self.assertIn("confirmation required", blocked.error)
        self.assertTrue(os.path.exists(target))

        self.stack.actions.approve("fs_delete")
        allowed = self.stack.tool("fs_delete", capabilities=["filesystem.*"], confirmed=(), path=target)
        self.assertTrue(allowed.ok)
        self.assertFalse(os.path.exists(target))

    def test_unknown_action_and_bad_arguments_are_recoverable(self):
        unknown = self.stack.tool("does_not_exist")
        self.assertFalse(unknown.ok)
        self.assertIn("unknown action", unknown.error)
        bad = self.stack.tool("fs_write", path=self.path("y.txt"), nonsense=1)
        self.assertFalse(bad.ok)
        self.assertIn("bad arguments", bad.error)
        self.assertIn("signature", bad.evidence)

    def test_dry_run_never_touches_the_disk(self):
        from orbynth.actions.registry import ActionContext

        context = ActionContext(agent_id="t", capabilities=expand_capabilities(["*"]), memory=self.stack.memory, config=self.stack.config, dry_run=True)
        result = self.stack.actions.invoke("fs_write", context, path=self.path("never.txt"), content="x")
        self.assertTrue(result.ok)
        self.assertTrue(result.output["dry_run"])
        self.assertFalse(os.path.exists(self.path("never.txt")))

    def test_driver_unavailable_is_an_expected_failure_not_a_crash(self):
        result = self.stack.tool("vision_capture")
        self.assertFalse(result.ok)
        self.assertIn("driver", result.error.lower())

    def test_allowed_roots_are_enforced(self):
        self.stack.config.safety.allowed_fs_roots = [self.workdir]
        inside = self.stack.tool("fs_write", path=self.path("ok.txt"), content="fine")
        self.assertTrue(inside.ok)
        outside = self.stack.tool("fs_write", path=os.path.join(tempfile.gettempdir(), "orbynth-outside.txt"), content="no")
        self.assertFalse(outside.ok)
        self.assertIn("outside allowed roots", outside.error)


class FilesystemTests(ActionTestCase):
    def test_write_read_range_and_stat(self):
        target = self.path("code.py")
        content = "\n".join(f"line {i}" for i in range(1, 21))
        self.assertTrue(self.stack.tool("fs_write", path=target, content=content).ok)
        read = self.stack.tool("fs_read", path=target, start_line=5, end_line=7)
        self.assertTrue(read.ok)
        self.assertEqual(read.output["content"], "line 5\nline 6\nline 7")
        self.assertEqual(read.output["total_lines_in_slice"], 20)
        stat = self.stack.tool("fs_stat", path=target)
        self.assertTrue(stat.output["sha256"])

    def test_edit_makes_a_backup_and_reports_replacements(self):
        target = self.path("cfg.ini")
        self.stack.tool("fs_write", path=target, content="mode = slow\nother = 1\n")
        result = self.stack.tool("fs_edit", path=target, find="slow", replace="fast")
        self.assertTrue(result.ok)
        self.assertEqual(result.output["replacements"], 1)
        self.assertIn("fast", open(target).read())
        self.assertTrue(os.path.exists(target + ".orbynth-bak"))

    def test_edit_without_a_match_fails_loudly(self):
        target = self.path("cfg.ini")
        self.stack.tool("fs_write", path=target, content="a = 1\n")
        result = self.stack.tool("fs_edit", path=target, find="zzz", replace="y")
        self.assertFalse(result.ok)
        self.assertIn("did not match", result.error)

    def test_binary_files_are_reported_not_mangled(self):
        target = self.path("blob.bin")
        with open(target, "wb") as handle:
            handle.write(bytes(range(256)) * 4)
        result = self.stack.tool("fs_read", path=target)
        self.assertTrue(result.ok)
        self.assertEqual(result.output["kind"], "binary")
        self.assertIn("preview_base64", result.output)

    def test_create_only_does_not_clobber(self):
        target = self.path("new.rs")
        first = self.stack.tool("fs_create", path=target)
        self.assertTrue(first.ok)
        self.assertIn("fn main", open(target).read())
        second = self.stack.tool("fs_create", path=target)
        self.assertFalse(second.ok)

    def test_move_copy_list_search(self):
        src = self.path("a.txt")
        self.stack.tool("fs_write", path=src, content="needle here\n")
        self.assertTrue(self.stack.tool("fs_move", src=src, dst=self.path("b.txt")).ok)
        self.assertTrue(self.stack.tool("fs_copy", src=self.path("b.txt"), dst=self.path("c.txt")).ok)
        listing = self.stack.tool("fs_list", path=self.workdir)
        self.assertEqual(listing.output["count"], 2)
        found = self.stack.tool("fs_search", root=self.workdir, content="needle", name_glob="*.txt")
        self.assertEqual(found.output["count"], 2)

    def test_zip_and_unzip_round_trip(self):
        self.stack.tool("fs_write", path=self.path("one.txt"), content="1")
        self.stack.tool("fs_write", path=self.path("two.txt"), content="2")
        archive = self.path("bundle.zip")
        zipped = self.stack.tool("fs_zip", paths=[self.path("one.txt"), self.path("two.txt")], dest=archive)
        self.assertTrue(zipped.ok)
        self.assertEqual(zipped.output["entries"], 2)
        extracted = self.stack.tool("fs_unzip", src=archive, dest=self.path("out"))
        self.assertTrue(extracted.ok)
        self.assertTrue(os.path.exists(self.path("out/one.txt")))

    def test_zip_slip_is_refused(self):
        archive = self.path("evil.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../../escaped.txt", "nope")
        result = self.stack.tool("fs_unzip", src=archive, dest=self.path("safe"))
        self.assertFalse(result.ok)
        self.assertIn("unsafe path", result.error)

    def test_delete_requires_confirmation_and_supports_directories(self):
        folder = self.path("tree/inner")
        os.makedirs(folder)
        open(os.path.join(folder, "f.txt"), "w").write("x")
        blocked = self.stack.tool("fs_delete", capabilities=["filesystem.*"], confirmed=(), path=self.path("tree"), recursive=True)
        self.assertFalse(blocked.ok)
        allowed = self.stack.tool("fs_delete", path=self.path("tree"), recursive=True, to_trash=False)
        self.assertTrue(allowed.ok)
        self.assertFalse(os.path.exists(self.path("tree")))


class ShellTests(ActionTestCase):
    def test_which_and_run(self):
        found = self.stack.tool("proc_which", name="python3")
        self.assertTrue(found.ok)
        self.assertTrue(found.output["installed"])
        run = self.stack.tool("proc_run", argv=["python3", "-c", "print(6*7)"])
        self.assertTrue(run.ok)
        self.assertEqual(run.output["stdout"].strip(), "42")
        self.assertEqual(run.output["exit_code"], 0)

    def test_missing_executable_is_reported_not_raised(self):
        result = self.stack.tool("proc_run", argv=["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.error)

    def test_timeout_is_bounded(self):
        result = self.stack.tool("proc_run", argv=["python3", "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)


class InputGuardTests(unittest.TestCase):
    """The two anti-mistake guarantees are tested with a recording backend: no display needed."""

    def setUp(self):
        self.backend = RecordingBackend()
        set_backend(self.backend)

    def tearDown(self):
        set_backend(None)

    def test_typing_verifies_read_back(self):
        guard = FocusGuard.capture("Target App")
        outcome = type_verified("hello world", guard, reader=lambda: self.backend.types_seen, retries=0)
        self.assertTrue(outcome.verified)
        self.assertEqual(outcome.typed, "hello world")

    def test_focus_drift_aborts_before_the_wrong_window_gets_text(self):
        # Focus is captured correctly, then stolen before the keystrokes go out.
        self.backend.focus_drift_after_reads = 1
        guard = FocusGuard.capture("Target App")
        with self.assertRaises(Exception) as caught:
            guard.verify("before typing")
        self.assertIn("focus guard aborted", str(caught.exception))
        self.assertEqual(self.backend.types_seen, "")

    def test_typing_stops_mid_flight_when_focus_is_lost(self):
        self.backend.focus_drift_after_reads = 1
        guard = FocusGuard.capture("Target App")
        outcome = type_verified("long text that must not land elsewhere", guard, reader=None, chunk=8, retries=0)
        self.assertTrue(outcome.aborted)
        self.assertLess(len(outcome.typed), len("long text that must not land elsewhere"))

    def test_dropped_keystrokes_are_not_reported_as_success(self):
        self.backend.drop_keystrokes = True
        guard = FocusGuard.capture("Target App")
        outcome = type_verified("important text", guard, reader=lambda: "", retries=0)
        self.assertFalse(outcome.verified)
        self.assertEqual(self.backend.types_seen, "")


class InputActionTests(ActionTestCase):
    def setUp(self):
        super().setUp()
        self.backend = RecordingBackend()
        set_backend(self.backend)

    def tearDown(self):
        set_backend(None)
        super().tearDown()

    def test_input_type_without_a_title_still_reports_guard_failure_honestly(self):
        # No display backends here: the driver must report the guard problem, not pretend.
        result = self.stack.tool("input_type", capabilities=["input.type"], text="hello", verify=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.output["typed_chars"], 5)

    def test_input_type_refuses_when_the_wrong_window_is_in_front(self):
        result = self.stack.tool(
            "input_type", capabilities=["input.type"], text="secret", expected_title="Some Other App", verify=False
        )
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.types_seen, "")

    def test_read_back_mismatch_fails_the_action(self):
        # Nothing on this machine can confirm the focused control's value, so the driver must
        # fail the call rather than claim the text was entered.
        self.backend.drop_keystrokes = True
        result = self.stack.tool("input_type", capabilities=["input.type"], text="hello", verify=True, retries=0)
        self.assertFalse(result.ok)
        self.assertIn("did not appear", result.error)

    def test_click_reports_coordinates_and_focus_state(self):
        result = self.stack.tool("input_click", capabilities=["input.mouse"], x=100, y=200, expected_title="Target App")
        self.assertTrue(result.ok)
        self.assertEqual(result.output["focus_clean"], True)


class FakeBrowser(BrowserController):
    """A scripted page so browser actions can be tested without a browser."""

    def __init__(self):
        super().__init__("test://")
        self.tabs_list = [TabInfo(id="t1", url="https://portal.example", title="Portal", active=True)]
        self.log: list[tuple] = []
        self.page = {"value": ""}

    def tabs(self):
        return self.tabs_list

    def open_tab(self, url=""):
        self.tabs_list.append(TabInfo(id=f"t{len(self.tabs_list)+1}", url=url, active=True))
        return self.tabs_list[-1]

    def close_tab(self, tab_id=""):
        self.tabs_list = [tab for tab in self.tabs_list if tab.id != (tab_id or self.tabs_list[-1].id)]
        return True

    def switch_tab(self, tab_id="", index=-1):
        self.log.append(("switch", tab_id, index))
        return self.tabs_list[index if index >= 0 else 0]

    def navigate(self, url, tab_id=""):
        self.tabs_list[0].url = url
        return {"url": url, "title": "Portal", "status": "ok"}

    def reload(self, hard=False):
        return {"url": self.tabs_list[0].url, "hard": hard}

    def click(self, target, by="css", nth=1):
        self.log.append(("click", target, by, nth))
        if target == "missing":
            from orbynth.actions.registry import ActionFailed

            raise ActionFailed("no element matched", {"candidates": ["Sign in"]})
        return {"clicked": target, "by": by, "found": True, "point": [120, 300]}

    def type_text(self, target, text, by="css", clear=True):
        self.log.append(("type", target, text))
        self.page["value"] = text
        return {"typed_into": target, "chars": len(text), "read_back": text, "verified": True}

    def fill_form(self, values):
        return {"filled": [{"field": k, "verified": True} for k in values], "count": len(values)}

    def scroll(self, dy=0, dx=0, to=""):
        return {"scroll": [0, dy], "to": to or None}

    def evaluate(self, script):
        if "location.href" in script and "document.body" not in script:
            return self.tabs_list[0].url
        if "document.body" in script or "innerText" in script:
            return {
                "url": self.tabs_list[0].url,
                "title": "Portal",
                "readyState": "complete",
                "headings": [{"level": "H1", "text": "Invoices"}],
                "links": [{"text": "Download", "href": "https://portal.example/i/1"}],
                "buttons": [{"text": "Sign in"}],
                "fields": [{"name": "email", "value": "", "type": "email"}],
                "tables": [{"rows": 2, "columns": 3, "firstRow": ["id", "amount", "date"]}],
                "dialogs": [],
                "text": "Invoices 41 outstanding",
            }
        return "ok"

    def screenshot(self, full_page=False):
        return b"\x89PNG\r\n\x1a\n" + b"0" * 32

    def cookies(self):
        return [{"name": "sid", "domain": ".portal.example", "value": "secret", "secure": True}]

    def history(self, limit=200):
        return [{"url": "https://portal.example"}]

    def key(self, combo):
        return {"key": combo}

    def chrome_geometry(self):
        return {"outer": [1600, 900], "inner": [1600, 780], "screen": [0, 0], "chrome_height": 120, "toolbar_y": 60}

    def downloads(self):
        return []


class BrowserActionTests(ActionTestCase):
    def setUp(self):
        super().setUp()
        self.browser = FakeBrowser()
        set_controller(self.browser)

    def tearDown(self):
        set_controller(None)
        super().tearDown()

    def test_tabs_open_switch_close(self):
        self.assertEqual(self.stack.tool("browser_tabs", capabilities=["browser.tabs"]).output["count"], 1)
        opened = self.stack.tool("browser_open_tab", capabilities=["browser.tabs"], url="https://example.com")
        self.assertEqual(opened.output["url"], "https://example.com")
        self.assertEqual(self.stack.tool("browser_tabs", capabilities=["browser.tabs"]).output["count"], 2)
        closed = self.stack.tool("browser_close_tab", capabilities=["browser.tabs"], tab_id="t2")
        self.assertTrue(closed.output["closed"])

    def test_navigate_normalises_bare_domains_and_phrases(self):
        result = self.stack.tool("browser_navigate", capabilities=["browser.navigate"], url="invoices example com")
        self.assertTrue(result.output["url"].startswith("https://www.google.com/search"))
        result2 = self.stack.tool("browser_navigate", capabilities=["browser.navigate"], url="portal.example/invoices")
        self.assertEqual(result2.output["url"], "https://portal.example/invoices")

    def test_click_and_type_report_evidence(self):
        click = self.stack.tool("browser_click", capabilities=["browser.click"], target="Sign in", by="text")
        self.assertTrue(click.ok)
        self.assertEqual(click.output["point"], [120, 300])
        typed = self.stack.tool("browser_type", capabilities=["browser.type"], target="email", text="me@example.com", by="name")
        self.assertTrue(typed.output["verified"])

    def test_click_on_missing_element_returns_candidates(self):
        result = self.stack.tool("browser_click", capabilities=["browser.click"], target="missing")
        self.assertFalse(result.ok)
        self.assertIn("Sign in", str(result.evidence))

    def test_page_model_contains_the_same_information_as_vision(self):
        model = self.stack.tool("browser_model", capabilities=["browser.extract"])
        self.assertTrue(model.ok)
        self.assertEqual(model.output["detail"], "text")
        self.assertIn("Invoices", model.output["text"])
        self.assertTrue(model.output["fields"])
        self.assertTrue(model.output["tabs"])

    def test_form_fill_and_screenshot(self):
        filled = self.stack.tool("browser_fill_form", capabilities=["browser.forms"], fields={"#email": "a@b.c", "#name": "Ann"})
        self.assertEqual(filled.output["count"], 2)
        shot = self.stack.tool("browser_screenshot", capabilities=["browser.extract"])
        self.assertGreater(shot.output["bytes"], 0)
        self.assertTrue(base64.b64decode(shot.output["png_base64"]).startswith(b"\x89PNG"))

    def test_eval_is_confirm_gated(self):
        blocked = self.stack.tool("browser_eval", capabilities=["browser.eval"], confirmed=(), script="1+1")
        self.assertFalse(blocked.ok)
        allowed = self.stack.tool("browser_eval", capabilities=["browser.eval"], script="1+1")
        self.assertTrue(allowed.ok)

    def test_cookie_values_are_never_exposed(self):
        result = self.stack.tool("browser_cookies", capabilities=["browser.cookies"])
        self.assertEqual(result.output["cookies"][0]["value"], "***")


class WhatsAppTests(ActionTestCase):
    def setUp(self):
        super().setUp()
        self.mock = MockWhatsApp()
        set_whatsapp_backend(self.mock)
        import orbynth.actions.whatsapp as whatsapp

        whatsapp.ALLOW_LIST = {"Mom"}

    def tearDown(self):
        set_whatsapp_backend(None)
        import orbynth.actions.whatsapp as whatsapp

        whatsapp.ALLOW_LIST = set()
        super().tearDown()

    def test_pairing_then_chat_flow(self):
        status = self.stack.tool("whatsapp_status", capabilities=["whatsapp.chat"])
        self.assertFalse(status.output["paired"])
        pair = self.stack.tool("whatsapp_pair", capabilities=["whatsapp.pair"])
        self.assertTrue(pair.output["paired"])
        chats = self.stack.tool("whatsapp_chats", capabilities=["whatsapp.chat"])
        self.assertGreaterEqual(chats.output["count"], 2)
        search = self.stack.tool("whatsapp_search", capabilities=["whatsapp.chat"], query="invoice")
        self.assertTrue(search.output["results"])

    def test_unpaired_actions_fail_with_a_next_step(self):
        result = self.stack.tool("whatsapp_chats", capabilities=["whatsapp.chat"])
        self.assertFalse(result.ok)
        self.assertIn("not paired", result.error)

    def test_sending_outside_the_allow_list_is_refused(self):
        self.stack.tool("whatsapp_pair", capabilities=["whatsapp.pair"])
        refused = self.stack.tool("whatsapp_send", capabilities=["whatsapp.chat"], chat="Unknown Group", text="hello")
        self.assertFalse(refused.ok)
        self.assertIn("allow-list", refused.error)
        allowed = self.stack.tool("whatsapp_send", capabilities=["whatsapp.chat"], chat="Mom", text="on my way")
        self.assertTrue(allowed.output["sent"])

    def test_voice_call_is_confirm_gated_and_transcribable(self):
        self.stack.tool("whatsapp_pair", capabilities=["whatsapp.pair"])
        blocked = self.stack.tool("whatsapp_call", capabilities=["whatsapp.call"], confirmed=(), chat="Mom")
        self.assertFalse(blocked.ok)
        placed = self.stack.tool("whatsapp_call", capabilities=["whatsapp.call"], chat="Mom")
        self.assertTrue(placed.output["answered"] is False)
        transcript = self.stack.tool("whatsapp_transcript", capabilities=["whatsapp.call"], seconds=3)
        self.assertIn("mock", transcript.output["text"])

    def test_media_send_requires_the_file_to_exist(self):
        self.stack.tool("whatsapp_pair", capabilities=["whatsapp.pair"])
        missing = self.stack.tool("whatsapp_send_media", capabilities=["whatsapp.media"], chat="Mom", path="/nope.png")
        self.assertFalse(missing.ok)
        real = self.path("pic.png")
        open(real, "wb").write(b"\x89PNG")
        sent = self.stack.tool("whatsapp_send_media", capabilities=["whatsapp.media"], chat="Mom", path=real, caption="hi")
        self.assertTrue(sent.output["sent"])


if __name__ == "__main__":
    unittest.main()
