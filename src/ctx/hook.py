"""PreToolUse context guard (SPEC §10.2, §11).

Latency contract: this module is on the hot path of every intercepted tool
call. It imports only stdlib modules that are already loaded by the CLI fast
path (json, os, re, sys, shlex, pathlib, tomllib) and never touches the
artifact store, git, or the network.

Output contract: exactly one JSON object on stdout for every code path.
Internal errors follow the configured policy — fail-open (`allow`) in the
default guarded mode, because a broken guard must not brick the workspace.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

DECISION_ALLOW = {"decision": "allow"}

# ---------------------------------------------------------------- classifier
# Commands whose output is unbounded or unknown: deny with a ctx remediation.
_UNBOUNDED_CMDS = {
    "pytest", "py.test", "jest", "vitest", "mocha", "tox", "nox",
    "make", "cmake", "ninja", "bazel", "mvn", "gradle", "gradlew",
    "cargo", "go", "tsc", "eslint", "ruff", "mypy", "pyright", "flake8", "pylint",
    "find", "rg", "grep", "egrep", "fgrep", "ag", "ack",
    "cat", "tac", "less", "more", "strings", "xxd", "hexdump", "od",
    "journalctl", "dmesg", "docker", "podman", "kubectl", "helm",
    "terraform", "pulumi", "aws", "gcloud", "az",
    "curl", "wget", "http", "httpie",
    "npm", "npx", "yarn", "pnpm", "bun", "pip", "uv", "poetry", "pipenv",
    "tree", "du", "ps", "lsof", "netstat", "ss", "strace", "ltrace",
}

# git subcommands that flood; the rest of git is judged separately.
_GIT_UNBOUNDED = {"log", "diff", "show", "blame", "reflog", "shortlog", "whatchanged"}

# Bounded-by-construction commands: allow natively.
_BOUNDED_CMDS = {
    "pwd", "whoami", "hostname", "true", "false", "echo", "printf",
    "which", "type", "basename", "dirname", "realpath", "date", "uname",
    "mkdir", "touch", "cd", "test", "sleep", "wc", "md5sum", "sha256sum",
}

# Shell metacharacters that make static reasoning unreliable.
_SHELL_META_RE = re.compile(r"[|;&<>`$(){}\\]|\|\||&&|\$\(")

_SECRET_PATH_RE = re.compile(
    r"(^|/)(\.env(\..*)?|\.aws|\.ssh|\.config/gcloud|secrets?|credentials?)(/|$)"
    r"|\.(pem|key)$|id_rsa|id_ed25519",
)

_HEAD_TAIL_MAX = 400  # max -n allowed for native head/tail

_MAX_INLINE_BYTES_DEFAULT = 16384


def _load_guard_policy(workspace_root: str | None) -> dict[str, Any]:
    """Minimal ctx.toml read for the guard section only. Never raises."""
    policy: dict[str, Any] = {
        "mode": "guarded",
        "unknown_command": "force_ask",
        "internal_error": "allow",
        "max_inline_bytes": _MAX_INLINE_BYTES_DEFAULT,
        "allow_commands": [],
        "deny_commands": [],
    }
    if not workspace_root:
        return policy
    path = Path(workspace_root) / "ctx.toml"
    if not path.is_file():
        return policy
    try:
        import tomllib

        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        guard = raw.get("guard") or {}
        budgets = raw.get("budgets") or {}
        policy["mode"] = str(guard.get("mode", policy["mode"]))
        policy["unknown_command"] = str(guard.get("unknown_command", policy["unknown_command"]))
        policy["internal_error"] = str(guard.get("internal_error", policy["internal_error"]))
        policy["max_inline_bytes"] = int(
            budgets.get("max_inline_bytes", policy["max_inline_bytes"])
        )
        # Repo-tunable classification: prefix matches against canonical argv.
        policy["allow_commands"] = [str(x) for x in guard.get("allow_commands", [])]
        policy["deny_commands"] = [str(x) for x in guard.get("deny_commands", [])]
    except Exception:
        pass
    return policy


# Wrappers that prefix another command; unwrap to classify the real program.
_WRAPPERS = {"env", "sudo", "doas", "nice", "nohup", "time", "stdbuf", "timeout", "command", "xvfb-run"}

# Redirection-only tail: `cmd ... > file 2>&1` — console output proven small.
_REDIR_ALL_RE = re.compile(
    r"^(?P<cmd>[^|;&<>`$(){}]+?)\s*(?:>>?\s*(?P<t1>\S+)\s*2>&1|&>>?\s*(?P<t2>\S+)|2>&1\s*>>?\s*(?P<t3>\S+))\s*$"
)


def _unwrap(argv: list[str]) -> list[str]:
    """Strip wrapper programs, env assignments, and their option arguments so
    classification sees the real command (defeats `env FOO=1 timeout 5 pytest`)."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        prog = os.path.basename(tok)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        if prog in _WRAPPERS:
            i += 1
            if prog == "timeout":  # skip duration (and -k/-s options)
                while i < len(argv) and argv[i].startswith("-"):
                    i += 2 if argv[i] in ("-k", "-s", "--signal", "--kill-after") else 1
                if i < len(argv):
                    i += 1  # the DURATION argument
            elif prog in ("stdbuf", "nice", "nohup", "xvfb-run"):
                while i < len(argv) and argv[i].startswith("-"):
                    i += 2 if argv[i] in ("-n", "--adjustment", "-s", "-a") else 1
            elif prog == "env":
                while i < len(argv) and (
                    argv[i].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i])
                ):
                    i += 1
            continue
        break
    return argv[i:]


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason}


