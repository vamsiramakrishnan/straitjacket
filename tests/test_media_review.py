"""Review fixes for media reads (independent review of c360d01)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ctx import mediaread


def _plant(ws: Path, marker: Path) -> None:
    pkg = ws / "ctx"
    pkg.mkdir()
    body = f"open({str(marker)!r}, 'w').write('ran')\n"
    (pkg / "__init__.py").write_text(body)
    (pkg / "__main__.py").write_text(body)


@pytest.mark.parametrize("pythonpath", [None, "", ".", "ctx-not-abs"])
def test_redirect_never_imports_a_workspace_ctx_package(tmp_path, monkeypatch, pythonpath):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    marker = tmp_path / "PWNED"
    _plant(ws, marker)
    # The control: the unsafe launch form really would run it.
    subprocess.run([sys.executable, "-m", "ctx", "--version"], cwd=ws, capture_output=True,
                   env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert marker.exists(), "control failed: the planted package should run without -P"
    marker.unlink()
    if pythonpath is None:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONPATH", pythonpath)
    mediaread.make_overview(str(ws), "shot.png", 5000)  # the real launch
    assert not marker.exists()


# ------------------------------------- item 2: the window note is never false
from test_media_read import hso, pre, ws  # noqa: E402,F401  (ws is a fixture)


def _note(out):
    return hso(out).get("additionalContext", "")


def _dumps(o):
    import json

    return json.dumps(o)


@pytest.mark.parametrize("name,data,extra", [
    (".ctx-session-reads/media/overview-aaaaaaaaaaaa.png", b"\x89PNG\r\n\x1a\n" + b"x" * 40000, {}),
    ("Screenshot (1).png", b"\x89PNG\r\n\x1a\n" + b"x" * 40000, {}),
    ("doc.pdf", b"%PDF-1.7\n" + b"x" * 40000, {"pages": "1-3"}),
    ("doc2.pdf", b"%PDF-1.7\n" + b"x" * 40000, {}),
    ("nb.ipynb", b"{" + b" " * 40000 + b"}", {}),
    ("blob.zip", b"PK\x03\x04" + b"x" * 40000, {}),
])
def test_no_line_window_note_on_media_documents_or_session_files(
        ws, monkeypatch, capsys, name, data, extra):  # noqa: F811
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    out = pre(ws, "Read", {"file_path": str(p), **extra}, monkeypatch, capsys)
    assert "limited this Read" not in _note(out)


def test_no_window_note_on_codex(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "big.txt").write_text("line\n" * 20000)
    out = pre(ws, "read_file", {"path": str(ws / "big.txt")}, monkeypatch, capsys, "codex")
    assert "limited this Read" not in _dumps(out)


def test_short_file_over_the_byte_budget_is_not_called_partial(ws, monkeypatch, capsys):  # noqa: F811
    (ws / "wide.txt").write_text(("x" * 100 + "\n") * 170)  # 17,170 bytes, 170 lines
    note = _note(pre(ws, "Read", {"file_path": str(ws / "wide.txt")}, monkeypatch, capsys))
    assert "not the whole file" not in note
    assert "at most 240 lines starting at line 1 (the file is 17,170 bytes)" in note
    assert "If the result ends at line 240, the file may continue" in note


@pytest.mark.parametrize("neighbor", [
    '[budgets]\nmax_inline_bytes = "lots"\n',
    '[engagement]\nactivate_after_calls = "x"\n',
    "budgets = 5\n",
    '[guard]\nallow_commands = 42\n',
])
def test_media_choice_survives_malformed_neighbors(tmp_path, neighbor):
    """Review item 4: an opt-out must not depend on the rest of the file."""
    from ctx.config import load_config
    from ctx.hook import _load_guard_policy

    (tmp_path / "ctx.toml").write_text(
        neighbor + '[media]\nimage_read = "off"\nvideo_read = "off"\n')
    pol = _load_guard_policy(str(tmp_path))
    cfg = load_config(tmp_path)
    assert pol["media_image_read"] == cfg.media.image_read == "off"
    assert pol["media_video_read"] == cfg.media.video_read == "off"


@pytest.mark.parametrize("body", [
    "x/**\n" * 200_000,                                      # 1.2 MB
    "".join(f"g{i}/**\n" for i in range(1500)),             # small, too many globs
])
def test_a_huge_ctxignore_is_bounded_and_takes_the_safe_branch(ws, monkeypatch, capsys, body):  # noqa: F811
    import time

    from media_headers import mp4_header

    (ws / ".ctxignore").write_text(body)
    (ws / "clip.mp4").write_bytes(mp4_header())
    t = time.monotonic()
    out = pre(ws, "Read", {"file_path": str(ws / "clip.mp4")}, monkeypatch, capsys)
    assert time.monotonic() - t < 2
    assert "CTX_MEDIA" not in _dumps(out)  # no media handling: the ordinary path


def test_every_figure_names_its_basis_and_percentages_need_a_single_baseline(monkeypatch):
    """Review item 6."""
    from ctx import media

    p = media.MediaProbe(kind="image", format="png", bytes=1, sha256="", width=3000,
                         height=2000)
    monkeypatch.delenv("CTX_MODEL", raising=False)
    n, basis = media.raw_tokens(p)
    assert n == 4784 and "high end of the standard-to-high-res tier range" in basis
    assert "tok est., same on either tier" in mediaread.advise_note(
        "x.png", "png", 3000, 2000, host="claude-code", pillow=True)
    from ctx.commands.view import _render_text

    def facts(low):
        return {"target": "x.png", "view": "overview", "status": "ok", "notes": [],
                "omitted": [], "next": [], "source": {
                    "kind": "image", "format": "png", "bytes": 1, "sha256": "0" * 64,
                    "width": 3000, "height": 2000, "tokens_raw": 4784,
                    "tokens_raw_low": low, "tokens_basis": "b"},
                "derived": {"op": "overview", "mime": "image/png", "width": 1024,
                            "height": 683, "bytes": 1, "tokens": 925, "tokens_before": 4784,
                            "tokens_basis": "b", "engine": "pillow", "engine_version": "x",
                            "path": None, "meta": {"rows": 3, "cols": 4, "columns": "A-D"}}}

    assert "fewer" not in _render_text(facts(1568))       # range: no percentage
    monkeypatch.setenv("CTX_MODEL", "claude-opus-5-5")
    assert "81% fewer than the source's ~4,784" in _render_text(facts(4784))


@pytest.mark.parametrize("where,rules", [
    ("project", {"deny": ["Read(./private/**)"]}),
    ("local", {"ask": ["Read"]}),
    ("user", {"deny": ["Read(~/x)"]}),
    ("project", "not json"),
])
def test_redirect_steps_aside_for_the_users_read_rules(ws, monkeypatch, capsys, where, rules):  # noqa: F811
    import json as _json

    from test_media_read import _redirect_ws

    shot = _redirect_ws(ws)
    if where == "user":
        base = Path(os.environ["HOME"]) / ".claude"
    else:
        base = ws / ".claude"
    base.mkdir(parents=True, exist_ok=True)
    name = "settings.local.json" if where == "local" else "settings.json"
    (base / name).write_text(rules if isinstance(rules, str)
                             else _json.dumps({"permissions": rules}))
    called = []
    monkeypatch.setattr(mediaread, "make_overview", lambda *a: called.append(a) or (None, ""))
    h = hso(pre(ws, "Read", {"file_path": str(shot)}, monkeypatch, capsys))
    assert called == [] and "updatedInput" not in h
    assert "--cell" in h["additionalContext"]  # the advisory instead


@pytest.mark.parametrize("rel", [
    "shot.png", "Screenshot (1).png", "it's.png", "café.png", "-x.png", "blob:a.png",
    "PATH", "a b/c d.png", "x`id`.png", "uni‮code.png", "ok CTX_MEDIA x.png", "z" * 300])
def test_every_printed_command_parses_to_its_argv_or_is_a_marked_template(rel, monkeypatch):
    """Re-review item 3: each `ctx view ...` the module prints either splits
    (shlex) to exactly the argv meant, or is the PATH template and says so."""
    import re
    import shlex

    monkeypatch.setattr(mediaread, "has_pillow", lambda: True)
    monkeypatch.setattr(mediaread, "which", lambda n: f"/usr/bin/{n}")
    target, exact = mediaread.command_target(rel)
    texts = [
        mediaread.advise_note(target, "png", 4000, 3000, host="claude-code", pillow=True),
        mediaread.advise_note(target, "png", 4000, 3000, host="codex", pillow=True,
                              viewer="view_image"),
        mediaread.redirect_note(target, 4000, 3000, 1024, 768, 3, 4, host="claude-code"),
        mediaread.convert_note(target, "bmp", (640, 480)),
        mediaread.steer_reason(target, "mp4", "video"),
        mediaread.steer_reason(target, "mp3", "audio"),
        mediaread.shell_hint(target, "video"),
        mediaread.shell_hint(target, "audio"),
    ]
    for text in texts:
        cmds = re.findall(r"`(ctx view [^`]*)`", text)
        assert cmds, text
        for cmd in cmds:
            argv = shlex.split(cmd)
            assert argv[:2] == ["ctx", "view"]
            if exact:
                want = "./" + rel if rel.startswith("-") or rel.startswith("blob:") \
                    or rel == "PATH" else rel
                assert argv[2] == want, (cmd, rel)
            else:
                assert argv[2] == "PATH"
                assert "PATH stands for the file you just opened" in text
            assert all(not a.startswith(("<", ">")) for a in argv)


def test_view_json_names_the_derived_basis_as_the_text_does(monkeypatch):
    """Re-review item 4."""
    from types import SimpleNamespace

    from ctx.commands.view import _derived_basis

    monkeypatch.delenv("CTX_MODEL", raising=False)
    rec = SimpleNamespace(tokens_basis="image-patch, high end of the range")
    small = SimpleNamespace(mime="image/png", width=1024, height=683)
    assert _derived_basis(small, rec) == "same on either tier"
    big = SimpleNamespace(mime="image/png", width=1500, height=1500)
    assert _derived_basis(big, rec).startswith("high end of standard-to-high-res")
    text = SimpleNamespace(mime="text/plain", width=None, height=None)
    assert _derived_basis(text, rec) == rec.tokens_basis


def test_interleaved_sessions_keep_their_own_hint_state(tmp_path):
    """Re-review item 5: session B recording a hint must not erase A's."""
    from ctx.hook import _hint_state

    root = str(tmp_path)
    _hint_state(root, "A", "k", "delivered")
    _hint_state(root, "B", "k", "delivered")
    assert _hint_state(root, "A", "k") == "delivered"
    assert _hint_state(root, "B", "k") == "delivered"
    for i in range(40):  # bounded: old sessions age out
        _hint_state(root, f"S{i}", "k", "delivered")
    assert _hint_state(root, "A", "k") == ""


