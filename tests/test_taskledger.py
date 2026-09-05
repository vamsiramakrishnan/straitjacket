"""The task ledger: the bus harnesses collaborate over.

Pins the contract that makes the ledger trustworthy as a bus: rows outside the
closed vocabulary are refused rather than stored, folding the rows is pure, a
torn line from a killed process cannot poison a task, and nothing in the file
is content — every row is an address, an id, a number, or a closed-vocabulary
string. The one free-text field (an inbox note) is bounded and declared.
"""

import json

import pytest

from conftest import make_ws

from ctx import taskledger as L


def _tid():
    return L.new_task_id()


# ------------------------------------------------------------ the contract

def test_rows_outside_the_closed_vocabulary_are_refused_not_stored(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    tid = _tid()
    with pytest.raises(L.LedgerError):
        L.append(ws.root, {"schema": "ctx.made-up/v1", "task_id": tid})
    with pytest.raises(L.LedgerError):
        L.append(ws.root, L.handback_row(
            tid, "n", attempt=1, reason="exploded", failure_kind="none", checkpoint=None,
            turns=0, cost_usd=None, tokens=0, exit_code=1, host="h", model="m"))
    with pytest.raises(L.LedgerError):
        L.append(ws.root, L.handback_row(
            tid, "n", attempt=1, reason="failed", failure_kind="cosmic_rays", checkpoint=None,
            turns=0, cost_usd=None, tokens=0, exit_code=1, host="h", model="m"))
    with pytest.raises(L.LedgerError):
        L.append(ws.root, {"schema": L.INBOX_SCHEMA, "task_id": tid, "to": "n", "from": "o"})
    with pytest.raises(L.LedgerError):
        L.append(ws.root, {"schema": L.INBOX_SCHEMA, "task_id": tid, "to": "n", "from": "o",
                           "ref": "run:abc123", "note": "x" * (L.INBOX_NOTE_CHARS + 1)})
    with pytest.raises(L.LedgerError):
        L.ledger_path(ws.root, "../escape")
    assert L.load(ws.root, tid) == []  # nothing above landed


def test_append_load_round_trip_survives_a_torn_line(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    tid = _tid()
    L.append(ws.root, L.claim_row(tid, "a", attempt=1, host="claude", model="m", tier="economy",
                                  expected_turns=12, expected_cost_usd=0.02))
    path = L.ledger_path(ws.root, tid)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"schema": "ctx.handback/v1", "task_id": "' + tid + '", "node_id": "a", "reas')
    L.append(ws.root, L.handback_row(tid, "a", attempt=1, reason="done", failure_kind="none",
                                     checkpoint="checkpoint:abc123abc123", turns=3, cost_usd=0.01,
                                     tokens=40, exit_code=0, host="claude", model="m"))
    rows = L.load(ws.root, tid)
    assert [r["schema"] for r in rows] == [L.CLAIM_SCHEMA, L.HANDBACK_SCHEMA]
    assert all("ts" in r for r in rows)


def test_append_survives_concurrent_writes_from_separate_os_processes(state_home, workspace_dir):
    """append() holds one flock across its read-tail-then-write, not just
    ctx.orchestrator's in-process threading.Lock -- two independent OS
    PROCESSES (not threads) racing on the same ledger file must not corrupt
    or drop each other's rows, even for a row large enough to exceed a
    single write()'s atomicity guarantee (PIPE_BUF, 4096 bytes on Linux)."""
    import os

    if not hasattr(os, "fork"):
        pytest.skip("POSIX-only: append()'s locking is fcntl.flock")

    ws = make_ws(workspace_dir)
    tid = _tid()
    n_children, rows_per_child = 8, 10
    # A big enough `nodes` payload pushes one serialized row past PIPE_BUF,
    # which is exactly the size class two unlocked writers could interleave.
    big_nodes = [{"id": f"n{i}", "goal": "x" * 40} for i in range(120)]

    def _child(child_id: int) -> None:
        for i in range(rows_per_child):
            L.append(ws.root, L.task_row(
                tid, goal_ref="blob:" + f"{child_id:02d}{i:02d}".ljust(12, "a"),
                nodes=big_nodes, budget_usd=0.0, task_kind="general",
                source=f"child-{child_id}-{i}",
            ))
        os._exit(0)

    pids = []
    for child in range(n_children):
        pid = os.fork()
        if pid == 0:
            _child(child)
        pids.append(pid)
    for pid in pids:
        _, status = os.waitpid(pid, 0)
        assert status == 0, "a child process crashed while appending"

    rows = L.load(ws.root, tid)
    assert len(rows) == n_children * rows_per_child          # none lost to a race
    assert len({r["source"] for r in rows}) == n_children * rows_per_child  # none glued together
    assert all(len(r["nodes"]) == len(big_nodes) for r in rows)            # none truncated mid-write


def test_append_holds_an_exclusive_flock_across_its_critical_section(
    state_home, workspace_dir, monkeypatch
):
    """append()'s tail-check-then-write must be one held OS-level lock, not
    separate unlocked reads and writes. Pause the write mid-call so the
    critical section is measurably open, then prove -- from OUTSIDE, via an
    independent non-blocking flock attempt on the same file -- that the
    lock is genuinely held during that window, and genuinely released once
    append() returns."""
    import fcntl
    import os
    import threading

    ws = make_ws(workspace_dir)
    tid = _tid()
    # A warm-up call (unpatched) creates the file so the probe below can
    # open an existing path.
    L.append(ws.root, L.claim_row(tid, "warmup", attempt=1, host="claude", model="m",
                                  tier="economy", expected_turns=1, expected_cost_usd=0.001))
    path = L.ledger_path(ws.root, tid)

    real_write = os.write
    entered = threading.Event()
    release = threading.Event()

    def paused_write(fd, data):
        entered.set()
        release.wait(timeout=2)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", paused_write)

    def do_append():
        L.append(ws.root, L.claim_row(tid, "held", attempt=1, host="claude", model="m",
                                      tier="economy", expected_turns=1, expected_cost_usd=0.001))

    t = threading.Thread(target=do_append)
    t.start()
    assert entered.wait(timeout=2), "append() never reached its write"

    probe_fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # held by the paused append()
    finally:
        os.close(probe_fd)

    release.set()
    t.join(timeout=2)
    assert not t.is_alive()

    probe_fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)       # released once append() returned
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd)


