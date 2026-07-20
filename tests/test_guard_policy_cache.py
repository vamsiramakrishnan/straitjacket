"""Acceptance: the guard-policy JSON cache is a pure derivation of the TOML
sources — hits return identical policy, edits invalidate, corruption and
read-only ledgers fail open to a fresh parse."""

import json
from pathlib import Path

from ctx.hook import _LEDGER_DIR_NAME, _POLICY_CACHE_NAME, _load_guard_policy


def _write_ctx_toml(ws: Path, allow: str = "mytool status") -> None:
    ws.joinpath("ctx.toml").write_text(
        f'version = 1\n[guard]\nallow_commands = ["{allow}"]\n', encoding="utf-8"
    )


def _cache_path(ws: Path) -> Path:
    return ws / _LEDGER_DIR_NAME / _POLICY_CACHE_NAME


def test_cache_hit_returns_identical_policy(tmp_path):
    _write_ctx_toml(tmp_path)
    cold = _load_guard_policy(str(tmp_path))
    assert _cache_path(tmp_path).is_file()
    warm = _load_guard_policy(str(tmp_path))
    assert warm == cold
    assert warm["allow_commands"] == ["mytool status"]


def test_cache_invalidated_when_toml_changes(tmp_path):
    _write_ctx_toml(tmp_path, allow="old status")
    _load_guard_policy(str(tmp_path))
    _write_ctx_toml(tmp_path, allow="new status")
    pol = _load_guard_policy(str(tmp_path))
    assert pol["allow_commands"] == ["new status"]


def test_cache_invalidated_when_policy_file_appears(tmp_path):
    _write_ctx_toml(tmp_path)
    assert _load_guard_policy(str(tmp_path))["promoted_commands"] == []
    tmp_path.joinpath("ctx-policy.toml").write_text(
        'schema = "ctx.policy/v1"\n\n[[promoted]]\nsignature = "go test"\n',
        encoding="utf-8",
    )
    pol = _load_guard_policy(str(tmp_path))
    assert pol["promoted_commands"] == ["go test"]


def test_corrupt_cache_falls_back_to_parse(tmp_path):
    _write_ctx_toml(tmp_path)
    _load_guard_policy(str(tmp_path))
    _cache_path(tmp_path).write_text("{not json", encoding="utf-8")
    pol = _load_guard_policy(str(tmp_path))
    assert pol["allow_commands"] == ["mytool status"]


def test_cache_with_wrong_key_is_ignored(tmp_path):
    _write_ctx_toml(tmp_path)
    _load_guard_policy(str(tmp_path))
    doc = json.loads(_cache_path(tmp_path).read_text(encoding="utf-8"))
    doc["key"] = [["bogus", 1, 1]]
    doc["policy"] = {"allow_commands": ["poisoned"]}
    _cache_path(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    pol = _load_guard_policy(str(tmp_path))
    assert pol["allow_commands"] == ["mytool status"]


def test_no_toml_files_means_no_cache_write(tmp_path):
    pol = _load_guard_policy(str(tmp_path))
    assert pol["mode"] == "guarded"
    assert not _cache_path(tmp_path).exists()


def test_new_defaults_win_over_stale_cached_policy_keys(tmp_path):
    # A cache written by an older ctx lacks keys added later; defaults must
    # backfill them rather than KeyError at decision time.
    _write_ctx_toml(tmp_path)
    _load_guard_policy(str(tmp_path))
    doc = json.loads(_cache_path(tmp_path).read_text(encoding="utf-8"))
    del doc["policy"]["engagement_mode"]
    _cache_path(tmp_path).write_text(json.dumps(doc), encoding="utf-8")
    pol = _load_guard_policy(str(tmp_path))
    assert pol["engagement_mode"] == "auto"
    assert pol["allow_commands"] == ["mytool status"]
