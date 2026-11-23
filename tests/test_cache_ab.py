"""Model-free integration checks for the coding experiment's real local tools.

The scripted provider below is a test double, not evidence of model task success.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import importlib.metadata
import json
import time

import pytest

from evals import cache_ab as ab
from evals.cache_ab_accounting import Ledger
from evals.cache_ab_tasks import build_fixture


def require_tokenizer():
    pytest.importorskip("tiktoken")
    if importlib.metadata.version("tiktoken") != "0.11.0":
        pytest.skip("this integration metric uses the protocol's pinned tokenizer")


class ScriptedProvider:
    def __init__(self, replies):
        self.replies = replies
        self.bodies = []
        self.counts = 0

    def count(self, body, timeout):
        self.counts += 1
        assert 0 < timeout <= 60
        return 600

    def generate(self, body, timeout, raw_path):
        assert 0 < timeout <= 60
        blocks, reason = self.replies[len(self.bodies)]
        self.bodies.append(copy.deepcopy(body))
        return {"message": {
            "model": ab.MODEL, "content": copy.deepcopy(blocks), "stop_reason": reason,
            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 300,
                      "cache_read_input_tokens": 200, "output_tokens": 20},
        }, "request_id": "test-only", "wall_s": 0.1, "ttfb_s": 0.01, "first_content_s": 0.02}


def call(identifier, name, arguments):
    return {"type": "tool_use", "id": identifier, "name": name, "input": arguments}


def tiny_plan(tmp_path):
    fixture = build_fixture("quiet_flood", tmp_path / "reference")
    return fixture, {"tasks": [{"name": fixture.name, "manifest": fixture.manifest()}]}


@pytest.mark.parametrize("arm", ab.ARMS)
def test_coding_cell_runs_real_tools_and_independent_referee(tmp_path, arm):
    require_tokenizer()
    fixture, plan = tiny_plan(tmp_path)
    fixed = fixture.initial["app/router.py"].decode().replace("'apac': 'primary-apac'", "'apac': 'fallback-apac'")
    exploration = ("ctx def repo:app/router.py:route" if arm in ("explore", "full")
                   else "cat app/router.py")
    client = ScriptedProvider([
        ([call("diagnostic", "shell", {"command": "python3 diagnose.py"})], "tool_use"),
        ([call("explore", "shell", {"command": exploration})], "tool_use"),
        ([call("edit", "write_file", {"path": "app/router.py", "content": fixed}),
          call("verify", "shell", {"command": "python3 tests/verify.py"})], "tool_use"),
        ([{"type": "text", "text": "Fixed the apac route."}], "end_turn"),
    ])
    ledger = Ledger(Decimal("15"))
    spec = {"task": "quiet_flood", "repeat": 0, "arm": arm, "block": 0, "position": 0}
    directory = tmp_path / "cell"
    result = ab.coding_cell(spec, plan, client, ledger, directory, "test-only-namespace")

    assert result["status"] == "completed"
    assert result["complete"] and result["accepted"] and result["metrics_complete"]
    assert result["referee"]["accepted"]
    assert client.counts == len(client.bodies) == 4
    assert result["metrics"]["tool_calls"] == 4
    assert result["metrics"]["total_input_tokens"] == 2400
    assert result["metrics"]["output_tokens"] == 80
    assert Decimal(result["metrics"]["confirmed_cost_usd"]) == Decimal("0.00238")
    assert ledger.known_cost == Decimal("0.00238")
    assert ledger.reserved == 0
    tools = json.loads((directory / "tools.json").read_text())
    assert all(not record["error"] for record in tools)
    assert tools[0]["raw_bytes"] > 100_000
    assert "captured_ref" in tools[0] if arm in ("contain", "full") else "captured_ref" not in tools[0]
    # The final request actually contains the prior source edit and test result.
    final_request = client.bodies[-1]
    assert any(block.get("tool_use_id") == "verify" for block in final_request["messages"][-1]["content"])
    assert final_request["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert json.loads((directory / "result.json").read_text())["accepted"] is True


def test_model_claiming_success_does_not_override_referee(tmp_path):
    require_tokenizer()
    _, plan = tiny_plan(tmp_path)
    client = ScriptedProvider([([{"type": "text", "text": "All tests pass; fixed."}], "end_turn")])
    result = ab.coding_cell({"task": "quiet_flood", "repeat": 0, "arm": "native", "block": 0},
                            plan, client, Ledger(Decimal("15")), tmp_path / "cell", "test")
    assert result["complete"] is True
    assert result["accepted"] is False
    assert result["metrics"]["requests"] == 1


def test_preflight_exception_creates_no_reservation_and_sends_no_inference(tmp_path):
    class FailedCounter:
        def count(self, body, timeout):
            raise OSError("count endpoint unavailable")
        def generate(self, *args):
            pytest.fail("preflight failure must prevent inference")
    ledger = Ledger(Decimal("15"))
    body = ab.body_for([{"role": "user", "content": "test"}], [], None, 64)
    with pytest.raises(OSError, match="count endpoint"):
        ab.paid_request(FailedCounter(), ledger, body, tmp_path / "request", time.monotonic() + 10)
    assert ledger.snapshot()["requests"] == []
    assert ledger.known_cost == 0


def test_study_stops_after_preflight_failure_without_trying_next_cell(monkeypatch, tmp_path):
    require_tokenizer()
    _, plan = tiny_plan(tmp_path)
    schedule = [
        {"task": "quiet_flood", "repeat": 0, "arm": arm, "block": 0, "position": index}
        for index, arm in enumerate(ab.ARMS)
    ]
    plan.update({"schema": "test", "budget": {"cap_usd": "15"},
                 "coding_schedule": schedule, "cache_probe_schedule": []})
    attempts = []
    class FailedCounter:
        def __init__(self, api_key):
            pass
        def count(self, body, timeout):
            attempts.append(body)
            raise OSError("count endpoint unavailable")
        def generate(self, *args):
            pytest.fail("preflight failure must prevent inference")
    monkeypatch.setattr(ab, "Anthropic", FailedCounter)
    monkeypatch.setattr(ab, "validate_plan", lambda *args: None)
    result = ab.run_live(plan, "test-only", True, tmp_path / "study")
    assert len(attempts) == 1
    assert result["coding"][0]["status"] == "execution_error"
    assert all(row["status"] == "not_started" for row in result["coding"][1:])
    assert result["ledger"]["requests"] == []
