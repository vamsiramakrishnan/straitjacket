# Changelog

All notable changes to ctx-harness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is 0.x
with a minor bump per mechanism wave (see CONTRIBUTING.md).

## [Unreleased]

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
