"""The snapcompact technique: deterministic text -> bitmap PNG rendering,
its cost estimate, and the `ctx get --snapcompact` / MCP wiring.

What this file does NOT and cannot claim to test: that a live vision model
actually reads a rendered image back correctly. That requires a real model
call this sandbox has no way to make (see ctx.snapcompact's module
docstring). Every test below checks the deterministic encoding half only —
the PNG bytes, their dimensions, the token-cost arithmetic, and that the
retrieval surface actually wires an opt-in flag through to a stored blob.
"""

import io
import re

import pytest

from conftest import make_store, make_ws

try:
    import PIL  # noqa: F401
except ImportError:
    _HAS_PIL = False
else:
    _HAS_PIL = True

pil_only = pytest.mark.skipif(not _HAS_PIL, reason="image extra (Pillow) not installed")


# --------------------------------------------------------- pure rendering


@pil_only
def test_render_returns_valid_png_with_reported_dimensions():
    from PIL import Image

    from ctx import snapcompact as sc

    text = "hello snapcompact\nsecond line here"
    img = sc.render_text_to_image(text, font_size=13, chars_per_line=20)

    assert img.png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    assert img.width == img.chars_per_line * img.cell_width_px
    assert img.height == img.lines * img.cell_height_px
    assert img.cell_area_px2 == img.cell_width_px * img.cell_height_px

    with Image.open(io.BytesIO(img.png_bytes)) as im:
        assert im.format == "PNG"
        assert im.size == (img.width, img.height)


@pil_only
def test_render_is_byte_for_byte_deterministic():
    from ctx import snapcompact as sc

    text = "The quick brown fox jumps over the lazy dog.\nLine two.\n"
    a = sc.render_text_to_png(text)
    b = sc.render_text_to_png(text)
    assert a == b
    # Different text must not collide onto the same bytes.
    c = sc.render_text_to_png(text + "!")
    assert c != a


@pil_only
def test_render_to_image_is_dimensionally_deterministic_too():
    from ctx import snapcompact as sc

    text = "repeat me\n" * 5
    a = sc.render_text_to_image(text)
    b = sc.render_text_to_image(text)
    assert (a.width, a.height, a.lines) == (b.width, b.height, b.lines)
    assert a.png_bytes == b.png_bytes


@pil_only
def test_wrap_hard_wraps_long_lines_deterministically():
    from ctx import snapcompact as sc

    text = "x" * 25  # no newlines
    img = sc.render_text_to_image(text, chars_per_line=10)
    # 25 chars at 10 cols -> 3 wrapped rows (10 + 10 + 5), never fewer/more.
    assert img.lines == 3
    assert img.chars_per_line == 10


@pil_only
def test_wrap_preserves_existing_newlines_as_hard_breaks():
    from ctx import snapcompact as sc

    text = "a\n\nb"  # a blank line in the middle
    img = sc.render_text_to_image(text, chars_per_line=80)
    assert img.lines == 3


@pil_only
def test_empty_text_still_renders_a_one_line_image():
    from ctx import snapcompact as sc

    img = sc.render_text_to_image("", chars_per_line=10)
    assert img.lines == 1
    assert img.width > 0 and img.height > 0


@pil_only
def test_chars_per_line_and_font_size_are_plain_tunables_not_hardcoded():
    from ctx import snapcompact as sc

    small = sc.render_text_to_image("hello world", font_size=10, chars_per_line=40)
    big = sc.render_text_to_image("hello world", font_size=20, chars_per_line=40)
    assert small.font_size == 10 and big.font_size == 20
    # A larger em size must not shrink the measured cell.
    assert big.cell_height_px > small.cell_height_px


@pil_only
def test_invalid_chars_per_line_and_font_size_are_refused():
    from ctx import snapcompact as sc

    with pytest.raises(ValueError):
        sc.render_text_to_png("x", chars_per_line=0)
    with pytest.raises(ValueError):
        sc.render_text_to_png("x", font_size=0)


# ------------------------------------------------- missing-Pillow degrade


def test_snapcompact_unavailable_without_pillow_is_a_clear_error(monkeypatch):
    """House style for an opt-in feature whose engine is missing (see
    ctx.astgrep.EngineMissing / ctx.semgrep_engine.EngineMissing / the
    binfmt.py phrasing): a clear, actionable message -- never a bare
    ImportError traceback. Exercised via monkeypatch so it runs regardless
    of whether Pillow actually happens to be installed in this environment."""
    from ctx import snapcompact as sc

    monkeypatch.setattr(sc, "Image", None)
    monkeypatch.setattr(sc, "ImageDraw", None)
    monkeypatch.setattr(sc, "ImageFont", None)

    with pytest.raises(sc.SnapcompactUnavailable, match=r"ctx-harness\[image\]"):
        sc.render_text_to_png("anything")
    with pytest.raises(sc.SnapcompactUnavailable):
        sc.render_text_to_image("anything")
    with pytest.raises(sc.SnapcompactUnavailable):
        sc.estimate_savings("anything")


