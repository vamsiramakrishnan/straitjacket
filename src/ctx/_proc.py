"""The subprocess mechanism the foreground and background runners share (R7).

:mod:`ctx.execution` runs a command synchronously and returns its manifest;
:mod:`ctx.jobs` runs one under a detached supervisor that outlives the caller.
They are genuinely different things and this module does not pretend
otherwise — it holds only the two steps whose *implementation* was identical
in both, character for character:

* :func:`wait_or_kill` — bound a child's runtime and, on expiry, take down
  the whole process group rather than the leader (both runners spawn with
  ``start_new_session``, so a shell that forked children leaves orphans if you
  only kill the pid you know).
* :func:`exit_status` — turn a ``Popen.returncode`` into the manifest's
  ``(exitCode, signal)`` pair. Signal death is NOT an exit code: the manifest
  reports ``exitCode: null`` plus a signal name, and a run killed by a signal
  must never read as "exited 0" or "exited 137".

Deliberately stdlib-only and dependency-free. ``ctx.jobs.supervise_main`` is
a detached process that reads everything it needs from ``meta.json`` and
never resolves a workspace or opens the store; importing these two helpers
out of :mod:`ctx.execution` would have dragged the store, the workspace
resolver and git parsing into it. That property is the reason this is its own
module rather than two more functions in ``execution.py``.

What is NOT here, because the similarity is superficial rather than shared
logic — see ``tests/test_proc_mechanism.py``, which records the reasoning:

* Argv validation. The messages match but the exception classes differ
  (``ExecutionError`` / ``JobError``), and a shared validator parameterized
  by exception class is more machinery than the two lines it saves.
* The ``Popen`` call itself. One feeds a spooled stdin file and raises on
  ``FileNotFoundError``; the other feeds ``DEVNULL`` and records a ``failed``
  state transition into ``meta.json``. Same API, different contracts.
"""

from __future__ import annotations

import os
import signal as signal_mod
import subprocess
import time

__all__ = ["exit_status", "native_exit_status", "kill_and_reap", "wait_or_kill",
           "shell_popen_args", "capture_shell", "capture_shell_choice", "SH_FALLBACK_NOTE"]

#: Where a shell may be installed besides $PATH (Claude Code's own search).
_STANDARD_DIRS = ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")
_VERIFIED: dict[str, bool] = {}
_CHOICE: dict[tuple, tuple[str, str | None]] = {}

#: The one line said when no bash or zsh could be found.
SH_FALLBACK_NOTE = ("[ctx: no working bash or zsh found; the command ran under /bin/sh, "
                    "which may not accept bash syntax]")


