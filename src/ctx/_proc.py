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

__all__ = ["exit_status", "wait_or_kill"]


def wait_or_kill(proc: subprocess.Popen, timeout: float | None) -> bool:
    """Wait for ``proc``; on timeout SIGKILL its process group and reap it.

    Returns True iff the timeout expired. ``timeout=None`` waits forever.

    The group kill is the point: both callers spawn with
    ``start_new_session=True``, so the child leads its own process group and
    ``killpg`` reaches anything it forked. ``ProcessLookupError`` (already
    gone) and ``PermissionError`` (unsignalable group) fall back to killing
    the leader, exactly as both copies did. The second ``wait()`` is not
    optional — without it the killed child stays a zombie.
    """
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        return True
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
