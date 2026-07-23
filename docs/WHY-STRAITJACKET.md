# Why straitjacket

Coding agents do not just consume context once. They carry prior tool output through a
probabilistic control loop, turn after turn. That makes context management a
cumulative-cost problem across tokens, cache behavior, turns, latency, and decision
quality.

straitjacket changes how the interaction is stored: the complete evidence goes into an
immutable, addressable store, and the transcript keeps only a bounded index into it.

The immediate benefit is smaller prompts. The deeper benefit is fewer expensive
crossings between deterministic computation and probabilistic reasoning.

## The naive loop is cumulative

Let:

- $R$ be the number of model reasoning rounds;
- $P_0$ be the fixed prompt prefix;
- $O_i$ be tool output introduced after round $i$.

Without perfect cache reuse, total model-visible input is approximately:

$$
C_{input} \approx R P_0 + \sum_{i=1}^{R}(R-i)O_i
$$

If outputs have average size $\bar O$, the growing-history term approaches:

$$
O(R^2 \bar O)
$$

The expensive mistake is not only printing 100,000 tokens once. It is introducing those
tokens early enough that later rounds repeatedly inherit them.

Compaction changes the failure mode rather than solving it: the context becomes smaller
by deleting evidence without a durable route back to the exact source.

## Bounded digests reduce the inner term

straitjacket captures every raw byte and replaces each unbounded output with a digest
whose visible size is bounded by $B$:

$$
C_{bounded} = O(RP_0 + R^2B), \quad B \ll \bar O
$$

This is a large improvement, but interactive exploration can still require many model
rounds. Bounded output removes payload growth; it does not automatically remove control
loop growth.

## Compiled evidence work reduces boundary crossings

Many exploration steps are deterministic fan-out:

```text
search names → list candidates → inspect signatures → find callers → run tests → join results
```

The model should specify the epistemic objective. A local evidence program can schedule,
parallelize, cache, deduplicate, and join the operations beside the repository, then
return one bounded result.

Let $H$ be the number of genuine hypothesis transitions. A well-planned workflow aims
for model rounds proportional to $H$, not the number of shell commands $M$:

$$
C_{planned} = O(H(P + D))
$$

where $D$ is the decision-oriented digest for one hypothesis epoch and usually
$H \ll M$.

The right rule is not “plan everything once.” It is:

> Batch deterministic work within an epistemic epoch. Return to the model when evidence
> can materially change the next plan.

## Cost: local compute is cheaper than repeated inference

Repository parsing, AST search, database joins, and artifact indexing still consume
compute. The system objective is not minimum compute; it is minimum weighted cost:

$$
\min(\alpha C_{model} + \beta C_{latency} + \gamma C_{quality\ loss} + \delta C_{local})
$$

For current coding-agent economics, repeated inference, extra turns, and wrong decisions
usually dominate incremental local CPU. Content-keyed indexes make the local term cheaper
again across turns and sessions.

## Cache: reduce entropy, not only bytes

Prompt caching rewards stable prefixes. Interactive exploration injects volatile data:

- timings and temporary paths;
- nondeterministic result order;
- partial logs;
- repeated plans and summaries;
- tool-specific metadata;
- slightly different reruns.

A deterministic digest keeps the transcript closer to an append-only sequence of stable
bytes. Two equally short prompts are not equally cheap when one preserves the provider’s
cache prefix and the other diverges early.

This is why determinism is both a correctness property and an economic property.

## Turns: model latency surrounds cheap tools

A 100 ms local search can cost seconds when it requires another model turn to select,
parse, and react to it. Interactive execution approaches:

$$
T_{serial} = \sum_i T_{tool_i} + M T_{model}
$$

A local DAG approaches:

$$
T_{planned} = T_{plan} + T_{critical\ path} + T_{reason}
$$

Independent operators can run in parallel. More importantly, repeated model queueing,
prompt ingestion, inference, and tool selection disappear from the critical path.

## Quality: more context is not monotonic progress

Large context can lower quality through:

- attention dilution;
- stale or contradictory evidence;
- duplicate findings;
- anchoring on the first verbose failure;
- arbitrary salience created by output order;
- compaction that removes the causal line;
- premature commitment before the failure census is visible.

