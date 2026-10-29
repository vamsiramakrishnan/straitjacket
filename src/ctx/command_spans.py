"""Declarative output-risk spans for common developer commands.

The context guard is not a shell permission system. This module answers the
narrower question it owns: can terminal output pass directly, or should the
same command execute behind ``ctx run``? Unknown or mutation-shaped commands
return ``None`` so the configured permission boundary still decides.

The registry adapts RTK's useful split between commands that do not benefit
from compaction and read-only command families that do. Straitjacket adds a
stricter third state: unrecognised/mutation-shaped commands are neither
pass-through nor transparently executed.

Hot-path constraint: imported by ``ctx.hook`` on every invocation, so this
module uses only ``os`` and built-in containers (no pathlib/typing/dataclasses).
"""

from __future__ import annotations

import os

ALLOW = "allow"
CAPTURE = "capture"

_HELP_FLAGS = frozenset({"-h", "--help", "help"})
_VERSION_FLAGS = frozenset({"-V", "--version", "version"})

LOW_OUTPUT_PROGRAMS = frozenset(
    {
        "arch", "basename", "clear", "date", "dirname", "groups", "hostname",
        "id", "mktemp", "nproc", "pwd", "readlink", "realpath", "sync",
        "tty", "uname", "whoami",
    }
)

# Local observation/build tools whose output is worth compacting. External
# mutation CLIs are deliberately absent; a neighbouring read command never
# widens their approval boundary.
CAPTURE_PROGRAMS = frozenset(
    {
        "bat", "delta", "eza", "exa", "fd", "golangci-lint", "hadolint",
        "jq", "lsd", "markdownlint", "phpstan", "phpunit", "pest", "rspec",
        "rubocop", "shellcheck", "stylelint", "swiftlint",
    }
)

_LIST_PATHS = frozenset(
    {
        ("run", "list"), ("pr", "list"), ("issue", "list"),
        ("repo", "list"), ("release", "list"), ("workflow", "list"),
    }
)
_GH_DIRECT_ALLOW = frozenset(
    {
        ("auth", "status"), ("alias", "list"), ("config", "get"),
        ("extension", "list"), ("pr", "checks"),
    }
)
_GH_READONLY_CAPTURE = frozenset(
    {
        ("run", "view"), ("run", "watch"), ("pr", "view"),
        ("pr", "diff"), ("issue", "view"), ("repo", "view"),
        ("release", "view"), ("workflow", "view"),
    }
)
_LARGE_JSON_FIELDS = frozenset(
    {"body", "comments", "files", "jobs", "log", "text", "commits"}
)
_GH_GLOBAL_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname"})
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)
_GIT_SUMMARY_FLAGS = frozenset(
    {"--check", "--stat", "--numstat", "--shortstat", "--name-only",
     "--name-status", "--summary"}
)
_GIT_PATCH_FLAGS = frozenset({"-p", "-u", "--patch", "--patch-with-raw", "--patch-with-stat"})  # was missing; full patch leaked through as ALLOW


def _metadata_query(argv) -> bool:
    rest = list(argv[1:])
    if len(rest) == 1 and rest[0] in (_HELP_FLAGS | _VERSION_FLAGS):
        return True
    return len(rest) == 2 and rest[-1] in _HELP_FLAGS and not rest[0].startswith("-")


