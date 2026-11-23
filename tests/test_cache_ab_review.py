"""Independent regression checks for approval, billing, and comparison validity.

All provider interactions here are in-memory fakes. No credential or network is
needed; these checks must also run in the minimal CI job.
"""
from __future__ import annotations

import copy
import io
import itertools
import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from evals import cache_ab as ab
from evals.cache_ab_accounting import BudgetExceeded, Ledger, LedgerBlocked


def provider_usage(**changes):
    result = {"input_tokens": 17, "cache_creation_input_tokens": 20,
              "cache_read_input_tokens": 30, "output_tokens": 9}
    result.update(changes)
    return result


def stream_events(*, stop_reason="end_turn", tool_json=None):
    result = [{"type": "message_start", "message": {
        "id": "msg_fake", "model": ab.MODEL, "type": "message", "role": "assistant",
        "content": [], "stop_reason": None, "usage": provider_usage(output_tokens=1),
    }}]
    if tool_json is None:
        result.extend([
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        ])
    else:
        result.extend([
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "tool_fake", "name": "shell", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": tool_json}},
        ])
    result.extend([
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 4}},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"},
    ])
    return result


def sse(events):
    return "".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n" for item in events)


def no_provider(*args, **kwargs):
    pytest.fail("an unapproved or offline path constructed a provider client")


def test_plan_cli_is_model_free(monkeypatch, tmp_path):
    monkeypatch.setattr(ab, "Anthropic", no_provider)
    target = tmp_path / "plan.json"
    assert ab.main(["plan", "--repeats", "1", "--out", str(target)]) == 0
    plan = json.loads(target.read_text())
    assert len(plan["coding_schedule"]) == 20
    assert len(plan["cache_probe_schedule"]) == 6
    assert plan["budget"]["estimate_usd"] == "2.35692"
    assert all(isinstance(task["manifest"], dict) for task in plan["tasks"])


def test_offline_cli_has_no_provider_client(monkeypatch, tmp_path):
    from evals import cache_ab_offline

    monkeypatch.setattr(ab, "Anthropic", no_provider)
    monkeypatch.setattr(cache_ab_offline, "run_offline", lambda out: {"provider_calls": 0})
    out = tmp_path / "offline"
    assert ab.main(["offline", "--out", str(out)]) == 0
    assert json.loads((out / "receipt.json").read_text())["provider_calls"] == 0


def test_live_requires_spend_flag_before_any_side_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(ab, "Anthropic", no_provider)
    monkeypatch.setattr(ab, "validate_plan", no_provider)
    out = tmp_path / "forbidden"
    with pytest.raises(ValueError, match="accept-spend"):
        ab.run_live({}, "not-approved", False, out)
    assert not out.exists()


def test_wrong_hash_never_constructs_provider_or_creates_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ab, "Anthropic", no_provider)
    plan = ab.make_plan(1)
    out = tmp_path / "forbidden"
    with pytest.raises(ValueError, match="hash"):
        ab.run_live(plan, "0" * 64, True, out)
    assert not out.exists()


def test_rehashing_modified_settings_cannot_replace_the_executable_protocol():
    plan = copy.deepcopy(ab.make_plan(1))
    plan["settings"]["max_calls_per_task"] = 999
    plan.pop("approval_sha256")
    plan["approval_sha256"] = ab.sha(ab.canonical(plan))
    with pytest.raises(ValueError, match="changed"):
        ab.validate_plan(plan, plan["approval_sha256"])


def test_changed_source_invalidates_previously_approved_plan(monkeypatch):
    plan = ab.make_plan(1)
    monkeypatch.setattr(ab, "source_identity", lambda: {"sha256": "changed", "files": {}})
    with pytest.raises(ValueError, match="changed"):
        ab.validate_plan(plan, plan["approval_sha256"])


def test_changed_runtime_invalidates_previously_approved_plan(monkeypatch):
    plan = ab.make_plan(1)
    altered = copy.deepcopy(plan["runtime"])
    altered["python"] = "a different Python interpreter"
    monkeypatch.setattr(ab, "runtime_identity", lambda: altered)
    with pytest.raises(ValueError, match="changed"):
        ab.validate_plan(plan, plan["approval_sha256"])


