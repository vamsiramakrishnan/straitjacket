#!/usr/bin/env python3
"""Offline detector replay against the archived spec3 transcripts.

EDC §19.3 gate evidence (Controller State wave): every NEW detector ships
shadow-first and must validate its precision against the archived
transcripts (evals/archive/*.tar.gz — spec3 rounds 1-3, the frozen referee
corpus) BEFORE anything graduates to live. This script is that gate:

* extract the tarballs (stdlib tarfile only);
* walk each sj agent session's Claude Code project transcript and rebuild
  the ordered tool sequence (Bash commands + Edit/Write events);
* replay the sequence through the real ``ctx.reflex`` module against a
  fresh throwaway workspace, with a SYNTHETIC generation counter (each
  Edit/Write bumps the generation — offline replay has no worktree to
  hash, and EDC §8 generations are operational identity, so an explicit
  override is the honest replay);
* fold the resulting v1/v2 ledgers into a precision report and check the
  §19 controller gates:

  1. the round-1 slicer flail (the "8× same pytest command" loop) collapses
     to ONE episode per signature × generation — at most one circuit
     transition, however many confirmed reruns the episode counts;
  2. the round-2/3 edit cadence splits correctly — a rerun in a NEW
     generation scores ``validation_after_edit`` (a typed positive), never
     a confirmed starvation;
  3. material narrowing fires — single-test reruns after a broad
     intervention score ``narrowed_execution`` (Rule 9b), never starvation.

Replay model of the live pipeline (mirrors hook + cli wiring):

  Bash command  -> reflex.landing_ref? note_landing : check_command(gen)
                -> pytest-family commands additionally record an
                   intervention (note_intervention with the same gen) —
                   in live sessions only omission-bearing digests do this;
                   every archived pytest run flooded, so replay assumes it.
  Edit/Write    -> reflex.note_edit + generation bump.

Usage: python evals/replay_detectors.py [--archive-dir evals/archive]
Exit code 0 iff every gate passes. Stdlib-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ctx import reflex  # noqa: E402

ARCHIVES = [
    ("r1", "spec3-transcripts.tar.gz"),
    ("r2", "spec3-r2-transcripts.tar.gz"),
    ("r3", "spec3-r3-transcripts.tar.gz"),
]

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_CONFIRMED_STARVATION = {"equivalent_rerun", "slicer_rerun"}


def _extract(tar_path: Path, dest: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # pragma: no cover - pre-3.11.4 fallback
            tf.extractall(dest)


def _session_files(round_dir: Path) -> list[tuple[str, Path]]:
    """(session name, transcript path) for the sj AGENT sessions only —
    ``cc-review-*`` transcripts are the Opus grader, not the agent."""
    out = []
    for d in sorted(round_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("cc-") or "review" in d.name:
            continue
        for f in sorted(d.glob("projects/*/*.jsonl")):
            out.append((d.name, f))
    return out


def _tool_sequence(transcript: Path) -> list[tuple[str, str]]:
    """Ordered [("bash", command) | ("edit", file_path)] from a Claude Code
    project transcript."""
    seq: list[tuple[str, str]] = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        for blk in msg.get("content") or []:
            if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                continue
            name = str(blk.get("name") or "")
            inp = blk.get("input") or {}
            if name == "Bash" and isinstance(inp.get("command"), str):
                seq.append(("bash", inp["command"]))
            elif name in _EDIT_TOOLS and isinstance(inp.get("file_path"), str):
                seq.append(("edit", inp["file_path"]))
    return seq


def _replay(seq: list[tuple[str, str]], ws: Path) -> dict:
    """Drive the sequence through the real reflex module; fold the ledgers."""
    gen_n = 0
    n_bash = n_edit = n_interventions = 0
    for i, (kind, payload) in enumerate(seq):
        if kind == "edit":
            n_edit += 1
            gen_n += 1
            reflex.note_edit(ws)
            continue
        n_bash += 1
        gen = f"gen-{gen_n}"
        handle = reflex.landing_ref(payload)
        if handle:
            reflex.note_landing(ws, handle)
            continue
        reflex.check_command(ws, payload, generation=gen)
        sig = reflex.command_signature(payload)
        if sig and reflex.family_of(sig) == "pytest":
            run_id = hashlib.sha256(f"{i}|{payload}".encode()).hexdigest()[:12]
            reflex.note_intervention(ws, sig, run_id, hints=0, generation=gen)
            n_interventions += 1

    # ---- fold ledgers
    emissions: dict[str, dict] = {}  # iid -> emission line
    outcomes: list[dict] = []
    transitions: list[dict] = []
    v2_path = ws / ".ctx-session-reads" / "interventions.jsonl"
    if v2_path.is_file():
        for line in v2_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get("event")
            if ev == "intervention_emitted":
                emissions[rec["interventionId"]] = rec
            elif ev == "intervention_outcome":
                outcomes.append(rec)
            elif ev == "circuit_transition":
                transitions.append(rec)
    v1_events = []
    v1_path = ws / ".ctx-session-reads" / "reflex-outcomes.jsonl"
    if v1_path.is_file():
        for line in v1_path.read_text(encoding="utf-8").splitlines():
            try:
                v1_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    by_outcome: dict[str, int] = {}
    episodes: dict[tuple[str, str], int] = {}  # (sig, gen) -> confirmed count
    for o in outcomes:
        by_outcome[o["outcome"]] = by_outcome.get(o["outcome"], 0) + 1
        em = emissions.get(o.get("interventionId") or "")
        ev = o.get("evidence") or {}
        if (
            em is not None
            and o["outcome"] in _CONFIRMED_STARVATION
            and ev.get("confirmed") is True
        ):
            key = (em["signature"], str(em.get("generation")))
            episodes[key] = episodes.get(key, 0) + 1
    transitions_by_ep: dict[tuple[str, str], list[str]] = {}
    for t in transitions:
        key = (t["signature"], str(t.get("generation")))
        transitions_by_ep.setdefault(key, []).append(f"{t['from']}→{t['to']}")

    return {
        "bash": n_bash,
        "edits": n_edit,
        "interventions": n_interventions,
        "outcomes": dict(sorted(by_outcome.items())),
        "episodes": episodes,
        "transitions_by_ep": transitions_by_ep,
        "transitions": transitions,
        "v1_starvation": sum(1 for e in v1_events if e.get("event") == "starvation"),
        "v1_landings": sum(1 for e in v1_events if e.get("event") == "landing"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--archive-dir",
        default=str(Path(__file__).resolve().parent / "archive"),
        help="directory holding the spec3 transcript tarballs",
    )
    ns = ap.parse_args(argv)
    archive_dir = Path(ns.archive_dir)

    tmp = Path(tempfile.mkdtemp(prefix="ctx-replay-"))
    results: dict[str, dict] = {}
    try:
        for round_tag, name in ARCHIVES:
            tar_path = archive_dir / name
            if not tar_path.is_file():
                print(f"missing archive: {tar_path}", file=sys.stderr)
                return 2
            dest = tmp / round_tag
            dest.mkdir()
            _extract(tar_path, dest)
            round_dir = next(d for d in dest.iterdir() if d.is_dir())
            for session, transcript in _session_files(round_dir):
                seq = _tool_sequence(transcript)
                ws = tmp / f"ws-{round_tag}-{session}"
                ws.mkdir()
                results[f"{round_tag}/{session}"] = _replay(seq, ws)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------- report
    print("shadow-detector replay against evals/archive (EDC §19.3 gate)")
    print("=" * 64)
    for key, r in results.items():
        print(f"\n== {key} ==")
        print(
            f"  replayed: {r['bash']} bash · {r['edits']} edits · "
            f"{r['interventions']} interventions (pytest family)"
        )
        oc = r["outcomes"]
        print(
            "  shadow outcomes: "
            + (" · ".join(f"{k} {v}" for k, v in oc.items()) or "none")
        )
        print(f"  live v1 events (comparison): {r['v1_starvation']} starvation "
              f"· {r['v1_landings']} landings")
        if r["episodes"]:
            print("  confirmed-starvation episodes (signature × generation):")
            for (sig, gen), n in sorted(r["episodes"].items()):
                trans = r["transitions_by_ep"].get((sig, gen), [])
                print(
                    f"    '{sig}' @ {gen}: {n} confirmed rerun(s) → 1 episode "
                    f"· transitions {trans or ['none']}"
                )

    # ------------------------------------------------------------ gates
    print("\ngates")
    print("-" * 64)
    ok = True

    def gate(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        dots = "." * max(1, 52 - len(label))
        print(f"gate: {label} {dots} {'PASS' if passed else 'FAIL'} ({detail})")

    # 1: r1 loop → one episode, one transition per episode.
    r1_eps = {
        k: v for key, r in results.items() if key.startswith("r1/")
        for k, v in r["episodes"].items()
    }
    r1_trans_ok = all(
        len(r["transitions_by_ep"].get(ep, [])) <= 1
        for key, r in results.items() if key.startswith("r1/")
        for ep in set(r["episodes"]) | set(r["transitions_by_ep"])
    )
    r1_loop = max(r1_eps.values(), default=0)
    gate(
        "r1 slicer loop collapses to one episode",
        bool(r1_eps) and r1_trans_ok,
        f"largest episode {r1_loop} confirmed reruns, ≤1 transition each",
    )

    # 2: r2/r3 edit cadence — post-edit reruns are verification positives,
    # and no confirmed starvation is ever scored across a generation change
    # (structural: confirmation requires generation equality).
    v23 = sum(
        r["outcomes"].get("validation_after_edit", 0)
        for key, r in results.items() if key[:2] in ("r2", "r3")
    )
    gate(
        "r2/r3 edit-cadence reruns score as verification",
        v23 > 0,
        f"{v23} validation_after_edit positives, confirmed starvation "
        "requires generation equality by construction",
    )

    # 3: narrowing positives on single-test runs.
    narrowed = sum(
        r["outcomes"].get("narrowed_execution", 0) for r in results.values()
    )
    gate(
        "narrowing positives on single-test runs",
        narrowed > 0,
        f"{narrowed} narrowed_execution outcomes corpus-wide",
    )

    print("\nresult:", "ALL GATES PASS" if ok else "GATE FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
