# Documentation

[Project README](../README.md) · [Getting started](GETTING-STARTED.md) · [CLI](CLI.md) · [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md)

straitjacket stores complete evidence outside the model transcript. It gives the model a bounded deterministic view with exact retrieval addresses.

The documentation separates four kinds of truth:

| Source | Purpose |
|---|---|
| [`docs/`](.) | Explanations and design rationale |
| [`spec/`](../spec/) | Normative schemas and behavioural contracts |
| [`evals/`](../evals/) | Measurements, fixtures, and negative results |
| [`CHANGELOG.md`](../CHANGELOG.md) | Released behaviour |

A design document can describe shipped, experimental, rejected, or planned work. Check its status. Use the specifications and changelog when compatibility matters.

## Use the product

Read these in order:

1. [How it works](HOW-IT-WORKS.md) — the data path in five minutes.
2. [Getting started](GETTING-STARTED.md) — installation, host setup, and the first capture.
3. [CLI guide](CLI.md) — command selection and full syntax.
4. [Configuration](CONFIGURATION.md) — budgets, guard policy, storage, scopes, and redaction.
5. [Troubleshooting](TROUBLESHOOTING.md) — symptoms, causes, and fixes.

Useful operational references:

| Need | Read |
|---|---|
| Choose a workflow | [Use cases](USE-CASES.md) |
| Compare host enforcement | [Host capabilities](HOST-CAPABILITIES.md) |
| Understand handles and digests | [Core concepts](CONCEPTS.md) |
| See code ownership | [Architecture](ARCHITECTURE.md) |
| Add a digest profile | [Writing a profile](WRITING-A-PROFILE.md) |
| Draw evidence, not decoration | [Visual design](VISUAL-DESIGN.md) |
| Run a release | [Releasing](RELEASING.md) |

## Understand the design

The shortest model is:

```text
complete evidence stays in the store
bounded context carries addresses
retrieval reconnects them on demand
```

The design documents answer narrower questions:

| Document | Question |
|---|---|
| [Why straitjacket](WHY-STRAITJACKET.md) | Why is context containment an economic and correctness problem? |
| [Anchors](ANCHORS.md) | How can an address remain valid while a file changes? |
| [Priced context](PRICED-CONTEXT.md) | When should an agent retrieve more evidence? |
| [Lossless rescue](LOSSLESS-RESCUE.md) | How can an overloaded session shed resident bytes without losing evidence? |
| [Ladders](LADDERS.md) | Which conditional mechanisms exist, and what signal controls each one? |
| [Reflex](REFLEX.md) | How does the fast loop react to observed agent behaviour? |
| [EDC](EDC.md) | How are typed facts, contracts, budgets, and renderers composed? |
| [Algebra](ALGEBRA.md) | How are repository and runtime facts derived and joined? |
| [Digest closure](DIGEST-CLOSURE.md) | Which operators work without rehydrating raw bytes? |
| [Theory](THEORY.md) | What objective and invariants does the system enforce? |
| [Substrate](SUBSTRATE.md) | Which physical engines sit behind logical operators? |
| [Ask](ASK.md) | How do typed intents compile into bounded investigation plans? |
| [Evidence plans](EVIDENCE-PLANS.md) | How does a multi-step investigation run in one bounded local pass? |
| [Routing](ROUTING.md) | How is work allocated across hosts and models? |
| [Replacement surface](REPLACEMENT-SURFACE.md) | Which native host operations can be collapsed safely? |
| [Capability surface](CAPABILITY-SURFACE.md) | How is the input capability surface measured and constrained? |

AlphaEvolve-specific material is in [optimization](ALPHAEVOLVE-OPTIMIZATION.md) and [deployment](ALPHAEVOLVE-DEPLOYMENT.md). The active oh-my-pi integration design is in [OH-MY-PI-INTEGRATION.md](OH-MY-PI-INTEGRATION.md).

## Status terms

- **Shipped** — implemented and covered by acceptance tests.
- **Shadow** — records a decision but does not enforce it.
- **Designed** — specified with an evaluation gate, but not implemented.
- **Rejected** — investigated and deliberately not adopted.

These labels matter. Intent, experiment, and product behaviour are different things.

## Design rules

Every mechanism follows the same rules:

1. Capture potential floods before execution.
2. Keep an address for every omission.
3. Declare coverage.
4. Render deterministically.
5. Keep hard safety limits non-adaptive.
6. Label degraded precision.
7. Measure behaviour before promoting policy.

Start with [Architecture](ARCHITECTURE.md) before modifying the system. It maps each mechanism to its owner plane and source modules.
