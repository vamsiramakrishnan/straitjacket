# Agent-harness referee

## adapter: `canary`

- Tasks: **3** · repeats: **1** · max turns: 30 · model: host default
- Arms differ only in the wrapper: `claude` vs `ctx wrap claude --proxy`
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

## Reading this

Resolve rate is a gate, not a headline: the claim this harness can support is *matched-or-better success, then fewer turns, tokens, and seconds*. A wrapper that resolves fewer tasks has not saved anything, however good its token column looks.

---

## Validity of this run: the flood fixture does not test containment

Do **not** read the token or cost columns above as a containment result. The
`flood` fixture is defeated by a one-line shell idiom, and the agent used it.

An instrumented session (`--output-format stream-json`) on the same fixture
shows the agent's own defence:

```
python3 -m pytest tests/ -v 2>&1 | tail -30
```

The suite emits 4,016 lines / 177 KB in the failing state, but that command
returned a **1,442-char** tool result. The flood never entered the transcript.
Corroborating evidence in the table's own data: `naive/flood` consumed 316,245
input tokens against `naive/quiet`'s 317,366 — indistinguishable, despite 177 KB
more output being available to consume.

So on all three fixtures there was nothing to contain, and the `sj` arm paid
wrapper overhead against a baseline that was never harmed. Its 1.45–1.65x input
and ~38% higher cost measure that overhead, not a containment failure.

This is the regime `evals/BENCHMARK.md` calls the low-output control, and the
regression it predicts is the one that produced graduated engagement. The
result is consistent with doctrine; it just is not evidence about floods.

### What a fixture must do to test containment

Piping to `tail`/`head`/`grep` must not be able to win. That means the evidence
the agent needs has to be **dispersed through** the noise rather than sitting at
one end of it:

- failures interleaved across the output, not clustered at the tail
- the decisive assertion in the middle (`headroom_needle_v2.py` and
  `field_needle.py` already exercise this shape model-free)
- several failing tests whose messages differ, so a fixed window loses some
- output that is not line-greppable for a single obvious token

Until the fixture has that property, this adapter validates the harness
end-to-end — arms, grading, isolation, metrics — and nothing more. That is what
it was built for; the containment question needs SWE-bench Verified or
Terminal-Bench, where the floods are real and not of our own construction.
