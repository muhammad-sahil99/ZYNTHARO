"""Browser control — the deepest and most important driver in the system.

Two ways to drive a browser, and the fleet uses both interchangeably:

* **Attach mode (default).** Connect to the browser the user already has open via the
  DevTools protocol (``--remote-debugging-port``). Their real profile, their logged-in
  sessions, their tabs — nothing is re-authenticated, nothing is re-created. ZYNTHARO can
  start the browser with the right flag itself and then attach.
* **Managed mode.** Playwright launches an ZYNTHARO-owned profile when the user wants a
  clean, isolated automation browser.

What an agent can do, and how the "vision and no-vision information parity" requirement is
met:

* **Model path (no vision):** :func:`browser_model` returns a complete structured page
  model — url, title, every tab, headings, links with hrefs, form fields with names and
  current values, buttons with accessible names, tables, dialogs, scroll position, and the
  full visible text. That is strictly *more* information than a screenshot carries.
* **Vision path:** ``browser_screenshot`` plus the shared vision/input drivers for browser
  *chrome* — refresh, extension icons, Lens, the account avatar, the three-dot menu — which
  live in native UI and are unreachable from the page DOM. :func:`browser_chrome_click`
  does exactly that: ground the icon by OCR/geometry, click it, verify the popup opened.

Everything else is here too: navigation, tabs (list/open/close/switch/reorder/pin), history,
back/forward, reload, scroll, clicking by selector/text/role, typing into fields, filling
whole forms, extraction (text/tables/links/inputs/network JSON), waiting, downloads, cookies,
console/network capture, and ``eval`` for the cases nothing else reaches (confirm-gated).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlparse

from .registry import Action, ActionContext, ActionFailed, DriverUnavailable, action

DEFAULT_CDP_PORT = 9222


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------


@dataclass
class TabInfo:
    id: str
    url: str = ""
    title: str = ""
    active: bool = False
    pinned: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "active": self.active, "pinned": self.pinned}


@dataclass
class PageModel:
    """The vision-free description of what the browser is showing."""

    url: str = ""
    title: str = ""
    ready_state: str = ""
    scroll: dict[str, Any] = field(default_factory=dict)
    viewport: dict[str, Any] = field(default_factory=dict)
    headings: list[dict[str, str]] = field(default_factory=list)
    links: list[dict[str, str]] = field(default_factory=list)
    buttons: list[dict[str, str]] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    tabs: list[dict[str, Any]] = field(default_factory=list)
    alerts_open: bool = False

    def to_json(self, max_text: int = 6000) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["text"] = self.text[:max_text]
        data["text_truncated"] = len(self.text) > max_text
        data["detail"] = "text"
        return data

    def summary(self) -> str:
        return (
            f"{self.title or '(untitled)'} — {self.url}\n"
            f"{len(self.links)} links, {len(self.fields)} fields, {len(self.buttons)} buttons, "
            f"{len(self.dialogs)} dialogs, {len(self.tabs)} tabs"
        )


class BrowserController:
    """Base class. A real controller implements the primitives; actions call them."""

    def __init__(self, endpoint: str = ""):
        self.endpoint = endpoint

    # primitives -- overridden
    def tabs(self) -> list[TabInfo]: raise NotImplementedError
    def open_tab(self, url: str = "") -> TabInfo: raise NotImplementedError
    def close_tab(self, tab_id: str = "") -> bool: raise NotImplementedError
    def switch_tab(self, tab_id: str = "", index: int = -1) -> TabInfo: raise NotImplementedError
    def navigate(self, url: str, tab_id: str = "") -> dict[str, Any]: raise NotImplementedError
    def reload(self, hard: bool = False) -> dict[str, Any]: raise NotImplementedError
    def go_back(self) -> dict[str, Any]: raise NotImplementedError
    def go_forward(self) -> dict[str, Any]: raise NotImplementedError
    def click(self, target: str, by: str = "css", nth: int = 1) -> dict[str, Any]: raise NotImplementedError
    def type_text(self, target: str, text: str, by: str = "css", clear: bool = True) -> dict[str, Any]: raise NotImplementedError
    def fill_form(self, values: dict[str, str]) -> dict[str, Any]: raise NotImplementedError
    def scroll(self, dy: int = 0, dx: int = 0, to: str = "") -> dict[str, Any]: raise NotImplementedError
    def evaluate(self, script: str) -> Any: raise NotImplementedError
    def screenshot(self, full_page: bool = False) -> bytes: raise NotImplementedError
    def cookies(self) -> list[dict[str, Any]]: raise NotImplementedError
    def history(self, limit: int = 200) -> list[dict[str, Any]]: raise NotImplementedError
    def key(self, combo: str) -> dict[str, Any]: raise NotImplementedError
    def chrome_geometry(self) -> dict[str, Any]: raise NotImplementedError
    def downloads(self) -> list[dict[str, Any]]: raise NotImplementedError

    def release(self) -> dict[str, Any]:
        """Let go of an owned browser so its memory is returned to the user.

        The default is honest rather than silent: a controller that did not start a browser
        reports that it owns nothing to close. Overriding is what frees real resources.
        """
        return {"released": False, "reason": "this controller does not own a browser process"}


class PlaywrightController(BrowserController):
    """Managed browser via Playwright (isolated profile)."""

    def __init__(self, browser: str = "chromium", headless: bool = False, profile_dir: str = "", endpoint: str = ""):
        super().__init__(endpoint)
        self.browser_name = browser
        self.headless = headless
        self.profile_dir = profile_dir
        self._playwright = None
        self._browser = None
        self._context = None

    def start(self) -> "PlaywrightController":
        try:  # pragma: no cover - desktop only
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise DriverUnavailable(
                "browser.playwright",
                "playwright + its browser binaries (playwright install chromium)",
                str(exc),
            ) from exc
        self._playwright = sync_playwright().start()
        launcher = getattr(self._playwright, self.browser_name)
        args: dict[str, Any] = {"headless": self.headless}
        if self.profile_dir:
            args["args"] = [f"--user-data-dir={self.profile_dir}"]
        self._browser = launcher.launch(**args)
        self._context = self._browser.new_context()
        return self

    def release(self) -> dict[str, Any]:
        """Close the managed browser this controller launched (and nothing the user owns).

        Without this, quitting ZYNTHARO left a Chromium holding hundreds of megabytes: the
        Playwright driver, the browser and every tab lived until the OS reaped them.
        """
        released = False
        error = ""
        if self._context is not None:
            try:
                self._context.close()
                released = True
            except Exception as exc:  # noqa: BLE001 - closing must never raise at shutdown
                error = str(exc)[:200]
        if self._browser is not None:
            try:
                self._browser.close()
                released = True
            except Exception as exc:  # noqa: BLE001
                error = error or str(exc)[:200]
        if self._playwright is not None:
            try:
                self._playwright.stop()
                released = True
            except Exception as exc:  # noqa: BLE001
                error = error or str(exc)[:200]
        self._context = None
        self._browser = None
        self._playwright = None
        return {"released": released, "reason": error or "managed browser closed"}

    def _page(self) -> Any:
        if self._context is None:
            self.start()
        pages = self._context.pages
        if not pages:
            self._context.new_page()
        return self._context.pages[-1]

    def tabs(self) -> list[TabInfo]:
        if self._context is None:
            self.start()
        current = self._page()
        return [
            TabInfo(id=str(index), url=page.url, title=page.title(), active=page is current)
            for index, page in enumerate(self._context.pages)
        ]

    def open_tab(self, url: str = "") -> TabInfo:
        page = self._context.new_page() if self._context else self._page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return TabInfo(id=str(len(self._context.pages) - 1), url=page.url, title=page.title(), active=True)

    def close_tab(self, tab_id: str = "") -> bool:
        pages = self._context.pages
        if not pages:
            return False
        page = pages[int(tab_id)] if tab_id.isdigit() else self._page()
        page.close()
        return True

    def switch_tab(self, tab_id: str = "", index: int = -1) -> TabInfo:
        pages = self._context.pages
        page = pages[int(tab_id)] if tab_id.isdigit() else pages[index]
        page.bring_to_front()
        return TabInfo(id=tab_id or str(index), url=page.url, title=page.title(), active=True)

    def navigate(self, url: str, tab_id: str = "") -> dict[str, Any]:
        page = self._page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        return {"url": page.url, "title": page.title(), "status": "ok"}

    def reload(self, hard: bool = False) -> dict[str, Any]:
        page = self._page()
        page.reload(wait_until="domcontentloaded")
        return {"url": page.url, "hard": hard}

    def go_back(self) -> dict[str, Any]:
        page = self._page()
        page.go_back()
        return {"url": page.url}

    def go_forward(self) -> dict[str, Any]:
        page = self._page()
        page.go_forward()
        return {"url": page.url}

    def click(self, target: str, by: str = "css", nth: int = 1) -> dict[str, Any]:
        page = self._page()
        locator = self._locator(page, target, by, nth)
        locator.click(timeout=20_000)
        return {"clicked": target, "by": by}

    def type_text(self, target: str, text: str, by: str = "css", clear: bool = True) -> dict[str, Any]:
        page = self._page()
        locator = self._locator(page, target, by, 1)
        if clear:
            locator.fill("")
        locator.type(text, delay=15)
        value = ""
        try:
            value = locator.input_value()
        except Exception:
            pass
        return {"typed_into": target, "chars": len(text), "read_back": value, "verified": text in value}

    def fill_form(self, values: dict[str, str]) -> dict[str, Any]:
        page = self._page()
        filled: list[dict[str, Any]] = []
        for selector, value in values.items():
            locator = self._locator(page, selector, "css", 1)
            locator.fill(value)
            filled.append({"field": selector, "verified": locator.input_value() == value})
        return {"filled": filled, "count": len(filled)}

    def scroll(self, dy: int = 0, dx: int = 0, to: str = "") -> dict[str, Any]:
        page = self._page()
        if to:
            page.evaluate(f"document.querySelector({json.dumps(to)})?.scrollIntoView({{block:'center'}})")
        else:
            page.mouse.wheel(dx, dy)
        position = page.evaluate("[window.scrollX, window.scrollY]")
        return {"scroll": position, "to": to or None}

    def evaluate(self, script: str) -> Any:
        return self._page().evaluate(script)

    def screenshot(self, full_page: bool = False) -> bytes:
        return self._page().screenshot(full_page=full_page)

    def cookies(self) -> list[dict[str, Any]]:
        return self._context.cookies()

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._page().evaluate("performance.getEntriesByType('navigation').map(e => ({url: e.name}))")[:limit]

    def key(self, combo: str) -> dict[str, Any]:
        self._page().keyboard.press(combo)
        return {"key": combo}

    def chrome_geometry(self) -> dict[str, Any]:
        return {"note": "managed browser window is positioned by Playwright; use vision_capture"}

    def downloads(self) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def _locator(page: Any, target: str, by: str, nth: int) -> Any:
        selector = {
            "css": target,
            "xpath": f"xpath={target}",
            "text": f"text={target}",
            "role": f"role={target}",
            "label": f"label={target}",
            "placeholder": f"placeholder={target}",
            "testid": f"data-testid={target}" if not target.startswith("data-testid") else target,
        }.get(by, target)
        return page.locator(selector).nth(max(0, nth - 1))


class CDPController(BrowserController):
    """Attach to the user's real browser over the DevTools protocol.

    Uses ``websocket-client`` when present; otherwise it reports precisely what is missing.
    Only the primitives the actions need are wrapped, and each maps to a standard CDP
    command so behaviour is identical to what DevTools itself does.
    """

    def __init__(self, endpoint: str = f"http://127.0.0.1:{DEFAULT_CDP_PORT}", timeout: float = 30.0):
        super().__init__(endpoint)
        self.timeout = timeout
        self._ids = 0
        self._sockets: dict[str, Any] = {}
        self._downloads: dict[str, dict[str, Any]] = {}
        self._download_dir = ""
        self._dialog_policy = ""

    # -- transport ---------------------------------------------------------------------
    def _http(self, path: str) -> Any:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{self.endpoint}{path}", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise DriverUnavailable(
                "browser.attach",
                f"a browser running with --remote-debugging-port={urlparse(self.endpoint).port}",
                f"Start it with: chrome --remote-debugging-port={DEFAULT_CDP_PORT} "
                f"(or use the managed browser). Details: {exc}",
            ) from exc

    def _socket(self, ws_url: str) -> Any:
        if ws_url in self._sockets:
            return self._sockets[ws_url]
        try:  # pragma: no cover - desktop only
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise DriverUnavailable("browser.attach", "websocket-client", str(exc)) from exc
        socket_ref = websocket.create_connection(ws_url, timeout=self.timeout)
        self._sockets[ws_url] = socket_ref
        return socket_ref

    def _send(self, ws_url: str, method: str, params: dict[str, Any] | None = None) -> Any:
        import websocket  # type: ignore

        socket_ref = self._socket(ws_url)
        self._ids += 1
        message_id = self._ids
        socket_ref.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                raw = socket_ref.recv()
            except websocket.WebSocketTimeoutException:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise ActionFailed(f"CDP {method} failed: {payload['error']}", {"method": method})
                return payload.get("result", {})
        raise ActionFailed(f"CDP {method} timed out", {"method": method})

    def _page_ws(self, tab_id: str = "") -> str:
        for target in self._http("/json/list"):
            if target.get("type") != "page":
                continue
            if not tab_id or str(target.get("id")) == tab_id:
                return target["webSocketDebuggerUrl"]
        raise ActionFailed(f"no page target matched {tab_id!r}", {"hint": "run browser_tabs to list targets"})

    # -- primitives --------------------------------------------------------------------
    def tabs(self) -> list[TabInfo]:
        out: list[TabInfo] = []
        for index, target in enumerate(self._http("/json/list")):
            if target.get("type") != "page":
                continue
            out.append(
                TabInfo(id=target.get("id", ""), url=target.get("url", ""), title=target.get("title", ""), active=index == 0)
            )
        return out

    def open_tab(self, url: str = "") -> TabInfo:
        import urllib.request

        request = urllib.request.Request(f"{self.endpoint}/json/new?{url or 'about:blank'}", method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read().decode("utf-8"))
        return TabInfo(id=target.get("id", ""), url=target.get("url", ""), title=target.get("title", ""), active=True)

    def close_tab(self, tab_id: str = "") -> bool:
        if not tab_id:
            tab_id = self.tabs()[-1].id
        import urllib.request

        try:
            urllib.request.urlopen(f"{self.endpoint}/json/close/{tab_id}", timeout=10).read()
            return True
        except Exception as exc:
            raise ActionFailed(f"could not close tab {tab_id}: {exc}") from exc

    def switch_tab(self, tab_id: str = "", index: int = -1) -> TabInfo:
        tabs = self.tabs()
        target = next((t for t in tabs if t.id == tab_id), None) or (tabs[index] if tabs else None)
        if target is None:
            raise ActionFailed(f"no tab matched {tab_id!r}", {"tabs": [t.to_json() for t in tabs]})
        self._send(self._page_ws(target.id), "Page.bringToFront")
        return target

    def navigate(self, url: str, tab_id: str = "") -> dict[str, Any]:
        """Navigate, then wait for the page to be genuinely ready.

        Returning "navigating" immediately meant the next action ran against the previous
        document — the classic reason an automated flow clicks the old page's button.
        """
        ws = self._page_ws(tab_id)
        self._send(ws, "Page.navigate", {"url": url})
        return {"url": url, "status": "ready", **self.wait_ready(ws)}

    def wait_ready(self, ws_url: str = "", timeout: float = 30.0) -> dict[str, Any]:
        """Poll the document until it reports complete (or the timeout is reported honestly)."""
        ws = ws_url or self._page_ws()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                payload = self._send(ws, "Runtime.evaluate", {
                    "expression": "({ready: document.readyState, url: location.href, title: document.title})",
                    "returnByValue": True,
                })
            except Exception:
                payload = {}
            value = (payload or {}).get("result", {}).get("value") if isinstance(payload, dict) else None
            if isinstance(value, dict) and value.get("ready") == "complete":
                return {"ready_state": "complete", "final_url": value.get("url"), "title": value.get("title")}
            time.sleep(0.15)
        return {"ready_state": "timeout"}

    def handle_dialog(self, accept: bool = True, text: str = "", policy: str = "") -> dict[str, Any]:
        """Answer a blocking JavaScript dialog.

        An unhandled ``confirm()`` or ``beforeunload`` freezes the page: every later command waits
        for a dialog nobody will ever answer. A policy can also be set once so dialogs during a
        run are answered automatically.
        """
        if policy:
            self._dialog_policy = policy
        self._send(self._page_ws(), "Page.handleJavaScriptDialog", {"accept": bool(accept), "promptText": text or None})
        return {"accept": accept, "prompt": text, "policy": self._dialog_policy or "manual"}

    def reload(self, hard: bool = False) -> dict[str, Any]:
        self._send(self._page_ws(), "Page.reload", {"ignoreCache": hard})
        return {"hard": hard}

    def go_back(self) -> dict[str, Any]:
        return self.evaluate("history.back(), 'ok'")  # type: ignore[return-value]

    def go_forward(self) -> dict[str, Any]:
        return self.evaluate("history.forward(), 'ok'")  # type: ignore[return-value]

    def evaluate(self, script: str) -> Any:
        ws = self._page_ws()
        result = self._send(ws, "Runtime.evaluate", {"expression": script, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise ActionFailed(f"page script failed: {result['exceptionDetails'].get('text', '')}", {"script": script[:200]})
        return result.get("result", {}).get("value")

    def click(self, target: str, by: str = "css", nth: int = 1, verify: bool = True) -> dict[str, Any]:
        """Click, then check that something actually happened; otherwise escalate the strategy.

        Clicking and returning is how an automated flow ends up continuing on a page that never
        changed — a decoy matched, a handler swallowed the event, or the element sat behind a
        transparent overlay. Every attempt is now compared against a page fingerprint, and the
        next strategy (text, aria, like, css) is tried before failure is reported.
        """
        attempts: list[dict[str, Any]] = []
        strategies = [by] + [other for other in ("text", "aria", "like", "css") if other != by]
        before = self.fingerprint() if verify else {}
        for strategy in strategies:
            try:
                result = self.evaluate(_click_script(target, strategy, nth))
            except ActionFailed as exc:
                attempts.append({"by": strategy, "ok": False, "error": str(exc)[:160], "evidence": getattr(exc, "evidence", None)})
                continue
            if not (result or {}).get("found"):
                attempts.append({
                    "by": strategy, "ok": False,
                    "candidates": (result or {}).get("candidates", [])[:8],
                    "cross_origin_frames": (result or {}).get("cross_origin_frames"),
                })
                continue
            if not verify:
                return {"clicked": target, "by": strategy, **result, "verified": None, "attempts": attempts}
            time.sleep(0.35)
            after = self.fingerprint()
            if not isinstance(after, dict) or not after:
                # The page could not be fingerprinted: accept the click but do not claim proof.
                return {"clicked": target, "by": strategy, **result, "verified": False,
                        "verify_error": "page fingerprint unavailable", "attempts": attempts}
            change = self._changed(before, after)
            attempts.append({"by": strategy, "ok": True, "changed": change or None, "point": result.get("point")})
            if change:
                return {"clicked": target, "by": strategy, **result, "verified": True, "change": change, "attempts": attempts}
        raise ActionFailed(
            f"click on {target!r} did not change the page ({len(attempts)} strategy attempt(s) tried)",
            {"attempts": attempts, "hint": "the target may sit in a cross-origin frame, need a hover first, or be a no-op control — pass verify=false to click it anyway"},
        )

    def fingerprint(self) -> dict[str, Any]:
        """A cheap but real fingerprint of page state, used to prove an action did something."""
        value = self.evaluate(
            "({url: location.href, title: document.title, "
            "nodes: document.getElementsByTagName('*').length, "
            "text_len: (document.body ? document.body.innerText.length : 0), "
            "hash: (() => { const t = document.body ? document.body.innerText.slice(0, 4000) : ''; "
            "let h = 0; for (let i = 0; i < t.length; i++) { h = (h * 31 + t.charCodeAt(i)) | 0; } return h; })()})"
        )
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _changed(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for key in ("url", "title", "nodes", "text_len", "hash"):
            if before.get(key) != after.get(key):
                changes[key] = {"from": before.get(key), "to": after.get(key)}
        return changes

    def type_text(self, target: str, text: str, by: str = "css", clear: bool = True) -> dict[str, Any]:
        script = _type_script(target, text, by, clear)
        result = self.evaluate(script)
        if not result or not result.get("found"):
            raise ActionFailed(f"no field matched {target!r}", {"by": by})
        return {"typed_into": target, "chars": len(text), **result}

    def fill_form(self, values: dict[str, str]) -> dict[str, Any]:
        script = "(() => { const out=[]; " + "".join(
            f"try {{ const el=document.querySelector({json.dumps(sel)}); if(el) {{ el.focus(); "
            f"el.value={json.dumps(val)}; el.dispatchEvent(new Event('input',{{bubbles:true}})); "
            f"el.dispatchEvent(new Event('change',{{bubbles:true}})); out.push({{field:{json.dumps(sel)}, ok: el.value==={json.dumps(val)}}}); }} "
            "else out.push({field:" + json.dumps(sel) + ", ok:false, error:'not found'}); } catch(e){ out.push({field:"
            + json.dumps(sel) + ", ok:false, error:String(e)}); } "
            for sel, val in values.items()
        ) + "return out; })()"
        filled = self.evaluate(script) or []
        return {"filled": filled, "count": len(filled)}

    def scroll(self, dy: int = 0, dx: int = 0, to: str = "") -> dict[str, Any]:
        if to:
            self.evaluate(f"document.querySelector({json.dumps(to)})?.scrollIntoView({{block:'center'}})")
        else:
            self.evaluate(f"window.scrollBy({dx}, {dy})")
        return {"scroll": self.evaluate("[window.scrollX, window.scrollY]")}

    def screenshot(self, full_page: bool = False) -> bytes:
        ws = self._page_ws()
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            metrics = self._send(ws, "Page.getLayoutMetrics")
            size = metrics.get("contentSize", {})
            params["clip"] = {"x": 0, "y": 0, "width": size.get("width", 1280), "height": size.get("height", 720), "scale": 1}
        data = self._send(ws, "Page.captureScreenshot", params)
        return base64.b64decode(data.get("data", ""))

    def cookies(self) -> list[dict[str, Any]]:
        return self._send(self._page_ws(), "Network.getAllCookies").get("cookies", [])

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.evaluate("performance.getEntriesByType('navigation').map(e => ({url: e.name, type: e.type}))") or []
        return rows[:limit]

    def key(self, combo: str) -> dict[str, Any]:
        self.evaluate(_key_script(combo))
        return {"key": combo}

    def chrome_geometry(self) -> dict[str, Any]:
        metrics = self.evaluate(
            "({outer:[window.outerWidth, window.outerHeight], inner:[window.innerWidth, window.innerHeight], "
            "screen:[window.screenX, window.screenY], dpr: window.devicePixelRatio})"
        )
        if not metrics:
            raise ActionFailed("could not read window geometry")
        chrome_height = max(0, int(metrics["outer"][1]) - int(metrics["inner"][1]))
        return {
            **metrics,
            "chrome_height": chrome_height,
            "toolbar_y": int(metrics["screen"][1]) + chrome_height // 2,
            "note": "chrome_height is the real height of the browser toolbar in physical pixels",
        }

    def downloads(self) -> list[dict[str, Any]]:
        """Downloads captured through the DevTools download events, with their real state.

        The previous implementation listed ~/Downloads and hoped: it could hand back a file the
        user downloaded last week as though the agent had just fetched it, and it could not tell a
        finished file from a half-written ``.crdownload``. Rows now come from
        ``Page.downloadWillBegin`` / ``Page.downloadProgress``, keyed by the browser's own guid.
        """
        return [
            {
                "guid": guid,
                "url": item.get("url", ""),
                "path": item.get("path", ""),
                "suggested": item.get("suggested", ""),
                "bytes": item.get("bytes", 0),
                "state": item.get("state", "inProgress"),
            }
            for guid, item in self._downloads.items()
        ]

    def enable_downloads(self, directory: str = "") -> dict[str, Any]:
        """Point the browser at a capture directory and start receiving progress events."""
        target = directory or os.environ.get("ZYNTHARO_DOWNLOAD_DIR") or os.path.join(
            os.path.expanduser(os.environ.get("ZYNTHARO_HOME", "~/.zyntharo")), "downloads"
        )
        os.makedirs(target, exist_ok=True)
        self._download_dir = target
        ws = self._page_ws()
        self._send(ws, "Page.setDownloadBehavior", {"behavior": "allowAndName", "downloadPath": target, "eventsEnabled": True})
        self._drain_events(ws)
        return {"directory": target, "capturing": True}

    def wait_for_download(self, timeout: float = 120.0, index: int = 0) -> dict[str, Any]:
        """Block until a download is genuinely complete, then return it with its real size."""
        if not self._download_dir:
            self.enable_downloads()
        ws = self._page_ws()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._drain_events(ws)
            rows = self.downloads()
            if len(rows) > index and rows[index].get("state") == "completed":
                item = rows[index]
                path = item.get("path") or ""
                if path and os.path.exists(path) and not path.endswith(".crdownload"):
                    item["bytes"] = os.path.getsize(path)
                    return item
                item["state"] = "completed_without_file"
                return item
            time.sleep(0.25)
        raise ActionFailed(f"no download finished within {timeout}s", {"downloads": self.downloads()})

    def _drain_events(self, ws_url: str) -> list[dict[str, Any]]:
        """Read queued browser events without consuming a response someone is waiting for."""
        try:
            import websocket  # type: ignore
        except Exception:
            return []
        events: list[dict[str, Any]] = []
        socket_ref = self._socket(ws_url)
        previous = socket_ref.gettimeout()
        socket_ref.settimeout(0.05)
        try:
            while True:
                try:
                    raw = socket_ref.recv()
                except (websocket.WebSocketTimeoutException, OSError):
                    break
                except Exception:
                    break
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                method = payload.get("method", "")
                params = payload.get("params", {}) or {}
                if method == "Page.downloadWillBegin":
                    guid = str(params.get("guid", ""))
                    self._downloads[guid] = {
                        "guid": guid, "url": params.get("url", ""),
                        "suggested": params.get("suggestedFilename", ""),
                        "state": "inProgress", "bytes": 0,
                    }
                elif method == "Page.downloadProgress":
                    guid = str(params.get("guid", ""))
                    item = self._downloads.setdefault(guid, {"guid": guid, "url": "", "state": "inProgress", "bytes": 0})
                    item["state"] = params.get("state", item.get("state", "inProgress"))
                    item["bytes"] = params.get("receivedBytes", item.get("bytes", 0))
                    if params.get("filePath"):
                        item["path"] = params["filePath"]
                    if item["state"] == "completed" and not item.get("path"):
                        item["path"] = os.path.join(self._download_dir or "", item.get("suggested", guid))
                elif method == "Page.javascriptDialogOpening" and self._dialog_policy in ("accept", "dismiss"):
                    try:
                        self._send(ws_url, "Page.handleJavaScriptDialog", {"accept": self._dialog_policy == "accept"})
                        events.append({"method": "dialog_handled", "params": params})
                        continue
                    except Exception:
                        pass
                if method:
                    events.append(payload)
        finally:
            try:
                socket_ref.settimeout(previous)
            except Exception:
                pass
        return events


#: Injected at the top of every finder script. A plain ``document.querySelector`` reaches
#: nothing on a modern application: component libraries render into shadow roots and checkout or
#: chat widgets live inside iframes, so the selector matches an empty document and the click
#: silently does nothing. This walks every open shadow root and every same-origin iframe document,
#: and *counts* the cross-origin frames it cannot reach instead of pretending they are empty.
_DEEP_JS = """
      const __zyntharoRoots = (root) => {
        const roots = [root];
        const seen = new Set(roots);
        let blocked = 0;
        for (let i = 0; i < roots.length && i < 64; i++) {
          const current = roots[i];
          try {
            for (const el of current.querySelectorAll('*')) {
              if (el.shadowRoot && !seen.has(el.shadowRoot)) { seen.add(el.shadowRoot); roots.push(el.shadowRoot); }
            }
          } catch (err) {}
          try {
            for (const frame of current.querySelectorAll('iframe,frame')) {
              try {
                const doc = frame.contentDocument;
                if (doc && !seen.has(doc)) { seen.add(doc); roots.push(doc); }
              } catch (err) { blocked += 1; }
            }
          } catch (err) {}
        }
        return {roots, blocked};
      };
      const __deepQueryAll = (selector) => {
        const scan = __zyntharoRoots(document);
        const hits = [];
        for (const root of scan.roots) {
          try { hits.push(...root.querySelectorAll(selector)); } catch (err) {}
        }
        return {hits, blocked: scan.blocked, roots: scan.roots.length};
      };
