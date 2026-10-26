"""Window and application control: enumerate, launch, focus, inspect the UI tree.

Applications are driven the way a human drives them, but with better eyes: the UI tree
(Win32 UI Automation / macOS AX / Linux AT-SPI) exposes every control with its role, name,
value, bounds and state. That tree is what lets an agent *set a field's value directly*
when that is available, and only fall back to mouse+keyboard when it is not.

Nothing here assumes Windows: each function tries the platform API, then degrades to a
portable command-line tool, and finally raises :class:`ActionFailed` with a precise hint.
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .registry import Action, ActionContext, ActionFailed, action

IS_WINDOWS = os.name == "nt"
IS_MAC = platform.system() == "Darwin"


# --------------------------------------------------------------------------------------
# Window enumeration
# --------------------------------------------------------------------------------------


def list_windows() -> list[dict[str, Any]]:
    """Every top-level window: handle, title, pid, bounds, minimised/foreground flags."""
    try:
        if IS_WINDOWS:  # pragma: no cover - Windows only
            return _windows_win32()
        if IS_MAC:  # pragma: no cover - macOS only
            return _windows_macos()
        return _windows_x11()
    except Exception:
        return []


def _windows_win32() -> list[dict[str, Any]]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out: list[dict[str, Any]] = []
    foreground = user32.GetForegroundWindow()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(handle, _param):
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        rect = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(rect))
        out.append(
            {
                "handle": int(handle),
                "title": buffer.value,
                "pid": int(pid.value),
                "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                "foreground": int(handle) == int(foreground),
                "minimised": bool(user32.IsIconic(handle)),
            }
        )
        return True

    user32.EnumWindows(callback, 0)
    return out


def _windows_macos() -> list[dict[str, Any]]:  # pragma: no cover - macOS only
    script = (
        'set out to ""\n'
        'tell application "System Events"\n'
        '  repeat with p in (every application process whose visible is true)\n'
        '    repeat with w in (every window of p)\n'
        '      set out to out & (name of p) & tab & (name of w) & tab & (position of w as string) & tab & (size of w as string) & linefeed\n'
        "    end repeat\n"
        "  end repeat\n"
        "end tell\n"
        "return out"
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        try:
            x, y = [int(v.strip()) for v in parts[2].split(",")]
            width, height = [int(v.strip()) for v in parts[3].split(",")]
        except ValueError:
            continue
        rows.append(
            {
                "handle": f"{parts[0]}|{parts[1]}",
                "app": parts[0],
                "title": parts[1],
                "pid": 0,
                "bounds": [x, y, x + width, y + height],
                "foreground": False,
            }
        )
    return rows


def _windows_x11() -> list[dict[str, Any]]:
    """Linux: xdotool/wmctrl when present. Wayland compositors restrict this by design."""
    if not _has("wmctrl"):
        active = active_title()
        return [{"handle": 0, "title": active or "", "pid": 0, "bounds": [0, 0, 0, 0], "foreground": True}] if active else []
    out = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True).stdout
    active = active_title()
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        handle, desktop, x, y, width, height, pid, title = parts
        rows.append(
            {
                "handle": handle,
                "desktop": desktop,
                "title": title,
                "pid": int(pid),
                "bounds": [int(x), int(y), int(x) + int(width), int(y) + int(height)],
                "foreground": title.strip() == (active or "").strip(),
            }
        )
    return rows


def active_title() -> str:
    if IS_WINDOWS:  # pragma: no cover
        from .input_control import active_window

        window = active_window()
        return window.title if window else ""
    if IS_MAC:  # pragma: no cover
        out = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to name of first application process whose frontmost is true'],
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    if _has("xdotool"):
        out = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True)
        return out.stdout.strip()
    return ""


def _has(binary: str) -> bool:
    from shutil import which

    return which(binary) is not None


def _title_contains(name: str, strip_suffixes: bool = True) -> str:
    """'chrome' -> 'Chrome', 'chrome.exe' -> 'Chrome' (so users can name apps naturally)."""
    base = name.strip()
    for suffix in (".exe", ".app", ".desktop"):
        if strip_suffixes and base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base


# --------------------------------------------------------------------------------------
# UI tree
# --------------------------------------------------------------------------------------


def read_ui_tree(handle: int | str = 0, max_depth: int = 5, max_nodes: int = 400) -> list[dict[str, Any]]:
    """Structured element list for a window: role, name, value, bounds, state."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import uiautomation as auto  # type: ignore
        except Exception as exc:
            raise ActionFailed(
                "UI Automation is unavailable: pip install uiautomation",
                {"needs": "uiautomation", "driver": "windows.uia", "error": str(exc)},
            ) from exc
        root = auto.ControlFromHandle(int(handle)) if handle else auto.GetForegroundControl()
        rows: list[dict[str, Any]] = []

        def walk(node: Any, depth: int) -> None:
            if depth > max_depth or len(rows) >= max_nodes:
                return
            try:
                rect = node.BoundingRectangle
                rows.append(
                    {
                        "role": node.ControlTypeName,
                        "name": node.Name,
                        "value": _safe_value(node),
                        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                        "enabled": bool(node.IsEnabled),
                        "focused": bool(node.HasKeyboardFocus),
                        "automation_id": getattr(node, "AutomationId", ""),
                    }
                )
                for child in node.GetChildren():
                    walk(child, depth + 1)
            except Exception:
                return

        walk(root, 0)
        return rows
    if IS_MAC:  # pragma: no cover - macOS only
        raise ActionFailed(
            "macOS AX tree reading needs the accessibility bridge",
            {"needs": "pyobjc-framework-ApplicationServices", "driver": "macos.ax", "installed": False},
        )
    if _has("accerciser") or _has("python3"):
        try:  # pragma: no cover - Linux desktop only
            import pyatspi  # type: ignore

            rows: list[dict[str, Any]] = []
            desktop = pyatspi.Registry.getDesktop(0)
            for app in desktop:
                for window in app:
                    if len(rows) >= max_nodes:
                        break
                    rows.append(
                        {
                            "role": window.getRoleName(),
                            "name": window.name,
                            "value": "",
                            "bounds": [],
                            "enabled": bool(window.getState().contains(pyatspi.STATE_ENABLED)),
                            "focused": bool(window.getState().contains(pyatspi.STATE_ACTIVE)),
                        }
                    )
            return rows
        except Exception:
            pass
    raise ActionFailed(
        "no accessibility tree available on this platform",
        {"platform": platform.system(), "hint": "install AT-SPI (pyatspi) on Linux, or use vision_ocr / vision_describe"},
    )


