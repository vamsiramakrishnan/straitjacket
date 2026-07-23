# Core concepts

<sub><a href="README.md">« straitjacket / docs</a></sub>

straitjacket is easiest to understand as an evidence-delivery system with a strict separation between **what happened**, **what is stored**, and **what the model is allowed to see at once**.

This page defines the vocabulary used across the CLI, specifications, architecture documents, and benchmark receipts. New here? Read [How it works](HOW-IT-WORKS.md) first for the plain-language walkthrough; this page is the reference you come back to.

## Quick glossary

Scan this first; the sections below explain each term in depth.

| Term | In one sentence |
|---|---|
| **Artifact** | An immutable stored result — command output, a file snapshot, a query result — the durable source a view is rendered from. |
| **Handle** | A stable reference to an artifact or a region of one, e.g. `run:ba3d1020ee8f#stdout`. |
| **Span** | An exact range inside an artifact: lines, bytes, a failure block, a symbol body. |
| **Digest** | The small, deterministic, bounded rendering of an artifact that the model actually sees. |
| **Profile** | The parser that knows a command family's shape (pytest, logs, JSON…) and extracts typed evidence from it. |
| **Evidence contract** | The declaration of what a command family must preserve (REQUIRED / ELASTIC / RETRIEVABLE). |
| **Delivery plan** | The resolved decision about how an artifact is shown right now, given the contract, budget, and pressure. |
| **Coverage receipt** | The digest's honest account of what it kept, what it omitted, and how to retrieve the rest. |
| **The four gates** | Birth, Entry, Residence, Emission — the four moments in a byte's life where the harness can act. |
| **Capture ladder** | The choice of the least-powerful verb that fits the work: native read → `run` → `--shell` → `seq` → `eval`, with `q` alongside. |
| **Reflex vs. policy epoch** | A within-session adjustment (fast loop) versus a reviewed, committed config change (slow loop). |
| **Determinism** | Same evidence + same contract + same plan → byte-identical digest, so caches stay warm and diffs are real signal. |

## The central distinction: evidence versus context

A command may produce megabytes of useful evidence. The model does not need all of those bytes resident in every later prompt.

straitjacket therefore separates:

- **Evidence** — the complete captured record of an operation.
- **Context** — the bounded view currently shown to the model.
- **Address** — a stable reference that reconnects the bounded view to exact evidence.

The store preserves evidence. The delivery layer decides what enters context. Addresses keep omission reversible.

```text
complete evidence ≠ active context

active context + addresses → complete evidence on demand
```

## Artifact

An artifact is an immutable stored result: command streams, a file snapshot, a derived census, a query result, or another piece of captured evidence.

Artifacts are content-addressed where the semantics allow it. Volatile details that should not affect identity—temporary paths, timings, terminal decoration, process identifiers—are quarantined from deterministic views.

An artifact is not necessarily shown to the model. It is the durable source from which bounded views are rendered.

## Handle

A handle identifies an artifact or a region within one:

```text
run:ba3d1020ee8f
run:ba3d1020ee8f#stdout
run:ba3d1020ee8f#stderr
blob:...
job:...
```

A stream fragment is `#stdout` or `#stderr`; an exact region within it is a
selector (`--lines`, `--span`, `--bytes`) or a span token minted by a digest.

Handles are the interchange format between model turns, sub-agents, commands, and future host adapters. A good claim cites a handle; a claim without resolvable evidence remains a hypothesis.

Current local handles provide stable addressing. Broker-era capability handles will additionally encode authority and become unforgeable across trust boundaries.

## Span

A span is an exact region within an artifact: lines, bytes, a failure block, a symbol body, or another typed range.

A digest can omit a large middle region while retaining a span that resolves it later. If the requested span is itself too large, retrieval returns a bounded zoom view with narrower spans. Retrieval therefore cannot become a new flood source.

## Digest

A digest is a deterministic, bounded rendering of evidence.

It should answer four questions:

1. What operation ran?
2. What are the most decision-relevant findings?
3. What coverage was achieved and what was omitted?
4. Which addresses recover the omitted evidence?

A digest is not a free-form summary. It is produced under a profile, evidence contract, and delivery plan. The same evidence under the same contract and plan must render to the same bytes.

## Profile

A profile understands the shape of an operation family:

- `pytest` failures and test identities;
- compiler diagnostics;
- lint findings;
- logs and recurring templates;
- search results;
- JSON or JSONL records;
- source outlines;
- generic text.

Profiles extract typed evidence rather than relying only on truncation. When a specialized profile cannot safely parse the result, the system degrades to a more general profile and labels the loss of precision.

## Evidence graph

An evidence graph is the typed representation produced by extraction. It separates facts from presentation.

Typical items include:

- test identity;
- failure class;
- diagnostic location;
- symbol declaration;
- changed file or generation;
- log template;
- source span;
- relation between a failure frame and a changed symbol.

Coverage is computed over these typed items before rendering. The renderer does not re-parse its own prose to decide whether it preserved the required facts.

## Evidence contract

An evidence contract declares what a command family must preserve:

- **REQUIRED** — identities or facts that may not disappear;
- **ELASTIC** — useful detail that may expand or contract with budget;
- **RETRIEVABLE** — evidence that may remain out of context only when a valid address exists.

The contract also declares loss severity and valid degradation behaviour. Coverage becomes the objective; token size is the constraint.

## Delivery plan

A delivery plan is the resolved decision about how an evidence graph will be shown in the current situation.

Inputs can include:

- the evidence contract;
- success or failure state;
- current context pressure;
- reader behaviour observed in the session;
- policy epoch;
- hard safety limits.

Plans have closed reason codes and emit receipts. Safety decisions are deliberately non-adaptive: no behavioural signal may weaken path confinement, timeouts, hard storage limits, or secret controls.

## Coverage receipt

A coverage receipt records what the digest preserved:

```text
identities: 37/37
inline detail: 5/37
retrievable detail: 32/37
unrepresented required facts: 0
```

This makes omission explicit and testable. “Shorter” is not automatically better; a digest wins only when it remains sufficient for the downstream task.

## The four gates

Every byte has four moments in its lifecycle.

### Gate 1 — Birth

**Question:** Can this operation flood at the source?

Capture commands, pipelines, sequences, computed programs, and long runners before their raw output enters the transcript.

### Gate 2 — Entry

**Question:** What actually crossed the host boundary?

The wire observer measures bytes and shapes from commands, MCP tools, web fetches, tasks, and other result channels.

### Gate 3 — Residence

**Question:** What may remain in active context, and for how long?

Session ledgers, window pressure, rescue, and checkpoints govern context lifecycle without deleting the underlying evidence.

### Gate 4 — Emission

**Question:** What should the model send back out?

The emission layer encourages citations over pasted payloads and bounded deliverables over accidental transcript recitation.

One artifact store serves all four gates.

## Capture ladder

The capture ladder moves from the least expressive operation to the most expressive:

```text
native read → run → shell → seq → eval
                         ↘ q
```

- **Native read** for output known to be small.
- **`ctx run`** for one potentially noisy command.
- **`ctx run --shell`** for shell syntax and pipelines.
- **`ctx seq`** for a declared sequence of steps.
- **`ctx eval`** for genuinely computed branching, looping, and aggregation.
- **`ctx q`** for bounded composition over typed repository and runtime facts.

The ladder is an economic and safety choice. More expressive operations cost more model-authored intent and have wider trust envelopes.

## Static, dynamic, and temporal facts

straitjacket’s repository model combines three planes:

- **Static:** what the code is—declarations, imports, references, signatures.
- **Dynamic:** what the code did—tests, failures, diagnostics, traces.
- **Temporal:** what changed and when—worktree generations, interventions, outcomes.

The useful queries live at their intersections:

```text
failure frame
  JOIN symbol range
  JOIN changed generation
  → failing code inside a symbol changed this generation
```

This is why the fact store is more than a code index and more than a test-result database.

## `ctx q` and totality

`ctx q` is a bounded pipeline algebra over typed record streams. It deliberately excludes unbounded loops, recursion, and arbitrary code execution.

That restriction buys:

- termination by construction;
- static cost bounds;
- operator-level provenance;
- safe exposure through constrained tool surfaces;
- compact model-authored intent.

Use `ctx eval` when the work truly needs general computation. Use `ctx q` when the work is evidence composition.

## Reflex and policy epoch

A **reflex** is a fast-loop adjustment within a session based on observed behaviour—for example, increasing detail after an equivalent rerun indicates that the first digest was insufficient.

A **policy epoch** is a reviewed slow-loop configuration compiled from receipts across sessions.

The distinction prevents a local behavioural guess from silently becoming global policy. Fast loops respond; slow loops learn under explicit review.

## Determinism

Determinism is not aesthetic. It controls:

- reproducibility of benchmark receipts;
- stable artifact identity;
- reliable diffing;
- prompt-cache prefix reuse;
- confidence that a policy change, rather than incidental output noise, caused a behavioural difference.

The invariant is:

> same evidence graph + same contract + same delivery plan → identical rendered bytes

## Fail-open versus fail-closed

straitjacket uses both, deliberately.

**Fail closed** when an operation could violate a hard safety or containment invariant: path escape, forbidden access, unbounded output crossing the gate.

**Fail open with a labeled degradation** when an optional precision mechanism is unavailable: tree-sitter absent, SCIP stale, specialized parser unable to classify a result.

Availability should not masquerade as precision, and precision should not weaken safety.

## Receipt

A receipt is a durable explanation of a mechanism decision or benchmark result. Examples include:

- the selected delivery plan and reason;
- bytes captured versus bytes shown;
- a retrieval landing after an intervention;
- a repeated command classified as equivalent;
- a benchmark gate and its measured verdict.

A mechanism is adopted because measured behaviour supports it, not because the design sounds plausible.

## The one-sentence model

straitjacket moves deterministic evidence work out of the probabilistic model loop, keeps the full result addressable, and admits only a bounded, reproducible view into active context.