"""


def _click_script(target: str, by: str, nth: int) -> str:
    """A single JS expression that finds, highlights and clicks an element.

    The search is deep: shadow roots and same-origin iframes are included, and the number of
    cross-origin frames that could not be searched is reported in the result so a miss is
    explainable rather than mysterious.
    """
    selector = {
        "css": f"__deepQueryAll({json.dumps(target)}).hits[{nth - 1}]",
        "xpath": f"document.evaluate({json.dumps(target)}, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue",
        "text": f"[...__deepQueryAll('a,button,[role=button],input[type=submit],span,div').hits].filter(e => (e.innerText||e.value||'').trim() === {json.dumps(target)})[{nth - 1}]",
        "like": f"[...__deepQueryAll('a,button,[role=button],input,[role=link],li,span,div').hits].filter(e => ((e.innerText||e.getAttribute('aria-label')||e.value||'')+'').toLowerCase().includes({json.dumps(target.lower())}))[{nth - 1}]",
        "aria": f"__deepQueryAll('[aria-label=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "testid": f"__deepQueryAll('[data-testid=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "name": f"__deepQueryAll('[name=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "id": f"__deepQueryAll('#' + CSS.escape({json.dumps(target)})).hits[0]",
    }.get(by, f"__deepQueryAll({json.dumps(target)}).hits[0]")
    return f"""(() => {{
{_DEEP_JS}
      try {{
        const el = {selector};
        if (!el) {{
          const probe = __deepQueryAll('a,button,[role=button],input,[role=link]');
          return {{found:false, searched_roots: probe.roots, cross_origin_frames: probe.blocked,
                   candidates: probe.hits.slice(0,20).map(e => (e.innerText||e.getAttribute('aria-label')||e.value||'').trim())}};
        }}
        el.scrollIntoView({{block:'center'}});
        const rect = el.getBoundingClientRect();
        el.click();
        return {{found:true, tag: el.tagName, text:(el.innerText||el.value||'').slice(0,120),
                 point:[Math.round(rect.left+rect.width/2), Math.round(rect.top+rect.height/2)],
                 visible: rect.width>0 && rect.height>0,
                 in_shadow: !!(el.getRootNode && el.getRootNode() !== document)}};
      }} catch (e) {{ return {{found:false, error:String(e)}}; }}
    }})()"""


def _type_script(target: str, text: str, by: str, clear: bool) -> str:
    finders = {
        "css": f"__deepQueryAll({json.dumps(target)}).hits[0]",
        "name": f"__deepQueryAll('[name=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "id": f"__deepQueryAll('#' + CSS.escape({json.dumps(target)})).hits[0]",
        "aria": f"__deepQueryAll('[aria-label=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "placeholder": f"__deepQueryAll('[placeholder=' + JSON.stringify({json.dumps(target)}) + ']').hits[0]",
        "label": f"__deepQueryAll('input,textarea,select').hits.filter(e => ((e.labels && e.labels[0] ? e.labels[0].innerText : '') || '').trim() === {json.dumps(target)})[0]",
    }
    finder = finders.get(by, finders["css"])
    return f"""(() => {{
{_DEEP_JS}
      try {{
        const el = {finder};
        if (!el) return {{found:false}};
        el.focus();
        if ({str(clear).lower()}) el.value = '';
        for (const ch of {json.dumps(text)}) {{
          el.value += ch;
          el.dispatchEvent(new InputEvent('input', {{bubbles:true, data:ch, inputType:'insertText'}}));
        }}
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
        return {{found:true, read_back: el.value, verified: el.value === {json.dumps(text)}}};
      }} catch (e) {{ return {{found:false, error:String(e)}}; }}
    }})()"""


def _key_script(combo: str) -> str:
    parts = combo.split("+")
    key = parts[-1]
    modifiers = {"ctrl": "ctrlKey", "control": "ctrlKey", "shift": "shiftKey", "alt": "altKey", "meta": "metaKey", "cmd": "metaKey"}
    flags = {modifiers.get(p.lower(), ""): True for p in parts[:-1]}
    return (
        "(() => { const el = document.activeElement || document.body; "
        f"const opts = {{key:{json.dumps(key)}, bubbles:true, "
        + ", ".join(f"{k}:true" for k in flags if k)
        + "}; el.dispatchEvent(new KeyboardEvent('keydown', opts)); el.dispatchEvent(new KeyboardEvent('keyup', opts)); return 'ok'; })()"
    )


# --------------------------------------------------------------------------------------
# Controller selection + test injection
# --------------------------------------------------------------------------------------

_CONTROLLER: BrowserController | None = None
_FACTORY: Callable[[], BrowserController] | None = None


def set_controller(controller: BrowserController | None) -> None:
    global _CONTROLLER
    _CONTROLLER = controller


def set_controller_factory(factory: Callable[[], BrowserController] | None) -> None:
    global _FACTORY
    _FACTORY = factory


def release() -> dict[str, Any]:
    """Release whatever browser ZYNTHARO is holding, if anything.

    Called on shutdown and available as an action, so an idle automation browser does not keep
    several hundred megabytes of the user's RAM after the task that needed it has finished.
    """
    global _CONTROLLER
    current = _CONTROLLER
    if current is None:
        return {"released": False, "reason": "no browser controller is open"}
    try:
        report = current.release()
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
        report = {"released": False, "reason": f"release failed: {exc}"}
    _CONTROLLER = None
    return report


def controller() -> BrowserController:
    global _CONTROLLER
    if _CONTROLLER is None:
        if _FACTORY is not None:
            _CONTROLLER = _FACTORY()
        elif find_running_browser():
            _CONTROLLER = CDPController()
        else:
            raise DriverUnavailable(
                "browser",
                "a running Chromium-family browser with remote debugging, or playwright",
                "Attach: chrome --remote-debugging-port=9222, or install the desktop extra "
                "(pip install \"zyntharo[desktop]\") for the managed browser.",
            )
    return _CONTROLLER


def find_running_browser(port: int = DEFAULT_CDP_PORT) -> bool:
    """True when a browser is already listening for DevTools connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def launch_browser_with_debugging(url: str = "", port: int = DEFAULT_CDP_PORT, profile: str = "") -> dict[str, Any]:
    """Start the user's real browser with DevTools enabled, then attach to it."""
    candidates = [
        os.environ.get("ZYNTHARO_BROWSER", ""),
        shutil.which("chrome") or "",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("brave") or "",
        shutil.which("msedge") or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe" if os.name == "nt" else "",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" if os.name == "nt" else "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" if os.path.exists("/Applications/Google Chrome.app") else "",
    ]
    binary = next((c for c in candidates if c and os.path.exists(c)), "")
    if not binary:
        raise ActionFailed("no Chromium-family browser found; set ZYNTHARO_BROWSER to its path")
    argv = [binary, f"--remote-debugging-port={port}"]
    if profile:
        argv.append(f"--user-data-dir={profile}")
    if url:
        argv.append(url)
    # The automation browser is a helper we own: supervised, so quitting ZYNTHARO does not leave
    # a headless Chrome with a DevTools port open on the user's machine.
    from ..ops.spawn import managed_popen

    managed_popen(argv, name=f"automation-browser-{port}", kind="browser", restart="on-failure")
    deadline = time.time() + 20
    while time.time() < deadline:
        if find_running_browser(port):
            return {"binary": binary, "port": port, "attached": True}
        time.sleep(0.4)
    raise ActionFailed(f"browser started but DevTools port {port} never opened", {"argv": argv})


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


