"""The edit-outcome instrument: what happened to the host's own edit.

straitjacket has always SEEN every Edit/Write the host issues -- `_tool_kind`
classifies them and PreToolUse allows them through -- and has never looked at
whether they landed. These pin the ledger that closes that gap, including the
two properties that decide whether its numbers can be trusted: an unrecognised
result never gets forced into a bucket, and no edited content reaches disk.
"""

import json

import pytest

from conftest import make_ws

from ctx.edit_outcomes import (
    EDIT_OUTCOME_SCHEMA,
    OUTCOMES,
    append_edit_outcome,
    classify,
    edit_summary,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("String to replace not found in file.", "not_found"),
        ("Error: old_string not found in /a/b.py", "not_found"),
        ("Found 3 matches of the string to replace, but replace_all is false", "not_unique"),
        ("The string is not unique in the file", "not_unique"),
        ("The file /a/b.py has been updated.", "applied"),
        ("Successfully edited /a/b.py", "applied"),
        ("Permission denied", "other_error"),
    ],
)
def test_classify_reads_the_host_error_vocabulary(text, expected):
    assert classify(text) == expected


def test_unrecognised_text_stays_unknown_rather_than_becoming_success():
    """Hosts reword their errors between releases. Defaulting an unrecognised
    result to `applied` would hide that behind a rising success rate on exactly
    the hosts we have not learned yet; `unknown` makes the drift visible."""
    assert classify("<<< some future host phrasing >>>") == "unknown"
    assert classify("") == "unknown"
    # The host's own error flag is a fact even when the wording is not.
    assert classify("<<< future phrasing >>>", is_error=True) == "other_error"
    assert classify("", is_error=True) == "other_error"


def test_failure_markers_beat_the_success_marker():
    """A failure message can legitimately contain success words while
    describing what did NOT happen."""
    assert classify("The file has been updated? No: string to replace not found") == "not_found"


