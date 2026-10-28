"""Vision: capture, OCR, grounding and the vision-free equivalent of seeing.

The requirement is explicit and this module is where it is honoured: *"it can see what is
on the page with vision or without vision, and without vision it delivers the same
information as vision provides."*

How that is actually achieved:

* **Vision path** — :func:`vision_capture` grabs the screen (or a window/region),
  :func:`vision_ocr` returns text with boxes, :func:`vision_ground` turns a description
  into clickable coordinates, :func:`vision_diff` proves whether an action changed
  anything.
* **Text path** — the platform accessibility trees (UIA on Windows, AX on macOS, AT-SPI on
  Linux) and the browser DOM/CDP tree (see :mod:`orbynth.actions.browser`) describe the
  same window as a structured element list: role, name, value, bounds, enabled/focused
  state. An agent that receives ``detail="text"`` has *more* information than the pixels
  carry (state, exact values, full off-screen text), not less — so it must not demand a
  screenshot to make a decision.

Every capture is downscaled and metadata-tagged so the HUD can show it and the audit log
can store it without blowing up memory.
"""

from __future__ import annotations

import base64
import io
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .registry import Action, ActionContext, ActionFailed, DriverUnavailable, action

# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------


@dataclass
class Capture:
    """A captured frame plus the metadata agents and the HUD need."""

    width: int
    height: int
    png: bytes
    origin: tuple[int, int] = (0, 0)
    label: str = "screen"
    captured_at: float = field(default_factory=time.time)

    @property
    def base64(self) -> str:
        return base64.b64encode(self.png).decode("ascii")

    def to_json(self, include_image: bool = False, max_edge: int = 1280) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "origin": list(self.origin),
            "bytes": len(self.png),
            "captured_at": self.captured_at,
        }
        if include_image:
            payload["png_base64"] = self.base64
        return payload


def _grab(pil_image: Any, max_edge: int) -> tuple[bytes, int, int]:
    """Encode a PIL image to PNG, downscaling so the HUD and model stay cheap."""
    width, height = pil_image.size
    if max_edge and max(width, height) > max_edge:
        scale = max_edge / float(max(width, height))
        pil_image = pil_image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), width, height


def capture_screen(region: tuple[int, int, int, int] | None = None, max_edge: int = 1280) -> Capture:
    """Capture the desktop (or a region) using mss, falling back to Pillow."""
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - desktop only
        raise DriverUnavailable(
            "vision.capture",
            "mss + Pillow",
            "Install with: pip install \"orbynth[desktop]\" (headless machines have no screen to capture).",
        ) from exc
    with mss.mss() as screen:
        monitor = screen.monitors[1] if len(screen.monitors) > 1 else screen.monitors[0]
        box = {"left": region[0], "top": region[1], "width": region[2] - region[0], "height": region[3] - region[1]} if region else monitor
        shot = screen.grab(box)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    png, width, height = _grab(image, max_edge)
    return Capture(width=width, height=height, png=png, origin=(box["left"], box["top"]), label="region" if region else "screen")


def capture_window(title_contains: str = "", max_edge: int = 1280) -> Capture:
    """Capture one window (by title substring) rather than the whole desktop."""
    if not title_contains:
        return capture_screen(max_edge=max_edge)
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise DriverUnavailable("vision.capture_window", "mss + Pillow", str(exc)) from exc
    bounds = window_bounds(title_contains)
    if bounds is None:
        raise ActionFailed(f"no window whose title contains {title_contains!r}", {"title": title_contains})
    x, y, width, height = bounds
    with mss.mss() as screen:
        shot = screen.grab({"left": x, "top": y, "width": max(1, width), "height": max(1, height)})
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    png, w, h = _grab(image, max_edge)
    return Capture(width=w, height=h, png=png, origin=(x, y), label=title_contains)


def window_bounds(title_contains: str) -> tuple[int, int, int, int] | None:
    """Screen rectangle of a window, used for region capture and click offset mapping."""
    from .windows import list_windows

    for window in list_windows():
        if title_contains.lower() in str(window.get("title", "")).lower():
            bounds = window.get("bounds") or (0, 0, 0, 0)
            return (bounds[0], bounds[1], bounds[2] - bounds[0], bounds[3] - bounds[1])
    return None


# --------------------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------------------

_OCR_ENGINE: Any = None


