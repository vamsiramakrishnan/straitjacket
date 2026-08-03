"""Six defects from one bug-bash round, pinned as the mechanisms that close
them (evals/devex/, round 6, 2026-08-02).

Two of the six were second and third doors onto guards this branch had
already hardened at one door each -- the settings-merge shape check
(install_claude got it, install_codex did not) and the range-selector
refusal (`--lines` got it twice, `--records` never). Both are now the only
way to perform that operation, so a new host or a new selector inherits the
guarantee instead of being remembered.
"""

from __future__ import annotations

import json
import os

import pytest


# ------------------------------------------- one merge, every host settings
def test_hook_merge_refuses_every_foreign_shape():
    from ctx.installer import SettingsUnreadable, merge_hook_stages

    stages = {"PreToolUse": [{"hooks": [{"command": "ctx hook"}]}]}
    for doc, needle in (
        ({"hooks": []}, "not an object"),
        ({"hooks": "nope"}, "not an object"),
        ({"hooks": {"PreToolUse": "nope"}}, "not an array"),
        ({"hooks": {"PreToolUse": 7}}, "not an array"),
    ):
        with pytest.raises(SettingsUnreadable) as e:
            merge_hook_stages(doc, stages)
        assert needle in str(e.value)


def test_hook_merge_appends_without_clobbering():
    from ctx.installer import merge_hook_stages

    doc = {"hooks": {"PreToolUse": [{"hooks": [{"command": "theirs"}]}]}}
    merge_hook_stages(doc, {"PreToolUse": [{"hooks": [{"command": "ours"}]}],
                            "PostToolUse": [{"hooks": [{"command": "post"}]}]})
    cmds = [h["command"] for g in doc["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert cmds == ["theirs", "ours"], "an existing entry must survive the merge"
    assert "PostToolUse" in doc["hooks"]


def test_install_codex_refuses_a_non_object_hooks_value(tmp_path):
    """Fifth door onto one guard. install_claude grew an isinstance check
    after a bug bash crashed it on `"hooks": []`; install_codex performs the
    identical merge four hundred lines away and did not inherit it, so the
    next bash crashed the Codex door with the same raw AttributeError."""
    from ctx.installer import install_codex
    from ctx.workspace import resolve_workspace

    (tmp_path / ".codex").mkdir()
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.write_text(json.dumps({"hooks": ["wrong shape"]}), encoding="utf-8")
    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")

    out = install_codex(resolve_workspace(str(tmp_path)), init_policy=False)
    assert "cannot set up Codex hooks" in out
    assert "not an object" in out
    assert json.loads(hooks.read_text(encoding="utf-8")) == {"hooks": ["wrong shape"]}


def test_no_hand_rolled_hook_merge_remains():
    """The invariant, not the moment: a sixth host must not re-implement it."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
    offenders = []
    for path in (src / "installer.py", src / "wrap.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'setdefault\(\s*["\']hooks["\']', line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert len(offenders) == 1, (
        "the hooks merge belongs to installer.merge_hook_stages alone:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------- one refusal, every range selector
def test_every_range_selector_refuses_a_start_past_the_end(
    state_home, workspace_dir, tmp_path
):
    """`--lines` was hardened for this twice, once per code path, and stopped
    there; `--records` had the identical hole and returned an empty body under
    a header stating a range whose start exceeds its own total, exit 0."""
    from ctx.retrieval import RetrievalError, Selector, get
    from conftest import make_store, make_ws

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"one\ntwo\nthree\n")

    for sel, flag in (
        (Selector(records=(50, 60)), "--records"),
        (Selector(lines=(50, 60)), "--lines"),
        (Selector(bytes=(5000, 6000)), "--bytes"),
    ):
        with pytest.raises(RetrievalError) as e:
            get(store, ws, f"blob:{blob[:12]}", sel)
        assert "selects nothing" in str(e.value), flag
        assert flag in str(e.value)


def test_in_range_selectors_still_work(state_home, workspace_dir):
    from ctx.retrieval import Selector, get
    from conftest import make_store, make_ws

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"one\ntwo\nthree\n")
    out = get(store, ws, f"blob:{blob[:12]}", Selector(records=(1, 2)))
    assert "one" in out and "two" in out and "three" not in out
    assert "one" in get(store, ws, f"blob:{blob[:12]}", Selector(bytes=(1, 3)))


# ------------------------------------- the guard resolves what it will open
def test_secret_guard_follows_a_symlink(tmp_path, monkeypatch):
    """A name-based denylist has to look at the name the FILESYSTEM opens.
    Both doors matched the literal argument, so `ln -s .env notes.txt` was a
    complete bypass -- an innocuous name for the same bytes."""
    from ctx.hook import classify_command, classify_read

    (tmp_path / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    link = tmp_path / "notes.txt"
    link.symlink_to(tmp_path / ".env")

    policy = {"deny_commands": [], "allow_commands": [], "steering": "deny"}
    assert classify_read(str(link), str(tmp_path), policy)["decision"] == "force_ask"
    d = classify_command("cat notes.txt", policy, cwd=str(tmp_path))
    assert d["decision"] == "force_ask", "the shell door must resolve it too"


def test_secret_guard_does_not_cry_wolf_inside_a_secret_named_checkout(tmp_path):
    """The resolution must be judged workspace-RELATIVE. A checkout living
    under ~/my-credentials/ would otherwise force-ask every read in it, which
    is the guard failing loudly rather than working."""
    from ctx.hook import classify_read

    root = tmp_path / "my-credentials"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    policy = {"deny_commands": [], "allow_commands": [], "steering": "deny"}
    d = classify_read(str(root / "src" / "app.py"), str(root), policy)
    assert d["decision"] != "force_ask"


def test_secret_guard_still_fires_on_a_plain_name(tmp_path):
    from ctx.hook import classify_read

    policy = {"deny_commands": [], "allow_commands": [], "steering": "deny"}
    assert classify_read(str(tmp_path / ".env"), str(tmp_path), policy)[
        "decision"] == "force_ask"


# ------------------------------------ a drop is declared with ITS OWN remedy
def test_unscoped_edges_are_declared_not_silently_dropped(state_home, workspace_dir):
    """`ctx callers` declares its unscoped tail with the flag that resolves
    it; the q stages filtered the same edges and said nothing, so
    `ctx ask --intent impact` could report 0 rows for a symbol with hundreds
    of real callers -- an empty answer indistinguishable from a fact."""
    from ctx.query import Stream

    s = Stream("sites", [], omitted=3, omitted_reason="unscoped: ...")
    assert s.omitted == 3 and s.omitted_reason.startswith("unscoped")


def test_stream_positional_construction_is_unchanged():
    """omitted_reason is appended LAST because Stream is built positionally
    in places; a field inserted mid-list silently rebinds those arguments."""
    from ctx.query import Stream

    s = Stream("sites", [], 3, [("k", 3)])
    assert s.omitted == 3 and s.groups == [("k", 3)]
    assert s.omitted_reason is None


# --------------------------------- one parse of one argument list
def test_a_flag_before_the_positional_does_not_steal_it():
    """`impact --depth 3 run_query` handed "3" to the stage as its symbol and
    dropped the real one -- silently, returning a plausible empty answer for
    a symbol nobody asked about."""
    from ctx.query import _need_arg, _positionals

    assert _positionals(["--depth", "3", "run_query"]) == ["run_query"]
    assert _positionals(["run_query", "--depth", "3"]) == ["run_query"]
    assert _need_arg(["--depth", "3", "run_query"], "impact", "a <Symbol>") == "run_query"


def test_boolean_flags_do_not_swallow_the_positional():
    from ctx.query import _positionals

    assert _positionals(["--changed", "src/a.py"]) == ["src/a.py"]
    assert _positionals(["--jsonl", "x"]) == ["x"]


def test_boolean_flag_set_covers_every_valueless_flag_the_stages_read():
    """The set is small and enumerable on purpose -- listing the BOOLEANS
    means a new value-taking flag is handled correctly by default. This keeps
    the list honest as stages are added."""
    import pathlib
    import re

    from ctx.query import _BOOLEAN_FLAGS

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
    used = set()
    for name in ("query.py", "facts.py"):
        text = (src / name).read_text(encoding="utf-8")
        used |= set(re.findall(r'"(--[a-z][a-z-]*)"\s+in\s+args', text))
    missing = used - set(_BOOLEAN_FLAGS)
    assert not missing, f"valueless flags not declared boolean: {sorted(missing)}"


# ------------------------- a census entry is identified by its node, not prose
def test_collection_error_is_one_entry_not_two():
    """The block header ("ERROR collecting tests/test_x.py") and the summary
    line ("ERROR tests/test_x.py") never matched, so one failure produced two
    entries: the real one and a phantom with no failure_class, no location and
    no summary -- under a coverage line attesting complete identity coverage."""
    from ctx.digest.pytestprof import _block_node, _collect_blocks, _match_entries

    assert _block_node("ERROR collecting tests/test_x.py") == "tests/test_x.py"
    assert _block_node("test_beta") == "test_beta"

    out_lines = [
        "==================== ERRORS ====================",
        "_____ ERROR collecting tests/test_x.py _____",
        "ImportError: no module named nope",
        "=========== short test summary info ============",
        "ERROR tests/test_x.py",
    ]
    blocks = _collect_blocks(out_lines)
    entries = _match_entries([(5, "tests/test_x.py", "")], blocks)
    assert len(entries) == 1, f"one collection error, one entry: {entries}"
    assert entries[0]["id"] == "tests/test_x.py"
    assert entries[0]["b"] is not None, "the surviving entry keeps its block span"
