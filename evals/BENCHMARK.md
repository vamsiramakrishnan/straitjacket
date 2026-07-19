# straitjacket-bench: the paired-corpus charter

Adopted 2026-07-19 from an external review whose core argument we accept:
**one corpus cannot referee this system.** straitjacket changes the agent's
information channel, so the benchmark must answer four different questions
with four different instruments:

1. Does the agent still solve the task? (outcome)
2. Does it find the right evidence efficiently? (retrieval)
3. Does the environment produce the pathological outputs we exist to
   contain? (stress)
4. Does containment ever remove decisive evidence? (invariants)

This charter records what we adopt, what we adapt to existing doctrine,
what we decline, and the concrete build list — with the honest inventory
of how much already exists. Receipts style: nothing below claims to be
built unless a file path says where.

## The corpus, and what already exists

```
straitjacket-bench/v1
├── explore/      swe-explore-60        retrieval quality      [TO BUILD]
├── repair/       swe-bench-verified-60 downstream correctness [TO BUILD]
├── terminal/     terminal-bench-30     hostile-output shapes  [PARTIAL]
├── multilingual/ swe-multilingual-30   non-Python regression  [TO BUILD]
├── evidence/     sj-evidencebench-40   invariant adversaries  [~80% EXISTS]
└── smoke/        sj-canary-12          PR-cheap determinism   [EXISTS, unlabeled]
```

### evidence/ — the inventory (verified against tests/, 2026-07-19)

| Scenario | Status | Where |
|---|---|---|
| A · tail-critical outputs | ✅ exists | tests/test_headtail_digest.py |
| B · middle-critical quiet needle | ✅ exists | tests/test_logprofile.py + evals/headroom-needle-drop |
| C · multi-failure census | ✅ exists | tests/test_pytest_census.py, contracts |
| D · repetition vs starvation | ✅ exists | tests/test_reflex.py (edit-cadence scoring) |
| E · scope vs presentation flags | ✅ pytest only | tests/test_reflex.py; other families are table rows to add (reflex.py `_FAMILY_SCOPE_FLAGS`) |
| F · stdout/stderr descriptor graphs | ⚠️ partial | hook `_REDIR_ALL_RE` covers `>f 2>&1`; `2>&1 >f`, `tee`, `2>err.log` untested — **build** |
| G · tabular adversaries | ✅ core | tests/test_coverage_profiles.py; ragged/100k-row/dup-header variants — extend |
| H · machine-format negotiation | ❌ missing | JUnit XML / SARIF / JSON-diagnostics parity vs prose — **build** |
| I · binary / invalid text / secrets | ✅ exists | tests/test_capture_and_determinism.py, test_emission_gate.py, redaction tests |
| J · long-runner lifecycle | ✅ exists | tests/test_jobs.py (8 tests) |

The external review's framing is adopted verbatim as the module name:
these are **evidence-channel conformance tests, not model benchmarks**.
They run in Tier 0, unpaid.

### terminal/ — partial via evals/coverage_corpus.py

The coverage corpus (11 output families, live + labeled replay) is the
static half: it referees digest shape against hostile outputs. The
Terminal-Bench slice adds the dynamic half — an agent driving those
outputs. Selection rule adopted: choose by **evidence shape** (compiler,
package-manager, docker build, process tables, JSON, ANSI/progress noise,
mixed streams, long-runners), not by task topic.

## Metrics: adopted, with two additions of our own

Adopted verbatim (definitions per the external review):

- **Evidence density** = gold-region lines surfaced / model-visible
  evidence tokens.
- **Retrieval regret** R = T_actual − T_oracle (tokens before sufficient
  evidence vs oracle-span minimum).
- **Containment ratio** = 1 − visible/raw tool-output tokens (already
  computed live by `ctx gain`; the benchmark reports it per-arm).
- **Evidence preservation** = solved-under-SJ / solved-native. The
  load-bearing gate: no headline is reportable unless this stays ≈ 1.0.
- **Success-adjusted cost** = $ / resolved task.
- Pareto surface, never a single collapsed score. (Already doctrine —
  the regime scoreboard in README is this; the benchmark formalizes it.)
- **Low-output controls** as a first-class stratum. (Already learned the
  hard way: the tiny-surgical-task 4.5× regression → graduated
  engagement. The controls keep that lesson enforced.)

Our additions, from mechanisms this repo already has:

- **Evidence sufficiency (offline)** — `ctx replay` scores facts the
  model *provably used downstream* as inline vs one-hop against simulated
  digests (src/ctx/replay.py). This is evidence density's cheap sibling:
  it needs no paid runs and no gold labels, and it regression-gates
  profile changes against archived transcripts (measured: 11/11 and
  42/42 on spec3 archives). SWE-Explore's gold regions, when we ingest
  them, upgrade this from "facts used" to "facts needed."
