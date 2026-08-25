# Addressable Evidence — the visual language

<sub><a href="README.md">« straitjacket / docs</a></sub>

straitjacket does not decorate documentation. It makes the argument visible.

The visual language is called **Addressable Evidence**. Every graphic should
show one of three things: where bytes reside, what a digest preserves, or how an
address reconnects the reader to omitted evidence. If a graphic cannot sharpen
one of those claims, prose is probably better.

## The governing idea

> Draw the evidence path, not the software topology.

Architecture boxes describe what exists. Evidence traces describe what happens.
Prefer the trace: command, capture, digest, address, retrieval. Show the moment
where an alternative floods, truncates, relocates, or refuses.

The strongest visual unit has four parts:

1. a claim that can stand alone;
2. a concrete specimen or trace;
3. a measured consequence;
4. a receipt naming the source or the boundary of the claim.

## The five primitives

| Primitive | Use it to show | Canonical example |
|---|---|---|
| Residency trace | bytes persisting across turns | `ae-residency` |
| Evidence fates | the same input under competing treatments | `ae-evidence-fates` |
| Digest anatomy | which fields carry identity, coverage, and continuation | `ae-digest-anatomy` |
| Anchor drift | an address verifying, moving, or refusing | `ae-anchor-drift` |
| Host lane | where each host can prevent, replace, or only observe a flood | `ae-host-lanes` |

Compose new explanations from these primitives before inventing a new diagram
grammar. Repetition makes the system legible.

## Colour has a job

Colour is semantic, never atmospheric.

| Colour | Meaning |
|---|---|
| Blue | bounded, addressed, or controlled |
| Amber | identity, receipt, or decision point |
| Red | resident flood, loss, or unsupported boundary |
| Green | verified outcome |
| Neutral | storage, structure, or unselected context |

Do not add gradients, glow, glass effects, or decorative colour. A reader should
be able to infer why a mark is coloured without reading the legend.

## Type is evidence hierarchy

Display text states the claim. Large tabular figures use the display face so
their comparison reads at a glance. Monospaced text carries byte-shaped
evidence: commands, handles, selectors, coordinates, and inline receipt values.

- Use short uppercase display claims, not paragraph-sized titles.
- Use sentence case for explanations.
- Keep specimens byte-shaped; do not typeset a command like marketing copy.
- Prefer a system sans stack and the repository's embedded JetBrains Mono face.
- Never depend on a page font cascading into an SVG loaded through `<img>`.

## Layout is an instrument panel

The canonical canvas is 1200 pixels wide on a 64-pixel grid. Use square corners,
one-pixel rules, generous outer margins, and aligned baselines. Panels organize
evidence; they are not cards competing for attention.

Make comparison axes explicit. Label the unit. Put the decisive delta next to
the thing it describes. Empty space should separate ideas, not soften them.

At narrow widths, preserve the claim, the decisive value, and the continuation
address first. Each canonical primitive therefore has a 640-pixel compact
variant that restructures evidence instead of shrinking the desktop canvas.
Remove secondary annotation before reducing evidence text below legibility.

## Receipts before spectacle

Measured values must come from a checked-in receipt. Illustrative traces must say
they are illustrative. A visual may simplify a path; it may not upgrade an
example into a benchmark or a benchmark into a guarantee.

The current visual set reads:

- [`evals/field-needle-record.json`](../evals/field-needle-record.json) for the
  field-needle corpus, competing emissions, digest, and retrieval handle;
- [`evals/anchor-drift-2026-08-20.json`](../evals/anchor-drift-2026-08-20.json)
  for verification, relocation, refusal, wrong-answer count, and address overhead;
- [`spec/adr/005-antigravity-hook-contract.md`](../spec/adr/005-antigravity-hook-contract.md)
  for the host-lane capabilities and the explicit Antigravity retry boundary.

Source the number in the generator rather than copying it into SVG markup.

## Accessibility is part of the contract

Every diagram ships in dark and light variants. Both must express the same
information, preserve sufficient contrast, and render without scripts, external
fonts, or network resources. Every embed needs alt text that states the finding,
not merely the chart type.

Do not encode the result through colour alone. Pair colour with position, labels,
line style, or shape. Keep the reading order meaningful when the image is absent.

## Build and verify

Generate the complete set from repository receipts:

```bash
python scripts/gen_addressable_evidence_visuals.py
```

Verify that source assets and site mirrors are current:

```bash
python scripts/gen_addressable_evidence_visuals.py --check
```

Generated files live in `assets/readme/diagrams/` and are mirrored byte-for-byte
Desktop files use the primitive name; compact files add `-mobile`. Every embed
selects the compact pair at 640 pixels or below.

The final test is editorial: hide the surrounding prose. The visual should still
make one true, precise claim.
