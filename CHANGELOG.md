# Changelog

All notable changes to ctx-harness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is 0.x
with a minor bump per mechanism wave (see CONTRIBUTING.md).

## [0.31.0] - 2026-07-24

Harness collaboration: `ctx wrap` stops knowing three hosts by name, and starts
routing work across the harnesses it finds by what their models cost.

- **M-M · Data-driven host registry** (`src/ctx/hosts.py`): one `HostSpec` per
  coding-agent CLI states how to detect it on PATH, how to resolve its model,
  which installer/wrapper wires it, and whether its output side can substitute
  (enforced) or only nudge. Adding a host is a data edit. Each detected CLI is
  joined to `ctx.pricing` so it carries a model→price tier. The three shipped
  hosts move in verbatim; extra CLIs (Gemini, Cursor, aider, opencode) are
  detected and priced but marked not-yet-harnessable rather than silently
  dropped.
- **`ctx wrap detect`** prints an installed/model/price table across every
  registered CLI; **`ctx wrap setup` is now detection-driven** — it configures
  the harnessable CLIs it finds and names the ones it skipped, while
  **`ctx wrap all`** forces every supported host (the old behaviour). The
  low-level `setup_hosts` primitive is unchanged.
- **M-M · Harness collaboration orchestrator** (`src/ctx/orchestrator.py`,
  `ctx orchestrate "<task>"`): **task coordination, not open-loop calling.** A
  cheap coordinator — the cheapest installed harness priced by its *coordinator
  model* (Antigravity on Gemini-flash-lite), guided by the routing skill —
  splits the task into a `ctx.route/v1` DAG. Each node is routed by **capability
  × price**: the cheapest installed harness that clears the node's `min_tier`
  and covers its capability tags (`hosts.pick_worker`) — economy work
  (search/triage/verify) to the economy harness, frontier work (synthesis/edit)
  to the frontier one. The DAG is validated (acyclic, bounded, budgeted) and
  **priced up front, shown, then run in a closed loop**: ready nodes run in
  parallel waves; each dependent sees only its upstreams' `ctx.checkpoint/v1`
  digests (addressed evidence, never raw bytes); a failed node escalates once to
  a stronger harness; between waves the coordinator may patch the plan with
  follow-up nodes. When no coordinator can run, a deterministic capability-routed
  fallback DAG (explore→implement→verify) is used, so orchestration works
  offline. Bounded by `max_waves` / `max_replans` / `budget_usd`; fail-open
  throughout; a single installed harness degrades with zero claimed saving.
- **Routing is by model, not just harness.** `HostSpec` carries a `models`
  catalog — each harness runs several models spanning tiers (Claude:
  opus-4.8/sonnet-4.6/haiku-4.5; Codex: gpt-5.6 sol/terra/luna; Antigravity:
  gemini-3.1-pro/3.6-flash/3.6-flash-lite), researched from each CLI's model
  list. `hosts.pick_model` chooses the cheapest `(harness, model)` that clears a
  node's tier and covers its roles, so **ordinary implementation routes to a
  cheap standard model (Gemini 3.6 Flash) and planning to a frontier model — even
  within a single harness** (Claude-only still routes explore→Haiku, plan→Opus,
  implement→Sonnet). Nodes can pin `"model"`; escalation bumps to a stronger
  model; the model catalog is documented in the routing skill. New
  gemini-3.6-flash / 3.6-flash-lite / 3.5-flash-lite price rows.
- **Routing skill** (`references/harness-collaboration.md`): the `ctx.route/v1`
  contract and capability×price routing rules, kept in lockstep with
  `ROUTING_CONTRACT` so the coordinator behaves the same from the skill or the
  inlined prompt.
- **`[orchestrate]` config block** (`ctx.config.OrchestratePolicy`): closed-loop
  bounds (`max_nodes`/`max_waves`/`max_replans`/`budget_usd`/`node_timeout`),
  `fallback_only`, `confirm` gate, and per-node token estimates.
