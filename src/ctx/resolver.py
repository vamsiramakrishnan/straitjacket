"""The Delivery Policy Resolver (docs/EDC.md §5.4, §13; docs/LADDERS.md §3).

One choke point where every conditional ladder composes: the evidence
outcome, the circuit state (reflex), the signal record (window.json, model
id), the reader-capability posterior (EDC §6), and the committed config
budgets meet here and emit a single frozen :class:`DeliveryPlan`.

Doctrine encoded (EDC §13, adopted):

* success → ``pass_summary``; failure → ``fail_census``; the compression
  circuit overrides to ``dense``/``bypass``; an unfittable census selects
  ``flood`` with declared partial inline coverage — never silent truncation.
* Budget math order is FROZEN: base → failure multiplier → pressure
  multiplier → evidence floor (max) → hard ceiling (min). The floor is
  applied AFTER the multipliers so window pressure can never squeeze
  failure evidence below usefulness (the failure-asymmetry doctrine), and
  ``floor <= ceiling`` is validated at contract load
  (:func:`validate_rendering_policy`).
* Reasons are a CLOSED vocabulary (EDC §5.3 amendment 2) — free text
  cannot train epoch tables. Plans carry a stable ``plan_id`` (hash of the
  non-reason fields, amendment 3) so P_rerun(p) is estimable.
* Addresses are contract-driven, not plan-driven (EDC §12 correction 1):
  ``include_addresses`` governs teaching-level address prose only; any
  nonempty retrievable-tier class emits its address in every mode — the
  renderer never consults the plan for provenance, and this resolver never
  emits a plan that suppresses it.
* Safety is outside the plan space (rule 7 / EDC §11): nothing in this
  module is consulted by ``ctx.hook.classify`` and nothing here can alter a
  guard decision. tests/test_safety_invariant.py enforces this by bytes.

Discipline: stdlib-only, deterministic (same inputs → same plan), and
fail-open at every entry point — a broken window.json, reflex blob, or
contract table yields the safe default plan that mirrors the pre-EDC
budget behavior, never an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ctx.sessiondir import session_reads_path

# --------------------------------------------------------------------------
# Frozen interfaces from the evidence layer (src/ctx/evidence.py). Imported
# defensively: until that module ships, the Protocol stubs below carry the
# exact attribute names this resolver codes against (duck-typed everywhere).
try:  # pragma: no cover - exercised only once evidence.py exists
    from ctx.evidence import CoverageReceipt, EvidenceGraph, RenderedEvidence  # noqa: F401
except Exception:  # pragma: no cover - stub path is the current reality

    @runtime_checkable
    class EvidenceGraph(Protocol):  # type: ignore[no-redef]
        family: str
        outcome: str
        items: Any
        coverage: Any

    @runtime_checkable
    class CoverageReceipt(Protocol):  # type: ignore[no-redef]
        ...

    @runtime_checkable
    class RenderedEvidence(Protocol):  # type: ignore[no-redef]
        ...


_READER_STATE_NAME = "reader.json"

# ----------------------------------------------------------- plan literals
MODES = ("pass_summary", "fail_census", "dense", "bypass", "flood")
CENSUS_LEVELS = ("none", "complete", "bounded")
ITEM_SUMMARY_LEVELS = ("none", "class_only", "one_line", "expanded")

# Closed reason vocabulary (EDC §5.3 amendment 2 — ledger-event-shaped).
# Adding a reason is a schema change reviewed like any ledger vocabulary
# bump; free-text reasons are rejected at plan construction.
REASON_VOCABULARY = (
    "outcome_success",
    "outcome_failure",
    "circuit_dense",
    "circuit_bypass",
    "failure_multiplier",
    "window_pressure",
    "evidence_floor",
    "hard_ceiling",
    "census_unfittable",
    "reader_inline_latch",
    "reader_low_confidence",
    "epoch_default",
    "fail_open_default",
)

# Reader-capability constants (EDC §6, amendments 2 and the confidence
# floor): dropping to `inline` latches; recovery is EARNED — followthrough
# above the bar AND a minimum landing count; below the confidence floor the
# epoch/config default wins over observed behavior.
READER_CONFIDENCE_FLOOR = 0.3
READER_RECOVERY_FOLLOWTHROUGH = 0.7
READER_RECOVERY_LANDINGS = 2
READER_DEFAULT_CAPABILITY = "expanded"

# Fallbacks mirroring config.Budgets defaults, used only when even the
# config object is broken (fail-open floor of the floor).
_FALLBACK_DIGEST_TOKENS = 480
_FALLBACK_RESULT_TOKENS = 1200
_FALLBACK_FAILURE_FACTOR = 2.0
_FALLBACK_TURN_RETRIEVAL_TOKENS = 2800
_FALLBACK_WINDOW_PRESSURE_PCT = 70

# "No configured ceiling": large enough to never bind in practice while
# keeping the plan-id input a plain int (EDC §13: ceiling is applied last).
DEFAULT_HARD_CEILING = 1_000_000


# ------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class EnvironmentSignals:
    """The LADDERS signal record, environment half: proxy ground truth."""

    window_pct: float | None = None
    model_id: str | None = None


@dataclass(frozen=True)
class SessionState:
    """Observed session behavior: circuit state (reflex latches, read-only)
    plus the ReaderState fields of EDC §6."""

    circuit: str = "normal"  # normal | dense | bypass (per family/signature)
    reader_preference: str | None = None
    reader_latched_inline: bool = False
    followthrough: float = 0.0
    landings: int = 0
    confidence: float = 0.0


@dataclass(frozen=True)
class DeliveryPlan:
    """EDC §5.4: the resolver's only output. Everything the deterministic
    renderer needs, nothing it may reinterpret."""

    mode: str  # pass_summary | fail_census | dense | bypass | flood
    census: str  # none | complete | bounded (bounded = hierarchically
    #             compacted, identity-preserving — NEVER identity-dropping)
    item_summary: str  # none | class_only | one_line | expanded
    inline_detail_count: int
    include_addresses: bool  # teaching-level address prose ONLY; contract-
    #                          driven addresses ride in every mode regardless
    include_teaching: bool
    token_budget: int
    evidence_floor: int
    hard_ceiling: int
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown plan mode: {self.mode!r}")
        if self.census not in CENSUS_LEVELS:
            raise ValueError(f"unknown census level: {self.census!r}")
        if self.item_summary not in ITEM_SUMMARY_LEVELS:
            raise ValueError(f"unknown item_summary level: {self.item_summary!r}")
        for reason in self.reasons:
            if reason not in REASON_VOCABULARY:
                raise ValueError(f"reason outside the closed vocabulary: {reason!r}")

    @property
    def plan_id(self) -> str:
        """Stable identity over the non-reason fields (EDC §5.3 amendment
        3): outcome events record it, making P_rerun plan-conditional.
        Reasons are excluded — they explain the selection, they are not it."""
        doc = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "reasons"
        }
        blob = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------- signal readers
def read_window(ws_root: Path | str | None) -> tuple[float | None, str | None]:
    """Tiny fail-open reader of the Tier-0 proxy's ground truth at
    ``<workspace>/.ctx-session-reads/proxy/window.json`` → (window_pct,
    model_id). Deliberately replicated from (never imported out of)
    ctx.hook: the hook is the safety plane and this module must not create
    an import edge into it. Any missing/corrupt telemetry → (None, None)."""
    if ws_root is None:
        return None, None
    try:
        path = session_reads_path(ws_root, "proxy", "window.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        pct_raw = doc.get("window_pct")
        pct = (
            float(pct_raw)
            if isinstance(pct_raw, (int, float)) and not isinstance(pct_raw, bool)
            else None
        )
        model_raw = doc.get("model")
        model = str(model_raw) if isinstance(model_raw, str) and model_raw else None
        return pct, model
    except Exception:
        return None, None


def environment_signals(ws_root: Path | str | None) -> EnvironmentSignals:
    """Assemble the environment half of the signal record. Fail-open."""
    pct, model = read_window(ws_root)
    return EnvironmentSignals(window_pct=pct, model_id=model)


def _reader_state_path(ws_root: Path | str) -> Path:
    return session_reads_path(ws_root, _READER_STATE_NAME)


def _read_reader_state(ws_root: Path | str | None) -> dict[str, Any]:
    if ws_root is None:
        return {}
    try:
        doc = json.loads(_reader_state_path(ws_root).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _write_reader_state(ws_root: Path | str, state: dict[str, Any]) -> None:
    """Atomic write (temp + rename), mirroring ctx.reflex's discipline.
    Raises to the caller; every caller is fail-open."""
    path = _reader_state_path(ws_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=_READER_STATE_NAME + ".")
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


def note_reader_drop(ws_root: Path | str | None) -> None:
    """The session dropped to inline evidence consumption: LATCH it (EDC §6
    amendment 2 — preference transitions follow the latching discipline).
    Recovery is earned via :func:`note_reader_evidence`. Fail-open."""
    if ws_root is None:
        return
    try:
        state = _read_reader_state(ws_root)
        state["preference"] = "inline"
        state["latched_inline"] = True
        _write_reader_state(ws_root, state)
    except Exception:
        pass


def note_reader_evidence(
    ws_root: Path | str | None,
    *,
    followthrough: float,
    landings: int,
    confidence: float | None = None,
) -> None:
    """Record observed reader behavior. Clears the inline latch only when
    recovery is EARNED: followthrough > 0.7 AND landings >= 2. Fail-open."""
    if ws_root is None:
        return
    try:
        state = _read_reader_state(ws_root)
        state["followthrough"] = float(followthrough)
        state["landings"] = int(landings)
        if confidence is not None:
            state["confidence"] = float(confidence)
        if (
            state.get("latched_inline")
            and float(followthrough) > READER_RECOVERY_FOLLOWTHROUGH
            and int(landings) >= READER_RECOVERY_LANDINGS
        ):
            state["latched_inline"] = False
            state["preference"] = None
        _write_reader_state(ws_root, state)
    except Exception:
        pass


def session_state(
    ws_root: Path | str | None, signature: str | None = None
) -> SessionState:
    """Assemble session behavior from the reflex latches (READ-ONLY — the
    reflex arc owns its own writes) and the reader-capability state file.
    ``signature``/family scoping: circuit latches are per-signature; with no
    signature the circuit is ``normal``. Fail-open to the empty state."""
    circuit = "normal"
    try:
        if ws_root is not None and signature:
            from ctx import reflex

            state = reflex.read_state(ws_root)
            bypass = state.get("bypass")
            densify = state.get("densify")
            if isinstance(bypass, dict) and bypass.get(signature):
                circuit = "bypass"
            elif isinstance(densify, dict) and densify.get(signature):
                circuit = "dense"
    except Exception:
        circuit = "normal"
    reader = _read_reader_state(ws_root)
    try:
        return SessionState(
            circuit=circuit,
            reader_preference=(
                str(reader["preference"])
                if isinstance(reader.get("preference"), str)
                else None
            ),
            reader_latched_inline=bool(reader.get("latched_inline")),
            followthrough=float(reader.get("followthrough") or 0.0),
            landings=int(reader.get("landings") or 0),
            confidence=float(reader.get("confidence") or 0.0),
        )
    except Exception:
        return SessionState(circuit=circuit)


# ----------------------------------------------------- reader capability
def infer_reader_capability(
    session: SessionState,
    *,
    default: str = READER_DEFAULT_CAPABILITY,
) -> str:
    """EDC §6 posterior decision: tier/epoch default is the prior, observed
    session behavior is the evidence.

    Amendments encoded: (a) below the confidence floor (0.3) the epoch/
    config ``default`` wins over any observed preference; (b) a drop to
    ``inline`` LATCHES — recovery requires followthrough > 0.7 AND >= 2
    landings (earned, never automatic). Pure function; never raises."""
    try:
        if float(session.confidence) < READER_CONFIDENCE_FLOOR:
            return default
        if session.reader_latched_inline:
            recovered = (
                float(session.followthrough) > READER_RECOVERY_FOLLOWTHROUGH
                and int(session.landings) >= READER_RECOVERY_LANDINGS
            )
            return default if recovered else "inline"
        return session.reader_preference or default
    except Exception:
        return default


# ------------------------------------------------------------- validation
def validate_rendering_policy(contract_rendering: dict[str, Any]) -> None:
    """Contract-load-seam validation (EDC §13): ``floor <= ceiling`` or the
    contract is rejected loudly HERE — the resolver itself is fail-open and
    would otherwise mask a nonsense table forever."""
    floor = contract_rendering.get("evidence_floor")
    ceiling = contract_rendering.get("hard_ceiling")
    if floor is None or ceiling is None:
        return
    if int(floor) > int(ceiling):
        raise ValueError(
            f"contract rendering policy invalid: evidence_floor {floor} > "
            f"hard_ceiling {ceiling} (floor must never exceed ceiling)"
        )


# ---------------------------------------------------------------- resolver
def _budgets_attr(config_budgets: Any, name: str, fallback: Any) -> Any:
    try:
        value = getattr(config_budgets, name)
        return fallback if value is None else value
    except Exception:
        return fallback


def _pressure_factor(window_pct: float | None, threshold: int) -> float | None:
    """The SAME deterministic linear ramp the hook applies to native-read
    budgets (docs/LADDERS.md edge 2 — the two halves of the defense finally
    meet): 2 points of budget per point of fullness above threshold,
    floored at a quarter. None below threshold / without telemetry."""
    if window_pct is None or window_pct < threshold:
        return None
    return max(0.25, 1 - (window_pct - threshold) / 100 * 2)


def _safe_default_plan(
    evidence_outcome: str, contract_rendering: Any, config_budgets: Any
) -> DeliveryPlan:
    """The fail-open plan: mirrors pre-EDC budget behavior exactly (base
    tokens, failure multiplier, no pressure, no floor, no ceiling)."""
    try:
        base = int(
            (contract_rendering or {}).get("base_tokens")
            or _budgets_attr(config_budgets, "digest_tokens", _FALLBACK_DIGEST_TOKENS)
        )
    except Exception:
        base = _FALLBACK_DIGEST_TOKENS
    success = evidence_outcome == "success"
    budget = base
    if not success:
        try:
            factor = float(
                _budgets_attr(
                    config_budgets, "failure_budget_factor", _FALLBACK_FAILURE_FACTOR
                )
            )
        except Exception:
            factor = _FALLBACK_FAILURE_FACTOR
        budget = int(budget * factor)
    return DeliveryPlan(
        mode="pass_summary" if success else "fail_census",
        census="none" if success else "complete",
        item_summary="none" if success else "one_line",
        inline_detail_count=0 if success else 1,
        include_addresses=True,
        include_teaching=True,
        token_budget=budget,
        evidence_floor=0,
        hard_ceiling=DEFAULT_HARD_CEILING,
        reasons=("fail_open_default",),
    )


def resolve_delivery(
    evidence_outcome: str,
    family: str,
    *,
    contract_rendering: dict[str, Any],
    session: SessionState,
    environment: EnvironmentSignals,
    config_budgets: Any,
) -> DeliveryPlan:
    """EDC §13, the five-input resolver. Deterministic: the plan is a pure
    function of the arguments. Fail-open: any internal error yields the
    safe default plan mirroring pre-EDC behavior.

    ``contract_rendering`` keys (all optional, duck-typed toward the
    Evidence Contract's rendering table):

    * ``base_tokens`` — the base emission budget (defaults to the config
      digest budget). Callers pass the result-vs-digest choice here.
    * ``failure_factor`` — override of the config failure multiplier.
    * ``evidence_floor`` / ``hard_ceiling`` — the contract's budget bounds
      (``floor <= ceiling`` enforced at load by
      :func:`validate_rendering_policy`). Default floor: the config digest
      budget for failures (failure evidence is never squeezed below the
      standard digest), 0 for successes.
    * ``census_min_tokens`` — the smallest fit of the REQUIRED census; when
      even the ceiling cannot fit it, the plan escalates to ``flood`` with
      declared partial inline coverage (never silent truncation).
    * ``retrievable_nonempty`` — informational; addresses ride regardless
      (contract-driven, EDC §12 correction 1).
    """
    del family  # v1: family selects the contract upstream; kept in the
    #             frozen signature for plan-conditional epoch tables.
    try:
        reasons: list[str] = []
        contract = contract_rendering or {}
        validate_rendering_policy(contract)

        budgets_digest = int(
            _budgets_attr(config_budgets, "digest_tokens", _FALLBACK_DIGEST_TOKENS)
        )
        base = int(contract.get("base_tokens") or budgets_digest)
        success = evidence_outcome == "success"

        # ---- mode: circuit overrides outcome (EDC §13 adopted order).
        if session.circuit == "bypass":
            mode = "bypass"
            reasons.append("circuit_bypass")
        elif session.circuit in ("dense", "densify"):
            mode = "dense"
            reasons.append("circuit_dense")
        elif success:
            mode = "pass_summary"
            reasons.append("outcome_success")
        else:
            mode = "fail_census"
            reasons.append("outcome_failure")

        # ---- budget: failure multiplier, THEN pressure multiplier, THEN
        # floor (max), THEN ceiling (min). Frozen order: the floor comes
        # after the multipliers so pressure never squeezes below it.
        budget = base
        if not success:
            factor = float(
                contract.get(
                    "failure_factor",
                    _budgets_attr(
                        config_budgets, "failure_budget_factor", _FALLBACK_FAILURE_FACTOR
                    ),
                )
            )
            budget = int(budget * factor)
            reasons.append("failure_multiplier")

        threshold = int(
            _budgets_attr(
                config_budgets, "window_pressure_pct", _FALLBACK_WINDOW_PRESSURE_PCT
            )
        )
        pressure = _pressure_factor(environment.window_pct, threshold)
        if pressure is not None:
            budget = int(budget * pressure)
            reasons.append("window_pressure")

        floor = int(
            contract.get("evidence_floor", budgets_digest if not success else 0)
        )
        ceiling = int(contract.get("hard_ceiling", DEFAULT_HARD_CEILING))
        if budget < floor:
            budget = floor
            reasons.append("evidence_floor")
        if budget > ceiling:
            budget = ceiling
            reasons.append("hard_ceiling")

        # ---- census fit: an unfittable REQUIRED census escalates to FLOOD
        # with declared partial inline coverage — never silent truncation.
        census_min = contract.get("census_min_tokens")
        if (
            mode == "fail_census"
            and census_min is not None
            and int(census_min) > ceiling
        ):
            mode = "flood"
            reasons.append("census_unfittable")

        # ---- reader capability (EDC §6): inline latch narrows detail; the
        # confidence floor keeps low-evidence sessions on epoch defaults.
        capability = infer_reader_capability(session)
        if session.reader_latched_inline and capability == "inline":
            reasons.append("reader_inline_latch")
        elif (
            session.confidence
            and float(session.confidence) < READER_CONFIDENCE_FLOOR
        ):
            reasons.append("reader_low_confidence")

        # ---- per-mode knobs. ``bounded`` census is hierarchically
        # compacted and identity-preserving (EDC §5.3 amendment 4).
        if mode == "pass_summary":
            census, item_summary, detail = "none", "none", 0
        elif mode == "fail_census":
            census = "complete"
            item_summary = "class_only" if capability == "inline" else "one_line"
            detail = 1
        elif mode == "dense":
            census, item_summary, detail = "complete", "expanded", 1
        elif mode == "bypass":
            census, item_summary, detail = "complete", "class_only", 0
        else:  # flood
            census, item_summary, detail = "bounded", "class_only", 0

        # Teaching prose is the first rung dropped (degradation order:
        # teaching before evidence); addresses are NOT teaching and are
        # never suppressed by the plan (contract-driven provenance).
        include_teaching = mode not in ("bypass", "flood")

        return DeliveryPlan(
            mode=mode,
            census=census,
            item_summary=item_summary,
            inline_detail_count=detail,
            include_addresses=True,
            include_teaching=include_teaching,
            token_budget=int(budget),
            evidence_floor=floor,
            hard_ceiling=ceiling,
            reasons=tuple(reasons),
        )
    except Exception:
        return _safe_default_plan(evidence_outcome, contract_rendering, config_budgets)


# --------------------------------------------------------------- retrieval
def resolve_retrieval_budget(
    config: Any,
    environment: EnvironmentSignals | None = None,
    *,
    requested: int | None = None,
) -> int:
    """The retrieval paths' single budget choke point (LADDERS edge 8).

    Today this returns exactly the current values — ``requested`` when the
    caller carries an explicit budget (``ctx map --budget``), otherwise the
    configured turn-retrieval budget — so behavior is unchanged by
    construction. The window-pressure hook-in for retrieval budgets lands
    here later, in ONE place, instead of in seven. Fail-open."""
    del environment  # reserved for the pressure hook-in
    try:
        if requested is not None:
            return int(requested)
        return int(config.budgets.turn_retrieval_tokens)
    except Exception:
        return (
            int(requested)
            if requested is not None
            else _FALLBACK_TURN_RETRIEVAL_TOKENS
        )


# --------------------------------------------------------------- telemetry
def record_plan_receipt(audit_dir: Path | str | None, plan: DeliveryPlan) -> None:
    """Append the plan receipt to the existing telemetry stream (LADDERS §3
    item 3: receipts per branch make every conditional measurable; the
    epoch compiler needs plan_id-tagged emissions). One JSON line, sorted
    keys, ``op: plan``. Fail-open: telemetry must never block emission."""
    if audit_dir is None:
        return
    try:
        import time

        path = Path(audit_dir) / "telemetry.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "op": "plan",
                "plan_id": plan.plan_id,
                "mode": plan.mode,
                "reasons": list(plan.reasons),
                "ts": time.time(),
            },
            sort_keys=True,
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
