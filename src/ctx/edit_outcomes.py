"""What happened to the host's own edit — the fact nothing was recording.

``ctx`` sits in front of every ``Edit``/``Write``/``MultiEdit`` the host issues
(``hook._tool_kind`` classifies them, ``hook`` line ~1960 sees them) and, until
now, did exactly two things with that knowledge: disarm a reflex signature, and
allow the call through. Whether the edit *landed* was never looked at.

That gap matters because the field's most-cited harness failure is an edit that
does not apply — a ``old_string`` the model could not reproduce byte-for-byte,
or one that matches in more than one place. straitjacket is positioned to
repair those (its PreToolUse hook can rewrite tool arguments on the dialects
that support it) but has no idea how often they happen, and the house rule is
that a mechanism ships on measured behaviour rather than a plausible story.

This module is the instrument that makes the rate knowable. It classifies an
edit tool's *result* into a small closed vocabulary and appends a
privacy-safe row — the outcome, the tool, a path digest, sizes. Never the
edited content, never the strings themselves: the ledger has to be safe to
read, share, and attach to a receipt.

Two deliberate limits, stated because they bound what the resulting numbers
mean:

  * **Best-effort classification.** Hosts phrase edit failures in their own
    words and change that wording between releases. Unrecognised text becomes
    ``unknown`` rather than being forced into a bucket, so a wording change
    shows up as a rise in ``unknown`` instead of a silent mis-count.
  * **Only where PostToolUse exists.** Claude Code and Codex deliver a tool
    result to the hook; Antigravity's published contract does not. Rates
    gathered here describe the hosts that report, and the summary says which.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

from ctx.sessiondir import session_reads_path

EDIT_OUTCOME_SCHEMA = "ctx.edit-outcome/v1"

#: The closed vocabulary. Two failure kinds are separated because they need
#: different repairs and only one of them is safely repairable at all: a needle
#: that matches nothing may still be findable by content, while a needle that
#: matches several places is *ambiguous by construction* and must never be
#: resolved by guessing which one the model meant.
OUTCOMES = ("applied", "not_found", "not_unique", "other_error", "unknown")

#: Result text that means the needle matched nothing. Substrings, lowercased,
#: matched anywhere in the result — hosts wrap their errors in varying
#: prefixes, and anchoring to a full sentence broke on the first release that
#: added a filename to it.
_NOT_FOUND_MARKERS = (
    "string to replace not found",
    "old_string not found",
    "no match found for",
    "could not find the string",
    "pattern not found in file",
)

#: Result text that means the needle matched more than once.
_NOT_UNIQUE_MARKERS = (
    "matches of the string to replace",
    "found multiple matches",
    "is not unique",
    "appears multiple times",
    "replace_all",
)

#: Result text that means an edit happened. Checked LAST: a failure message may
#: legitimately contain the word "edited" while describing what did not happen,
#: so the failure markers get first refusal.
_APPLIED_MARKERS = (
    "has been updated",
    "successfully edited",
    "file updated",
    "applied edit",
    "the file has been created",
    "updated successfully",
)

#: Generic failure words, consulted only after the specific kinds miss.
_ERROR_MARKERS = ("error", "failed", "denied", "refused", "cannot", "invalid")


def classify(result_text: str, *, is_error: bool | None = None) -> str:
    """Classify one edit tool result. Total: always returns a member of OUTCOMES.

    ``is_error`` is the host's own error flag when it supplies one. It is used
    only to break a tie -- an unrecognised result that the host flagged as an
    error is ``other_error`` rather than ``unknown``, because "the host said
    this failed" is a fact we have even when the wording is new.
    """
    text = (result_text or "").lower()
    if not text.strip():
        return "other_error" if is_error else "unknown"
    for marker in _NOT_FOUND_MARKERS:
        if marker in text:
            return "not_found"
    for marker in _NOT_UNIQUE_MARKERS:
        if marker in text:
            return "not_unique"
    for marker in _APPLIED_MARKERS:
        if marker in text:
            return "applied"
    if is_error:
        return "other_error"
    for marker in _ERROR_MARKERS:
        if marker in text:
            return "other_error"
    # A result that names no outcome at all is most often a host that returns
    # the edited region on success and says nothing else. Calling that
    # "applied" would inflate the success rate on exactly the hosts whose
    # wording we have not learned yet, so it stays unknown and visible.
    return "unknown"


def _path_digest(path: str) -> str:
    """A stable, non-reversible handle for a path.

    The rate we are after is per-file-shaped ("do the same files keep failing")
    and that needs an identity, not a name. A digest keeps the ledger safe to
    paste into a receipt while still letting a summary count distinct files.
    """
    return hashlib.sha256(str(path).encode("utf-8", "replace")).hexdigest()[:12]


def append_edit_outcome(
    workspace_root: Path,
    *,
    tool: str,
    outcome: str,
    path: str | None = None,
    old_len: int = 0,
    new_len: int = 0,
    flavor: str = "",
) -> None:
    """Append one privacy-safe outcome row. Fail-open, never raises.

    Called from the hook's PostToolUse path, which has a latency contract: this
    does one small append and swallows every error, because a telemetry write
    must never be the reason an agent's edit appears to fail.
    """
    if outcome not in OUTCOMES:
        outcome = "unknown"
    try:
        row = {
            "schema": EDIT_OUTCOME_SCHEMA,
            "ts": int(time.time()),
            "tool": str(tool)[:64],
            "outcome": outcome,
            "flavor": str(flavor)[:32],
            "oldLen": max(0, int(old_len)),
            "newLen": max(0, int(new_len)),
        }
        if path:
            row["pathDigest"] = _path_digest(path)
        target = _ledger_path(workspace_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        return


def _ledger_path(workspace_root: Path) -> Path:
    """Beside the other per-session ledgers, which .gitignore already covers.

    Named directly rather than derived from a sibling: the first cut built this
    by taking another ledger's path and swapping the filename, which happened
    to land in the right directory and would have quietly followed that
    ledger's path anywhere it moved.
    """
    return session_reads_path(workspace_root, "edit-outcomes.jsonl")


def _rows(workspace_root: Path) -> Iterable[dict[str, Any]]:
    try:
        with _ledger_path(workspace_root).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    doc = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(doc, dict) and doc.get("schema") == EDIT_OUTCOME_SCHEMA:
                    yield doc
    except OSError:
        return


def edit_summary(workspace_root: Path) -> dict[str, Any]:
    """The measured edit-failure picture for this workspace.

    ``repairable_share`` is the fraction of *failures* that are ``not_found`` —
    the only failure kind a content-based repair can address without guessing.
    ``not_unique`` is excluded on purpose: several equally good matches is not
    a lookup problem, and picking one would be the harness inventing intent.

    Reported as counts alongside every rate, because a rate over eleven edits
    is not a rate and the reader has to be able to see that.
    """
    counts = {name: 0 for name in OUTCOMES}
    files: set[str] = set()
    flavors: set[str] = set()
    for row in _rows(workspace_root):
        counts[row.get("outcome", "unknown") if row.get("outcome") in counts else "unknown"] += 1
        digest = row.get("pathDigest")
        if isinstance(digest, str):
            files.add(digest)
        flavor = row.get("flavor")
        if isinstance(flavor, str) and flavor:
            flavors.add(flavor)
    total = sum(counts.values())
    failures = counts["not_found"] + counts["not_unique"] + counts["other_error"]
    return {
        "total": total,
        "counts": counts,
        "distinct_files": len(files),
        "hosts_reporting": sorted(flavors),
        "failure_rate": (failures / total) if total else 0.0,
        "failures": failures,
        "repairable_share": (counts["not_found"] / failures) if failures else 0.0,
    }


__all__ = [
    "EDIT_OUTCOME_SCHEMA",
    "OUTCOMES",
    "classify",
    "append_edit_outcome",
    "edit_summary",
]