def _force_ask(reason: str) -> dict[str, str]:
    return {"decision": "force_ask", "reason": reason}


def _remediation(argv: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in argv)
    return (
        "CTX_CONTEXT_GUARD: this command may emit unbounded output.\n"
        f"Run it as: ctx run -- {quoted}\n"
        "Then use ctx search/get/stats on the returned handle."
    )


def _resolve_workspace_root(payload: dict[str, Any]) -> str | None:
    """Longest containing workspacePath match against Cwd/Path/TargetFile
    (SPEC §5.1 rule 2). Never depends on the hook process CWD."""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    probe = None
    for key in ("Cwd", "cwd", "Path", "path", "TargetFile", "target_file", "file_path"):
        v = tool_input.get(key) or payload.get(key)
        if isinstance(v, str) and v:
            probe = v
            break
    ws_paths = (
        payload.get("workspacePaths")
        or payload.get("workspace_paths")
        or payload.get("workspaces")
        or []
    )
    if isinstance(ws_paths, str):
        ws_paths = [ws_paths]
    if not ws_paths:
        return probe if probe and os.path.isdir(probe) else payload.get("cwd")
    if probe:
        probe_abs = os.path.abspath(probe)
        best = None
        for wp in ws_paths:
            wp_abs = os.path.abspath(str(wp))
            if probe_abs == wp_abs or probe_abs.startswith(wp_abs + os.sep):
                if best is None or len(wp_abs) > len(best):
                    best = wp_abs
        if best:
            return best
    return os.path.abspath(str(ws_paths[0])) if len(ws_paths) == 1 else None


def _path_outside(path_str: str, workspace_root: str | None) -> bool:
    if not workspace_root:
        return False
    p = os.path.abspath(os.path.realpath(path_str))
    root = os.path.abspath(os.path.realpath(workspace_root))
    return not (p == root or p.startswith(root + os.sep))


