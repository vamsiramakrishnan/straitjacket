# Code search and symbol navigation

Start with the question the agent needs to answer. A task description can rank
candidate files. A symbol name can locate its definition and uses. An error
message can find matching lines in the working tree or a captured run. Each
tool returns a bounded result that the agent can inspect before reading further.
[Walk through a symbol lookup](USE-CASES.md#find-the-code-behind-a-change)

| Piece | Command | What it answers |
|---|---|---|
| Text index | `ctx index --text`, `--status` | which files may contain this, which define that name |
| Query language | `ctx search repo: … file: lang: sym: case: type:` | search with filters, in one grammar everywhere |
| History | `type:commit`, `type:diff`; `q` stages `commits`, `touched`, `history` | which commit said or changed this, with the files it touched |
| SCIP per-file currency | `ctx refs`, `ctx def`, `ctx impls`, `ctx index --status` | exact answers survive an edit, per file, disclosed |
| Context pack | `ctx pack "<task>"` | where to look first, ranked, with the reason |
| ctx as the host | `ctx agent -p "<task>"` | the same substrate as the model's only retrieval tools |

## The text index

`src/ctx/codeindex.py` is a Zoekt-shaped index: 24-bit trigrams over the
lowercased bytes of every eligible file, a symbol table per file (from the
skeleton backends: tree-sitter, ctags or stdlib ast), and a fingerprint per
file (size, mtime, content hash). It lives in the store's `indexes/` area,
never in the worktree, in immutable segments plus a small catalog.

Measured on this repository (868 files, 11.5 MB of text):

| | |
|---|---|
| first build (trigrams + symbols) | 6 s |
| resync before a query (stat sweep) | 45 ms |
| candidates for `TokenBucket` | 17 files in 5 ms |
| candidates for `def resolve_refs\(` | 6 files in 1 ms |
| index size | 3.4 MB |

### The staleness contract

An index is allowed to exist only as a *candidate generator*. Three rules
keep it from ever answering about a tree that is gone:

1. **Fingerprint sweep before every query.** `Index.sync` stats every
   eligible file, re-indexes the ones whose `(size, mtime)` moved *and* whose
   content hash differs, and drops the ones that are gone — into a new segment,
   with the catalog repointed. The sweep is 6 ms per 870 files, so it is
   charged on every query rather than trusted to a watcher or a daemon.
2. **Verification against live bytes.** A candidate is a file that *may*
   match. `ctx search` runs the real pattern over the file's current bytes; the
   pack counts whole-word occurrences in them. A stale posting costs a wasted
   read, never a wrong answer.
3. **Superset by design.** Trigrams are taken over lowercased bytes, so a
   case-sensitive pattern's candidates are a superset of its matches; a regex
   contributes only the literal runs a match *must* contain (the parse tree's
   literal runs, AND across concatenation, OR across alternation, nothing from
   `.*`); a pattern with no run of three literal bytes is answered by a full
   scan, and the coverage line says so.

So there is no "re-index on every file change" hook, because there does not
need to be one: the query re-indexes what changed, at the moment it is asked,
and nothing it says is unverified. The cost of that choice is the sweep — for
a 50k-file tree about 250 ms per query — and the first build, which above
4,000 files or 48 MB is not done implicitly inside a retrieval verb (a hook's
latency budget) but by `ctx index --text`; `ctx doctor` and `ctx index
--status` say which case you are in.

Files over 1 MiB are not indexed (a log or a data dump has trigrams of
everything). A search over at most eight files (`ctx search repo:server.log
PATTERN`, or a small directory or glob) scans those files directly, in blocks of
whole lines up to 256 MiB per file, and the coverage line says "N over the 1 MiB
index limit scanned directly". A wider search does not scan them; it says "N files
over the 1 MiB index limit were not searched: …" instead of reporting zero matches
for files it never read.

Segments accumulate as files change; past sixteen the index compacts by
re-reading the live files (the cheapest correct merge, and it re-verifies).
`CTX_SEARCH_INDEX=off` disables the whole tier; `CTX_SEARCH_ENGINE=rg|python`
bypasses it for one search.

### Branch catalogs

Git workspaces keep a separate trigram and symbol catalog for each branch.
The key is a hash of the worktree's HEAD reference, so branch names never become
paths. Detached HEAD uses the commit ID. Linked worktrees have separate workspace
stores and use their own HEAD file. An open index detects a branch switch again
at sync, closes its old segments and loads the matching catalog.

Switching back still checks every live file. Unchanged content reuses its segment
and parsed symbols even if checkout changed its timestamp; edits, additions and
deletions update that branch's catalog. Dirty files are included, so this is a
cache of the working tree, not a committed-code snapshot or cross-branch search.
`ctx index --status` includes the selected branch and catalog directory.

Catalogs persist in `indexes/trigram/branches/` within the workspace store. Each
catalog compacts its own segments, but inactive branch and detached-commit
catalogs are not automatically evicted. They are disposable caches: when no
index commands are running, removing an inactive catalog directory only costs
a rebuild on its next use. This does not partition SCIP databases or claim
branch-aware type resolution for external language servers.

## The query language

Filters ride in the pattern list, so the same grammar serves `ctx search`,
the `q` `search` stage, `ctx pack` and the `ctx agent` search tool:

```bash
ctx search repo: TokenBucket file:src/ '!file:tests' lang:python
ctx search repo: 'rate limit' case:no
ctx search repo: sym:resolve_refs                  # the definition sites, from the symbol table
ctx search repo: take sym:TokenBucket              # a pattern, within the files defining a symbol
ctx search repo: type:commit "prefix tax"          # commits whose message says so
ctx search repo: type:diff ENABLE_TOOL_SEARCH file:src/   # commits whose change carries it
ctx search repo: type:commit limiter after:2026-08-01 author:Ada
```

| filter | meaning |
|---|---|
| `file:` / `path:` | a glob when it looks like one, else a regex searched in the path |
| `!file:` / `-file:` | exclude (the `!` spelling survives argparse; `-file:` needs `--` first) |
| `lang:` / `!lang:` | by the skeleton's language table (`py`, `ts`, `rs`, `go`, … aliases) |
| `case:no` | case-insensitive (`case:yes` is the default) |
| `sym:Name` | files whose symbol table defines `Name`; alone, the definition sites themselves |
| `type:commit` / `type:diff` | git history: messages, or the change (`git log -G`) |
| `after:` `before:` `author:` `rev:` | git's own options, passed through |

A token whose key is not a filter stays a pattern (`re:foo` is searched for),
and filters alone select nothing — the error says so rather than returning the
corpus.

## History as evidence

A commit is evidence with a hash, a date, an author, a subject and the files it
touched, rendered in the same bounded, deterministic shape as a hit, minted as
a `blob:` the model can cite:

```
[ctx search repo: · history]
patterns: 'ENABLE_TOOL_SEARCH' · file:src/ type:diff
  2467ed5 2026-09-13 Claude · Measure the host's prefix on the wire, and probe wrapper parity
    files: src/ctx/wrap.py
  800f2a7 2026-09-13 Claude · wrap: keep tool deferral through the proxy; native-shaped hook rewrites
    files: src/ctx/wrap.py
coverage:
  commits: 2 (newest first, bounded at 80)
```

In `ctx q`, history composes with everything else:

```bash
ctx q 'commits "rate limit" | touched | top 5 | outline'    # commits → files they touched → outlines
ctx q 'search TokenBucket file:src | history'               # each site with the commit that last changed its file
ctx q 'search retry lang:python | history --line'           # per line (git blame), first 24 files
ctx q 'commits validator type:diff after:2026-01-01 | touched'
```

Nothing here invents a database: git already keeps the history index; the
shell-out is bounded by `--max-count` and a timeout, and outside a git
workspace the answer is a clean error.

## SCIP per-file currency

A SCIP index used to be all-or-nothing: the first edit after `ctx index` made
the exact tier vanish for the whole tree ("stale index skipped, re-run ctx
index"). Now `ctx index` records the content hash of every source file it
described, and an edited tree is answered in two parts:

```
[ctx refs helper · engine scip (exact) · 1 changed file via ast (textual)]
```

Sites in files whose hash still matches come from the compiler index. A
textual scan covers changed and new files. The header identifies the mixed
result. Inspect textual matches before treating them as references. `ctx def`
reaches the exact tier the same way; `ctx impls` has an exact rung from the
index's own `is_implementation` edges; `ctx index --status` lists the files
the index no longer describes. An index without a per-file basis (a project's
own, or one built before this) keeps the all-or-nothing rule — see "An index
goes stale" in the CLI guide.

## `ctx pack`: where to look first

```
ctx pack "Add partial_structure to BaseConverter; return a PartialResult with structured_fields …"
```

renders the files a task points at, ranked, with each file's outline and the
reason it is there:

```
[ctx pack · 8 of 71 candidate files · index trigram · 170 files · 20 ms sync]
task: Add `partial_structure` to `BaseConverter` (and top-level). Returns a…
terms: PartialResult(5.1) failed_fields(5.1) structured_fields(5.1) error_map(5.1) …
1. repo:src/cattrs/converters.py · 1,588 lines · score 95.1
   why: terms structure, Converter, BaseConverter, structured (+7) · defines BaseConverter, Converter · commit bc1a458 "Tin/typed dicts (#364)"
   class BaseConverter L64
   function structure L446
   …
```

Signals, all from things ctx already has (nothing embedded, nothing learned):

- **terms** from the task text — identifiers and words, weighted by where they
  come from (a backticked span 2.0, a code-shaped identifier 1.5, a word 1.0,
  a compound's sub-words 0.6) and by rarity across the corpus (idf from the
  index); presence is a whole-word count in the file's bytes, never a trigram
  guess, so an SVG is not a hit for everything;
- **symbols** — a term that *names* a symbol in a file (the index's symbol
  table, exact or case-insensitive exact, never substring);
- **paths** — a term in the path, or a path the task names;
- **history** — commits whose message names the strongest terms; the files
  they touched carry the commit, weighted down by how many files it touched;
- **shape** — tests demoted unless the task is about tests, prose demoted
  unless it is about docs, long files mildly normalized.

The rendering is budgeted (`--budget`, default 2,500 tokens) and the omission
is declared; `--json` is the machine form the agent runtime uses.
The text renderer caps output at four characters per requested token, including
headers and the continuation. This is an estimate, not a model tokenizer count.
JSON retains the bounded selected records and is not fitted to the text budget.

The first three files now include up to eight lines near a matched definition
or term when the excerpt budget permits. Each excerpt comes through ordinary
`ctx get`, including its immutable snapshot address, so the original bytes stay
retrievable after an edit while the artifact is retained. At most half of the
requested budget is allocated to excerpts; ranking and outlines keep priority.

For those matched definitions, the pack also loads the existing cached call
graph once and includes up to four scoped caller/callee sites each. The lookup
uses the definition's file and line, so unrelated same-named functions do not
merge. Relationship rows report their local/import resolution tier and any
remaining ambiguity. Repository-wide name guesses are excluded. These are
static, AST-derived facts, not LSP type-resolution guarantees. The graph's
5,000-file cap and the languages of installed parsers still apply. A first pack
can therefore cost more than the historical index-only timings above; later
packs reuse graph extraction. No embedding service or model call is involved.
On the four-file workflow fixture, one pack returned the source, caller, callee
and snapshot in 1,715 bytes. The previous workflow used three operations and
returned 1,385 bytes. Bundling saved two tool calls and returned 330 extra
bytes. The static graph also missed a caller imported under an alias. Use
LSP-backed references when symbol identity matters. The replay made zero model
calls. [Workflow fixture and results](../evals/pack-workflow-2026-10-29.json)

Reproduce the comparison from this repository:

```bash
python -m evals.pack_workflow --output /tmp/pack-workflow.json
```

### From discovery to a verified repair

Use `ctx pack "repair structure_attrs for missing fields"` to discover entry
points, source and relationships in one request. An SDK host can then use the
existing `ToolSession.repair_model` operation to apply a multi-file anchored
request and run targeted plus regression checks in one call:

```python
from ctx.edit_verification import Check

# session is a ToolSession; failure_ref names an observed failing run.
# anchored_request contains the reviewed edits with their source anchors.
result = await session.repair_model(
    failure_ref,
    request=anchored_request,
    targeted=[Check("behavior", ("python", "-m", "pytest", "tests/test_repair.py"))],
    regression=[Check("behavior", ("python", "-m", "pytest", "tests/"))],
    witnesses=["tests/test_repair.py"],
    operation_key="repair-missing-fields",
)
```

The first targeted command must match the observed failing command. The request
uses the existing compare-and-swap edit transaction; changed anchors are refused.
Checks run against the applied bytes, and failure stops subsequent checks.
The bounded result names the complete proof rather than requiring a verification
read per file. Syntax or LSP diagnostics do not substitute for behavioral tests.
Use `repair` for the structured full receipt or `edit_receipt=` to verify an
already applied transaction. This SDK operation needs local edit/command
authority; the bounded MCP retrieval tool does not gain mutation permissions.
See [the SDK repair contract](TOOL-SDK.md#repair-with-evidence).

### File ranking on 14 DeepSWE tasks

The evaluation compared each task's ranked files with the source files changed
by its reference fix. It scored 51 source files that existed at the base
commits. The fixes created another 17. Recall is the fraction of existing
reference files retrieved, averaged across tasks. Mean reciprocal rank (MRR)
measures how early the first relevant file appears.
[Evaluation code](../evals/agentbench/pack_recall.py) · [Per-task results](../evals/agentbench/results/pack_recall.json)

| Ranker | Recall at 5 | Recall at 8 | MRR | Tasks with a relevant file in the top 8 |
|---|---:|---:|---:|---:|
| Files ranked by task identifier count | 0.376 | 0.429 | 0.547 | 14/14 |
| `ctx pack --no-history` | 0.445 | 0.540 | 0.641 | 12/14 |
| `ctx pack` | 0.445 | 0.540 | 0.641 | 12/14 |

History changed file order on ten tasks and left the top-eight recall
unchanged. The pack retrieved a larger share of relevant files on average.
Identifier counts found at least one relevant file on two tasks the pack
missed. The evaluation measured file selection. It made no model calls.

### Working-tree edits

The text index checks file size and modification time on each query and
reindexes changed content. Search verifies candidate matches against current
bytes. On trees above the automatic build limit, run `ctx index --text` to
build the index explicitly. Check `ctx index --status` for coverage. For
compiler-backed references after edits, refresh the SCIP index with `ctx index`.

## `ctx agent`: ctx as the host

`ctx wrap claude` keeps the host and shapes what flows through it. `ctx agent`
is the other end of the same idea on the [Claude Agent
SDK](https://docs.claude.com/en/api/agent-sdk/overview): ctx owns the tool
surface (Bash, Read, Edit, Write, MultiEdit plus in-process `search`,
`outline`, `get`, `refs`, `pack`), the system prompt, the first turn (the task;
with `--pack` a pack too — off by default, because the uncapped DeepSWE
receipt found haiku follows the pack's ranking into the wrong file and passes
fewer tests), and the same PreToolUse/PostToolUse containment the wrapper
installs — with no proxy and no native Grep/Glob for the model to reach for
first. It does not call the Messages API itself: the SDK drives the same
`claude` binary `claude -p` runs, so sessions are billed, cached and
transcribed the way the host does it, on the host's own login.

```bash
pip install 'ctx-harness[agent]'
ctx agent -p "Add partial_structure to BaseConverter …" --model haiku --max-turns 60 --output-format json
ctx agent -p @task.md --pack           # a ctx pack in the first turn (off by default)
```

A model trained on `grep` reaches for `grep`: in the first DeepSWE sessions of
this runtime the ctx tools were called 0 to 6 times against 5 to 13 shell greps
through Bash, so the arm measured naive-without-Grep, not the index. The
runtime therefore carries an in-process PreToolUse router: `grep`, `rg`, `ag`
and `git grep` in Bash are refused with a reason that contains the translated
`search` call (the pattern, paths as `file:` filters, `-i` as `case:no`), and
the model's next call goes through the index. Measured on the sessions that
ran with it, ctx tool calls rose to 2–16 per session with `search` at 2–11.

The result JSON is the host's shape (`num_turns`, `usage`, `total_cost_usd`,
`modelUsage`) plus `pack` (the files the first turn carried) and `runtime`, so
`evals/agentbench/harness.py` reads a `ctx agent` session like a `claude -p`
one; the `sdk` and `sdk_nopack` arms are that. Measured on a two-file
workspace with haiku: a first request of ~9.9k cached prefix tokens against
~12.7k for `claude -p` with its default surface, one tool call, $0.023.

What the SDK route cannot do, and why it is still the right first step, is in
the DeepSWE receipt's discussion of ctx as an SDK: cache breakpoints, context
editing and programmatic tool calling need the Messages API directly, which
needs an API key; the Agent SDK needs only the login you already have, and it
is where the tool surface and the first turn — the two levers the receipts
showed matter most — already live.
