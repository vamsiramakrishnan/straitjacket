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
                                   "hints": int, "starved": bool,
                                   "armed": bool,
                                   "generation": "<hash or null>",   # shadow
                                   "iid": "<intervention id>"}},     # shadow
     "densify": {signature: true},
     "outcomes_appended": int,
     "seq": int,            # intervention sequence (deterministic v2 ids)
     "commands": int,       # tool-bearing command counter (hypothesis windows)
     "open": {iid: {"signature", "family", "generation", "opened_at"}},
     "circuit_shadow": {signature: {"state", "episode", "positives",
                                    "transitioned_in_episode",
                                    "starved_in_episode"}},
     "q_dry": {q_signature: {"dry": bool}},  # q-dry ledger fold state
     "q_ops": int}          # q-dry op-line cursor (lines consumed once)

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

--------------------------------------------------------------------------
``ctx q`` visibility (the ALGEBRA live-A/B gap, evals/spec3-haiku-
2026-07-18.md addendum): the taught arm re-ran an identical dry
``ctx q 'fails last | in-changed'`` three times and the reflex saw
nothing — ctx verbs other than ``run`` had no signature. Now:

* ``command_signature("ctx q '<pipeline>'")`` → ``"q <normalized
  pipeline>"`` — shlex-flattened (quoting variance collapses), whitespace
  collapsed, ``--trace`` stripped (presentation-only). Stage names AND
  their args are KEPT: they are the semantics (no flag-stripping beyond
  ``--trace``).
