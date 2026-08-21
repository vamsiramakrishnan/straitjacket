# How straitjacket works

[Documentation](README.md) · [Getting started](GETTING-STARTED.md) · [Core concepts](CONCEPTS.md)

## The problem

Tool output becomes model input. A large test log or connector response can occupy most of a coding agent's context window. The same bytes are then sent again on later turns. When the window fills, compaction may remove the evidence the agent still needs.

Larger context windows delay this failure. They do not change the data path.

straitjacket changes the data path:

```text
tool output
    │
    ├── complete bytes ──→ local artifact store
    │
    └── typed facts ─────→ bounded digest ──→ model
                               │
                               └── exact retrieval address
```

## One command

```bash
ctx run -- pytest -q
```

The command runs normally. stdout and stderr stream into a local artifact. A pytest profile extracts the failure identities, locations, and useful detail. The model receives a bounded digest:

```text
[ctx run:8d8335db6848 profile=pytest/v2]
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failures:
  tests/test_auth.py::test_token_expiry  tests/test_auth.py:42
coverage:
  identities: 1/1
  omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

The digest is a view. It is not the stored evidence.

If the traceback matters, retrieve that region:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

If the error text matters, search the stored artifact:

```bash
ctx search run:8d8335db6848 "MissingTenantError"
```

Retrieval is also bounded. A broad request returns another small view with narrower addresses.

## Why the digest is typed

Simple truncation preserves position, not meaning. It can keep progress output and drop the failure. A profile understands the output family.

Examples:

- pytest profiles preserve failed test identities and trace locations;
- diagnostic profiles preserve severity, code, file, and line;
- log profiles surface rare templates and repeated patterns;
- JSON profiles preserve schema, counts, and exceptional records;
- generic text keeps bounded head and tail windows.

Each profile declares what must remain inline, what can shrink, and what may stay out of context only when it has an address. The digest includes a coverage receipt.

## Why the output is deterministic

The same evidence, contract, and delivery plan must produce the same bytes. Volatile timings, terminal decoration, and temporary paths are normalized where appropriate.

Determinism has two practical effects:

- stable prompt prefixes are easier for providers to cache;
- a diff between two digests reflects evidence changes, not rendering noise.

Use `ctx diff run:<before> run:<after>` when the question is what changed.

## How agent integration works

`ctx setup` installs host-specific hooks and the bounded retrieval surface.

Before execution, the birth gate classifies the operation:

- known small operations pass through;
- known noisy reads route through capture;
- mutations retain their permission boundary;
- unknown operations follow the configured guard policy.

After execution, supported hosts can capture an oversized result that escaped birth-time classification and replace it with a digest.

Host APIs differ. Claude Code and Codex support transparent command rewriting and output substitution. Antigravity can deny a noisy call before execution, but cannot replace an already returned tool result through its published PostToolUse contract. The exact behaviour is documented in [Host capabilities](HOST-CAPABILITIES.md).

## The four gates

| Gate | Function |
|---|---|
| Birth | Prevent an unbounded operation from entering context raw. |
| Entry | Measure and constrain results crossing a host or tool boundary. |
| Residence | Control what remains in active context. |
| Emission | Prevent large stored evidence from being pasted back into a deliverable. |

One artifact store supports all four. The critical invariant is simple:

> Potentially unbounded output is either captured before it reaches the model or rejected before execution.

## Repository evidence

The same model applies to code navigation. Repository maps, symbol definitions, references, call graphs, and compiled plans return bounded views with source addresses.

Files can change between turns. A plain line number is only a position. straitjacket can attach a content anchor:

```bash
ctx get repo:src/auth.py --lines 40:52@07407f1c
```

On retrieval, the anchor is verified. If the content moved, the address follows it. If the content disappeared, retrieval refuses. It never silently returns different code at the old line number.

## What the system guarantees

- Complete captured evidence remains locally retrievable until retention removes it.
- Every digest is bounded.
- Every declared omission has a retrieval path.
- Rendering is deterministic for the same inputs.
- Path traversal and symlink escape are checked relative to the active workspace.
- Retrieval cannot return an unbounded payload.

## What it does not guarantee

straitjacket is not a full process sandbox. Captured commands run with the invoking user's authority. It does not make a dangerous command safe. It preserves mutation approvals instead.

It is also not agent memory and not semantic transcript compression. It contains tool evidence at its source.

Next: [install it and run the first capture](GETTING-STARTED.md).
