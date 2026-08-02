"""`ctx get --bytes A:B` returns the bytes asked for. All of them, only them.

docs/CLI.md calls --bytes the exact-bytes escape hatch, and get.py itself
tells callers to "use --bytes A:B for exact slices" for binary content. It
then decoded the slice with errors="replace", so every undecodable byte
became a 3-byte U+FFFD: the result was neither the same bytes nor the same
LENGTH as what was captured, through the tool's own exactness interface,
while the blob on disk stayed byte-perfect. Two bug-bash arms found it
independently.

The fix is a codec pair used at both ends of the pipeline (decode_exact /
encode_exact, surrogateescape) plus two contract decisions this file pins:
an exact answer skips the DISPLAY sanitizer but not redaction, and it gets
no trailing newline.
"""

from __future__ import annotations

import pytest
from conftest import make_store, make_ws


PAYLOAD = bytes(range(256))


def _blob(store, data: bytes) -> str:
    return store.put_blob(data)


# ------------------------------------------------------------ the codec pair
def test_codec_round_trips_arbitrary_bytes():
    from ctx.textutil import decode_exact, encode_exact

    for raw in (PAYLOAD, b"", b"\xff\xfe\x80", b"plain ascii", b"\xc3\xa9 utf8"):
        assert encode_exact(decode_exact(raw)) == raw


def test_encode_exact_matches_plain_utf8_for_ordinary_text():
    from ctx.textutil import encode_exact

    for text in ("hello", "héllo", "日本語", ""):
        assert encode_exact(text) == text.encode("utf-8")


# --------------------------------------------------- the retrieval contract
def test_bytes_selector_returns_the_exact_slice(state_home, workspace_dir):
    from ctx.retrieval import Selector, get
    from ctx.textutil import encode_exact

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    ref = f"blob:{_blob(store, PAYLOAD)[:12]}"

    out = get(store, ws, ref, Selector(bytes=(1, 256)))
    body = out.split("\n", 2)[2]
    assert encode_exact(body) == PAYLOAD, "every byte, unchanged"
    assert len(encode_exact(body)) == 256, "and the same LENGTH -- U+FFFD is 3 bytes"


def test_bytes_selector_is_exact_for_a_sub_range(state_home, workspace_dir):
    from ctx.retrieval import Selector, get
    from ctx.textutil import encode_exact

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    ref = f"blob:{_blob(store, PAYLOAD)[:12]}"
    out = get(store, ws, ref, Selector(bytes=(129, 160)))
    # A partial slice carries a continuation line after the body. The header
    # states "of N", so the exact payload is the first (b - a + 1) bytes after
    # it -- which is what makes the trailing "next:" unambiguous.
    body = encode_exact(out.split("\n", 2)[2])
    assert body[:32] == PAYLOAD[128:160]
    assert b"next: ctx get" in body[32:], "the rest of it stays addressable"


def test_control_bytes_survive_an_exact_request(state_home, workspace_dir):
    """strip_control is a display nicety everywhere else and the thing that
    makes THIS answer wrong: it silently deletes every byte below 0x20 from a
    slice the caller asked for verbatim."""
    from ctx.retrieval import Selector, get
    from ctx.textutil import encode_exact

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    raw = b"A\x00\x01\x02\x1bB"
    ref = f"blob:{_blob(store, raw)[:12]}"
    out = get(store, ws, ref, Selector(bytes=(1, len(raw))))
    assert encode_exact(out.split("\n", 2)[2]) == raw


def test_line_selectors_still_get_the_display_sanitizer(state_home, workspace_dir):
    """The opt-out is scoped to the selector that promises exactness. A
    --lines read is a model-visible rendering and keeps control stripping."""
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    raw = b"one\x1b[31mtwo\ntwo\n"
    ref = f"blob:{_blob(store, raw)[:12]}"
    out = get(store, ws, ref, Selector(lines=(1, 2)))
    assert "\x1b[31m" not in out, "ANSI must still be stripped from a rendered read"


def test_redaction_still_runs_on_an_exact_request(state_home, workspace_dir):
    """Redaction is a security control, not a presentation one. It stays on,
    and it announces itself -- so the one case where an exact answer is not
    byte-exact says so out loud."""
    from ctx.config import Redaction
    from ctx.retrieval import Selector, get

    import dataclasses

    ws = make_ws(workspace_dir)
    ws.config = dataclasses.replace(  # Config is frozen
        ws.config, redaction=Redaction(patterns=("aws-access-key",))
    )
    store = make_store(ws)
    raw = b"key=AKIAIOSFODNN7EXAMPLE\n"
    ref = f"blob:{_blob(store, raw)[:12]}"
    out = get(store, ws, ref, Selector(bytes=(1, len(raw))))
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "redaction: applied" in out, "an inexact exact answer must declare itself"


# --------------------------------------------------------- the emission tail
def test_bounded_measures_surrogate_bearing_text_without_raising():
    """bounded() is the ONE backstop every emission passes through, so an
    exact result reaches it carrying lone surrogates. A strict encode there
    turned a correct answer into a crash at the measuring step."""
    from ctx.textutil import bounded, decode_exact, encode_exact

    text = decode_exact(b"A" * 100 + PAYLOAD)
    assert bounded(text, 10_000) == text
    cut = bounded(text, 4)  # forces the truncation branch
    assert "[ctx:truncated" in cut
    encode_exact(cut)  # must not raise


def test_write_exact_does_not_corrupt_and_honours_the_newline_choice(tmp_path):
    from ctx.textutil import decode_exact, write_exact

    class _Stream:
        def __init__(self, path):
            self.buffer = path.open("wb")

        def flush(self):
            self.buffer.flush()

    p = tmp_path / "out.bin"
    st = _Stream(p)
    write_exact(decode_exact(PAYLOAD), st, newline=False)
    st.buffer.close()
    assert p.read_bytes() == PAYLOAD, "no trailing byte the caller did not ask for"

    p2 = tmp_path / "out2.bin"
    st2 = _Stream(p2)
    write_exact("hello", st2)
    st2.buffer.close()
    assert p2.read_bytes() == b"hello\n", "ordinary output keeps the convention"


def test_cli_get_bytes_round_trips_through_stdout(state_home, workspace_dir, tmp_path):
    """End to end, the way the bug bash found it: capture arbitrary bytes and
    read them back through the CLI."""
    import subprocess
    import sys

    (workspace_dir / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    env = _cli_env(tmp_path)
    src = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = src + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")

    run = subprocess.run(
        [sys.executable, "-m", "ctx", "--workspace", str(workspace_dir), "run", "--",
         sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes(range(256)))"],
        capture_output=True, env=env, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    head = run.stdout.decode("utf-8", "replace").splitlines()[0]
    rid = next(t.split(":", 1)[1] for t in head.replace("[", " ").replace("]", " ").split()
               if t.startswith("run:"))

    got = subprocess.run(
        [sys.executable, "-m", "ctx", "--workspace", str(workspace_dir), "get",
         f"run:{rid}#stdout", "--bytes", "1:256"],
        capture_output=True, env=env, timeout=120,
    )
    assert got.returncode == 0, got.stderr
    assert got.stdout.split(b"\n", 2)[2] == PAYLOAD


def _cli_env(tmp_path):
    import os

    return {**os.environ, "CTX_STATE_HOME": str(tmp_path / "state")}
