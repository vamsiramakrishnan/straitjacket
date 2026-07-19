<div align="center">

<img src="../assets/readme/docs-header.svg" width="100%" alt="straitjacket docs — design notes and mechanism specs. Every claim backed by a receipt in evals; bytes become addresses. Current wave: LADDERS, REFLEX, EDC, ALGEBRA."/>

</div>

Working design notes for straitjacket's mechanisms. House rules apply
here as everywhere: every claim traces to a receipt in
[`evals/`](../evals/), normative behavior lives in [`spec/`](../spec/),
and shipped history lives in [`CHANGELOG.md`](../CHANGELOG.md). A design
doc is where a mechanism earns its shape *before* it ships — hypotheses
are stated, tested, and kept or reversed on evidence.

<div align="center">

<img src="../assets/readme/docs-path.svg" width="100%" alt="Reading path: two shipped mechanism theses (PRICED-CONTEXT — price at decision time; LOSSLESS-RESCUE — elide bytes, keep addresses), then the current architecture wave in order: LADDERS, REFLEX, EDC, ALGEBRA."/>

</div>

## Shipped mechanism theses

The theses behind mechanisms that are already in the product, each
validated by a measured A/B or head-to-head before adoption.

| Doc | One line |
|---|---|
| [PRICED-CONTEXT.md](PRICED-CONTEXT.md) | Metadata as economic signposting: retrieval choices are rational only when every choice carries a visible price — in tokens, at decision time, relative to the remaining window, with a cheaper alternative attached. |
| [LOSSLESS-RESCUE.md](LOSSLESS-RESCUE.md) | Taking the rewriting proxy's one structural edge (rescuing an already-bloated transcript) without its costs: epoch-latched elision, every elided byte file-backed and addressed. |

## The current architecture wave

Written against the spec3 receipt
([`evals/spec3-haiku-2026-07-18.md`](../evals/spec3-haiku-2026-07-18.md))
— the one regime straitjacket currently loses. Read in order; each doc
builds on the previous one's diagnosis.

| # | Doc | One line |
|---|---|---|
| 1 | [LADDERS.md](LADDERS.md) | The conditionality audit: a registry of every tiered/conditional construct in the product, judged by the house criterion that a conditional is only as good as its measurement. |
| 2 | [REFLEX.md](REFLEX.md) | Closed-loop conditionality: why open-loop ladders fired on the wrong axis, and the ten reflex design rules for steering on observed session behavior instead. |
| 3 | [EDC.md](EDC.md) | The Evidence Delivery Controller — adopted target architecture for the digest layer: typed Facts per command family, Evidence Contracts, deterministic Delivery Plans; coverage becomes the objective, size the constraint. |
| 4 | [ALGEBRA.md](ALGEBRA.md) | Facts and the composition algebra: the EDC governs how evidence is delivered; this layer governs how it is derived and composed (tree-sitter skeleton tier, derived artifacts, Angle-inspired queries). |

---

<div align="center">
<sub><a href="../README.md">« back to straitjacket</a> · <a href="../spec/">spec/</a> · <a href="../evals/">evals/</a> · <a href="../ROADMAP.md">roadmap</a></sub>
</div>
