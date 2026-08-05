# Specifications

This directory is the **normative** source of truth for straitjacket's
behaviour. When a design doc and the spec disagree about what the product does,
the spec wins. When the spec and the code disagree, that's a bug — file it.

New contributor? Read this before proposing a change: it tells you what any
change must satisfy to merge.

## What's here

| File | What it is |
|---|---|
| [`SPEC.md`](SPEC.md) | The normative specification. RFC-2119 keywords (MUST/SHOULD/MAY), the core invariants, the wire and packaging contracts. |
| [`ACCEPTANCE.md`](ACCEPTANCE.md) | The acceptance suite: the list of MUSTs a release has to satisfy. `tests/` is its executable form — a green suite is the merge gate. |
| [`REFERENCES.md`](REFERENCES.md) | External references the integration was designed against (host docs), with a "re-verify before release" note. |
| [`adr/`](adr/) | Architecture Decision Records — the load-bearing choices and why they were made. |
| [`schemas/`](schemas/) | JSON Schemas for the wire contracts, validated in CI. |

## The decisions (ADRs)

| ADR | Decision |
|---|---|
| [001](adr/001-transcript-is-an-index.md) | Treat the transcript as an index over evidence, not a warehouse of it. |
| [002](adr/002-store-outside-repository.md) | Store payloads outside the repository. |
| [003](adr/003-pretool-routing.md) | Route before execution; do not repair after execution. |
| [004](adr/004-plugin-contains-skill.md) | The plugin contains the skill (they must not both be installed). |

## The wire schemas

| Schema | Contract |
|---|---|
| [`invocation-v1`](schemas/invocation-v1.schema.json) | The capture/invocation manifest written for every run. |
| [`mcp-request-v1`](schemas/mcp-request-v1.schema.json) | The bounded MCP request surface (the single `ctx` tool). |

## How the spec relates to everything else

The project separates four kinds of truth (see [`docs/README.md`](../docs/README.md)):

- **`spec/`** (here) — what the product *must* do (normative).
- **`docs/`** — how it works and *why* each mechanism exists (design intent).
- **`evals/`** — the *measurements* that justified shipping each mechanism.
- **`CHANGELOG.md`** — what actually shipped, and when.

For how a change gets reviewed and merged, see
[`CONTRIBUTING.md`](../CONTRIBUTING.md).