The useful quantity is evidence density:

$$
\rho = \frac{decision\text{-}relevant\ evidence}{model\text{-}visible\ evidence}
$$

But density alone can be gamed by dropping decisive evidence. The delivery objective is:

$$
quality \propto recall_{decisive} \times precision_{presented} \times coverage\ confidence
$$

That is why a straitjacket digest includes a census, exact addresses, declared omission,
and a coverage receipt—not merely a compressed summary.

## The architecture follows from the economics

The LLM should own:

- objective interpretation;
- hypothesis generation;
- uncertainty resolution;
- repair design;
- trade-offs and final judgment.

The harness should own:

- capture and process supervision;
- query planning and parallel scheduling;
- structural extraction and indexing;
- joins and deduplication;
- budgeting and deterministic rendering;
- provenance and coverage;
- artifact retention and retrieval.

Making the model act as planner, scheduler, parser, join engine, and state store is an
expensive category error.

## Why addresses instead of lossy compression

Lossy compression answers “what can be removed?” straitjacket asks a stricter question:

> What can leave the current view while keeping an exact, bounded route back?

An omission without an address is a permanent loss. An omission with a span is only
paging: the bytes are still in the store, one retrieval away. The transcript can stay
small without discarding the omitted evidence.

This is the architectural fault line between straitjacket and the neighbouring tools.
A rewriting proxy compresses *after* the bytes are already resident and discards the
original; source-side filters and terse-prompting styles cut earlier but still throw the
cut bytes away. straitjacket captures at the source into an addressable store and puts
only a bounded digest — plus a resolvable address for every omitted byte — on the wire.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch-light.svg" width="100%" alt="Two lanes. Top: an agent loop feeds a rewriting proxy that compresses messages and rewrites history on each call; the model sees a rewritten log and the quiet needle is silently dropped with no address. Bottom: straitjacket captures tool output at the birth gate into an immutable addressable store, sends the model a bounded digest, and ctx get resolves any omitted line by address.">
</picture>

The same divergence, seen across the whole field — each tile names a neighbour's one
good idea and the lossless form straitjacket adopted (the amber strip):

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/field-treemap.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/field-treemap-light.svg" width="100%" alt="A treemap of the field: Headroom, rtk, Caveman, Compaction, RAG/vectors, Ponytail, Maki and wozcode. Each tile names the tool's one good idea, its limitation, and the lossless form straitjacket adopted.">
</picture>

## What straitjacket is not

### Not agent memory

Memory systems decide what prior information to recall. straitjacket governs the birth,
residence, and delivery of evidence generated during tool use. It can support memory,
but its contract is narrower and more testable.

### Not a summarizer

A summary may be useful, but a summary alone does not prove coverage, preserve identity,
or resolve to exact source bytes.

### Not a sandbox—yet

Current capture constrains output, paths, and process handling within the agent’s existing
execution authority. Separate-identity broker isolation and capability authorization are
a distinct security boundary.

### Not a larger context window

A larger window raises the ceiling while preserving cumulative transcript growth. It
also increases the amount of evidence the model must discriminate. straitjacket changes
what occupies the window.

## The product-level metric

A containment claim is meaningful only at matched or better task success:

$$
S_{SJ} \geq S_{native} - \epsilon
$$

while reducing:

$$
tokens + \lambda_1 turns + \lambda_2 latency + \lambda_3 unresolved\ omissions
$$

The most honest public result is a Pareto frontier: success, tokens, turns, wall time,
re-execution, evidence recall, and false interventions—not one proprietary score.

## The stronger thesis

straitjacket began as output containment. Its broader goal is to minimize the crossings
between deterministic computation and probabilistic reasoning while keeping reversible
access to the evidence.

That goal unifies capture, typed evidence, the query algebra, compiled investigation
plans, auditable delegation, and future broker isolation. The transcript holds pointers
to evidence; the evidence itself stays in the store.

---

[Concepts](CONCEPTS.md) · [Use cases](USE-CASES.md) · [Architecture sequence](README.md) · [Evaluation receipts](../evals/)
