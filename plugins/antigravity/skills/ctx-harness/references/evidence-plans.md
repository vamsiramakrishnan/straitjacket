# Compiled evidence plans — authoring reference

A plan pays one model round to compile a bounded evidence DAG; the harness
validates, prices, and executes it locally, then returns one ranked
investigation digest. Read this before authoring a `ctx.plan/v1` document
for `ctx plan run` or `ctx investigate`.

## The `ctx.plan/v1` shape

```json
{
  "version": "ctx.plan/v1",
  "objective": {"kind": "diagnose", "question": "which changed symbols explain the failing tests?"},
  "budget": {"wall_seconds": 120, "max_nodes": 24, "max_fanout": 64},
  "steps": [
    {"id": "changes", "op": "repo.changed"},
    {"id": "tests", "op": "test.run",
     "args": {"command": "python3 -m pytest -q"}},
    {"id": "culprits", "op": "evidence.join",
     "args": {"on": "failing_in_changed"}, "after": ["tests", "changes"]},
    {"id": "counter", "op": "evidence.join",
     "args": {"on": "untouched_failures"}, "after": ["tests"]},
    {"id": "probe", "op": "ast.search",
     "args": {"pattern": "$AUTH.authorize($ARG)", "language": "python"},
     "when": "culprits.count > 0"}
  ],
  "emit": {"rank_by": ["dynamic_confirmation", "changedness", "causal_proximity"],
           "sections": ["conclusion_candidates", "counterevidence", "coverage"]}
}
```

This is the diagnose-auth-regression plan: `changes` and `tests` run
independently; `culprits` joins failures against changed symbols (the
conclusion candidate); `counter` joins failures that touch nothing changed
(the required counterevidence section, present even when empty); `probe`
is a guarded structural search that only runs once a culprit exists —
skipped nodes are declared in coverage, never silently absent. Step
fields: `op`, `args`, `input` (single upstream data-flow id), `after`
(ordering-only edges), `foreach` + `cap` (bounded fan-out), `when` (guard
micro-grammar), `on_error`, `on_missing`. Validate before running:
`ctx plan validate <plan.json>` (typed verdict, nothing executes),
`ctx plan price <plan.json>` (wall/nodes/tokens estimate vs. an
interactive round count).

## Op inventory (`ctx plan ops` is the authoritative source)

| op | I/O kind | class | engine chain (first available wins) |
|---|---|---|---|
| `repo.changed` | ∅→files | observe | git porcelain → facts |
| `repo.inventory` | ∅→text | observe | repomap |
| `ast.outline` | files\|sites→text | observe | ast-grep → tree-sitter → ctags → stdlib ast |
| `ast.search` | ∅→sites | observe | ast-grep → labeled regex fallback |
| `ast.rewrite.preview` | ∅→records | execute | ast-grep only, no lossy fallback |
| `ast.rewrite.apply` | records→records | execute | ast-grep only; transactional, generation-guarded |
| `code.search` | ∅→sites | observe | regex over repo files |
| `code.refs` | ∅→sites | observe | jedi → ast fallback |
| `code.callers` / `code.callees` | ∅→sites | observe | ast call graph |
| `code.impact` | ∅→sites | observe | ast call graph, depth ≤ 6 |
| `code.related_tests` | files\|sites\|records→files | observe | path heuristic |
| `test.run` | ∅→records | execute | run-capture + family extractor (birth-gate) |
| `evidence.join` | ∅→records | observe | facts Angle-lite joins (`args.on`: `failing_in_changed` \| `untouched_failures` \| `shared_cause_groups` \| `symbol_neighbors`) |
| `evidence.group` / `.count` / `.top` / `.where` | rows→records | observe | q combinators |
| `q.pipe` | ∅→records | observe | any existing `ctx q` pipeline as one node |
| `semantic.search` / `.taint` / `.policy_scan` | sites\|files\|records→records | observe | Semgrep only (`[sem]` extra); absent ⇒ declared skip |

Engine choice is never a model decision — the plan IR has no engine
field. The chosen engine is disclosed per node in coverage, and
participates in the node's cache key.

## Validation rules (static; fail before execution, never during)

- **Edges are backward-only.** `input`/`after` may reference only
  declared upstream step ids; cycles are unrepresentable and rejected.
- **≤ 24 nodes per plan** (`MAX_NODES_HARD`; config may lower it, never
  raise it) — the totality bound. Split a larger investigation into
  epochs instead.
- **`foreach` requires an explicit `cap`**, `1 <= cap <= 64`
  (`MAX_FANOUT_HARD`). Uncapped or over-cap fan-out is a typed rejection,
  not a runtime surprise.
- **`when` admits only the guard micro-grammar**: `<node>.count <op>
  <int>` (`op` ∈ `== != >= <= > <`) or `<node>.outcome ==|!= pass|fail`.
  No arbitrary predicates — computed control flow stays in `ctx eval`,
  off the plan language entirely.
- **Execute-class ops are CLI-only.** `test.run`, `ast.rewrite.preview`,
  `ast.rewrite.apply` require the CLI tier; an MCP-tier plan containing
  one is a rejection, not a silent downgrade to observe.
- **Absent engines degrade by declaration.** `on_missing`:
  `degrade` (labeled fallback) | `skip` (declared, counted in coverage) |
  `fail` — never a silent gap.

## When to compile a plan vs. stay interactive

Compile a plan when you would otherwise run 3+ exploration commands whose
sequence you can already name up front. Stay interactive — `run`,
`search`, `get`, `stats`, `def`/`refs` — when each result changes what you
would do next; a static DAG cannot adapt mid-execution.

## Epochal discipline

One reconnaissance plan per hypothesis epoch. At most one causal replan
when the first evidence overturns the working hypothesis (`--replans 1`
by default — a budget, not advice; the replan reuses the unchanged node
cache). After that: patch, then verify — never a third planning epoch.
Exceeding the replan budget is declared and recorded, not silently taken.
