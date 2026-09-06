"""Addressable edit plans are CAS transactions, never fuzzy patches."""

import json
from pathlib import Path

import pytest

from conftest import make_store, make_ws
from ctx import anchors
from ctx.edit_transactions import (
    EditTransactionError,
    PLAN_SCHEMA,
    RECEIPT_SCHEMA,
    REQUEST_SCHEMA,
    apply_edit_plan,
    create_edit_plan,
    preview_edit_plan,
)


def _request(*edits):
    return {"schema": REQUEST_SCHEMA, "edits": list(edits)}


def _edit(path, lines, start, end, replacement):
    span_anchor = anchors.anchor(lines[start - 1 : end])
    return {
        "path": path,
        "span": anchors.format_span(start, end, span_anchor),
        "replacement": replacement,
    }


def test_plan_preview_apply_is_sealed_addressable_and_diagnostic(
    state_home, workspace_dir
):
    path = workspace_dir / "m.py"
    text = "def alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    path.write_text(text, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    lines = text.splitlines()

    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", lines, 5, 5, "    return 3\n"))
    )
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["id"].startswith("sha256:")
    assert plan["edits"][0]["sourceSnapshot"].startswith("snapshot:")
    assert plan["edits"][0]["beforeSha256"].startswith("sha256:")

    preview = preview_edit_plan(ws, store, plan)
    assert preview["schema"] == RECEIPT_SCHEMA
    assert preview["outcome"] == "ready"
    assert preview["patch"].startswith("blob:")
    assert path.read_text(encoding="utf-8") == text

    receipt = apply_edit_plan(ws, plan)
    assert receipt["outcome"] == "applied"
    assert receipt["files"][0]["diagnostics"]["outcome"] == "clean"
    assert path.read_text(encoding="utf-8").endswith("    return 3\n")


def test_apply_recovers_a_uniquely_relocated_exact_span(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    text = "one\ntwo\nthree\n"
    path.write_text(text, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", text.splitlines(), 2, 2, "TWO\n"))
    )

    path.write_text("zero\n" + text, encoding="utf-8")
    receipt = apply_edit_plan(ws, plan)

    assert path.read_text(encoding="utf-8") == "zero\none\nTWO\nthree\n"
    edit_receipt = receipt["files"][0]["edits"][0]
    assert edit_receipt["plannedSpan"].startswith("2:2@")
    assert edit_receipt["resolvedSpan"] == "3:3"
    assert edit_receipt["relocated"] is True


def test_changed_target_refuses_without_writing(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    text = "one\ntwo\nthree\n"
    path.write_text(text, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", text.splitlines(), 2, 2, "TWO\n"))
    )
    changed = "one\ntwo changed\nthree\n"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(EditTransactionError) as caught:
        apply_edit_plan(ws, plan)

    assert "changed or disappeared" in str(caught.value)
    assert caught.value.receipt["outcome"] == "refused"
    assert path.read_text(encoding="utf-8") == changed


def test_deleted_target_returns_a_refusal_receipt(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    path.write_text("x = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n"))
    )
    path.unlink()

    with pytest.raises(EditTransactionError) as caught:
        apply_edit_plan(ws, plan)
    assert caught.value.receipt["outcome"] == "refused"
    assert not path.exists()


def test_ambiguous_stale_target_refuses_rather_than_choosing(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    text = "top\nneedle\nbottom\n"
    path.write_text(text, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", text.splitlines(), 2, 2, "done\n"))
    )
    ambiguous = "needle\ntop\nneedle\nbottom\n"
    path.write_text(ambiguous, encoding="utf-8")

    with pytest.raises(EditTransactionError, match="ambiguous"):
        apply_edit_plan(ws, plan)
    assert path.read_text(encoding="utf-8") == ambiguous


def test_multifile_preflight_is_all_before_any_write(state_home, workspace_dir):
    a = workspace_dir / "a.py"
    b = workspace_dir / "b.py"
    a.write_text("a = 1\n", encoding="utf-8")
    b.write_text("b = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws,
        store,
        _request(
            _edit("a.py", ["a = 1"], 1, 1, "a = 2\n"),
            _edit("b.py", ["b = 1"], 1, 1, "b = 2\n"),
        ),
    )
    b.write_text("b = externally_changed\n", encoding="utf-8")

    with pytest.raises(EditTransactionError):
        apply_edit_plan(ws, plan)
    assert a.read_text(encoding="utf-8") == "a = 1\n"
    assert b.read_text(encoding="utf-8") == "b = externally_changed\n"


def test_multiple_edits_in_one_file_require_one_relocation_offset(
    state_home, workspace_dir
):
    path = workspace_dir / "m.txt"
    text = "a\nb\nc\nd\n"
    path.write_text(text, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws,
        store,
        _request(
            _edit("m.txt", text.splitlines(), 1, 1, "A\n"),
            _edit("m.txt", text.splitlines(), 4, 4, "D\n"),
        ),
    )
    # Inserting between the targets moves only the second one. Each target is
    # individually findable, but accepting different shifts would reinterpret
    # one logical patch against a source shape it was never reviewed on.
    changed = "a\nb\ninserted\nc\nd\n"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(EditTransactionError, match="inconsistent offsets"):
        apply_edit_plan(ws, plan)
    assert path.read_text(encoding="utf-8") == changed


def test_plan_integrity_and_workspace_identity_are_mandatory(state_home, workspace_dir):
    path = workspace_dir / "m.py"
    path.write_text("x = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n"))
    )
    plan["edits"][0]["replacement"] = "x = 999\n"

    with pytest.raises(EditTransactionError, match="integrity"):
        apply_edit_plan(ws, plan)
    assert path.read_text(encoding="utf-8") == "x = 1\n"

    for field, value, message in (
        ("path", "another.py", "integrity"),
        ("replacement", "x = 3\n", "integrity"),
        ("workspaceId", "sha256:not-this-workspace", "different workspace"),
    ):
        tampered = json.loads(json.dumps(create_edit_plan(
            ws, store, _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n"))
        )))
        if field == "workspaceId":
            tampered[field] = value
        else:
            tampered["edits"][0][field] = value
        with pytest.raises(EditTransactionError, match=message):
            apply_edit_plan(ws, tampered)