def _safe_value(node: Any) -> str:
    try:
        pattern = node.GetValuePattern()
        if pattern is not None:
            return str(pattern.Value)
    except Exception:
        pass
    try:
        return str(node.LegacyIAccessiblePatternValue or "")
    except Exception:
        return ""


# --------------------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------------------


def launch_app(target: str, args: Sequence[str] = (), wait: float = 0.0) -> dict[str, Any]:
    """Launch an application or document the way the OS shell would."""
    argv: list[str] = []
    if os.path.exists(target) and not target.lower().endswith((".app", ".desktop")):
        argv = [target, *args]
    elif IS_WINDOWS:  # pragma: no cover
        argv = ["cmd", "/c", "start", "", target, *args]
    elif IS_MAC:  # pragma: no cover
        argv = ["open", "-a", target, *args] if not target.endswith(".app") else ["open", target, *args]
    else:
        executable = _has(target) or _has(target.lower())
        argv = [target, *args] if executable else ["xdg-open", target, *args]
    try:
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as exc:
        raise ActionFailed(f"cannot launch {target!r}: {exc}", {"argv": argv}) from exc
    if wait:
        time.sleep(wait)
    return {"launched": target, "argv": argv, "pid": process.pid}


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


@action(
    name="windows_list",
    description="List open windows with title, pid, bounds and which one is in the foreground.",
    capability="windows.list",
    group="windows",
    parameters={"type": "object", "properties": {"filter": {"type": "string"}}},
)
def windows_list(ctx: ActionContext, filter: str = "") -> Any:  # noqa: A002
    rows = list_windows()
    if filter:
        rows = [r for r in rows if filter.lower() in str(r.get("title", "")).lower()]
    return {"count": len(rows), "foreground": next((r["title"] for r in rows if r.get("foreground")), None), "windows": rows[:80]}


