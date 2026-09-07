"""One explicit JSON-in/JSON-out worker; bounded pipes and owned-process cleanup.

The driver owns provider authentication and model invocation. Its temporary
working directory is not an OS sandbox. No workspace tools, MCP server, or
editing capability are injected by this transport.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time

from ctx._proc import kill_and_reap


@dataclass(frozen=True)
class WorkerResult:
    stdout: bytes = b""
    stderr: bytes = b""
    error: str | None = None
    returncode: int | None = None


class CommandWorker:
    """Callable SDK seam: (request_bytes, *, timeout, response_bytes) -> result.

    A custom SDK worker must honor timeout/output limits and propagate
    cancellation. The built-in transport enforces them on owned processes.
    The driver must apply max_output_tokens at the provider boundary; ctx
    cannot make provider-side spend a hard guarantee.
    """

    def __init__(self, command: list[str]):
        self.command = command

    def __call__(self, request: bytes, *, timeout: float, response_bytes: int) -> WorkerResult:
        deadline = time.monotonic() + timeout
        output = {"stdout": bytearray(), "stderr": bytearray()}
        error = None
        with tempfile.TemporaryDirectory(prefix="ctx-semantic-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_bytes(request)
            with request_path.open("rb") as stdin:
                try:
                    proc = subprocess.Popen(self.command, stdin=stdin, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, cwd=directory,
                                            start_new_session=True)
                except OSError:
                    return WorkerResult(error="launch_failed")
            try:
                with selectors.DefaultSelector() as selector:
                    for name in output:
                        stream = getattr(proc, name)
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, name)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            error = "timeout"
                            break
                        for key, _ in selector.select(min(remaining, 0.05)):
                            cap = response_bytes if key.data == "stdout" else min(response_bytes, 16000)
                            data = os.read(key.fd, min(8192, cap - len(output[key.data]) + 1))
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            room = cap - len(output[key.data])
                            output[key.data].extend(data[:room])
                            if len(data) > room:
                                error = "output_limit"
                                break
                        if error:
                            break
                    if not error:
                        try:
                            proc.wait(timeout=max(0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            error = "timeout"
            finally:
                # Also covers a leader that exits while descendants hold a pipe
                # open, and Ctrl-C while select/read/wait is in progress.
                kill_and_reap(proc)
                proc.stdout.close()
                proc.stderr.close()
            if not error and proc.returncode != 0:
                error = "worker_failed"
            return WorkerResult(bytes(output["stdout"]), bytes(output["stderr"]), error, proc.returncode)
