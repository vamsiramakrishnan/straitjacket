import pytest
from ctx.edit_policy import choose_format


def rows(n=60):
    return [{"case": str(i), "repeat": 0, "format": fmt, "model": "m", "shape": "mechanical",
             "measurement": "live", "task_success": True, "wrong_target": False,
             "cost_usd": cost}
            for i in range(n) for fmt, cost in [("native", 1.0), ("anchored", 0.5)]]


def test_policy_requires_paired_quality_cost_and_distinct_cases():
    assert choose_format(rows(), model="m", shape="mechanical")["format"] == "anchored"
    assert choose_format(rows(10), model="m", shape="mechanical")["format"] == "native"
    assert choose_format(rows(), model="different", shape="mechanical")["format"] == "native"
    repeated = rows()
    for i, row in enumerate(repeated):
        row["case"], row["repeat"] = "same", i // 2
    assert choose_format(repeated, model="m", shape="mechanical")["format"] == "native"


@pytest.mark.parametrize("field,value", [("measurement", "fixture"), ("wrong_target", True),
    ("cost_usd", None), ("cost_usd", float("nan")), ("cost_usd", 2.0)])
def test_policy_refuses_incomplete_or_worse_evidence(field, value):
    sample = rows()
    for row in sample:
        if row["format"] == "anchored":
            row[field] = value
    assert choose_format(sample, model="m", shape="mechanical")["format"] == "native"


def test_successes_cannot_hide_paired_regressions():
    sample = rows()
    for row in sample[:20]:
        if row["format"] == "anchored":
            row["task_success"] = False
    assert choose_format(sample, model="m", shape="mechanical")["format"] == "native"
    assert choose_format(rows() + [rows()[0]], model="m", shape="mechanical")["reason"] == "invalid_or_duplicate_observations"
