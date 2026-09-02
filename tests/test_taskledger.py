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