- **Unresolved-omission rate** — every omission must carry an address
  that resolves; the store can verify this mechanically per digest.
  Target is 100% resolvable, and it is testable without a model.

## Arms and controls: adapted

Adopted: three arms (native / +capture / +capture+verbs), matched model,
prompt-delta minimized, matched turn and wall budgets, matched repo
images. The B-vs-C split is new to us and worth keeping — it separates
"containment works" from "retrieval verbs help," which our A/Bs have
conflated.

Adapted with a correction: **temperature and seed are not controllable**
through Claude Code. Determinism of the *judgment* comes instead from
paired tasks × repeated runs × median aggregation with frozen-constant
checksums — exactly the spec3 referee machinery (`spec3_runner.py
--repeats/--gates`). Three seeds → three repeats; the statistics
discipline is the same, the mechanism is different.

## Sampling: adopted outright, and it fixes a real bias

Stratify by **output pathology, not topic**, using a cheap oracle run
that annotates each candidate task (raw output tokens, largest single
output, failure count, command repetitions, evidence dispersion,
tail-criticality, mixed streams, language, build family). The review's
warning is one we've already half-lived: sample only floods and you
overstate the harness; sample only small tasks and you kill it. The
pathology annotation is itself computable by `ctx replay` over an oracle
trajectory — the annotator and the simulator are the same machinery.

## Declined or deferred, with reasons

- **Resolve rate as headline** — declined (was already doctrine). The
  public statement is the constraint form: matched-or-better success,
  then minimize tokens/turns/time/unresolved omissions.
- **Claw-SWE-Bench** — deferred, same reason the review gives: too new
  to be a public proof point. Revisit after v1 stabilizes; its
  harness-as-variable premise is our premise, so it is a natural fit
  later.
- **Terminal-Bench Pro** — declined for v1: half the tasks are private;
  reproducibility of the public claim wins.
- **SWE-Explore / benchmark citations** — adopted contingent on
  verification. The dataset claims (848 issues, 10 languages, line-level
  gold regions) must be verified against the actual release before any
  manifest is committed; if the gold regions hold up, explore/ becomes
  the primary mechanism benchmark, exactly as argued.

## Tiers, mapped to infrastructure that exists

| Tier | Trigger | Content | Infra |
|---|---|---|---|
| 0 · PR canary | every PR | evidence/ conformance + determinism + store/jobs/secrets | pytest markers over existing tests (label as `sj_canary`) |
| 1 · mechanism referee | digest/hook/reflex/query changes | 12–20 paired agent tasks, 3 repeats | matrix_runner subsets + ctx replay regression on archived transcripts |
| 2 · wave gate | per mechanism wave | 60 paired tasks | matrix_runner + spec3 referee |
| 3 · release | per public release | full 180 + EvidenceBench | all of the above |

Tier 1 gains a free member no external design could have: **replay
regression** — rerun `ctx replay` on archived harnessed transcripts after
any profile change; evidence sufficiency must not drop. Zero API cost.

## Build list (ordered)

1. **EvidenceBench F + H** — descriptor-graph semantics tests and
   machine-format negotiation tests (the two verified gaps). Small,
   unpaid, closes evidence/ to ~100%.
2. **Canary labeling** — mark the Tier-0 set with a pytest marker so
   `pytest -m sj_canary` is the PR gate.
3. **Pathology oracle** — `evals/` annotator emitting the stratification
   JSON from a recorded trajectory (shares parsing with ctx.replay).
4. **SJ-Explore-60 manifest** — after verifying the dataset: stratified
   per the review (20 single-file / 20 cross-file / 20 dispersed, ≥6
   languages, oversample flood repos), with evidence-density scoring
   wired through the replay machinery.
5. **SJ-SWE-60 manifest** — stratified by observability problem (15
   test-flood / 10 search-flood / 10 re-verification / 10 cross-file / 5
   long-runner / 5 low-output controls / 5 misleading-output).
6. **Terminal-30 + Multilingual-30** — multilingual doubles as the
   regression corpus for the Python-shaped mechanisms (skeletons,
   failure extraction, symbol verbs); "falls back without crashing" and
   "delivers useful evidence" are different thresholds, and the corpus
   referees the second.

Items 1–3 are this-week work. Items 4–6 need dataset verification,
runner plumbing (Docker eval images), and paid runs — wave-scale work,
gated on the same rule as everything else here: instrument first, then
spend.
