# Five arms, one task — needle survival head-to-head

**Date:** 2026-07-20 · **Harness:** [`evals/five_arms_needle.py`](five_arms_needle.py)
· **Record:** [`five-arms-needle-record.json`](five-arms-needle-record.json)
· Model-free, deterministic (`o200k_base` tokenizer, `headroom-ai==0.32.1`).

Every arm is handed the **same bytes** — a 20,001-line integration log
(1,002,594 B · 302,628 tok) hiding one structurally rare "quiet needle" at
L14238 (`fell back to legacy gateway after circuit opened`, no ERROR keyword)
plus two loud ERRORs. No LLM is involved; this exercises the delivery layer
only, so it re-runs cheaply and identically.

| arm | out tok | ratio | quiet needle | address |
|---|---:|---:|---|---|
| **naive** (raw flood) | 302,628 | 1.0× | SURVIVED | n/a |
| **caveman** (head+tail truncation) | 1,219 | 248× | **DROPPED** | n/a |
| **ponytail** (advisory ruleset) | 302,698 | 1.0× | SURVIVED | n/a |
| **headroom-ai 0.32.1** | 357 | 848× | **DROPPED** | no |
| **sj** (`ctx run` logtemplate/v1) | 524 | 578× | **SURVIVED** | **yes** |

## What each arm shows (faithful to the field taxonomy)

- **naive** — the needle is present because *everything* is present, but at
  302k tokens it is unusable: you pay the whole flood every turn and still have
  to find the line. Baseline, not a solution.
- **caveman** ("say less" / terse truncation) — 248× smaller, but the middle is
  gone, so the quiet needle at L14238 is **dropped with no trace**. The
  documented anti-pattern: saving tokens by destroying evidence.
- **ponytail** (advisory discipline ladder) — the ruleset is injected but the
  bytes still flow, so output is **unchanged (slightly larger, 302,698 tok)**.
  This is the point of the critique: advisory-only guidance never actually
  bounds anything; nothing measures whether the ladder held.
- **headroom** — compresses hardest (848×) but **silently drops the quiet
  needle** and leaves no address to recover it. (Ran in its unidiff fallback
  tier here — onnxruntime/magika absent — consistent with the published
  needle-drop result.)
- **sj** — the only arm that is **both** bounded (578×, 524 tok) **and**
  lossless: the quiet needle survives in the digest **and** carries a
  `ctx get run:… --lines` retrieval address, so any omitted line is
  recoverable on demand.

## The decisive column is "survived AND addressable"

Compression ratio alone is a trap — headroom "wins" on ratio precisely by
throwing the needle away. The metric that matters is whether the model can
still *recover* the rare evidence:

- **DROPPED** (caveman, headroom): the needle is gone; no ratio redeems that.
- **SURVIVED, no address** (naive, ponytail): present but only by keeping the
  whole flood — no bounding, or unciteable.
- **SURVIVED + addressable** (sj): bounded to 524 tok *and* every omitted line
  keeps an exact address. Only straitjacket lands both.

## Reproduce

```bash
pip install -e '.[dev]' headroom-ai tiktoken
python evals/five_arms_needle.py           # table above
python evals/five_arms_needle.py --json      # machine record
```

Caveats: model-free (delivery layer only — task *success* under a real model is
a separate A/B, see `evals/antigravity-gemini-2026-07-19.md`). Caveman and
Ponytail are modelled as their documented strategies (head+tail truncation;
advisory ruleset with raw passthrough), not third-party packages; Headroom and
sj are the real implementations.