def classify_command(command: str, policy: dict[str, Any], _depth: int = 0) -> dict[str, str]:
    """Classify a shell command string. Conservative and config-driven; not a
    shell-security parser (SPEC §11)."""
    stripped = command.strip()
    if not stripped or _depth > 3:
        return dict(DECISION_ALLOW) if not stripped else _force_ask(
            "CTX_CONTEXT_GUARD: deeply nested shell invocation; use ctx run --shell"
        )

    has_meta = bool(_SHELL_META_RE.search(stripped))

    # `cmd > file 2>&1`: both streams redirected to a real file — console
    # output is proven small, and the follow-up read of the file is itself
    # guarded (SPEC §11.2). Pseudo-devices would defeat the redirect.
    if has_meta:
        redir = _REDIR_ALL_RE.match(stripped)
        if redir:
            target = redir.group("t1") or redir.group("t2") or redir.group("t3") or ""
            if not target.startswith("/dev/") and not target.startswith("/proc/"):
                return dict(DECISION_ALLOW)

    try:
        argv = shlex.split(stripped)
    except ValueError:
        return _force_ask(
            "CTX_CONTEXT_GUARD: unparseable shell expression. "
            "If output may be large, use: ctx run --shell -- '<command>'"
        )
    if not argv:
        return dict(DECISION_ALLOW)

    argv = _unwrap(argv)
    if not argv:
        return dict(DECISION_ALLOW)
    prog = os.path.basename(argv[0])

    # Already routed through ctx → always allow.
    if prog == "ctx":
        return dict(DECISION_ALLOW)

    # Repo-configured overrides win over built-in tables.
    canonical = " ".join(argv)
    for prefix in policy.get("deny_commands", []):
        if canonical.startswith(prefix):
            return _deny(_remediation(argv))
    for prefix in policy.get("allow_commands", []):
        if canonical.startswith(prefix):
            return dict(DECISION_ALLOW)

    # `bash -c '<inner>'`: classify the inner command, not the shell.
    if prog in ("bash", "sh", "zsh", "dash", "fish") and len(argv) >= 3 and argv[1] == "-c":
        return classify_command(argv[2], policy, _depth + 1)

    if prog == "xargs":
        return _deny(_remediation(argv))

    if has_meta:
        # A pipeline containing head is not automatically safe (SPEC §11.2).
        return _force_ask(
            "CTX_CONTEXT_GUARD: compound shell expression with unproven output bound. "
            f"Prefer: ctx run --shell -- {shlex.quote(stripped)}"
        )

    # Bounded head/tail with explicit small -n.
    if prog in ("head", "tail"):
        n = _extract_line_count(argv)
        if n is not None and n <= _HEAD_TAIL_MAX and "-f" not in argv and "--follow" not in argv:
            return dict(DECISION_ALLOW)
        return _deny(_remediation(argv))

    if prog == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
        if sub in _GIT_UNBOUNDED:
            return _deny(_remediation(argv))
        if sub == "status" and not ("--short" in argv or "-s" in argv or "--porcelain" in argv):
            return _deny(_remediation(argv))
        return dict(DECISION_ALLOW)

    if prog == "ls":
        if any(a.startswith("-") and "R" in a for a in argv[1:]):
            return _deny(_remediation(argv))
        return dict(DECISION_ALLOW)

    if prog in ("python", "python3", "node", "ruby", "perl", "deno"):
        # Interpreter invocations can read anything (guard-bypass channel)
        # and emit anything; route through ctx.
        return _deny(_remediation(argv))

    if prog in _BOUNDED_CMDS:
        return dict(DECISION_ALLOW)

    if prog in _UNBOUNDED_CMDS:
        return _deny(_remediation(argv))

    # Unknown command → configured policy.
    unknown = policy.get("unknown_command", "force_ask")
    if unknown == "allow" or policy.get("mode") == "advisory":
        return dict(DECISION_ALLOW)
    if unknown == "deny":
        return _deny(_remediation(argv))
    return _force_ask(
        f"CTX_CONTEXT_GUARD: unknown output bound for {prog!r}. "
        f"If output may be large, run: ctx run -- {' '.join(shlex.quote(a) for a in argv)}"
    )


def _extract_line_count(argv: list[str]) -> int | None:
    for i, a in enumerate(argv[1:], start=1):
        if a in ("-n", "--lines") and i + 1 < len(argv):
            try:
                return abs(int(argv[i + 1].lstrip("+-")))
            except ValueError:
                return None
        if a.startswith("-n"):
            try:
                return abs(int(a[2:].lstrip("+-")))
            except ValueError:
                return None
        m = re.match(r"^-(\d+)$", a)
        if m:
            return int(m.group(1))
    return 10  # head/tail default is bounded


