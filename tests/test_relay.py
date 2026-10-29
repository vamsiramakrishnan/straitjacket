"""Acceptance: the cross-harness relay (`ctx relay`, hook delivery, jobs).

The contract under test, in the order it matters:

1. **Addresses only.** A signal carries a ref the receiver resolves and a
   bounded note. Prose, output, or an unbounded string is refused at the
   boundary — the relay must never become a way to push content into another
   harness's prompt.
2. **Delivery at a boundary, exactly once.** A queued signal reaches the
   subscriber at its next hook stage and does not come back on the one after.
3. **Interrupts stop a tool call, not a stream.** A pending interrupt turns
   the next PreToolUse into a force_ask naming the address — and never blocks
   the `ctx` call that would resolve it, which would deadlock the agent
   against the message.
4. **Advisory, never fatal.** A corrupt or unwritable queue degrades to
   silence. The relay is an addition to the loop and is not allowed to become
   a new way for the loop to break.
5. **Nobody polls.** A backgrounded job whose completion someone subscribed to
   announces itself; the agent learns at its next tool result.
"""

import json
import subprocess
import time

import pytest

from ctx import relay


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    (d / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    return d


# ------------------------------------------------------- 1. addresses only


def test_a_signal_must_carry_an_address_not_prose(ws):
    with pytest.raises(relay.RelayError):
        relay.publish(ws, topic="job", ref="the tests failed", origin="codex", to="claude")
    with pytest.raises(relay.RelayError):
        relay.interrupt(ws, to="claude", ref="stop doing that", origin="codex")


def test_a_note_is_bounded_and_flattened_to_one_line(ws):
    from ctx.taskledger import INBOX_NOTE_CHARS

    row = relay.publish(
        ws, topic="job", ref="run:8f2c1d3e4a5b#stdout", origin="codex", to="claude",
        note="line one\nline two\x07  padded   " + "x" * 400,
    )[0]
    assert "\n" not in row["note"] and "\x07" not in row["note"]
    assert len(row["note"]) <= INBOX_NOTE_CHARS


def test_an_unknown_kind_or_topic_is_refused(ws):
    with pytest.raises(relay.RelayError):
        relay.publish(ws, topic="gossip", ref="repo:a.py", origin="x", to="claude")
    with pytest.raises(relay.RelayError):
        relay.watch(ws, subscriber="claude", topic="job", action="shout")


# --------------------------------------------- 2. delivery, exactly once


def test_publish_fans_out_only_to_matching_watches(ws):
    relay.watch(ws, subscriber="claude", topic="job")
    relay.watch(ws, subscriber="codex", topic="edit")
    queued = relay.publish(
        ws, topic="job", selector="job-1", ref="run:8f2c1d3e4a5b#stdout", origin="ctx-job"
    )
    assert [q["to"] for q in queued] == ["claude"]


def test_a_selector_narrows_by_prefix(ws):
    relay.watch(ws, subscriber="claude", topic="job", selector="job-aa")
    assert relay.publish(ws, topic="job", selector="job-bb", ref="repo:a.py", origin="o") == []
    assert relay.publish(ws, topic="job", selector="job-aa1", ref="repo:a.py", origin="o")


def test_the_subscriber_chooses_the_kind_not_the_publisher(ws):
    """One completion is a report to one harness and a stop to another."""
    relay.watch(ws, subscriber="claude", topic="job", action="report")
    relay.watch(ws, subscriber="codex", topic="job", action="interrupt")
    kinds = {q["to"]: q["kind"] for q in relay.publish(
        ws, topic="job", ref="run:8f2c1d3e4a5b#stdout", origin="ctx-job"
    )}
    assert kinds == {"claude": "report", "codex": "interrupt"}


def test_a_signal_is_delivered_once_and_not_again(ws):
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="claude")
    text, signals = relay.drain(ws, "claude:s1", stage="post-tool-use")
    assert len(signals) == 1 and "repo:a.py" in text
    assert relay.drain(ws, "claude:s1", stage="post-tool-use") == ("", [])