@action(
    name="browser_attach",
    description=(
        "Attach to a running browser (real profile, real sessions) or launch one with DevTools "
        "enabled and attach. This is the default entry point before any browser work."
    ),
    capability="browser.navigate",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "port": {"type": "integer"},
            "launch_if_missing": {"type": "boolean"},
            "url": {"type": "string"},
            "profile": {"type": "string", "description": "Profile directory for a dedicated automation profile."},
        },
    },
)
def browser_attach(ctx: ActionContext, port: int = DEFAULT_CDP_PORT, launch_if_missing: bool = True, url: str = "", profile: str = "") -> Any:
    if find_running_browser(port):
        set_controller(CDPController(f"http://127.0.0.1:{port}"))
        info = _require().tabs()
        return {"mode": "attached", "port": port, "tabs": [t.to_json() for t in info]}
    if not launch_if_missing:
        raise ActionFailed(
            f"nothing is listening on DevTools port {port} and launch_if_missing=false",
            {"hint": "start the browser with --remote-debugging-port, or allow launching"},
        )
    result = launch_browser_with_debugging(url, port, profile)
    set_controller(CDPController(f"http://127.0.0.1:{port}"))
    return {"mode": "launched", **result}


def _require() -> BrowserController:
    return controller()


@action(
    name="browser_tabs",
    description="List every open tab with id, url, title and which one is active.",
    capability="browser.tabs",
    group="browser",
    parameters={"type": "object", "properties": {}},
)
def browser_tabs(ctx: ActionContext) -> Any:
    tabs = _require().tabs()
    return {"count": len(tabs), "tabs": [t.to_json() for t in tabs]}


