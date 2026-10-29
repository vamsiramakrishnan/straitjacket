"""Acceptance: lossless epoch-latched rescue (Tier-1, opt-in).

The properties that distinguish this from a rewriting proxy's compression:
determinism (same transcript → byte-identical rewrite), prefix stability
(a grown transcript rewrites to an identical prefix — the cache is re-bought
once, not per request), preservation-before-elision (every elided byte is on
disk before the stub exists), and the epoch latch (elision sets only grow,
frozen at threshold crossings)."""

import json
import time

import pytest


def _wait_until(predicate, timeout=5.0, interval=0.02):
    """Poll ``predicate`` until true or the timeout elapses; returns its
    last value. Used for state a streaming proxy writes AFTER the client
    response returns — the relay delivers the body first and records wire
    telemetry after (correct production ordering), so a test reading
    wire.jsonl the instant ``post()`` returns races that trailing write."""
    deadline = time.monotonic() + timeout
    val = predicate()
    while not val and time.monotonic() < deadline:
        time.sleep(interval)
        val = predicate()
    return val


def _tool_result(text, i):
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}", "content": text}
        ],
    }


def _transcript(n_results, big_bytes=3000):
    msgs = [{"role": "user", "content": "task"}]
    for i in range(n_results):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]})
        msgs.append(_tool_result(f"result-{i}-" + "x" * big_bytes, i))
    return msgs


def test_plan_epoch_selects_old_large_only():
    from ctx.rescue import plan_epoch

    msgs = _transcript(10)
    msgs.append(_tool_result("tiny", 99))  # small: never selected
    plan = plan_epoch(msgs, keep_recent=4, min_block_bytes=1024)
    # 11 tool_results; the last 4 are protected; of the first 7, all big ones.
    assert plan == [0, 1, 2, 3, 4, 5, 6]
    assert plan_epoch(msgs, keep_recent=4, min_block_bytes=1024) == plan  # pure


def test_apply_elision_preserves_bytes_and_is_deterministic(tmp_path):
    from ctx.rescue import apply_elision

    msgs = _transcript(6)
    out1, n1 = apply_elision(msgs, {0, 1}, tmp_path)
    out2, n2 = apply_elision(msgs, {0, 1}, tmp_path)
    assert n1 == n2 == 2
    assert json.dumps(out1) == json.dumps(out2)  # byte-identical rewrite
    # Every elided block's exact bytes are on disk, stub cites hash + path.
    elided = list((tmp_path / "elided").glob("*.txt"))
    assert len(elided) == 2
    originals = {p.read_text() for p in elided}
    assert any(t.startswith("result-0-") for t in originals)
    stub = out1[2]["content"][0]["content"]
    assert "ctx rescue: tool_result elided" in stub
    assert "sha256:" in stub and "/elided/" in stub
    # PR-review regression: the stub's path must RESOLVE — nonstandard state
    # dirs get the absolute path, never a bare basename.
    assert str(tmp_path) in stub


def test_stub_paths_resolve_from_workspace_cwd(tmp_path):
    """Under the standard wrap layout (<ws>/.ctx-session-reads/proxy) the
    stub cites the workspace-relative path an agent can actually read."""
    from ctx.rescue import apply_elision

    state = tmp_path / ".ctx-session-reads" / "proxy"
    msgs = _transcript(3)
    out, n = apply_elision(msgs, {0}, state)
    assert n == 1
    stub = out[2]["content"][0]["content"]
    assert ".ctx-session-reads/proxy/elided/" in stub
    # And the cited file exists exactly where the stub says, relative to ws.
    rel = stub.split("preserved verbatim at ")[1].split(";")[0]
    assert (tmp_path / rel).is_file()


def test_prefix_stability_as_transcript_grows(tmp_path):
    """The cache property: with a frozen elision set, a longer transcript
    rewrites to a byte-identical prefix for the shared messages."""
    from ctx.rescue import apply_elision

    t1 = _transcript(6)
    t2 = _transcript(9)  # same first 13 messages + 6 more
    out1, _ = apply_elision(t1, {0, 1, 2}, tmp_path)
    out2, _ = apply_elision(t2, {0, 1, 2}, tmp_path)
    assert json.dumps(out2[: len(out1)]) == json.dumps(out1)


def test_rescue_state_latches_epochs(tmp_path):
    from ctx.rescue import RescueState

    rs = RescueState(tmp_path, threshold_pct=70, keep_recent=2, min_block_bytes=1024)
    body = json.dumps({"messages": _transcript(5)}).encode()

    # Below threshold: byte-exact passthrough, no epoch.
    out, n = rs.maybe_rescue(body, 30.0)
    assert out == body and n == 0

    # Crossing: epoch freezes {0,1,2} (5 results, keep 2).
    out, n = rs.maybe_rescue(body, 71.0)
    assert n == 3
    doc = json.loads(out)
    assert "ctx rescue" in json.dumps(doc["messages"][2])

    # Pressure recedes, transcript grows: the frozen set STILL applies
    # (latched), and the newer blocks are NOT elided by the old epoch.
    grown = json.dumps({"messages": _transcript(8)}).encode()
    out2, n2 = rs.maybe_rescue(grown, 20.0)
    assert n2 == 3  # same frozen set, deterministically re-applied
    # Second crossing opens a second epoch covering the newly-old blocks.
    out3, n3 = rs.maybe_rescue(grown, 75.0)
    assert n3 == 6  # 8 results, keep_recent=2 → ordinals 0..5

    # State survives a restart (persisted epochs).
    rs2 = RescueState(tmp_path, threshold_pct=70, keep_recent=2, min_block_bytes=1024)
    out4, n4 = rs2.maybe_rescue(grown, 10.0)
    assert n4 == 6
    assert json.loads(out4) == json.loads(out3)


