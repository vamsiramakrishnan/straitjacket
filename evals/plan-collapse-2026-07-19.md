# Plan-collapse: measured cost/turn/cache evidence for compiled evidence plans

**Date:** 2026-07-19 · v0.25.0 (P0–P3 + P5 surface shipped this wave;
docs/EVIDENCE-PLANS.md). Harness: `evals/plan_collapse.py`, CI-guarded by
`tests/test_plan_collapse.py`. One seeded auth-regression diagnosis, three
arms, **all real bytes** — real git, real pytest, the real digest and
plan-executor pipelines. Every number below is a byte count this script
produced; the snapshot is one run (real pytest/git output moves a few bytes
run to run — the script prints fresh numbers each time).

## Method

Fixture (shape copied from `tests/test_plan_exec.py::seeded_repo`): a git
repo with a committed baseline `auth.py` + `test_auth.py`, a ~300-line noise
module whose comments mention `from_request` in a scattered handful of lines
(so a raw grep is realistically noisy), then the regression edit that adds
`raise ValueError('missing tenant')` inside `from_request`. Built in a temp
dir with an isolated store (`CTX_STATE_HOME`); `__pycache__`/`.pytest_cache`
gitignored so the worktree is stable.

- **Arm N — naive interactive.** The canonical exploration a bare agent
  runs, one model round each: `git status --porcelain`; `git diff HEAD`;
  `python3 -m pytest -q`; `grep -rn from_request .`; `cat auth.py`;
  `cat test_auth.py`. `O_i` = raw output bytes of step *i*.
- **Arm B — harnessed interactive.** The same epistemic steps through the
  shipped verbs: `ctx run -- git diff HEAD`; `ctx run -- python3 -m pytest
  -q`; `ctx search repo: from_request`; `ctx get repo:auth.py --symbol
  from_request`. `O_i` = the actual emitted digest bytes (via
  `ctx.cli.main()`, stdout captured).
- **Arm P — compiled plan.** The 5-node diagnosis plan (`repo.changed` ·
  `test.run 'python3 -m pytest -q'` · `evidence.join failing_in_changed`
  after tests+changes · `evidence.join untouched_failures` after tests ·
  `ast.search 'from_request($ARG)'` guarded on `culprits.count > 0`) via
  `ctx plan run`. One boundary crossing; `O_1` = the single investigation
  digest; the plan JSON is counted as model **output**.

Tokens are `bytes // 4` (the repo's `estimate_tokens` convention). Engine
disclosure: arm P's probe node ran on `ast-grep-py 0.44.1` here (named in
the digest's coverage row); absent the binary it degrades to anchored-rg →
stdlib with the same contract, so the arm runs on a bare `[dev]` install.

**Not measured (declared).** There is no live model loop here — no
turns-to-fix, no answer quality, no per-round TTFB or cache-write wire cost.
This is transcript byte-flow only. The live four-arm referee
(docs/EVIDENCE-PLANS.md) is what closes those; see the Headroom note below
for why it did not run in this environment.

## The snapshot (one run of `python3 evals/plan_collapse.py`)

| arm | R (crossings) | Σ O_i first-exposure | C = Σ i·O_i (resend) |
|---|---|---|---|
| N naive interactive | 6 | 1,969 B · 492 tok | 6,818 B · 1,704 tok |
| B harnessed interactive | 4 | 2,240 B · 560 tok | 5,346 B · 1,336 tok |
| P compiled plan | 1 | 758 B · 189 tok | 758 B · 189 tok |

Arm P model-authored plan JSON (OUTPUT, not resent input): **607 B · est 151
tok**. Token counts only — output and input are priced asymmetrically; this
harness reports neither dollars.

Per-step `O_i` (the bytes behind the totals):

| arm | steps (`O_i` est tok) |
|---|---|
| N | status 2 · diff 76 · pytest 202 · grep 136 · cat auth 48 · cat test 25 |
| B | run diff 75 · run pytest 257 · search 163 · get 64 |
| P | plan digest 189 |

Cache-stability (each arm's step set run twice on the unchanged worktree +
store; byte-identical ⇒ a cache-aligned append):

| arm | byte-stable steps | instability source(s) |
|---|---|---|
| N | 5–6 / 6 | `pytest` carries a wall-clock `in N.NNs` token (volatile by construction; two adjacent runs may coincide) |
| B | 2 / 4 (`search`, `get`) | `ctx run` re-runs **densify** (a re-exposure feature — denser digest / banner) + a fresh capture-id handle |
| P | 0 / 1 | one `investigate:<id>` handle moved; the digest **body** was byte-identical (node cache returned identical artifacts) |

## The formulas (EDC §20: counterfactuals carry their derivation)

Let arm have rounds `i = 1..R`, each emitting `O_i` model-visible bytes.

- **First-exposure tokens** `Σ O_i` — bytes crossing the boundary once, when
  first read. This is what the raw table's "first-exposure" column sums.
- **Boundary crossings** `R` — model rounds; the count the plan attacks
  directly (6 → 4 → 1).
- **Episode input cost (append-only resend model)**
  `C = Σ_i (i · O_i)`. Model, stated explicitly: outputs land in context and
  are re-sent on every later round, so an output that only arrives at round
  *i* has dragged the whole accumulated prefix to get there — it is weighted
  by the round index at which it finally lands. One early crossing beats six
  late ones at equal bytes; an `R = 1` arm pays exactly `1 · O_1`. This is a
  monotone comparator, not a billing model (real caching discounts resends,
  and input/output prices differ — hence the plan JSON is reported separately
  as output, above).

Reading the snapshot through the formulas:

- **P collapses rounds 6 → 1** and lands the whole diagnosis in one 189-tok
  digest. On resend cost it is **9.0× under N** (189 vs 1,704) and **7.1×
  under B** (189 vs 1,336). The model-authored plan is 151 output tokens —
  paid once, on the assistant side.
- **B beats N on rounds and resend** (4 vs 6; 1,336 vs 1,704 tok, −22%) but
  its **first-exposure is *higher* than N's** (560 vs 492 tok). This is the
  honest floor: on a fixture this tiny the digests' provenance (spans,
  snapshots, coverage attestation, the pytest census) costs more bytes than
  the raw output it replaces. Digest bounding is a *scale* win, not a
  small-input win — cf. the needle-drop eval where raw 304k tok became ~180
  (evals/headroom-needle-drop-2026-07-17.md). Here the input is already
  small, so B's value is round economy and byte-stability, not compression.
