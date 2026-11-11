"""Acceptance: every `next:` line `ctx view` prints is a command the tool
accepts and that does something (writes a derived file to open), across a
matrix of awkward inputs - and a capped animation scan is not stated as the
file's length."""

from __future__ import annotations

import random
import shlex
import shutil
import subprocess

import pytest

from ctx.cli import main

HAS_PILLOW = True
try:
    from PIL import Image, ImageDraw
except Exception:
    HAS_PILLOW = False
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

pytestmark = pytest.mark.skipif(not (HAS_PILLOW and HAS_FFMPEG), reason="Pillow and ffmpeg required")


def run(ws, *args, capsys):
    rc = main(["--workspace", str(ws), "view", *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _ff(args, out):
    cp = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args, str(out)], capture_output=True)
    if cp.returncode != 0:
        pytest.skip("ffmpeg could not build the fixture")


@pytest.fixture()
def matrix(state_home, workspace_dir, tmp_path):
    ws = workspace_dir
    rnd = random.Random(5)
    shot = Image.new("RGB", (1280, 720), (245, 245, 245))
    d = ImageDraw.Draw(shot)
    for i in range(25):
        d.text((220, 60 + i * 20), f"row {i}: the quick brown fox", fill=(20, 20, 20))
    shot.save(ws / "shot.png")
    Image.frombytes("RGB", (800, 600), rnd.randbytes(800 * 600 * 3)).save(ws / "photo.jpg", quality=80)
    for name, size in (("one.png", (1, 1)), ("icon.png", (64, 64)), ("strip.png", (4000, 20)),
                       ("tall.png", (30, 3000)), ("tiny.png", (23, 23)), ("mid.png", (200, 120))):
        Image.new("RGB", size, (9, 9, 9)).save(ws / name)
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    frames[0].save(ws / "anim.gif", save_all=True, append_images=frames[1:], duration=300, loop=0)
    (tmp_path / "s.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nhello there\n", encoding="utf-8")
    _ff(["-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10", "-i", str(tmp_path / "s.srt"),
         "-map", "0", "-map", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "srt"], ws / "subs.mkv")
    _ff(["-f", "lavfi", "-i", "testsrc=duration=12:size=160x120:rate=5", "-c:v", "libx264",
         "-pix_fmt", "yuv420p"], ws / "clip.mp4")
    return ws


FIXTURES = ["shot.png", "photo.jpg", "one.png", "icon.png", "strip.png", "tall.png", "tiny.png",
            "mid.png", "anim.gif", "subs.mkv", "clip.mp4"]


def _next_args(out: str) -> list[list[str]]:
    lines = [ln for ln in out.splitlines() if ln.startswith("next: ")]
    cmds = []
    for ln in lines:
        argv = shlex.split(ln.removeprefix("next: "))
        assert argv[:2] == ["ctx", "view"], ln
        cmds.append(argv[2:])
    return cmds


def test_every_next_line_is_accepted_and_does_something(matrix, capsys):
    executed = 0
    for name in FIXTURES:
        rc, out, err = run(matrix, name, capsys=capsys)
        assert rc == 0, (name, err)
        first = _next_args(out)
        assert first, name
        for argv in first:
            rc, out2, err2 = run(matrix, *argv, capsys=capsys)
            executed += 1
            assert rc == 0, (name, argv, err2)
            quiet_scan = "scenes: no change above the threshold" in out2   # a true, empty answer
            assert "derived:" in out2 and ("\nopen: " in out2 or quiet_scan), (name, argv)
            for argv2 in _next_args(out2)[:2]:                       # and one level deeper
                rc, out3, err3 = run(matrix, *argv2, capsys=capsys)
                executed += 1
                assert rc == 0 and "derived:" in out3, (name, argv, argv2, err3)
    assert executed > 60


def test_the_icon_no_longer_suggests_a_grid_it_cannot_draw(matrix, capsys):
    rc, out, _ = run(matrix, "icon.png", capsys=capsys)
    assert rc == 0 and "--grid 4x4" not in out and "--fit 1600" not in out
    rc, out, _ = run(matrix, "shot.png", capsys=capsys)
    assert "--grid 6x10 --fit 1600" in out                           # still offered where it works


def test_a_capped_animation_scan_is_stated_as_a_lower_bound(matrix, capsys):
    frames = [Image.new("RGB", (8, 8), (i % 256, 0, 0)) for i in range(520)]
    frames[0].save(matrix / "long.gif", save_all=True, append_images=frames[1:], duration=100, loop=0)
    rc, out, _ = run(matrix, "long.gif", capsys=capsys)
    assert rc == 0
    source = next(ln for ln in out.splitlines() if ln.startswith("source:"))
    assert "at least 500 frames" in source and "≥0:50.0" in source
    assert "scanned to 500 frames" in out
    assert "at least" in next(ln for ln in out.splitlines() if ln.startswith("raw cost"))
    rc, out, _ = run(matrix, "anim.gif", capsys=capsys)              # a complete scan stays exact
    source = next(ln for ln in out.splitlines() if ln.startswith("source:"))
    assert "3 frames" in source and "at least" not in source and "≥" not in source


def test_time_grammar_errors_are_usage_errors(matrix, capsys):
    for args in (("--at", "1:-30"), ("--at", "-0"), ("--range", "2-1:-30"), ("--at", "1e3"),
                 ("--range", "1:30")):
        rc, out, err = run(matrix, "clip.mp4", *args, capsys=capsys)
        assert rc == 2 and "cannot read" in err and out == "", args