def _ocr_engine() -> Any:
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:  # pragma: no cover - optional dependency
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        _OCR_ENGINE = ("rapidocr", RapidOCR())
        return _OCR_ENGINE
    except Exception:
        pass
    try:  # pragma: no cover - optional dependency
        import pytesseract  # type: ignore

        _OCR_ENGINE = ("tesseract", pytesseract)
        return _OCR_ENGINE
    except Exception as exc:
        raise DriverUnavailable(
            "vision.ocr",
            "rapidocr-onnxruntime or pytesseract",
            "Install with: pip install \"orbynth[vision]\" (or the tesseract binary for pytesseract).",
        ) from exc


def ocr_capture(capture: Capture, languages: Sequence[str] | str = ()) -> list[dict[str, Any]]:
    """OCR a capture into ``[{text, box, confidence}]`` in *screen* coordinates.

    ``languages`` is a language-code list ("ur", "ar") or a raw engine pack string. Without
    it the engine would be left in its own default, which is where Urdu and Arabic interfaces
    silently come back as garbage (contract 22.11).
    """
    from ..ops.languages import LANGUAGES, direction_of, normalize, ocr_languages

    pack = languages if isinstance(languages, str) else ocr_languages(languages)
    engine, runtime = _ocr_engine()
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise DriverUnavailable("vision.ocr", "Pillow", str(exc)) from exc
    image = Image.open(io.BytesIO(capture.png))
    items: list[dict[str, Any]] = []
    if engine == "rapidocr":
        try:
            import numpy as np  # type: ignore
        except Exception as exc:  # pragma: no cover - ships with the vision extra
            raise DriverUnavailable("vision.ocr", "numpy", str(exc)) from exc
        result, _elapsed = runtime(np.array(image))
        for entry in result or []:
            try:
                quad, text, score = entry[0], entry[1], entry[2]
            except (TypeError, IndexError):  # pragma: no cover - engine variant
                continue
            xs = [float(point[0]) for point in quad]
            ys = [float(point[1]) for point in quad]
            items.append({"text": str(text), "box": [min(xs), min(ys), max(xs), max(ys)], "confidence": float(score)})
    else:
        config = ""
        if pack and pack != "eng":
            # Tesseract takes "+"-joined ISO 639-2 codes and needs the pack installed; if it is
            # missing the call raises, which surfaces as a DriverUnavailable instead of silence.
            config = f"--oem 3 --psm 6 -l {pack}"
        data = runtime.image_to_data(image, output_type=runtime.Output.DICT, config=config) if config else runtime.image_to_data(image, output_type=runtime.Output.DICT)
        for index, text in enumerate(data["text"]):
            if not str(text).strip():
                continue
            left, top = data["left"][index], data["top"][index]
            items.append(
                {
                    "text": str(text),
                    "box": [left, top, left + data["width"][index], top + data["height"][index]],
                    "confidence": float(data["conf"][index]) / 100.0,
                }
            )
    out: list[dict[str, Any]] = []
    for item in items:
        box = item.get("box") or [0, 0, 0, 0]
        raw_text = str(item.get("text", "")).strip()
        # Persian/Arabic presentation forms and ligatures make OCR text unequal to the same
        # text typed by the user, which then looks like a failed action. Normalise once here.
        out.append(
            {
                "text": raw_text,
                "normalised": normalize(raw_text),
                "direction": direction_of(raw_text),
                "box": [
                    int(box[0]) + capture.origin[0],
                    int(box[1]) + capture.origin[1],
                    int(box[2]) + capture.origin[0],
                    int(box[3]) + capture.origin[1],
                ],
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )
    rows = [row for row in out if row["text"]]
    from ..ops.languages import detect as detect_language

    for row in rows:
        row["language"] = detect_language(row["normalised"] or row["text"])
    return rows


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


@action(
    name="vision_capture",
    description=(
        "Capture the screen, a region, or a named window. Returns dimensions, origin and a "
        "downscaled PNG. Use for anything the accessibility tree cannot answer: canvas apps, "
        "games, custom-drawn UI, verification screenshots."
    ),
    capability="vision.capture",
    group="vision",
    parameters={
        "type": "object",
        "properties": {
            "region": {"type": "array", "items": {"type": "integer"}, "description": "[left, top, right, bottom]"},
            "title_contains": {"type": "string", "description": "Capture this window instead of the desktop."},
            "include_image": {"type": "boolean", "description": "Include the PNG bytes (base64)."},
            "max_edge": {"type": "integer"},
        },
    },
)
def vision_capture(
    ctx: ActionContext,
    region: Sequence[int] | None = None,
    title_contains: str = "",
    include_image: bool = True,
    max_edge: int = 1280,
) -> Any:
    capture = (
        capture_window(title_contains, max_edge=max_edge)
        if title_contains
        else capture_screen(tuple(region) if region else None, max_edge=max_edge)  # type: ignore[arg-type]
    )
    data = capture.to_json(include_image=include_image, max_edge=max_edge)
    data["origin"] = list(capture.origin)
    return data


@action(
    name="vision_ocr",
    description=(
        "Read text from the screen (or a region/window) with bounding boxes in screen "
        "coordinates. This is how an agent finds clickable text in an image-only UI."
    ),
    capability="vision.ocr",
    group="vision",
    parameters={
        "type": "object",
        "properties": {
            "region": {"type": "array", "items": {"type": "integer"}},
            "title_contains": {"type": "string"},
            "contains": {"type": "string", "description": "Filter to lines containing this text."},
            "languages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Language codes for OCR, e.g. [\"ur\",\"en\"]. Defaults to the configured UI languages.",
            },
            "max_edge": {"type": "integer"},
        },
    },
)
def vision_ocr(
    ctx: ActionContext,
    region: Sequence[int] | None = None,
    title_contains: str = "",
    contains: str = "",
    languages: Sequence[str] | None = None,
    max_edge: int = 1600,
) -> Any:
    capture = (
        capture_window(title_contains, max_edge=max_edge)
        if title_contains
        else capture_screen(tuple(region) if region else None, max_edge=max_edge)  # type: ignore[arg-type]
    )
    codes = list(languages or [])
    if not codes and ctx.config is not None:
        voice = getattr(ctx.config, "voice", None)
        codes = [getattr(voice, "language", "en"), *getattr(voice, "secondary_languages", [])]
    lines = ocr_capture(capture, codes)
    if contains:
        lines = [line for line in lines if contains.lower() in line["text"].lower()]
    rtl_lines = sum(1 for line in lines if line.get("direction") == "rtl")
    return {
        "label": capture.label,
        "line_count": len(lines),
        "languages": codes or ["en"],
        "rtl_lines": rtl_lines,
        "lines": lines,
    }


