# Changelog

All notable changes to ctx-harness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is 0.x
with a minor bump per mechanism wave (see CONTRIBUTING.md).

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