@action(
    name="windows_focus",
    description=(
        "Bring a window to the foreground by title substring (or pid). Verifies the window is "
        "actually foreground afterwards — the guard that keeps later typing honest."
    ),
    capability="windows.focus",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "title_contains": {"type": "string"},
            "pid": {"type": "integer"},
            "exact": {"type": "boolean"},
            "timeout": {"type": "number"},
        },
    },
)
def windows_focus(
    ctx: ActionContext,
    title_contains: str = "",
    pid: int = 0,
    exact: bool = False,
    timeout: float = 5.0,
) -> Any:
    if not title_contains and not pid:
        raise ActionFailed("title_contains or pid is required")
    deadline = time.time() + timeout
    last: list[dict[str, Any]] = []
    while time.time() < deadline:
        last = list_windows()
        for window in last:
            title = str(window.get("title", ""))
            match = (
                (title.strip().lower() == title_contains.strip().lower())
                if exact
                else (title_contains.lower() in title.lower())
            )
            if pid and int(window.get("pid") or 0) != pid:
                continue
            if title_contains and not match:
                continue
            if _activate(window):
                time.sleep(0.15)
                foreground = next((w for w in list_windows() if w.get("foreground")), None)
                return {
                    "focused": True,
                    "window": window,
                    "foreground_now": foreground.get("title") if foreground else None,
                    "verified": bool(foreground and str(window.get("title", ""))[:20] in str(foreground.get("title", ""))),
                }
        time.sleep(0.2)
    raise ActionFailed(
        f"could not focus a window matching {title_contains or pid!r}",
        {"visible_titles": [w.get("title") for w in last][:40]},
    )


def _activate(window: dict[str, Any]) -> bool:
    handle = window.get("handle")
    try:
        if IS_WINDOWS and handle:  # pragma: no cover
            import ctypes

            user32 = ctypes.windll.user32
            user32.ShowWindow(int(handle), 9)  # SW_RESTORE
            user32.SetForegroundWindow(int(handle))
            return True
        if IS_MAC:  # pragma: no cover
            app = window.get("app") or window.get("title", "")
            return subprocess.run(["osascript", "-e", f'tell application "{app}" to activate'], capture_output=True).returncode == 0
        if _has("wmctrl") and handle:
            return subprocess.run(["wmctrl", "-ia", str(handle)], capture_output=True).returncode == 0
        if _has("xdotool") and handle:
            return subprocess.run(["xdotool", "windowactivate", str(handle)], capture_output=True).returncode == 0
    except Exception:
        return False
    return False


@action(
    name="windows_launch",
    description=(
        "Launch an application, file or URL exactly as the OS shell would (like double-clicking "
        "it), optionally waiting for its window to appear."
    ),
    capability="windows.launch",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Executable name, full path, .app/.desktop, document or URL."},
            "args": {"type": "array", "items": {"type": "string"}},
            "expect_window": {"type": "string", "description": "Title substring to wait for."},
            "timeout": {"type": "number"},
        },
        "required": ["target"],
    },
)
def windows_launch(
    ctx: ActionContext,
    target: str,
    args: Sequence[str] = (),
    expect_window: str = "",
    timeout: float = 20.0,
) -> Any:
    result = launch_app(target, args)
    if expect_window:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(expect_window.lower() in str(w.get("title", "")).lower() for w in list_windows()):
                result["window_found"] = expect_window
                return result
            time.sleep(0.4)
        raise ActionFailed(
            f"launched {target!r} but no window matching {expect_window!r} appeared within {timeout}s",
            {"argv": result["argv"], "titles": [w.get("title") for w in list_windows()][:30]},
        )
    return result


