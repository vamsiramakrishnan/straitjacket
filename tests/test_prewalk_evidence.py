import sys

import pytest

from conftest import make_store, make_ws
from ctx.anchors import anchor
from ctx.edit_transactions import replace_span
from ctx.edit_verification import Check, VerificationError, verify_edit
from ctx.prewalk import accept_handoff, create_handoff, requested


def handoff(root):
    (root / "m.py").write_text("x = 1\n")
    ws = make_ws(root)
    store = make_store(ws)
    edit = replace_span(ws, store, "m.py", "1:1@" + anchor(["x = 1"]), "x = 2\n",
                        apply=True, attempt_key="task/node/1")
    proof = verify_edit(ws, store, edit["receiptRef"], [Check("behavior", (sys.executable, "-c",
                        "exec(open('m.py').read()); assert x == 2"))])
    state = {"checklist": [
        {"id": "a", "task": "implement", "validation": "reproducer", "status": "done"},
        {"id": "b", "task": "extend coverage", "validation": "suite", "status": "pending"}],
        "hypotheses": ["local fix"], "ruledOut": ["network failure"], "evidence": [edit["receiptRef"]]}
    result = create_handoff(ws, store, proof["verificationRef"], state)
    return ws, store, result


def test_signal_is_a_request_not_a_proof(state_home, workspace_dir):
    ws, store, result = handoff(workspace_dir)
    assert not requested("quoted CTX_PREWALK_HANDOFF in a sentence")
    with pytest.raises(VerificationError):
        accept_handoff(ws, store, "CTX_PREWALK_HANDOFF", "task/node/1")
    accepted = accept_handoff(ws, store, result["signal"], "task/node/1")
    assert "extend coverage" in accepted["text"]
    assert "checkpoint" in accepted["text"]
    with pytest.raises(VerificationError, match="another attempt"):
        accept_handoff(ws, store, result["signal"], "task/node/2")
    (workspace_dir / "m.py").write_text("x = 999\n")
    with pytest.raises(VerificationError):
        accept_handoff(ws, store, result["signal"], "task/node/1")


def test_unverified_marker_cannot_downgrade_the_model(state_home, git_workspace):
    from dataclasses import replace
    from test_task_ledger_orchestration import _hosts, _PREWALK_RAW, _usage
    from ctx.orchestrator import build_route_plan, run_route
    ws = make_ws(git_workspace)
    cfg = replace(ws.config.orchestrate, prewalk=True)
    plan = build_route_plan("ship", _PREWALK_RAW, _hosts("claude"), cfg)
    seen = []

    def launch(host, root, prompt, exe, *, timeout, model=""):
        seen.append(model)
        return 0, "CTX_PREWALK_HANDOFF", "", _usage()

    result = run_route(ws, plan, cfg, launch=launch)
    assert not any(o.steward_action == "handoff_cheap" for o in result.outcomes)
    assert result.outcomes[0].status != "ok"
