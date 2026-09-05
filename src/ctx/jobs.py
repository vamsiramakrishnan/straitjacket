"""Long-runner backgrounding: `ctx run --bg[-after T]`, `ctx job`, `ctx jobs`.

When a process outlives its foreground patience window it is backgrounded:
its output only ever exists as spool files under the store's audit area
(outside the repository), and the transcript gets a bounded status line plus
a job handle. Finalization turns the spools into ordinary content-addressed
stream blobs plus a REAL ``ctx.invocation/v1`` run manifest — `ctx search
run:<id> ...` and `ctx get run:<id>#stdout --lines A:B` work identically to
a foreground capture.

Architecture: EVERY --bg / --bg-after run starts under a small detached
supervisor process (``python -m ctx job _supervise <jobdir>``,
start_new_session) that spools the child's stdout/stderr and records state
transitions atomically (temp+rename, the store._atomic_write pattern). The
foreground ``ctx run`` merely polls the job directory: child done within
the window → finalize and print the normal digest, byte-identical to a
plain foreground run; still running → bounded status + handle, exit 0.
The supervisor survives the foreground process exiting — that is the point.

Determinism: the finalized manifest is a pure function of captured bytes +
normalized invocation (argv, shell flag, cwd, worktree state) + digest
policy. Job ids, pids, and timestamps are OPERATIONAL identity only — they
live in meta.json and the jobs listing, and never enter the manifest or the
digest body (identical bytes + argv ⇒ identical manifest id).

Single-writer discipline (race containment):
  * ``meta.json``  — written once by ``start_job`` before the supervisor
    exists, then owned exclusively by the supervisor (launching → running →
    done | failed). Everyone else only reads it. The one sanctioned
    takeover is orphan adoption: supervisor AND child both dead while the
    state still says running, re-checked after a re-read.
  * ``kill``       — marker file, created by ``ctx job --kill`` *before*
    signalling, so any later finalizer sees the intent.
  * ``finalized.json`` / ``digest`` — written only by finalizers; the whole
    path is idempotent (content-addressed blobs and manifest), so two
    concurrent finalizers converge on byte-identical results.
"""

from __future__ import annotations

from ctx import bounds

import json
import os
import re
import signal as signal_mod
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ctx._proc import exit_status, wait_or_kill
from ctx.store import Store, _atomic_write
from ctx.textutil import short_id
from ctx.workspace import Workspace


class JobError(Exception):
    pass


_POLL_S = 0.04  # local-disk state poll; latency floor for inline finalize
_CLIP_COLS = 200  # spool display: clip long lines at this many chars
_MAX_STATUS_LINES = 40  # hard bound on spool lines shown in one status


# ------------------------------------------------------------------ layout
def jobs_root(store: Store) -> Path:
    """Jobs live under the store's audit area: operational, outside the
    repository (no pollution), never part of content identity."""
    return store.audit_dir / "jobs"


def _job_dir(store: Store, job_id: str) -> Path:
    return jobs_root(store) / job_id


def _new_job_id() -> str:
    # Operational identity, never content identity: 12 hex chars of OS
    # randomness name the job directory and the transcript handle. The
    # id never appears in the finalized manifest or the digest body —
    # content identity there is the sha256 of bytes + normalized argv.
    return os.urandom(6).hex()


def resolve_job_id(store: Store, ref: str) -> str:
    """Accept a full 12-hex id or a unique prefix (≥6 hex chars)."""
    ref = ref.strip().removeprefix("job:").lower()
    if not re.fullmatch(r"[0-9a-f]{6,12}", ref):
        raise JobError(f"invalid job id {ref!r} (12 hex chars, ≥6 for a prefix)")
    root = jobs_root(store)
    if (root / ref).is_dir() and len(ref) == 12:
        return ref
    matches = sorted(p.name for p in root.glob(ref + "*") if p.is_dir()) if root.is_dir() else []
    if not matches:
        raise JobError(f"unknown job {ref!r} in this workspace (see `ctx jobs`)")
    if len(matches) > 1:
        raise JobError(f"ambiguous job prefix {ref!r}: " + ", ".join(matches))
    return matches[0]


# -------------------------------------------------------------------- meta
def _write_meta(jobdir: Path, meta: dict[str, Any]) -> None:
    _atomic_write(jobdir / "meta.json", json.dumps(meta, sort_keys=True).encode("utf-8"))