@action(
    name="windows_close",
    description="Close a window gracefully by title substring (sends WM_CLOSE / equivalent).",
    capability="windows.focus",
    group="windows",
    parameters={"type": "object", "properties": {"title_contains": {"type": "string"}}, "required": ["title_contains"]},
)
def windows_close(ctx: ActionContext, title_contains: str) -> Any:
    for window in list_windows():
        title = str(window.get("title", ""))
        if title_contains.lower() not in title.lower():
            continue
        handle = window.get("handle")
        if IS_WINDOWS and handle:  # pragma: no cover
            import ctypes

            ctypes.windll.user32.PostMessageW(int(handle), 0x0010, 0, 0)  # WM_CLOSE
            return {"closed": title}
        if IS_MAC:  # pragma: no cover
            subprocess.run(["osascript", "-e", f'tell application "{window.get("app", title)}" to close window "{title}"'], capture_output=True)
            return {"closed": title}
        if _has("wmctrl") and handle:
            subprocess.run(["wmctrl", "-ic", str(handle)], capture_output=True)
            return {"closed": title}
    raise ActionFailed(f"no window matched {title_contains!r}", {"titles": [w.get("title") for w in list_windows()][:30]})


@action(
    name="windows_uia_read",
    description=(
        "Read the accessibility tree of a window: every control with role, name, value, bounds "
        "and focused state. Prefer this over OCR — it is exact, cheaper and complete."
    ),
    capability="windows.uia",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "title_contains": {"type": "string"},
            "handle": {"type": "integer"},
            "max_depth": {"type": "integer"},
            "max_nodes": {"type": "integer"},
            "role_filter": {"type": "string"},
        },
    },
)
def windows_uia_read(
    ctx: ActionContext,
    title_contains: str = "",
    handle: int = 0,
    max_depth: int = 4,
    max_nodes: int = 400,
    role_filter: str = "",
) -> Any:
    if not handle and title_contains:
        handle = _first_handle(title_contains)
    nodes = read_ui_tree(handle, max_depth=max_depth, max_nodes=max_nodes)
    if role_filter:
        nodes = [n for n in nodes if role_filter.lower() in str(n.get("role", "")).lower()]
    return {"element_count": len(nodes), "elements": nodes}


def _first_handle(title_contains: str) -> int:
    for window in list_windows():
        if title_contains.lower() in str(window.get("title", "")).lower():
            return int(window.get("handle") or 0)
    raise ActionFailed(f"no window matched {title_contains!r}", {"titles": [w.get("title") for w in list_windows()][:30]})


@action(
    name="windows_read_value",
    description=(
        "Read the value of the currently focused control. This is the read-back used by "
        "input_type to prove that typed text actually landed somewhere."
    ),
    capability="windows.uia",
    group="windows",
    parameters={"type": "object", "properties": {"title_contains": {"type": "string"}}},
)
def windows_read_value(ctx: ActionContext, title_contains: str = "") -> Any:
    if not IS_WINDOWS:
        from .input_control import _readback_reader

        reader = _readback_reader(ctx)
        return {"value": reader() if reader else ""}
    try:  # pragma: no cover - Windows only
        import uiautomation as auto  # type: ignore

        control = auto.GetFocusedControl()
        value = ""
        try:
            pattern = control.GetValuePattern()
            value = pattern.Value if pattern else ""
        except Exception:
            value = getattr(control, "Name", "")
        return {"value": value, "role": control.ControlTypeName, "name": control.Name}
    except Exception as exc:
        raise ActionFailed(f"UI Automation unavailable: {exc}", {"needs": "uiautomation"}) from exc


@action(
    name="windows_set_value",
    description=(
        "Set a field's value directly through the accessibility API (no typing, no focus "
        "game). Falls back to focus+type when the control refuses a value pattern."
    ),
    capability="windows.uia",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "title_contains": {"type": "string"},
            "name": {"type": "string", "description": "Control name/label to target."},
            "value": {"type": "string"},
            "automation_id": {"type": "string"},
        },
        "required": ["value"],
    },
)
def windows_set_value(
    ctx: ActionContext,
    title_contains: str = "",
    name: str = "",
    value: str = "",
    automation_id: str = "",
) -> Any:
    if not IS_WINDOWS:
        raise ActionFailed(
            "direct value setting requires Windows UI Automation on this platform",
            {"hint": "use input_type with expected_title, or a browser read/write action"},
        )
    try:  # pragma: no cover - Windows only
        import uiautomation as auto  # type: ignore

        root = auto.ControlFromHandle(_first_handle(title_contains)) if title_contains else auto.GetForegroundControl()
        target = None
        for control, _depth in auto.WalkControl(root, maxDepth=6):
            if automation_id and getattr(control, "AutomationId", "") == automation_id:
                target = control
                break
            if name and control.Name == name:
                target = control
                break
        if target is None:
            raise ActionFailed(f"no control matched name={name!r} automation_id={automation_id!r}")
        pattern = target.GetValuePattern()
        if pattern is None:
            target.SetFocus()
            auto.SendKeys(value, waitTime=0)
        else:
            pattern.SetValue(value)
        return {"set": value, "control": target.Name, "verified": _safe_value(target) == value}
    except ActionFailed:
        raise
    except Exception as exc:
        raise ActionFailed(f"could not set value: {exc}") from exc


