"""Chunk map (ctx.digest.chunkmap): pure, deterministic, total coverage."""

from __future__ import annotations

import base64
import random

import pytest

from ctx.digest.chunkmap import Chunk, chunk_map, flavor, line_rank, may_signal
from ctx.digest.text import _signal_rank
from ctx.textutil import index_lines

PARAMS = dict(target_cost=1024, max_cost=2048, max_lines=60, line_overhead=0)


def _doc(sections: int = 40) -> str:
    r = random.Random(3)
    out = []
    for s in range(1, sections + 1):
        out.append(f"# Section {s}")
        for sub in (1, 2):
            out.append(f"## Part {s}.{sub}")
            for _ in range(3):
                out.append(" ".join(r.choice(["alpha", "beta", "gamma"]) for _ in range(20)))
                out.append("")
        if s == 7:
            out += ["```", "# not a heading inside a fence", "```", ""]
    return "\n".join(out) + "\n"


def _assert_partition(text: str, chunks: tuple[Chunk, ...]) -> None:
    data = text.encode("utf-8")
    lines = index_lines(text)
    assert chunks[0].start_line == 1 and chunks[0].start_byte == 1
    assert chunks[-1].end_line == len(lines)
    assert chunks[-1].end_byte == len(data)
    for i, (a, b) in enumerate(zip(chunks, chunks[1:]), start=1):
        assert a.index == i and b.index == i + 1
        assert b.start_byte == a.end_byte + 1, (a, b)
        if a.split == "lines" or b.start_line != a.end_line:
            assert b.start_line == a.end_line + 1, (a, b)
    # byte ranges of whole-line chunks are exactly those lines
    offsets = [0]
    for part in text.split("\n")[: len(lines)]:
        offsets.append(offsets[-1] + len(part.encode("utf-8")) + 1)
    for c in chunks:
        if c.split == "lines":
            assert c.start_byte == offsets[c.start_line - 1] + 1
            assert c.end_byte == min(offsets[c.end_line], len(data))


def test_partition_and_determinism():
    text = _doc()
    a, b = chunk_map(text, **PARAMS), chunk_map(text, **PARAMS)
    assert a == b and len(a) > 5
    _assert_partition(text, a)
    for c in a:
        assert c.bytes <= PARAMS["max_cost"] and c.lines <= PARAMS["max_lines"]


def test_boundaries_prefer_headings_and_labels_carry_the_heading_path():
    # Each section: an h1 and two h2 parts of ~900 bytes. The h1 cut point is
    # target/2 (512), the h2 one 3/4 of target (768): every h1 and every
    # second h2 starts a chunk, and a chunk starting at an h2 is labelled
    # with the full heading path.
    para = "word " * 59 + "end\n\n"            # 300 bytes
    doc = "".join(
        f"# Section {s}\n\n" + "".join(f"## Part {s}.{p}\n\n" + para * 3 for p in (1, 2))
        for s in range(1, 6)
    ) + "```\n# not a heading inside a fence\n```\n"
    chunks = chunk_map(doc, **PARAMS)
    lines = index_lines(doc)
    labels = [c.label for c in chunks]
    assert labels[:4] == ["Section 1", "Section 1 > Part 1.2",
                          "Section 2", "Section 2 > Part 2.2"]
    for c in chunks:
        assert lines[c.start_line - 1].startswith("#"), (c, lines[c.start_line - 1])
    # the fenced "# not a heading" never becomes a label or a boundary
    assert not any("not a heading" in c.label for c in chunks)
    assert chunks[-1].label == "Section 5 > Part 5.2"


def test_signal_flag_uses_the_text_ranking():
    text = "\n".join(["plain words"] * 200 + ["Traceback (most recent call last):"]
                     + ["plain words"] * 200) + "\n"
    chunks = chunk_map(text, **PARAMS)
    flagged = [c for c in chunks if c.signal]
    assert len(flagged) == 1
    assert flagged[0].start_line <= 201 <= flagged[0].end_line
    assert flagged[0].signal == _signal_rank("Traceback (most recent call last):")


def test_one_enormous_line_becomes_byte_chunks():
    payload = base64.b64encode(random.Random(5).randbytes(600_000)).decode()
    chunks = chunk_map(payload, **PARAMS)
    assert len(chunks) > 300
    assert all(c.split == "bytes" and c.start_line == c.end_line == 1 for c in chunks)
    assert all(c.bytes <= PARAMS["max_cost"] for c in chunks)
    _assert_partition(payload, chunks)
    data = payload.encode()
    assert b"".join(data[c.start_byte - 1:c.end_byte] for c in chunks) == data


def test_byte_cuts_never_split_a_utf8_character():
    payload = "é" * 50_000 + "\n" + "short line\n"
    chunks = chunk_map(payload, **PARAMS)
    data = payload.encode("utf-8")
    for c in chunks:
        data[c.start_byte - 1:c.end_byte].decode("utf-8")  # raises if split
    _assert_partition(payload, chunks)
    assert chunks[-1].split == "lines" and chunks[-1].start_line == 2