def test_a_host_signal_reaches_any_session_of_that_host(ws):
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="claude")
    assert relay.drain(ws, "claude:whichever", stage="post-tool-use")[1]


def test_a_session_signal_does_not_reach_a_sibling_session(ws):
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="claude:s1")
    assert relay.drain(ws, "claude:s2", stage="post-tool-use") == ("", [])
    assert relay.drain(ws, "claude:s1", stage="post-tool-use")[1]


def test_post_tool_use_never_delivers_an_interrupt(ws):
    """That stage cannot stop anything; an interrupt shown there is demoted."""
    relay.interrupt(ws, to="claude", ref="repo:a.py", origin="codex")
    assert relay.drain(ws, "claude:s1", stage="post-tool-use") == ("", [])
    assert relay.drain(ws, "claude:s1", stage="pre-tool-use")[1]


def test_an_expired_signal_is_never_delivered(ws):
    relay.publish(
        ws, topic="peer", ref="repo:a.py", origin="codex", to="claude", ttl_seconds=1
    )
    # ttl is floored at 60s by publish, so expire it by asking about later.
    assert relay.pending(ws, "claude", now=time.time() + 3600) == []


def test_a_subscriber_that_never_drains_cannot_grow_the_queue(ws):
    relay.watch(ws, subscriber="claude", topic="job")
    for _ in range(relay.MAX_PENDING_PER_SUBSCRIBER + 10):
        relay.publish(ws, topic="job", ref="repo:a.py", origin="o")
    assert len(relay.pending(ws, "claude")) <= relay.MAX_PENDING_PER_SUBSCRIBER


def test_render_is_bounded(ws):
    for i in range(20):
        relay.publish(
            ws, topic="peer", ref="repo:a.py", origin="codex", to="claude",
            note="n" * 200,
        )
    text = relay.render(relay.pending(ws, "claude"))
    assert len(text) <= relay.MAX_RENDER_CHARS


def test_unwatch_stops_the_fan_out(ws):
    row = relay.watch(ws, subscriber="claude", topic="job")
    assert relay.unwatch(ws, row["watch_id"]) is True
    assert relay.publish(ws, topic="job", ref="repo:a.py", origin="o") == []


def test_gc_drops_settled_rows_but_keeps_pending_ones(ws):
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="claude")
    relay.drain(ws, "claude", stage="cli")
    relay.publish(ws, topic="peer", ref="repo:b.py", origin="codex", to="claude")
    before = len(relay.pending(ws, "claude"))
    relay.gc(ws, keep_seconds=-1)
    assert len(relay.pending(ws, "claude")) == before == 1


# --------------------------------------------------- 3. interrupt semantics


def _hook(ws, stage, payload, flavor="claude-code"):
    r = subprocess.run(
        ["ctx", "hook", flavor, stage],
        input=json.dumps(payload), capture_output=True, text=True, cwd=str(ws), timeout=60,
    )
    return json.loads(r.stdout or "{}")


def _payload(ws, command="ls", **extra):
    return {
        "session_id": "s1", "cwd": str(ws), "tool_name": "Bash",
        "tool_input": {"command": command}, **extra,
    }


def test_a_pending_interrupt_stops_the_next_tool_call(ws):
    relay.interrupt(
        ws, to="claude", ref="checkpoint:d914ee702801", origin="codex",
        note="the schema changed under you",
    )
    out = _hook(ws, "pre-tool-use", _payload(ws))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "CTX_RELAY_INTERRUPT" in hso["permissionDecisionReason"]
    assert "checkpoint:d914ee702801" in hso["permissionDecisionReason"]


