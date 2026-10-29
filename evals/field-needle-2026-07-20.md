# Seven containment strategies on one task — needle survival

**Date:** 2026-07-20 · **Harness:** [`evals/field_needle.py`](field_needle.py)
· **Record:** [`field-needle-record.json`](field-needle-record.json)
· Model-free, deterministic (`o200k_base` tokenizer, `headroom-ai==0.32.1`).

Every arm is handed the **same bytes** — a 20,001-line integration log
(1,002,594 B · 302,628 tok) hiding one structurally rare "quiet needle" at
L14238 (`fell back to legacy gateway after circuit opened`, no ERROR keyword)
plus two loud ERRORs. No LLM is involved; this exercises the delivery layer
only, so it re-runs cheaply and identically. Headroom and straitjacket execute
their real implementations; the other five arms are inspectable models of
their documented strategies, not executions of those third-party products.
Opaque 12-hex run IDs are canonicalized to the recorded example before token
counting, avoiding workspace-path BPE jitter without changing address shape.

| arm | out tok | ratio | quiet needle | address |
|---|---:|---:|---|---|
| **naive** (raw flood) | 302,628 | 1.0× | SURVIVED | n/a |
| **caveman** (head+tail truncation) | 1,219 | 248× | **DROPPED** | n/a |
| **rtk** (bash-hook filter) | 361 | 838× | **DROPPED** | n/a |
| **ponytail** (advisory ruleset) | 302,698 | 1.0× | SURVIVED | n/a |
| **maki** (sandboxed script) | 58 | 5,218× | **DROPPED** | n/a |
| **headroom-ai 0.32.1** | 357 | 848× | **DROPPED** | no |
| **sj** (`ctx run` logtemplate/v1) | 531 | 570× | **SURVIVED** | **yes** |

## The pattern: the harder the field compresses, the surer it loses the needle

- **naive** — needle present only because *everything* is (302k tok). Unusable.
- **caveman** ("say less") — 248× smaller, middle dropped, quiet needle gone.
- **rtk** (bash-hook flood filter) — keeps the loud ERRORs + head/tail, drops
  the success-path bulk. The quiet needle is an INFO success line, so it is
  filtered out: **lossy on success paths, no address** (exactly its documented
  limit).
- **ponytail** (advisory ladder) — output **unchanged** (+70 tok from the
  ruleset); advisory guidance bounds nothing.
- **maki** (sandboxed script) — compresses the *most* (5,218×): one script
  greps for anomalies and emits only its matches. But the script queried for
  ERROR-shaped lines, so the quiet needle is **never selected**, and the script
  + full log **vanish** — no provenance, no address. Maximum compression,
  maximum loss.
- **headroom** — 848×, but **silently drops** the quiet needle with no address
  (unidiff fallback tier; onnxruntime absent).
- **sj** — 570× *and* the quiet needle survives *and* it carries a
  `ctx get run:… --lines` retrieval address. The only arm that bounds without
  losing.

## The decisive column is "survived AND address emitted", not ratio

Ratio is a trap: the modeled Maki-style arm wins the ratio (5,218×) by
discarding the needle. Every aggressive treatment exercised in this fixture —
three explicit models plus pinned Headroom 0.32.1 — drops it. The two other arms
that keep it (naive and the modeled Ponytail-style treatment) keep the whole
302k-token flood and emit no retrieval address. **`sj` is the only arm
that is simultaneously bounded, quiet-target-preserving, and
address-emitting.** This fixture checks the address's presence and form. It does
not execute `ctx get` or compare a retrieval round trip; retrieval behavior is
covered by the implementation's focused tests, not this receipt.

```
                   bounded?   needle survives?   address emitted?
  naive               no            yes              no
  caveman            yes            NO               —
  rtk                yes            NO               —
  ponytail            no            yes              no
  maki               yes            NO               —
  headroom           yes            NO               no
  sj                 yes            YES              YES   <- only one with all three
```

## Reproduce

```bash
pip install -e '.[dev]' headroom-ai tiktoken
python evals/field_needle.py           # table above
python evals/field_needle.py --json      # machine record
```

Caveats: model-free (delivery layer only — task *success* under a real model is
the separate A/B, `evals/antigravity-gemini-2026-07-19.md`). **headroom**
(0.32.1) and **sj** (`ctx run`) are the real implementations; **caveman**,
**rtk**, **ponytail**, and **maki** are modelled as their documented strategies
(head+tail truncation; loud-line hook filter; advisory-ruleset passthrough;
anomaly-grep script with no retained provenance), not third-party packages.
