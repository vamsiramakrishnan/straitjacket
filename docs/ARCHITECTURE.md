<sub><a href="README.md">« straitjacket / docs</a></sub>

# Architecture & code map

Where things live in the code, and which file to open for a given change.
`src/ctx/` is a flat package of ~60 modules; this page is the map. If you're
here to make a change, start with the "which file do I touch" table, then read
the plane your change lands in.

New to the design ideas themselves? Read [How it works](HOW-IT-WORKS.md) and
[Core concepts](CONCEPTS.md) first — this page assumes that vocabulary.

## The six planes

Every module belongs to one of six planes. A change belongs in one plane,
inherits that plane's invariants, and ships with a test.

| Plane | Owns | You'd change it to… |
|---|---|---|
| **Safety** | hard, non-adaptive limits: path, process, storage, secret | tighten confinement or redaction |
| **Execution** | commands, jobs, capture, host integration | add a capture surface or host adapter |
| **Derivation** | symbols, references, facts, queries | add an indexer or fact producer |
| **Evidence** | extraction, coverage, contracts | add a command-family profile |
| **Delivery** | plans, budgets, rendering, retrieval | add a deterministic renderer or plan op |
| **Behaviour** | interventions, outcomes, reflex, policy | add a measured reflex or scorecard metric |

## Which file do I touch for…

