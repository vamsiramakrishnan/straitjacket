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

Every row also names the edit's **format** and the **model** that produced
it. Published edit benchmarks (Aider's format ladder, hashline, EDIT-Bench)
agree that the same model succeeds or fails on the *shape* of the edit far
more than folklore assumes -- a search/replace needle, a whole-file rewrite,
a unified patch and an anchored ``ctx edit`` span are different tasks -- and
that the ranking differs by model. A ledger that cannot split by those two
axes cannot say whether the anchored format straitjacket already ships
beats a host's native ``Edit`` for the model actually in use. The format is
derived from the tool name (a closed vocabulary; anything else is ``other``).
The model comes from ``CTX_MODEL``, which ``ctx orchestrate`` sets on every
host it launches; outside an orchestrated run the hook reads it from the
tail of the host's transcript when one is named, and ``unknown`` otherwise.
No row ever asserts a model it did not see named.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

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


#: The edit formats a row can name. Closed on purpose: a summary that groups
#: by free text groups by nothing. ``search_replace`` is a needle and a
#: replacement (Claude Code Edit/MultiEdit, str_replace_editor, Codex
#: replace_file_content); ``whole_file`` rewrites the file (Write, WriteFile,
#: create_file); ``patch`` is a unified-diff style block (apply_patch);
#: ``anchored`` is ``ctx edit apply`` -- a span address plus digest, resolved
#: by content, refused when ambiguous.
FORMATS = ("search_replace", "whole_file", "patch", "anchored", "other")

_FORMAT_EXACT = {
    "edit": "search_replace",
    "multiedit": "search_replace",
    "str_replace_editor": "search_replace",
    "str_replace_based_edit_tool": "search_replace",
    "replace_file_content": "search_replace",
    "replace_in_file": "search_replace",
    "write": "whole_file",
    "writefile": "whole_file",
    "write_file": "whole_file",
    "create_file": "whole_file",
    "apply_patch": "patch",
    "applypatch": "patch",
    "ctx edit apply": "anchored",
    "ctx_edit_apply": "anchored",
}

#: ``CTX_MODEL`` is the one name every launcher can set. ``ctx orchestrate``
#: does; a user running a host by hand can export it. Nothing else is
#: consulted before the transcript, so a stale unrelated variable cannot
#: label rows with a model that never ran.
MODEL_ENV = "CTX_MODEL"
_MODEL_CHARS = 64
#: How much of a transcript tail to read when no ``CTX_MODEL`` is set. A
#: bounded seek from the end, never the whole file: PostToolUse has a latency
#: contract and a long session's transcript can be tens of megabytes.
_TRANSCRIPT_TAIL_BYTES = 64 * 1024
_TRANSCRIPT_MODEL = re.compile(r'"model"\s*:\s*"([A-Za-z0-9][A-Za-z0-9._:/-]{1,80})"')


def edit_format(tool: str) -> str:
    """Name the format an edit tool speaks. Total: always a member of FORMATS."""
    raw = str(tool or "").strip()
    exact = _FORMAT_EXACT.get(raw.lower())
    if exact:
        return exact
    # Split camelCase before lowercasing, the way hook._tool_words does, so
    # NotebookEdit is ("notebook", "edit") and not one unrecognised word.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", spaced)]
    if "patch" in words:
        return "patch"
    if "write" in words or "create" in words:
        return "whole_file"
    if "replace" in words or "edit" in words:
        return "search_replace"
    return "other"


def resolve_model(
    env: Mapping[str, str] | None = None, transcript_path: str | None = None
) -> str:
    """The model behind an edit, or ``unknown``. Never raises.

    Order: the ``CTX_MODEL`` variable a launcher set for this host process,
    then the last ``"model"`` named in the tail of the transcript the hook
    payload pointed at (Claude Code writes one per assistant message), then
    ``unknown``. A row labelled ``unknown`` is counted, and the summary says
    how many there are, because a per-model table that silently dropped the
    rows it could not label would overstate its own coverage.
    """
    source = os.environ if env is None else env
    try:
        named = str(source.get(MODEL_ENV, "") or "").strip()
    except Exception:
        named = ""
    if named:
        return named[:_MODEL_CHARS]
    if transcript_path:
        try:
            with open(transcript_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
                tail = handle.read(_TRANSCRIPT_TAIL_BYTES).decode("utf-8", "replace")
            hits = _TRANSCRIPT_MODEL.findall(tail)
            if hits:
                return hits[-1][:_MODEL_CHARS]
        except Exception:
            pass
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
    model: str = "",
    fmt: str = "",
) -> None:
    """Append one privacy-safe outcome row. Fail-open, never raises.

    Called from the hook's PostToolUse path, which has a latency contract: this
    does one small append and swallows every error, because a telemetry write
    must never be the reason an agent's edit appears to fail.

    ``model`` defaults to ``unknown`` when empty; ``fmt`` defaults to the
    format the tool name implies (see ``edit_format``). Both are closed at
    the summary: an unlisted format is ``other``.
    """
    if outcome not in OUTCOMES:
        outcome = "unknown"
    try:
        fmt = str(fmt or "").strip() or edit_format(tool)
        if fmt not in FORMATS:
            fmt = "other"
        row = {
            "schema": EDIT_OUTCOME_SCHEMA,
            "ts": int(time.time()),
            "tool": str(tool)[:64],
            "outcome": outcome,
            "flavor": str(flavor)[:32],
            "model": (str(model or "").strip() or "unknown")[:_MODEL_CHARS],
            "format": fmt,
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


def _new_counts() -> dict[str, int]:
    return {name: 0 for name in OUTCOMES}


def _cell(counts: dict[str, int]) -> dict[str, Any]:
    """One (model, format) cell: counts plus the two rates a reader compares.

    ``success_rate`` is applied over *classified* rows -- ``unknown`` is left
    out of the denominator because it is a wording we have not learned, not
    an outcome -- and ``unknown`` is reported beside it so the reader can see
    how much of the cell the rate is silent about.
    """
    total = sum(counts.values())
    classified = total - counts["unknown"]
    failures = counts["not_found"] + counts["not_unique"] + counts["other_error"]
    return {
        "total": total,
        "counts": dict(counts),
        "classified": classified,
        "failures": failures,
        "success_rate": (counts["applied"] / classified) if classified else 0.0,
        "failure_rate": (failures / total) if total else 0.0,
    }


def summarize_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The edit-failure picture over any row sequence (a ledger, a fixture).

    ``repairable_share`` is the fraction of *failures* that are ``not_found`` —
    the only failure kind a content-based repair can address without guessing.
    ``not_unique`` is excluded on purpose: several equally good matches is not
    a lookup problem, and picking one would be the harness inventing intent.

    ``by_model`` splits the same counts by (model, format), which is the one
    table the edit-format question needs: for the model actually in use, did
    the anchored format land more often than the host's native one? Rows from
    before the two fields existed are folded into ``unknown`` / the format
    the tool name implies, so an old ledger still summarizes.

    Reported as counts alongside every rate, because a rate over eleven edits
    is not a rate and the reader has to be able to see that.
    """
    counts = _new_counts()
    files: set[str] = set()
    flavors: set[str] = set()
    cells: dict[str, dict[str, dict[str, int]]] = {}
    for row in rows:
        outcome = row.get("outcome")
        outcome = outcome if outcome in counts else "unknown"
        counts[outcome] += 1
        digest = row.get("pathDigest")
        if isinstance(digest, str):
            files.add(digest)
        flavor = row.get("flavor")
        if isinstance(flavor, str) and flavor:
            flavors.add(flavor)
        model = row.get("model")
        model = model if isinstance(model, str) and model else "unknown"
        fmt = row.get("format")
        if not (isinstance(fmt, str) and fmt in FORMATS):
            fmt = edit_format(str(row.get("tool") or ""))
        cells.setdefault(model, {}).setdefault(fmt, _new_counts())[outcome] += 1
    total = sum(counts.values())
    failures = counts["not_found"] + counts["not_unique"] + counts["other_error"]
    by_model = {
        model: {fmt: _cell(c) for fmt, c in sorted(fmts.items())}
        for model, fmts in sorted(cells.items())
    }
    return {
        "total": total,
        "counts": counts,
        "distinct_files": len(files),
        "hosts_reporting": sorted(flavors),
        "models_reporting": sorted(m for m in cells if m != "unknown"),
        "unlabelled_model_rows": sum(
            sum(c.values()) for c in cells.get("unknown", {}).values()
        ),
        "failure_rate": (failures / total) if total else 0.0,
        "failures": failures,
        "repairable_share": (counts["not_found"] / failures) if failures else 0.0,
        "by_model": by_model,
    }


def edit_summary(workspace_root: Path) -> dict[str, Any]:
    """``summarize_rows`` over this workspace's ledger."""
    return summarize_rows(_rows(workspace_root))


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Read one ledger file directly (for the replay eval). Missing → []."""
    rows: list[dict[str, Any]] = []
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    doc = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(doc, dict) and doc.get("schema") == EDIT_OUTCOME_SCHEMA:
                    rows.append(doc)
    except OSError:
        return []
    return rows


__all__ = [
    "EDIT_OUTCOME_SCHEMA",
    "FORMATS",
    "MODEL_ENV",
    "OUTCOMES",
    "classify",
    "edit_format",
    "resolve_model",
    "append_edit_outcome",
    "edit_summary",
    "summarize_rows",
    "load_rows",
]


def refusal_outcome(reason: str) -> str:
    """Map an apply refusal onto the edit-outcome vocabulary.

    The anchored format has the same two addressable failures a needle has --
    the target moved or vanished (``not_found``) and the target now has more
    than one equally good copy (``not_unique``) -- and every other refusal
    (stale plan, overlap, a file that changed mid-commit) is ``other_error``.
    """
    text = reason.lower()
    if "changed or disappeared" in text:
        return "not_found"
    if "ambiguous" in text:
        return "not_unique"
    return "other_error"


def record_anchored(ws, plan, *, outcome: str, receipt=None) -> None:
    """One ledger row per planned file, beside the host's own Edit/Write rows.

    Same ledger, same vocabulary, format ``anchored``: this is what lets a
    summary compare the anchored format against the host's native one for
    the model in use. Model comes from ``CTX_MODEL`` the same way the hook
    finds it. Fail-open like every telemetry write.
    """
    try:
        edits = plan.get("edits") if isinstance(plan, dict) else None
        if not isinstance(edits, list):
            return
        by_path: dict[str, dict[str, int]] = {}
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            rel = str(edit.get("path") or "")
            sizes = by_path.setdefault(rel, {"old": 0, "new": 0})
            sizes["new"] += len(str(edit.get("replacement") or "").encode("utf-8"))
        # The bytes the plan replaced, per file, recovered from the receipt's
        # before/after sizes so the row's oldLen means what a native row's
        # does (the region the edit targeted, not the whole file). A refusal
        # has no receipt and records 0.
        replaced: dict[str, int] = {}
        if isinstance(receipt, dict):
            for item in receipt.get("files") or []:
                if isinstance(item, dict):
                    rel = str(item.get("path"))
                    delta = int(item.get("bytesBefore") or 0) - int(item.get("bytesAfter") or 0)
                    replaced[rel] = max(0, delta + by_path.get(rel, {}).get("new", 0))
        model = resolve_model()
        for rel, sizes in by_path.items():
            append_edit_outcome(
                ws.root,
                tool="ctx edit apply",
                outcome=outcome,
                path=rel or None,
                old_len=replaced.get(rel, 0),
                new_len=sizes["new"],
                flavor="ctx",
                model=model,
                fmt="anchored",
            )
    except Exception:
        return

