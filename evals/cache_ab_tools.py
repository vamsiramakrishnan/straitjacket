"""Matched, bounded tool surface for the cache A/B evaluation.

The shell tool interprets an allowlisted argv grammar; it never launches a
shell. Fixture Python still executes candidate source and is not an OS sandbox.
Provider credentials are excluded from every child environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time

from evals.cache_ab_tasks import Fixture, integrity_errors

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("native", "explore", "contain", "full")
CAPTURE_CLEANUP_MARGIN_S = 0.5
CAPTURE_CLEANUP_FRACTION = 0.2
TOOLS = [
    {"name": "shell", "description": "Run one allowed read, diagnostic or ctx command; no pipes or shell operators.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                      "required": ["command"], "additionalProperties": False}},
    {"name": "write_file", "description": "Replace one declared editable source file with complete UTF-8 content.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"], "additionalProperties": False}},
]


def child_env(state_dir: Path) -> dict[str, str]:
    """Explicit allowlist: never inherit API keys, Python hooks or ctx config."""
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    executable_dirs = [str(Path(sys.executable).parent)]
    executable_dirs.extend(str(Path(p).parent) for name in ("rg", "ls", "cat", "sed")
                           if (p := shutil.which(name)))
    return {
        "PATH": os.pathsep.join(dict.fromkeys(executable_dirs)) + os.pathsep + os.defpath,
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "CTX_STATE_HOME": str(state_dir), "CTX_PICKER_MODE": "local",
        "CTX_MAP_ENGINE": "builtin", "CTX_CODE_ENGINE": "ast",
        "CTX_SEARCH_ENGINE": "python", "CTX_SEARCH_INDEX": "off", "CTX_NO_CTAGS": "1",
    }


def ctx_command(args: list[str]) -> list[str]:
    bootstrap = f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); from ctx.cli import main; sys.exit(main())"
    return [sys.executable, "-I", "-B", "-c", bootstrap, *args]


def _run(argv: list[str], *, cwd: Path, env: dict, timeout_s: float, limit: int) -> dict:
    """Bound output while reading; kill the process group on timeout/overflow."""
    start = time.monotonic()
    if timeout_s <= 0:
        return {"text": "Tool deadline exhausted", "exit_code": None,
                "error": "timeout", "raw_bytes": 0, "elapsed_s": 0.0}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    data = bytearray()
    error = None
    assert proc.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = timeout_s - (time.monotonic() - start)
                if remaining <= 0:
                    error = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    data.extend(chunk)
                    if len(data) > limit:
                        error = "output_limit"
                        break
                if error:
                    break
        if error:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=max(0.01, timeout_s - (time.monotonic() - start)))
        except subprocess.TimeoutExpired:
            error = "timeout"
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    # Overflow is an explicit failed tool result, never a shortened success.
    text = data.decode("utf-8", errors="replace") if not error else f"Tool failed: {error}; captured {len(data)} bytes."
    return {"text": text, "exit_code": proc.returncode, "error": error,
            "raw_bytes": len(data), "elapsed_s": time.monotonic() - start}


class ToolExecutor:
    tools = TOOLS

    def __init__(self, fixture: Fixture, arm: str, state_dir: Path,
                 timeout_s: float = 10.0, output_limit_bytes: int = 1048576):
        if arm not in ARMS:
            raise ValueError("unknown arm")
        if timeout_s <= 0 or output_limit_bytes <= 0:
            raise ValueError("positive tool bounds required")
        self.fixture, self.arm = fixture, arm
        self.state_dir = Path(state_dir).resolve()
        if self.state_dir.is_relative_to(fixture.root):
            raise ValueError("state directory must be outside the candidate")
        self.timeout_s, self.output_limit_bytes = timeout_s, output_limit_bytes
        self.env = child_env(self.state_dir)
        self.records: list[dict] = []

    def _path(self, value: str, *, directory: bool = False) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("only relative fixture paths are allowed")
        target = self.fixture.root / path
        if any(p.is_symlink() for p in (target, *target.parents)
               if p.is_relative_to(self.fixture.root)):
            raise ValueError("symlinks are not allowed")
        rel = path.as_posix()
        if rel in self.fixture.initial:
            return rel
        if directory and (rel == "." or any(name.startswith(rel.rstrip("/") + "/") for name in self.fixture.initial)):
            return rel
        raise ValueError("path is outside declared fixture files")

    def _native(self, argv: list[str]) -> tuple[list[str], bool]:
        cmd, *args = argv
        if cmd in ("python", "python3"):
            if len(args) != 1 or args[0] not in ("diagnose.py", "run.py", "tests/verify.py"):
                raise ValueError("Python is limited to fixture diagnostics and tests/verify.py")
            return ["python3", "-I", "-B", self._path(args[0])], True
        if cmd == "cat" and args:
            return [cmd, "--", *[self._path(p) for p in args]], False
        if cmd == "ls":
            flags = [p for p in args if p.startswith("-")]
            if any(p not in ("-a", "-l", "-la", "-al") for p in flags):
                raise ValueError("unsupported ls flag")
            paths = [self._path(p, directory=True) for p in args if not p.startswith("-")]
            return [cmd, *flags, "--", *(paths or ["."])], False
        if cmd == "sed" and len(args) == 3 and args[0] == "-n" and re.fullmatch(r"\d+(?:,\d+)?p", args[1]):
            return [cmd, "-n", args[1], self._path(args[2])], False
        if cmd == "rg":
            flags = []
            while args and args[0].startswith("-"):
                flag = args.pop(0)
                if flag not in ("-n", "-i", "-F", "-l", "-c", "--files"):
                    raise ValueError("unsupported rg flag")
                flags.append(flag)
            if "--files" in flags:
                return [cmd, *flags, *[self._path(p, directory=True) for p in args]], False
            if not args:
                raise ValueError("rg requires a pattern")
            pattern = args.pop(0)
            return [cmd, *flags, "--", pattern, *[self._path(p, directory=True) for p in args]], False
        raise ValueError("allowed native commands: ls, rg, cat, sed -n, python3 fixture-script")

    def _ctx(self, argv: list[str]) -> list[str]:
        if not argv:
            raise ValueError("ctx command required")
        op, *args = argv
        if op in ("map", "def") and self.arm not in ("explore", "full"):
            raise ValueError("context exploration is disabled in this arm")
        if op == "map":
            if not args or (len(args) == 2 and args[0] == "--budget" and args[1].isdigit() and 1 <= int(args[1]) <= 2000):
                return argv
        elif op == "def" and len(args) == 1 and args[0].startswith("repo:"):
            value = args[0][5:]
            path, separator, symbol = value.rpartition(":")
            if separator and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", symbol):
                self._path(path)
                return argv
        elif op == "search" and len(args) >= 2:
            ref = args[0]
            if ref.startswith("repo:"):
                if self.arm not in ("explore", "full"):
                    raise ValueError("context exploration is disabled in this arm")
                self._path(ref[5:], directory=True)
            elif self.arm not in ("contain", "full") or not re.fullmatch(r"(?:run|blob|snap):[0-9a-f]{12,64}(?:#(?:stdout|stderr))?", ref):
                raise ValueError("invalid retrieval search handle")
            remaining = list(args[1:])
            patterns = []
            while remaining:
                token = remaining.pop(0)
                if token in ("--context", "--max-matches"):
                    if not remaining or not remaining[0].isdigit():
                        raise ValueError("search bounds must be integers")
                    value = int(remaining.pop(0))
                    if not 0 <= value <= (20 if token == "--context" else 80):
                        raise ValueError("search bound exceeds protocol limit")
                elif token in ("--fixed", "--all"):
                    continue
                elif token.startswith("-"):
                    raise ValueError("unsupported ctx search option")
                else:
                    patterns.append(token)
            if not patterns:
                raise ValueError("ctx search requires a pattern")
            return argv
        elif op == "get" and self.arm in ("contain", "full") and len(args) == 3:
            ref, selector, value = args
            if ref.startswith("repo:"):
                self._path(ref[5:])
            elif not re.fullmatch(r"(?:run|blob|snap):[0-9a-f]{12,64}(?:#(?:stdout|stderr))?", ref):
                raise ValueError("invalid retrieval handle")
            if selector in ("--lines", "--bytes") and re.fullmatch(r"[1-9]\d*:[1-9]\d*", value):
                return argv
            if selector == "--span" and re.fullmatch(r"[A-Za-z0-9:_-]+", value):
                return argv
        raise ValueError("unsupported ctx command; use map, search repo:., def repo:file:symbol, or get HANDLE --lines A:B")

    def _capture_metadata(self, result: dict) -> None:
        match = re.search(r"\brun:([0-9a-f]{12,64})\b", result["text"])
        if not match:
            raise RuntimeError("ctx capture emitted no run handle")
        candidates = list(self.state_dir.glob(f"workspaces/*/manifests/{match.group(1)}*.json"))
        if len(candidates) != 1:
            raise RuntimeError("ctx run manifest is ambiguous or missing")
        manifest = json.loads(candidates[0].read_text(encoding="utf-8"))
        result["captured_ref"] = "run:" + match.group(1)
        result["exit_code"] = manifest["result"]["exitCode"]
        result["raw_bytes"] = sum(s["bytes"] for s in manifest["streams"].values())
        if manifest["result"].get("timedOut"):
            result["error"] = "timeout"
        if manifest["result"].get("outputLimited"):
            result["error"] = "output_limit"

    def execute(self, name: str, arguments: dict) -> dict:
        start = time.monotonic()
        try:
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            errors = integrity_errors(self.fixture)
            if errors:
                raise ValueError("fixture integrity failed: " + "; ".join(errors))
            if name == "write_file":
                if set(arguments) != {"path", "content"} or not all(isinstance(v, str) for v in arguments.values()):
                    raise ValueError("write_file requires path and content strings")
                path = self._path(arguments["path"])
                if path not in self.fixture.editable:
                    raise ValueError("file is immutable")
                data = arguments["content"].encode("utf-8")
                if len(data) > self.output_limit_bytes:
                    raise ValueError("source exceeds byte limit")
                (self.fixture.root / path).write_bytes(data)
                result = {"text": f"Wrote {path} ({len(data)} bytes)", "exit_code": 0, "error": None, "raw_bytes": 0}
            elif name == "shell":
                if set(arguments) != {"command"} or not isinstance(arguments["command"], str):
                    raise ValueError("shell requires one command string")
                argv = shlex.split(arguments["command"])
                if not argv or any(p in (";", "&&", "||", "|", ">", ">>", "<", "&") for p in argv):
                    raise ValueError("shell operators are not allowed")
                contained = False
                capture_timeout = None
                if argv[0] == "ctx":
                    command = ctx_command(self._ctx(argv[1:]))
                else:
                    command, _ = self._native(argv)
                    contained = self.arm in ("contain", "full")
                    if contained:
                        # ctx launches its captured child in a separate process
                        # group. Let ctx reap that child before the outer wall
                        # deadline can terminate the capture/render process.
                        margin = max(CAPTURE_CLEANUP_MARGIN_S,
                                     self.timeout_s * CAPTURE_CLEANUP_FRACTION)
                        capture_timeout = max(0.01, self.timeout_s - margin)
                        command = ctx_command(["run", "--timeout", str(capture_timeout), "--", *command])
                result = _run(command, cwd=self.fixture.root, env=self.env,
                              timeout_s=self.timeout_s, limit=self.output_limit_bytes)
                result["tool_timeout_s"] = self.timeout_s
                if capture_timeout is not None:
                    result["capture_command_timeout_s"] = capture_timeout
                if contained and not result["error"]:
                    self._capture_metadata(result)
            else:
                raise ValueError("unknown tool")
        except (ValueError, OSError, RuntimeError) as exc:
            result = {"text": f"Tool rejected: {exc}", "exit_code": None,
                      "error": type(exc).__name__, "raw_bytes": 0}
        result["elapsed_s"] = time.monotonic() - start
        self.records.append({"name": name, "arguments": arguments, **result})
        return result
