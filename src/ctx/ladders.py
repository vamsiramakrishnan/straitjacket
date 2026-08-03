"""The ladder registry — one declaration, four consumers.

Nine mechanisms in this harness share a shape: start on the cheapest rung and
escalate only as far as the work demands. Until now that shape lived in four
places that could not disagree loudly — a prose table in `docs/LADDERS.md`, a
hardcoded list in `scripts/gen_ladders_diagram.py`, the rung strings scattered
through the code that emits them, and a hand-maintained "measured today?"
column that nobody could verify. Four copies of one fact is the defect class
this codebase keeps finding in its own caches.

So the ladder is data. The registry below is read by:

  * `ctx ladders` — the measurement report (this module)
  * `scripts/gen_ladders_diagram.py` — the diagram
  * `docs/LADDERS.md` — the audit table (CHECKED against this registry by
    `tests/test_ladders.py`, not generated: each row carries prose a table
    cannot hold, and generating would either lose it or drag it in here)
  * `ctx.toml [ladders.<key>]` — user-configured rungs

**The measured column is derived, not asserted.** A ladder is measurable when
it declares a `Signal` naming a ledger and a field that actually carries rung
values; the report then reads that ledger and shows the real distribution. A
ladder with no signal reports "not scored" because it *cannot* be scored, not
because someone wrote that down. When a ladder gains telemetry, the audit
updates itself — which is the whole point, since the hand-maintained column
was the part most likely to drift into advertising.

Configurable rungs fall out for free. If the rungs are a declaration rather
than a literal, a workspace can shorten a ladder it does not want climbed:

    [ladders.capture]
    rungs = ["native read", "run", "seq"]      # this repo never uses `py`

Configuration narrows a ladder; it cannot invent rungs the code does not
implement, and `validate()` says so rather than failing silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ctx.sessiondir import LEDGER_DIR_NAME


@dataclass(frozen=True, slots=True)
class Signal:
    """Where a ladder's traversal evidence actually lives.

    ``ledger`` is a file under the session-reads directory, ``field`` the key
    on each record holding the rung. ``rung_of`` maps a recorded value onto a
    declared rung when the two vocabularies differ — the emitting code names
    rungs for its own purposes and this registry names them for a reader, and
    pretending those are the same string is how the mapping rots.
    """

    ledger: str
    field: str
    rung_of: dict[str, str] | None = None
    note: str = ""

    def resolve(self, value: str) -> str | None:
        if self.rung_of is None:
            return value
        return self.rung_of.get(value)


@dataclass(frozen=True, slots=True)
class Ladder:
    key: str
    name: str
    axis: str
    rungs: tuple[str, ...]
    traversed_by: str          # "model" | "hook" | "static"
    latching: str
    signal: Signal | None = None
    #: Why it cannot be measured yet — required when ``signal`` is None, so
    #: "not scored" always comes with the reason rather than a shrug.
    unmeasured_because: str = ""

    @property
    def measurable(self) -> bool:
        return self.signal is not None


#: The nine. Order is the order they are presented everywhere.
LADDERS: tuple[Ladder, ...] = (
    Ladder(
        key="solution",
        name="Solution",
        axis="what code to write",
        rungs=("not needed", "reuse", "native", "stdlib", "one-liner", "new code"),
        traversed_by="model",
        latching="n/a (in-prompt)",
        unmeasured_because=(
            "the rung is chosen inside the model's reasoning and never crosses "
            "a tool boundary, so nothing observes it; the A/B measured the "
            "ladder's OUTCOME (-28% turns) and not its traversal"
        ),
    ),
    Ladder(
        key="capture",
        name="Capture",
        axis="how work executes",
        rungs=("native read", "run", "--shell", "seq", "py", "job"),
        traversed_by="model",
        latching="n/a",
        signal=Signal(
            ledger="collapse.jsonl",
            field="rung",
            rung_of={
                "reuse-index": "native read",
                "bounded-search": "run",
                "skeleton-first": "native read",
                "addressed-range": "native read",
                "bounded-listing": "run",
                "failure-slice": "run",
            },
            note="substitution rungs, mapped onto the capture rung they land on",
        ),
    ),
    Ladder(
        key="emission",
        name="Emission budgets",
        axis="how many bytes may be emitted",
        rungs=("digest", "result", "turn", "failure x2"),
        traversed_by="hook",
        latching="flaps freely (economic)",
        unmeasured_because=(
            "nothing records WHICH budget bound a given emission. "
            "interventions.jsonl carries the output family, not the tier, and "
            "reading family as a rung was the first draft of this registry -- "
            "it produced a confident histogram of zeros. The digest layer "
            "measures budget OUTCOMES (`ctx gain`); traversal needs the gate "
            "to record the tier it applied"
        ),
    ),
    Ladder(
        key="engagement",
        name="Graduated engagement",
        axis="how hard to steer",
        rungs=("passive", "active"),
        traversed_by="hook",
        latching="latches (defensive)",
        signal=Signal(ledger="engagement.json", field="level",
                      note="current level; a point sample, not a history"),
    ),
    Ladder(
        key="pressure",
        name="Window pressure",
        axis="how tight the budgets are",
        rungs=("calm", "70%", "84%", "floor 1/4"),
        traversed_by="hook",
        latching="flaps freely (economic)",
        signal=Signal(ledger="proxy/window.json", field="window_pct",
                      note="observed window fullness; bucketed onto the rungs"),
    ),
    Ladder(
        key="guard",
        name="Guard modes",
        axis="what the guard may refuse",
        rungs=("advisory", "guarded", "strict"),
        traversed_by="static",
        latching="set once, in ctx.toml",
        unmeasured_because=(
            "a static setting, not a traversal — the useful question is the "
            "distribution across workspaces, which no single workspace can see"
        ),
    ),
    Ladder(
        key="epochs",
        name="Policy epochs",
        axis="how a bloated window is reclaimed",
        rungs=("latch", "rescue", "clear"),
        traversed_by="hook",
        latching="latches (defensive)",
        unmeasured_because=(
            "epoch transitions are not logged. `planMode` (normal|dense|bypass) "
            "rides on every intervention and is tempting to read as this "
            "ladder, but it is a different axis -- mapping it here would report "
            "plan density under the name of epoch escalation"
        ),
    ),
    Ladder(
        key="deployment",
        name="Deployment tiers",
        axis="how strongly containment is enforced",
        rungs=("skill", "plugin", "native", "hardened"),
        traversed_by="static",
        latching="set once, at install",
        unmeasured_because=(
            "chosen by `ctx wrap`, not climbed during a session; `ctx doctor` "
            "reports the tier in force but there is no traversal to score"
        ),
    ),
    Ladder(
        key="model_tiers",
        name="Model tiers",
        axis="which model does the work",
        rungs=("economy", "adaptive", "flagship"),
        traversed_by="static",
        latching="per-node, at route time",
        signal=Signal(ledger="route.jsonl", field="tier",
                      note="one record per routed node when `ctx orchestrate` ran"),
    ),
)

BY_KEY = {lad.key: lad for lad in LADDERS}


# ------------------------------------------------------------------ config
def configured(raw: dict[str, Any] | None) -> tuple[Ladder, ...]:
    """Apply `[ladders.<key>]` overrides from ctx.toml.

    Narrowing only. A workspace may drop rungs it never wants climbed —
    `rungs = ["native read", "run", "seq"]` on a repo with no `ctx py` use —
    but it cannot invent rungs, because a rung is a code path and declaring
    one that nothing implements would produce a report about a ladder that
    does not exist. Unknown names are dropped and reported by `validate()`
    rather than accepted quietly, which is the same declared-omission rule
    the digest layer follows for bytes.
    """
    if not raw:
        return LADDERS
    out = []
    for lad in LADDERS:
        section = raw.get(lad.key)
        if not isinstance(section, dict):
            out.append(lad)
            continue
        want = section.get("rungs")
        if not isinstance(want, (list, tuple)) or not want:
            out.append(lad)
            continue
        kept = tuple(r for r in lad.rungs if r in {str(w) for w in want})
        out.append(replace(lad, rungs=kept) if kept else lad)
    return tuple(out)


def validate(raw: dict[str, Any] | None) -> list[str]:
    """Human-readable problems with a `[ladders]` table (empty = fine)."""
    problems: list[str] = []
    if not raw:
        return problems
    for key, section in raw.items():
        lad = BY_KEY.get(key)
        if lad is None:
            problems.append(
                f"[ladders.{key}] is not a ladder; known: {', '.join(BY_KEY)}"
            )
            continue
        if not isinstance(section, dict):
            problems.append(f"[ladders.{key}] must be a table")
            continue
        want = section.get("rungs")
        if want is None:
            continue
        if not isinstance(want, (list, tuple)):
            problems.append(f"[ladders.{key}] rungs must be a list of strings")
            continue
        unknown = [str(w) for w in want if str(w) not in lad.rungs]
        if unknown:
            problems.append(
                f"[ladders.{key}] unknown rung(s) {unknown}; this ladder has "
                f"{list(lad.rungs)}. A rung is a code path — configuration can "
                "narrow a ladder, not extend it."
            )
        if not [w for w in want if str(w) in lad.rungs]:
            problems.append(
                f"[ladders.{key}] rungs selects nothing; leaving the ladder at "
                "its full set rather than disabling it"
            )
    return problems


# ------------------------------------------------------------- measurement
def _read_records(root: Path, ledger: str) -> list[dict]:
    path = Path(root) / LEDGER_DIR_NAME / ledger
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix == ".json":
        try:
            doc = json.loads(text)
        except ValueError:
            return []
        return [doc] if isinstance(doc, dict) else []
    out = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _bucket_pressure(value: Any, rungs: tuple[str, ...]) -> str | None:
    """Window fullness onto its rungs — the one signal that is a number."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct >= 100:
        return rungs[-1] if rungs else None
    if pct >= 84:
        return rungs[2] if len(rungs) > 2 else None
    if pct >= 70:
        return rungs[1] if len(rungs) > 1 else None
    return rungs[0] if rungs else None


