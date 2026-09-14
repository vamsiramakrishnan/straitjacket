# Code search: the index, the query language, history as evidence, and the pack

This page covers the retrieval substrate added after the DeepSWE receipts
showed where a harnessed session spends its first ten turns: grep a term, read
a file, grep another, read the wrong file, read the right one. Every one of
those turns re-sends the cached prefix and leaves a tool result in the
transcript for the rest of the session. The substrate answers *where would I
look* cheaply, repeatedly, and without going stale.

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

Segments accumulate as files change; past sixteen the index compacts by
re-reading the live files (the cheapest correct merge, and it re-verifies).
`CTX_SEARCH_INDEX=off` disables the whole tier; `CTX_SEARCH_ENGINE=rg|python`
bypasses it for one search.

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

Sites in files whose hash still matches come from the index — exact. Sites in
files that changed or are new come from the textual rung, restricted to those
files. The answer is complete, and each part says what it is. `ctx def`
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

### What a Cody-style context engine gets wrong, measured

Sourcegraph's Cody assembles context by embedding or keyword-retrieving chunks
and stuffing the top-k into the prompt. `evals/agentbench/pack_recall.py`
measures the alternative here against a ground truth that does not care about
anyone's taste: for each validated DeepSWE task, the source files the reference
solution actually changes, at the base commit, with no model involved. Three
rankers: `grep-count` (files by how many task identifiers they contain — the
keyword baseline), `pack-nohist`, and `pack`.

| ranker | recall@5 | recall@8 | MRR | first hit in top 8 |
|---|---:|---:|---:|---:|
| grep-count | 0.376 | 0.429 | 0.547 | 14/14 |
| pack (no history) | 0.445 | 0.540 | 0.641 | 12/14 |
| pack | 0.445 | 0.540 | 0.641 | 12/14 |

Over 14 tasks and 51 existing gold files (`evals/agentbench/results/pack_recall.json`);
17 further gold files are *created* by the fix and no ranker can surface them
(the ceiling is visible in the receipt, not hidden in the denominator). The
history signal reordered the pack in 10 of 14 tasks without moving any gold
file across the top-8 boundary — neutral on this set, kept because a commit is
evidence the model can open, and it costs one bounded `git log`. The first cut of the pack lost to the
keyword baseline (recall@8 0.36, MRR 0.44): it trusted trigram candidates as
presence, so `logo/logo.svg` led one task's pack, and it summed set-presence
so a changelog that mentioned every term once outranked the module that used
them. Verifying whole-word counts and demoting prose fixed both; the
measurement, not a hunch, said which.

The issues this design avoids, by construction rather than by tuning:

| Cody-shaped issue | Here |
|---|---|
| The embedding index describes a commit; edits after it are invisible until a rebuild nobody triggers | fingerprint sweep before every pack; outlines from the current bytes |
| Chunks are cut by line count and land mid-symbol | rows are files with their symbol table; the next read is a symbol or a range |
| Top-k by similarity floods the prompt with near-duplicates and tests | one row per file, tests and prose demoted, budgeted rendering with declared omission |
| The model cannot tell why a chunk is there | every row carries its terms, symbols, path hits and commits |
| Retrieval at turn one is the only retrieval | the pack is a verb the model can run again, and the same index answers `search`, `refs` and `sym:` |

### Staleness trade-offs, stated

| approach | stale window | cost per query | wrong answers possible |
|---|---|---|---|
| grep the tree | none | a full scan | no |
| index rebuilt on demand (SCIP, embeddings) | until someone rebuilds | none | yes, confidently |
| watcher-driven reindex | until the watcher catches up; nothing if it died | none | yes, silently |
| **this index** | none at query time | a stat sweep (6 ms / 870 files, ~250 ms / 50k) + reads of the candidates | no: candidates are verified |
| **SCIP per-file** | none for unchanged files; changed files answered textually and labelled | a hash comparison against the text index's catalog | no: each part says what it is |

The remaining trade-off is the first build on a very large tree, which is
explicit (`ctx index --text`) and reported (`ctx doctor`).

## `ctx agent`: ctx as the host

`ctx wrap claude` keeps the host and shapes what flows through it. `ctx agent`
is the other end of the same idea on the [Claude Agent
SDK](https://docs.claude.com/en/api/agent-sdk/overview): ctx owns the tool
surface (Bash, Read, Edit, Write, MultiEdit plus in-process `search`,
`outline`, `get`, `refs`, `pack`), the system prompt, the first turn (the task
and a pack), and the same PreToolUse/PostToolUse containment the wrapper
installs — with no proxy and no native Grep/Glob for the model to reach for
first. It does not call the Messages API itself: the SDK drives the same
`claude` binary `claude -p` runs, so sessions are billed, cached and
transcribed the way the host does it, on the host's own login.

```bash
pip install 'ctx-harness[agent]'
ctx agent -p "Add partial_structure to BaseConverter …" --model haiku --max-turns 60 --output-format json
ctx agent -p @task.md --no-pack        # the ablation
```

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
