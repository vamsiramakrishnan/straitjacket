# Why straitjacket exists

Large tool results compete with the code, instructions, and decisions already
in an agent's context. Truncating a result can also remove the evidence needed
for the next step.

Straitjacket stores captured output and returns a bounded view with retrieval
addresses. Use it when retaining access to omitted evidence matters. Small,
complete native results may be cheaper and easier to use.

The measurements below distinguish a digest's properties from end-to-end task
success. They include cases where adding the harness increased cost.

## The failure

The pattern is easy to reproduce.

The field-needle fixture contains 20,001 log lines, one quiet target at line
14,238, and two loud `ERROR` controls. The raw output is 302,628
`o200k_base` tokens.

Passing the whole log preserves the line but floods the prompt.

Keeping only the head and tail reduces the output to 1,219 `o200k_base` tokens
and loses the quiet target.

straitjacket emits 531 `o200k_base` tokens, keeps the quiet target, and emits an
address for the omitted region. This fixture checks address emission; it does
not execute a retrieval round trip.

That result does not prove that an agent completes more tasks. It proves a
narrower property: the digest can remain bounded, retain this quiet target, and
name an omitted region at the same time.

The distinction matters because tool output has a longer life than the process
that produced it.

A test command may run once. Its output can remain in ten later prompts. A
second run then adds another copy. Eventually the host compacts the
conversation, after already carrying the bytes repeatedly.

The session gets the cost of preservation and the semantics of deletion.

## The fixes that did not hold

A larger context window delays the problem. It does not change the residence
cost or the eventual compaction boundary.

Head-and-tail truncation works when the useful evidence is near an edge. Logs,
JSON responses, compiler output, and search results make no such promise.

Filtering for `ERROR` works when the anomaly announces itself. The quiet line
in the fixture is an `INFO` line. It is unusual structurally, not lexically.

A language-model summary works when the summarizer already knows which fact
will matter on the next turn. It cannot prove what it omitted, and it cannot
recover the original bytes by itself.

Prompting the agent to redirect output and use `grep` can work.

In the [first Claude Code A/B](../evals/ab-claude-code-2026-07-17.md), the
native agent independently redirected a large log to a file, searched it, and
solved the task. It used 7 turns and $0.140. The first straitjacket path used 8
turns and $0.353.

The first run used the v0.1 deny-and-rerun design. A later v0.4 comparison used
transparent substitution and five repeats per arm (10 sessions); it found cost
parity within noise. Cache warmth and the mechanism change were bundled, so the
later result cannot assign the correction to either one. The original design
mistake was still concrete: denial spent another turn teaching the agent how to
rerun the command.

The shipped Claude Code path now rewrites recognized noisy commands
transparently. The Codex path implements and contract-tests the same behavior,
but still lacks a live CLI receipt. Antigravity has to deny and name the bounded
replacement because its published hook contract does not permit argument
replacement.

The lesson was not that agents cannot manage output themselves. Strong agents
often can.

The lesson was that manual restraint has no durable contract. The redirected
file has no stable identity, no coverage receipt, no bounded retrieval rule,
and no guarantee that another model will follow the same habit.

## The insight

Evidence and prompt need different lifetimes.

Evidence should remain complete for as long as policy retains it.

The prompt should contain only the facts needed for the current decision.

An address connects the two.

That changes the design question from:

> How aggressively can this output be compressed?

to:

> What may leave the current view while remaining addressable in retained
> evidence?

The decisive property is not the compression ratio. It is whether omission is
reversible.

This requires capture before the flood reaches the model. Compressing a
transcript after roughly 300,000 tokenizer-counted tokens have entered it is
already late.

It also requires bounded retrieval. A `get` operation that can paste the whole
artifact back into the prompt merely moves the flood to the next turn.

## What shipped

The package is `ctx-harness`. The command is `ctx`. It requires Python 3.11 or
newer.

```bash
python -m pip install --upgrade ctx-harness
cd your-repository
ctx setup
ctx doctor
ctx run -- pytest -q
```

`ctx run` captures stdout and stderr into a local content-addressed store.

Small results may pass through unchanged. Large results become typed digests.
Test output reports failed identities and locations. Log output reports
repeated families and rare lines. JSON output reports shape, counts, and
exceptional records.