def test_ledger_records_the_outcome_and_none_of_the_content(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    secret_old = "API_KEY = 'sk-do-not-log-me'"
    secret_new = "API_KEY = os.environ['K']"
    append_edit_outcome(
        ws.root, tool="Edit", outcome="not_found",
        path="src/secrets.py", old_len=len(secret_old), new_len=len(secret_new),
        flavor="claude-code",
    )
    from ctx.edit_outcomes import _ledger_path

    raw = _ledger_path(ws.root).read_text(encoding="utf-8")
    row = json.loads(raw.strip())
    assert row["schema"] == EDIT_OUTCOME_SCHEMA
    assert row["outcome"] == "not_found"
    assert row["oldLen"] == len(secret_old)
    # A ledger meant to be pasted into a receipt cannot carry the edit's
    # content, nor the path in the clear.
    assert secret_old not in raw and secret_new not in raw
    assert "src/secrets.py" not in raw
    assert len(row["pathDigest"]) == 12


def test_unknown_outcome_names_are_normalized_not_stored(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    append_edit_outcome(ws.root, tool="Edit", outcome="wishful-thinking")
    assert edit_summary(ws.root)["counts"]["unknown"] == 1


def test_summary_separates_the_repairable_failures(state_home, workspace_dir):
    """`not_unique` is a failure but NOT a repairable one: several equally good
    matches is the model's ambiguity, and resolving it by choosing would be the
    harness inventing intent. The summary must not blend the two."""
    ws = make_ws(workspace_dir)
    for outcome in ["applied"] * 6 + ["not_found"] * 3 + ["not_unique"]:
        append_edit_outcome(ws.root, tool="Edit", outcome=outcome, path="a.py")
    s = edit_summary(ws.root)
    assert s["total"] == 10
    assert s["failures"] == 4
    assert s["failure_rate"] == pytest.approx(0.4)
    assert s["repairable_share"] == pytest.approx(0.75)  # 3 of 4, not 4 of 4


def test_summary_of_an_empty_ledger_is_zeros_not_a_crash(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    s = edit_summary(ws.root)
    assert s["total"] == 0 and s["failure_rate"] == 0.0
    assert set(s["counts"]) == set(OUTCOMES)


def test_recording_never_raises(state_home, workspace_dir):
    """Telemetry must never be the reason an agent's edit appears to fail."""
    ws = make_ws(workspace_dir)
    append_edit_outcome(ws.root, tool="Edit", outcome="applied", old_len=-5, new_len=-1)
    append_edit_outcome(None, tool="Edit", outcome="applied")  # type: ignore[arg-type]
    assert edit_summary(ws.root)["counts"]["applied"] == 1


def test_hook_records_only_edit_kind_tools(state_home, workspace_dir, monkeypatch):
    """The PostToolUse path sees every tool. Only the edit family belongs here;
    counting a Read as an edit outcome would poison the rate this exists for."""
    from ctx import hook

    ws = make_ws(workspace_dir)
    monkeypatch.setenv("CTX_WORKSPACE_ROOT", str(ws.root))

    def payload(tool, response):
        return {
            "tool_name": tool,
            "cwd": str(ws.root),
            "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
            "tool_response": response,
        }

    hook._record_edit_outcome(payload("Read", "some file text"), "claude-code")
    hook._record_edit_outcome(payload("Grep", "matches"), "claude-code")
    assert edit_summary(ws.root)["total"] == 0

    hook._record_edit_outcome(
        payload("Edit", "String to replace not found in file."), "claude-code"
    )
    hook._record_edit_outcome(payload("Write", "The file has been updated."), "claude-code")
    s = edit_summary(ws.root)
    assert s["total"] == 2
    assert s["counts"]["not_found"] == 1 and s["counts"]["applied"] == 1
    assert s["hosts_reporting"] == ["claude-code"]


# ------------------------------------------------- format × model axes
#
# Published edit benchmarks agree the same model succeeds or fails on the
# SHAPE of an edit, and that the ranking differs by model. A ledger that
# cannot split by (format, model) cannot say whether the anchored format
# beats a host's native Edit for the model in use. These pin the two axes.


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("Edit", "search_replace"),
        ("MultiEdit", "search_replace"),
        ("str_replace_editor", "search_replace"),
        ("replace_file_content", "search_replace"),
        ("NotebookEdit", "search_replace"),
        ("Write", "whole_file"),
        ("WriteFile", "whole_file"),
        ("create_file", "whole_file"),
        ("apply_patch", "patch"),
        ("ApplyPatch", "patch"),
        ("ctx edit apply", "anchored"),
        ("SomeFutureTool", "other"),
        ("", "other"),
    ],
)
def test_edit_format_is_a_closed_vocabulary_over_tool_names(tool, expected):
    from ctx.edit_outcomes import FORMATS, edit_format

    assert edit_format(tool) == expected
    assert edit_format(tool) in FORMATS


def test_model_comes_from_the_launcher_variable_first(tmp_path):
    from ctx.edit_outcomes import resolve_model

    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"assistant","message":{"model":"from-transcript"}}\n')
    assert resolve_model({"CTX_MODEL": "from-env"}, str(transcript)) == "from-env"