def test_user_rules_are_found_under_claude_config_dir(ws, tmp_path, monkeypatch):  # noqa: F811
    import json as _json

    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    (cfg / "settings.json").write_text(_json.dumps({"permissions": {"deny": ["Read"]}}))
    assert not mediaread.read_rules_present(str(ws))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert mediaread.read_rules_present(str(ws))


def test_a_backtracking_ctxignore_under_the_caps_is_fast(ws, monkeypatch, capsys):  # noqa: F811
    """Re-review item 1: 1,000 globs of `*a`x14+`*bN` (32 KB, under both
    caps) against a 190-character image name ran past 120 s before the
    pathglob engine was replaced; the hook must stay far inside its 10 s."""
    import time

    (ws / ".ctxignore").write_text("".join(("*a" * 14) + f"*b{i}\n" for i in range(1000)))
    name = "a" * 186 + ".png"
    from test_media_read import png_header

    (ws / name).write_bytes(png_header(4000, 3000))
    t = time.monotonic()
    out = pre(ws, "Read", {"file_path": str(ws / name)}, monkeypatch, capsys)
    assert time.monotonic() - t < 1.0
    assert "CTX_MEDIA" in _dumps(out)  # not excluded: no glob matches


def test_context_is_never_attached_to_deny_or_ask():
    """Mutation guard (review item 8): context rides only a call that runs."""
    from ctx.hook import _to_claude_code_schema, _to_codex_schema

    for raw in ("deny", "force_ask"):
        d = {"decision": raw, "reason": "r", "context": "SECRET-CONTEXT"}
        assert "SECRET-CONTEXT" not in _dumps(_to_claude_code_schema(d))
        assert "SECRET-CONTEXT" not in _dumps(_to_codex_schema(d))
    ok = {"decision": "allow", "context": "C"}
    assert _to_claude_code_schema(ok)["hookSpecificOutput"]["additionalContext"] == "C"
    assert _to_codex_schema(ok)["hookSpecificOutput"]["additionalContext"] == "C"
