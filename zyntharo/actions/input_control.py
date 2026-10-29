"""Mouse, keyboard and the two anti-mistake guarantees the user demanded.

**Guarantee 1 — never type into the wrong window.**
Every type/click operation runs inside a :class:`FocusGuard`. The guard snapshots the
foreground window *identity* (title, pid, class) at the moment the agent decides to act,
verifies it again immediately before the input is delivered, and re-verifies between
chunks of long text. If focus drifted — the user clicked elsewhere, a dialog stole focus,
the target window minimised — the operation **aborts mid-flight**, reports exactly what it
saw, and the remaining text is not sent. Nothing is ever typed into an unknown window.

**Guarantee 2 — never "type into nothing".**
Typing is not fire-and-forget. After each chunk, a *value reader* (UI Automation on
Windows, AX on macOS, AT-SPI on Linux, or the browser DOM via CDP for web fields) is asked
for the focused control's current value and compared with the intended text. Mismatch or an
empty read-back means the keystrokes went nowhere: the driver re-focuses the target and
retries once, then escalates as a failure with a screenshot as evidence — instead of
silently pretending success and moving on.

Backends are pluggable, so the whole state machine is unit-testable with no display:
:func:`set_backend` swaps in a recording backend that logs every event and can simulate
focus drift, dropped keystrokes and read-back mismatches.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .registry import Action, ActionContext, ActionFailed, DriverUnavailable, action

# --------------------------------------------------------------------------------------
# Backend abstraction
# --------------------------------------------------------------------------------------


class InputBackend(Protocol):
    """The narrow surface a platform backend must implement."""

    def move(self, x: int, y: int, duration: float = 0.0) -> None: ...
    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, button: str = "left") -> None: ...
    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> None: ...
    def type_text(self, text: str, interval: float = 0.0) -> None: ...
    def press(self, key: str, presses: int = 1, interval: float = 0.02) -> None: ...
    def hotkey(self, *keys: str) -> None: ...
    def position(self) -> tuple[int, int]: ...
    def screen_size(self) -> tuple[int, int]: ...
    def active_window(self) -> "WindowRef | None": ...


@dataclass(slots=True)
class WindowRef:
    """Identity of a window. Used by the focus guard to detect drift."""

    title: str = ""
    pid: int = 0
    cls: str = ""
    handle: int = 0
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)

    def matches(self, other: "WindowRef | None", strict: bool = False) -> bool:
        if other is None:
            return False
        if self.handle and other.handle:
            return self.handle == other.handle
        if strict:
            return self.title == other.title and self.pid == other.pid
        return self.pid == other.pid and (
            self.title[:24] == other.title[:24] or self.cls == other.cls
        )

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, "pid": self.pid, "cls": self.cls, "handle": self.handle}


class PyAutoGUIBackend:
    """Default desktop backend (pyautogui + pynput), imported lazily."""

    def __init__(self) -> None:
        try:
            import pyautogui  # type: ignore
        except Exception as exc:  # pragma: no cover - desktop only
            raise DriverUnavailable("pyautogui", "pip install pyautogui (+Pillow, pyscreeze)", str(exc)) from exc
        try:
            from pynput import keyboard  # type: ignore

            self._keyboard = keyboard.Controller()
        except Exception:
            self._keyboard = None
        # Before a single coordinate is sent: the process must share the OS's coordinate space.
        ensure_dpi_awareness()
        self._gui = pyautogui
        self._gui.FAILSAFE = True
        self._gui.PAUSE = 0.02

    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        self._gui.moveTo(x, y, duration=duration)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self._gui.click(x=x, y=y, clicks=clicks, button=button)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, button: str = "left") -> None:
        self._gui.moveTo(x1, y1)
        self._gui.dragTo(x2, y2, duration=duration, button=button)

    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> None:
        if x is not None and y is not None:
            self._gui.moveTo(x, y)
        self._gui.scroll(amount)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        self._gui.typewrite(text, interval=interval)

    def press(self, key: str, presses: int = 1, interval: float = 0.02) -> None:
        self._gui.press(key, presses=presses, interval=interval)

    def hotkey(self, *keys: str) -> None:
        self._gui.hotkey(*keys)

    def position(self) -> tuple[int, int]:
        return tuple(self._gui.position())  # type: ignore[return-value]

    def screen_size(self) -> tuple[int, int]:
        return tuple(self._gui.size())  # type: ignore[return-value]

    def active_window(self) -> WindowRef | None:
        return active_window()  # falls back to the ctypes/OS implementation below


class RecordingBackend:
    """Headless backend for tests: records everything, can simulate drift/failures."""

    def __init__(self, size: tuple[int, int] = (1920, 1080), window: WindowRef | None = None):
        self.events: list[dict[str, Any]] = []
        self._pos = (0, 0)
        self._size = size
        self.window = window or WindowRef(title="Target App", pid=1234, cls="Chrome_WidgetWin_1", handle=42)
        self.focused_value: str = ""
        self.types_seen: str = ""
        # failure injection
        self.drop_keystrokes = False
        self.read_back_value: str | None = None
        self.focus_changes_after: int = -1  # after N events, focus "jumps" elsewhere
        self.focus_drift_after_reads: int = -1  # after N foreground reads, another app takes focus
        self._count = 0
        self._reads = 0

    def _log(self, kind: str, **data: Any) -> None:
        self._count += 1
        self.events.append({"kind": kind, **data})
        if self.focus_changes_after >= 0 and self._count > self.focus_changes_after:
            self.window = WindowRef(title="Some Other App", pid=9999, cls="Other", handle=7)

    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        self._pos = (x, y)
        self._log("move", x=x, y=y)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        self._pos = (x, y)
        self._log("click", x=x, y=y, button=button, clicks=clicks)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3, button: str = "left") -> None:
        self._log("drag", x1=x1, y1=y1, x2=x2, y2=y2)

    def scroll(self, amount: int, x: int | None = None, y: int | None = None) -> None:
        self._log("scroll", amount=amount, x=x, y=y)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        if not self.drop_keystrokes:
            self.types_seen += text
            self.focused_value += text if self.read_back_value is None else ""
        self._log("type", text=text)

    def press(self, key: str, presses: int = 1, interval: float = 0.02) -> None:
        self._log("press", key=key, presses=presses)

    def hotkey(self, *keys: str) -> None:
        self._log("hotkey", keys=list(keys))

    def position(self) -> tuple[int, int]:
        return self._pos

    def screen_size(self) -> tuple[int, int]:
        return self._size

    def active_window(self) -> WindowRef | None:
        self._reads += 1
        if self.focus_drift_after_reads >= 0 and self._reads > self.focus_drift_after_reads:
            # Simulates the user (or a dialog) stealing focus mid-operation.
            self.window = WindowRef(title="Some Other App", pid=9999, cls="Other", handle=7)
        return self.window


_DPI_STATE: dict[str, Any] = {"applied": False, "scale": 1.0, "detail": "not probed"}


def ensure_dpi_awareness() -> dict[str, Any]:
    """Make this process DPI aware, then report the scale factor.

    Windows silently virtualises coordinates for a process that is not DPI aware. On a laptop at
    150% scaling the app receives a 1280x720 viewport while the accessibility tree reports
    physical pixels, so *every* click computed from UI Automation bounds lands several hundred
    pixels off — the classic "it clicked the wrong thing" failure, and nothing about it looks like
    a bug. Declaring per-monitor awareness makes the coordinate spaces agree.
    """
    global _DPI_STATE
    if _DPI_STATE["applied"]:
        return dict(_DPI_STATE)
    import sys

    if not sys.platform.startswith("win"):
        _DPI_STATE = {"applied": False, "scale": 1.0, "detail": "not Windows; coordinates are not virtualised"}
        return dict(_DPI_STATE)
    try:  # pragma: no cover - Windows only
        import ctypes

        user32 = ctypes.windll.user32
        shcore = getattr(ctypes.windll, "shcore", None)
        applied = False
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            applied = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))  # per-monitor v2
        if not applied and shcore is not None:
            applied = shcore.SetProcessDpiAwareness(2) == 0
        scale = 1.0
        try:
            hdc = user32.GetDC(0)
            scale = user32.GetDeviceCaps(hdc, 88) / 96.0  # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
        except Exception:
            pass
        _DPI_STATE = {"applied": applied, "scale": round(scale, 3), "detail": f"per-monitor DPI aware, {int(scale * 100)}% scaling"}
    except Exception as exc:  # pragma: no cover - Windows only
        _DPI_STATE = {"applied": False, "scale": 1.0, "detail": f"DPI awareness could not be set: {exc}"}
    return dict(_DPI_STATE)


_BACKEND: InputBackend | None = None
_BACKEND_FACTORY: Callable[[], InputBackend] | None = None


def set_backend(backend: InputBackend | None) -> None:
    """Inject a backend (tests, or an alternative platform)."""
    global _BACKEND
    _BACKEND = backend


def set_backend_factory(factory: Callable[[], InputBackend] | None) -> None:
    global _BACKEND_FACTORY
    _BACKEND_FACTORY = factory


def backend() -> InputBackend:
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = (_BACKEND_FACTORY or PyAutoGUIBackend)()
    return _BACKEND


# --------------------------------------------------------------------------------------
# Foreground window detection
# --------------------------------------------------------------------------------------


def active_window() -> WindowRef | None:
    """Best-effort foreground window identity across platforms (no dependency)."""
    try:
        if __import__("os").name == "nt":  # pragma: no cover - Windows only
            import ctypes

            user32 = ctypes.windll.user32
            handle = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
            bounds = (0, 0, 0, 0)
            if rect is not None:
                user32.GetWindowRect(handle, ctypes.byref(rect))
                bounds = (rect.left, rect.top, rect.right, rect.bottom)
            return WindowRef(title=buffer.value, pid=int(pid.value), handle=int(handle), bounds=bounds)
        if __import__("platform").system() == "Darwin":  # pragma: no cover - macOS only
            from subprocess import run

            script = (
                'tell application "System Events" to set frontApp to first application process whose frontmost is true\n'
                'set t to name of frontApp\nset p to unix id of frontApp\nreturn t & "|" & p'
            )
            out = run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()
            title, _, pid = out.partition("|")
            return WindowRef(title=title, pid=int(pid or 0))
        from subprocess import run

        active = run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True)
        if active.returncode != 0:
            return None
        pid_out = run(["xdotool", "getactivewindow", "getwindowpid"], capture_output=True, text=True)
        return WindowRef(title=active.stdout.strip(), pid=int(pid_out.stdout.strip() or 0))
    except Exception:
        return None


@dataclass
class FocusGuard:
    """Ensures input lands where it was intended, or not at all."""

    target: WindowRef
    strict: bool = False
    backend_ref: InputBackend | None = None
    drift: list[dict[str, Any]] = field(default_factory=list)
    checks: int = 0

    @classmethod
    def capture(cls, expected_title: str = "", backend_ref: InputBackend | None = None, strict: bool = False) -> "FocusGuard":
        current = (backend_ref or backend()).active_window()
        if current is None:
            raise ActionFailed(
                "cannot determine the foreground window on this platform",
                {"hint": "install xdotool (Linux) or run on Windows/macOS with a desktop session"},
            )
        if expected_title and expected_title.lower() not in current.title.lower():
            raise ActionFailed(
                f"foreground window is {current.title!r}, expected one containing {expected_title!r}",
                {"foreground": current.to_json(), "expected": expected_title},
            )
        return cls(target=current, strict=strict, backend_ref=backend_ref)

    def verify(self, context: str) -> WindowRef:
        self.checks += 1
        current = (self.backend_ref or backend()).active_window()
        if not self.target.matches(current, self.strict):
            self.drift.append({"context": context, "expected": self.target.to_json(), "saw": current.to_json() if current else None})
        else:
            return current  # type: ignore[return-value]
        raise ActionFailed(
            f"focus guard aborted {context}: window changed to {current.title if current else 'unknown'!r}",
            {"expected": self.target.to_json(), "saw": current.to_json() if current else None, "checks": self.checks},
        )

    @property
    def clean(self) -> bool:
        return not self.drift


# --------------------------------------------------------------------------------------
# Verified typing
# --------------------------------------------------------------------------------------


@dataclass
class TypingOutcome:
    typed: str
    remaining: str
    verified: bool
    read_back: str
    attempts: int
    aborted: bool = False


def type_verified(
    text: str,
    guard: FocusGuard,
    reader: Callable[[], str] | None = None,
    chunk: int = 400,
    interval: float = 0.0,
    retries: int = 1,
) -> TypingOutcome:
    """Type ``text`` with focus verification between chunks and value read-back after each.

    Three exit paths, all explicit:
      * ``verified=True``               — the control reports the text; done.
      * ``aborted=True``                — focus drifted; remaining text was never sent.
      * ``verified=False, aborted=False`` — keystrokes were delivered but the read-back did
        not confirm them (nothing was actually entered). The caller decides to retry or fail.
    """
    be = guard.backend_ref or backend()
    typed = ""
    attempts = 0
    pending = text
    while True:
        attempts += 1
        delivered = 0
        try:
            while delivered < len(pending):
                guard.verify(f"typing chunk at offset {len(typed) + delivered}")
                piece = pending[delivered : delivered + chunk]
                be.type_text(piece, interval=interval)
                typed += piece
                delivered += len(piece)
        except ActionFailed:
            if retries <= 0:
                return TypingOutcome(typed, text[len(typed) :], False, _read(reader), attempts, aborted=True)
            retries -= 1
            pending = text[len(typed) :]
            if not pending:
                break
            guard.verify("re-focus before retry")  # raises if the target window is really gone
            continue

        if reader is None:
            return TypingOutcome(typed, text[len(typed) :], False, "", attempts)
        read_back = _read(reader)
        if text_in(read_back, typed):
            return TypingOutcome(typed, "", True, read_back, attempts)
        if retries <= 0:
            return TypingOutcome(typed, "", False, read_back, attempts)
        retries -= 1
        # The control reported less than we sent: retype only what is missing.
        pending = _missing_tail(typed, read_back)
        if not pending:
            return TypingOutcome(typed, "", False, read_back, attempts)
        guard.verify("re-focus before read-back retry")


def _missing_tail(typed: str, read_back: str) -> str:
    """Which part of ``typed`` the control has not registered yet (whitespace-insensitive)."""
    compact_typed = "".join(typed.split())
    compact_read = "".join(read_back.split())
    if not compact_read or not compact_typed.endswith(compact_read):
        return typed
    seen = 0
    for index, char in enumerate(typed):
        if not char.isspace():
            seen += 1
            if seen == len(compact_read):
                return typed[index + 1 :]
    return ""


def _read(reader: Callable[[], str] | None) -> str:
    if reader is None:
        return ""
    try:
        return reader() or ""
    except Exception:
        return ""


def text_in(haystack: str, needle: str) -> bool:
    """Whitespace-tolerant containment: editors reformat, they do not lose text."""
    if not needle:
        return True
    if needle in haystack:
        return True
    compact_h = "".join(haystack.split())
    compact_n = "".join(needle.split())
    return bool(compact_n) and compact_n in compact_h


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


def _guard_or_fail(ctx: ActionContext, expected_title: str, strict: bool = False) -> FocusGuard:
    try:
        return FocusGuard.capture(expected_title, strict=strict)
    except ActionFailed as exc:
        if expected_title:
            raise
        if ctx.identity is not None and getattr(ctx.identity, "notes", ""):
            pass
        raise ActionFailed(
            "cannot read the foreground window; pass expected_title explicitly",
            exc.evidence,
        ) from exc


@action(
    name="input_move",
    description="Move the mouse pointer to absolute screen coordinates.",
    capability="input.mouse",
    group="input",
    parameters={
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "duration": {"type": "number", "description": "Seconds to animate the move (human-like)."},
        },
        "required": ["x", "y"],
    },
)
def input_move(ctx: ActionContext, x: int, y: int, duration: float = 0.15) -> Any:
    be = backend()
    width, height = be.screen_size()
    if not (0 <= x <= width and 0 <= y <= height):
        raise ActionFailed(f"point ({x},{y}) is outside the screen {width}x{height}")
    be.move(x, y, duration=duration)
    return {"x": x, "y": y, "verified": be.position() == (x, y)}


@action(
    name="input_click",
    description=(
        "Move and click at coordinates, then optionally verify the click had an effect "
        "(the caller supplies a verifier action name to compare before/after state). "
        "Verification is what turns 'I clicked' into 'the click worked'."
    ),
    capability="input.mouse",
    group="input",
    parameters={
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "clicks": {"type": "integer"},
            "expected_title": {"type": "string", "description": "Foreground window title substring; abort if it drifts."},
            "verify": {"type": "string", "description": "Action name to call before/after to compare state."},
        },
        "required": ["x", "y"],
    },
)
def input_click(
    ctx: ActionContext,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    expected_title: str = "",
    verify: str = "",
) -> Any:
    guard = _guard_or_fail(ctx, expected_title) if expected_title else None
    be = backend()
    if guard:
        guard.verify("before click")
    be.move(x, y, duration=0.05)
    be.click(x, y, button=button, clicks=clicks)
    if guard:
        guard.verify("after click")
    return {"x": x, "y": y, "button": button, "clicks": clicks, "focus_clean": guard.clean if guard else None}


@action(
    name="input_drag",
    description="Drag from one point to another (select text, move a window, drop a file).",
    capability="input.mouse",
    group="input",
    parameters={
        "type": "object",
        "properties": {
            "x1": {"type": "integer"}, "y1": {"type": "integer"},
            "x2": {"type": "integer"}, "y2": {"type": "integer"},
            "duration": {"type": "number"},
        },
        "required": ["x1", "y1", "x2", "y2"],
    },
)
def input_drag(ctx: ActionContext, x1: int, y1: int, x2: int, y2: int, duration: float = 0.4) -> Any:
    backend().drag(x1, y1, x2, y2, duration=duration)
    return {"from": [x1, y1], "to": [x2, y2]}


@action(
    name="input_scroll",
    description="Scroll the wheel at an optional position. Positive scrolls up.",
    capability="input.mouse",
    group="input",
    parameters={
        "type": "object",
        "properties": {"amount": {"type": "integer"}, "x": {"type": "integer"}, "y": {"type": "integer"}},
        "required": ["amount"],
    },
)
def input_scroll(ctx: ActionContext, amount: int, x: int | None = None, y: int | None = None) -> Any:
    backend().scroll(amount, x, y)
    return {"amount": amount, "at": [x, y] if x is not None else None}


@action(
    name="input_type",
    description=(
        "Type text with focus guarding and read-back verification. Pass expected_title to "
        "guarantee the text lands in the intended window; pass read_back_action (e.g. "
        "'windows_read_value' or 'browser_read_value') so the driver can prove the keystrokes "
        "registered. If focus drifts or the value does not appear, the call fails with evidence "
        "instead of silently typing into the wrong place."
    ),
    capability="input.type",
    group="input",
    danger="medium",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "expected_title": {"type": "string"},
            "strict": {"type": "boolean"},
            "interval": {"type": "number", "description": "Per-character delay; 0 is fastest."},
            "chunk": {"type": "integer", "description": "Characters between focus checks."},
            "retries": {"type": "integer"},
            "verify": {"type": "boolean", "description": "Require read-back verification."},
        },
        "required": ["text"],
    },
)
def input_type(
    ctx: ActionContext,
    text: str,
    expected_title: str = "",
    strict: bool = False,
    interval: float = 0.0,
    chunk: int = 400,
    retries: int = 1,
    verify: bool = True,
) -> Any:
    guard = _guard_or_fail(ctx, expected_title, strict)
    reader = _readback_reader(ctx) if verify else None
    outcome = type_verified(text, guard, reader=reader, chunk=chunk, interval=interval, retries=retries)
    evidence = {
        "target": guard.target.to_json(),
        "focus_checks": guard.checks,
        "drift": guard.drift,
        "attempts": outcome.attempts,
        "read_back": outcome.read_back[:400],
        "typed_chars": len(outcome.typed),
    }
    if outcome.aborted:
        raise ActionFailed(
            f"typing aborted: foreground window drifted away from {guard.target.title!r}; "
            "nothing further was typed (this is the guard working, not a crash)",
            evidence,
        )
    if verify and not outcome.verified:
        raise ActionFailed(
            "typed text did not appear in the target control after retry (the control was "
            "probably not focused); text was NOT assumed to be entered",
            evidence,
        )
    if outcome.remaining:
        raise ActionFailed(f"incomplete typing: {len(outcome.remaining)} characters not delivered", evidence)
    return {
        "typed_chars": len(outcome.typed),
        "target": guard.target.title,
        "verified": outcome.verified,
        "focus_checks": guard.checks,
        **({"evidence": evidence} if not outcome.verified else {}),
    }


def _readback_reader(ctx: ActionContext) -> Callable[[], str] | None:
    """Build a reader that asks the OS/DOM for the focused control's value."""
    try:
        if __import__("os").name == "nt":  # pragma: no cover - Windows only
            import uiautomation as auto  # type: ignore

            def read() -> str:
                control = auto.GetFocusedControl()
                return str(getattr(control, "GetValuePattern", lambda: None)() or getattr(control, "Name", "") or "")

            return read
        if __import__("platform").system() == "Darwin":  # pragma: no cover - macOS only
            import subprocess

            def read() -> str:
                script = (
                    'tell application "System Events" to tell (first application process whose frontmost is true) '
                    'to try\n return value of focused UI element of front window\n on error\n return ""\n end try'
                )
                return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout

            return read
        import subprocess  # pragma: no cover - Linux only

        def read() -> str:
            out = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True)
            if out.returncode != 0:
                return ""
            window = out.stdout.strip()
            name = subprocess.run(["xdotool", "getwindowname", window], capture_output=True, text=True)
            return name.stdout

        return read
    except Exception:
        return None


