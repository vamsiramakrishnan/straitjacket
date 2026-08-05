"""Acceptance: binary-format containment — typed image/pdf digests, the
binary/v1 run profile, and pixel-free perceptual diffing."""

import io
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ctx import binfmt

SRC = Path(__file__).resolve().parent.parent / "src"

_PIL = pytest.importorskip("PIL", reason="image extra (pillow) not installed")


def _png(w, h, shift=0):
    from PIL import Image

    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 3 + shift) % 256, (y * 2) % 256, 90)
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


# ------------------------------------------------------------- format core
def test_sniff_formats():
    assert binfmt.sniff_format(b"\x89PNG\r\n\x1a\n....") == "png"
    assert binfmt.sniff_format(b"\xff\xd8\xff\xe0") == "jpeg"
    assert binfmt.sniff_format(b"%PDF-1.7\n") == "pdf"
    assert binfmt.sniff_format(b"GIF89a") == "gif"
    assert binfmt.sniff_format(b"plain text here, no nul") == "text"
    assert binfmt.sniff_format(b"has a \x00 nul byte") == "binary"


def test_png_dimensions_stdlib():
    info = binfmt.inspect(_png(320, 240))
    assert info.format == "png"
    assert (info.width, info.height) == (320, 240)
    assert info.color == "rgb"
    assert len(info.sha256) == 64


def test_jpeg_and_gif_dimensions():
    from PIL import Image

    jb = io.BytesIO()
    Image.open(io.BytesIO(_png(128, 64))).save(jb, "JPEG")
    ji = binfmt.inspect(jb.getvalue())
    assert ji.format == "jpeg" and (ji.width, ji.height) == (128, 64)
    gb = io.BytesIO()
    Image.open(io.BytesIO(_png(48, 24))).save(gb, "GIF")
    gi = binfmt.inspect(gb.getvalue())
    assert gi.format == "gif" and (gi.width, gi.height) == (48, 24)


def test_pdf_page_count_and_text_stdlib():
    pdf = (b"%PDF-1.5\n1 0 obj<</Type /Catalog>>endobj\n"
           b"2 0 obj<</Type /Page>>endobj\n3 0 obj<</Type /Page>>endobj\n"
           b"BT (hello) Tj ET\n%%EOF")
    info = binfmt.inspect(pdf)
    assert info.format == "pdf"
    assert info.pages == 2
    assert info.text_extractable is True


def test_digest_never_contains_pixels():
    data = _png(200, 150)
    d = binfmt.render_digest(binfmt.inspect(data), address="ctx get run:x#stdout")
    assert "image: 200×150" in d
    assert "phash:" in d
    assert "ctx get run:x#stdout" in d
    # the digest is tiny relative to the raw bytes
    assert len(d.encode()) < len(data)


# ------------------------------------------------------------- perceptual diff
def test_perceptual_hash_identical_is_zero():
    h = binfmt.inspect(_png(200, 150)).perceptual_hash
    assert h is not None
    assert binfmt.phash_distance(h, h) == 0


def test_perceptual_diff_discriminates():
    base = binfmt.inspect(_png(200, 150)).perceptual_hash
    big = binfmt.inspect(_png(200, 150, shift=100)).perceptual_hash
    assert binfmt.phash_distance(base, big) > 12   # substantial change detected


def test_phash_distance_bad_input():
    assert binfmt.phash_distance(None, "abc") is None
    assert binfmt.phash_distance("zz", "00") is None


# ------------------------------------------------------------- run profile
def test_ctx_run_binary_profile_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "shot.png").write_bytes(_png(320, 240))
        proc = subprocess.run(
            [sys.executable, "-m", "ctx", "run", "--shell", "cat shot.png"],
            cwd=td, capture_output=True, text=True,
            env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
        )
    out = proc.stdout
    assert "profile=binary/v1" in out          # NOT text/v1
    assert "image: 320×240 rgb" in out
    # The captured-run digest is identity-bearing, so it must NOT carry the
    # Pillow-gated phash line (that would make the run id depend on an optional
    # extra). The perceptual hash lives on the interactive `ctx image` path.
    assert "phash:" not in out
    assert "status: exit 0" in out             # exit status is preserved
    assert "ctx get run:" in out and "#stdout" in out   # working address


def test_binary_profile_preserves_failure_evidence():
    # A process that emits a valid PNG on stdout but fails on stderr with a
    # nonzero exit must not be rendered as an apparently-normal image.
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "shot.png").write_bytes(_png(64, 48))
        script = "cat shot.png; echo 'FATAL conversion failed' >&2; exit 7"
        proc = subprocess.run(
            [sys.executable, "-m", "ctx", "run", "--shell", script],
            cwd=td, capture_output=True, text=True,
            env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
        )
    out = proc.stdout
    assert "profile=binary/v1" in out          # still a typed binary digest
    assert "image: 64×48" in out
    assert "status: exit 7" in out             # the failure is not swallowed
    assert "FATAL conversion failed" in out    # stderr diagnostics survive


def test_binary_digest_identity_independent_of_pillow():
    # render_digest with include_perceptual=False must be byte-identical whether
    # or not Pillow could produce a hash — that body feeds the run identity.
    data = _png(120, 90)
    info = binfmt.inspect(data)
    body = binfmt.render_digest(info, address="ctx get run:x#stdout",
                                include_perceptual=False)
    assert "phash" not in body
    # Simulate the Pillow-absent environment: identity body is unchanged.
    info_no_pill = binfmt.inspect(data)
    info_no_pill.perceptual_hash = None
    body2 = binfmt.render_digest(info_no_pill, address="ctx get run:x#stdout",
                                 include_perceptual=False)
    assert body == body2


def test_ctx_image_diff_cli():
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.png", Path(td) / "b.png"
        a.write_bytes(_png(200, 150))
        b.write_bytes(_png(200, 150, shift=100))
        env = {"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")}
        r = subprocess.run([sys.executable, "-m", "ctx", "image", "diff", str(a), str(b)],
                           capture_output=True, text=True, env=env)
        assert "perceptual distance:" in r.stdout
        r2 = subprocess.run([sys.executable, "-m", "ctx", "image", "diff", str(a), str(a)],
                            capture_output=True, text=True, env=env)
        assert "identical render" in r2.stdout and "byte-identical" in r2.stdout


def test_text_output_still_text_profile():
    # regression: the binary profile must NOT capture ordinary text output.
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "log.txt").write_text("line one\nline two\nall good\n")
        proc = subprocess.run(
            [sys.executable, "-m", "ctx", "run", "--shell", "cat log.txt"],
            cwd=td, capture_output=True, text=True,
            env={"PYTHONPATH": str(SRC), "PATH": __import__("os").environ.get("PATH", "")},
        )
    assert "binary/v1" not in proc.stdout
