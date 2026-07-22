# Straitjacket documentation

Straitjacket is an artifact-backed context containment harness for coding agents. It captures potentially unbounded tool output, stores the complete evidence locally, and returns a bounded deterministic view with exact retrieval addresses.

This documentation is organized by task. Start with the page that matches what you need to do.

## Start here

| Goal | Read |
|---|---|
| Install Straitjacket and complete a first capture | [Getting started](GETTING-STARTED.md) |
| Understand the data flow and core terms | [How it works](HOW-IT-WORKS.md) |
| Choose a command and check its syntax | [CLI guide](CLI.md) |
| Start from a common task or failure mode | [Use cases](USE-CASES.md) |
| Learn the vocabulary and invariants | [Core concepts](CONCEPTS.md) |
| Understand the product and economic thesis | [Why Straitjacket](WHY-STRAITJACKET.md) |
| Extend extraction or rendering | [Writing an evidence profile](WRITING-A-PROFILE.md) |
| Verify a performance or quality claim | [`evals/`](../evals/) |
| Check a normative contract | [`spec/`](../spec/) |

## Documentation types

The repository separates different kinds of information so that design intent is not confused with current behavior.

| Location | Purpose | Use it for |
|---|---|---|
| [`README.md`](../README.md) | Product overview | The value proposition, first commands, and navigation |
| [`docs/`](.) | Guides and explanations | Learning, operating, and extending the system |
| [`spec/`](../spec/) | Normative contracts | Schemas, compatibility, invariants, and acceptance requirements |
| [`evals/`](../evals/) | Evaluation evidence | Workloads, referees, results, and negative findings |
| [`CHANGELOG.md`](../CHANGELOG.md) | Shipped history | Current version behavior and release-level changes |
| [`ROADMAP.md`](../ROADMAP.md) | Planned work | Designed mechanisms that have not shipped |

When sources disagree, use this order:

1. `spec/` for required behavior;
2. `CHANGELOG.md` for what shipped;
3. executable tests for enforced acceptance criteria;
4. design documents for rationale and alternatives.

A design document may describe a proposal, a rejected direction, or an implementation that later changed. Treat its status label literally.

## Product guides

### Getting started

[Getting started](GETTING-STARTED.md) covers:

- source installation;
- workspace setup for Antigravity, Claude Code, and Codex;
- the first `ctx run`, `ctx get`, and `ctx search` workflow;
- optional analysis engines;
- common setup problems.

### How it works

[How it works](HOW-IT-WORKS.md) follows one command through capture, storage, evidence extraction, digest rendering, and retrieval. Read it before the architecture papers if the terms `artifact`, `profile`, `digest`, `span`, and `handle` are new.

### CLI guide

[CLI guide](CLI.md) is the task-oriented command reference. It groups commands by setup, capture, retrieval, repository analysis, evidence composition, measurement, and lifecycle management.

### Use cases

[Use cases](USE-CASES.md) starts from the work rather than the mechanism: noisy test suites, repository exploration, long-running commands, large connector responses, verification, and delegated investigation.

## Architecture reading path

Read [Core concepts](CONCEPTS.md) first. Then choose the part of the system you are changing or evaluating.

| Document | Question |
|---|---|
| [CAPABILITY-SURFACE](CAPABILITY-SURFACE.md) | Which tools and schemas should enter the model's capability surface? |
| [LADDERS](LADDERS.md) | When should a conditional mechanism engage? |
| [REFLEX](REFLEX.md) | How are interventions evaluated against observed agent behavior? |
| [EDC](EDC.md) | How are typed evidence, coverage contracts, budgets, and delivery plans resolved? |
| [ALGEBRA](ALGEBRA.md) | How are repository facts derived, joined, and queried? |
| [DIGEST-CLOSURE](DIGEST-CLOSURE.md) | Which operations can execute on bounded representations without rehydrating bytes? |
| [EVIDENCE-PLANS](EVIDENCE-PLANS.md) | How are multi-step investigations compiled into one bounded execution plan? |
| [ASK](ASK.md) | How do typed intents compile repository questions into evidence plans? |
| [PRICED-CONTEXT](PRICED-CONTEXT.md) | How should retrieval cost be exposed at the decision point? |
| [LOSSLESS-RESCUE](LOSSLESS-RESCUE.md) | How can an already-large transcript be reduced without orphaning evidence? |
| [SUBSTRATE](SUBSTRATE.md) | Which physical engines sit behind the logical operator surface? |
| [THEORY](THEORY.md) | What objective and structural invariants organize the system? |

These documents preserve reasoning, trade-offs, and measured failures. They are not substitutes for the CLI guide or normative specifications.

## System planes

A mechanism should have one primary owner.

| Plane | Responsibility | Examples |
|---|---|---|
| Safety | Hard, non-adaptive constraints | Path confinement, redaction, quotas, timeouts |
| Execution | Running and capturing work | Commands, sequences, jobs, host interception |
| Derivation | Producing repository facts | Symbols, references, outlines, change generations |
| Evidence | Extracting typed findings | Test failures, diagnostics, templates, coverage |
| Delivery | Selecting and rendering views | Contracts, plans, budgets, deterministic digests |
| Behavior | Measuring agent response | Retrieval, reruns, interventions, policy epochs |

Behavioral measurements may change delivery policy. They must not weaken safety constraints.

## Status language

Architecture and roadmap documents use four states:

- **Shipped** — implemented and covered by acceptance tests.
- **Shadow** — records or scores a decision without enforcing it.
- **Designed** — specified with an acceptance referee, but not implemented.
- **Rejected** — investigated and deliberately not adopted.

Do not describe designed or shadow behavior as available product functionality.

## Documentation standards

Use [Documentation style](DOCUMENTATION-STYLE.md) when creating or revising a page. The guide defines terminology, page types, source-of-truth rules, command verification, and review checks.

The central rule is simple: explain one job per page, keep claims close to their evidence, and do not duplicate volatile facts such as versions or test counts across multiple documents.

---

[Repository](../README.md) · [Getting started](GETTING-STARTED.md) · [CLI](CLI.md) · [Specifications](../spec/) · [Evaluation receipts](../evals/)
