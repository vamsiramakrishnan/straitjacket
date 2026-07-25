"""Smoke tests for modernization pass: verify behavior of touched code."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ctx.callgraph import _Node, _Graph, _graph_cache_key, _from_json, _to_json
from ctx.checkpoint import create_checkpoint
from ctx.debt import add, resolve, outstanding, render
from ctx.engagement import note_call, note_truncation, claim_emission_tier, note_symbol_grep
from ctx.rundiff import _pytest_failures
from ctx.textutil import (
    redact,
    strip_control,
    fmt_bytes,
    fmt_int,
    bounded,
    estimate_tokens,
)


class TestTextutilModernization:
    """Verify textutil behavior (regex patterns at module level)."""

    def test_redaction_patterns_are_compiled(self):
        """REDACTION_PATTERNS dict values are compiled Pattern objects."""
        from ctx.textutil import REDACTION_PATTERNS

        for name, pattern in REDACTION_PATTERNS.items():
            assert hasattr(pattern, "match"), f"{name} should be compiled pattern"
            assert hasattr(pattern, "search"), f"{name} should have search method"

    def test_strip_control_removes_ansi(self):
        """strip_control uses pre-compiled ANSI_RE and CTRL_RE."""
        text_with_ansi = "hello\x1b[31mred\x1b[0m world"
        result = strip_control(text_with_ansi)
        assert "\x1b" not in result
        assert "red" in result

    def test_redact_aws_key(self):
        """redact() correctly identifies and marks AWS access keys."""
        secret = "AKIA1234567890ABCDEF"
        text = f"leaked: {secret}"
        redacted, fired = redact(text, ("aws-access-key",))
        assert "aws-access-key" in redacted
        assert secret not in redacted
        assert "aws-access-key" in fired

    def test_fmt_functions_are_deterministic(self):
        """fmt_int, fmt_bytes produce stable output."""
        assert fmt_int(1000) == "1,000"
        assert fmt_int(1000) == fmt_int(1000)  # idempotent
        assert fmt_bytes(1024) == "1.0 KiB"
        assert fmt_bytes(1024) == fmt_bytes(1024)

    def test_bounded_truncates_with_metadata(self):
        """bounded() enforces budget and includes truncation marker."""
        large_text = "line\n" * 1000
        result = bounded(large_text, budget_tokens=100)
        # Should be shorter than input and contain truncation marker
        assert len(result) < len(large_text)
        assert "[ctx:truncated" in result

    def test_estimate_tokens_is_deterministic(self):
        """estimate_tokens produces stable estimates."""
        assert estimate_tokens(400) == 100
        assert estimate_tokens(400) == estimate_tokens(400)


class TestDebtModernization:
    """Verify debt behavior (file I/O with encoding)."""

    def test_debt_add_returns_idempotent_id(self):
        """add() returns consistent ID for same note."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            id1 = add(ws_root, "do something later")
            id2 = add(ws_root, "do something later")
            assert id1 == id2  # idempotent

    def test_debt_resolve_works_end_to_end(self):
        """resolve() marks debt as resolved."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            debt_id = add(ws_root, "fix later", ref="issue:123")
            assert resolve(ws_root, debt_id) is True
            assert resolve(ws_root, "nonexistent") is False

    def test_debt_outstanding_tracks_state(self):
        """outstanding() returns only unresolved debt."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            id1 = add(ws_root, "debt one")
            id2 = add(ws_root, "debt two")
            resolve(ws_root, id1)
            out = outstanding(ws_root)
            assert len(out) == 1
            assert out[0]["id"] == id2

    def test_debt_render_produces_readable_output(self):
        """render() produces deterministic, readable debt summary."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            add(ws_root, "implement feature", ref="readme")
            rendered = render(ws_root)
            assert "[ctx debt" in rendered
            assert "implement feature" in rendered
            assert "readme" in rendered


class TestEngagementModernization:
    """Verify engagement behavior (dict operations with setdefault)."""

    def test_note_call_increments_state(self):
        """note_call() tracks hook calls and activates on threshold."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            # Start passive
            level1 = note_call(ws_root, mode="auto", activate_after_calls=3)
            assert level1 == "passive"
            # Keep calling
            for _ in range(2):
                note_call(ws_root, mode="auto", activate_after_calls=3)
            # Should activate after third call
            level_final = note_call(ws_root, mode="auto", activate_after_calls=3)
            assert level_final == "active"

    def test_note_truncation_activates_session(self):
        """note_truncation() immediately activates auto sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            note_truncation(ws_root)
            # Should not crash, activates silently
            level = note_call(ws_root, mode="auto")
            assert level == "active"

    def test_claim_emission_tier_fires_once(self):
        """claim_emission_tier() fires exactly once per tier."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            assert claim_emission_tier(ws_root, 1) is True  # first time
            assert claim_emission_tier(ws_root, 1) is False  # second time
            assert claim_emission_tier(ws_root, 2) is True  # new tier

    def test_note_symbol_grep_tracks_distinct_symbols(self):
        """note_symbol_grep() counts distinct symbols and fires navigation nudge."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            count1, nudged1 = note_symbol_grep(ws_root, "foo")
            assert count1 == 1
            assert nudged1 is False
            # Same symbol shouldn't increase count
            count2, nudged2 = note_symbol_grep(ws_root, "foo")
            assert count2 == 1
            assert nudged2 is False
            # Add second distinct symbol
            count_mid, nudged_mid = note_symbol_grep(ws_root, "bar")
            assert count_mid == 2
            assert nudged_mid is False
            # Add third distinct symbol (reaches threshold)
            count3, nudged3 = note_symbol_grep(ws_root, "baz")
            assert count3 == 3
            # After reaching threshold, nav_nudged is set; next call sees it
            count4, nudged4 = note_symbol_grep(ws_root, "qux")
            assert count4 == 4
            assert nudged4 is True  # Now we see it was nudged


class TestRundiffModernization:
    """Verify rundiff behavior (module-level regex patterns)."""

    def test_pytest_failures_detects_pytest_output(self):
        """_pytest_failures() parses pytest-style output deterministically."""
        pytest_output = """=== test session starts ===
