# Agent-harness referee

## adapter: `canary`

- Tasks: **3** · repeats: **1** · max turns: 30 · model: host default (not recorded)
- Arms: plain `claude` vs the full `ctx wrap claude --proxy` intervention; effective prompt/tools may differ
- Provenance: **live agent sessions** (simulated runs are refused)


| Arm | Resolved | Median turns | Median cache hit | Total input tok | Output tok | Cost $ | Median wall s | Timeouts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `naive` | 3/3 | 8 | 96.5% | 912,105 | 3,474 | $0.6676 | 26.9 | 0 |
| `sj` | 3/3 | 8 | 97.5% | 1,376,829 | 3,257 | $0.9206 | 30.8 | 0 |

### Evidence-preservation gate

`solved_arm / solved_naive` must hold at ~1.0. Nothing below is reportable otherwise.

| Arm | Resolved | Ratio vs naive | Gate |
|---|---:|---:|---|
| `naive` | 3/3 | 1.00 | baseline |
| `sj` | 3/3 | 1.00 | PASS |

### Paired outcome (McNemar, exact)

| A | B | A only | B only | both | neither | p |
|---|---|---:|---:|---:|---:|---:|
| `naive` | `sj` | 0 | 0 | 3 | 0 | 1.000 |
## adapter: `deepswe` — wrapper before fix (v0.39.0: proxy disabled tool deferral, receipt-shaped rewrites)

- Tasks: **8** · repeats: **1** · max turns: 60 · model: haiku
- Arms: plain `claude` vs the full `ctx wrap claude --proxy` intervention; effective prompt/tools may differ
- Provenance: **live agent sessions** (simulated runs are refused)

- Model billed (from session usage): `claude-haiku-4-5-20251001`
- Concurrency: 4 sessions at a time (wall-clock is per session, not per sweep)

| Arm | Resolved | Median turns | Median cache hit | Total input tok | Output tok | Cost $ | Median wall s | Timeouts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `naive` | 0/8 | 61.0 | 98.4% | 31,711,195 | 192,301 | $5.1143 | 273.3 | 0 |
| `sj` | 0/8 | 61.0 | 98.6% | 42,964,627 | 203,442 | $6.4825 | 277.8 | 0 |

### Partial credit (whitelisted test ids passed)

`resolved` needs every fail-to-pass id green and no pass-to-pass id red. The fraction below is the grader's own `partial` score, averaged over runs; `f2p` sums fail-to-pass ids passed across runs. Only COMMITTED work is graded (v1.1 collect semantics), so `uncommitted` counts sessions that left edits behind.

| Arm | Mean partial | f2p ids passed | Patches that failed to apply | Uncommitted at exit | Verifier errors |
|---|---:|---:|---:|---:|---:|
| `naive` | 0.777 | 10/435 | 0 | 6 | 1 |
| `sj` | 0.884 | 97/514 | 0 | 7 | 0 |

#### Per task