def test_list_tasks_is_newest_first(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    a, b = _tid(), _tid()
    for tid in (a, b):
        L.append(ws.root, L.claim_row(tid, "n", attempt=1, host="h", model="m", tier="economy",
                                      expected_turns=1, expected_cost_usd=0.0))
    assert L.list_tasks(ws.root)[:2] == [b, a]


# ------------------------------------------------------------- derivation

def _seed(ws, tid, budget=1.0):
    L.append(ws.root, L.task_row(tid, goal_ref="blob:aaaaaaaaaaaa", budget_usd=budget,
                                 task_kind="general", source="coordinator",
                                 nodes=[{"id": "a"}, {"id": "b", "deps": ["a"]}]))


def test_task_state_folds_attempts_cost_turns_and_done(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    tid = _tid()
    _seed(ws, tid, budget=1.0)
    L.append(ws.root, L.claim_row(tid, "a", attempt=1, host="h", model="m1", tier="economy",
                                  expected_turns=12, expected_cost_usd=0.1))
    L.append(ws.root, L.handback_row(tid, "a", attempt=1, reason="failed", failure_kind="capability_limit",
                                     checkpoint="checkpoint:111111111111", turns=5, cost_usd=0.10,
                                     tokens=1, exit_code=1, host="h", model="m1"))
    L.append(ws.root, L.steward_row(tid, "a", attempt=1, on_reason="failed", failure_kind="capability_limit",
                                    action="escalate", target="h/m2", budget_remaining_usd=0.9))
    L.append(ws.root, L.claim_row(tid, "a", attempt=2, host="h", model="m2", tier="standard",
                                  expected_turns=12, expected_cost_usd=0.3))
    L.append(ws.root, L.handback_row(tid, "a", attempt=2, reason="done", failure_kind="none",
                                     checkpoint="checkpoint:222222222222", turns=4, cost_usd=0.30,
                                     tokens=1, exit_code=0, host="h", model="m2"))
    st = L.task_state(L.load(ws.root, tid))
    a, b = st.nodes["a"], st.nodes["b"]
    assert a.attempts == 2 and a.done and a.status == "ok"
    assert a.checkpoint == "checkpoint:222222222222"   # the LAST handback's
    assert a.turns == 9 and a.cost_usd == pytest.approx(0.40)
    assert b.status == "pending" and not b.done
    assert st.spent_usd == pytest.approx(0.40) and st.cost_complete
    assert st.remaining_usd == pytest.approx(0.60)
    assert [s["action"] for s in st.steward] == ["escalate"]


def test_missing_cost_marks_partial_never_zero(state_home, workspace_dir):
    """A handback without a cost is an unobserved attempt, not a free one."""
    ws = make_ws(workspace_dir)
    tid = _tid()
    _seed(ws, tid, budget=0.0)
    L.append(ws.root, L.handback_row(tid, "a", attempt=1, reason="done", failure_kind="none",
                                     checkpoint=None, turns=0, cost_usd=None, tokens=0,
                                     exit_code=0, host="h", model="m"))
    st = L.task_state(L.load(ws.root, tid))
    assert st.spent_usd == 0.0 and not st.cost_complete
    assert st.remaining_usd == float("inf")  # unbounded budget never refuses


def test_steward_row_with_unbounded_budget_round_trips_through_json(state_home, workspace_dir):
    """JSON has no infinity. A row that json.loads cannot read back is a row
    resume cannot read."""
    ws = make_ws(workspace_dir)
    tid = _tid()
    L.append(ws.root, L.steward_row(tid, "a", attempt=1, on_reason="failed", failure_kind="unknown",
                                    action="stop_blocked", target=None,
                                    budget_remaining_usd=float("inf")))
    raw = L.ledger_path(ws.root, tid).read_text(encoding="utf-8")
    assert "Infinity" not in raw
    assert json.loads(raw)["budget_remaining_usd"] is None


def test_inbox_carries_an_address_and_a_bounded_note(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    tid = _tid()
    L.append(ws.root, L.inbox_row(tid, to="implement", sender="explore",
                                  ref="repo:src/x.py --lines 4:5@07407f1c",
                                  note="the\x00 place\x07 to start"))
    L.append(ws.root, L.inbox_row(tid, to="verify", sender="implement", ref="checkpoint:abc123abc123"))
    st = L.task_state(L.load(ws.root, tid))
    mine = L.inbox_for(st, "implement")
    assert len(mine) == 1 and mine[0]["ref"].startswith("repo:src/x.py")
    assert "\x00" not in mine[0]["note"] and "\x07" not in mine[0]["note"]
    assert L.inbox_for(st, "nobody") == []


def test_render_task_shows_the_collaboration(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    tid = _tid()
    _seed(ws, tid, budget=2.0)
    L.append(ws.root, L.claim_row(tid, "a", attempt=1, host="codex", model="gpt-5.6-luna", tier="economy",
                                  expected_turns=12, expected_cost_usd=0.1))
    L.append(ws.root, L.handback_row(tid, "a", attempt=1, reason="blocked", failure_kind="auth_failure",
                                     checkpoint="checkpoint:333333333333", turns=1, cost_usd=0.01,
                                     tokens=1, exit_code=1, host="codex", model="gpt-5.6-luna"))
    L.append(ws.root, L.steward_row(tid, "a", attempt=1, on_reason="blocked", failure_kind="auth_failure",
                                    action="stop_blocked", target=None, budget_remaining_usd=1.99))
    out = L.render_task(L.task_state(L.load(ws.root, tid)))
    assert tid in out and "blob:aaaaaaaaaaaa" in out
    assert "a            failed   codex/gpt-5.6-luna" in out
    assert "blocked/auth_failure" in out and "checkpoint:333333333333" in out
    assert "steward: a#1 on blocked/auth_failure → stop_blocked" in out


def test_inbox_ref_must_be_an_address(state_home, workspace_dir):
    """The inbox is the one row a caller writes freely, so it is the one
    place content could leak into a prompt. A ref is an address under the
    reference grammar, optionally followed by `ctx get` options, and bounded;
    prose, output and unbounded strings are refused before they are stored."""
    ws = make_ws(workspace_dir)
    tid = L.new_task_id()
    L.append(ws.root, L.task_row(tid, goal_ref="blob:abc123abc123", nodes=[{"id": "a"}],
                                 budget_usd=0.0, task_kind="general", source="test"))
    ok = [
        "repo:README.md --lines 1:3",
        "repo:src/auth.py --lines 40:52@07407f1c",
        "repo:src/auth.py --lines 40:52 --hashlines",
        "checkpoint:abc123abc123",
        "run:7bd91f2a4c3d#stdout",
        "ws:api/repo:src/main.py",
    ]
    for ref in ok:
        L.append(ws.root, L.inbox_row(tid, to="a", sender="b", ref=ref))
    bad = [
        "please rewrite the auth module and add tests",          # prose, not an address
        "repo:README.md then start from the title",              # prose after the address
        "repo:README.md --lines 1:3 --note 'do it like\nthis'",  # newline
        "repo:README.md --lines 1:3 the quick brown fox",        # value where a flag must be
        "repo:" + "x" * 300,                                     # unbounded
        "blob:not-hex",
        "",
    ]
    for ref in bad:
        with pytest.raises(L.LedgerError):
            L.append(ws.root, L.inbox_row(tid, to="a", sender="b", ref=ref))
    with pytest.raises(L.LedgerError):
        L.append(ws.root, L.inbox_row(tid, to="a node with spaces", sender="b", ref=ok[0]))
    with pytest.raises(L.LedgerError):
        L.append(ws.root, L.inbox_row(tid, to="a", sender="x" * 65, ref=ok[0]))
    assert len(L.task_state(L.load(ws.root, tid)).inbox) == len(ok)


def test_open_claim_reserves_its_estimate_until_handback(state_home, workspace_dir):
    """A claim reserves what it expects to cost. Remaining budget is actuals
    minus reservations, so two nodes claimed in parallel each see the other's
    claim; the reservation is released by the handback."""
    ws = make_ws(workspace_dir)
    tid = L.new_task_id()
    L.append(ws.root, L.task_row(tid, goal_ref="blob:abc123abc123",
                                 nodes=[{"id": "a"}, {"id": "b"}],
                                 budget_usd=1.0, task_kind="general", source="test"))
    L.append(ws.root, L.claim_row(tid, "a", attempt=1, host="claude", model="m", tier="economy",
                                  expected_turns=4, expected_cost_usd=0.4))
    L.append(ws.root, L.claim_row(tid, "b", attempt=1, host="claude", model="m", tier="economy",
                                  expected_turns=4, expected_cost_usd=0.3))
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["a"].open_claim is not None
    assert st.reserved_usd == pytest.approx(0.7)
    assert st.remaining_usd == pytest.approx(0.3)

    L.append(ws.root, L.handback_row(tid, "a", attempt=1, reason="done", failure_kind="none",
                                     checkpoint="checkpoint:abc123abc123", turns=3, cost_usd=0.25,
                                     tokens=10, exit_code=0, host="claude", model="m"))
    st = L.task_state(L.load(ws.root, tid))
    assert st.nodes["a"].open_claim is None
    assert st.reserved_usd == pytest.approx(0.3)                 # only b is still in flight
    assert st.remaining_usd == pytest.approx(1.0 - 0.25 - 0.3)
