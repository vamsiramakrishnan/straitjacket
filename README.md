# straitjacket

**Context and evidence tools for coding agents.**

Straitjacket turns long command output into bounded digests with retrieval
addresses. Agents can open the exact evidence they need, find relevant code,
and hand a failing test, source location, or verified patch to another harness.
Use it alongside Claude Code, Codex, Antigravity, and the other supported hosts.
Your harness still runs the model, authenticates the user, and controls its tools.

<p>
  <a href="https://code.claude.com/"><img src="assets/agents/claude.svg" width="36" height="36" alt="Claude Code" title="Claude Code"></a>
  <a href="https://developers.openai.com/codex/"><img src="assets/agents/codex.svg" width="36" height="36" alt="Codex" title="Codex"></a>
  <a href="https://antigravity.google/"><img src="assets/agents/antigravity.png" width="36" height="36" alt="Antigravity" title="Antigravity"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="assets/agents/hermes.svg" width="36" height="36" alt="Hermes" title="Hermes"></a>
  <a href="https://github.com/can1357/oh-my-pi"><img src="assets/agents/omp.svg" width="36" height="36" alt="Oh My Pi" title="Oh My Pi"></a>
  <a href="https://opencode.ai/"><img src="assets/agents/opencode.svg" width="36" height="36" alt="OpenCode" title="OpenCode"></a>
  <a href="https://github.com/deepseek-ai/deepseek-harness"><img src="assets/agents/dsh.svg" width="36" height="36" alt="DeepSeek Harness" title="DeepSeek Harness"></a>
</p>

