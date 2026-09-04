"""Deterministic text -> bitmap image rendering ("snapcompact") — Delivery
plane, alongside ``digest/evidence_render.py`` and ``resolver.py``: this is a
rendering/selection concern, not a new evidence source.

Provenance: stencil.so/blog/snapcompact (2026) reports that rendering dense
text passages as small monospace bitmap images and passing them to a vision
model *instead of* raw text tokens costs roughly a third as much per input
token on four frontier models, while the model still transcribes the text
back near-verbatim on extractive QA (SQuAD v1.1) — PROVIDED the rendered
character cell clears a measured density floor. Their own sweep: an 8x13
cell (104 px² per character) scored 1.00 F1; a 6x10 cell (60 px²) scored
0.79; a 5x8 cell (40 px²) fell off a cliff to 0.37. They also report that
aligning the render to the vision model's own patch grid improves decode
confidence — which lines up with Anthropic's own documented vision-token
formula below: Claude tiles images into 28x28px patches.

This module implements ONLY the deterministic half of that technique: text
-> PNG bytes, plus a best-effort, explicitly-labeled token-cost estimate.
It CANNOT verify the technique's actual payoff — that a live vision model
reads the rendered image back correctly at the claimed cost savings —
because doing that needs a real model call, and this sandbox has no way to
make one. Every number `estimate_savings` returns is real and computable
locally (this repo's own token estimator; Anthropic's own documented image
tiling formula); whether decode fidelity holds is NOT tested here. Treat the
blog's 2-3x figure as the unverified TARGET this mechanism aims at, not a
confirmed result of running this code. See docs/ARCHITECTURE.md and the
`--snapcompact` wiring in `ctx get` (src/ctx/_retrieval/get.py) for how a
caller opts in.
"""

from __future__ import annotations

import functools
import io
import math
from dataclasses import dataclass
from pathlib import Path

from ctx.textutil import EVIDENCE_LINE_CHARS, estimate_tokens

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]


class SnapcompactUnavailable(RuntimeError):
    """Raised when the optional ``image`` extra (Pillow) is not installed.

    Matches this repo's house style for an opt-in feature whose engine is
    missing — a clear, actionable message instead of a bare ImportError
    traceback (see ``ctx.astgrep.EngineMissing``, ``ctx.semgrep_engine
    .EngineMissing``, and the ``pip install 'ctx-harness[image]'`` phrasing
    already used by ``ctx.binfmt``'s dHash degrade and ``ctx image diff``).
    It only ever fires when a caller actually asked for this feature —
    nothing on any other code path imports this module.
    """


# The blog's own measured density thresholds (stencil.so/blog/snapcompact,
# their SQuAD v1.1 transcription-fidelity sweep): below ~35-40 px² per
# character, decode quality falls off a cliff; their best-scoring config (an
# 8x13 bitmap font, 104 px²/char) hit near-ceiling F1. This module never
# hardcodes a font's pixel geometry to hit these numbers — it MEASURES the
# real cell size of whatever font actually loaded (`_char_cell`) and reports
# it, so the density claim stays checkable per-call instead of assumed.
DENSITY_FLOOR_PX2 = 35.0
DENSITY_TARGET_PX2 = 104.0

DEFAULT_FONT_SIZE = 13  # px; matches the blog's winning config's cell HEIGHT
#: This repo's own line-clip width elsewhere (ctx.digest.text's evidence
#: lines) — reused here rather than inventing a second wrap-width constant.
DEFAULT_CHARS_PER_LINE = EVIDENCE_LINE_CHARS

# Genuine fixed-width TTFs, most-preferred first. Every path below was
# CONFIRMED present on a stock dev image before being listed (not assumed):
# this sandbox's own /usr/share/fonts carries DejaVu Sans Mono, Liberation
# Mono, and FreeMono at exactly these paths. `fonts-dejavu-core` and
# `fonts-liberation2` are common baseline Debian/Ubuntu packages;
# `fonts-freefont-ttf` is the next-most-common fallback. The first candidate
# that actually exists on THIS machine is used; if none does, Pillow's own
# bundled font is used instead (see `_load_font`) — degrade, never fabricate
# a font this module did not actually check for.
_MONOSPACE_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",              # Debian/Ubuntu: fonts-dejavu-core
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",  # Debian/Ubuntu: fonts-liberation2
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",                  # Debian/Ubuntu: fonts-freefont-ttf
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",       # Fedora/RHEL
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",      # Fedora/RHEL
    "/opt/homebrew/Caskroom/font-dejavu-sans-mono/DejaVuSansMono.ttf",  # macOS + Homebrew
    "/Library/Fonts/Menlo.ttc",                                         # macOS (Xcode CLT)
)


