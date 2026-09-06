import json
import sys

import pytest

from conftest import make_store, make_ws
from ctx.anchors import anchor
from ctx.edit_transactions import replace_span
from ctx.edit_verification import Check, VerificationError, validate_verification, verify_edit


def edited(root):
    (root / "m.py").write_text("x = 1\n")
    ws = make_ws(root)
    store = make_store(ws)
    receipt = replace_span(ws, store, "m.py", "1:1@" + anchor(["x = 1"]), "x = 2\n", apply=True)
    return ws, store, receipt["receiptRef"]


def test_behavioral_proof_and_later_drift(state_home, workspace_dir):
    ws, store, ref = edited(workspace_dir)
    check = Check("behavior", (sys.executable, "-c", "exec(open('m.py').read()); assert x == 2"))
    proof = verify_edit(ws, store, ref, [check])
    assert validate_verification(ws, store, proof["verificationRef"])["outcome"] == "passed"
    (workspace_dir / "m.py").write_text("x = 3\n")
    with pytest.raises(VerificationError, match="changed"):
        validate_verification(ws, store, proof["verificationRef"])


@pytest.mark.parametrize("script,outcome", [("raise SystemExit(1)", "failed"),
    ("open('m.py','w').write('x = 99\\n')", "stale")])
def test_failed_or_mutating_checks_do_not_authorize_handoff(state_home, workspace_dir, script, outcome):
    ws, store, ref = edited(workspace_dir)
    proof = verify_edit(ws, store, ref, [Check("behavior", (sys.executable, "-c", script))])
    assert proof["outcome"] == outcome
    with pytest.raises(VerificationError):
        validate_verification(ws, store, proof["verificationRef"])


def test_syntax_is_not_behavior_and_witness_changes_invalidate(state_home, workspace_dir):
    ws, store, ref = edited(workspace_dir)
    (workspace_dir / "test_m.py").write_text("assert True\n")
    proof = verify_edit(ws, store, ref, [Check("syntax", (sys.executable, "-c", "pass"))])
    with pytest.raises(VerificationError, match="behavioral"):
        validate_verification(ws, store, proof["verificationRef"])
    proof = verify_edit(ws, store, ref, [Check("behavior", (sys.executable, "test_m.py"))], witnesses=["test_m.py"])
    (workspace_dir / "test_m.py").write_text("assert False\n")
    with pytest.raises(VerificationError, match="stale"):
        validate_verification(ws, store, proof["verificationRef"])


def test_verify_cli_preserves_child_failure(state_home, workspace_dir, capsys):
    from ctx.cli import main
    ws, store, ref = edited(workspace_dir)
    assert main(["--workspace", str(workspace_dir), "edit", "verify", ref, "--", sys.executable,
                 "-c", "raise SystemExit(1)"]) == 3
    assert json.loads(capsys.readouterr().out)["outcome"] == "failed"
