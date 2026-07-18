# Contributing to straitjacket / ctx-harness

## Dev setup

```bash
pip install -e '.[dev]'
```

That is the whole baseline: Python ≥ 3.11, `pathspec` (the one runtime
dependency), plus `pytest` and `jsonschema` from the dev extra. Optional
extras unlock faster or richer engines but are never required:

```bash
pip install -e '.[dev,map,fast,code]'   # grimp+networkx map engine, orjson, jedi
# and, if you want the binary-accelerated paths:
apt-get install ripgrep universal-ctags
```

## Running tests

```bash
python -m pytest tests/ -q
```

The suite is acceptance-oriented (determinism, budgets, hook contract,
path/symlink escapes, redaction) and must pass on a bare `[dev]` install with
no binaries present. CI runs both configurations: the full matrix with every
engine installed, and a "minimal" job with `CTX_SEARCH_ENGINE=python
CTX_MAP_ENGINE=builtin CTX_CODE_ENGINE=ast` and no optional deps. If your
change only works when ripgrep/ctags/grimp/jedi are installed, it is not done.

## The four load-bearing conventions

These are the invariants every change is reviewed against:

1. **Stdlib-first, with opportunistic binaries and optional pure-Python
   extras, always backed by a deterministic fallback.** A dependency that
   cannot install everywhere must never be the only path — ripgrep, ctags,
   grimp/networkx, orjson, and jedi accelerate or enrich, but the builtin
   engine ships the same output contract and the same coordinates.
2. **Determinism rules for digests: no timestamps, no absolute host paths,
   no locale-dependent text, no ANSI, in any model-visible byte.** Identical
   input bytes must yield byte-identical digests so prompt-cache prefixes
   stay stable across sessions, machines, and replays.
3. **Hook hot-path contract: stdlib-only imports, fail-open on internal
   error, exactly one JSON decision object on every code path.** The
   PreToolUse guard runs on every tool call — a slow, crashy, or chatty hook
   bricks the host session, so `hook.py` may not import third-party code.
4. **Budgets and declared omission on every model-visible surface.** Every
   verb output fits a token budget, and anything omitted is declared with a
   count plus a resolvable continuation coordinate — silent truncation is
   the failure mode this project exists to prevent.

## Acceptance suite as merge gate

`spec/ACCEPTANCE.md` is normative and `tests/` is its executable form: a
green suite is the merge gate. New mechanisms inherit the invariants
(determinism, budgets, declared omission, telemetry) or they don't merge —
ship the acceptance tests in the same change as the mechanism.

## Engine disclosure convention

When an output can be produced by more than one engine, the active engine is
disclosed in the output header and participates in any cache key — e.g.
`ctx map` prints `engine grimp+networkx` or `engine builtin`, `ctx doctor`
reports the active search engine and ignore matcher. Fallbacks are
transparent in behavior but never anonymous: a labeled note, never a silent
swap and never an error.

## Versioning

0.x throughout: expect breaking changes. The minor version bumps once per
mechanism wave (v0.4 steering + wrap, v0.5 zoom spans, v0.6 map/diff/
explorer/governor), recorded in `CHANGELOG.md`; patch-level churn within a
wave does not get its own release.

## Where things are decided

- `spec/` — the normative SPEC, acceptance suite, ADRs, and wire schemas.
- `ROADMAP.md` — planned mechanisms (M-A…M-E) with contracts and acceptance
  gates; check it before proposing a new verb.
- `evals/` — measured results behind every performance or quality claim; new
  claims need an eval doc, not adjectives.