def _require_pillow() -> None:
    if Image is None:
        raise SnapcompactUnavailable(
            "snapcompact requires the optional `image` extra: "
            "pip install 'ctx-harness[image]'"
        )


@functools.lru_cache(maxsize=1)
def _font_path() -> str | None:
    for candidate in _MONOSPACE_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


@functools.lru_cache(maxsize=None)
def _load_font(font_size: int):
    _require_pillow()
    path = _font_path()
    if path is not None:
        return ImageFont.truetype(path, font_size)
    # No system TTF found (e.g. a minimal container with only Pillow itself
    # installed). Pillow >=10.1 can scale its own bundled outline font
    # (Aileron) via `load_default(size=...)` — but Aileron is proportional,
    # not monospace, so the fixed-cell-width grid this module draws on is
    # only APPROXIMATE in this fallback path (measured from "M", same as
    # always — just no longer exactly right for every other glyph). Still
    # strictly better than refusing to render.
    try:
        return ImageFont.load_default(size=font_size)
    except TypeError:
        # Pillow 10.0.x's load_default() takes no `size` argument, so there
        # is no way to honor font_size on that version; its one fixed bitmap
        # size is used instead of raising over a version skew this feature
        # does not otherwise care about.
        return ImageFont.load_default()


def _char_cell(font) -> tuple[int, int]:
    """Measured ``(width, height)`` in px of one monospace character cell.

    Never assumed: a monospace TTF gives every printable ASCII glyph the
    same advance width, so measuring one glyph ("M") gives this specific
    font/size's real per-character footprint — what `estimate_savings`
    needs to report an actual, checkable px²/char instead of restating the
    blog's own number.
    """
    ascent, descent = font.getmetrics()
    height = max(1, ascent + descent)
    width = font.getlength("M")
    return max(1, round(width)), height


def _wrap(text: str, cols: int) -> list[str]:
    """Deterministic hard-wrap at `cols` columns; existing newlines are hard
    breaks. This is meant for a bounded digest slice already on its way out
    as content, not a markdown-aware reflow — it never collapses whitespace
    or reorders anything, byte for byte."""
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        for i in range(0, len(raw), cols):
            lines.append(raw[i : i + cols])
    return lines or [""]


@dataclass(frozen=True, slots=True)
class SnapcompactImage:
    """Result of rendering one text slice to a PNG, plus the geometry that
    produced it — what a caller needs to report density/cost honestly."""

    png_bytes: bytes
    width: int
    height: int
    lines: int
    chars_per_line: int
    font_size: int
    cell_width_px: int
    cell_height_px: int

    @property
    def cell_area_px2(self) -> float:
        return float(self.cell_width_px * self.cell_height_px)


def _render(text: str, font_size: int, cols: int):
    _require_pillow()
    if cols < 1:
        raise ValueError(f"chars_per_line must be >= 1, got {cols}")
    if font_size < 1:
        raise ValueError(f"font_size must be >= 1, got {font_size}")
    font = _load_font(font_size)
    lines = _wrap(text, cols)
    cell_w, cell_h = _char_cell(font)

    # cols is already validated >= 1 above, and _wrap never returns an empty
    # list (`return lines or [""]`) -- no floor needed on either factor.
    width = cols * cell_w
    height = len(lines) * cell_h
    # Mode "1" (1 bit/pixel, white=1/black=0) with fontmode "1" (no
    # anti-aliasing): crisp bilevel glyphs, smallest PNG, and no subpixel
    # rounding for identical input to vary across — the determinism this
    # function promises. Same text + same font_size + same chars_per_line
    # renders identical pixels every time on this machine; PNG bytes carry
    # no timestamp or metadata chunk (Pillow's default PNG writer adds
    # none), so repeat calls are byte-identical here too. Byte-for-byte
    # identity ACROSS machines additionally depends on the installed
    # zlib/libpng build matching (the compressed stream, not the pixels) —
    # true within one interpreter/host, not guaranteed universally, so a
    # cross-machine determinism check should compare decoded pixels, not
    # raw PNG bytes.
    image = Image.new("1", (width, height), color=1)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"
    for row, line in enumerate(lines):
        if line:
            draw.text((0, row * cell_h), line, font=font, fill=0)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), width, height, len(lines), cell_w, cell_h


