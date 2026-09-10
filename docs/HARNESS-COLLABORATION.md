# Harness collaboration: where straitjacket stands, and what is missing

straitjacket's premise is that you keep your coding agent. That makes the
harness boundary the most important interface in the project, and it is worth
being precise about what that boundary can and cannot carry today.

This document is the audit and the ranked backlog. One item on it has landed
(§1); the rest are stated as gaps with the mechanism each would need, so that
nothing here reads as a capability that exists.

## What exists today

Six distinct integration mechanisms, and they are not interchangeable.

| mechanism | hosts | what it can do |
|---|---|---|
| Hook interception | claude, codex, antigravity, hermes, omp, opencode, dsh | deny / rewrite a tool call; substitute a tool result |
| MCP server | hermes, omp, opencode, dsh (+ any MCP client) | bounded retrieval and edit tools |
| CLI wrapper | claude, codex, antigravity, … | ephemeral per-session config, then hand back |
| ACP worker | all seven | ctx-owned subprocess with a model endpoint |
| Observer proxy | claude | byte-exact traffic relay; window/usage ground truth |
| SDK agent | antigravity-sdk | containment inside the tool implementations |

And one shared record: the **task ledger**
([TASK-LEDGER.md](TASK-LEDGER.md)), an append-only JSONL of typed rows per
task. Its design statement is the important part — *"harnesses never address
each other; they append to the ledger and read from it"* — and the reason it
works is that nobody has to be listening.

That is also its limit.

## The shape of the problem

Every ctx→harness data flow in the system is a **return value on a call the
harness made**. A hook subprocess answers the hook. An MCP tool call answers
the tool call. `ctx get` answers the command. There is no path by which ctx
initiates anything.

The capability matrix is also asymmetric in a way the *agent* cannot see.
Antigravity's PostToolUse contract has exactly one legal output, so containment
there happens at PreToolUse or not at all. Codex has no "ask", so an ask
degrades to a deny. ctx knows all of this — `HostSpec.input_substitution` and
`output_substitution` are declared per host — and the model running inside that
host knows none of it.

## 1. No way to tell another harness anything — **landed**

The gap: a finished background job was invisible until somebody polled. Two
harnesses on one workspace could not exchange a finding. Nothing could stop an
agent walking into a wall a peer had already hit.

The fix is `ctx relay` ([RELAY.md](RELAY.md)): a workspace-scoped append-only
queue with subscriptions, signals and delivery receipts, drained at the hook
boundaries that already exist. Three things make it fit rather than bolt on:

- **It reuses the ledger's address rule.** A signal carries a ref and a bounded
  note, never content. The same closed grammar, so the two buses cannot drift
  into accepting different things.
- **It states its latency instead of hiding it.** Delivery is at the next hook
  boundary; an interrupt stops the next *tool call*, not a token stream. There
  is no push, and `stream_rules.py` already said so — a feature that quietly
  contradicted it would have been worse than no feature.
- **The subscriber picks the kind.** One job completion is an advisory report
  to one harness and a hard stop to another, decided by each of them.

Landed with it: background jobs announce themselves when somebody is watching,
and an expensive capture publishes its `run:` address on the `digest` topic —
the store was always shared, what was missing is that the peer knew.

## 2. The agent cannot see its own host's limits — **gap**

An agent on Antigravity should behave differently from one on Claude Code: its
tool results are never substituted, so volume discipline has to be voluntary.
Today nothing tells it. The `DIALECT_CAPS` table in `hook.py` and the
`HostSpec` flags in `hosts.py` are read by ctx and never surfaced to the model.

The mechanism already exists: SessionStart / PreInvocation returns
`additionalContext`. A three-line capability statement there — what this host
can substitute, what it cannot, and what that means for how much output to
produce — costs a handful of tokens once per session and is strictly better
than the agent guessing. This is the cheapest remaining item on the list.

## 3. No presence: nobody knows who else is live — **gap**

`ctx relay` introduces `<host>:<session>` as an address, which is the first
half. The second half is a registry: which harnesses are *currently* active on
this workspace, last seen when. A `ctx.presence/v1` row written at each
session-start drain would let `ctx relay status` show live peers rather than
just queued signals, and would let `ctx orchestrate` route a node to a harness
that is already running instead of spawning a fresh one.

The cost of not having it: an interrupt addressed to a host that went away sits
in the queue until its TTL, and `orchestrate` pays cold-start on every node.

## 4. ACP sessions are single-shot — **gap, and the structural one**

`docs/ACP.md` is explicit: *"Each ACP attempt creates a fresh session"*, and
session reload/resume are not implemented in the transport. Every attempt
re-establishes context from checkpoints.

This is the deepest limit on genuine multi-harness collaboration, because it
means a peer harness cannot be *kept warm*. The relay makes it possible to tell
a running harness something; ACP resume is what would make it possible to hand
one a follow-up. The two compose: a resumable ACP worker that drains the relay
at each turn boundary is a peer you can hold a conversation with, and neither
half is sufficient alone.

## 5. Mid-stream interruption, where the stream is ours — **gap**

`stream_rules.py` is already the transport-neutral state machine for callers
that own their stream, and it is honest that hook hosts are not among them. But
ACP workers and the SDK-backed runner *are* — its own docs call this
"eligible; transport wiring not implemented".

The relay's interrupt queue is the natural producer for it. A ctx-owned worker
could drain `pre-tool-use`-class signals between tokens rather than between
tool calls, giving a genuine mid-stream stop on exactly the hosts where the
claim would be true. The queue does not change; only the drain point does.

## 6. No back-pressure between peers — **gap**

`ctx proxy` measures real window pressure, and `proxywindow.json` holds it. A
peer harness approaching its context limit is exactly the sort of thing another
harness should know before handing it more work — and exactly the sort of thing
the relay's `advise` kind is shaped for. What is missing is the producer: a
threshold crossing in the proxy that publishes on a `pressure` topic.

This is the one item that would benefit from a topic the relay does not yet
have, so it is also the one that would tell us whether the topic vocabulary is
right.

## Ranked

| # | item | status | cost | why it ranks here |
|---|---|---|---|---|
| 1 | cross-harness relay | **landed** | — | unblocks 2, 5, 6 |
| 2 | host capability statement | gap | hours | cheapest; the agent is guessing today |
| 3 | presence registry | gap | days | makes the relay's addressing complete |
| 6 | pressure back-pressure | gap | days | first real test of the topic vocabulary |
| 5 | mid-stream drain for owned streams | gap | weeks | needs 1; honest only on owned hosts |
| 4 | ACP session resume | gap | weeks | the structural one; unblocks warm peers |

The ordering is deliberate: the two cheap items (2, 3) both make the landed
mechanism more useful without changing it, and the two expensive ones (4, 5)
are the ones that would let straitjacket claim something stronger than
"delivery at the next boundary" — which is a claim worth earning rather than
asserting.