Python 3.11+ · `ctx-harness` v0.5.0 · Apache-2.0 · [Documentation](https://vamsiramakrishnan.github.io/straitjacket/)

**See the mechanism:** the [20,001-line log replay](evals/cache-ab-2026-11-23.md)
reduces 302,628 tokens to a 531-token canonical digest, retains a quiet anomaly,
and retrieves omitted lines exactly. This local fixture measures evidence
delivery. [Task success and cost need separate measurements](#what-the-measurements-show).

## Try it on a failing test

In a repository with a pytest suite, run:

```sh
python -m pip install 'ctx-harness==0.5.0'
ctx run -- pytest -q
```

For a long failure, `ctx` returns the
test names, a digest, and an address for the captured output. For a failing test command, the digest records the child
exit code and `ctx run` returns 3. To find the traceback, replace the sample
handle below with the one from your run and search the stream pytest used.

```sh
ctx search run:ba3d1020ee8f#stdout 'AssertionError'
```

Use the line range in the search result with `ctx get`. The default capture
limit is 64 MiB per stream. If output crosses it, the digest identifies the
discarded middle. To connect your coding harness, run `ctx setup` and then
`ctx doctor`. [Installation and host setup](docs/GETTING-STARTED.md).

## Choose a workflow

| If you want to… | Start here |
|---|---|
| Find an implementation or call site without reading whole files | [Search by symbol, follow references, and open a source range](docs/USE-CASES.md) |
| Reuse a prompt prefix as the question and evidence change | [Build a stable prefix](docs/QUERY-AWARE-SELECTION.md) and [measure the session](docs/HOW-IT-WORKS.md) |
| Give exploration, implementation, and verification to different harnesses | [Delegate through ACP](docs/ACP.md) and [inspect the route](docs/ROUTING.md) |
| Control which skills and tools a session sees | [Inspect skill listings and configure a launch profile](docs/CAPABILITY-SURFACE.md) |
| Run Gemini 4 Hydrogen, Neon, Carbon, or Argon | [Discover and select an Antigravity model](docs/NATIVE-AGY-SESSIONS.md) |

## Delegate work across harnesses

`ctx orchestrate` assigns each step to a harness and model using its capability
requirement and configured costs. A worker receives the task and checkpoints
from its dependencies. The task ledger records attempts and handoffs, so a
resumed route can reuse finished work. The relay lets a peer send an evidence
address while work is in progress. [Worker routing](docs/ROUTING.md) ·
[Task ledger](docs/TASK-LEDGER.md) · [Relay](docs/RELAY.md).

Agent Client Protocol (ACP) lets Straitjacket start and cancel workers through
one interface. Install and authenticate an adapter, then configure the exact
model ID it advertises. For example, with OpenCode:

```sh
ctx setup --host opencode --acp --acp-model 'provider/model-id'
ctx orchestrate 'Review the current diff' --dry-run
```

Replace `provider/model-id` with an ID from that endpoint. The preview shows
worker assignments. Use the [checkout installation](#development) for model-free
previews of general tasks and the new Gemini 4 routing entries. Running the route starts model work. Each ACP attempt opens
a fresh session and receives bounded retrieval and edit tools over MCP.
[ACP setup for all seven harnesses](docs/ACP.md).

The host decides when to load a skill. On Claude Code, `ctx surface inventory`
estimates the cost of listing a skill separately from loading its body.
`ctx wrap claude --surface auto` can shorten listings for skills the project's
session history shows it never used. [Skill selection and launch profiles](docs/CAPABILITY-SURFACE.md).

## Spend fewer turns finding code

In a Straitjacket checkout, these commands locate the reference parser, read
its definition, and find uses under the package:

```sh
ctx search repo: sym:parse_ref
ctx def repo:src/ctx/refs.py:parse_ref
ctx refs parse_ref --path src/ctx
```

If all you have is a bug report, start with `ctx pack "your task"` for ranked
starting points. If you already have a path and symbol, go straight to
`ctx def`. The result identifies the lookup method and its coverage.
[Code search](docs/CODE-SEARCH.md) · [Investigation workflows](docs/USE-CASES.md).

## Keep repeated input cheap

A large result early in a session can be read and billed on every later turn.
Capture it once, then retrieve just the evidence that changes the next decision.
Straitjacket keeps its own shared instructions stable across turns. For a
custom model client, it can also serialize fixed reference material ahead of a
changing question and selected excerpts:

```sh
ctx compress build.log --query 'Which target failed?' --cache-prefix reference.txt
```

Send the returned `cache_wrap.prefix` first and `cache_wrap.suffix` second.
The same reference produces the same prefix bytes across questions. The
provider determines reuse. To evaluate savings, compare runs on the same task
and model. Record completion, cache reads and writes, uncached input, output,
and cost. `ctx stats --session` can report observed usage when available.
[Prompt layout](docs/QUERY-AWARE-SELECTION.md) · [Session measurement](docs/HOW-IT-WORKS.md).

## Select a Gemini 4 model

Run `agy models` with the account that will do the work. In the native
`ctx collab` workflow, pass a listed slug such as `gemini-4-hydrogen-high` to
`--model`. For ACP, use the ID advertised by the ACP endpoint. Availability
and reasoning levels depend on the account and client. The [Antigravity guide](docs/NATIVE-AGY-SESSIONS.md)
covers Hydrogen, Neon, Carbon, and Argon, including how to start and continue a
native conversation.

## What the measurements show

| Workload | Observed result |
|---|---|
| [20,001-line log fixture, replayed November 23](evals/cache-ab-2026-11-23.md) | The raw log took 302,628 o200k_base tokens. The canonical digest took 531; actual counts were 531–537 as opaque run handles changed. It retained the quiet event at line 14,238, preserved the captured bytes exactly, and retrieved an omitted control line. No model ran. |
| [Source ranges after edits](evals/anchor-drift-2026-08-20.md) | Across 1,920 lookups, anchored ranges relocated 1,452 times, verified in place twice, refused 466 times, and returned the wrong content zero times. |
| [Three live canary tasks, one run per arm](evals/agentbench/RESULTS.md) | Both arms solved 3/3. With the wrapper, median cache hit rate rose from 96.5% to 97.5% and total cost rose from $0.67 to $0.92. |
| [Finding files on 14 DeepSWE tasks](evals/agentbench/results/pack_recall.json) | Among the top eight suggestions, `ctx pack` found 54.0% of existing files touched by the reference fix on average. Ranking by grep count found 42.9%. Grep found at least one relevant file on all 14 tasks, the pack on 12. |
| [DeepSWE, eight Python tasks, Haiku, 60-turn cap](evals/agentbench/deepswe-2026-09-13.md) | The wrapped arm passed 177 held-out fail-to-pass test IDs on committed patches versus 54 for plain Claude. The runs cost $5.74 and $5.42 respectively. Neither arm fully resolved a task. |

## Integration and storage

The CLI works in any terminal. Claude Code and Codex can replace oversized
results after a command runs. Antigravity CLI can rewrite supported calls
before execution. Hermes, Oh My Pi, and OpenCode can rewrite arguments and
replace text results. DeepSeek Harness can gate calls and replace text results.
The [host matrix](docs/HOST-CAPABILITIES.md) lists version-specific behavior
and distinguishes live observations from adapter tests.

Captured artifacts live in a local plaintext store. Model-visible reads follow
the configured redaction policy. Unpinned artifacts become eligible for
collection after 30 days by default and remain until `ctx gc` runs. Commands
run with the invoking user's permissions. [Configuration](docs/CONFIGURATION.md).

[Verified edits](docs/EDIT-LOOP.md) · [Tool SDK](docs/TOOL-SDK.md) ·
[CLI reference](docs/CLI.md) · [All guides](docs/README.md) · [Changelog](CHANGELOG.md)

## Development

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
pytest
```

New mechanisms need deterministic output, explicit degradation, and a named
evaluation gate. Follow [CONTRIBUTING.md](CONTRIBUTING.md).

Useful first contributions: [add a digest profile for a command you use](docs/WRITING-A-PROFILE.md),
reproduce an output that loses important evidence, or add a small-output case
where the wrapper costs more than it saves. Include the command, expected
evidence, and reproduction steps in an [issue](https://github.com/vamsiramakrishnan/straitjacket/issues/new).
The [evaluation guide](evals/README.md) shows how to check delivery without a model.

## Related projects

- [branchyard](https://github.com/vamsiramakrishnan/branchyard). Run coding agents on isolated branches and review their changes.
