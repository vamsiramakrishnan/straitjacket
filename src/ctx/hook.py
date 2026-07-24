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

# Text-transform tools (M-K5.3, docs/SUBSTRATE.md): read-only invocations
# are unbounded-output commands (→ ctx run capture, like grep/find); an
# IN-PLACE invocation is a structural-rewrite smell and force_asks with a
# preview-first remediation — a textual approximation of a codemod is the
# bug-generator failure mode, so it is never silently rerouted.
_TEXT_TOOLS = {"sed", "awk", "gawk", "mawk", "nawk"}
_SED_INPLACE_RE = re.compile(r"^-[a-zA-Z]*i")  # -i, -i.bak, clustered -ni

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
# Universal emission gate: a PostToolUse tool result larger than this many
# bytes is replaced by a bounded digest. Keep in sync with config.Budgets.
_MAX_TOOL_OUTPUT_BYTES_DEFAULT = 16384
_LEDGER_DIR_NAME = ".ctx-session-reads"
_POLICY_FILENAME = "ctx-policy.toml"  # compiled learned-policy epoch
_GREP_MATCH_CAP = 25  # -m injected into single-file grep under rewrite steering

_REWRITE_REASON = "CTX_CONTEXT_GUARD: routed through ctx for bounded capture"

# --- Tool-kind classification -------------------------------------------------
# Which guard branch a tool name takes (edit / command / read / search), matched
# by EXACT name or by whole WORD — never by raw substring. Substring matching
# silently mis-routed unrelated third-party tools: `credit_check` contains
# "edit", `playlist` contains "list", `thread_reply` contains "read". Priority
# order is load-bearing (edit → command → read → search): an `edit_command`-style
# name classifies as edit, exactly as the old ordered `if` chain did.
_TOOL_EXACT_KIND = {
    "create_file": "edit", "replace_file_content": "edit",
    "bash": "command", "shell": "command", "exec": "command",
    "open_file": "read", "view_file": "read",
    "grep": "search", "glob": "search", "find_by_name": "search",
    # Antigravity's semantic search: an exact name, not a word-match, so it is
    # contained under the collapse posture like grep_search/glob_search — while
    # unrelated "*_search" MCP tools (search_issues, search_code, web_search)
    # keep falling through to allow (never denied).
    "codebase_search": "search",
}
# (kind, word-stems): a name matches this kind if any of its words starts with a
# stem. `editor` starts with "edit"; `credit` does not.
_TOOL_STEM_KINDS = (
    ("edit", ("edit", "write")),   # Edit, MultiEdit, str_replace_editor, Write, WriteFile
    ("command", ("command",)),     # run_command, Command
    ("read", ("read",)),           # Read, ReadFile, ReadManyFiles (not thread)
)
# Search family. `list` matches as an exact word (`list_dir` yes, `playlist`
# no). grep/glob match as a whole-word SUFFIX so variants like `ripgrep` /
# `ripgrep_search` route to search — as the old `"grep" in name` substring did
# — while non-search words that merely contain the letters (`telegraph`,
# `playlist`) stay out.
_TOOL_SEARCH_WORDS = frozenset({"list"})
_TOOL_SEARCH_SUFFIXES = ("grep", "glob")


