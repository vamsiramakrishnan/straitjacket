"""Source-state generations (docs/EDC.md §8) — Controller State wave.

Contracts under test:

* ``ctx.execution.generation_hash`` — the untracked-content fix: an edit to
  an untracked file (size or mtime_ns) changes the generation while the
  porcelain-only ``_worktree_hash`` (manifest identity, golden — unchanged)
  does not; untracked directories are recursed; the session ledger dir is
  excluded so reflex's own writes never bump the generation; non-git roots
  and errors fail open to None.
* the confirmed/provisional rerun-classification matrix (EDC §8):
  rerun classification = signature relation × generation equality —
  equal hash → starvation CONFIRMED even when event-disarmed (the sed
  case); different hash → verification regardless of arming; unknown →
  provisional, classified by the armed bit, marked ``confirmed: false``.
  All SHADOW: the live return value / v1 ledger / densify latch are
  byte-identical to the v2-shipped behavior in every cell.
"""

import json
import os
import subprocess
from pathlib import Path

LEDGER = ".ctx-session-reads"


def _git(ws: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=ws, check=True, env=env,
                   capture_output=True)


def _v2_events(root: Path) -> list[dict]:
    path = Path(root) / LEDGER / "interventions.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _v2_outcomes(root: Path) -> list[dict]:
    return [e for e in _v2_events(root) if e.get("event") == "intervention_outcome"]


def _v1_events(root: Path) -> list[dict]:
    path = Path(root) / LEDGER / "reflex-outcomes.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(ln)
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


# ------------------------------------------------------- generation_hash


def test_generation_none_for_non_git_and_none_root(tmp_path):
    from ctx.execution import generation_hash

    assert generation_hash(tmp_path) is None  # no git repo → unknown
    assert generation_hash(None) is None


def test_untracked_content_edit_changes_generation_not_worktree_hash(git_workspace):
    """The §8.2 trap: porcelain lists ``?? tests/`` regardless of content —
    ``_worktree_hash`` (manifest identity) is blind to edits inside, and
    generation_hash must not be."""
    from conftest import make_ws
    from ctx.execution import _worktree_hash, generation_hash

    (git_workspace / "tests").mkdir()
    f = git_workspace / "tests" / "test_x.py"
    f.write_text("def test_a(): pass\n", encoding="utf-8")

    ws = make_ws(git_workspace)
    g1, w1 = generation_hash(git_workspace), _worktree_hash(ws)
    assert g1 and g1.startswith("sha256:")
    assert w1 and w1.startswith("sha256:")

    # Edit the untracked file (different size ⇒ no mtime races).
    f.write_text("def test_a(): pass\ndef test_b(): pass\n", encoding="utf-8")
    g2, w2 = generation_hash(git_workspace), _worktree_hash(ws)
    assert w2 == w1  # porcelain-only identity is blind — by design (golden)
    assert g2 != g1  # the generation sees the untracked edit


def test_mtime_only_change_changes_generation(git_workspace):
    from ctx.execution import generation_hash

    f = git_workspace / "scratch.py"
    f.write_text("x = 1\n", encoding="utf-8")
    g1 = generation_hash(git_workspace)
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert generation_hash(git_workspace) != g1  # same bytes, touched file


def test_tracked_modification_changes_generation(git_workspace):
    from ctx.execution import generation_hash

    g1 = generation_hash(git_workspace)
    (git_workspace / "hello.py").write_text("print('changed')\n", encoding="utf-8")
    assert generation_hash(git_workspace) != g1  # porcelain gains ` M hello.py`


def test_ledger_dir_writes_never_bump_generation(git_workspace):
    """Reflex/session state mutates on every scored command; if it counted,
    the generation could never confirm anything."""
    from ctx.execution import generation_hash

    ledger = git_workspace / LEDGER
    ledger.mkdir()
    (ledger / "reflex.json").write_text("{}", encoding="utf-8")
    g1 = generation_hash(git_workspace)
    (ledger / "reflex.json").write_text('{"densify": {}}', encoding="utf-8")
    (ledger / "interventions.jsonl").write_text("{}\n", encoding="utf-8")
    assert generation_hash(git_workspace) == g1


def test_generation_deterministic_without_changes(git_workspace):
    from ctx.execution import generation_hash

    (git_workspace / "tests").mkdir()
    (git_workspace / "tests" / "t.py").write_text("pass\n", encoding="utf-8")
    assert generation_hash(git_workspace) == generation_hash(git_workspace)


# ------------------------------- the confirmed/provisional matrix (shadow)


def _drive(ws: Path, *, disarm: bool, rerun_gen: str | None, base_gen="g-A"):
    """intervention(gen=base) → optional edit-disarm → rerun(gen=rerun_gen).
    Returns (live_result, v2 outcome dicts, v1 events)."""
    from ctx import reflex

    reflex.note_intervention(
        ws, "pytest tests/x.py", "abc123def456", generation=base_gen
    )
    if disarm:
        reflex.note_edit(ws)
    live = reflex.check_command(
        ws, "pytest tests/x.py --tb=short", generation=rerun_gen
    )
    return live, _v2_outcomes(ws), _v1_events(ws)