| Task | Arm | Resolved | f2p | p2p | Partial | Turns | Cost $ | Wall s | Patch bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `adaptix-name-mapping-aliases` | `naive` | False | 0/44 | 2738/2738 | 0.9842 | 61 | $0.64 | 271.6 | 0 |
| `adaptix-name-mapping-aliases` | `sj` | False | 0/44 | 2738/2738 | 0.9842 | 61 | $0.81 | 266.9 | 0 |
| `bandit-incremental-cache-control` | `naive` | False | 0/88 | 275/275 | 0.7576 | 61 | $0.70 | 283.7 | 0 |
| `bandit-incremental-cache-control` | `sj` | False | 0/88 | 275/275 | 0.7576 | 61 | $0.78 | 286.1 | 0 |
| `bandit-interprocedural-taint-checks` | `naive` | False | 0/66 | 293/293 | 0.8162 | 61 | $0.67 | 273.7 | 0 |
| `bandit-interprocedural-taint-checks` | `sj` | False | 0/66 | 293/293 | 0.8162 | 61 | $0.82 | 326.2 | 0 |
| `cattrs-partial-structuring-recovery` | `naive` | False | 0/69 | 7/7 | 0.0921 | 61 | $0.60 | 224.8 | 0 |
| `cattrs-partial-structuring-recovery` | `sj` | False | 57/69 | 7/7 | 0.8421 | 61 | $0.76 | 273.5 | 14085 |
| `ipython-session-bundle-replay` | `naive` | False | 10/17 | 29/29 | 0.8478 | 54 | $0.57 | 272.9 | 21097 |
| `ipython-session-bundle-replay` | `sj` | False | 7/17 | 29/29 | 0.7826 | 60 | $0.78 | 275.8 | 21243 |
| `kombu-single-active-consumer-priority` | `naive` | False | 0/85 | 1421/1421 | 0.9436 | 61 | $0.61 | 371.8 | 0 |
| `kombu-single-active-consumer-priority` | `sj` | False | 0/85 | 1421/1421 | 0.9436 | 61 | $0.85 | 260.3 | 0 |
| `mashumaro-flattened-dataclass-fields` | `naive` | False | 0/66 | 30014/30014 | 0.9978 | 61 | $0.62 | 250.5 | 0 |
| `mashumaro-flattened-dataclass-fields` | `sj` | False | 0/66 | 30014/30014 | 0.9978 | 61 | $0.82 | 279.8 | 0 |
| `mobly-grouped-test-barriers` | `naive` | False | n/a | n/a | None | 57 | $0.72 | 274.3 | 35772 |
| `mobly-grouped-test-barriers` | `sj` | False | 33/79 | 807/808 | 0.947 | 61 | $0.87 | 325.9 | 34354 |

### Evidence-preservation gate

`solved_arm / solved_naive` must hold at ~1.0. Nothing below is reportable otherwise.

| Arm | Resolved | Ratio vs naive | Gate |
|---|---:|---:|---|
| `naive` | 0/8 | nan | baseline |
| `sj` | 0/8 | nan | UNDECIDED (naive solved 0) |

### Paired outcome (McNemar, exact)

| A | B | A only | B only | both | neither | p |
|---|---|---:|---:|---:|---:|---:|
| `naive` | `sj` | 0 | 0 | 0 | 8 | 1.000 |
## adapter: `deepswe` — wrapper after fix: tool deferral kept through proxy, passthrough rewrites

- Tasks: **8** · repeats: **1** · max turns: 60 · model: haiku
- Arms: plain `claude` vs the full `ctx wrap claude --proxy` intervention; effective prompt/tools may differ
- Provenance: **live agent sessions** (simulated runs are refused)

- Model billed (from session usage): `claude-haiku-4-5-20251001`
- Concurrency: 4 sessions at a time (wall-clock is per session, not per sweep)

| Arm | Resolved | Median turns | Median cache hit | Total input tok | Output tok | Cost $ | Median wall s | Timeouts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `naive` | 0/8 | 61.0 | 98.4% | 32,922,769 | 214,977 | $5.4152 | 280.4 | 0 |
| `sj` | 0/8 | 61.0 | 98.2% | 33,997,073 | 239,270 | $5.7391 | 313.8 | 0 |

### Partial credit (whitelisted test ids passed)

`resolved` needs every fail-to-pass id green and no pass-to-pass id red. The fraction below is the grader's own `partial` score, averaged over runs; `f2p` sums fail-to-pass ids passed across runs. Only COMMITTED work is graded (v1.1 collect semantics), so `uncommitted` counts sessions that left edits behind.

| Arm | Mean partial | f2p ids passed | Patches that failed to apply | Uncommitted at exit | Verifier errors |
|---|---:|---:|---:|---:|---:|
| `naive` | 0.787 | 54/514 | 0 | 6 | 0 |
| `sj` | 0.89 | 177/514 | 0 | 7 | 0 |

#### Per task

