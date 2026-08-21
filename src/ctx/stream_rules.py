"""Bounded stream-rule matching for ctx-owned model transports.

Cross-host hooks cannot observe assistant tokens, so this module does not claim
to retrofit mid-stream interruption into Claude Code or Codex.  It is the
transport-neutral state machine for callers that *do* own a stream (SDK-backed
hosts and future native runners): feed text deltas, abort when a match is
returned, persist the activated rule names, then retry with ``injection`` as a
targeted system reminder.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Pattern

STREAM_RULE_SCHEMA = "ctx.stream-rule/v1"
DEFAULT_WINDOW_CHARS = 8192


@dataclass(frozen=True)
class StreamRule:
    """One dormant rule activated by a regex in assistant output."""

    name: str
    pattern: str
    reminder: str
    flags: int = re.IGNORECASE
    max_fires: int = 1


@dataclass(frozen=True)
class StreamRuleMatch:
    """An abort-and-retry request returned to a stream-owning caller."""

    rule: str
    matched_text: str
    injection: str
    receipt: dict


@dataclass(frozen=True)
class _CompiledRule:
    spec: StreamRule
    regex: Pattern[str]


class StreamRuleEngine:
    """Incremental bounded matcher with serializable activation state."""

    def __init__(
        self,
        rules: Iterable[StreamRule],
        *,
        window_chars: int = DEFAULT_WINDOW_CHARS,
        prior_state: dict | None = None,
    ) -> None:
        self.window_chars = max(256, int(window_chars))
        self._rules: list[_CompiledRule] = []
        for rule in rules:
            if not rule.name.strip() or not rule.reminder.strip() or rule.max_fires < 1:
                raise ValueError("stream rules need a name, reminder, and max_fires >= 1")
            self._rules.append(_CompiledRule(rule, re.compile(rule.pattern, rule.flags)))
        state = prior_state if isinstance(prior_state, dict) else {}
        raw_counts = state.get("fires", {}) if state.get("schema") == STREAM_RULE_SCHEMA else {}
        self._fires = {
            item.spec.name: max(0, int(raw_counts.get(item.spec.name, 0) or 0))
            for item in self._rules
        }
        activated = state.get("activated", []) if state.get("schema") == STREAM_RULE_SCHEMA else []
        known = {item.spec.name for item in self._rules}
        self._activated = [str(name) for name in activated if str(name) in known]
        self.begin_turn()

    def begin_turn(self) -> None:
        """Reset only the rolling text and per-turn deduplication state."""
        self._buffer = ""
        self._turn_fired: set[str] = set()

    def feed(self, delta: str) -> StreamRuleMatch | None:
        """Consume one text delta and return the first newly activated rule.

        Matching is bounded to the latest ``window_chars``. Regexes intended to
        span more than that are outside the contract; the bound prevents a long
        response from becoming another context-sized in-memory accumulator.
        """
        if not delta:
            return None
        combined = self._buffer + str(delta)
        start = len(combined) - min(len(combined), self.window_chars)
        self._buffer = combined[start:]
        for item in self._rules:
            rule = item.spec
            if rule.name in self._turn_fired or self._fires.get(rule.name, 0) >= rule.max_fires:
                continue
            match = item.regex.search(self._buffer)
            if match is None:
                continue
            self._turn_fired.add(rule.name)
            self._fires[rule.name] = self._fires.get(rule.name, 0) + 1
            if rule.name not in self._activated:
                self._activated.append(rule.name)
            matched = match.group(0)[-256:]
            receipt = {
                "schema": STREAM_RULE_SCHEMA,
                "event": "activated",
                "rule": rule.name,
                "fire": self._fires[rule.name],
                "windowChars": self.window_chars,
            }
            injection = (
                f"A stream rule named {rule.name!r} activated. Correct course before "
                f"continuing:\n\n{rule.reminder}"
            )
            return StreamRuleMatch(rule.name, matched, injection, receipt)
        return None

    def state(self) -> dict:
        """State safe to preserve across retry and compaction boundaries."""
        return {
            "schema": STREAM_RULE_SCHEMA,
            "activated": list(self._activated),
            "fires": dict(sorted(self._fires.items())),
        }

    def persistent_reminders(self) -> tuple[str, ...]:
        """Reminder bodies for rules activated earlier in the session."""
        by_name = {item.spec.name: item.spec.reminder for item in self._rules}
        return tuple(by_name[name] for name in self._activated if name in by_name)

    @property
    def buffered_chars(self) -> int:
        """Current rolling-window size, exposed for observability and tests."""
        return len(self._buffer)


def load_state(path: Path) -> dict:
    """Read state fail-open; malformed or absent state means a fresh engine."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    """Atomically persist state; callers decide whether persistence is required."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
