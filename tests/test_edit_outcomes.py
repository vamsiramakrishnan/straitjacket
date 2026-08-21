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
