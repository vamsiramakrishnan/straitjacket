"""`ctx prune`: bound before bloat, at setup time.

The audit and the compile step both existed; prune is the decision rule
between them, run when the harness is installed, with a receipt. These pin
the rule (kernel and L0/L1 stay, L2+ is deferred), the receipt, idempotence,
and the CLI wiring."""

import json
from pathlib import Path


from conftest import make_ws


def _repo(root: Path) -> None:
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "github": {"command": "gh-mcp", "args": []},
        "deploy-prod": {"command": "deployer", "args": ["--production"]},
    }}), encoding="utf-8")
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "releaser.md").write_text(
        "---\nname: releaser\ndescription: deploys releases; can delete tags and force-push\n---\n"
        "Run `git push --force` and `rm -rf build/` when asked.\n" * 8, encoding="utf-8")
    skills = root / ".claude" / "skills" / "notes"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: notes\ndescription: how we write commit messages\n---\nKeep them short.\n",
        encoding="utf-8")


def test_plan_keeps_kernel_and_visible_levels_and_defers_the_rest(state_home, workspace_dir):
    from ctx.prune import KEEP_LEVELS, plan_prune

    _repo(workspace_dir)
    plan = plan_prune(workspace_dir)
    assert plan["decisions"], "the audit saw nothing"
    for d in plan["decisions"]:
        if d["keep"]:
            assert d["why"] == "kernel" or d["level"] in KEEP_LEVELS, d
        else:
            assert d["level"] not in KEEP_LEVELS, d
    assert plan["deferred"], "an unused deploy server and a destructive agent must defer"
    kinds = {d["kind"] for d in plan["decisions"] if not d["keep"]}
    assert kinds & {"mcp_server", "agent"}
    t = plan["tokens_per_turn"]
    assert t["after"] == sum(d["tokens"] for d in plan["decisions"] if d["keep"])
    assert t["saved"] == t["before"] - t["after"] > 0
    assert plan["repo"]["runners"] == ["pytest"]
    assert ".py" in {d["ext"] for d in plan["repo"]["languages"]}


def test_keep_forces_a_capability_to_stay(state_home, workspace_dir):
    from ctx.prune import plan_prune

    _repo(workspace_dir)
    deferred = plan_prune(workspace_dir)["deferred"]
    chosen = deferred[0]
    again = plan_prune(workspace_dir, keep=(chosen,))
    assert chosen not in again["deferred"]
    assert next(d for d in again["decisions"] if d["id"] == chosen)["why"] == "kept by request"


def test_apply_writes_host_config_and_a_receipt_and_is_idempotent(state_home, workspace_dir):
    from ctx.prune import PRUNE_SCHEMA, run_prune

    _repo(workspace_dir)
    preview = run_prune(workspace_dir, hosts=("claude", "codex"))
    assert not (workspace_dir / ".ctx-surface").exists(), "preview must not write"
    rep = run_prune(workspace_dir, hosts=("claude", "codex"), apply=True)
    receipt = workspace_dir / ".ctx-surface" / "prune-receipt.json"
    assert receipt.is_file() and rep["receipt"] == ".ctx-surface/prune-receipt.json"
    doc = json.loads(receipt.read_text())
    assert doc["schema"] == PRUNE_SCHEMA and doc["applied"] is True
    assert doc["deferred"] == preview["deferred"]
    for host in ("claude", "codex"):
        h = rep["hosts"][host]
        assert "error" not in h and h["written"], host
        assert h["tokens"]["after_gateway"] <= h["tokens"]["before"]
        assert h["servers_dropped"], "the unused deploy server is dropped from the launch config"
    second = run_prune(workspace_dir, hosts=("claude", "codex"), apply=True)
    assert second["deferred"] == rep["deferred"]
    assert second["tokens_per_turn"] == rep["tokens_per_turn"]


def test_unknown_host_is_reported_not_raised(state_home, workspace_dir):
    from ctx.prune import run_prune

    _repo(workspace_dir)
    rep = run_prune(workspace_dir, hosts=("antigravity-sdk",))
    assert "error" in rep["hosts"]["antigravity-sdk"]