@pil_only
def test_falls_back_to_bundled_font_when_no_system_ttf_is_found(monkeypatch):
    """Degrade, never fabricate: when none of the checked font paths exist,
    Pillow's own bundled font is used instead of refusing to render."""
    from PIL import Image

    from ctx import snapcompact as sc

    monkeypatch.setattr(sc, "_font_path", lambda: None)
    sc._load_font.cache_clear()
    try:
        img = sc.render_text_to_image("fallback path", font_size=11, chars_per_line=20)
    finally:
        sc._load_font.cache_clear()  # never leak the synthetic "no font" result
    assert img.width > 0 and img.height > 0
    with Image.open(io.BytesIO(img.png_bytes)) as im:
        assert im.size == (img.width, img.height)


# ------------------------------------------------------- the token estimate


def test_image_tokens_matches_anthropics_own_worked_examples():
    """Golden values lifted verbatim from platform.claude.com/docs/en/
    build-with-claude/vision, 'Resolution and token cost' -- the three rows
    where the image is NOT downscaled, so ceil(w/28)*ceil(h/28) must match
    the documented number exactly."""
    from ctx.snapcompact import _image_tokens

    assert _image_tokens(200, 200, high_resolution=False) == 64
    assert _image_tokens(1000, 1000, high_resolution=False) == 1296
    assert _image_tokens(1092, 1092, high_resolution=False) == 1521


def test_image_tokens_caps_at_the_tier_limit():
    from ctx.snapcompact import _STANDARD_TIER, _HIGH_RES_TIER, _image_tokens

    assert _image_tokens(4000, 4000, high_resolution=False) <= _STANDARD_TIER["max_tokens"]
    assert _image_tokens(4000, 4000, high_resolution=True) <= _HIGH_RES_TIER["max_tokens"]
    # High-resolution tier must never be MORE restrictive than standard.
    assert _HIGH_RES_TIER["max_tokens"] >= _STANDARD_TIER["max_tokens"]


@pil_only
def test_estimate_savings_returns_plausible_labeled_numbers():
    from ctx import snapcompact as sc

    text = "line of text\n" * 200
    stats = sc.estimate_savings(text)

    assert stats["raw_bytes"] == len(text.encode("utf-8"))
    assert stats["raw_tokens"] > 0
    assert stats["image_tokens"] > 0
    assert stats["image_width_px"] > 0 and stats["image_height_px"] > 0
    assert stats["cell_area_px2"] > 0
    assert isinstance(stats["density_note"], str) and stats["density_note"]
    assert stats["ratio"] == pytest.approx(stats["raw_tokens"] / stats["image_tokens"])
    # The one claim this module refuses to make silently: fidelity is unverified.
    assert "cannot make" in stats["unverified"]
    assert "stencil.so/blog/snapcompact" in stats["unverified"]


@pil_only
def test_estimate_savings_raw_side_uses_the_shared_token_estimator():
    """The raw-token count must come from the SAME estimator every other
    cost/budget figure in this codebase trusts -- not a second one invented
    for this module."""
    from ctx.snapcompact import estimate_savings
    from ctx.textutil import estimate_tokens

    text = "a" * 4000
    stats = estimate_savings(text)
    assert stats["raw_tokens"] == estimate_tokens(len(text.encode("utf-8")))


# --------------------------------------------------- ctx get --snapcompact


def _fetch_full_blob_bytes(store, ws, blob_short: str) -> bytes:
    """Fetch a blob's complete bytes through `ctx get --bytes`, following
    the standard `next: ctx get ... --bytes A:B` continuation (docs/CLI.md)
    however many windows the retrieval budget actually needs -- rather than
    assuming one call covers a PNG of unknown compressed size."""
    from ctx.retrieval import Selector, get
    from ctx.textutil import encode_exact

    ref = f"blob:{blob_short}"
    chunks: list[bytes] = []
    start = 1
    for _ in range(64):  # generous ceiling; a real loop must terminate well before this
        out = get(store, ws, ref, Selector(bytes=(start, start + 1_000_000)))
        rest = out.split("\n", 2)[2]
        m = re.search(r"\nnext: ctx get blob:[0-9a-f]+ --bytes (\d+):(\d+)\Z", rest)
        if m:
            rest = rest[: m.start()]
        chunks.append(encode_exact(rest))
        if not m:
            return b"".join(chunks)
        start = int(m.group(1))
    raise AssertionError("blob fetch did not terminate -- continuation loop")