def test_an_interrupt_never_blocks_the_ctx_call_that_resolves_it(ws):
    relay.interrupt(ws, to="claude", ref="checkpoint:d914ee702801", origin="codex")
    out = _hook(ws, "pre-tool-use", _payload(ws, "ctx get checkpoint:d914ee702801"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_delivered_interrupt_does_not_stop_every_later_call(ws):
    relay.interrupt(ws, to="claude", ref="repo:a.py", origin="codex")
    assert _hook(ws, "pre-tool-use", _payload(ws))["hookSpecificOutput"][
        "permissionDecision"
    ] == "ask"
    later = _hook(ws, "pre-tool-use", _payload(ws))
    assert later.get("hookSpecificOutput", {}).get("permissionDecision") != "ask"


def test_a_report_reaches_the_agent_at_the_next_tool_result(ws):
    relay.publish(
        ws, topic="job", selector="job-1", ref="run:8f2c1d3e4a5b#stdout",
        origin="ctx-job", to="claude", note="pytest: 3 failed",
    )
    out = _hook(ws, "post-tool-use", _payload(ws, tool_response="ok"))
    ctxt = out["hookSpecificOutput"]["additionalContext"]
    assert "run:8f2c1d3e4a5b#stdout" in ctxt and "pytest: 3 failed" in ctxt


def test_antigravity_receives_the_relay_through_pre_invocation(ws):
    """Its PostToolUse contract has exactly one legal output, so this stage
    is that host's only delivery point."""
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="hermes", to="antigravity")
    out = _hook(ws, "pre-invocation", {"cwd": str(ws), "session_id": "s1"}, "antigravity")
    assert "repo:a.py" in out["injectSteps"][0]["ephemeralMessage"]


# ------------------------------------------------------ 4. never fatal


def test_a_corrupt_queue_degrades_to_silence_not_a_failed_tool_call(ws):
    path = relay.relay_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all\n\x00\x00\n", encoding="utf-8")
    out = _hook(ws, "pre-tool-use", _payload(ws))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert relay.drain_quietly(ws, "claude", stage="post-tool-use") == ("", [])


def test_a_torn_write_does_not_poison_the_queue(ws):
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="claude")
    path = relay.relay_path(ws)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"schema": "ctx.signal/v1", "sig')  # killed mid-write
    relay.publish(ws, topic="peer", ref="repo:b.py", origin="codex", to="claude")
    refs = {s["ref"] for s in relay.pending(ws, "claude")}
    assert refs == {"repo:a.py", "repo:b.py"}


def test_a_workspace_with_no_relay_is_unaffected(ws):
    out = _hook(ws, "pre-tool-use", _payload(ws))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert not relay.relay_path(ws).exists()


# -------------------------------------------------------- 5. nobody polls


