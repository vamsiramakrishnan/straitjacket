"""The PreToolUse guard used to classify tools by raw substring on the lowercased
name, which silently mis-routed unrelated third-party tools: `credit_check`
contains "edit", `playlist` contains "list", `thread_reply` contains "read".
A mis-route is not cosmetic — a tool wrongly seen as "search" gets a head-limit
injected into its input; one wrongly seen as "edit" pollutes starvation state.

`_tool_kind` classifies by exact name or whole word instead. These tests pin
both directions: every real tool keeps its kind, and the bystanders get None.
"""
from __future__ import annotations

import pytest

from ctx.hook import _tool_kind, classify

# (name, expected kind) — the real tools the guard must keep routing correctly.
TRUE_POSITIVES = [
    ("Edit", "edit"), ("MultiEdit", "edit"), ("NotebookEdit", "edit"),
    ("Write", "edit"), ("WriteFile", "edit"), ("write_file", "edit"),
    ("str_replace_editor", "edit"), ("str_replace_based_edit_tool", "edit"),
    ("create_file", "edit"), ("replace_file_content", "edit"), ("file_editor", "edit"),
    ("Bash", "command"), ("shell", "command"), ("exec", "command"),
    ("run_command", "command"),
    ("Read", "read"), ("read_file", "read"), ("ReadFile", "read"),
    ("ReadManyFiles", "read"), ("open_file", "read"), ("view_file", "read"),
    ("Grep", "search"), ("Glob", "search"), ("grep", "search"), ("glob", "search"),
    ("list_dir", "search"), ("find_by_name", "search"),
    # grep/glob variants must route to search (the old substring caught these);
    # matched by whole-word suffix, so `ripgrep` yes, `telegraph` no.
    ("ripgrep", "search"), ("ripgrep_search", "search"), ("RipgrepSearch", "search"),
]

# Names that merely CONTAIN a keyword as a substring — must NOT be classified.
FALSE_POSITIVES = [
    "credit_check",   # contains "edit"
    "playlist", "blocklist", "checklist", "allowlist_manager",  # contain "list"
    "listener",       # starts with "list"
    "thread_reply",   # contains "read"
    "telegraph",      # contains "graph", not a grep suffix
    "get_weather", "slack_post_message", "fetch_url",  # unrelated
]


@pytest.mark.parametrize("name,kind", TRUE_POSITIVES)
def test_real_tools_keep_their_kind(name, kind):
    assert _tool_kind(name) == kind


@pytest.mark.parametrize("name", FALSE_POSITIVES)
def test_bystander_tools_are_not_misrouted(name):
    assert _tool_kind(name) is None


def test_priority_order_edit_before_command():
    # An ambiguous name resolves by the documented priority (edit wins), exactly
    # as the old ordered if-chain did.
    assert _tool_kind("edit_command") == "edit"


def test_misrouted_tool_input_is_no_longer_mutated():
    """End-to-end: a bystander tool named `playlist` used to route to the native
    search classifier and could get its input rewritten. It must now pass clean."""
    decision = classify({"tool_name": "playlist", "tool_input": {"query": "jazz"}})
    assert decision.get("decision") == "allow"
    assert "_rewrite" not in decision
    assert "updatedInput" not in decision
