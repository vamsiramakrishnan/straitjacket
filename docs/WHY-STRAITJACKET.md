# Why Straitjacket

Coding agents use tools to read files, run tests, inspect logs, query repositories, and call external systems. The results of those tools become part of the model context.

That creates a cumulative systems problem. A large result is not paid for only once. It can remain in later model inputs, compete with newer evidence, slow the session, and eventually be reduced by a compaction step that may not preserve an exact route back to the original detail.

Straitjacket changes where evidence lives:

> Store complete evidence outside the transcript. Put only a bounded, addressable view in model context.

The result is a smaller context surface without treating omitted evidence as disposable.

## The problem is context residency

A coding agent may need to inspect a large output once but reason over it for several turns.

Common examples include:

- a test suite with thousands of lines and several failures;
- a build that emits long progress logs before the final error;
- a repository search with many low-value matches;
- a cloud or MCP tool that returns a large structured payload;
- repeated before-and-after verification runs.

The raw result becomes resident in the conversation even when the model only needs a few identities, one anomaly, or a small exact region.

A larger context window raises the ceiling. It does not change this residency model.

## Why truncation is not enough

Truncation can make an output smaller, but it introduces three problems.

### Selection is often arbitrary

Head-only truncation favors startup output. Tail-only truncation can hide the first causal error. Keyword selection misses evidence that does not announce itself with words such as `ERROR` or `FAIL`.

### Coverage becomes unclear

A short excerpt does not tell the reader whether it contains every failure, every object identity, or only a convenient sample.

### Omission may become irreversible

Once the removed bytes have no stable address, the agent must rerun the operation or accept that the evidence is gone.

Straitjacket treats omission as a view decision, not a deletion decision.

## The storage model

```text
complete tool output
        │
        ▼
immutable local artifact
        │
        ├──> typed evidence ──> bounded digest ──> model context
        │
        └<── exact retrieval ───────────────────── ctx get / ctx search
```

The artifact store owns completeness. The digest owns relevance, coverage, and budget. Retrieval reconnects the two when more detail is required.

This separation enables four properties that ordinary truncation does not provide.

## 1. Bounded context

A digest has an explicit token budget that does not grow with the original payload.

The renderer can preserve a complete identity census while limiting detail. For example, a test digest can show every failing test name, include selected traceback evidence, and provide exact coordinates for the remaining tracebacks.

Small outputs may pass through unchanged. Containment should not add ceremony when the complete result is already bounded.

## 2. Reversible omission

Every omitted region retains a stable address.

```bash
ctx get run:<id>#stdout --lines 1280:1300
ctx search run:<id>#stdout "MissingTenantError"
```

The original command does not need to run again. The model can page in the exact evidence required for the current decision.

An omission with an address is a paging decision. An omission without an address is an irreversible quality bet.

## 3. Deterministic views

Straitjacket normalizes incidental variation before rendering model-visible output. Absolute paths, ANSI sequences, unstable ordering, temporary paths, and non-evidentiary timing noise should not cause two equivalent results to produce different digests.

Determinism matters for:

- reproducible evaluation;
- meaningful run comparison;
- stable prompt prefixes;
- content-keyed caching;
- debugging the harness itself.

Two short prompts are not operationally equivalent when one changes unpredictably on every run.

## 4. Fewer model-mediated operations

Bounded output solves payload growth. It does not automatically reduce the number of reasoning rounds.

Many repository workflows contain deterministic work:

```text
select files
search symbols
resolve references
run checks
join results
rank evidence
```

The model should define the objective and decide when evidence changes the hypothesis. Local code can schedule, parse, join, deduplicate, and render the deterministic steps.

Straitjacket therefore provides several execution shapes:

| Work | Operation |
|---|---|
| One command | `ctx run` |
| Known steps | `ctx seq` |
| Computed control flow | `ctx eval` |
| Typed evidence composition | `ctx q` |
| Validated investigation graph | `ctx plan` and `ctx investigate` |
| Typed repository question | `ctx ask` |