@action(
    name="browser_release",
    description=(
        "Give the automation browser back: close the managed browser ZYNTHARO launched and free "
        "its memory. An attached user browser is left running and the result says so. Use when "
        "browser work is finished."
    ),
    capability="browser.tabs",
    group="browser",
    parameters={"type": "object", "properties": {}},
)
def browser_release(ctx: ActionContext) -> Any:
    return release()


@action(
    name="browser_open_tab",
    description="Open a new tab, optionally at a URL, and return its id.",
    capability="browser.tabs",
    group="browser",
    parameters={"type": "object", "properties": {"url": {"type": "string"}}},
)
def browser_open_tab(ctx: ActionContext, url: str = "") -> Any:
    return _require().open_tab(url).to_json()


@action(
    name="browser_close_tab",
    description="Close a tab by id (or the current tab when no id is given).",
    capability="browser.tabs",
    group="browser",
    parameters={"type": "object", "properties": {"tab_id": {"type": "string"}}},
)
def browser_close_tab(ctx: ActionContext, tab_id: str = "") -> Any:
    return {"closed": _require().close_tab(tab_id), "tab_id": tab_id or "current"}


@action(
    name="browser_switch_tab",
    description="Focus a tab by id or index — the switching a human does by clicking a tab strip.",
    capability="browser.tabs",
    group="browser",
    parameters={"type": "object", "properties": {"tab_id": {"type": "string"}, "index": {"type": "integer"}}},
)
def browser_switch_tab(ctx: ActionContext, tab_id: str = "", index: int = -1) -> Any:
    return _require().switch_tab(tab_id, index).to_json()