def test_schedule_matches_every_fixture_repeat_and_counterbalances_positions():
    plan = ab.make_plan(3)
    schedule = plan["coding_schedule"]
    assert len(schedule) == 60
    assert len({(x["task"], x["repeat"], x["arm"]) for x in schedule}) == 60
    for task in plan["tasks"]:
        for repeat in range(3):
            assert {x["arm"] for x in schedule if x["task"] == task["name"] and x["repeat"] == repeat} == set(ab.ARMS)
    for arm in ab.ARMS:
        counts = [sum(x["arm"] == arm and x["position"] == position for x in schedule) for position in range(4)]
        assert sorted(counts) == [3, 4, 4, 4]
    assert plan["budget"]["estimate_usd"] == "7.07076"


def test_stream_uses_final_cumulative_counters():
    result = ab.parse_stream(sse(stream_events()))
    assert result["usage"]["output_tokens"] == 9
    assert result["usage"]["input_tokens"] == 17
    assert result["content"] == [{"type": "text", "text": "OK"}]


def test_rolling_cache_marker_does_not_mutate_or_accumulate_in_history():
    messages = [{"role": "user", "content": "repair the source"}]
    original = copy.deepcopy(messages)
    system = [{"type": "text", "text": "policy", "cache_control": {"type": "ephemeral", "ttl": "5m"}}]
    first = ab.coding_body(messages, system, [])
    assert first["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert messages == original
    messages.extend([
        {"role": "assistant", "content": [{"type": "tool_use", "id": "fake", "name": "shell", "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "fake", "content": "app/"}]},
    ])
    second = ab.coding_body(messages, system, [])
    assert second["messages"][0]["content"] == "repair the source"
    assert second["messages"][-1]["content"][-1]["cache_control"]["ttl"] == "5m"
    assert "cache_control" not in messages[-1]["content"][-1]
    assert second["system"] == system


def test_provider_request_is_pinned_to_reviewed_sampling_and_service_tier():
    body = request_body()
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["temperature"] == 0
    assert body["thinking"] == {"type": "disabled"}
    assert body["service_tier"] == "standard_only"
    assert body["stream"] is True
    assert ab.SETTINGS["api_version"] == "2023-06-01"
    assert not {"speed", "inference_geo", "context_management"} & set(body)


@pytest.mark.parametrize("mutation", ["no_stop", "no_final_usage", "wrong_model", "provider_error"])
def test_incomplete_or_wrong_provider_stream_has_unknown_cost(mutation):
    events = stream_events()
    if mutation == "no_stop":
        events.pop()
    elif mutation == "no_final_usage":
        for event in events:
            if event["type"] == "message_delta":
                event["usage"] = {}
    elif mutation == "wrong_model":
        events[0]["message"]["model"] = "unapproved-model"
    else:
        events.insert(-1, {"type": "error", "error": {"type": "overloaded_error"}})
    with pytest.raises(ValueError):
        ab.parse_stream(sse(events))


def test_truncated_tool_json_keeps_final_usage_without_executable_input():
    partial = '{"command":"cat app/'
    result = ab.parse_stream(sse(stream_events(stop_reason="max_tokens", tool_json=partial)))
    assert result["usage"]["output_tokens"] == 9
    assert result["stop_reason"] == "max_tokens"
    assert result["content"][0]["input"] is None
    assert result["content"][0]["incomplete_json"] == partial


class FakeClient:
    def __init__(self, *, count=123, fail=False):
        self.estimate = count
        self.fail = fail
        self.counts = self.generations = 0

    def count(self, body, timeout):
        self.counts += 1
        return self.estimate

    def generate(self, body, timeout, raw_path):
        self.generations += 1
        if self.fail:
            raise TimeoutError("stream interrupted")
        return {"message": ab.parse_stream(sse(stream_events())), "wall_s": 0.1,
                "ttfb_s": 0.02, "first_content_s": 0.03, "request_id": "fake"}


def request_body():
    return ab.body_for([{"role": "user", "content": "hello"}], [], None, 2048)


def test_preflight_limit_never_sends_inference_or_reserves_budget(tmp_path):
    client = FakeClient(count=64_001)
    ledger = Ledger(Decimal("15"))
    with pytest.raises(ValueError, match="input token"):
        ab.paid_request(client, ledger, request_body(), tmp_path / "request", time.monotonic() + 10)
    assert client.counts == 1
    assert client.generations == 0
    assert ledger.snapshot()["requests"] == []


def test_budget_failure_is_before_inference(tmp_path):
    client = FakeClient()
    ledger = Ledger(Decimal("0.01"))
    with pytest.raises(BudgetExceeded):
        ab.paid_request(client, ledger, request_body(), tmp_path / "request", time.monotonic() + 10)
    assert client.generations == 0


def test_unknown_provider_charge_preserves_reservation_and_refuses_next_inference(tmp_path):
    client = FakeClient(fail=True)
    ledger = Ledger(Decimal("15"))
    with pytest.raises(LedgerBlocked):
        ab.paid_request(client, ledger, request_body(), tmp_path / "first", time.monotonic() + 10)
    assert ledger.blocked
    assert ledger.reserved == Decimal("0.26024")
    assert json.loads((tmp_path / "first/failure.json").read_text())["ledger"]["unknown_requests"] == 1
    with pytest.raises(LedgerBlocked):
        ab.paid_request(client, ledger, request_body(), tmp_path / "second", time.monotonic() + 10)
    assert client.generations == 1


def test_truncated_tool_response_is_not_executed(monkeypatch, tmp_path):
    from evals import cache_ab_tasks, cache_ab_tools

    fixture = SimpleNamespace(prompt="repair", manifest=lambda: {"task": "tiny"})
    monkeypatch.setattr(cache_ab_tasks, "build_fixture", lambda *args: fixture)
    monkeypatch.setattr(cache_ab_tasks, "grade", lambda fixture: {"accepted": False})
    class Tools:
        def __init__(self, *args, **kwargs):
            pass
        def execute(self, *args, **kwargs):
            pytest.fail("a max_tokens response must not execute a partial command")
    monkeypatch.setattr(cache_ab_tools, "ToolExecutor", Tools)
    response = {
        "message": ab.parse_stream(sse(stream_events(stop_reason="max_tokens", tool_json='{"command":'))),
        "usage": {**provider_usage(), "total_input_tokens": 67},
        "cost_usd": "0.00009", "wall_s": 0.1, "ttfb_s": 0.01, "first_content_s": 0.02,
    }
    monkeypatch.setattr(ab, "paid_request", lambda *args, **kwargs: response)
    plan = {"tasks": [{"name": "tiny", "manifest": fixture.manifest()}]}
    result = ab.coding_cell({"task": "tiny", "repeat": 0, "arm": "native", "block": 0},
                            plan, None, Ledger(Decimal("15")), tmp_path / "cell", "nonce")
    assert result["status"] == "output_limit"
    assert result["metrics"]["output_tokens"] == 9
    assert result["metrics"]["confirmed_cost_usd"] == "0.00009"
    assert result["complete"] is False


def fake_live_plan():
    return {"schema": "test", "budget": {"cap_usd": "15"}, "coding_schedule": [],
            "cache_probe_schedule": [
                {"repeat": 0, "layout": "stable_first", "query": 0},
                {"repeat": 0, "layout": "dynamic_first", "query": 0},
            ]}


def test_invocations_get_new_nonce_and_probe_layouts_use_identical_content(monkeypatch, tmp_path):
    monkeypatch.setattr(ab, "validate_plan", lambda *args: None)
    monkeypatch.setattr(ab, "Anthropic", lambda key: object())
    bodies = []
    def paid(*args, **kwargs):
        bodies.append(copy.deepcopy(args[2]))
        return {"usage": provider_usage(cache_read_input_tokens=0),
                "cost_usd": "0.00009", "wall_s": 0.1, "first_content_s": 0.02}
    monkeypatch.setattr(ab, "paid_request", paid)
    plan = fake_live_plan()
    first = ab.run_live(plan, "fake-approved", True, tmp_path / "first")
    second = ab.run_live(plan, "fake-approved", True, tmp_path / "second")
    assert first["invocation_nonce"] != second["invocation_nonce"]
    for start in (0, 2):
        a, b = bodies[start:start + 2]
        assert a["messages"][0]["content"] == list(reversed(b["messages"][0]["content"]))
    assert bodies[0]["messages"] != bodies[2]["messages"]


def test_unknown_charge_stops_remaining_cells_and_keeps_planned_denominator(monkeypatch, tmp_path):
    monkeypatch.setattr(ab, "validate_plan", lambda *args: None)
    monkeypatch.setattr(ab, "Anthropic", lambda key: object())
    plan = fake_live_plan()
    plan["coding_schedule"] = [
        {"task": "task", "repeat": 0, "arm": "native", "block": 0, "position": 0},
        {"task": "task", "repeat": 0, "arm": "full", "block": 0, "position": 1},
    ]
    calls = []
    def cell(spec, *args):
        calls.append(spec)
        return {**spec, "status": "unknown_charge", "accepted": False, "complete": False}
    monkeypatch.setattr(ab, "coding_cell", cell)
    run = ab.run_live(plan, "fake-approved", True, tmp_path / "run")
    assert len(calls) == 1
    assert run["coding"][1]["status"] == "not_started"
    assert all(p["status"] == "not_started" for p in run["cache_probes"])
    assert run["summary"]["arms"]["full"]["planned"] == 1
    assert run["summary"]["arms"]["full"]["observed"] == 0


def cell_row(task, arm, *, accepted, complete=True, metrics_complete=True, cost="0.1", tokens=100, wall=1.0):
    return {"task": task, "repeat": 0, "arm": arm, "accepted": accepted,
            "complete": complete, "metrics_complete": metrics_complete, "wall_s": wall,
            "metrics": {"confirmed_cost_usd": cost, "total_input_tokens": tokens}}


def test_aggregation_includes_failed_cost_and_censors_incomplete_pairs():
    rows = [
        cell_row("one", "native", accepted=True, cost="0.1"),
        cell_row("two", "native", accepted=False, cost="0.2"),
        cell_row("one", "explore", accepted=False, cost="0.12", tokens=80),
        cell_row("one", "full", accepted=False, complete=False, metrics_complete=False, cost="0.03"),
        {"task": "one", "repeat": 0, "arm": "contain", "status": "not_started", "accepted": None},
    ]
    result = ab.summarize(rows)
    native = result["arms"]["native"]
    assert native["cost_per_accepted_usd"] == "0.3"
    assert native["success_lower_bound_planned_denominator"] == 0.5
    assert native["success_rate_started_denominator"] == 0.5
    full = result["arms"]["full"]
    assert full["confirmed_cost_lower_bound_usd"] == "0.03"
    assert full["median_complete_wall_s"] is None
    assert full["cost_per_accepted_usd"] is None
    pairs = {r["contrast"]: r for r in result["paired_contrasts"] if r["task"] == "one"}
    assert pairs["explore-native"]["input_tokens_delta"] == -20
    assert pairs["explore-native"]["treatment_accepted"] is False
    assert pairs["full-native"]["complete_pair"] is False
    assert "cost_delta_usd" not in pairs["full-native"]
    assert pairs["contain-native"]["complete_pair"] is False


def test_zero_accepted_tasks_has_undefined_cost_per_success():
    result = ab.summarize([cell_row("one", "native", accepted=False)])
    assert result["arms"]["native"]["cost_per_accepted_usd"] is None


def test_receipt_write_failure_preserves_known_charge_and_stops_study(monkeypatch, tmp_path):
    from evals import cache_ab_tasks, cache_ab_tools

    fixture = SimpleNamespace(prompt="repair", manifest=lambda: {"task": "tiny"})
    monkeypatch.setattr(cache_ab_tasks, "build_fixture", lambda *args: fixture)
    monkeypatch.setattr(cache_ab_tasks, "grade", lambda fixture: {"accepted": False})
    monkeypatch.setattr(cache_ab_tools, "ToolExecutor", lambda *args, **kwargs: object())
    monkeypatch.setattr(ab, "validate_plan", lambda *args: None)
    client = FakeClient()
    monkeypatch.setattr(ab, "Anthropic", lambda key: client)
    original_write = ab.write_json

    def selective_failure(path, value):
        if path.name == "response.json":
            raise OSError("simulated selective response write failure")
        original_write(path, value)

    monkeypatch.setattr(ab, "write_json", selective_failure)
    plan = fake_live_plan()
    plan["tasks"] = [{"name": "tiny", "manifest": fixture.manifest()}]
    plan["coding_schedule"] = [
        {"task": "tiny", "repeat": 0, "arm": "native", "block": 0, "position": 0},
        {"task": "tiny", "repeat": 0, "arm": "full", "block": 0, "position": 1},
    ]
    run = ab.run_live(plan, "fake-approved", True, tmp_path / "run")
    first = run["coding"][0]
    assert first["status"] == "evidence_persistence_error"
    assert first["complete"] is False
    assert first["metrics_complete"] is False
    assert first["metrics"]["confirmed_cost_usd"] == "0.00009"
    assert first["metrics"]["total_input_tokens"] == 67
    assert first["metrics"]["output_tokens"] == 9
    assert run["ledger"]["known_cost_usd"] == "0.00009"
    assert run["ledger"]["reserved_usd"] == "0"
    assert run["ledger"]["unknown_requests"] == 0
    assert run["coding"][1]["status"] == "not_started"
    assert all(row["status"] == "not_started" for row in run["cache_probes"])
    assert client.counts == client.generations == 1
    persisted = json.loads((tmp_path / "run/results.json").read_text())
    assert persisted["stop_reason"] == "evidence_persistence_error"
    assert persisted["summary"]["arms"]["native"]["confirmed_cost_lower_bound_usd"] == "0.00009"


class HTTPResponse(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def getheader(self, name):
        return "request-fake" if name == "request-id" else None


class HTTPConnection:
    def __init__(self, response):
        self.response = response
        self.sock = self
        self.timeouts = []
        self.calls = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self):
        self.calls.append("connect")

    def request(self, method, path, body, headers):
        self.calls.append((method, path, body, headers))

    def getresponse(self):
        self.calls.append("getresponse")
        return self.response

    def close(self):
        self.closed = True


def test_mock_http_stream_preserves_first_byte_and_measures_it_before_content(monkeypatch, tmp_path):
    raw = sse(stream_events()).encode()
    response = HTTPResponse(raw)
    connection = HTTPConnection(response)
    destinations = []

    def constructor(host, timeout):
        destinations.append(host)
        return connection

    monkeypatch.setattr(ab.http.client, "HTTPSConnection", constructor)
    ticks = itertools.count(100_000)
    monkeypatch.setattr(ab.time, "monotonic", lambda: next(ticks) / 100)
    result = ab.Anthropic("fake-key-for-test").generate(request_body(), 10, tmp_path / "stream.sse")
    assert destinations == ["api.anthropic.com"]
    assert response.read_sizes == [1]
    assert (tmp_path / "stream.sse").read_bytes() == raw
    assert result["message"]["usage"]["output_tokens"] == 9
    assert result["message"]["content"][0]["text"] == "OK"
    assert 0 < result["headers_s"] < result["ttfb_s"] < result["first_content_s"] < result["wall_s"]
    assert result["request_id"] == "request-fake"
    assert connection.closed is True
    assert connection.calls[0] == "connect"
    method, path, body, headers = connection.calls[1]
    assert (method, path) == ("POST", "/v1/messages")
    assert json.loads(body) == request_body()
    assert headers["anthropic-version"] == "2023-06-01"
    assert all(0 < timeout <= 10 for timeout in connection.timeouts)
    assert connection.timeouts == sorted(connection.timeouts, reverse=True)


def test_mock_count_http_sends_only_countable_fields_and_closes(monkeypatch):
    connection = HTTPConnection(HTTPResponse(b'{"input_tokens": 123}'))
    monkeypatch.setattr(ab.http.client, "HTTPSConnection", lambda *args, **kwargs: connection)
    assert ab.Anthropic("fake-key-for-test").count(request_body(), 10) == 123
    method, path, body, headers = connection.calls[1]
    assert (method, path) == ("POST", "/v1/messages/count_tokens")
    assert set(json.loads(body)) == {"model", "system", "messages"}
    assert connection.closed is True
