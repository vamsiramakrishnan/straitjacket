"""`q` on the bounded MCP tier.

The composition algebra shipped CLI-only, on the stated grounds that MCP
wiring would churn the prefix asset. The cost of that deferral was that the
sharpest turn-compressing surface in the harness — locate → narrow → read in
ONE call instead of three round-trips — was reachable only by shelling out,
while the bounded tier got the heavier `investigate` plan interface instead.

Totality is the whole argument for why it is safe here and `ctx py` is not:
no loops, no recursion, a hard 8-stage cap, and every stage's cost statically
boundable. These tests pin the three properties that keep it bounded-only.
"""

from __future__ import annotations

import json

import pytest

from ctx.mcp import TOOL_SCHEMA, _dispatch


def test_q_is_a_published_op():
    """Advertised, not just implemented — a working op nobody can discover
    is the same as no op."""
    assert "q" in TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"]
    assert "options.pipeline" in TOOL_SCHEMA["inputSchema"]["properties"]["op"]["description"]
    assert "{pipeline}" in TOOL_SCHEMA["inputSchema"]["properties"]["options"]["description"]


def test_the_surface_is_still_exactly_one_tool():
    """The point of `op` is that adding a capability does not add a tool.

    Wiring q must not have grown the tool count or the top-level property
    set — those are the things that churn the cached prefix on every release.
    """
    assert TOOL_SCHEMA["name"] == "ctx"
    assert set(TOOL_SCHEMA["inputSchema"]["properties"]) == {
        "op", "workspace", "ref", "patterns", "selector", "options", "maxTokens",
    }
    assert TOOL_SCHEMA["inputSchema"]["additionalProperties"] is False


def test_q_composes_in_one_call(workspace_dir, state_home, monkeypatch):
    """The behaviour that justifies the op: several stages, one round-trip."""
    (workspace_dir / "a.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n",
        encoding="utf-8",
    )
    out = _dispatch(
        {"op": "q", "workspace": str(workspace_dir), "options": {"pipeline": "corpus --ext py | count"}}
    )
    assert "[ctx q" in out
    assert "stages" in out


def test_a_missing_pipeline_is_a_typed_error(workspace_dir, state_home):
    """Not an empty result. `q` with no pipeline is a caller mistake, and a
    silent empty census would read as 'this repository has nothing'."""
    from ctx.retrieval import RetrievalError

    with pytest.raises(RetrievalError) as e:
        _dispatch({"op": "q", "workspace": str(workspace_dir), "options": {}})
    assert "options.pipeline" in str(e.value)


def test_a_malformed_pipeline_surfaces_as_an_error_not_as_content(
    workspace_dir, state_home
):
    """`run_query` renders query errors as a teaching LINE with exit code 2.

    Returned as content, that line would reach the model as a successful
    result whose body happens to describe a failure — the fail-open shape
    this codebase keeps finding. It has to raise.
    """
    from ctx.retrieval import RetrievalError

    with pytest.raises(RetrievalError) as e:
        _dispatch(
            {
                "op": "q",
                "workspace": str(workspace_dir),
                "options": {"pipeline": "bogusstage | count"},
            }
        )
    # The teaching content survives the conversion — the error is still useful.
    assert "unknown stage" in str(e.value)


def test_q_honours_maxtokens(workspace_dir, state_home):
    """The caller's cap must reach the algebra's own emission backstop.

    `run_query` bounds its render against `result_tokens`; `_dispatch`
    tightens `result_tokens` to maxTokens on a per-call copy. If that wiring
    ever breaks, a q result becomes the one unbounded thing on a
    bounded-by-construction tier.
    """
    for i in range(40):
        (workspace_dir / f"f{i}.py").write_text(
            f"# {'padding ' * 200}\ndef fn{i}():\n    return {i}\n", encoding="utf-8"
        )
    big = _dispatch(
        {"op": "q", "workspace": str(workspace_dir),
         "options": {"pipeline": "corpus --ext py | outline"}}
    )
    small = _dispatch(
        {"op": "q", "workspace": str(workspace_dir), "maxTokens": 64,
         "options": {"pipeline": "corpus --ext py | outline"}}
    )
    assert len(small) < len(big), (
        "maxTokens did not reach run_query's emission backstop"
    )


def test_execution_stays_off_this_tier():
    """`q` is total; `py` is not. The op list must never gain the latter.

    This is the line that makes the bounded tier meaningful: an algebra whose
    cost is statically boundable can live here, arbitrary code cannot.
    """
    ops = set(TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"])
    assert not ops & {"py", "eval", "run", "seq", "job", "rewrite"}, (
        "an execute-class op reached the bounded MCP surface"
    )


def test_the_schema_is_json_serialisable():
    """It is published over JSON-RPC; a non-serialisable schema breaks
    tools/list for every client at once."""
    json.dumps(TOOL_SCHEMA)