- **Live cross-vendor collaboration, proven** (`evals/live-collab-antigravity-claude-2026-07-24.md`):
  real Gemini (Antigravity's model, via the API) + real Claude run a two-node
  route through the actual `run_route` loop, with the CAS `checkpoint:` handoff
  verified in-harness and real tokens billed (total ~$0.02). Surfaced and fixed
  a real gap: launch-time model ids differ from display/pricing ids — Claude
  wants `haiku`, the Gemini API serves `gemini-3.5-flash-lite`. Added
  `ModelChoice.cli_id` (`launch_id`), threaded through `run_route`; Codex's
  non-interactive form corrected to `codex exec` (flag order still unverified —
  Codex not installed where the live A/B ran).
- **Offline receipt** (`evals/orchestrator-cost-routing-2026-07-24.md`): the
  deterministic cost model — ~72% cheaper than an all-frontier run within a
  single harness, ~92% with an economy harness. The full billed A/B vs a
  single-model baseline remains TO-BUILD.
- Tests: `tests/test_hosts.py` (capability tiers, `pick_worker` gating,
  cheapest-coordinator), `tests/test_orchestrator.py` (route-IR validation —
  cycles/unknown-deps/budget/node-cap, topological waves, deterministic priced
  plan, coordinator JSON parse, and the closed loop — parallel handoff, failure
  escalation, dependent-skip, bounded re-plan).

## [0.30.0] - 2026-07-21

Building the toolchains that were "not available" — tree-sitter and SCIP.

- **M-K4 · SCIP ingestion, shipped** (`scip_ingest.py`, `_vendor/scip_pb2`):
  an opportunistic `index.scip` reader adds **precise, compiler-backed
  references** at the top of the refs engine ladder (**SCIP (exact) →
  jedi → ast**), disclosed per node (`ctx refs` / `code.refs` show `engine
  scip (exact)`). `find_index` reads `index.scip` at the workspace root or
  `$CTX_SCIP_INDEX`; the index is only read, never generated. The protobuf
  runtime is the `[scip]` extra; the SCIP bindings are vendored
  (`src/ctx/_vendor/scip_pb2.py` generated from the committed `scip.proto`);
  either absent → the ingester degrades to None and the ladder falls
  through — absence costs nothing. `resolve_refs` is now the single ladder
  used by `ctx refs`, `ctx q refs`, and the `code.refs` op.
  - **Precision receipt** (`evals/scip_precision.py` + `.md`): on an
    ambiguity fixture (a name also in a comment, a string, and a shadowing
    local), SCIP scores 100% precision / 0 false positives vs the textual
    rung's 50% / 4 false positives. Tested with a committed real
    `index.scip` (`tests/fixtures/scip_sample.scip`, from `scip-python`),
    so CI needs only protobuf, not the indexer.
- **Tree-sitter grammar-wheel backend** (`skeleton.py`): the skeleton
  tier's tree-sitter extractor gains a third, offline-safe path —
  individual `tree_sitter_<lang>` grammar wheels via the modern core API
  (the bundle `tree-sitter-language-pack` fetches parsers at runtime, a
  sandbox 403). It carries a JS/TS skeleton that stdlib `ast` cannot parse
  and ctags need not. The `[code]` extra now pins the grammar wheels
  (`tree-sitter-python/javascript/typescript`) instead of the unreliable
  language-pack.
- CI `full` job installs `.[dev,map,fast,code,scip]` so both new backends
  are exercised, not just skipped. Suite: 994 passed (venv with all
  extras); tests skip-if-absent so the minimal job stays green.

## [0.29.0] - 2026-07-20

Finishing the designed-not-built bucket (M-K/M-L), with receipts.

- **`ctx ask` intent family completed** (`ask.py`, `plan_ops.py`, `cli.py`):
  four new intents join locate/impact/diagnose. `trace` (structural call
  path — refs → callers → callees → transitive reach) and `compare`
  (behavioral run-diff via the new `evidence.diff` plan op) are observe-
  class. `verify` (changes → related tests → run the suite) and `review`
  (changes → symbols → tests → run → root-cause join + counterevidence)
  are **execute-class**: CLI runs them, the bounded MCP tier rejects
  `test.run` (`execute_on_observe_tier`), and each intent discloses its
  class. New `--against`/`--command` flags; compare/verify slots teach when
  missing. All seven compile deterministically to `ctx.plan/v1`.
- **M-K3 `records_opportunity` ledger** (`hook.py`): a jq / `sort|uniq -c`
  / awk-projection pipeline is detected, taught the `ctx q records`
  collapse, and recorded to `.ctx-session-reads/records-adoption.jsonl` —
  the demand denominator. (The jq physical compile target stays deferred:
  pure speed, no capability gain.)
- **M-K5 comby decline-corpus gate** (`plan_ops.py`): `ast.rewrite.preview`
  now records `comby_candidate` entries (engine absent, or no structural
  match) to `.ctx-session-reads/rewrite-declines.jsonl`. Instrumentation
  ONLY — the comby rung stays unbuilt until this corpus shows real demand.
- **M-K4 SCIP ingestion: deferred, with reason** — no SCIP toolchain or
  protobuf in this environment to produce a real `.scip` test fixture, so
  building an untested ingester is the speculative code the project
  refuses. Recorded in docs/SUBSTRATE.md.
- **Evals**: M-K2 scoped-scan receipt (`evals/corpus_scoped_scan.py` +
  receipt) — corpus reduces the eligible set 178→9 files (94.9%), a 13.1×
  ast-grep wall speedup even on the fast engine (the slow Semgrep arm is
  declared, not run — Semgrep absent here). Plus a Sonnet addendum to the
  3-arm diagnosis receipt: a stronger model adopts `ctx ask` once the card
  is in context (as haiku did), but on a no-flood task adoption still
  costs turns — the A/B/C payoff referee needs a flood-bearing task.
- Skill/AGENTS teach all seven intents (skill BODY change — invocation-
  loaded, no prefix-cache cost; manifest regenerated at PREFIX_VERSION 4).

## [0.28.0] - 2026-07-20

The skill catches up to the engines, plus a measured three-arm receipt.

- **Skill vocabulary refresh** (`plugins/antigravity/skills/ctx-harness/`,
  Codex `AGENTS.md` block): `SKILL.md` and `references/verbs.md` stopped at
  the pre-M-J `run/search/get/stats` vocabulary. They now teach `ctx ask`
  (intents locate/impact/diagnose), `ctx q` (the composition algebra incl.
  `corpus`/`records`/`distinct`/`histogram`), and `ctx investigate`/`plan`.
  **PREFIX_VERSION 3 → 4**: the skill body/frontmatter are prefix-resident,
  so this is a one-time full-prefix cache rewrite per user (the injected-
  prefix stability contract; `prefixassets.py` manifest regenerated).
- **Claude Code teaching surface** (`installer.py`): `install_claude` now
  upserts a compact ctx verb card into the workspace `CLAUDE.md` (marker-
  delimited, idempotent, mirroring the Codex `AGENTS.md` block). Measured
  gap — the shipped verbs had no teaching surface on Claude Code, so agents
  never invoked them (see the receipt below); with the card in context,
  they do.
- **Three-arm diagnosis receipt** (`evals/ask_diagnose_3arm.py`,
  `evals/ask-diagnose-3arm-2026-07-20.md`): real coding agents (Haiku),
  naive vs Headroom vs straitjacket vs straitjacket+card, on a seeded
  single-bug diagnosis with a model-free grader. Findings: on a no-flood
  task all arms solve it and containment is bounded overhead (the expected
  low-complexity regime); and the vocabulary is adopted only when it
  reaches the agent (0 `ctx ask`/`ctx q` bare; both invoked once the card
  is in `CLAUDE.md`). Reusable 3/4-arm harness with a transcript-derived
  adoption counter.

## [0.27.0] - 2026-07-20

The `ctx ask` wave (ROADMAP M-L, docs/ASK.md): a repository question
compiles into a typed intent preset — a frozen `ctx.plan/v1` template
with typed slots — executed on the shipped plan tier. Collapses the
*decision cost* of exploration (which verbs, in what order) the way M-J
collapsed its *turn cost*. The adopted core of an external retrieval
proposal, audited: the natural-language parser, `reveal`/`audit` verbs,
the whole-surface rebrand, and the entity/relation ontology were cut;
what shipped is the elegant, testable spine.

- **Phase 0 · thin observe ops** (`plan_ops.py`):
  - `evidence.failures` — failure census from CAPTURED facts, never a
    rerun. Freshness against the current generation is computed and
    DECLARED: stale facts carry `fresh: false` + a note proposing (never
    running) a refresh — the observe invariant made legible, using the
    same `generation_hash` semantics as the rest of the system.
  - `code.symbols` — structured symbol rows (identity · kind · range ·
    span) from skeleton-derived facts; census before detail, no outline
    text. An input warms facts for exactly those files (content-keyed).
  - `code.context` — terminal bounded materialization (sites get
    line±context, symbols their clamped range); emits `text`, the
    refinement boundary at the plan tier.
- **Phase 1 · intents + `ctx ask`** (`ask.py`, `cli.py`): `locate`,
  `impact`, `diagnose` as deterministic slot→`ctx.plan/v1` presets
  (`json.dumps(sort_keys=True)` ⇒ stable plan id ⇒ stable node-cache
  keys). **No natural-language parser**: `--intent` is a flag; the
  subject is `--symbol` or the question's sole identifier-shaped token
  (dotted/snake/CamelCase — capitalized English is skipped), inferred
  only when unambiguous and always disclosed. A missing/ambiguous slot
  is a teaching error that SUGGESTS an intent and never guesses-and-runs.
  The interpretation (`intent:`/`subject:`) rides above the digest, never
  behind `--trace`. `ctx ask "q" --intent <i> [--symbol X] [--run r]
  [--depth N] [--plan]`.
- Every intent is observe-class end to end (diagnose reads captured
  failures, never reruns); counterevidence is a structural join node
  (rendered even when empty); the only text-emitting node is
  `code.context` (bytes materialize once, terminally — the closure law).
- Verified end to end: on a seeded regression (`raise` in a changed
  function, its failing run captured), `ctx ask --intent diagnose` names
  the culprit symbol with plane attribution in one digest, no rerun.
- Tests: `test_ask.py` (compiler determinism, teaching-not-guessing,
  no-rerun invariant at compile time and end to end, freshness
  declaration, terminal materialization). Suite 968 passed / 0 failed.

## [0.26.0] - 2026-07-20

The substrate wave (ROADMAP M-K, docs/SUBSTRATE.md): the operator classes
beneath the semantic layers, from the audited external "evidence algebra"
proposal. Phases K1–K3 + K5.3 shipped; K4 (SCIP) and K5 (comby, gated on a
decline corpus) remain designed; K6 (watch warming) waits for the broker.

- **M-K1 · span-precise sites** (`rg_engine.py`, `search.py`, `query.py`):
  search results carry 1-based half-open `[col_a, col_b)` character
  columns — captured from the rg `--json` submatches already on the wire,
  and from `finditer` spans in the Python engine (leftmost match per line,
  parity by construction; pattern-index recovery is span-anchored, the
  whole-line re-match demoted to labeled fallback). Every `ctx search`
  emission now mints a `ctx.search/v1` result blob (`result: blob:<id>`)
  so a search is citable as one handle — engine parity extends to
  byte-identical blobs.
- **M-K2 · the file-set algebra** (`filesets.py`, new): the missing
  `file_select` operator class. `ctx q 'corpus [--ext E]… [--glob G]…
  [--exclude G]… [--changed] [--max N]'` and the `repo.files` plan op
  emit a bounded eligible file set with a coverage receipt (`considered ·
  selected · engine [· gen]`) that survives combinators and rides the
  minted payload. Engine ladder git ls-files → **fd** (opportunistic, run
  `--no-ignore` so `ws.is_ignored` stays the single ignore authority —
  listings byte-identical across engines by construction;
  `CTX_FILES_ENGINE` kill-switch) → os.walk. `--changed` binds to the
  generation snapshot, never mtime (SUBSTRATE §2.4). Scoping
  `semantic.*` to a `repo.files` result confines the engine to the
  selected set — *select files before scanning*.
- **M-K3 · the records algebra** (`query.py`): `records <run:|blob:>
  [--jsonl] [--pointer /p]` opens stored JSON/JSONL artifacts (compiler
  output, test JSON, SARIF, lockfiles) as the `records` kind, where the
  shipped combinators plus new total stages `distinct <field>` and
  `histogram <field> [--buckets N]` (numeric buckets or categorical
  census, capped with declared omission) absorb the jq class without
  importing the jq language. All four new stages carry derived closure
  classes; the digest-closure pins extend to them.
- **M-K5.3 · text-tool steering** (`hook.py`): `sed`/`awk`-family
  commands leave the unknown-command limbo. Read-only invocations steer
  into bounded `ctx run` capture like grep/find; **in-place** invocations
  (`sed -i`, `gawk -i inplace` — detected in plain argv and inside
  compound expressions, which is where every `{…}` awk program lands)
  force_ask with a preview-first remediation and are never auto-rewritten
  into a capture that would still mutate files.
- Tests: `test_filesets.py` (engine parity incl. fd skip-if-absent,
  generation-bound `--changed`, receipts), `test_substrate.py` (span
  blobs, records/distinct/histogram, totality), closure pins, rg/python
  column-parity extension, sed/awk steering cases.
- **Word-anchored pytest detection** (`pytestprof.py`, `facts.py`): the
  profile claim and the facts-tier family detection matched `"pytest"`
  as a raw substring of the joined argv, so a command whose INTERPRETER
  lives under a pytest-named directory (uv tool shims:
  `…/tools/pytest/bin/python -c …`) or whose args carry pytest-named
  paths (`/tmp/pytest-of-root/…`) was misclaimed as a test run — the
  replay doctrine's "a file containing test markers is not a test run",
  violated at birth. Detection is now word-anchored (program basename or
  `-m` module target; never an interior path component), shared via
  `argv_invokes_pytest`, and regression-pinned.
- **Environment-robust fixtures**: three fixtures invoked a bare
  `python3 -m pytest` (the one interpreter NOT guaranteed to carry
  pytest) — `test_plan_exec`'s diagnosis plan, `evals/plan_collapse.py`'s
  plan arm (its other two arms already used `sys.executable`), and
  `test_reflex`'s ground-truth run — all now `sys.executable`. The
  scaffold-slim overhead budget in `test_lint_and_gain` is now relative
  to the rendered command line (a venv-deep interpreter path must not
  fail a fixed byte budget). Full suite green under both a clean venv
  and a uv-tool pytest shim.

## [0.25.0] - 2026-07-19

The compiled-evidence-plans wave (ROADMAP M-J, docs/EVIDENCE-PLANS.md):
repository exploration moves from an LLM-mediated control loop toward one
model round per hypothesis epoch — the model compiles a typed, total,
bounded DAG of evidence operations; the harness validates, prices, and
executes it locally; one causally organized digest returns.

- **`ctx.plan/v1` IR** (`plan_ir.py`): model-authored JSON DAG, statically
  validated (cycle-free by construction — edges reference earlier steps
  only; ≤24 nodes; mandatory foreach caps ≤64; `when` guard micro-grammar;
  closed rejection vocabulary) and priced before execution
  (`ctx plan validate|price`, the PRICED-CONTEXT idiom).
- **22 logical operators** (`plan_ops.py`) over shipped machinery: the q
  stage registry (`code.search/refs/callers/callees/impact`, combinators,
  `q.pipe`), facts Angle-lite joins (`evidence.join` — the root-cause join
  `failing_in_changed`, counterevidence via `untouched_failures`),
  skeleton outlines, `repo.changed` (now deriving decl facts for changed
  files, upgrading the join to symbol precision), `test.run` (birth-gate
  capture + failing census + `run:` handle). Ops declare capability class
  (observe|execute), cost class, and engine requirements.
- **Executor + `investigate/v1` digest** (`plan_exec.py`): plan-order
  execution (deterministic bytes by construction), per-node
  `ctx.plan-node/v1` blobs, typed skip declarations (guards, engine
  absences, error cascades, wall-budget exhaustion — the digest always
  renders), `ctx.investigation/v1` manifests, ranked conclusion candidates
  with plane attribution (dynamic/temporal/static/semantic), REQUIRED
  counterevidence (empty form declared), coverage attestation with
  per-node engine disclosure, contract-checked at the selection seam
  (`contracts/investigate.toml`). Expensive external-engine scans are
  node-cached on a content-sensitive workspace fingerprint.
- **ast-grep tier** (`astgrep.py`, opportunistic binary): structural
  `ast.search` with span-shaped sorted matches; degraded tier is a
  metavariable-anchored regex honestly labeled `textual`. Probe rejects
  shadow-utils `sg`. `ast.rewrite.preview` mints the full patch as an
  addressable blob; `apply` is transactional (`git apply`) and refuses on
  generation drift. No lossy fallback for rewrites, by design.
- **Semgrep tier** (`semgrep_engine.py`, `[sem]` extra): hermetic by
  construction (local rules confined to the workspace, `--metrics=off`,
  no version check, no registry fetch); findings normalized/sorted into
  typed rows with dataflow-trace frames; absence is a declared skip.
- **EvidenceGraph v2 relations** (additive): typed `(from, relation, to)`
  triples from a closed vocabulary; a graph without relations serializes
  byte-identically to v1, so every pinned golden and cache key holds.
- **CLI + MCP**: `ctx plan validate|price|run|ops`, `ctx investigate`
  (epochal control: replans beyond the `[plan]` allowance get a declared
  banner + reflex-plane ledger event, never a block). MCP op
  `investigate` accepts observe-class plans only; execute-class ops are
  typed rejections at tier=mcp (SPEC §10.4 preserved; tool description
  bytes unchanged — no prefix-version bump).
- Declared debt e319eef641: physical operator selection is
  availability-based (the shipped `_select_engine` idiom); the
  telemetry-compiled `[plan_engines]` cost-table epoch (EVIDENCE-PLANS
  P4) lands once plan-node telemetry accumulates.
- 43 new acceptance tests (IR totality, end-to-end diagnosis, byte
  determinism, addressability, tier enforcement, fake-binary engine
  contracts, generation-guarded apply); full suite 757 passed on both the
  full and minimal (no-binaries) matrices.

Second batch, same wave:

- **ast-grep-py library rung**: `ast.search` now degrades through three
  disclosed tiers — ast-grep binary (structural) → `ast-grep-py` library
  (structural, in-process, added to the `[code]` extra) → labeled
  metavariable-anchored regex. `engine_id()` precedence feeds node cache
  keys; rewrites stay binary-only by design.
- **Measured evidence** (`evals/plan-collapse-2026-07-19.md`, runnable
  `evals/plan_collapse.py`, CI-guarded): on a seeded auth-regression
  diagnosis, boundary crossings collapse 6 (naive) → 4 (harnessed) → 1
  (plan); append-only resend cost 1,704 → 1,336 → **189 tok** (9.0× under
  naive, 7.1× under harnessed-interactive); the plan digest body is
  byte-identical across re-runs (cache-aligned) where naive pytest output
  carries a volatile wall-clock token. Headroom comparison cited from
  prior measurements and explicitly labeled derived, not head-to-head.
- **Skill progressive disclosure**: plan authoring ships as
  `references/evidence-plans.md` (loaded on demand only); the SKILL.md
  body gains a one-line pointer; frontmatter untouched, prefix manifest
  regenerated without a PREFIX_VERSION bump — zero always-in-prompt
  footprint growth.
- **Fix**: repo search no longer scans the `.ctx-session-reads/` ledger
  (both rg and python engines) — the ledger is bookkeeping, never
  evidence, and it grows as the harness runs, so scanning it made
  identical searches non-byte-identical (found by the plan-collapse
  cache-stability probe).

## [0.24.0] - 2026-07-19

The coverage-corpus wave: rtk's breadth question answered the house way —
real corpora measured before any profile was built, hypotheses killed on
the record (evals/coverage-corpus-2026-07-19.md).

- **`evals/coverage_corpus.py`** — the rtk-corpus method made re-runnable:
  every corpus (live toolchain capture or labeled fixture replay) goes
  through a stub binary carrying the real tool's name, so `ctx run`
  exercises true argv-anchored detection, shape dispatch, slim inline, and
  budgets; emits the raw/digest/ratio/profile table per corpus.
- **`cargotest/v1`** (SPEC §9 Cargo row): exact suite-aggregated census,
  one line per failing test with coordinates, first panic location+message
  inlined; detection anchored on the libtest `test result:` shape so
  compile-error runs fall through to lint/build. Measured: 150-test crate
  with 6 failures went from "names one failure" (text/v1, 117 tok) to the
  full failing census (203 tok).
- **`table/v1`** (SPEC §9 tabular row): shape-detected caps-header aligned
  tables (docker/podman ps, kubectl/oc get, MCP-delivered tables); exact
  row×column count, low-cardinality column value censuses, minority rows
  cited verbatim with coordinates. Measured: 180-pod `kubectl get pods`
  under text/v1 hid 13 of 14 broken pods in the omitted middle; table/v1
  names the exact state distribution at equal budget — tabular needle-drop
  100% → 0%.
- **Killed by measurement** (reasons in the eval): mvn/gradle profile
  (logtemplate/v1 already surfaces every failure via rarity), AWS parsers
  (json/v1 shape census, 150.9×), pip/gh listing profiles (slim inline
  correct at ~1.0×), ps aux (no census worth its tokens).
- **straitjacket-bench charter** (evals/BENCHMARK.md): the paired-corpus
  benchmark design adopted from external review — retrieval quality
  (SWE-Explore, pending dataset verification), downstream correctness
  (SWE-bench Verified subset), hostile-output stress (Terminal-Bench
  slice), and SJ-EvidenceBench invariant adversaries; metrics (evidence
  density, retrieval regret, evidence preservation as the load-bearing
  gate), pathology-stratified sampling, and four evaluation tiers mapped
  to existing infrastructure. Inventory verified: 8 of 10 EvidenceBench
  scenarios already existed as tests; the two gaps shipped
  (tests/test_evidencebench.py, `sj_canary` marker): machine-format
  negotiation baselines (JSON/JSONL/SARIF claimed structurally, JUnit XML
  bounded+deterministic fallthrough) and stdout/stderr descriptor-graph
  classification — whose first probe caught and fixed a real defect:
  `cmd 2>&1 > file` was classified proven-small although POSIX sends
  stderr to the console (hook `_REDIR_ALL_RE` now order-aware).
- **`ctx replay`** (ROADMAP M-F, session-history learning loop): replay
  recorded Claude Code transcripts through the real steering + digest
  code, open-loop and workspace-free — interception verdicts, wire
  residency recorded-vs-simulated, evidence sufficiency (downstream-used
  facts scored inline vs one-hop), and `--gaps` (the empirical coverage
  priority list mined from real sessions). Read-only by construction;
  read results counted under the read path, never shape-digested.
  Measured: the naive dev session replays at 46% residency saved; spec3
  harnessed archives replay at zero delta with 71/71 and 21/21
  downstream-used facts inline (figures regenerated after review fixes:
  already-harnessed digests are fact-scored — they ARE the regression
  surface — and read-path results are excluded). Pathway mining receipts:
  evals/pathways-spec3-2026-07-19.md (70% of commands are pytest; 15
  starvations, zero retrieval-verb adoption — command-channel
  continuations filed as the fix).

## [0.22.0] - 2026-07-19

The Evidence Delivery Controller wave (docs/EDC.md, all 24 sections
specified and adversarially reviewed before build — seven defects died on
paper). Built by seven parallel engineers in two increments; 612 tests.

- **Evidence core**: typed EvidenceGraph/Item/Ref with volatile quarantine
  and coverage attestation (ctx.evidence); TOML Evidence Contracts with
  loss severities and floor<=ceiling load validation (ctx.contracts);
  selection-seam validation — coverage computed over typed facts, never
  re-parsed text.
- **pytest/v2 extract/render split** (the layering law made real):
  extraction emits attested graphs (failure class + one-line summary per
  census row now DEFAULT — hierarchy levels 3-4); rendering through
  contracts: FAIL_CENSUS, DENSE (grouped under extracted keys only),
  FLOOD (histograms + first-N census + complete census minted as a
  derived blob: artifact); degradation cascade never truncates identities
  outside declared FLOOD; pass path byte-identical pytest/v1.
- **Delivery Policy Resolver** (ctx.resolver): the single choke point
  replacing seven hand-rolled budget sites; DeliveryPlan with plan_id and
  closed reason vocabulary; floor applied after multipliers; reader
  capability with latching and confidence floor; plan receipts to
  telemetry. Safety invariant test: guard decisions byte-identical under
  every adaptive state.
- **Controller state, shadow-first** (EDC 5-7+6b): source generations
  with untracked-content hashing (ledger-dir excluded, capped,
  deterministic); per-family signature tables closing the scope-flag
  defect; narrowing relation + positives; v2 intervention/outcome ledger
  with deterministic ids, hypothesis windows, censored expiry; shadow
  circuit machine (episode semantics + hysteresis); graduated-steering
  shadow ledger. Replay gates vs archived transcripts ALL PASS: the r1
  8x slicer loop collapses to one episode/one transition; edit cadence
  scores as verification; 7 narrowing positives.
- **Seeded referee + scorecard v2**: spec3 --repeats/--gates with median
  aggregation and frozen-constants checksum; per-family behavioral
  blocks, coverage tables, episode narratives, formula-labeled
  counterfactuals; censored events excluded from denominators.
- **Perf with receipts** (rejected optimizations documented): resolve_id
  ~500x (index-seekable range scan), line-index repeat access ~3600x
  (in-process cache; the mmap "win" was this confound), gc 3.7x
  (batched); retrieval.py modularized into ctx/_retrieval behind a
  byte-compatible facade; MCP schema drift fixed (call-graph ops
  declared, diff wired) + bounded workspace cache.

## [0.21.0] - 2026-07-18

The reflex wave: closed-loop conditionality (docs/REFLEX.md), built
against the spec3 receipt where every conditional fired to spec on the
flood axis while the failure lived on the uninstrumented information
axis. Every intervention is now a hypothesis about the model's next
action; the system scores the hypothesis per event and adapts on the axis
the evidence names.

- **`pytest/v1` failing-test census** (debt 74db82e027): one line per
  failing test — node id, output coordinates, traceback span — rendered
  above and outliving the inline first-failure detail under budget
  pressure; overflow declared with a continuation span. Dense mode adds
  one evidence line per test. Bare `-q` summaries, `--tb=line/no`, and
  pipe-truncated output all parse (the spec3 "summary line not found"
  breakage fixed); all-pass runs byte-identical to before.
- **Reflex arc v1** (`ctx.reflex`, hook + cli wiring): slicer-normalized
  command signatures (`pytest -v`, `… | head -100`, `… --tb=short | tail`
  → one signature); starvation detector — a signature re-issued after its
  digest-with-omissions appends an outcome event and latches densify for
  the session; landing detector on `ctx get`/`search` of known handles.
  Reflexes act through rendering only (`densified: re-run detected` header
  on the printed digest; dense flag never in digest meta — content
  identity stays a pure function of bytes). All state fail-open,
  replay-deterministic from the command sequence. Outcome ledger:
  `.ctx-session-reads/reflex-outcomes.jsonl` (frozen schema).
- **Behavioral-anomalies scorecard**: `ctx stats --session` renders
  starvation/landing/densify counts per signature when present — the
  single-session instrument that would have caught spec3 without a
  benchmark. Summary line flags `⚠ N starvation/M landings`.
- **Slow-loop epoch schema**: `ctx policy compile` aggregates reflex
  outcomes into `[digest_density]` — signatures with ≥2 starvations and
  landings < starvations start dense in future epochs; address-following
  readers keep lean digests. Additive to ctx.policy/v1 (hook parser
  verified tolerant); consumption deliberately deferred.

## [0.20.0] - 2026-07-18

The measurement-driven wave: three mechanisms built in parallel by
independent engineers against the receipts of the eval-collapse
measurements (evals/eval-collapse-2026-07-18.md) and the conditionality
audit (docs/LADDERS.md), then assembled with the audit's consistency fixes.

- **Head/tail evidence windows** (`digest/text.py`): large text/v1 digests
  now show the first `digest_head_lines` AND last `digest_tail_lines`
  lines (both configurable via `[budgets]` in ctx.toml, default 5/5), each
  with real coordinates; the omitted middle carries a deterministic region
  span plus a `ctx get --lines` continuation. Motivated by a measured
  failure: CLIs put conclusions at the END of output, and the S-C flood
  scenario's own SUMMARY line was being omitted. Budget fitting shrinks
  tail first, then head; small-output and error-signal paths byte-identical
  to before.
- **Long-runner backgrounding** (`jobs.py`, `run --bg`/`--bg-after T`,
  `job`, `jobs`): every `--bg*` run starts under a detached supervisor
  spooling to the store; finish within T → the normal digest, byte-for-byte
  identical to a foreground run including the same `run:` id. Outlive T →
  the transcript gets `job:<id>` immediately; `ctx job <id>` shows a
  bounded live tail (never a flood), `--wait` blocks then digests,
  `--kill` finalizes what spooled. Finalized jobs are ordinary `run:`
  artifacts — search/get address them identically; job ids, pids, and
  timestamps never enter content identity. Six launch/kill/finalize races
  identified and closed (single-writer meta, idempotent finalization,
  orphan adoption).
- **Adoption steering** (hook + skill, shipped mid-wave as its own commit):
  eval-opportunity detection (python heredoc/-c) appends the collapse
  teaching to remediations at every friction point and ledgers each
  opportunity fail-open (`.ctx-session-reads/eval-adoption.jsonl`) — the
  adoption ratio's denominator. Doctrine scoping fix: terseness governs
  scripts and narration, never the final deliverable.
- **Conditionality audit applied** (docs/LADDERS.md): seq emissions now
  respect the engagement filter like run/eval (edge 1); timeouts and
  signal deaths get the failure budget in `run` (edge 4, parity with
  eval); seq marks signal-death steps as failures (S6 finding). Remaining
  audit items (pressure-aware budgets via a single resolve_budget choke
  point, hint follow-through telemetry, MCP schema drift) are the next
  wave's candidates, ranked in the doc.
- Skill: verb index + rule 15 (never idle on a long runner) + long-runners
  reference section. Prefix manifest regenerated; PREFIX_VERSION unchanged
  (invocation-tier assets only — no cache impact).

## [0.19.0] - 2026-07-18

Programmable capture: the Maki absorption. Maki (maki.sh) demonstrated the
strongest form of tool-chain collapse — the model writes one script that
chains N operations, and intermediates never enter the transcript (their
demo: 1300× context reduction). `ctx seq` already performed this collapse
for *declared* trees; this wave generalizes it to *computed* control flow
(branch on a result, loop over files, aggregate before emitting) while
keeping what a raw interpreter sandbox drops: provenance. Maki's script and
its intermediates vanish into the chat log with no address; here every
piece keeps one.

- **`ctx eval`** (`ctx.pyeval`): a Python script runs under birth-gate
  capture and only its bounded digest returns. The script is stored first
  as a content-addressed blob, cited in the digest header
  (`script blob:<id>`) and in the final manifest (`eval.script`) —
  reproduce with `ctx get blob:<id> | python3 -I -`. Streams are the usual
  span-addressable blobs; the existing profile registry digests the output
  (flood → bounded digest with continuation coordinates; small result →
  complete inline). Failure asymmetry applies: a failing script's
  traceback rides on the failure budget, and frames are deterministic and
  path-free (`File "<stdin>"` — the script feeds stdin, never a temp
  file). `python -I` isolated mode blocks cwd/PYTHONPATH injection.
  Sub-steps that deserve their own handles call `ctx run` from inside the
  script. Trust envelope identical to `ctx run` (bounded capture, not OS
  isolation — that remains the broker's job, Phase 3). Deterministic:
  identical script + identical worktree → byte-identical digest.
- **Capture runner**: `run_capture` gains `stdin_bytes` (spooled to disk
  and fed as the child's stdin — never a pipe, so no deadlock and no size
  limit) and `record_argv` (normalized model-visible argv, so the
  host-specific interpreter path never appears in manifests or digests).
- **Telemetry attribution**: `render_run_digest` takes an `op` name so
  `ctx gain` reports eval under its own by-verb row; `op` never
  participates in digest bytes or content identity.
- Skill body rule 14 + verb index teach the seq/eval split (declared →
  `seq`, computed → `eval`); prefix manifest regenerated — the skill body
  is invocation-tier, so PREFIX_VERSION stays 3 and there is **no cache
  impact**.
- **Eval set + first measurements** (`evals/evalset_collapse.py`,
  `evals/ab_eval_live.py`, results in `evals/eval-collapse-2026-07-18.md`,
  smoke-guarded by `tests/test_evalset_collapse.py`): mechanical arms on
  real fixtures (fan-out aggregate 146 tok vs 96k naive with the
  best-play baseline provably unable to finish; bash-pipeline control
  showing `run --shell` already covers stream-shaped chains; flood/needle
  provenance net; 299-tok wrong-script recovery vs 192k re-pay) plus a
  live mechanism-isolated A/B (haiku, n=2) and a wrapped condition. Live
  findings recorded honestly: the one-script discipline wins (−15–63%
  cost, fewer turns, −79% cache churn at best) but the verb itself went
  unadopted (0/3 sessions) and the terse doctrine leaked into final
  deliverables — both filed in the debt ledger with coordinates.

## [0.18.0] - 2026-07-18

The universal emission gate: one output-side gate for every faucet. Prior
waves plugged faucets one tool at a time (Bash wrapped, Read/Grep/Glob
input-bounded) — a per-tool if-ladder that never terminates. This wave
replaces it with a single PostToolUse gate that dispatches on output *shape*,
not tool name: a new tool needs no new code. Motivated by measurement — a
routine `mcp__github__list_commits(perPage=100)` returns ~79 KB / ~19.8k
tokens and is re-sent every turn; its `json/v1` digest is ~0.4–1.4 KB
(≈57–190×), and the full payload stays retrievable.

- **Universal PostToolUse gate** (`ctx.hook._emission_gate`, claude-code):
  any tool result over `budgets.max_tool_output_bytes` (default 16384) is
  replaced — via the documented `hookSpecificOutput.updatedToolOutput` — with
  a bounded deterministic digest carrying a working `ctx get run:<short>`
  ref. Under budget → byte-identical no-op. The raw bytes are persisted
  losslessly first (lossy-in-window, lossless-on-disk); nothing the model
  needed is ever destroyed, only relocated to an addressable artifact.
- **Shape-dispatched, name-agnostic**: the gate synthesizes `argv=[tool_name]`
  and reuses the existing digest registry (`digest.digest_output`), so MCP
  JSON lands on `json/v1`, grep-shaped output on `search/v1`, prose on
  `text/v1` — no per-tool branches. Idempotent (never re-digests its own
  output or `ctx`'s), fail-open (any error → pass-through), deterministic
  (content-addressed id is a pure function of bytes + tool name).
- **`json/v1` head-N record inlining**: a shape line alone forced a re-fetch;
  the digest now inlines the first records' scalar fields + a json-pointer
  span to the rest (mirrors `search/v1`'s top-matches+span). Byte-stable.
- **`search/v1`** now recognizes a synthesized `argv=[tool_name]` (native
  `Grep`, mcp `*search_code` / `*grep*`) so those faucets reach it through
  the gate; narrow suffix/exact match preserves the log-line theft guard.
- **Matchers broadened** to every emitting faucet — Claude Code PostToolUse
  `Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*` (Edit/Write/Todo
  excluded as tiny), Antigravity nudge-path likewise. Antigravity stays
  nudge-only (output-replacement contract unverified upstream). Matcher
  strings are host settings, not prefix assets → no `PREFIX_VERSION` bump.
- Removed the now-unwired `_post_hook_exe` native-shim selector: the gate
  needs the Store/digest layer, so PostToolUse runs in Python. A shim that
  measures bytes and re-execs only over budget is a possible follow-up.

## [0.17.0] - 2026-07-18

The native-search wave: close the model-ignoring gap. Measurement showed
the model navigates with the *native* `Grep`/`Glob` tools — not shell
`grep` — so our `Bash|Read` matcher never saw the flood, and the
navigation governor never fired. This wave intercepts the tools the model
actually reaches for.

- Matcher extended to `Bash|Read|Grep|Glob` (Claude Code) and
  `…|grep_search|glob_search|codebase_search` (Antigravity). The tools the
  model uses to navigate are now in scope, not just shell commands.
- Native content-mode `Grep` with no `head_limit` gets one injected
  transparently via `updatedInput` (`head_limit: 60`) — the tool still
  runs, the model adopts nothing, and an unbounded flood becomes a bounded
  slice with a pointer to the structured digest. `files_with_matches` /
  `count` / already-bounded greps pass through raw. Under strict
  `steering = "deny"` the same case is redirected to `ctx run -- grep`
  instead (never silently rewritten).
- `search/v1` digest profile: a wrapped `grep`/`rg` (via `ctx run`) is now
  rendered as *search results* — exact match count, per-file histogram,
  top hits with coordinates, and a span to the full set — instead of the
  generic text profile's byte counts. Sibling of `lint/v1`; the two share
  the `file:line:content` shape, so `search/v1` is argv-anchored to actual
  `grep`/`rg`/`ack`/`ag` invocations (a content-ratio trigger was tried and
  dropped — it stole log and lint lines) and ordered *after* `lint/v1` so
  diagnostics claim their own output first.
- No prefix asset changed (matcher strings are host-settings, not
  resident prompts), so no `PREFIX_VERSION` bump: zero cold-cache cost.

## [0.16.0] - 2026-07-18

The call-graph wave: edges, done in-doctrine. We had nodes (`def`/`refs`);
this adds the edges that turn a recursive grep-and-read trace into one
query — the one capability that makes tokensave enviable, built the
straitjacket way (pure stdlib `ast`, zero new deps, deterministic,
worktree-hash cached, no daemon, span-backed, addressable).

- `ctx callers <Symbol>` — direct callers, each with file:line.
- `ctx callees <Symbol>` — in-repo functions it calls.
- `ctx impact <Symbol> [--depth N]` — transitive callers (blast radius),
  grouped by hop distance; bounded recursion (≤6). "What breaks if I change
  this?" in one call. On our own repo, `ctx impact register_span` returns
  the full 179-node reachable set in ~0.8s (cached thereafter).
- Name-resolved edges (a call to `foo` binds to any in-repo `def foo`):
  approximate but disclosed like the ctags map engine; ambiguous names
  report every candidate, never hidden (SPEC §8). Python-only for now;
  tree-sitter breadth deferred to an optional `[polyglot]` extra pending a
  measured win.
- Ships CLI-first + skill-taught (bump-free). The MCP `op` enum is a prefix
  asset, so exposing the verbs there is a deliberate future PREFIX_VERSION
  decision, not paid on spec (same discipline as the v0.9.0 priced outline).

## [0.15.0] - 2026-07-18

The cross-validation wave: two dual-use benchmark cells (S5 library-hunt,
S6 bug-bash) whose output is repo work — held-out by construction, novel
regimes, findings adversarially re-verified by hand before harvest
(evals/cross-validation-2026-07-18.md).

- **6 real defects found and fixed** (of 15 S6 claims; verification
  refuted 1 and deferred 8 to `ctx debt`). All regression-tested in
  tests/test_bugbash_s6.py:
  - compound-command bypass: `allow_commands=["echo"]` let
    `echo hi && rm -rf x` through — prefix allows now gated on `not
    has_meta`.
  - `tail -n +N` / `head -n -N` (whole-file reads) were classified bounded
    — sign-prefixed counts now route to the unbounded path.
  - mid-path directory-symlink escape survived `confine` when the full
    path already existed — now checks each symlink's immediate (one-hop,
    lexical) target.
  - `window.json` was clobbered to `window_pct:0` by any usage-less
    response, silently disengaging the window-pressure throttle — the
    write is now skipped when a response carries no usage.
  - `create_checkpoint` crashed (`IndexError`) on a blank evidence line.
  - a string `patterns` typo in ctx.toml silently disabled ALL secret
    redaction (chars iterated as patterns) — now isinstance-guarded to
    the full default set. (Two of these — redaction, window throttle —
    are security/safety bugs that survived 14 versions + a hand audit.)
- **Library adoptions** from the doctrine-faithful S5 audit: `_mask_token`'s
  hand-rolled bounded dict → `functools.lru_cache`; the containment check →
  `Path.is_relative_to`. Three larger candidates deferred to `ctx debt`.
- **Metrology fix**: cache-read invalidations are judged within
  reconstructed transcript threads, not a single global max — parallel
  tool-call models no longer produce false invalidations (declared
  metrology debt resolved).
- **Emission governor validated in the wild**: a 208k-output bug-hunt
  session crossed all 10 pressure tiers, one nudge each, correct dedup —
  the first real-load exercise of the mechanism.

## [0.14.0] - 2026-07-18

The cleanup wave: audit with receipts, debt paid down, and Rust exactly
where measurement says it makes sense.

- **Audit results:** lint debt was 4 findings (fixed, ruff clean); type
  debt 43 mypy findings → 24 (real fixes in proxy/hook/codeverbs; the
  runtime-safe residual is declared in `ctx debt` with coordinates).
  Hand-rolled-vs-library review: the stdlib-first doctrine holds — every
  remaining hand-rolled piece is deliberate, documented, and has an
  opportunistic accelerator path (rg, ctags, orjson, jedi, grimp).
- **Real bug found by the audit:** the no-`--settings` fallback path
  merged only PreToolUse hooks, silently dropping the emission governor —
  fixed to merge every stage.
- **Rust where it makes sense (`native/ctx-hook-native`):** CPython's
  startup floor is a measured ~29 ms and PostToolUse fires on every
  Bash/Read/Edit/Write (~80 spawns/session ≈ 2.7 s). The Rust shim does
  identical work in ~3 ms (12×), is selected opportunistically
  (CTX_NATIVE_HOOK / PATH), and is parity-tested byte-for-byte against
  the canonical Python — including shared flock'd tier state and both
  host dialects. A full Rust rewrite remains declined by measurement:
  hook time is ~1% of session wall-clock.
- Price tables deduplicated (matrix_report now imports ctx.scorecard's).
- **README overhauled:** quickstart, the four-gate model, current verb
  table (seq/gain/debt/outlines), the five-system stack comparison with
  receipts, and the regime scoreboard.

## [0.13.0] - 2026-07-18

The Tura wave: round economy. Wire replay over five real sessions showed
32% of tool-bearing rounds were mechanical bash-after-bash chains (70% on
lint-fix, 65% on creation) — each ~1.5-2s ttfb plus a suffix cache write.

- `ctx seq`: declared command trees — N steps, one round, `&&` semantics,
  every step a full birth-gate capture addressable as `run:<id>`; failing
  step's digest rides in full, green trees stay terse. The runtime-owner
  advantage (Tura's macro execution) taken at harness level, losslessly.
- Scorecard: `rounds` is now the headline metric; `rescue_recovery`
  (first rescued round, rounds after, blocks elided) adopted from Tura's
  best measurement.
- **Backward planning adopted into the discipline prompt after a held-out
  A/B win on every axis (haiku, fresh task): -17% cost, -16% turns, -14%
  time, -18% output, 9 tests vs 7. PREFIX_VERSION 3 — one cold cache
  write per model, disclosed.** Skill rule 14 teaches the same plus seq.
- Benchmark manifest (`evals/bench-manifest.json` + test): task
  definitions frozen behind hashes; held-out rule recorded — a mechanism
  tuned against a task records its win only on an unseen variant.
- Declared in `ctx debt`: state-projection context (needs a runtime
  channel hosts do not expose) and an invalidation-metric investigation
  (first nonzero readings look like parallel-request interleaving, not
  real prefix regressions).

## [0.12.0] - 2026-07-18

The open-threads wave: both remaining designed-but-unbuilt items, each
gated by an isolated live experiment (both on haiku, one variable per test).

- **Solution ladder adopted into the wrap discipline prompt** after a
  measured A/B win on a creation task: -28% turns, -33% time, -17% cost,
  -28% output tokens, 9% less product code with MORE test code (effort
  floor held), quality green. **This is a prefix-resident change:
  PREFIX_VERSION 2 — every user pays one cold cache write per model on
  first post-upgrade session.** The same ladder is skill rule 13, paired
  with debt declaration.
- **Emission governor validated live** for the first time: fired exactly
  once at the 20k tier on a verbose doc-gen session, correct dedup, zero
  quality damage (non-inferior on every axis; efficiency effect size needs
  longer sessions and stays under scorecard watch).
- **`ctx debt`**: declared-omission ledger for engineering decisions
  (append-only committed JSONL, content-derived idempotent ids,
  add/list/resolve) — SPEC §8's no-silent-omission rule applied to scope.
- **Deliverable-level scorecard metrics**: LOC delta, files touched, and
  untracked-file line counts from git, in the summary line and history —
  over-engineering and effort-thinning are now measured regressions.
- **Skill shipped for real progressive disclosure**: body now advertises
  its reference tier (`references/verbs.md` — full verb/flag detail — plus
  routing-policy and repository-addressing pointers), carries a compact
  verb index covering everything since v0.2, and the prefix manifest
  splits the skill into frontmatter (prefix-resident, cache-relevant) vs
  body (invocation-loaded, tracked but bump-free) so future body
  improvements are not mispriced as cache invalidations.

## [0.11.0] - 2026-07-18

The rtk-inspired wave, hypotheses revised by real-corpus measurement before
building (evals/rtk-corpus-2026-07-18.md — two reversals: diagnostics
needed structure not compression; small outputs were being inflated by our
own scaffold).

- lint/v1 digest profile: eslint/ruff(rustc-style)/tsc/cargo/go/mypy
  diagnostics rendered as exact censuses (by severity, rule, file) with a
  span-backed first-diagnostic region — decision-grade structure at ~2x
  the blind text digest's budget.
- Scaffold-slim inline emission: small complete outputs emit command +
  exit + unindented content (~20 token overhead, was 100-400; pip digests
  were literally 2x the size of the output they contained).
- Failure-asymmetric budgets: `[budgets] failure_budget_factor` (default
  2.0) — failing runs get twice the emission budget; success is
  boilerplate, failure is evidence.
- `ctx gain`: cumulative containment savings by verb from telemetry, with
  token and dollar framing.

## [0.10.0] - 2026-07-18

Lossless mid-session rescue (docs/LOSSLESS-RESCUE.md) — the rewriting
proxy's last structural edge, taken without its costs. Opt-in Tier-1:
`ctx wrap claude --rescue-pct 70` (or `ctx proxy --rescue-pct`); the
default proxy remains the byte-exact Tier-0 observer.

- Epoch-latched elision: at a window-pressure crossing, ONE deterministic
  set freezes (tool_results older than the 6 most recent, >1 KiB); every
  subsequent request rewrites to a byte-identical prefix, so the cache is
  re-bought once at the smaller size and stays stable. Simulated with
  measured prices and real S4 wire shapes: ~18× less cache overhead than
  per-request rewriting, 18 turns of lossless runway per 27k elided.
- Nothing destroyed: elided bytes persist verbatim to
  `<state>/elided/<sha256>.txt` before the stub exists; stubs carry hash,
  size, and retrieval path; `rescued: N` disclosed on every wire record;
  startup banner marks the mode non-byte-exact. Fail-open on any parse
  problem. Property-tested: determinism, grown-transcript prefix
  stability, epoch latching across restarts.

## [0.9.0] - 2026-07-18

The priced-context wave (thesis: docs/PRICED-CONTEXT.md — metadata as
economic signposting; every mechanism survived a measured cheap test, and
the rejects are recorded there too).

- **Price tags in guard steering (M1).** Oversized-read deny/rewrite
  reasons now carry the cost in the agent's native currency —
  "~30k tok ≈ 15% of window" — computed from the stat the hook already
  performs and the proxy's window ground truth when present (measured
  cost: 0.003 ms). Coarse buckets by design: precision only needs to
  cross decision thresholds (`textutil.fmt_tokens_coarse`).
- **Priced symbol outlines (M2).** `ctx stats repo:<file>.py` returns the
  menu instead of an aggregate: every top-level symbol and method with
  line range, ~token price, and a resolvable span handle (snapshot-backed,
  deterministic). Measured 12.8–54.5× cheaper than the file it describes
  across src/ctx. The guard's oversized-read remediation names this verb —
  degrading a read is now structured-lossy, not truncated-lossy.
- **Priced map survivors (M3).** `ctx map` entries carry "~⟨tok⟩ tok · ⟨n⟩d"
  for ranked survivors only (flat inventories were measured and rejected:
  5× waste that scales with repo size). Map cache format bumped to
  ctx.map/v3.
- Deliberately NOT changed: the MCP tool description — advertising the new
  verb there would cold-invalidate every user's prompt cache; the prefix
  manifest holds at version 1 and the outline is discoverable through
  mid-stream steering instead. (The prefix-stability contract shaping its
  first real decision.)
- Benchmark harness: fixture agents can no longer hijack the host's
  editable install (`PIP_REQUIRE_VIRTUALENV=1` in matrix runner env — an
  S4 overhaul agent actually did this).

## [0.8.0] - 2026-07-18

The measurement-loop wave: six mechanisms that convert benchmark
postmortems into runtime feedback, each grounded in a measured failure
from evals/matrix-2026-07-18.md.

- **Prefix-stability contract (A).** Every injected prefix byte (wrap
  discipline prompt, explorer agent, MCP tool description, skill) is
  locked behind `src/ctx/prefix-manifest.json` + `PREFIX_VERSION`; the
  golden-hash test fails on unacknowledged change, because a 9-token edit
  measurably cost one full cold cache rewrite per model (~56k tokens).
- **Session scorecard (D) + effort mix (F).** `wire.jsonl` now records the
  request model and a tool_use census (names only); `ctx.scorecard`
  computes token classes, cold-prefix vs true invalidations vs suffix
  growth, ttfb/generation split, per-model usage, and edit-share.
  `ctx wrap` prints a one-line scorecard at session end and appends
  history to `.ctx-session-reads/scorecards.jsonl`; `ctx stats --session`
  renders the full card.
- **Graduated engagement (C).** Sessions start passive under
  `[engagement] mode = "auto"`: digests carry no "next:" affordances until
  a measured signal graduates the session (hook call count, window
  pressure, or a digest that actually truncated). Lean models (haiku by
  default) keep a single suggestion even when active — measured: haiku
  over-executes affordances as work items. Filtering happens at the
  emission boundary only; stored digests remain byte-identical pure
  functions (SPEC §8).
- **Emission governor (B).** New `ctx hook <host> post-tool-use` stage —
  the symmetric partner of the read-budget governor. When proxy-measured
  cumulative output crosses a 20k-token tier AND the per-request average
  is verbose, it injects one terse-narration nudge (Claude Code
  `additionalContext`; Antigravity decision dialect), exactly once per
  tier. Registered by `ctx wrap` and the plugin hooks template.
- **Anticipatory inlining (E).** The pytest digest inlines the first
  failure region (budget-gated, separator-bounded, deterministic) so the
  most common follow-up costs zero retrieval hops — each avoided hop is
  ~2s of ttfb plus a suffix cache write.

No injected prefix text changed in this release: the prefix manifest holds
at version 1, so v0.8.0 causes no cache cold-write.

## [0.7.1] - 2026-07-18

Benchmark-diagnosis fixes, all three grounded in measured evidence rather
than suspicion. The proxy now passes `Accept-Encoding` through untouched and
decompresses only the observer's private copy (`_Decoder`, zlib
auto-detect; unknown encodings fail open to no-usage) — the earlier
forced-identity workaround is gone. The proxy keeps a small pool of warm
upstream connections (TLS handshake amortization for remote upstreams;
stale pooled connections retry once on a fresh socket) and stamps every
`wire.jsonl` record with `ms: {connect, ttfb, total}` and `reused_conn`, so
per-exchange latency attribution is now ground truth instead of guesswork.
`ctx wrap claude` injects an emission-discipline system prompt in print
mode (the v0.7 rematch showed the entire wall-clock gap was output-token
volume: 69k vs 42k tokens ≈ the whole duration delta at ~80 tok/s); opt out
with `CTX_WRAP_NO_DISCIPLINE=1` or by supplying your own
`--append-system-prompt`. The profiled digest hot path
(`logprof._mask_token`: 180k per-character digit scans over 20k lines) now
uses a compiled digit regex plus a bounded token-mask memo — same masks,
~6× faster (0.82s → 0.14s on the 20k-line profile fixture).

## [0.7.0] - 2026-07-18

Tier-0 wire observer shipped: `ctx proxy` is a localhost-only pass-through
proxy for Anthropic-API traffic (byte-exact relay, SSE unbuffered) with a
fail-open observation tap writing `window.json` (provider-reported
input/cache/output usage, window fullness) and `wire.jsonl` (per-exchange
block census and tool_result sizes — no bodies, no auth headers);
`ctx wrap claude --proxy` supervises it per-session and injects
`ANTHROPIC_BASE_URL` into the child env only, failing open to an unproxied
session. Validated against the live production API. In progress: adaptive
guard, symbol-addressed code verbs (`ctx def`/`refs`/`diag`, jedi-backed
`[code]` extra with ast fallback), and learned policy epochs
(telemetry → committed policy).

## [0.6.0] - 2026-07-17

The roadmap's mechanism wave (M-A, M-C, M-D, Gate 3): the `ctx-explorer`
quarantine agent gives sub-agent exploration provenance — evidence via ctx
verbs, cite-don't-quote, mandatory checkpoint-shaped reports where a claim
without a handle is labeled a hypothesis; the cumulative session read-budget
governor closes the death-by-a-thousand-small-reads hole (graduated pressure
past `session_read_budget_bytes`, byte-identical behavior below it);
`ctx map` produces a deterministic budget-fitted codebase map
(reference-graph ranking, evidence weighting from recent captured runs,
worktree-hash caching, `engine grimp+networkx` when the optional `[map]`
extra imports, builtin otherwise); `ctx diff run:A run:B` emits run-to-run
regression digests (exit/stream/failure-set/template deltas with minted
spans). Library swaps landed with the fallback doctrine intact: flock'd
read ledger, opportunistic orjson (`[fast]` extra), drain3 evaluated and
declined. Measured (evals/overhaul-3arm-2026-07-17.md, v0.6 rematch): on
the full repo-overhaul benchmark the harnessed arm was **40% cheaper than
naive ($2.21 vs $3.70) and faster (6.1 vs 7.2 min) at quality parity**,
reversing round 1's cost sign — the ungoverned-fork externality is gone.
168 tests.

## [0.5.0] - 2026-07-17

Deterministic zoom spans (SPEC 6.4): digests attach content-derived span
tokens (`sha256(blob|kind|params)[:10]`) exactly at every omission point;
resolving a span is structurally bounded — small regions return exact
lines, large regions return a zoom sub-digest minting further sub-spans —
so retrieval can never re-flood the transcript. PR #1 review hardening:
`ws:<alias>` routing via committed `[aliases]`, lease-aware gc with
time-bounded retention, single-file `repo:<file>` selectors. The honest
measurement pass recorded the N=5 matched-warm-cache A/B (**cost parity
within noise, ~13% overhead, 5/5 correct both arms, zero denials**) and the
Headroom 0.32.0 needle-drop head-to-head: on a quiet structural needle
(no error keywords in 20,001 lines) **Headroom silently dropped it
(347,595 → 68 tokens, no trace); logtemplate/v1 preserved it verbatim with
its coordinate** — 100% vs 0% needle-drop rate. Roadmap and the four-gate
unified architecture were written down; the three-arm overhaul benchmark
(naive vs straitjacket vs Headroom) recorded no quality degradation from
context mediation in any arm. 131 tests.

## [0.4.0] - 2026-07-17

The transparent-steering wave: complete substitution steering rewrites
flooding commands instead of denying them, in both host dialects (Claude
Code `updatedInput`, Antigravity allow+updatedInput) — zero denial
round-trips, `steering = "deny"` reproduces the v0.3 contract
byte-identically. Claude Code support landed end-to-end: the PreToolUse
hook adapter (`ctx hook claude-code pre-tool-use`) and `ctx wrap` for
one-command harnessed sessions (ephemeral `--settings` injection, zero
residue). `logtemplate/v1` added deterministic Drain-style log template
mining (5,000-line log → 0.27% of raw bytes with the single ERROR needle
preserved at its exact coordinate), and the zero-hop inline threshold
widened to the result budget. Measured (evals/ab-claude-code-2026-07-17.md):
the full evidence workflow showed **456 model-visible tokens vs ~222,000
raw — a 487× reduction on first exposure**; the v0.4 rematch beat naive
6 turns/$0.072 vs 9/$0.186 on matched warm caches (later corrected by the
N=5 batch to cost parity within variance). 114+ tests.

## [0.3.0] - 2026-07-17

Library-grade engines, tiered so the hook hot path stays stdlib-only:
`repo:` searches use ripgrep (`rg --json`) when installed — SIMD prefilter,
parallel walk, deterministic ordering enforced, ~3x on the work portion —
with a transparent builtin fallback (`CTX_SEARCH_ENGINE=python` forces it);
`.ctxignore` matching moved to pathspec for true gitignore semantics;
secret redaction expanded from 3 to 16 vendored gitleaks-grade patterns;
manifests validated against the vendored invocation-v1 JSON Schema in tests
and `ctx doctor`; doctor discloses the active search engine and ignore
matcher. 78 tests.

## [0.2.0] - 2026-07-17

Performance and capability wave: search core rewritten to whole-text
matching (**13x on sparse patterns over 500k lines**, end-to-end CLI
271→149 ms), on-disk line-offset indexes so `ctx get --lines` touches only
the requested byte range, zero-subprocess git identity, MCP connection
caching. New capability: zero-hop digests (small complete outputs inline
verbatim), four new deterministic profiles (gotest/v1, jest/v1, build/v1,
gitdiff/v1), hook v2 (wrapper unwrapping, `bash -c` classification,
redirection allowance, repo-configured allow/deny prefixes),
`ctx checkpoint` (pinned content-addressed task epochs), `ctx get --symbol`
via stdlib ast, and the telemetry ledger (raw vs emitted bytes per op,
surfaced in doctor, never in digests). 70 tests.

## [0.1.0] - 2026-07-17

Initial implementation of the CTX harness specification (Phases 1–2):
pure-stdlib runtime with workspace resolution, a content-addressed artifact
store (SQLite WAL catalog outside the repo), birth-time capture runner
emitting `ctx.invocation/v1` manifests, and the four model-facing verbs
(run/search/get/stats) with deterministic ordering, token budgets,
continuation coordinates, and snapshot-on-read evidence. Deterministic
digest profiles (text, json, jsonl, pytest) with ANSI stripping and secret
redaction; the stdlib-only PreToolUse context guard (~40 ms, fail-open,
exactly one JSON decision); a bounded single-tool MCP stdio server; the
Antigravity plugin package with installer, `ctx init`, `ctx doctor`, and
lease-aware gc; vendored normative spec, acceptance suite, ADRs, and wire
schemas under `spec/`. 51 tests.
