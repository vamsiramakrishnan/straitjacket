---
layout: default
title: straitjacket documentation
description: Artifact-backed context containment for coding agents.
---

<p align="center">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs-header-light.svg" width="100%" alt="straitjacket documentation">
</p>

# Keep the evidence. Lose the transcript bloat.

**straitjacket is an artifact-backed context containment harness for coding agents.** It captures potentially unbounded tool output before it enters the model context, stores the complete bytes as immutable evidence, and returns a small deterministic digest with exact retrieval addresses.

The result is not lossy summarization. It is a different information architecture:

```text
raw output  →  immutable artifact  →  bounded digest  →  model context
                    ↑                         │
                    └──── exact retrieval ────┘
```

The transcript becomes an **index over evidence, never a warehouse of it**.

[Get started](GETTING-STARTED.md) · [Learn the model](CONCEPTS.md) · [Read the architecture](README.md) · [Inspect the evidence](../evals/) · [See the roadmap](../ROADMAP.md)

---

## Start with the job you need to do

### I want to use straitjacket

Start with **[Getting started](GETTING-STARTED.md)**. It covers the shortest path for Claude Code and Antigravity, the first commands worth learning, and how to verify that capture and retrieval are working.

### I want to understand the design

Read **[Core concepts](CONCEPTS.md)** first, then the architecture sequence:

1. **[LADDERS](LADDERS.md)** — why a conditional mechanism is only as good as the signal that controls it.
2. **[REFLEX](REFLEX.md)** — how straitjacket closes the loop around observed agent behaviour.
3. **[EDC](EDC.md)** — how typed evidence, contracts, and delivery plans turn coverage into the objective and size into the constraint.
4. **[ALGEBRA](ALGEBRA.md)** — how evidence is derived and composed across static, dynamic, and temporal facts.

### I want to verify the claims

Go directly to **[`evals/`](../evals/)**. Design claims are not treated as product truths until they survive a named referee. The benchmark receipts include fixtures, frozen gates, paired arms, and the failures that caused mechanisms to be revised or rejected.

### I want to extend the system

Use the architecture docs to identify the plane you are changing:

| Plane | Owns | Typical extension |
|---|---|---|
| **Execution** | commands, jobs, capture | a new capture surface or host adapter |
| **Derivation** | symbols, references, facts | a new indexer or fact producer |
| **Evidence** | extraction, coverage, contracts | a new command-family profile |
| **Delivery** | plans, budgets, rendering | a new deterministic renderer |
| **Behaviour** | interventions and outcomes | a new measured reflex |
| **Safety** | hard, non-adaptive limits | path, process, storage, or secret controls |

A mechanism belongs in one plane, inherits the system invariants, and ships only with a referee.

---

## The product in four guarantees

### 1. Bounded at birth

Potentially unbounded output is captured before it can flood the transcript. Small output may pass through whole; large output becomes a digest whose size is bounded independently of the original payload.

### 2. Reversible by address

Every omitted region retains a stable coordinate. `ctx get` and `ctx search` retrieve exact evidence without replaying the original command or reintroducing the entire payload.

### 3. Deterministic by construction

Volatile fields are quarantined, ordering is canonical, and identical evidence under the same contract and plan produces identical rendered bytes. This keeps receipts reproducible and prompt-cache prefixes stable.

### 4. Measured in use

Hooks, interventions, retrievals, reruns, and policy decisions emit receipts. Fast-loop behaviour adapts only from observed outcomes; slow-loop policy changes are compiled, reviewed, and committed.

---

## Choose the lightest sufficient operation

```text
small and statically bounded?  → native read
one noisy command?             → ctx run
shell pipeline?                → ctx run --shell
known sequence of steps?       → ctx seq
computed control flow?         → ctx eval
bounded evidence composition?  → ctx q
long-running work?             → add --bg-after / use ctx job
```

The goal is not to force every action through the most powerful verb. It is to move deterministic work out of the model loop while preserving exact evidence and provenance.

---

## What straitjacket is — and is not

**It is:**

- a source-side containment boundary for tool output;
- an immutable evidence store with bounded views;
- a repository-aware derivation and query layer;
- a measurement system for agent context economics;
- a host integration for Claude Code and Antigravity.

**It is not:**

- a vector database or conversational memory product;
- a semantic compressor that deletes unaddressable evidence;
- a general sandbox or privilege boundary yet;
- a replacement for the model’s reasoning;
- a promise that more compression is always better.

The current security contract is **output containment and path confinement**. Broker-grade process isolation and capability handles remain separate planned work; the docs distinguish shipped, shadow, and designed mechanisms explicitly.

---

## Documentation map

| Read this | When you need |
|---|---|
| [Getting started](GETTING-STARTED.md) | installation, first session, first retrieval |
| [Core concepts](CONCEPTS.md) | the vocabulary and invariants |
| [PRICED CONTEXT](PRICED-CONTEXT.md) | why retrieval choices expose their token price |
| [LOSSLESS RESCUE](LOSSLESS-RESCUE.md) | how an already-bloated session is reduced without orphaning evidence |
| [LADDERS](LADDERS.md) | conditionality and graduated engagement |
| [REFLEX](REFLEX.md) | interventions, outcomes, hysteresis, and closed loops |
| [EDC](EDC.md) | evidence graphs, contracts, plans, and coverage |
| [ALGEBRA](ALGEBRA.md) | skeletons, facts, bounded queries, and joins |
| [`spec/`](../spec/) | normative schemas and behavioural contracts |
| [`evals/`](../evals/) | benchmark receipts and negative results |
| [`CHANGELOG.md`](../CHANGELOG.md) | what shipped and when |
| [`ROADMAP.md`](../ROADMAP.md) | mechanisms not yet earned |

---

## A note on reading the design docs

The design documents preserve the reasoning that produced the implementation. They may describe an idea before it ships, record a rejected mechanism, or compare several candidate designs. Treat their status labels literally:

- **Shipped** — implemented and acceptance-tested.
- **Shadow** — observes or computes decisions without enforcing them.
- **Designed** — specified with a named referee, not yet implemented.
- **Rejected** — investigated and deliberately not adopted.

For current product behaviour, prefer `spec/` and the changelog. For why that behaviour exists, read the design sequence and its linked receipts.


<!-- docs-phase2:start -->
## Practical guides

| Guide | Use it when |
|---|---|
| [Use cases](USE-CASES.md) | You know the task or failure mode and want the shortest path through the harness. |
| [CLI guide](CLI.md) | You need to choose a verb, retrieve evidence, or interpret a scorecard. |
| [Writing an evidence profile](WRITING-A-PROFILE.md) | You are extending extraction, contracts, or rendering. |
| [Why Straitjacket](WHY-STRAITJACKET.md) | You want the context-cost, cache, latency, and quality thesis in one place. |
<!-- docs-phase2:end -->

---

<p align="center">
  <strong>Bytes become evidence. Evidence keeps an address. The model sees only what it needs.</strong>
</p>
