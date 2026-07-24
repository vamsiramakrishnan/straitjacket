"""Acceptance: the CLI's human surface (ctx.cliux).

The front door is curated by hand, so the risk is drift: a command gets added,
renamed or removed and the help quietly stops matching the parser. These tests
pin the surface to the real parser and pin the tone rules that made it readable.
"""

from __future__ import annotations

import re

from ctx import cliux


def _parser_commands() -> set[str]:
    """Every command the real parser accepts."""
    import argparse

    from ctx.cli import _main_slow

    seen: set[str] = set()
    real = argparse.ArgumentParser.add_subparsers

    def spy(self, *a, **kw):
        sub = real(self, *a, **kw)
        real_add = sub.add_parser

        def add_parser(name, *aa, **kk):
            if self.prog == "ctx":
                seen.add(name)
            return real_add(name, *aa, **kk)

        sub.add_parser = add_parser
        return sub

    argparse.ArgumentParser.add_subparsers = spy
    try:
        _main_slow(["--this-is-not-a-command-xyz"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.add_subparsers = real
    return seen


def test_every_listed_command_exists_in_the_parser():
    """No help entry may advertise a command you cannot type."""
    listed = set(cliux.all_commands())
    real = _parser_commands()
    assert listed - real == set(), f"help lists commands the parser lacks: {listed - real}"


def test_every_parser_command_is_listed_somewhere():
    """No command may be invisible: it is either front-door or under --all."""
    real = _parser_commands()
    listed = set(cliux.all_commands())
    assert real - listed == set(), f"parser has commands no help lists: {real - listed}"


def test_front_door_is_only_what_a_human_needs():
    """A person sets it up, checks it, and looks at what it saved. The agent
    runs everything else. If this grows, someone has confused the audiences."""
    visible = [c for _, items in cliux.GROUPS for c, _ in items]
    assert visible == ["wrap", "doctor", "gain"], visible
    # the verbs the agent drives must NOT be on the human's first screen
    for agent_verb in ("run", "get", "search", "ask", "refs", "orchestrate"):
        assert agent_verb not in visible


def test_help_says_whose_job_the_rest_is():
    out = cliux.render_help()
    assert "Your agent runs these" in cliux.render_help(show_all=True)
    assert "agent runs for you once wrapped" in out
    # and it points at the context view rather than more commands to learn
    assert "kept out" in out


def test_no_jargon_on_the_first_screen():
    """The words that made the old help unreadable stay out of the front door."""
    banned = [
        "birth gate", "birth-time", "cache epoch", "hypothesis epoch",
        "pipeline algebra", "declared-omission", "capability surface",
        "typed intent preset", "rate-distortion", "lexicographic",
    ]
    screen = cliux.render_help().lower()
    for term in banned:
        assert term not in screen, f"jargon back on the first screen: {term!r}"


def test_help_is_grouped_and_has_a_next_step():
    out = cliux.render_help()
    for title, _ in cliux.GROUPS:
        assert f"{title}:" in out
    assert "Getting started:" in out
    assert "ctx wrap setup" in out
    assert "ctx help --all" in out


def test_help_all_reveals_the_advanced_commands():
    brief, full = cliux.render_help(), cliux.render_help(show_all=True)
    for cmd, _ in cliux.ADVANCED:
        assert re.search(rf"^\s+{re.escape(cmd)}\s", full, re.M), cmd
    assert len(full) > len(brief)


def test_did_you_mean_suggests_instead_of_dumping():
    out = cliux.did_you_mean("serach")
    assert "search" in out
    assert "no 'serach' command" in out
    # a suggestion, not the whole catalogue
    assert out.count("\n") < 12


def test_did_you_mean_survives_a_wild_miss():
    out = cliux.did_you_mean("zzzzzz")
    assert "ctx help" in out  # always leaves a way forward


def test_one_word_for_a_pointer():
    """`handle` is the single user-facing word for a pointer to evidence."""
    assert cliux.HANDLE_METAVAR == "handle"
    screen = cliux.render_help(show_all=True).lower()
    for rival in (" ref ", "coordinate", "reference"):
        assert rival not in screen, f"competing pointer word on screen: {rival!r}"


# ---------------------------------------------------- wrap-and-forget surface


def test_orchestration_is_a_wrap_mode_not_a_command_to_type(tmp_path):
    """Routing work across models should happen because you wrapped with it on,
    not because a human stopped to hand-route their own task."""
    from ctx.wrap import _ORCHESTRATION_MODE, _with_output_discipline

    off = _with_output_discipline(["-p", "do a thing"])
    on = _with_output_discipline(["-p", "do a thing"], orchestrate=True)
    assert _ORCHESTRATION_MODE not in " ".join(off)
    assert _ORCHESTRATION_MODE in " ".join(on)
    # it tells the session to route, and not to push the choice back on the user
    assert "cheapest one that can do each part" in _ORCHESTRATION_MODE
    assert "routing is your job now" in _ORCHESTRATION_MODE


def test_context_view_reports_containment_without_the_proxy(state_home, git_workspace):
    """The number that shows the harness is working must appear on the ordinary
    `ctx wrap` path — it used to require opting into --proxy, so a normal user
    never saw it."""
    import json as _json

    from ctx.statusline import render
    from ctx.store import Store

    from conftest import make_ws

    ws = make_ws(git_workspace)
    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
    store.audit_dir.mkdir(parents=True, exist_ok=True)
    (store.audit_dir / "telemetry.jsonl").write_text(
        "".join(
            _json.dumps({"op": "run", "raw_bytes": 400_000, "emitted_bytes": 800}) + "\n"
            for _ in range(3)
        ),
        encoding="utf-8",
    )
    line = render("claude-code", {"model": {"display_name": "Sonnet"}}, ws.root)
    assert "kept out" in line, line
    assert "ctx◇" in line