def test_a_backgrounded_job_announces_itself_when_someone_is_watching(ws):
    relay.watch(ws, subscriber="claude", topic="job", action="report")
    r = subprocess.run(
        ["ctx", "run", "--bg", "--", "python", "-c", "import sys; sys.exit(3)"],
        capture_output=True, text=True, cwd=str(ws), timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr

    deadline = time.time() + 90
    while time.time() < deadline and not relay.pending(ws, "claude"):
        time.sleep(0.5)
    signals = relay.pending(ws, "claude")
    assert signals, "the supervisor never announced the finished job"
    assert signals[0]["ref"].startswith("run:")
    assert "exit 3" in signals[0]["note"]

    out = _hook(ws, "post-tool-use", _payload(ws, tool_response="ok"))
    assert signals[0]["ref"] in out["hookSpecificOutput"]["additionalContext"]


def test_a_job_nobody_asked_about_spawns_no_announcer(ws):
    """The supervisor stays dependency-free unless a watch existed at launch."""
    subprocess.run(
        ["ctx", "run", "--bg", "--", "python", "-c", "pass"],
        capture_output=True, text=True, cwd=str(ws), timeout=120, check=True,
    )
    time.sleep(2)
    assert relay.status(ws)["signals"] == 0


def test_an_expensive_capture_announces_its_address_to_watchers(ws):
    """The store was always shared; what was missing is that the peer knew."""
    relay.watch(ws, subscriber="codex", topic="digest")
    subprocess.run(
        ["ctx", "run", "--", "python", "-c", "print('x' * 40000)"],
        capture_output=True, text=True, cwd=str(ws), timeout=120, check=True,
    )
    signals = relay.pending(ws, "codex")
    assert signals, "an expensive capture told nobody"
    assert signals[0]["ref"].startswith("run:")
    assert "KiB captured" in signals[0]["note"]


def test_a_cheap_capture_is_not_worth_a_peer_interrupt(ws):
    relay.watch(ws, subscriber="codex", topic="digest")
    subprocess.run(
        ["ctx", "run", "--", "python", "-c", "print('small')"],
        capture_output=True, text=True, cwd=str(ws), timeout=120, check=True,
    )
    assert relay.pending(ws, "codex") == []


def test_a_host_that_cannot_carry_text_does_not_swallow_the_signal(ws):
    """Antigravity's PostToolUse has exactly one legal output.

    Draining there would mark the signal delivered and then discard it —
    the one failure this channel exists to prevent. It must stay pending
    until a stage that can carry it (pre-invocation) runs.
    """
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="antigravity")
    out = _hook(ws, "post-tool-use", _payload(ws, tool_response="ok"), "antigravity")
    assert out == {}
    assert relay.pending(ws, "antigravity"), "the signal was consumed and thrown away"

    out = _hook(ws, "pre-invocation", {"cwd": str(ws), "session_id": "s1"}, "antigravity")
    assert "repo:a.py" in out["injectSteps"][0]["ephemeralMessage"]
    assert relay.pending(ws, "antigravity") == []


def test_a_native_host_gets_the_relay_on_its_substitution_channel(ws):
    """hermes/omp/opencode/dsh have no additionalContext at PostToolUse; the
    replaced output is the only channel, so the advisory rides with it."""
    relay.publish(ws, topic="peer", ref="repo:a.py", origin="codex", to="opencode")
    small = _hook(ws, "post-tool-use", _payload(ws, tool_response="ok"), "opencode")
    assert small == {}
    assert relay.pending(ws, "opencode"), "no channel, so nothing may be consumed"

    big = _hook(
        ws, "post-tool-use", _payload(ws, tool_response="x" * 40000), "opencode"
    )
    assert "repo:a.py" in big["output"]
    assert relay.pending(ws, "opencode") == []


def test_a_harness_is_not_told_about_its_own_capture(ws):
    """Watching `digest` should surface a *peer's* expensive output, not
    an echo of the flood you just produced yourself."""
    relay.watch(ws, subscriber="claude", topic="digest")
    relay.watch(ws, subscriber="codex", topic="digest")
    out = _hook(
        ws, "post-tool-use", _payload(ws, tool_response="x" * 40000), "claude-code"
    )
    assert out  # the gate substituted the output, so a capture really happened
    assert relay.pending(ws, "claude") == []
    assert [s["topic"] for s in relay.pending(ws, "codex")] == ["digest"]


def test_the_producer_identity_does_not_leak_between_in_process_calls(ws):
    """The hook normally runs in a fresh interpreter; tests and embedders call
    it in-process. An earlier revision stashed the producing harness in an
    environment variable, and one in-process hook call then suppressed the
    announcement for every later capture in the same process — which is how
    this test came to exist."""
    import ctx.hook as hook

    relay.watch(ws, subscriber="claude", topic="digest")
    hook._relay_subscriber("claude-code", {"session_id": "s1"})

    from ctx.digest import digest_output
    from ctx.store import Store
    from ctx.workspace import resolve_workspace

    w = resolve_workspace(str(ws))
    digest_output(Store(w.workspace_id), w, "bash", "x" * 40000)
    assert relay.pending(ws, "claude"), "an unrelated call suppressed the announcement"
