# Changelog

All notable changes to ctx-harness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is 0.x
with a minor bump per mechanism wave (see CONTRIBUTING.md).

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
