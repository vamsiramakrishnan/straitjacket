"""``ctx.evidence-outcome/v1`` — deterministic evidence-to-action attribution.

The bridge between offline evidence-regret measurement (what should cross
the context boundary — ``ctx replay --regret``) and online investigation
decisions (which evidence-producing action should run next —
``ctx.plan_value``). This module converts *observable* downstream behavior
into typed outcome events:

    plan node / command emits evidence
            ↓ identities recorded (handles, test ids, files, symbols, spans)
    subsequent commands / retrievals / edits / tests observed
            ↓ ordered deterministic attribution rules
    evidence_outcome/v1 events (closed vocabularies, content-derived ids)
            ↓ offline aggregation (ctx policy compile --plan-value)
    reviewable [plan_value] priors consumed by investigation ranking

This is NOT reinforcement learning and NOT model self-report. Attribution
is conservative, rule-ordered, and confidence is a deterministic function
of the reason set (max over per-reason confidences — never
pseudo-probabilistic multiplication). Sessions that end before a window
closes are ``censored=True`` and never count as negative evidence.

Reused single sources of truth (no parallel implementations):

- normalized command signatures + scope-flag tables: ``reflex.command_signature``;
- the narrowing relation: ``reflex.is_narrower``;
- handle-landing detection: ``reflex.landing_ref``;
- test-id / coordinate extraction: ``replay._NODEID_RE`` / ``replay._COORD_RE``;
- canonical serialization: ``store.canonical_json``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from ctx.store import canonical_json

SCHEMA = "ctx.evidence-outcome/v1"

#: Closed outcome vocabulary (frozen; additions are a schema bump).
OUTCOME_VOCABULARY = (
    "landed",
    "narrowed",
    "discriminated",
    "validated_after_edit",
    "retrieved",
    "equivalent_requery",
    "redundant",
    "reversed",
    "abandoned",
)

#: Closed attribution-reason vocabulary, each with its deterministic
#: confidence. Combination rule: ``attribution_confidence = max(confidence
#: of reasons present)`` — documented, monotone, and never multiplies
#: pseudo-probabilities.
REASON_CONFIDENCE: dict[str, float] = {
    "exact_handle": 1.00,
    "edit_span_overlap": 0.98,
    "exact_test_id": 0.98,
    "exact_symbol": 0.95,
    "mapped_failures_resolved": 0.90,
    "edit_reverted": 0.90,
    "equivalent_signature": 0.90,
    "exact_file": 0.85,
    "ranked_candidate_action": 0.80,
    "scope_narrowing": 0.75,
    "identity_subset": 0.70,
    "shared_identity": 0.60,  # identity claimed by >1 open window: degraded, never arbitrary
    "window_expired": 0.50,
}
REASON_VOCABULARY = tuple(sorted(REASON_CONFIDENCE))


@dataclass(frozen=True)
class EvidenceOutcome:
    """One attributed evidence→action event. Frozen, closed-vocabulary,
    content-addressed (``event_id`` derives from the canonical fields, so
    identical observable behavior yields identical events on every replay)."""

    version: Literal["ctx.evidence-outcome/v1"]
    event_id: str
    investigation_id: str | None
    plan_node_id: str | None
    evidence_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    downstream_action_kind: str
    downstream_action_ref: str | None
    outcomes: tuple[str, ...]
    attribution_reasons: tuple[str, ...]
    attribution_confidence: float
    generation_before: str | None
    generation_after: str | None
    actions_observed: int
    censored: bool
    operator: str = "unknown"  # logical op or profile/command family (aggregation key)
    #: Additive instrumentation (appended field, default None): the majority
    #: language family of the emission's identity files. Captured so future
    #: replays can test for a language interaction effect; NOT aggregated
    #: into compiled priors today, and never consulted by scheduler logic.
    language: str | None = None

    def __post_init__(self) -> None:
        for o in self.outcomes:
            if o not in OUTCOME_VOCABULARY:
                raise ValueError(f"outcome outside the closed vocabulary: {o!r}")
        for r in self.attribution_reasons:
            if r not in REASON_CONFIDENCE:
                raise ValueError(f"reason outside the closed vocabulary: {r!r}")

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "version": self.version,
            "event_id": self.event_id,
            "investigation_id": self.investigation_id,
            "plan_node_id": self.plan_node_id,
            "evidence_ids": list(self.evidence_ids),
            "candidate_ids": list(self.candidate_ids),
            "downstream_action_kind": self.downstream_action_kind,
            "downstream_action_ref": self.downstream_action_ref,
            "outcomes": list(self.outcomes),
            "attribution_reasons": list(self.attribution_reasons),
            "attribution_confidence": self.attribution_confidence,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "actions_observed": self.actions_observed,
            "censored": self.censored,
            "operator": self.operator,
        }
        # Included ONLY when set: this dict feeds _event_id, so language-less
        # events keep byte-identical payloads (and ids) across the upgrade.
        if self.language is not None:
            body["language"] = self.language
        return body


def combine_confidence(reasons: tuple[str, ...]) -> float:
    """The documented combination rule: maximum reason confidence."""
    return max((REASON_CONFIDENCE[r] for r in reasons), default=0.0)


def _event_id(fields: dict[str, Any]) -> str:
    body = {k: v for k, v in fields.items() if k != "event_id"}
    return hashlib.sha256(canonical_json(body)).hexdigest()[:16]


def make_event(**kw: Any) -> EvidenceOutcome:
    """Construct an event with sorted collections and a content-derived id."""
    kw.setdefault("version", SCHEMA)
    kw["evidence_ids"] = tuple(sorted(set(map(str, kw.get("evidence_ids") or ()))))
    kw["candidate_ids"] = tuple(sorted(set(map(str, kw.get("candidate_ids") or ()))))
    kw["outcomes"] = tuple(sorted(set(map(str, kw.get("outcomes") or ()))))
    reasons = tuple(sorted(set(map(str, kw.get("attribution_reasons") or ()))))
    kw["attribution_reasons"] = reasons
    kw["attribution_confidence"] = round(combine_confidence(reasons), 2)
    probe = EvidenceOutcome(event_id="", **kw)
    return EvidenceOutcome(**{**kw, "event_id": _event_id(probe.payload())})


# ---------------------------------------------------------------- windows


@dataclass(frozen=True)
class ObservationWindow:
    """Bounded per-emission observation window (Part 3). Defaults mirror the
    reflex hypothesis-window idiom; callers configure via this object, never
    via scattered constants."""

    max_actions: int = 6
    max_generations: int = 2


# ------------------------------------------------------ language families

#: File-extension → language-family table (frozen; additions are reviewed
#: like any vocabulary change). Language is a PARTITION KEY for compiled
#: priors only — the scheduler stays language-neutral and no logic may
#: branch on a family name.
LANGUAGE_FAMILY_OF_EXTENSION: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "js": "js",
    "jsx": "js",
    "ts": "js",
    "tsx": "js",
    "go": "go",
    "rs": "rust",
    "java": "jvm",
    "kt": "jvm",
    "rb": "ruby",
    "c": "c",
    "h": "c",
    "cc": "c",
    "cpp": "c",
    "hpp": "c",
    "cs": "dotnet",
    "php": "php",
    "swift": "swift",
}


def language_family(paths: Iterable[str]) -> str | None:
    """Majority language family over the identity files, deterministic:
    ties break alphabetically on the family name; ``None`` when no path
    carries a recognizable extension."""
    counts: dict[str, int] = {}
    for p in paths:
        name = str(p).replace("\\", "/").rsplit("/", 1)[-1]
        if "." not in name:
            continue
        fam = LANGUAGE_FAMILY_OF_EXTENSION.get(name.rsplit(".", 1)[-1].lower())
        if fam:
            counts[fam] = counts.get(fam, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda f: (-counts[f], f))


# ---------------------------------------------------- emissions & actions


@dataclass(frozen=True)
class EvidenceEmission:
    """One evidence-producing event with its extractable identity sets.
    Built by :func:`emissions_from_calls` for transcripts, or directly by
    plan integration (which can supply candidates and symbols)."""

    index: int  # position in the action stream
    operator: str  # logical op name, or "profile:<x>" / "command:<family>"
    signature: str | None  # normalized command signature (reflex semantics)
    handles: frozenset[str] = frozenset()  # run:<id> etc.
    test_ids: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()
    symbols: frozenset[str] = frozenset()
    candidates: tuple[tuple[str, int], ...] = ()  # (candidate_id, rank)
    failing_ids: frozenset[str] = frozenset()  # failing test identities, if known
    raw_text: str = ""  # emission text (digest or raw) for span-overlap checks
    investigation_id: str | None = None
    plan_node_id: str | None = None
    language: str | None = None  # language_family(files); partition key, never logic

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
_FILE_RE = re.compile(r"\b[\w./-]+/[\w.-]+\.\w{1,8}\b")
_FAILING_ID_RE = re.compile(r"^\s*(?:\d+\.\s+)?([\w./-]+::[\w:\[\]-]+)", re.MULTILINE)


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
        failing = frozenset(
            m for m in _FAILING_ID_RE.findall(res) if "FAIL" in res or "fail" in res
        )
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


# ------------------------------------------------------------ attribution


def _looks_pass(text: str) -> bool:
    """Deterministic verifier-pass check over recorded output."""
    t = text.lower()
    if "all tests passed" in t:
        return True
    return "passed" in t and "failed" not in t and "error" not in t


def _edit_matches(em: EvidenceEmission, act: Action) -> tuple[str, ...]:
    """Reasons for an edit action landing on an emission's evidence."""
    reasons: list[str] = []
    if act.file and act.file in em.files:
        reasons.append("exact_file")
    for line in act.old_string.splitlines():
        line = line.strip()
        if len(line) >= 12 and line in em.raw_text:
            reasons.append("edit_span_overlap")
            break
    for sym in em.symbols:
        if sym and (sym in act.old_string or (act.file and sym in (act.file or ""))):
            reasons.append("exact_symbol")
            break
    if em.candidates:
        for cid, _rank in em.candidates:
            if cid and (cid in act.old_string or (act.file and cid in act.file)):
                reasons.append("ranked_candidate_action")
                break
    return tuple(reasons)


