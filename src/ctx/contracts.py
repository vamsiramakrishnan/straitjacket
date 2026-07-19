"""Evidence Contracts (docs/EDC.md §5.3, layer 2).

A contract makes rules 1-2 machine-checkable: per outcome it names the
fact classes that are REQUIRED (a plan that cannot fit them escalates,
never silently truncates), PREFERRED (dropped under pressure, in ladder
order), and RETRIEVABLE (delivered as addresses; provenance always
rides). Contracts are committed TOML tables loaded via stdlib tomllib —
never YAML (EDC §5 amendment 2: no hard dependencies in the core), and
change by code review, never at runtime (only layer 3 adapts).

Validation happens at the *selection seam* over typed facts
(:func:`validate_selection`), never by parsing rendered text (EDC §5.3
amendment 1); rendered-text substring checks are a secondary smoke layer.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from ctx.evidence import CoverageReceipt, EvidenceGraph, LEGACY_ADAPTER_WARNING

CONTRACT_SCHEMA = "ctx.evidence-contract/v1"

# Typed loss severities: what omitting a fact class costs. Severity gates
# dropping (catastrophic facts are never dropped, only hierarchically
# compacted with every rung address-bearing); the ladder governs
# compaction (EDC §5 amendment 7).
LossSeverity = Literal["catastrophic", "major", "minor"]
_LOSS_SEVERITIES = ("catastrophic", "major", "minor")

# The census fact class: the complete item-identity set. A contract that
# requires it must never be applied to a legacy (adapter) graph — a
# degenerate graph would satisfy it vacuously or starve silently (§21).
CENSUS_CLASS = "complete_identity_census"

_DATA_DIR = Path(__file__).resolve().parent / "contracts"


class ContractError(Exception):
    pass


@dataclass(frozen=True)
class OutcomeRequirements:
    required: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()
    retrievable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rendering:
    stable_order: str = "occurrence"
    evidence_floor_tokens: int = 128
    hard_ceiling_tokens: int = 4000


@dataclass(frozen=True)
class EvidenceContract:
    schema: str
    family: str
    profile: str
    decision_unit: str
    outcomes: Mapping[str, OutcomeRequirements]
    loss_severities: Mapping[str, LossSeverity]
    rendering: Rendering
    source: str = ""  # provenance (path stem), never part of semantics

    def for_outcome(self, outcome: str) -> OutcomeRequirements:
        got = self.outcomes.get(outcome) or self.outcomes.get("default")
        return got if got is not None else OutcomeRequirements()

    def loss_severity(self, fact_class: str) -> LossSeverity:
        return self.loss_severities.get(fact_class, "minor")

    def requires_census(self, outcome: str) -> bool:
        return CENSUS_CLASS in self.for_outcome(outcome).required


def _str_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ContractError(f"{where} must be a list of strings, got {raw!r}")
    return tuple(raw)


def _parse(data: dict[str, Any], source: str) -> EvidenceContract:
    schema = data.get("schema")
    if schema != CONTRACT_SCHEMA:
        raise ContractError(f"{source}: schema must be {CONTRACT_SCHEMA!r}, got {schema!r}")
    for key in ("family", "profile", "decision_unit"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ContractError(f"{source}: missing or empty field {key!r}")
    raw_outcomes = data.get("outcomes")
    if not isinstance(raw_outcomes, dict) or not raw_outcomes:
        raise ContractError(f"{source}: [outcomes.*] tables are required")
    outcomes: dict[str, OutcomeRequirements] = {}
    for name, table in raw_outcomes.items():
        if not isinstance(table, dict):
            raise ContractError(f"{source}: [outcomes.{name}] must be a table")
        outcomes[name] = OutcomeRequirements(
            required=_str_tuple(table.get("required"), f"{source}: outcomes.{name}.required"),
            preferred=_str_tuple(table.get("preferred"), f"{source}: outcomes.{name}.preferred"),
            retrievable=_str_tuple(
                table.get("retrievable"), f"{source}: outcomes.{name}.retrievable"
            ),
        )
    losses: dict[str, str] = {}
    for cls, sev in (data.get("loss_severities") or {}).items():
        if sev not in _LOSS_SEVERITIES:
            raise ContractError(
                f"{source}: loss_severities.{cls} = {sev!r}; expected one of {_LOSS_SEVERITIES}"
            )
        losses[cls] = sev
    rend = data.get("rendering") or {}
    rendering = Rendering(
        stable_order=str(rend.get("stable_order", "occurrence")),
        evidence_floor_tokens=int(rend.get("evidence_floor_tokens", 128)),
        hard_ceiling_tokens=int(rend.get("hard_ceiling_tokens", 4000)),
    )
    if rendering.evidence_floor_tokens < 0 or rendering.hard_ceiling_tokens <= 0:
        raise ContractError(f"{source}: rendering budgets must be positive")
    # floor <= ceiling asserted at load (EDC §13): a contract whose floor
    # exceeds its ceiling is unsatisfiable by construction — fail loudly
    # at commit time, never at delivery time.
    if rendering.evidence_floor_tokens > rendering.hard_ceiling_tokens:
        raise ContractError(
            f"{source}: evidence_floor_tokens ({rendering.evidence_floor_tokens}) exceeds "
            f"hard_ceiling_tokens ({rendering.hard_ceiling_tokens})"
        )
    return EvidenceContract(
        schema=CONTRACT_SCHEMA,
        family=data["family"],
        profile=data["profile"],
        decision_unit=data["decision_unit"],
        outcomes=outcomes,
        loss_severities=losses,
        rendering=rendering,
        source=source,
    )


def load_contract_path(path: Path | str) -> EvidenceContract:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ContractError(f"contract file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"{path.name}: invalid TOML: {exc}") from None
    return _parse(data, path.stem)


def available_contracts() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in _DATA_DIR.glob("*.toml")))


@lru_cache(maxsize=None)
def load_contract(family: str) -> EvidenceContract:
    """Load the committed contract for a family (src/ctx/contracts/<family>.toml)."""
    return load_contract_path(_DATA_DIR / f"{family}.toml")


def contract_for_family(family: str) -> EvidenceContract:
    """The family's contract, else the generic fallback — the ONLY contract
    legal for legacy-adapter graphs (§21: a legacy graph is never validated
    against a census-requiring contract, and never satisfies one vacuously)."""
    if (_DATA_DIR / f"{family}.toml").is_file():
        return load_contract(family)
    return load_contract("generic")


def _is_legacy(graph: EvidenceGraph) -> bool:
    return any(w.startswith("legacy-adapter") for w in graph.parser_warnings)


def _class_present(
    fact_class: str,
    graph: EvidenceGraph,
    selected: tuple,
    selected_ids: frozenset[str],
    included_fields: frozenset[str],
) -> bool:
    """Is a fact class actually delivered by this selection, judged over
    TYPED facts — a class counts only if it was both included in the
    selection AND the underlying facts exist in the graph."""
    if fact_class not in included_fields:
        return False
    if fact_class == "aggregate_counts":
        return bool(graph.aggregate)
    if fact_class == CENSUS_CLASS:
        # Complete relative to parsed facts; the attestation rides on the
        # receipt so an incomplete parse can never masquerade as coverage.
        return bool(graph.items) and {i.id for i in graph.items} <= selected_ids
    if fact_class == "location":
        return bool(selected) and all(i.location for i in selected)
    if fact_class == "failure_class":
        return bool(selected) and all(i.failure_class for i in selected)
    if fact_class == "one_line_summary":
        return bool(selected) and all(i.summary for i in selected)
    if fact_class == "root_detail":
        return any(i.detail_ref is not None for i in selected)
    # Unknown/forward-compatible classes: declared inclusion is presence.
    return True


def validate_selection(
    selected_item_ids: Iterable[str],
    included_fields: Iterable[str],
    contract: EvidenceContract,
    graph: EvidenceGraph,
) -> CoverageReceipt:
    """Coverage accounting at the selection seam (EDC §5.3 amendment 1).

    ``selected_item_ids``: item identities the plan delivers inline.
    ``included_fields``: fact classes the selection includes inline
    (e.g. {"aggregate_counts", "complete_identity_census", "location",
    "failure_class", "one_line_summary", "root_detail"}).

    Returns a CoverageReceipt computed over typed facts — never by
    parsing rendered text. ``omitted_bytes`` is 0 here; the renderer
    fills it from its own emission accounting.
    """
    if contract.requires_census(graph.outcome) and _is_legacy(graph):
        raise ContractError(
            f"census-requiring contract {contract.source or contract.family!r} applied to a "
            "legacy-adapter graph; legacy graphs pair exclusively with the generic contract (§21)"
        )
    selected_ids = frozenset(selected_item_ids)
    included = frozenset(included_fields)
    # Graph (stable) order preserved by filtering the graph, not the input.
    selected = tuple(i for i in graph.items if i.id in selected_ids)
    req = contract.for_outcome(graph.outcome)
    present = sum(
        1
        for cls in req.required
        if _class_present(cls, graph, selected, selected_ids, included)
    )
    named = len(selected)
    return CoverageReceipt(
        items_total=len(graph.items),
        items_named_inline=named,
        items_summarized_inline=(
            sum(1 for i in selected if i.summary) if "one_line_summary" in included else 0
        ),
        items_detailed_inline=(
            min(1, sum(1 for i in selected if i.detail_ref is not None))
            if "root_detail" in included
            else 0
        ),
        items_addressable=sum(1 for i in graph.items if i.detail_ref is not None),
        required_fields_total=len(req.required),
        required_fields_present=present,
        omitted_bytes=0,
        omitted_items=len(graph.items) - named,
        attested_complete=bool(graph.coverage.get("complete")),
    )


__all__ = [
    "CONTRACT_SCHEMA",
    "CENSUS_CLASS",
    "LossSeverity",
    "ContractError",
    "OutcomeRequirements",
    "Rendering",
    "EvidenceContract",
    "load_contract",
    "load_contract_path",
    "contract_for_family",
    "available_contracts",
    "validate_selection",
]