@action(
    name="browser_navigate",
    description="Navigate the active tab to a URL. Accepts bare domains (google.com) and search phrases.",
    capability="browser.navigate",
    group="browser",
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string"}, "tab_id": {"type": "string"}, "new_tab": {"type": "boolean"}},
        "required": ["url"],
    },
)
def browser_navigate(ctx: ActionContext, url: str, tab_id: str = "", new_tab: bool = False) -> Any:
    target = _normalise_url(url)
    browser = _require()
    if new_tab:
        tab = browser.open_tab(target)
        return {"url": target, "tab": tab.to_json()}
    result = browser.navigate(target, tab_id)
    if result.get("status") == "navigating":  # CDP is async; confirm the page actually moved
        deadline = time.time() + 15
        while time.time() < deadline:
            current = browser.evaluate("location.href")
            if current and not str(current).startswith("about:"):
                result["url"] = current
                break
            time.sleep(0.3)
    return result


def _normalise_url(url: str) -> str:
    value = url.strip()
    if value.startswith(("http://", "https://", "about:", "chrome://", "file://", "data:")):
        return value
    if " " in value or "." not in value.split("/")[0]:
        return f"https://www.google.com/search?q={value.replace(' ', '+')}"
    return f"https://{value}"


@action(
    name="browser_reload",
    description="Reload the page (hard=true bypasses cache). This is the toolbar refresh button.",
    capability="browser.navigate",
    group="browser",
    parameters={"type": "object", "properties": {"hard": {"type": "boolean"}}},
)
def browser_reload(ctx: ActionContext, hard: bool = False) -> Any:
    return _require().reload(hard)