@dataclass
class _OpenWindow:
    em: EvidenceEmission
    outcomes: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    actions_seen: int = 0
    generations: int = 0
    action_ref: str | None = None
    action_kind: str | None = None
    attributed_edits: list[Action] = field(default_factory=list)
    closed: bool = False
    censored: bool = False


def attribute(
    emissions: list[EvidenceEmission],
    actions: list[Action],
    *,
    window: ObservationWindow = ObservationWindow(),
    session_complete: bool = True,
) -> list[EvidenceOutcome]:
    """The deterministic attribution join (Part 2). Ordered rules, bounded
    windows, conservative censoring. Same inputs ⇒ identical events.

    ``session_complete=False`` (or a window still open when actions run out)
    marks the event censored: session end is never negative evidence.
    """
    # Identity ambiguity map: identity string -> emission indices claiming it.
    claims: dict[str, list[int]] = {}
    for em in emissions:
        for ident in em.identity_set():
            claims.setdefault(ident, []).append(em.index)

    open_windows: list[_OpenWindow] = []
    events: list[EvidenceOutcome] = []
    seen_identities: dict[str, int] = {}  # identity -> first emitting index

    def _close(w: _OpenWindow, *, censored: bool) -> None:
        if w.closed:
            return
        w.closed = True
        w.censored = censored
        if (
            not censored
            and not w.outcomes & {"landed", "narrowed", "discriminated",
                                  "validated_after_edit", "retrieved"}
            and not w.outcomes & {"reversed", "equivalent_requery"}
            and w.actions_seen >= window.max_actions
        ):
            w.outcomes.add("abandoned")
            w.reasons.add("window_expired")
        events.append(
            make_event(
                investigation_id=w.em.investigation_id,
                plan_node_id=w.em.plan_node_id,
                evidence_ids=tuple(sorted(w.em.identity_set()))[:16],
                candidate_ids=tuple(cid for cid, _ in w.em.candidates),
                downstream_action_kind=w.action_kind or "none",
                downstream_action_ref=w.action_ref,
                outcomes=tuple(w.outcomes),
                attribution_reasons=tuple(w.reasons),
                generation_before="g0",
                generation_after=f"g{w.generations}",
                actions_observed=w.actions_seen,
                censored=censored,
                operator=w.em.operator,
                language=w.em.language,
            )
        )

    def _ambiguous(ident: str, em_index: int) -> bool:
        holders = [
            i for i in claims.get(ident, [])
            if i != em_index
            and any(not w.closed and w.em.index == i for w in open_windows)
        ]
        return bool(holders)

    def _add(w: _OpenWindow, outcome: str, reason: str, idents: tuple[str, ...] = ()) -> None:
        # Shared-identity degradation: a match on an identity claimed by
        # another still-open window replaces the exact reason with the
        # deterministic lower-confidence ``shared_identity`` reason.
        if idents and all(_ambiguous(i, w.em.index) for i in idents):
            reason = "shared_identity"
        w.outcomes.add(outcome)
        w.reasons.add(reason)

    # Every call is an action against earlier open windows FIRST, and only
    # then (if it emits evidence) opens its own window — so a narrower
    # re-run both lands the prior emission and starts a fresh window.
    stream: list[tuple[int, int, str, Any]] = []
    for act in actions:
        stream.append((act.index, 0, "action", act))
    for em in emissions:
        stream.append((em.index, 1, "emission", em))
    stream.sort(key=lambda t: (t[0], t[1]))
    stream_items = [(kind, obj) for _i, _o, kind, obj in stream]

    for kind, obj in stream_items:
        if kind == "emission":
            em: EvidenceEmission = obj
            w = _OpenWindow(em=em)
            idents = em.identity_set()
            if idents and idents <= set(seen_identities):
                w.outcomes.add("redundant")
                w.reasons.add("identity_subset")
            for ident in idents:
                seen_identities.setdefault(ident, em.index)
            open_windows.append(w)
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
                matched_handle = next((h for h in em.handles if h in cmd), None)
                if matched_handle:
                    _add(w, "retrieved", "exact_handle", (matched_handle,))
                    _add(w, "landed", "exact_handle", (matched_handle,))
                    w.action_kind, w.action_ref = act.kind, matched_handle
                    if "--lines" in cmd or "--span" in cmd:
                        _add(w, "narrowed", "exact_handle", (matched_handle,))
                matched_tid = next((t for t in em.test_ids if t in cmd), None)
                if matched_tid:
                    _add(w, "landed", "exact_test_id", (matched_tid,))
                    w.action_kind, w.action_ref = "bash", matched_tid
                    from ctx import reflex

                    if em.signature and reflex.is_narrower(act.signature, em.signature):
                        _add(w, "narrowed", "exact_test_id", (matched_tid,))
                else:
                    from ctx import reflex

                    if (
                        em.signature
                        and act.signature
                        and reflex.is_narrower(act.signature, em.signature)
                    ):
                        _add(w, "narrowed", "scope_narrowing")
                matched_file = next(
                    (
                        f for f in em.files
                        if f in cmd and len(claims.get(f, [])) == 1
                    ),
                    None,
                )
                if matched_file and not matched_handle and not matched_tid:
                    _add(w, "landed", "exact_file", (matched_file,))
                    w.action_kind = w.action_kind or "bash"
                    w.action_ref = w.action_ref or matched_file
                # Equivalent requery: same signature, no intervening
                # generation change, not a narrowing.
                if (
                    em.signature
                    and act.signature == em.signature
                    and w.generations == 0
                ):
                    _add(w, "equivalent_requery", "equivalent_signature")
                # Validation: an attributed edit happened, then a passing
                # verifier resolves the mapped failures.
                if w.attributed_edits and act.result_text:
                    resolved = _looks_pass(act.result_text) and not any(
                        fid in act.result_text for fid in em.failing_ids
                    )
                    if resolved and (em.failing_ids or _looks_pass(act.result_text)):
                        _add(w, "validated_after_edit", "mapped_failures_resolved")
                        _close(w, censored=False)
                        continue

            elif act.kind in ("edit", "write"):
                reasons = _edit_matches(em, act)
                if reasons:
                    for r in reasons:
                        idents: tuple[str, ...] = ()
                        if r == "exact_file" and act.file:
                            idents = (act.file,)
                        _add(w, "landed", r, idents)
                    if "ranked_candidate_action" in reasons:
                        _add(w, "discriminated", "ranked_candidate_action")
                    w.action_kind = "edit"
                    w.action_ref = act.file
                    w.attributed_edits.append(act)
                # Reversal: this edit exactly undoes an attributed edit.
                for prev in w.attributed_edits[:-1] if reasons else w.attributed_edits:
                    if (
                        prev.new_string
                        and act.old_string == prev.new_string
                        and act.new_string == prev.old_string
                    ):
                        w.outcomes.add("reversed")
                        w.reasons.add("edit_reverted")
                        w.outcomes.discard("validated_after_edit")
                        _close(w, censored=False)
                        break
                if w.closed:
                    continue

            if w.actions_seen >= window.max_actions or w.generations > window.max_generations:
                _close(w, censored=False)

    for w in open_windows:
        if not w.closed:
            _close(w, censored=not session_complete)

    return sorted(events, key=lambda e: (e.operator, e.event_id))


def attribute_session(
    calls: list[dict[str, Any]],
    *,
    window: ObservationWindow = ObservationWindow(),
    session_complete: bool = False,
) -> list[EvidenceOutcome]:
    """Transcript-level attribution: emissions + actions from a parsed
    replay call list. ``session_complete`` defaults to False because a
    recorded transcript's end is a session end — open windows are censored,
    never negative."""
    emissions = emissions_from_calls(calls)
    actions = actions_from_calls(calls)
    return attribute(
        emissions, actions, window=window, session_complete=session_complete
    )


__all__ = [
    "SCHEMA",
    "OUTCOME_VOCABULARY",
    "REASON_VOCABULARY",
    "REASON_CONFIDENCE",
    "LANGUAGE_FAMILY_OF_EXTENSION",
    "language_family",
    "EvidenceOutcome",
    "EvidenceEmission",
    "Action",
    "ObservationWindow",
    "combine_confidence",
    "make_event",
    "attribute",
    "attribute_session",
    "emissions_from_calls",
    "actions_from_calls",
]
