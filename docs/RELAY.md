# The cross-harness relay

`ctx relay` is the direction the task ledger never had.

The ledger ([TASK-LEDGER.md](TASK-LEDGER.md)) lets harnesses share a *record*:
they append rows, and whoever reads next finds out. It works because nobody has
to be listening. It also means nobody is ever **told**. Every ctx→harness data
flow in this project is a return value on a call the harness made — a hook
subprocess, an MCP tool call, a CLI invocation. Nothing in the system can start
a conversation.

Three things break as a result:

- `ctx run --bg` hands back a job handle and a supervisor. When the job
  finishes, nothing says so. An agent that forgets to poll `ctx job` never
  learns the build broke.
- Two harnesses on one workspace cannot tell each other anything. The second
  one re-derives what the first already paid for.
- Nothing can stop a harness that is confidently walking into a wall somebody
  else already found.

The relay closes those, and is explicit about the bound it closes them within.

## What it does not do

**There is no push, and there is no mid-stream interruption.** A PreToolUse
hook cannot observe assistant tokens; `src/ctx/stream_rules.py` says so, and
this feature does not quietly claim otherwise.

What the relay does is convert *polling* into *delivery at the next boundary
the harness already crosses*:

| delivery point | what drains there | on which hosts |
|---|---|---|
| `session-start` | everything pending, as session advisory context | claude, codex |
| `pre-invocation` | everything pending, before every model call | antigravity |
| `post-tool-use` | `report` and `advise`, as additional context | claude, codex |
| `post-tool-use` | `report` and `advise`, appended to the replaced output | hermes, omp, opencode, dsh — only when there *is* a replacement |
| `pre-tool-use` | `interrupt`, as a stop on the next tool call | all seven |

That last column is not decoration. **A drain marks its signals delivered**, so
draining on a host that cannot carry the text would consume the message and
throw it away — the one failure this channel exists to prevent. The drain is
therefore gated on there being a real channel on that flavor, and anything
pending simply waits for a stage that has one. Antigravity's PostToolUse has
exactly one legal output (`{}`), so nothing is ever drained there; the native
hosts have no `additionalContext` at all, so the advisory rides along with the
substituted output or waits.

So the latency of a signal is **one hook boundary**: the next tool call, or the
next turn. An interrupt lands at a **tool-call boundary**, not mid-token. That
bound is part of the contract, and `ctx relay status` prints it rather than
leaving you to infer it. A host that owns its own stream — an ACP worker, the
SDK-backed runner — can drain the same queue earlier without changing anything.

## Addresses, never content

A signal carries a **ref** and an optional bounded **note**. The ref is
validated by the same closed grammar `ctx task send` uses: one reference
(`run:`, `checkpoint:`, `blob:`, `repo:…`) plus `ctx get` options, nothing else.

```console
$ ctx relay signal claude "the tests failed with an assertion error" --note x
ctx relay: inbox ref must be an address: unrecognized reference 'the'
```

That refusal is the feature. The relay cannot become a way to push another
harness's output into your prompt. The receiving agent resolves the address
itself, under its own permissions, and pays for exactly the bytes it reads.

## The rows

Three schemas, one append-only file at
`.ctx-session-reads/relay/relay.jsonl`, written under `flock` with the same
torn-line repair the task ledger uses.

```
ctx.watch/v1      a subscription: who wants to hear about what
ctx.signal/v1     a queued delivery: an address, a bounded note, a deadline
ctx.delivery/v1   the receipt: which signal reached whom, at which stage, when
```

Delivery receipts are what make draining exactly-once without a mutable cursor:
a signal is pending while no delivery row exists for it. Matching on the
*signal* rather than on the subscriber string is deliberate — `claude` and
`claude:s1` are two addresses for the same reader, so a per-string key would
re-deliver every host-addressed signal to the next session of that host. A
broadcast to `*` is the one case that genuinely needs a copy each, and keeps
the per-subscriber key. Two processes draining at once cannot lose a signal
between them — at worst one is
delivered twice, and a duplicate advisory is a much better failure than a
dropped one.

## Who you are

A subscriber is `<host>` or `<host>:<session>`.

- `claude` addresses every Claude Code session on this workspace. Right for
  "the build finished."
- `claude:0f1e2d` addresses one. Right for "here is the answer to what you
  asked."

## The subscriber picks the kind, not the publisher

A watch declares what a matching event should *do* to you:

| kind | meaning | delivered at |
|---|---|---|
| `report` | something you were waiting on finished | post-tool-use, session start, pre-invocation |
| `advise` | a peer found something worth your attention | post-tool-use, session start, pre-invocation |
| `interrupt` | stop before your next tool call and read this | pre-tool-use |

