# Speculative-native named-test promotion — 2026-08-18

## Decision

Promote one narrow graduated-steering arm: on Claude Code and Codex, run one
explicit pytest node (`path::node`) without the `ctx run` wrapper while the
session is passive and that normalized signature has no prior flood.

This is not a general pytest bypass. Whole suites, directories, file-only
targets, shell expressions, active sessions, strict/deny steering, and hosts
without PostToolUse output substitution retain birth-time capture. Set
`[guard].speculative_native = false` to disable the arm.

## Why this arm

The live named-test comparison was Straitjacket's known naive-loss case. The
wrapper imposed fixed process and digest scaffolding on output already small
enough to pass through. The AlphaEvolve emission and engagement policies both
selected the same structure: raw small output first, with a bounded digest only
after observed truncation or pressure.

## Matched local measurement

Environment: this checkout, CPython/pytest available on PATH, warm filesystem,
11 alternating measured repetitions after one warm-up per arm. Target:
`tests/test_version.py::test_cli_version_matches_runtime`.

| Metric | Always captured | Speculative native | Delta |
|---|---:|---:|---:|
| Median wall time | 642.306 ms | 512.910 ms | **20.15% lower** |
| Tool-result bytes | 150 B | 80 B | **46.67% lower** |
| Successful executions | 11/11 | 11/11 | equal |

This is a local tool-path benchmark, not a billed-token or production canary.
It measures removal of Straitjacket's fixed wrapper tax on the exact small-task
shape; it must not be extrapolated to other task families.

## Safety fallback measurement

The real PostToolUse gate was given a 41,939-byte pytest failure under the same
named-test command. It emitted an 871-byte `pytest/*` digest with a working
`run:` address: **48.15x containment / 97.92% fewer visible bytes**. The gate
also recorded the signature as an intervention, making the next same-signature
call ineligible for speculative execution.

## Verification

- Full suite: **PASS**, 1,684 test functions (configured skips unchanged).
- Documentation facts: **PASS**.
- Documentation links: **PASS**.
- Focused hook, dialect, fail-closed, config-parity, intervention, and safety
  suites: **PASS**.

## Live evidence emitted

`.ctx-session-reads/steering-decisions.jsonl` contains no prompt or output
text. It records normalized signatures plus:

- `ctx.steering-decision/v1`: native selection and reason;
- `ctx.steering-result/v1`: raw byte count, whether the gate fired, and error
  status.

These receipts are the input for the next actual-usage/production canary. The
promotion criterion remains completion and safety first, then lower cost,
visible context, turns, and latency.