def _known_shells() -> frozenset[str]:
    """Resolved paths of the system's own shells: every entry of
    /etc/shells, and the bash/zsh installed beside the system's /bin/sh."""
    out = set()
    try:
        with open("/etc/shells", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("/"):
                    out.add(os.path.realpath(line))
    except OSError:
        pass
    for d in ("/bin", "/usr/bin", os.path.dirname(os.path.realpath("/bin/sh"))):
        for name in ("bash", "zsh"):
            p = os.path.join(d, name)
            if os.path.exists(p):
                out.add(os.path.realpath(p))
    return frozenset(out)


def _working_shell(path: str | None) -> bool:
    """Is ``path`` a bash or zsh that runs? Asked of the binary itself (it
    must report BASH_VERSION or ZSH_VERSION), never read off its name: a
    file named ``bash`` can be anything."""
    if not path or not os.path.isabs(path) or os.path.isdir(path) or not os.access(path, os.X_OK):
        return False
    if os.path.realpath(path) not in _known_shells():
        return False
    hit = _VERIFIED.get(path)
    if hit is not None:
        return hit
    env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
    try:
        out = subprocess.run(
            [path, "-c", 'printf %s "${BASH_VERSION:-}${ZSH_VERSION:-}"'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env=env)
        ok = out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        ok = False
    _VERIFIED[path] = ok
    return ok


def _search(names: tuple[str, ...]) -> str | None:
    import shutil

    for name in names:
        found = shutil.which(name)
        cands = ([found] if found else []) + [os.path.join(d, name) for d in _STANDARD_DIRS]
        for cand in cands:
            if _working_shell(cand):
                return cand
    return None


def capture_shell_choice() -> tuple[str, str | None]:
    """``(shell, note)``: the shell that runs a ``--shell`` command string,
    and the one line to say when it is not a shell the host would use.

    The shell the agent's own shell tool uses, so a string written for it --
    brace expansion, ``[[ ]]``, arrays, here-strings, ``set -o pipefail`` --
    runs the same. Claude Code documents its choice (env-vars,
    ``CLAUDE_CODE_SHELL``): that variable when it names a working bash or
    zsh, else ``$SHELL`` when that is bash or zsh, else the first working
    zsh, then bash, on PATH and the standard locations; never another shell.
    Under Claude Code (``CLAUDECODE=1`` in its Bash tool's environment) ctx
    follows exactly that. Elsewhere the same, but preferring bash when
    ``$SHELL`` is not bash or zsh (fish, unset, a wrapper). ``CTX_SHELL``
    overrides when it is a working bash or zsh. Only when no bash or zsh
    exists at all does the string go to /bin/sh, and ``note`` says so.
    """
    key = tuple(os.environ.get(k) for k in
                ("CTX_SHELL", "CLAUDECODE", "CLAUDE_CODE_SHELL", "SHELL", "PATH"))
    hit = _CHOICE.get(key)
    if hit is not None:
        return hit
    in_claude = os.environ.get("CLAUDECODE") == "1"
    explicit = [os.environ.get("CTX_SHELL")]
    if in_claude:
        explicit.append(os.environ.get("CLAUDE_CODE_SHELL"))
    explicit.append(os.environ.get("SHELL"))
    choice: tuple[str, str | None] | None = None
    for cand in explicit:
        if _working_shell(cand):
            choice = (str(cand), None)
            break
    if choice is None:
        found = _search(("zsh", "bash") if in_claude else ("bash", "zsh"))
        choice = (found, None) if found else ("/bin/sh", SH_FALLBACK_NOTE)
    _CHOICE[key] = choice
    return choice


def capture_shell() -> str:
    """The shell :func:`capture_shell_choice` picks."""
    return capture_shell_choice()[0]


def shell_popen_args(command: str) -> tuple[object, bool]:
    """``(popen_args, shell)`` that run ``command`` VERBATIM in that shell."""
    if os.name == "nt":  # pragma: no cover - POSIX shells only
        return command, True
    return [capture_shell(), "-c", command], False


def kill_and_reap(proc: subprocess.Popen) -> None:
    """Stop an owned process group and reap its leader, even if it exited.

    Call only for children started with ``start_new_session=True``. A dead
    leader can still have live descendants, so do not gate the group signal
    on ``poll()``. This is process cleanup, not containment of descendants
    that deliberately escape the group with ``setsid()``.
    """
    try:
        os.killpg(proc.pid, signal_mod.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def wait_or_kill(proc: subprocess.Popen, timeout: float | None, *, cancelled=None) -> bool:
    """Wait for ``proc``; on timeout SIGKILL its process group and reap it.

    Returns True iff the timeout expired. ``timeout=None`` waits forever.
    Interruption or another wait failure also stops the group, then propagates
    the original exception. It must never be reported as a successful run.

    The group kill is the point: both callers spawn with
    ``start_new_session=True``, so the child leads its own process group and
    ``killpg`` reaches anything it forked. ``ProcessLookupError`` (already
    gone) and ``PermissionError`` (unsignalable group) fall back to killing
    the leader, exactly as both copies did. The second ``wait()`` is not
    optional — without it the killed child stays a zombie.
    """
    try:
        if cancelled is None:
            proc.wait(timeout=timeout)
        else:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if cancelled():
                    kill_and_reap(proc)
                    return False
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    kill_and_reap(proc)
                    return True
                try:
                    proc.wait(timeout=min(0.05, remaining) if remaining is not None else 0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
    except subprocess.TimeoutExpired:
        kill_and_reap(proc)
        return True
    except BaseException:
        kill_and_reap(proc)
        raise
    if timeout == 0:
        # timeout=0 means the caller already detected the timeout elsewhere
        # (orchestrator._run_bounded: communicate() timed out) and is forcing
        # cleanup now. The leader may have exited already while an orphaned
        # grandchild keeps the process group alive, so wait() did not raise
        # and killpg still has to run. Improvement route, first live run.
        kill_and_reap(proc)
    return False


def exit_status(returncode: int | None) -> tuple[int | None, str | None]:
    """``Popen.returncode`` → the manifest's ``(exitCode, signal)`` pair.

    A negative ``returncode`` means death by signal N. That is reported as
    ``exitCode: null`` with the signal's name, never as a number — a run
    killed by SIGKILL is not a run that exited. An unrecognized signal number
    still gets a name (``SIG9``) rather than falling through as an exit code.
    """
    if returncode is not None and returncode < 0:
        try:
            return None, signal_mod.Signals(-returncode).name
        except ValueError:  # pragma: no cover - exotic signal number
            return None, f"SIG{-returncode}"
    return returncode, None


def native_exit_status(result: dict) -> int:
    """Shell-facing status, distinct from the command's typed termination.

    A timeout is 124. A signal is 128+N, while the manifest continues to
    carry exitCode=null and signal=SIGNAME. Unknown status must fail.
    """
    if result.get("timedOut"):
        return 124
    name = result.get("signal")
    if name:
        try:
            number = int(signal_mod.Signals[str(name)])
        except (KeyError, ValueError):
            try:
                number = int(str(name).removeprefix("SIG"))
            except ValueError:
                return 1
        return 128 + number if 0 < number < 128 else 1
    code = result.get("exitCode")
    return code if type(code) is int and 0 <= code <= 255 else 1
