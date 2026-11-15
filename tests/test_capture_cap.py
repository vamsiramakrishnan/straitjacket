"""The hard cap on what one captured command stores (ctx.capture).

A command that never stops printing used to be spooled to disk without
limit. These pin the rule: at most ``[store] max_capture_bytes`` is stored
per command (half per stream: head and tail, middle dropped and counted),
output is drained past the cap until the command ends or passes 16 x the cap
(then its process group is killed), counts stay exact, and every retrieval
verb speaks the original stream's line numbers and refuses the dropped
middle. Under the cap nothing changes.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from conftest import make_store, make_ws

CAP = 64 * 1024  # per stream: 32 KiB head + 32 KiB tail


def _ws(workspace_dir: Path, cap: int = CAP):
    (workspace_dir / "ctx.toml").write_text(
        f"version = 1\n[store]\nmax_capture_bytes = {cap}\n", encoding="utf-8")
    return make_ws(workspace_dir)


def _run(ws, store, argv, **kw):
    from ctx.execution import run_capture

    return run_capture(ws, argv, store=store, **kw)


def _blob(store, meta) -> bytes:
    return store.get_blob(meta["blob"].removeprefix("sha256:"))


def _dir_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


# ------------------------------------------------------------- the spool
def _spool(tmp_path, cap, data_chunks):
    from ctx.capture import CappedSpool

    sp = CappedSpool(tmp_path / "out", cap)
    for c in data_chunks:
        sp.write(c)
    block = sp.finish()
    return block, (tmp_path / "out").read_bytes()


def test_spool_under_cap_is_exact_and_unmarked(tmp_path):
    data = b"".join(b"line %d\n" % i for i in range(2000))  # ~17 KiB, cap 32 KiB
    block, stored = _spool(tmp_path, 32 * 1024, [data[i:i + 777] for i in range(0, len(data), 777)])
    assert block is None and stored == data
    assert not (tmp_path / "out.tail").exists()


def test_spool_over_cap_keeps_head_and_tail_with_exact_counts(tmp_path):
    lines = [b"row %06d\n" % i for i in range(50_000)]
    data = b"".join(lines)
    block, stored = _spool(tmp_path, 32 * 1024, [data[i:i + 4093] for i in range(0, len(data), 4093)])
    assert block["capped"] and block["totalBytes"] == len(data)
    assert block["totalLines"] == 50_000
    assert len(stored) == block["keptBytes"] <= 32 * 1024
    h, t = block["headBytes"], block["tailBytes"]
    assert stored == data[:h] + data[-t:]
    assert block["droppedBytes"] == len(data) - h - t
    # Cut on line boundaries: the stored lines are whole original lines.
    assert data[:h].endswith(b"\n") and data[len(data) - t - 1:len(data) - t] == b"\n"
    assert block["headLines"] == data[:h].count(b"\n")
    assert block["tailFirstLine"] == data[:len(data) - t].count(b"\n") + 1
    assert block["droppedLines"] == block["tailFirstLine"] - block["headLines"] - 1
    assert not (tmp_path / "out.tail").exists()


def test_spool_without_newlines_and_binary(tmp_path):
    data = os.urandom(200_000).replace(b"\n", b"x")
    block, stored = _spool(tmp_path, 8192, [data[i:i + 5000] for i in range(0, len(data), 5000)])
    assert block["totalBytes"] == len(data) and block["totalLines"] == 1
    assert block["lineBreakAdded"] is True
    assert stored == data[:4096] + b"\n" + data[-4096:]
    from ctx.capture import CapMap

    cm = CapMap(block)
    assert cm.orig_byte_to_blob(1) == 1 and cm.orig_byte_to_blob(4097) is None
    assert cm.orig_byte_to_blob(len(data)) == len(stored)


# --------------------------------------------------- the running command
FOREVER = "import sys\nwhile True:\n    sys.stdout.write('y' * 99 + '\\n')\n"

#: Numbered 100-byte lines written forever in 4,093-byte pieces, so the kill
#: lands inside a line however the run is timed. Each write is below
#: PIPE_BUF, so whole pieces are drained; the test never assumes where.
NUMBERED_FOREVER = (
    "import os\nbuf = b''\ni = 0\n"
    "while True:\n"
    "    while len(buf) < 4093:\n"
    "        buf += b'%09d' % i + b'y' * 90 + b'\\n'\n"
    "        i += 1\n"
    "    os.write(1, buf[:4093])\n"
    "    buf = buf[4093:]\n")


def _numbered_stream(n_bytes: int) -> bytes:
    lines = n_bytes // 100 + 1
    return b"".join(b"%09d" % i + b"y" * 90 + b"\n" for i in range(lines))[:n_bytes]


def test_runaway_command_is_stopped_and_disk_stays_bounded(state_home, workspace_dir):
    ws = _ws(workspace_dir)
    store = make_store(ws)
    start = time.monotonic()
    cap = _run(ws, store, [sys.executable, "-c", NUMBERED_FOREVER], timeout=120)
    assert time.monotonic() - start < 60  # stopped by the runaway limit, not the timeout
    m = cap.manifest
    assert m["result"]["outputLimited"] is True and m["result"]["signal"] == "SIGKILL"
    assert m["result"]["timedOut"] is False
    out = m["streams"]["stdout"]
    block = out["capture"]
    total = block["totalBytes"]
    assert total >= 16 * CAP  # drained, and counted, up to the limit
    # Exact against ground truth, wherever the kill landed: the drained
    # stream is the first `total` bytes of the numbered stream.
    drained = _numbered_stream(total)
    head, tail = block["headBytes"], block["tailBytes"]
    assert head + block["droppedBytes"] + tail == total
    assert block["keptBytes"] == head + tail
    stored = _blob(store, out)
    added = b"\n" if block["lineBreakAdded"] else b""
    assert stored == drained[:head] + added + drained[total - tail:]  # the tail IS the end
    assert block["totalLines"] == drained.count(b"\n") + (0 if drained.endswith(b"\n") else 1)
    assert block["headLines"] == drained[:head].count(b"\n") + (0 if not head or drained[:head].endswith(b"\n") else 1)
    assert block["tailFirstLine"] == drained[:total - tail].count(b"\n") + 1
    assert out["bytes"] <= CAP + 1
    # Everything the store holds for this run: the cap plus manifests/index.
    assert _dir_bytes(state_home) <= CAP + 256 * 1024


def test_a_stream_cut_mid_line_by_the_kill_is_counted_exactly(state_home, workspace_dir):
    # The counts for a stream that ends inside a line: the partial last line
    # is a line (docs: "lines in all"), its bytes are all counted, and the
    # stored tail ends with exactly those bytes.
    ws = _ws(workspace_dir)
    store = make_store(ws)
    script = "import os\nos.write(1, b'x' * 99 + b'\\n')\nos.write(1, (b'y' * 99 + b'\\n') * 2000 + b'tail-no-nl')"
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    block = m["streams"]["stdout"]["capture"]
    total = 100 + 200000 + len(b"tail-no-nl")
    assert block["totalBytes"] == total and block["totalLines"] == 2002
    assert block["headBytes"] + block["droppedBytes"] + block["tailBytes"] == total
    assert _blob(store, m["streams"]["stdout"]).endswith(b"y\ntail-no-nl")


def test_large_output_under_the_runaway_limit_is_drained_not_killed(state_home, workspace_dir):
    ws = _ws(workspace_dir)
    store = make_store(ws)
    script = ("import sys\nfor i in range(1, 30001): sys.stdout.write(f'{i:07d} ok\\n')\n"
              "sys.stderr.write('done\\n'); sys.exit(4)")
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    assert m["result"]["exitCode"] == 4 and not m["result"].get("outputLimited")
    block = m["streams"]["stdout"]["capture"]
    assert block["totalBytes"] == 30000 * 11 and block["totalLines"] == 30000
    assert "capture" not in m["streams"]["stderr"]
    assert _blob(store, m["streams"]["stderr"]) == b"done\n"


def test_stderr_and_no_newline_output_respect_the_cap(state_home, workspace_dir):
    ws = _ws(workspace_dir)
    store = make_store(ws)
    script = "import sys\nsys.stderr.write('e' * 500000)\nsys.stdout.buffer.write(bytes(300000))"
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    for name, total in (("stderr", 500000), ("stdout", 300000)):
        meta = m["streams"][name]
        assert meta["capture"]["totalBytes"] == total and meta["capture"]["totalLines"] == 1
        assert meta["bytes"] <= CAP + 1  # + the line break that ends the cut head


def test_small_command_is_unchanged(state_home, workspace_dir):
    ws = make_ws(workspace_dir)  # default cap
    store = make_store(ws)
    script = "import sys; print('alpha'); sys.stderr.write('beta\\n')"
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    assert set(m["result"]) == {"exitCode", "signal", "timedOut"}
    for name, want in (("stdout", b"alpha\n"), ("stderr", b"beta\n")):
        meta = m["streams"][name]
        assert set(meta) == {"blob", "bytes", "lines", "mediaType", "encoding"}
        assert _blob(store, meta) == want


def test_background_job_is_capped_too(state_home, workspace_dir):
    from ctx.jobs import finalize_job, start_job, wait_for_done

    ws = _ws(workspace_dir)
    store = make_store(ws)
    job = start_job(ws, store, [sys.executable, "-c", FOREVER], timeout=120)
    assert wait_for_done(store, job, timeout=90)
    digest, m = finalize_job(ws, store, job)
    assert m["result"]["outputLimited"] is True
    assert m["streams"]["stdout"]["bytes"] <= CAP
    assert "profile=capped/v1" in digest.splitlines()[0]


# ------------------------------------------------- digest and retrieval
SCRIPT = "import sys\nfor i in range(1, 20001): sys.stdout.write(f'line {i:05d} value\\n')\n"
LINE = len("line 00001 value\n")


@pytest.fixture()
def capped(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = _ws(workspace_dir)
    store = make_store(ws)
    m = _run(ws, store, [sys.executable, "-c", SCRIPT]).manifest
    digest, final = render_run_digest(store, ws, m)
    from ctx.textutil import short_id

    return ws, store, digest, final, short_id(final["id"])


def test_digest_states_exact_counts(capped):
    ws, store, digest, m, short = capped
    block = m["streams"]["stdout"]["capture"]
    head_n = (32 * 1024) // LINE
    tail_n = (32 * 1024) // LINE
    assert block["headLines"] == head_n and block["tailLines"] == tail_n
    assert f"profile=capped/v1" in digest
    total = 20000 * LINE
    kept = (head_n + tail_n) * LINE
    assert f"captured {kept:,} of {total:,} bytes" in digest
    assert "20,000 lines in all" in digest
    first_tail = 20000 - tail_n + 1
    assert f"kept: L1-L{head_n} and L{first_tail}-L20000" in digest
    assert f"not stored: {total - kept:,} bytes, L{head_n + 1}-L{first_tail - 1}" in digest
    assert "L1: line 00001 value" in digest
    assert "L20000: line 20000 value" in digest
    # Nothing in the digest claims a line of the dropped middle.
    for n in range(head_n + 1, first_tail):
        assert f"L{n}: " not in digest


def test_get_reads_head_and_tail_by_original_line_numbers(capped):
    from ctx.retrieval import RetrievalError, Selector, get

    ws, store, _digest, m, short = capped
    block = m["streams"]["stdout"]["capture"]
    tail_first = block["tailFirstLine"]
    out = get(store, ws, f"run:{short}#stdout", Selector(lines=(19998, 20000)))
    assert "L19998: line 19998 value" in out and "L20000: line 20000 value" in out
    assert "capture: capped" in out
    out = get(store, ws, f"run:{short}#stdout", Selector(lines=(10, 12)))
    assert "L10: line 00010 value" in out and "L12: line 00012 value" in out
    with pytest.raises(RetrievalError, match="dropped middle"):
        get(store, ws, f"run:{short}#stdout", Selector(lines=(5000, 5010)))
    # A range that starts in the middle is answered from the tail, and says so.
    out = get(store, ws, f"run:{short}#stdout", Selector(lines=(10000, tail_first + 1)))
    assert f"lines 10000:{tail_first - 1} were not stored" in out
    assert f"L{tail_first}: line {tail_first:05d} value" in out
    # Bytes use the original offsets too.
    start = (19999 - 1) * LINE + 1
    out = get(store, ws, f"run:{short}#stdout", Selector(bytes=(start, start + LINE - 2)))
    assert "line 19999 value" in out
    with pytest.raises(RetrievalError, match="dropped middle"):
        get(store, ws, f"run:{short}#stdout", Selector(bytes=(100_000, 100_010)))
    with pytest.raises(RetrievalError, match="capped capture"):
        get(store, ws, f"run:{short}#stdout", Selector(records=(1, 2)))


def test_search_numbers_hits_in_the_original_stream(capped):
    from ctx.retrieval import search

    ws, store, _digest, m, short = capped
    out = search(store, ws, f"run:{short}", ["line 19999", "line 00002 "], context=1)
    assert "L19999: line 19999 value" in out and "L2: line 00002 value" in out
    assert "capped:" in out and "not stored or searched" in out
    out = search(store, ws, f"run:{short}", ["line 10000"])
    assert "matches: 0" in out and "not stored or searched" in out


def test_diff_refuses_a_capped_capture(capped):
    from ctx.retrieval import RetrievalError
    from ctx.rundiff import run_diff

    ws, store, _digest, m, short = capped
    with pytest.raises(RetrievalError, match="capped capture"):
        run_diff(store, ws, f"run:{short}", f"run:{short}")


# ------------------------------------------------------- review round 1
def test_one_stream_may_use_the_whole_cap(state_home, workspace_dir):
    # The cap is per stream: a command writing only stdout keeps up to all
    # of it (it used to be cut at half the cap with stderr empty).
    ws = _ws(workspace_dir)
    store = make_store(ws)
    data_len = CAP - 1000
    script = f"import sys; sys.stdout.write('x' * {data_len - 1} + '\\n')"
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    assert "capture" not in m["streams"]["stdout"]
    assert m["streams"]["stdout"]["bytes"] == data_len


def test_a_line_cut_by_the_cap_says_where_its_rest_is(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.retrieval import Selector, get
    from ctx.textutil import short_id

    ws = _ws(workspace_dir)
    store = make_store(ws)
    script = "import sys; sys.stdout.write('a' * 300000)"
    m = _run(ws, store, [sys.executable, "-c", script]).manifest
    _digest, final = render_run_digest(store, ws, m)
    short = short_id(final["id"])
    block = final["streams"]["stdout"]["capture"]
    assert block["headLines"] == 1 and block["tailFirstLine"] == 1
    out = get(store, ws, f"run:{short}#stdout", Selector(lines=(1, 1)))
    assert "L1 was cut by the capture" in out
    assert f"--bytes {block['tailStartByte']}:300000" in out


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_a_background_writer_is_drained_not_waited_for(state_home, workspace_dir, tmp_path):
    # A descendant that keeps the output open (`server &`) does not hold the
    # capture: after the grace period its pipe goes to a detached drain, so
    # it keeps running (and is never killed by a closed pipe).
    import signal

    ws = _ws(workspace_dir)
    store = make_store(ws)
    pidfile = tmp_path / "bg.pid"
    script = (f"(while :; do echo tick || exit 1; sleep 0.05; done) & echo $! > {pidfile}; "
              "echo started")
    start = time.monotonic()
    m = _run(ws, store, [script], shell=True, timeout=60).manifest
    took = time.monotonic() - start
    pid = int(pidfile.read_text().strip())
    try:
        assert took < 20, took
        assert m["result"]["exitCode"] == 0
        assert _blob(store, m["streams"]["stdout"]).startswith(b"started\n") or \
            b"started\n" in _blob(store, m["streams"]["stdout"])
        time.sleep(0.5)
        # still running (not a zombie): its writes went to the drain, not a
        # closed pipe
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        assert state != "Z", state
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
