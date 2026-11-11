"""Acceptance: `ctx.media` — overview first, zoom on demand.

Fixtures are generated at test time: images are drawn with Pillow and the
clips come from ffmpeg's `testsrc` source. Everything that needs Pillow or
ffmpeg skips cleanly without them (the minimal CI job has neither); the
grammar, limits, unavailable-engine and materialization tests need neither.
"""

from __future__ import annotations

import hashlib
import io
import random
import shutil
import subprocess
import sys

import media_headers as H
import pytest

from conftest import make_store, make_ws
from ctx import media

HAS_PILLOW = True
try:
    from PIL import Image, ImageDraw
except Exception:
    HAS_PILLOW = False

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="Pillow (the `image` extra) absent")
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe absent")
needs_both = pytest.mark.skipif(not (HAS_PILLOW and HAS_FFMPEG),
                                reason="Pillow and ffmpeg both required")


# ----------------------------------------------------------------- fixtures
def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _photo(w: int = 800, h: int = 600) -> bytes:
    """Photograph-like: smooth gradients under per-pixel noise, saved as JPEG."""
    rnd = random.Random(7)
    noise = Image.frombytes("RGB", (w, h), rnd.randbytes(w * h * 3))
    ramp = Image.linear_gradient("L").resize((w, h))
    base = Image.merge("RGB", (ramp, ramp.rotate(90), ramp.rotate(180)))
    out = io.BytesIO()
    Image.blend(base, noise, 0.25).save(out, "JPEG", quality=92)
    return out.getvalue()


def _screenshot(w: int = 1280, h: int = 720) -> bytes:
    """Screenshot-like: flat panels, rules and text in a handful of colours."""
    im = Image.new("RGB", (w, h), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, 40), fill=(40, 44, 52))
    d.rectangle((0, 40, 200, h), fill=(225, 228, 232))
    for i in range(30):
        d.text((220, 60 + i * 20), f"row {i}: the quick brown fox", fill=(20, 20, 20))
    return _png(im)


def _cells_image(w: int, h: int, rows: int, cols: int) -> bytes:
    """Every grid cell a distinct flat colour, edges by the project's own
    integer arithmetic: a crop must come back one solid colour."""
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = c * w // cols, r * h // rows
            x1, y1 = (c + 1) * w // cols, (r + 1) * h // rows
            d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(10 + r * 40, 20 + c * 30, 200 - r * 10 - c))
    return _png(im)


def _run_ffmpeg(args, out):
    cp = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args, str(out)],
                        capture_output=True)
    if cp.returncode != 0:
        pytest.skip("ffmpeg could not build the test clip: " + cp.stderr.decode()[-200:])
    return out


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """4 s, 320x240, 10 fps testsrc + audio + a text subtitle track."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe absent")
    d = tmp_path_factory.mktemp("clip")
    (d / "subs.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\nhello there\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nsecond cue\n", encoding="utf-8")
    return _run_ffmpeg(
        ["-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=duration=4", "-i", str(d / "subs.srt"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text"],
        d / "clip.mp4")


@pytest.fixture(scope="module")
def scene_clip(tmp_path_factory):
    """2 s of testsrc then 2 s of flat red: one hard cut at t=2."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe absent")
    d = tmp_path_factory.mktemp("scenes")
    return _run_ffmpeg(
        ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-f", "lavfi", "-i", "color=c=red:duration=2:size=320x240:rate=10",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p"],
        d / "scenes.mp4")


def _hide_pillow(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)


