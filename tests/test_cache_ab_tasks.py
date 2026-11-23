from pathlib import Path

import pytest

from evals.cache_ab_tasks import TASKS, apply_gold, build_fixture, grade, integrity_errors


@pytest.mark.parametrize("name", TASKS)
def test_frozen_referee_rejects_original_accepts_gold(tmp_path, name):
    fixture = build_fixture(name, tmp_path / "candidate")
    manifest = fixture.manifest()
    assert not grade(fixture)["accepted"]
    apply_gold(fixture)
    assert grade(fixture)["accepted"]
    assert fixture.manifest() == manifest
    assert not integrity_errors(fixture)


@pytest.mark.parametrize("target", ["tests/verify.py", "diagnose.py", "ctx.toml"])
def test_immutable_files_cannot_be_used_to_cheat(tmp_path, target):
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    apply_gold(fixture)
    (fixture.root / target).write_text("print('OK')\n")
    result = grade(fixture)
    assert not result["accepted"]
    assert "immutable" in result["details"][0]


def test_grader_ignores_pythonoptimize_and_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONOPTIMIZE", "2")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    assert not grade(fixture)["accepted"]


def test_os_exit_zero_is_not_success(tmp_path):
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    (fixture.root / "app/router.py").write_text("import os\nos._exit(0)\n")
    assert not grade(fixture)["accepted"]


def test_referee_checks_behavior_beyond_public_examples(tmp_path):
    fixture = build_fixture("traceback", tmp_path / "candidate")
    (fixture.root / "pipeline.py").write_text(
        "def normalize(cfg):\n    return 6 if cfg.get('ratio') == 3 else 2.0\n"
        "def load(cfg):\n    return normalize(cfg)\n")
    assert not grade(fixture)["accepted"]


def test_source_symlink_is_rejected_even_inside_fixture(tmp_path):
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    source = fixture.root / "app/router.py"
    source.unlink()
    source.symlink_to(fixture.root / "tests/verify.py")
    assert not grade(fixture)["accepted"]


def test_added_file_is_rejected(tmp_path):
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    apply_gold(fixture)
    (fixture.root / "sitecustomize.py").write_text("pass\n")
    assert not grade(fixture)["accepted"]


def test_initial_bytes_cannot_be_modified(tmp_path):
    fixture = build_fixture("quiet_flood", tmp_path / "candidate")
    with pytest.raises(TypeError):
        fixture.initial["tests/verify.py"] = b"print('OK')\n"
    with pytest.raises(ValueError, match="empty"):
        build_fixture("quiet_flood", fixture.root)
