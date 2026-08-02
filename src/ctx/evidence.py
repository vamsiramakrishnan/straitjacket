"""Typed evidence layer (docs/EDC.md §5, phases 1-3).

The EvidenceGraph is the seam between semantic extraction (layer 1) and
everything downstream: contracts (layer 2) validate against typed facts,
delivery policy (layer 3) selects among representations, and renderers
(layer 4) format what the plan selected. The layering law: policy cannot
buy what extraction didn't build — a fact class absent here is invisible
to every downstream layer.

Design rules encoded structurally:

- **Fact lists before fact graphs** (EDC refinement 3): items are flat,
  versioned records; relations arrive when a consumer needs them.
- **Volatile quarantine** (EDC §5 amendment 1): timing and other volatile
  values live in the ``volatile`` map, excluded from serialization
  identity and default rendering. Two runs differing only in duration
  yield the same ``graph_id``.
- **Graphs serialize via canonical_json and are content-addressed**
  (EDC §5 amendment 8): :func:`to_canonical_bytes` uses the store's
  canonical serialization semantics (sorted keys, compact separators);
  :func:`graph_id` is the sha256 of those bytes.
- **Completeness attestation** (EDC §5 amendment 5): ``coverage`` carries
  ``{parsed, total_estimate, complete}`` so a contract's census
  requirement is checkable, and a partial parse can never silently
  masquerade as full coverage.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from ctx.store import canonical_json

# Outcome of the run the evidence describes. ``error`` = the tool itself
# broke (crash, collection error); ``fail`` = the tool ran and reported
# failures; ``warning`` = passed with advisories; ``unknown`` = the
# extractor could not attest an outcome (e.g. truncated output, legacy
# adapter).
Outcome = Literal["pass", "fail", "error", "warning", "unknown"]

# Per-item severity. Severity gates *dropping* (a critical/error fact is
# never dropped, only hierarchically compacted); the ladder governs
# compaction (EDC §5 amendment 7).
Severity = Literal["info", "warning", "error", "critical"]

_OUTCOMES = ("pass", "fail", "error", "warning", "unknown")
_SEVERITIES = ("info", "warning", "error", "critical")

# selector grammar for EvidenceRef: a minted span token or the existing
# retrieval selector grammar — never a synthetic stream or free text
# (EDC §5 amendment 4: the proposed ``failure:<name>`` resolves nowhere).
_SELECTOR_RE = re.compile(r"^(span:[0-9a-f]{6,64}|lines:\d+:\d+|blob:[0-9a-f]{6,64})$")

# Closed relation vocabulary (docs/EVIDENCE-PLANS.md, graph v2). Relations
# are additive: a graph with none serializes byte-identically to v1, so
# every existing graph_id and pinned golden is unchanged. Relations arrive
# now because a consumer finally exists — the investigation join/rank
# renderer ("fact lists before fact graphs", honored).
RELATION_VOCABULARY = (
    "span_contains",
    "symbol_identity",
    "frame_of",
    "changed_in",
    "taints",
)


@dataclass(frozen=True)
class EvidenceRef:
    """Address of drill-down evidence inside a retained artifact.

    ``artifact`` names a stream or derived blob ("stdout", "stderr",
    "blob:<hash>"); ``selector`` is a minted span token (``span:<id>``,
    resolvable via ``ctx get run:<id>#stream --span <id>``) or the
    existing selector grammar (``lines:<a>:<b>``). Free-text selectors
    are rejected at construction — an address that resolves nowhere is a
    bug at mint time, not at retrieval time.
    """

    artifact: str
    selector: str | None = None
    media_type: str = "text/plain"

    def __post_init__(self) -> None:
        if not self.artifact:
            raise ValueError("EvidenceRef.artifact must be non-empty")
        if self.selector is not None and not _SELECTOR_RE.match(self.selector):
            raise ValueError(
                f"EvidenceRef.selector {self.selector!r} is not a minted span "
                "token ('span:<id>') or existing selector grammar ('lines:<a>:<b>')"
            )


@dataclass(frozen=True)
class EvidenceItem:
    """One typed fact: a failing test, a diagnostic, a failed target.

    ``causal_rank`` v1 is deterministic occurrence order (EDC §5
    amendment 6); causal inference is a versioned extractor upgrade,
    never a silent behavior change.
    """

    id: str
    kind: str
    severity: Severity
    summary: str | None = None
    failure_class: str | None = None
    location: str | None = None
    detail_ref: EvidenceRef | None = None
    causal_rank: int | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("EvidenceItem.id must be non-empty")
        if self.severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}; expected one of {_SEVERITIES}")


def _default_coverage() -> dict[str, Any]:
    return {"parsed": 0, "total_estimate": 0, "complete": False}


@dataclass(frozen=True)
class EvidenceGraph:
    """The complete typed extraction of one run — layer 1's output.

    ``volatile`` (timing, load, anything non-reproducible) is excluded
    from serialization identity and default rendering; ``coverage`` is
    the extractor's completeness attestation — never claim complete when
    the parse cannot prove it (pipe truncation, byte caps).
    """

    family: str
    profile_version: str
    outcome: Outcome
    aggregate: Mapping[str, Any]
    items: tuple[EvidenceItem, ...]
    artifacts: Mapping[str, str]
    parser_warnings: tuple[str, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=_default_coverage)
    volatile: Mapping[str, Any] = field(default_factory=dict)
    # v2 (additive): typed relations between item ids / extracted keys, as
    # (from_id, relation, to_id) triples with ``relation`` drawn from the
    # closed RELATION_VOCABULARY. Empty ⇒ the graph serializes as v1,
    # byte-identical to before this field existed.
    relations: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}; expected one of {_OUTCOMES}")
        # Coerce sequence inputs so the graph is hash-stable regardless of
        # whether the extractor built lists or tuples.
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "parser_warnings", tuple(str(w) for w in self.parser_warnings))
        rels = []
        for rel in self.relations:
            frm, kind, to = rel
            if kind not in RELATION_VOCABULARY:
                raise ValueError(
                    f"relation outside the closed vocabulary: {kind!r}; "
                    f"expected one of {RELATION_VOCABULARY}"
                )
            rels.append((str(frm), str(kind), str(to)))
        object.__setattr__(self, "relations", tuple(rels))
        cov = dict(_default_coverage(), **dict(self.coverage))
        missing = {"parsed", "total_estimate", "complete"} - set(cov)
        if missing:  # pragma: no cover - defaults make this unreachable
            raise ValueError(f"coverage missing keys: {sorted(missing)}")
        cov["parsed"] = int(cov["parsed"])
        cov["total_estimate"] = int(cov["total_estimate"])
        cov["complete"] = bool(cov["complete"])
        object.__setattr__(self, "coverage", cov)


def _ref_payload(ref: EvidenceRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {"artifact": ref.artifact, "selector": ref.selector, "media_type": ref.media_type}


def _item_payload(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "severity": item.severity,
        "summary": item.summary,
        "failure_class": item.failure_class,
        "location": item.location,
        "detail_ref": _ref_payload(item.detail_ref),
        "causal_rank": item.causal_rank,
        "attributes": dict(item.attributes),
    }


def to_canonical_bytes(graph: EvidenceGraph) -> bytes:
    """Canonical serialization of a graph: store.canonical_json semantics
    (sorted keys, compact separators, utf-8), with ``volatile`` stripped —
    presentation never enters content identity, and neither does timing.

    Version negotiation is by content, not by flag: a graph without
    relations serializes as ``ctx.evidence-graph/v1`` byte-identically to
    before the field existed (every pinned golden holds); a graph carrying
    relations serializes as v2 with a ``relations`` key."""
    payload = {
        "schema": "ctx.evidence-graph/v1",
        "family": graph.family,
        "profile_version": graph.profile_version,
        "outcome": graph.outcome,
        "aggregate": dict(graph.aggregate),
        "items": [_item_payload(i) for i in graph.items],
        "artifacts": dict(graph.artifacts),
        "parser_warnings": list(graph.parser_warnings),
        "coverage": dict(graph.coverage),
    }
    if graph.relations:
        payload["schema"] = "ctx.evidence-graph/v2"
        payload["relations"] = [list(r) for r in graph.relations]
    return canonical_json(payload)


def graph_id(graph: EvidenceGraph) -> str:
    """Content address of the typed extraction: sha256 hex of the canonical
    bytes. Enables extraction caching keyed by blob hash and conformance
    goldens pinned to graph bytes, not rendering bytes (EDC §5 am. 8)."""
    return hashlib.sha256(to_canonical_bytes(graph)).hexdigest()


def legacy_graph(
    family: str,
    profile_version: str,
    outcome: Outcome = "unknown",
    aggregate: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, str] | None = None,
) -> EvidenceGraph:
    """Compatibility adapter (EDC §21): a degenerate graph for profiles
    without typed extraction — empty items, declared parser warning,
    attested-incomplete coverage. Paired exclusively with the generic
    fallback contract: a census-requiring contract must never be applied
    to a legacy graph (contracts.validate_selection enforces this)."""
    return EvidenceGraph(
        family=family,
        profile_version=profile_version,
        outcome=outcome,
        aggregate=dict(aggregate or {}),
        items=(),
        artifacts=dict(artifacts or {}),
        parser_warnings=(LEGACY_ADAPTER_WARNING,),
        coverage={"parsed": 0, "total_estimate": 0, "complete": False},
    )


LEGACY_ADAPTER_WARNING = "legacy-adapter: no typed extraction for this profile"


@dataclass(frozen=True)
class CoverageReceipt:
    """Selection accounting (EDC §14): filled at the selection seam over
    typed facts, never by re-parsing rendered text. ``attested_complete``
    carries extraction's attestation so required_fraction over a partial
    parse cannot masquerade as full coverage."""

    items_total: int
    items_named_inline: int
    items_summarized_inline: int
    items_detailed_inline: int
    items_addressable: int
    required_fields_total: int
    required_fields_present: int
    omitted_bytes: int
    omitted_items: int
    attested_complete: bool
    #: Required fact classes the validator has NO checker for. Appended last
    #: so positional construction keeps working. These are counted as ABSENT,
    #: not present: a contract that cannot check a class it calls required is
    #: not enforcing it, and saying so is the only honest option.
    unverifiable_fields: tuple[str, ...] = ()

    @property
    def required_fraction(self) -> float:
        if self.required_fields_total <= 0:
            return 1.0
        return self.required_fields_present / self.required_fields_total


@dataclass(frozen=True)
class RenderedEvidence:
    """The renderer's return (EDC §14): text + the coverage receipt + the
    plan that produced it. ``plan`` is typed Any at this layer — the
    resolver owns the DeliveryPlan type; renderers duck-type it."""

    text: str
    coverage: CoverageReceipt
    plan: Any


__all__ = [
    "Outcome",
    "Severity",
    "RELATION_VOCABULARY",
    "EvidenceRef",
    "EvidenceItem",
    "EvidenceGraph",
    "CoverageReceipt",
    "RenderedEvidence",
    "to_canonical_bytes",
    "graph_id",
    "legacy_graph",
    "LEGACY_ADAPTER_WARNING",
]
