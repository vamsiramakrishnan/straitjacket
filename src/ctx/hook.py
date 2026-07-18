"""PreToolUse context guard (SPEC §10.2, §11).

Latency contract: this module is on the hot path of every intercepted tool
call. It imports only stdlib modules that are already loaded by the CLI fast
path (json, os, re, sys, shlex, pathlib, tomllib) and never touches the
artifact store, git, or the network.

Output contract: exactly one JSON object on stdout for every code path.
Internal errors follow the configured policy — fail-open (`allow`) in the
default guarded mode, because a broken guard must not brick the workspace.

Two-layer steering design ("rewrite, don't reject"):

* Layer 1 — classification. ``classify()`` always keeps the canonical
  ``decision`` field stable ("allow" / "deny" / "force_ask") exactly as in
  the deny-with-remediation contract, so ``ctx doctor`` and policy tests can
  assert on it. When ``[guard] steering`` is ``"auto"`` (default) or
  ``"rewrite"``, an eligible deny (or compound-shell force_ask) additionally
  carries a ``rewrite`` field: ``{"updatedInput": {...}, "reason": "..."}``
  describing a transparent input substitution (route the command through
  ``ctx run``, bound an oversized read with ``limit``, cap a single-file
  grep with ``-m``). Under ``steering = "deny"`` no ``rewrite`` is ever
  attached and behavior is byte-identical to the pure deny contract.

* Layer 2 — dialect emission. Each emitter decides what a ``rewrite`` means
  on the wire: the antigravity emitter turns it into the canonical
  substitution form ``{"decision": "allow", "updatedInput": {...},
  "reason": "..."}``; the claude-code emitter turns it into
  ``hookSpecificOutput`` with ``permissionDecision: "allow"`` and
  ``updatedInput`` (https://code.claude.com/docs/en/hooks). Decisions
  without a ``rewrite`` pass through the emitters unchanged.

Never rewritten: ctx-routed commands (already allowed), secret-path and
outside-workspace force_asks, and interactive-suspect commands (pagers,
bare REPLs, stdin consumers, ``head -f``).

Session read ledger ("death by a thousand small reads"): single native reads
under ``max_inline_bytes`` pass raw, but a session that walks a codebase
file-by-file is cumulatively unbounded. Every allowed native Read is charged
to a per-session byte counter stored at
``<workspace>/.ctx-session-reads/<session_id>.count`` (a single ASCII
integer; rewritten oversized reads are charged ``max_inline_bytes``). Once
the cumulative total exceeds ``[budgets] session_read_budget_bytes``
(default 256 KiB), further reads come under graduated pressure: under
rewrite steering they are allowed but bounded via ``updatedInput`` with a
small ``limit`` window; under ``steering = "deny"`` they are denied with the
ctx remediation. All ledger IO is fail-open — any error degrades to counting
nothing, never blocking a read. Reads of the ledger directory itself are
never counted. Projects should add ``.ctx-session-reads/`` to their
``.gitignore``; the leading dot keeps it out of casual listings.

Window-pressure loop: the Tier-0 observer proxy writes ground truth about
context-window fullness to ``<workspace>/.ctx-session-reads/proxy/window.json``
(``{"window_pct": float, ...}``). When ``window_pct`` reaches
``[budgets] window_pressure_pct`` (default 70), the guard tightens: the
effective ``max_inline_bytes``, ``session_read_budget_bytes``, and head/tail
``-n`` cap are scaled by ``max(0.25, 1 - (window_pct - threshold)/100*2)``
and affected reason strings gain a suffix like
`` [window 84% full — budgets tightened]``. Reading the window file is
fail-open (missing/corrupt → no pressure); below the threshold behavior is
byte-identical to the unpressured guard.

Learned policy epochs: ``<workspace>/ctx-policy.toml`` (compiled by
``ctx.policy`` from run telemetry, reviewed and committed like code) may
promote command signatures whose observed output is reliably small. Promoted
signatures behave exactly like ``guard.allow_commands`` canonical prefixes;
demoted signatures are never allowed via promotion (checked first). The file
is read fail-open — a corrupt policy changes nothing.
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
_MAX_INLINE_LINES_DEFAULT = 240
_SESSION_READ_BUDGET_DEFAULT = 262144  # 256 KiB of raw native reads per session
_WINDOW_PRESSURE_PCT_DEFAULT = 70  # window fullness (%) at which budgets tighten
_LEDGER_DIR_NAME = ".ctx-session-reads"
_POLICY_FILENAME = "ctx-policy.toml"  # compiled learned-policy epoch
_GREP_MATCH_CAP = 25  # -m injected into single-file grep under rewrite steering

_REWRITE_REASON = "CTX_CONTEXT_GUARD: routed through ctx for bounded capture"

# Interactive/stdin-suspect programs: rewriting these into a non-interactive
# `ctx run` capture would hang or change semantics, so they stay plain deny.
_NO_REWRITE_PROGS = {"less", "more", "vi", "vim", "nano", "emacs", "top", "htop", "watch", "ssh", "xargs"}


def _load_guard_policy(workspace_root: str | None) -> dict[str, Any]:
    """Minimal ctx.toml read for the guard section, plus the compiled
    ctx-policy.toml learned-policy epoch. Never raises."""
    policy: dict[str, Any] = {
        "mode": "guarded",
        "unknown_command": "force_ask",
        "internal_error": "allow",
        "steering": "auto",
        "max_inline_bytes": _MAX_INLINE_BYTES_DEFAULT,
        "max_inline_lines": _MAX_INLINE_LINES_DEFAULT,
        "session_read_budget_bytes": _SESSION_READ_BUDGET_DEFAULT,
        "window_pressure_pct": _WINDOW_PRESSURE_PCT_DEFAULT,
        "allow_commands": [],
        "deny_commands": [],
        "promoted_commands": [],
        "demoted_commands": [],
        "engagement_mode": "auto",
        "engagement_activate_after": 8,
        "emission_nudge_tokens": 20000,
    }
    if not workspace_root:
        return policy
    path = Path(workspace_root) / "ctx.toml"
    if path.is_file():
        try:
            import tomllib

            raw = tomllib.loads(path.read_text(encoding="utf-8"))
            guard = raw.get("guard") or {}
            budgets = raw.get("budgets") or {}
            policy["mode"] = str(guard.get("mode", policy["mode"]))
            policy["unknown_command"] = str(guard.get("unknown_command", policy["unknown_command"]))
            policy["internal_error"] = str(guard.get("internal_error", policy["internal_error"]))
            policy["steering"] = str(guard.get("steering", policy["steering"]))
            policy["max_inline_bytes"] = int(
                budgets.get("max_inline_bytes", policy["max_inline_bytes"])
            )
            policy["max_inline_lines"] = int(
                budgets.get("max_inline_lines", policy["max_inline_lines"])
            )
            policy["session_read_budget_bytes"] = int(
                budgets.get("session_read_budget_bytes", policy["session_read_budget_bytes"])
            )
            policy["window_pressure_pct"] = int(
                budgets.get("window_pressure_pct", policy["window_pressure_pct"])
            )
            # Repo-tunable classification: prefix matches against canonical argv.
            policy["allow_commands"] = [str(x) for x in guard.get("allow_commands", [])]
            policy["deny_commands"] = [str(x) for x in guard.get("deny_commands", [])]
            eng = raw.get("engagement") or {}
            policy["engagement_mode"] = str(eng.get("mode", policy["engagement_mode"]))
            policy["engagement_activate_after"] = int(
                eng.get("activate_after_calls", policy["engagement_activate_after"])
            )
            policy["emission_nudge_tokens"] = int(
                eng.get("emission_nudge_tokens", policy["emission_nudge_tokens"])
            )
        except Exception:
            pass
    # Learned policy epoch (compiled, committed ctx-policy.toml): promoted
    # signatures act like allow_commands prefixes; demoted never do. Read in
    # its own fail-open block so a corrupt epoch cannot poison ctx.toml
    # settings (and vice versa).
    ppath = Path(workspace_root) / _POLICY_FILENAME
    if ppath.is_file():
        try:
            import tomllib

            praw = tomllib.loads(ppath.read_text(encoding="utf-8"))
            if str(praw.get("schema", "")) == "ctx.policy/v1":
                promoted: list[str] = []
                for item in praw.get("promoted") or []:
                    sig = item.get("signature") if isinstance(item, dict) else item
                    if isinstance(sig, str) and sig.strip():
                        promoted.append(sig.strip())
                demoted: list[str] = []
                for item in praw.get("demoted") or []:
                    sig = item.get("signature") if isinstance(item, dict) else item
                    if isinstance(sig, str) and sig.strip():
                        demoted.append(sig.strip())
                policy["promoted_commands"] = promoted
                policy["demoted_commands"] = demoted
        except Exception:
            pass
    return policy


def _window_pct(workspace_root: str | None) -> float | None:
    """Ground-truth context-window fullness written by the Tier-0 proxy at
    ``<workspace>/.ctx-session-reads/proxy/window.json``. Fail-open by
    contract: any missing file, IO error, or malformed document → None
    (no pressure is ever applied because of broken telemetry)."""
    if not workspace_root:
        return None
    try:
        path = os.path.join(workspace_root, _LEDGER_DIR_NAME, "proxy", "window.json")
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        pct = doc.get("window_pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return None
        return float(pct)
    except Exception:
        return None


def _apply_window_pressure(
    policy: dict[str, Any], workspace_root: str | None
) -> dict[str, Any]:
    """Close the window-pressure loop. When the proxy-reported window
    fullness reaches ``window_pressure_pct``, return a tightened copy of the
    policy; otherwise return ``policy`` unchanged (byte-identical decisions).

    Tightening is the deterministic linear ramp

        factor = max(0.25, 1 - (window_pct - threshold) / 100 * 2)

    i.e. budgets shrink 2 percentage points per point of window fullness
    above the threshold, floored at a quarter of their configured values
    (threshold 70: 84% full → factor 0.72; ≥ 107.5% would floor at 0.25).
    The factor scales ``max_inline_bytes``, ``session_read_budget_bytes``,
    and the head/tail ``-n`` cap; affected reasons carry ``_window_note``."""
    pct = _window_pct(workspace_root)
    if pct is None:
        return policy
    threshold = int(policy.get("window_pressure_pct", _WINDOW_PRESSURE_PCT_DEFAULT))
    if pct < threshold:
        return policy
    factor = max(0.25, 1 - (pct - threshold) / 100 * 2)
    tightened = dict(policy)
    tightened["max_inline_bytes"] = max(
        1, int(int(policy.get("max_inline_bytes", _MAX_INLINE_BYTES_DEFAULT)) * factor)
    )
    tightened["session_read_budget_bytes"] = max(
        1,
        int(
            int(policy.get("session_read_budget_bytes", _SESSION_READ_BUDGET_DEFAULT))
            * factor
        ),
    )
    tightened["_head_tail_max"] = max(1, int(_HEAD_TAIL_MAX * factor))
    tightened["_window_note"] = f" [window {pct:g}% full — budgets tightened]"
    return tightened


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


def _steering_allows(policy: dict[str, Any]) -> bool:
    return str(policy.get("steering", "auto")) in ("auto", "rewrite")


def _deny_cmd(
    argv: list[str],
    policy: dict[str, Any],
    *,
    original: str | None = None,
    has_meta: bool = False,
) -> dict[str, Any]:
    """Command deny with remediation. Under rewrite steering, attach the
    substitution hint (layer 1): plain argv routes through ``ctx run --``;
    a denied command that already contained shell metacharacters routes
    through ``ctx run --shell -- '<original string>'``."""
    decision: dict[str, Any] = _deny(_remediation(argv))
    if not _steering_allows(policy):
        return decision
    prog = os.path.basename(argv[0]) if argv else ""
    if prog in _NO_REWRITE_PROGS:
        return decision
    if has_meta and original:
        cmd = "ctx run --shell -- " + shlex.quote(original)
    else:
        cmd = "ctx run -- " + " ".join(shlex.quote(a) for a in argv)
    decision["_rewrite"] = {"command": cmd, "reason": _REWRITE_REASON}
    return decision


def _split_simple_chain(stripped: str) -> list[str] | None:
    """Split ``a; b && c`` into segments when the only metacharacters present
    are the separators ``;``, ``&&``, ``||``. Returns None when any other
    metacharacter (``| > < ` $ ( ) { } \\`` or a lone ``&``) remains in a
    segment, so pipelines and substitutions never take this path."""
    parts = re.split(r";|&&|\|\|", stripped)
    if len(parts) < 2:
        return None
    if any(_SHELL_META_RE.search(p) for p in parts):
        return None
    return [p.strip() for p in parts if p.strip()]


def _grep_single_file_rewrite(argv: list[str], cwd: str | None) -> str | None:
    """``grep [flags] PATTERN FILE`` with exactly one existing file argument,
    no recursion, and no existing ``-m`` cap → the same command with
    ``-m 25`` injected. Anything ambiguous returns None (generic path)."""
    flags = [a for a in argv[1:] if a.startswith("-")]
    for a in flags:
        if a in ("--recursive", "--dereference-recursive"):
            return None
        if a.startswith("--max-count") or a == "-m" or re.match(r"^-m\d", a):
            return None
        if not a.startswith("--") and ("r" in a[1:] or "R" in a[1:]):
            return None  # -r/-R possibly clustered (-rn)
    positional = [a for a in argv[1:] if not a.startswith("-")]
    if len(positional) != 2:
        return None  # flag arguments (-e, -A, -f, …) land here too: bail out
    file_arg = positional[1]
    if os.path.isabs(file_arg):
        probe = file_arg
    elif cwd:
        probe = os.path.join(cwd, file_arg)
    else:
        return None
    if not os.path.isfile(probe):
        return None
    new_argv = [argv[0], "-m", str(_GREP_MATCH_CAP), *argv[1:]]
    return " ".join(shlex.quote(a) for a in new_argv)


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


def classify_command(
    command: str, policy: dict[str, Any], _depth: int = 0, *, cwd: str | None = None
) -> dict[str, str]:
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
        # Bounded chain: `a; b && c` with no other metacharacters. Each
        # segment is classified independently; the chain is allowed only if
        # every segment is independently allowed. Any deny/force_ask segment
        # falls through to the compound-expression handling below.
        segments = _split_simple_chain(stripped)
        if segments and all(
            classify_command(seg, policy, _depth + 1, cwd=cwd).get("decision") == "allow"
            for seg in segments
        ):
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
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
    for prefix in policy.get("allow_commands", []):
        if canonical.startswith(prefix):
            return dict(DECISION_ALLOW)
    # Learned policy epoch (ctx-policy.toml): promoted signatures behave
    # exactly like allow_commands canonical prefixes. Demoted signatures are
    # checked FIRST and are never allowed via promotion (belt against a
    # conflicting or hand-edited epoch); a demoted command is not denied
    # here — it simply falls through to normal classification.
    if not any(canonical.startswith(p) for p in policy.get("demoted_commands", [])):
        for prefix in policy.get("promoted_commands", []):
            if canonical.startswith(prefix):
                return dict(DECISION_ALLOW)

    # `bash -c '<inner>'`: classify the inner command, not the shell.
    if prog in ("bash", "sh", "zsh", "dash", "fish") and len(argv) >= 3 and argv[1] == "-c":
        return classify_command(argv[2], policy, _depth + 1, cwd=cwd)

    if prog == "xargs":
        return _deny_cmd(argv, policy)  # stdin consumer: never rewritten

    if has_meta:
        # A pipeline containing head is not automatically safe (SPEC §11.2).
        # Canonical decision stays force_ask; under rewrite steering the
        # whole expression is steered into a bounded `ctx run --shell`
        # capture instead (secret/outside-workspace force_asks never are).
        fa = _force_ask(
            "CTX_CONTEXT_GUARD: compound shell expression with unproven output bound. "
            f"Prefer: ctx run --shell -- {shlex.quote(stripped)}"
        )
        if _steering_allows(policy):
            fa["_rewrite"] = {
                "command": "ctx run --shell -- " + shlex.quote(stripped),
                "reason": _REWRITE_REASON,
            }
        return fa

    # Bounded head/tail with explicit small -n. Under window pressure the
    # cap shrinks with the same factor as the byte budgets.
    if prog in ("head", "tail"):
        n = _extract_line_count(argv)
        if "-f" in argv or "--follow" in argv:
            return _deny(_remediation(argv))  # streaming: never rewritten
        cap = int(policy.get("_head_tail_max", _HEAD_TAIL_MAX))
        if n is not None and n <= cap:
            return dict(DECISION_ALLOW)
        decision = _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
        note = str(policy.get("_window_note", ""))
        if note:
            decision["reason"] += note
            if "_rewrite" in decision:
                decision["_rewrite"]["reason"] += note
        return decision

    if prog == "git":
        sub = next((a for a in argv[1:] if not a.startswith("-")), "")
        if sub in _GIT_UNBOUNDED:
            return _deny_cmd(argv, policy)
        if sub == "status" and not ("--short" in argv or "-s" in argv or "--porcelain" in argv):
            return _deny_cmd(argv, policy)
        return dict(DECISION_ALLOW)

    if prog == "ls":
        if any(a.startswith("-") and "R" in a for a in argv[1:]):
            return _deny_cmd(argv, policy)
        return dict(DECISION_ALLOW)

    if prog in ("python", "python3", "node", "ruby", "perl", "deno"):
        # Interpreter invocations can read anything (guard-bypass channel)
        # and emit anything; route through ctx.
        if len(argv) == 1:
            return _deny(_remediation(argv))  # bare REPL: interactive-suspect
        return _deny_cmd(argv, policy)

    if prog in _BOUNDED_CMDS:
        return dict(DECISION_ALLOW)

    if prog == "grep" and _steering_allows(policy):
        # Single-file grep gets a match cap injected instead of a reroute.
        capped = _grep_single_file_rewrite(argv, cwd)
        if capped:
            decision = _deny(_remediation(argv))
            decision["_rewrite"] = {
                "command": capped,
                "reason": (
                    f"CTX_CONTEXT_GUARD: single-file grep capped at "
                    f"-m {_GREP_MATCH_CAP} matches for bounded output"
                ),
            }
            return decision

    if prog in _UNBOUNDED_CMDS:
        return _deny_cmd(argv, policy)

    # Unknown command → configured policy.
    unknown = policy.get("unknown_command", "force_ask")
    if unknown == "allow" or policy.get("mode") == "advisory":
        return dict(DECISION_ALLOW)
    if unknown == "deny":
        return _deny_cmd(argv, policy)
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


def _ledger_charge(workspace_root: str | None, session_id: str, nbytes: int) -> int:
    """Add ``nbytes`` to the per-session native-read counter and return the
    new cumulative total. The ledger is a single ASCII integer at
    ``<workspace>/.ctx-session-reads/<session_id>.count``. Fail-open by
    contract: any IO or parse problem returns 0 (no pressure is ever applied
    because of a broken ledger, and this function never raises)."""
    if not workspace_root:
        return 0
    try:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:80] or "unknown"
        ledger_dir = os.path.join(workspace_root, _LEDGER_DIR_NAME)
        os.makedirs(ledger_dir, exist_ok=True)
        path = os.path.join(ledger_dir, safe + ".count")
        # Parallel tool calls fire hooks concurrently; an advisory flock
        # makes the read-modify-write atomic so charges are never lost.
        # fcntl is stdlib (POSIX); on platforms without it the original
        # racy-but-fail-open behavior is preserved.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:
                pass
            raw = os.read(fd, 64)
            try:
                total = int(raw.decode("ascii").strip() or 0)
            except ValueError:
                total = 0
            total += int(nbytes)
            payload = str(total).encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.ftruncate(fd, len(payload))
            return total
        finally:
            os.close(fd)  # closing releases the flock
    except Exception:
        return 0


def _price_note(size_bytes: int, workspace_root: str | None) -> str:
    """Priced-context signpost (docs/PRICED-CONTEXT.md, M1): the cost of a
    read in the agent's native currency, relativized to its window when the
    proxy's ground truth is available. Measured cost: ~0.003 ms. Fail-open
    to an empty string — a price tag must never break a decision."""
    try:
        tok = max(1, size_bytes // 4)
        from ctx.textutil import fmt_tokens_coarse

        note = f"{fmt_tokens_coarse(tok)} tok"
        if workspace_root:
            try:
                path = os.path.join(
                    workspace_root, _LEDGER_DIR_NAME, "proxy", "window.json"
                )
                with open(path, "r", encoding="utf-8") as fh:
                    limit = json.load(fh).get("context_limit")
                if isinstance(limit, int) and limit > 0:
                    note += f" ≈ {max(1, round(100 * tok / limit))}% of window"
            except Exception:
                pass
        return f" ({note})"
    except Exception:
        return ""


def _outline_hint(path_str: str) -> str:
    """Menu line for structured files: the priced symbol outline verb."""
    if path_str.endswith(".py"):
        return "or:  ctx stats repo:<relative-path>   (priced symbol outline)\n"
    return ""


def _read_budget_reason(total: int) -> str:
    return (
        f"CTX_CONTEXT_GUARD: session native-read budget exceeded "
        f"(~{total // 1024} KiB raw reads); reads are now bounded — use "
        "ctx search repo: '<pattern>' or ctx get repo:<path> "
        "--symbol/--lines for targeted evidence"
    )


def classify_read(
    path_str: str,
    workspace_root: str | None,
    policy: dict[str, Any],
    session_id: str = "unknown",
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
    # The session ledger itself is bookkeeping, never evidence: reads of it
    # are neither counted nor pressured.
    in_ledger = _LEDGER_DIR_NAME in path_str.replace("\\", "/").split("/")
    limit = int(policy.get("max_inline_bytes", _MAX_INLINE_BYTES_DEFAULT))
    note = str(policy.get("_window_note", ""))  # window pressure, "" when idle
    if size > limit:
        price = _price_note(size, workspace_root)
        decision: dict[str, Any] = _deny(
            f"CTX_CONTEXT_GUARD: file is {size} bytes{price} (> {limit} inline budget).\n"
            + _outline_hint(path_str)
            + "Use: ctx get repo:<relative-path> --lines A:B\n"
            "or:  ctx search repo:<relative-path> '<pattern>' --context 3" + note
        )
        if _steering_allows(policy):
            max_lines = int(policy.get("max_inline_lines", _MAX_INLINE_LINES_DEFAULT))
            decision["_rewrite"] = {
                "fields": {"limit": max_lines},
                "reason": (
                    f"CTX_CONTEXT_GUARD: file is {size} bytes{price} (> {limit} inline "
                    f"budget); bounded to the first {max_lines} lines. "
                    + _outline_hint(path_str).replace("\n", " ")
                    + "For other slices use: ctx get repo:<relative-path> --lines A:B"
                    + note
                ),
            }
            if not in_ledger:
                # A rewritten read still lands ~max_inline_bytes of content.
                _ledger_charge(workspace_root, session_id, limit)
        return decision
    if in_ledger:
        return dict(DECISION_ALLOW)
    total = _ledger_charge(workspace_root, session_id, size)
    budget = int(
        policy.get("session_read_budget_bytes", _SESSION_READ_BUDGET_DEFAULT)
    )
    if total > budget:
        reason = _read_budget_reason(total) + note
        if _steering_allows(policy):
            max_lines = int(policy.get("max_inline_lines", _MAX_INLINE_LINES_DEFAULT))
            pressured: dict[str, Any] = dict(DECISION_ALLOW)
            pressured["_rewrite"] = {
                "fields": {"limit": max_lines // 4},
                "reason": reason,
            }
            return pressured
        return _deny(reason)
    return dict(DECISION_ALLOW)


def _apply_rewrite(
    decision: dict[str, Any],
    tool_input: dict[str, Any],
    command_key: str | None = None,
) -> dict[str, Any]:
    """Convert the layer-1 ``_rewrite`` hint into the public ``rewrite``
    field, preserving the original tool_input key names and every unrelated
    field (description, timeout, …) untouched in ``updatedInput``."""
    hint = decision.pop("_rewrite", None)
    if not hint:
        return decision
    updated = dict(tool_input)
    if "command" in hint:
        if not command_key:
            return decision  # no command field to substitute: keep plain decision
        updated[command_key] = hint["command"]
    else:
        updated.update(hint.get("fields", {}))
    decision["rewrite"] = {"updatedInput": updated, "reason": hint["reason"]}
    return decision


def classify(payload: dict[str, Any]) -> dict[str, str]:
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    workspace_root = _resolve_workspace_root(payload)
    policy = _load_guard_policy(workspace_root)

    if policy.get("mode") == "advisory":
        return dict(DECISION_ALLOW)

    # Window-pressure loop: proxy-observed window fullness tightens budgets.
    # Below threshold (or with no/broken window.json) this is a no-op and
    # every decision below is byte-identical to the unpressured guard.
    policy = _apply_window_pressure(policy, workspace_root)

    # Graduated engagement (mechanism C): count this interception and let
    # the session graduate passive→active on measured signals. The level
    # affects digest affordances, never guard decisions. Fail-open.
    try:
        from ctx.engagement import note_call

        note_call(
            workspace_root,
            mode=str(policy.get("engagement_mode", "auto")),
            activate_after_calls=int(policy.get("engagement_activate_after", 8)),
            window_pressure_pct=int(
                policy.get("window_pressure_pct", _WINDOW_PRESSURE_PCT_DEFAULT)
            ),
        )
    except Exception:
        pass

    lowered = tool_name.lower()
    if "command" in lowered or lowered in ("bash", "shell", "exec"):
        command = ""
        command_key = None
        for key in ("CommandLine", "command", "Command", "cmd"):
            v = tool_input.get(key)
            if isinstance(v, str):
                command, command_key = v, key
                break
        cwd = None
        for key in ("Cwd", "cwd"):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                cwd = v
                break
        decision = classify_command(command, policy, cwd=cwd or workspace_root)
        return _apply_rewrite(decision, tool_input, command_key)

    if "read" in lowered or lowered in ("open_file", "view_file"):
        session_id = str(
            payload.get("session_id") or payload.get("conversation_id") or "unknown"
        )
        for key in ("AbsolutePath", "TargetFile", "file_path", "path", "Path"):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                return _apply_rewrite(
                    classify_read(v, workspace_root, policy, session_id), tool_input
                )
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


def _to_claude_code_schema(decision: dict[str, Any]) -> dict[str, Any]:
    """Translate the canonical decision into Claude Code's PreToolUse hook
    schema (hookSpecificOutput.permissionDecision: allow|deny|ask).

    Layer 2: a decision carrying a ``rewrite`` becomes a transparent input
    substitution — ``permissionDecision: "allow"`` with ``updatedInput``
    directly under ``hookSpecificOutput``, which replaces the tool's
    arguments before it runs (https://code.claude.com/docs/en/hooks)."""
    rewrite = decision.get("rewrite")
    if isinstance(rewrite, dict) and isinstance(rewrite.get("updatedInput"), dict):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": rewrite["updatedInput"],
                "permissionDecisionReason": str(rewrite.get("reason", "")),
            }
        }
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


def _to_antigravity_schema(decision: dict[str, Any]) -> dict[str, Any]:
    """Layer 2 for the antigravity dialect. A decision carrying a ``rewrite``
    becomes the canonical substitution form; everything else passes through
    unchanged (byte-identical to the pure deny contract).

    Assumed Antigravity input-substitution contract (mirrors the decision
    schema; not yet published upstream):

        {"decision": "allow", "updatedInput": {...}, "reason": "..."}
    """
    rewrite = decision.get("rewrite")
    if isinstance(rewrite, dict) and isinstance(rewrite.get("updatedInput"), dict):
        return {
            "decision": "allow",
            "updatedInput": rewrite["updatedInput"],
            "reason": str(rewrite.get("reason", "")),
        }
    return {k: v for k, v in decision.items() if k != "rewrite" and not k.startswith("_")}


def _emission_nudge(payload: dict[str, Any]) -> str | None:
    """Emission governor (mechanism B): the symmetric partner of the read
    budget. The proxy measures cumulative output tokens; when the session
    crosses a new pressure tier (``emission_nudge_tokens`` each, default
    20k) AND the per-request average is verbose, return a one-line nudge to
    inject after the tool result. Each tier nudges exactly once. Returns
    None (silence) in every other case — including any error."""
    try:
        workspace_root = _resolve_workspace_root(payload)
        if not workspace_root:
            return None
        path = os.path.join(workspace_root, _LEDGER_DIR_NAME, "proxy", "window.json")
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        cum_output = int(doc.get("cum_output") or 0)
        requests = int(doc.get("requests") or 0)
        if requests <= 0 or cum_output <= 0:
            return None
        policy = _load_guard_policy(workspace_root)
        step = max(1, int(policy.get("emission_nudge_tokens", 20000)))
        tier = cum_output // step
        if tier < 1:
            return None
        per_request = cum_output / requests
        if per_request < 500:  # already terse: no nudge, ever
            return None
        from ctx.engagement import claim_emission_tier

        if not claim_emission_tier(workspace_root, tier):
            return None
        return (
            f"CTX_EMISSION_GOVERNOR: session output ~{cum_output:,} tokens "
            f"(avg {per_request:.0f}/turn). Output volume is the dominant "
            "cost+latency driver. Keep narration terse; cite coordinates "
            "(file:line, run:/span handles) instead of restating content."
        )
    except Exception:
        return None


def main_post_tool_use(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> post-tool-use``. Reads one JSON
    payload on stdin, writes exactly one JSON object on stdout: either a
    no-op ``{}`` or an emission-governor nudge in the host dialect."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        nudge = _emission_nudge(payload)
    except Exception:
        nudge = None
    if nudge is None:
        emitted: dict[str, Any] = {}
    elif flavor == "claude-code":
        emitted = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": nudge,
            }
        }
    else:  # antigravity dialect mirrors its decision schema
        emitted = {"decision": "allow", "reason": nudge}
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


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
        _to_claude_code_schema(decision)
        if flavor == "claude-code"
        else _to_antigravity_schema(decision)
    )
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0
