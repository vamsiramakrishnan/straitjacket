import json

import pytest

from ctx import astgrep
from ctx.edit_expansion import plan_expansion
from ctx.edit_transactions import apply_edit_plan
from ctx.edit_verification import Check, VerificationError, read_evidence, verify_edit
from test_edit_verification import edited
import sys


def fake_engine(ws, argv):
    pattern = argv[argv.index("--pattern") + 1].encode()
    replacement = argv[argv.index("--rewrite") + 1]
    rows = []
    for path in ws.root.rglob("*.py"):
        data = path.read_bytes()
        start = data.find(pattern)
        if start >= 0:
            rows.append({"file": path.relative_to(ws.root).as_posix(),
                         "replacement": replacement,
                         "range": {"byteOffset": {"start": start, "end": start + len(pattern)}}})
    return json.dumps(rows).encode()


def example(root):
    ws, store, ref = edited(root)
    proof = verify_edit(ws, store, ref, [Check("behavior", (sys.executable, "-c",
                        "exec(open('m.py').read()); assert x == 2"))])
    (root / "n.py").write_text("x = 1\n")
    return ws, store, proof["verificationRef"]


def test_expand_preview_is_scoped_and_reuses_apply_transaction(state_home, workspace_dir, monkeypatch):
    monkeypatch.setattr(astgrep, "_run_astgrep", fake_engine)
    ws, store, ref = example(workspace_dir)
    result = plan_expansion(ws, store, ref, pattern="x = 1", replacement="x = 2", language="python", glob="*.py")
    assert (workspace_dir / "n.py").read_text() == "x = 1\n"
    assert [f["path"] for f in result["files"]] == ["n.py"]
    plan = read_evidence(store, result["planRef"], "ctx.edit-plan/v1")
    receipt = apply_edit_plan(ws, plan)
    assert receipt["outcome"] == "applied"
    assert (workspace_dir / "n.py").read_text() == "x = 2\n"


def test_wrong_generalization_is_refused(state_home, workspace_dir, monkeypatch):
    monkeypatch.setattr(astgrep, "_run_astgrep", fake_engine)
    ws, store, ref = example(workspace_dir)
    with pytest.raises(VerificationError, match="does not reproduce"):
        plan_expansion(ws, store, ref, pattern="x = 1", replacement="x = 3", language="python", glob="*.py")
    assert (workspace_dir / "n.py").read_text() == "x = 1\n"


def test_expansion_refuses_concurrent_source_change(state_home, workspace_dir, monkeypatch):
    ws, store, ref = example(workspace_dir)
    calls = 0

    def race(frozen, argv):
        nonlocal calls
        calls += 1
        raw = fake_engine(frozen, argv)
        if calls == 2:
            (workspace_dir / "n.py").write_text("x = 99\n")
        return raw

    monkeypatch.setattr(astgrep, "_run_astgrep", race)
    with pytest.raises(VerificationError, match="source changed"):
        plan_expansion(ws, store, ref, pattern="x = 1", replacement="x = 2", language="python", glob="*.py")
    assert (workspace_dir / "n.py").read_text() == "x = 99\n"


def test_expansion_declares_missing_engine(state_home, workspace_dir, monkeypatch):
    ws, store, ref = example(workspace_dir)
    monkeypatch.setattr(astgrep, "binary", lambda: None)
    with pytest.raises(astgrep.EngineMissing):
        plan_expansion(ws, store, ref, pattern="x = 1", replacement="x = 2", language="python", glob="*.py")


@pytest.mark.skipif(astgrep.binary() is None, reason="ast-grep binary optional")
def test_real_structural_expansion(state_home, workspace_dir):
    ws, store, ref = example(workspace_dir)
    result = plan_expansion(ws, store, ref, pattern="x = 1", replacement="x = 2", language="python", glob="*.py")
    assert result["outcome"] == "ready"