| Task | Arm | Resolved | f2p | p2p | Partial | Turns | Cost $ | Wall s | Patch bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| `adaptix-name-mapping-aliases` | `naive` | False | 0/44 | 2738/2738 | 0.9842 | 61 | $0.65 | 273.6 | 0 |
| `adaptix-name-mapping-aliases` | `sj` | False | 0/44 | 2738/2738 | 0.9842 | 61 | $0.68 | 290.2 | 0 |
| `bandit-incremental-cache-control` | `naive` | False | 0/88 | 275/275 | 0.7576 | 61 | $0.78 | 288.8 | 0 |
| `bandit-incremental-cache-control` | `sj` | False | 0/88 | 275/275 | 0.7576 | 61 | $0.66 | 272.2 | 0 |
| `bandit-interprocedural-taint-checks` | `naive` | False | 0/66 | 293/293 | 0.8162 | 61 | $0.85 | 409.7 | 0 |
| `bandit-interprocedural-taint-checks` | `sj` | False | 0/66 | 293/293 | 0.8162 | 61 | $0.75 | 332.7 | 0 |
| `cattrs-partial-structuring-recovery` | `naive` | False | 0/69 | 7/7 | 0.0921 | 61 | $0.70 | 280.3 | 0 |
| `cattrs-partial-structuring-recovery` | `sj` | False | 51/69 | 7/7 | 0.7632 | 56 | $0.71 | 314.6 | 20810 |
| `ipython-session-bundle-replay` | `naive` | False | 5/17 | 29/29 | 0.7391 | 59 | $0.61 | 296.4 | 23645 |
| `ipython-session-bundle-replay` | `sj` | False | 10/17 | 29/29 | 0.8478 | 61 | $0.73 | 326.3 | 20992 |
| `kombu-single-active-consumer-priority` | `naive` | False | 0/85 | 1421/1421 | 0.9436 | 61 | $0.62 | 223.6 | 0 |
| `kombu-single-active-consumer-priority` | `sj` | False | 66/85 | 1420/1421 | 0.9867 | 61 | $0.78 | 393.3 | 25657 |
| `mashumaro-flattened-dataclass-fields` | `naive` | False | 0/66 | 30014/30014 | 0.9978 | 61 | $0.53 | 211.6 | 0 |
| `mashumaro-flattened-dataclass-fields` | `sj` | False | 0/66 | 30014/30014 | 0.9978 | 61 | $0.71 | 301.7 | 0 |
| `mobly-grouped-test-barriers` | `naive` | False | 49/79 | 805/808 | 0.9628 | 54 | $0.66 | 280.4 | 22655 |
| `mobly-grouped-test-barriers` | `sj` | False | 50/79 | 808/808 | 0.9673 | 61 | $0.72 | 313.0 | 22017 |

### Evidence-preservation gate

`solved_arm / solved_naive` must hold at ~1.0. Nothing below is reportable otherwise.

| Arm | Resolved | Ratio vs naive | Gate |
|---|---:|---:|---|
| `naive` | 0/8 | nan | baseline |
| `sj` | 0/8 | nan | UNDECIDED (naive solved 0) |

### Paired outcome (McNemar, exact)

| A | B | A only | B only | both | neither | p |
|---|---|---:|---:|---:|---:|---:|
| `naive` | `sj` | 0 | 0 | 0 | 8 | 1.000 |

> Run health: 1 run(s) modified instance tests (SWE-bench-style adapters score these unresolved; the deepswe grader resets them, so there it is a recorded signal only), 0 session(s) produced no result JSON.
## adapter: `dogfood`

- Tasks: **1** · repeats: **1** · max turns: 40 · model: host default (not recorded)
- Arms: plain `claude` vs the full `ctx wrap claude --proxy` intervention; effective prompt/tools may differ
- Provenance: **live agent sessions** (simulated runs are refused)


| Arm | Resolved | Median turns | Median cache hit | Total input tok | Output tok | Cost $ | Median wall s | Timeouts |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `naive` | 1/1 | 41 | 98.1% | 3,406,175 | 22,505 | $10.6755 | 703.9 | 0 |
| `sj` | 1/1 | 41 | 99.0% | 4,003,068 | 17,189 | $13.7022 | 1107.7 | 0 |

### Yield (open-ended mission: count, not pass/fail)

`resolved` only asks whether an arm produced ANY result. For a mission with an open-ended count, that collapses very different outcomes into the same cell.

| Arm | Failing test nodes reproduced | Cost per reproduction |
|---|---:|---:|
| `naive` | 8 | $1.33 |
| `sj` | 5 | $2.74 |

### Evidence-preservation gate

`solved_arm / solved_naive` must hold at ~1.0. Nothing below is reportable otherwise.

