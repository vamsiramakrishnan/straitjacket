"""`ctx py`: programmable capture — a Python script runs under the birth
gate and only its bounded digest returns.

The Maki absorption (maki.sh, studied 2026-07-18): its sandboxed interpreter
lets the model chain N operations in one script so intermediates never reach
the transcript — the collapse `ctx seq` performs for declared trees,
generalized to computed control flow (branch on a result, loop over files,
aggregate before emitting). Maki's limit is the usual one: the script and
its intermediates vanish into the chat log with no address. Under the
harness the collapse keeps provenance: the script is a content-addressed
blob cited in the manifest and the digest header, both streams are
span-addressable blobs, and sub-steps that deserve their own handles opt in
by calling `ctx run` from inside the script.

Trust envelope: identical to `ctx run` — the script executes with the
caller's privileges. This verb is bounded capture, not OS isolation (that
is the broker's job, Phase 3). The interpreter runs in isolated mode
(`python -I`: no user site, no PYTHONPATH/cwd injection) with the script
fed on stdin from its stored bytes, so tracebacks say `File "<stdin>"` and
host interpreter paths never appear in manifests or digests.
"""

from __future__ import annotations

import sys

from ctx.execution import ExecutionError, run_capture
from ctx.store import Store
from ctx.textutil import short_id
from ctx.workspace import Workspace

# Model-visible argv: reproduce with `ctx get blob:<script> | python3 -I -`.
# The executed argv uses sys.executable, which is host-specific and must
# never reach a manifest (record_argv substitution in run_capture).
_RECORD_ARGV = ["python3", "-I", "-"]


def run_eval(
    ws: Workspace,
    store: Store,
    script: str,
    *,
    timeout: float | None = 600.0,
    cwd: str | None = None,
    focus: str | None = None,
) -> tuple[str, int]:
    """Execute a Python script under birth-gate capture.

    Returns (digest_text, exit_code, timed_out). ``timed_out`` is the ONLY
    trustworthy timeout signal: exit code 124 is by convention a timeout, but
    it is also a value a script may legitimately return, and overloading the
    two made ``sys.exit(124)`` indistinguishable from a real kill. The code is
    still 124 on timeout for the documented CLI contract; callers that need to
    know WHY must read the flag. Historically returned (digest_text,
    otherwise the script's own exit code (signal death maps to 1).
    """
    from ctx.digest import render_run_digest

    if not script.strip():
        raise ExecutionError("empty script")
    data = script.encode("utf-8")
    script_blob = store.put_blob(data)
    lines = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)

    capture = run_capture(
        ws,
        [sys.executable, "-I", "-"],
        cwd=cwd,
        timeout=timeout,
        store=store,
        stdin_bytes=data,
        record_argv=list(_RECORD_ARGV),
    )
    # Provenance rides in the final (digest-bearing) manifest: the script is
    # part of the invocation's identity, addressable independently of it.
    capture.manifest["eval"] = {"script": f"sha256:{script_blob}", "lines": lines}
    digest, manifest = render_run_digest(store, ws, capture.manifest, focus=focus, op="eval")

    unit = "line" if lines == 1 else "lines"
    header = f"[ctx py · script blob:{short_id(script_blob)} · {lines} {unit}]"
    result = manifest["result"]
    timed_out = bool(result["timedOut"])
    if timed_out:
        code = 124
    elif result["exitCode"] is None:
        code = 1  # killed by signal
    else:
        code = int(result["exitCode"])
    # The digest must not claim a timeout the runner did not observe: a script
    # that chose 124 itself gets its own exit code reported as its own.
    if code == 124 and not timed_out:
        header += "  [exit 124 chosen by the script; not a timeout]"
    return header + "\n" + digest, code, timed_out