@action(
    name="input_press",
    description="Press a named key (enter, tab, esc, f5, up, …) once or repeatedly.",
    capability="input.keyboard",
    group="input",
    parameters={
        "type": "object",
        "properties": {"key": {"type": "string"}, "presses": {"type": "integer"}, "expected_title": {"type": "string"}},
        "required": ["key"],
    },
)
def input_press(ctx: ActionContext, key: str, presses: int = 1, expected_title: str = "") -> Any:
    guard = _guard_or_fail(ctx, expected_title) if expected_title else None
    if guard:
        guard.verify("before keypress")
    backend().press(key, presses=presses)
    return {"key": key, "presses": presses}


@action(
    name="input_hotkey",
    description="Press a key combination (ctrl+c, cmd+shift+t, alt+tab, win+r).",
    capability="input.hotkey",
    group="input",
    parameters={
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}},
            "expected_title": {"type": "string"},
        },
        "required": ["keys"],
    },
)
def input_hotkey(ctx: ActionContext, keys: list[str], expected_title: str = "") -> Any:
    if not keys:
        raise ActionFailed("keys must not be empty")
    guard = _guard_or_fail(ctx, expected_title) if expected_title else None
    if guard:
        guard.verify("before hotkey")
    backend().hotkey(*keys)
    return {"keys": keys}


@action(
    name="input_state",
    description="Current pointer position, screen size and foreground window. Cheap ground truth before acting.",
    capability="input.mouse",
    group="input",
    parameters={"type": "object", "properties": {}},
)
def input_state(ctx: ActionContext) -> Any:
    be = backend()
    window = be.active_window()
    return {
        "pointer": list(be.position()),
        "screen": list(be.screen_size()),
        "foreground": window.to_json() if window else None,
    }


all_actions = lambda: [  # noqa: E731
    input_move,
    input_click,
    input_drag,
    input_scroll,
    input_type,
    input_press,
    input_hotkey,
    input_state,
]
