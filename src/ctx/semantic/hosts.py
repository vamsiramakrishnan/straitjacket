"""Use a configured ACP agent as a JSON worker; never trust generated usage."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile

from ctx.acp import launch
from ctx.semantic.contract import parse_json
from ctx.semantic.worker import WorkerResult
from ctx.store import canonical_json


class ACPWorker:
    """The agent owns authentication and inference; ctx owns tool execution.

    No MCP, file, or terminal capabilities are supplied. The agent receives
    frozen evidence in a disposable cwd. This is not an OS sandbox: configure
    the agent's sandbox for untrusted agents or repositories.
    """
    def __init__(self, endpoint, *, cancelled=None):
        self.endpoint = replace(endpoint, command=tuple(endpoint.command), permissions="deny")
        self.cancelled = cancelled

    def __call__(self, data, *, timeout, response_bytes):
        with tempfile.TemporaryDirectory(prefix="ctx-analysis-") as directory:
            code, text, error, _ = launch(self.endpoint, Path(directory), data.decode(), "",
                                          timeout=timeout, with_tools=False, cancelled=self.cancelled)
        output = text.encode()
        if len(output) > response_bytes:
            return WorkerResult(output[:response_bytes], error="output_limit", returncode=code)
        if code:
            return WorkerResult(output, error.encode()[:min(16000, response_bytes)], "agent_failed", code)
        try:
            value = parse_json(output)
            if not isinstance(value, dict):
                raise ValueError
            # ACP launch currently supplies no authenticated usage envelope.
            # Even if the model emits plausible numbers they are discarded.
            value["usage"] = {}
            output = canonical_json(value)
        except ValueError:
            return WorkerResult(output, error="invalid_agent_json", returncode=code)
        return WorkerResult(output, returncode=code)
