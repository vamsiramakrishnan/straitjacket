# How Straitjacket works

Straitjacket separates evidence storage from model context.

The complete output of a tool call is stored locally. The model receives a bounded view that preserves the important identities, declares what was omitted, and provides exact addresses for later retrieval.

```text
agent tool call
    │
    ▼
pre-tool gate ──> execute command ──> capture stdout and stderr
                                      │
                                      ▼
                               immutable artifact
                                      │
                         profile extracts typed evidence
                                      │
                                      ▼
                               bounded digest
                                      │
                                      ▼
                                model context
                                      │
                         ctx get / ctx search
                                      │
                                      └────────> exact stored evidence
```

This page follows one command through that path.

## 1. Capture happens before output enters context

Suppose an agent runs:

```bash
ctx run -- pytest -q
```

Straitjacket starts the child process and captures stdout and stderr as they are produced. The raw streams do not need to enter the transcript before the harness can decide how to represent them.

This is the birth gate: potentially unbounded output is contained at its source.

Small results may pass through unchanged. The goal is bounded context, not mandatory indirection.

## 2. The complete result becomes an artifact

The captured streams, command metadata, and manifest are written to the local artifact store. The artifact receives a stable handle such as:

```text
run:8d8335db6848
```

A handle identifies evidence. It can be used to:

- retrieve stdout or stderr;
- search the captured output;
- compare the run with a later run;
- pin the artifact against collection;
- cite the evidence from another result.

Captured artifacts are immutable. Repository selectors such as `repo:src/auth.py` are different: they refer to live workspace state and are snapshotted when read.

## 3. A profile extracts typed evidence

A profile recognizes the shape of the captured output. Examples include test results, compiler diagnostics, linter output, JSON, JSONL, logs, search results, and generic text.

The profile does not replace the artifact. It derives a structured evidence view from it.

For a test run, the view may include:

- exit status;
- the complete failing-test identity census;
- selected failure details;
- file and line coordinates;
- coverage and omission counts.

For a repetitive log, it may include recurring templates, structurally rare lines, and the final summary.

If no specialized profile applies, Straitjacket uses a generic bounded representation.

## 4. A contract and budget produce the digest

The extracted evidence is rendered into a deterministic digest.

```text
[ctx run:8d8335db6848 profile=pytest/v2]
command: pytest -q
exit: 1
stdout: 4,102 lines · 402.1 KiB
failing tests:
  tests/test_auth.py::test_token_expiry   tests/test_auth.py:42
coverage:
  identities: 1/1
  omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

A useful digest answers five questions:

1. What happened?
2. Which evidence supports that conclusion?
3. What was omitted?
4. How complete is the visible view?
5. Which address retrieves the next useful detail?

The digest has a fixed budget. The original output may contain ten lines or ten million lines; the model-visible representation remains bounded by the selected contract.

## 5. Retrieval is exact and bounded

The model can retrieve a specific region:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

It can also search the stored artifact:

```bash
ctx search run:8d8335db6848#stdout "MissingTenantError" --context 3
```

Neither operation reruns the original command.

A small request returns exact bytes. A broad request returns another bounded view with narrower continuation addresses. Retrieval cannot reintroduce an unbounded payload by accident.

This is the central distinction:

- **not visible now** means the evidence is outside the current view;
- **lost** would mean there is no exact route back.

Straitjacket permits the first and rejects the second.

## 6. Determinism keeps the interface stable

Model-visible output is normalized before rendering. The deterministic contract excludes or canonicalizes fields that would otherwise change without changing the evidence, including:

- ANSI control sequences;
- absolute host paths;
- locale-dependent text;
- unstable ordering;
- temporary paths;
- incidental timing data.

Identical evidence under the same profile, contract, and budget should produce identical digest bytes.

This property supports reproducible evaluation, meaningful run comparison, and stable prompt prefixes.

## 7. Host hooks apply the policy mechanically

`ctx wrap setup` configures supported hosts with pre-tool and post-tool gates.

### Pre-tool gate

Before execution, the hook classifies the operation.

- Bounded operations continue unchanged.
- Recognized high-volume operations are routed through `ctx run`.
- Sensitive, interactive, or outside-workspace operations can require confirmation according to policy.

### Post-tool gate

After execution, the hook checks the returned payload. If an oversized result reached the host through another tool surface, the gate stores it and replaces it with a bounded, addressable view.

### Retrieval surface

The host receives a bounded `ctx` retrieval interface for stored artifacts and repository state. The retrieval surface has explicit budgets and cannot emit an unlimited result.

The hooks provide enforcement. The agent does not need to remember a prompt instruction before every command.

## 8. Multi-step work can execute beside the repository

Containment reduces payload size. It does not by itself reduce the number of model round trips.

Straitjacket provides progressively richer execution shapes:

| Work shape | Operation |
|---|---|
| One command | `ctx run` |
| Known sequence | `ctx seq` |
| Computed control flow | `ctx eval` |
| Typed evidence pipeline | `ctx q` |
| Compiled investigation DAG | `ctx plan` and `ctx investigate` |
| Typed repository question | `ctx ask` |

The model should choose the objective and resolve uncertainty. Deterministic scheduling, parsing, joining, deduplication, and rendering can run locally and return one bounded result.

The operating rule is: batch deterministic work within one hypothesis. Return to the model when new evidence can change the hypothesis.

## Core terms

| Term | Meaning |
|---|---|
| Artifact | Immutable stored evidence produced by a command, read, query, or derivation |
| Handle | Stable identifier for an artifact, such as `run:`, `blob:`, or `snapshot:` |
| Span | Address for a region within an artifact |
| Profile | Command-family extractor that derives typed evidence from captured output |
| Digest | Bounded deterministic view over evidence |
| Contract | Required evidence identities, coverage rules, and rendering constraints |
| Plan | Validated bounded program that composes evidence operations |

See [Core concepts](CONCEPTS.md) for the complete vocabulary.

## Current trust boundary

Straitjacket currently provides output containment, bounded retrieval, repository-relative path confinement, traversal and symlink checks, timeouts, process-group handling, and deterministic rendering.

It is not yet a general process sandbox. Commands execute with the authority of the invoking user. Capability-authorized handles and separate-identity broker execution are planned as a distinct security layer.

## What to read next

| Need | Page |
|---|---|
| Install and try the workflow | [Getting started](GETTING-STARTED.md) |
| Find exact command syntax | [CLI guide](CLI.md) |
| Start from a task | [Use cases](USE-CASES.md) |
| Learn the formal vocabulary | [Core concepts](CONCEPTS.md) |
| Understand the product thesis | [Why Straitjacket](WHY-STRAITJACKET.md) |
| Inspect architecture decisions | [Documentation hub](README.md) |
| Verify measured claims | [`evals/`](../evals/) |

---

[Documentation](README.md) · [Getting started](GETTING-STARTED.md) · [CLI guide](CLI.md) · [Core concepts](CONCEPTS.md)
