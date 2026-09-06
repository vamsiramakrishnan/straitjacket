# Content anchors — the address that survives an edit

<sub><a href="README.md">« straitjacket / docs</a></sub>

> **Status: Shipped** (v0.34.0) — implemented in [`src/ctx/anchors.py`](../src/ctx/anchors.py),
> covered by [`tests/test_anchors.py`](../tests/test_anchors.py), measured in
> [`evals/anchor-drift-2026-08-20.md`](../evals/anchor-drift-2026-08-20.md).
> New to the vocabulary? Read [How it works](HOW-IT-WORKS.md) and
> [Concepts](CONCEPTS.md) first.

## The promise that had a hole in it

straitjacket's second repository invariant is *omission keeps an address*. For frozen
artifacts — `run:`, `blob:`, `snapshot:` — a handle resolves the exact stored
bytes while that artifact store is available.

`repo:` needs a different promise. It names a live file that someone may be
editing, so its content can move or disappear between the turn that records an
address and the turn that follows it.

Before this mechanism:

```console
$ ctx get repo:m.py --lines 4:5
[ctx get repo:m.py (snapshot:cf3db50116cd)]
selector: --lines 4:5 of 8
L4: def beta():
L5:     return 2

# ... two imports get added at the top of the file ...

$ ctx get repo:m.py --lines 4:5
[ctx get repo:m.py (snapshot:a4536d5d3f22)]
selector: --lines 4:5 of 10
L4:     return 1
L5:
```

Same address. Different code. **Exit 0, and nothing in the output says so.** The
snapshot id changed, but nobody reads a snapshot id for a diff they were not
told to expect. A line number is a *position*, not an *identity*, and every
navigation verb that hands back `repo:<path> L<a>:<b>` — `ctx def`, `ctx refs`,
`ctx diag`, `ctx map`, `ctx get` — was minting addresses with that built in.

The exposure is not theoretical. Replaying ordinary edit shapes over this
repository's own source, **99.9% of re-resolved unanchored addresses returned
different content, silently** ([receipt](../evals/anchor-drift-2026-08-20.md)).
An address is only worth something between the turn that mints it and the turn
that uses it, and that interval is exactly when an agent edits files.

## Where this problem comes from

This is the read-side twin of a failure the field has already spent a lot of
effort on from the other end. String-replacement edit tools fail when the model
cannot reproduce a region byte-for-byte; patch formats fail when they apply
against lines that have moved; at least one vendor shipped a separate 70B model
whose whole job is reconciling edits that did not apply cleanly.

Those look like edit-tool problems. They are all the same missing primitive:

> **a stable, verifiable identifier for a region of a file, that does not cost
> the whole file in context.**

Without it, a model has exactly two ways to name code — reproduce it (expensive,
and it must be perfect) or point at a line number (cheap, and it goes stale) —
and both fail in the same situation: the file changed since it was read.

straitjacket's entire architecture is built on the claim that the third option
is better than either: *an address*. So the gap here was not a missing feature
at the edge of the system. It was the central mechanism not holding on the one
surface where it is hardest to hold.

## The grammar

An anchor is a short content digest that rides **inside the selector the address
already carries**, so no emission site grows a second field and no parser learns
a second address shape:

```text
ctx get repo:m.py --lines 4:5@07407f1c
                          └──┬──┘└──┬───┘
                          where it  what was there
                          was       (content, never position)
```

Everything that accepted `--lines A:B` still accepts it. The anchor is optional
everywhere, on the CLI and on the MCP surface alike.

## The three outcomes

Re-resolving an anchored address does one of three things, and which one
happened is always visible.

<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-anchor-drift-mobile.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-anchor-drift-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-anchor-drift.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-anchor-drift-light.svg" width="100%" alt="A content-anchored repository address recorded at lines 40 to 52 follows the same content to lines 46 to 58 after six lines are inserted above it. Across 1,920 measured cases, two addresses verified in place, 1,452 relocated, 466 refused, and none returned wrong content.">
</picture>

### Verified — the content is still there

The read is **byte-identical to the unanchored one**. No extra note, no reflow.
Declaring omissions is house style; narrating successes is window tax, and a
confirmation line would also break digest determinism for callers that anchor.

### Relocated — the content moved

```console
$ ctx get repo:m.py --lines 4:5@07407f1c
[ctx get repo:m.py (snapshot:a4536d5d3f22)]
anchor: @07407f1c moved L4:5 → L6:7 (content unchanged)
selector: --lines 6:7@07407f1c of 10
L6: def beta():
L7:     return 2
```

The caller asked for *content*, and the content is what comes back — along with
the move, and a **corrected address** to carry into the next turn. This is the
half that makes an anchor a working identifier rather than a tripwire: in the
receipt, following moved content accounts for **1,452 of 1,454 correct answers**.
An address that merely detected staleness would have been right once.