def render_text_to_png(
    text: str,
    *,
    font_size: int = DEFAULT_FONT_SIZE,
    chars_per_line: int | None = None,
) -> bytes:
    """Render `text` deterministically to PNG bytes: a monospace bitmap
    rendering of the exact text, crisp black-on-white glyphs (no
    anti-aliasing) so identical input renders identical pixels every time.

    `font_size` is the TrueType em size in px (default 13px, matching the
    blog's winning config's cell HEIGHT); `chars_per_line` hard-wraps before
    rendering (default `ctx.textutil.EVIDENCE_LINE_CHARS`). Both are plain
    tunables, not magic constants baked into the geometry — see
    `estimate_savings` to check what density a given choice actually lands
    at before trusting it.

    Raises `SnapcompactUnavailable` if Pillow is not installed.
    """
    cols = DEFAULT_CHARS_PER_LINE if chars_per_line is None else chars_per_line
    png_bytes, *_ = _render(text, font_size, cols)
    return png_bytes


def render_text_to_image(
    text: str,
    *,
    font_size: int = DEFAULT_FONT_SIZE,
    chars_per_line: int | None = None,
) -> SnapcompactImage:
    """Like `render_text_to_png`, but returns the full result plus the
    geometry that produced it (dimensions, line count, measured cell size)."""
    cols = DEFAULT_CHARS_PER_LINE if chars_per_line is None else chars_per_line
    png_bytes, width, height, n_lines, cell_w, cell_h = _render(text, font_size, cols)
    return SnapcompactImage(
        png_bytes=png_bytes,
        width=width,
        height=height,
        lines=n_lines,
        chars_per_line=cols,
        font_size=font_size,
        cell_width_px=cell_w,
        cell_height_px=cell_h,
    )


# ------------------------------------------------------- cost-tradeoff estimate

# Anthropic's own documented vision-token formula (Claude tiles images into
# 28x28px patches — one patch is one visual token):
#     tokens = ceil(width_px / 28) * ceil(height_px / 28)
# for an image at or under the model's resolution tier limits. Source:
# platform.claude.com/docs/en/build-with-claude/vision, "Resolution and
# token cost" (fetched 2026-09-03) — confirmed against three of the doc's
# own worked examples with no downscaling involved: 200x200px -> 64 tokens,
# 1000x1000px -> 1296, 1092x1092px -> 1521 (see tests/test_snapcompact.py).
# This is REAL and CURRENT, not a guess — but it only tells you what tokens
# the API bills for the rendered image's pixel dimensions. It says nothing
# about whether a model actually transcribes the text back correctly at
# that size; see the module docstring and `estimate_savings`'s
# `unverified` field.
_PATCH_PX = 28
_STANDARD_TIER = {"max_edge_px": 1568, "max_tokens": 1568}
_HIGH_RES_TIER = {"max_edge_px": 2576, "max_tokens": 4784}