def measure(root: Path | str, lad: Ladder) -> dict[str, Any]:
    """Observed traversal for one ladder, from this workspace's ledgers.

    Returns ``{"measurable": bool, "records": int, "rungs": {rung: n}, ...}``.
    A measurable ladder with zero records is reported as measurable-but-silent
    rather than as unmeasured: "the instrument exists and saw nothing" and
    "there is no instrument" are different facts, and collapsing them is how a
    dry ladder starts looking like a working one.
    """
    if lad.signal is None:
        return {
            "measurable": False,
            "reason": lad.unmeasured_because,
            "records": 0,
            "rungs": {},
        }
    records = _read_records(Path(root), lad.signal.ledger)
    counts: dict[str, int] = {r: 0 for r in lad.rungs}
    seen = unmapped = 0
    for rec in records:
        if lad.signal.field not in rec:
            continue
        seen += 1
        raw = rec[lad.signal.field]
        rung = (
            _bucket_pressure(raw, lad.rungs)
            if lad.key == "pressure"
            else lad.signal.resolve(str(raw))
        )
        if rung in counts:
            counts[rung] += 1
        else:
            unmapped += 1
    return {
        "measurable": True,
        "ledger": lad.signal.ledger,
        "note": lad.signal.note,
        "records": seen,
        "unmapped": unmapped,
        "rungs": counts,
    }