def test_malformed_or_unknown_bodies_pass_through(tmp_path):
    from ctx.rescue import RescueState

    rs = RescueState(tmp_path, threshold_pct=50)
    for raw in (b"not json{{{", b"[]", json.dumps({"messages": "nope"}).encode()):
        out, n = rs.maybe_rescue(raw, 99.0)
        assert out == raw and n == 0


# --------------------------------------------------------- proxy integration
def test_proxy_rescue_end_to_end(tmp_path):
    import http.client
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from ctx.proxy import _make_server

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.server.captured.append(body)
            # Report enormous input usage → window_pct crosses any threshold.
            resp = json.dumps({
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 150_000, "output_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    up.captured = []
    threading.Thread(target=up.serve_forever, daemon=True).start()
    state = tmp_path / "state"
    srv = _make_server(
        0, f"http://127.0.0.1:{up.server_address[1]}", state, "ws", rescue_pct=70.0
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"model": "claude-sonnet-5",
                           "messages": _transcript(10)}).encode()

        def post(payload):
            conn = http.client.HTTPConnection(
                "127.0.0.1", srv.server_address[1], timeout=10
            )
            conn.request("POST", "/v1/messages", body=payload,
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(len(payload))})
            resp = conn.getresponse()
            resp.read()
            conn.close()

        post(body)  # no window knowledge yet → forwarded byte-exact
        assert up.captured[0] == body

        # The proxy records window pressure AFTER relaying the first
        # response (deliver body first, bookkeep after — correct streaming
        # ordering), and the rescue tier reads that recorded pct. So the
        # second request must not fire until the first request's window
        # observation has actually landed, else no rescue triggers. Wait
        # for it rather than assuming it raced in — the flake this fixes.
        def _window_pressured():
            wf = state / "window.json"
            if not wf.is_file():
                return False
            try:
                return float(json.loads(wf.read_text()).get("window_pct", 0)) >= 70.0
            except (json.JSONDecodeError, ValueError, AttributeError):
                return False

        assert _wait_until(_window_pressured), "window pressure never recorded"

        post(body)  # window now known at 75% → epoch fires
        forwarded = json.loads(up.captured[1])
        stubs = sum(
            1 for m in forwarded["messages"] if isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "tool_result" and "ctx rescue" in str(b.get("content"))
        )
        assert stubs > 0
        assert len(up.captured[1]) < len(body)  # transcript actually shrank
        # Elided bytes are on disk; wire discloses the rescue.
        assert list((state / "elided").glob("*.txt"))

        # wire.jsonl is written AFTER the response is relayed to the client
        # (the proxy delivers the body first, records telemetry after), so
        # poll for the trailing write rather than assuming it landed by the
        # time post() returned — the flake this replaces (race, not a bug).
        def _rescued_on_wire():
            wf = state / "wire.jsonl"
            if not wf.is_file():
                return False
            rescued = 0
            for line in wf.read_text().splitlines():
                try:
                    rescued += int(json.loads(line).get("rescued", 0) or 0)
                except (json.JSONDecodeError, ValueError, AttributeError):
                    continue  # a concurrently-appended partial line: skip
            return rescued > 0

        assert _wait_until(_rescued_on_wire), "wire.jsonl never disclosed a rescue"
    finally:
        srv.shutdown(); srv.server_close(); up.shutdown(); up.server_close()


# ------------------------------------------------------ the stub's first line
def test_stub_carries_the_first_line_of_what_it_replaced(tmp_path):
    """Headlong keeps a one-line tldr on every summarized entry so its
    trajectory stays a readable index. Our stub carried bytes and a hash
    only, so a rescued transcript was a column of identical placeholders.
    The first line of a harnessed tool_result is its digest header; it is a
    pure function of the elided bytes and rides on the stub."""
    from ctx.rescue import HEAD_LINE_MAX, apply_elision, head_line, stub_for

    body = "pytest: 37 failed, 412 passed [run:ba3d1020ee8f]\n" + "x" * 2000
    msgs = [_tool_result(body, 0), _tool_result("y" * 2000, 1)]
    out, n = apply_elision(msgs, {0}, tmp_path)
    assert n == 1
    stub = out[0]["content"][0]["content"]
    assert 'first line: "pytest: 37 failed, 412 passed [run:ba3d1020ee8f]"' in stub
    assert "sha256:" in stub and "/elided/" in stub  # nothing lost from before
    assert "\n" not in stub

    # Bounded, single-line, deterministic whatever the block held.
    noisy = "\n\n  \x1b[31mERROR\x1b[0m\t\tline\x00one   here\nsecond line\n"
    assert head_line(noisy) == "[31mERROR [0m line one here"
    long = "w" * (HEAD_LINE_MAX * 3)
    assert len(head_line(long)) == HEAD_LINE_MAX and head_line(long).endswith("…")
    assert head_line("\n \n") == ""
    assert "first line:" not in stub_for("\n \n" + " " * 1100, "ab" * 32, "p")
    assert stub_for(noisy, "cd" * 32, "p") == stub_for(noisy, "cd" * 32, "p")
