# Triage-channel referee — BigCodeBench-Hard

- Tasks: **30**, identical across arms: **True**
- Provenance: **live API calls only** (simulated arms are refused by this generator)
- Sandbox: `/tmp/claude-0/-home-user-straitjacket/e913754c-4b2d-5b7f-98b1-b17d2ff117a4/scratchpad/bench-env/bin/python`
- Price table: Unverified: carried over from tokenomics-benchmark-multi-llms/src/config.py. Re-price with report.py --prices before quoting USD.

## Per-arm results

| Arm | Ladder | Triage | Pass | Rate (95% CI) | Solver tok (in/out) | Triage tok | Triage $ | Total $ | $/solved | Repair-channel chars | ctx get hops |
|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| `cascade_llm` | 3.5-flash-lite -> 3.6-flash(low) | llm | 21/30 | 70.0% (52%–83%) | 37,843/65,974 | 23,963 | $0.0172 | $0.4611 | $0.0220 | 20,237 | 0 |
| `cascade_raw` | 3.5-flash-lite -> 3.6-flash(low) | raw | 20/30 | 66.7% (49%–81%) | 49,985/57,253 | 0 | $0.0000 | $0.3953 | $0.0198 | 18,203 | 0 |
| `cascade_sj` | 3.5-flash-lite -> 3.6-flash(low) | sj | 19/30 | 63.3% (46%–78%) | 39,554/65,331 | 0 | $0.0000 | $0.4376 | $0.0230 | 8,822 | 0 |
| `cascade_sjhop` | 3.5-flash-lite -> 3.6-flash(low) | sj_hop | 21/30 | 70.0% (52%–83%) | 46,523/66,575 | 0 | $0.0000 | $0.4612 | $0.0220 | 6,725 | 19 |
| `smart_llm` | 3.6-flash(low) -> 3.5-flash-lite -> 3.6-flash(medium) | llm | 24/30 | 80.0% (63%–90%) | 49,083/109,221 | 37,928 | $0.0256 | $0.8342 | $0.0348 | 17,808 | 0 |
| `smart_raw` | 3.6-flash(low) -> 3.5-flash-lite -> 3.6-flash(medium) | raw | 22/30 | 73.3% (56%–86%) | 68,355/133,697 | 0 | $0.0000 | $1.0123 | $0.0460 | 10,993 | 0 |
| `smart_sj` | 3.6-flash(low) -> 3.5-flash-lite -> 3.6-flash(medium) | sj | 21/30 | 70.0% (52%–83%) | 56,434/135,824 | 0 | $0.0000 | $1.0166 | $0.0484 | 6,160 | 0 |
| `smart_sjhop` | 3.6-flash(low) -> 3.5-flash-lite -> 3.6-flash(medium) | sj_hop | 23/30 | 76.7% (59%–88%) | 67,818/134,655 | 0 | $0.0000 | $1.0159 | $0.0442 | 4,815 | 31 |

## Run health (excluded and degraded tasks are counted, never dropped silently)

| Arm | passed | failed | infra_error | errored (API) | scored | truncated calls | wall s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `cascade_llm` | 21 | 7 | 2 | 0 | 28 | 0 | 521 |
| `cascade_raw` | 20 | 8 | 2 | 0 | 28 | 0 | 453 |
| `cascade_sj` | 19 | 10 | 1 | 0 | 29 | 1 | 635 |
| `cascade_sjhop` | 21 | 8 | 1 | 0 | 29 | 0 | 667 |
| `smart_llm` | 24 | 5 | 1 | 0 | 29 | 1 | 744 |
| `smart_raw` | 22 | 7 | 1 | 0 | 29 | 3 | 852 |
| `smart_sj` | 21 | 9 | 0 | 0 | 30 | 3 | 876 |
| `smart_sjhop` | 23 | 6 | 1 | 0 | 29 | 3 | 1007 |

## Paired comparison within each family (McNemar, exact)

Same tasks, same ladder, same prompts — only the triage channel differs.

| Family | A | B | A only | B only | both | neither | p |
|---|---|---|---:|---:|---:|---:|---:|
| cascade | `llm` | `raw` | 2 | 1 | 19 | 8 | 1.000 |
| cascade | `llm` | `sj` | 4 | 2 | 17 | 7 | 0.688 |
| cascade | `llm` | `sj_hop` | 3 | 3 | 18 | 6 | 1.000 |
| cascade | `raw` | `sj` | 3 | 2 | 17 | 8 | 1.000 |
| cascade | `raw` | `sj_hop` | 1 | 2 | 19 | 8 | 1.000 |
| cascade | `sj` | `sj_hop` | 1 | 3 | 18 | 8 | 0.625 |
| smart_repair | `llm` | `raw` | 2 | 0 | 22 | 6 | 0.500 |
| smart_repair | `llm` | `sj` | 3 | 0 | 21 | 6 | 0.250 |
| smart_repair | `llm` | `sj_hop` | 1 | 0 | 23 | 6 | 1.000 |
| smart_repair | `raw` | `sj` | 2 | 1 | 20 | 7 | 1.000 |
| smart_repair | `raw` | `sj_hop` | 0 | 1 | 22 | 7 | 1.000 |
| smart_repair | `sj` | `sj_hop` | 0 | 2 | 21 | 7 | 0.500 |

## What this measures

The triage channel is the only manipulated variable inside a family. `raw` forwards the unittest stderr verbatim, `llm` pays a model to compress it, `sj` forwards the digest emitted by the real `ctx run` CLI, and `sj_hop` forwards that digest plus the spans it cites, resolved locally with `ctx get`. Pass-rate differences inside a family are attributable to that channel; differences across families are not (the ladder changes too).

The triage-cost column is the mechanical claim and it is exact: `raw`, `sj` and `sj_hop` make no triage API call, so their triage cost is $0.0000 by construction — `ctx get` reads the local store. `llm` pays per repair loop. Pass rate is the gate — a cheaper channel is only interesting if accuracy holds, and at this N the confidence intervals are wide enough that small differences are not resolved.

**Caveat on `Repair-channel chars`:** it sums the final channel of FAILED tasks only, so an arm that passes more tasks scores lower for free. It mixes channel size with failure count and is NOT a clean bytes-per-repair comparison across arms; measuring that properly needs a per-repair-loop metric the runner does not yet record.