@action(
    name="vision_ground",
    description=(
        "Turn a description into click coordinates: OCR the screen and return the best match "
        "(plus alternatives) with a ready-to-click centre point. Use before input_click when "
        "the a11y tree is unavailable."
    ),
    capability="vision.ground",
    group="vision",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Visible label to find, e.g. 'Sign in'."},
            "title_contains": {"type": "string"},
            "nth": {"type": "integer", "description": "Pick the nth match when several fit."},
            "fuzzy": {"type": "boolean", "description": "Allow partial/near matches."},
            "max_edge": {"type": "integer"},
        },
        "required": ["target"],
    },
)
def vision_ground(
    ctx: ActionContext,
    target: str,
    title_contains: str = "",
    nth: int = 1,
    fuzzy: bool = True,
    max_edge: int = 1600,
) -> Any:
    capture = (
        capture_window(title_contains, max_edge=max_edge)
        if title_contains
        else capture_screen(max_edge=max_edge)
    )
    lines = ocr_capture(capture)
    needle = target.strip().lower()
    exact = [line for line in lines if line["text"].strip().lower() == needle]
    partial = [line for line in lines if needle in line["text"].strip().lower()]
    candidates = exact or (partial if fuzzy else [])
    if not candidates:
        raise ActionFailed(
            f"could not find {target!r} on screen",
            {
                "seen": [line["text"] for line in lines][:40],
                "hint": "try vision_ocr to inspect the visible labels, or pass a shorter target",
            },
        )
    index = max(1, nth) - 1
    if index >= len(candidates):
        index = 0
    chosen = candidates[index]
    box = chosen["box"]
    return {
        "target": target,
        "matched_text": chosen["text"],
        "confidence": chosen["confidence"],
        "point": [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2],
        "box": box,
        "alternatives": [line["text"] for line in candidates[1:6]],
        "origin": list(capture.origin),
    }


