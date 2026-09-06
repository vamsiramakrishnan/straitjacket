# Documentation

[Project README](../README.md) · [Getting started](GETTING-STARTED.md) ·
[CLI](CLI.md) · [Configuration](CONFIGURATION.md) ·
[Troubleshooting](TROUBLESHOOTING.md)

straitjacket keeps large captured tool output outside a coding agent's prompt.
The agent receives a bounded deterministic digest and bounded routes back to
the captured evidence.

Handles address immutable stored bytes while retained. Model-visible retrieval
remains bounded and subject to the current redaction policy; when redaction
changes an exact-byte request, the response declares it.

## Start here

Read these pages in order:

1. [How it works](HOW-IT-WORKS.md) — one command through capture, storage,
   digesting, and retrieval.
2. [Getting started](GETTING-STARTED.md) — install the package, configure a
   host, and run the first capture.
3. [Use cases](USE-CASES.md) — choose the smallest useful command for the work.
4. [CLI guide](CLI.md) — syntax and behavior for the complete command surface.
5. [Troubleshooting](TROUBLESHOOTING.md) — symptoms, causes, and fixes.

The shortest model is:

```text
complete evidence stays in the local store
bounded context carries facts and addresses
retrieval returns bounded regions on demand
```

## Find the right page

| Need | Read |
|---|---|
| Assess the next harness improvements | [Harness Playbook application](HARNESS-PLAYBOOK.md) |
| Understand the product decision | [Why straitjacket](WHY-STRAITJACKET.md) |
| Configure budgets, storage, or redaction | [Configuration](CONFIGURATION.md) |
| Compare host enforcement | [Host capabilities](HOST-CAPABILITIES.md) |
| Understand handles, spans, and profiles | [Core concepts](CONCEPTS.md) |
| Apply, verify, and expand an observed edit | [Edit loop](EDIT-LOOP.md) |
| Understand content-stable repository addresses | [Anchors](ANCHORS.md) |
| Route work across hosts and models | [Routing](ROUTING.md) |
| Run or resume multi-agent work | [Task ledger](TASK-LEDGER.md) |
| Hand a frontier model's edit off to a cheaper one | [Prewalk](PREWALK.md) |
| See module ownership and data flow | [Architecture](ARCHITECTURE.md) |
| Add a typed digest | [Writing a profile](WRITING-A-PROFILE.md) |
| Inspect measurements and counterexamples | [Evaluation receipts](../evals/) |
| Check released behavior | [Changelog](../CHANGELOG.md) |

## Product truth and design notes

The repository contains different kinds of evidence:

| Source | Use it for |
|---|---|
| [`src/`](../src/) and [`tests/`](../tests/) | Current executable behavior |
| [`CHANGELOG.md`](../CHANGELOG.md) | Behavior released by version |
| [`spec/`](../spec/) | Draft target contracts and the original Antigravity design |
| [`evals/`](../evals/) | Methods, fixtures, raw records, wins, and losses |
| [`docs/`](.) | Explanation, operating guidance, and design work |

Not every design note is a compatibility promise. A page may describe shipped,
shadow, designed, or rejected work. When a page carries one of those labels,
read it as follows:

- **Shipped** — implemented and covered by acceptance tests.
- **Shadow** — records a decision but does not enforce it.
- **Designed** — specified with an evaluation gate, but not implemented.
- **Rejected** — investigated and deliberately not adopted.

An unlabelled design note is explanatory material, not proof that every
mechanism on the page ships. Use the CLI guide, host-capability matrix,
changelog, and tests when compatibility matters.

## Design library

The design notes answer narrower questions. They are useful after the operating
path is clear.

### Evidence and retrieval

- [Priced context](PRICED-CONTEXT.md) — when another retrieval is worth its
  prompt cost.
- [Lossless rescue](LOSSLESS-RESCUE.md) — freeing an overloaded transcript
  without orphaning evidence.
- [Digest closure](DIGEST-CLOSURE.md) — operations that work without
  rehydrating raw bytes.
- [Evidence plans](EVIDENCE-PLANS.md) — bounded multi-step investigations.

### Execution and policy

- [Ladders](LADDERS.md) — conditional mechanisms and the signals that control
  them.
- [Reflex](REFLEX.md) — steering from observed session behavior.
- [Routing](ROUTING.md) — allocating work across hosts and models.
- [Task ledger](TASK-LEDGER.md) — persisted multi-agent work, resume, recovery,
  and budget state.
- [Prewalk](PREWALK.md) — hand a frontier model's validated first edit off to
  a cheaper model, opt-in.
- [Replacement surface](REPLACEMENT-SURFACE.md) — transparent command
  substitution and its adoption limits.
- [Capability surface](CAPABILITY-SURFACE.md) — containing input schemas and MCP
  surface area.

### Internals

- [Theory](THEORY.md) — formal objective and invariants.
- [EDC](EDC.md) — facts, evidence contracts, and delivery plans.
- [Algebra](ALGEBRA.md) — deriving and joining repository and runtime facts.
- [Substrate](SUBSTRATE.md) — physical engines behind logical operators.
- [Ask](ASK.md) — typed intents that compile into bounded plans.

AlphaEvolve experiments remain in
[optimization](ALPHAEVOLVE-OPTIMIZATION.md) and
[deployment](ALPHAEVOLVE-DEPLOYMENT.md). The oh-my-pi integration is documented
in [OH-MY-PI-INTEGRATION.md](OH-MY-PI-INTEGRATION.md). These are specialist
design notes, not the product introduction.

## Design rules

New mechanisms should preserve the same small set of rules:

1. Capture potential floods before execution when the host allows it.
2. Keep an address for every omitted region in a captured digest.
3. Declare coverage and degradation.
4. Render deterministically within an enforced budget.
5. Keep hard safety limits non-adaptive.
6. Measure task outcomes, not only byte reduction.
7. Publish counterexamples when the native path wins.

Start with [Architecture](ARCHITECTURE.md) before changing the system. It maps
mechanisms to their owner modules and acceptance gates.
