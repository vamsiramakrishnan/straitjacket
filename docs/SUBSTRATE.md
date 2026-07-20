<sub><a href="README.md">« straitjacket / docs</a></sub>

# The substrate: operator classes under the semantic layers

**Date:** 2026-07-20 · review and adoption plan for an external proposal
("complete a small evidence algebra beneath ast-grep and Semgrep": fd ·
rg --json · ctags --json · jq/jaq · comby · watchexec). Verdict up front:
**the proposal's organizing principle is already this product's shipped
doctrine, three of its six "additions" already exist in the tree, one is
mis-specified against our determinism rules, and two survive as real
work.** This document is the audit with coordinates, the corrected
operator-class registry, and the phased plan (M-K) for what remains.

House rules apply throughout: receipts before doctrine; every phase names
its referee before implementation; status labels (**Shipped / Shadow /
Designed / Rejected**) are literal.

## 0 · Verdict table

| Proposal layer | Proposed as | Actual status in tree | Disposition |
|---|---|---|---|
| "Integrate operator classes, not binaries" | the deeper principle | **Shipped** — plan ops select physical engines by cost class (`plan_ops.py`); the "ripgrep pattern" (opportunistic binary, labeled fallback) is named house style (ROADMAP M-C) | already doctrine; adopt the *vocabulary* (operator-class registry, §3) |
| `rg --json` as typed producer | "highest-leverage next addition" | **Shipped** since v0.3 — `rg --json --no-config --sort path` parsed message-by-message (`_retrieval/rg_engine.py:50`), Python-engine fallback, cross-engine parity test | real gap is narrower: **column spans + per-result provenance** → M-K1 |
| `fd` file-set algebra / `repo.files` | new logical op | **Gap confirmed** — no file-set source exists; enumeration is `git ls-files`/`os.walk` inside `Workspace.list_files` (`workspace.py:150`), never a first-class stream | adopt as `corpus` source + `repo.files` plan op → M-K2; `--changed-within` **Rejected** (§2.4) |
| `jq`/`jaq` stream processor | "the missing evidence-stream processor" | **Deliberately absent** — `ctx q` is the total algebra built *instead of* jq (ALGEBRA M-H; `query.py:79-158`) | adopt as **physical engine + records source**, never vocabulary → M-K3 |
| `ctags --json` symbol census | new fallback role | **Shipped** — `--output-format=json` in the skeleton chain (`skeleton.py:361`) and repo map (`repomap.py:151`); chain is tree-sitter → ctags → stdlib ast | done; the census-before-detail read discipline it enables is also shipped (M-F). Remaining symbol gap is **SCIP** → M-K4 |
| `comby` rewrite rung | gap between regex and AST | **Absent**, but the surrounding contract is shipped *stronger than proposed*: ast-grep preview/apply is transactional, overlap-rejecting, generation-guarded (`astgrep.py:346-469`) | adopt as second rewrite rung, **gated on a measured decline corpus** → M-K5 |
| `watchexec` incremental layer | behind-the-runtime watcher | **Rejected for this era** — conflicts with the no-daemons non-goal (ALGEBRA §sequencing); a background watcher was built and removed twice for runaway resources (`callgraph.py:13`) | re-phased: broker-era (M-E) warming tenant → M-K6; correctness never depends on it |

## 1 · What the proposal gets right

Credit where due, because these points shape the phases:

1. **"Select files before scanning."** The one genuinely missing operator
   class. Every scan-class op today implicitly ranges over the whole
   corpus (bounded by `max_files=5000`, `targets.py:110`); there is no way
   for a plan or a `q` pipeline to *say* "the eligible set is these 17
   files" and hand that set to ast-grep/Semgrep. The proposal's coverage
   block (`files_considered` / `files_selected`) is exactly our coverage-
   receipt idiom applied to a new evidence kind. This is M-K2 and it is
   the highest-leverage item in the essay.
2. **The precision ladder** (exact literal → anchored regex → ast-grep →
   Semgrep/SCIP/facts) matches the shipped engine ladders and the skill's
   routing rules. Nothing to build; worth stating normatively (§3).
3. **"Promote recurring expressions into named logical operations."** This
   is the adoption-ledger pattern we already run for `eval` and `seq`
   opportunities (LADDERS §3.4). M-K3 extends the same instrument to
   record-transform shapes.
4. **"A textual approximation of a structural rewrite is a bug
   generator."** Already enforced — rewrites are binary-engine-only with
   no lossy fallback (`astgrep.py:8-11`); M-K5 keeps that invariant when
   adding a second rung.