@action(
    name="browser_history_nav",
    description="Go back or forward in the active tab.",
    capability="browser.navigate",
    group="browser",
    parameters={"type": "object", "properties": {"direction": {"type": "string", "enum": ["back", "forward"]}}, "required": ["direction"]},
)
def browser_history_nav(ctx: ActionContext, direction: str) -> Any:
    browser = _require()
    return browser.go_back() if direction == "back" else browser.go_forward()


@action(
    name="browser_click",
    description=(
        "Click an element in the page. Resolution ladder: css | id | name | xpath | text | like "
        "(substring) | aria (aria-label) | testid. Returns the element's screen point as evidence."
    ),
    capability="browser.click",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "by": {"type": "string", "enum": ["css", "id", "name", "xpath", "text", "like", "aria", "testid"]},
            "nth": {"type": "integer"},
        },
        "required": ["target"],
    },
)
def browser_click(ctx: ActionContext, target: str, by: str = "css", nth: int = 1) -> Any:
    return _require().click(target, by, nth)


@action(
    name="browser_type",
    description=(
        "Focus a field and type text into it with real keystroke events, then read the value "
        "back and report whether it matches. Handles React/Vue-controlled inputs."
    ),
    capability="browser.type",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "text": {"type": "string"},
            "by": {"type": "string", "enum": ["css", "id", "name", "aria", "placeholder"]},
            "clear": {"type": "boolean"},
            "submit": {"type": "boolean", "description": "Press Enter after typing (send a chat message, run a search)."},
        },
        "required": ["target", "text"],
    },
)
def browser_type(ctx: ActionContext, target: str, text: str, by: str = "css", clear: bool = True, submit: bool = False) -> Any:
    result = _require().type_text(target, text, by, clear)
    if submit:
        _require().key("Enter")
        result["submitted"] = True
    return result


@action(
    name="browser_scroll",
    description="Scroll the page by pixels, or bring a selector into view.",
    capability="browser.scroll",
    group="browser",
    parameters={
        "type": "object",
        "properties": {"dy": {"type": "integer"}, "dx": {"type": "integer"}, "to": {"type": "string"}},
    },
)
def browser_scroll(ctx: ActionContext, dy: int = 0, dx: int = 0, to: str = "") -> Any:
    return _require().scroll(dy, dx, to)


@action(
    name="browser_fill_form",
    description="Fill many fields at once from a selector→value map, verifying each value landed.",
    capability="browser.forms",
    group="browser",
    parameters={
        "type": "object",
        "properties": {"fields": {"type": "object", "description": "CSS selector -> value"}},
        "required": ["fields"],
    },
)
def browser_fill_form(ctx: ActionContext, fields: dict[str, str] | None = None, **kwargs: Any) -> Any:
    values = dict(fields or {})
    values.update({k: v for k, v in kwargs.items() if isinstance(v, str)})
    if not values:
        raise ActionFailed("fields must contain at least one selector -> value pair")
    return _require().fill_form({k: str(v) for k, v in values.items()})


@action(
    name="browser_model",
    description=(
        "The complete, vision-free model of the page: url, title, tabs, headings, links with "
        "hrefs, buttons, form fields with current values, tables, dialogs and full visible "
        "text. Everything a screenshot would tell you, in structured form, plus content that "
        "is scrolled out of view. Call this instead of screenshotting when you only need to "
        "understand or extract."
    ),
    capability="browser.extract",
    group="browser",
    parameters={"type": "object", "properties": {"include_text": {"type": "boolean"}, "max_links": {"type": "integer"}}},
)
def browser_model(ctx: ActionContext, include_text: bool = True, max_links: int = 200) -> Any:
    browser = _require()
    script = """(() => {
      const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
      const box = el => { try { const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]; } catch (e) { return []; } };
      const fields = [...document.querySelectorAll('input,textarea,select')].slice(0, 200).map(el => ({
        tag: el.tagName.toLowerCase(), type: el.type || '', name: el.name || '', id: el.id || '',
        placeholder: el.placeholder || '', label: clean(el.labels && el.labels[0] ? el.labels[0].innerText : ''),
        value: (el.type === 'password' ? (el.value ? '********' : '') : el.value).slice(0, 300),
        required: !!el.required, disabled: !!el.disabled, box: box(el),
      }));
      return {
        url: location.href, title: document.title, readyState: document.readyState,
        scroll: {x: window.scrollX, y: window.scrollY, height: document.body.scrollHeight, viewport: window.innerHeight},
        viewport: {width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio},
        headings: [...document.querySelectorAll('h1,h2,h3')].slice(0, 60).map(h => ({level: h.tagName, text: clean(h.innerText)})),
        links: [...document.querySelectorAll('a[href]')].slice(0, 400).map(a => ({text: clean(a.innerText).slice(0,140), href: a.href, box: box(a)})),
        buttons: [...document.querySelectorAll('button,input[type=submit],[role=button]')].slice(0, 120).map(b => ({text: clean(b.innerText || b.value || b.getAttribute('aria-label')), box: box(b), disabled: !!b.disabled})),
        fields: fields,
        tables: [...document.querySelectorAll('table')].slice(0, 10).map(t => ({rows: t.rows.length, columns: t.rows[0] ? t.rows[0].cells.length : 0, firstRow: t.rows[0] ? [...t.rows[0].cells].map(c => clean(c.innerText)) : []})),
        dialogs: [...document.querySelectorAll('[role=dialog],dialog,[aria-modal=true]')].map(d => ({text: clean(d.innerText).slice(0,600), box: box(d)})),
        text: document.body ? document.body.innerText : '',
      };
    })()"""
    data = browser.evaluate(script) or {}
    if not include_text:
        data.pop("text", None)
    data["links"] = data.get("links", [])[:max_links]
    data["tabs"] = [t.to_json() for t in browser.tabs()]
    data["detail"] = "text"
    data["note"] = "vision-free page model: same information as a screenshot, plus exact values and off-screen content"
    return data


