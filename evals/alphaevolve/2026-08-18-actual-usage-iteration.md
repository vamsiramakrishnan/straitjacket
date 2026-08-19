# Actual-usage iteration — 2026-08-18

## Question

Can a compact single-node Straitjacket prompt beat both the current routed path
and a direct naive invocation for the named-test use case?

## Matched task

Run `tests/test_usage.py` with Claude Haiku 4.5 and report whether the five tests
pass. Success required an explicit test result. All dollar figures below are
host-reported actual usage, not route estimates.

| Arm | Result | Actual dollars | Total tokens | Duration |
|---|---:|---:|---:|---:|
| Straitjacket, current warm route | 5 passed | $0.01409415 | 53,140 | 6,994 ms |
| Direct naive, warm | 5 passed | $0.01298365 | 52,660 | not captured |
| Straitjacket, compact candidate | 5 passed | $0.01444830 | 53,201 | 8,719 ms |

The current warm route cost 8.55% more than direct naive. The compact candidate
cost 2.51% more and took 24.66% longer than the current warm route, while using
0.11% more tokens. It was therefore rejected and removed from production code.

A preceding malformed direct invocation was excluded: shell interpretation
changed the supplied command and the model did not execute the requested test.
It is operator error, not a valid naive observation.

## Decision

- Keep the high-confidence one-node named-test route: it eliminates coordinator
  and multi-node turns while preserving completion.
- Do not claim a cost win over direct naive for this case; direct won this probe.
- Do not promote the compact-prompt candidate.
- Use randomized, repeated warm A/B runs before changing this prompt again.
- Continue targeting 100x improvements where the reducible waste is large
  (especially raw tool output and redundant context), and use parity/direct
  bypass for small already-efficient cases.

This receipt intentionally records a reversal. AlphaEvolve candidates are
hypotheses; live task completion and actual usage decide promotion.
