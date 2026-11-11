"""Media wiring outside the hook: `@file` routing under `ctx wrap claude`,
intake's description of images and video, and the governance/coverage rows."""

from __future__ import annotations

import os
import stat
import struct
from pathlib import Path

import pytest

from conftest import make_store, make_ws
from media_headers import ftyp, mp4_header

# ------------------------------------------------------------ @file routing


def _fake_claude(bin_dir: Path, env_file: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--help" ]; then echo "usage: claude [--settings <file>]"; exit 0; fi\n'
        f"printenv CLAUDE_CODE_DISABLE_FILE_MENTIONS > {env_file} || echo UNSET > {env_file}\n"
        "exit 0\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


@pytest.fixture()
def wrap_env(tmp_path, monkeypatch, state_home):
    for var in ("CLAUDE_CODE_DISABLE_FILE_MENTIONS", "CTX_WRAP_NO_FILE_MENTIONS"):
        monkeypatch.delenv(var, raising=False)
    ws = tmp_path / "proj"
    ws.mkdir()
    env_file = tmp_path / "env.txt"
    _fake_claude(tmp_path / "bin", env_file)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    return ws, env_file


def _launch(ws, args=("-p", "hi")):
    from ctx.wrap import wrap_claude

    assert wrap_claude(ws, list(args), "/opt/bin/ctx") == 0


def test_wrap_leaves_file_mentions_alone_by_default(wrap_env, capsys):
    ws, env_file = wrap_env
    _launch(ws)
    assert env_file.read_text().strip() == "UNSET"
    assert "@file" not in capsys.readouterr().err


@pytest.mark.parametrize("args", [("-p", "hi"), ()])
def test_wrap_routes_file_mentions_when_opted_in(wrap_env, capsys, args):
    ws, env_file = wrap_env
    (ws / "ctx.toml").write_text("[media]\nfile_mentions = true\n")
    _launch(ws, args)
    assert env_file.read_text().strip() == "1"
    assert "@file mentions are read through the Read tool" in capsys.readouterr().err


def test_the_users_own_value_wins(wrap_env, monkeypatch, capsys):
    ws, env_file = wrap_env
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_FILE_MENTIONS", "")
    (ws / "ctx.toml").write_text("[media]\nfile_mentions = true\n")
    _launch(ws)
    assert env_file.read_text().strip() == ""
    assert "@file" not in capsys.readouterr().err


@pytest.mark.parametrize("how", ["env", "config", "advisory"])
def test_file_mention_routing_opt_outs(wrap_env, monkeypatch, capsys, how):
    ws, env_file = wrap_env
    if how == "env":
        monkeypatch.setenv("CTX_WRAP_NO_FILE_MENTIONS", "1")
        (ws / "ctx.toml").write_text("[media]\nfile_mentions = true\n")
    elif how == "config":
        (ws / "ctx.toml").write_text("[media]\nfile_mentions = false\n")
    else:
        (ws / "ctx.toml").write_text('[guard]\nmode = "advisory"\n[media]\nfile_mentions = true\n')
    _launch(ws)
    assert env_file.read_text().strip() == "UNSET"
    assert "@file" not in capsys.readouterr().err


def test_routing_never_touches_the_parent_environment(wrap_env):
    ws, _ = wrap_env
    (ws / "ctx.toml").write_text("[media]\nfile_mentions = true\n")
    _launch(ws)
    assert "CLAUDE_CODE_DISABLE_FILE_MENTIONS" not in os.environ


# ------------------------------------------------------------------ intake


def _png(w, h):
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00" * 200


def _mp4_with_moov(seconds: int, w: int, h: int) -> bytes:
    def box(kind, body):
        return struct.pack(">I", 8 + len(body)) + kind + body

    mvhd = box(b"mvhd", b"\x00" * 4 + b"\x00" * 8 + struct.pack(">II", 1000, seconds * 1000)
               + b"\x00" * 80)
    tkhd = box(b"tkhd", b"\x00" * 76 + struct.pack(">II", w << 16, h << 16))
    return ftyp() + box(b"moov", mvhd + box(b"trak", tkhd))


def test_intake_describes_an_image_with_exact_view_commands(workspace_dir, state_home):
    from ctx.intake import intake_bytes

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    rec = intake_bytes(store, ws, _png(2560, 1440), label="file", name="shot.png")
    text = rec.digest
    addr = rec.address
    assert ("media: image 2560×1440 · ~1,568-4,784 tok if shown whole (estimate; "
            "standard-to-high-res tier range, model unknown)") in text
    assert f"next: ctx view {addr}  (overview with a labelled 3x5 grid)" in text
    assert f"next: ctx view {addr} --grid 3x5 --cell B2" in text and "~342 tok est." in text
    again = intake_bytes(store, ws, _png(2560, 1440), label="file", name="shot.png")
    assert again.digest == text  # identity: a pure function of the bytes


def test_intake_describes_a_video_from_its_header(workspace_dir, state_home):
    from ctx.intake import intake_bytes

    ws = make_ws(workspace_dir)
    rec = intake_bytes(make_store(ws), ws, _mp4_with_moov(42, 1920, 1080), label="file")
    assert "media: video mp4 · 1920×1080 · 0:42 (header)" in rec.digest
    assert f"next: ctx view {rec.address}  (contact sheet" in rec.digest
    assert f"next: ctx view {rec.address} --at 21" in rec.digest


def test_intake_survives_a_hostile_container(workspace_dir, state_home):
    from ctx.intake import _isobmff_facts, intake_bytes

    huge = mp4_header() + struct.pack(">I", 1) + b"moov" + struct.pack(">Q", 2**63)
    assert _isobmff_facts(huge) == (None, None)
    loop = mp4_header() + (struct.pack(">I", 8) + b"free") * 5000
    assert _isobmff_facts(loop) == (None, None)
    ws = make_ws(workspace_dir)
    rec = intake_bytes(make_store(ws), ws, huge, label="file")
    assert "duration: from `ctx view`" in rec.digest


def test_intaken_image_command_resolves(workspace_dir, state_home, monkeypatch):
    """The blob address in the intake digest is one `ctx view` reads."""
    pytest.importorskip("PIL")
    import io
    import subprocess
    import sys

    from PIL import Image

    from ctx.intake import intake_bytes

    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), "white").save(buf, "PNG")
    ws = make_ws(workspace_dir)
    rec = intake_bytes(make_store(ws), ws, buf.getvalue(), label="file")
    cmd = [ln for ln in rec.digest.splitlines() if "--cell" in ln][0]
    argv = cmd.removeprefix("next: ctx ").split("  (")[0].split()
    cp = subprocess.run([sys.executable, "-m", "ctx", *argv], cwd=workspace_dir,
                        capture_output=True, text=True, env=dict(os.environ))
    assert cp.returncode == 0, cp.stderr
    assert "view: crop" in cp.stdout


# --------------------------------------------------------------- governance


def test_every_host_declares_its_media_reads():
    from ctx import governance as gov

    assert set(gov.MEDIA_READS) == set(gov.HOSTS)
    assert any("clipboard" in x for x in gov.LIMITS["claude-code"])
    assert any("@-referenced" in x and "opt-in" in x for x in gov.LIMITS["claude-code"])
    assert any("Contract only" in x for x in gov.MEDIA_READS["codex"])
    assert gov.MEDIA_READS["antigravity"][0].startswith("unchanged")


def test_coverage_prints_media_rows_and_observed_decisions(tmp_path):
    import json

    from ctx import coverage

    led = tmp_path / ".ctx-session-reads"
    led.mkdir()
    row = {"schema": "ctx.coverage/v1", "host": "claude-code", "stage": "pre",
           "tool": "Read", "kind": "read", "route": "bound", "decision": "allowed",
           "note": "media:advise", "session": "s", "ts": 1}
    (led / "coverage.jsonl").write_text(json.dumps(row) + "\n")
    data = coverage.report(tmp_path)
    assert data["hosts"]["claude-code"]["media_reads"]
    text = coverage.render(data)
    assert "  media reads:" in text
    assert "claude-code Read media:advise: 1" in text
