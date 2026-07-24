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


def test_front_door_stays_small():
    """The first screen is a path, not a wall. If this fails, justify the growth."""
    visible = [c for _, items in cliux.GROUPS for c, _ in items]
    assert len(visible) <= 16, f"front door grew to {len(visible)} commands"
    # the three commands the quickstart promises must be visible
    for must in ("wrap", "run", "get"):
        assert must in visible


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
    assert "New here:" in out
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
