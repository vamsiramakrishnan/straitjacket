# Delegate work across coding harnesses

Use `ctx orchestrate` when one harness should find the evidence, another should
make the change, and a verifier should check the result. Each worker keeps its
own model, credentials and tools. Straitjacket assigns the work, saves results
in the workspace and gives each dependent worker a bounded handoff with
`checkpoint:` addresses. Workers can finish at different times and use different
harnesses.

## Try a two-stage handoff

From a Git checkout of this project, with at least one unattended host
configured, preview a small edit:

```sh
ctx orchestrate 'Fix a typo in README.md' --dry-run
```

The deterministic plan has an `implement` node followed by `verify`. Read
the selected host, model, tier and estimated cost for each node. A single
installed harness can handle both nodes. Configuring a second host gives the
router more choices. To execute, replace the example with an actual task and
its acceptance check, then omit `--dry-run`. Execution can invoke paid models.

| What the worker needs | How it gets there |
|---|---|
| A defined job | The orchestrator passes the overall task, this node's goal and any declared target paths. |
| Evidence from a predecessor | The worker receives a bounded handoff and resolves cited regions with `ctx get`. The store keeps the captured log up to the configured limit. |
| A specific finding for a later node | From the same workspace, use `ctx task send TASK_ID NODE_ID repo:README.md --note "Check the installation example"`. Use the task and node IDs from your run. |
| An update for a peer harness | `ctx relay signal codex repo:README.md --origin claude --note "See the installation example"` queues an advisory. Delivery depends on the receiving host. |
| Work after an interruption | `ctx task show TASK_ID` shows attempts and handoffs. `ctx orchestrate --resume TASK_ID` restores completed nodes and retries unfinished work. |

## Give a worker a task that needs a skill

A route node declares a role such as `implement` and capability tags such as
`edit` and `code`. Straitjacket uses these tags to select a worker. The
[coordinator skill reference](../plugins/antigravity/skills/ctx-harness/references/harness-collaboration.md)
shows the JSON it expects. When the job needs a specific installed skill, name
the skill in the node's goal and choose a harness that provides it. That harness
controls skill discovery and invocation. ACP passes the task and its tools.
Use the [capability inventory](CAPABILITY-SURFACE.md) to inspect the skills a
workspace exposes.

For a single Claude Code investigation, `ctx setup --host claude` installs the
`ctx-explorer` subagent when the project has no local override. Ask Claude to
delegate a concrete question, such as “Use ctx-explorer to find where ACP
denies a permission request. Return the file, test and evidence addresses.”
The explorer reports findings, negative searches and open questions. The
parent can inspect a cited location before deciding to edit.
[Explorer contract](../plugins/antigravity/agents/ctx-explorer.md).

## Choose the collaboration mechanism

| Mechanism | Use it for | Delivery |
|---|---|---|
| [Routing](ROUTING.md) | Split one task into dependent jobs or parallel independent jobs | The scheduler launches eligible workers when their dependencies finish. |
| [Agent Client Protocol](ACP.md) | Start an installed harness as a worker over stdio | A fresh ACP session gets the selected model, task and workspace-scoped `ctx` and `ctx_edit` MCP tools. |
| [Task ledger](TASK-LEDGER.md) | Transfer a finding to a named task node or recover a run | The next attempt reads its inbox. Completed nodes keep their checkpoints. |
| [Relay](RELAY.md) | Notify a peer on the same workspace, including a running worker | An ACP advisory arrives before the next prompt. An ACP interrupt can cancel the current turn. A hooked host handles an interrupt at a tool-call boundary. |

Task resume restores workflow state. Each ACP attempt still opens a fresh
agent session. Worker addresses do not indicate presence, and a queued signal
can expire before the recipient reads it. Native interception also varies by
host. See the [host capability table](HOST-CAPABILITIES.md) before depending on
a hook to deliver an advisory or replace output.