def test_model_falls_back_to_the_transcript_tail_then_unknown(tmp_path):
    """No launcher set CTX_MODEL (an interactive host session). The transcript
    names the model on every assistant message; the LAST one wins, read from
    a bounded tail so a long session never costs a full-file read."""
    from ctx.edit_outcomes import _TRANSCRIPT_TAIL_BYTES, resolve_model

    transcript = tmp_path / "t.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "x" * 200}}) + "\n"
    body = (
        '{"type":"assistant","message":{"model":"older-model"}}\n'
        + filler * (2 * _TRANSCRIPT_TAIL_BYTES // len(filler))
        + '{"type":"assistant","message":{"model":"newer-model"}}\n'
        + filler
    )
    transcript.write_text(body, encoding="utf-8")
    assert transcript.stat().st_size > _TRANSCRIPT_TAIL_BYTES
    assert resolve_model({}, str(transcript)) == "newer-model"
    assert resolve_model({}, str(tmp_path / "missing.jsonl")) == "unknown"
    assert resolve_model({}, None) == "unknown"
    # An unnamed model is never invented from an unrelated variable.
    assert resolve_model({"MODEL": "not-ours", "ANTHROPIC_MODEL": "not-ours"}) == "unknown"


def test_rows_carry_model_and_format_and_the_summary_splits_on_them(
    state_home, workspace_dir
):
    ws = make_ws(workspace_dir)
    for _ in range(3):
        append_edit_outcome(ws.root, tool="Edit", outcome="applied", model="m-a")
    append_edit_outcome(ws.root, tool="Edit", outcome="not_found", model="m-a")
    append_edit_outcome(ws.root, tool="ctx edit apply", outcome="applied", model="m-a",
                        fmt="anchored")
    append_edit_outcome(ws.root, tool="Write", outcome="applied", model="m-b")
    append_edit_outcome(ws.root, tool="Edit", outcome="unknown", model="m-b")
    append_edit_outcome(ws.root, tool="Edit", outcome="applied")  # no model named

    from ctx.edit_outcomes import _ledger_path

    rows = [json.loads(ln) for ln in _ledger_path(ws.root).read_text().splitlines()]
    assert rows[0]["model"] == "m-a" and rows[0]["format"] == "search_replace"
    assert rows[4]["format"] == "anchored"
    assert rows[-1]["model"] == "unknown"

    s = edit_summary(ws.root)
    assert s["models_reporting"] == ["m-a", "m-b"]
    assert s["unlabelled_model_rows"] == 1
    cell = s["by_model"]["m-a"]["search_replace"]
    assert cell["total"] == 4 and cell["failures"] == 1
    assert cell["success_rate"] == pytest.approx(0.75)
    assert s["by_model"]["m-a"]["anchored"]["success_rate"] == 1.0
    # `unknown` is not a failure and not a success: it leaves the denominator.
    b = s["by_model"]["m-b"]["search_replace"]
    assert b["total"] == 1 and b["classified"] == 0 and b["success_rate"] == 0.0
    assert s["by_model"]["unknown"]["search_replace"]["total"] == 1


def test_rows_from_before_the_two_fields_still_summarize(state_home, workspace_dir):
    """An old ledger has no model/format. It folds into `unknown` and the
    format the tool name implies rather than being dropped or crashing."""
    from ctx.edit_outcomes import _ledger_path

    ws = make_ws(workspace_dir)
    path = _ledger_path(ws.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": EDIT_OUTCOME_SCHEMA, "ts": 0, "tool": "Write", "outcome": "applied",
        "flavor": "codex", "oldLen": 0, "newLen": 9,
    }) + "\n")
    s = edit_summary(ws.root)
    assert s["by_model"] == {"unknown": {"whole_file": {
        "total": 1, "counts": {"applied": 1, "not_found": 0, "not_unique": 0,
                               "other_error": 0, "unknown": 0},
        "classified": 1, "failures": 0, "success_rate": 1.0, "failure_rate": 0.0,
    }}}


def test_unlisted_format_is_other_never_free_text(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    append_edit_outcome(ws.root, tool="Edit", outcome="applied", fmt="my-new-format")
    assert list(edit_summary(ws.root)["by_model"]["unknown"]) == ["other"]


def test_hook_labels_rows_with_the_launcher_model(state_home, workspace_dir, monkeypatch):
    from ctx import hook

    ws = make_ws(workspace_dir)
    monkeypatch.setenv("CTX_WORKSPACE_ROOT", str(ws.root))
    monkeypatch.setenv("CTX_MODEL", "launched-model")
    hook._record_edit_outcome({
        "tool_name": "Edit", "cwd": str(ws.root),
        "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
        "tool_response": "The file has been updated.",
    }, "claude-code")
    monkeypatch.delenv("CTX_MODEL")
    transcript = ws.root / "transcript.jsonl"
    transcript.write_text('{"type":"assistant","message":{"model":"session-model"}}\n')
    hook._record_edit_outcome({
        "tool_name": "Write", "cwd": str(ws.root), "transcript_path": str(transcript),
        "tool_input": {"file_path": "b.py", "content": "z"},
        "tool_response": "The file has been updated.",
    }, "claude-code")
    s = edit_summary(ws.root)
    assert s["models_reporting"] == ["launched-model", "session-model"]
    assert s["by_model"]["launched-model"]["search_replace"]["total"] == 1
    assert s["by_model"]["session-model"]["whole_file"]["total"] == 1


def test_orchestrator_launch_names_the_model_for_the_hooks(monkeypatch, tmp_path):
    """`ctx orchestrate` is the one place that knows which model it launched.
    It must tell the child's hooks, or every orchestrated row is `unknown`."""
    import subprocess

    from ctx import hosts, orchestrator

    seen = {}

    def fake_run(argv, **kw):
        seen["env"] = kw.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "_run_bounded", fake_run)
    monkeypatch.setattr(orchestrator, "parse_host_output", lambda *a, **k: ("", None))
    # The suite itself may run under `ctx wrap`, which exports this; the
    # assertion is about what the LAUNCHER adds, not what it inherits.
    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    codex = next(h for h in hosts.detect_all(which=lambda b: None) if h.spec.name == "codex")
    code, *_ = orchestrator._launch_host(codex, tmp_path, "p", "ctx", timeout=5, model="m-x")
    assert code == 0
    assert seen["env"]["CTX_MODEL"] == "m-x" and seen["env"]["CTX_HOST"] == "codex"
    assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS" not in seen["env"]  # not a claude launch


