"""Large deterministic cross-policy corpus for orchestration AlphaEvolve runs."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
from collections import Counter

from ctx.handoff_policy import choose_handoff
from ctx.mutation_policy import choose_mutation_isolation
from ctx.verification_policy import choose_verification
from ctx.wave_policy import choose_wave


def _record(digest, row: dict) -> None:
    digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
    digest.update(b"\n")


def run_matrix() -> dict:
    started = time.perf_counter()
    digest = hashlib.sha256()
    failures: list[dict] = []
    counts: Counter[str] = Counter()
    cases = 0

    # Cross the scheduler, mutation boundary, and verifier route over a broad
    # task/host state space. Some axes are deliberately irrelevant to one
    # policy: invariance under irrelevant state is itself part of the corpus.
    for ready in range(26):
        for mutations in range(ready + 1):
            readonly = ready - mutations
            for rate_limited, max_workers, isolated, declared, overlap, complexity, alternate in itertools.product(
                (False, True), range(1, 9), (False, True), (False, True),
                (False, True), (1, 3, 5), (False, True),
            ):
                state = {
                    "ready_count": ready,
                    "mutation_count": mutations,
                    "readonly_count": readonly,
                    "provider_rate_limited": rate_limited,
                    "max_workers": max_workers,
                    "isolated_worktrees": isolated,
                    "shared_workspace": not isolated,
                    "targets_declared": declared,
                    "target_overlap": overlap,
                    "complexity": complexity,
                    "alternate_host": alternate,
                }
                wave = choose_wave(state)
                isolation = choose_mutation_isolation(state)
                verification = choose_verification(
                    {
                        "mutation": mutations > 0,
                        "complexity": complexity,
                        "high_risk": complexity == 5,
                        "alternate_host": alternate,
                    }
                )
                expected_wave = (
                    "mutation_serial" if mutations and (ready <= 1 or rate_limited)
                    else "serial" if ready <= 1 or rate_limited
                    else "readonly_first" if mutations and readonly
                    else "mutation_serial" if mutations
                    else "parallel_two" if ready == 2
                    else "parallel_four"
                )
                expected_isolation = (
                    "readonly_shared" if mutations == 0
                    else "parallel_worktrees"
                    if mutations > 1 and isolated and declared and not overlap
                    else "serial_workspace"
                )
                expected_verification = (
                    "independent_standard" if complexity == 5 and alternate
                    else "focused_standard" if complexity == 5
                    else "independent_economy"
                    if mutations and complexity >= 3 and alternate
                    else "focused_economy"
                )
                actual = (wave, isolation, verification)
                expected = (expected_wave, expected_isolation, expected_verification)
                row = {"state": state, "actual": actual, "expected": expected}
                _record(digest, row)
                cases += 1
                counts.update((f"wave:{wave}", f"isolation:{isolation}", f"verification:{verification}"))
                if actual != expected and len(failures) < 20:
                    failures.append(row)

    for failed, mutation, verification, dependents, output_bytes in itertools.product(
        (False, True), (False, True), (False, True), (False, True),
        (0, 1, 80, 600, 601, 1200, 2400, 100_000),
    ):
        state = {
            "failed": failed,
            "mutation": mutation,
            "verification": verification,
            "has_dependents": dependents,
            "output_bytes": output_bytes,
        }
        actual = choose_handoff(state)
        expected = (
            "expanded" if failed
            else "standard" if mutation or verification
            else "address_only" if not dependents
            else "compact"
        )
        row = {"state": state, "actual": actual, "expected": expected}
        _record(digest, row)
        cases += 1
        counts.update((f"handoff:{actual}",))
        if actual != expected and len(failures) < 20:
            failures.append(row)

    return {
        "schema": "ctx.alphaevolve-orchestration-matrix/v1",
        "cases": cases,
        "failures": len(failures),
        "failure_examples": failures,
        "by_action": dict(sorted(counts.items())),
        "corpus_fingerprint": digest.hexdigest()[:16],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "all_gates_pass": not failures,
    }


def main() -> None:
    print(json.dumps(run_matrix(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