def _tool_words(tool_name: str) -> list[str]:
    """Split a tool name into lowercased words on camelCase and delimiters."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tool_name)
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", spaced)]


def _tool_kind(tool_name: str) -> str | None:
    """Classify a tool name into edit/command/read/search, or None. Pure."""
    exact = _TOOL_EXACT_KIND.get(tool_name.lower())
    if exact:
        return exact
    words = _tool_words(tool_name)
    for kind, stems in _TOOL_STEM_KINDS:
        if any(w.startswith(stems) for w in words):
            return kind
    if any(w in _TOOL_SEARCH_WORDS or w.endswith(_TOOL_SEARCH_SUFFIXES) for w in words):
        return "search"
    return None

# Interactive/stdin-suspect programs: rewriting these into a non-interactive
# `ctx run` capture would hang or change semantics, so they stay plain deny.
_NO_REWRITE_PROGS = {"less", "more", "vi", "vim", "nano", "emacs", "top", "htop", "watch", "ssh", "xargs"}


_POLICY_CACHE_NAME = "guard-policy-cache.json"


def _policy_cache_key(paths: list[Path]) -> list[list[Any]]:
    """(path, mtime_ns, size) triples for every policy source file; a missing
    file contributes a zero row so appearing/disappearing invalidates too."""
    key: list[list[Any]] = []
    for p in paths:
        try:
            st = p.stat()
            key.append([str(p), st.st_mtime_ns, st.st_size])
        except OSError:
            key.append([str(p), 0, 0])
    return key


def _load_guard_policy(workspace_root: str | None) -> dict[str, Any]:
    """Minimal ctx.toml read for the guard section, plus the compiled
    ctx-policy.toml learned-policy epoch. Never raises.

    The parsed result is cached as JSON in the session ledger keyed by the
    (mtime_ns, size) of both source files: TOML parsing (and the tomllib
    import itself, ~5ms) runs only when a source file actually changed.
    The cache is a pure derivation of the TOMLs — deleting it is always safe.
    """
    policy: dict[str, Any] = {
        "mode": "guarded",
        "unknown_command": "force_ask",
        "internal_error": "allow",
        "steering": "auto",
        "collapse": True,  # replacement surface (default posture): substitute loop-shapes with collapsed ctx ops; set guard.collapse=false to break-glass off
        "max_inline_bytes": _MAX_INLINE_BYTES_DEFAULT,
        "max_inline_lines": _MAX_INLINE_LINES_DEFAULT,
        "session_read_budget_bytes": _SESSION_READ_BUDGET_DEFAULT,
        "window_pressure_pct": _WINDOW_PRESSURE_PCT_DEFAULT,
        "max_tool_output_bytes": _MAX_TOOL_OUTPUT_BYTES_DEFAULT,
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
    ppath = Path(workspace_root) / _POLICY_FILENAME
    if not path.is_file() and not ppath.is_file():
        return policy
    cache_path = Path(workspace_root) / _LEDGER_DIR_NAME / _POLICY_CACHE_NAME
    key = _policy_cache_key([path, ppath])
    try:
        doc = json.loads(cache_path.read_text(encoding="utf-8"))
        if doc.get("key") == key and isinstance(doc.get("policy"), dict):
            # Fresh defaults first so keys added in a newer ctx win over a
            # cache written by an older one.
            policy.update(doc["policy"])
            return policy
    except Exception:
        pass
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
            policy["collapse"] = bool(guard.get("collapse", policy["collapse"]))
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
            policy["max_tool_output_bytes"] = int(
                budgets.get("max_tool_output_bytes", policy["max_tool_output_bytes"])
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
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(
            f"{_POLICY_CACHE_NAME}.{os.getpid()}.{os.urandom(4).hex()}"
        )
        tmp.write_text(json.dumps({"key": key, "policy": policy}), encoding="utf-8")
        os.replace(tmp, cache_path)
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
    tightened["max_tool_output_bytes"] = max(
        1,
        int(int(policy.get("max_tool_output_bytes", _MAX_TOOL_OUTPUT_BYTES_DEFAULT)) * factor),
    )
    tightened["_head_tail_max"] = max(1, int(_HEAD_TAIL_MAX * factor))
    tightened["_window_note"] = f" [window {pct:g}% full — budgets tightened]"
    return tightened


# Wrappers that prefix another command; unwrap to classify the real program.
_WRAPPERS = {"env", "sudo", "doas", "nice", "nohup", "time", "stdbuf", "timeout", "command", "xvfb-run"}

# Redirection-only tail: `cmd ... > file 2>&1` — console output proven small.
# Only `> file 2>&1` (dup AFTER the stdout redirect) and `&> file` prove
# both streams leave the console. `2>&1 > file` is NOT included: POSIX
# processes redirections left to right, so the dup targets the console and
# stderr still floods the transcript (SJ-EvidenceBench scenario F).
_REDIR_ALL_RE = re.compile(
    r"^(?P<cmd>[^|;&<>`$(){}]+?)\s*(?:>>?\s*(?P<t1>\S+)\s*2>&1|&>>?\s*(?P<t2>\S+))\s*$"
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


# ------------------------------------------------- eval teaching surface
# Measured gap (evals/eval-collapse-2026-07-18.md, finding 2): agents write
# raw `python3 << 'EOF'` heredocs / `python -c` chains instead of `ctx eval`
# — 0/3 live adoption, because the verb has no teaching surface on this
# host. When such a command hits the guard, the remediation additionally
# teaches the collapse move. Teaching-only this wave: heredocs are NEVER
# auto-rewritten into `ctx eval` (quoting hazards).
_EVAL_TEACH = (
    "Or collapse the chain: ctx eval '<python script>' — the script becomes "
    "an addressable blob and only a bounded digest returns."
)

_RECORDS_TEACH = (
    "Or query the structured records directly: ctx q 'records <run:|blob:> "
    "--jsonl | group <field> | count' (or distinct/histogram) — bounded, "
    "typed, no re-parsing."
)

_PY_PROG_RE = re.compile(r"^python(3(\.\d+)?)?$")


def _eval_opportunity(command: str) -> bool:
    """True when ``command`` is a raw python invocation carrying inline code
    — a heredoc/herestring (``<<``) or a ``-c`` flag — i.e. the chain shape
    ``ctx eval`` collapses. Conservative by construction: the program must
    be python/python3/python3.N after unwrapping, and ``-c`` counts only
    among python's own leading options (before ``-m``, ``--``, or a script
    path), so ``python3 -m pytest`` and ``python3 script.py`` never match."""
    if "python" not in command:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    argv = _unwrap(argv)
    if not argv or not _PY_PROG_RE.match(os.path.basename(argv[0])):
        return False
    if "<<" in command:
        return True
    for tok in argv[1:]:
        if tok.startswith("-c"):  # "-c" / "-c<code>"; long opts start "--", not "-c"
            return True
        if tok == "-m" or tok == "--":
            return False  # module mode: -c beyond here is not python's
        if not tok.startswith("-"):
            # Script path. An EPHEMERAL script (written to a temp/scratch
            # dir moments earlier, run once, never addressed) is the
            # measured real-world evasion of this detector: agents do
            # `cat > /tmp/.../x.py` then `python3 /tmp/.../x.py`
            # (eval-collapse doc, layer 2b — 0 ledger entries because both
            # halves individually looked innocent). Workspace-resident
            # scripts stay non-opportunities: they are addressable code.
            return tok.startswith("/tmp/") or "/scratchpad/" in tok
    return False


def _note_eval_opportunity(workspace_root: str | None, taught: bool) -> None:
    """Adoption telemetry for the eval teaching surface: append one JSON
    line to ``<workspace>/.ctx-session-reads/eval-adoption.jsonl``. This is
    the denominator of the measurement loop (actual ``ctx eval`` use is
    counted in store telemetry as op="eval"). Fail-open by contract: any IO
    error counts nothing and never blocks a decision."""
    if not workspace_root:
        return
    try:
        import time

        ledger_dir = os.path.join(workspace_root, _LEDGER_DIR_NAME)
        os.makedirs(ledger_dir, exist_ok=True)
        path = os.path.join(ledger_dir, "eval-adoption.jsonl")
        line = json.dumps(
            {"op": "eval_opportunity", "taught": taught, "ts": time.time()},
            sort_keys=True,
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# Record-transform shapes that ``ctx q`` (records/group/count/distinct/
# histogram) collapses (docs/SUBSTRATE.md M-K3): jq programs, sort|uniq -c
# (group+count), awk field projection, and count-after-filter. Conservative
# by construction — the shape must be unambiguous, so a bare `sort` or a
# plain `awk` script never matches. This is the DEMAND denominator that
# gates promoting further named projections into the algebra.
_UNIQ_C_RE = re.compile(r"\buniq\s+-\w*c")
_AWK_PROJECT_RE = re.compile(r"\bg?awk\s+.*\{\s*print\s+\$[0-9]")
_JQ_RE = re.compile(r"(^|[|;&]\s*)jq\b")


def _records_opportunity(command: str) -> bool:
    """True when ``command`` is a structured-record transform that a
    bounded ``ctx q`` pipeline expresses (a jq program, a sort|uniq -c
    group-count, or an awk field projection)."""
    if _JQ_RE.search(command):
        return True
    if _UNIQ_C_RE.search(command):
        return True
    if _AWK_PROJECT_RE.search(command):
        return True
    return False


def _note_records_opportunity(workspace_root: str | None, taught: bool) -> None:
    """Adoption telemetry for the records-transform surface: one JSON line
    to ``<workspace>/.ctx-session-reads/records-adoption.jsonl`` — the
    denominator against which ``ctx q records`` use (store telemetry
    op="q") is measured. Fail-open; never blocks a decision."""
    if not workspace_root:
        return
    try:
        import time

        ledger_dir = os.path.join(workspace_root, _LEDGER_DIR_NAME)
        os.makedirs(ledger_dir, exist_ok=True)
        path = os.path.join(ledger_dir, "records-adoption.jsonl")
        line = json.dumps(
            {"op": "records_opportunity", "taught": taught, "ts": time.time()},
            sort_keys=True,
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _failure_available(workspace_root: str | None) -> bool:
    """True when a test run has been captured this session, so the
    ``fails last | in-changed`` slice has data to return. Cheap ledger scan;
    False on any doubt, so the pytest collapse never fires blind."""
    if not workspace_root:
        return False
    try:
        path = os.path.join(workspace_root, _LEDGER_DIR_NAME, "interventions.jsonl")
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"family": "pytest"' in line and "intervention_emitted" in line:
                    return True
    except OSError:
        return False
    return False


def _symbols_resolvable(workspace_root: str | None) -> bool:
    """Cheap check: can `ctx q refs` resolve symbols in this repo? True when a
    SCIP index is present or the tree has Python sources (ast/jedi handle
    those). Bounded scan; on a miss a symbol grep degrades to bounded content
    search, never to nothing. False on any doubt."""
    if not workspace_root:
        return False
    try:
        root = Path(workspace_root)
        if (root / "index.scip").is_file() or (root / ".ctx" / "index.scip").is_file():
            return True
        seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in
                           ("node_modules", "venv", "__pycache__", "dist", "build")]
            for fn in filenames:
                if fn.endswith(".py"):
                    return True
                seen += 1
                if seen > 2000:  # bounded — don't walk a huge non-Python tree
                    return False
    except Exception:
        return False
    return False


def _note_collapse(workspace_root: str | None, shape: str, rung: str) -> None:
    """Adoption telemetry for the replacement surface: one JSON line per
    substitution to ``<workspace>/.ctx-session-reads/collapse.jsonl`` — the
    numerator for 'loop-shapes collapsed'. Fail-open; never blocks."""
    if not workspace_root:
        return
    try:
        import time

        ledger_dir = os.path.join(workspace_root, _LEDGER_DIR_NAME)
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "collapse.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"op": "collapse", "shape": shape, "rung": rung, "ts": time.time()},
                sort_keys=True) + "\n")
    except Exception:
        pass


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


def _inplace_text_edit_in(stripped: str) -> bool:
    """Conservative token scan for an in-place sed/awk-family invocation
    anywhere in a compound expression. False positives only force_ask."""
    try:
        toks = shlex.split(stripped)
    except ValueError:
        return False
    for j, t in enumerate(toks):
        prog = os.path.basename(t)
        if prog in _TEXT_TOOLS and _text_tool_inplace(prog, toks[j:]):
            return True
    return False


def _text_tool_inplace(prog: str, argv: list[str]) -> bool:
    """Does this sed/awk-family invocation mutate files in place?

    sed: ``-i``/``-i.bak`` (possibly clustered, e.g. ``-ni``) or
    ``--in-place[=suffix]``. awk family: gawk's ``-i inplace`` /
    ``--include=inplace``. False positives only force_ask — safe."""
    rest = argv[1:]
    if prog == "sed":
        for a in rest:
            if a == "--in-place" or a.startswith("--in-place="):
                return True
            if not a.startswith("--") and _SED_INPLACE_RE.match(a):
                return True
        return False
    for j, a in enumerate(rest):
        if a in ("-i", "--include") and j + 1 < len(rest) and rest[j + 1].startswith("inplace"):
            return True
        if a.startswith("--include=inplace") or a.startswith("-iinplace"):
            return True
    return False


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
            target = redir.group("t1") or redir.group("t2") or ""
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
    # A prefix allow/promotion applies to a single command only. When shell
    # metacharacters survived the chain/redirect handling above (e.g.
    # ``echo hi && rm -rf x``), ``shlex.split`` keeps ``&&`` as an ordinary
    # token, so ``canonical`` would still start with an allowed prefix — a
    # compound-command bypass. Prefix allows are therefore gated on
    # ``not has_meta`` (deny prefixes are not: denying more is always safe).
    if not has_meta:
        for prefix in policy.get("allow_commands", []):
            if canonical.startswith(prefix):
                return dict(DECISION_ALLOW)
        # Learned policy epoch (ctx-policy.toml): promoted signatures behave
        # exactly like allow_commands canonical prefixes. Demoted signatures
        # are checked FIRST and are never allowed via promotion (belt against
        # a conflicting or hand-edited epoch); a demoted command is not
        # denied here — it simply falls through to normal classification.
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
        # Exception (M-K5.3): an IN-PLACE text edit inside the expression
        # (sed -i, gawk -i inplace — awk programs always carry `{}`, so
        # they land here, not in the plain-argv branch) is a mutation; a
        # capture rewrite would still mutate files, so it keeps the plain
        # force_ask with the preview-first remediation instead.
        if _inplace_text_edit_in(stripped):
            return _force_ask(
                "CTX_CONTEXT_GUARD: in-place text edit over files. A textual "
                "approximation of a structural rewrite is a bug generator — "
                "collapse the whole find-and-edit into one op: "
                "ctx rewrite '<pattern>' '<replacement>' --lang <l> --apply "
                "(previewed, generation-guarded, transactional) — or the "
                "editor's edit tool."
            )
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

    if prog in _TEXT_TOOLS:
        if _text_tool_inplace(prog, argv):
            return _force_ask(
                "CTX_CONTEXT_GUARD: in-place text edit over files. A textual "
                "approximation of a structural rewrite is a bug generator — "
                "collapse the whole find-and-edit into one op: "
                "ctx rewrite '<pattern>' '<replacement>' --lang <l> --apply "
                "(previewed, generation-guarded, transactional); for plain-text "
                f"targets, capture it: ctx run -- {' '.join(shlex.quote(a) for a in argv)}"
            )
        return _deny_cmd(argv, policy)  # read-only: bounded capture via ctx run

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
    # A sign-prefixed argument flips head/tail into unbounded mode:
    # ``tail -n +N`` prints from line N to EOF; ``head -n -N`` prints all but
    # the last N. Both are effectively ``cat`` and must NOT be read as a small
    # bounded count. Return None (→ the deny/rewrite path) for those.
    def _count(raw: str) -> int | None:
        if raw[:1] in ("+", "-"):
            return None
        try:
            return abs(int(raw))
        except ValueError:
            return None

    for i, a in enumerate(argv[1:], start=1):
        if a in ("-n", "--lines") and i + 1 < len(argv):
            return _count(argv[i + 1])
        if a.startswith("-n") and len(a) > 2:
            return _count(a[2:])
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

    kind = _tool_kind(tool_name)

    # Reflex v2 (spec3 round-2 finding): an Edit/Write disarms starvation
    # detection — run → census → edit → re-run is healthy verification, and
    # v1 counted it as starvation (6 spurious events on the referee). Pure
    # observation: always allow, never rewrite, fail-open.
    if kind == "edit":
        try:
            from ctx import reflex

            reflex.note_edit(workspace_root)
        except Exception:
            pass
        return dict(DECISION_ALLOW)

    if kind == "command":
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
        # Replacement surface (docs/REPLACEMENT-SURFACE.md): when collapse is
        # enabled, a recognised navigation-loop shape — recursive grep, or a
        # whole-suite re-run after a captured failure — is transparently
        # substituted with the collapsed, addressable `ctx q` op. Delivered
        # under the tool the agent already invoked, so the cheap path is taken
        # *for* the model, not left for it to choose. Off by default; the
        # substituted op is bounded and lossless (handles page exact bytes).
        if policy.get("collapse") and _steering_allows(policy):
            try:
                from ctx import substitute

                sub = substitute.collapse(
                    command, failure_available=_failure_available(workspace_root),
                    symbols_resolvable=_symbols_resolvable(workspace_root))
                if sub is not None:
                    decision = dict(DECISION_ALLOW)
                    decision["_rewrite"] = {"command": sub.command, "reason": sub.reason}
                    _note_collapse(workspace_root, sub.shape, sub.rung)
            except Exception:
                pass
        # Eval teaching surface: a raw python heredoc / -c chain that hits
        # the guard gets the collapse move appended to its remediation (and
        # to the rewrite reason, so wrapped sessions see it too). Every
        # detected opportunity is ledgered — taught or not — as the
        # adoption denominator. Teaching-only: never auto-rewritten.
        if _eval_opportunity(command):
            taught = decision.get("decision") in ("deny", "force_ask")
            if taught:
                decision["reason"] = decision.get("reason", "") + "\n" + _EVAL_TEACH
                if "_rewrite" in decision:
                    decision["_rewrite"]["reason"] += "\n" + _EVAL_TEACH
            _note_eval_opportunity(workspace_root, taught)
        # Records-transform teaching surface (M-K3): a jq / sort|uniq -c /
        # awk-projection pipeline that hits the guard gets the ctx q records
        # move appended, and is ledgered as the adoption denominator.
        if _records_opportunity(command):
            taught = decision.get("decision") in ("deny", "force_ask")
            if taught:
                decision["reason"] = decision.get("reason", "") + "\n" + _RECORDS_TEACH
                if "_rewrite" in decision:
                    decision["_rewrite"]["reason"] += "\n" + _RECORDS_TEACH
            _note_records_opportunity(workspace_root, taught)
        # Reflex arc (docs/REFLEX.md layers 1-3): score this command against
        # the session's recorded interventions. A `ctx get`/`ctx search` on a
        # known run handle is a landing (the positive class); anything else
        # is checked for the starvation pattern (same signature re-issued
        # after its digest — the spec3 re-run loop). The result NEVER changes
        # the decision — reflexes act through rendering (`ctx run` reads the
        # densify latch), not through blocking. Fail-open by contract.
        try:
            from ctx import reflex

            handle = reflex.landing_ref(command)
            if handle:
                reflex.note_landing(workspace_root, handle)
            else:
                reflex.check_command(workspace_root, command)
        except Exception:
            pass
        # Graduated steering — the null plan (EDC phase 6b) — SHADOW ONLY
        # this wave: when steering is about to rewrite this command (a
        # command-substitution `_rewrite`, i.e. an unbounded/compound
        # command being routed through ctx), record whether the graduated
        # regime WOULD have bypassed the rewrite (engagement still passive
        # AND no prior flood for the signature). NO behavior change: the
        # rewrite below is applied exactly as before. The PostToolUse
        # emission gate (`_emission_gate`) is the safety net that will make
        # the eventual relaxation safe — even a bypassed unbounded command
        # is bounded at emission time, so the null plan risks one bounded
        # digest, never a transcript flood. Fail-open by contract.
        if isinstance(decision.get("_rewrite"), dict) and "command" in decision["_rewrite"]:
            try:
                from ctx import reflex

                reflex.note_steer_shadow(workspace_root, command)
            except Exception:
                pass
        return _apply_rewrite(decision, tool_input, command_key)

    if kind == "read":
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

    if kind == "search":
        return _apply_rewrite(_classify_native_search(tool_name, tool_input, policy), tool_input)

    return dict(DECISION_ALLOW)


# Claude Code native Grep/Glob tools bypass the Bash path entirely (they are
# their own tools, not shell commands) — so their content output is never
# wrapped through ``ctx run`` and never digested. We cannot touch their output
# from a PreToolUse hook, but we CAN bound it transparently: a content-mode
# grep with no ``head_limit`` gets one injected via ``updatedInput``. The tool
# still runs, the model adopts nothing, and a flood becomes a bounded slice
# with a pointer to the structured search digest (measured gap: the model
# navigates with native Grep, which our old Bash-only matcher never saw).
_NATIVE_GREP_CAP = 60  # matches returned before the model should narrow


def _native_search_redirect(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Remediation that points a native Grep/Glob call at the collapsed op.
    Under the replacement surface a host's own search tool is off — it cannot
    be transparently rewritten into a ``ctx q`` call (unlike a shell command),
    so it is denied with the equivalent collapsed op named."""
    pat = tool_input.get("pattern") or tool_input.get("query") or ""
    if "grep" not in tool_name.lower():  # Glob / file-name search
        collapsed = "ctx q 'files --glob <glob>'"
    elif isinstance(pat, str) and re.match(r"^[A-Za-z_]\w*$", pat):
        collapsed = f"ctx q 'refs {pat} | group file'"
    elif pat:
        collapsed = f"ctx q 'search {pat} | files'"
    else:
        collapsed = "ctx q 'refs <Symbol>'  (symbol)  or  ctx q 'search <pattern>'"
    return ("CTX_CONTEXT_GUARD: native search is off under the replacement "
            "surface (guard.collapse). Use  " + collapsed + "  for a bounded, "
            "addressable answer — or run `grep -rn <pattern>` in Bash, which is "
            "auto-collapsed to the same op.")