def _read_meta(jobdir: Path) -> dict[str, Any]:
    try:
        return json.loads((jobdir / "meta.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JobError(f"unknown job {jobdir.name!r} in this workspace") from None
    except json.JSONDecodeError as e:  # pragma: no cover - atomic writes prevent this
        raise JobError(f"corrupt job metadata for {jobdir.name}: {e}") from None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists under another uid
        return True
    return True


def _adopt_if_orphaned(jobdir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """If both the supervisor and the child died without a state transition
    (host crash, external SIGKILL of the whole tree), take over meta once.
    Gated on a re-read so a supervisor writing 'done' concurrently wins."""
    if meta.get("state") == "launching":
        # Never left "launching": the supervisor died before its first
        # state write. Without this, `ctx job <id> --wait` polled forever.
        sup = meta.get("launcherSupervisorPid")
        if sup is None or _pid_alive(sup):
            return meta
        meta = _read_meta(jobdir)
        if meta.get("state") != "launching":
            return meta
        meta.update(
            state="failed",
            error="supervisor exited before the job started",
            endedAt=time.time(),
            orphaned=True,
        )
        _write_meta(jobdir, meta)
        return meta
    if meta.get("state") != "running":
        return meta
    if _pid_alive(meta.get("supervisorPid")) or _pid_alive(meta.get("pid")):
        return meta
    meta = _read_meta(jobdir)  # re-read: the supervisor may have just written
    if meta.get("state") != "running":
        return meta
    meta.update(
        state="done",
        exitCode=None,
        signal="SIGKILL",
        timedOut=True,
        endedAt=time.time(),
        orphaned=True,
    )
    _write_meta(jobdir, meta)
    return meta


# ------------------------------------------------------------------- start
def start_job(
    ws: Workspace,
    store: Store,
    argv: list[str],
    *,
    cwd: str | None = None,
    shell: bool = False,
    timeout: float | None = 600.0,
    focus: str | None = None,
) -> str:
    """Launch <argv> under a detached supervisor; return the job id.

    Workspace confinement mirrors execution.run_capture: the cwd must
    resolve inside the workspace root before anything is spawned.
    """
    if not argv:
        raise JobError("empty command")
    if shell and len(argv) != 1:
        raise JobError("--shell mode takes exactly one command string")
    workdir = ws.confine(cwd or ".", must_exist=True)
    rel_cwd = ws.relativize(workdir) or "."

    job_id = _new_job_id()
    jobdir = _job_dir(store, job_id)
    jobdir.mkdir(parents=True, exist_ok=False)
    (jobdir / "stdout").touch()
    (jobdir / "stderr").touch()
    meta: dict[str, Any] = {
        "schema": "ctx.job/v1",
        "argv": list(argv),
        "shell": bool(shell),
        "cwd": rel_cwd,  # model-visible form (manifest); abs path is operational
        "cwdAbs": str(workdir),
        "timeout": timeout,
        "focus": focus,
        "state": "launching",
        "createdAt": time.time(),
    }
    _write_meta(jobdir, meta)

    # Defensive PYTHONPATH: works for src layouts even without an install.
    import ctx as _ctx_pkg

    pkg_parent = str(Path(_ctx_pkg.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    sup_err = (jobdir / "supervisor.err").open("wb")
    try:
        sup = subprocess.Popen(
            [sys.executable, "-m", "ctx", "job", "_supervise", str(jobdir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sup_err,
            start_new_session=True,  # survives the foreground `ctx run` exiting
            env=env,
        )
    finally:
        sup_err.close()
    # Record the supervisor's pid while the job is still "launching", so a
    # supervisor that dies before its first state write (import failure,
    # OOM in the window) can be recognised instead of waited on forever.
    # Re-read first: the supervisor may already have written "running".
    current = _read_meta(jobdir)
    if current.get("state") == "launching":
        current["launcherSupervisorPid"] = sup.pid
        _write_meta(jobdir, current)
    return job_id


# -------------------------------------------------------------- supervisor
def supervise_main(jobdir_str: str) -> int:
    """`ctx job _supervise <jobdir>` — the hidden, detached supervisor.

    Dependency-free by design: everything it needs (argv, shell flag, abs
    cwd, timeout) is read from meta.json; it never resolves a workspace or
    opens the store. It is the sole writer of meta.json from here on.
    """
    jobdir = Path(jobdir_str)
    meta = json.loads((jobdir / "meta.json").read_text(encoding="utf-8"))
    argv = list(meta["argv"])
    shell = bool(meta["shell"])
    popen_args: Any = argv[0] if shell else argv

    with (jobdir / "stdout").open("wb") as out_fh, (jobdir / "stderr").open("wb") as err_fh:
        try:
            proc = subprocess.Popen(
                popen_args,
                cwd=meta["cwdAbs"],
                shell=shell,
                stdout=out_fh,
                stderr=err_fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own group: killable without touching us
            )
        except FileNotFoundError:
            meta.update(state="failed", error=f"command not found: {argv[0]}")
            _write_meta(jobdir, meta)
            return 1
        except OSError as e:
            meta.update(state="failed", error=f"spawn failed: {e}")
            _write_meta(jobdir, meta)
            return 1

        meta.update(
            state="running",
            pid=proc.pid,  # pgid == pid (start_new_session)
            supervisorPid=os.getpid(),
            startedAt=time.time(),
        )
        _write_meta(jobdir, meta)

        timed_out = wait_or_kill(proc, meta.get("timeout"))

    exit_code, sig_name = exit_status(proc.returncode)
    meta.update(
        state="done",
        exitCode=exit_code,
        signal=sig_name,
        timedOut=timed_out,
        endedAt=time.time(),
    )
    _write_meta(jobdir, meta)
    return 0


# ---------------------------------------------------------------- finalize
def finalize_job(ws: Workspace, store: Store, job_id: str) -> tuple[str, dict[str, Any]]:
    """Turn a done job's spools into a real run manifest + digest.

    Idempotent: a previously finalized job returns the stored digest and
    manifest. The manifest is synthesized in exactly the shape
    execution.run_capture produces, so the digest path (and therefore
    search/get/diff over run:<id>) behaves identically to a foreground
    capture — and identical bytes + argv yield an identical manifest id.
    """
    jobdir = _job_dir(store, job_id)
    fin_path = jobdir / "finalized.json"
    if fin_path.is_file():
        info = json.loads(fin_path.read_text(encoding="utf-8"))
        digest = (jobdir / "digest").read_text(encoding="utf-8")
        return digest, store.get_manifest(info["manifestId"])

    meta = _read_meta(jobdir)
    if meta.get("state") == "failed":
        raise JobError(f"job {job_id} failed to launch: {meta.get('error', 'unknown')}")
    if meta.get("state") != "done":
        raise JobError(f"job {job_id} is still {meta.get('state', 'unknown')}; not finalizable")

    from ctx.digest import render_run_digest
    from ctx.execution import invocation_manifest, stream_entries

    for name in ("stdout", "stderr"):
        if not (jobdir / name).exists():  # pragma: no cover - created at start
            (jobdir / name).touch()
    streams = stream_entries(store, {n: jobdir / n for n in ("stdout", "stderr")})

    exit_code = meta.get("exitCode")
    sig_name = meta.get("signal")
    timed_out = bool(meta.get("timedOut"))
    # A --kill lands as a timedOut-style result — but only when the SIGKILL
    # actually took the process (kill racing a clean exit honors the real
    # exit status rather than fabricating a timeout).
    if (jobdir / "kill").exists() and sig_name == "SIGKILL":
        timed_out = True

    # The shared shape (R7). This used to be a hand-kept copy of
    # run_capture's literal, with a comment promising it stayed "exactly the
    # shape run_capture produces"; the promise is now structural, which is
    # what makes identical bytes + argv yield an identical manifest id
    # whether the command ran in the foreground or under a supervisor.
    manifest = invocation_manifest(
        ws,
        cwd=meta["cwd"],
        argv=list(meta["argv"]),
        shell=bool(meta["shell"]),
        exit_code=exit_code,
        signal=sig_name,
        timed_out=timed_out,
        streams=streams,
    )
    mid = store.put_manifest(manifest, kind="run")
    manifest["id"] = f"sha256:{mid}"
    digest, final_manifest = render_run_digest(store, ws, manifest, focus=meta.get("focus"))

    _atomic_write(jobdir / "digest", digest.encode("utf-8"))
    _atomic_write(
        fin_path,
        json.dumps(
            {"manifestId": final_manifest["id"].removeprefix("sha256:")}, sort_keys=True
        ).encode("utf-8"),
    )
    return digest, final_manifest


# -------------------------------------------------------------- wait / kill
def wait_for_done(store: Store, job_id: str, *, timeout: float | None = None) -> bool:
    """Poll until the job leaves running states. True = finalizable/finalized,
    raises JobError on a failed launch, False = still running at timeout."""
    jobdir = _job_dir(store, job_id)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if (jobdir / "finalized.json").is_file():
            return True
        meta = _read_meta(jobdir)
        if meta.get("state") in ("running", "launching"):
            meta = _adopt_if_orphaned(jobdir, meta)
        if meta.get("state") == "done":
            return True
        if meta.get("state") == "failed":
            raise JobError(f"job {job_id} failed to launch: {meta.get('error', 'unknown')}")
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)


def kill_job(ws: Workspace, store: Store, job_id: str, *, settle_s: float = 8.0) -> tuple[str, dict[str, Any]]:
    """SIGKILL the job's process group and finalize what spooled."""
    jobdir = _job_dir(store, job_id)
    if (jobdir / "finalized.json").is_file():
        return finalize_job(ws, store, job_id)
    # Marker before signal: any finalizer that runs later sees the intent.
    _atomic_write(jobdir / "kill", b"")
    deadline = time.monotonic() + settle_s
    # The pid appears only once the supervisor reaches "running".
    while True:
        meta = _read_meta(jobdir)
        state = meta.get("state")
        if state in ("done", "failed") or meta.get("pid"):
            break
        if time.monotonic() >= deadline:
            raise JobError(f"job {job_id} never started; nothing to kill")
        time.sleep(_POLL_S)
    if state == "running":
        try:
            os.killpg(int(meta["pid"]), signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone (or unsignalable): the supervisor settles state
        # Let the supervisor record the death; adopt only if it died too.
        while time.monotonic() < deadline:
            meta = _read_meta(jobdir)
            if meta.get("state") != "running":
                break
            meta = _adopt_if_orphaned(jobdir, meta)
            if meta.get("state") != "running":
                break
            time.sleep(_POLL_S)
        if meta.get("state") == "running":
            raise JobError(f"job {job_id}: kill signalled but state did not settle")
    return finalize_job(ws, store, job_id)


# ------------------------------------------------------------------ status
def _clip(line: str) -> str:
    return line if len(line) <= _CLIP_COLS else line[: _CLIP_COLS - 1] + "…"


def _tail_of(lines: list[str], tail: int) -> list[str]:
    """The last ``tail`` lines -- and NO lines when ``tail`` is 0.

    `lines[-tail:]` is `lines[0:]` at zero, so `ctx job <id> --tail 0`, an
    explicit request for no live tail, dumped the entire spool. Negative
    indexing turning a request for nothing into a request for everything is
    the same shape ctx.bounds exists for; it just was not spelled `max(1, n)`,
    which is all the adoption test was looking for.
    """
    n = bounds.count(tail)
    return lines[len(lines) - n :] if n else []


def _spool_excerpt(path: Path, head: int, tail: int) -> list[str]:
    """Bounded head/tail of a (possibly still-growing) spool file. Reads at
    most 64 KiB from each end; never returns more than head+tail+1 lines."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    window = 64 * 1024
    with path.open("rb") as fh:
        head_text = fh.read(window).decode("utf-8", "replace")
        if size <= window:
            lines = head_text.splitlines()
            if len(lines) <= head + tail:
                return [_clip(ln) for ln in lines]
            omitted = len(lines) - head - tail
            return (
                [_clip(ln) for ln in lines[:head]]
                + [f"... ({omitted} lines omitted) ..."]
                + [_clip(ln) for ln in _tail_of(lines, tail)]
            )
        fh.seek(size - window)
        tail_text = fh.read(window).decode("utf-8", "replace")
    head_lines = head_text.splitlines()[:head]
    tail_lines = tail_text.splitlines()[1:]  # first tail line is likely partial
    return (
        [_clip(ln) for ln in head_lines]
        + ["... (middle omitted; spool exceeds preview window) ..."]
        + [_clip(ln) for ln in _tail_of(tail_lines, tail)]
    )


def _spool_lines_so_far(path: Path) -> int:
    from ctx.execution import _count_lines

    try:
        return _count_lines(path)
    except OSError:
        return 0


def _runtime_s(meta: dict[str, Any]) -> int:
    start = meta.get("startedAt") or meta.get("createdAt") or time.time()
    end = meta.get("endedAt") or time.time()
    return max(0, int(end - start))


def _command_display(meta: dict[str, Any]) -> str:
    cmd = meta["argv"][0] if meta.get("shell") else " ".join(meta["argv"])
    return cmd if len(cmd) <= 120 else cmd[:119] + "…"


def backgrounded_status(store: Store, job_id: str) -> str:
    """The bounded handoff `ctx run --bg` prints when the patience window
    expires. Runtime is coarse whole seconds — operational, never identity."""
    jobdir = _job_dir(store, job_id)
    meta = _read_meta(jobdir)
    n = _spool_lines_so_far(jobdir / "stdout")
    return "\n".join(
        [
            f"[ctx job:{job_id} backgrounded · running {_runtime_s(meta)}s · "
            f"stdout {n} lines so far]",
            "next:",
            f"  ctx job {job_id}            # bounded status + live tail",
            f"  ctx job {job_id} --tail 40  # more of the live tail",
            f"  ctx job {job_id} --wait     # block until done, then digest",
        ]
    )


def job_status(store: Store, job_id: str, *, tail: int | None = None) -> str:
    """`ctx job <id>` on a non-finalized job: bounded live view of the spool.
    Never more than ~40 spool lines; long lines clipped."""
    jobdir = _job_dir(store, job_id)
    meta = _read_meta(jobdir)
    if meta.get("state") in ("running", "launching"):
        meta = _adopt_if_orphaned(jobdir, meta)
    state = meta.get("state", "unknown")
    if state == "failed":
        return f"[ctx job:{job_id} failed]\nerror: {meta.get('error', 'unknown')}"

    out_n = _spool_lines_so_far(jobdir / "stdout")
    err_n = _spool_lines_so_far(jobdir / "stderr")
    lines = [
        f"[ctx job:{job_id} {state} · runtime {_runtime_s(meta)}s · "
        f"stdout {out_n} lines so far · stderr {err_n} lines]",
        f"command: {_command_display(meta)}",
    ]
    if tail is not None:
        tail = min(bounds.count(tail), _MAX_STATUS_LINES)
        head_n, tail_n = 0, tail
    else:
        head_n, tail_n = 6, 18
    excerpt = _spool_excerpt(jobdir / "stdout", head_n, tail_n)
    if excerpt:
        lines.append("stdout (live tail):")
        lines.extend("  " + ln for ln in excerpt)
    if err_n:
        lines.append("stderr (live tail):")
        lines.extend("  " + ln for ln in _spool_excerpt(jobdir / "stderr", 0, 6))
    lines += [
        "next:",
        f"  ctx job {job_id} --wait     # block until done, then digest",
        f"  ctx job {job_id} --tail 40  # more of the live tail",
        f"  ctx job {job_id} --kill     # SIGKILL the group; finalize what spooled",
    ]
    return "\n".join(lines)


def job_state(store: Store, job_id: str) -> str:
    """Coarse lifecycle state: launching | running | done | failed | finalized."""
    jobdir = _job_dir(store, job_id)
    if (jobdir / "finalized.json").is_file():
        return "finalized"
    meta = _read_meta(jobdir)
    if meta.get("state") in ("running", "launching"):
        meta = _adopt_if_orphaned(jobdir, meta)
    return str(meta.get("state", "unknown"))


def list_jobs(store: Store) -> str:
    """`ctx jobs`: one bounded line per job. Ages are coarse whole seconds
    (operational output); digests themselves stay free of any of this."""
    root = jobs_root(store)
    entries: list[tuple[float, str, str]] = []
    for jobdir in sorted(root.iterdir()) if root.is_dir() else []:
        if not jobdir.is_dir():
            continue
        try:
            meta = _read_meta(jobdir)
        except JobError:
            continue
        jid = jobdir.name
        fin = jobdir / "finalized.json"
        if fin.is_file():
            try:
                short = short_id(json.loads(fin.read_text(encoding="utf-8"))["manifestId"])
            except (OSError, json.JSONDecodeError, KeyError):  # pragma: no cover
                short = "?"
            desc = f"finalized → run:{short}"
        else:
            if meta.get("state") in ("running", "launching"):
                meta = _adopt_if_orphaned(jobdir, meta)
            state = meta.get("state", "unknown")
            desc = state
            if state in ("running", "done"):
                desc += f" · runtime {_runtime_s(meta)}s"
            if state == "failed":
                desc += f" · {meta.get('error', 'unknown')}"
        entries.append(
            (float(meta.get("createdAt", 0.0)), jid, f"job:{jid}  {desc}  · {_command_display(meta)}")
        )
    if not entries:
        return "no jobs for this workspace"
    entries.sort()
    return "\n".join([f"[ctx jobs · {len(entries)}]"] + ["  " + e[2] for e in entries])
