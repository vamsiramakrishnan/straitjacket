"""The shape of the CLI a human meets.

`ctx` has 33 commands. A handful are the product; the rest are machinery a
user grows into. Argparse showed all of them at equal weight in source order,
which reads as a wall and hides the one path that actually works. This module
holds the curated surface: what is shown first, in what groups, in what words.

Rules this file exists to enforce:

* **Say when, not what.** A one-liner answers "why would I reach for this?",
  never "which internal abstraction does this expose?". No coined vocabulary
  (no "birth gate", "cache epoch", "pipeline algebra") on the first screen.
* **One name per thing.** A pointer to stored evidence is a *handle*
  everywhere — not ref/reference/coordinate/address depending on the file.
* **Front door first.** The commands a new user needs are listed first and
  named as a path; everything else lives behind `ctx help --all`.

This is the presentation layer over the parser. Where a *name* itself was the
problem it was fixed at the source instead of described around: `eval` became
`py` (it runs a Python script, it is not shell-eval) and `investigate` was
folded into `plan run` (it was that same execution plus a replan ledger).
"""

from __future__ import annotations

import difflib

# The pointer word. Chosen once, used everywhere the user can see it.
HANDLE_METAVAR = "handle"

TAGLINE = "Keep your coding agent's context small: big output is stored whole, the agent sees a short digest."

# ---------------------------------------------------------------- the surface
# (group title, [(command, one-line "when would I use this")])
GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Start here",
        (
            ("wrap", "hook ctx into your coding agent (Claude Code, Antigravity, Codex)"),
            ("run", "run a command — every byte is kept, the agent sees a short digest"),
            ("doctor", "check the setup: hooks, storage, which agents are installed"),
        ),
    ),
    (
        "Find and read",
        (
            ("get", "read the exact lines behind a digest, or any slice of a file"),
            ("search", "search files or saved output; results stay small"),
            ("stats", "size, shape and schema of a file or saved output"),
            ("map", "a ranked tour of the codebase that fits a token budget"),
        ),
    ),
    (
        "Ask about the code",
        (
            ("ask", "ask a question about this repo; get one evidence-backed answer"),
            ("def", "where a symbol is defined"),
            ("refs", "where a symbol is used"),
            ("callers", "what calls this symbol"),
            ("callees", "what this symbol calls"),
            ("impact", "what breaks if this symbol changes"),
        ),
    ),
    (
        "Work across agents",
        (
            ("orchestrate", "split one task across your agents, cheapest model that can do each part"),
        ),
    ),
    (
        "Set up and upkeep",
        (
            ("init", "create ctx.toml and .ctxignore in this repo"),
            ("gain", "how many tokens and dollars ctx has saved you so far"),
        ),
    ),
)

# Shown only under `ctx help --all`. Still plain English — a user who goes
# looking deserves a sentence, not a research abstract.
ADVANCED: tuple[tuple[str, str], ...] = (
    ("diff", "what changed between two saved runs"),
    ("checkpoint", "save task state so a fresh session can pick the work up"),
    ("debt", "track work you deliberately deferred"),
    ("q", "chain retrieval steps into one query"),
    ("plan", "build, price and run a multi-step evidence plan"),
    ("surface", "see and trim the tools your agent exposes (they cost tokens too)"),
    ("job", "inspect or stop one background run"),
    ("jobs", "list background runs in this repo"),
    ("seq", "run several commands as one step"),
    ("py", "run a Python script the way `run` runs a command"),
    ("rewrite", "find and edit across many files in one transaction"),
    ("diag", "lint and type errors as a short digest"),
    ("policy", "the steering rules ctx compiled from your own history"),
    ("replay", "replay recorded sessions to measure what ctx would have saved"),
    ("gc", "delete artifacts past their retention window"),
    ("pin", "keep one artifact forever"),
    ("proxy", "watch API traffic to measure what a session really spent"),
    ("antigravity", "install the Antigravity workspace plugin"),
)

QUICKSTART = (
    "New here:\n"
    "  ctx wrap setup           hook ctx into the agents you have installed\n"
    "  ctx run -- pytest -q     run something noisy and watch it shrink\n"
    "  ctx get <handle>         pull back any bytes the digest left out\n"
)


def _visible() -> dict[str, str]:
    return {cmd: line for _, items in GROUPS for cmd, line in items}


def all_commands() -> dict[str, str]:
    """Every command a user can type, with its plain-English line."""
    return {**_visible(), **dict(ADVANCED)}


def help_line(cmd: str) -> str:
    """The one-liner for a command (used as argparse `help=`)."""
    return all_commands().get(cmd, "")


def render_help(*, show_all: bool = False, width: int = 78) -> str:
    """The top-level help: a path, not a wall."""
    pad = max(len(c) for c in all_commands()) + 2
    out: list[str] = ["ctx — " + TAGLINE, "", "usage: ctx [--workspace PATH] <command> [args]", ""]

    for title, items in GROUPS:
        out.append(f"{title}:")
        for cmd, line in items:
            out.append(f"  {cmd.ljust(pad)}{line}")
        out.append("")

    if show_all:
        out.append("More commands:")
        for cmd, line in ADVANCED:
            out.append(f"  {cmd.ljust(pad)}{line}")
        out.append("")
    else:
        out.append(f"More:  ctx help --all   {len(ADVANCED)} further commands "
                   "(background runs, evidence plans, telemetry, upkeep)")
        out.append("")

    out.append(QUICKSTART)
    out.append("Flags for one command:  ctx <command> --help")
    out.append("Workspace defaults to the git root above the current directory; "
               "override with --workspace.")
    return "\n".join(out)


def did_you_mean(word: str) -> str:
    """`ctx summarise` should suggest, not dump the whole command list."""
    names = list(all_commands())
    close = difflib.get_close_matches(word, names, n=3, cutoff=0.55)
    if not close:
        # substring fallback: `ctx check` -> checkpoint
        close = [n for n in names if word in n or n in word][:3]
    lines = [f"ctx: there is no '{word}' command."]
    if close:
        lines.append("")
        lines.append("Did you mean:")
        pad = max(len(c) for c in close) + 2
        for c in close:
            lines.append(f"  {c.ljust(pad)}{all_commands()[c]}")
    lines.append("")
    lines.append("See all commands:  ctx help")
    return "\n".join(lines)


__all__ = [
    "HANDLE_METAVAR",
    "TAGLINE",
    "GROUPS",
    "ADVANCED",
    "QUICKSTART",
    "all_commands",
    "help_line",
    "render_help",
    "did_you_mean",
]
