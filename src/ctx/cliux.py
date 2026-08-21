"""The shape of the CLI a human meets.

The central fact: **a human is not the main user of these commands.** Once
`ctx wrap` has hooked the harness into their coding agent, it is the *agent*
that runs `get`/`search`/`ask`/`refs` — through hooks and a single MCP tool.
The person goes back to working on their own code.

So the first screen is split by *audience*, not by task. A person needs three
commands: set it up, check it works, see what it saved. Everything else is
agent tooling, kept behind `ctx help --all` for the times a human wants to
drive it by hand.

`ctx` has three dozen commands (``len(all_commands())`` is the only count
that cannot go stale -- the hardcoded "34" here was two behind the dispatch
table, while the --help footer computed its own and printed 36). A handful are the product; the rest are machinery a
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
        "You run these",
        (
            ("setup", "detect, configure, and verify your coding agents — one command"),
            ("doctor", "check it is working: hooks, storage, which agents are set up"),
            ("gain", "see what it has kept out of your context, and what that saved"),
        ),
    ),
)

# Everything the *agent* drives once wrapped. A human can run them by hand, so
# they stay grouped by task under `ctx help --all` — but nobody needs to learn
# them to use the product.
AGENT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Capture",
        (
            ("run", "run a command — every byte is kept, only a short digest is shown"),
            ("py", "run a Python script the same way"),
            ("seq", "run several commands as one step"),
            ("job", "inspect or stop one background run"),
            ("jobs", "list background runs in this repo"),
        ),
    ),
    (
        "Find and read",
        (
            ("get", "read the exact lines behind a digest, or any slice of a file"),
            ("search", "search files or saved output; results stay small"),
            ("stats", "size, shape and schema of a file or saved output"),
            ("map", "a ranked tour of the codebase that fits a token budget"),
            ("diff", "what changed between two saved runs"),
            ("image", "inspect image/PDF structure or compare two image renders"),
        ),
    ),
    (
        "Ask about the code",
        (
            ("ask", "ask a question about this repo; get one evidence-backed answer"),
            ("q", "chain retrieval steps into one query"),
            ("plan", "build, price and run a multi-step evidence plan"),
            ("def", "where a symbol is defined"),
            ("refs", "where a symbol is used"),
            ("callers", "what calls this symbol"),
            ("callees", "what this symbol calls"),
            ("impact", "what breaks if this symbol changes"),
            ("impls", "what implements or extends this type"),
            ("cycles", "circular imports, or mutual recursion"),
            ("diag", "lint and type errors as a short digest"),
        ),
    ),
    (
        "Change code",
        (
            ("edit", "plan, preview, and apply anchored edits without guessing"),
            ("rewrite", "find and edit across many files in one transaction"),
        ),
    ),
    (
        "Across agents",
        (
            ("orchestrate", "split one task across your agents, cheapest model that fits "
                            "(usually a wrap mode, not something you type)"),
        ),
    ),
    (
        "Keep track",
        (
            ("checkpoint", "save task state so a fresh session can pick the work up"),
            ("debt", "track work you deliberately deferred"),
            ("ladders", "audit where the harness escalates, and what it measured"),
            ("surface", "see and trim the tools your agent exposes (they cost tokens too)"),
        ),
    ),
    (
        "Upkeep and internals",
        (
            ("init", "create ctx.toml and .ctxignore in this repo"),
            ("policy", "the steering rules ctx compiled from your own history"),
            ("replay", "replay recorded sessions to measure what ctx would have saved"),
            ("gc", "delete artifacts past their retention window"),
            ("pin", "keep one artifact forever"),
            ("proxy", "watch API traffic to measure what a session really spent"),
            ("antigravity", "install the Antigravity workspace plugin"),
            ("wrap", "advanced per-host setup and ephemeral launch modes"),
        ),
    ),
)

# Shown only under `ctx help --all`. Still plain English — a user who goes
# looking deserves a sentence, not a research abstract.
ADVANCED: tuple[tuple[str, str], ...] = tuple(
    item for _, items in AGENT_GROUPS for item in items
)

QUICKSTART = (
    "Getting started:\n"
    "  ctx setup         detect, configure, and verify the agents you have installed\n"
    "\n"
    "From then on your agent runs the rest itself. Watch the status line in your\n"
    "agent for `ctx NN%` (window used) and `ctx\u25c7 NNk kept out` (what never\n"
    "reached the model), or run `ctx gain` any time.\n"
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
    """The top-level help. A person needs three commands; say so plainly, and
    keep the agent's 31 verbs one keystroke away instead of on the first
    screen."""
    pad = max(len(c) for c in all_commands()) + 2
    out: list[str] = [
        "ctx — " + TAGLINE,
        "",
        "usage: ctx [--workspace PATH] <command> [args]",
        "",
    ]
    for title, items in GROUPS:
        out.append(f"{title}:")
        for cmd, line in items:
            out.append(f"  {cmd.ljust(pad)}{line}")
        out.append("")

    if show_all:
        out.append("Your agent runs these once wrapped — you do not need to learn them,")
        out.append("but you can drive any of them by hand:")
        out.append("")
        for title, items in AGENT_GROUPS:
            out.append(f"  {title}:")
            for cmd, line in items:
                out.append(f"    {cmd.ljust(pad)}{line}")
            out.append("")
    else:
        out.append(
            f"That is the whole human surface. The other {len(ADVANCED)} commands are what your"
        )
        out.append("agent runs for you once wrapped — see them with:  ctx help --all")
        out.append("")

    out.append(QUICKSTART)
    out.append("Flags for one command:  ctx <command> --help")
    out.append(
        "Workspace defaults to the git root above the current directory; "
        "override with --workspace."
    )
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
