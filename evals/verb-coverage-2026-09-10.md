# Does the map advertise addresses the verbs can resolve?

**Date** 2026-09-10 · **ctx** v0.38.0 · **eval** [`verb_coverage.py`](verb_coverage.py)

**Environment** (it decides the answer, so it is part of the result)
`universal-ctags` present · `jedi` · `tree_sitter` + `tree_sitter_go` +
`tree_sitter_typescript` · `ast_grep_py` — i.e. the full `.[dev,map,fast,code]`
set CI installs.

**Corpus** six checkouts, one per language, from the ContextBench repo set:
`ansible/ansible`, `cli/cli`, `facebook/zstd`, `fasterxml/jackson`,
`mui/material-ui`, `tokio-rs/bytes`. Up to 12 advertised symbols per language.

## The question

`ctx map` is the discovery surface. For every symbol it finds, it prints the
exact address to use next:

    repo:api/client.go --symbol NewClientFromHTTP

That line is an affordance. This eval asks the only question that matters about
an affordance — **does it work?** — by taking the symbols the map advertises and
asking `ctx def` to resolve each one.

There is no corpus label, no model, and no ranking in the loop. The map produced
both the symbol and the address; if `ctx def` refuses it, the two halves of ctx
disagree with each other and the defect is ctx's *by construction*. That is the
property [`contextbench.py`](contextbench.py) lacks, and why a low score there
could not be attributed to anything.

## Result

| language | before | after |
|---|---:|---:|
| c | 0/12 | 12/12 |
| go | 0/8 | 8/8 |
| java | 0/9 | 9/9 |
| javascript | 0/12 | 12/12 |
| python | 18/18 | 18/18 |
| rust | 0/12 | 12/12 |
| typescript | 0/9 | 9/9 |
| **total** | **18/80 — 22%** | **80/80 — 100%** |

Python was already perfect. Every other language was zero. The map advertised
530 non-Python symbols across these repositories and `ctx def` would resolve
none of them.

## Three defects, found in that order

**1 · Both definition engines were Python-only, and nothing said so.**
`_select_engine()` in `codeverbs.py` chose between jedi and stdlib `ast`
without ever looking at the file's language. `skeleton.py` already extracted
symbols for 16 languages — `ctx map` was using it — so the fix was a third
engine that resolves through the same skeleton the map consults. Resolution now
agrees with discovery by construction.

**2 · A degraded parse was cached under a key that did not record the
degradation.** With the first fix in, C still answered *"0 symbols known for
this file"* for symbols ctags could see. `_extract` returned `ctags/118`
called directly, while `skeleton_for` returned `none/0`: the skeleton had been
computed earlier in an environment without ctags and cached under a key made of
the blob hash alone. Installing the dependency changed nothing, and the stale
hit was indistinguishable from a correct answer.

A parse is a function of the source bytes **and** of the backends able to run,
so the cache key now includes a per-language backend fingerprint.

**3 · The ladder's floor was its most language-specific rung.** `ctx refs`
fell back to a word-boundary regex over `**/*.py`. On a Go repository it
returned `sites: 0` — not a refusal but a *wrong answer*, indistinguishable from
a symbol with no references. It now scans every language the skeleton knows.

A fourth, smaller gap closed on the way: `ctx map` advertises from ctags, while
the skeleton answers with whichever backend ran first — tree-sitter, for Go,
with a narrower idea of what counts. Package constants fell in that gap. `ctx
def` now tries ctags as a final rung, so anything the map advertises resolves.

## The one that was not a ctx defect

Before any of the above, `ctx map` reported **0 files** for Go, Java,
TypeScript and Rust. That reads exactly like a ctx defect. It was a missing
`universal-ctags` binary; installing it took Go 0→457 files, Java 0→445,
TypeScript 0→255, C 7→348.

This is the second time in this workstream that a thin environment nearly got
written up as a finding — the first cost a published claim and a retraction (see
[`contextbench-2026-09-10.md`](contextbench-2026-09-10.md)). So the degradation
is no longer silent: `ctx doctor` now carries a **code intelligence** row.

```
✓ code intelligence — 16/16 languages parseable
✓ code intelligence — 5/16 languages parseable; no universal-ctags
    (non-Python symbols unavailable); no backend for c, c#, c++, java, …
```

It is not marked as a failure, because the extras are genuinely optional. It is
marked as *visible*, because measuring ctx against a stripped install and
reporting the result is the expensive mistake, not running one.

## Standing caveat on the ContextBench receipt

The 40-instance run in [`contextbench-2026-09-10.md`](contextbench-2026-09-10.md)
was executed **without ctags and without the `code` extra**. Every non-Python
number in it was measured against a ctx missing three of four backend rungs, on
top of the policy-attribution error already retracted there. Those figures are
not a measurement of ctx and should not be quoted; this eval, not that one, is
the current statement about non-Python coverage.

## Why this shape of eval earns its place

It is cheap enough to be a gate rather than a study — one `ctx map` and N
`ctx def` calls per repository, no network beyond the checkout. Per
[`BENCHMARK.md`](BENCHMARK.md)'s rule that external corpora are teachers and
never referees, ContextBench pointed at the area; the referee here is ctx's own
internal agreement.
