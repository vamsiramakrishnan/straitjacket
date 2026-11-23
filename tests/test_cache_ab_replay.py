import ast
import importlib.metadata
import json
from pathlib import Path
import re

import pytest

from evals.cache_ab_offline import TOKENIZER_VERSION, sha256
from evals.cache_ab_replay import ARMS, SCRIPTS, commands_for, normalize_visible, run_replay


def test_every_arm_has_same_diagnostic_and_declared_source_coverage():
    for task, script in SCRIPTS.items():
        for arm in ARMS:
            commands = commands_for(task, arm)
            assert commands[0] == script["diagnostic"]
            for path, symbols in script["sources"].items():
                if arm in ("native", "contain"):
                    assert f"cat {path}" in commands
                else:
                    assert all(f"ctx def repo:{path}:{symbol}" in commands for symbol in symbols)
    assert len(SCRIPTS["bigtest"]["sources"]["mathlib.py"]) == 5


def test_normalization_preserves_arm_specific_evidence():
    root = Path("/tmp/cab-12345678/00/00")
    text = f"{root}/pipeline.py\nrun:123456789abc\nsnapshot:abcdef012345\nhot (recent run evidence)\n"
    actual = normalize_visible(text, root)
    assert "<fixture>/pipeline.py" in actual
    assert "hot (recent run evidence)" in actual
    assert "run:8956c843e556" in actual and "snapshot:8956c843e556" in actual
    assert normalize_visible("route('apac')", root) == "route('apac')"


def test_replay_has_twenty_auditable_nonbillable_cells(tmp_path):
    pytest.importorskip("tiktoken")
    if importlib.metadata.version("tiktoken") != TOKENIZER_VERSION:
        pytest.skip(f"replay requires tiktoken=={TOKENIZER_VERSION}")
    result = run_replay(tmp_path)
    from evals.cache_ab_tasks import build_fixture

    originals = {name: build_fixture(name, tmp_path / "originals" / name) for name in SCRIPTS}
    assert result["provider_calls"] == 0
    assert len(result["cells"]) == 20
    for row in result["cells"]:
        payload = (tmp_path / row["transcript"]).read_bytes()
        assert sha256(payload) == row["transcript_sha256"]
        trace = [json.loads(line) for line in payload.decode().splitlines()]
        assert row["visible_tokens_actual"] == sum(t["visible_tokens_actual"] for t in trace)
        assert row["visible_tokens_normalized"] == sum(t["visible_tokens_normalized"] for t in trace)
        assert row["model_task_success"] is None and row["provider_cost_usd"] is None
        assert row["tool_calls"] == len(commands_for(row["task"], row["arm"]))
        assert all(t["tool"] == "shell" for t in trace)
        # The replay's matched source-coverage claim must survive the actual
        # renderers, not merely appear in the command manifest. Preserve the
        # legitimate recent-run map differences; compare function bodies.
        visible = "\n".join(re.sub(r"^L\d+: ", "", t["result"]["text"], flags=re.M) for t in trace)
        for path, symbols in SCRIPTS[row["task"]]["sources"].items():
            source = originals[row["task"]].initial[path].decode()
            definitions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
            for symbol in symbols:
                assert ast.get_source_segment(source, definitions[symbol]) in visible
