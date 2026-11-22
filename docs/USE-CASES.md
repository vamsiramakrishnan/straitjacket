# Use cases

## Find the code behind a change

If you know the symbol, start there. From the Straitjacket checkout, these
commands locate the function that parses artifact handles, read its definition,
and find its uses in the CLI command modules.

```bash
ctx search repo: sym:parse_ref
ctx def repo:src/ctx/refs.py:parse_ref
ctx refs parse_ref --path src/ctx/commands
```

The search finds the definition in `src/ctx/refs.py`. The definition includes a
snapshot and separate commands to retrieve the captured body or check the
current source. The references list paths and line numbers. Check the engine
in the header. A current SCIP index can resolve symbol identity. The textual
fallback also matches names in comments, strings and unrelated scopes.
[Code search and index coverage](CODE-SEARCH.md)

Choose the starting point that matches what you know.

| Starting point | Command | What to read |
|---|---|---|
| An unfamiliar repository | `ctx map --budget 600` | Ranked files and symbol signatures within the requested map budget |
| An issue or task description | `ctx pack 'Where does parse_ref validate a run handle?' --files 3` | Three candidate files at most, with ranking reasons and source coordinates |
| An error string | `ctx search repo: 'unknown stream' file:src/ lang:python` | Matching lines in Python source and a coverage count |
| A symbol with many uses | `ctx q 'refs parse_ref \| group file \| top 3'` | Reference sites grouped by file, with any upstream omissions reported |

On 14 DeepSWE tasks, the top eight files from `ctx pack` included an average
54.0% of the existing source files changed by the reference fix. Ranking by
grep count reached 42.9%. The grep ranking found at least one relevant file on
all 14 tasks, and the pack did so on 12. If the ranked files miss, search for
a specific error or symbol. [Per-task results](../evals/agentbench/results/pack_recall.json)

## Run known checks in one turn

When you already know which commands to run, submit them together. Each step
keeps its own result and evidence address. The following example runs in the
Straitjacket checkout with its development dependencies installed.

```bash
ctx seq \
  --step 'git diff --stat' \
  --step 'python -m pytest tests/test_codeverbs.py'
```

Use `ctx q` to group or filter existing results locally. Use `ctx py` when a
later command depends on an earlier result and you can express the choice in
Python. When the next step requires judgment, return the evidence to the agent.
[CLI guide](CLI.md)

## Diagnose a failing run

```bash
ctx run -- python -m pytest
```

For verbose pytest output, the digest lists failing tests and points to their
tracebacks. Set `RUN` to the printed `run:` address. Search the captured stream,
then use the matching lines to choose a range. The range here is an example.

```bash
ctx search "${RUN}#stdout" 'AssertionError'
ctx get "${RUN}#stdout" --lines 120:136
```

The same approach works for a repeated log message or an error buried in a
build. Switch to `#stderr` if the command writes diagnostics there. A capture
that exceeds the per-stream storage cap reports which bytes it kept. Searches
can only find evidence in those regions. [Capture limits](CLI.md#output-that-never-stops)

After an edit, run the check again. Set `AFTER` to the new `run:` address and
compare the outcomes, added or removed failures, and changed log patterns.

```bash
ctx diff "$RUN" "$AFTER"
```

For a slow suite, use `ctx run --bg-after 30 -- python -m pytest`. If the
suite is still running after 30 seconds, the response gives you a job ID. Run
`ctx job "$JOB" --wait` with that ID to get the final digest.

## Hand off an investigation

Give the next worker the finding, the evidence address and line range, the
searches that returned no matches, and any open question. In a shared workspace
the worker can open the same snapshot or captured run. The bundled
`ctx-explorer` agent uses this handoff format. For a different workspace,
export the cited artifacts with `ctx capsule export`, then import them there.
[Evidence capsules](CLI.md#evidence-that-travels-ctx-capsule) · [ACP orchestration](ACP.md)

For a repair that spans several turns, `ctx task prepare`, `run`, and `resume`
share a task ledger and budget. The controller records observations, invokes
the configured worker for analysis, and checks its patch against the specified
tests. [Investigate and verify a repair](TASK-EXECUTION.md)

## Bound a verbose connector result

With a host that supports tool-result replacement, Straitjacket can return a
digest of a large text result and an address for retrieving more. Antigravity
CLI can intercept supported calls before execution but cannot replace an
arbitrary connector response after it returns. Check the host's capabilities
before relying on interception. [Host capabilities](HOST-CAPABILITIES.md)

## Measure exploration cost

Compare the same tasks, model, and acceptance checks with and without
Straitjacket. Record task pass rate, tool calls, repeated reads, wall time, and
provider-reported usage or cost per accepted task. A shorter result helps when
the agent can still find the evidence required to finish.
