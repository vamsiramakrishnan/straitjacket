# straitjacket

Context containment for coding agents.

[![Tests](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml/badge.svg)](https://github.com/vamsiramakrishnan/straitjacket/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ctx-harness)](https://pypi.org/project/ctx-harness/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/github/license/vamsiramakrishnan/straitjacket)](LICENSE)

A coding agent can produce hundreds of thousands of tokens from one test run. That output enters the transcript, gets sent again on later turns, and eventually competes with the work itself.

straitjacket changes the data path. It stores complete tool output locally. The model receives a small deterministic digest with exact retrieval addresses. Nothing omitted becomes unreachable.

```text
command → local artifact → bounded digest → model
              ↑                              │
              └──────── exact retrieval ─────┘
```

The package is `ctx-harness`. The command is `ctx`.

## Install

```bash
python -m pip install --upgrade ctx-harness
cd your-repository
ctx setup
ctx doctor
```

Python 3.11 or newer is required. `ctx setup` detects Antigravity, Claude Code, and Codex. It merges the required configuration and verifies the result. Re-running it is safe.

To preview a host configuration without writing it:

```bash
ctx wrap codex --print-config
```

## Try the core loop

Capture a noisy command:

```bash
ctx run -- pytest -q
```

The full stdout and stderr remain in the local artifact store. The terminal receives a bounded result:

```text
[ctx run:8d8335db6848 profile=pytest/v2]
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failures:
  tests/test_auth.py::test_token_expiry  tests/test_auth.py:42
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

Retrieve only the evidence you need:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
ctx search run:8d8335db6848 "MissingTenantError"
```

This is the entire mechanism: capture once, keep a bounded view in context, and retrieve exact bytes on demand.

## What it does

- Contains large command output before it enters the transcript.
- Preserves stdout, stderr, structured results, and derived evidence locally.
- Produces deterministic digests for tests, diagnostics, logs, JSON, tables, searches, and generic text.
- Keeps every omission behind a resolvable handle.
- Bounds retrieval, so `ctx get` cannot become a second flood source.
- Integrates with Antigravity, Claude Code, and Codex.
- Measures containment, retrieval, reruns, cache behaviour, and intervention outcomes.
- Supports repository maps, symbol navigation, call graphs, typed queries, and compiled investigations.

It does not rewrite transcript history. It is not agent memory. It is not a complete process sandbox. Commands still run with the invoking user's authority.

## Choose the right command

| Need | Command |
|---|---|
| Capture one command | `ctx run -- <command>` |
| Capture a pipeline | `ctx run --shell '<pipeline>'` |
| Run declared steps | `ctx seq` |
| Run computed local control flow | `ctx py <script>` |
| Continue a long-running command | `ctx run --bg-after 30 -- <command>` |
| Inspect a background job | `ctx job <id>` |
| Retrieve exact evidence | `ctx get <handle>` |
| Search stored evidence | `ctx search <handle> <pattern>` |
| Compare two runs | `ctx diff run:<before> run:<after>` |
| Map a repository | `ctx map --budget 500` |
| Find definitions or references | `ctx def`, `ctx refs`, `ctx callers` |
| Ask a typed repository question | `ctx ask '<question>' --intent <intent>` |
| Run a compiled investigation | `ctx plan run <plan.json>` |
| Inspect session economics | `ctx stats --session` |

Use the least expressive command that fits the job. Batch deterministic work. Return to the model when new evidence can change the hypothesis.

## How containment works

straitjacket acts at four points:

| Gate | Decision |
|---|---|
| Birth | Can this operation flood before it runs? |
| Entry | What crossed the tool or host boundary? |
| Residence | What should remain in active context? |
| Emission | What should the model send back out? |

The birth gate is the critical one. Known noisy commands are routed through capture. Small bounded commands can pass through directly. Unknown commands follow the configured guard policy.

Host capabilities differ. Claude Code and Codex can rewrite noisy calls before execution and replace oversized tool results after execution. Antigravity can stop a noisy call before execution, but its published hook contract cannot replace a completed tool result. See [host capabilities](docs/HOST-CAPABILITIES.md) for the exact matrix.

## Evidence, not summaries

A digest is a typed view over immutable evidence. It reports:

1. what ran;
2. what failed or changed;
3. what was shown;
4. what was omitted;
5. how to retrieve the omitted region.

Specialized profiles extract identities such as failed tests, diagnostics, symbols, and log templates. Generic truncation is the fallback, not the design.

For files that may change, line addresses can carry a content anchor:

```bash
ctx get repo:src/auth.py --lines 40:52@07407f1c
```

The anchor verifies the content. If the code moved, retrieval follows it. If the content no longer exists, retrieval refuses instead of returning unrelated lines.

## Measured claims

The repository keeps benchmarks and negative results in [`evals/`](evals/). Current receipts cover:

- containment across real command-output families;
- prompt-cache stability;
- agent A/B runs;
- decisive-evidence preservation;
- hook latency;
- compiled investigation turn reduction;
- address stability across edits;
- policy and routing counterexamples.

The project does not treat a smaller digest as success by itself. The target is lower context cost at matched or better task completion, with every omission declared and retrievable.

## Documentation

Start here:

1. [How it works](docs/HOW-IT-WORKS.md)
2. [Getting started](docs/GETTING-STARTED.md)
3. [Core concepts](docs/CONCEPTS.md)
4. [CLI reference](docs/CLI.md)
5. [Configuration](docs/CONFIGURATION.md)

Then use:

- [Architecture and code map](docs/ARCHITECTURE.md)
- [Host capability matrix](docs/HOST-CAPABILITIES.md)
- [Use cases](docs/USE-CASES.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Design documentation](docs/README.md)
- [Normative specifications](spec/)
- [Evaluation receipts](evals/)
- [Changelog](CHANGELOG.md)
- [Roadmap](ROADMAP.md)

## Development

```bash
git clone https://github.com/vamsiramakrishnan/straitjacket.git
cd straitjacket
python -m pip install -e '.[dev]'
pytest
```

Optional extras add code analysis, semantic search, faster serialization, image decoding, and graph support. See [Getting started](docs/GETTING-STARTED.md) and [`pyproject.toml`](pyproject.toml).

Before changing a mechanism, read [CONTRIBUTING.md](CONTRIBUTING.md). New mechanisms need a clear owner plane, deterministic output, explicit degradation, and a named evaluation gate.

Apache-2.0.