def _image_tokens(width: int, height: int, *, high_resolution: bool) -> int:
    """Visual-token estimate for one image, downscaled to fit its tier first.

    Below the tier's long-edge AND token limits, this is exact (the three
    worked examples above hit the documented number precisely). Above
    either limit, the docs say only that Claude downscales to the largest
    size fitting *both* limits while preserving aspect ratio — the precise
    rounding/padding rule lives in a separate doc
    (vision-coordinates#how-claude-resizes-and-pads-images) this module does
    NOT replicate. The two-pass scale below (edge first, then area to fit
    the token cap) approximates that rule; treat an over-the-cap estimate as
    close, not certified bit-exact.
    """
    tier = _HIGH_RES_TIER if high_resolution else _STANDARD_TIER
    w, h = float(width), float(height)
    long_edge = max(w, h)
    if long_edge > tier["max_edge_px"]:
        scale = tier["max_edge_px"] / long_edge
        w, h = max(1.0, w * scale), max(1.0, h * scale)
    tokens = math.ceil(w / _PATCH_PX) * math.ceil(h / _PATCH_PX)
    if tokens > tier["max_tokens"]:
        area_scale = math.sqrt(tier["max_tokens"] / tokens)
        w, h = max(1.0, w * area_scale), max(1.0, h * area_scale)
        tokens = math.ceil(w / _PATCH_PX) * math.ceil(h / _PATCH_PX)
    return min(tokens, tier["max_tokens"])


def estimate_savings(
    text: str,
    *,
    font_size: int = DEFAULT_FONT_SIZE,
    chars_per_line: int | None = None,
    high_resolution: bool = False,
) -> dict:
    """Best-effort, explicitly labeled token-cost comparison for rendering
    `text` as a snapcompact image instead of sending it as raw text.

    Raw side: `ctx.textutil.estimate_tokens` — the SAME ~4-bytes/token
    estimator every other cost/budget figure in this codebase already
    trusts (digest headers, resolver ladders, `ctx stats`), so this number
    is directly comparable to anything else this tool prints.

    Image side: Anthropic's own documented 28x28px-patch tiling formula
    (see the module-level comment above) applied to the rendered PNG's
    pixel dimensions, downscaled first to approximately fit the chosen
    resolution tier. Exact when the image is under the tier's limits
    (confirmed against the docs' own worked examples); an approximation of
    Anthropic's undocumented-in-detail resize/pad rule when it is not. This
    is a real, current, citable computation, not a guess — but it is not
    certified bit-exact above the tier limits.

    What this function does NOT and CANNOT verify: whether a live vision
    model actually reads the rendered image back correctly at this size —
    that is the blog's own measured claim (SQuAD v1.1 transcription
    fidelity), and confirming it needs a real model call this sandbox has
    no way to make. The `unverified` field says so explicitly; treat
    `ratio` as "what the numbers say if the blog's fidelity claim holds
    for this density", not a confirmed result of running this code.
    """
    raw_bytes = len(text.encode("utf-8"))
    raw_tokens = estimate_tokens(raw_bytes)

    image = render_text_to_image(text, font_size=font_size, chars_per_line=chars_per_line)
    image_tokens = _image_tokens(image.width, image.height, high_resolution=high_resolution)

    cell_area = image.cell_area_px2
    if cell_area >= DENSITY_TARGET_PX2:
        density_note = "at/above the blog's own best-scoring density (near-ceiling F1 in their sweep)"
    elif cell_area >= DENSITY_FLOOR_PX2:
        density_note = "above the blog's measured reliable-decode floor, but below their best-scoring config"
    else:
        density_note = (
            "BELOW the blog's measured reliable-decode floor (~35-40 px2/char) — "
            "their own data shows transcription quality falling off a cliff under this density"
        )

    return {
        "raw_bytes": raw_bytes,
        "raw_tokens": raw_tokens,
        "image_width_px": image.width,
        "image_height_px": image.height,
        "image_tokens": image_tokens,
        "cell_width_px": image.cell_width_px,
        "cell_height_px": image.cell_height_px,
        "cell_area_px2": cell_area,
        "density_note": density_note,
        "ratio": (raw_tokens / image_tokens) if image_tokens else None,
        "unverified": (
            "raw_tokens and image_tokens above are both real, locally-computed numbers "
            "(this repo's own token estimator; Anthropic's documented 28x28-patch vision "
            "formula). Whether a live model actually TRANSCRIBES this rendered image back "
            "correctly is NOT verified by this function, or by any test in this repo — that "
            "requires a real vision-model call this sandbox cannot make. The blog's own "
            "reported result (stencil.so/blog/snapcompact) is ~2-3x cost reduction at "
            "near-ceiling F1 on SQuAD v1.1 extractive QA; treat that as the unverified "
            "TARGET this mechanism aims at, not a confirmed outcome of running this code."
        ),
    }
