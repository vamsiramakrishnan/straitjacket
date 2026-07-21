# M-K4 referee: SCIP precision over the textual rung

**Date:** 2026-07-21 · deterministic, on a committed real SCIP index.
Harness: [`evals/scip_precision.py`](scip_precision.py); numbers:
[`scip-precision-2026-07-21.json`](scip-precision-2026-07-21.json).

## The claim under test

docs/SUBSTRATE.md §M-K4: an `index.scip` gives **precise, compiler-backed
references** — the tier jedi (semantic best-effort) and ast (textual)
approximate. The value shows on *ambiguity*: a name that also appears in a
comment, a string, or a shadowing scope, where textual matching produces
false positives that SCIP's symbol resolution rejects.

## The fixture

A two-file project indexed by `scip-python` (the index committed as
`tests/fixtures/scip_sample.scip`). `pkg/core.py` defines `helper`;
`main.py` imports and calls it — and also carries three **decoys** that
all contain the token `helper`:

```python
# helper is the tenant helper described in the docs      ← comment
note = "remember to call helper before commit"           ← string
def local_shadow():
    helper = "shadowed string, not the function"          ← shadowing local
    return helper
```

Ground truth (the real function references): `pkg/core.py:1` (def),
`pkg/core.py:6` (call), `main.py:1` (import), `main.py:12` (call).

## Result

| engine | recall | precision | false positives |
|---|---|---|---|
| **scip (exact)** | 100% | **100%** | **0** |
| ast (textual) | 100% | 50% | **4** (comment, string, shadow ×2) |

Both recover every real reference; only SCIP **rejects the four decoys**.
The textual rung reports the comment, the string, and the shadowing local
as references — the exact false-positive class that sends an agent reading
irrelevant sites. This is why SCIP sits at the top of the `refs` ladder
(**SCIP → jedi → ast**), disclosed per node (`ctx refs` prints `engine
scip (exact)`), and why it multiplies every downstream join's value:
a root-cause or impact query built on precise references inherits the
precision.

## Toolchain note

This receipt exists because the toolchain that was "not available" got
built: `scip-python` (npm) produces the index; the SCIP protobuf schema is
vendored (`src/ctx/_vendor/scip_pb2.py` + `scip.proto`) and read via the
`[scip]` extra (protobuf). The committed index means CI and this eval need
only the protobuf runtime, never the indexer — and absence of either
degrades the ladder to jedi/ast, never errors.

## What is not claimed

One symbol, one fixture — a demonstration of the precision *class*, not a
corpus-scale recall/precision study. The honest generalization: SCIP's
edge is proportional to a codebase's name ambiguity (shared names,
shadowing, string/comment noise); on globally-unique names all three tiers
agree (an earlier unambiguous fixture showed 100% across the board). The
mechanism is now shipped and precise where it matters.
