"""Mistake memory: what went wrong, in a shape the *next* attempt can use.

The complaint this module answers is "it keeps making the same mistake". More retries do not
fix that; a retry that repeats the same wrong assumption fails the same way. What fixes it is
remembering the failure in a form an agent can read *before* choosing a tool:

    browser_click failed: selector matched nothing (shadow DOM) → known fix: use browser_click
    with pierce=true, or vision_ground on the label

So each failure is distilled into a :class:`Lesson` keyed by a *signature* — the error with the
volatile parts (numbers, ids, paths, addresses) stripped out — and the registry attaches the
best matching fix to the next failure of the same kind. Two properties matter and are easy to
get wrong:

1. **A lesson is a hint, never an instruction.** It is attached to an error message and
   offered to the agent; nothing executes automatically because a lesson exists.
2. **A learning system that only remembers successes learns nothing.** Failures are recorded
   by default, including permission denials (a denied capability is the single most common
   repeat mistake in a fleet), and the book is capped so it cannot grow without bound.

Storage is a small JSON file next to the other per-user state. No network, no model call: a
lesson must be available on the very first failure, not after a round trip.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Strip the volatile parts of an error so "timed out after 12s" and "timed out after 30s"
#: are the same lesson instead of two.
_VOLATILE = (
    (re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE), "<id>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<hex>"),
    # Any run of digits, including "12s" and "300ms": a timeout that changes by a second is the
    # same lesson, and the boundary-anchored version left "12s" distinct from "300s".
    (re.compile(r"\d+(?:\.\d+)?"), "<num>"),
    (re.compile(r"(['\"])[^'\"]{0,200}\1"), "<str>"),
    (re.compile(r"\b[A-Za-z]:\\[^\s]+"), "<path>"),
    (re.compile(r"/(?:home|Users|tmp|var)/[^\s]+"), "<path>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
)
_WHITESPACE = re.compile(r"\s+")
MAX_LESSONS = 400


def signature_of(action: str, error: str) -> str:
    """A stable key for \"this kind of failure of this tool\"."""
    text = (error or "").strip().lower()
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    text = _WHITESPACE.sub(" ", text)[:180]
    return f"{action}|{text}"


def _tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z_]{3,}", (text or "").lower())}


@dataclass(slots=True)
class Lesson:
    action: str
    signature: str
    cause: str = ""
    fix: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    hits: int = 1
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return self.signature

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Lesson":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (data or {}).items() if key in known})

    def line(self) -> str:
        """One readable line: what failed and what to do instead."""
        head = f"{self.action}: {self.cause or self.signature.split('|', 1)[-1]}"
        return f"{head} — known fix: {self.fix}" if self.fix else head

    def score(self, action: str, error: str = "") -> float:
        """Rank lessons for a new situation: same tool first, then similar words, then volume."""
        value = 0.0
        if action and self.action == action:
            value += 3.0
        elif action and self.action != action:
            value -= 0.5
        wanted = _tokens(error)
        if wanted:
            value += 2.0 * len(wanted & _tokens(self.signature)) / max(1, len(wanted))
        value += math.log1p(self.hits)
        age_days = max(0.0, (time.time() - self.last_seen) / 86_400.0)
        value += 1.0 / (1.0 + age_days)
        return round(value, 3)


