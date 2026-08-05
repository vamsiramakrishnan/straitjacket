---
name: straitjacket
description: Use this skill when executing noisy CLI commands, test suites (e.g., pytest, cargo test, build logs), or long-running commands in an agent session, or when inspecting specific lines from previously captured tool outputs using the 'ctx' CLI harness. Prevents context window flooding, eliminates prompt prefix cache drift, and enables exact span retrieval.
---

# Straitjacket CLI Harness: Context Containment & Exact Span Retrieval

When running test suites, build tools, or verbose CLI commands, raw stdout/stderr can flood the agent's context window (e.g., a single `pytest -q` run can consume 10k–300k+ tokens). Re-sending these logs on every subsequent turn slows down the session, increases token costs, and risks losing critical error lines when the host compaction runs.

The `straitjacket` CLI (`ctx`) solves this by capturing raw bytes into an immutable local store and presenting a small, deterministic **digest** with exact retrieval addresses.

---

## 1. Graduated Engagement (When to Use vs. When NOT to Use)

- **ALWAYS USE (`ctx run -- <command>`) for:**
  - Test suite executions (`pytest`, `unittest`, `cargo test`, `go test`, `npm test`, `bazel test`).
  - Heavy compiler or build commands where verbose logs or stack traces are expected.
  - Commands likely to generate **> 1,000 tokens** of output.
- **DO NOT USE for:**
  - Short, highly targeted commands (`git status`, short `ls`, `whoami`, simple `pwd`).
  - Outputs expected to be **< 1,000 tokens** where direct inline reading is simpler and indirection adds unnecessary overhead.

---

## 2. Core Operational Workflow (3-Step Loop)

### Step 1: Capture & Digest
Instead of running a verbose command directly, wrap it with `ctx run`:
```bash
ctx run -- pytest -q
```
*Why:* The command executes normally, but the raw output streams into an immutable local store. The agent context receives a compact, deterministic digest (~200 tokens) instead of the raw flood.

### Step 2: Understand the 4-Part Deterministic Digest
The digest returned by `ctx run` conforms to a standard 4-part structure:
```
[ctx run:8d8335db6848 profile=pytest/v2]
command: pytest -q
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failing tests (census):
  1. tests/test_auth.py::test_token_expiry   tests/test_auth.py:42
coverage:
  census: 1/1 identities inline · attested complete
  shown: 1 spans · omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```
- **Header & Stats:** Shows total lines, byte size, and estimated tokens saved from the context window.
- **Failing Tests Census:** Lists every failing test with its exact `file:line` coordinates.
- **Assertion Profile / Shown Spans:** Displays the deterministic core assertion or error snippet with ephemeral noise (timestamps, PIDs, ANSI colors) removed.
- **Next (Span Address):** Provides an exact command to retrieve any omitted byte range.

### Step 3: Surgical Retrieval (On-Demand)
If you need more context around an error than shown in the initial digest, **do not re-run the test command**. Query the exact byte/line slice using the address from `next`:
```bash
# Retrieve a specific line range from the saved execution run
ctx get run:8d8335db6848#stdout --lines 1280:1300
```
- **Rule of Thumb:** Request at most **50–100 lines per retrieval** to maintain strict context window discipline.

---

## 3. Essential `ctx` Command Cheat Sheet

| Command | Purpose | Example Usage |
|---|---|---|
| `ctx run -- <cmd>` | Execute a command, capture raw stdout/stderr locally, and output a bounded digest. | `ctx run -- pytest tests/` |
| `ctx get run:<id>#<stream> --lines <start>:<end>` | Retrieve an exact line range from a stored execution run without re-execution. | `ctx get run:8d8#stdout --lines 45:90` |
| `ctx diff run:<id1> run:<id2>` | Compare two execution runs, stripping ephemeral noise to show true signal diffs. | `ctx diff run:8d8 run:9f2` |
| `ctx search <query>` | Perform bounded semantic/keyword search over captured artifacts and repository files. | `ctx search "AssertionError: token"` |
| `ctx stats` | Check local artifact storage and total token savings accumulated across the session. | `ctx stats` |

---

## 4. Why Determinism & Prompt Cache Preservation Matter

- **Prompt Prefix Caching:** Because `straitjacket` strips non-deterministic noise (locale, temp paths, timestamps), identical test failures produce **byte-identical digests**. This prevents prompt prefix drift across multi-turn repair attempts, preserving prompt cache hit rates (typically 96–98%) and lowering API token costs by up to 10×.
- **Diffing True Signal:** Always prefer `ctx diff run:A run:B` over manual inspection when checking whether a code edit resolved a specific test regression.

