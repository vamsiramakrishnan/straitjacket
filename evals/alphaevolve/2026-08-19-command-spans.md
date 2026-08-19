# AlphaEvolve command-span expansion receipt

**Date:** 2026-08-19  
**Scope:** command classification before execution  
**Product seam:** `classify_command_span` -> `choose_guard` -> host steering

## What changed

The command guard now has three explicit outcomes instead of treating every
unlisted command as equivalent:

1. known bounded or structured queries run directly;
2. known read-only commands that may flood are transparently captured when the
   host supports input substitution; and
3. unknown, mutation-shaped, secret-bearing, outside-workspace, or explicitly
   denied commands retain their permission boundary.

The registry covers common low-output utilities, Git summary queries, bounded
GitHub/GitLab list and view operations, GitHub safe `GET` API calls, additional
linters/test runners, and build/test commands from .NET, Swift, and Zig. Git
global options such as `git -C` and `git -c` are parsed before classifying the
subcommand. Follow-forever GitHub operations are captured in the background.

This was inspired by [RTK](https://github.com/rtk-ai/rtk), particularly its
[declarative rule registry](https://github.com/rtk-ai/rtk/blob/develop/src/discover/rules.rs)
and [segment-level classification](https://github.com/rtk-ai/rtk/blob/develop/src/discover/registry.rs).
Straitjacket keeps a distinct third state for commands that still need review:
unsupported is not silently equated with safe.

## Massive deterministic matrix

Command:

```bash
python -m evals.alphaevolve.guard_policy.command_matrix
```

The generator combines direct commands with `env`, `timeout`, `nice`, `sudo`,
and absolute-path wrappers; bounded GitHub limits from 0 through 150; structured
fields; read-only noisy operations; explicit denies; mutation-shaped unknowns;
and compound shell expressions.

| Result | Cases |
|---|---:|
| Total generated | 57,313 |
| Direct allow | 34,311 |
| Transparent capture | 15,654 |
| Permission boundary | 7,344 |
| Explicit deny | 4 |
| Classification failures | **0** |
| Corpus fingerprint | `c32e1d880128c9ad` |

The matrix is deterministic and is also exercised by the repository test
suite. It is deliberately large enough to test interactions rather than only a
hand-maintained happy-path list.

## Counterexample found and integrated

The first broad run found that a compound such as a bounded read followed by an
unknown mutation could fall through to whole-command capture. Since capture
executes the command, that weakened the permission boundary. The reviewed
integration now evaluates every segment before rewriting:

- all bounded segments may run directly;
- capture is allowed only when every non-direct segment is capture-eligible;
- any secret, outside-workspace, explicit deny, or unknown mutation prevents
  whole-command rewriting.

Repository-configured `deny_commands` also now carry an explicit no-rewrite
safety marker, so host substitution cannot turn a committed deny into an
execution path.

## AlphaEvolve policy result

The `guard-policy` family now searches the same production decision function.
Its hard gates require safe classification and task-completion capability before
efficiency is considered. Against the deliberately naive policy model, the
integrated policy reduces modeled dollars by 33.33%, model turns by 66.67%, and
visible tokens by 41.75%. Capture adds local latency and tool work, so the
candidate is not claimed to dominate every metric.

Those percentages are evaluator-model results, not provider billing. No paid
managed AlphaEvolve campaign was run for this receipt. Managed search remains
bounded and spend-gated.

## Host behavior and limits

- Claude Code and Codex can substitute the capture command in the same turn.
- Antigravity's current hook contract cannot replace tool input, so a noisy
  read is returned as an exact rerun instruction; newly recognized bounded
  commands still avoid that extra turn.
- The generated corpus proves classifier consistency over its declared grammar,
  not completeness over every shell program or semantic equivalence of every
  third-party version.
- Unknown and mutating commands intentionally remain visible approval events.
