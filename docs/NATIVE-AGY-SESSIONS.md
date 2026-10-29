# Native agy sessions

`ctx collab` coordinates provider-native agy turns. The existing `ctx task`,
`ctx orchestrate`, ACP transport, and relay remain the general harness runtime.
Use native sessions when preserving an explicit agy conversation is required.
The selected model must be advertised by your installed `agy models` command.

Create a scoped task, claim it, and launch a read-only review:

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
ctx collab prewalk TASK_ID --model gemini-4-argon-high
ctx collab continue GUIDE_SESSION_ID --model gemini-3.8-flash-high
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

Native streaming and same-conversation read-only review were checked with Argon.
Guide-to-Flash editing, cancellation, deadlines, and isolation have offline fake-host
coverage; a live editing run is still needed before claiming provider-level savings.

## Lessons from jCodeMunch

[jCodeMunch](https://github.com/jgravelle/jcodemunch-mcp) reinforces a useful
workflow: discover symbols cheaply, retrieve exact implementations, and measure
against a realistic search-and-read baseline. ctx already provides bounded
`def`, `refs`, `impact`, and `get --symbol` operations plus a progressive tool
surface. This release adds no jCodeMunch dependency or external summarization
call. Its published savings are not evidence for ctx savings. Future comparisons
must pin source revisions and tokenizers, count follow-up retrievals, and verify
task outcomes alongside context size.
