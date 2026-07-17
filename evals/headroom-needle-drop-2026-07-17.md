# Needle-drop: Headroom 0.32.0 vs ctx logtemplate/v1

**Date:** 2026-07-17 · Headroom installed from PyPI (`headroom-ai==0.32.0`),
library API `headroom.compress(messages)`, default config (magika ONNX
detector unavailable in this environment; its unidiff-tier fallback was used —
noted for fairness). ctx digest produced by `ctx run` on the identical bytes.

Corpus: 20,001-line integration log (~1.2 MiB, ~304k est tokens).

## Test 1 — loud needle (2 ERROR lines among 20k INFO)

| | Headroom | ctx logtemplate/v1 |
|---|---|---|
| Output size | 272 tok | ~210 tok |
| Needle survived | ✅ (ERROR-keyword prioritization) | ✅ with exact `L14238` coordinate |
| Omission declared | Partially — "[19993 lines omitted: 2 ERROR, 19998 INFO]", counts inconsistent (shown ERRORs also counted as omitted; totals ≠ omitted) | Exact: "parsed 20,001/20,001 · shown 5 spans · omitted 19,996 lines" |
| Retrieval | opaque hash reference | executable `ctx get run:<id>#stdout --lines …` |

Both preserved the evidence. Headroom's coverage arithmetic is best-effort;
ours is exact — matters for audit-grade use.

## Test 2 — quiet needle (structurally rare line, NO error keywords)

One line in 20,001: "INFO worker-13 checkout request req-14237 fell back to
legacy gateway after circuit opened". No ERROR/fail/exception token.

| | Headroom | ctx logtemplate/v1 |
|---|---|---|
| Compression | 347,595 → **68 tok** | 304k est → ~180 tok |
| Needle survived | ❌ **silently dropped** | ✅ verbatim, at `L14238`, in both the template list (1× template) and the `exceptional:` section |
| Model can recover | No — nothing signals an anomaly existed | Yes — the digest itself suggests `ctx get run:…#stdout --lines 14238:14241` |
| Latency | ~1,000 ms **per request, recurring** | 360 ms **once at capture** |

## Conclusion

Keyword-prioritizing compression keeps evidence that announces itself and
silently destroys evidence that doesn't. Deterministic template mining keeps
the quiet needle *because rarity is structural, not lexical* — the anomalous
line forms a 1-occurrence template and is surfaced verbatim with coordinates.
This is the "unknown-unknowns vs known-unknowns" difference in one number:
on quiet-needle workloads Headroom's needle-drop rate here is 100%, ours is 0%.

Caveats: single scenario per test; Headroom ran its fallback content-detection
tier (onnxruntime unavailable); results are from library mode, not proxy mode.
A full four-arm eval (spec ACCEPTANCE) should randomize needle styles.
