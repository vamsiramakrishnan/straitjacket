# Tool-boundary improvements — 2026-10-12

## Selection from the supplied reference

Source: the user-supplied `sanjitmehta-google-agy-ai-dev-main.zip`, source revision
`74339fb6236d61ee081a8a3f0535c5cb32f7ac64`. The attached benchmark pins
`ctx-harness==0.39.0`; current main already contains native small-output
passthrough and other later fixes. This change extends those mechanisms rather
than assuming every historical defect still exists.

The [count-only source receipt](agentbench/results/agy-reference.json) preserves
task names, selected run IDs, official binary outcomes, recorded elapsed time,
explicitly assumed token totals and estimates. It contains no customer notes,
credentials, commands or tool output. Its extractor records the source report
hash and keeps missing billed cost as null.

Eighteen selected pairs resolve 14/18 in each arm. Cliffy is a gain; oxvg is a
regression. Under the source's separate-cache assumption, selected gross totals
are 277,991,057 / 263,234,952, and model-dollar estimates are
57.26627535 / 52.64883675. Agent wall totals are 35,862.473 / 35,909.466 seconds.
These are historical single-attempt observations. They exclude other attempts
and do not establish causality, repeated-run superiority or billed savings.
FastAPI's two arms use different revisions and dates.

Across available command-call logs, 984 runs include 708 outputs at most 1 KiB,
878 at most 4 KiB and 66 empty outputs; 68 get and 20 search calls are recorded.
This census includes available attempts beyond the selected pair table. The
archive's `delivered_bytes` counts ctx subprocess stdout/stderr and omits the
adapter's outer JSON envelope. It is not a model-wire token count.

## Ten ranked changes

Rank reflects proximity to the observed tool boundary, execution correctness
and ability to test the mechanism without model sampling.

| Rank | Gap | Implemented change | Regression gate |
|---|---|---|---|
| 1 | Adapter scrapes human headers and private manifest directories | `CommandResult`, public `ToolSession`, `ctx run --json`, versioned wire schema | Child status, empty output, launch failure and schema validation |
| 2 | Signal death maps to an unrelated generic exit | Shared native status mapping; retain null command exit plus signal | SIGTERM gives native 143, never command exit 0/1 |
| 3 | Sync vendor tools block the event loop and own cancellation glue | Async session with capture cancellation, process-group cleanup and partial publication | Live descendant termination and retained partial output |
| 4 | Small commands pay digest/receipt costs | Serialized-size-aware complete inline delivery; separate stdout/stderr | Empty output, missing final newline, escaping, byte/line bounds |
| 5 | Retrieval is bespoke; string splitting breaks quoted patterns | Typed shared MCP dispatch and existing finite query composition; corpus file discovery | Quoted search, empty files, workspace refusal, execute refusal |
| 6 | Parse cap applies only after an unbounded blob allocation | Bounded blob read at `DigestContext.load` | Whole-blob reader is forbidden in the parse-cap test |
| 7 | Telemetry credits intermediate views rather than final delivery | Actual JSON bytes, expansion, duration, termination; native trailer included | One run accounting event equals emitted size |
| 8 | Cached/thinking fields disappear; unknown usage becomes zero | Raw usage record with explicit inclusive/separate/unknown input semantics | Missing/invalid counters and conflicting cache reports |
| 9 | Harness copies private store trees into opaque archives | Session export through existing verified capsule closure | Import in another store and retrieve original bytes |
| 10 | Integration changes lack a reproducible delivery ablation | Model-free same-capture replay, count-only reference extractor, installed-wheel SDK probe | Exact inline streams, statuses, quiet-needle retrieval and clean-wheel execution |

Shared runtime code remains provider-independent. The Antigravity dependency
stays in its own environment; its launcher loads only the ctx package, avoiding
exposure of the parent interpreter's whole site-packages tree.

## Delivery ablation

Run from the repository:

```bash
python evals/tool_boundary_replay.py --out /tmp/tool-boundary-replay.json
```

The [checked-in result](agentbench/results/tool-boundary-replay.json) compares
auto and forced digest delivery on the **same captured evidence**, using this
revision's profiles. All seven cases preserve termination; complete inline
streams match captured bytes. The 88,885-byte flood emits a 1,117-byte command
envelope and retrieves the quiet line at 2,137. JSON-escaping pressure stays on
the digest path. Empty output emits 227 bytes rather than 468 under forced
digest; a six-byte output emits 234 rather than 294. Both are still larger than
native delivery. This is a delivery ablation, not a historical 0.39.0 comparison.
No provider was called, and no coding-success or billing improvement is claimed.

Reproduce the historical count receipt from the extracted reference:

```bash
python evals/agy_reference.py /path/to/agy-sdk-evals/deepswe --out /tmp/agy-reference.json
```

Before promotion, rerun matched command-output profiles with the same SDK,
model, task environment, retries and pinned new wheel. Preserve failed and
infrastructure attempts. Assess task-level regressions, total cost under an
explicit accounting convention, elapsed time and retrieval behavior together.
