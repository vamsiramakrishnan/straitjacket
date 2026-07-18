"""The reflex arc (docs/REFLEX.md layers 1-3): deterministic behavioral
detectors, an append-only outcome ledger, and the densify-on-starvation
latch.

Measured motivation (evals/spec3-haiku-2026-07-18.md): the same pytest
command re-issued 8x with slicing decorations (`| head`, `| tail`,
`--tb=`, `| grep`) after each digest, 16 retrieval hints emitted, 0
followed — mechanically detectable, counted by nothing. This module is the
counter. Every intervention (a run digest with omissions) is a hypothesis:
"the model will use the digest instead of re-running." The reflex arc
scores that hypothesis against the *next* command and adapts on the axis
the evidence names: a re-run of the same signature is a **starvation**
event, and the signature's next digest densifies (full evidence inline).

Discipline (same as ctx.engagement / the adoption ledger in ctx.hook):
stdlib-only, every entry point fail-open — broken reflex state must never
change a guard decision or break a digest. Reflex state influences only
*which* deterministic rendering is chosen (declared in the printed header);
digest content identity never depends on reflex state.

State: ``<workspace>/.ctx-session-reads/reflex.json`` —

    {"interventions": {signature: {"count": int, "last_run": "<short id>",
                                   "hints": int, "starved": bool}},
     "densify": {signature: true},
     "outcomes_appended": int}

(``starved`` is operational dedup state: it marks that a starvation event
has been appended since the signature's last intervention, so the hook and
``ctx run`` observing the *same* physical re-run append one event, not
two. Writes are atomic: temp file + rename.) Session scoping comes from
the ledger-dir lifecycle, exactly like engagement state.

Outcome ledger: append-only ``.ctx-session-reads/reflex-outcomes.jsonl``,
one JSON object per line, keys sorted. FROZEN schema (the scorecard reader
is built against it):

    {"ts": <float epoch>,
     "event": "starvation" | "landing" | "friction",
     "signature": "<command signature>",
     "run": "<short run id or null>",
     "action": "densify" | "none"}

``ts`` is operational only — reflex state and the ledger minus ``ts`` are
a pure function of the session's command sequence (determinism contract).
"friction" is reserved in the schema for the deny→verbatim-retry detector
(a later wave); nothing emits it yet.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

_LEDGER_DIR = ".ctx-session-reads"
_STATE_NAME = "reflex.json"
_OUTCOMES_NAME = "reflex-outcomes.jsonl"

DENSIFY_HEADER = "densified: re-run detected · full evidence inline"

# ------------------------------------------------------------- signatures
#
# Base rule mirrors ctx.policy.command_signature (program basename plus the
# bare-word subcommand) — deliberately NOT imported: ctx.policy pulls in the
# store, and signature computation runs on the PreToolUse hot path. The
# reflex signature extends the base with the remaining positional arguments
# so ``pytest tests/x.py`` and ``pytest tests/y.py`` stay distinct, and
# normalizes away the slicing decorations the spec3 loop cycled through:
#
#   * a trailing ``2>&1`` (and ``2>/dev/null`` / ``>/dev/null``) tail;
#   * a trailing pipe segment whose program is a slicer
#     (head/tail/grep/sed/awk/...) — applied repeatedly, so
#     ``pytest x 2>&1 | grep FAIL | head -5`` collapses too;
#   * every ``-``-prefixed token (flag noise: ``--tb=short``, ``-x``, ``-v``);
#   * wrapper programs (env/timeout/...), ``python -m <module>``,
#     ``bash -c '<inner>'``, and ``ctx run [--shell] -- <cmd>`` unwrapping,
#     so a guard-rewritten re-run keeps the signature of the raw command.

_SLICER_PROGS = {
    "head", "tail", "grep", "egrep", "fgrep", "rg",
    "sed", "awk", "wc", "cut", "sort", "uniq", "less", "more", "cat",
}
_REDIR_TAIL_TOKENS = {"2>&1", "2>/dev/null", ">/dev/null", "&>/dev/null"}
_META_TOKENS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "2>&1"}
_PY_PROG_RE = re.compile(r"^python(3(\.\d+)?)?$")
_MAX_SIGNATURE_CHARS = 240


def _strip_slicer_tokens(argv: list[str]) -> list[str]:
    """Drop trailing stderr-merge redirections and trailing pipe segments
    whose program is a slicer, repeatedly, at token level (quote-safe):
    ``pytest x --tb=short 2>&1 | tail -50`` → ``pytest x --tb=short``."""
    argv = list(argv)
    changed = True
    while changed and argv:
        changed = False
        while argv and argv[-1] in _REDIR_TAIL_TOKENS:
            argv.pop()
            changed = True
        if "|" in argv:
            i = len(argv) - 1 - argv[::-1].index("|")
            seg = argv[i + 1 :]
            if seg and os.path.basename(seg[0]) in _SLICER_PROGS:
                argv = argv[:i]
                changed = True
    return argv


def command_signature(command: str, _depth: int = 0) -> str | None:
    """Normalized behavioral signature of a shell command string, or None
    when the command has no signature worth tracking (empty, pure ctx
    retrieval verbs). Never raises."""
    try:
        if not isinstance(command, str) or _depth > 3:
            return None
        s = command.strip()
        if not s:
            return None
        try:
            argv = shlex.split(s)
        except ValueError:
            argv = s.split()
        argv = [str(t) for t in argv]
        try:
            from ctx.hook import _unwrap

            argv = _unwrap(argv)
        except Exception:
            pass
        argv = _strip_slicer_tokens(argv)
        if not argv:
            return None
        prog = os.path.basename(str(argv[0]))
        # `python -m module ...` → the module is the real program.
        if _PY_PROG_RE.match(prog) and len(argv) >= 3 and argv[1] == "-m":
            argv = argv[2:]
            prog = os.path.basename(str(argv[0]))
        # `bash -c '<inner>'` → classify the inner command.
        if prog in ("bash", "sh", "zsh", "dash", "fish") and len(argv) >= 3 and argv[1] == "-c":
            return command_signature(str(argv[2]), _depth + 1)
        # ctx: only `ctx run` carries an underlying command; retrieval verbs
        # (get/search/stats/...) never accrue re-run signatures.
        if prog == "ctx":
            sub = str(argv[1]) if len(argv) > 1 else ""
            if sub != "run":
                return None
            rest = list(argv[2:])
            shell = "--shell" in rest
            if "--" in rest:
                rest = rest[rest.index("--") + 1 :]
            else:
                rest = [t for t in rest if not str(t).startswith("-")]
            if not rest:
                return None
            if shell:
                return command_signature(str(rest[0]), _depth + 1)
            return command_signature(shlex.join([str(t) for t in rest]), _depth + 1)
        parts = [prog] + [
            str(t)
            for t in argv[1:]
            if not str(t).startswith("-") and str(t) not in _META_TOKENS
        ]
        sig = " ".join(" ".join(parts).split())
        return sig[:_MAX_SIGNATURE_CHARS] or None
    except Exception:
        return None


def signature_of_argv(argv: list[str]) -> str | None:
    """Signature of an argv-form command (``ctx run -- <argv>``)."""
    try:
        return command_signature(shlex.join([str(a) for a in argv]))
    except Exception:
        return None


_LANDING_REF_RE = re.compile(r"run:([0-9a-fA-F]{4,64})")


def landing_ref(command: str) -> str | None:
    """If ``command`` is a ``ctx get`` / ``ctx search`` targeting a
    ``run:<id>`` handle, return that handle string; else None."""
    try:
        if not isinstance(command, str) or "run:" not in command:
            return None
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        try:
            from ctx.hook import _unwrap

            argv = _unwrap(argv)
        except Exception:
            pass
        if not argv or os.path.basename(str(argv[0])) != "ctx":
            return None
        sub = next((str(a) for a in argv[1:] if not str(a).startswith("-")), "")
        if sub not in ("get", "search"):
            return None
        for tok in argv[1:]:
            m = _LANDING_REF_RE.search(str(tok))
            if m:
                return "run:" + m.group(1)
        return None
    except Exception:
        return None


# ------------------------------------------------------------------ state


def _state_path(ws_root: Path | str) -> Path:
    return Path(ws_root) / _LEDGER_DIR / _STATE_NAME


def read_state(ws_root: Path | str | None) -> dict[str, Any]:
    """Fail-open read of the reflex state blob."""
    if ws_root is None:
        return {}
    try:
        state = json.loads(_state_path(ws_root).read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_state(ws_root: Path | str, state: dict[str, Any]) -> None:
    """Atomic write: temp file in the ledger dir + rename. Raises to the
    caller (every caller is fail-open)."""
    path = _state_path(ws_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=_STATE_NAME + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalized(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state.get("interventions"), dict):
        state["interventions"] = {}
    if not isinstance(state.get("densify"), dict):
        state["densify"] = {}
    if not isinstance(state.get("outcomes_appended"), int):
        state["outcomes_appended"] = 0
    return state


def _append_outcome(
    ws_root: Path | str,
    event: str,
    signature: str,
    run: str | None,
    action: str,
) -> None:
    """Append one FROZEN-schema line to the outcome ledger. Raises to the
    caller (every caller is fail-open). ``ts`` is operational only."""
    path = Path(ws_root) / _LEDGER_DIR / _OUTCOMES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "ts": time.time(),
            "event": event,
            "signature": signature,
            "run": run,
            "action": action,
        },
        sort_keys=True,
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# -------------------------------------------------------------------- API


def note_intervention(
    ws_root: Path | str | None,
    signature: str | None,
    run_short_id: str | None,
    *,
    hints: int = 0,
) -> None:
    """Record that a run digest with omissions was emitted for ``signature``
    (the intervention whose hypothesis is "no re-run follows"). Resets the
    starvation-dedup flag so the *next* re-run scores a fresh starvation
    event. Fail-open: any IO problem records nothing."""
    if ws_root is None or not signature:
        return
    try:
        state = _normalized(read_state(ws_root))
        rec = state["interventions"].get(signature)
        if not isinstance(rec, dict):
            rec = {"count": 0, "last_run": None, "hints": 0}
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last_run"] = str(run_short_id) if run_short_id else None
        rec["hints"] = int(rec.get("hints") or 0) + max(0, int(hints))
        rec["starved"] = False
        state["interventions"][signature] = rec
        _write_state(ws_root, state)
    except Exception:
        pass


def check_command(ws_root: Path | str | None, command: str) -> str | None:
    """Score ``command`` against this session's recorded interventions.

    A command whose signature already has an intervention recorded is the
    starvation detector firing (the spec3 re-run loop, caught at occurrence
    2): append one "starvation" outcome (deduped per intervention cycle so
    the hook and ``ctx run`` seeing the same re-run count it once), latch
    densify for the signature (latching: once on, stays on all session),
    and return "densify". Returns None otherwise. The return value NEVER
    changes a guard decision — reflexes act through rendering. Fail-open."""
    if ws_root is None:
        return None
    try:
        sig = command_signature(command)
        if not sig:
            return None
        state = _normalized(read_state(ws_root))
        rec = state["interventions"].get(sig)
        if not isinstance(rec, dict):
            return None
        fresh = not bool(rec.get("starved"))
        rec["starved"] = True
        state["densify"][sig] = True
        if fresh:
            state["outcomes_appended"] = int(state.get("outcomes_appended") or 0) + 1
        _write_state(ws_root, state)
        if fresh:
            _append_outcome(
                ws_root, "starvation", sig, rec.get("last_run") or None, "densify"
            )
        return "densify"
    except Exception:
        return None


def note_landing(ws_root: Path | str | None, ref_or_handle: str) -> None:
    """A ``ctx get``/``ctx search`` targeted a run handle. If the handle is
    one this session's interventions minted, append a "landing" outcome —
    the positive class (the hint was followed). Landings clear nothing;
    they are data for the slow loop. Fail-open."""
    if ws_root is None or not ref_or_handle:
        return
    try:
        m = _LANDING_REF_RE.search(str(ref_or_handle))
        if not m:
            return
        target = m.group(1).lower()
        state = _normalized(read_state(ws_root))
        for sig in sorted(state["interventions"]):
            rec = state["interventions"][sig]
            if not isinstance(rec, dict):
                continue
            known = str(rec.get("last_run") or "").lower()
            if not known:
                continue
            if known.startswith(target) or target.startswith(known):
                state["outcomes_appended"] = (
                    int(state.get("outcomes_appended") or 0) + 1
                )
                _write_state(ws_root, state)
                _append_outcome(ws_root, "landing", sig, known, "none")
                return
    except Exception:
        pass


def densify_latched(ws_root: Path | str | None, signature: str | None) -> bool:
    """Read the densify latch for ``signature``. Fail-open to False."""
    if ws_root is None or not signature:
        return False
    try:
        return bool(_normalized(read_state(ws_root))["densify"].get(signature))
    except Exception:
        return False


# -------------------------------------------------------------- heuristics

_ZERO_OMITTED_RE = re.compile(r"omitted: 0 lines")


def has_omissions(digest_text: str) -> bool:
    """True when a digest declares omitted content (coverage lines with a
    nonzero count, or an ``… omitted <stream>:LA-LB`` marker). A digest
    without omissions is complete — re-running after it is not starvation,
    so it records no intervention."""
    try:
        return "omitted" in _ZERO_OMITTED_RE.sub("", digest_text)
    except Exception:
        return False


def count_hints(digest_text: str) -> int:
    """Number of ``next:`` suggestion lines a digest carries (intervention
    telemetry: hints emitted, the denominator of hint follow-through)."""
    try:
        n = 0
        in_next = False
        for line in digest_text.splitlines():
            if line == "next:":
                in_next = True
                continue
            if in_next and line.startswith("  ") and line.strip():
                n += 1
                continue
            in_next = False
        return n
    except Exception:
        return 0