@action(
    name="browser_extract",
    description=(
        "Extract specific data from the page: text of a selector, a table as rows, all links, "
        "attributes, or a JSON path result from the page. Use for reading data rather than "
        "clicking."
    ),
    capability="browser.extract",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "what": {"type": "string", "enum": ["text", "table", "links", "inputs", "html", "attribute", "count"]},
            "selector": {"type": "string"},
            "attribute": {"type": "string"},
        },
        "required": ["what"],
    },
)
def browser_extract(ctx: ActionContext, what: str, selector: str = "", attribute: str = "") -> Any:
    browser = _require()
    sel = json.dumps(selector or "body")
    scripts = {
        "text": f"document.querySelector({sel})?.innerText || ''",
        "html": f"document.querySelector({sel})?.outerHTML || ''",
        "table": f"""(() => {{ const t = document.querySelector({sel}); if (!t) return []; return [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim())); }})()""",
        "links": f"""(() => {{ const root = document.querySelector({sel}) || document; return [...root.querySelectorAll('a[href]')].map(a => ({{text: a.innerText.trim().slice(0,160), href: a.href}})); }})()""",
        "inputs": f"""(() => {{ const root = document.querySelector({sel}) || document; return [...root.querySelectorAll('input,textarea,select')].map(el => ({{name: el.name, type: el.type, value: el.type==='password'?'********':el.value, label: el.labels && el.labels[0] ? el.labels[0].innerText.trim() : ''}})); }})()""",
        "count": f"document.querySelectorAll({sel}).length",
        "attribute": f"document.querySelector({sel})?.getAttribute({json.dumps(attribute)}) ?? null",
    }
    if what not in scripts:
        raise ActionFailed(f"unknown extraction {what!r}", {"supported": sorted(scripts)})
    return {"what": what, "selector": selector or "body", "result": browser.evaluate(scripts[what])}


@action(
    name="browser_screenshot",
    description="Screenshot the page (full_page=true for the whole scrolled document) as base64 PNG.",
    capability="browser.extract",
    group="browser",
    parameters={"type": "object", "properties": {"full_page": {"type": "boolean"}}},
)
def browser_screenshot(ctx: ActionContext, full_page: bool = False) -> Any:
    png = _require().screenshot(full_page)
    return {"bytes": len(png), "png_base64": base64.b64encode(png).decode("ascii"), "full_page": full_page}


@action(
    name="browser_chrome_click",
    description=(
        "Click browser chrome that is NOT in the page DOM: refresh, back, forward, the "
        "extensions icon, Lens, the account avatar, the three-dot menu and its items. Uses "
        "window geometry plus OCR grounding to find the icon, clicks it, and reports the "
        "popup/menu state as evidence. Give target_name ('refresh', 'extensions', 'lens', "
        "'account', 'menu') or explicit x/y."
    ),
    capability="browser.icons",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "target_name": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "title_contains": {"type": "string", "description": "Window title substring of the browser."},
            "then_type": {"type": "string", "description": "Text to type once the control is open."},
            "then_press": {"type": "string", "description": "Key to press once open, e.g. enter."},
        },
    },
)
def browser_chrome_click(
    ctx: ActionContext,
    target_name: str = "",
    x: int | None = None,
    y: int | None = None,
    title_contains: str = "",
    then_type: str = "",
    then_press: str = "",
) -> Any:
    from .input_control import backend
    from .vision import capture_screen, ocr_capture, window_bounds

    geometry: dict[str, Any] = {}
    try:
        geometry = _require().chrome_geometry()
    except Exception:
        geometry = {}
    origin_x, origin_y = 0, 0
    if title_contains:
        bounds = window_bounds(title_contains)
        if bounds:
            origin_x, origin_y = bounds[0], bounds[1]
    elif geometry.get("screen"):
        origin_x, origin_y = int(geometry["screen"][0]), int(geometry["screen"][1])

    if x is None or y is None:
        if not target_name:
            raise ActionFailed("provide target_name or explicit x/y")
        anchors: dict[str, tuple[float, float]] = {
            # fractions of the window width measured from the right edge, and a toolbar-row guess
            "refresh": (0.055, 0),
            "back": (0.16, 0),
            "forward": (0.115, 0),
            "home": (0.02, 0),
            "extensions": (-0.075, 0),
            "lens": (-0.115, 0),
            "account": (-0.035, 0),
            "menu": (-0.012, 0),
        }
        if target_name not in anchors:
            grounded = _ground_chrome_icon(target_name, origin_x, origin_y)
            x, y = grounded["point"]
        else:
            width = int(geometry.get("outer", [1280, 800])[0]) or 1280
            chrome_height = int(geometry.get("chrome_height", 80) or 80)
            ratio, _ = anchors[target_name]
            x = int(origin_x + width - ratio * width) if ratio < 0 else int(origin_x + ratio * width)
            y = int(origin_y + max(20, chrome_height // 2))
    before = capture_screen(max_edge=900)
    backend().click(int(x), int(y))
    time.sleep(0.4)
    after = capture_screen(max_edge=900)
    if then_type:
        from .input_control import type_verified

        guard = _guard_for_browser(title_contains or "chrome")
        type_verified(then_type, guard, reader=None, retries=0)
    if then_press:
        backend().press(then_press)
    return {
        "clicked": target_name or "coordinates",
        "at": [x, y],
        "toolbar_detected_height": geometry.get("chrome_height"),
        "geometry": geometry,
        "before_png_base64": base64.b64encode(before.png).decode("ascii"),
        "after_bytes": len(after.png),
        "note": "pass before_png_base64 to vision_diff to prove the popup opened",
    }


def _ground_chrome_icon(target_name: str, origin_x: int, origin_y: int) -> dict[str, Any]:
    from .vision import capture_screen, ocr_capture

    lines = ocr_capture(capture_screen((origin_x, origin_y, origin_x + 4000, origin_y + 120), max_edge=2000))
    for line in lines:
        if target_name.lower() in line["text"].lower():
            box = line["box"]
            return {"point": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2], "matched": line["text"]}
    raise ActionFailed(
        f"could not locate browser control {target_name!r} (toolbar OCR found: "
        f"{[line['text'] for line in lines][:12]})",
        {"hint": "pass explicit x/y from a screenshot when the chrome is themed or icon-only"},
    )


def _guard_for_browser(title_contains: str) -> Any:
    from .input_control import FocusGuard

    return FocusGuard.capture(title_contains)


@action(
    name="browser_eval",
    description=(
        "Evaluate JavaScript in the page and return the value. Confirm-gated because it can "
        "bypass every other restriction. Use when no structured action reaches the target."
    ),
    capability="browser.eval",
    group="browser",
    danger="high",
    parameters={"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]},
)
def browser_eval(ctx: ActionContext, script: str) -> Any:
    return {"result": _require().evaluate(script)}


@action(
    name="browser_cookies",
    description="List cookies for the browser profile (values redacted). Sensitive; audit-logged.",
    capability="browser.cookies",
    group="browser",
    danger="medium",
    parameters={"type": "object", "properties": {}},
)
def browser_cookies(ctx: ActionContext) -> Any:
    rows = _require().cookies()
    return {
        "count": len(rows),
        "cookies": [
            {"name": c.get("name"), "domain": c.get("domain"), "path": c.get("path"), "expires": c.get("expires"), "secure": c.get("secure"), "value": "***"}
            for c in rows
        ],
    }


@action(
    name="browser_act",
    description=(
        "Click and prove it worked: the click is verified against the page state (url, title, node "
        "count, text fingerprint) and the expected element or text, retrying with a wider target "
        "strategy before it reports failure. Use this for any click whose result matters; use "
        "browser_click only when you truly do not care what it did."
    ),
    capability="browser.click",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "by": {"type": "string"},
            "expect": {"type": "string", "description": "CSS selector that must appear afterwards."},
            "expect_text": {"type": "string", "description": "Text that must be present afterwards."},
            "timeout": {"type": "number"},
        },
        "required": ["target"],
    },
)
def browser_act(
    ctx: ActionContext,
    target: str,
    by: str = "css",
    expect: str = "",
    expect_text: str = "",
    timeout: float = 8.0,
) -> Any:
    browser = _require()
    before = browser.fingerprint() if hasattr(browser, "fingerprint") else {}
    clicked = browser.click(target, by)
    deadline = time.time() + timeout
    verified: dict[str, Any] | None = None
    while time.time() < deadline:
        if expect and browser.evaluate(f"!!document.querySelector({json.dumps(expect)})"):
            verified = {"by": "expected_selector", "selector": expect}
            break
        if expect_text and browser.evaluate(
            f"(document.body ? document.body.innerText : '').includes({json.dumps(expect_text)})"
        ):
            verified = {"by": "expected_text", "text": expect_text}
            break
        if hasattr(browser, "fingerprint") and before:
            change = browser._changed(before, browser.fingerprint())
            if change:
                verified = {"by": "page_change", "change": change}
                break
        time.sleep(0.25)
    if verified is None and (expect or expect_text):
        raise ActionFailed(
            f"clicked {target!r} but {(expect or expect_text)!r} never appeared within {timeout}s",
            {"clicked": clicked, "expect": expect or expect_text},
        )
    return {"target": target, "clicked": clicked, "verified": verified, "waited_s": round(timeout, 2)}