### Lost — the content is gone

```console
$ ctx get repo:m.py --lines 4:5@07407f1c
ctx get: anchor @07407f1c not found in repo:m.py: the content that was at
lines 4:5 is no longer in this file (it was edited or deleted). Re-navigate —
`ctx def`/`ctx refs`/`ctx search` mint a fresh anchored address — or read the
current lines with `ctx get repo:m.py --lines 4:5` if the coordinates are what
you meant.
$ echo $?
2
```

It **refuses**. The alternative is to return whatever now occupies those
coordinates — which is what an unanchored address already does, and is the
entire defect. A read that cannot keep its promise fails loudly; it does not
quietly answer a different question. The refusal still keeps omission
reversible: it names both ways forward.

## Line tags

The same idea at line granularity, for naming individual lines rather than a
span — two characters each, rendered only on request:

```console
$ ctx get repo:m.py --lines 6:7 --hashlines
L6:78| def beta():
L7:56|     return 2
```

Off by default, because every existing digest, receipt and test depends on the
untagged `L6: text` shape being byte-identical. Both renderings are produced by
one function so they cannot drift apart.

## What it costs, and where it is spent

An anchor is nine characters — about **20% on top of a bare `repo:` line
address**, and a rounding error against the span it addresses.

It is still not minted everywhere, and the choice is deliberate:

| Surface | Anchored? | Why |
|---|---|---|
| `ctx get repo:… --lines` | yes | the address most likely to be replayed after an edit |
| `ctx get` continuations (`next:`) | yes | a chain that sheds its anchor at the first budget cut silently stops being verifiable |
| `ctx def` | yes | the verb that runs *immediately before* an edit — the navigate-to-edit handoff |
| `ctx refs`, `ctx diag` | no | one anchor per row, on addresses that mostly get navigated rather than edited |
| `run:`, `blob:`, `snapshot:` | no | stored bytes cannot move while retained; the ref kind already carries content identity |

`ctx def` now emits **two** addresses, labelled, because they answer different
questions and confusing them was its own quiet bug:

```text
span: b34a0fcd32 (region L4:5) · as captured: ctx get repo:m.py --span b34a0fcd32
live: ctx get repo:m.py --lines 4:5@07407f1c
```

The span resolves against the snapshot that call froze, so while retained it
answers *"what did I read"*. The anchored range resolves against the worktree, so it answers
*"what is there now"* and follows the definition if an edit moves it. The span
alone used to be offered for both, which returned the pre-edit body to a reader
asking about current code.

## Design constraints

`ctx.anchors` is **pure and total** — no I/O, no store access, no shell-out, and
no imports from the retrieval package, so an emission site can mint an address
without pulling retrieval onto the hook's hot import path.

- **Position-free.** Anchors hash content and nothing else. An anchor minted for
  lines 4:5 equals one minted for the same two lines at 6:7 — that equality is
  what makes relocation possible at all. Mixing a line number into the digest
  would degrade the mechanism to a tripwire.
- **Bounded.** Relocation searches outward from the address's stated position and
  stops at a fixed candidate cap, so a lost anchor costs a predictable scan
  rather than one proportional to file size.
- **Versioned.** Both digests are domain-separated with a scheme version, so a
  future change to the derivation is a mismatch — caught, and routed through the
  same refusal path — rather than a silent reinterpretation.

## Known limits

- **Duplicate content relocates to the nearest copy.** An anchor names content,
  so a span that is byte-identical to another region can resolve to that region.
  The bytes returned are the bytes addressed, which is the promise; the
  *identity* is not recovered. Longer spans make this vanishingly rare, and the
  receipt shows it happening only where a file genuinely repeats itself.
- **Anchoring opts out of the seek fast path.** Verifying needs only the window,
  but relocating needs the whole file to search, and the fast path cannot know
  which outcome it is in before computing it. An anchored read therefore costs a
  full file read. Uniform verification was worth more than a seek on the one
  selector that asked to be checked.
- **Rewrite-in-place is unrecoverable by design.** If the addressed lines were
  themselves edited, no mechanism can find them; anchors convert that from a
  wrong answer into a refusal, which is the whole available win.
- **`--symbol` and an anchor are mutually exclusive.** `--symbol` looks the
  range up for you, discarding the one the caller supplied — so an anchor
  alongside it describes a range that is about to be thrown away. That is
  refused rather than ignored: silently dropping it would leave an address that
  looks verified and is not, which is this mechanism's own failure mode arriving
  through a selector combination instead of an edit.
- **Listing verbs stay bare.** `ctx refs` and `ctx diag` rows are unanchored. If
  measurement shows those addresses being replayed post-edit as often as
  `ctx def`'s, the two-character line tag is the cheaper rung to reach for
  before the nine-character anchor.