The operating rule is simple: batch deterministic work while the hypothesis is stable. Return to the model when the evidence can change the plan.

## Addresses instead of summaries

A summary can be useful, but it is not a sufficient evidence contract.

A summary alone does not guarantee:

- complete identity coverage;
- stable provenance;
- exact recovery;
- deterministic rendering;
- declared omission;
- compatibility across extractors.

A Straitjacket digest may contain summary text, but it also carries coverage and retrieval coordinates. The summary is a view over stored evidence, not the only surviving representation.

## How this differs from neighboring approaches

| Approach | Useful property | Limitation addressed by Straitjacket |
|---|---|---|
| Larger context windows | More capacity | Raw evidence still accumulates and competes for attention |
| Post-hoc compaction | Reclaims an already-large context | Exact omitted evidence may not remain addressable |
| Source-side filters | Prevent some floods early | Removed bytes may be discarded without provenance |
| Rewriting proxies | Can reduce resident history | Rewriting changes prior prompt bytes and may drop evidence |
| Vector or semantic memory | Supports probabilistic recall | Retrieval may not return exact source bytes or complete identities |
| Terse prompting | Reduces narration | It does not govern tool payloads or preserve omitted evidence |

Straitjacket's position is narrower: capture deterministic evidence at the source, retain it exactly, and expose bounded views with stable addresses.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch-light.svg" width="100%" alt="A rewriting proxy changes transcript history after output is resident. Straitjacket captures output at the source, stores it as immutable evidence, and sends a bounded digest with retrieval addresses.">
</picture>

Detailed comparisons and reproducible workloads belong in [`evals/`](../evals/), not in this explanation page.

## When Straitjacket helps most

The strongest use cases have three properties:

1. the operation can produce much more evidence than the model should retain;
2. the decisive detail is not known before the operation runs;
3. the evidence may be needed again after the first turn.

Examples include large test suites, operational logs, repository-wide analysis, structured connector payloads, long-running processes, and delegated investigations.

## When it may not help

Straitjacket should stay out of the way when:

- the complete result is already small;
- the task ends immediately after one bounded command;
- a simple local filter can produce the exact required answer with no loss of identity or provenance;
- the model needs the complete payload and it already fits the intended context budget.

Containment has overhead. The correct target is not maximum compression. It is the smallest reversible view that preserves task success.

## How success should be measured

A context system should be evaluated on more than token reduction.

Track:

- task success;
- decisive-evidence recall;
- visible tool-output tokens;
- model rounds;
- wall time;
- repeated operations;
- retrieval success;
- false interventions;
- unresolved omissions.

A smaller digest that causes the agent to miss the causal line is a regression.

Evaluation receipts in [`evals/`](../evals/) publish positive results, neutral regimes, and observed losses. Start with the [benchmark charter](../evals/BENCHMARK.md).

## What Straitjacket is not

### Not agent memory

Memory systems decide which prior information to recall. Straitjacket governs the capture, storage, and delivery of evidence produced during tool use.

### Not only a summarizer

Profiles may summarize, classify, or rank evidence. The product contract also requires bounds, coverage, determinism, and retrieval addresses.

### Not a general sandbox

Commands currently run with the authority of the invoking user. Output containment and repository-relative path controls do not provide separate-identity process isolation.

### Not a claim that less context is always better

Some tasks need complete local detail. Straitjacket preserves that detail in the artifact store and exposes it on demand rather than assuming that every byte belongs in every later model input.

## The product thesis

Straitjacket began as output containment. The broader design principle is:

> Minimize unnecessary model boundary crossings while preserving reversible access to deterministic evidence.

That principle connects capture, typed evidence, bounded retrieval, repository analysis, compiled investigations, delegated work, evaluation, and future broker isolation.

The transcript becomes a control surface over evidence, not the database where evidence must live.

---

[Documentation](README.md) · [How it works](HOW-IT-WORKS.md) · [Use cases](USE-CASES.md) · [Theory](THEORY.md) · [Evaluation receipts](../evals/)