* Retrieval purity: ``q`` is a READ verb, so a repeated q is NOT
  starvation-after-intervention in the EDC §8 sense. It is its own event
  class, fed EXCLUSIVELY by the q-dry ledger the query engine writes
  (``ctx.query`` self-healing wave) — read fail-open; ledger absent means
  nothing is scored and nothing is written, never guessed:

      ``.ctx-session-reads/q-dry.json``   — state: {"dry": [<raw
          pipeline text>, ...]} — the engine's last-N 0-row pipelines;
          a pipeline leaving the list after a non-empty result is the
          recovery signal. (The provisional {"pipelines": {"<q
          signature>": {"rows": <int>}}} mapping shape is tolerated.)
      ``.ctx-session-reads/q-dry.jsonl``  — op lines: {"op":
          "q_dry_rerun", "pipeline": "<raw pipeline text>", "ts":
          <float>} — one per identical dry re-issue. ("signature" and
          "rows" keys are tolerated variants.)

  Raw pipeline texts normalize through :func:`query_signature` so ledger
  identity and hook-sighted command identity agree. Reflex folds the
  ledger (cursor ``q_ops``, dryness map ``q_dry`` in reflex state) into
  ADDITIVE schema-v2 events on ``interventions.jsonl`` (scorecard
  readers skip unknown events by design):

      {"schema": "ctx.q/v1", "event": "dry_query_rerun",
       "signature": "<q signature>", "rows": 0, "ts": <float>}
          — an identical q re-issued after a 0-row result this session
            (the live-A/B "3 identical dry joins" loop, now counted);
      {"schema": "ctx.q/v1", "event": "recovered",
       "signature": "<q signature>", "rows": <int > 0 | null>,
       "ts": <float>}
          — a q pipeline that returns rows following a prior dry
            identical pipeline: the landing extension (the teaching
            worked). ``rows`` is null when the ledger shape carries no
            row count (the dry-list recovery signal).

  Ledger-driven ONLY (no sighting heuristics): the engine sees execution
  and priors, so one physical rerun folds to one event — the asymmetric
  loss prior prefers a missed event over a double-counted one. Caveat,
  declared: the engine's dry list is a ring (last N); eviction of a
  still-dry pipeline can masquerade as recovery once N+1 distinct dry
  pipelines accumulate — rare, positive-class only, accepted.

--------------------------------------------------------------------------
Controller State wave (EDC §7–§10 + phase 6b) — everything below ships in
SHADOW MODE: the new detectors and the circuit state machine RECORD, they
never change behavior. The v2 live loop above (event-armed starvation →
densify latch → dense rendering) is byte-for-byte unchanged; rendering is
still driven exclusively by the ``densify`` latch. Shadow output validates
against the archived spec3 transcripts (``evals/replay_detectors.py``)
before anything graduates to live.

v2 intervention ledger: append-only
``.ctx-session-reads/interventions.jsonl`` — DUAL-WRITTEN alongside the v1
ledger above (v1 lines are frozen and unchanged). FROZEN schemas, built
against the scorecard v2 reader (``ctx.scorecard._interventions_v2``):

    emission (ctx.intervention/v1):
      {"schema": "ctx.intervention/v1", "event": "intervention_emitted",
       "interventionId": sha256("<sessionSeq>|<signature>")[:12],
       "sessionSeq": int, "family": str, "signature": str,
       "generation": "<generation hash or null>",
       "artifact": "<run short id or null>", "planId": "<id or null>",
       "planMode": "normal|dense|bypass", "coverage": {...}, "hints": int,
       "ts": float}

    outcome (ctx.intervention-outcome/v1):
      {"schema": "ctx.intervention-outcome/v1",
       "event": "intervention_outcome", "interventionId": str,
       "outcome": "<scorecard v2 vocabulary>", "evidence": {...},
       "ts": float}

    circuit transition (shadow, ctx.circuit/v1):
      {"schema": "ctx.circuit/v1", "event": "circuit_transition",
       "family", "signature", "generation", "from", "to", "shadow": true,
       "ts": float}

Hypothesis windows (EDC §9): each emission opens a window; it resolves on
the first classified next action (rerun / narrowing / landing) or expires
after 3 subsequent tool-bearing commands or on a generation change —
"expired_unresolved" is a CENSORED observation, excluded from every rate
denominator by the scorecard.

Generations (EDC §8): rerun classification = signature relation ×
generation equality. ``ctx.execution.generation_hash`` is computed LAZILY,
only at scoring moments. Equal hash → starvation CONFIRMED even if
event-disarmed (the ``sed -i`` blind spot); different hash → verification
regardless of arming; unknown → provisional, classified by the armed bit
and marked ``confirmed: false``.

Steering shadow (EDC phase 6b): ``note_steer_shadow`` records, per
would-be command rewrite, whether the graduated-steering null plan WOULD
have bypassed it — ``.ctx-session-reads/steering-shadow.jsonl`` lines
follow the adoption-ledger pattern. No rewrite behavior changes this wave.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time

# Hot path: hook.py imports this module on every intercepted command, so
# `pathlib` (~4.7 ms) and `typing` (~4.3 ms) are kept out of module scope.
# Path handling here is joins plus existence checks, which `os.path` does
# more cheaply (and it accepts the `Path` values callers still pass, via the
# os.PathLike protocol); `from __future__ import annotations` makes `Any` and
# `Path` annotation-only names that are never evaluated at runtime. Spelled
# with a local constant rather than `from typing import TYPE_CHECKING`, since
# that import would pull in the module being avoided.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

_LEDGER_DIR = ".ctx-session-reads"
_STATE_NAME = "reflex.json"
_OUTCOMES_NAME = "reflex-outcomes.jsonl"
_INTERVENTIONS_NAME = "interventions.jsonl"  # v2 ledger (EDC §9, shadow wave)
_STEER_SHADOW_NAME = "steering-shadow.jsonl"  # phase 6b shadow ledger
_Q_DRY_STATE_NAME = "q-dry.json"  # q-dry ledger state (query engine writes)
_Q_DRY_OPS_NAME = "q-dry.jsonl"  # q-dry ledger op lines (query engine writes)

DENSIFY_HEADER = "densified: re-run detected · full evidence inline"

# Hypothesis-window bound (EDC §9): an unresolved intervention expires after
# this many subsequent tool-bearing commands (or on a generation change).
HYPOTHESIS_WINDOW_COMMANDS = 3

# Circuit hysteresis defaults (EDC §10, epoch-tunable later): downward
# transitions are EARNED — bypass→dense after 2 positive outcomes,
# dense→normal after 3. Upward transitions fire on confirmed starvation,
# at most one transition per (signature × generation) episode.
CIRCUIT_BYPASS_TO_DENSE_POSITIVES = 2
CIRCUIT_DENSE_TO_NORMAL_POSITIVES = 3

# Positive-outcome classes that feed the hysteresis counter.
_CIRCUIT_POSITIVE_OUTCOMES = {
    "retrieval_landing",
    "narrowed_execution",
    "validation_after_edit",
    "progressed_without_retrieval",
}

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

# Per-family signature tables (EDC §7 defect, debt 748f470aa1): v1 stripped
# ALL flags as presentation noise — including scope-affecting ones, so
# ``pytest -k auth`` equalled bare ``pytest`` and a legitimate scope change
# could score as starvation. SCOPE flags (they change WHICH tests run) are
# kept in the signature, value included; presentation flags (-v, --tb=, -x,
# -q, ...) stay dropped exactly as before. Breadth is data: adding a family
# is a table row, not code.
_FAMILY_SCOPE_FLAGS: dict[str, dict[str, set[str]]] = {
    "pytest": {
        # flags that take a value (space- or =-separated)
        "valued": {"-k", "-m", "--deselect"},
        # bare flags (long/short spellings normalized as written)
        "bare": {"--lf", "--last-failed", "--ff", "--failed-first"},
    },
}
# Program spellings mapped onto their family table.
_FAMILY_OF_PROG = {"pytest": "pytest", "py.test": "pytest"}


def family_of(signature: str | None) -> str:
    """Command family of a signature (its program token, spelling-normalized).
    Used for the v2 intervention ledger's ``family`` field. Never raises."""
    try:
        if not signature:
            return "unknown"
        prog = str(signature).split()[0]
        return _FAMILY_OF_PROG.get(prog, prog)
    except Exception:
        return "unknown"


def _signature_parts(prog: str, rest: list[str]) -> list[str]:
    """Positional + kept-scope-flag tokens for the signature, applying the
    program's family table. Presentation flags are dropped (v1 behavior);
    scope flags are kept WITH their values. Normalizations so equivalent
    spellings share one signature: ``--flag=v`` → ``--flag v``, and scope
    flags sort AFTER the positional targets regardless of where they
    appeared (``pytest --lf tests/x.py`` == ``pytest tests/x.py --lf``)."""
    table = _FAMILY_SCOPE_FLAGS.get(_FAMILY_OF_PROG.get(prog, ""))
    targets: list[str] = []
    scope: list[str] = []
    i = 0
    while i < len(rest):
        t = str(rest[i])
        if table is not None and t.startswith("-"):
            if t in table["bare"]:
                scope.append(t)
                i += 1
                continue
            if t in table["valued"]:
                if i + 1 < len(rest):
                    scope.append(f"{t} {rest[i + 1]}")
                    i += 2
                else:
                    scope.append(t)
                    i += 1
                continue
            matched = next(
                (f for f in sorted(table["valued"]) if t.startswith(f + "=")),
                None,
            )
            if matched is not None:
                scope.append(f"{matched} {t[len(matched) + 1 :]}")
                i += 1
                continue
        if not t.startswith("-") and t not in _META_TOKENS:
            targets.append(t)
        i += 1
    return [prog] + targets + sorted(scope)


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


def _q_signature(rest: list[str]) -> str | None:
    """``"q <normalized pipeline>"`` for a ``ctx q`` invocation (``rest`` =
    argv after the ``q`` verb). Normalization: shlex-flatten every token
    (quoting variance collapses — ``'fails last | in-changed'`` and
    ``"fails  last |  in-changed"`` are one signature), drop ``--trace``
    (presentation-only), collapse whitespace. Stage names and their args
    are KEPT — they are the semantics. Empty pipeline → None."""
    toks: list[str] = []
    for t in rest:
        s = str(t)
        try:
            parts = shlex.split(s)
        except ValueError:
            parts = s.split()
        toks.extend(parts)
    toks = [t for t in toks if t != "--trace"]
    if not toks:
        return None
    sig = "q " + " ".join(" ".join(toks).split())
    return sig[:_MAX_SIGNATURE_CHARS]


def query_signature(pipeline_text: str) -> str | None:
    """Signature of a raw q pipeline text (the cli's ``ns.query``) —
    exactly what ``command_signature`` yields for ``ctx q '<pipeline>'``.
    The q-dry ledger writer uses this so ledger keys and hook-side
    signatures agree byte-for-byte. Never raises."""
    try:
        if not isinstance(pipeline_text, str):
            return None
        return _q_signature([pipeline_text])
    except Exception:
        return None


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
        # ctx: `ctx run` carries an underlying command, and `ctx q` carries
        # a pipeline whose identity IS the signature (the ALGEBRA live-A/B
        # gap: 3 identical dry q reruns were invisible). Other retrieval
        # verbs (get/search/stats/...) never accrue re-run signatures.
        if prog == "ctx":
            sub = str(argv[1]) if len(argv) > 1 else ""
            if sub == "q":
                return _q_signature(argv[2:])
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
        parts = _signature_parts(prog, [str(t) for t in argv[1:]])
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


def _split_signature(sig: str) -> tuple[str, list[str], set[tuple[str, str]]]:
    """(program, positional targets, scope-constraint pairs) of a signature.
    Signatures are already normalized (``--flag=v`` → ``--flag v``)."""
    toks = sig.split()
    prog, rest = toks[0], toks[1:]
    table = _FAMILY_SCOPE_FLAGS.get(_FAMILY_OF_PROG.get(prog, ""), {})
    valued = table.get("valued", set())
    bare = table.get("bare", set())
    targets: list[str] = []
    scope: set[tuple[str, str]] = set()
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in valued and i + 1 < len(rest):
            scope.add((t, rest[i + 1]))
            i += 2
            continue
        if t in bare:
            scope.add((t, ""))
            i += 1
            continue
        targets.append(t)
        i += 1
    return prog, targets, scope


def _target_contains(broad: str, narrow: str) -> bool:
    """pytest target containment: ``tests/x.py::TestC::test_t`` is contained
    by ``tests/x.py::TestC``, ``tests/x.py``, and ``tests`` (path prefix)."""
    if broad == narrow:
        return True
    if narrow.startswith(broad + "::"):
        return True
    if "::" in broad:
        return False
    narrow_path = narrow.split("::", 1)[0]
    return narrow_path == broad or narrow_path.startswith(broad.rstrip("/") + "/")


def is_narrower(a: str | None, b: str | None) -> bool:
    """True when signature ``a`` is a MATERIALLY NARROWER execution of ``b``
    (EDC §7 relation algebra, pytest-only v1): every target of ``a`` is
    contained by some target of ``b`` (node-id / path prefix containment; a
    bare ``pytest`` contains everything), ``a`` carries at least ``b``'s
    scope constraints, and the scope is STRICTLY reduced (proper target
    containment or extra scope flags). A narrower rerun after an
    intervention is census consumption (Rule 9b, ``narrowed_execution``) —
    never starvation. Pure function; never raises."""
    try:
        if not a or not b or a == b:
            return False
        prog_a, targets_a, scope_a = _split_signature(str(a))
        prog_b, targets_b, scope_b = _split_signature(str(b))
        if prog_a != prog_b or _FAMILY_OF_PROG.get(prog_a) != "pytest":
            return False
        if targets_b:
            if not targets_a:
                return False  # bare pytest is BROADER than any targeted run
            for t in targets_a:
                if not any(_target_contains(bt, t) for bt in targets_b):
                    return False
        # b's scope constraints must all survive in a (otherwise a broadens
        # on the flag axis while narrowing on the path axis: incomparable).
        if not scope_b.issubset(scope_a):
            return False
        if sorted(targets_a) != sorted(targets_b):
            return True  # proper target containment
        return scope_a != scope_b  # same targets, strictly more constraints
    except Exception:
        return False


def _has_slicers(command: str) -> bool:
    """True when the raw command carries slicing decorations (a trailing
    slicer pipe segment or stderr-merge redirection) — distinguishes
    ``slicer_rerun`` from ``equivalent_rerun`` in the v2 outcome vocabulary.
    Never raises."""
    try:
        if not isinstance(command, str):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        argv = [str(t) for t in argv]
        return _strip_slicer_tokens(argv) != argv
    except Exception:
        return False


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


def _state_path(ws_root: Path | str) -> str:
    return os.path.join(ws_root, _LEDGER_DIR, _STATE_NAME)


def read_state(ws_root: Path | str | None) -> dict[str, Any]:
    """Fail-open read of the reflex state blob."""
    if ws_root is None:
        return {}
    try:
        with open(_state_path(ws_root), encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_state(ws_root: Path | str, state: dict[str, Any]) -> None:
    """Atomic write: temp file in the ledger dir + rename. Raises to the
    caller (every caller is fail-open)."""
    path = _state_path(ws_root)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    payload = json.dumps(state, sort_keys=True)
    # Hand-rolled unique temp name instead of tempfile.mkstemp: importing
    # tempfile pulls shutil (~4ms) into every hook call for one mkstemp.
    tmp = os.path.join(parent, f"{_STATE_NAME}.{os.getpid()}.{os.urandom(4).hex()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
    # Controller State wave (shadow) bookkeeping. v1 state files normalize
    # cleanly: missing keys default to empty/zero.
    if not isinstance(state.get("seq"), int):
        state["seq"] = 0
    if not isinstance(state.get("commands"), int):
        state["commands"] = 0
    if not isinstance(state.get("open"), dict):
        state["open"] = {}
    if not isinstance(state.get("circuit_shadow"), dict):
        state["circuit_shadow"] = {}
    # ctx q visibility wave: dryness map + op-line cursor for the q-dry
    # ledger fold. Older state files normalize cleanly (empty/zero).
    if not isinstance(state.get("q_dry"), dict):
        state["q_dry"] = {}
    if not isinstance(state.get("q_ops"), int):
        state["q_ops"] = 0
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
    path = os.path.join(ws_root, _LEDGER_DIR, _OUTCOMES_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
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


# --------------------------------------------------- v2 ledger (shadow wave)


def _append_jsonl(ws_root: Path | str, name: str, doc: dict[str, Any]) -> None:
    """Append one sorted-keys JSON line to a session ledger. Raises to the
    caller (every caller is fail-open)."""
    path = os.path.join(ws_root, _LEDGER_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, sort_keys=True) + "\n")


def _append_v2_events(ws_root: Path | str, docs: list[dict[str, Any]]) -> None:
    """Best-effort append of shadow events to ``interventions.jsonl``.
    Never raises — a failed shadow write must never alter a live result."""
    for doc in docs:
        try:
            _append_jsonl(ws_root, _INTERVENTIONS_NAME, doc)
        except Exception:
            return


def _outcome_doc(
    intervention_id: str, outcome: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    """One FROZEN ctx.intervention-outcome/v1 line (see module docstring;
    key set matches the scorecard v2 reader exactly)."""
    return {
        "schema": "ctx.intervention-outcome/v1",
        "event": "intervention_outcome",
        "interventionId": intervention_id,
        "outcome": outcome,
        "evidence": evidence,
        "ts": time.time(),
    }


def _generation(ws_root: Path | str | None) -> str | None:
    """Lazy generation hash (EDC §8) — imported at scoring moments only
    (hot-path discipline: the common non-matching command never pays for
    git). Fail-open to None (unknown generation)."""
    if ws_root is None:
        return None
    try:
        from ctx.execution import generation_hash

        return generation_hash(ws_root)
    except Exception:
        return None


# ------------------------------------------------- circuit shadow (EDC §10)


def _episode_key(signature: str, generation: str | None) -> str:
    return f"{signature}\x00{generation or '?'}"


def _circuit_rec(state: dict[str, Any], signature: str) -> dict[str, Any]:
    rec = state["circuit_shadow"].get(signature)
    if not isinstance(rec, dict):
        rec = {
            "state": "normal",
            "episode": None,
            "positives": 0,
            "transitioned_in_episode": False,
            "starved_in_episode": False,
        }
        state["circuit_shadow"][signature] = rec
    return rec


def _circuit_enter_episode(rec: dict[str, Any], episode: str) -> None:
    """Generation change ⇒ new episode: resets the per-episode transition
    budget and starvation flag (EDC §10 — episode state resets, history
    such as the hysteresis positives counter is preserved)."""
    if rec.get("episode") != episode:
        rec["episode"] = episode
        rec["transitioned_in_episode"] = False
        rec["starved_in_episode"] = False


def _transition_doc(
    signature: str, generation: str | None, from_state: str, to_state: str
) -> dict[str, Any]:
    return {
        "schema": "ctx.circuit/v1",
        "event": "circuit_transition",
        "family": family_of(signature),
        "signature": signature,
        "generation": generation,
        "from": from_state,
        "to": to_state,
        "shadow": True,
        "ts": time.time(),
    }


def _circuit_on_starvation(
    state: dict[str, Any], signature: str, generation: str | None
) -> list[dict[str, Any]]:
    """SHADOW circuit, upward pressure: confirmed starvation escalates
    normal→dense (the shadow of the live densify latch) or, in a LATER
    episode, dense→bypass (the breaker concession). At most ONE transition
    per (signature × generation) episode — the shipped reflex logged six
    events for one round-2 episode; this forbids re-transitioning while
    keeping continued starvation countable. Returns transition event docs."""
    rec = _circuit_rec(state, signature)
    episode = _episode_key(signature, generation)
    _circuit_enter_episode(rec, episode)
    rec["starved_in_episode"] = True
    if rec.get("transitioned_in_episode"):
        return []
    current = str(rec.get("state") or "normal")
    nxt = {"normal": "dense", "dense": "bypass"}.get(current)
    if nxt is None:
        return []  # already bypass: countable, never re-transitions
    rec["state"] = nxt
    rec["transitioned_in_episode"] = True
    rec["positives"] = 0
    return [_transition_doc(signature, generation, current, nxt)]


def _circuit_on_positive(
    state: dict[str, Any], signature: str, generation: str | None
) -> list[dict[str, Any]]:
    """SHADOW circuit, hysteresis (replaces the permanent latch):
    bypass→dense after 2 positive outcomes, dense→normal after 3 — earned
    recovery, still bounded to one transition per episode."""
    rec = _circuit_rec(state, signature)
    episode = _episode_key(signature, generation)
    _circuit_enter_episode(rec, episode)
    rec["positives"] = int(rec.get("positives") or 0) + 1
    if rec.get("transitioned_in_episode"):
        return []
    current = str(rec.get("state") or "normal")
    if current == "bypass" and rec["positives"] >= CIRCUIT_BYPASS_TO_DENSE_POSITIVES:
        nxt = "dense"
    elif current == "dense" and rec["positives"] >= CIRCUIT_DENSE_TO_NORMAL_POSITIVES:
        nxt = "normal"
    else:
        return []
    rec["state"] = nxt
    rec["transitioned_in_episode"] = True
    rec["positives"] = 0
    return [_transition_doc(signature, generation, current, nxt)]


def circuit_state(ws_root: Path | str | None, signature: str | None) -> str:
    """Read the SHADOW circuit state for a signature (normal|dense|bypass).
    Recording-only this wave: rendering stays driven by the v2 densify
    latch, and the resolver keeps reading ``densify``/``bypass`` state keys
    — this shadow table deliberately lives under ``circuit_shadow`` so it
    can NEVER leak into live plan selection until it graduates. Fail-open
    to "normal"."""
    if ws_root is None or not signature:
        return "normal"
    try:
        rec = _normalized(read_state(ws_root))["circuit_shadow"].get(signature)
        if isinstance(rec, dict) and rec.get("state") in ("normal", "dense", "bypass"):
            return str(rec["state"])
        return "normal"
    except Exception:
        return "normal"


# ------------------------------------------ hypothesis windows (EDC §9)


def _expire_windows(
    state: dict[str, Any],
    *,
    now_commands: int | None = None,
    generation: str | None = None,
) -> list[dict[str, Any]]:
    """Expire open hypothesis windows: after HYPOTHESIS_WINDOW_COMMANDS
    subsequent tool-bearing commands (``now_commands`` given), or on a
    generation change (``generation`` given and differing from the window's
    recorded one; unknown generations never expire on this axis).
    ``expired_unresolved`` is a CENSORED observation (EDC §9 amendment) —
    the scorecard excludes it from every rate denominator. Returns the
    outcome docs to append."""
    docs: list[dict[str, Any]] = []
    for iid in sorted(state["open"]):
        win = state["open"].get(iid)
        if not isinstance(win, dict):
            del state["open"][iid]
            continue
        reason = None
        if (
            now_commands is not None
            and int(now_commands) - int(win.get("opened_at") or 0)
            >= HYPOTHESIS_WINDOW_COMMANDS
        ):
            reason = "window_elapsed"
        elif (
            generation is not None
            and win.get("generation")
            and win.get("generation") != generation
        ):
            reason = "generation_change"
        if reason is None:
            continue
        del state["open"][iid]
        docs.append(
            _outcome_doc(iid, "expired_unresolved", {"reason": reason})
        )
    return docs


def _resolve_window(
    state: dict[str, Any],
    signature: str,
    outcome: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve the signature's open hypothesis window (if any) with a typed
    outcome. Closing on first resolution mirrors the v1 per-cycle dedup:
    hammering the same signature within one intervention cycle scores one
    shadow outcome, not many. Returns outcome docs (empty when no window)."""
    rec = state["interventions"].get(signature)
    iid = rec.get("iid") if isinstance(rec, dict) else None
    if not iid or iid not in state["open"]:
        return []
    del state["open"][iid]
    return [_outcome_doc(str(iid), outcome, evidence)]


# -------------------------------------------------------------------- API


def emit_intervention(
    ws_root: Path | str | None,
    *,
    family: str | None = None,
    signature: str,
    generation: str | None = None,
    artifact_run_id: str | None = None,
    plan_id: str | None = None,
    plan_mode: str | None = None,
    coverage: dict[str, Any] | None = None,
    hints: int = 0,
) -> str | None:
    """Emit one ctx.intervention/v1 line (EDC §9): the emission becomes a
    first-class event carrying its coverage vector, plan identity, and
    generation, and opens a bounded hypothesis window.

    Deterministic identity: ``interventionId =
    sha256("<sessionSeq>|<signature>")[:12]`` with ``sessionSeq`` the
    monotone per-session intervention counter in reflex state — same
    command sequence, same ids (replay holds). Returns the id, or None on
    any failure (fail-open).

    Shadow-wave integration: :func:`note_intervention` calls this
    internally with plan fields defaulted (``planId`` null, ``planMode``
    from the densify latch), so the v2 pipeline works with NO cli changes;
    the cli passes richer plan data (the resolver's ``plan_id``/``mode``
    and the renderer's coverage receipt) in a later integration wave.

    NEVER arms the live starvation detector: emitting an event for a
    signature that ``note_intervention`` has not recorded must not change
    live behavior, so a missing intervention record is created disarmed."""
    if ws_root is None or not signature:
        return None
    try:
        import hashlib

        state = _normalized(read_state(ws_root))
        if generation is None:
            generation = _generation(ws_root)
        state["seq"] = int(state.get("seq") or 0) + 1
        seq = state["seq"]
        iid = hashlib.sha256(f"{seq}|{signature}".encode("utf-8")).hexdigest()[:12]
        fam = family if family else family_of(signature)
        docs: list[dict[str, Any]] = []
        # A fresh emission supersedes the signature's still-open window;
        # generation-change expiry runs here too (a generation value is in
        # hand — a scoring moment, per the lazy-hash discipline).
        docs.extend(
            _expire_windows(state, generation=generation)
            if generation is not None
            else []
        )
        rec = state["interventions"].get(signature)
        if not isinstance(rec, dict):
            # Shadow-only record: disarmed and starvation-deduped so the
            # LIVE detector ignores it (see docstring).
            rec = {
                "count": 0,
                "last_run": None,
                "hints": 0,
                "starved": True,
                "armed": False,
            }
            state["interventions"][signature] = rec
        prior_iid = rec.get("iid")
        if prior_iid and prior_iid in state["open"]:
            del state["open"][prior_iid]
            docs.append(
                _outcome_doc(
                    str(prior_iid), "expired_unresolved", {"reason": "superseded"}
                )
            )
        rec["iid"] = iid
        rec["generation"] = generation
        state["open"][iid] = {
            "signature": signature,
            "family": fam,
            "generation": generation,
            "opened_at": int(state.get("commands") or 0),
        }
        _write_state(ws_root, state)
        docs.append(
            {
                "schema": "ctx.intervention/v1",
                "event": "intervention_emitted",
                "interventionId": iid,
                "sessionSeq": seq,
                "family": fam,
                "signature": signature,
                "generation": generation,
                "artifact": str(artifact_run_id) if artifact_run_id else None,
                "planId": str(plan_id) if plan_id else None,
                "planMode": str(plan_mode) if plan_mode else "normal",
                "coverage": coverage if isinstance(coverage, dict) else {},
                "hints": max(0, int(hints)),
                "ts": time.time(),
            }
        )
        _append_v2_events(ws_root, docs)
        return iid
    except Exception:
        return None


def note_intervention(
    ws_root: Path | str | None,
    signature: str | None,
    run_short_id: str | None,
    *,
    hints: int = 0,
    generation: str | None = None,
) -> None:
    """Record that a run digest with omissions was emitted for ``signature``
    (the intervention whose hypothesis is "no re-run follows"). Resets the
    starvation-dedup flag so the *next* re-run scores a fresh starvation
    event. Fail-open: any IO problem records nothing.

    Controller State wave: additionally DUAL-WRITES the v2
    ctx.intervention/v1 emission line via :func:`emit_intervention` with
    plan fields defaulted from the densify latch (``dense`` when latched,
    else ``normal``) and records the source generation (EDC §8) so the
    next equivalent rerun can be confirmed against it. ``generation`` is an
    explicit override for offline replay; None computes it lazily (this is
    a scoring moment). The v1 state/ledger writes are byte-identical to
    the pre-wave behavior."""
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
        # v2 (spec3 round-2 finding): the intervention ARMS the signature.
        # Only an armed signature can score starvation; an Edit/Write
        # disarms (note_edit) because run → census → edit → re-run is the
        # healthy verification loop, not the slicer flail.
        rec["armed"] = True
        state["interventions"][signature] = rec
        plan_mode = "dense" if state["densify"].get(signature) else "normal"
        _write_state(ws_root, state)
    except Exception:
        return
    try:
        if generation is None:
            generation = _generation(ws_root)
        emit_intervention(
            ws_root,
            family=family_of(signature),
            signature=signature,
            generation=generation,
            artifact_run_id=run_short_id,
            plan_id=None,
            plan_mode=plan_mode,
            coverage={},
            hints=hints,
        )
    except Exception:
        pass


def note_edit(ws_root: Path | str | None) -> None:
    """An Edit/Write happened: disarm every intervened signature. The next
    re-run of each is verification of the edit, not starvation; the run's
    own digest re-arms via :func:`note_intervention`. Deliberately coarse
    (any edit disarms all signatures): mapping edited paths to tested
    signatures is guesswork, and the asymmetric loss prior says false
    negatives (missed starvation, costs tokens) beat false positives
    (spurious densify + polluted [digest_density] training data). Fail-open."""
    if ws_root is None:
        return
    try:
        state = _normalized(read_state(ws_root))
        if not state["interventions"]:
            return
        changed = False
        for rec in state["interventions"].values():
            if isinstance(rec, dict) and rec.get("armed", True):
                rec["armed"] = False
                changed = True
        if changed:
            _write_state(ws_root, state)
    except Exception:
        pass


def check_command(
    ws_root: Path | str | None, command: str, *, generation: str | None = None
) -> str | None:
    """Score ``command`` against this session's recorded interventions.

    LIVE path (v2, byte-for-byte unchanged): a command whose signature
    already has an intervention recorded — and is still event-armed — is
    the starvation detector firing (the spec3 re-run loop, caught at
    occurrence 2): append one "starvation" outcome (deduped per
    intervention cycle so the hook and ``ctx run`` seeing the same re-run
    count it once), latch densify for the signature (latching: once on,
    stays on all session), and return "densify". Returns None otherwise.
    The return value NEVER changes a guard decision — reflexes act through
    rendering. Fail-open.

    SHADOW paths (Controller State wave — record-only, they never change
    the return value, the latch, or the v1 ledger):

    * generation confirmation (EDC §8): on an equivalent rerun the source
      generation is computed lazily (this is a scoring moment) and compared
      to the one recorded at intervention. Equal → starvation CONFIRMED
      (``slicer_rerun``/``equivalent_rerun``, ``confirmed: true``) even if
      event-disarmed (the ``sed -i`` blind spot); different → verification
      (``validation_after_edit``) regardless of arming; unknown →
      provisional, classified by the armed bit, ``confirmed: false``.
    * narrowing (EDC §7 / Rule 9b): a signature that is materially narrower
      than an intervened one is census consumption — a
      ``narrowed_execution`` positive, never starvation.
    * hypothesis windows (EDC §9): this command counts toward every open
      window; unresolved windows expire (censored) after
      ``HYPOTHESIS_WINDOW_COMMANDS`` commands or on a generation change.
    * circuit (EDC §10): confirmed starvation / positive outcomes drive the
      shadow NORMAL→DENSE→BYPASS machine (one transition per episode,
      hysteresis down).

    ``generation`` is an explicit override for offline replay
    (``evals/replay_detectors.py``); None computes lazily on demand."""
    if ws_root is None:
        return None
    try:
        sig = command_signature(command)
        if not sig:
            return None
        # ctx q: READ verb, its own event class (retrieval purity — a
        # repeated q is never §8 starvation). Routed BEFORE the run
        # machinery so q commands never touch the commands counter,
        # hypothesis windows, or the densify latch: pre-wave behavior for
        # run signatures stays byte-identical. Detection is ledger-driven
        # (the query engine's q-dry ledger); the sighting only triggers
        # the fold.
        if sig.startswith("q "):
            sync_query_outcomes(ws_root)
            return None
        state = _normalized(read_state(ws_root))
        # Nothing recorded, nothing open → nothing to score: return WITHOUT
        # writing (v1 behavior kept). This matters beyond IO thrift: writing
        # here would CREATE the ledger dir on first sight of any command,
        # changing `git status --porcelain` — and with it the manifest
        # worktreeHash (golden) — between two otherwise identical runs.
        if not state["interventions"] and not state["open"]:
            return None
        # Tool-bearing command counter (hypothesis windows). Counting only
        # signature-bearing commands keeps it a pure function of the
        # command sequence.
        state["commands"] = int(state.get("commands") or 0) + 1
        shadow_docs: list[dict[str, Any]] = []
        gen_holder: list[Any] = [generation, generation is not None]

        def _gen() -> str | None:
            if not gen_holder[1]:
                gen_holder[0] = _generation(ws_root)
                gen_holder[1] = True
            return gen_holder[0]

        rec = state["interventions"].get(sig)
        result: str | None = None
        fresh = False
        run_id: str | None = None
        if isinstance(rec, dict):
            # ---- LIVE v2 path, unchanged: an Edit/Write since the digest
            # disarmed this signature — the re-run is verification, not
            # starvation. (Default True keeps v1 state files and the
            # hook/cli same-re-run dedup working.)
            if rec.get("armed", True):
                fresh = not bool(rec.get("starved"))
                rec["starved"] = True
                state["densify"][sig] = True
                if fresh:
                    state["outcomes_appended"] = (
                        int(state.get("outcomes_appended") or 0) + 1
                    )
                run_id = rec.get("last_run") or None
                result = "densify"
            # ---- SHADOW: generation confirmation (EDC §8).
            try:
                gen = _gen()
                recorded = rec.get("generation")
                if gen and recorded:
                    rel = "equal" if gen == recorded else "changed"
                else:
                    rel = "unknown"
                armed = bool(rec.get("armed", True))
                if rel == "changed":
                    outcome, starve, confirmed = "validation_after_edit", False, True
                elif rel == "equal":
                    outcome = "slicer_rerun" if _has_slicers(command) else "equivalent_rerun"
                    starve, confirmed = True, True
                else:  # unknown generation: provisional, event-classified
                    if armed:
                        outcome = (
                            "slicer_rerun" if _has_slicers(command) else "equivalent_rerun"
                        )
                        starve = True
                    else:
                        outcome, starve = "validation_after_edit", False
                    confirmed = False
                resolved = _resolve_window(
                    state,
                    sig,
                    outcome,
                    {"generation": rel, "confirmed": confirmed, "signature": sig},
                )
                shadow_docs.extend(resolved)
                if resolved:  # dedupe: circuit moves once per window/cycle
                    if starve:
                        shadow_docs.extend(_circuit_on_starvation(state, sig, gen))
                    else:
                        shadow_docs.extend(_circuit_on_positive(state, sig, gen))
            except Exception:
                pass
        else:
            # ---- SHADOW: narrowing (EDC §7 / Rule 9b). Deterministic:
            # first match in sorted signature order.
            try:
                for b_sig in sorted(state["interventions"]):
                    brec = state["interventions"][b_sig]
                    if not isinstance(brec, dict):
                        continue
                    if not is_narrower(sig, b_sig):
                        continue
                    gen = _gen()
                    recorded = brec.get("generation")
                    if gen and recorded:
                        rel = "equal" if gen == recorded else "changed"
                    else:
                        rel = "unknown"
                    resolved = _resolve_window(
                        state,
                        b_sig,
                        "narrowed_execution",
                        {
                            "generation": rel,
                            "narrower_signature": sig,
                            "signature": b_sig,
                        },
                    )
                    shadow_docs.extend(resolved)
                    if resolved:
                        shadow_docs.extend(_circuit_on_positive(state, b_sig, gen))
                    break
            except Exception:
                pass
        # ---- SHADOW: expire unresolved windows (censored observations).
        # The generation axis applies only when a hash is already in hand —
        # never computed just for expiry (hot-path discipline).
        try:
            shadow_docs.extend(
                _expire_windows(
                    state,
                    now_commands=state["commands"],
                    generation=gen_holder[0] if gen_holder[1] else None,
                )
            )
        except Exception:
            pass
        _write_state(ws_root, state)
        if fresh:
            _append_outcome(ws_root, "starvation", sig, run_id, "densify")
        _append_v2_events(ws_root, shadow_docs)
        return result
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
                # SHADOW (EDC §9): a landing resolves the signature's open
                # hypothesis window with the retrieval_landing positive and
                # feeds the circuit hysteresis. The v1 "landing" line below
                # stays byte-identical (dual-write).
                shadow_docs: list[dict[str, Any]] = []
                try:
                    resolved = _resolve_window(
                        state,
                        sig,
                        "retrieval_landing",
                        {"handle": "run:" + known, "signature": sig},
                    )
                    shadow_docs.extend(resolved)
                    if resolved:
                        shadow_docs.extend(
                            _circuit_on_positive(state, sig, rec.get("generation"))
                        )
                except Exception:
                    shadow_docs = []
                _write_state(ws_root, state)
                _append_outcome(ws_root, "landing", sig, known, "none")
                _append_v2_events(ws_root, shadow_docs)
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


def note_steer_shadow(ws_root: Path | str | None, command: str) -> None:
    """Graduated steering — the null plan (EDC phase 6b) — in SHADOW mode.

    Called by the guard when steering is about to rewrite an unbounded
    command. Records what the graduated regime WOULD have done:
    ``would_bypass`` is True when the session's engagement level is still
    passive AND the command's signature has no prior flood this session (no
    recorded intervention — i.e. no digest ever had to omit content for
    it). One line to ``.ctx-session-reads/steering-shadow.jsonl`` (the
    adoption-ledger pattern):

        {"op": "steer_shadow", "signature": str, "would_bypass": bool,
         "ts": float}

    NO behavior change this wave — the caller still applies the rewrite.
    The PostToolUse emission gate (``ctx.hook._emission_gate``) is the
    safety net that makes the eventual relaxation safe: even a bypassed
    unbounded command's output is bounded at emission time, so the null
    plan can only ever cost one bounded flood, never a transcript flood.
    Fail-open: any error records nothing and never touches a decision."""
    if ws_root is None:
        return
    try:
        sig = command_signature(command) or ""
        passive = True
        try:
            from ctx.engagement import read_state as _eng_read

            passive = str(_eng_read(ws_root).get("level") or "passive") != "active"
        except Exception:
            passive = True  # sessions start passive; unreadable state = prior
        prior_flood = False
        try:
            if sig:
                prior_flood = isinstance(
                    _normalized(read_state(ws_root))["interventions"].get(sig), dict
                )
        except Exception:
            prior_flood = False
        _append_jsonl(
            ws_root,
            _STEER_SHADOW_NAME,
            {
                "op": "steer_shadow",
                "signature": sig,
                "would_bypass": bool(passive and not prior_flood),
                "ts": time.time(),
            },
        )
    except Exception:
        pass


# ------------------------------------------- ctx q dry-rerun visibility
#
# q is a READ verb: a repeated q is NOT §8 starvation-after-intervention.
# It is its own event class (dry_query_rerun / recovered), derived ONLY
# from the q-dry ledger the query engine writes (ctx.query's self-healing
# wave: `_qdry_write` state + `_qdry_ledger_append` op lines) — read
# fail-open; a missing ledger scores nothing (never guess from
# run-intervention state, which carries no row counts). The engine sees
# execution and priors, so one physical rerun folds to exactly one event.


def _q_outcome_doc(event: str, signature: str, rows: int | None) -> dict[str, Any]:
    """One additive ctx.q/v1 event line for ``interventions.jsonl``.
    Scorecard v2 readers skip unknown event kinds by design (future
    schema, never errors), so this vocabulary is additive-safe. ``rows``
    is null when the ledger shape carries no row count."""
    return {
        "schema": "ctx.q/v1",
        "event": event,
        "signature": signature,
        "rows": int(rows) if rows is not None else None,
        "ts": time.time(),
    }


def _qrec(qd: dict[str, Any], signature: str) -> dict[str, Any]:
    """Normalized per-signature dryness record (``dry``: the engine's last
    result for this pipeline was 0 rows)."""
    rec = qd.get(signature)
    if not isinstance(rec, dict):
        rec = {"dry": bool(rec)}
        qd[signature] = rec
    return rec


def _q_line_signature(rec: dict[str, Any]) -> str:
    """Signature of one op line / census entry: a pre-normalized
    ``signature`` key wins; otherwise the engine's raw ``pipeline`` text
    normalizes through :func:`query_signature`."""
    sig = rec.get("signature")
    if isinstance(sig, str) and sig:
        return sig
    return query_signature(str(rec.get("pipeline") or "")) or ""


def _fold_q_ledger(
    state: dict[str, Any], ops_path: str | None, json_path: str | None
) -> tuple[list[dict[str, Any]], bool]:
    """Fold unseen q-dry ledger content into reflex state; return
    (event docs to append, state changed). Op lines are consumed once via
    the ``q_ops`` cursor (hook and cli sighting the same ledger fold it
    once — determinism: events minus ts are a pure function of ledger
    content). Corrupt lines are skipped individually; the cursor still
    advances so they are never re-scored."""
    qd = state["q_dry"]
    docs: list[dict[str, Any]] = []
    changed = False
    if ops_path is not None:
        try:
            with open(ops_path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except Exception:
            lines = None
        if lines is not None:
            start = max(0, int(state.get("q_ops") or 0))
            for ln in lines[start:]:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                sig = _q_line_signature(rec)
                if not sig:
                    continue
                rows = rec.get("rows")
                rows_i = (
                    rows
                    if isinstance(rows, int) and not isinstance(rows, bool)
                    else None
                )
                qrec = _qrec(qd, sig)
                if str(rec.get("op") or "") == "q_dry_rerun":
                    # The engine confirmed an identical re-issue of a
                    # dry pipeline (it sees execution + priors). The
                    # engine's lines carry no rows key — a dry rerun is
                    # 0 rows by definition.
                    docs.append(
                        _q_outcome_doc("dry_query_rerun", sig, rows_i or 0)
                    )
                    qrec["dry"] = True
                elif rows_i == 0:
                    qrec["dry"] = True  # tolerated variant: explicit dry result
                elif rows_i is not None and rows_i > 0:
                    if qrec.get("dry"):
                        # The landing extension: rows after a dry
                        # identical pipeline — the teaching worked.
                        docs.append(_q_outcome_doc("recovered", sig, rows_i))
                    qrec["dry"] = False
            if len(lines) != start:
                state["q_ops"] = len(lines)
                changed = True
    if json_path is not None:
        blob: Any = None
        try:
            with open(json_path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception:
            blob = None
        if isinstance(blob, dict) and isinstance(blob.get("dry"), list):
            # The engine's actual shape: {"dry": [<raw pipeline text>...]}
            # — an authoritative census of currently-dry pipelines. New
            # entries mark dryness (a dry result alone is not an event);
            # a previously-dry signature ABSENT from the census recovered
            # (the engine removes a pipeline on its first non-empty
            # result). Declared caveat: ring eviction (last N) can
            # masquerade as recovery — rare, positive-class, accepted.
            census = {
                s
                for s in (
                    query_signature(str(p)) for p in blob["dry"]
                )
                if s
            }
            for sig in sorted(census):
                qrec = _qrec(qd, sig)
                if not qrec.get("dry"):
                    qrec["dry"] = True
                    changed = True
            for sig in sorted(qd):
                rec = qd[sig]
                if isinstance(rec, dict) and rec.get("dry") and sig not in census:
                    docs.append(_q_outcome_doc("recovered", sig, None))
                    rec["dry"] = False
                    changed = True
        elif isinstance(blob, dict):
            # Provisional mapping shape, tolerated: {"pipelines":
            # {<signature>: {"rows": N}}} or a bare signature→rows map.
            pipelines = blob.get("pipelines")
            mapping = pipelines if isinstance(pipelines, dict) else blob
            for sig in sorted(mapping):
                val = mapping[sig]
                rows_i = None
                if isinstance(val, int) and not isinstance(val, bool):
                    rows_i = val
                elif isinstance(val, dict):
                    rv = val.get("rows")
                    if isinstance(rv, int) and not isinstance(rv, bool):
                        rows_i = rv
                if rows_i is None:
                    continue  # unrecognized shape: skipped, never guessed
                qrec = _qrec(qd, sig)
                if rows_i == 0 and not qrec.get("dry"):
                    qrec["dry"] = True
                    changed = True
                elif rows_i > 0 and qrec.get("dry"):
                    docs.append(_q_outcome_doc("recovered", sig, rows_i))
                    qrec["dry"] = False
                    changed = True
    return docs, changed or bool(docs)


def sync_query_outcomes(ws_root: Path | str | None) -> None:
    """Fold the q-dry ledger into reflex state and append any derived
    ctx.q/v1 events. The hook path syncs via :func:`check_command` on
    every q-signature command; public so the cli's q verb can sync right
    after writing its ledger. Fail-open: absent ledger → no reads, no
    writes (the ledger dir is never created here — worktreeHash golden).
    Never changes a guard decision; never touches the run-signature
    machinery (retrieval purity: q reruns are not §8 starvation)."""
    if ws_root is None:
        return
    try:
        led = os.path.join(ws_root, _LEDGER_DIR)
        ops_path = os.path.join(led, _Q_DRY_OPS_NAME)
        json_path = os.path.join(led, _Q_DRY_STATE_NAME)
        has_ops, has_json = os.path.isfile(ops_path), os.path.isfile(json_path)
        if not has_ops and not has_json:
            return
        state = _normalized(read_state(ws_root))
        docs, changed = _fold_q_ledger(
            state, ops_path if has_ops else None, json_path if has_json else None
        )
        if changed:
            _write_state(ws_root, state)
        if docs:
            _append_v2_events(ws_root, docs)
    except Exception:
        pass


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