def _classify_native_search(
    tool_name: str, tool_input: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    lowered = tool_name.lower()
    # Replacement surface: with collapse on, the host's native search tool is
    # removed from the surface — deny and redirect to the collapsed ctx op (or
    # to Bash grep, which is transparently substituted). One code path, so the
    # gap closes for every harness whose hook sees a native search tool.
    if policy.get("collapse"):
        return _deny(_native_search_redirect(tool_name, tool_input))
    recursive = bool(tool_input.get("Recursive") or tool_input.get("recursive"))
    # Glob / file-name search / listings return paths (bounded-ish); only a
    # recursive one under strict steering is worth redirecting.
    if "grep" not in lowered:
        if recursive and not _steering_allows(policy):
            return _deny(
                "CTX_CONTEXT_GUARD: recursive listing/search may flood.\n"
                "Use: ctx search repo: '<pattern>' --glob '<glob>'  or  ctx stats repo:"
            )
        return dict(DECISION_ALLOW)

    # Native Grep. files_with_matches / count modes are already small — allow.
    mode = str(tool_input.get("output_mode") or "files_with_matches")
    if mode != "content":
        return dict(DECISION_ALLOW)
    # Content mode already bounded by the model → respect it.
    for k in ("head_limit", "headLimit"):
        v = tool_input.get(k)
        if isinstance(v, int) and v > 0:
            return dict(DECISION_ALLOW)
    if not _steering_allows(policy):
        return _deny(
            "CTX_CONTEXT_GUARD: unbounded content grep may flood the transcript.\n"
            "Add head_limit, or use: ctx run -- grep -rn '<pattern>' <path>  "
            "(digested, structured by file)"
        )
    cap = int(policy.get("_grep_native_cap", _NATIVE_GREP_CAP))
    decision: dict[str, Any] = dict(DECISION_ALLOW)
    decision["_rewrite"] = {
        "fields": {"head_limit": cap},
        "reason": (
            f"CTX_CONTEXT_GUARD: content grep bounded to {cap} matches. "
            "For the full set structured by file (counts, top hits, span): "
            "ctx run -- grep -rn '<pattern>' <path>"
        ),
    }
    return decision


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


# Navigation governor (docs/CALL-GRAPH): the impact benchmark proved that a
# capable model, asked for a transitive call graph, hand-traces it with grep —
# undercounting 18x (22 vs the true 399) or failing outright — even when
# `ctx impact` is built and taught. Teaching is ignored; forcing backfires
# (rtk bash-only failed). The measured-correct middle path: detect the
# dominated pattern (repeated grep for bare identifiers = hand-tracing calls)
# and price the better verb at the point of friction, exactly once.
_IDENT_RE = re.compile(r"^[A-Za-z_]\w{2,}$")
_GREP_PROGS = ("grep", "rg", "egrep", "ag", "ack")
_NAV_THRESHOLD = 3  # bare-identifier greps before the nudge fires


def _grep_symbol(command: str) -> str | None:
    """If ``command`` is a grep/rg searching for a bare identifier (the
    signature of tracing a symbol's call sites), return that identifier."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    argv = _unwrap(argv)
    if not argv or os.path.basename(argv[0]) not in _GREP_PROGS:
        return None
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        term = a.strip("'\"")
        # A bare identifier — not a regex, not a path — is a symbol trace.
        if _IDENT_RE.match(term):
            return term
        return None  # first positional is the pattern; if not an ident, skip
    return None


def _navigation_nudge(payload: dict[str, Any]) -> str | None:
    """Fire once when the session has grepped for >= _NAV_THRESHOLD distinct
    bare identifiers — the hand-traced-call-graph pattern. Fail-open."""
    try:
        tool = str(payload.get("tool_name") or payload.get("toolName") or "").lower()
        if "bash" not in tool and "command" not in tool:
            return None
        ti = payload.get("tool_input") or payload.get("toolInput") or {}
        command = ""
        for k in ("command", "Command", "CommandLine", "cmd"):
            if isinstance(ti.get(k), str):
                command = ti[k]
                break
        symbol = _grep_symbol(command)
        if not symbol:
            return None
        workspace_root = _resolve_workspace_root(payload)
        if not workspace_root:
            return None
        from ctx.engagement import note_symbol_grep

        distinct, fired = note_symbol_grep(workspace_root, symbol)
        if fired or distinct < _NAV_THRESHOLD:
            return None
        return (
            f"CTX_NAV_GOVERNOR: you have grepped for {distinct} distinct symbols "
            "— that is hand-tracing a call graph, which grep computes "
            "unreliably (transitive closure is easy to undercount). One call "
            "gives the exact graph: `ctx callers <sym>` (direct) or "
            f"`ctx impact <sym>` (transitive blast radius, e.g. `ctx impact {symbol}`)."
        )
    except Exception:
        return None


def _normalize_tool_response(tr: Any) -> tuple[str, str]:
    """Reduce any tool_response shape to ``(stdout, stderr)`` text.

    Handles the shapes a PostToolUse result actually arrives in: a plain
    string; a list of ``{type,text}`` content blocks (MCP / Claude tool
    results); a ``{content: [...]}`` wrapper; a ``{stdout, stderr}`` capture;
    else a canonical JSON dump (so the JSON profile can still fire)."""
    if isinstance(tr, str):
        return tr, ""
    if isinstance(tr, list):
        # Collapse to text ONLY when EVERY block is a text block. If any block
        # is non-text (image / resource / audio), the joined text would
        # silently drop it — and since this text is exactly what gets
        # persisted, that would violate lossless-on-disk (the dropped block
        # would be unrecoverable via `ctx get`). Serialize the whole structure
        # instead so the artifact is complete.
        if tr and all(isinstance(b, dict) and isinstance(b.get("text"), str) for b in tr):
            return "\n".join(b["text"] for b in tr), ""
        return json.dumps(tr, ensure_ascii=False, sort_keys=True), ""
    if isinstance(tr, dict):
        if isinstance(tr.get("content"), list):
            return _normalize_tool_response(tr["content"])
        if isinstance(tr.get("stdout"), str):
            return tr["stdout"], tr.get("stderr") if isinstance(tr.get("stderr"), str) else ""
        if isinstance(tr.get("text"), str):
            return tr["text"], ""
    return json.dumps(tr, ensure_ascii=False, sort_keys=True), ""


def _emission_gate(payload: dict[str, Any], flavor: str) -> str | None:
    """The universal output-side gate. When a tool result exceeds the byte
    budget, persist it losslessly and return a bounded digest (with a working
    ``ctx get`` ref) to substitute for the raw output via ``updatedToolOutput``.
    Returns None to pass the result through untouched. Fail-open: any error →
    None (the raw output is never lost, only un-digested).

    Claude Code (``updatedToolOutput``) and Codex (``decision:block`` + reason,
    https://learn.chatgpt.com/docs/hooks) both have a verified substitution
    field; Antigravity's is unverified upstream, so there we stay nudge-only.
    """
    if flavor not in ("claude-code", "codex"):
        return None
    try:
        tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
        tr = None
        for k in ("tool_response", "toolResponse", "tool_output", "toolOutput", "output", "result", "content"):
            if k in payload:
                tr = payload[k]
                break
        if tr is None:
            return None
        stdout, stderr = _normalize_tool_response(tr)

        # Never digest our own digests or ctx's own tool results (recursion /
        # double-wrap guard). "[ctx " covers every ctx header — run: (digest),
        # get / search / stats (retrieval) — so a large `ctx get` slice run via
        # Bash is not itself re-digested. "densified:" is the reflex arc's
        # declared-densification header prepended above the "[ctx run:" line.
        if stdout.lstrip().startswith(("[ctx ", "densified:")) or tool_name == "ctx" or tool_name.startswith("mcp__ctx"):
            return None

        ws_root = _resolve_workspace_root(payload)
        policy = _apply_window_pressure(_load_guard_policy(ws_root), ws_root)
        if str(policy.get("mode")) == "advisory":
            return None
        threshold = int(policy.get("max_tool_output_bytes", _MAX_TOOL_OUTPUT_BYTES_DEFAULT))
        if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) <= threshold:
            return None  # under budget → byte-identical pass-through

        is_error = bool(
            payload.get("is_error")
            or payload.get("isError")
            or (isinstance(tr, dict) and tr.get("is_error"))
        )
        # Lazy: only pay the Store/digest import cost on the over-budget path.
        from ctx.digest import digest_output
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        ws = resolve_workspace(ws_root or ".")
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        text, _short = digest_output(store, ws, tool_name, stdout, stderr, is_error=is_error)
        return text
    except Exception:
        return None


def main_session_start(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> session-start``. Runs the capability
    surface pre-flight ('bound before bloat') once, before the first turn, and
    injects a bounded advisory when the discretionary surface exceeds budget.
    Fires once per session (not the hot path), so the surface import is fine.
    Fail-open: any error emits a no-op, never a blocked session."""
    advisory = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        ws = _resolve_workspace_root(payload)
        if ws:
            from ctx.config import load_config
            from ctx.surface import preflight

            sp = load_config(Path(ws)).surface
            if sp.gate != "off":
                advisory = preflight(
                    ws, max_static_tokens=sp.max_static_tokens,
                    default_profile=sp.default_profile, gateway=sp.gateway,
                    probe=sp.probe)
    except Exception:
        advisory = ""
    if flavor in ("claude-code", "codex"):
        emitted: dict[str, Any] = (
            {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                    "additionalContext": advisory}}
            if advisory else {"continue": True}
        )
    else:  # antigravity dialect
        emitted = {"additionalContext": advisory} if advisory else {}
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def main_post_tool_use(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> post-tool-use``. Reads one JSON
    payload on stdin, writes exactly one JSON object on stdout: either a
    no-op ``{}`` or a governor nudge (emission or navigation) in the host
    dialect."""
    replacement = None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        # Navigation first: it targets a specific, high-cost wrong pattern;
        # emission is the ambient volume backstop.
        nudge = _navigation_nudge(payload) or _emission_nudge(payload)
        # Universal emission gate: replace over-budget output with a digest.
        replacement = _emission_gate(payload, flavor)
    except Exception:
        nudge = None
    if flavor == "claude-code":
        hso: dict[str, Any] = {"hookEventName": "PostToolUse"}
        if replacement is not None:
            hso["updatedToolOutput"] = replacement
        if nudge is not None:
            hso["additionalContext"] = nudge
        emitted: dict[str, Any] = {"hookSpecificOutput": hso} if len(hso) > 1 else {}
    elif flavor == "codex":
        # Codex PostToolUse substitutes the model-visible result via
        # {"decision":"block","reason":<text>}; additionalContext carries a
        # non-substituting nudge (https://learn.chatgpt.com/docs/hooks).
        emitted = {}
        if replacement is not None:
            emitted["decision"] = "block"
            emitted["reason"] = replacement
        chso: dict[str, Any] = {"hookEventName": "PostToolUse"}
        if nudge is not None:
            chso["additionalContext"] = nudge
        if len(chso) > 1:
            emitted["hookSpecificOutput"] = chso
    elif nudge is not None:  # antigravity dialect: nudge-only (no replacement)
        emitted = {"decision": "allow", "reason": nudge}
    else:
        emitted = {}
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
    # Codex uses Claude Code's PreToolUse contract verbatim
    # (hookSpecificOutput.permissionDecision + updatedInput), per
    # https://learn.chatgpt.com/docs/hooks.
    emitted: dict[str, Any] = (
        _to_claude_code_schema(decision)
        if flavor in ("claude-code", "codex")
        else _to_antigravity_schema(decision)
    )
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0
