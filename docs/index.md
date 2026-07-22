---
layout: default
title: Straitjacket documentation
description: Artifact-backed context containment for coding agents.
---

<p align="center">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/docs-header-light.svg" width="100%" alt="Straitjacket documentation">
</p>

# Keep the evidence. Bound the context.

Straitjacket captures potentially unbounded tool output before it enters a coding agent's context. It stores the complete result locally and returns a small deterministic digest with exact retrieval addresses.

```text
complete evidence stays in the artifact store
bounded context carries stable addresses
retrieval reconnects the two on demand
```

[Get started](GETTING-STARTED.md) · [How it works](HOW-IT-WORKS.md) · [CLI guide](CLI.md) · [Use cases](USE-CASES.md) · [Repository](../README.md)

## Choose a path

### Use Straitjacket

Start with [Getting started](GETTING-STARTED.md). It covers installation, workspace setup, the first capture, and exact retrieval.

Then use:

- [CLI guide](CLI.md) for command syntax and selection;
- [Use cases](USE-CASES.md) for task-specific workflows;
- [How it works](HOW-IT-WORKS.md) for the end-to-end data flow.

### Understand the system

Read [Core concepts](CONCEPTS.md), then follow the [architecture documentation](README.md). The design sequence covers capability surfaces, execution, evidence derivation, deterministic delivery, behavioral measurement, and compiled investigations.

### Verify a claim

Use [`evals/`](../evals/). Evaluation receipts record the workload, comparison arms, acceptance criteria, results, and negative findings.

Use [`spec/`](../spec/) for normative behavior and [`CHANGELOG.md`](../CHANGELOG.md) for shipped history.

### Extend Straitjacket

Start with [Writing an evidence profile](WRITING-A-PROFILE.md) when adding support for a new command family. Use [Documentation style](DOCUMENTATION-STYLE.md) when changing user-facing documentation.

## Core guarantees

| Guarantee | Meaning |
|---|---|
| Bounded output | A digest has a fixed budget independent of the original payload size. |
| Reversible omission | Every omitted region retains a stable retrieval address. |
| Deterministic rendering | Identical evidence under the same contract produces identical model-visible bytes. |
| Declared coverage | The digest states what it parsed, showed, and omitted. |

## Current trust boundary

Straitjacket contains output and constrains repository-relative access. It is not yet a general process sandbox. Commands run with the authority of the invoking user.

See [Core concepts](CONCEPTS.md) and [Roadmap](../ROADMAP.md) for the current and planned security boundaries.

## Documentation map

| Need | Page |
|---|---|
| First successful session | [Getting started](GETTING-STARTED.md) |
| End-to-end explanation | [How it works](HOW-IT-WORKS.md) |
| Command reference | [CLI guide](CLI.md) |
| Common workflows | [Use cases](USE-CASES.md) |
| Terms and invariants | [Core concepts](CONCEPTS.md) |
| Product thesis | [Why Straitjacket](WHY-STRAITJACKET.md) |
| Architecture sequence | [Documentation hub](README.md) |
| Normative contracts | [`spec/`](../spec/) |
| Evaluation evidence | [`evals/`](../evals/) |
| Release history | [`CHANGELOG.md`](../CHANGELOG.md) |

---

<p align="center">
  <strong>The store keeps the evidence. The context keeps the address.</strong>
</p>