def test_cli_prune_previews_then_applies(state_home, workspace_dir, capsys):
    from ctx.cli import main

    _repo(workspace_dir)
    root = str(workspace_dir)
    assert main(["--workspace", root, "prune"]) == 0
    out = capsys.readouterr().out
    assert "preview" in out and "deferred" in out
    assert not (workspace_dir / ".ctx-surface").exists()
    assert main(["--workspace", root, "prune", "--apply", "--host", "claude", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["applied"] and doc["hosts"]["claude"]["written"]
    assert (workspace_dir / ".ctx-surface" / "prune-receipt.json").is_file()


def test_setup_has_a_prune_flag():
    from ctx.cli import _build_parser

    ns = _build_parser().parse_args(["setup", "--prune", "--host", "claude"])
    assert ns.prune is True and ns.hosts == ["claude"]


def test_nothing_to_defer_on_a_bare_repo(state_home, workspace_dir):
    from ctx.prune import render_prune, run_prune

    ws = make_ws(workspace_dir)
    rep = run_prune(ws.root)
    assert rep["deferred"] == []
    assert "nothing to defer" in render_prune(rep)


# ------------------------------------------------------------ interactive
def _script(answers):
    """An `ask` that replays scripted answers and records the prompts."""
    prompts = []
    it = iter(answers)

    def ask(prompt):
        prompts.append(prompt)
        return next(it)

    return ask, prompts


def test_parse_selection_grammar():
    from ctx.prune import parse_selection

    d = {2, 4}
    assert parse_selection("", 5, d) == {2, 4}
    assert parse_selection("all", 5, d) == {1, 2, 3, 4, 5}
    assert parse_selection("none", 5, d) == set()
    assert parse_selection("1,3-5", 5, d) == {1, 3, 4, 5}
    assert parse_selection("+1", 5, d) == {1, 2, 4}
    assert parse_selection("-2", 5, d) == {4}
    assert parse_selection("-2 +5", 5, d) == {4, 5}
    assert parse_selection("?3", 5, d) == "explain:3"
    for bad in ("9", "x", "1-9", "?9", "?x", "2-"):
        assert parse_selection(bad, 5, d) == "invalid", bad


def test_interactive_enter_everywhere_equals_the_rule(state_home, workspace_dir):
    """Enter at every prompt must produce exactly what --yes produces."""
    from ctx.prune import interactive_prune, run_prune

    _repo(workspace_dir)
    rule = run_prune(workspace_dir, hosts=("claude",))
    ask, prompts = _script([""] * 12)
    said = []
    rep = interactive_prune(workspace_dir, ask=ask, say=said.append, hosts=("claude",))
    assert rep["applied"] and rep["interactive"]
    assert rep["deferred"] == rule["deferred"]
    assert rep["tokens_per_turn"] == rule["tokens_per_turn"]
    assert all(d["by"] == "rule" for d in rep["decisions"] if d["why"] != "kernel")
    assert any("defer which?" in p for p in prompts)
    assert any("write the compiled" in p for p in prompts)
    assert (workspace_dir / ".ctx-surface" / "prune-receipt.json").is_file()
    # Kernel is stated as kept without asking, never listed in a selector.
    assert not any(d["why"] == "kernel" and any(d["id"] in line for line in said if line.startswith("  ")
                                                  and "[" in line) for d in rep["decisions"])


def test_interactive_keep_all_defers_nothing_and_records_the_user(state_home, workspace_dir):
    from ctx.prune import interactive_prune

    _repo(workspace_dir)
    # "none" at every selector, default hosts, then decline the write.
    ask, prompts = _script(["none"] * 6 + ["", "n"])
    said = []
    rep = interactive_prune(workspace_dir, ask=ask, say=said.append, hosts=("claude",))
    assert rep["deferred"] == [] and rep["applied"] is False
    assert not (workspace_dir / ".ctx-surface").exists()
    overridden = [d for d in rep["decisions"] if d.get("by") == "user"]
    assert overridden and all(d["keep"] and d["why"].startswith("kept by user") for d in overridden)
    assert any("declined" in line for line in said)


def test_interactive_explain_and_invalid_reask(state_home, workspace_dir):
    from ctx.prune import interactive_prune

    _repo(workspace_dir)
    # First selector: an invalid answer, then an explain, then accept.
    ask, prompts = _script(["zzz", "?1", ""] + [""] * 10)
    said = []
    interactive_prune(workspace_dir, ask=ask, say=said.append, hosts=("claude",))
    assert any("not understood" in line for line in said)
    assert any("level L" in line for line in said)


def test_interactive_host_choice_is_validated(state_home, workspace_dir):
    from ctx.prune import interactive_prune

    _repo(workspace_dir)
    host_answers = iter(["vscode", "codex, claude"])

    def ask(prompt):  # prompt-aware: only the host question gets a non-default answer
        return next(host_answers) if "hosts?" in prompt else ""

    said = []
    rep = interactive_prune(workspace_dir, ask=ask, say=said.append, hosts=("claude",))
    assert set(rep["hosts"]) == {"codex", "claude"}
    assert any("hosts are" in line for line in said)


def test_cli_yes_applies_without_questions(state_home, workspace_dir, capsys, monkeypatch):
    from ctx.cli import main

    _repo(workspace_dir)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert main(["--workspace", str(workspace_dir), "prune", "--yes"]) == 0
    assert "applied" in capsys.readouterr().out
    assert (workspace_dir / ".ctx-surface" / "prune-receipt.json").is_file()
