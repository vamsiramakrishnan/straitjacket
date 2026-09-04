"""``ctx.evidence-followup/v1`` — deterministic evidence→follow-up association.

Observable follow-up, named as such. The joins below can prove that a
digest surfaced symbol X, a later edit touched X, and a later verifier
passed. They cannot prove the digest *caused* the model's choice — so
nothing here is called attribution, outcome, confidence, or validation of
evidence. The event records **associations** with the match class that
established each one:

    plan node / command emits evidence
            ↓ identities recorded (handles, spans, symbols, test ids, files)
    subsequent commands / retrievals / edits / tests observed
            ↓ exact-match joins (deterministic, ordered by strength)
    evidence_followup/v1 events
            ↓ offline aggregation (ctx policy compile --plan-value)
    per-operator follow-up REPORT · shadow ranking · (later, if the paired
    referee proves counterfactual value) a conservative tie-break

Design laws:

- **Match classes, not floats.** ``exact_handle`` is easier to review than
  ``confidence = 0.98``, and a float suggests statistical calibration that
  does not exist. The class itself encodes strength.
- **Four states.** ``used_exactly`` (an exact identity emitted here was
  acted on), ``validation_associated`` (an associated edit was followed by
  a passing verifier — association, not causation), ``equivalent_requery``
  (the same normalized signature re-issued with no intervening generation
  change), and ``censored`` (the window never closed: session end is never
  negative evidence). Finer distinctions return only when a measurement
  proves they carry signal.
- **Counts survive to the report** so 2/2 can never masquerade as 100%.

Reused single sources of truth (no parallel implementations):
``reflex.command_signature`` (+ scope-flag tables), ``reflex.landing_ref``,
``replay._NODEID_RE``/``_COORD_RE``, ``store.canonical_json``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ctx.store import canonical_json

SCHEMA = "ctx.evidence-followup/v1"

#: Closed match-class vocabulary, strongest first. The class IS the strength
#: signal — there is deliberately no numeric confidence.
MATCH_CLASSES = (
    "exact_handle",        # later call resolves an emitted run:/blob: handle
    "exact_span_overlap",  # later edit's old_string line appears in the emission
    "exact_test_id",       # later command invokes an emitted test node id
    "exact_symbol",        # later edit/command targets an emitted symbol
    "exact_file",          # later action targets a surfaced file
)

# ---------------------------------------------------------------- language
#
# Captured for future replay interaction-effect analysis ONLY. Never
# consulted by scheduler logic; not aggregated into compiled priors today
# (start global; partition only when replay demonstrates an interaction).
LANGUAGE_FAMILY_OF_EXTENSION: dict[str, str] = {
    "py": "python", "pyi": "python",
    "js": "js", "jsx": "js", "ts": "js", "tsx": "js",
    "go": "go",
    "rs": "rust",
    "java": "jvm", "kt": "jvm",
    "rb": "ruby",
    "c": "c", "h": "c", "cc": "c", "cpp": "c", "hpp": "c",
    "cs": "dotnet",
    "php": "php",
    "swift": "swift",
}


def language_family(paths: Iterable[str]) -> str | None:
    """Majority file-extension family over ``paths``; ties break
    alphabetically on family name; None when nothing is recognizable."""
    counts: dict[str, int] = {}
    for p in paths:
        ext = str(p).rsplit(".", 1)[-1].lower() if "." in str(p) else ""
        fam = LANGUAGE_FAMILY_OF_EXTENSION.get(ext)
        if fam:
            counts[fam] = counts.get(fam, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ------------------------------------------------------------------ events


@dataclass(frozen=True)
class FollowupEvent:
    """One evidence→follow-up association. Frozen, closed-vocabulary,
    content-addressed (``event_id`` derives from the canonical fields, so
    identical observable behavior yields identical events on every replay).
    """

    version: Literal["ctx.evidence-followup/v1"]
    event_id: str
    investigation_id: str | None
    plan_node_id: str | None
    operator: str
    evidence_ids: tuple[str, ...]
    match_classes: tuple[str, ...]  # sorted subset of MATCH_CLASSES; () = no exact use
    used_exactly: bool
    validation_associated: bool
    equivalent_requery: bool
    censored: bool
    generation_before: str | None
    generation_after: str | None
    actions_observed: int
    # Additive optional instrumentation; omitted from payload() when None so
    # ids of events without them are byte-stable.
    cost_ms: int | None = None
    visible_tokens: int | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        for m in self.match_classes:
            if m not in MATCH_CLASSES:
                raise ValueError(f"match class outside the closed vocabulary: {m!r}")
        if self.used_exactly and not self.match_classes:
            raise ValueError("used_exactly requires at least one match class")

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "event_id": self.event_id,
            "investigation_id": self.investigation_id,
            "plan_node_id": self.plan_node_id,
            "operator": self.operator,
            "evidence_ids": list(self.evidence_ids),
            "match_classes": list(self.match_classes),
            "used_exactly": self.used_exactly,
            "validation_associated": self.validation_associated,
            "equivalent_requery": self.equivalent_requery,
            "censored": self.censored,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "actions_observed": self.actions_observed,
        }
        # Omitted (not null) when unknown: _event_id hashes this payload, so
        # inserting null keys would silently re-id every historical event.
        if self.cost_ms is not None:
            out["cost_ms"] = self.cost_ms
        if self.visible_tokens is not None:
            out["visible_tokens"] = self.visible_tokens
        if self.language is not None:
            out["language"] = self.language
        return out


def _event_id(fields: dict[str, Any]) -> str:
    body = {k: v for k, v in fields.items() if k != "event_id"}
    return hashlib.sha256(canonical_json(body)).hexdigest()[:16]


def make_event(**kw: Any) -> FollowupEvent:
    """Construct an event with sorted collections and a content-derived id."""
    kw.setdefault("version", SCHEMA)
    kw["evidence_ids"] = tuple(sorted(set(map(str, kw.get("evidence_ids") or ()))))
    matches = tuple(sorted(set(map(str, kw.get("match_classes") or ()))))
    kw["match_classes"] = matches
    kw.setdefault("used_exactly", bool(matches))
    probe = FollowupEvent(event_id="", **kw)
    return FollowupEvent(**{**kw, "event_id": _event_id(probe.payload())})


# ---------------------------------------------------------------- windows


@dataclass(frozen=True)
class ObservationWindow:
    """Bounded per-emission observation window. Defaults mirror the reflex
    hypothesis-window idiom; callers configure via this object, never via
    scattered constants."""

    max_actions: int = 6
    max_generations: int = 2


# ---------------------------------------------------- emissions & actions


@dataclass(frozen=True)
class EvidenceEmission:
    """One evidence-producing event with its extractable identity sets.
    Built by :func:`emissions_from_calls` for transcripts, or directly by
    plan integration (which can supply symbols and real generations)."""

    index: int  # position in the action stream
    operator: str  # logical op name, or "profile:<x>" / "command:<family>"
    signature: str | None  # normalized command signature (reflex semantics)
    handles: frozenset[str] = frozenset()  # run:<id> etc.
    test_ids: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()
    failing_ids: frozenset[str] = frozenset()  # failing test identities, if known
    raw_text: str = ""  # emission text (digest or raw) for span-overlap checks
    investigation_id: str | None = None
    plan_node_id: str | None = None
    language: str | None = None  # via language_family(files); instrumentation only

    def identity_set(self) -> frozenset[str]:
        return frozenset(self.handles | self.test_ids | self.files | self.symbols)


@dataclass(frozen=True)
class Action:
    """One downstream observable action."""

    index: int
    kind: str  # "bash" | "edit" | "write" | "retrieval" | "other"
    command: str = ""
    signature: str | None = None
    file: str | None = None
    old_string: str = ""
    new_string: str = ""
    result_text: str = ""


# -------------------------------------------------- transcript extraction

_HANDLE_RE = re.compile(r"\brun:[0-9a-f]{4,64}\b")
_FAILING_ID_RE = re.compile(r"^\s*(?:\d+\.\s+)?([\w./-]+::[\w:\[\]-]+)", re.MULTILINE)


_PASS_WORDS = ("passed", "skipped", "xfail", "xpass", " ok")
_FAIL_WORDS = ("fail", "error")


def _failing_ids(res: str) -> frozenset[str]:
    """Node ids that FAILED in one tool result, decided line by line.

    The first cut filtered every `path::name` match on whether the WHOLE
    result contained "fail" -- a condition that never mentioned the match, so
    one failure in a `pytest -v` run tagged every passing id as failing too,
    and `followup_join` then refused to associate a later green run with the
    fix. A line that names its own verdict decides by it; a numbered entry
    (the digest's failure list carries no verdict per line) counts when the
    result reports failures at all; a bare id with no verdict is not a
    failure.
    """
    has_failures = "fail" in res.lower() or "error" in res.lower()
    out: set[str] = set()
    for line in res.splitlines():
        m = _FAILING_ID_RE.match(line)
        if not m:
            continue
        low = line.lower()
        if any(w in low for w in _PASS_WORDS):
            continue
        if any(w in low for w in _FAIL_WORDS):
            out.add(m.group(1))
        elif line.lstrip()[:1].isdigit() and has_failures:
            out.add(m.group(1))
    return frozenset(out)


def _profile_of(text: str) -> str | None:
    if "profile=" in text:
        return text.split("profile=", 1)[1].split("]", 1)[0].strip() or None
    return None


def _sig(command: str) -> str | None:
    from ctx import reflex

    return reflex.command_signature(command)


def emissions_from_calls(calls: list[dict[str, Any]]) -> list[EvidenceEmission]:
    """Command/profile-level emissions from a parsed transcript (replay's
    ``parse_transcript`` shape). Conservative: a call is an emission only
    when its result carries at least one extractable identity. Investigation
    ids are never invented for sessions that predate compiled plans."""
    from ctx.replay import _COORD_RE, _NODEID_RE

    out: list[EvidenceEmission] = []
    for i, c in enumerate(calls):
        if c.get("tool") not in ("Bash",) and not str(c.get("tool", "")).startswith("mcp__"):
            continue
        res = str(c.get("result") or "")
        if not res:
            continue
        handles = frozenset(_HANDLE_RE.findall(res))
        test_ids = frozenset(_NODEID_RE.findall(res))
        coords = _COORD_RE.findall(res)
        files = frozenset(x.split(":", 1)[0] for x in coords)
        if not (handles or test_ids or files):
            continue
        cmd = str((c.get("input") or {}).get("command") or "")
        profile = _profile_of(res)
        sig = _sig(cmd)
        if profile:
            operator = f"profile:{profile}"
        else:
            from ctx import reflex

            operator = f"command:{reflex.family_of(sig)}"
        failing = _failing_ids(res)
        out.append(
            EvidenceEmission(
                index=i,
                operator=operator,
                signature=sig,
                handles=handles,
                test_ids=test_ids,
                files=files,
                failing_ids=failing & test_ids,
                raw_text=res,
                language=language_family(files),
            )
        )
    return out


def actions_from_calls(calls: list[dict[str, Any]]) -> list[Action]:
    from ctx import reflex

    out: list[Action] = []
    for i, c in enumerate(calls):
        tool = str(c.get("tool") or "")
        inp = c.get("input") or {}
        if tool in ("Edit", "Write"):
            out.append(
                Action(
                    index=i,
                    kind="edit" if tool == "Edit" else "write",
                    file=str(inp.get("file_path") or "") or None,
                    old_string=str(inp.get("old_string") or ""),
                    new_string=str(inp.get("new_string") or ""),
                )
            )
        elif tool == "Bash" or tool.startswith("mcp__"):
            cmd = str(inp.get("command") or "")
            kind = "retrieval" if reflex.landing_ref(cmd) else "bash"
            out.append(
                Action(
                    index=i,
                    kind=kind,
                    command=cmd,
                    signature=_sig(cmd),
                    result_text=str(c.get("result") or ""),
                )
            )
        else:
            out.append(Action(index=i, kind="other", command=str(inp)[:120]))
    return out


# --------------------------------------------------------------- the join


def _looks_pass(text: str) -> bool:
    """Deterministic verifier-pass check over recorded output."""
    t = text.lower()
    if "all tests passed" in t:
        return True
    return "passed" in t and "failed" not in t and "error" not in t


def _edit_matches(em: EvidenceEmission, act: Action) -> set[str]:
    """Match classes for an edit action against an emission's identities."""
    matches: set[str] = set()
    if act.file and act.file in em.files:
        matches.add("exact_file")
    for line in act.old_string.splitlines():
        line = line.strip()
        if len(line) >= 12 and line in em.raw_text:
            matches.add("exact_span_overlap")
            break
    for sym in em.symbols:
        if sym and (sym in act.old_string or (act.file and sym in act.file)):
            matches.add("exact_symbol")
            break
    return matches


@dataclass
class _OpenWindow:
    em: EvidenceEmission
    matches: set[str] = field(default_factory=set)
    validation_associated: bool = False
    equivalent_requery: bool = False
    actions_seen: int = 0
    generations: int = 0
    associated_edit: bool = False
    closed: bool = False


def followup_join(
    emissions: list[EvidenceEmission],
    actions: list[Action],
    *,
    window: ObservationWindow = ObservationWindow(),
    session_complete: bool = True,
) -> list[FollowupEvent]:
    """The deterministic follow-up join. Exact-match rules only, bounded
    windows, conservative censoring. Same inputs ⇒ identical events.

    ``session_complete=False`` (or a window still open when actions run
    out) marks the event censored: session end is never negative evidence.
    An identity surfaced by more than one open window associates with every
    window that surfaced it — deterministic, no arbitrary winner, no
    pseudo-confidence discount (the shared evidence id is recoverable from
    the events themselves)."""
    open_windows: list[_OpenWindow] = []
    events: list[FollowupEvent] = []

    def _close(w: _OpenWindow, *, censored: bool) -> None:
        if w.closed:
            return
        w.closed = True
        events.append(
            make_event(
                investigation_id=w.em.investigation_id,
                plan_node_id=w.em.plan_node_id,
                operator=w.em.operator,
                evidence_ids=tuple(sorted(w.em.identity_set()))[:16],
                match_classes=tuple(w.matches),
                validation_associated=w.validation_associated,
                equivalent_requery=w.equivalent_requery,
                censored=censored,
                generation_before="g0",
                generation_after=f"g{w.generations}",
                actions_observed=w.actions_seen,
                language=w.em.language,
            )
        )

    # Every call is an action against earlier open windows FIRST, and only
    # then (if it emits evidence) opens its own window — so a narrower
    # re-run both associates with the prior emission and starts fresh.
    stream: list[tuple[int, int, str, Any]] = []
    for act in actions:
        stream.append((act.index, 0, "action", act))
    for em in emissions:
        stream.append((em.index, 1, "emission", em))
    stream.sort(key=lambda t: (t[0], t[1]))

    for _i, _o, kind, obj in stream:
        if kind == "emission":
            open_windows.append(_OpenWindow(em=obj))
            continue

        act: Action = obj
        is_generation = act.kind in ("edit", "write")
        for w in open_windows:
            if w.closed:
                continue
            w.actions_seen += 1
            if is_generation:
                w.generations += 1
            em = w.em

            if act.kind in ("bash", "retrieval"):
                cmd = act.command
                if any(h in cmd for h in em.handles):
                    w.matches.add("exact_handle")
                if any(t in cmd for t in em.test_ids):
                    w.matches.add("exact_test_id")
                if any(s and s in cmd for s in em.symbols):
                    w.matches.add("exact_symbol")
                matched_file = next((f for f in em.files if f in cmd), None)
                if matched_file and not (w.matches & {"exact_handle", "exact_test_id"}):
                    w.matches.add("exact_file")
                # Equivalent requery: same normalized signature with no
                # intervening generation change. An edit in between makes a
                # re-run legitimate re-verification, never a requery.
                if (
                    em.signature
                    and act.signature == em.signature
                    and w.generations == 0
                ):
                    w.equivalent_requery = True
                # Validation ASSOCIATION (not causation): an associated edit
                # happened, then a passing verifier whose output no longer
                # names the emission's failing ids.
                if w.associated_edit and act.result_text:
                    resolved = _looks_pass(act.result_text) and not any(
                        fid in act.result_text for fid in em.failing_ids
                    )
                    if resolved:
                        w.validation_associated = True
                        _close(w, censored=False)
                        continue

            elif act.kind in ("edit", "write"):
                matches = _edit_matches(em, act)
                if matches:
                    w.matches |= matches
                    w.associated_edit = True

            if w.actions_seen >= window.max_actions or w.generations > window.max_generations:
                _close(w, censored=False)

    for w in open_windows:
        if not w.closed:
            _close(w, censored=not session_complete)

    return sorted(events, key=lambda e: (e.operator, e.event_id))


def followups_from_session(
    calls: list[dict[str, Any]],
    *,
    window: ObservationWindow = ObservationWindow(),
    session_complete: bool = False,
) -> list[FollowupEvent]:
    """Transcript-level join: emissions + actions from a parsed replay call
    list. ``session_complete`` defaults to False because a recorded
    transcript's end is a session end — open windows are censored."""
    return followup_join(
        emissions_from_calls(calls),
        actions_from_calls(calls),
        window=window,
        session_complete=session_complete,
    )


__all__ = [
    "SCHEMA",
    "MATCH_CLASSES",
    "LANGUAGE_FAMILY_OF_EXTENSION",
    "language_family",
    "FollowupEvent",
    "EvidenceEmission",
    "Action",
    "ObservationWindow",
    "make_event",
    "followup_join",
    "followups_from_session",
    "emissions_from_calls",
    "actions_from_calls",
]