@action(
    name="windows_click_element",
    description=(
        "Click a control found in the accessibility tree by name/role — resolving the element "
        "to coordinates and clicking its centre, then verifying the click landed on a live "
        "element."
    ),
    capability="windows.uia",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "title_contains": {"type": "string"},
            "name": {"type": "string"},
            "role": {"type": "string"},
            "nth": {"type": "integer"},
        },
        "required": ["name"],
    },
)
def windows_click_element(
    ctx: ActionContext, title_contains: str = "", name: str = "", role: str = "", nth: int = 1
) -> Any:
    handle = _first_handle(title_contains) if title_contains else 0
    nodes = read_ui_tree(handle, max_depth=6, max_nodes=800)
    matches = [
        node
        for node in nodes
        if name.lower() in str(node.get("name", "")).lower()
        and (not role or role.lower() in str(node.get("role", "")).lower())
        and node.get("bounds")
    ]
    if not matches:
        raise ActionFailed(
            f"no element matched name={name!r} role={role!r}",
            {"candidates": [n.get("name") for n in nodes][:40]},
        )
    chosen = matches[min(max(1, nth) - 1, len(matches) - 1)]
    left, top, right, bottom = chosen["bounds"]
    point = ((left + right) // 2, (top + bottom) // 2)
    from .input_control import backend

    backend().click(point[0], point[1])
    return {"clicked": chosen.get("name"), "role": chosen.get("role"), "at": point, "bounds": chosen["bounds"]}


@action(
    name="windows_state",
    description="Minimise, maximise, restore or hide a window.",
    capability="windows.focus",
    group="windows",
    parameters={
        "type": "object",
        "properties": {
            "title_contains": {"type": "string"},
            "state": {"type": "string", "enum": ["minimise", "maximise", "restore", "hide"]},
        },
        "required": ["title_contains", "state"],
    },
)
def windows_state(ctx: ActionContext, title_contains: str, state: str) -> Any:
    handle = _first_handle(title_contains)
    if IS_WINDOWS:  # pragma: no cover
        import ctypes

        codes = {"minimise": 6, "maximise": 3, "restore": 9, "hide": 0}
        ctypes.windll.user32.ShowWindow(int(handle), codes.get(state, 9))
        return {"window": title_contains, "state": state}
    if IS_MAC:  # pragma: no cover
        script = {
            "minimise": "set miniaturized of window 1 to true",
            "maximise": "set zoomed of window 1 to true",
            "restore": "set miniaturized of window 1 to false",
            "hide": "set visible to false",
        }.get(state, "")
        subprocess.run(["osascript", "-e", f'tell application "System Events" to {script}'], capture_output=True)
        return {"window": title_contains, "state": state}
    if _has("xdotool"):
        action_map = {"minimise": "windowminimize", "maximise": "windowsize --sync %s 100% 100%", "restore": "windowactivate"}
        cmd = action_map.get(state, "windowactivate")
        subprocess.run(f"xdotool {cmd} {handle}".split(), capture_output=True)
        return {"window": title_contains, "state": state}
    raise ActionFailed("window state changes are unsupported on this platform")


all_actions = lambda: [  # noqa: E731
    windows_list,
    windows_focus,
    windows_launch,
    windows_close,
    windows_uia_read,
    windows_read_value,
    windows_set_value,
    windows_click_element,
    windows_state,
]
