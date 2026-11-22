# Route a task across harnesses and models

`ctx orchestrate` assigns a **host and model to each part of a task**. A
review of one diff can run on one worker. A change that needs exploration,
implementation and verification can use different workers and pass a
checkpoint between them. The route shows which dependencies force work to run
in sequence and which jobs can run together.

```sh
ctx wrap detect
ctx orchestrate 'Run the named pytest test tests/test_acp.py' --dry-run
```

The preview prints the model menu, selected workers, dependencies and
estimated node costs. It runs locally. For a general task, the preview uses
the deterministic route. Running that task can ask a coordinator model to
choose a different graph. To execute, omit `--dry-run`. If your project sets
`[orchestrate] confirm = true`, pass `--run` to execute.

## How a task becomes a route

| Request shape | Deterministic route |
|---|---|
| Answer a question or inspect code | Answer |
| Run a named test | Verify |
| Review a diff | Review |
| Make an explicitly small edit | Implement → verify |
| Add a low-risk feature with a named source target, tests and a behavioral contract | Explore → implement → verify |
| General task | Explore → plan → implement → verify |

For the first five recognized shapes, Straitjacket skips the coordinator.
For general work, it asks the cheapest eligible coordinator to propose a
`ctx.route/v1` graph. If the coordinator is unavailable or supplies an invalid
graph, Straitjacket uses the deterministic route. A mutating node needs a
separate downstream verification node. Independent jobs may run in the same
wave. The [task ledger](TASK-LEDGER.md) records claims, attempts, results and
recovery decisions.

## Choose eligible workers

The router considers unattended workers. Claude Code, Codex and an installed
Antigravity SDK runner have native unattended paths. Configure an [ACP
endpoint](ACP.md) to make Claude Code, Codex, Antigravity, Hermes, Oh My Pi,
OpenCode or DeepSeek Harness available over stdio. An endpoint offers its
configured model and declared tier, and the launch checks the exact model ID
against the adapter's advertised options. The orchestrator can select the
Antigravity ACP endpoint automatically. The native `agy` CLI uses a
separate launch path. See
[Gemini model selection](NATIVE-AGY-SESSIONS.md#choose-a-gemini-model).

For automatic assignment the router applies these rules in order:

1. Filter installed candidates by the requested minimum tier
   (`economy`, `standard` or `frontier`).
2. Maximize the number of the node's `needs` tags covered by the model's
   declared roles. These tags describe work such as `search`, `edit` or
   `verify`. The worker controls the skills it loads.
3. Among equal matches, prefer lower output price, then lower input price.
   `"prefer": "strong"` instead favors the highest tier and then the flagship
   output price among equal role matches.
4. If no candidate meets the tier, select the strongest available tier and
   print **tier unmet** beside the assignment. Inspect this warning before
   relying on a lower-tier model for a difficult task.

The [model catalog](../src/ctx/data/model-catalog.json) records specialities,
latency and measured behavior with sources. A workspace `.ctx-catalog.json`
can add evidence for an operator choosing a pin. The deterministic picker
uses the roles declared on the host's model entry. Adding catalog specialities
does not change its assignments.

## Pinning a host or model

A coordinator can include a `host` in a node, or both `host` and `model` to
request an exact pair. Choose from the advertised menu. If the host is
unavailable, normal assignment resumes. If the host is eligible but the model
is missing, the router selects a model on that host. Check the assignment:
pins are preferences and do not bypass unattended eligibility.

```json
{
  "id": "verify",
  "goal": "Run tests/test_acp.py and cite the failing test if it fails",
  "role": "verify",
  "needs": ["test", "verify"],
  "min_tier": "economy",
  "host": "codex",
  "deps": ["implement"]
}
```

This is a node inside the coordinator's graph. Use the
[coordinator reference](../plugins/antigravity/skills/ctx-harness/references/harness-collaboration.md)
for the complete contract. For a cross-harness example, including when to use
a skill, see [Harness collaboration](HARNESS-COLLABORATION.md).

## Read the outcome and the cost together

Route estimates use the requested token counts and model price table. A task
with a configured spend budget reserves the estimated worker cost at claim
time. An admitted worker can still cost more than its estimate. Each executed
route appends a receipt to `.ctx-session-reads/route.jsonl`, including
attempts, elapsed time and observed usage where available. ACP supplies no
provider usage today. A receipt with `partial` or `unavailable` usage needs
provider records before you can compare total spend.

Use a completed acceptance check to compare routes. In the published
three-phase build, the split planner/implementer scored 98% at $7.04, versus
98% at $9.59 for the single flagship arm. A lower-priced implementation model
on another arm replayed 4.25M input tokens to produce 63k output tokens.
Those numbers describe that build. See the [iterative orchestrator
results](../evals/iterative-orchestrator-2026-07-25.md) and the
[Gemini tier comparison](../evals/antigravity-3.6-flash-vs-3.5-flash-lite-2026-07-25.md)
for the workloads and limits.
