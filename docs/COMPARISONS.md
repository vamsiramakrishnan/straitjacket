<sub><a href="README.md">« straitjacket / docs</a></sub>

# Comparisons

How straitjacket relates to the other tools in this space. Each neighbour
does one thing well; we benchmarked or stress-tested each, took the good
idea without its cost, and recorded what each still does better. All data
lives in [`evals/`](../evals/). The amber strip on each treemap tile is the
idea the harness kept — losslessly.

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
| **Headroom** (rewriting wire proxy) | rescue an already-bloated transcript | silent evidence drops (347,595→68 tok, no trace); cache hit 80.6–84.2% vs our 96.5–98.1%; 3–6× cache-write churn | v0.10 epoch-latched lossless rescue: ~18× less cache churn, every elided byte file-backed and addressed |
| **rtk** (bash-hook filter binary) | filter floods at the source, across 100+ commands | lossy on success paths; no addresses, no cache-stability policy | failure-asymmetric budgets, `ctx gain`, structure-not-compression `lint/v1` — and the *breadth* idea vendored: the replacement surface now covers 8 command shapes (grep-family, `cat`, `pytest`, `head`, `sed -n A,Bp`, `wc -l`, `find -name`, `ls -R`/`tree`), each substituted only where a bounded `ctx` op means the **same** thing |
| **Ponytail** (ruleset injection) | the solution ladder | advisory only; never measured whether the ladder held | ladder A/B-adopted on evidence (−28% turns, −33% time, −17% cost) + `ctx debt` |
| **Caveman** (terse prompting style) | say less | destroys evidence to save tokens — the quiet-needle anti-pattern | cite-don't-quote with resolvable handles (skill rules 11–12) |
| **Maki** (sandboxed interpreter) | one script collapses N ops (their demo: 1300×) | no provenance: script and output vanish into the chat log | `ctx py`: script is an addressable `blob:`, streams span-addressed, tracebacks path-free |
| **TokenSave** (code-intelligence MCP server) | pre-indexed symbol graph answers instead of file reads; savings metered on every call | 40+ tool schemas is 40+ tool definitions of prompt prefix, re-paid every request and churned on release | one stable `ctx` tool with an `op` discriminator — prefix never churns (96.5–98.1% cache hit); per-call savings already in the digest header |
| **wozcode** (Claude Code plugin) | replaces built-in file tools with purpose-built agents; installs in seconds, no signup | plugin-scoped to one host; no addressing story published | `ctx wrap` targets three hosts and merges non-destructively — but see the setup-friction note below, where they beat us outright |

*Rows for TokenSave and wozcode are desk research, not head-to-head runs —
their figures are their own claims. Marked as such in
[`evals/field-devex-2026-08-02.md`](../evals/field-devex-2026-08-02.md), which
is the receipt for this section and is explicitly not allowed to move any
performance number we publish.*

What each still does better than us, by design: Headroom's zero-integration
generality, rtk's 15-host reach and <10ms single binary, Ponytail's 20-host
rule files, Maki's OS-level sandbox (ours arrives with the broker, Phase 3)
and its user-space `init.lua` plugin model, TokenSave's ambient session cost
panel, wozcode's zero-signup install.

### Two places the field beats us on devex, stated plainly

**Distribution.** rtk is `cargo install`. Headroom is `headroom wrap claude`.
wozcode installs in seconds with no signup. We are `git clone` →
`pip install -e .` → `ctx wrap setup`. Step three is the best in the field —
idempotent, non-destructive, self-verifying, exits non-zero rather than
claiming a success it didn't achieve. Steps one and two are the worst. That is
a release we have not cut, not a design position.

**Malleability.** Maki's users shape the agent from `init.lua` in user space.
Ours must edit `src/ctx/digest/<family>prof.py` and append to the `_PROFILES`
tuple in our source tree — i.e. **carry a fork** to teach the harness their
own test runner or in-house log format. For a project whose thesis is that
output families are diverse and deserve typed treatment, a closed profile
registry caps the system at the families we personally got around to writing.
Opening it is backlog item 2 in the scan above.

## How each neighbour is built — and where the harness diverges

The neighbours split into two architectural families. **Headroom** sits on the
wire and rewrites transcript history on every request — compression happens
_after_ the bytes are already resident, and the original is gone. **rtk** and
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
- **TokenSave** → the argument for keeping our surface at *one* tool got
  sharper, not weaker: their 40+ schemas are the concrete cost of the
  alternative. Taken: the instinct to meter savings where the user sees them.
  Declined: metering *bytes avoided*, which is trivially inflatable — a
  savings counter here has to be billed-token delta against a measured naive
  arm, the distinction our own bug-bash A/B ran into when the harnessed arm
  won on bytes-per-result and lost on total billed tokens by taking more
  turns.
- **wozcode** → nothing technical yet; it is on this list as a standing
  reproach about install friction.