The digest is produced locally. It does not require another model call.

The common path stays small:

```bash
# Search the stored output.
ctx search run:8d8335db6848#stdout "MissingTenantError"

# Retrieve one bounded region.
ctx get run:8d8335db6848#stdout --lines 1280:1300

# Compare two captured executions.
ctx diff run:8d8335db6848 run:5a67c9de0123
```

The model sees the outcome, a census of relevant facts, what was omitted, and
how to retrieve more.

Repository evidence has a second problem: files move while the agent edits
them.

A bare address such as `src/auth.py:40:52` may return different code after
lines are inserted above it. straitjacket can attach a content anchor:

```bash
ctx get repo:src/auth.py --lines 40:52@07407f1c
```

Resolution verifies the original position, relocates the same content if it
moved, and refuses if the content no longer exists.

For repository navigation, `ctx map`, `ctx def`, `ctx refs`, and `ctx callers`
return structure instead of raw search floods.

For deterministic fan-out, `ctx seq`, `ctx q`, and `ctx plan run` execute known
work beside the repository and return one bounded result. They do not try to
plan through uncertainty. The model regains control when evidence can change
the hypothesis.

## Where the boundary is

Host APIs determine how much can be enforced.

| Host | Before execution | After execution |
|---|---|---|
| Claude Code | Rewrites recognized noisy calls | Replaces oversized results |
| Codex | Implemented and contract-tested | Implemented and contract-tested; live CLI receipt pending |
| Antigravity CLI | Denies and names the bounded command | Cannot replace output |
| ctx-owned Antigravity SDK | Uses bounded tools directly | Uses bounded tools directly |

On Antigravity, a verbose connector result can still enter the transcript
because the host exposes no result-replacement field. Direct `ctx` operations
remain bounded. The full matrix is in
[Host capabilities](HOST-CAPABILITIES.md).

straitjacket is not a process sandbox.

Commands run with the invoking user's permissions. Capturing a destructive
command does not make it safe. Mutation approvals remain mutation approvals.

The store is local by default, outside the repository when the user-state
directory is writable. This reduces accidental commits and deletion. It is not
a security boundary against an agent with unrestricted access to the same user
account.

Host-hook reads of secret-bearing paths require an explicit permission step and
are excluded from automatic capture by default. A directly authorized
`ctx run` can still capture them. Model-visible output is deterministically
redacted by default, but redaction can be disabled explicitly. Raw captured
artifacts may still contain sensitive data. The artifact store is plaintext
today and should not be treated as an encrypted vault.

Thirty days is the default GC eligibility horizon; artifacts remain until
`ctx gc` runs. `ctx gc --retention-days N` can override the horizon, while pins
and active checkpoint leases survive collection.

Current local handles provide stable addressing while their artifacts are
retained. They are not authorization capabilities.

straitjacket is also not agent memory, a larger context window, or a
replacement for host compaction.

Most importantly, it does not promise that every task becomes cheaper.

Already-bounded output should usually stay native. On small tasks, harness
overhead can exceed the cost of the evidence it contains.

## The result that went backwards

The AgentBench dogfood run tested a navigation-heavy bug bash against this
repository.

Both arms received the same task prompt, fixture, requested tool list, and
40-turn cap. The comparison is plain Claude Code versus the full
`ctx wrap claude --proxy` intervention. The wrapper can inject guidance, expose
ctx tools, proxy traffic, and change effective native-tool availability. The
record's model field is null, so the resolved host-default model is not
auditable.

This was one mission and one repeat. Both arms reached the cap. The adapter
counts failing pytest nodes reproduced within the budget; it does not
independently adjudicate or deduplicate root-cause defects.

| Metric | Native | straitjacket |
|---|---:|---:|
| Failing test nodes reproduced | 8 | 5 |
| Reported turns | 41 | 41 |
| Total input tokens | 3,406,175 | 4,003,068 |
| Output tokens | 22,505 | 17,189 |
| Cache hit rate | 98.1% | 99.0% |
| Cost | $10.6755 | $13.7022 |
| Wall time | 703.9 s | 1,107.7 s |

Containment was active.

