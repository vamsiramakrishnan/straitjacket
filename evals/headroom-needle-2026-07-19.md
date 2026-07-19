# Needle-drop rerun: Headroom 0.32.1 vs `ctx run` logtemplate/v1 (2026-07-19)

A model-free rerun of [`headroom-needle-drop-2026-07-17.md`](headroom-needle-drop-2026-07-17.md)
against the **current** Headroom release, with the whole comparison packaged as a
committed, deterministic harness: [`evals/headroom_needle_v2.py`](headroom_needle_v2.py).
No language model is involved — this exercises the compression/digest layer only, so
it re-runs in a review sandbox or CI in seconds.

```bash
pip install -e '.[dev]' headroom-ai tiktoken
python evals/headroom_needle_v2.py          # human table
python evals/headroom_needle_v2.py --json    # machine record
```

## Setup

- **Headroom** `headroom-ai==0.32.1` from PyPI, library API `headroom.compress(messages)`,
  **default config**. The flood is presented the way an agent actually meets it — an
  older `tool_result` (a `bash` call that ran `cat corpus.log`) with five later turns
  after it, so Headroom's `protect_recent` default does not shield it. Caveat, unchanged
  from the 2026-07-17 run: the magika ONNX detector is unavailable in this environment,
  so its unidiff-tier fallback is used (`headroom-ai[proxy]` / `onnxruntime` would enable
  the full detector).
- **`ctx run --shell 'cat corpus.log'`** on the identical bytes, profile `logtemplate/v1`.
- **Corpus:** a deterministic (`seed=1234`) 20,001-line integration log, 1,002,594 B,
  **302,628 tokens** (`o200k_base`). It hides one **quiet needle** at L14238 —
  `INFO … checkout request req-14237 fell back to legacy gateway after circuit opened`,
  carrying **no** ERROR/fail/exception keyword — plus two **loud** ERROR lines.
- **Token accounting:** both arms' *outputs* are counted with the same `o200k_base`
  tokenizer, so the size comparison is tokenizer-independent. Headroom's own
  self-reported counts are recorded alongside for transparency.

## Result

| | Headroom 0.32.1 | `ctx run` logtemplate/v1 |
|---|---|---|
| Output size | **357 tok** (847× smaller) | **~520 tok** (584× smaller) |
| Self-reported | 286,649 → 434 tok | — |
| Loud ERROR needle | ✅ survived (keyword window) | ✅ survived, at `L17650` |
| **Quiet structural needle** | ❌ **silently dropped** | ✅ **verbatim at `L14238`** |
| Omission carries an address | ❌ none | ✅ `ctx get run:<id>#stdout --lines 14238:14241` |
| Transform applied | `router:tool_result:log` | template mining + `exceptional:` section |

Headroom compresses harder (357 vs ~520 tok) and keeps the ERROR line **because it
announces itself** — its output includes a verbatim window around the ERROR. The quiet
needle, structurally identical to an INFO line except for its content, is discarded with
no trace that an anomaly existed. `ctx`'s deterministic template mining keeps the quiet
needle **because rarity is structural, not lexical**: the anomalous line forms a
1-occurrence template and is surfaced verbatim in both the template list and the
`exceptional:` section, with a coordinate the digest itself suggests retrieving.

## Conclusion

The 2026-07-17 finding reproduces on the current release: **on quiet-needle workloads
Headroom's needle-drop rate is 100% here, `ctx`'s is 0%** — the "unknown-unknowns vs
known-unknowns" gap in one number. `ctx` spends ~160 more tokens to buy two things
Headroom's output cannot provide: the evidence that doesn't announce itself, and a
resolvable address for every omitted line.

Caveats: single scenario per run; Headroom on its fallback content-detection tier
(onnxruntime unavailable); library mode, not proxy mode. Output sizes vary a few tokens
run-to-run (per-run artifact id in the `ctx` digest; Headroom's tier heuristics). The
machine record is written to
[`headroom-needle-v2-record.json`](headroom-needle-v2-record.json) by `--json`.
