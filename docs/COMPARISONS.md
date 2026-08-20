<sub><a href="README.md">« straitjacket / docs</a></sub>

# Comparisons

How straitjacket relates to the other tools in this space. We benchmarked the
mechanisms we could reproduce and desk-researched the others against their
public contracts. Measured claims live in [`evals/`](../evals/); vendor claims
are labelled and never move a straitjacket performance number. We record both
what was integrated and what still beats us.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/field-treemap.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/field-treemap-light.svg" width="100%" alt="A treemap of the field: Headroom, rtk, Caveman, Compaction, RAG/vectors, Ponytail, Maki and wozcode. Each tile names the tool's one good idea, its limitation, and — on an amber strip — the lossless form straitjacket adopted.">
</picture>

</div>

## The field, in one table

| Approach | What it does well | Limitation (measured where marked) | How we took it |
|---|---|---|---|
| Post-hoc compaction / summarization | reclaim a bloated window | rewrites history; evidence irrecoverable, prefix cache invalidated | checkpoint-then-rescue: secure handles first, then clearing is lossless |
| RAG / vector memory | recall without resending | probabilistic, no provenance | deterministic addresses: `run:<id>#stdout --lines 8412:8422` returns the same bytes forever |
| [**Headroom**](https://github.com/headroomlabs-ai/headroom) (wire proxy/library/MCP) | broad, low-integration transcript optimization; current releases advertise reversible originals | our reproducible 0.32.1 path dropped a quiet needle and churned cache; this is a dated benchmark, not a claim about current upstream | epoch-latched lossless rescue, exact addresses and prefix-stability gates; rerun current upstream before making a new comparison |
| [**rtk**](https://github.com/rtk-ai/rtk) (native command filter) | fast, wide command and host coverage; project-defined filters | filtered success output has no exact address for each omitted byte | safe equivalence substitutions plus structured command spans for git/GitHub/build/test families; unknown or mutating shapes remain fail-closed |
| [**Ponytail**](https://github.com/DietrichGebert/ponytail) (ruleset injection) | the solution ladder | advisory only; never measured whether the ladder held | ladder A/B-adopted on evidence (−28% turns, −33% time, −17% cost) + `ctx debt` |
| [**Caveman**](https://github.com/juliusbrussee/caveman) (terse prompting style) | say less | destroys evidence to save tokens — the quiet-needle anti-pattern | cite-don't-quote with resolvable handles (skill rules 11–12) |
| [**Maki**](https://maki.sh/) (sandboxed interpreter) | one script collapses N ops (their demo: 1300×) | no provenance: script and output vanish into the chat log | `ctx py`: script is an addressable `blob:`, streams span-addressed, tracebacks path-free |
| [**TokenSave**](https://tokensave.dev/) (semantic code graph) | one-call context, per-branch indexes, 50+ languages, broad editor reach and ambient savings ledger | semantic ranking is probabilistic; 80+ MCP operations require dynamic disclosure to avoid a large stable prefix | one stable `ctx` op surface, typed symbol/call/impact facts and billed-token accounting; branch graphs and semantic ranking remain gaps |
| [**WozCode**](https://www.wozcode.com/how-it-works) (Claude Code plugin) | combines glob/regex/read into ranked snippets; fuzzy batch edits with post-write syntax checks; SQL graph and session recall | host-specific; no exact omitted-byte address is publicly documented | compiled evidence plans and addressable AST rewrites; batch edit/validate and SQL graph workflows remain gaps |

*Rows for TokenSave and WozCode are desk research, not head-to-head runs —
their figures are their own claims. Marked as such in
[`evals/field-devex-2026-08-02.md`](../evals/field-devex-2026-08-02.md), which
is the receipt for this section and is explicitly not allowed to move any
performance number we publish.*

What each still does better than us is listed explicitly below; these are
product gaps, not design victories.

### Two places the field beats us on devex, stated plainly

**Distribution.** This gap is now closed: `pip install ctx-harness` installs the
published `ctx` CLI, then `ctx setup` performs the idempotent, non-destructive,
self-verifying host integration. Source `main` may be ahead of the PyPI badge;
the README states both rather than pretending an unreleased source version is
already published.

**Malleability.** Maki's users shape the agent from `init.lua` in user space.
Ours must edit `src/ctx/digest/<family>prof.py` and append to the `_PROFILES`
tuple in our source tree — i.e. **carry a fork** to teach the harness their
own test runner or in-house log format. For a project whose thesis is that
output families are diverse and deserve typed treatment, a closed profile
registry caps the system at the families we personally got around to writing.
Opening it is backlog item 2 in the scan above.

## Integration gap ledger (2026-08-20)

This is the durable output of the field scan. “Integrated” means a mechanism is
in code and tests; “partial” means straitjacket has the primitive but not the
neighbour's reach or UX. Claims about neighbours below come from their public
documentation and still need local, version-pinned reproduction before they
become benchmark claims.

| Source | Integrated now | Still missing | Next falsifiable mechanism |
|---|---|---|---|
| **rtk** | birth-gate interception, failure-asymmetric profiles, safe command equivalences, command-span capture | native single binary/Windows path, wider host adapters, user TOML filter packs, hook-integrity hash, deeper adoption analytics | load signed project filter packs through the profile registry; prove semantic equivalence and exact fallback on a frozen command corpus |
| **TokenSave** | one-call `ctx ask`, symbol/caller/callee/impact facts, incremental fingerprints, one dynamically dispatched tool, billed-token scorecards | semantic ranking, branch-local graph databases, cross-branch search/diff, background catch-up sync, cross-session code memory, editor breadth | key code-index generations by branch lineage and evaluate semantic candidate ranking behind exact file/symbol coordinates |
| **WozCode** | compiled multi-step evidence plans, addressable AST rewrite previews, syntax-aware analysis | combined find/read ranking, fuzzy multi-file edit + automatic syntax validation, SQL schema/FK graph, session recall, summarized subagent output | add an addressable edit transaction: preview → fuzzy apply → parser check → rollback receipt, measured against read/edit/verify loops |
| **Headroom** | lossless rescue, wire observer, prompt-prefix stability, addressed originals | a current version-pinned rematch, effort routing after routine outputs, learned compression policy, general proxy reach | rerun the quiet-needle/cache suite against current upstream before changing this comparison |
| **Ponytail** | measured solution ladders and enforced debt ledger | role-scoped injection, user-selectable policy intensity, broader host rules | compile host/role-specific minimal instruction cards and A/B prompt-prefix cost plus task completion |
| **Caveman** | cite-don't-quote narration while evidence stays exact | user verbosity levels for prose-only output | add a response-style dial that golden-tests code, commands and errors as byte-exact invariants |
| **Maki** | `ctx py`, provenance, bounded streams, declared orchestration, surface gateway | OS sandbox, resource caps, asynchronous tool gather, user-space plugin API | broker `ctx py` with CPU/memory/network policy and an addressable execution receipt |

### Friction found by using ctx on this change

The harness should learn from its own operator loop, not only other products.

| Observation | Cost | Durable response |
|---|---|---|
| A read-only `~/.local/state/ctx` made harmless commands fail before execution | every repository read required an approval/escalation retry | **fixed in source v0.33:** prove writability, select a sticky workspace-local fallback, expose it in `ctx doctor`, and test retrieval continuity |
| Four parallel retrievals raced catalog initialization with `database is locked` | parallel orchestration became less reliable than serial work | **fixed in source v0.33:** WAL initialization now has a bounded lock-only retry; non-lock database errors still fail immediately |
| `ctx ask` accepted a natural question but `impact` then demanded a subject; `compare` meant run receipts, not concept comparison | one avoidable tool round and misleading intent choice | **fixed in generated host guidance:** symbol-requiring intents and receipt-only compare semantics are explicit; natural-language guessing remains intentionally prohibited |
| A nested web/MCP call returned a ctx digest as a tool error despite the captured command exiting 0, and its run handle was not visible to the next CLI process | successful external evidence looked failed and could not be retrieved | **open, P0:** add a structured-tool adapter that preserves the host's success envelope while storing raw content, then prove cross-process handle resolution |
| Product/profile families are source-registered | teams must carry a fork for an internal log grammar | **open, P1:** signed declarative profile packs with deterministic golden fixtures and fail-open raw capture |

## How each neighbour is built — and where the harness diverges

The neighbours split into two architectural families. **Headroom** sits on the
wire and optimizes transcript history after bytes are already resident; current
upstream advertises reversible originals, while our pinned 0.32.1 benchmark did
not preserve the quiet needle through the exercised path. **rtk** and
**Caveman** cut earlier, at the shell hook or in the prompt, but throw the cut
bytes away. The harness's move is orthogonal to all three: capture at the
source into an immutable, addressable store, and put only a bounded digest —
plus a resolvable address for every omitted byte — on the wire.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/headroom-arch-light.svg" width="100%" alt="Two lanes. Top: an agent loop feeds a Headroom proxy that compresses messages and rewrites history on each call; the model sees a rewritten log and the quiet needle is silently dropped with no address. Bottom: straitjacket captures tool output at the birth gate into an immutable artifact store where every line is addressed, sends the model a bounded digest, and ctx get resolves any omitted line by address.">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/filters-arch.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/filters-arch-light.svg" width="100%" alt="rtk filters a flooding shell command at a fast bash hook and emits truncated output with no addresses; Caveman prompts the agent to narrate tersely, squeezing evidence into prose that cannot be resolved. Both feed into the harness's answer: keep the bytes in the store and carry a cited, resolvable handle, under a failure-asymmetric budget.">
</picture>

</div>

## The one we ran head-to-head — Headroom

Headroom is the only neighbour that is a drop-in library, so it is the only one
we can run behind our own observer. The needle-drop comparison is **model-free
and reproducible** — it exercises the compression/digest layer only, no LLM, so
it re-runs in a review sandbox in seconds
([`evals/headroom_needle_v2.py`](../evals/headroom_needle_v2.py)):

```bash
pip install -e '.[dev]' headroom-ai tiktoken
python evals/headroom_needle_v2.py
```

Rerun **2026-07-19 against the current `headroom-ai==0.32.1`** on a 20,001-line
log (302,628 tok) hiding one structurally rare "quiet needle" with no error
keyword ([receipt](../evals/headroom-needle-2026-07-19.md)):

| | Headroom 0.32.1 | `ctx run` logtemplate/v1 |
|---|---|---|
| Output | **357 tok** (847×) | **~520 tok** (584×) |
| Loud ERROR line | ✅ kept (keyword window) | ✅ kept, at `L17650` |
| **Quiet structural needle** | ❌ **silently dropped** | ✅ **verbatim at `L14238`** |
| Omission keeps an address | ❌ none | ✅ `ctx get run:<id>#stdout --lines 14238:14241` |

Headroom compresses harder and keeps the ERROR **because it announces itself**;
the quiet needle, structurally identical to an INFO line, vanishes with no
trace. `ctx` spends ~160 more tokens to buy the evidence that _doesn't_ announce
itself, plus an address for every omitted line — **needle-drop rate 100% vs 0%**
on this workload. (On the long task our mechanisms also beat Headroom outright:
42 turns / 243s vs 53 / 279s at comparable cost, per the
[2026-07-17 run](../evals/headroom-needle-drop-2026-07-17.md).) The same anomalous
line, drawn out under each approach:

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/fates.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/fates-light.svg" width="100%" alt="A 20,001-line log with one anomalous line. Compaction deletes it without trace. A rewriting proxy dropped it in every measured run. straitjacket's logtemplate profile kept it verbatim with an exact retrieval address.">
</picture>

</div>

## One hostile payload across seven containment strategies

The broader model-free comparison sends the same 302,628-token log through
seven delivery strategies. Headroom and straitjacket execute their real
implementations. Caveman, rtk, Ponytail and Maki are explicit models of their
documented strategy; they are not presented as third-party package benchmarks.

| Strategy | Output tokens | Compression | Quiet needle | Address after omission |
|---|---:|---:|---|---|
| Naive raw output | 302,628 | 1.0× | kept | none |
| Caveman head + tail | 1,219 | 248× | **dropped** | none |
| rtk-style loud-line filter | 361 | 838× | **dropped** | none |
| Ponytail-style advisory rules | 302,698 | 1.0× | kept | none |
| Maki-style anomaly script | 58 | 5,218× | **dropped** | none |
| Headroom 0.32.1 | 357 | 848× | **dropped** | none |
| **straitjacket `ctx run`** | **524** | **578×** | **kept** | **yes** |

The result is not “more compression is better.” It exposes three independent
properties: bounded output, survival of structurally quiet evidence, and a
resolvable address for omitted bytes. Only the straitjacket arm has all three
on this workload. See the [runner](../evals/field_needle.py), [machine
record](../evals/field-needle-record.json), and [dated
receipt](../evals/field-needle-2026-07-20.md).

## The workload curve from 20 real agent runs

A separate five-task Antigravity evaluation tested naive and harnessed agent
loops across two heavy floods, one medium flood, and two low-volume tasks. All
20 runs completed correctly.

| Output regime | Billed-token result | Direct tool-output effect |
|---|---:|---:|
| Heavy keyword flood | −71.9% | 186× less into context |
| Heavy quiet flood | −61.0% | 63× less into context |
| Medium traceback | −13.4% | 11× less into context |
| Small file read | within run-to-run noise | wrapper overhead becomes visible |
| Several small files | −4.2%, treated as neutral | harness emitted more tool-context bytes |

This is the expected mechanism curve: large savings when output floods,
smaller savings on medium output, and neutral-to-negative overhead when there
is nothing to contain. The run used two repeats per arm and committed aggregate
records, so it is directional regime evidence rather than a current benchmark.
See the [receipt](../evals/coding-suite-2026-07-20.md),
[record](../evals/coding-suite-record.json), and
[runner](../evals/coding_suite.py).

## Regime scoreboard (worst case and best case, all measured)

| Regime | straitjacket vs naive | vs the field |
|---|---|---|
| Catastrophic floods | 456 tok vs ~222k first exposure (487×) | Headroom silently dropped the needle (347,595→68) |
| Repo comprehension | only-correct-answers across rounds; first-ever haiku pass | untested by others |
| Long overhaul | −21% turns, −9% time, −16% output | beats Headroom on turns/time at par cost |
| Tiny surgical tasks | parity (was 4.5×; graduated engagement fixed it) | rtk-class tasks: parity is the ceiling |
| Mechanical bulk repair | parity after per-file-span iteration | our worst regime, no longer a loss |
| Small spec-driven creation (haiku) | **current loss**: 33 turns (cap) vs naive's 11–26 at 2.7–3.8× cost; quality tied (16/16 holdout all arms), cache hit still best (96–98%) | diagnosed to one loop — pytest digest lacks the failing-test census — fix candidates ranked, referee frozen ([`evals/spec3-haiku-2026-07-18.md`](../evals/spec3-haiku-2026-07-18.md)) |

Depth, per topic:
[`evals/matrix-2026-07-18.md`](../evals/matrix-2026-07-18.md) (scenario matrix +
cache economics) ·
[`evals/headroom-needle-2026-07-19.md`](../evals/headroom-needle-2026-07-19.md)
(needle-drop rerun vs headroom 0.32.1, model-free + reproducible) ·
[`evals/headroom-needle-drop-2026-07-17.md`](../evals/headroom-needle-drop-2026-07-17.md)
(original needle-drop head-to-head) ·
[`evals/ab-claude-code-2026-07-17.md`](../evals/ab-claude-code-2026-07-17.md)
(N=5 A/B: cost parity, 5/5 correct both arms, zero denials) ·
[`evals/antigravity-gemini-2026-07-19.md`](../evals/antigravity-gemini-2026-07-19.md)
(first non-Claude host: Antigravity SDK + `gemini-3.5-flash`, −30% total / 152×
less tool-output on an unavoidable flood, honest parity-loss on the greppable one) ·
[`evals/overhaul-3arm-2026-07-17.md`](../evals/overhaul-3arm-2026-07-17.md)
(v0.6 rematch: −40% cost vs naive at quality parity) ·
[`evals/rtk-corpus-2026-07-18.md`](../evals/rtk-corpus-2026-07-18.md)
(real-corpus reversals + live lint-fix rounds) ·
[`evals/eval-collapse-2026-07-18.md`](../evals/eval-collapse-2026-07-18.md)
(programmable capture) ·
[`evals/plan-collapse-2026-07-19.md`](../evals/plan-collapse-2026-07-19.md)
(compiled evidence plans: rounds 6→1, resend cost 9.0×↓, byte-stable digest) ·
[`LOSSLESS-RESCUE.md`](LOSSLESS-RESCUE.md) ·
[`PRICED-CONTEXT.md`](PRICED-CONTEXT.md) ·
[`LADDERS.md`](LADDERS.md) (the conditionality audit behind v0.20).

## What we took from each

- **rtk** → real corpora reversed our hypotheses before we built: diagnostics
  needed *structure, not compression* (`lint/v1` exact censuses; the live
  lint-fix benchmark went honest-loss → iterate → parity), and our own
  scaffold was inflating small outputs (slim inline: ~100–400 tok overhead →
  ~20). **Breadth taken second, deliberately**: rtk intercepts 100+ commands
  and we had three shapes, which was never an architectural gap — a
  substitution only ships where a bounded `ctx` op means the *same* thing, and
  nobody had walked the common commands looking for those pairs. Five more
  landed (`head`, `sed -n A,Bp`, `wc -l`, `find -name`, `ls -R`/`tree`), each
  with the equivalence pinned by test rather than asserted. The bar that keeps
  this from becoming rtk's lossiness: `head -n 20 f` and `ctx get repo:f
  --lines 1:20` are the same bytes, so it substitutes; `ls -R` and `ctx map`
  are *different questions* (a map is ranked and budgeted, a listing is
  exhaustive), so `ls -R` maps to a corpus listing instead. Most of
  `tests/test_substitute_common_commands.py` is negative cases — a recogniser
  that fires too eagerly answers a question nobody asked, under the operator's
  own command, which is precisely the complaint against the lossy filters.
- **Headroom** → its one structural edge (rescuing a bloated transcript) taken
  losslessly: epoch-latched elision, +$0.05 where per-request rewriting pays
  $0.90 in churn, 18 turns of lossless runway per 27k elided; live-validated
  with 10/10 facts correct including elided ones.
- **Ponytail** → solution ladder adopted only after the A/B won on every axis;
  rebuilt with enforcement (`ctx debt`) and per-session measurement.
- **Caveman** → terse narration kept, the loss dropped: citations resolve,
  compressed prose doesn't.
- **Maki** → the interpreter collapse generalized (`ctx seq` declared → `ctx
  eval` computed) with the provenance a raw sandbox drops. Still owed: its
  user-space extension model — see the malleability note above.
- **TokenSave** → the argument for keeping our stable prefix at *one* tool got
  sharper, while the surface gateway lets operations be disclosed on demand
  instead of paying for 80+ schemas on every request. Taken: one-call context,
  typed code facts and the instinct to meter savings where the user sees them.
  Declined: metering *bytes avoided*, which is trivially inflatable — a
  savings counter here has to be billed-token delta against a measured naive
  arm, the distinction our own bug-bash A/B ran into when the harnessed arm
  won on bytes-per-result and lost on total billed tokens by taking more
  turns.
- **WozCode** → compiled plans already share its collapse-N-reads instinct.
  Still to take: addressable fuzzy edit transactions, parser validation and
  rollback receipts; its install-friction reproach is closed by the PyPI release.
