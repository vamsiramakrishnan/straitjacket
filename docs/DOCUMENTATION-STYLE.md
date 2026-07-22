# Documentation style

This guide defines the writing and maintenance standards for Straitjacket documentation.

The goal is not a uniform voice for its own sake. The goal is a documentation system in which readers can find the right page, understand which statements are authoritative, run commands that match the product, and distinguish shipped behavior from design work.

## Product terminology

Use these names consistently.

| Term | Use |
|---|---|
| **Straitjacket** | Product name in prose and headings |
| `straitjacket` | Repository name, URL, or literal lowercase brand asset |
| `ctx-harness` | Python package name |
| `ctx` | Command-line interface |
| artifact | Immutable stored evidence |
| handle | Stable artifact identifier such as `run:` or `blob:` |
| span | Address for a region within an artifact |
| profile | Command-family evidence extractor |
| digest | Bounded deterministic view over evidence |
| contract | Required identities, coverage, and rendering constraints |
| plan | Validated bounded composition of evidence operations |
| host | Antigravity, Claude Code, Codex, or another agent environment |

Do not use `summary`, `compression`, `memory`, `sandbox`, or `capability` as loose synonyms. Each has a narrower technical meaning.

## Page types

Each page should have one primary job.

### Tutorial

A tutorial takes a new user to a successful outcome. It is linear, runnable, and explicit about prerequisites.

Example: [Getting started](GETTING-STARTED.md).

### Task guide

A task guide begins with a user goal or failure mode and provides the shortest safe workflow.

Example: [Use cases](USE-CASES.md).

### Reference

Reference documentation describes syntax, options, contracts, and edge behavior. It should be complete enough to consult without reading an essay.

Example: [CLI guide](CLI.md).

### Explanation

An explanation develops the mental model, architecture, economics, or design rationale. It may discuss alternatives and trade-offs.

Examples: [How it works](HOW-IT-WORKS.md), [Why Straitjacket](WHY-STRAITJACKET.md), and the architecture papers.

### Specification

A specification defines required behavior. Normative language belongs in `spec/`.

### Evaluation receipt

An evaluation receipt records a workload, comparison arms, referee, acceptance criteria, measurements, and negative findings. Receipts belong in `evals/`.

Do not combine all six page types into one document. Link between them.

## Source-of-truth hierarchy

When sources disagree, prefer:

1. `spec/` for required behavior;
2. `CHANGELOG.md` for shipped release behavior;
3. executable tests for enforced acceptance criteria;
4. current CLI implementation for command syntax;
5. design documents for rationale and alternatives.

The README is an entry point, not an independent source of volatile truth.

## Volatile facts

Do not duplicate facts that change frequently unless the page is generated or the value is essential to the task.

Avoid hard-coded:

- current package version;
- test counts;
- benchmark totals without a dated receipt;
- dependency versions unless compatibility requires them;
- roadmap timing;
- supported command counts.

Link to `pyproject.toml`, `CHANGELOG.md`, CI, or the relevant evaluation receipt instead.

## Command accuracy

Every command in a guide or reference page must match the current CLI.

Before merging documentation that changes command syntax:

1. compare the example with `ctx --help` and the subcommand parser;
2. check positional arguments and separator placement;
3. verify that flags belong before or after `--` as shown;
4. distinguish preview from mutation;
5. label commands that execute tests or modify files;
6. prefer placeholders such as `<id>` and `<path>` consistently.

Examples should be copyable after replacing explicit placeholders.

Do not preserve obsolete syntax for continuity. Remove it or label it as deprecated when compatibility code still accepts it.

## Voice and sentence structure

Use direct technical prose.

- Prefer one claim per sentence.
- Prefer active voice when the actor matters.
- Use imperative verbs for procedures: `Run`, `Open`, `Verify`, `Retrieve`.
- Use sentence-case headings.
- Keep the subject close to the verb.
- Define a term before using it as shorthand.
- Use contractions sparingly in reference and specification pages.
- Avoid rhetorical questions in procedures.
- Avoid emoji in headings.
- Avoid slogans where a precise statement is possible.

Prefer:

> `ctx get` returns exact bytes when the requested region fits the retrieval budget.

Avoid:

> With the power of lossless evidence paging, you can always get everything back whenever you need it.

## Claims and evidence

A product claim should identify its scope.

State:

- what was measured;
- under which workload;
- against which comparison;
- on which date or release when relevant;
- where the receipt lives;
- which negative or counter-regime was observed.

Keep detailed benchmark tables in `evals/`. The README may summarize the thesis and link to the evidence.

Do not turn a result from one model, repository, or workload into a universal claim.

## Status language

Use only these states in architecture and roadmap material:

- **Shipped** — implemented and acceptance-tested.
- **Shadow** — observes or scores without enforcing.
- **Designed** — specified with an acceptance referee, not implemented.
- **Rejected** — investigated and deliberately not adopted.

Place the status near the mechanism name. Do not imply that a designed mechanism is available.

## Security language

Separate containment from isolation.

Current documentation may describe:

- output containment;
- bounded retrieval;
- repository-relative path confinement;
- traversal and symlink checks;
- timeouts and process-group handling.

Do not call the current system a general sandbox. Commands run with the authority of the invoking user until a separate-identity broker boundary ships.

Use `handle` for a content address. Use `capability handle` only when authorization semantics are implemented and enforced.

## Structure of a task page

Use this default shape:

1. outcome;
2. prerequisites;
3. command or procedure;
4. expected result;
5. important limits or safety notes;
6. troubleshooting;
7. next page.

Use tables for choice and comparison. Use numbered steps for ordered work. Use bullets only when order does not matter.

## Link strategy

Link to the page that owns the detail.

- README → guides and evidence.
- Guides → reference for full syntax.
- Reference → specs for normative contracts.
- Architecture → receipts for measured decisions.
- Changelog → implementation and migration detail.

Avoid repeating the same explanation across the README, docs index, and product guides.

Use relative repository links so GitHub and the documentation site remain portable.

## Review checklist

Before merging a documentation change, confirm:

- [ ] The page has one primary job.
- [ ] The intended reader and outcome are clear in the opening paragraph.
- [ ] Commands match the current CLI.
- [ ] Preview and mutation are clearly distinguished.
- [ ] Observe-only and execution-capable operations are clearly distinguished.
- [ ] Current and planned security boundaries are not conflated.
- [ ] Volatile facts are linked rather than duplicated.
- [ ] Product terminology is consistent.
- [ ] Status labels are accurate.
- [ ] Claims link to a specification, changelog entry, test, or evaluation receipt.
- [ ] Links resolve from the file's location.
- [ ] The page does not duplicate another page's primary content.

## Maintenance rule

A public CLI or behavior change is incomplete until its owning documentation changes in the same pull request.

At minimum, inspect:

- `README.md` when the first-use story changes;
- `docs/GETTING-STARTED.md` when setup changes;
- `docs/CLI.md` when syntax or semantics change;
- `docs/USE-CASES.md` when the recommended workflow changes;
- `spec/` when the compatibility contract changes;
- `CHANGELOG.md` when behavior ships;
- `evals/` when a measured claim changes.

---

[Documentation](README.md) · [Contributing](../CONTRIBUTING.md) · [Specifications](../spec/) · [Evaluation receipts](../evals/)
