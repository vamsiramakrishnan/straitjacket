# Coverage-corpus wave: rtk's breadth, measured before built (2026-07-19)

Method: the rtk-corpus method (evals/rtk-corpus-2026-07-18.md) made
re-runnable — `evals/coverage_corpus.py`. Every corpus is (argv, captured
bytes, exit code) replayed through a stub binary carrying the real tool's
name, so `ctx run` exercises the true capture path: argv-anchored
detection, shape dispatch, slim inline, budgets, telemetry. Live corpora
come from real toolchains on the machine (cargo, pip, ps, find); tools
without a runnable daemon/CLI here (docker, kubectl, gh, mvn, rspec)
replay faithfully-shaped fixtures and are labeled `replay` — provenance is
a report column, never a footnote.

The target list is rtk's published parser coverage (file ops, git, gh,
test runners, build/lint, package management, AWS, containers, IaC) crossed
with SPEC §9's aspirational rows (tabular output, Cargo, Maven/Gradle,
directory trees). rtk's own benchmark frames the question per command; ours
adds the axis rtk doesn't measure — what the digest *says*, not just what
it weighs.

## Baseline (before)

| corpus | source | raw tok | digest tok | ratio | profile | utility |
|---|---|---|---|---|---|---|
| cargo test (6/150 failing) | live | 3,091 | 117 | 26.4× | text/v1 | names ONE failure of 6; no census, no `test result:` line |
| cargo test (150 passing) | live | 1,185 | 146 | 8.1× | text/v1 | summary reaches digest via tail window — adequate by luck |
| pip list | live | 283 | 305 | 0.9× | text/v1 | complete inline — correct |
| ps aux | live | 5,605 | 129 | 43.4× | text/v1 | arbitrary "first signal" row |
| find src tests -type f | live | 1,116 | 201 | 5.6× | text/v1 | head/tail of listing; count exact in header |
| docker ps -a (40) | replay | 1,522 | 450 | 3.4× | text/v1 | head/tail rows; no state counts |
| kubectl get pods (180) | replay | 3,200 | 304 | 10.5× | text/v1 | **7 of 8 CrashLoopBackOff + all ImagePullBackOff pods in the omitted middle** |
| gh pr list (30) | replay | 574 | 599 | 1.0× | text/v1 | complete inline — correct |
| mvn test (3,360 tests, 4 failing) | replay | 4,781 | 584 | 8.2× | logtemplate/v1 | every failure in `exceptional:` with coordinates + final census line |
| rspec (132 ex, 2 failing) | replay | 282 | 302 | 0.9× | text/v1 | complete inline — correct |
| aws ec2 describe-instances (120) | replay | 16,748 | 111 | 150.9× | json/v1 | exact shape census |

## What the measurements said

1. **The AWS/cloud family is already covered** — by shape, not by parser.
   json/v1 claimed the 16.7k-token describe-instances at 150.9× with an
   exact shape census. rtk ships eight AWS parsers; shape dispatch makes
   them unnecessary here. Hypothesis killed.
2. **Maven was already covered by logtemplate/v1** — template mining put
   every `[ERROR]` failure line in `exceptional:` with coordinates and kept
   the `Tests run: 432, Failures: 4` census line. A dedicated surefire
   profile would re-derive what rarity already surfaces. Killed.
3. **Small listings stay killed** (pip list, gh pr list, small rspec):
   complete-inline at ~1.0× is the correct behavior; the v0.11 scaffold-slim
   lesson holds. rtk compresses these; we don't need to.
4. **ps aux measured-and-declined**: single-space/right-aligned columns
   defeat offset slicing, and no census beats its 43× ratio meaningfully.
5. **Two real gaps**: cargo test (26× compression, near-zero utility — a
   test digest without a failing census starves the fix loop; the spec3
   lesson on a second runner) and caps-header tables (the kubectl row is
   the tabular quiet needle: minority states are structurally rare *values*,
   not keyword-bearing lines, so text/v1's head/tail and error-regex both
   miss them and Headroom-class keyword compression would too).

## What shipped and the after-measurements

- **cargotest/v1** — exact suite-aggregated census (passed/failed/ignored
  across binaries + doctests), one line per failing test with stream:line
  coordinates (the census is the work queue), first panic location+message
  inlined. Detection anchored on the libtest `test result:` shape, never
  argv alone — a `cargo test` that dies in the compiler falls through to
  lint/build.
- **table/v1** — shape-detected aligned caps-header tables (docker/podman
  ps/images, kubectl/oc get, compose ps, and MCP-delivered tables with
  synthesized argv): exact row×column count, per-column value censuses for
  low-cardinality columns only, minority rows surfaced verbatim with
  coordinates (rarest first, deduped by line), whole-body span + search
  continuation.