5. **The restraint list.** No sd/choose/miller/xsv/gron until a measured
   workload demands an operator. Adopted verbatim as §5's standing rule.

## 2 · Corrections — where the proposal is wrong about this tree

### 2.1 It proposes our architecture back to us

"Do not integrate binaries, integrate operator classes … the harness can
compile intent to the physical pipeline" *is* M-J, shipped v0.25.0: typed
logical ops, static validation and pricing, cost-based physical engine
selection, capped fan-out (`plan_ir.py:110`, `plan_ops.py:490-590`). The
essay's `xargs` replacement ("fd -X / plan foreach with cap") describes
`PlanStep.foreach` + mandatory `cap` + `fanout_uncapped` rejection, which
exists (`plan_ir.py:416-435`). The value of the essay is therefore *not*
architectural; it is a gap list — and the gap list is one-third stale.

### 2.2 "Normalize rg --json" is mostly done; the live gap is spans

We have parsed `rg --json` since v0.3 and never parse human output. What
we do **not** capture is the submatch column data rg already emits: typed
results are `(target, line_no, pattern_index, line)` (`rg_engine.py:14`),
so a "site" is line-precise, not span-precise. Downstream effects: `q`
site rows carry no columns, minted spans for search hits are line-ranged,
and match-level dedup falls back to line text. M-K1 closes exactly this,
not the already-solved parsing problem. Similarly "never reparse human
output" is already invariant, and search hits are already snapshot-backed
(`search.py:137-145`) — the missing half is a per-result evidence blob
(parity with `q`'s final-stream minting, `query.py:829`).

### 2.3 jq as vocabulary would un-ship a shipped theorem

The essay wants jq programs as a primary operation and concedes in
passing that "arbitrary jq belongs in the CLI/execute tier … unless you
validate a total subset." That concession *is* the design: `ctx q` exists
because bounded verbs over typed streams are total by construction,
closure-audited (DIGEST-CLOSURE; `tests/test_digest_closure.py`), and
therefore MCP-tier-safe — three properties arbitrary jq (recursion,
`while`, generators) cannot have. A `records.query` op whose argument is
a raw jq expression, as the essay sketches, would put an unvalidated
Turing-adjacent language on the plan surface. Inverted, it is correct and
valuable: **the algebra stays the vocabulary; jq/jaq becomes a physical
engine** that our validated stage chains compile *to*, plus an untyped
escape hatch inside the existing `run`/`eval` trust envelope. That is
M-K3.

### 2.4 `fd --changed-within` is a determinism regression

The essay's recency examples select files by mtime. We already hold a
stronger mechanism: source-state generations (EDC §8) — git porcelain ×
untracked `(path,size,mtime)` signatures, persisted as `changed(file,
generation)` facts (`execution.py:216`, `facts.py:493`). Generations are
content-confirmed and replayable; wall-clock mtime windows are neither
(they drift across machines, clones, and CI, and two runs of the same
query disagree). `corpus --changed` therefore binds to generation facts,
never to mtime. `fd` remains useful purely as a fast physical walker —
and even there its parallel traversal emits nondeterministic order, so
the engine contract imposes our own terminal sort exactly as the rg
engine does (`rg_engine.py:120`).

### 2.5 watchexec: right instinct, wrong era, and weaker than claimed

Two corrections. First, the sequencing non-goal is explicit: no resident
daemons before the broker (ALGEBRA sequencing §4; M-E), and the tree
carries a scar — the call-graph background watcher was removed twice for
runaway resource use (`callgraph.py:13`). Second, the essay overstates
the payoff: because derived artifacts are content-addressed and keyed by
source blob hash (skeletons, facts derivation fingerprints,
`facts.py:127-163`), invalidation is already *lazy and free* — an
unchanged file is never re-parsed, and staleness is impossible because
generation hashes are checked at use time. What watching buys is only
**warming** (recompute before the next request instead of during it),
which is a latency optimization, not a correctness or token mechanism.
Latency optimizations do not justify a new resident process with process-
group management inside the trust boundary. M-K6 records the design so
the broker era can host it as a tenant.

### 2.6 What the proposal misses entirely

Four planes any new producer must land on, absent from the essay:

- **The EDC.** New evidence kinds (file sets, record streams) need a
  contract (REQUIRED census / ELASTIC detail / RETRIEVABLE bodies), a
  coverage receipt, and deterministic rendering — or they are just more
  bytes. Each M-K phase names its contract.
- **Prefix-asset economics.** Adding MCP-visible surface churns prefix-
  resident bytes and breaks prompt-cache reuse (`query.py:8`,
  `prefixassets.py`). `q` deliberately shipped with no MCP wiring for
  this reason. New sources arrive CLI + plan-tier first; MCP exposure is
  a separately versioned decision.
- **The trust envelope.** Each new binary (fd, jaq, comby) widens the
  execution surface. They enter as opportunistic engines behind logical
  ops — probed with `shutil.which`, kill-switchable by env, parity-tested
  against a stdlib fallback (the established idiom:
  `tests/test_v03_libraries.py:44`) — never as separately taught tools.
  Which is the essay's own thesis, applied to its own binaries.
- **Referees.** Every phase below ships behind a named measurement, or it
  doesn't merge. The essay's "measured workload" rule, made binding.

## 3 · The operator-class registry (normative)

The durable form of the essay's stack. Logical classes, their engine
ladders (left = preferred, `→` = labeled degradation), and status:

| Operator class | Logical surface | Engine ladder | Status |
|---|---|---|---|
| `file_select` | `corpus` (q source) · `repo.files` (plan op) | git ls-files → **fd** → os.walk; `--changed` from generation facts | **Shipped** v0.26.0 (M-K2) |
| `site_search` | `search`/`refs` (q) · `code.search` (plan) · MCP `search` | rg --json → Python regex | **Shipped** incl. span capture + result blobs, v0.26.0 (M-K1) |
| `symbol_extract` | `decls`/`outline` (q) · `ast.outline` (plan) · `def/refs` | SCIP → jedi/ast-grep → tree-sitter → ctags --json → heuristic | **Shipped** except SCIP → M-K4 |
| `record_transform` | `records` source + `where/group/top/count/distinct/histogram` (q) · `evidence.*` (plan) | native stages → **jq/jaq compile target** → `ctx eval` escape hatch | **Shipped** native, v0.26.0 (M-K3); jq engine + opportunity ledger open |
| `structural_rewrite` | `ast.rewrite.preview/apply` (plan, execute-class, CLI-only) | ast-grep → **comby** → **decline** (never textual) | **Shipped** (1 rung); → M-K5 |
| `incremental_trigger` | none (generation checks at use time) | content-keyed laziness → broker-era watcher tenant | lazy form **Shipped**; watcher **Deferred** → M-K6 |

The product law, restated in registry terms: *select files before
scanning; select sites before reading; select symbols before
materializing bodies; transform records without a model turn; rewrite
structurally or decline; recompute only what changed — lazily.*

## 4 · The phases (M-K)

Ordered by leverage ÷ risk. Each inherits determinism, budgets, declared
omission, and telemetry, or it doesn't merge.

### M-K1 · Span-precise sites with per-result provenance

**Status: shipped v0.26.0** (all deliverables; engine parity extends to
byte-identical `ctx.search/v1` result blobs).

*Finish the shipped rg normalization: sites become `(path, line, col_a,
col_b)`, and every search mints an addressable result.*

**Deliverables**
- `RgMatch` gains submatch columns from the `rg --json` messages already
  parsed; the Python engine emits the same fields from its `finditer`
  spans; `SearchHit` unified accordingly.
- `q` site rows and `code.search` plan artifacts carry columns; minted
  spans for search hits become span-precise; dedup keys on coordinates,
  not line text.
- Search emissions mint a per-result blob (parity with `q`'s final-stream
  minting) so a search is citable as one handle, not only per-file
  snapshots.
- No MCP schema change (fields are additive inside existing payloads).

**Acceptance**: engine parity extended to columns (grow
`test_rg_and_python_engines_agree`); byte-determinism of digests
unchanged; every emitted site resolves via `ctx get` to the exact span.
**Referee**: replay archived transcripts (`ctx replay`) — evidence-
sufficiency scores must not drop; span-precision must remove the
line-text re-match fallback (`rg_engine.py:44-45`).
**Effort**: ~½ day. **Depends on**: nothing.

### M-K2 · `corpus`: the file-set algebra

**Status: shipped v0.26.0** (`filesets.py`; the wall-clock/token referee
over Semgrep/ast-grep scoped runs remains open — file it with the next
eval refresh).

*The missing operator class. A bounded, receipted answer to "which files
may the next operation touch?"*

**Deliverables**
- `q` source stage `corpus` (emits the existing `files` kind):
  `corpus [--ext E]… [--glob G]… [--exclude G]… [--changed [gen:N]]
  [--max N]`. Composes with the existing chain: `corpus --ext py
  --changed | outline`, `corpus --glob 'migrations/*.sql' | get`.
- Plan op `repo.files` (observe-class, index cost) with the same args;
  scan-class ops (`ast.search`, `code.search`, `semantic.*`) accept a
  file-set input via the existing capped `foreach` — plans can finally
  express *file reduction first, semantic sophistication second*.
- Physical ladder: `Workspace.list_files` (git ls-files, shipped) →
  **fd** when on PATH (`--color=never -t f`, our terminal sort imposed,
  `CTX_FILES_ENGINE=python` kill-switch, `HAS_FD` probe) → `os.walk`.
  All three produce identical sorted listings.
- `--changed` binds to generation facts (`changed(file, gen)`), never
  mtime (§2.4).
- Evidence contract for the `files` kind: REQUIRED — selection census
  (count + coverage: roots scanned, considered, selected, truncated-at);
  ELASTIC — per-file rows (path, size, generation); RETRIEVABLE — the
  full listing as a minted blob when it exceeds budget.

**Acceptance**: byte-identical listings across all three engines on this
repository (parity test per the established idiom); coverage receipt
present on every emission; a plan that scopes `semantic.taint` to a
`repo.files` result is validated and runs the engine over only the
selected set.
**Referee**: measured wall-clock and token deltas for Semgrep and
ast-grep plan runs, whole-repo vs corpus-scoped, on the eval fixtures —
the essay's "17 files, not 1,482" claim gets a receipt or the foreach
wiring is dropped.
**Effort**: ~1 day. **Depends on**: nothing (M-K1 independent).

### M-K3 · `records`: the stream algebra over structured artifacts

**Status: shipped v0.26.0** — the `records` source, `distinct`, and
`histogram` (native engine, closure-pinned). Open, deliberately: the
jq/jaq physical compile target (pure speed, adds no capability) and the
`records_opportunity` adoption ledger (ship with the next telemetry
wave); the eval-collapse records-arm referee runs then.

*Absorb the jq class without importing the jq language.*

**Deliverables**
- `q` source stage `records <handle> [--jsonl] [--pointer /path]` —
  opens a stored artifact (run stream, blob, plan artifact) as the
  existing `records` kind. Compiler/test/SARIF/lockfile JSON becomes
  queryable where it already lives: the store.
- New total stages `distinct <field>` and `histogram <field>
  [--buckets N]` (records|sites → records), closing the M-H combinator
  set alongside shipped `where/group/top/count`. Closure classes derived
  from type signatures as ever; `tests/test_digest_closure.py` extended.
- **jq/jaq as physical engine, not vocabulary**: when on PATH, a
  validated stage chain over records may compile to a single jq program
  executed with `--` args and our determinism flags; output is parsed
  back into the typed stream. Byte-identical to the Python engine by
  construction (parity-tested), kill-switchable, absent-cost-nothing.
  Model-authored jq text is **never** accepted on the bounded tier.
- The escape hatch stays where it is: arbitrary jq inside `ctx run`
  / `ctx eval` under the CLI trust envelope. New instrument: the
  adoption-ledger pattern counts record-transform shapes appearing in
  `run --shell` pipelines and `eval` scripts (`jq`, `sort | uniq -c`,
  `awk '{print $N}'`) as `records_opportunity` events — the observed
  demand that decides which projections get promoted to named stages
  (the essay's "observe → freeze → typed operator" loop, instrumented
  rather than assumed).

**Acceptance**: totality preserved (parser rejects unknown stages; no
stage accepts arbitrary expressions); Python/jq engine parity
byte-identical on fixtures (SARIF, pytest-json, lockfile); `records`
reachable from plans via existing `q.pipe`.
**Referee**: re-run the eval-collapse scenarios with a records arm —
target unchanged from M-H: eval-arm correctness at ≤¼ the model-authored
tokens; plus the opportunity ledger reporting ≥1 promotion candidate
from real sessions before any further stage is added.
**Effort**: 1–2 days. **Depends on**: M-K1 useful, not required.

### M-K4 · Precise references: SCIP ingestion (M-G increment, resequenced)

Unchanged in content from ALGEBRA M-G (designed, unbuilt): opportunistic
`index.scip` reader → `ref` facts with a labeled precision tier;
`refs`/`code.refs` engine ladder gains SCIP above jedi/ast; absence costs
nothing. Resequenced *above* the comby rung because precise references
multiply the value of every downstream join (root-cause query, impact),
while a second rewrite engine only widens one op. **Acceptance/referee**
as specified in ALGEBRA §M-G and its root-cause-join eval. **Effort**:
~2 days.

### M-K5 · Rewrite breadth: the comby rung, behind a decline corpus

**Status: deliverable 3 (sed/awk steering) shipped v0.26.0** — read-only
steers to capture; in-place force_asks with the preview-first remediation,
detected in plain argv *and* inside compound expressions (where every
`{…}` awk program lands). The gate instrumentation and the comby rung
itself remain designed.

*ast-grep → comby → decline. The contract does not change; only the
ladder grows a rung.*

**Deliverables — gated, in order**
1. **The gate first**: instrument `ast.rewrite.preview` declines and
   `rewrite`-shaped requests the current rung cannot express (no
   tree-sitter grammar; config/DSL/partial syntax; balanced-hole
   templates). A committed fixture corpus from real sessions. **Comby
   merges only if the corpus shows a real population** — the essay's own
   "only when you have real rewrite fixtures," made binding.
2. If gated in: `comby` as second engine under the *same* contract —
   match census → preview diff minted as blob → generation guard →
   transactional `git apply` → verifier — by reusing the shipped
   apply path (`astgrep.py:431-469`) verbatim. Execute-class, CLI-only,
   typed-rejected on MCP, exactly like `ast.rewrite.*` today.
3. Explicit text-tool steering, shippable independent of comby: `sed`
   and `awk` currently fall through to the unknown-command policy
   (`hook.py:736-744`) with no named rule. Add the policy the essay
   correctly wants: read-only `sed`/`awk` allowed through `run`;
   in-place mutation of source files steered to `ast.rewrite.preview`
   with a remediation line; in-place over generated/plain text permitted
   under a generation guard. Never silently downgrade a structural
   rewrite to sed — already invariant, now also taught.

**Acceptance**: no behavior change while gate data accumulates (item 3
excepted, which lands with steering tests alongside the grep/find
rules); if comby lands: preview/apply property tests shared with
ast-grep (overlap rejection, stale-generation refusal, all-or-nothing),
absence degrades to decline with a labeled note.
**Risk, recorded**: comby is an OCaml binary with a quiet maintenance
cadence; it must never become required (the decline rung is a correct
terminal state).
**Effort**: instrumentation ~¼ day; the rung 1–2 days if and only if
gated in.

### M-K6 · Incremental frontier: deferred to the broker, on the record

No watcher ships in this era (§2.5). What this phase *is*: the design
note that when M-E lands, the broker hosts a watch tenant (watchexec or
the watchfiles library) whose only privilege is warming — coalesced
changed-path events pre-derive skeletons and facts for the changed
frontier and refresh the capability surface. Correctness continues to
come from generation checks at use time; killing the watcher changes
latency, never results. Until then, the shipped content-keyed laziness
*is* the incremental algebra. **Effort now**: zero. **Depends on**: M-E.

## 5 · Rejected, with reasons

| Item | Reason |
|---|---|
| mtime-based file selection (`fd --changed-within`) | volatile, machine-local, unreplayable; generations are shipped and strictly stronger (§2.4) |
| raw jq/jaq expressions on the plan or MCP tier | unvalidatable totality; inverts the shipped M-H theorem (§2.3) |
| `xargs` as vocabulary | `foreach` + mandatory `cap` shipped with better error semantics, provenance, and determinism (`plan_ir.py:416`) |
| `sort`/`uniq`/`cut`/`awk` as vocabulary | they are `top`/`group`/`count`/`distinct`/projection — algebra stages, some shipped, rest in M-K3; ad-hoc use stays legal inside `run --shell` where it is captured and counted |
| resident watcher pre-broker | non-goal (ALGEBRA §4); removed twice before (`callgraph.py:13`); buys warming only (§2.5) |
| sd, choose, miller, xsv, gron, sad, amber, rpl, … | standing rule, adopted from the proposal: no new binary without a measured workload demonstrating a missing operator class — and then it enters as an engine behind a logical op, never as vocabulary |

## 6 · Sequencing

```
now ──► M-K1 span-precise sites (½d) ──┐
        M-K2 corpus / repo.files (1d) ─┼─► referee: scoped-scan receipts +
                                       │   replay evidence-sufficiency
next ─► M-K3 records algebra (1–2d) ───┴─► referee: eval-collapse records arm
        M-K5.3 sed/awk steering (¼d, independent)
then ─► M-K4 SCIP ingestion (2d) ──► root-cause-join eval (ALGEBRA M-G)
        M-K5 comby rung — if and only if the decline corpus gates it in
broker era (M-E) ──► M-K6 watch tenant (warming only)
```

The one-sentence law, kept: **compile the durable algebra of the Unix
tools into a small, typed, addressable execution plane — and make every
binary an engine, every operator a contract, and every claim a receipt.**