def test_matrix_equal_generation_armed_confirmed_starvation(tmp_path):
    live, outcomes, v1 = _drive(tmp_path, disarm=False, rerun_gen="g-A")
    assert live == "densify"  # live v2 behavior unchanged
    assert [e["event"] for e in v1] == ["starvation"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "equivalent_rerun"
    assert outcomes[0]["evidence"]["generation"] == "equal"
    assert outcomes[0]["evidence"]["confirmed"] is True


def test_matrix_equal_generation_disarmed_still_confirmed(tmp_path):
    """The sed case: the model mutated source without an Edit tool event —
    but content identity says nothing changed, so the rerun IS starvation.
    Live stays disarmed (shadow-first: record, don't act)."""
    live, outcomes, v1 = _drive(tmp_path, disarm=True, rerun_gen="g-A")
    assert live is None  # live event-disarm behavior unchanged
    assert v1 == []  # no v1 starvation, no densify action
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "equivalent_rerun"
    assert outcomes[0]["evidence"] == {
        "generation": "equal", "confirmed": True, "signature": "pytest tests/x.py",
    }
    from ctx import reflex

    assert reflex.densify_latched(tmp_path, "pytest tests/x.py") is False


def test_matrix_changed_generation_armed_is_verification_shadow(tmp_path):
    """Source changed but no Edit event fired (sed the other way): live
    still scores starvation (unchanged), shadow says verification."""
    live, outcomes, v1 = _drive(tmp_path, disarm=False, rerun_gen="g-B")
    assert live == "densify"  # live behavior byte-identical
    assert [e["event"] for e in v1] == ["starvation"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "validation_after_edit"
    assert outcomes[0]["evidence"]["generation"] == "changed"
    assert outcomes[0]["evidence"]["confirmed"] is True


def test_matrix_changed_generation_disarmed_is_verification(tmp_path):
    live, outcomes, _ = _drive(tmp_path, disarm=True, rerun_gen="g-B")
    assert live is None
    assert [o["outcome"] for o in outcomes] == ["validation_after_edit"]
    assert outcomes[0]["evidence"]["confirmed"] is True


def test_matrix_unknown_generation_is_provisional(tmp_path):
    """Non-git workspace: hashes are None on both sides → provisional
    classification by the armed bit, marked confirmed: false."""
    from ctx import reflex

    ws_a = tmp_path / "armed"
    ws_a.mkdir()
    reflex.note_intervention(ws_a, "pytest tests/x.py", "abc123def456")
    live = reflex.check_command(ws_a, "pytest tests/x.py | head -50")
    assert live == "densify"
    (o,) = _v2_outcomes(ws_a)
    assert o["outcome"] == "slicer_rerun"  # slicer decoration recorded
    assert o["evidence"] == {
        "generation": "unknown", "confirmed": False,
        "signature": "pytest tests/x.py",
    }

    ws_d = tmp_path / "disarmed"
    ws_d.mkdir()
    reflex.note_intervention(ws_d, "pytest tests/x.py", "abc123def456")
    reflex.note_edit(ws_d)
    assert reflex.check_command(ws_d, "pytest tests/x.py") is None
    (o,) = _v2_outcomes(ws_d)
    assert o["outcome"] == "validation_after_edit"
    assert o["evidence"]["confirmed"] is False


def test_slicer_vs_equivalent_rerun_distinguished(tmp_path):
    from ctx import reflex

    reflex.note_intervention(tmp_path, "pytest tests/x.py", "a" * 12, generation="g")
    reflex.check_command(tmp_path, "pytest tests/x.py 2>&1 | tail -50", generation="g")
    (o,) = _v2_outcomes(tmp_path)
    assert o["outcome"] == "slicer_rerun"


def test_lazy_confirmation_against_a_real_worktree(git_workspace):
    """End-to-end without explicit generations: note_intervention records
    the real hash; an unchanged worktree confirms; an untracked edit flips
    the shadow to verification."""
    from ctx import reflex

    (git_workspace / "tests").mkdir()
    f = git_workspace / "tests" / "test_x.py"
    f.write_text("def test_a(): assert False\n", encoding="utf-8")

    reflex.note_intervention(git_workspace, "pytest tests/test_x.py", "b" * 12)
    assert reflex.check_command(git_workspace, "pytest tests/test_x.py -x") == "densify"
    outcomes = _v2_outcomes(git_workspace)
    assert [o["outcome"] for o in outcomes] == ["equivalent_rerun"]
    assert outcomes[0]["evidence"] == {
        "generation": "equal", "confirmed": True,
        "signature": "pytest tests/test_x.py",
    }

    # sed-style mutation (no Edit tool event) → next cycle is verification.
    reflex.note_intervention(git_workspace, "pytest tests/test_x.py", "c" * 12)
    f.write_text("def test_a(): assert True\n", encoding="utf-8")
    assert reflex.check_command(git_workspace, "pytest tests/test_x.py") == "densify"
    outcomes = _v2_outcomes(git_workspace)
    assert [o["outcome"] for o in outcomes] == [
        "equivalent_rerun", "validation_after_edit",
    ]
    assert outcomes[1]["evidence"]["generation"] == "changed"


def test_generation_shadow_fail_open_when_ledger_unwritable(tmp_path):
    from ctx import reflex

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / LEDGER).write_text("a file, not a directory", encoding="utf-8")
    reflex.note_intervention(ws, "pytest tests/x.py", "d" * 12, generation="g")
    assert reflex.check_command(ws, "pytest tests/x.py", generation="g") is None
    assert (ws / LEDGER).read_text(encoding="utf-8") == "a file, not a directory"
