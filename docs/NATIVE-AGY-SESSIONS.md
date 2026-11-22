# Native Antigravity sessions

Use `ctx collab` when a worker needs to keep a native Antigravity conversation
across turns. Use [ACP](ACP.md) to delegate independent tasks across harnesses.

## Choose a Gemini model

Run `agy models` in the account that will do the work. Copy a slug from its first
column, including the thinking level. Google documents these four model families.
Availability depends on your account and plan.

| Model | Example native CLI slug | SDK model ID | Suggested work |
|---|---|---|---|
| Hydrogen | `gemini-4-hydrogen-high` | `gemini-4-hydrogen` | Implement a scoped edit and run its tests |
| Neon | `gemini-4-neon-high` | `gemini-4-neon` | Debug or review a change across files |
| Carbon | `gemini-4-carbon-high` | `gemini-4-carbon` | Plan a complex change with several dependencies |
| Argon | `gemini-4-argon-high` | `gemini-4-argon` | Reason through an architecture decision or a difficult bug |

The work suggestions are routing heuristics. Google publishes the account lookup
and slug syntax in the [headless CLI reference](https://antigravity.google/docs/cli/headless/),
the [model picker](https://antigravity.google/docs/models), and its
[model ID table](https://antigravity.google/docs/enterprise#supported-models-in-gemini-enterprise).

**Native CLI.** Install the workspace integration with `ctx setup --host antigravity`.
Start an interactive session with the exact slug, or pass it to `ctx collab launch`
to record the conversation and result.

```sh
agy models
agy --model gemini-4-neon-high
```

**ACP worker.** An ACP adapter advertises its own exact IDs. For example, if the
adapter advertises `gemini-4-neon`, use the following configuration. Straitjacket
checks the ID when it opens the session and fails before sending the task if the
adapter does not offer it.

```sh
ctx setup --host antigravity --acp --acp-model gemini-4-neon --acp-tier frontier
ctx orchestrate 'Review the current diff' --dry-run
```

**SDK worker.** `ctx wrap antigravity-sdk` installs the SDK worker and prints the
launcher path. With `GEMINI_API_KEY` set, pass an API ID to that launcher, e.g.
`--model gemini-4-hydrogen -p 'Run the parser tests'`. To make a Gemini 4 model
eligible for automatic SDK routing, set `ANTIGRAVITY_MODEL` to its base ID.
The router still chooses among eligible models for each task. Installing the
SDK alone keeps the existing routing defaults.

Route estimates use global standard API rates before promotional credits.
The table prices cache writes as uncached input. Google lists separate write
rates for Hydrogen and Neon starting January 12, 2027. Antigravity subscriptions
and credits use their own plan. [API prices](https://cloud.google.com/model-garden/pricing) · [Antigravity plans](https://antigravity.google/docs/plans)

## Launch a read-only review

Replace the path with one in your checkout. Use the task ID from the first command
and the session ID from the launch response in the commands that follow.

```sh
ctx collab new --goal "Review the proposed change" --owns src/example.py
ctx collab claim TASK_ID --by agy
ctx relay watch codex task --selector TASK_ID
ctx collab launch TASK_ID --model gemini-4-argon-high --mode plan
ctx collab watch SESSION_ID --until any --timeout 30
ctx collab events SESSION_ID --after 0
ctx collab status SESSION_ID
```

The session ID and exact native conversation ID are stored separately. Multiple
IDs can be passed to `watch`: `--until any` returns when one is terminal;
`--until all` waits for every session, within the specified timeout (maximum 60
seconds per call). Local FIFO notifications wake the wait promptly; a five-second
status check covers lost wakeups. Event sequence numbers support incremental
reads. Full provider output remains in `.ctx/collab/sessions/SESSION_ID/`.

Terminal state is also published to the existing relay's `task` topic. Hooked
hosts receive it at their next supported hook boundary. This does not wake an
idle chat application by itself. ACP consumers can use the existing relay and
protocol cancellation path; native JSON events are not presented as ACP messages.
See [Relay](RELAY.md) and [ACP](ACP.md).

`cancel` requests process-group termination. Deadlines also terminate the owned
process group. Permission denials, missing final results, mismatched conversation
or workspace identity, and nonzero exits do not count as successful turns.
`recover` can reconcile a previously captured final result locally without
calling the provider. A completed turn never marks its task completed or applies
changes automatically.

## Native guide and continuation

Attach a plan of at most eight `{ "goal": "...", "check": "..." }` steps using
`ctx collab plan TASK_ID --by agy --file PLAN.json`. Then:

```sh
ctx collab prewalk TASK_ID --model gemini-4-carbon-high
ctx collab continue GUIDE_SESSION_ID --model gemini-4-hydrogen-high
```

The guide runs in an isolated Git worktree containing the current tracked and
untracked work. Continuation requires a successful guide turn, actual edits
inside the owned paths, an unchanged lease and plan, and a passing first-step
check that does not modify source files. It resumes the exact native conversation
in the same worktree. This is separate from the orchestrator's verified checkpoint
handoff in [Prewalk](PREWALK.md); native history is retained, not pruned.

Ownership checks and worktrees are coordination measures, not an OS sandbox.
Checks are trusted local commands. No permission bypass is enabled. Review and
integration remain explicit; the controller does not merge the isolated changes.

The repository records a live Argon read-only review. Tests exercise guide and
continuation, cancellation, deadlines, and isolation with a simulated CLI.
There are no provider measurements here for a Carbon-to-Hydrogen edit.