def test_mixed_long_and_short_lines_keep_line_numbers():
    text = "head\n" + ("x" * 9000) + "\n" + "tail one\ntail two\n"
    chunks = chunk_map(text, **PARAMS)
    _assert_partition(text, chunks)
    assert chunks[0] .split == "lines" and chunks[0].end_line == 1
    assert {c.start_line for c in chunks if c.split == "bytes"} == {2}
    assert chunks[-1].start_line == 3 and chunks[-1].end_line == 4


def test_line_cap_and_crlf():
    text = "".join(f"row {i}\r\n" for i in range(500))
    chunks = chunk_map(text, **PARAMS)
    _assert_partition(text, chunks)
    assert max(c.lines for c in chunks) <= PARAMS["max_lines"]
    assert all("\r" not in c.label for c in chunks)


@pytest.mark.parametrize("text", ["", ])
def test_empty_is_empty(text):
    assert chunk_map(text, **PARAMS) == ()


def test_hundreds_of_thousands_of_lines_is_linear_enough():
    text = "".join(f"line {i} quick brown fox\n" for i in range(300_000))
    chunks = chunk_map(text, target_cost=2000, max_cost=4000, max_lines=240, line_overhead=9)
    _assert_partition(text, chunks)


def test_precheck_never_hides_a_line_the_ranking_scores():
    """may_signal is a fast necessary condition for _signal_rank > 0."""
    samples = [
        "ERROR: disk", "build failed", "Traceback (most recent call last):",
        "AssertionError: x", "HTTP/1.1 502 Bad Gateway", "status_code=503",
        "exited with code 2", "src/app.py:12:3: E501", '  File "a.py", line 3, in f',
        "--> src/main.rs:4:5", "at Foo(Bar.java:12)", "npm ERR! code ELIFECYCLE",
        "FATAL boom", "CRITICAL thing", "PERMISSION_DENIED", "quota exceeded",
        "connection refused", "OOMKilled", "panic: oops", "timed out after 3s",
        "cannot open", "unable to x", "--- FAIL: TestX", "2 failed, 3 passed",
        "Exception in thread", "gyp ERR! stack", "psql:x ERROR",
        "Back-off restarting failed container",
    ]
    for line in samples:
        if _signal_rank(line) > 0:
            assert may_signal(line), line
    for line in ["the quick brown fox", "no errors detected", "lorem ipsum"]:
        if not may_signal(line):
            assert _signal_rank(line) <= 0


def test_flavor_labels():
    assert flavor(_doc()) == "markdown"
    assert flavor('{"a": 1}') == "json"
    assert flavor('{"a": 1}\n{"a": 2}\n') == "json lines"
    assert flavor(base64.b64encode(b"x" * 900).decode()) == "base64"
    assert flavor("".join(f"2024-01-01 INFO n={i}\n" for i in range(50))) == "log"
    assert flavor("a,b,c\n1,2,3\n4,5,6\n") == "csv"
    assert flavor("just some words\n") == "text"


def test_flagged_chunk_carries_its_strongest_and_last_signal_line():
    body = ["    return f(x)"] * 50
    text = "\n".join(["Traceback (most recent call last):"] + body
                     + ["RecursionError: maximum recursion depth exceeded"]) + "\n"
    chunks = chunk_map(text, target_cost=4000, max_cost=8000, max_lines=240)
    assert len(chunks) == 1
    c = chunks[0]
    assert c.signal == 3 and c.signal_line == 52     # latest of equal rank
    assert c.last_signal_line == 52
    assert c.last_signal_text.startswith("RecursionError:")


def test_exception_class_lines_rank_only_in_the_chunk_map():
    """The widening is intake-only: the shared ranking is untouched."""
    for line in ("RecursionError: maximum recursion depth exceeded",
                 "KeyError: 'x'", "json.decoder.JSONDecodeError: Expecting value",
                 "KeyboardInterrupt", "SystemExit: 2"):
        assert line_rank(line) == 3, line
        assert may_signal(line), line
    assert _signal_rank("RecursionError: maximum recursion depth exceeded") == 0
    assert line_rank("ValueError is what this function raises") < 3
    assert line_rank("no errors detected") == -1


def test_coordinate_hint_is_a_necessary_condition():
    """_signal_rank skips _COORDINATE_RE when its hint is absent; that must
    never change a rank, so every coordinate match must carry the hint."""
    from ctx.digest.text import _COORDINATE_HINT_RE, _COORDINATE_RE

    r = random.Random(17)
    alphabet = "ab/\\.:-_ 0123456789\"(),>filenpyjavrst"
    hits = 0
    for _ in range(60_000):
        line = "".join(r.choice(alphabet) for _ in range(r.randrange(1, 40)))
        if _COORDINATE_RE.search(line):
            hits += 1
            assert _COORDINATE_HINT_RE.search(line), line
    for line in ('  file "a.py", line 3, in f', "src/a.py:12:3", "err x--> x.rs:4:5",
                 "at Foo(Bar.java:12)", "pkg/x.go:7"):
        assert _COORDINATE_RE.search(line), line
        assert _COORDINATE_HINT_RE.search(line), line
    assert hits > 100


def test_signal_rank_is_fast_on_a_long_slash_line():
    import time

    line = "/".join(["ab"] * 66_666)            # 200 KB of word boundaries
    t = time.perf_counter()
    assert _signal_rank(line) == 0
    assert time.perf_counter() - t < 2.0