def test_orchestrator_claude_nodes_wait_for_their_subagents(monkeypatch, tmp_path):
    """Print mode kills background subagents 600 s after the main turn ends;
    a node that fans out and waits would lose its work on that timer. The
    launcher defaults the ceiling off; the per-node timeout is the bound."""
    import subprocess

    from ctx import hosts, orchestrator

    seen = {}

    def fake_run(argv, **kw):
        seen["env"] = kw.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator, "_run_bounded", fake_run)
    monkeypatch.setattr(orchestrator, "parse_host_output", lambda *a, **k: ("", None))
    claude = next(h for h in hosts.detect_all(which=lambda b: None) if h.spec.name == "claude")
    monkeypatch.delenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", raising=False)
    orchestrator._launch_host(claude, tmp_path, "p", "ctx", timeout=5, model="m-x")
    assert seen["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    monkeypatch.setenv("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "5000")
    orchestrator._launch_host(claude, tmp_path, "p", "ctx", timeout=5, model="m-x")
    assert seen["env"]["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "5000"


def test_ctx_edit_apply_records_anchored_rows_in_the_same_ledger(
    state_home, workspace_dir, capsys, monkeypatch
):
    """The comparison the format question needs: anchored rows beside native
    rows, same vocabulary, same model label. One row per planned file; the
    two addressable refusals map to the needle's two failure kinds."""
    from ctx import anchors
    from ctx.cli import main
    from ctx.edit_transactions import REQUEST_SCHEMA

    monkeypatch.setenv("CTX_MODEL", "m-anch")
    target = workspace_dir / "m.py"
    target.write_text("x = 1\ny = 2\n", encoding="utf-8")
    span = anchors.format_span(1, 1, anchors.anchor(["x = 1"]))
    request = {"schema": REQUEST_SCHEMA,
               "edits": [{"path": "m.py", "span": span, "replacement": "x = 22\n"}]}
    (workspace_dir / "r.json").write_text(json.dumps(request), encoding="utf-8")
    argv = ["--workspace", str(workspace_dir), "edit"]
    assert main([*argv, "plan", "r.json", "--out", "plan.json"]) == 0
    assert main([*argv, "preview", "plan.json"]) == 0     # a preview is not an edit
    assert main([*argv, "apply", "plan.json"]) == 0
    capsys.readouterr()
    ws = make_ws(workspace_dir)
    s = edit_summary(ws.root)
    assert s["total"] == 1
    cell = s["by_model"]["m-anch"]["anchored"]
    assert cell["counts"]["applied"] == 1
    from ctx.edit_outcomes import _ledger_path

    row = json.loads(_ledger_path(ws.root).read_text().splitlines()[0])
    assert row["tool"] == "ctx edit apply" and row["flavor"] == "ctx"
    assert row["oldLen"] == len("x = 1\n") and row["newLen"] == len("x = 22\n")
    assert "m.py" not in _ledger_path(ws.root).read_text()

    # The target is gone: the plan cannot find its bytes -> not_found.
    target.write_text("q = 0\ny = 2\n", encoding="utf-8")
    assert main([*argv, "apply", "plan.json"]) == 2
    # The target moved and now exists twice: refused, not chosen -> not_unique.
    target.write_text("q = 0\nx = 1\nx = 1\n", encoding="utf-8")
    assert main([*argv, "apply", "plan.json"]) == 2
    capsys.readouterr()
    cell = edit_summary(ws.root)["by_model"]["m-anch"]["anchored"]
    assert cell["counts"] == {"applied": 1, "not_found": 1, "not_unique": 1,
                              "other_error": 0, "unknown": 0}
