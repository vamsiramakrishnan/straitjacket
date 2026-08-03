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
        rungs=("under digest", "digest..result", "result..turn", "over turn"),
        traversed_by="hook",
        latching="flaps freely (economic)",
        signal=Signal(
            ledger="plan-emissions.jsonl",
            field="visible_tokens",
            note=(
                "BUCKETED by emitted size against the configured budgets "
                "(480/1200/2800). This is the tier an emission's size falls "
                "under, not a record of which check bound it -- the gate still "
                "does not log the tier it applied. A derived rung, labelled as "
                "one, the same way window pressure buckets a percentage"
            ),
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
        signal=Signal(
            ledger="guard-policy-cache.json",
            field="mode",
            note=(
                "the resolved mode for this workspace. A point sample, not a "
                "traversal -- the useful question is the distribution ACROSS "
                "workspaces, which is what `ctx ladders --corpus` answers"
            ),
        ),
    ),
    Ladder(
        key="epochs",
        name="Policy epochs",
        axis="how a bloated window is reclaimed",
        rungs=("unknown", "promoted", "demoted"),
        traversed_by="hook",
        latching="latches (defensive)",
        signal=Signal(
            ledger="guard-policy-cache.json",
            field="_epoch",
            note=(
                "counts of promoted/demoted commands in the committed policy. "
                "`planMode` (normal|dense|bypass) rides on every intervention "
                "and is tempting to read as this ladder; it is a different axis "
                "(plan density) and mapping it here would report the wrong "
                "thing under this name"
            ),
        ),
    ),
    Ladder(
        key="deployment",
        name="Deployment tiers",
        axis="how strongly containment is enforced",
        rungs=("skill", "plugin", "native", "hardened"),
        traversed_by="static",
        latching="set once, at install",
        signal=Signal(
            ledger="(filesystem probe)",   # not a file — see measure()
            field="_deployment",
            note=(
                "probed from what `ctx wrap` actually installed in the "
                "workspace, not from a ledger. Static per workspace, so the "
                "informative form is the corpus distribution"
            ),
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


#: Emission budget thresholds, in tokens. Read from ctx.toml when available;
#: these are the shipped defaults and keep the bucket honest when it is not.
_EMISSION_TIERS = (480, 1200, 2800)


def _bucket_emission(value: Any, rungs: tuple[str, ...],
                     tiers: tuple[int, ...] = _EMISSION_TIERS) -> str | None:
    """Emitted size onto the budget tier it falls under.

    Derived, and labelled as derived in the signal note: this says which tier
    a given emission's size lands in, NOT which check bound it. The gate does
    not record the tier it applied, and inventing that record here would be
    the confident-histogram failure this registry already made once.
    """
    try:
        tok = float(value)
    except (TypeError, ValueError):
        return None
    for i, edge in enumerate(tiers):
        if tok <= edge:
            return rungs[i] if i < len(rungs) else None
    return rungs[-1] if rungs else None


def _epoch_rung(record: dict, rungs: tuple[str, ...]) -> list[str]:
    """Policy epochs are counts, not a single value.

    One committed policy can hold many promoted and many demoted commands, so
    a record yields a LIST of rungs rather than one. Everything not named in
    either list is `unknown` — the rung a command sits on before the epoch
    compiler has an opinion about it, which is most of them and is the honest
    denominator.
    """
    policy = record.get("policy") or record
    promoted = list(policy.get("promoted_commands") or [])
    demoted = list(policy.get("demoted_commands") or [])
    out = [rungs[1]] * len(promoted) + [rungs[2]] * len(demoted)
    return out or [rungs[0]]


def _deployment_rung(root: Path, rungs: tuple[str, ...]) -> str | None:
    """Which enforcement tier is actually installed in this workspace.

    Probed rather than recorded: the tier is a property of the files `ctx
    wrap` wrote, and asking the filesystem is both cheaper and harder to
    falsify than a ledger entry claiming a tier. Highest tier present wins,
    because the tiers are cumulative.
    """
    hooks = any(
        (root / rel).is_file()
        for rel in (".claude/settings.json", ".codex/hooks.json")
    ) or (root / ".antigravity" / "hooks.json").is_file()
    skill = any(
        (root / rel).is_dir()
        for rel in (".claude/skills", ".antigravity/skills", ".codex")
    ) or (root / "AGENTS.md").is_file()
    if hooks:
        return rungs[1] if len(rungs) > 1 else None   # plugin
    if skill:
        return rungs[0]                               # skill
    return None


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
    if lad.key == "deployment":
        rung = _deployment_rung(Path(root), lad.rungs)
        counts = {r: 0 for r in lad.rungs}
        if rung:
            counts[rung] = 1
        return {
            "measurable": True, "ledger": "(filesystem probe)",
            "note": lad.signal.note, "records": 1 if rung else 0,
            "unmapped": 0, "rungs": counts,
        }
    records = _read_records(Path(root), lad.signal.ledger)
    counts: dict[str, int] = {r: 0 for r in lad.rungs}
    seen = unmapped = 0
    for rec in records:
        # Two ledgers nest the interesting fields under "policy"; flattening
        # here keeps the Signal declaration a plain (ledger, field) pair.
        flat = {**(rec.get("policy") or {}), **rec} if isinstance(rec, dict) else {}
        if lad.key == "epochs":
            seen += 1
            for rung in _epoch_rung(rec, lad.rungs):
                counts[rung] = counts.get(rung, 0) + 1
            continue
        if lad.signal.field not in flat:
            continue
        seen += 1
        raw = flat[lad.signal.field]
        if lad.key == "pressure":
            rung = _bucket_pressure(raw, lad.rungs)
        elif lad.key == "emission":
            rung = _bucket_emission(raw, lad.rungs)
        else:
            rung = lad.signal.resolve(str(raw))
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


def discover_workspaces(root: Path | str) -> list[Path]:
    """Every workspace under ``root`` that carries a session ledger directory.

    The bug-bash arms each ran in their own checkout and left their own
    ledgers, so a directory of round outputs is a corpus of real sessions at
    zero additional cost — the measurement equivalent of mining the arms for
    defects rather than running new ones.
    """
    root = Path(root)
    if (root / LEDGER_DIR_NAME).is_dir():
        return [root]
    return sorted({p.parent for p in root.rglob(f"{LEDGER_DIR_NAME}/")})


def measure_corpus(roots, lad: Ladder) -> dict[str, Any]:
    """``measure`` summed across many workspaces.

    Two of these ladders are *static per workspace* — guard mode is set once
    in ctx.toml, deployment tier once at install — so a single workspace can
    only ever report one value. Their distribution is a cross-workspace
    question, and this is the instrument that can ask it. `workspaces` is
    reported beside the counts because "18 workspaces all say guarded" and
    "18 sessions in one workspace" are very different evidence.
    """
    roots = list(roots)
    total = {r: 0 for r in lad.rungs}
    records = unmapped = seen_ws = 0
    measurable = lad.measurable
    for root in roots:
        m = measure(root, lad)
        if not m["measurable"]:
            return m
        if m["records"]:
            seen_ws += 1
        records += m["records"]
        unmapped += m.get("unmapped", 0)
        for rung, n in m["rungs"].items():
            total[rung] = total.get(rung, 0) + n
    return {
        "measurable": measurable,
        "ledger": lad.signal.ledger if lad.signal else None,
        "note": lad.signal.note if lad.signal else "",
        "records": records,
        "unmapped": unmapped,
        "rungs": total,
        "workspaces": len(roots),
        "workspaces_with_data": seen_ws,
    }


def report_corpus(root: Path | str, raw_config: dict[str, Any] | None = None) -> str:
    """`ctx ladders --corpus <dir>`: the audit over a directory of sessions."""
    roots = discover_workspaces(root)
    ladders = configured(raw_config)
    lines = [
        f"[ctx ladders · corpus of {len(roots)} workspace(s) under {root}]",
    ]
    measured = silent = unscored = 0
    for lad in ladders:
        m = measure_corpus(roots, lad)
        lines.append("")
        lines.append(f"{lad.name} — {lad.axis} · climbed by {lad.traversed_by}")
        if not m["measurable"]:
            unscored += 1
            lines.append(f"  not scored: {m['reason']}")
            continue
        if m["records"] == 0:
            silent += 1
            lines.append(
                f"  instrumented, silent: {m['ledger']} carried no usable "
                "records in any workspace"
            )
            continue
        measured += 1
        total = sum(m["rungs"].values()) or 1
        lines.append(
            f"  {m['records']:,} record(s) across "
            f"{m['workspaces_with_data']}/{m['workspaces']} workspaces"
        )
        for rung in lad.rungs:
            n = m["rungs"].get(rung, 0)
            bar = "#" * min(30, round(30 * n / total))
            lines.append(f"  {rung:<16} {n:>6}  {100 * n / total:5.1f}%  {bar}")
        if m["unmapped"]:
            lines.append(f"  {m['unmapped']} unmapped value(s) — declared, not dropped")
        if m["note"]:
            lines.append(f"  note: {m['note']}")
    lines.append("")
    lines.append(f"{measured} measured · {silent} instrumented but silent · {unscored} not scored")
    return "\n".join(lines)
