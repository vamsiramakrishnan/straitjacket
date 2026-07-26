<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs-header.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs-header-light.svg" width="100%" alt="straitjacket documentation — use the harness, understand the architecture, verify the claims, and extend the evidence system.">
</picture>

[How it works](HOW-IT-WORKS.md) · [Getting started](GETTING-STARTED.md) · [Core concepts](CONCEPTS.md) · [Architecture path](#architecture-reading-path) · [Evaluation receipts](../evals/) · [Normative specs](../spec/)

</div>

straitjacket’s implementation is built around a simple promise: **unbounded output becomes immutable evidence plus a bounded, deterministic, addressable view.** The documentation explains how to operate that system, why each mechanism exists, and what evidence justified shipping it.

The project separates four kinds of truth:

| Source | What it tells you |
|---|---|
| [`docs/`](.) | design intent, conceptual model, and mechanism reasoning |
| [`spec/`](../spec/) | normative schemas and behavioural contracts |
| [`evals/`](../evals/) | measurements, fixtures, referees, and negative results |
| [`CHANGELOG.md`](../CHANGELOG.md) | what actually shipped and when |

A design document is not automatically current product behaviour. It may describe a mechanism before implementation, preserve a rejected direction, or explain why an earlier design changed. Use status labels literally and prefer the specifications plus changelog when compatibility matters.

## Choose your path

### Use straitjacket

Start with **[How it works](HOW-IT-WORKS.md)** (a ten-minute plain-language walkthrough), then **[Getting started](GETTING-STARTED.md)**.

You will learn how to:

- set up every agent CLI you have in one guided command;
- see what each host can actually enforce ([Host capabilities](HOST-CAPABILITIES.md)) —
  they do not all protect you equally;
- run one harnessed or ephemeral agent session;
- capture, inspect, search, and retrieve evidence;
- choose between `run`, `seq`, `eval`, `q`, and background jobs;
- interpret the session scorecard;
- split a task across models by cost and capability ([Routing](ROUTING.md));
- understand the current trust boundary.

### Understand the mental model

Read **[Core concepts](CONCEPTS.md)** before the longer architecture documents. It defines artifacts, handles, spans, digests, evidence graphs, contracts, delivery plans, coverage receipts, gates, reflexes, and policy epochs.

The shortest useful model is:

```text
complete evidence stays in the store
bounded context carries addresses
retrieval reconnects the two on demand
```

### Verify the claims

Read **[`evals/`](../evals/)**.

straitjacket does not treat mechanism plausibility as evidence. Important changes are paired with a named referee, frozen acceptance gates, and a receipt that records both positive and negative findings. Several shipped mechanisms exist specifically because an earlier design lost a live A/B.

### Extend the system

First identify which plane owns the change:

| Plane | Responsibility | Examples |
|---|---|---|
| **Safety** | hard, non-adaptive limits | path confinement, timeouts, redaction, quotas |
| **Execution** | running and capturing work | commands, sequences, jobs, host interception |
| **Derivation** | producing repository facts | outlines, symbols, references, change generations |
| **Evidence** | extracting typed findings | test failures, diagnostics, log templates, coverage |
| **Delivery** | selecting and rendering views | contracts, plans, budgets, deterministic digests |
| **Behaviour** | measuring agent response | retrieval landings, reruns, reflexes, policy epochs |

A new mechanism should fit one plane, reuse the existing evidence and delivery contracts, and name its acceptance referee before implementation.

Then find the code: **[ARCHITECTURE.md](ARCHITECTURE.md)** maps every module to
its plane with a "which file do I touch for X" table. To add a digest profile,
follow **[Writing a profile](WRITING-A-PROFILE.md)**; for setup, the invariants,
and how to run the tests and evals, see **[`CONTRIBUTING.md`](../CONTRIBUTING.md)**.

---

## Foundations already shipped

These documents explain mechanisms that have already crossed their acceptance gates.

| Document | The question it answers |
|---|---|
| **[PRICED CONTEXT](PRICED-CONTEXT.md)** | How should an agent decide whether to retrieve more evidence? Put the token price at the decision point, relate it to the remaining window, and offer the cheaper continuation. |
| **[LOSSLESS RESCUE](LOSSLESS-RESCUE.md)** | How can an already-bloated session be reduced without repeating the destructive behaviour of transcript rewriting? Elide resident bytes only after preserving them behind stable addresses. |

## Architecture reading path

The current architecture sequence began with a measured failure: a regime where the harness obeyed its containment rules but still made the agent worse. The documents follow the diagnosis in order.

### 1. [LADDERS — conditionality must be measured](LADDERS.md)

A taxonomy of every tiered or conditional mechanism in the system. Its governing rule is blunt: **a conditional is only as good as the signal that controls it.** This document separates genuine adaptation from thresholds that merely look adaptive.

### 2. [REFLEX — close the loop around behaviour](REFLEX.md)

The open-loop ladders fired on output volume while the actual failure lived on the information axis. REFLEX defines interventions as hypotheses about the model’s next action, then scores the observed outcome: retrieval, narrowing, validation, equivalent rerun, workaround, or censored expiry.

### 3. [EDC — govern evidence delivery](EDC.md)

The Evidence Delivery Controller replaces profile-specific truncation logic with typed evidence, command-family contracts, one policy resolver, deterministic plans, and coverage receipts. It optimizes for keeping the required facts, and treats the token budget as the limit to fit them into.

### 4. [ALGEBRA — derive and compose evidence](ALGEBRA.md)

The EDC governs how evidence is delivered; ALGEBRA governs how it is produced and joined. Tree-sitter skeletons, opportunistic SCIP ingestion, a typed fact store, bounded query stages, and static × dynamic × temporal joins make repository investigation compositional without making it Turing-complete.

### 4b. [DIGEST-CLOSURE — compute on the compressed form](DIGEST-CLOSURE.md)

The closure audit of the algebra: which operators run at digest-rate (homomorphic over the representation) versus which rehydrate raw bytes. Closure turns out to be a total function of the `ctx q` type signature, and the single-refinement-boundary theorem — bytes materialize at most once, and only terminally — is a structural invariant of the kind graph, pinned in [`tests/test_digest_closure.py`](../tests/test_digest_closure.py).

### 4c. [THEORY — the objective, the theorems, the measured gap](THEORY.md)

The formalization in one page: the information-bottleneck objective with the lazy-lossless constraint, the two enforced theorems (determinism, single refinement boundary), the evidence-regret metric (`ctx replay --regret`) that scores every digest profile's distance from the rate–distortion frontier on real trajectories, and the honest ledger of which mechanisms are derived from the objective versus empirically adopted under it.

### 4d. [SUBSTRATE — operator classes under the semantic layers](SUBSTRATE.md)

The audit of the "just add Unix tools" instinct. Six proposed additions (fd,
rg --json, ctags, jq, comby, watchexec) examined against the shipped tree:
three already exist, one is rephrased to survive determinism, and the rest
become the M-K phases — a file-set algebra (`corpus`), a records algebra over
stored artifacts, span-precise sites, and a gated second rewrite rung. The
governing rule: every binary is an engine behind a logical operator, every
operator carries a contract, and no tool merges without a referee.

### 4e. [ASK — intents as typed plan presets](ASK.md)

The retrieval front door done without a natural-language parser. A repository
question becomes a frozen `ctx.plan/v1` template with typed slots
(`locate`/`impact`/`diagnose`), executed on the shipped plan tier and answered
with the investigate digest — collapsing the *decision cost* of exploration the
way evidence plans collapsed its *turn cost*. Includes the audit's cut list:
what an elegant system declines (the NL parser as primary path, a speculative
ontology, unscoped new verbs) matters as much as what it builds.

### 5. [EVIDENCE-PLANS — compile the investigation](EVIDENCE-PLANS.md)

The model compiles its exploration intent into a typed, total, bounded DAG (`ctx plan` / `ctx plan run`); the harness validates, prices, and executes it locally, and one causally organized digest returns. ast-grep and Semgrep join as physical operators behind logical ops; rounds go from O(operations) to O(hypothesis epochs). Shipped v0.25.0; measured in [`evals/plan-collapse-2026-07-19.md`](../evals/plan-collapse-2026-07-19.md).

---

## Status language

The docs use four explicit states:

- **Shipped** — implemented and covered by acceptance tests.
- **Shadow** — computes or records a decision without enforcing it.
- **Designed** — specified with a named referee, not yet implemented.
- **Rejected** — investigated and deliberately not adopted.

These labels are not decorative. They prevent architecture diagrams from collapsing intent, experiment, and product reality into one misleading picture.

## House rules for mechanisms

Every mechanism inherits the same invariants:

1. **Capture before flood.** Potentially unbounded bytes do not enter the transcript raw.
2. **Omission keeps an address.** Evidence may leave active context; it may not become unrecoverable.
3. **Coverage is declared.** A short digest is not a success if required identities disappear.
4. **Rendering is deterministic.** Same evidence, contract, and plan means identical bytes.
5. **Safety does not adapt.** Behavioural signals may tune delivery, never weaken hard limits.
6. **Degradation is labeled.** Optional precision may fail open; it may not pretend to be exact.
7. **Receipts precede doctrine.** A mechanism ships on measured behaviour, not aesthetic confidence.

## Documentation map

| Need | Read |
|---|---|
| the ten-minute overview | [HOW-IT-WORKS.md](HOW-IT-WORKS.md) |
| first successful session | [GETTING-STARTED.md](GETTING-STARTED.md) |
| vocabulary and invariants | [CONCEPTS.md](CONCEPTS.md) |
| every `ctx.toml` setting | [CONFIGURATION.md](CONFIGURATION.md) |
| when something breaks | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| the code map (which file for what) | [ARCHITECTURE.md](ARCHITECTURE.md) |
| retrieval economics | [PRICED-CONTEXT.md](PRICED-CONTEXT.md) |
| context rescue | [LOSSLESS-RESCUE.md](LOSSLESS-RESCUE.md) |
| conditional mechanisms | [LADDERS.md](LADDERS.md) |
| closed-loop adaptation | [REFLEX.md](REFLEX.md) |
| evidence contracts and plans | [EDC.md](EDC.md) |
| compute on the compressed form | [DIGEST-CLOSURE.md](DIGEST-CLOSURE.md) |
| the objective, theorems, and the measured gap | [THEORY.md](THEORY.md) |
| facts, indexing, and queries | [ALGEBRA.md](ALGEBRA.md) |
| compiled evidence plans | [EVIDENCE-PLANS.md](EVIDENCE-PLANS.md) |
| the input side — capability surface containment | [CAPABILITY-SURFACE.md](CAPABILITY-SURFACE.md) |
| schemas and compatibility | [`spec/`](../spec/) |
| benchmark receipts | [`evals/`](../evals/) |
| shipped history | [`CHANGELOG.md`](../CHANGELOG.md) |
| unshipped mechanisms | [`ROADMAP.md`](../ROADMAP.md) |


<!-- docs-phase2:start -->
## Practical guides

| Guide | Use it when |
|---|---|
| [Use cases](USE-CASES.md) | You know the task or failure mode and want the shortest path through the harness. |
| [CLI guide](CLI.md) | You need to choose a verb, retrieve evidence, or interpret a scorecard. |
| [Configuration](CONFIGURATION.md) | You want to tune budgets, the guard, scopes, or redaction in `ctx.toml`. |
| [Troubleshooting & FAQ](TROUBLESHOOTING.md) | Something isn't working, or you have a "does it…?" question. |
| [Writing an evidence profile](WRITING-A-PROFILE.md) | You are extending extraction, contracts, or rendering. |
| [Why straitjacket](WHY-STRAITJACKET.md) | You want the context-cost, cache, latency, and quality thesis in one place. |
| [Comparisons](COMPARISONS.md) | You want the head-to-head data versus Headroom, rtk, Ponytail, Caveman, Maki, and the rest of the field. |
<!-- docs-phase2:end -->

---

<div align="center">

<sub><a href="../README.md">« repository</a> · <a href="../spec/">specifications</a> · <a href="../evals/">evaluation receipts</a> · <a href="../ROADMAP.md">roadmap</a></sub>

</div>
