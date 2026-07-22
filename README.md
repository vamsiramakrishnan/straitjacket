<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/readme/hero.svg">
  <img src="assets/readme/hero-light.svg" width="100%" alt="Straitjacket: artifact-backed context containment for coding agents.">
</picture>

[![Tests](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml/badge.svg)](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/github/license/vamsiramakrishnan/straitjacket)](LICENSE)

[Get started](docs/GETTING-STARTED.md) · [How it works](docs/HOW-IT-WORKS.md) · [CLI](docs/CLI.md) · [Use cases](docs/USE-CASES.md) · [Documentation](docs/README.md)
</div>

# Straitjacket

Straitjacket keeps verbose tool output out of a coding agent's transcript without making the omitted evidence disappear.

Potentially unbounded output is captured before it reaches the model. The complete bytes are stored locally as immutable evidence. The agent receives a small, deterministic digest with exact retrieval addresses.

```text
tool output  ──capture──>  local artifact store
                              │
                              ├──> bounded digest ──> model context
                              │
                              └<── exact retrieval ── ctx get / ctx search
```

The transcript becomes an index over evidence rather than the place where evidence must live.

## Why this exists

Coding agents accumulate tool output across a session. A large test run, build log, repository search, or connector response can occupy the context window for many later turns. Truncation and compaction reduce the visible size, but they can also make the omitted evidence difficult or impossible to recover.

Straitjacket changes the storage model:

- complete output remains available outside the transcript;
- the model sees a bounded view selected for the command type;
- every omission is declared;
- omitted regions retain stable retrieval coordinates;
- repeated views are rendered deterministically.

It does not ask the model to remember less. It gives the model a smaller, reversible interface to the same evidence.

## Quick start

Straitjacket currently installs from source and requires Python 3.11 or newer.

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e .

cd /path/to/your/project
ctx wrap setup
```

`ctx wrap setup` configures the current workspace for Antigravity, Claude Code, and Codex. It merges with existing host configuration and is safe to run again.

Preview a host configuration before writing it:

```bash
ctx wrap codex --print-config
```

Verify the installation:

```bash
ctx doctor
ctx doctor --antigravity
```

See [Getting started](docs/GETTING-STARTED.md) for host-specific behavior, optional dependencies, and troubleshooting.

## The core workflow

### 1. Capture a noisy command

```bash
ctx run -- pytest -q
```

A large result is stored in full and replaced with a bounded digest:

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

The handle identifies the stored run. The digest is a bounded view over that run, not a replacement for it.

### 2. Retrieve only the evidence you need

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

Small selections return exact bytes. Large selections return another bounded view with narrower continuation addresses.

### 3. Search stored evidence without rerunning the command

```bash
ctx search run:8d8335db6848#stdout "MissingTenantError"
```

The original command is not executed again. Search runs against the captured artifact.

## Automatic host integration

The harness can intercept tool calls before their output enters the model context.

| Host | Workspace setup | Session behavior |
|---|---|---|
| Antigravity | Installs a workspace plugin under `.agents/plugins/ctx-harness/` | Persistent for the workspace |
| Claude Code | Installs hooks and a compact `ctx` command card | `ctx wrap claude -- ...` also supports an ephemeral, zero-residue session |
| Codex | Installs `.codex` configuration, hooks, and an `AGENTS.md` block | Persistent for the workspace |

Recognized high-volume commands are routed through `ctx run`. Bounded commands continue unchanged. A post-tool gate catches oversized results from other tool surfaces.

The policy is mechanical. Correct containment does not depend on the model remembering an instruction.

## Choose the smallest sufficient operation

| Work | Command |
|---|---|
| Capture one command | `ctx run -- <command>` |
| Capture a shell pipeline | `ctx run --shell '<pipeline>'` |
| Run known steps in one round | `ctx seq '<step 1>' '<step 2>'` |
| Run computed control flow | `ctx eval <script>` |
| Supervise long-running work | `ctx run --bg-after <seconds> -- <command>` and `ctx job` |
| Retrieve exact evidence | `ctx get <handle>` |
| Search captured or repository evidence | `ctx search <ref> <pattern>...` |
| Query typed evidence | `ctx q '<pipeline>'` |
| Compile a repository question | `ctx ask "<question>" --intent <intent>` |
| Compare captured runs | `ctx diff run:<before> run:<after>` |
| Inspect session behavior | `ctx stats --session` and `ctx gain` |

`ctx ask` supports `locate`, `impact`, `diagnose`, `trace`, `compare`, `verify`, and `review`. The `verify` and `review` intents may execute tests; the other intents are observation-only.

See the [CLI guide](docs/CLI.md) for syntax and decision guidance.

## Core guarantees

### Bounded output

The size of a digest is constrained independently of the size of the captured output. Small results may pass through unchanged when capture would add no value.

### Reversible omission

Omitted evidence retains an address. `ctx get` and `ctx search` recover exact regions without replaying the original operation.

### Deterministic rendering

Volatile fields such as absolute paths, ANSI control sequences, unstable ordering, and timing noise are normalized before model-visible output is rendered. Identical evidence under the same contract produces identical digest bytes.

### Declared coverage

A digest states what it parsed, what it showed, and what it omitted. A short output is not considered successful if required evidence identities disappear.

## What Straitjacket does not do

Straitjacket is not a general process sandbox. Commands run with the authority of the user who invoked them. The current security boundary covers output containment, bounded retrieval, repository-relative path confinement, traversal and symlink checks, timeouts, and process-group handling.

Separate-identity execution, capability-authorized handles, and broker-grade isolation are planned as a distinct boundary. See [Core concepts](docs/CONCEPTS.md) and [Roadmap](ROADMAP.md).

It is also not:

- a vector database or conversational memory product;
- a lossy summarizer that discards unaddressable evidence;
- a replacement for the model's reasoning;
- a claim that compression always improves task quality.

## Evidence, not adjectives

Performance and quality claims live in [`evals/`](evals/), with the workload, comparison arms, acceptance criteria, and negative results recorded alongside the measurement. Current product behavior is tracked in [`CHANGELOG.md`](CHANGELOG.md). Normative contracts live in [`spec/`](spec/).

Start with:

- [Benchmark charter](evals/BENCHMARK.md)
- [Evaluation receipts](evals/)
- [Why Straitjacket](docs/WHY-STRAITJACKET.md)
- [Theory](docs/THEORY.md)

## Documentation

| Goal | Read |
|---|---|
| Install and complete the first capture | [Getting started](docs/GETTING-STARTED.md) |
| Understand the end-to-end data flow | [How it works](docs/HOW-IT-WORKS.md) |
| Find the right command | [CLI guide](docs/CLI.md) |
| Start from a task or failure mode | [Use cases](docs/USE-CASES.md) |
| Learn the vocabulary and invariants | [Core concepts](docs/CONCEPTS.md) |
| Extend an evidence profile | [Writing an evidence profile](docs/WRITING-A-PROFILE.md) |
| Read the design sequence | [Architecture documentation](docs/README.md) |
| Check compatibility contracts | [Specifications](spec/) |
| Verify a claim | [Evaluation receipts](evals/) |

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/ -q
```

Optional extras add richer or faster analysis engines:

```bash
python -m pip install -e '.[dev,map,fast,code,scip,sem]'
```

Optional engines must degrade to a documented fallback. They must not be required for the core capture path.

Read [Contributing](CONTRIBUTING.md) before changing a mechanism, contract, or public command.

## License

Apache-2.0.