| Arm | Resolved | Ratio vs naive | Gate |
|---|---:|---:|---|
| `naive` | 1/1 | 1.00 | baseline |
| `sj` | 1/1 | 1.00 | PASS |

### Paired outcome (McNemar, exact)

| A | B | A only | B only | both | neither | p |
|---|---|---:|---:|---:|---:|---:|
| `naive` | `sj` | 0 | 0 | 1 | 0 | 1.000 |

## Reading this

Resolve rate is a gate, not a headline: the claim this harness can support is *matched-or-better success, then fewer turns, tokens, and seconds*. A wrapper that resolves fewer tasks has not saved anything, however good its token column looks.

---

## Reading the dogfood run

**This is N=1, one mission, one repeat, and both arms hit the 40-turn cap.**
Neither finished. The metric is reproduced failing pytest nodes within the
cap, not independently adjudicated root-cause defects.

Containment was demonstrably active in the `sj` arm — `ctx gain` on its
workspace reports 13.3 MiB raw reduced to 1.0 MiB emitted (12.9x), roughly
3.2M tokens kept out of context across 260 `run` / 210 `get` / 36 `search`
interceptions. This is the human `ctx gain` receipt; the token figure is a
byte-based estimate, not a tokenizer count. The wrapped intervention did not
help here.

### `ctx gain` overstates savings by about 10x in exactly these sessions

The same `ctx gain` output reports **~$9.67 spend avoided**. The session
measured **$3.02 more expensive** than naive. Both cannot be true, and the
arithmetic shows which is wrong:

    3,224,906 estimated tokens (raw-minus-emitted bytes divided by four)
      priced at input rate  $3.00/Mtok  ->  $9.67   what gain reports
      priced at cache read  $0.30/Mtok  ->  $0.97   realistic at 99% cache hit

`pricing.py` already carries the tiers (`cache_read: 0.30`) and its docstring
says cache reads are cheap "exactly as the vendors bill them" — but the gain
calculation prices avoided bytes as though every one would have been paid
fresh. A session running at 98–99% cache hit would have re-read them at a
tenth of that. The overstatement is largest precisely where straitjacket
performs best, which is the worst place for a metric to be optimistic.

### Plausible contributors to the loss

The wrapped workspace recorded 210 `get` events against 260 `run` events, but
events are not turns and this design has no retrieval ablation. Retrieval is a
plausible contributor under a fixed turn cap, not an isolated cause. The full
wrapper also changed guidance and effective tool availability. The avoided
bytes would largely have been cheap cache reads in this run.

### What this does not establish

Not that containment is worthless, nor that containment alone caused the loss.
The full wrapped arm lost one navigation-heavy run with a turn cap and hot
cache. A useful rerun needs a pinned model, a containment-only arm
(`collapse = false`, `CTX_WRAP_NO_DISCIPLINE=1`), and a cap above the point where
each arm stops naturally.

---

## Reading the DeepSWE runs

**Eight Python tasks, haiku, one repeat, 60-turn cap, two sweeps of the same
tasks.** Nothing resolved in either arm or either sweep: 28 of 32 sessions used
every turn, most before `git commit`, and v1.1 grades committed work only. The
partial-credit tables are the comparison.

The first sweep (`wrapper before fix`) is the counterexample this harness exists
to catch: the wrapped arm cost 27% more and read 36% more input than plain Claude,
with no outcome to show for it. The transcripts put both numbers on the wrapper.
The observer proxy's non-Anthropic base URL switched Claude Code's deferred tool
loading off (41 inline tool schemas instead of 16, ~15k cached tokens per call),
and hook rewrites returned receipt-shaped results with ctx's exit `3` for commands
whose whole output was a few hundred bytes. No sj session ever issued a `ctx get`,
`ctx search` or `ctx map`, so there was nothing to offset the tax.

The second sweep (`wrapper after fix`) reruns the same tasks with both defects
fixed. The wrapped arm's cached read is within 3% of plain Claude, cost within 6%,
and it passed more held-out tests in aggregate (177 vs 54 committed fail-to-pass
ids; 326 vs 180 counting uncommitted trees). Same-model variance is visible in the
naive column between sweeps, so treat the after-fix gap as a direction. Full
mechanism analysis and per-task tables: [`deepswe-2026-09-13.md`](deepswe-2026-09-13.md).