- **Cache shape is the sharper contrast.** N's raw pytest line is volatile
  by construction (a wall-clock token); B's read verbs are byte-identical;
  P's digest body is byte-identical across re-runs with only a retrieval
  handle moving. A stable prefix is worth more than a slightly smaller
  volatile one — the prefix-stability contract (CONTRIBUTING rule 2),
  extended to the shape of a whole investigation.

## Honest Headroom comparison (derived, not head-to-head)

Headroom was **not re-run in this environment — its proxy is not installed
here.** The comparison below therefore cites *already-measured* numbers from
the two prior docs and derives the arm-P contrast; it is not a fresh
head-to-head. The four-arm live referee that would settle it remains open
(docs/EVIDENCE-PLANS.md, P5 gate).

Measured Headroom facts on record:

- **Quiet-needle drop:** Headroom compressed a 347,595-tok log to **68 tok**
  and **silently dropped** the structurally-rare needle; ctx's template
  digest kept it (304k → ~180 tok, needle verbatim at `L14238`)
  (headroom-needle-drop-2026-07-17.md).
- **Cache churn:** on short tasks Headroom's cache-hit rate was **80.6–84.2%**
  vs **96.5–98.1%** for naive/sj, with **3–6× the ongoing cache-write**
  volume, because rewriting transcript content retroactively breaks upstream
  prefix matches (matrix-2026-07-18.md; the earlier overhaul measured 3.6×
  write volume).

Derived arm-P contrast (from *this* eval's cache-stability rows, not from a
Headroom run): the plan's digest body is byte-identical across re-exposure —
retroactive rewriting is structurally impossible because the model never
re-reads a growing transcript; it reads one bounded digest and every
volatile intermediate lives in an addressed artifact. So the churn mechanism
that costs Headroom 12–16 points of hit rate **cannot arise** on the plan
arm. That is a claim about *shape*, derived from the byte-stability measured
here — **not** a measured hit-rate delta against Headroom in this
environment. Confirming it needs the live referee.

## Caveats (read before citing)

- One fixture, one seed, one diagnosis shape (hypothesis-stable). No live
  model: turns-to-fix, answer quality, TTFB, and true billed cache are all
  out of scope here — the four-arm referee owns them and has not run.
- `C = Σ i·O_i` is a deliberately simple monotone comparator; it ignores
  prompt-cache read discounts and input/output price asymmetry. Use it for
  *ordering* arms, not for costing an episode in dollars.
- Arm B's first-exposure exceeding arm N's is real and reported, not hidden:
  digest overhead dominates compression at this input size.
- The Headroom section is derived from prior measurements plus this eval's
  byte-stability rows; it is explicitly **not** a head-to-head in this
  environment (Headroom's proxy is absent).
- ast-grep is present here (`ast-grep-py 0.44.1`); the numbers would shift a
  little on the stdlib fallback, but the arm structure and the round collapse
  do not depend on it.