| I want to… | Start in |
|---|---|
| add a digest profile (pytest-like) for a new tool | `src/ctx/digest/<family>prof.py` + `src/ctx/contracts/<family>.toml` — see [Writing a profile](WRITING-A-PROFILE.md) |
| change how a command is classified/steered | `src/ctx/hook.py` (the PreToolUse guard — stdlib-only hot path) |
| add or change a CLI verb | `src/ctx/cli.py` (its parser block + one row in `_COMMANDS`) + a handler in `src/ctx/commands/` + its one-liner in `src/ctx/cliux.py` |
| add a `ctx q` stage | `src/ctx/query.py` |
| add an evidence-plan operator | `src/ctx/plan_ops.py` (+ `plan_ir.py`, `plan_exec.py`) |
| change how a digest is selected/sized | `src/ctx/resolver.py` (the Delivery Policy Resolver) |
| change retrieval (`get`/`search`/spans) | `src/ctx/retrieval.py` + `src/ctx/_retrieval/` |
| change how a `repo:` line address stays valid across edits | `src/ctx/anchors.py` |
| measure whether the host's own edits are landing | `src/ctx/edit_outcomes.py` |
| change what happens when a node does not finish | `src/ctx/steward.py` (classifier + menu) · `src/ctx/recovery_policy.py` (the choice) |
| change prewalk (frontier → cheap handoff after one edit) | `src/ctx/orchestrator.py` (`run_one`'s prewalk branch, `PREWALK_SENTINEL`) · `src/ctx/steward.py` (`de_escalation_target`) |
| change what harnesses record about a collaboration, or resume one | `src/ctx/taskledger.py` · `src/ctx/orchestrator.py:run_route` |
| change the artifact store | `src/ctx/store.py` |
| change path confinement or secret redaction | `src/ctx/workspace.py` / `src/ctx/textutil.py` |
| add a code-navigation verb (callers/refs/def) | `src/ctx/codeverbs.py` / `src/ctx/callgraph.py` |
| change host setup / plugin rendering | `src/ctx/installer.py`, `src/ctx/wrap.py` |
| change the MCP tool surface | `src/ctx/mcp.py` |
| change the session scorecard | `src/ctx/scorecard.py` |

## Safety — hard, non-adaptive limits

No behavioural signal may weaken anything here.

| Module | Responsibility |
|---|---|
| `workspace.py` | Workspace resolution, identity, path confinement; absolute paths never leave here in model-visible form (SPEC §5). |
| `hook.py` | The PreToolUse context guard — runs on the hot path of every intercepted tool call; **stdlib-only** for latency and reliability (SPEC §10.2, §11). |
| `textutil.py` | Deterministic text: token estimation, ANSI/control stripping, **secret redaction**, bounded emission (SPEC §8, §16). |
| `surface.py`, `surface_profiles.py`, `surface_gateway.py`, `surface_reconcile.py` | The input side: `ctx surface` capability-context audit, minimal-surface compilation, the progressive-disclosure MCP gateway, and shadow reconciliation. |

## Execution — running and capturing work

| Module | Responsibility |
|---|---|
| `execution.py` | Birth-time capture runner; output spools to disk and is content-addressed before the model sees it (SPEC §6.2, §7). |
| `store.py` | The content-addressed artifact store and quota enforcement (SPEC §12). |
| `jobs.py` | Long-runner backgrounding: `ctx run --bg`, `ctx job(s)`. |
| `seq.py` | `ctx seq` — declared command trees (round economy without losing gates). |
| `pyeval.py` | `ctx py` — programmable capture; a Python script runs under the birth gate, only its digest returns. |
| `wrap.py` | `ctx wrap <host>` — run an agent under the harness, ephemerally. |
| `installer.py` | Plugin rendering, installation, and health checks; `ctx doctor`, `ctx antigravity install` (SPEC §4, §18). |
| `proxy.py` | The Tier-0 observer proxy: byte-exact relay for API traffic that measures wire ground truth. |
| `rescue.py` | Lossless mid-session rescue: epoch-latched transcript elision (Tier-1). |
| `mcp.py` | The bounded MCP retrieval server — one tool schema with an `op` discriminator. |
| `cli.py`, `__main__.py` | CLI entry, the front door, argument parsing, and the `_COMMANDS` dispatch table (the `hook` subcommand is dispatched before argparse, for latency). |
| `commands/` | One module per verb family holding the command bodies. The table maps a command to a module *name*, so an invocation imports only the family it needs — and every dependency stays inside the function that uses it, for the same reason. |
| `config.py` | Repository policy: committed `ctx.toml` plus hard defaults (SPEC §13). See [Configuration](CONFIGURATION.md). |
| `statusline.py` | Host-neutral status-line rendering. |

## Derivation — producing repository facts

| Module | Responsibility |
|---|---|
| `facts.py` | The typed fact store and Angle-lite joins, in per-workspace SQLite. |
| `skeleton.py` | The tree-sitter skeleton tier: imports, types, signatures with line ranges. |
| `callgraph.py` | The deterministic call graph behind `ctx callers/callees/impact`. |
| `codeverbs.py` | Symbol-addressed verbs `ctx def/refs/diag` (jedi backend, AST fallback). |
| `query.py` | `ctx q` — the composition algebra: a total pipeline, ≤ 8 stages, no loops. |
| `filesets.py` | The file-set algebra (`corpus` source, `repo.files` op); fd engine with a Python fallback. |
| `repomap.py` | The ranked repository map (damped-PageRank over the reference graph). |
| `astgrep.py`, `semgrep_engine.py` | Structural- and semantic-search engine tiers behind logical plan ops. |
| `scip_ingest.py`, `_vendor/scip_pb2.py` | Opportunistic SCIP cross-reference ingestion; degrades to none if absent. |

## Evidence — extracting typed findings

This is the plane most contributions touch. The flow is
**extractor → `EvidenceGraph` + coverage → Evidence Contract → (resolver) → renderer**.

| Module | Responsibility |
|---|---|
| `evidence.py` | The typed evidence layer: `EvidenceGraph` / `EvidenceItem` / `CoverageReceipt` — the seam between extraction and everything downstream (EDC §5). |
| `contracts.py` | Evidence Contracts: per-outcome REQUIRED / PREFERRED / RETRIEVABLE fact classes, validated over typed facts at the selection seam (EDC §5.3). |
| `contracts/*.toml` | The committed contracts: `pytest.toml`, `lint.toml`, `investigate.toml`, `generic.toml`. |
| `digest/base.py` | Shared profile machinery: the `Profile` base class, `DigestContext`, `StreamView`. |
| `digest/__init__.py` | The **profile registry** (`_PROFILES`) and `detect_profile` / `render_run_digest`. |
| `digest/pytestprof.py` | The pytest profile (pass path `pytest/v1`; census failures `pytest/v2`) — the reference EDC instance. |
| `digest/lintprof.py`, `logprof.py`, `jsonprof.py`, `tableprof.py`, `searchprof.py`, `moreprofs.py` | Profiles for diagnostics, logs, JSON/JSONL, tables, search results, and go/cargo/jest/git-diff families. |
| `digest/text.py` | The generic text profile — the universal deterministic fallback (`text/v1`). |
| `rundiff.py` | `ctx diff run:A run:B` — structural run-to-run regression digest. |

## Delivery — selecting and rendering views

| Module | Responsibility |
|---|---|
| `resolver.py` | **The Delivery Policy Resolver** — the single choke point where every ladder composes into a `DeliveryPlan` (EDC §5.4). |
| `digest/evidence_render.py` | The plan-obeying **pure** renderer for census-grade profiles: `(graph, contract, plan) → bytes`. |
| `plan_ir.py`, `plan_ops.py`, `plan_exec.py` | The compiled evidence-plan IR (a total, bounded DAG), its logical operators, and the executor + `investigate/v1` digest. |
| `ask.py` | `ctx ask` — intents as typed plan presets (the seven intents). |
| `edit_outcomes.py` | What happened to the host's own Edit/Write: a closed-vocabulary classifier over the tool result and a privacy-safe rate ledger. Observation only. |
| `edit_transactions.py`, `commands/edit.py` | `ctx edit plan\|preview\|apply`: a sealed, anchor-verified edit transaction (compare-and-swap on content, not fuzzy patching) — CLI-only by the same invariant that keeps `ctx run` the one path to filesystem mutation. |
| `anchors.py` | Content anchors and line tags: the verify → relocate → refuse ladder that keeps a `repo:` line address meaningful after an edit. Pure and total ([ANCHORS.md](ANCHORS.md)). |
| `retrieval.py`, `_retrieval/` | Bounded `ctx search` / `get` / `stats` / spans: deterministic, budget-capped, provenance-bearing (SPEC §6.3–6.5). |
| `refs.py` | The reference/handle grammar (`run:…#stdout`, spans) (SPEC §6.1). |
| `substitute.py` | The collapse substitution layer (the replacement surface). |
| `prefixassets.py` | The prefix-stability contract: every byte injected into the prompt prefix is locked behind a manifest. |
| `pricing.py`, `data/model-prices.json` | Host-neutral model pricing. |
| `debt.py` | `ctx debt` — declared omission for engineering scope. |

## Behaviour — measuring agent response

| Module | Responsibility |
|---|---|
| `reflex.py` | The reflex arc: deterministic behavioural detectors, an append-only outcome ledger, and the densify-on-starvation latch. |
| `policy.py` | Learned policy epochs: run telemetry compiled into committed policy. |
| `engagement.py` | Graduated engagement: scaling the harness footprint to measured task scale. |
| `evidence_outcomes.py`, `plan_value.py` | Deterministic evidence→follow-up association and per-operator follow-up statistics. |
| `scorecard.py` | The session scorecard: cache / cost / effort economics from `wire.jsonl`. |
| `replay.py` | The session-history replay learning loop (`ctx replay --regret/--outcomes`). |
| `checkpoint.py` | Epoch checkpoints: freeze task state — goal, decisions, evidence handles (SPEC §14). |
| `taskledger.py` | The task ledger: six closed-vocabulary row types, append/load/fold, the inbox. The bus harnesses collaborate over ([TASK-LEDGER.md](TASK-LEDGER.md)). |
| `steward.py` | Typed failure classification and the action menu for the recovery policy; every decision is a ledger row before it is acted on. |
| `recovery_policy.py` | The promoted AlphaEvolve `choose_recovery` seam: retry / escalate / re-plan / honest stop, by failure kind and remaining budget. |
| `orchestrator.py`'s prewalk branch, `steward.py`'s `de_escalation_target` | Prewalk: a frontier model plans and makes one edit, then hands the same node off to the cheapest cheaper model installed ([PREWALK.md](PREWALK.md)). |

## The native hook

`native/` holds an optional Rust implementation of the PreToolUse hook (~3 ms
vs ~29 ms for Python), parity-tested against `hook.py`. It's an accelerator, not
a requirement — the Python hook ships the same decision on every code path.

---

For the invariants every change is reviewed against, and how to run the tests,
see [`CONTRIBUTING.md`](../CONTRIBUTING.md). For the normative contracts, see
[`spec/`](../spec/).
