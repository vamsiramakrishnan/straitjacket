"""Learned policy epochs: run telemetry compiled into committed policy.

The guard's built-in tables are static priors. Every ``ctx run`` capture,
however, records ground truth — the actual byte volume a command produced in
this repository. ``compile_policy`` distills that telemetry into a small,
deterministic, *reviewable* artifact (``ctx-policy.toml``): command
signatures whose output is reliably bounded are PROMOTED (the hook then
treats them like ``guard.allow_commands`` canonical prefixes), and any
signature ever observed flooding is DEMOTED (never allowed via promotion).

The compiled file is meant to be reviewed and committed like code — the
epoch id is a content hash of the policy body, so two compiles of the same
telemetry produce byte-identical output and a stable id. Nothing here runs
on the hook hot path; the hook only re-reads the rendered TOML fail-open.

REFLEX slow loop (layer 4): the compiler also aggregates the reflex-arc
outcome ledger (``.ctx-session-reads/reflex-outcomes.jsonl``) into a
``[digest_density]`` table — per command signature, the starting digest
density the next epoch should use. Signatures that repeatedly starved
(re-runs after their digest) start ``"dense"`` so the in-session reflex has
nothing left to correct. The section is additive: the hook's policy loader
reads only schema/promoted/demoted and ignores it, and nothing consumes it
at render time yet — epoch consumption is deliberately deferred.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from ctx.store import Store, canonical_json
from ctx.workspace import Workspace

POLICY_SCHEMA = "ctx.policy/v1"
POLICY_FILENAME = "ctx-policy.toml"

# Most recent run manifests considered per compile; keeps compilation O(1)
# in store age and makes the input window explicit.
_SCAN_LIMIT = 500

# A single observation above DEMOTE_MULTIPLE × cap marks the signature as a
# flooder regardless of how well-behaved its p95 is.
_DEMOTE_MULTIPLE = 4

# Digest-density promotion threshold (REFLEX slow loop). A signature enters
# [digest_density] only when the outcome ledger shows at least this many
# starvation events for it — the same "two independent observations before
# the policy moves" conservatism as min_runs/_DEMOTE_MULTIPLE above. And it
# NEVER enters when landings >= starvations: a model that follows the
# digest's addresses at least as often as it re-runs is being served by the
# lean form, and lean digests stay the default (the asymmetric-loss prior
# only pays for density where starvation dominates).
_DENSIFY_MIN_STARVATION = 2

# A "subcommand" is a bare lowercase word (``git status``, ``cargo build``).
# Script paths, code snippets, and option values never qualify, so
# ``python3 script.py`` collapses to the signature ``python3``.
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def command_signature(argv: list[str]) -> str | None:
    """Canonical signature for grouping runs: ``argv[0]`` basename plus the
    first non-flag subcommand when it looks like one.

    ``["git", "status", "--short"]`` → ``"git status"``;
    ``["pytest", "-q"]`` → ``"pytest"``;
    ``["python3", "script.py"]`` → ``"python3"`` (a path is not a subcommand).
    Returns None for empty argv."""
    if not argv:
        return None
    prog = os.path.basename(str(argv[0])).strip()
    if not prog:
        return None
    sub = next((str(a) for a in argv[1:] if not str(a).startswith("-")), None)
    if sub is not None and _SUBCOMMAND_RE.match(sub):
        return f"{prog} {sub}"
    return prog


def _p95(values: list[int]) -> int:
    """Deterministic nearest-rank p95 over a non-empty list."""
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[idx]


def _reflex_tallies(ledger_dir: Path) -> dict[str, dict[str, int]]:
    """Per-signature starvation/landing tallies from the reflex outcome
    ledger (``<ledger_dir>/reflex-outcomes.jsonl``; one JSON object per
    line: ``{"ts", "event", "signature", "run", "action"}``).

    Fail-open like the manifest scan above: a missing or unreadable ledger,
    and any individually corrupt line, contribute nothing."""
    tallies: dict[str, dict[str, int]] = {}
    try:
        lines = (
            (Path(ledger_dir) / "reflex-outcomes.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        return tallies
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        event = rec.get("event")
        sig = rec.get("signature")
        if event not in ("starvation", "landing"):
            continue  # friction scores guard outcomes, not digest density
        if not isinstance(sig, str) or not sig.strip():
            continue
        t = tallies.setdefault(sig.strip(), {"starvation": 0, "landing": 0})
        t[event] += 1
    return tallies


def _run_total_bytes(manifest: dict[str, Any]) -> int:
    total = 0
    for stream in (manifest.get("streams") or {}).values():
        if isinstance(stream, dict):
            try:
                total += int(stream.get("bytes", 0))
            except (TypeError, ValueError):
                pass
    return total


def compile_policy(
    store: Store,
    ws: Workspace,
    min_runs: int = 5,
    max_p95_bytes: int | None = None,
) -> dict[str, Any]:
    """Compile a policy epoch from the store's run telemetry.

    Scans the most recent ``_SCAN_LIMIT`` run manifests (catalog kind='run',
    ordered by created_at). Per command signature the total stream bytes of
    each run are collected. A signature observed at least ``min_runs`` times
    whose p95 total bytes fits inside ``max_p95_bytes`` (default: the
    workspace's ``budgets.max_inline_bytes``) is promoted. Any single
    observation above ``_DEMOTE_MULTIPLE`` × cap demotes the signature —
    excluded from promotion even when its p95 passes.

    Additionally aggregates the reflex outcome ledger under
    ``<ws.root>/.ctx-session-reads/`` into ``digest_density`` (REFLEX slow
    loop): a signature with >= ``_DENSIFY_MIN_STARVATION`` starvation events
    and strictly more starvations than landings compiles to ``"dense"``.

    Returns ``{"schema", "epoch", "promoted", "demoted"}`` plus
    ``"digest_density"`` when non-empty, where ``epoch`` is the first 12 hex
    chars of the sha256 of the canonical body (without the epoch itself) —
    deterministic across compiles of the same inputs. The density table is
    part of the hashed body only when non-empty, so pre-reflex epochs keep
    their ids and recompiling unchanged telemetry stays a no-op diff."""
    cap = int(max_p95_bytes or ws.config.budgets.max_inline_bytes)
    rows = store.db.execute(
        "SELECT id FROM objects WHERE kind='run' "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (_SCAN_LIMIT,),
    ).fetchall()

    observed: dict[str, list[int]] = {}
    for (obj_id,) in rows:
        try:
            manifest = store.get_manifest(obj_id)
        except Exception:
            continue  # a missing/corrupt manifest never blocks compilation
        if manifest.get("schema") != "ctx.invocation/v1":
            continue
        if manifest.get("shell"):
            continue  # shell strings have no reliable argv signature
        argv = manifest.get("argv")
        if not isinstance(argv, list):
            continue
        sig = command_signature([str(a) for a in argv])
        if not sig:
            continue
        observed.setdefault(sig, []).append(_run_total_bytes(manifest))

    promoted: list[dict[str, Any]] = []
    demoted: list[str] = []
    for sig in sorted(observed):
        totals = observed[sig]
        if max(totals) > _DEMOTE_MULTIPLE * cap:
            demoted.append(sig)
            continue
        if len(totals) >= int(min_runs):
            p95 = _p95(totals)
            if p95 <= cap:
                promoted.append(
                    {"signature": sig, "runs": len(totals), "p95_bytes": p95}
                )

    # Digest-density promotion (REFLEX slow loop). Rule, mirroring the
    # promote/demote conservatism above:
    #   - >= _DENSIFY_MIN_STARVATION starvation events for the signature in
    #     the telemetry window (one event never moves policy), AND
    #   - starvations strictly greater than landings — a signature whose
    #     landings keep pace is following addresses and keeps lean digests.
    digest_density: dict[str, str] = {}
    tallies = _reflex_tallies(ws.root / ".ctx-session-reads")
    for sig in sorted(tallies):
        t = tallies[sig]
        if (
            t["starvation"] >= _DENSIFY_MIN_STARVATION
            and t["landing"] < t["starvation"]
        ):
            digest_density[sig] = "dense"

    body = {"schema": POLICY_SCHEMA, "promoted": promoted, "demoted": demoted}
    if digest_density:
        # Additive: absent from the hash body when empty so pre-reflex
        # epochs keep their ids.
        body["digest_density"] = digest_density
    epoch = hashlib.sha256(canonical_json(body)).hexdigest()[:12]
    out: dict[str, Any] = {
        "schema": POLICY_SCHEMA,
        "epoch": epoch,
        "promoted": promoted,
        "demoted": demoted,
    }
    if digest_density:
        out["digest_density"] = digest_density
    return out


def render_policy(policy: dict[str, Any]) -> str:
    """Deterministic TOML rendering of a compiled policy epoch."""
    lines = [
        f"# {POLICY_FILENAME} — compiled context-guard policy (learned policy epoch).",
        "#",
        "# COMPILED, REVIEWABLE, COMMITTED LIKE CODE. This file is generated by",
        "# `ctx policy compile` from run telemetry in the artifact store: command",
        "# signatures whose observed output is reliably bounded are promoted (the",
        "# guard treats them as allow prefixes); signatures ever observed flooding",
        "# are demoted and are never allowed via promotion. Review the diff like",
        "# any code change; do not hand-edit — recompile instead.",
        "#",
        f"# epoch: {policy.get('epoch', '')}",
        "",
        f'schema = {json.dumps(str(policy.get("schema", POLICY_SCHEMA)))}',
        f'epoch = {json.dumps(str(policy.get("epoch", "")))}',
        "",
    ]
    demoted = list(policy.get("demoted") or [])
    if demoted:
        lines.append("demoted = [")
        for sig in demoted:
            lines.append(f"  {json.dumps(str(sig))},")
        lines.append("]")
    else:
        lines.append("demoted = []")
    for entry in policy.get("promoted") or []:
        lines.extend(
            [
                "",
                "[[promoted]]",
                f'signature = {json.dumps(str(entry["signature"]))}',
                f'runs = {int(entry["runs"])}',
                f'p95_bytes = {int(entry["p95_bytes"])}',
            ]
        )
    density = policy.get("digest_density") or {}
    if density:
        lines.extend(
            [
                "",
                "# Starting digest density per command signature (REFLEX slow",
                "# loop): compiled from the reflex outcome ledger. A signature",
                "# is listed only after repeated starvation (>= 2 events) with",
                "# starvations exceeding landings. Not consumed by rendering",
                "# yet — the in-session reflex handles densify; the guard's",
                "# policy loader ignores this table (fail-open).",
                "[digest_density]",
            ]
        )
        for sig in sorted(density):
            lines.append(f"{json.dumps(str(sig))} = {json.dumps(str(density[sig]))}")
    return "\n".join(lines) + "\n"


def write_policy(ws: Workspace, policy: dict[str, Any]) -> Path:
    """Write the rendered epoch to ``<workspace>/ctx-policy.toml`` and return
    the path. The file is the reviewable artifact the hook consumes."""
    path = ws.root / POLICY_FILENAME
    path.write_text(render_policy(policy), encoding="utf-8")
    return path