def classify_read(
    path_str: str, workspace_root: str | None, policy: dict[str, Any]
) -> dict[str, str]:
    if _SECRET_PATH_RE.search(path_str.replace("\\", "/")):
        return _force_ask(
            "CTX_CONTEXT_GUARD: secret-bearing path. Reading it requires an explicit "
            "user-visible permission step; it is excluded from automatic capture."
        )
    if _path_outside(path_str, workspace_root):
        return _force_ask(
            "CTX_CONTEXT_GUARD: path resolves outside the active workspace. "
            "Confirm outside-root access explicitly or pass --workspace."
        )
    try:
        size = os.stat(path_str).st_size
    except OSError:
        return dict(DECISION_ALLOW)  # nonexistent/new file: let the tool error
    limit = int(policy.get("max_inline_bytes", _MAX_INLINE_BYTES_DEFAULT))
    if size > limit:
        return _deny(
            f"CTX_CONTEXT_GUARD: file is {size} bytes (> {limit} inline budget).\n"
            f"Use: ctx get repo:<relative-path> --lines A:B\n"
            f"or:  ctx search repo:<relative-path> '<pattern>' --context 3"
        )
    return dict(DECISION_ALLOW)


def classify(payload: dict[str, Any]) -> dict[str, str]:
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    workspace_root = _resolve_workspace_root(payload)
    policy = _load_guard_policy(workspace_root)

    if policy.get("mode") == "advisory":
        return dict(DECISION_ALLOW)

    lowered = tool_name.lower()
    if "command" in lowered or lowered in ("bash", "shell", "exec"):
        command = ""
        for key in ("CommandLine", "command", "Command", "cmd"):
            v = tool_input.get(key)
            if isinstance(v, str):
                command = v
                break
        return classify_command(command, policy)

    if "read" in lowered or lowered in ("open_file", "view_file"):
        for key in ("AbsolutePath", "TargetFile", "file_path", "path", "Path"):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                return classify_read(v, workspace_root, policy)
        return dict(DECISION_ALLOW)

    if "list" in lowered or "find_by_name" in lowered or "grep" in lowered:
        # Directory listings and native search: allow shallow, redirect broad.
        recursive = bool(
            tool_input.get("Recursive") or tool_input.get("recursive")
        )
        if recursive:
            return _deny(
                "CTX_CONTEXT_GUARD: recursive listing/search may flood the transcript.\n"
                "Use: ctx search repo: '<pattern>' --glob '<glob>'  or  ctx stats repo:"
            )
        return dict(DECISION_ALLOW)

    return dict(DECISION_ALLOW)


def _to_claude_code_schema(decision: dict[str, str]) -> dict[str, Any]:
    """Translate the canonical decision into Claude Code's PreToolUse hook
    schema (hookSpecificOutput.permissionDecision: allow|deny|ask)."""
    mapping = {"allow": "allow", "deny": "deny", "force_ask": "ask"}
    out: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": mapping.get(decision.get("decision", "allow"), "allow"),
        }
    }
    if decision.get("reason"):
        out["hookSpecificOutput"]["permissionDecisionReason"] = decision["reason"]
    return out


def main_pre_tool_use(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> pre-tool-use``. Reads one JSON
    payload on stdin, writes exactly one JSON decision on stdout.

    Flavors: ``antigravity`` (spec schema) and ``claude-code``
    (hookSpecificOutput schema). Classification logic is identical.
    """
    internal_error_policy = "allow"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        ws_root = _resolve_workspace_root(payload)
        internal_error_policy = _load_guard_policy(ws_root).get("internal_error", "allow")
        decision = classify(payload)
    except Exception:
        if internal_error_policy == "deny":
            decision = _deny("CTX_CONTEXT_GUARD: internal guard error (fail-closed policy)")
        else:
            decision = dict(DECISION_ALLOW)
    emitted: dict[str, Any] = (
        _to_claude_code_schema(decision) if flavor == "claude-code" else decision
    )
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0
