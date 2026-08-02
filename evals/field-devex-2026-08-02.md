# Field scan: devex and UX, 2026-08-02

**Method, stated first because it bounds every claim below.** This is *desk
research*, not a head-to-head. Nothing here was run, benchmarked, or
reproduced. Every number attributed to another project is that project's own
published claim, marked *(their claim)*, and should be read as a marketing
figure until we measure it ourselves. That is a weaker instrument than
[`evals/ab-claude-code-2026-07-17.md`](ab-claude-code-2026-07-17.md) or the
Headroom head-to-head, and the conclusions are scoped accordingly: this file
is allowed to change *our devex backlog*, and is not allowed to change any
performance claim we publish.

House rule unchanged: receipts before doctrine.

Scanned: [maki](https://maki.sh/) · [rtk](https://github.com/rtk-ai/rtk) ·
[wozcode](https://www.wozcode.com/) ·
[tokensave](https://github.com/aovestdipaperino/tokensave) ·
[Headroom](https://www.alphamatch.ai/blog/headroom-context-compression-ai-agents-2026)
· [Ponytail](https://www.alphamatch.ai/blog/ponytail-ai-coding-skill-2026).

Three of these were already in [`docs/COMPARISONS.md`](../docs/COMPARISONS.md)
with measured entries. This scan adds **wozcode** and **tokensave**, refreshes
the others, and — the actual point — reads all six for *devex* rather than for
containment technique.

---

## 1 · What each one is, in one line

| Project | Shape | Distribution | Their claim |
|---|---|---|---|
| **maki** | standalone Rust agent + TUI | single binary | indexes instead of reads; sandboxed Python interpreter chains tools, `asyncio.gather()` across them, intermediate data never enters context |
| **rtk** | CLI proxy under the shell | `cargo install`, single binary, zero deps | 60–90% token reduction across 100+ dev commands, <10 ms overhead |
| **wozcode** | Claude Code plugin | install in seconds, no signup | replaces built-in file tools with three purpose-built agents; 25–50% cheaper, 5–10× faster |
| **tokensave** | MCP server | Rust binary | 40+ tools, 30+ languages, 9 agent integrations, pre-indexed semantic knowledge graph |
| **Headroom** | wire proxy + library + MCP + `wrap` | four integration modes | 60–95% fewer tokens, reversible via CCR |
| **Ponytail** | injected ruleset | rule file per host | decision ladder, reinjected every turn |

## 2 · The devex lessons, ranked by how much they should change us

### 2.1 Distribution is the first UX surface, and ours is the worst in the field

Every project scanned installs in one step from a published artifact:

```
cargo install --git https://github.com/rtk-ai/rtk    # rtk
headroom wrap claude                                  # Headroom
# wozcode: install in seconds, no signup (their claim)
```

Ours:

```
git clone …
pip install -e .    # from a clone — PyPI release pending
ctx wrap setup
```

`ctx wrap setup` is genuinely good — idempotent, non-destructive, merges
rather than clobbers, names every file it writes, self-verifies, exits
non-zero rather than claiming a success it didn't achieve. **None of that is
reachable until you have cloned a repository and installed it editable.**
We have polished step two of a two-step flow and left step one at
`git clone`.

This is the single largest devex gap and it is not a technical problem. It is
a release we have not cut.

*Second-order:* rtk and tokensave ship a **single static binary with zero
runtime dependencies**. We are Python ≥3.11 with a tiered dependency policy
(stdlib-only hook hot path, one pure-Python runtime dep, optional
accelerators). The tiering is the right design and is why the hook survives
hostile environments — but "requires a Python 3.11+ toolchain" is a real
adoption filter that a Rust binary does not pay. The native Rust hook already
exists (`native hook parity (Rust ↔ Python)` is a CI job); the question worth
asking is whether a shipped binary for the *hot path only* closes most of
this gap without giving up the Python runtime.

### 2.2 Malleability: we are the only one you must fork to extend

Maki's model is the sharpest here: **`init.lua`, a Lua API that mirrors
Neovim's.** Users shape the agent from user space, in a scripting language
chosen because a large population already knows its idioms. Plugins, settings,
and behavior all live in one file the user owns.

Ours, from [`docs/WRITING-A-PROFILE.md`](../docs/WRITING-A-PROFILE.md):

> 1. **Write the profile.** Create `src/ctx/digest/<family>prof.py` …
> 2. **Register it.** Add your class to the `_PROFILES` tuple in
>    `src/ctx/digest/__init__.py`.

To teach this harness a new output family — your company's in-house test
runner, your CI's bespoke log format, the one build tool your team actually
uses — **you must edit our source tree and carry a fork.** There is no
entry-point group, no user-space profile directory, no config-declared
extractor. Every profile in the system is one we wrote.

That is a strange position for a project whose entire thesis is that *output
families are diverse and deserve typed treatment*. We built the argument for
extensibility and then made ourselves the only party who can act on it.

The gap is not "we should add Lua". It is that **the profile registry is
closed**, and a closed registry caps the system at the families we personally
got around to.

### 2.3 Surfacing the win: tokensave measures per call, we measure per digest

tokensave includes savings metrics **in every tool response**, plus a monitor
TUI with a live cost panel (today's spend, 7-day total, efficiency ratio)
*(their claim)*.

We are closer than the gap suggests — our digests already carry the line:

```
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens   ← what your agent DIDN'T pay for
```

— and `ctx gain` exists. What we lack is the **ambient, session-level**
view: no live panel, no running total, no "this session has cost you X and
saved you Y" that a user sees without asking. The per-call receipt is the
harder half and we have it; the easy half is missing.

Worth noting the honesty risk in copying this: a "tokens saved" counter is
trivially inflatable (count the bytes you never sent, ignore the turns you
added). If we ship one it should be **billed-token delta against a measured
naive arm**, not bytes-avoided — which is exactly the distinction the
bug-bash A/B made when the harnessed arm won on bytes-per-result and *lost*
on total billed tokens because it took more turns.

### 2.4 The one-tool-vs-forty question

tokensave exposes 40+ MCP tools. We expose exactly one, with an `op`
discriminator.

This is a real trade and we should keep making it our way, but we should say
*why* out loud where users can see it: **every tool definition is prompt
prefix, and prefix churn costs cache hits.** 40 tool schemas is a large fixed
cost paid on every request, and a server that adds tools over time invalidates
the prefix each release. One stable schema with an `op` parameter never
churns. Our measured cache-hit band (96.5–98.1%) is downstream of choices like
this one.

The cost of our choice is discoverability: `op` is less legible to a model
than forty named tools, and less legible to a human reading a tool list. That
is the trade, and it belongs in the docs rather than in our heads.

### 2.5 Setup-step friction we can remove without shipping anything

Reading our own flow against theirs, three frictions are ours and cheap:

1. **No PyPI release.** Above. Everything else is downstream of it.
2. **`ctx wrap setup` is not discoverable from a failure.** If someone
   installs and runs `ctx run -- pytest` in an unharnessed repo, we should say
   so and name the one command that fixes it.
3. **`ctx doctor` is opt-in.** It is a good self-check that most users will
   never type. First-run should verify itself.

---

## 3 · Ponytail: what we already have, and the one rung we don't

Checked against [the article](https://www.alphamatch.ai/blog/ponytail-ai-coding-skill-2026)
rung by rung, because "have we incorporated it?" deserves an audit and not a
vibe.

Ponytail's decision ladder vs. **skill rule 13**:

| Ponytail rung | Ours (skill r13) | Status |
|---|---|---|
| 1 · "Does this need to exist?" (YAGNI) | "not needed at all" | ✅ |
| 2 · "Does the stdlib do it?" | "standard library" | ✅ (we order it after reuse — see below) |
| 3 · "Is there a native platform feature?" | — | ❌ **missing** |
| 4 · "Is there an installed dependency?" | "reuse what exists" | ✅ |
| 5 · "Can it be one line?" | "a one-liner" | ✅ |
| 6 · Minimal working code | "minimal new code" | ✅ |

**Five of six rungs, adopted before this scan and A/B-validated** (−28% turns,
−33% wall-clock, −17% cost — [`evals/ab-claude-code-2026-07-17.md`](ab-claude-code-2026-07-17.md)).
The missing rung is *native platform feature*, and its absence is not
cosmetic: it is the rung that catches "wrote a helper for something the
language, runtime, or host already does natively". Added to rule 13 in this
change.

Rung order differs deliberately: ponytail checks stdlib before installed
dependencies; we check *reuse what exists* first. In a codebase with an
established internal vocabulary, reaching for `hashlib` when the repo already
has `stat_fingerprint` is the wrong move — and that is not hypothetical, it is
exactly the defect an automated reviewer found in our rewrite guard the day
before this scan. We keep our order.

### Where we go further

- **"Lazy, not negligent"** ↔ our **"Be lazy about the solution, never about
  reading."** Same instinct; ours names the axis on which laziness is
  forbidden, which is the one that matters for an agent that can skip reading
  and guess.
- **Declared deferral.** Ponytail's ladder can silently skip work. Ours
  requires `ctx debt add "<note>" --ref repo:file:line` — a deferred
  improvement is *recorded and addressed*, not dropped. This is the same
  declared-omission principle the digest layer uses for bytes, applied to
  intent.
- **Measured, not asserted.** Ponytail's own framing is that the ladder is
  advisory. We adopted it only after an A/B won on every axis, and we say in
  [`docs/LADDERS.md`](../docs/LADDERS.md) that it remains model-traversed and
  advisory — a ladder nobody measures is a ladder nobody knows is being
  climbed.

### Where they go further, and we should think about it

**Reinjection.** Ponytail's stated mechanism is that the rules *reinject every
turn*, on the theory that a rule read once at session start decays. Ours is
loaded once as a skill, with **conditional** nudges from the PostToolUse hook
(`_navigation_nudge`, `_emission_nudge`) that fire only when a specific wrong
pattern is observed.

The trade is real in both directions. Conditional nudging costs nothing when
the agent is behaving and cannot correct drift it doesn't detect;
unconditional reinjection catches drift but pays on every turn — though, since
it is stable text, it is prefix-cacheable and the marginal cost is smaller
than it first looks. We have never measured our own ladder adherence over a
long session, which means we cannot currently say whether rule 13 is still
being followed at turn 60. **That is a measurable question and we should
measure it** rather than assume either answer.

**Non-negotiable safety carve-outs.** Ponytail explicitly exempts trust
boundaries, data loss, security, and accessibility from its
reduce-the-code pressure — laziness must not touch those four. Our rule 13
has no such carve-out list; it relies on the surrounding rules and on
`ctx debt`. Adding an explicit exemption is cheap and closes a foreseeable
failure where "prefer the one-liner" meets an input-validation path.

---

## 4 · Backlog this produces

Ordered by (impact ÷ effort), highest first. None of these are done here;
this file's job is to name them.

| # | Item | Why | Size |
|---|---|---|---|
| 1 | **Cut a PyPI release** | the whole first step of adoption | S |
| 2 | **Open the profile registry** — entry-point group + user-space profile dir, config-declared | stop requiring a fork to extend the thing whose thesis is extensibility | M |
| 3 | Safety carve-out in skill rule 13 | foreseeable failure at the ladder/validation intersection | XS |
| 4 | Session-level savings view (billed-token delta, not bytes-avoided) | the easy half of a receipt we already earn | S |
| 5 | Measure ladder adherence at turn 60 vs turn 5 | answers the reinjection question with data instead of preference | M |
| 6 | First-run self-verify; name `ctx wrap setup` in the unharnessed-repo error | removes two setup frictions with no new surface | S |
| 7 | Consider shipping the native Rust hook as a binary | removes the Python-toolchain filter from the hot path only | L |

Items 3 and the ponytail rung are landed in this change. The rest are
backlog, filed as `ctx debt` entries rather than asserted as done.
