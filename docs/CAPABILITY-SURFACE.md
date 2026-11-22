# Choose which skills and tools a harness sees

A skill gives a coding agent instructions. An MCP server gives it callable
tools. A delegated task gives a worker a goal. Each enters the session through
a different interface.

| What you need | How to do it |
|---|---|
| See which skills contribute to the prompt | Run `ctx surface inventory --json`. For a recognized skill listing, `tokens` estimates the name and description. `deferred_tokens` estimates the body loaded on use. |
| Find a missing tool named by a skill | Run `ctx surface graph`. A reference such as `mcp__github__create_pull_request` points to the required server. A missing provider appears under broken dependencies. |
| Reduce the skill descriptions Claude Code carries between turns | Launch with `ctx wrap claude --surface auto -- …`. Skills with no observed use can keep just their names in the listing. |
| Give another harness work that requires a skill | Make the skill available to that harness and name it in the node's goal. The receiving harness decides whether to invoke it. [Route and delegate a task](HARNESS-COLLABORATION.md). |

## Route a task that needs a skill

In a `ctx.route/v1` graph, `role`, `needs`, and `min_tier` describe the
worker the task requires. A `host` or `model` pin requests a particular
worker. The route has no skill selector. ACP starts a worker in the specified
workspace and supplies a prompt and session-scoped MCP tools. Skills follow the
receiving harness's own installation and discovery rules.

For example, suppose a review worker needs your project's release checklist.
Install that skill on the worker's host and name the checklist in its goal.
If every worker must follow the same revision, commit the checklist to the
workspace and put its `repo:` path and expected checks in the goal. The worker
can read that file directly through bounded retrieval. [Worker selection](ROUTING.md)
and [ACP sessions](ACP.md) describe the handoff.

Straitjacket's own Antigravity skill uses the same distinction. It points the
agent to the [command routing policy](../plugins/antigravity/skills/ctx-harness/references/routing-policy.md)
when deciding whether an operation needs bounded output, and to the
[collaboration instructions](../plugins/antigravity/skills/ctx-harness/references/harness-collaboration.md)
when splitting work across workers. The host loads each reference when needed.

## Inspect the workspace

```bash
ctx surface inventory --json
ctx surface audit
ctx surface graph
ctx surface trim
```

The inventory covers project MCP registrations, repository instructions, agent
definitions, and skill files under `.claude/skills/`, `.agents/**/skills/`,
and `.cursor/rules/`. It also reads user-level Claude Code skills and agents,
Claude Code local and user MCP registrations, and Codex user MCP configuration.
Built-in host prompts and native tool schemas are outside its view.

Use an ID from the inventory with `ctx surface explain <id>` to inspect its
source, estimated size and authority. Recognized `SKILL.md` listings and their
reference files are priced separately. A reference file contributes zero
up-front tokens and carries a deferred estimate. For MCP tools, run
`ctx surface inventory --probe-mcp` to start configured stdio servers and
measure their advertised schema sizes. Counts use an estimate of about four
bytes per token. The host's disclosure mode determines how much of a schema
appears up front.

The audit flags overlapping names, unused high-authority tools and references
to unavailable servers. It infers authority from tool names and descriptions.
Skills and instructions carry no execution authority of their own.
`ctx surface trim` previews what could be deferred. `ctx prune` can compile
a selection into host-specific MCP configuration. [CLI reference](CLI.md).

## Narrow the Claude Code skill listing

From the project where you use Claude Code, launch a session with a task:

```bash
ctx wrap claude --surface auto -- -p "Locate the parser entry point."
```

This starts Claude Code using its configured account. The launch plan reads
this project's transcripts. After at least five complete sessions by default,
a skill with zero observed invocations is eligible for a `name-only`
override. Its description leaves the listing and its name stays available.
Used skills, new skills added since the latest transcript, the `ctx-harness`
skill and existing user overrides keep their settings.

Read `.ctx-surface/wrap-receipt.json` to see each skill's decision and reason,
the transcript coverage, and estimated listing sizes before and after the
change. The wrapper passes overrides through temporary settings for that
session. Set `[surface] wrap_min_sessions` to change the evidence floor or
pass `--surface off` to skip planning for one launch. This skill-listing
policy applies to Claude Code. [Launch flags and caller overrides](CLI.md#shrink-the-launch-surface---surface).

## Limit MCP tools at launch

```bash
ctx surface compile --profile read-only --host claude
ctx surface compile --profile local-dev --host codex
ctx surface compile --profile review --host antigravity
```

These commands preview the servers and tools a profile selects. Add
`--apply` to write launch configuration under `.ctx-surface/`. The report
names excluded servers and any problems with dependencies, authority or
schema budget. Built-in profiles are `read-only`, `local-dev`, `review` and
`full`. Define additional ones in `[surface.profiles.<name>]` in `ctx.toml`.
[Profile configuration](CONFIGURATION.md).

For a Claude Code session, `ctx wrap claude --surface read-only -- …` applies
the chosen profile at launch. A CLI profile can limit individual tools on a
stdio server through a gateway allow-list built from cached tool schemas.
When the wrapper cannot enforce that restriction, it omits the server and
records the reason. Explicit caller settings take precedence. The default
`auto` mode uses Claude Code's own MCP tool deferral.

The startup hook can also warn when estimated up-front context exceeds
`[surface] max_static_tokens` (8,000 by default). Claude Code and Codex receive
the advisory at session start. On Antigravity it uses `SessionStart` where
supported and `PreInvocation` on older hook contracts.
[Host capabilities](HOST-CAPABILITIES.md).

## Reveal MCP tools on demand

`ctx surface install-gateway --host claude` previews the gateway configuration.
Add `--apply` to write it. Point the host at the generated configuration and
launch with the gateway as its MCP server. The model can search the gateway's
index with `surface_index`, reveal a family, server or tool with
`surface_reveal`, and remove it from the listing with `surface_hide`.
The core bounded retrieval family stays visible.

| Host | When the client sees a gateway change |
|---|---|
| Claude Code | The normal tool list refreshes on `list_changed`. Reconnect to refresh the deferred ToolSearch index. |
| Codex | Restart the session to load a new tool list. |
| Antigravity | Use MCP Refresh to load a new tool list. |

`ctx surface reconcile --intent "open a PR"` records recommended changes.
Pass `--enforce` to apply the recommendations to gateway state.
`ctx surface referee` scores previous hide recommendations against later use.

A test against two live reference MCP servers illustrates the distinction.
Both supplied read tools, so the read-only profile kept both servers. Dropping
whole servers and other excluded listings reduced the estimated surface by
99 tokens. Filtering out 17 individual tools reduced it by 1,654 tokens on
the same inventory. Those figures describe the listed schemas in that run.
[Measurement and method](../evals/surface-real-2026-07-20.md).
