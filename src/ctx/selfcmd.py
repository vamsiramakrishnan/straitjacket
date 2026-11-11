"""How ctx launches itself — the one place that decides it.

``python -m <module>`` puts the CURRENT DIRECTORY first on ``sys.path``. ctx
launches itself (hooks, MCP servers, the job supervisor, the gateway, the
LSP service) with the current directory inside the user's workspace, so a
repository that contains a top-level ``ctx/`` package (or ``ctx.py``) would
have its own code run as the tool — as the guard, on every tool call.

Every self-launch therefore goes through this module:

* the installed ``ctx`` entry point, by ABSOLUTE path, found only in
  absolute ``PATH`` entries (a relative entry such as ``.`` or
  ``node_modules/.bin`` would resolve inside the workspace); a console
  script puts its own directory, not the current one, on ``sys.path``;
* otherwise ``<python> -P -m ctx``. ``-P`` (Python ≥ 3.11, the project's
  floor) drops the unsafe path entry. It is a command-line flag rather than
  ``PYTHONSAFEPATH`` because commands written into a host's configuration
  are run by the host, which does not carry ctx's environment.

Stdlib only: imported from the hook path.
"""

from __future__ import annotations

import os
import shlex
import sys

#: The interpreter flag that keeps the current directory off ``sys.path``.
SAFE_PATH_FLAG = "-P"


def which_absolute(name: str, path: str | None = None) -> str | None:
    """``shutil.which`` restricted to ABSOLUTE ``PATH`` entries (PATHEXT and
    the platform's other rules still apply), never answering with a file
    inside the current directory tree (the workspace) unless it is this
    interpreter's own entry point. That guards against an absolute PATH
    entry pointing into the repository (``direnv``'s ``PATH_add bin``, a
    project ``.venv/bin`` of another tool) whose ``ctx`` would otherwise be
    written into hook commands."""
    exts = [""]
    if os.name == "nt":  # shutil.which's PATHEXT rule
        exts += [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    for d in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
                continue
            if _inside_cwd(cand) and not _is_own_entry_point(cand, name):
                continue
            return cand
    return None


_ROOT_MARKERS = (".git", "ctx.toml", ".ctx")


def workspace_boundary(start: str | None = None) -> str:
    """The boundary of the workspace containing ``start`` (default: the
    current directory): the OUTERMOST ancestor holding ``.git``,
    ``ctx.toml`` or ``.ctx``, so a nested repository, a submodule or a
    ``ctx.toml`` above the git root all count as one workspace. The walk
    stops below the user's home directory (HOME itself and above are where
    tools are installed, never "the repository").

    LIMIT: with no marker anywhere, the boundary is the start directory
    itself, so a PATH entry pointing at a sibling or parent directory of
    an unmarked working directory is not recognised as workspace content."""
    cur = os.path.realpath(start or os.getcwd())
    home = os.path.realpath(os.environ.get("HOME") or os.path.expanduser("~"))
    outermost = None
    probe = cur
    while True:
        if probe == home or (os.path.commonpath([probe, home]) == probe and cur.startswith(home + os.sep)):
            break                      # never HOME or its ancestors
        if any(os.path.exists(os.path.join(probe, m)) for m in _ROOT_MARKERS):
            outermost = probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return outermost or cur


def _inside_cwd(path: str) -> bool:
    """Is ``path`` inside the workspace (not merely the current directory)?"""
    try:
        root = workspace_boundary()
        real = os.path.realpath(path)
        return os.path.commonpath([root, real]) == root
    except (OSError, ValueError):
        return False


def _is_own_entry_point(path: str, name: str) -> bool:
    own = os.path.join(os.path.dirname(sys.executable), name)
    try:
        return os.path.realpath(path) == os.path.realpath(own) or \
            os.path.realpath(path) == os.path.realpath(own + ".exe")
    except OSError:
        return False


def module_argv(module: str = "ctx") -> list[str]:
    """``[<python>, -P, -m, <module>]`` — a module launch that cannot import
    from the current directory."""
    return [sys.executable, SAFE_PATH_FLAG, "-m", module]


def ctx_argv() -> list[str]:
    """argv that runs THIS installation of ctx from any working directory."""
    exe = which_absolute("ctx")
    return [exe] if exe else module_argv("ctx")


def ctx_command() -> str:
    """:func:`ctx_argv` as one shell-safe string (hook command fields)."""
    return shlex.join(ctx_argv())


#: Single-letter interpreter flags that take no value (may be clustered).
_NOARG = set("bBdEhiIOPqRsSuvVx")
#: Flags whose value is the next token when not attached (-W x, -X dev).
_VALUED = ("-W", "-X")


def python_m_ctx(argv: list[str]) -> tuple[bool, bool] | None:
    """For a Python launch of module ``ctx`` / ``ctx.*``: (True, safe).

    CONSERVATIVE, for detection: every option form the interpreter accepts
    is parsed (``-X dev``, ``-W x``, ``-Wfoo``, clusters like ``-Pm ctx``,
    attached ``-mctx``). Safe only when ``-P`` or ``-I`` is present as a real
    flag before the module, never when the letters merely occur inside an
    option VALUE (``-Wignore::PendingDeprecationWarning``). None when argv
    is not a Python launch of ctx."""
    if not argv or not os.path.basename(argv[0]).startswith("python"):
        return None
    safe = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--" or not tok.startswith("-") or tok == "-":
            return None                      # a script, stdin, or end of options
        if tok in _VALUED:
            i += 2
            continue
        if tok.startswith(_VALUED):
            i += 1
            continue
        if tok.startswith("--"):
            i += 1                           # long option; conservatively not P/I
            continue
        cluster = tok[1:]
        if "c" in cluster.split("m", 1)[0]:
            return None                      # -c program
        if "m" in cluster:
            head, _, attached = cluster.partition("m")
            if not all(ch in _NOARG for ch in head):
                return None
            if "P" in head or "I" in head:
                safe = True
            module = attached or (argv[i + 1] if i + 1 < len(argv) else "")
            if module == "ctx" or module.startswith("ctx."):
                return True, safe
            return None
        if all(ch in _NOARG for ch in cluster):
            if "P" in cluster or "I" in cluster:
                safe = True
        i += 1
    return None


def is_unsafe_ctx_command(command: str) -> bool:
    """Does this command launch ctx as ``python -m ctx`` WITHOUT ``-P``?"""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    found = python_m_ctx(argv)
    return bool(found and not found[1])


def make_safe(argv: list[str]) -> list[str]:
    """EXACT, for rewriting: only the shape ctx itself wrote,
    ``<python> -m ctx …``, gains ``-P`` after the interpreter. Anything else
    is returned unchanged (detection is :func:`python_m_ctx`'s job)."""
    if (len(argv) >= 3 and argv[1:3] == ["-m", "ctx"]
            and os.path.basename(argv[0]).startswith("python")):
        return [argv[0], SAFE_PATH_FLAG, *argv[1:]]
    return list(argv)


__all__ = ["SAFE_PATH_FLAG", "which_absolute", "module_argv", "ctx_argv", "ctx_command",
           "python_m_ctx", "is_unsafe_ctx_command", "make_safe"]