@pil_only
def test_get_snapcompact_returns_a_blob_ref_that_fetches_a_real_png(
    state_home, workspace_dir
):
    from PIL import Image

    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    f = workspace_dir / "notes.txt"
    f.write_text("\n".join(f"line {i}" for i in range(30)) + "\n", encoding="utf-8")

    out = get(store, ws, "repo:notes.txt", Selector(lines=(1, 30), snapcompact=True))
    assert "snapcompact: blob:" in out
    m = re.search(r"snapcompact: blob:([0-9a-f]{6,64})", out)
    assert m, out
    blob_short = m.group(1)

    dims = re.search(r"image/png, (\d+)x(\d+)px", out)
    assert dims, out
    header_w, header_h = int(dims.group(1)), int(dims.group(2))

    # The body must never leak the raw text once snapcompact is on -- the
    # whole point is that the text left the transcript.
    assert "line 0" not in out
    assert f"ctx get blob:{blob_short}" in out

    png_bytes = _fetch_full_blob_bytes(store, ws, blob_short)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    with Image.open(io.BytesIO(png_bytes)) as im:
        assert im.format == "PNG"
        assert im.size == (header_w, header_h)


@pil_only
def test_get_snapcompact_is_opt_in_default_behavior_unchanged(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    f = workspace_dir / "notes.txt"
    f.write_text("line 0\nline 1\n", encoding="utf-8")

    out = get(store, ws, "repo:notes.txt", Selector(lines=(1, 2)))
    assert "snapcompact" not in out
    assert "line 0" in out


def test_get_snapcompact_without_pillow_raises_clear_retrieval_error(
    state_home, workspace_dir, monkeypatch
):
    from ctx import snapcompact as sc
    from ctx.retrieval import RetrievalError, Selector, get

    monkeypatch.setattr(sc, "Image", None)
    monkeypatch.setattr(sc, "ImageDraw", None)
    monkeypatch.setattr(sc, "ImageFont", None)

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    f = workspace_dir / "notes.txt"
    f.write_text("line 0\nline 1\n", encoding="utf-8")

    with pytest.raises(RetrievalError, match=r"ctx-harness\[image\]"):
        get(store, ws, "repo:notes.txt", Selector(lines=(1, 2), snapcompact=True))


def test_get_snapcompact_rejects_combination_with_span(state_home, workspace_dir):
    """--span resolves and returns through its own path, never reaching the
    snapcompact rendering -- accepting the flag there would silently drop
    it, so the combination is refused instead (same house rule as the
    --symbol + anchored --lines rejection just below)."""
    from ctx.retrieval import RetrievalError, Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "notes.txt").write_text("a\nb\n", encoding="utf-8")

    with pytest.raises(RetrievalError, match="--span"):
        get(store, ws, "repo:notes.txt", Selector(span="deadbeef00", snapcompact=True))


@pil_only
def test_get_snapcompact_survives_the_symbol_rewrite(state_home, workspace_dir):
    """--symbol replaces the line range with one it resolves itself; the
    comment above that rewrite already warns hashlines would be silently
    dropped if not carried across -- snapcompact must survive it too."""
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    f = workspace_dir / "mod.py"
    f.write_text("def target():\n    return 1\n", encoding="utf-8")

    out = get(store, ws, "repo:mod.py", Selector(symbol="target", snapcompact=True))
    assert "snapcompact: blob:" in out


# ------------------------------------------------------------- CLI wiring


def test_cli_snapcompact_flag_is_registered_and_opt_in():
    from ctx.cli import _build_parser

    parser = _build_parser()
    ns_on = parser.parse_args(["get", "repo:x.txt", "--snapcompact"])
    assert ns_on.snapcompact is True
    ns_off = parser.parse_args(["get", "repo:x.txt"])
    assert ns_off.snapcompact is False


# ------------------------------------------------------------- MCP wiring


@pil_only
def test_mcp_get_selector_wires_snapcompact_through(state_home, workspace_dir):
    from ctx.mcp import _dispatch

    (workspace_dir / "notes.txt").write_text("hello from mcp\nsecond line\n", encoding="utf-8")

    out = _dispatch({
        "op": "get",
        "workspace": str(workspace_dir),
        "ref": "repo:notes.txt",
        "selector": {"snapcompact": True},
    })
    assert "snapcompact: blob:" in out


def test_mcp_get_selector_description_documents_snapcompact():
    from ctx.mcp import TOOL_SCHEMA

    props = TOOL_SCHEMA["inputSchema"]["properties"]
    assert "snapcompact" in props["selector"]["description"]
