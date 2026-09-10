# Harness collaboration: where straitjacket stands, and what is missing

straitjacket's premise is that you keep your coding agent. That makes the
harness boundary the most important interface in the project, and it is worth
being precise about what that boundary can and cannot carry today.

This document is the audit and the ranked backlog. Two items on it have landed
(§1, and §5 on the one transport where it is honest); the rest are stated as
gaps with the mechanism each would need, so that nothing here reads as a
capability that exists.

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
- **It states its latency instead of hiding it.** On a hooked host, delivery is
  at the next hook boundary and an interrupt stops the next *tool call*, not a
  token stream. There is no push, and `stream_rules.py` already said so — a
  feature that quietly contradicted it would have been worse than no feature.
  The one exception is an ACP worker, whose subprocess ctx owns; §5.
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
means a peer harness cannot be *kept warm*. The relay now reaches into a
running ACP worker — its prompt carries queued reports and a queued interrupt
stops its turn — but every attempt still starts from nothing. ACP resume is
what would make a follow-up possible. The two compose: a resumable worker that
drains the relay each turn is a peer you can hold a conversation with, and
neither half is sufficient alone.

## 5. Mid-turn interruption, where the stream is ours — **landed for ACP**

`stream_rules.py` is the transport-neutral state machine for callers that own
their stream, and it is honest that hook hosts are not among them. ACP workers
*are*: ctx spawns the subprocess, polls a cancellation source on every wait
iteration, and already sends `session/cancel` on teardown.

That is now wired. A queued interrupt becomes the cancellation source for an
ACP worker, so the turn is cut off while it is running rather than at the next
tool call, and the failure names the peer and the address. Queued reports reach
the same worker through its prompt, since it has no `additionalContext`
channel. Both are opt-in per worker: the semantic analysis worker stays
unreachable by design, running with no tools over frozen evidence.

The queue did not change; only the drain point did, which is what the design
predicted. What remains is the SDK-backed runner, which owns its stream for the
same reason and is not yet wired.

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
| 5 | mid-turn drain for owned streams | **ACP landed** | — | honest only on owned hosts; SDK runner remains |
| 4 | ACP session resume | gap | weeks | the structural one; unblocks warm peers |

The ordering is deliberate: the two cheap items (2, 3) both make the landed
mechanism more useful without changing it. Item 5 has now earned the stronger
claim on exactly the transport where it is true — an ACP worker really is
stopped mid-turn — and item 4 remains the structural one, because being able
to interrupt a worker is not the same as being able to keep one warm.