So one job completion can be an advisory report to one harness and a hard stop
to another, decided by each of them rather than by whoever published.

## Nobody polls a background job any more

```console
$ ctx relay watch claude job --action report
watching job as claude → report
watch: watch-18d3de2e053146d1

$ ctx run --bg -- pytest -q
[ctx job:6ad7c74d725b backgrounded · running 0s · stdout 0 lines so far]
```

When the job ends, the supervisor spawns `ctx job <id> --announce`, which
finalizes the run and publishes its address. At the agent's very next tool
result:

```
[ctx relay · 1 signal]
  finished · job/6ad7c74d725b · from ctx-job
    resolve: ctx get run:7002e3b08857#stdout
    note: pytest -q — exit 1
```

The supervisor stays as dependency-free as it has always been: it never
resolves a workspace or opens the store, it shells out to a detached `ctx`. And
the decision to announce is made by the launcher at start time, so a workspace
where nobody is watching the `job` topic spawns no extra process at all.

## Stopping a peer

```console
$ ctx relay signal claude:0f1e2d checkpoint:d914ee702801 --interrupt \
    --origin codex --note "the migration schema changed under you"
interrupt queued for claude:0f1e2d · sig-18d3de5873986d7e
delivery: at that harness's next tool call (pre-tool-use)
```

That harness's next tool call comes back as a `force_ask` naming the address:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "ask",
  "permissionDecisionReason": "CTX_RELAY_INTERRUPT: another harness asked you to stop and read this before continuing.\n[ctx relay · 1 signal]\n  STOP · peer · from codex\n    resolve: ctx get checkpoint:d914ee702801\n    note: the migration schema changed under you"}}
```

`force_ask`, not `deny`: a peer harness is not a safety authority, and a wrong
interrupt should cost a confirmation rather than a hard refusal. Hosts whose
dialect has no ask (Codex) degrade it to a deny carrying the same reason.

Two rules keep this from becoming a hazard:

- **A guard `deny` outranks a relay interrupt.** The guard's own verdict wins;
  the relay never softens a refusal into an ask.
- **A `ctx` call is never blocked by an interrupt.** The interrupt is telling
  the agent to go read an address; denying the call that resolves it would
  deadlock the agent against the message. It is delivered, and allowed.

## From inside a harness

The same three operations are on the MCP tool, so an agent can use them without
shelling out:

```json
{"op": "relay_watch",   "options": {"subscriber": "claude", "topic": "job"}}
{"op": "relay_publish", "options": {"topic": "digest", "ref": "run:7002e3b08857#stdout",
                                    "note": "the failing build", "origin": "claude"}}
{"op": "relay_publish", "options": {"to": "codex", "ref": "checkpoint:d914ee702801",
                                    "interrupt": true, "note": "schema changed"}}
{"op": "relay_pending", "options": {"subscriber": "claude"}}
```

## Bounds

Every one is enforced on write *and* on render, because a relay that grows
without limit is a context leak with extra steps.

| bound | value | why |
|---|---|---|
| pending per subscriber | 32 | a subscriber that never drains cannot grow the queue; the newest is dropped, since the existing backlog says the same thing |
| signals per drain | 8 | one tool result must not turn into a mailbox |
| rendered advisory | 1400 chars | the delivered text is bounded like a digest |
| note | 200 chars | the same cap as an inbox note |
| ref | 256 chars | an address, never content |
| default TTL | 6 hours | a stale "the build finished" is noise |

`ctx relay gc` drops settled rows. The queue is a queue, not an archive — the
evidence itself lives in the store, behind the address the signal carried.

## Failure direction

The relay is advisory, and it is not allowed to become a new way for the loop
to break. The hook's drain is gated on the queue file existing (one
`os.path.exists` per tool call, no import), and every relay path in the hook is
wrapped so a corrupt, unreadable, or read-only queue degrades to **silence** —
never to a failed tool call and never to a changed guard decision. That is
covered by `tests/test_relay.py`, including a deliberately corrupted queue and
a torn mid-write row.

## Commands

```console
ctx relay watch <subscriber> <topic> [--selector S] [--action report|advise|interrupt]
ctx relay unwatch <watch-id>
ctx relay publish <topic> <ref> [--selector S] [--to SUB] [--note N] [--origin O]
ctx relay signal <to> <ref> [--interrupt] [--note N] [--origin O]
ctx relay drain <host> [--session S] [--peek]
ctx relay status
ctx relay gc
```

Topics are `job`, `task`, `digest`, `edit`, `peer`. A selector narrows within a
topic by prefix, so a watch on `("job", "")` hears about every job and one on
`("job", "job-1a2b")` hears about one.

## Design record

[ADR 008](../spec/adr/008-cross-harness-relay.md) records the decision, the
invariants, and what it deliberately does not unlock.
