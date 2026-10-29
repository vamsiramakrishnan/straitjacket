from evals.edit_matrix import fixture, run_matrix
from ctx.edit_policy import choose_format


def test_fixture_receipt_checks_behavior_but_cannot_select_a_policy(state_home):
    cases, adapters = fixture()
    rows = list(run_matrix(cases, adapters, model="fixture", measurement="fixture"))
    assert len(rows) == 12 and all(r["task_success"] for r in rows)
    assert all(r["cost_usd"] is None for r in rows)
    assert choose_format(rows, model="fixture", shape="mechanical")["reason"] == "live_evidence_required"


def test_adapter_success_claim_does_not_override_failed_oracle(state_home):
    cases, _ = fixture()
    rows = list(run_matrix(cases[:1], {"native": lambda *args: {"task_success": True}}, model="m"))
    assert not rows[0]["task_success"]


def test_out_of_scope_edit_is_a_failure_even_when_oracle_passes(state_home):
    cases, adapters = fixture()
    real = adapters["native"]

    def wrong(ws, store, request, metrics):
        real(ws, store, request, metrics)
        (ws.root / "unrelated.py").write_text("changed = True\n")
        return {}

    row = list(run_matrix(cases[:1], {"native": wrong}, model="m"))[0]
    assert row["wrong_target"] and not row["task_success"]