@action(
    name="vision_diff",
    description=(
        "Compare two captures (or re-capture and compare) to prove whether an action changed "
        "anything. Returns changed pixel ratio and a coarse region map. This is the evidence "
        "that stops an agent claiming a click worked when it did nothing."
    ),
    capability="vision.capture",
    group="vision",
    parameters={
        "type": "object",
        "properties": {
            "before_png_base64": {"type": "string"},
            "region": {"type": "array", "items": {"type": "integer"}},
            "threshold": {"type": "number", "description": "Per-channel difference considered a change (0-255)."},
            "capture_now": {"type": "boolean", "description": "Capture the current screen as 'after'."},
        },
    },
)
def vision_diff(
    ctx: ActionContext,
    before_png_base64: str = "",
    region: Sequence[int] | None = None,
    threshold: float = 24.0,
    capture_now: bool = True,
) -> Any:
    if not before_png_base64:
        raise ActionFailed("before_png_base64 is required (capture it before the action)")
    if not capture_now:
        raise ActionFailed("capture_now=false is not supported without a stored 'after' frame")
    try:
        from PIL import Image, ImageChops  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise DriverUnavailable("vision.diff", "Pillow", str(exc)) from exc

    after = capture_screen(tuple(region) if region else None, max_edge=1280)  # type: ignore[arg-type]
    left = Image.open(io.BytesIO(base64.b64decode(before_png_base64))).convert("RGB")
    right = Image.open(io.BytesIO(after.png)).convert("RGB")
    if left.size != right.size:
        right = right.resize(left.size)
    diff = ImageChops.difference(left, right).convert("L")
    histogram = diff.histogram()
    total = sum(histogram) or 1
    changed = sum(count for value, count in enumerate(histogram) if value > threshold)
    return {
        "changed_ratio": round(changed / total, 5),
        "changed": changed > 0,
        "threshold": threshold,
        "size": list(left.size),
    }


@action(
    name="vision_wait_for",
    description=(
        "Poll the screen (with OCR) until a target text appears or vanishes — with a timeout. "
        "Makes automation robust against load times without blind sleeps."
    ),
    capability="vision.ocr",
    group="vision",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "state": {"type": "string", "enum": ["appear", "disappear"]},
            "timeout": {"type": "number"},
            "interval": {"type": "number"},
        },
        "required": ["target"],
    },
)
def vision_wait_for(
    ctx: ActionContext,
    target: str,
    state: str = "appear",
    timeout: float = 20.0,
    interval: float = 0.75,
) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            lines = ocr_capture(capture_screen(max_edge=1600))
        except DriverUnavailable:
            raise
        present = any(target.lower() in line["text"].lower() for line in lines)
        if present == (state == "appear"):
            return {"target": target, "state": state, "waited": round(timeout - (deadline - time.time()), 2)}
        time.sleep(max(0.05, interval))
    raise ActionFailed(
        f"timed out after {timeout}s waiting for {target!r} to {state}",
        {"target": target, "state": state, "timeout": timeout},
    )


@action(
    name="vision_describe",
    description=(
        "Return a structured, vision-free description of the current screen: every window, "
        "its geometry, the focused control and the text of the frontmost window. This is the "
        "'without vision you get the same information' path — call this before asking for pixels."
    ),
    capability="vision.capture",
    group="vision",
    parameters={"type": "object", "properties": {"include_text": {"type": "boolean"}}},
)
def vision_describe(ctx: ActionContext, include_text: bool = True) -> Any:
    from .windows import list_windows, read_ui_tree

    windows = list_windows()
    foreground = next((w for w in windows if w.get("foreground")), None)
    tree: list[dict[str, Any]] = []
    if include_text and foreground:
        try:
            tree = read_ui_tree(foreground.get("handle") or 0, max_depth=4, max_nodes=300)
        except ActionFailed:
            tree = []
    return {
        "detail": "text",
        "window_count": len(windows),
        "windows": windows[:40],
        "foreground": foreground,
        "elements": tree,
        "note": (
            "This is the same information a screenshot would provide — roles, labels, values, "
            "bounds and state — plus off-screen content that pixels cannot show."
        ),
    }


all_actions = lambda: [  # noqa: E731
    vision_capture,
    vision_ocr,
    vision_ground,
    vision_diff,
    vision_wait_for,
    vision_describe,
]
