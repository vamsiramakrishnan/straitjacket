"""Acceptance: `ctx view` — the CLI verb over `ctx.media`.

Help parity is checked against the real parser and `ctx.cliux`; behaviour is
driven in-process through `ctx.cli.main`. Pillow/ffmpeg-dependent cases skip
cleanly; the degrade paths (declared skip, usage errors, path policy, audio
census) run everywhere.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave

import media_headers as H
import pytest

from conftest import make_store, make_ws
from ctx import cliux
from ctx.cli import _COMMANDS, _build_parser, main

HAS_PILLOW = True
try:
    from PIL import Image, ImageDraw
except Exception:
    HAS_PILLOW = False
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

needs_pillow = pytest.mark.skipif(not HAS_PILLOW, reason="Pillow (the `image` extra) absent")
needs_both = pytest.mark.skipif(not (HAS_PILLOW and HAS_FFMPEG),
                                reason="Pillow and ffmpeg both required")

_PNG_HEADER = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x0a\x00\x00\x00\x05\xa0\x08\x06"
               b"\x00\x00\x00" + b"\x00" * 8)


def run(ws_dir, *args, capsys):
    rc = main(["--workspace", str(ws_dir), "view", *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _shot(path, w=1280, h=720):
    im = Image.new("RGB", (w, h), (245, 245, 245))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, 40), fill=(40, 44, 52))
    for i in range(25):
        d.text((220, 60 + i * 20), f"row {i}: the quick brown fox", fill=(20, 20, 20))
    im.save(path)


@pytest.fixture()
def ws(state_home, workspace_dir):
    return workspace_dir


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe absent")
    d = tmp_path_factory.mktemp("viewclip")
    (d / "s.srt").write_text("1\n00:00:00,500 --> 00:00:01,500\nhello there\n", encoding="utf-8")
    cp = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=6:size=320x240:rate=10",
         "-f", "lavfi", "-i", "color=c=red:duration=4:size=320x240:rate=10",
         "-i", str(d / "s.srt"),
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:s",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:s", "mov_text", str(d / "c.mp4")],
        capture_output=True)
    if cp.returncode != 0:
        pytest.skip("ffmpeg could not build the clip: " + cp.stderr.decode()[-200:])
    return d / "c.mp4"


# ------------------------------------------------------------- help parity
def test_view_is_registered_everywhere():
    assert "view" in _COMMANDS and _COMMANDS["view"][:2] == ("view", "cmd_view")
    assert "view" in cliux.all_commands()
    import importlib

    assert callable(importlib.import_module("ctx.commands.view").cmd_view)


def test_help_lists_every_selector_and_matches_the_parser():
    parser = _build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")
    text = sub.choices["view"].format_help()
    for flag in ("--fit", "--grid", "--cell", "--region", "--at", "--range", "--every",
                 "--count", "--sheet", "--scenes", "--subs", "--json"):
        assert flag in text, flag
    for word in ("ORIGINAL", "blob:", "run:"):
        assert word in text
    # the parser accepts exactly what the help promises
    ns = parser.parse_args(["view", "x.png", "--fit", "512", "--grid", "3x4", "--cell", "B2",
                            "--region", "0,0,1,1", "--at", "1", "--range", "1-2", "--every", "0.5",
                            "--count", "3", "--sheet", "--scenes", "--subs", "--json"])
    assert (ns.fit, ns.every, ns.count, ns.json) == (512, 0.5, 3, True)
    one_line = cliux.all_commands()["view"]
    assert "image" in one_line and "video" in one_line


def test_listed_under_find_and_read():
    group = dict(cliux.AGENT_GROUPS)["Find and read"]
    assert "view" in [c for c, _ in group]


# --------------------------------------------------------- degrade (no deps)
def test_declared_skip_without_pillow_is_not_a_traceback(ws, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    (ws / "shot.png").write_bytes(_PNG_HEADER)
    rc, out, err = run(ws, "shot.png", capsys=capsys)
    assert rc == 1 and "Traceback" not in out + err
    assert "source: image png 2560×1440" in out
    assert "skipped: Pillow is not installed" in out and "hint: install the `image` extra" in out
    rc, out, _ = run(ws, "shot.png", "--json", capsys=capsys)
    doc = json.loads(out)
    assert rc == 1 and doc["schema"] == "ctx.view/v1" and doc["status"] == "unavailable"
    assert doc["skipped"]["needs"] == "pillow" and doc["derived"] is None


def test_declared_skip_without_ffmpeg(ws, capsys, monkeypatch):
    monkeypatch.setattr("ctx.media.shutil.which", lambda name: None)
    (ws / "v.mp4").write_bytes(H.mp4_header())
    rc, out, err = run(ws, "v.mp4", capsys=capsys)
    assert rc == 1 and "Traceback" not in out + err
    assert "source: video mp4" in out and "skipped:" in out and "ffprobe" in out
    assert "no audio" not in out and "0 subtitle" not in out      # unknown, not "none"
    assert "streams unknown" in out


def test_audio_gets_a_census_and_is_never_transcribed(ws, capsys):
    with wave.open(str(ws / "tone.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    rc, out, err = run(ws, "tone.wav", capsys=capsys)
    assert rc == 0, err
    assert "source: audio wav" in out and "never transcribes" in out
    assert "open:" not in out and ".ctx-session-reads" not in out
    rc, _, err = run(ws, "tone.wav", "--at", "1", capsys=capsys)
    assert rc == 2 and "no visual view" in err


def test_non_media_is_digest_only(ws, capsys):
    (ws / "notes.txt").write_text("secret plan\n", encoding="utf-8")
    rc, out, _ = run(ws, "notes.txt", capsys=capsys)
    assert rc == 0 and "other text" in out and "secret plan" not in out


# ------------------------------------------------------------- path policy
def test_files_outside_the_workspace_are_refused_with_a_way_forward(ws, tmp_path, capsys):
    outside = tmp_path / "attachment.png"
    outside.write_bytes(_PNG_HEADER)
    rc, out, err = run(ws, str(outside), capsys=capsys)
    assert rc == 1 and out == ""
    assert "outside the workspace" in err and "ctx run -- cat" in err
    assert str(tmp_path) not in err                       # no host path echoed
    (ws / "link.png").symlink_to(outside)                 # a symlink out is the same escape
    rc, _, err = run(ws, "link.png", capsys=capsys)
    assert rc == 1 and "outside the workspace" in err


def test_policy_excluded_and_missing_paths(ws, capsys):
    (ws / ".ctxignore").write_text("private/\n", encoding="utf-8")
    (ws / "private").mkdir()
    (ws / "private" / "a.png").write_bytes(_PNG_HEADER)
    rc, _, err = run(ws, "private/a.png", capsys=capsys)
    assert rc == 1 and "excluded from capture by policy" in err
    rc, _, err = run(ws, "nope.png", capsys=capsys)
    assert rc == 1 and "cannot read" in err
    rc, _, err = run(ws, "blob:ffffffffffff", capsys=capsys)
    assert rc == 1 and "no object matches" in err
    (ws / "adir").mkdir()
    rc, _, err = run(ws, "adir", capsys=capsys)
    assert rc == 1 and "not a regular file" in err


# ------------------------------------------------------------- usage errors
def test_selector_conflicts_are_usage_errors(ws, capsys):
    (ws / "v.bin").write_bytes(_PNG_HEADER)
    cases = [
        ("--cell", "A1", "--region", "0,0,1,1"),
        ("--at", "1", "--range", "1-2"),
        ("--at", "1", "--scenes"),
        ("--every", "1", "--count", "2"),
        ("--subs", "--sheet"),
        ("--scenes", "--count", "3"),
        ("--count", "0"),
        ("--every", "-1"),
    ]
    for case in cases:
        rc, out, err = run(ws, "v.bin", *case, capsys=capsys)
        assert rc == 2 and out == "" and "ctx view:" in err, case


# ------------------------------------------------------------------- images
@needs_pillow
def test_image_overview_digest(ws, capsys):
    _shot(ws / "shot.png")
    rc, out, err = run(ws, "shot.png", capsys=capsys)
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[0] == "[ctx view shot.png]" and lines[1] == "view: overview"
    assert any(line.startswith("source: image png 1280×720") for line in lines)
    assert any(line.startswith("raw cost: ~") and "image-patch" in line for line in lines)
    derived = next(line for line in lines if line.startswith("derived: overview"))
    assert "tok" in derived and "pillow" in derived
    assert any(line.startswith("grid: 3 rows") and "5 columns (A-E" in line for line in lines)
    assert any(line.startswith("omitted: pixels:") for line in lines)
    opened = next(line for line in lines if line.startswith("open: "))[6:]
    assert opened.startswith(".ctx-session-reads/media/overview-") and (ws / opened).is_file()
    nexts = [line for line in lines if line.startswith("next: ctx view shot.png")]
    assert 2 <= len(nexts) <= 6
    assert any("--cell" in n for n in nexts) and any("--region" in n for n in nexts)
    assert str(ws) not in out and "\x1b" not in out


@needs_pillow
def test_next_commands_run_and_zoom_into_the_original(ws, capsys):
    _shot(ws / "shot.png")
    _, out, _ = run(ws, "shot.png", capsys=capsys)
    nxt = next(line for line in out.splitlines() if "--cell" in line and line.startswith("next: "))
    argv = nxt.removeprefix("next: ctx view ").split()
    rc, zoom, err = run(ws, *argv, capsys=capsys)
    assert rc == 0, err
    assert zoom.splitlines()[1] == "view: crop" and "(original pixels)" in zoom
    region = next(line for line in zoom.splitlines() if line.startswith("region:"))
    assert region.startswith("region: C2 = x512 y240 256×240 of 1280×720")


@needs_pillow
def test_cell_region_fit_and_grid_selectors(ws, capsys):
    _shot(ws / "shot.png")
    rc, out, _ = run(ws, "shot.png", "--grid", "2x2", "--cell", "B2", capsys=capsys)
    assert rc == 0 and "region: B2 = x640 y360 640×360" in out
    rc, out, _ = run(ws, "shot.png", "--region", "10%,10%,20%,20%", capsys=capsys)
    assert rc == 0 and "region: 10%,10%,20%,20% = x128 y72 256×144" in out
    rc, out, _ = run(ws, "shot.png", "--fit", "320", capsys=capsys)
    assert rc == 0 and "view: fit" in out and "derived: fit 320×180" in out
    rc, out, _ = run(ws, "shot.png", "--grid", "2x3", "--fit", "600", capsys=capsys)
    assert rc == 0 and "derived: overview 600×338" in out and "2 rows" in out
    rc, _, err = run(ws, "shot.png", "--cell", "Z9", capsys=capsys)
    assert rc == 2 and "outside the 3x5 grid" in err
    rc, _, err = run(ws, "shot.png", "--grid", "0x0", "--cell", "A1", capsys=capsys)
    assert rc == 2 and "grid" in err
    rc, _, err = run(ws, "shot.png", "--at", "3", capsys=capsys)
    assert rc == 2 and "still image" in err


@needs_pillow
def test_output_is_deterministic_and_the_path_is_stable(ws, capsys):
    _shot(ws / "shot.png")
    _, first, _ = run(ws, "shot.png", capsys=capsys)
    _, second, _ = run(ws, "shot.png", capsys=capsys)
    assert first == second


@needs_pillow
def test_json_schema_matches_the_text_facts(ws, capsys):
    _shot(ws / "shot.png")
    rc, out, _ = run(ws, "shot.png", "--json", capsys=capsys)
    doc = json.loads(out)
    assert rc == 0 and doc["schema"] == "ctx.view/v1" and doc["status"] == "ok"
    assert doc["target"] == "shot.png" and doc["view"] == "overview"
    assert {"kind", "format", "bytes", "sha256", "width", "height", "tokens_raw",
            "tokens_basis"} <= set(doc["source"])
    der = doc["derived"]
    assert {"op", "path", "address", "bytes", "tokens", "tokens_before", "engine",
            "engine_version", "output_sha256", "source_sha256", "params", "meta"} <= set(der)
    assert der["path"].startswith(".ctx-session-reads/media/") and der["address"].startswith("blob:")
    assert der["source_sha256"] == doc["source"]["sha256"]
    assert doc["omitted"] and doc["next"] and isinstance(doc["notes"], list)
    _, text, _ = run(ws, "shot.png", capsys=capsys)
    assert der["path"] in text and f"~{der['tokens']:,} tok" in text
    for cmd in doc["next"]:
        assert f"next: {cmd}" in text


@needs_pillow
def test_blob_address_target_and_file_stays_untouched(ws, state_home, capsys):
    _shot(ws / "shot.png")
    data = (ws / "shot.png").read_bytes()
    store = make_store(make_ws(ws))
    blob = store.put_blob(data)
    rc, out, err = run(ws, f"blob:{blob[:12]}", capsys=capsys)
    assert rc == 0, err
    assert out.splitlines()[0] == f"[ctx view blob:{blob[:12]}]"
    assert f"next: ctx view blob:{blob[:12]} --grid" in out
    assert (ws / "shot.png").read_bytes() == data


@needs_pillow
def test_run_address_with_stream(ws, state_home, capsys):
    _shot(ws / "shot.png")
    ws_obj = make_ws(ws)
    store = make_store(ws_obj)
    blob = store.put_blob((ws / "shot.png").read_bytes())
    manifest = store.put_manifest(
        {"kind": "invocation", "cwd": ".",
         "streams": {"stdout": {"blob": f"sha256:{blob}", "bytes": 1, "lines": 0,
                                "mediaType": "application/octet-stream"}}},
        kind="manifest")
    short = manifest.removeprefix("sha256:")[:12]
    rc, out, err = run(ws, f"run:{short}", capsys=capsys)
    assert rc == 0, err
    assert out.splitlines()[0] == f"[ctx view run:{short}#stdout]"
    rc, _, err = run(ws, f"run:{short}#stderr", capsys=capsys)
    assert rc == 1 and "no stream 'stderr'" in err


# -------------------------------------------------------------------- video
@needs_both
def test_video_default_is_a_contact_sheet_with_a_census(ws, clip, capsys):
    shutil.copy(clip, ws / "c.mp4")
    rc, out, err = run(ws, "c.mp4", capsys=capsys)
    assert rc == 0, err
    lines = out.splitlines()
    assert lines[1] == "view: sheet"
    src = next(line for line in lines if line.startswith("source: video mp4 320×240"))
    assert "0:10.0" in src and "10 fps" in src and "h264" in src and "no audio" in src
    assert "1 subtitle track " in src + " "
    assert any("video@1fps" in line for line in lines if line.startswith("raw cost"))
    assert any(line.startswith("frames: 12 over 0:00.0–0:10.0") for line in lines)
    assert any(line.startswith("omitted: 12 sampled frames") for line in lines)
    assert any(line.startswith("omitted: subtitles") for line in lines)
    nexts = [line for line in lines if line.startswith("next: ctx view c.mp4")]
    assert any("--at" in n for n in nexts) and any("--range" in n for n in nexts)
    assert any(n.endswith("--scenes") for n in nexts) and any(n.endswith("--subs") for n in nexts)
    opened = next(line for line in lines if line.startswith("open: "))[6:]
    assert (ws / opened).is_file() and opened.endswith((".jpg", ".png"))
    assert str(ws) not in out


@needs_both
def test_video_zoom_selectors(ws, clip, capsys):
    shutil.copy(clip, ws / "c.mp4")
    rc, out, err = run(ws, "c.mp4", "--at", "0:03", capsys=capsys)
    assert rc == 0, err
    assert "view: frame" in out and "frame: at 0:03.0" in out
    rc, out, _ = run(ws, "c.mp4", "--range", "2-6", "--count", "4", capsys=capsys)
    assert rc == 0 and "frames: 4 over 0:02.0–0:06.0" in out
    rc, out, _ = run(ws, "c.mp4", "--sheet", "--every", "2.5", capsys=capsys)
    assert rc == 0 and "frames: 4 over 0:00.0–0:10.0" in out
    rc, out, _ = run(ws, "c.mp4", "--scenes", capsys=capsys)
    assert rc == 0 and "scenes: 1 change(s)" in out and "0:06.0" in out
    assert any(line.startswith("next: ctx view c.mp4 --at 6") for line in out.splitlines())
    rc, out, _ = run(ws, "c.mp4", "--subs", capsys=capsys)
    assert rc == 0 and "hello there" in out and "subtitles: 1 of 1 cues" in out
    srt = next(line for line in out.splitlines() if line.startswith("open: "))[6:]
    assert srt.endswith(".srt") and "hello there" in (ws / srt).read_text()
    rc, _, err = run(ws, "c.mp4", "--cell", "A1", capsys=capsys)
    assert rc == 2 and "applies to still images" in err
    rc, _, err = run(ws, "c.mp4", "--at", "x", capsys=capsys)
    assert rc == 2 and "cannot read time" in err
    rc, _, err = run(ws, "c.mp4", "--range", "5-2", capsys=capsys)
    assert rc == 2 and "must end after" in err


@needs_both
def test_video_json_and_animated_gif(ws, clip, capsys):
    shutil.copy(clip, ws / "c.mp4")
    rc, out, _ = run(ws, "c.mp4", "--json", capsys=capsys)
    doc = json.loads(out)
    assert rc == 0 and doc["view"] == "sheet" and doc["source"]["subtitle_streams"][0]["text"]
    assert doc["derived"]["meta"]["frames"] == 12 and len(doc["derived"]["meta"]["labels"]) == 12
    assert doc["source"]["tokens_basis"].startswith("video@1fps")
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    frames[0].save(ws / "a.gif", save_all=True, append_images=frames[1:], duration=300, loop=0)
    rc, out, err = run(ws, "a.gif", capsys=capsys)
    assert rc == 0, err
    assert "view: sheet" in out and "3 frames" in out
