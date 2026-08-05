# Binary-format containment — typed evidence for images and PDFs

**Date:** 2026-07-20 · **Modules:** `src/ctx/binfmt.py`,
`src/ctx/digest/binprof.py` · **Command:** `ctx image {digest,diff}`.

This extends the harness contract — capture bytes, emit a bounded typed view,
retain an address — to binary output. It does not claim that a structural
digest replaces multimodal inspection.

## The problem it fixes

Before, `cat screenshot.png` fell to `text/v1` and was reported as "38 lines of
text" — meaningless — and unwrapped, a 100 KB screenshot enters a transcript as
**~34,000 tokens of opaque base64** that no compressor can help and every lossy
one corrupts.

## The typed digest (structure, never pixels)

```
[ctx run:ba5dc53d116e profile=binary/v1]
command: cat screenshot.png
format: png
bytes: 128,004 (125.0 KiB) · ~34,133 tok if inlined raw
image: 320×240 rgb
phash: 0303030303030303  (dhash/8x8 — `ctx image diff` to compare)
sha256: 3d167116768542d2… (exact identity)
bytes kept in store — retrieve: ctx get run:ba5dc53d116e#stdout
```

~34k tokens of base64 → a ~10-line structural digest, with the raw bytes kept
in the store behind a working `ctx get` address. Format id + dimensions +
colour + exact hash are **stdlib-only** for common image formats. PDF page
objects and visible text operators are labelled byte-scan heuristics: compressed
object streams mean absence cannot be interpreted as “scanned.” The dHash is
gated on the `image` extra (Pillow); absent, the digest says it is unavailable.

## The design-iteration primitive: pixel-free dHash comparison

The thing a "refine the look-and-feel" loop actually needs — *did the render
change, and how much* — without paying to send either image:

```
$ ctx image diff before.png after.png
a: screenshots/before.png · 200×150 png · 1313131313131313
b: screenshots/after.png · 200×150 png · 6464646464646464
dhash distance: 48/64 — substantial dHash change
byte identity: different
```

dHash Hamming distance is deterministic and cheap. It detects many render
changes without inlining either image, but its thresholds are coarse and it is
not an aesthetic, semantic, or accessibility evaluator.

## Honest scope

- **Images:** common dimensions are parsed by the stdlib floor; Pillow enriches
  other decodable formats and enables dHash comparison.
- **PDF:** the stdlib scan reports visible page objects and text operators as
  heuristics. It does not perform semantic page counting or text extraction.
- **Multimodal viewing is still out of scope:** to *judge* a look the model must
  see a raster render (multimodal tokens); the digest bounds the artifact and
  lets the model diff renders deterministically, but it does not replace the
  model actually looking when it needs to. That remains the honest limit of the
  whole category.

## Coverage

`tests/test_binfmt.py` covers format sniffing, dependency-free PNG dimensions,
labelled PDF heuristics, the `binary/v1` run profile end-to-end, the `ctx image
diff` CLI, dHash discrimination, and a regression that ordinary text output
still gets `text/v1`. Pillow-only tests skip independently; the stdlib floor
always runs.