# ------------------------------------------------- grammar (no dependencies)
def test_time_grammar():
    assert media.parse_time("12.5") == 12.5
    assert media.parse_time("12.5s") == 12.5
    assert media.parse_time("1:23") == 83.0
    assert media.parse_time("1:02:03.5") == 3723.5
    assert media.parse_time("250ms") == 0.25
    for bad in ("", "abc", "-1", "-0", "+5", "1:-30", "1:2:3:4", "nan", "inf", "1e3", ".5", "5.",
                "1::2", "1:90", "1.5:30", "1:60", "0:5s", "ms", "-5ms", "1 2", "0x10"):
        with pytest.raises(media.RegionError):
            media.parse_time(bad)
    assert str(media.parse_time(-0.0)) == "0.0" and media.parse_time("0") == 0.0
    with pytest.raises(media.RegionError):
        media.parse_time(-1)
    with pytest.raises(media.RegionError):
        media.parse_range("2-1:-30")
    with pytest.raises(media.RegionError):
        media.parse_range("-0-5")


def test_range_grammar():
    assert media.parse_range("2-9") == (2.0, 9.0)
    assert media.parse_range("12.5..20") == (12.5, 20.0)
    assert media.parse_range("1:23-1:30") == (83.0, 90.0)
    assert media.parse_range("0:05-0:10") == (5.0, 10.0)
    for bad in ("9-2", "5", "", "-", "2-"):
        with pytest.raises(media.RegionError):
            media.parse_range(bad)
    # `A:B` is the ambiguous spelling (--at 1:30 means 90 s); it is refused, and
    # the message says how to write what was meant.
    with pytest.raises(media.RegionError, match="ambiguous"):
        media.parse_range("1:30")
    with pytest.raises(media.RegionError, match="ambiguous"):
        media.parse_range("2:9")


def test_grid_grammar():
    assert media.parse_grid("3x4") == (3, 4)
    assert media.parse_grid(" 2 X 5 ") == (2, 5)
    for bad in ("3", "0x4", "3x27", "axb", "3x4x5"):
        with pytest.raises(media.RegionError):
            media.parse_grid(bad)


def test_default_grid_follows_orientation():
    assert media.default_grid(1600, 900) == (3, 5)
    assert media.default_grid(900, 1600) == (5, 3)
    assert media.default_grid(1000, 1000) == (3, 3)
    assert media.default_grid(5000, 100) == (3, 6)  # capped


def test_region_pixels_and_percent():
    r = media.parse_region("10,20,100,50", (400, 300))
    assert (r.x, r.y, r.w, r.h, r.clipped) == (10, 20, 100, 50, False)
    p = media.parse_region("25%,50%,50%,50%", (400, 300))
    assert (p.x, p.y, p.w, p.h) == (100, 150, 200, 150)


def test_region_clips_at_the_edge_and_rejects_outside():
    r = media.parse_region("350,250,100,100", (400, 300))
    assert (r.w, r.h, r.clipped) == (50, 50, True)
    for bad in ("400,0,10,10", "0,300,10,10", "-1,0,10,10", "0,0,0,10",
                "0,0,1.5,10", "1,2,3", "10%,10%,50,50", "0%,0%,101%,10%"):
        with pytest.raises(media.RegionError):
            media.parse_region(bad, (400, 300))


def test_grid_cell_arithmetic_partitions_the_image_exactly():
    for size, grid in (((1001, 777), (3, 4)), ((1920, 1080), (7, 5)), ((7, 5), (5, 7))):
        w, h = size
        rows, cols = grid
        covered = 0
        for r in range(rows):
            for c in range(cols):
                reg = media.parse_region(media.cell_label(r, c), size, grid)
                assert reg.w >= 0 and reg.h >= 0
                covered += reg.w * reg.h
        assert covered == w * h, (size, grid)
        last = media.parse_region(media.cell_label(rows - 1, cols - 1), size, grid)
        assert last.box[2:] == (w, h)                  # last edge lands on W, H
        first = media.parse_region("A1", size, grid)
        assert first.box[:2] == (0, 0)


