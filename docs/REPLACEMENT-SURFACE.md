# The replacement surface (retired)

> **Design & internals — not product documentation.** This doc explains *why* a
> mechanism existed and how it was reasoned out. This one **no longer ships as
> described**: the command substitutions were retired because they could change
> the answer a command returned. For what the product does **today**, read
> [Configuration](CONFIGURATION.md#steering-what-happens-to-a-shell-command),
> [Host capabilities](HOST-CAPABILITIES.md) and the [changelog](../CHANGELOG.md).

## What it was

The replacement surface was the idea that adoption is won by substitution, not
persuasion. A benchmark (14 scenarios, 3 arms, `evals/bench-*`) landed at parity with
a naive agent, and across all straitjacket cells the agent invoked a `ctx` verb three
times; it reached for raw `grep`, `cat` and `pytest` anyway. So the PreToolUse hook
rewrote a recognised loop-shape in place, through `updatedInput`, to a collapsed
`ctx` operation:

| the agent ran | it actually ran |
|---|---|
| `grep -rn Symbol .` | `ctx q 'refs Symbol \| group file'` |
| `grep -rn "pattern" .` | `ctx q 'search pattern \| files'` |
| a whole-suite `pytest` after a captured failure | `ctx q 'fails last \| in-changed'` |
| `cat`, `wc`, `head`, `sed`, `find`, `ls -R`, `tree` | an outline, a count, `ctx get`, a corpus listing |

The thesis was borrowed from tools that replace the tool surface instead of teaching:
the agent never decides to be efficient, because the path it is given already is. The
first measured run (`evals/replacement-surface-2026-07-22.md`) was an honest N=1 null:
the surface was correct and cache-neutral, adoption was flat.

## Why it was retired

The safety bar for rewriting a command was equivalence: the substitute must answer the
same question. Compared byte for byte against bash over 1,875 generated commands, and
in live sessions on Claude Code 2.1.322, it did not:

- `cat *.txt` ran with the glob quoted, so the shell never expanded it;
- `grep -rn TOKEN .` became a symbol lookup that returned one of three matches;
- a whole-suite `pytest` became a lookup of earlier failures instead of a run;
- `head f.txt` from a subdirectory read the root's file.

A substitution that answers a nearby question with the same confidence is worse than
none. There was a second fault in how the rewrite was approved: on Claude Code an
`allow` together with a rewritten command overrides the user's deny and ask rules, and
the hook sent `allow` with every rewrite. See [Host capabilities](HOST-CAPABILITIES.md).

## What exists now

- **No substitution.** The substitutions above are retired. A shell command runs as the
  user or model wrote it. On Claude Code and Codex, by default, it is not rewritten at
  all; ctx bounds the output after the command runs and the host decides about the
  command as it would without ctx. Where a rewrite still happens (Antigravity, which
  cannot replace output, and `[guard] steering = "rewrite"` on Claude Code), the
  original command is passed verbatim to the shell under bounded capture:
  `ctx run --passthrough --shell -- '<original>'`.
- **Native search tools are still steered.** Claude Code's `Grep` and `Glob` are
  distinct tool calls, not commands, so `updatedInput` cannot swap them into a `ctx`
  call. With `[guard] collapse = true` (the default) they are denied with the `ctx`
  command named, and `ctx wrap claude` removes them from the session with
  `--disallowedTools Grep Glob` unless you set your own. `collapse = false` lets them
  run. This is the only thing `collapse` governs now.
- **Search offers a different question, beside the answer.** For an identifier search
  the module keeps a hint that offers `ctx q refs <name>` as a *different* question
  (definition and uses), never as a replacement for the grep.
- **The Capture ladder in `ctx ladders` receives no records,** because no substitution
  is made. Its row is kept in the table.
- **Records moved.** Ledgers that recorded command text were in the workspace; they are
  now in the ctx state directory.

The `ctx` verbs the substitutions pointed at (`ctx q`, `ctx search`, `ctx get`,
`ctx run`) are unchanged and remain the right tools when the agent chooses them. The
measurement question the original page ended on, whether a fired substitution moves
cost or success at all, is closed by the retirement rather than answered.
