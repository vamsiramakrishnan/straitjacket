# Contributing to straitjacket / ctx-harness

## Dev setup

```bash
pip install -e '.[dev]'
```

Confirm that you are exercising this checkout, not another `ctx` on `PATH`:

```bash
ctx --version
python -c 'import ctx; print(ctx.__file__)'
```

That is the whole baseline: Python ≥ 3.11, `pathspec` (the one runtime
dependency), plus `pytest` and `jsonschema` from the dev extra. Optional
extras unlock faster or richer engines but are never required:

```bash
pip install -e '.[dev,map,fast,code]'   # grimp+networkx map engine, orjson, jedi
# and, if you want the binary-accelerated paths:
apt-get install ripgrep universal-ctags
```

## Where the code lives

`src/ctx/` is a flat package of ~60 modules organized into six planes
(Safety, Execution, Derivation, Evidence, Delivery, Behaviour).
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the map — start there with its
"which file do I touch for X" table, then read the plane your change lands in.
Common on-ramps:

- **A digest profile for a new tool** → [`docs/WRITING-A-PROFILE.md`](docs/WRITING-A-PROFILE.md)
  (names the modules, the registry, the contract format, and the tests).
- **Command steering** → `src/ctx/hook.py` (the stdlib-only hot path).
- **A `ctx q` stage** → `src/ctx/query.py`. **A plan operator** → `src/ctx/plan_ops.py`.
- **A CLI verb** → three edits: its parser block and one `_COMMANDS` row in
  `src/ctx/cli.py`, a `cmd_<verb>(ws, ns)` handler in
  `src/ctx/commands/<family>.py`, and its plain-English line in
  `src/ctx/cliux.py`. Tests fail if you skip one. Keep the handler's imports
  inside the function: the table is lazy on purpose.

## Running and writing tests

```bash
python -m pytest tests/ -q
```

The suite is acceptance-oriented (determinism, budgets, hook contract,
path/symlink escapes, redaction) and must pass on a bare `[dev]` install with
no binaries present. `tests/conftest.py` isolates each test's artifact store, so
tests never touch your real state.

**CI runs two configurations** (`.github/workflows/ci.yml`):

- **full** — the py3.11/3.12/3.13 matrix with every engine installed
  (ripgrep, universal-ctags, and the `map,fast,code,scip` extras);
- **minimal** — py3.11, `[dev]` only, no binaries, with the builtin engines
  pinned (`CTX_SEARCH_ENGINE=python CTX_MAP_ENGINE=builtin CTX_CODE_ENGINE=ast`)
  to prove the deterministic-fallback story.

If your change only works when ripgrep/ctags/grimp/jedi are installed, it is not
done. Run the minimal config locally before pushing:

```bash
CTX_SEARCH_ENGINE=python CTX_MAP_ENGINE=builtin CTX_CODE_ENGINE=ast \
  python -m pytest tests/ -q
```

**Which test file to extend:**

| Your change | Add tests to |
|---|---|
| a new digest profile | `tests/test_coverage_profiles.py` + `tests/test_contract_conformance.py` + a census test like `tests/test_pytest_census.py` |
| a `ctx q` stage | `tests/test_query.py` (the composition-algebra referee) |
| a CLI verb | the verb's own file (e.g. `tests/test_ask.py`, `tests/test_debt_and_deliverable.py`) |
| hook / steering behaviour | `tests/test_hook.py`, `tests/test_steering.py`, `tests/test_emission_gate.py` |
| determinism / safety invariants | `tests/test_capture_and_determinism.py`, `tests/test_safety_invariant.py` |

The one custom marker is `sj_canary` — the Tier-0 evidence-channel conformance
tests. Run them as a fast PR gate with `python -m pytest -m sj_canary`.

## Running the evals

Performance and quality claims are backed by measured receipts, not adjectives.
[`evals/README.md`](evals/README.md) explains how to run them — the model-free
ones (no API key, reproducible) and the live ones (real agent + API key) — and
which instrument answers which question. A new claim ships with its runner and a
dated receipt in the same change.

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

## Building the release artifact

Editable installs can accidentally borrow files from the checkout. Before a
release, build the artifacts and run the wheel in an empty directory:

```bash
python -m pip install build
python -m build
python scripts/check_distribution.py dist/*.whl
```

For a public release, follow [`docs/RELEASING.md`](docs/RELEASING.md). PyPI
publishing is performed from a tagged GitHub release through Trusted Publishing,
not from a developer machine with a long-lived token.

The distribution check verifies the runtime version, the `ctx` entrypoint,
all Antigravity/Claude/Codex configuration renderers, and the managed
Antigravity SDK shim. CI runs the same check in the `built wheel` job.

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

## Making a change

1. Branch from `main`.
2. Ship the mechanism, its acceptance tests, and — for any performance or
   quality claim — its eval receipt, in the same change. A green suite
   (including the minimal config) is the merge gate.
3. Keep model-visible output deterministic and every omission addressed; run
   `python scripts/check_docs_links.py` and `python scripts/check_docs_facts.py`
   if you touched docs.
4. Open a PR that says what changed and points at the receipt for any number you
   cite.

## Where things are decided

- `spec/` — the normative SPEC, acceptance suite, ADRs, and wire schemas.
- `ROADMAP.md` — planned mechanisms (M-A…M-E) with contracts and acceptance
  gates; check it before proposing a new verb.
- `evals/` — measured results behind every performance or quality claim; new
  claims need an eval doc, not adjectives.