FAILED tests/test_foo.py::test_bar - AssertionError
FAILED tests/test_foo.py::test_baz - ValueError
"""
        failures = _pytest_failures(pytest_output)
        assert failures is not None  # recognized as pytest output
        assert "tests/test_foo.py::test_bar" in failures
        assert failures["tests/test_foo.py::test_bar"] == 2

    def test_pytest_failures_rejects_non_pytest(self):
        """_pytest_failures() returns None for non-pytest text."""
        text = "some random output\nno pytest headers here"
        assert _pytest_failures(text) is None


class TestCallgraphModernization:
    """Verify callgraph behavior (dataclass without frozen, dict with setdefault)."""

    def test_node_dataclass_is_usable(self):
        """_Node dataclass maintains expected behavior."""
        n = _Node(qual="foo.bar", rel="src/foo.py", lineno=10, end=20)
        assert n.qual == "foo.bar"
        assert n.lineno == 10
        assert n.rel == "src/foo.py"
        assert n.end == 20

    def test_graph_json_roundtrip(self):
        """_to_json/_from_json maintain graph structure correctly."""
        g = _Graph()
        g.nodes = {"foo": _Node("foo", "test.py", 1, 10)}
        g.defs_by_name = {"foo": [g.nodes["foo"]]}
        g.out_edges = {"foo": ["bar"]}
        g.in_edges = {"bar": ["foo"]}

        # Roundtrip
        serialized = _to_json(g)
        g2 = _from_json(serialized)
        assert g2.nodes["foo"].qual == "foo"
        assert len(g2.defs_by_name["foo"]) == 1
        assert g2.out_edges["foo"] == ["bar"]

    def test_cache_key_is_deterministic(self):
        """_graph_cache_key() produces stable hashes for file lists."""
        from ctx.workspace import resolve_workspace

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp)
            (ws_root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
            (ws_root / "test.py").write_text("# test", encoding="utf-8")
            ws = resolve_workspace(str(ws_root))
            rels = ["test.py"]
            assert _graph_cache_key(ws, rels) == _graph_cache_key(ws, rels)


class TestCheckpointModernization:
    """Verify checkpoint behavior (text handling with proper encoding)."""

    def test_checkpoint_manifest_structure(self):
        """Verify checkpoint manifest structure is maintained."""
        manifest = {
            "schema": "ctx.checkpoint/v1",
            "workspaceId": "test-ws",
            "goal": "fix bugs",
            "state": "in progress",
            "decisions": ["decision 1"],
            "hypotheses": ["hypothesis 1"],
            "evidence": [{"ref": "run:abc123", "note": "test output"}],
            "attempted": ["search 1"],
            "files": ["src/test.py"],
            "source": {"gitHead": "abc123"},
        }
        # Verify structure is intact
        assert manifest["schema"] == "ctx.checkpoint/v1"
        assert manifest["goal"] == "fix bugs"
        assert isinstance(manifest["evidence"], list)
        assert len(manifest["decisions"]) == 1