def _skip_global_options(args, value_flags) -> list[str]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return list(args[i + 1 :])
        if arg in value_flags:
            i += 2
            continue
        if any(arg.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return list(args[i:])
    return []


def _flag_value(args, long: str, short: str) -> str | None:
    for i, arg in enumerate(args):
        if arg in (long, short):
            return args[i + 1] if i + 1 < len(args) else ""
        if arg.startswith(long + "="):
            return arg.split("=", 1)[1]
        if short != long and arg.startswith(short) and arg != short:
            return arg[len(short) :]
    return None


def _bounded_limit(args, maximum: int) -> bool:
    raw = _flag_value(args, "--limit", "-L")
    if raw is None:
        return True  # gh list commands have a finite default page.
    try:
        return 0 <= int(raw) <= maximum
    except ValueError:
        return False


def _json_fields(args) -> frozenset[str]:
    raw = _flag_value(args, "--json", "--json")
    if not raw:
        return frozenset()
    return frozenset(field.strip() for field in raw.split(",") if field.strip())


def _gh_span(argv, maximum_records: int) -> str | None:
    args = _skip_global_options(argv[1:], _GH_GLOBAL_VALUE_FLAGS)
    if not args:
        return None
    path = tuple(args[:2])
    fields = _json_fields(args)
    if path in _LIST_PATHS:
        if _bounded_limit(args, maximum_records) and not (fields & _LARGE_JSON_FIELDS):
            return ALLOW
        return CAPTURE
    if path in _GH_DIRECT_ALLOW:
        return CAPTURE if "--watch" in args else ALLOW
    if path == ("run", "view") and fields and not (fields & _LARGE_JSON_FIELDS):
        if not any(flag in args for flag in ("--log", "--log-failed", "--job")):
            return ALLOW
    if path in _GH_READONLY_CAPTURE or args[0] in ("search", "status"):
        return CAPTURE
    if args[0] == "api":
        mutation_flags = {"-X", "--method", "-f", "--field", "-F", "--raw-field", "--input"}
        short_flags = tuple(flag for flag in mutation_flags if not flag.startswith("--"))
        if not any(
            arg in mutation_flags
            or any(arg.startswith(flag + "=") for flag in mutation_flags if flag.startswith("--"))
            # attached shorthand (-XDELETE, -fquery=...) glues the value onto the flag
            or (arg.startswith(short_flags) and len(arg) > 2)
            for arg in args[1:]
        ):
            return CAPTURE
    return None


def git_subcommand(argv) -> tuple[str, list[str]]:
    """Return the real git subcommand after global options such as ``-C``."""
    args = _skip_global_options(argv[1:], _GIT_GLOBAL_VALUE_FLAGS)
    return (args[0], args[1:]) if args else ("", [])


def _git_span(argv) -> str | None:
    sub, rest = git_subcommand(argv)
    if sub != "diff":
        return None
    summary = any(
        arg in _GIT_SUMMARY_FLAGS or arg.startswith("--stat=") for arg in rest
    )
    patch = any(
        arg in _GIT_PATCH_FLAGS or arg.startswith("--unified")
        or (arg.startswith("-U") and len(arg) > 2)
        for arg in rest
    )
    return ALLOW if summary and not patch else CAPTURE


def classify_command_span(argv, *, maximum_records: int = 100) -> str | None:
    """Classify an already shell-split, wrapper-unwrapped argv."""
    if not argv:
        return ALLOW
    prog = os.path.basename(argv[0])
    if _metadata_query(argv):
        return ALLOW
    if prog in LOW_OUTPUT_PROGRAMS:
        return ALLOW
    if prog in CAPTURE_PROGRAMS:
        return CAPTURE
    if prog == "git":
        return _git_span(argv)
    if prog == "gh":
        return _gh_span(argv, maximum_records)
    if prog == "glab":
        args = _skip_global_options(argv[1:], frozenset({"-R", "--repo", "--hostname"}))
        if len(args) >= 2 and args[1] in {"list", "view", "diff", "status"}:
            return CAPTURE
    if prog == "gt" and len(argv) >= 2 and argv[1] in {"log", "status"}:
        return CAPTURE
    if prog == "dotnet" and len(argv) >= 2 and argv[1] in {"build", "test"}:
        return CAPTURE
    if prog == "swift" and len(argv) >= 2 and argv[1] in {"build", "test"}:
        return CAPTURE
    if prog == "zig" and len(argv) >= 2 and argv[1] in {"build", "test"}:
        return CAPTURE
    return None