def report(root: Path | str, raw_config: dict[str, Any] | None = None) -> str:
    """The `ctx ladders` view: every ladder, its rungs, and what was observed."""
    ladders = configured(raw_config)
    problems = validate(raw_config)
    lines = ["[ctx ladders · 9 conditionality ladders]"]
    if problems:
        lines.append("")
        for p in problems:
            lines.append(f"  config: {p}")
    measured = silent = unscored = 0
    for lad in ladders:
        m = measure(root, lad)
        lines.append("")
        head = f"{lad.name} — {lad.axis} · climbed by {lad.traversed_by}"
        lines.append(head)
        if not m["measurable"]:
            unscored += 1
            lines.append(f"  not scored: {m['reason']}")
            lines.append(f"  rungs: {' → '.join(lad.rungs)}")
            continue
        if m["records"] == 0:
            silent += 1
            lines.append(
                f"  no traversal recorded yet ({m['ledger']} has no "
                f"{lad.signal.field if lad.signal else '?'} records this "
                "workspace) — the instrument exists; it has seen nothing"
            )
            lines.append(f"  rungs: {' → '.join(lad.rungs)}")
            continue
        measured += 1
        total = sum(m["rungs"].values()) or 1
        for rung in lad.rungs:
            n = m["rungs"][rung]
            bar = "#" * min(30, round(30 * n / total))
            lines.append(f"  {rung:<14} {n:>6}  {100 * n / total:5.1f}%  {bar}")
        if m["unmapped"]:
            lines.append(
                f"  {m['unmapped']} record(s) carried a value this ladder does "
                "not map — declared, not dropped"
            )
    lines.append("")
    lines.append(
        f"{measured} measured · {silent} instrumented but silent · "
        f"{unscored} not scored. A ladder nobody measures is a ladder nobody "
        "knows is being climbed."
    )
    return "\n".join(lines)


__all__ = [
    "LADDERS", "BY_KEY", "Ladder", "Signal",
    "configured", "validate", "measure", "report",
]