def test_grid_cell_examples_and_ranges():
    r = media.parse_region("B2", (400, 300), (3, 4))
    assert (r.x, r.y, r.w, r.h, r.label) == (100, 100, 100, 100, "B2")
    rng = media.parse_region("A1:B2", (400, 300), (3, 4))
    assert rng.box == (0, 0, 200, 200) and rng.label == "A1:B2"
    rev = media.parse_region("b2:a1", (400, 300), (3, 4))
    assert rev.box == rng.box                           # corners in any order, any case
    whole = media.parse_region("A1:D3", (400, 300), (3, 4))
    assert whole.box == (0, 0, 400, 300)
    edge = media.parse_region("D3", (401, 301), (3, 4))
    assert edge.box[2:] == (401, 301)


def test_grid_cell_errors_name_the_valid_range():
    with pytest.raises(media.RegionError, match=r"outside the 3x4 grid.*A-D.*1-3"):
        media.parse_region("E1", (400, 300), (3, 4))
    with pytest.raises(media.RegionError):
        media.parse_region("A4", (400, 300), (3, 4))
    with pytest.raises(media.RegionError):
        media.parse_region("A1:B2:C3", (400, 300), (3, 4))
    with pytest.raises(media.RegionError):
        media.parse_region("hello", (400, 300), (3, 4))


def test_sample_times_bounds_and_declares_the_cut():
    times, dropped = media.sample_times(0, 10, count=4)
    assert times == [1.25, 3.75, 6.25, 8.75] and dropped == 0
    times, dropped = media.sample_times(0, 10, every=2)
    assert times == [0, 2, 4, 6, 8] and dropped == 0
    times, dropped = media.sample_times(0, 1000, count=500, cap=media.MAX_SHEET_FRAMES)
    assert len(times) == media.MAX_SHEET_FRAMES and dropped == 500 - media.MAX_SHEET_FRAMES
    times, dropped = media.sample_times(0, 100000, every=0.5, cap=24)
    assert len(times) == 24 and dropped > 0
    for bad in (dict(count=0), dict(every=0), dict(every=-1)):
        with pytest.raises(media.RegionError):
            media.sample_times(0, 10, **bad)
    with pytest.raises(media.RegionError):
        media.sample_times(5, 5, count=3)


def test_timestamp_format_is_locale_free_and_stable():
    assert media.fmt_ts(0) == "0:00.0"
    assert media.fmt_ts(3.75) == "0:03.8"
    assert media.fmt_ts(83.04) == "1:23.0"
    assert media.fmt_ts(3723.5) == "1:02:03.5"
    assert media.fmt_ts(-4) == "0:00.0"


# ------------------------------------------------------------ probe (always)
def test_probe_never_raises_and_classifies_without_engines(monkeypatch, tmp_path):
    _hide_pillow(monkeypatch)
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x80\x00\x00\x01\xe0\x08\x06\x00\x00\x00"
           + b"\x00" * 8)
    p = media.probe(png)
    assert (p.kind, p.format, p.width, p.height) == ("image", "png", 640, 480)
    assert p.has_alpha is True and p.animated is None
    assert any("Pillow" in n for n in p.notes)
    mp4 = H.mp4_header()
    v = media.probe(mp4)
    assert (v.kind, v.format) == ("video", "mp4")
    assert any("ffprobe" in n for n in v.notes)
    assert media.probe(b"hello\n").kind == "other"
    assert media.probe(b"%PDF-1.4\n").kind == "pdf"
    assert media.probe(H.wav()).kind == "audio"
    missing = media.probe(tmp_path / "nope.png")
    assert missing.kind == "other" and missing.notes
    assert "sha256" in media.probe(png).to_dict()