The human results receipt reports 13.3 MiB of raw output and 1.0 MiB emitted, a
12.9× reduction. Its roughly 3.2 million avoided-token figure is `ctx gain`'s
byte-based estimate, not a tokenizer count.

The wrapped arm still reproduced fewer failing test nodes, cost $3.03 more, and
took 404 seconds longer.

The human receipt also reports 210 `get` calls and 260 `run` calls. Those events
are not turns, and the experiment has no retrieval ablation. Retrieval is a
plausible contributor under the fixed cap, not an isolated cause.

The cache was also hot. Avoided cache reads were worth much less than avoided
uncached input.

`ctx gain` reported about $9.67 of input-priced savings. At the cache-read
price, the same avoided tokens were worth about $0.97. The estimate overstated
the economic benefit by roughly ten times in the exact regime where cache
performance was strongest.

The mechanism did what it claimed. It did not help this workload.

That result does not establish that containment is useless. It establishes a
narrower boundary: the wrapped arm lost this navigation-heavy N=1 run under a
fixed turn cap and a 98–99% hot cache. The design does not isolate containment,
retrieval behavior, and structural navigation from one another.

Session metrics are committed in
[`dogfood.json`](../evals/agentbench/results/dogfood.json). The containment byte
counters, event counts, and `ctx gain` estimates are in the human
[AgentBench results receipt](../evals/agentbench/RESULTS.md), not that JSON
record.

## Reproduce the receipts

The delivery-layer receipt is model-free and deterministic:

```bash
python -m pip install -e '.[dev]' headroom-ai tiktoken
python evals/field_needle.py
python evals/field_needle.py --json
```

The method, caveats, and machine record are in
[field-needle-2026-07-20.md](../evals/field-needle-2026-07-20.md) and
[`field-needle-record.json`](../evals/field-needle-record.json).

The content-anchor receipt is also model-free:

```bash
python evals/anchor_drift.py
python evals/anchor_drift.py --json
```

Across 1,920 post-edit resolutions, anchored addresses relocated the original
content 1,452 times, verified it in place twice, refused 466 times, and returned
the wrong content zero times.

The full receipt is in
[anchor-drift-2026-08-20.md](../evals/anchor-drift-2026-08-20.md).

Prove the AgentBench referee before paying for live sessions:

```bash
python evals/agentbench/validate.py --adapter canary
```

The validator checks four states for every fixture: baseline, gold fix, test
tampering, and source vandalism. The current canary referee passes 12 of 12
checks.

A live dogfood replay requires Claude Code credentials and spends real tokens:

```bash
DOGFOOD_REPO="$PWD" \
DOGFOOD_PYTHON="$(command -v python)" \
python evals/agentbench/harness.py \
  --adapter dogfood \
  --n 1 \
  --arms naive sj \
  --max-turns 40

python evals/agentbench/report.py \
  --results evals/agentbench/results
```

The report refuses records not marked as live provenance.

## What comes next

The current AgentBench `sj` arm is a bundled wrapper intervention, not an
output-containment ablation. With the default collapse policy it can disallow
native `Grep` and `Glob`, while also adding guidance, ctx tools, and the proxy.
The result therefore mixes two questions:

1. Does bounded output preserve enough evidence?
2. Does structural navigation reduce the number of searches and retrievals?

The next useful experiment needs a pinned model and at least three arms:

- native;
- containment-only, with `collapse = false` and
  `CTX_WRAP_NO_DISCIPLINE=1`;
- the full wrapper policy, including explicit code verbs.

It also needs a turn cap high enough for both paths to stop naturally and at
least three paired repeats. The report should separate completion, failing test
nodes, retrieval hops, uncached input, cache reads, output tokens, cost, and wall
time.

Until that run exists, the claim should remain narrow.

straitjacket helps when evidence is large, dispersed, repeated, or expected to
outlive the turn.

On small, already-bounded tasks, the native path may be better.

The goal is not the lowest token count. It is the lowest resident context that
preserves recoverable evidence.

---

[Getting started](GETTING-STARTED.md) · [How it works](HOW-IT-WORKS.md) ·
[Evaluation receipts](../evals/) · [Architecture](ARCHITECTURE.md)