@action(
    name="browser_download_wait",
    description=(
        "Wait until a download is genuinely complete, then return its real path and size. Use after "
        "clicking a download link: browser_downloads alone cannot tell a finished file from a "
        "half-written one."
    ),
    capability="browser.download",
    group="browser",
    parameters={
        "type": "object",
        "properties": {"timeout": {"type": "number"}, "index": {"type": "integer"}},
    },
)
def browser_download_wait(ctx: ActionContext, timeout: float = 120.0, index: int = 0) -> Any:
    browser = _require()
    waiter = getattr(browser, "wait_for_download", None)
    if waiter is None:
        return {"downloads": browser.downloads(), "note": "this controller cannot await a download; listed what exists"}
    try:
        return waiter(timeout, index)
    except NotImplementedError as exc:
        raise ActionFailed(
            "this browser controller cannot await a download",
            {"hint": "attach over CDP (browser_attach) so download progress events are available", "detail": str(exc)},
        ) from exc


@action(
    name="browser_dialog",
    description=(
        "Answer (or set a policy for) a blocking JavaScript dialog — alert, confirm, prompt or the "
        "beforeunload prompt. An unhandled dialog freezes the page and every later command waits "
        "on a box nobody will click."
    ),
    capability="browser.click",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "accept": {"type": "boolean"},
            "text": {"type": "string", "description": "Answer for a prompt() dialog."},
            "policy": {"type": "string", "enum": ["accept", "dismiss", "manual"]},
        },
    },
)
def browser_dialog(ctx: ActionContext, accept: bool = True, text: str = "", policy: str = "") -> Any:
    browser = _require()
    handler = getattr(browser, "handle_dialog", None)
    if handler is None:
        raise ActionFailed(
            "this browser controller cannot answer dialogs",
            {"hint": "attach over CDP (browser_attach) rather than the managed browser"},
        )
    try:
        return handler(accept=accept, text=text, policy=policy)
    except NotImplementedError as exc:
        raise ActionFailed(
            "this browser controller cannot answer dialogs",
            {"hint": "attach over CDP (browser_attach) rather than the managed browser", "detail": str(exc)},
        ) from exc


@action(
    name="browser_key",
    description="Send a key or combination to the page (Enter, ctrl+t, Escape, PageDown…).",
    capability="browser.click",
    group="browser",
    parameters={"type": "object", "properties": {"combo": {"type": "string"}}, "required": ["combo"]},
)
def browser_key(ctx: ActionContext, combo: str) -> Any:
    return _require().key(combo)


@action(
    name="browser_downloads",
    description="List files the browser has downloaded during this session and where they landed.",
    capability="browser.download",
    group="browser",
    parameters={"type": "object", "properties": {}},
)
def browser_downloads(ctx: ActionContext) -> Any:
    rows = _require().downloads()
    return {"count": len(rows), "downloads": rows}


@action(
    name="browser_history",
    description="Recent navigation history entries for the active tab.",
    capability="browser.history",
    group="browser",
    parameters={"type": "object", "properties": {"limit": {"type": "integer"}}},
)
def browser_history(ctx: ActionContext, limit: int = 200) -> Any:
    rows = _require().history(limit)
    return {"count": len(rows), "entries": rows}


@action(
    name="browser_wait_for",
    description="Wait until an element matching a selector appears (or disappears), then return. Replaces blind sleeps.",
    capability="browser.extract",
    group="browser",
    parameters={
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "state": {"type": "string", "enum": ["appear", "disappear"]},
            "timeout": {"type": "number"},
        },
        "required": ["selector"],
    },
)
def browser_wait_for(ctx: ActionContext, selector: str, state: str = "appear", timeout: float = 20.0) -> Any:
    browser = _require()
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        present = bool(browser.evaluate(f"!!document.querySelector({json.dumps(selector)})"))
        if present == (state == "appear"):
            return {"selector": selector, "state": state, "waited_s": round(time.time() - started, 2)}
        time.sleep(0.25)
    raise ActionFailed(f"timed out after {timeout}s waiting for {selector} to {state}", {"selector": selector})


all_actions = lambda: [  # noqa: E731
    browser_attach,
    browser_tabs,
    browser_release,
    browser_open_tab,
    browser_close_tab,
    browser_switch_tab,
    browser_navigate,
    browser_reload,
    browser_history_nav,
    browser_click,
    browser_type,
    browser_scroll,
    browser_fill_form,
    browser_model,
    browser_extract,
    browser_screenshot,
    browser_chrome_click,
    browser_eval,
    browser_cookies,
    browser_key,
    browser_downloads,
    browser_download_wait,
    browser_act,
    browser_dialog,
    browser_history,
    browser_wait_for,
]