class LessonBook:
    """Persistent, capped, ranked memory of failures and their fixes."""

    def __init__(self, home: str | os.PathLike[str] | None = None, bus: Any = None, path: str | os.PathLike[str] | None = None):
        self.bus = bus
        self.home = Path(home) if home is not None else Path(".")
        self._path = Path(path) if path is not None else self.home / "lessons.json"
        self.lessons: dict[str, Lesson] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for row in data.get("lessons") or []:
            lesson = Lesson.from_json(row)
            if lesson.signature:
                self.lessons[lesson.signature] = lesson

    def save(self) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [lesson.to_json() for lesson in self._capped()]
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "lessons": rows}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        return self._path

    def _capped(self) -> list[Lesson]:
        # Keep the ones that keep happening and the ones seen most recently: a one-off typo from
        # six months ago is not worth a slot that a repeating failure needs.
        ordered = sorted(self.lessons.values(), key=lambda item: (item.hits, item.last_seen), reverse=True)
        return ordered[:MAX_LESSONS]

    # -- writing -----------------------------------------------------------------------
    def record(
        self,
        action: str,
        error: str,
        evidence: dict[str, Any] | None = None,
        fix: str = "",
        cause: str = "",
        count: bool = True,
    ) -> Lesson:
        """Record a failure, or (``count=False``) just attach a fix to a pattern.

        The distinction matters: a fix written down *before* the failure happens is not an
        occurrence of it. Counting a note as a hit made the very next failure look like a
        repeat and annotate itself, which is exactly the noise that teaches agents to ignore
        the hint.
        """
        signature = signature_of(action, error)
        lesson = self.lessons.get(signature)
        if lesson is None:
            lesson = Lesson(
                action=action,
                signature=signature,
                cause=cause or (error or "").strip()[:240],
                fix=fix,
                evidence=dict(evidence or {}),
                # A fix written down before anything failed has seen zero failures. Starting it
                # at 1 made the next real failure look like a repeat and annotate itself.
                hits=1 if count else 0,
            )
            self.lessons[signature] = lesson
        else:
            if count:
                lesson.hits += 1
            lesson.last_seen = time.time()
            if fix:
                lesson.fix = fix
            if cause:
                lesson.cause = cause
            if evidence:
                lesson.evidence = {**(evidence or {}), **lesson.evidence}
        self._prune()
        self.save()
        if self.bus is not None:
            try:
                from ..events import LESSON_RECORDED

                self.bus.emit(
                    LESSON_RECORDED,
                    source="lessons",
                    action=lesson.action,
                    signature=lesson.signature,
                    hits=lesson.hits,
                    fix=lesson.fix[:200],
                )
            except Exception:  # noqa: BLE001 - learning must never break the action that failed
                pass
        return lesson

    def annotate(self, action: str, error: str, evidence: dict[str, Any] | None = None) -> str:
        """Record the failure; append the known fix only once it is a *repeat*.

        Annotating on the first occurrence made every validation refusal (“objective is too
        short”) carry a “known fix” line, which trains agents to read the hint as noise. The
        first failure is recorded; the second one — the one that means "it did it again" — is
        the one that carries the correction.
        """
        lesson = self.record(action, error, evidence)
        if lesson.hits < 2:
            return error
        hint = lesson.fix or self.best_fix(action, error)
        if hint and hint not in (error or ""):
            return f"{error} | known fix: {hint} (seen {lesson.hits}x)"
        return error

    def _prune(self) -> None:
        """Drop the least useful lessons from memory, not only from the file.

        Capping only the saved copy left the dictionary growing for the life of the process —
        exactly the long-running case the cap exists for.
        """
        if len(self.lessons) <= MAX_LESSONS:
            return
        keep = {lesson.signature for lesson in self._capped()}
        for signature in list(self.lessons):
            if signature not in keep:
                self.lessons.pop(signature, None)

    def forget(self, signature_or_action: str) -> int:
        removed = [key for key, lesson in self.lessons.items() if key == signature_or_action or lesson.action == signature_or_action]
        for key in removed:
            self.lessons.pop(key, None)
        if removed:
            self.save()
        return len(removed)

    # -- reading -----------------------------------------------------------------------
    def best_fix(self, action: str, error: str = "") -> str:
        for lesson in self.recall(action, error, k=3):
            if lesson.fix:
                return lesson.fix
        return ""

    def recall(self, action: str = "", error: str = "", k: int = 5) -> list[Lesson]:
        ranked = sorted(self.lessons.values(), key=lambda item: item.score(action, error), reverse=True)
        return [lesson for lesson in ranked if action or error][:k]

    def for_actions(self, actions: Sequence[str], k: int = 6) -> list[dict[str, Any]]:
        """Compact hints for a set of tools, for injection into an agent's context."""
        scored: list[Lesson] = []
        for name in actions:
            scored.extend(self.recall(name, k=2))
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for lesson in sorted(scored, key=lambda item: item.score(lesson_action(actions, item)), reverse=True):
            if lesson.signature in seen or not lesson.fix:
                continue
            seen.add(lesson.signature)
            out.append({"action": lesson.action, "cause": lesson.cause[:160], "fix": lesson.fix[:200], "hits": lesson.hits})
            if len(out) >= k:
                break
        return out

    def top(self, k: int = 5) -> list[dict[str, Any]]:
        """The most repeated fixed failures — what a new agent should read first."""
        fixed = [lesson for lesson in self.lessons.values() if lesson.fix]
        ordered = sorted(fixed, key=lambda item: (item.hits, item.last_seen), reverse=True)
        return [
            {"action": lesson.action, "cause": lesson.cause[:140], "fix": lesson.fix[:180], "hits": lesson.hits}
            for lesson in ordered[:k]
        ]

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "action": lesson.action,
                "cause": lesson.cause[:200],
                "fix": lesson.fix[:200],
                "hits": lesson.hits,
                "last_seen": lesson.last_seen,
            }
            for lesson in self._capped()
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "lessons": len(self.lessons),
            "hits": sum(lesson.hits for lesson in self.lessons.values()),
            "with_fix": sum(1 for lesson in self.lessons.values() if lesson.fix),
            "path": str(self._path),
        }


def lesson_action(actions: Iterable[str], lesson: Lesson) -> str:
    """Ranking helper: prefer the requested action's own lessons."""
    return lesson.action if lesson.action in set(actions) else ""