# ----------------------------------------------- missing engines and timeouts
def test_missing_pillow_is_a_declared_skip(monkeypatch):
    _hide_pillow(monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    for call in (lambda: media.fit(png), lambda: media.crop(png, "A1"),
                 lambda: media.overview(png)):
        with pytest.raises(media.MediaUnavailable) as err:
            call()
        assert err.value.needs == "pillow"
        assert "Pillow" in err.value.reason and "image" in err.value.hint


def test_missing_ffmpeg_is_a_declared_skip(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    mp4 = H.mp4_header()
    for call in (lambda: media.frame_at(mp4, 1), lambda: media.scene_changes(mp4),
                 lambda: media.extract_subtitles(mp4)):
        with pytest.raises(media.MediaUnavailable) as err:
            call()
        assert err.value.needs in ("ffprobe", "ffmpeg")
        assert "PATH" in err.value.reason


def test_subprocess_timeout_is_a_declared_skip_not_a_traceback(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen = []
    killed = []

    class HungProcess:
        pid = 4242

        def __init__(self, argv, **kw):
            seen.append((argv, kw))

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            killed.append("proc")

    monkeypatch.setattr(media.subprocess, "Popen", HungProcess)
    monkeypatch.setattr(media.os, "killpg", lambda pid, sig: killed.append(("group", pid)))
    mp4 = H.mp4_header()
    assert any("did not finish" in n for n in media.probe(mp4).notes)  # probe degrades
    with pytest.raises(media.MediaUnavailable) as err:
        media.frame_at(mp4, 1)
    assert err.value.needs == "timeout" and "did not finish" in err.value.reason
    # every subprocess is an argument list with no shell and a timeout
    for argv, kw in seen:
        assert isinstance(argv, list) and not kw.get("shell")
        assert kw["stdin"] is subprocess.DEVNULL and kw["cwd"]      # private working dir
        assert kw["start_new_session"] is True                      # own process group
        assert argv[argv.index("-f") + 1] == "mov"                  # demuxer pinned
    assert ("group", 4242) in killed                                # whole group killed


def test_only_recognised_containers_reach_ffmpeg(monkeypatch):
    called = []
    monkeypatch.setattr(media.subprocess, "Popen", lambda *a, **k: called.append(a))
    for hostile in (b"#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n",
                    b"ffconcat version 1.0\nfile 'secrets/notes.txt'\n",
                    H.NEAR_MISSES["id3_concat"][0], H.NEAR_MISSES["mkv_prose"][0]):
        with pytest.raises(media.MediaUnsupported):
            media.frame_at(hostile, 0)
    assert not called


# ------------------------------------------------------------------- images
@needs_pillow
def test_fit_never_enlarges_and_never_grows_the_file():
    small = _screenshot(200, 100)
    d = media.fit(small, 1024)
    assert (d.width, d.height) == (200, 100)
    assert d.record.bytes_after <= len(small)
    assert d.meta["resized"] is False
    tiny = _png(Image.new("RGB", (8, 8), (1, 2, 3)))
    assert media.fit(tiny, 1024).record.bytes_after <= len(tiny)
    big = media.fit(_photo(), 256)
    assert max(big.width, big.height) == 256
    assert big.record.tokens_after < big.record.tokens_before


@needs_pillow
def test_fit_picks_an_encoding_for_the_content():
    assert media.fit(_screenshot(), 512).mime == "image/png"
    assert media.fit(_photo(), 512).mime == "image/jpeg"
    rgba = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).ellipse((50, 50, 250, 150), fill=(255, 0, 0, 255))
    d = media.fit(_png(rgba), 1024)
    assert d.mime == "image/png"
    assert Image.open(io.BytesIO(d.data)).mode == "RGBA"
    opaque = Image.new("RGBA", (300, 200), (9, 9, 9, 255))
    assert Image.open(io.BytesIO(media.fit(_png(opaque), 100).data)).mode == "RGB"


@needs_pillow
def test_fit_is_deterministic_for_one_engine_version():
    data = _photo()
    a, b = media.fit(data, 300), media.fit(data, 300)
    assert a.data == b.data and a.record == b.record
    assert a.record.engine == "pillow" and a.record.engine_version == media.pillow_version()
    assert a.record.source_sha256 == hashlib.sha256(data).hexdigest()
    assert a.record.output_sha256 == hashlib.sha256(a.data).hexdigest()
    assert a.record.bytes_before == len(data) and a.record.bytes_after == len(a.data)
    assert a.record.params == {"max_edge": 300}


@needs_pillow
def test_exif_orientation_is_applied_and_metadata_stripped():
    base = Image.new("RGB", (200, 100), (200, 30, 30))
    ImageDraw.Draw(base).rectangle((0, 0, 20, 20), fill=(0, 0, 255))   # marks top-left
    exif = Image.Exif()
    exif[0x0112] = 6                                                   # rotate 90 cw to display
    exif[0x010F] = "SecretCam"
    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=95, exif=exif)
    raw = buf.getvalue()
    p = media.probe(raw)
    assert (p.width, p.height, p.orientation) == (100, 200, 6)         # as displayed
    d = media.fit(raw, 1024)
    assert (d.width, d.height) == (100, 200)
    out = Image.open(io.BytesIO(d.data))
    assert not out.getexif() and b"SecretCam" not in d.data
    # blue marker started top-left; a 90deg clockwise turn puts it top-right
    assert out.convert("RGB").getpixel((90, 8))[2] > 150
    assert d.meta["exif_orientation_applied"] == 6
    c = media.crop(raw, "0,0,50,50")                                   # oriented coordinates
    assert (c.width, c.height) == (50, 50)


@needs_pillow
def test_crop_reads_original_pixels_by_cell_and_region():
    w, h = 1001, 997
    data = _cells_image(w, h, 3, 4)
    src = Image.open(io.BytesIO(data)).convert("RGB")
    for label, grid in (("B2", (3, 4)), ("D3", (3, 4)), ("A1", (3, 4))):
        d = media.crop(data, label, grid=grid)
        reg = media.parse_region(label, (w, h), grid)
        out = Image.open(io.BytesIO(d.data)).convert("RGB")
        assert out.size == (reg.w, reg.h)
        assert out.getcolors(10) is not None and len(out.getcolors(10)) == 1  # one solid cell
        assert out.getpixel((0, 0)) == src.getpixel((reg.x, reg.y))
        assert d.meta["region"]["label"] == label and d.record.params["box"][2:] == [reg.w, reg.h]
    rng = media.crop(data, "A1:B2", grid=(3, 4))
    assert (rng.width, rng.height) == (media.parse_region("A1:B2", (w, h), (3, 4)).w,
                                       media.parse_region("A1:B2", (w, h), (3, 4)).h)
    pix = media.crop(data, "100,50,30,20")
    assert (pix.width, pix.height) == (30, 20)
    pct = media.crop(data, "50%,50%,50%,50%")
    assert pct.width == w - round(w / 2) and pct.height == h - round(h / 2)


@needs_pillow
def test_crop_clips_and_downsizes_only_when_asked():
    data = _screenshot(1280, 720)
    clipped = media.crop(data, "1200,700,500,500")
    assert (clipped.width, clipped.height) == (80, 20)
    assert any("clipped" in o for o in clipped.omitted)
    big = media.crop(data, "0,0,1280,720", max_edge=400)
    assert max(big.width, big.height) == 400 and big.meta["resized"]
    full = media.crop(data, "0,0,1280,720", max_edge=2000)
    assert (full.width, full.height) == (1280, 720) and not full.meta["resized"]
    with pytest.raises(media.RegionError):
        media.crop(data, "2000,0,10,10")
    with pytest.raises(media.RegionError):
        media.crop(data, "Z9", grid=(3, 4))


@needs_pillow
def test_overview_draws_a_grid_a_model_can_name():
    data = _screenshot(1280, 720)
    ov = media.overview(data, max_edge=800)
    assert ov.meta["grid"] == "3x5" and (ov.width, ov.height) == (800, 450)
    plain = Image.open(io.BytesIO(media.fit(data, 800).data)).convert("RGB")
    drawn = Image.open(io.BytesIO(ov.data)).convert("RGB")
    assert plain.size == drawn.size
    x = 1 * 800 // 5                                       # first interior column line
    assert drawn.getpixel((x, 400)) != plain.getpixel((x, 400))
    far = [abs(a - b) for a, b in zip(drawn.getpixel((x + 40, 400)),
                                       plain.getpixel((x + 40, 400)), strict=True)]
    assert max(far) < 40                                   # untouched away from the lines
    fine = media.overview(data, max_edge=800, grid=(6, 8))
    assert fine.meta["grid"] == "6x8"
    with pytest.raises(media.RegionError, match="too dense"):
        media.overview(data, max_edge=300, grid=(20, 20))


@needs_pillow
def test_overview_cell_covers_the_same_area_as_the_crop():
    data = _cells_image(1200, 800, 3, 4)
    grid = (3, 4)
    ov = Image.open(io.BytesIO(media.overview(data, max_edge=600, grid=grid).data)).convert("RGB")
    full = Image.open(io.BytesIO(data)).convert("RGB")
    for label in ("A1", "C2", "D3"):
        reg = media.parse_region(label, (1200, 800), grid)
        cx, cy = reg.x + reg.w // 2, reg.y + reg.h // 2
        px_full = full.getpixel((cx, cy))
        px_ov = ov.getpixel((cx // 2, cy // 2))
        assert sum(abs(a - b) for a, b in zip(px_full, px_ov, strict=True)) < 12, label


# ----------------------------------------------------------------- guard rails
@needs_pillow
def test_pixel_bomb_is_refused_before_decoding():
    bomb = io.BytesIO()
    Image.new("1", (9000, 9000)).save(bomb, "PNG")              # ~81 Mpx, a few KB
    data = bomb.getvalue()
    assert len(data) < 100_000 and 9000 * 9000 > media.MAX_IMAGE_PIXELS
    for call in (lambda: media.fit(data), lambda: media.crop(data, "A1"),
                 lambda: media.overview(data)):
        with pytest.raises(media.MediaLimitError, match="pixels"):
            call()
    p = media.probe(data)                                       # advisory: never raises
    assert (p.width, p.height) == (9000, 9000) and any("limit" in n for n in p.notes)


@needs_pillow
@pytest.mark.filterwarnings("ignore:Image size")
def test_pillows_own_bomb_error_is_converted(monkeypatch):
    import PIL.Image

    monkeypatch.setattr(PIL.Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(media.MediaLimitError, match="bomb"):
        media.fit(_screenshot(400, 300))


@needs_pillow
def test_byte_limit_and_garbage(monkeypatch):
    data = _screenshot(100, 100)
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(media.MediaLimitError, match="limit"):
        media.fit(data)
    monkeypatch.undo()
    with pytest.raises(media.MediaError):
        media.fit(b"\x89PNG\r\n\x1a\n" + b"garbage" * 5)


@needs_pillow
def test_svg_is_a_declared_skip():
    with pytest.raises(media.MediaUnavailable) as err:
        media.fit(b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>')
    assert err.value.needs == "renderer"


@needs_pillow
def test_animated_image_is_a_short_video(tmp_path):
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    gif = tmp_path / "a.gif"
    frames[0].save(gif, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
    p = media.probe(gif)
    assert (p.kind, p.animated, p.frames) == ("image", True, 3)
    assert p.duration_s == pytest.approx(0.6) and p.is_timeline
    f = media.frame_at(gif, 0.25)                                # second frame is green
    px = Image.open(io.BytesIO(f.data)).convert("RGB").getpixel((5, 5))
    assert px[1] > 200 and px[0] < 60
    sheet = media.contact_sheet(gif, count=3)
    assert sheet.meta["frames"] == 3 and len(sheet.meta["labels"]) == 3
    still = media.fit(gif, 32)                                   # first frame, declared
    assert any("first frame only" in o for o in still.omitted)
    assert media.fit(_screenshot(50, 50), 32).meta.get("exif_orientation_applied") is None


# ------------------------------------------------------------------- video
@needs_ffmpeg
def test_probe_census_of_a_clip(clip):
    p = media.probe(clip)
    assert (p.kind, p.format, p.width, p.height) == ("video", "mp4", 320, 240)
    assert p.duration_s == pytest.approx(4.0, abs=0.3) and p.fps == pytest.approx(10.0)
    assert p.video_codec == "h264" and p.audio_codec == "aac"
    assert len(p.audio_streams) == 1 and len(p.subtitle_streams) == 1
    assert p.subtitle_streams[0]["text"] is True and p.engine == "ffprobe"
    assert media.raw_tokens(p)[1].startswith("video@1fps")
    assert media.probe(clip.read_bytes()).sha256 == p.sha256     # bytes or path


@needs_both
def test_contact_sheet_frame_count_labels_and_geometry(clip):
    d = media.contact_sheet(clip, count=6)
    m = d.meta
    assert m["frames"] == 6 and len(m["labels"]) == 6 and len(m["times"]) == 6
    assert m["times"] == sorted(m["times"]) and 0 < m["times"][0] < m["times"][-1] < 4
    for i, (label, t) in enumerate(zip(m["labels"], m["times"], strict=True), start=1):
        assert label == f"{i}  {media.fmt_ts(t)}"
    tw, th = m["tile"]
    gap = 2
    assert d.width == m["cols"] * tw + gap * (m["cols"] + 1)
    assert d.height == m["rows"] * th + gap * (m["rows"] + 1)
    assert m["rows"] * m["cols"] >= 6 > m["rows"] * (m["cols"] - 1) - 1
    assert Image.open(io.BytesIO(d.data)).size == (d.width, d.height)
    from ctx.snapcompact import _image_tokens

    assert d.record.tokens_basis.startswith("video@1fps")
    assert d.record.tokens_after == _image_tokens(d.width, d.height, high_resolution=False)
    assert d.record.tokens_before == 4 * _image_tokens(320, 240, high_resolution=False)
    assert d.record.engine == "ffmpeg+pillow" and "ffmpeg" in d.record.engine_version
    assert any("audio" in o for o in d.omitted) and any("subtitles" in o for o in d.omitted)
    again = media.contact_sheet(clip, count=6)
    assert again.data == d.data and again.record == d.record     # deterministic


@needs_both
def test_contact_sheet_range_and_cap(clip):
    d = media.contact_sheet(clip, count=4, start=1, end="0:03")
    assert d.meta["range"] == [1.0, 3.0] and all(1 <= t < 3 for t in d.meta["times"])
    capped = media.contact_sheet(clip, count=500)
    assert capped.meta["frames"] == media.MAX_SHEET_FRAMES
    assert any("cap" in o for o in capped.omitted)
    with pytest.raises(media.RegionError):
        media.contact_sheet(clip, start=9, end=12)
    with pytest.raises(media.RegionError):
        media.contact_sheet(clip, count=0)


@needs_both
def test_single_frame_and_frames_over_a_range(clip):
    f = media.frame_at(clip, "0:02", max_edge=160)
    assert max(f.width, f.height) == 160 and f.meta["label"] == "0:02.0"
    full = media.frame_at(clip, 1.0, max_edge=4096)
    assert (full.width, full.height) == (320, 240)               # never upscaled
    past = media.frame_at(clip, 999)                              # clamped inside the clip
    assert past.meta["t"] < 4.0
    items, dropped = media.frames(clip, 0, 4, every=1.0)
    assert len(items) == 4 and dropped == 0
    items, dropped = media.frames(clip, 0, 4, count=2)
    assert [i.meta["t"] for i in items] == [1.0, 3.0]
    with pytest.raises(media.MediaUnsupported):
        media.frame_at(_screenshot(40, 40), 1)


@needs_ffmpeg
def test_scene_changes_find_the_cut(scene_clip):
    d = media.scene_changes(scene_clip)
    changes = d.meta["changes"]
    assert len(changes) == 1 and changes[0]["t"] == pytest.approx(2.0, abs=0.25)
    assert changes[0]["score"] > 0.5
    assert d.data.decode().split()[0] == f"{changes[0]['t']:.3f}"
    quiet = media.scene_changes(scene_clip, start=2.5, end=4)
    assert quiet.meta["changes"] == [] and quiet.data == b""
    assert any("threshold" in o for o in d.omitted)


@needs_ffmpeg
def test_subtitle_track_extraction(clip):
    d = media.extract_subtitles(clip)
    text = d.data.decode()
    assert "hello there" in text and "second cue" in text and d.ext == "srt"
    assert d.meta["cues"] == 2 and d.record.op == "subs"
    cut = media.extract_subtitles(clip, max_bytes=40)
    assert cut.meta["cues_kept"] < cut.meta["cues"] and any("beyond" in o for o in cut.omitted)
    with pytest.raises(media.RegionError):
        media.extract_subtitles(clip, stream=3)


@needs_ffmpeg
def test_video_without_subtitles_says_so(scene_clip):
    with pytest.raises(media.MediaUnsupported, match="no embedded subtitle"):
        media.extract_subtitles(scene_clip)


# --------------------------------------------------------- materialization
def _derived(data: bytes = b"\x89PNG-derived-bytes", op: str = "overview") -> media.Derived:
    digest = hashlib.sha256(data).hexdigest()
    rec = media.Derivation(
        source_sha256="a" * 64, op=op, params={"max_edge": 1024}, engine="pillow",
        engine_version="0", output_sha256=digest, bytes_before=10 * len(data),
        bytes_after=len(data), tokens_before=100, tokens_after=10)
    return media.Derived(data=data, mime="image/png", ext="png", op=op, record=rec,
                         width=2, height=2)


def test_materialize_is_idempotent_and_leased(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    d = _derived()
    first = media.materialize(ws, store, d)
    assert first.created is True
    assert first.rel_path == f".ctx-session-reads/media/overview-{d.record.output_sha256[:12]}.png"
    assert not first.rel_path.startswith("/") and str(workspace_dir) not in first.rel_path
    target = workspace_dir / first.rel_path
    assert target.read_bytes() == d.data
    assert store.get_blob(first.blob) == d.data
    assert first.address == f"blob:{first.blob[:12]}"
    lease = store.db.execute("SELECT reason, expires_at FROM leases WHERE id=?",
                             (first.blob,)).fetchone()
    assert lease is not None and lease[0] == "media-derivative" and lease[1] is not None

    mtime = target.stat().st_mtime_ns
    second = media.materialize(ws, store, _derived())
    assert (second.rel_path, second.blob, second.address) == (first.rel_path, first.blob,
                                                               first.address)
    assert second.created is False and target.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]  # no stray temp files

    target.write_bytes(b"tampered")                               # a damaged copy is repaired
    third = media.materialize(ws, store, _derived())
    assert third.created is True and target.read_bytes() == d.data


def test_materialize_names_differ_by_output_and_op(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    a = media.materialize(ws, store, _derived(b"one"))
    b = media.materialize(ws, store, _derived(b"two"))
    c = media.materialize(ws, store, _derived(b"one", op="crop"))
    assert len({a.rel_path, b.rel_path, c.rel_path}) == 3
    assert c.blob == a.blob                                       # same bytes, one stored blob


def test_materialize_refuses_a_session_dir_that_escapes(state_home, workspace_dir, tmp_path):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (workspace_dir / ".ctx-session-reads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(media.MediaError):
        media.materialize(ws, store, _derived())
    assert list(outside.iterdir()) == []
