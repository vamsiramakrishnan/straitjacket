# Binary-format containment — proper handling for images & PDFs

**Date:** 2026-07-20 · **Modules:** `src/ctx/binfmt.py`,
`src/ctx/digest/binprof.py` · **Command:** `ctx image {digest,diff}`.

Follow-up to the design-limits finding (`field-needle-2026-07-20.md`): the
whole compression field breaks on non-text formats. This adds the *proper*
approach — the harness's "capture bytes, emit a typed view, keep an address"
promise, extended to binary.

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
colour + exact hash are **stdlib-only** (magic-byte + header parse for
PNG/JPEG/GIF/BMP/WebP; page count + text-extractability for PDF). The perceptual
hash is gated on the `image` extra (Pillow); absent, the digest still carries
everything but the phash and says so.

## The design-iteration primitive: pixel-free perceptual diff

The thing a "refine the look-and-feel" loop actually needs — *did the render
change, and how much* — without paying to send either image:

```
$ ctx image diff before.png after.png
a: 200×150 png  phash 1313131313131313
b: 200×150 png  phash 6464646464646464
perceptual distance: 48/64 (~75%) — substantial visual change
```

dhash Hamming distance is deterministic and cheap: identical renders → 0, a
sub-perceptual tweak → ≤5, a real redesign → 48/64. This is visual regression at
digest-rate — a capability the field's text-compression tools cannot express at
all, and the honest answer to "what about design work."

## Honest scope

- **Images:** full support (dimensions stdlib; perceptual diff with Pillow).
- **PDF:** page count + text-extractability by stdlib structural scan; deep
  per-page text extraction is a natural extension behind an optional
  `pypdf`/`pdfminer` dep (not wired here — pypdf's crypto backend was
  unavailable in this environment; the stdlib floor is what shipped).
- **Multimodal viewing is still out of scope:** to *judge* a look the model must
  see a raster render (multimodal tokens); the digest bounds the artifact and
  lets the model diff renders deterministically, but it does not replace the
  model actually looking when it needs to. That remains the honest limit of the
  whole category.

## Coverage

`tests/test_binfmt.py` (11): format sniffing, stdlib dimensions (PNG/JPEG/GIF),
PDF page count, the `binary/v1` run profile end-to-end (PNG → not text/v1, with
a working address), the `ctx image diff` CLI, perceptual-diff discrimination,
and a regression that ordinary text output still gets `text/v1`. Full suite
**934 passed**; the always-on core is stdlib-only.