| corpus | before | after | profile | utility change |
|---|---|---|---|---|
| cargo test failing | 117 tok, names 1 of 6 | 203 tok | cargotest/v1 | exact: `passed 144 · failed 6` + all 6 names with coordinates + first panic `src/lib.rs:6:29` |
| cargo test passing | 146 tok | 105 tok | cargotest/v1 | exact one-line census, cheaper than head/tail |
| docker ps -a | 450 tok, no counts | 204 tok (7.5×) | table/v1 | exact: `Up 3 hours 34 · Exited (137) 6` + first Exited row cited |
| kubectl get pods | 304 tok, needles omitted | 295 tok (10.8×) | table/v1 | exact: `Running 166 · CrashLoopBackOff 8 · ImagePullBackOff 6`, minority rows cited — needle-drop 100% → 0% at equal budget |

The kubectl row is the wave's thesis in one line: within ±3% of text/v1's
token count, the digest goes from hiding thirteen broken pods to naming
their exact distribution and citing the first of each kind. Structure at
equal budget, again.

Deliberately not built, with reasons on the record: mvn/gradle
(logtemplate covers it), AWS/cloud JSON (json/v1 covers it), pip/gh/npm
listings (inline covers them), ps aux (no census worth its tokens), rspec
at realistic small-suite volume (inline covers it; a flood-scale rspec
corpus is future work if one shows up in telemetry), directory trees
(find's head/tail + exact line count adequate; revisit with `ctx map`
interplay).

## The external benchmark landscape (surveyed 2026-07-19)

What exists, and what each actually measures:

- **rtk's table** (README): per-command % reductions (−70..−92%) and a
  session estimate (~118k → ~23.9k, −80%), from estimated command
  frequencies; live cumulative tracking via `rtk gain`. Independent
  replication: ~111k → ~23.2k over 78 real commands (madplay.github.io).
  Measures compression ratio only — no quality/outcome gate.
- **Headroom's suites** (docs site): token reduction paired with quality
  gates — GSM8K Δ0.000, SQuAD v2 97% at 19% reduction, BFCL 97% at 32%
  reduction, N=100; plus a "CCR Needle" lossless-retrieval test (N=50,
  100% retrieval at 77% reduction). Right shape of methodology; weak
  proxies for coding-agent workloads.
- **Caveman MicroBench**: 25 identical tasks, two harnesses, same model —
  ~524K vs ~1,010K tokens (1.93×) at comparable pass rates, raw CSV
  published. Closest external analog to our three-arm evals.
- **Terminal-Bench 2.0**: tokens in/out + API cost per trial alongside
  resolution rate, ≥5 runs per pair, Wilson CIs — the best public template
  for a cost-vs-success Pareto frontier. Notable: turn count barely
  correlates with success.
- **Anthropic's own upper bounds**: code-execution-with-MCP 150k → 2k
  (98.7%) — the ctx eval mechanism class; context-editing 84% token cut on
  a 100-turn eval.

Nobody publishes a benchmark that jointly scores (a) end-to-end task
success, (b) true cost including cache dynamics, and (c) evidence
preservation under compression. Axis (c) exists publicly only as our
needle-drop eval and Headroom's CCR Needle. That triad is the bench this
project's matrix + scorecard already approximates internally — and the gap
in the field our receipts are pointed at.

## Re-verified (same day, post plan-value wave)

Fresh live run of the identical corpus after the v0.25 profile work. The
two cargo defects recorded above are FIXED — cargo output is now claimed by
`cargotest/v1` with an exact census (`passed 144 · failed 6`, all six
identities inline), not text/v1's lucky tail window:

| corpus | raw tok | digest tok | ratio | profile |
|---|--:|--:|--:|---|
| cargo test (6/150 failing) | 3,092 | 191 | 16.2× | cargotest/v1 (exact census) |
| cargo test (150 passing) | 1,185 | 92 | 12.9× | cargotest/v1 |
| ps aux | 5,676 | 129 | 44.0× | text/v1 |
| find src tests | 2,872 | 202 | 14.2× | text/v1 |
| docker ps -a (40) | 1,522 | 191 | 8.0× | table/v1 (40×7 exact) |
| kubectl get pods (180) | 3,200 | 259 | 12.4× | table/v1 (180×5 exact) |
| mvn test (3,360 tests) | 4,781 | 584 | 8.2× | logtemplate/v1 |
| aws ec2 describe-instances (120) | 16,748 | 111 | 150.9× | json/v1 (exact shape) |
| pip list · gh pr list · rspec | ~477–574 | ~1× | pass-through | text/v1 |

Reading: floods collapse 8×–151× with exact censuses where a profile owns
the family; small outputs pass through ~1:1 (containment that would add no
value is not applied). rspec remains the weakest family (0.9× — a profile
gap already on the coverage queue).
