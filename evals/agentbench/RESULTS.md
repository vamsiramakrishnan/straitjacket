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
