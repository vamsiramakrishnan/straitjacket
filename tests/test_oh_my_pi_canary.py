import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.oh_my_pi_canary import _json_object, run_edit_replay, run_orchestration_replay


def test_edit_canary_is_matched_and_exercises_relocation_and_refusal():
    record = run_edit_replay()
    metrics = record["metrics"]

    assert metrics["benign_completion"] == {"naive": 0.5, "ctx": 1.0}
    assert metrics["adversarial_safety"] == {"naive": 0.0, "ctx": 1.0}
    assert record["cases"][1]["ctx"]["relocated"] is True
    assert [item["ctx"]["outcome"] for item in record["cases"][2:]] == [
        "refused",
        "refused",
    ]


def test_orchestration_canary_uses_serial_and_isolated_production_paths():
    record = run_orchestration_replay(worker_seconds=0.05)

    assert record["serial"]["success"] is True
    assert record["isolated"]["success"] is True
    assert record["serial"]["mutation_isolation"] == [
        "shared_workspace",
        "shared_workspace",
    ]
    assert record["isolated"]["mutation_isolation"] == [
        "git_worktree",
        "git_worktree",
    ]


def test_live_json_extraction_is_bounded_to_first_valid_object():
    assert _json_object('note\n{"replacement":"target = 2\\n"}\ntrailing') == {
        "replacement": "target = 2\n"
    }
    assert _json_object("no object") is None