def test_multifile_commit_failure_rolls_back_and_discloses(
    state_home, workspace_dir, monkeypatch
):
    import ctx.edit_transactions as transactions

    a = workspace_dir / "a.py"
    b = workspace_dir / "b.py"
    a.write_text("a = 1\n", encoding="utf-8")
    b.write_text("b = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws,
        store,
        _request(
            _edit("a.py", ["a = 1"], 1, 1, "a = 2\n"),
            _edit("b.py", ["b = 1"], 1, 1, "b = 2\n"),
        ),
    )
    real_replace = transactions.os.replace
    source_commits = 0

    def fail_second_source_commit(source, destination):
        nonlocal source_commits
        if Path(destination) in (a, b):
            source_commits += 1
            if source_commits == 2:
                raise OSError("simulated second-file rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(transactions.os, "replace", fail_second_source_commit)
    with pytest.raises(EditTransactionError) as caught:
        apply_edit_plan(ws, plan)

    assert "simulated second-file rename failure" in str(caught.value)
    assert caught.value.receipt["outcome"] == "refused"
    assert a.read_text(encoding="utf-8") == "a = 1\n"
    assert b.read_text(encoding="utf-8") == "b = 1\n"


def test_outside_symlink_is_refused_by_workspace_confinement(
    state_home, workspace_dir, tmp_path
):
    from ctx.workspace import PathEscapeError

    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    (workspace_dir / "escape.py").symlink_to(outside)
    ws = make_ws(workspace_dir)
    store = make_store(ws)

    with pytest.raises(PathEscapeError):
        create_edit_plan(
            ws,
            store,
            _request(_edit("escape.py", ["x = 1"], 1, 1, "x = 2\n")),
        )


def test_diagnostic_failure_cannot_turn_a_committed_edit_into_a_refusal(
    state_home, workspace_dir, monkeypatch
):
    import ctx.post_edit_diagnostics as diagnostics

    path = workspace_dir / "m.py"
    path.write_text("x = 1\n", encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    plan = create_edit_plan(
        ws, store, _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n"))
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("diagnostic backend down")

    monkeypatch.setattr(diagnostics, "verify_post_edit", unavailable)
    receipt = apply_edit_plan(ws, plan)

    assert receipt["outcome"] == "applied"
    assert receipt["files"][0]["diagnostics"]["outcome"] == "unavailable"
    assert path.read_text(encoding="utf-8") == "x = 2\n"


def test_cli_plan_preview_apply_round_trip(state_home, workspace_dir, capsys):
    from ctx.cli import main

    target = workspace_dir / "m.py"
    target.write_text("x = 1\n", encoding="utf-8")
    request = _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n"))
    (workspace_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")

    assert main(["--workspace", str(workspace_dir), "edit", "plan", "request.json", "--out", "plan.json"]) == 0
    assert "planned" in capsys.readouterr().out
    assert main(["--workspace", str(workspace_dir), "edit", "preview", "plan.json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "ready"
    assert main(["--workspace", str(workspace_dir), "edit", "apply", "plan.json", "--receipt", "receipt.json"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "applied"
    assert target.read_text(encoding="utf-8") == "x = 2\n"
    assert json.loads((workspace_dir / "receipt.json").read_text())["outcome"] == "applied"


def test_cli_plan_error_and_help_are_deterministic(state_home, workspace_dir, capsys):
    from ctx.cli import main

    (workspace_dir / "bad.json").write_text("{}", encoding="utf-8")
    assert main([
        "--workspace", str(workspace_dir), "edit", "plan", "bad.json",
        "--out", "plan.json",
    ]) == 2
    assert capsys.readouterr().err == f"ctx edit: expected {REQUEST_SCHEMA}\n"

    with pytest.raises(SystemExit) as caught:
        main(["edit", "plan", "--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "ctx.edit-request/v1 JSON" in help_text
    assert "--out OUT" in help_text


def test_plan_uses_the_cited_snapshot_even_when_live_file_changes(
    state_home, workspace_dir, monkeypatch
):
    import ctx.edit_transactions as edits
    p = workspace_dir / "m.py"
    p.write_text("x = 1\ny = 1\n")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    real = edits.snapshot_file

    def race(*args):
        snap = real(*args)
        p.write_text("x = 1\ny = 99\n")
        return snap

    monkeypatch.setattr(edits, "snapshot_file", race)
    plan = create_edit_plan(ws, store, _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n")))
    row = plan["edits"][0]
    assert row["sourceFileSha256"] == row["sourceBlob"]
    assert store.get_blob(row["sourceBlob"].removeprefix("sha256:")) == b"x = 1\ny = 1\n"


def test_diagnostics_cannot_validate_a_concurrent_writers_bytes(
    state_home, workspace_dir, monkeypatch
):
    import ctx.post_edit_diagnostics as diagnostics
    p = workspace_dir / "m.py"
    p.write_text("x = 1\n")
    ws = make_ws(workspace_dir)
    plan = create_edit_plan(ws, make_store(ws), _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n")))
    real = diagnostics.builtin_syntax_snapshot

    def race(path):
        p.write_text("x = 300\n")
        return real(path)

    monkeypatch.setattr(diagnostics, "builtin_syntax_snapshot", race)
    receipt = apply_edit_plan(ws, plan)
    assert receipt["outcome"] == "applied"
    assert receipt["files"][0]["diagnostics"]["outcome"] == "stale"


def test_one_call_adapter_preview_apply_and_addressed_receipt(state_home, workspace_dir):
    from ctx.edit_transactions import replace_span
    p = workspace_dir / "m.py"
    p.write_text("x = 1\n")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    span = _edit("m.py", ["x = 1"], 1, 1, "")["span"]
    preview = replace_span(ws, store, "repo:m.py", span, "x = 2\n")
    assert preview["outcome"] == "ready"
    assert p.read_text() == "x = 1\n"
    applied = replace_span(ws, store, "repo:m.py", span, "x = 2\n", apply=True)
    saved = json.loads(store.get_blob(applied["receiptRef"].removeprefix("blob:")))
    assert saved["workspaceId"] == ws.workspace_id
    assert saved["files"][0]["afterSha256"] == applied["files"][0]["afterSha256"]


def test_stale_refusal_names_recovery_without_guessing(state_home, workspace_dir):
    p = workspace_dir / "m.py"
    p.write_text("x = 1\n")
    ws = make_ws(workspace_dir)
    plan = create_edit_plan(ws, make_store(ws), _request(_edit("m.py", ["x = 1"], 1, 1, "x = 2\n")))
    p.write_text("x = 99\n")
    with pytest.raises(EditTransactionError) as caught:
        apply_edit_plan(ws, plan)
    receipt = caught.value.receipt
    assert receipt["code"] == "stale_target" and receipt["retryable"]
    assert receipt["recovery"][0]["ref"] == "repo:m.py"
    assert p.read_text() == "x = 99\n"


def test_replace_cli_defaults_to_preview(state_home, workspace_dir, capsys):
    from ctx.cli import main
    p = workspace_dir / "m.py"
    p.write_text("x = 1\n")
    (workspace_dir / "replacement.txt").write_text("x = 2\n")
    args = ["--workspace", str(workspace_dir), "edit", "replace", "repo:m.py", "--lines",
            "1:1@" + anchors.anchor(["x = 1"]), "--replacement-file", "replacement.txt"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "ready"
    assert p.read_text() == "x = 1\n"
    assert main(args + ["--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["receiptRef"].startswith("blob:")
