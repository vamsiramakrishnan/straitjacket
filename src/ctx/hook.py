"""PreToolUse context guard (SPEC §10.2, §11).

Latency contract: this module is on the hot path of every intercepted tool
call, in a fresh interpreter each time, so its import graph is paid thousands
of times a session. It never touches the artifact store, git, or the network.

Nothing here is "already loaded" and therefore free — `ctx.cli`'s fast path
imports only `sys`, so this module pays for its entire graph on every call.
(This contract used to assert the opposite, which is how the graph came to be
the dominant term in per-call latency: it costs an order of magnitude more
than classification itself, which is a few hundred microseconds.) Measured
end-to-end (median wall clock of the real
`ctx hook claude-code pre-tool-use` subprocess, n=51 interleaved, CPython
3.11, warm page cache; bare `python -c pass` on the same box is 10.3 ms, so
that is the floor):

    payload            before     after
    Bash (command)     41.5 ms    23.9 ms
    Read               34.9 ms    27.2 ms
    Grep (search)      29.9 ms    22.2 ms

The budget this module actually holds itself to, and what enforces it:

* Module scope imports **json, os, re, shlex, sys only** — nothing that
  transitively pulls `typing` (~4.3 ms), `pathlib` (~4.7 ms), `dataclasses`
  (~9.8 ms via `inspect` → `ast`/`dis`/`tokenize`/`linecache`), `tempfile`,
  `subprocess`, `argparse`, or any third-party module. Those figures are the
  marginal cost each one adds back to *this* graph, measured the same way.
  `os.path` does the path work; `Path` is for globbing and `.parts`, neither
  of which happens per call.
* Annotations only ever need `Any`, and `from __future__ import annotations`
  makes annotations strings, so `typing` is imported nowhere at runtime.
* `tomllib` (~4 ms) is imported lazily inside `_load_guard_policy` and only
  when a policy TOML is actually present *and* the parsed-policy cache is
  stale — the steady state never pays it.
* The same discipline binds `ctx.engagement`, `ctx.reflex` and
  `ctx.substitute`, because this module imports them per call; a heavy import
  added to any of them lands on this hot path.
* Per-call work is incremental or bounded, never proportional to session
  history: see `_failure_available` (cursor over an append-only ledger, not a
  rescan) and `_symbols_resolvable` (bounded level-order scan, not `os.walk`).

Output contract: exactly one JSON object on stdout for every code path.

Internal errors never silently become permission. On the INPUT side
(PreToolUse) an internal error follows ``[guard] internal_error``: ``allow``
by default (a broken guard must not brick the workspace, SPEC §10.2),
``deny`` when configured — and that ``deny`` is honoured even if the failure
happened while loading the policy itself. When the configured value cannot be
determined at all (a ctx.toml that is present but unreadable), the guard
answers ``force_ask`` rather than guessing in either direction. Every internal
error is recorded as one line in ``.ctx-session-reads/guard-failures.jsonl``
with the stage that broke.

On the OUTPUT side the emission gate (``_emission_gate``) fails CLOSED: once a
tool result is known to be over budget, no bug in gate code may release it raw.
Failures there degrade to a bounded digest carrying a retrieval handle
(``ctx get blob:<id>``, or a spill file under
``.ctx-session-reads/gate-fallback/`` if the store is what broke) — bounded and
retrievable, never "everything" and never "nothing".

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

# Emission-governor tier size, imported rather than re-typed. ctx.engagement
# is stdlib-only and is already a per-call hot-path module (see
# tests/test_hook_hot_path.py::_HOT_MODULES), so the single source of truth
# costs nothing here. The Rust shim names it again in its own language; the
# two are pinned equal by tests/test_cross_language_constants.py.
from ctx.engagement import EMISSION_NUDGE_TOKENS_DEFAULT
from ctx.command_spans import ALLOW as SPAN_ALLOW
from ctx.command_spans import CAPTURE as SPAN_CAPTURE
from ctx.command_spans import classify_command_span, git_subcommand
from ctx.guard_policy import choose_guard

# The governance table (which route each host's native tools take). Imported,
# not re-typed, so the classifier below cannot drift from what `ctx coverage`
# reports and the registration tests pin. It imports nothing.
from ctx import governance as _gov

# The house ledger directory, named and joined in exactly one place
# (ctx.sessiondir). That module imports `os` only, for this contract.
from ctx.proxywindow import read_window_doc
from ctx.sessiondir import LEDGER_DIR_NAME as _LEDGER_DIR_NAME
from ctx.sessiondir import session_reads_dir

# Media reads (ctx.mediaread): what the read guard does with an image, video
# or audio file. That module is imported only once a path's suffix says it
# may be media; until then the whole cost is one set lookup on these
# suffixes (pinned equal to mediaread's own table by tests/test_media_read.py).
_MEDIA_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico",
    "mp4", "m4v", "mov", "mkv", "webm", "avi",
    "mp3", "wav", "flac", "ogg", "oga", "opus", "m4a"})
#: [media] values and hosts, restated for the policy dict (pinned to
#: ctx.mediaread and ctx.config by tests/test_media_read.py).
_MEDIA_IMAGE_MODES = ("advise", "redirect", "off")
_MEDIA_VIDEO_MODES = ("steer", "off")
_MEDIA_HOSTS = ("claude-code", "codex")


def _maybe_media(path_str: str) -> bool:
    """Could this path be media? From the name alone; no system call."""
    dot = path_str.rfind(".")
    return (dot >= 0 and len(path_str) - dot <= 6
            and path_str[dot + 1:].lower() in _MEDIA_SUFFIXES)

# `pathlib` (~4.2 ms) and `typing` (~2.3 ms) are deliberately NOT imported at
# module scope — see the latency contract above. Every path expression on the
# per-call path uses `os.path`, which is already loaded; the one `Path` this
# module still needs is in `main_session_start`, which runs once per session
# next to far heavier imports and takes it lazily. `Any` is only ever an
# annotation, and `from __future__ import annotations` makes annotations
# strings, so it is never needed at runtime. The guard below is spelled with a
# local constant rather than `from typing import TYPE_CHECKING`, because that
# import would pull in the very module the hot path is avoiding.
# What each host dialect's hook contract can actually do. This mirrors
# HostSpec.input_substitution / output_substitution in ctx.hosts, and is kept
# as a local literal rather than imported because this module has a latency
# contract (above) that forbids pulling ctx.hosts — and with it the price
# table — onto the per-call path. The two are pinned together by
# tests/test_dialect_conformance.py, which fails if they ever disagree: the
# duplicated-knowledge bug this project already shipped once (an assumed
# Antigravity contract that hosts.py and hook.py each described differently)
# is exactly what that test exists to catch.
#
#   input_substitution  — PreToolUse can rewrite the tool's arguments (on a
#                         host in ctx.governance.REWRITABLE, only for the
#                         tools named there)
#   output_substitution — PostToolUse can replace the tool's result
DIALECT_CAPS: dict[str, dict[str, bool]] = {
    "claude-code": {"input_substitution": True, "output_substitution": True},
    "codex": {"input_substitution": True, "output_substitution": True},
    # agy 1.5.5, observed live: a PreToolUse `overwrite` is merged into the
    # tool's args before it runs (run_command, read_file); PostToolUse still
    # accepts only {} and its payload carries no result.
    "antigravity": {"input_substitution": True, "output_substitution": False},
    "hermes": {"input_substitution": True, "output_substitution": True},
    "omp": {"input_substitution": True, "output_substitution": True},
    "opencode": {"input_substitution": True, "output_substitution": True},
    "dsh": {"input_substitution": False, "output_substitution": True},
}


def can_substitute_output(flavor: str) -> bool:
    """True when this dialect's PostToolUse can replace a tool result."""
    return DIALECT_CAPS.get(flavor, {}).get("output_substitution", False)


def can_substitute_input(flavor: str) -> bool:
    """True when this dialect's PreToolUse can rewrite tool arguments
    (for at least some tools; see :func:`can_rewrite_call`)."""
    return DIALECT_CAPS.get(flavor, {}).get("input_substitution", False)


def _agy_legacy(payload: dict[str, Any] | None) -> bool:
    """An Antigravity payload from before agy 1.5.4, which added
    ``hookEventName`` (agy changelog, 1.5.4). Rewriting was observed on 1.5.5
    only, so such a host keeps the deny-and-steer contract."""
    return not isinstance((payload or {}).get("hookEventName"), str)


def can_rewrite_call(flavor: str, name: str | None, payload: dict[str, Any] | None = None) -> bool:
    """Will this host apply a rewrite of this particular call's arguments?"""
    if not _gov.can_rewrite(flavor, name, DIALECT_CAPS.get(flavor, {})):
        return False
    return not (flavor == "antigravity" and _agy_legacy(payload))


TYPE_CHECKING = False
if TYPE_CHECKING:
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

# Shell metacharacters that make static reasoning unreliable. A line break
# separates commands exactly like `;` (`ls\ncat big.log` runs both), so it is
# one too; without it every first-program allow below was a bypass.
_SHELL_META_RE = re.compile(r"[|;&<>`$(){}\\\n\r]|\|\||&&|\$\(")

# The token used to have to be a WHOLE path component, so `secrets.json`,
# `my-secrets.yaml` and `app-credentials.env` all walked past the guard --
# a classifier whose entire job is to catch secret-bearing paths, blind to
# the most ordinary way of naming one. Tokens may now appear inside a
# filename, bounded by non-letters so `secretary.py` is still not a secret.
# For a safety classifier the failure direction must be over-matching: a
# false positive costs one permission prompt, a false negative leaks a key.
_SECRET_PATH_RE = re.compile(
    r"(^|/)(\.env(\..*)?|\.aws|\.ssh|\.config/gcloud)(/|$)"
    r"|(?<![A-Za-z])(secrets?|credentials?)(?![A-Za-z])"
    r"|\.(pem|key)$|id_rsa|id_ed25519",
)

# Secret filenames carrying neither a slash nor a dot: without this the
# path-shape filter below would skip them.
_SECRET_BARE_RE = re.compile(r"^(id_rsa|id_ed25519|id_ecdsa|id_dsa)$")

def _is_secret_path(path_str: str, root: str | None = None) -> bool:
    """Does this path name -- or the file it actually opens -- bear secrets?

    A name-based denylist has to look at the name the FILESYSTEM will open.
    Both doors onto this guard matched the literal argument only, so
    ``ln -s .env notes.txt`` was a complete bypass: an innocuous name for the
    same bytes, which is the cheapest possible attack on a rule expressed as
    a regex over strings. docs/TROUBLESHOOTING.md promises the guarantee is
    blanket, so the resolution belongs inside the guard rather than at one
    caller.

    Both the literal name and the resolution are judged WORKSPACE-RELATIVE
    when they land inside the workspace, and in full when they land outside.
    Absolute paths were previously judged whole, so a checkout at
    ``~/my-credentials/`` or ``~/.aws-tools/`` force-asked every native Read
    in it -- the guard failing loudly instead of working. The distinction is
    the useful one either way: a secret token in the path BELOW the root is
    about this file, and one above the root is about where you keep your
    code.

    Resolution is best-effort: a path that does not exist yet, or that
    cannot be resolved, is judged on its name alone.
    """
    literal = str(path_str)
    if _SECRET_PATH_RE.search(_scoped_path(literal, root)):
        return True
    try:
        raw = os.path.expanduser(literal)
        base = root or os.getcwd()
        real = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
    except (OSError, ValueError):
        return False
    return bool(_SECRET_PATH_RE.search(_scoped_path(real, root)))


def _scoped_path(path_str: str, root: str | None) -> str:
    """The part of a path the guard should judge, in POSIX form.

    Inside the workspace: the workspace-relative part. Outside (or with no
    workspace): the whole thing.
    """
    p = str(path_str).replace("\\", "/")
    if not root:
        return p
    try:
        root_abs = os.path.realpath(root)
        cand = os.path.abspath(os.path.expanduser(str(path_str)))
        if cand == root_abs or cand.startswith(root_abs + os.sep):
            return os.path.relpath(cand, root_abs).replace("\\", "/")
    except (OSError, ValueError):
        pass
    return p


_HEAD_TAIL_MAX = 400  # max -n allowed for native head/tail
# A bound expressed in LINES cannot see a request expressed in BYTES:
# `head -c 200000 f` walked straight past the -n guard. Aligned with
# max_inline_bytes so both spellings of "a small slice" agree.
_HEAD_TAIL_MAX_BYTES = 16384

_MAX_INLINE_BYTES_DEFAULT = 16384
_MAX_INLINE_LINES_DEFAULT = 240
_SESSION_READ_BUDGET_DEFAULT = 262144  # 256 KiB of raw native reads per session
# Floor for the escalating over-budget read window: below this a bounded read
# stops being evidence and becomes a round trip that teaches the model nothing.
_OVER_BUDGET_MIN_LINES = 20
_WINDOW_PRESSURE_PCT_DEFAULT = 70  # window fullness (%) at which budgets tighten
# Universal emission gate: a PostToolUse tool result larger than this many
# bytes is replaced by a bounded digest. Keep in sync with config.Budgets.
_MAX_TOOL_OUTPUT_BYTES_DEFAULT = 16384
_POLICY_FILENAME = "ctx-policy.toml"  # compiled learned-policy epoch
# Internal-error telemetry + the emission gate's fail-closed spill area.
_GUARD_FAILURE_LEDGER = "guard-failures.jsonl"
_GATE_FALLBACK_DIR = "gate-fallback"
_GATE_FAILURE_HEAD_BYTES = 2048  # bounded excerpt inside a gate-failure digest
_GREP_MATCH_CAP = 25  # -m injected into single-file grep under rewrite steering

_REWRITE_REASON = "CTX_CONTEXT_GUARD: routed through ctx for bounded capture"
# A transparent rewrite must be native-shaped where capture buys nothing.
# Measured on DeepSWE (evals/agentbench, haiku): the agent's own commands
# produced a median of 91-277 bytes, yet every routed one came back as a
# receipt (header, command echo, status line) and a failing `python -c`
# reported ctx's exit 3 instead of the command's 1 — results grew 2-8x and
# the model re-ran identical failing checks. `--passthrough` makes `ctx run`
# print small, complete output verbatim (handle on one trailing line) and
# exit with the wrapped command's own status; large output still digests.
_REWRITE_FLAGS = " --passthrough"

# --- Tool-kind classification -------------------------------------------------
# Which guard branch a tool name takes (edit / command / read / search), matched
# by EXACT name or by whole WORD — never by raw substring. Substring matching
# silently mis-routed unrelated third-party tools: `credit_check` contains
# "edit", `playlist` contains "list", `thread_reply` contains "read". Priority
# order is load-bearing (edit → command → read → search): an `edit_command`-style
# name classifies as edit, exactly as the old ordered `if` chain did.
_TOOL_EXACT_KIND = {
    "patch": "edit", "apply_patch": "edit",
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


def _word_kind(tool_name: str) -> str | None:
    """The legacy name rules: exact shared names, then whole-word stems."""
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


def _governed_kind(
    tool_name: str, flavor: str | None = None, payload: dict[str, Any] | None = None
) -> str:
    """The governance-table kind of one tool call. Pure; never None.

    Order is load-bearing:

    1. a tool served by an MCP server is ``mcp`` — never a native file or
       search tool, whatever words its name contains (``mcp__github__list_issues``
       is not a directory listing, ``mcp__fs__read_file`` is not a Read);
    2. the host's exact native table (``ctx.governance.NATIVE``);
    3. on hosts whose names are fully enumerated, or whose MCP names carry no
       namespace, anything unlisted is a generic ``tool`` (output gate);
    4. elsewhere the word rules, else ``tool``.
    """
    if _gov.is_mcp(tool_name, payload):
        return "mcp"
    if flavor:
        listed = _gov.table_kind(flavor, tool_name)
        if listed:
            return listed
    if flavor in _gov.EXACT_ONLY_HOSTS:
        return "tool"
    word = _word_kind(tool_name)
    if flavor and word == "edit":
        # Observe-only is a privilege of tools the table names as edits: an
        # unlisted `write_stdin` or `TodoWrite`, or an editor tool whose view
        # action returns a whole file, would otherwise pass ungated.
        return "tool"
    return word or "tool"


def _tool_kind(
    tool_name: str, flavor: str | None = None, payload: dict[str, Any] | None = None
) -> str | None:
    """Classify a tool name into edit/command/read/search, or None. Pure.

    The guard-branch projection of :func:`_governed_kind`: every kind without
    an input-side branch reads as None, exactly as before."""
    kind = _governed_kind(tool_name, flavor, payload)
    return kind if kind in _gov.GUARD_KINDS else None

# Interactive/stdin-suspect programs: rewriting these into a non-interactive
# `ctx run` capture would hang or change semantics, so they stay plain deny.
_NO_REWRITE_PROGS = {"less", "more", "vi", "vim", "nano", "emacs", "top", "htop", "watch", "ssh", "xargs"}

# A follow flag means the command never exits. `head`/`tail` already refuse to
# be rewritten for this reason, but the same shape rides on journalctl, docker
# logs, kubectl logs and friends — and those were being rewritten into a
# *blocking* `ctx run`, which waits out the full 600s timeout. Steering the
# model into a hang is worse than not steering it at all. These get `--bg`,
# which returns a job handle immediately and is what the skill already teaches.
#
# `-f` is badly overloaded — it is *file* in `grep -f`, `make -f`,
# `docker build -f`, and *force* in `rm -f`/`cp -f`. Treating every `-f` as
# follow backgrounds builds and searches, which is its own bug. So the short
# form counts only for the programs where it genuinely means follow (matched
# with the log-reading subcommand where one exists); the long `--follow` is
# unambiguous and counts anywhere.
_FOLLOW_PROGS = {"tail", "head", "journalctl"}
_FOLLOW_SUBCOMMANDS = {"docker": "logs", "podman": "logs", "kubectl": "logs",
                       "oc": "logs", "nerdctl": "logs", "heroku": "logs"}


def _follows_forever(argv: list[str]) -> bool:
    """Does this invocation follow a stream, so that it never terminates?

    A foreground capture of such a command blocks until the timeout, so the
    caller steers it to `--bg` instead. Conservative by construction: a false
    negative just means the old behaviour, while a false positive would
    background a build the model needed to read inline."""
    if not argv:
        return False
    prog = os.path.basename(argv[0])
    rest = argv[1:]
    if prog == "gh" and (
        rest[:2] == ["run", "watch"] or "--watch" in rest
    ):
        return True
    if any(a == "--follow" or a.startswith("--follow=") for a in rest):
        return True
    short_means_follow = prog in _FOLLOW_PROGS or (
        prog in _FOLLOW_SUBCOMMANDS
        and any(a == _FOLLOW_SUBCOMMANDS[prog] for a in rest)
    )
    if not short_means_follow:
        return False
    return any(
        a == "-f" or (len(a) > 1 and a[0] == "-" and a[1] != "-" and "f" in a[1:])
        for a in rest
    )


_POLICY_CACHE_NAME = "guard-policy-cache.json"


def _policy_cache_key(paths: list[str]) -> list[list[Any]]:
    """Content-addressed rows for every policy source file.

    A missing file contributes a zero row so appearing/disappearing
    invalidates too.  Timestamps are retained as a cheap diagnostic, but the
    digest is authoritative: some filesystems can preserve all stat fields
    across a same-size edit made within one clock tick.
    """
    import hashlib

    key: list[list[Any]] = []
    for p in paths:
        try:
            st = os.stat(p)
            with open(p, "rb") as fh:
                content_id = hashlib.sha256(fh.read()).hexdigest()
            key.append([p, st.st_mtime_ns, st.st_ctime_ns, st.st_size, content_id])
        except OSError:
            key.append([p, 0, 0, 0, ""])
    return key


def _load_guard_policy(workspace_root: str | None) -> dict[str, Any]:
    """Minimal ctx.toml read for the guard section, plus the compiled
    ctx-policy.toml learned-policy epoch. Never raises.

    The parsed result is cached as JSON in the session ledger keyed by the
    content digest of both source files: TOML parsing (and the tomllib import
    itself, ~5ms) runs only when a source file actually changed.  Reading and
    hashing the small policy sources is the correctness cost on cache hits.
    The cache is a pure derivation of the TOMLs — deleting it is always safe.
    """
    policy: dict[str, Any] = {
        "mode": "guarded",
        "unknown_command": "force_ask",
        "internal_error": "allow",
        "steering": "auto",
        "collapse": True,  # replacement surface (default posture): substitute loop-shapes with collapsed ctx ops; set guard.collapse=false to break-glass off
        # Graduated null plan: a single explicitly named pytest node may run
        # without the ctx capture wrapper on dialects whose PostToolUse hook
        # can replace an unexpected flood. Antigravity can never take it.
        "speculative_native": True,
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
        "emission_nudge_tokens": EMISSION_NUDGE_TOKENS_DEFAULT,
        # [media] (ctx.mediaread): image reads advise by default, redirect to
        # an overview only on opt-in; video and audio reads are steered.
        "media_image_read": "advise",
        "media_video_read": "steer",
        "media_redirect_budget_ms": 6000,
        # Provenance of the guard section, NOT a setting: "default" (no
        # ctx.toml), "ok" (parsed), "failed" (present but unreadable /
        # unparseable). The internal-error policy needs to tell "the config
        # says allow" apart from "we could not find out what it says" — with
        # only the values above, a broken ctx.toml is indistinguishable from
        # an absent one, and the deny knob inverts silently.
        "_guard_config": "default",
    }
    if not workspace_root:
        return policy
    path = os.path.join(workspace_root, "ctx.toml")
    ppath = os.path.join(workspace_root, _POLICY_FILENAME)
    path_is_file = os.path.isfile(path)
    ppath_is_file = os.path.isfile(ppath)
    if not path_is_file and not ppath_is_file:
        return policy
    cache_path = session_reads_dir(workspace_root, _POLICY_CACHE_NAME)
    key = _policy_cache_key([path, ppath])
    try:
        with open(cache_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("key") == key and isinstance(doc.get("policy"), dict):
            # Fresh defaults first so keys added in a newer ctx win over a
            # cache written by an older one.
            policy.update(doc["policy"])
            return policy
    except Exception:
        pass
    if path_is_file:
        try:
            import tomllib

            with open(path, encoding="utf-8") as fh:
                raw = tomllib.loads(fh.read())
            # [media] first, on its own: it reads only its own section and
            # cannot raise, so a malformed key elsewhere in the file (which
            # sends the rest of this block to the fail-open default below)
            # never overrides a user's media choice, e.g. image_read = "off".
            media = raw.get("media")
            if isinstance(media, dict):
                # An unknown value keeps the default rather than switching the
                # behavior off or on by accident (same rule as ctx.config).
                image_read = media.get("image_read")
                if image_read in _MEDIA_IMAGE_MODES:
                    policy["media_image_read"] = image_read
                video_read = media.get("video_read")
                if video_read in _MEDIA_VIDEO_MODES:
                    policy["media_video_read"] = video_read
                budget_ms = media.get("redirect_budget_ms")
                if isinstance(budget_ms, int) and not isinstance(budget_ms, bool):
                    policy["media_redirect_budget_ms"] = max(500, min(budget_ms, 8000))
            guard = raw.get("guard") or {}
            budgets = raw.get("budgets") or {}
            policy["mode"] = str(guard.get("mode", policy["mode"]))
            policy["unknown_command"] = str(guard.get("unknown_command", policy["unknown_command"]))
            policy["internal_error"] = str(guard.get("internal_error", policy["internal_error"]))
            policy["steering"] = str(guard.get("steering", policy["steering"]))
            policy["collapse"] = bool(guard.get("collapse", policy["collapse"]))
            speculative = guard.get(
                "speculative_native", policy["speculative_native"]
            )
            if isinstance(speculative, bool):
                policy["speculative_native"] = speculative
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
            policy["_guard_config"] = "ok"
        except Exception:
            # Still fail-open for every ordinary budget/mode key (a typo in
            # ctx.toml must not brick the workspace), but record that the
            # values below are OUR defaults and not the user's choices.
            policy["_guard_config"] = "failed"
    # Learned policy epoch (compiled, committed ctx-policy.toml): promoted
    # signatures act like allow_commands prefixes; demoted never do. Read in
    # its own fail-open block so a corrupt epoch cannot poison ctx.toml
    # settings (and vice versa).
    if ppath_is_file:
        try:
            import tomllib

            with open(ppath, encoding="utf-8") as fh:
                praw = tomllib.loads(fh.read())
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
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        tmp = os.path.join(
            cache_dir, f"{_POLICY_CACHE_NAME}.{os.getpid()}.{os.urandom(4).hex()}"
        )
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "policy": policy}))
        os.replace(tmp, cache_path)
    except Exception:
        pass
    return policy


def _window_pct(workspace_root: str | None) -> float | None:
    """Ground-truth context-window fullness written by the Tier-0 proxy at
    ``<workspace>/.ctx-session-reads/proxy/window.json``. Fail-open by
    contract: any missing file, IO error, or malformed document → None
    (no pressure is ever applied because of broken telemetry)."""
    pct = read_window_doc(workspace_root).get("window_pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


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
    r"^(?P<cmd>[^|;&<>`$(){}\n\r]+?)\s*(?:>>?\s*(?P<t1>\S+)\s*2>&1|&>>?\s*(?P<t2>\S+))\s*$"
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


def _single_named_pytest(command: str) -> bool:
    """Whether ``command`` runs one explicit pytest node id.

    This is deliberately much narrower than "a pytest command": a file,
    directory, ``-k`` expression, or whole suite may be cheap sometimes but
    has no structural upper bound on work or output. A ``path::node`` target
    is the measured naive-friendly case. Shell expressions are excluded so
    the output-substitution safety net remains attached to one tool result.
    """
    try:
        if not command.strip() or _SHELL_META_RE.search(command):
            return False
        argv = _unwrap(shlex.split(command))
        if not argv:
            return False
        prog = os.path.basename(argv[0])
        if prog in ("python", "python3") and len(argv) >= 3 and argv[1] == "-m":
            argv = argv[2:]
            prog = os.path.basename(argv[0])
        if prog not in ("pytest", "py.test"):
            return False
        targets = [str(arg) for arg in argv[1:] if "::" in str(arg) and not str(arg).startswith("-")]
        return len(targets) == 1
    except Exception:
        return False


# ------------------------------------------------- eval teaching surface
# Measured gap (evals/eval-collapse-2026-07-18.md, finding 2): agents write
# raw `python3 << 'EOF'` heredocs / `python -c` chains instead of `ctx py`
# — 0/3 live adoption, because the verb has no teaching surface on this
# host. When such a command hits the guard, the remediation additionally
# teaches the collapse move. Teaching-only this wave: heredocs are NEVER
# auto-rewritten into `ctx py` (quoting hazards).
_EVAL_TEACH = (
    "Or collapse the chain: ctx py '<python script>' — the script becomes "
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
    ``ctx py`` collapses. Conservative by construction: the program must
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
    the denominator of the measurement loop (actual ``ctx py`` use is
    counted in store telemetry as op="eval"). Fail-open by contract: any IO
    error counts nothing and never blocks a decision."""
    if not workspace_root:
        return
    try:
        import time

        ledger_dir = session_reads_dir(workspace_root)
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

        ledger_dir = session_reads_dir(workspace_root)
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


_FAILURE_CURSOR_NAME = "failure-available.json"
# Matched against raw ledger bytes: both markers must appear on the SAME line,
# exactly as the original text scan required. ASCII-only, so byte matching is
# equivalent to text matching and cannot raise on a malformed UTF-8 ledger.
_PYTEST_FAMILY_MARK = b'"family": "pytest"'
_EMITTED_MARK = b"intervention_emitted"


def _failure_available(workspace_root: str | None) -> bool:
    """True when a test run has been captured this session, so the
    ``fails last | in-changed`` slice has data to return. False on any doubt,
    so the pytest collapse never fires blind.

    Incremental, not O(file). ``interventions.jsonl`` is strictly append-only
    and grows for the whole session; the original re-scanned it end to end on
    every call, which is O(N) per call and O(N²) over a session (measured: a
    50k-line / 5 MiB ledger with no pytest line cost 4.81 ms *per call*, and
    the miss case never short-circuits). Instead a cursor sidecar records how
    many bytes have been scanned and what was found, so each call reads only
    the bytes appended since the last one — linear over the session.

    The cursor is a pure derivation of the ledger and is used only when the
    ledger has evolved by pure append (size and mtime both non-decreasing);
    anything else falls back to a full scan. Deleting it is always safe, and
    every failure degrades to the plain full scan or to False."""
    if not workspace_root:
        return False
    ledger_dir = session_reads_dir(workspace_root)
    path = session_reads_dir(workspace_root, "interventions.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return False

    cursor_path = os.path.join(ledger_dir, _FAILURE_CURSOR_NAME)
    start = 0
    found = False
    try:
        with open(cursor_path, "rb") as fh:
            doc = json.loads(fh.read())
        prev_scanned = int(doc["scanned"])
        # Trust the cursor only under pure-append evolution.
        if (int(doc["size"]) <= st.st_size and int(doc["mtime_ns"]) <= st.st_mtime_ns
                and 0 <= prev_scanned <= st.st_size):
            start = prev_scanned
            found = bool(doc["found"])
    except Exception:
        start, found = 0, False

    if found:
        return True  # a captured failure is on record and cannot un-record

    scanned = start
    try:
        with open(path, "rb") as fh:
            if start:
                fh.seek(start)
            chunk = fh.read()
    except OSError:
        return False
    # Only whole lines are consumed; a partial trailing line (a concurrent
    # append caught mid-write) is left for the next call rather than split
    # across two scans, which could hide a match.
    cut = chunk.rfind(b"\n")
    if cut >= 0:
        for line in chunk[: cut + 1].splitlines():
            if _PYTEST_FAMILY_MARK in line and _EMITTED_MARK in line:
                found = True
                break
        scanned = start + cut + 1

    if scanned != start or found:
        try:
            tmp = os.path.join(
                ledger_dir,
                f"{_FAILURE_CURSOR_NAME}.{os.getpid()}.{os.urandom(4).hex()}",
            )
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                    "scanned": scanned, "found": found,
                }, sort_keys=True))
            os.replace(tmp, cursor_path)
        except Exception:
            pass
    return found


_SKIP_DIRS = ("node_modules", "venv", "__pycache__", "dist", "build")
_SYMBOLS_SCAN_ENTRIES = 2000   # file budget, as before
_SYMBOLS_SCAN_DIRS = 500       # NEW: directory budget — os.walk had none


def _symbols_resolvable(workspace_root: str | None) -> bool:
    """Cheap check: can `ctx q refs` resolve symbols in this repo? True when a
    SCIP index is present or the tree has Python sources (ast/jedi handle
    those). Bounded scan; on a miss a symbol grep degrades to bounded content
    search, never to nothing. False on any doubt.

    Breadth-first, not ``os.walk``. The question is only "is there a ``.py``
    anywhere in this tree", and real repos answer it at depth 0-1 (``setup.py``
    at the root, ``src/``, ``tests/``). ``os.walk`` is depth-first: it descends
    fully into whichever subdirectory comes first and can spend the entire file
    budget inside one unrelated subtree — slower, and on a repo with a large
    non-Python subtree it could exhaust the budget and answer False for a repo
    that plainly has Python. A level-order scan reaches the shallow, decisive
    entries first, so it is both cheaper and more accurate.

    The old walk was also unbounded in *directories*: the 2000 budget counted
    only files, so a wide tree of near-empty directories was never cut off.
    A directory budget is enforced alongside the file budget now.
    """
    if not workspace_root:
        return False
    try:
        if (os.path.isfile(os.path.join(workspace_root, "index.scip"))
                or os.path.isfile(os.path.join(workspace_root, ".ctx", "index.scip"))):
            return True
        queue = [workspace_root]
        head = 0  # index cursor gives FIFO (level) order without an O(n) pop(0)
        files_seen = 0
        while head < len(queue):
            if head >= _SYMBOLS_SCAN_DIRS:
                return False
            directory = queue[head]
            head += 1
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if not entry.name.startswith(".") and entry.name not in _SKIP_DIRS:
                        queue.append(entry.path)
                    continue
                if entry.name.endswith(".py"):
                    return True
                files_seen += 1
                if files_seen > _SYMBOLS_SCAN_ENTRIES:
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

        ledger_dir = session_reads_dir(workspace_root)
        os.makedirs(ledger_dir, exist_ok=True)
        with open(os.path.join(ledger_dir, "collapse.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"op": "collapse", "shape": shape, "rung": rung, "ts": time.time()},
                sort_keys=True) + "\n")
    except Exception:
        pass


def _note_guard_failure(
    workspace_root: str | None, *, op: str, stage: str, exc: BaseException
) -> None:
    """Signal for an INTERNAL guard/gate error: one JSON line to
    ``<workspace>/.ctx-session-reads/guard-failures.jsonl``.

    Internal errors used to be indistinguishable from a clean allow — the
    whole point of the fail-closed work is that a broken gate is now both
    bounded AND visible. ``stage`` names which part broke (input / policy /
    classify / normalize / digest / fallback-*), so a blanket "the guard
    failed" is never all an operator gets.

    Fail-open by contract (a telemetry write must never be the thing that
    blocks a tool call) and bounded (the detail is truncated)."""
    if not workspace_root:
        return
    try:
        import time

        ledger_dir = session_reads_dir(workspace_root)
        os.makedirs(ledger_dir, exist_ok=True)
        line = json.dumps(
            {
                "op": op,
                "stage": stage,
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
                "ts": time.time(),
            },
            sort_keys=True,
        )
        with open(
            os.path.join(ledger_dir, _GUARD_FAILURE_LEDGER), "a", encoding="utf-8"
        ) as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _capture_command(command: str, bg: str = "") -> str:
    """The rewrite for a command the guard captures: ``ctx run``, then
    exactly what the agent typed.

    The original string is passed VERBATIM as one argument to ``ctx run
    --shell``, which runs it with ``-c`` in the same shell family the host's
    own shell tool uses (``ctx._proc.capture_shell``). It is never re-split
    into words and re-quoted: that was only correct for a command containing
    nothing the shell interprets, and it turned `cat *.txt` into a literal
    `*.txt`, a trailing `# comment` into three arguments, and a
    `FOO=bar cmd` prefix into a program name. Classification may parse the
    command; execution never depends on that parse.
    """
    return f"ctx run{bg}{_REWRITE_FLAGS} --shell -- " + shlex.quote(command)


def _deny_cmd(
    argv: list[str],
    policy: dict[str, Any],
    *,
    original: str | None = None,
    has_meta: bool = False,
) -> dict[str, Any]:
    """Command deny with remediation. Under rewrite steering, attach the
    substitution hint (layer 1): the agent's ORIGINAL command string, carried
    verbatim to ``ctx run --shell`` (see :func:`_capture_command`)."""
    decision: dict[str, Any] = _deny(_remediation(argv))
    if not _steering_allows(policy):
        return decision
    prog = os.path.basename(argv[0]) if argv else ""
    if prog in _NO_REWRITE_PROGS:
        return decision
    # A never-terminating command must not be steered into a blocking capture.
    bg = " --bg" if _follows_forever(argv) else ""
    del has_meta  # execution no longer depends on how the command parsed
    cmd = _capture_command(original if original else shlex.join(argv), bg)
    reason = _REWRITE_REASON
    if bg:
        reason = (
            "CTX_CONTEXT_GUARD: this follows output and never exits, so a "
            "foreground capture would block until the timeout. Backgrounded "
            "instead — it returns a job handle immediately; read a bounded "
            "tail with `ctx job <id>`, or collect it with `ctx job <id> --wait`."
        )
    decision["_rewrite"] = {"command": cmd, "reason": reason}
    return decision


def _safety_deny_cmd(
    argv: list[str],
    policy: dict[str, Any],
    *,
    original: str | None = None,
    has_meta: bool = False,
) -> dict[str, Any]:
    """A repository-committed deny is never eligible for input substitution."""
    decision = _deny_cmd(argv, policy, original=original, has_meta=has_meta)
    decision.pop("_rewrite", None)
    decision["_safety"] = "1"
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


def _shell_chars(stripped: str):
    """The characters the shell itself interprets, quote-aware.

    Yields ``(index, char)`` for every character outside quotes, and for a
    ``$`` or backtick inside double quotes (substitutions still execute
    there). Raises ValueError on an unterminated quote. The one scanner both
    the chain splitter and :func:`_single_invocation` use.
    """
    index = 0
    quote = None
    while index < len(stripped):
        char = stripped[index]
        if quote:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                index += 1
            elif quote == '"' and char in '$`':
                yield index, char
        elif char in "\"'":
            quote = char
        else:
            yield index, char
        index += 1
    if quote:
        raise ValueError("unterminated quote")


#: Shell characters that can start a second program, redirect into one, or
#: open a subshell: sequencing, pipes, background, redirection, grouping,
#: substitution, line breaks, and escapes (conservatively).
_SECOND_PROGRAM_CHARS = frozenset(";&|<>(){}`\n\r\\")


def _single_invocation(command: str) -> bool:
    """Is the WHOLE command one program invocation?

    Quoted operators are arguments (``ctx run -- sh -c 'a && b'``,
    ``ctx search 'a|b'``); unquoted ones, and ``$(``/backticks even inside
    double quotes, make a second program possible, so the answer is no.
    Unparseable quoting is also no (the safe side).
    """
    try:
        for index, char in _shell_chars(command.strip()):
            if char in _SECOND_PROGRAM_CHARS:
                return False
            if char == "$" and command.strip()[index + 1:index + 2] in ("(", "{"):
                return False
    except ValueError:
        return False
    return True


def _split_simple_chain(stripped: str) -> list[str] | None:
    """Split ``a; b && c`` into segments when the only metacharacters present
    are the separators ``;``, ``&&``, ``||``. Returns None when any other
    metacharacter (``| > < ` $ ( ) { } \\`` or a lone ``&``) remains in a
    segment, so pipelines and substitutions never take this path."""
    parts = []
    start = 0
    skip_to = -1
    try:
        for index, char in _shell_chars(stripped):
            if index < skip_to:
                continue
            quoted_subst = char in "$`" and _in_double_quotes(stripped, index)
            if quoted_subst:
                return None  # Double-quoted substitutions still execute in the shell.
            if char == ";" or stripped[index:index + 2] in {"&&", "||"}:
                parts.append(stripped[start:index].strip())
                start = index + (1 if char == ";" else 2)
                skip_to = start
                continue
            if _SHELL_META_RE.search(char):
                return None
    except ValueError:
        return None
    if not parts:
        return None
    parts.append(stripped[start:].strip())
    return [part for part in parts if part]


def _in_double_quotes(stripped: str, at: int) -> bool:
    """Is position ``at`` inside a double-quoted span? (quote-aware, bounded)"""
    quote = None
    index = 0
    while index < at:
        char = stripped[index]
        if quote:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                index += 1
        elif char in "\"'":
            quote = char
        index += 1
    return quote == '"'


#: ctx's own MCP server, exactly as each host names it. Registered as
#: ``ctx-harness`` by the Codex/Antigravity plugin manifests and
#: ``ctx.mcp_hosts.SERVER`` (Hermes sanitizes the hyphen to ``ctx_harness``),
#: as ``ctx`` by ``ctx agent`` (``ctx.agent.MCP_SERVER``), and by Claude
#: Code's plugin scoping as ``plugin_ctx-harness_ctx``. Not a prefix: an
#: MCP server named ``ctxfoo`` is somebody else's.
_CTX_MCP_SERVERS = frozenset({"ctx", "ctx-harness", "ctx_harness",
                              "plugin_ctx-harness_ctx", "plugin_ctx_harness_ctx",
                              "plugin_ctx-harness_ctx-harness"})


def _is_ctx_mcp_tool(name: str) -> bool:
    if name == "ctx":  # the ctx-owned Antigravity SDK host's own tool
        return True
    if not name.startswith("mcp__"):
        return False
    server, sep, tool = name[5:].partition("__")
    return bool(sep and tool) and server in _CTX_MCP_SERVERS


def _which(name: str) -> str | None:
    """PATH lookup with ``os`` only (``shutil`` is off the hot path)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d or ".", name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _self_ctx_paths() -> set[str]:
    """Real paths of the ctx executable this hook is running as."""
    out: set[str] = set()
    try:
        if os.path.basename(sys.argv[0]) in ("ctx", "ctx.exe"):
            out.add(os.path.realpath(sys.argv[0]))
        out.add(os.path.realpath(os.path.join(os.path.dirname(sys.executable), "ctx")))
        found = _which("ctx")
        if found:
            out.add(os.path.realpath(found))
    except Exception:
        pass
    return out


def _is_ctx_invocation(command: str | None, cwd: str | None = None) -> bool:
    """Is this whole command exactly one invocation of the real ctx?

    One invocation: no shell construct that could run a second program
    (:func:`_single_invocation`). The real ctx: the bare name ``ctx`` (what
    the rewrites emit and the host resolves on its PATH), or a path or
    ``<python> -m ctx`` that resolves to the ctx/interpreter this hook is
    running as. A file merely named ``ctx`` elsewhere (``./fake/ctx``) is
    not, and neither is a command that re-points PATH first.
    """
    if not command or not _single_invocation(command):
        return False
    try:
        raw = shlex.split(command)
    except ValueError:
        return False
    if any(t.startswith("PATH=") or re.match(r"[A-Za-z_]\w*=", t) or os.path.basename(t) == "env" for t in raw[: len(raw) - len(_unwrap(raw))]) or any(t.startswith("PATH=") for t in raw):
        return False
    argv = _unwrap(raw)
    if not argv:
        return False
    tok = argv[0]
    if tok == "ctx":
        return True
    base = os.path.basename(tok)
    if base in ("ctx", "ctx.exe"):
        path = tok if os.path.isabs(tok) else os.path.join(cwd or os.getcwd(), tok)
        return os.path.realpath(path) in _self_ctx_paths()
    if base.startswith("python") and argv[1:4] in (["-P", "-m", "ctx"], ["-I", "-m", "ctx"]):
        path = tok if os.sep in tok else (_which(tok) or "")
        return bool(path) and os.path.realpath(path) == os.path.realpath(sys.executable)
    return False



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


#: Fields a nested ``toolCall`` object carries, and the flat names the rest
#: of this module reads.
_TOOL_CALL_FIELDS = (("name", "tool_name"), ("args", "tool_input"), ("id", "tool_use_id"),
                     ("result", "tool_response"), ("output", "tool_response"),
                     ("response", "tool_response"))


def _normalize_payload(payload: Any) -> dict[str, Any]:
    """Accept every payload shape a host sends, at the dialect boundary.

    Live Antigravity CLI (``agy`` 1.5.5) nests the call as
    ``{"hookEventName": ..., "toolCall": {"name", "id", "args"}, "cwd", ...}``
    rather than the flat ``tool_name``/``tool_input`` (or camelCase
    ``toolName``/``toolInput``) this module reads. Without lifting it, every
    real Antigravity call classified as a nameless tool and was allowed:
    ``cat huge.log`` passed the guard. The nested fields are lifted to their
    flat names; a flat field already present is never overwritten. Pure; any
    non-dict becomes ``{}``.
    """
    if not isinstance(payload, dict):
        return {}
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        call = payload.get("tool_call")
    if not isinstance(call, dict):
        return payload
    out = dict(payload)
    present = {
        "tool_name": ("tool_name", "toolName"),
        "tool_input": ("tool_input", "toolInput"),
        "tool_use_id": ("tool_use_id", "toolUseId"),
        "tool_response": _RESPONSE_KEYS_FLAT,
    }
    for src, dst in _TOOL_CALL_FIELDS:
        if src in call and not any(k in out for k in present[dst]):
            out[dst] = call[src]
    if "hook_event_name" not in out and isinstance(out.get("hookEventName"), str):
        out["hook_event_name"] = out["hookEventName"]
    return out


# Every top-level key a tool result may arrive under (see _RESPONSE_KEYS,
# which adds the bare "content"; a nested call's own "content" is not lifted).
_RESPONSE_KEYS_FLAT = ("tool_response", "toolResponse", "tool_output", "toolOutput",
                       "output", "result")


def _ancestor_workspace(start: str) -> str:
    """The workspace containing ``start``, by the same markers
    ``ctx.workspace.resolve_workspace`` uses after explicit and hook paths:
    the nearest ancestor with ``ctx.toml``, else with ``.git``, else with
    ``.agents/``, else ``start`` itself. A host reports the shell's current
    directory as ``cwd``; taking it for the workspace put ledgers, policy and
    the confinement root in whatever subdirectory the agent had cd'd into."""
    here = os.path.abspath(start)
    chain = []
    cur = here
    for _ in range(64):
        chain.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for marker in ("ctx.toml", ".git", ".agents"):
        for d in chain:
            if os.path.exists(os.path.join(d, marker)):
                return d
    return here


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
        start = probe if probe and os.path.isdir(probe) else payload.get("cwd")
        return _ancestor_workspace(start) if isinstance(start, str) and start else start
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


_AGY_SCHEMA_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: Names a tool schema has no reason to carry. Such a file is asked about
#: even under the schema directory (the outside-root rule's answer).
_AGY_SCHEMA_SECRETISH = re.compile(
    r"(?i)(token|secret|credential|passw|private|cookie|session|auth|"
    r"id_(rsa|dsa|ecdsa|ed25519)|(^|[._-])keys?([._-]|$)|\.pem)")


def _agy_mcp_schema_read(payload: dict[str, Any], path_str: str) -> bool:
    """Is this read_file agy loading an MCP tool's schema?

    agy 1.5.5 loads MCP tools lazily: the model reads
    ``<appDataDir>/mcp/<server>/<tool>.json`` with read_file before calling
    the tool (observed live: the ctx server's own ``ctx.json``). That path is
    outside the workspace, so the outside-root rule asked for it, and a
    headless run refused it, which made every MCP tool unusable. It is the
    host's tool-definition file (the "schema" kind, exempt everywhere), so
    it passes. ``appDataDir`` is taken from the payload's
    ``artifactDirectoryPath`` (``<appDataDir>/brain/<conversationId>``).
    """
    art = payload.get("artifactDirectoryPath")
    if not isinstance(art, str) or not art or not path_str.endswith(".json"):
        return False
    app = os.path.dirname(os.path.dirname(os.path.normpath(art)))
    if os.path.basename(os.path.dirname(os.path.normpath(art))) != "brain":
        return False
    mcp = os.path.realpath(os.path.join(app, "mcp"))
    target = os.path.realpath(path_str)
    if not target.startswith(mcp + os.sep):
        return False
    # Exactly <server>/<tool>.json, the shape agy writes (live listing:
    # mcp/ctx-harness_ctx-harness/ctx.json); no hidden or nested parts.
    parts = os.path.relpath(target, mcp).split(os.sep)
    return (len(parts) == 2 and all(_AGY_SCHEMA_PART.match(x) for x in parts)
            and not any(_AGY_SCHEMA_SECRETISH.search(x) for x in parts)
            and not _is_secret_path(target, None))


def _path_outside(path_str: str, workspace_root: str | None) -> bool:
    if not workspace_root:
        return False
    p = os.path.abspath(os.path.realpath(path_str))
    root = os.path.abspath(os.path.realpath(workspace_root))
    return not (p == root or p.startswith(root + os.sep))


def classify_command(
    command: str, policy: dict[str, Any], _depth: int = 0, *, cwd: str | None = None
) -> dict[str, str]:
    """Classify a command, then abbreviate any lesson already taught.

    The guard has ~20 places that build remediation prose, and each one used to
    re-send its full explanation every time. Replaying this repo's own
    transcripts found sessions the harness made *worse* (128 -> 439 tokens,
    -243%) purely from repeated advice. Decay is applied here, once, so every
    builder inherits it instead of 20 of them each remembering to."""
    return _classify_command_inner(command, policy, _depth, cwd=cwd)


def _lesson_key(reason: str) -> str:
    """A stable name for the lesson a reason teaches: its headline, not the
    command it happened to be about."""
    head = reason.strip().splitlines()[0] if reason.strip() else ""
    head = head.replace("CTX_CONTEXT_GUARD:", "").strip()
    return " ".join(head.split()[:6]).rstrip(".,")


def _decay_taught_reason(decision: dict[str, Any], workspace_root: str | None) -> None:
    """Drop teaching prose already sent this session, keeping the verdict line.

    Scope is deliberately narrow: only a decision that *allows* the call while
    rewriting it. That is transparent usability steering — the command runs,
    just in bounded form. Safety-class outcomes (secret paths, workspace
    escape, committed deny_commands) are deny/force_ask and are therefore
    untouched by construction, which is what the rule-7 invariant in
    tests/test_safety_invariant.py requires: no adaptive state may reword a
    safety denial, however many times it fires."""
    if workspace_root is None or not isinstance(decision, dict):
        return
    if decision.get("_safety") or not decision.get("_rewrite"):
        return
    try:
        from ctx.engagement import note_taught
    except Exception:
        return
    for holder in (decision, decision.get("_rewrite")):
        if not isinstance(holder, dict):
            continue
        reason = holder.get("reason")
        if not isinstance(reason, str) or "\n" not in reason:
            continue  # already one line: nothing to save
        try:
            if note_taught(workspace_root, _lesson_key(reason)):
                continue  # first time: teach it in full
            holder["reason"] = reason.splitlines()[0].strip()
        except Exception:
            continue


def _ctx_run_inner(argv: list[str]) -> list[str]:
    """The command ``ctx run [flags] -- <argv>`` will execute, or ``[]``.
    ``--shell -- '<string>'`` is split like the shell would; a string the
    shell would reject yields ``[]`` (the guard then has nothing to judge,
    and ``ctx run`` itself reports the error)."""
    if len(argv) < 3 or argv[1] != "run" or "--" not in argv[2:]:
        return []
    sep = argv.index("--", 2)
    inner = argv[sep + 1:]
    if "--shell" in argv[2:sep] and len(inner) == 1:
        try:
            inner = shlex.split(inner[0])
        except ValueError:
            return []
    return inner


def _names_secret_path(argv: list[str], cwd: str | None) -> bool:
    """Does any path-shaped argument name a secret-bearing file?

    One predicate for every door: the plain command path and the
    ``cmd > file 2>&1`` shortcut both consult it, so a new early return
    cannot quietly skip the guarantee again. Restricted to arguments shaped
    like paths, because the secret regex alone matches the bare WORD
    "secrets", which is a sentence, not a file.
    """
    for _arg in argv[1:]:
        if _arg.startswith("-"):
            continue
        _p = _arg.replace("\\", "/")
        if "/" not in _p and "." not in _p and not _SECRET_BARE_RE.match(_p):
            continue
        if _is_secret_path(_p, cwd):
            return True
    return False


def _secret_path_force_ask() -> dict[str, str]:
    return _force_ask(
        "CTX_CONTEXT_GUARD: secret-bearing path. Reading it requires "
        "an explicit user-visible permission step; it is excluded "
        "from automatic capture."
    )


# Container-engine invocations that reach past the workspace confinement.
# The table is headlong's docker broker denylist (privileged containers,
# host namespaces, host devices, the engine socket, bind mounts from
# outside the workspace), applied here as a safety-class force_ask: the
# command is not wrong, it is a decision the user has to see. Same class,
# same door discipline, as secret-bearing paths -- the plain command, the
# ``> file 2>&1`` shortcut, chain segments and ``ctx run --`` all consult
# it. ``docker`` is also in _UNBOUNDED_CMDS, so a plain ``docker run`` still
# gets the volume-class deny-with-remediation; this check comes first because
# the remediation for a deny is ``ctx run -- <same argv>``, and a privileged
# container is not made acceptable by capturing its output.
_CONTAINER_PROGS = {"docker", "podman", "nerdctl"}
_HOST_NS_FLAGS = {"--pid", "--network", "--net", "--ipc", "--uts", "--userns", "--cgroupns"}
_CAP_ESCAPE = {"ALL", "SYS_ADMIN", "CAP_SYS_ADMIN", "CAP_ALL", "SYS_PTRACE", "CAP_SYS_PTRACE"}
_ENGINE_SOCKET_MARKS = ("docker.sock", "podman.sock", "containerd.sock")


def _bind_source_escape(src: str, cwd: str | None) -> str | None:
    """Why a bind-mount SOURCE reaches outside the workspace, or None."""
    if not src:
        return None
    if any(src.endswith(m) or f"{m}:" in src for m in _ENGINE_SOCKET_MARKS) or "/run/docker" in src:
        return "the container engine socket is mounted"
    if cwd:
        src = src.replace("${PWD}", cwd).replace("$PWD", cwd).replace("$(pwd)", cwd)
    if src.startswith("$"):
        return "a bind mount from a shell variable the guard cannot resolve"
    if src.startswith("~"):
        src = os.path.expanduser(src)
    if not src.startswith(("/", ".")):
        return None  # a named volume, not a host path
    if not cwd:
        # No workspace to confine to: only an absolute host root is certain.
        return "the host filesystem root is bind-mounted" if src.rstrip("/") == "" else None
    absolute = src if os.path.isabs(src) else os.path.join(cwd, src)
    if _path_outside(absolute, cwd):
        return "a bind mount from outside the workspace"
    return None


def _container_escape(argv: list[str], cwd: str | None) -> str | None:
    """The first container-escape surface in a docker/podman/nerdctl argv,
    as a short reason fragment, or None. Pure over argv; the only I/O is
    path resolution for bind-mount sources."""
    if not argv or os.path.basename(argv[0]) not in _CONTAINER_PROGS:
        return None
    toks = argv[1:]
    for i, tok in enumerate(toks):
        flag, has_eq, inline = tok.partition("=")
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        value = inline if has_eq else nxt
        if tok == "--privileged":
            return "a privileged container"
        if flag in _HOST_NS_FLAGS and value == "host":
            return f"a host namespace ({flag}=host)"
        if flag == "--device":
            return "a host device is passed through"
        if flag == "--cap-add" and value.upper() in _CAP_ESCAPE:
            return f"capability {value.upper()} is added"
        if flag in ("-v", "--volume"):
            why = _bind_source_escape(value.split(":", 1)[0], cwd)
            if why:
                return why
        if flag == "--mount":
            fields = dict(kv.split("=", 1) for kv in value.split(",") if "=" in kv)
            src = fields.get("source") or fields.get("src") or ""
            if fields.get("type", "volume") == "bind" or any(m in src for m in _ENGINE_SOCKET_MARKS):
                why = _bind_source_escape(src, cwd)
                if why:
                    return why
    return None


def _container_escape_force_ask(why: str) -> dict[str, str]:
    return _force_ask(
        f"CTX_CONTEXT_GUARD: container escape surface — {why}. It reaches past "
        "the workspace confinement; running it requires an explicit "
        "user-visible permission step."
    )


#: Programs whose part in a compound command ctx can vouch for: they read,
#: filter or print, or change only the shell's own state. A capture that
#: runs them as a side effect runs nothing the agent would need approval
#: for. Anything else that alone is merely "not ctx's business" (an allow
#: such as `git push`) keeps the whole command away from capture.
_INERT_PARTS = frozenset({
    "true", "false", ":", "test", "[", "[[", "echo", "printf", "pwd", "cd",
    "head", "tail", "wc", "cat", "grep", "egrep", "fgrep", "rg", "ls", "sort",
    "uniq", "cut", "tr", "nl", "column", "fold", "fmt", "rev", "tac", "paste",
    "comm", "expand", "unexpand", "basename", "dirname", "realpath", "date",
    "seq", "sleep", "jq", "tee", "set", "export", "exit", "return", "wait",
    "shift", "unset", "local", "declare", "typeset", "readonly", "alias",
    "unalias", "read", "shopt", "diff", "cmp", "stat", "file",
})
#: git subcommands that only read (no form of them writes).
_GIT_READ_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "ls-files", "grep", "rev-parse",
    "describe", "shortlog", "whatchanged", "cat-file", "ls-tree", "rev-list",
})
#: ctx verbs that only read: vouched as a part. `ctx run` and friends run
#: arbitrary commands and are not.
_CTX_READ_VERBS = frozenset({
    "stats", "get", "search", "q", "diff", "show", "outline", "refs", "doctor",
    "coverage", "jobs", "ladders", "status", "help", "--help", "--version",
})
_ASSIGN_ONLY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
#: find primaries that act on what they find.
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                           "-fprint", "-fprint0", "-fprintf", "-fls"})


def _find_acts(argv: list[str]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) == "find" and any(
        a in _FIND_ACTIONS for a in argv[1:])


def _vouched(prog: str, argv: list[str]) -> bool:
    """Can a capture run this allowed part as a side effect? Only a part
    that reads, prints or sets shell state: the inert table, the bounded
    table (less the two that create files), the span registry's read-only
    entries, and a bare assignment."""
    if prog in _INERT_PARTS or (prog in _BOUNDED_CMDS and prog not in ("mkdir", "touch")):
        return True
    if all(_ASSIGN_ONLY_RE.match(w) for w in argv):
        return True
    if prog == "ctx":
        return len(argv) > 1 and argv[1] in _CTX_READ_VERBS
    try:
        if classify_command_span(argv) == SPAN_ALLOW:
            return True
        return prog == "git" and git_subcommand(argv)[0] in _GIT_READ_SUBCOMMANDS
    except Exception:
        return False


def _part_program(segment: str) -> tuple[str, list[str]] | None:
    try:
        argv = _unwrap(shlex.split(segment))
    except ValueError:
        return None
    if not argv:
        return "", []
    return os.path.basename(argv[0]), argv


def _compound_verdict(
    stripped: str, policy: dict[str, Any], _depth: int, *, cwd: str | None
) -> dict[str, Any] | None:
    """The whole command's verdict from its parts, or None to classify it
    as one command (it is one simple command)."""
    from ctx.shsplit import split_segments

    whole = shlex.quote(stripped)
    segments = split_segments(stripped)
    if segments is None:
        return _force_ask(
            "CTX_CONTEXT_GUARD: this shell expression cannot be split into its "
            "commands with confidence, so it is put to you as written. To run "
            f"it with bounded output: ctx run --shell -- {whole}")
    if not segments:
        return dict(DECISION_ALLOW)
    if len(segments) == 1:
        return None  # one simple command (a here-document, a comment, a group)
    hard: list[tuple[str, dict[str, Any]]] = []
    unvouched: list[str] = []
    decisions = []
    # Each distinct part is classified and parsed once (a long compound
    # repeats its parts: `cat f | grep x && echo stepN` sixty times).
    seen: dict[str, tuple[dict[str, Any], Any]] = {}
    for seg in segments:
        hit = seen.get(seg)
        if hit is None:
            hit = (classify_command(seg, policy, _depth + 1, cwd=cwd), _part_program(seg))
            seen[seg] = hit
        d, part = hit
        decisions.append(d)
        prog, argv = part if part is not None else ("", [])
        if d.get("_safety"):
            return d  # a rule the repo committed, a secret, an escape: as is
        if part is None:
            hard.append((seg, d))
        elif d.get("decision") in ("deny", "force_ask") and not d.get("_rewrite"):
            if prog not in _INERT_PARTS:
                hard.append((seg, d))
        elif _find_acts(argv):
            unvouched.append(seg)
        elif d.get("decision") == "allow" and prog and not _vouched(prog, argv):
            unvouched.append(seg)
    if hard:
        seg, d = hard[0]
        reason = str(d.get("reason") or "")
        if reason and "unknown output bound" not in reason and "ctx run" not in reason:
            return d  # its own reason (a container escape, an in-place edit) names it
        return _force_ask(
            "CTX_CONTEXT_GUARD: part of this command is something ctx cannot "
            f"vouch for ({seg.splitlines()[0][:80]!r}), so the whole is put to you "
            f"as written. To run all of it with bounded output: ctx run --shell -- {whole}")
    if unvouched:
        # Capture would run these parts under the wrapper's approval: leave
        # the command untouched for the host to judge; the output gate
        # bounds what it prints.
        return dict(DECISION_ALLOW)
    if all(d.get("decision") == "allow" for d in decisions):
        return dict(DECISION_ALLOW)
    # Every part is capturable or inert, and at least one wants capture:
    # capture the whole, verbatim. Decided here, not by the first program
    # (`git status\ncat big.log` is not a git command).
    if any(_inplace_text_edit_in(seg) for seg in seen):
        return None  # the expression rules below give its own remediation
    fa = _force_ask(
        "CTX_CONTEXT_GUARD: compound shell expression with unproven output bound. "
        f"Prefer: ctx run --shell -- {whole}")
    if _steering_allows(policy):
        forever = any(part and part[1] and _follows_forever(part[1])
                      for _d, part in seen.values())
        fa["_rewrite"] = {"command": _capture_command(stripped, " --bg" if forever else ""),
                          "reason": _REWRITE_REASON}
    return fa


def _classify_command_inner(
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
                # This shortcut answers the VOLUME question and only that one:
                # `pytest > out.log 2>&1` keeps the console clean, so the
                # volume-class steering that would otherwise route it through
                # `ctx run` is legitimately satisfied. It must not also answer
                # the SAFETY question. It used to return straight out, ahead
                # of the repo-committed [guard] deny_commands check below, so
                # `denied-command > out.txt 2>&1` switched off a rule the repo
                # committed on purpose just by adding a redirect.
                denied = _deny_commands_match(redir.group("cmd") or "", policy)
                if denied is not None:
                    return _safety_deny_cmd(
                        denied, policy, original=stripped, has_meta=has_meta
                    )
                # Same lesson, second door. The deny_commands check above was
                # added because this shortcut returned ahead of a repo-
                # committed rule; it still returned ahead of the secret-path
                # guard below, so `cat .env > out.log 2>&1` was allowed while
                # `cat .env` force-asks. The redirect answers the volume
                # question only; the blanket secret guarantee holds here too.
                try:
                    inner = shlex.split(redir.group("cmd") or "")
                except ValueError:
                    inner = []
                if _names_secret_path(inner, cwd):
                    return _secret_path_force_ask()
                escape = _container_escape(inner, cwd)
                if escape is not None:
                    return _container_escape_force_ask(escape)
                return dict(DECISION_ALLOW)
        # A command is only as permitted as its least permitted part,
        # whatever joins the parts: newline, `;`, `&&`, `||`, `|`, `&`, a
        # subshell, a substitution, a here-document fed to a shell. Split
        # conservatively (ctx.shsplit), classify every part alone, and never
        # let the whole be captured -- which runs every part -- unless every
        # part could have been.
        compound = _compound_verdict(stripped, policy, _depth, cwd=cwd)
        if compound is not None:
            return compound

    try:
        argv = shlex.split(stripped)
    except ValueError:
        return _force_ask(
            "CTX_CONTEXT_GUARD: unparseable shell expression. "
            f"If output may be large, use: ctx run --shell -- {shlex.quote(stripped)}"
        )
    if not argv:
        return dict(DECISION_ALLOW)

    argv = _unwrap(argv)
    if not argv:
        return dict(DECISION_ALLOW)

    prog = os.path.basename(argv[0])

    # Already routed through ctx → allow, once the safety class has had its
    # look at what ``ctx run -- <argv>`` will actually execute. Capture is
    # a volume answer; it never makes a secret read or a container escape
    # acceptable, and the deny remediation itself points here.
    # Only when the WHOLE command is one invocation of the real ctx: a
    # compound that merely starts with ctx (`ctx stats && cat big.log`) or a
    # look-alike named ctx (`./fake/ctx dump`) is classified like any other.
    if prog == "ctx" and _is_ctx_invocation(stripped, cwd):
        inner = _ctx_run_inner(argv)
        if _names_secret_path(inner, cwd):
            return _secret_path_force_ask()
        escape = _container_escape(inner, cwd)
        if escape is not None:
            return _container_escape_force_ask(escape)
        return dict(DECISION_ALLOW)

    # Repo-configured overrides win over built-in tables.
    canonical = " ".join(argv)
    for prefix in policy.get("deny_commands", []):
        if _restricts_match(canonical, prefix):
            # Safety class: a rule the repo committed on purpose. Its wording is
            # frozen by the rule-7 invariant (tests/test_safety_invariant.py) —
            # no adaptive state may reword it, so mark it and never decay it.
            return _safety_deny_cmd(
                argv, policy, original=stripped, has_meta=has_meta
            )

    # One guard, every door. docs/TROUBLESHOOTING.md promises a BLANKET
    # guarantee that secret-bearing paths always force-ask, but the check
    # lived only in classify_read -- the native Read door -- so `head .env`,
    # `cat secrets.json` and `tail id_rsa` walked past it through the shell.
    #
    # Placed AFTER the deny_commands loop on purpose: an explicit repo-
    # committed deny is stronger than a force_ask, and putting this first
    # downgraded `echo secrets please` from deny to ask. Restricted to
    # arguments that are shaped like paths, because the regex alone matches
    # the bare WORD "secrets", which is a sentence, not a file.
    if _names_secret_path(argv, cwd):
        return _secret_path_force_ask()
    escape = _container_escape(argv, cwd)
    if escape is not None:
        return _container_escape_force_ask(escape)
    # A prefix allow/promotion applies to a single command only. When shell
    # metacharacters survived the chain/redirect handling above (e.g.
    # ``echo hi && rm -rf x``), ``shlex.split`` keeps ``&&`` as an ordinary
    # token, so ``canonical`` would still start with an allowed prefix — a
    # compound-command bypass. Prefix allows are therefore gated on
    # ``not has_meta`` (deny prefixes are not: denying more is always safe).
    if not has_meta:
        for prefix in policy.get("allow_commands", []):
            if _grants_match(canonical, prefix):
                return dict(DECISION_ALLOW)
        # Learned policy epoch (ctx-policy.toml): promoted signatures behave
        # exactly like allow_commands canonical prefixes. Demoted signatures
        # are checked FIRST and are never allowed via promotion (belt against
        # a conflicting or hand-edited epoch); a demoted command is not
        # denied here — it simply falls through to normal classification.
        if not any(
            # A demotion RESTRICTS (it withholds a promotion), so it matches
            # unbounded for the same reason a deny does.
            _restricts_match(canonical, p) for p in policy.get("demoted_commands", [])
        ):
            for prefix in policy.get("promoted_commands", []):
                if _grants_match(canonical, prefix):
                    return dict(DECISION_ALLOW)

    if _find_acts(argv):
        # `find -delete` / `-exec`: it changes files, and its output is small
        # or the gate bounds it. Capture would run it under the wrapper's
        # approval; the host judges it as typed.
        return dict(DECISION_ALLOW)

    # `bash -c '<inner>'`: classify the inner command, not the shell.
    if prog in ("bash", "sh", "zsh", "dash", "fish") and len(argv) >= 3 and argv[1] == "-c":
        inner = classify_command(argv[2], policy, _depth + 1, cwd=cwd)
        rw = inner.get("_rewrite")
        if isinstance(rw, dict) and "command" in rw:
            # The inner command decided the class; the capture still runs the
            # WHOLE command as typed. Rewriting to the inner string alone
            # dropped the outer shell, its environment prefix and the rest of
            # its arguments (`FOO=bar sh -c 'echo $FOO'` printed nothing).
            bg = " --bg" if " --bg " in rw["command"] else ""
            rw["command"] = _capture_command(command, bg)
        return inner

    if prog == "xargs":
        return _deny_cmd(
            argv, policy, original=stripped, has_meta=has_meta
        )  # stdin consumer: never rewritten

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
            bg = " --bg" if _follows_forever(argv) else ""  # fix: never-terminating piped command needs --bg too
            fa["_rewrite"] = {
                "command": _capture_command(command, bg),
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

    span = classify_command_span(argv)
    if span in (SPAN_ALLOW, SPAN_CAPTURE):
        guard_action = choose_guard(
            {
                "bounded_command": span == SPAN_ALLOW,
                "structured_result": span == SPAN_ALLOW,
                "readonly_noisy": span == SPAN_CAPTURE,
            }
        )
        if guard_action == "allow":
            return dict(DECISION_ALLOW)
        if guard_action == "rewrite_command":
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)

    if prog == "git":
        sub, _git_rest = git_subcommand(argv)
        if sub in _GIT_UNBOUNDED:
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
        if sub == "status" and not ("--short" in argv or "-s" in argv or "--porcelain" in argv):
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
        return dict(DECISION_ALLOW)

    if prog == "ls":
        if any(a.startswith("-") and "R" in a for a in argv[1:]):
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
        return dict(DECISION_ALLOW)

    if prog in ("python", "python3", "node", "ruby", "perl", "deno"):
        # Interpreter invocations can read anything (guard-bypass channel)
        # and emit anything; route through ctx.
        if len(argv) == 1:
            return _deny(_remediation(argv))  # bare REPL: interactive-suspect
        return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)

    if prog in _BOUNDED_CMDS:
        if choose_guard({"bounded_command": True}) == "allow":
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
        return _deny_cmd(
            argv, policy, original=stripped, has_meta=has_meta
        )  # read-only: bounded capture via ctx run

    if prog in _UNBOUNDED_CMDS:
        if choose_guard({"flood_command": True}) == "rewrite_command":
            return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)

    # Unknown command → configured policy.
    unknown = policy.get("unknown_command", "force_ask")
    if unknown == "allow" or policy.get("mode") == "advisory":
        return dict(DECISION_ALLOW)
    if unknown == "deny":
        return _deny_cmd(argv, policy, original=stripped, has_meta=has_meta)
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

    # Byte-count flags first: they bound the same read in a different unit,
    # and the line guard was blind to them entirely.
    for i, a in enumerate(argv[1:], start=1):
        raw_bytes: str | None = None
        if a in ("-c", "--bytes") and i + 1 < len(argv):
            raw_bytes = argv[i + 1]
        elif a.startswith("--bytes="):
            raw_bytes = a.split("=", 1)[1]
        elif a.startswith("-c") and len(a) > 2:
            raw_bytes = a[2:]
        if raw_bytes is not None:
            nbytes = _count(raw_bytes)
            # Unparseable, signed (unbounded mode), or simply too big.
            if nbytes is None or nbytes > _HEAD_TAIL_MAX_BYTES:
                return None
            return 1  # a genuinely small byte slice is a bounded read

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
        ledger_dir = session_reads_dir(workspace_root)
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
        # NB: no `isinstance(limit, bool)` guard — the original had none, and
        # a wide mechanical edit is not the place to change what a nonsense
        # `"context_limit": true` renders as.
        limit = read_window_doc(workspace_root).get("context_limit")
        if isinstance(limit, int) and limit > 0:
            note += f" ≈ {max(1, round(100 * tok / limit))}% of window"
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


def _deny_commands_match(fragment: str, policy: dict[str, Any]) -> list[str] | None:
    """The repo-committed ``[guard] deny_commands`` prefix match for a command
    fragment, or None. Returns the parsed argv so the caller can build the
    canonical denial.

    Split out so an allow-shortcut can consult the SAFETY class without
    inheriting volume-class steering. The two are different questions and a
    redirect only answers one of them: `pytest > out.log 2>&1` genuinely
    solves the volume problem, but no amount of redirection makes a command
    the repo deliberately forbade acceptable to run.
    """
    try:
        argv = _unwrap(shlex.split(fragment.strip()))
    except ValueError:
        return None
    if not argv:
        return None
    canonical = " ".join(argv)
    for prefix in policy.get("deny_commands", []):
        if _restricts_match(canonical, prefix):
            return argv
    return None


def _grants_match(canonical: str, prefix: str) -> bool:
    """Prefix match for a rule that GRANTS authority, at a token boundary.

    config.py documents these as "prefix matches against the canonical
    argv", and the match was a raw ``str.startswith`` -- so a prefix ending
    mid-token matched a different token that merely shares its opening
    characters. An ``allow_commands`` entry of ``git push origin main`` also
    matched ``git push origin main-hotfix --force``: a different branch,
    admitted by a rule written to scope pushes to one. Same class as a path
    glob whose ``*`` crosses a ``/``, an intent keyword firing inside
    "sprint", and an MCP provider named ``git`` absorbing ``github``'s
    invocations -- the fourth on this branch.

    A prefix still matches the same command with extra ARGUMENTS -- that is
    what makes it a prefix -- but only when the next character ends the token
    it stopped on.
    """
    prefix = prefix.strip()
    if not prefix:
        return False
    return canonical == prefix or canonical.startswith(prefix + " ")


def _restricts_match(canonical: str, prefix: str) -> bool:
    """Prefix match for a rule that RESTRICTS -- deliberately unbounded.

    The asymmetry is the same one already documented for ``has_meta`` a few
    lines below: denying more is always safe, allowing more is not. A
    ``deny_commands`` entry of ``rm -rf /tmp/scratch`` should keep covering
    ``rm -rf /tmp/scratch/inner``, and tightening it to a token boundary
    would have quietly NARROWED a safety rule while fixing the grant side --
    a fix in one direction that opens a hole in the other.
    """
    prefix = prefix.strip()
    return bool(prefix) and canonical.startswith(prefix)


def _pressured_window(max_lines: int, total: int, budget: int) -> int:
    """Line window for a bounded read, tightened by how far the session has
    already overrun its read budget.

    Two properties this must have, both learned from a measured failure
    (evals/devex/, harnessed arm: 167 native reads landed 796 KiB against a
    256 KiB budget):

    * **It escalates.** The old throttle was a single binary step — one fixed
      window the moment the budget was crossed, identical at 1.01x and at
      6.3x overrun. A session already 6x over kept paying the same toll, so
      133 post-budget reads still averaged ~4 KiB apiece.
    * **It applies to large files too.** The large-file branch returned before
      the ledger was ever consulted, so files over ``max_inline_bytes`` — the
      reads that dominate the flood — were the *only* ones exempt from session
      pressure. Measured max read stayed ~15 KiB before and after the budget
      was crossed, because those reads never saw it.

    At or under budget the window is unchanged. At exactly the budget it
    reproduces the previous over-budget behaviour (``max_lines // 4``), then
    tightens hyperbolically with the overrun ratio, floored so a bounded read
    still carries enough lines to be worth reading at all."""
    if budget <= 0 or total <= budget:
        return max_lines
    over = total / budget
    return max(_OVER_BUDGET_MIN_LINES, int(max_lines / (4 * over)))


#: Token budget for the outline that answers a whole-file read. A 64 KB
#: Python module (~16k tok) outlines to ~1k tok at this budget; the first
#: 240-line page it replaces was ~2.5k tok and showed 14% of the file.
_OUTLINE_BUDGET_TOKENS = 1200


def _whole_file_read(tool_input: dict[str, Any] | None) -> bool:
    """A Read that asked for the file, not a slice of it."""
    if not tool_input:
        return False
    return not any(
        isinstance(tool_input.get(k), int) and not isinstance(tool_input.get(k), bool)
        and tool_input.get(k) > 0
        for k in ("offset", "limit", "start_line", "end_line", "StartLine", "EndLine")
    )


def _skeleton_outline_for(path_str: str, workspace_root: str | None, size: int,
                          range_hint: str = "Read with offset and limit") -> str | None:
    """The map of a large code file, for a Read that wanted all of it.

    Measured on DeepSWE (cattrs, haiku): every whole-file Read of the 64 KB
    converters.py came back as its first 240 lines, ~2.5k tokens showing 14%
    of the file and no structure, and the session paged on from there. The
    skeleton (tree-sitter / ctags / stdlib ast, with line ranges and minted
    spans) is ~1k tokens for all 83 symbols and makes the next read a
    targeted one. Slice reads (offset/limit) are never touched. Fail-open:
    any problem returns None and the bounded first page applies as before."""
    if not workspace_root:
        return None
    try:
        from ctx.skeleton import language_for, skeleton_for, skeleton_outline

        rel = os.path.relpath(path_str, workspace_root).replace(os.sep, "/")
        if rel.startswith("..") or not language_for(rel):
            return None
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        ws = resolve_workspace(workspace_root)
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        sk = skeleton_for(store, ws, rel)
        if not sk.get("symbols"):
            return None
        outline = skeleton_outline(sk, _OUTLINE_BUDGET_TOKENS)
        return (
            f"CTX_CONTEXT_GUARD: whole-file read of a {size:,}-byte code file "
            f"(~{size // 4:,} tok). Its map is below instead of the first page; "
            f"read what you need by range ({range_hint}) or by "
            f"symbol (ctx get repo:{rel} --symbol <Name>).\n" + outline
        )
    except Exception:
        return None


#: Antigravity's view_file takes a line range, not offset/limit (hooks
#: reference, supported tools: "AbsolutePath, StartLine (optional), EndLine
#: (optional)"). A read in that style is bounded by its own range, and any
#: advice to it must name those parameters.
_RANGE_KEYS = ("StartLine", "EndLine")


def _range_style(tool_input: dict[str, Any] | None) -> bool:
    # ``AbsolutePath``: view_file. ``Path``: read_file, which replaced it in
    # agy's default toolset and takes the same StartLine/EndLine (1-based,
    # inclusive: live 1.5.5 answered StartLine=2 EndLine=3 with "Showing
    # lines 2-3 of 10").
    return isinstance(tool_input, dict) and (
        "AbsolutePath" in tool_input or "Path" in tool_input
        or any(k in tool_input for k in _RANGE_KEYS))


def _range_span(tool_input: dict[str, Any] | None) -> int | None:
    """Lines a StartLine/EndLine read asks for, or None when unbounded."""
    if not isinstance(tool_input, dict):
        return None
    a, b = tool_input.get("StartLine"), tool_input.get("EndLine")
    if (isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool)
            and not isinstance(b, bool) and b >= a >= 0):
        return b - a + 1
    return None


#: How far into a file the range-read byte check may scan before it gives
#: up and answers "too large" (the safe side). Only files over the inline
#: budget are scanned at all.
_RANGE_SCAN_CAP = 8 * 1024 * 1024


def _range_scan(path: str, start0: int, want: int, budget: int) -> tuple[int, int | None]:
    """Bytes in ``want`` lines starting at 0-based line ``start0``, bounded.

    Returns ``(fit, nbytes)``: ``fit`` is how many whole lines from
    ``start0`` fit in ``budget`` bytes (at most ``want``); ``nbytes`` is the
    range's size when all of it fits (end of file counts as fitting), else
    None. Reads in bounded pieces -- a 4 MB single line is never held whole
    -- and stops after ``_RANGE_SCAN_CAP`` bytes, answering None (too large).
    """
    scanned = 0
    try:
        with open(path, "rb") as fh:
            line = 0
            while line < start0:  # skip to the range, counting newlines only
                piece = fh.readline(65536)
                if not piece:
                    return 0, 0  # range starts past the end: nothing to read
                scanned += len(piece)
                if scanned > _RANGE_SCAN_CAP:
                    return -1, None  # start not located within the cap
                if piece.endswith(b"\n"):
                    line += 1
            fit, nbytes = 0, 0
            while fit < want:
                current = 0
                while True:  # one line, in pieces no larger than the budget allows
                    piece = fh.readline(max(1, budget - nbytes - current + 1))
                    current += len(piece)
                    if not piece or piece.endswith(b"\n") or nbytes + current > budget:
                        break
                if nbytes + current > budget:
                    return fit, None
                if current == 0:
                    return fit, nbytes  # end of file inside the range
                nbytes += current
                fit += 1
                if not piece.endswith(b"\n"):
                    return fit, nbytes  # last line, no trailing newline
            return fit, nbytes
    except OSError:
        return 0, None


def _range_advice(tool_input: dict[str, Any], path_str: str, workspace_root: str | None,
                  size: int, limit: int, window: int, note: str) -> dict[str, Any]:
    """Deny an over-budget StartLine/EndLine read with advice that works there.

    The saved Antigravity reference names view_file's parameters
    (AbsolutePath, StartLine, EndLine, IsSkillFile) but not whether lines
    start at 0 or 1, so the range is measured as the union of both readings
    (0-based lines ``StartLine-1 .. EndLine``): a range admitted here is
    within budget whichever the host means. ``0..0`` is therefore the first
    line. When even one line is over budget, only a byte-addressed read can
    help, and the advice names it.
    """
    a = tool_input.get("StartLine") if isinstance(tool_input.get("StartLine"), int) else 1
    a = max(int(a), 0)
    start0 = max(a - 1, 0)
    asked = _requested_range(tool_input)
    fit, _n = _range_scan(path_str, start0, window, limit)
    b = tool_input.get("EndLine") if isinstance(tool_input.get("EndLine"), int) else a + window - 1
    rel = path_str
    if workspace_root:
        try:
            rel = os.path.relpath(path_str, workspace_root).replace(os.sep, "/")
        except ValueError:
            pass
    end = start0 + fit - 1  # the union reading of (a, end) is exactly ``fit`` lines
    if asked is not None and asked[1] is not None:
        end = min(end, asked[1])  # never past the end that was asked for
    if fit < 0 or start0 > 0:
        # Byte offsets of a later range are not known (and past the scan cap
        # not even its start), so the advice addresses the requested lines;
        # `ctx get --lines` bounds its own output with a continuation.
        addr = f"--lines {max(a, 1)}:{max(b, a, 1)}  (ctx bounds it and names the continuation)"
    else:
        addr = f"--bytes 1:{limit}  (1-based byte span)"
    reason = (f"CTX_CONTEXT_GUARD: that line range of this {size}-byte file is over the "
              f"{limit}-byte inline budget (view_file bounds lines, not bytes).\n"
              f"Use: ctx get repo:{rel} {addr}\n"
              f"or:  ctx search repo:{rel} '<pattern>' --context 3" + note)
    decision: dict[str, Any] = _deny(reason)
    # A bound only ever narrows the request: same start, an earlier end. A
    # request that cannot be honoured that way (a bad start, an end before
    # the start, a first line already over budget) is denied with the advice
    # above rather than served a different region.
    if asked is not None and asked[0] == a and end >= max(a, 1):
        decision["_rewrite"] = {
            "fields": {"StartLine": a, "EndLine": end},
            # Its own first line: lesson decay keys on the first line, and the
            # deny above must not decay this one within the same call.
            "reason": (f"CTX_CONTEXT_GUARD: a view_file range that fits is StartLine={a} to "
                       f"EndLine={end}; the one asked for is over the {limit}-byte inline "
                       "budget.\n" + reason.split("\n", 1)[1].replace("Use:", "Or use:", 1)),
        }
    return decision


def _requested_range(tool_input: dict[str, Any] | None) -> tuple[int, int | None] | None:
    """``(start, end)`` of a StartLine/EndLine read, 1-based; ``start`` is 1
    when absent and ``end`` None when absent. None when the request is not a
    valid range (a start below 1, an end before the start, a non-integer)."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    a, b = ti.get("StartLine"), ti.get("EndLine")
    if a is None:
        a = 1
    if not isinstance(a, int) or isinstance(a, bool) or a < 1:
        return None
    if b is None:
        return a, None
    if not isinstance(b, int) or isinstance(b, bool) or b < a:
        return None
    return a, b


def _bound_fields(tool_input: dict[str, Any] | None, lines: int) -> dict[str, int]:
    """The input fields that bound a read to ``lines`` lines, in the tool's own
    terms. For a StartLine/EndLine read the bound stays inside the request:
    the same start, an end no later than the one asked for; an invalid
    request gets no fields (nothing to narrow)."""
    if _range_style(tool_input):
        asked = _requested_range(tool_input)
        if asked is None:
            return {}
        start, end = asked
        last = start + lines - 1
        return {"StartLine": start, "EndLine": last if end is None else min(last, end)}
    return {"limit": lines}


def _media_read(
    path_str: str,
    workspace_root: str | None,
    policy: dict[str, Any],
    tool_input: dict[str, Any] | None,
    path_key: str | None,
    viewer: str = "Read",
) -> dict[str, Any] | None:
    """The read guard's decision for an image, video or audio file, or None.

    Defaults (``[media]`` in ctx.toml): a large displayable image is allowed
    unchanged with a note (``image_read = "advise"``) or, opted in, redirected
    to a gridded overview (``"redirect"``); video and audio are denied with the
    exact ``ctx view`` command (``video_read = "steer"``). Only for the hosts
    in ``mediaread.HOSTS``; everything else keeps its behavior. Fail-open by
    contract: any error returns None and the read proceeds down the ordinary
    path, recorded in the coverage ledger as ``media:error``.
    """
    flavor = str(policy.get("_flavor") or "")
    if flavor not in _MEDIA_HOSTS or not workspace_root:
        return None
    try:
        return _media_read_inner(path_str, workspace_root, policy, tool_input,
                                 path_key, viewer, flavor)
    except Exception:
        policy["_media_note"] = "media:error"
        return None


_HINTS_FILE = "media-hints.json"
_HINTS_MAX = 512
_HINT_SESSIONS = 16


def _hint_state(ws_root: str, session: str, key: str, mark: str | None = None) -> str:
    """Per-session state of one file's video hint: "" (never given),
    "pending" (put in an ask reason, which the model may never see) or
    "delivered". With ``mark``, records it. Fail-open: "" on any error, so a
    broken ledger can only repeat a hint, never suppress one wrongly."""
    try:
        path = session_reads_dir(ws_root, _HINTS_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}
        sid = _safe_id(session, 64)
        rows = doc.get(sid) if isinstance(doc.get(sid), dict) else {}
        if mark is None:
            return str(rows.get(key, ""))
        rows[key] = mark
        # Every recent session keeps its own rows (interleaved sessions must not
        # erase each other's "delivered"); bounded per session and in sessions.
        doc.pop(sid, None)
        doc[sid] = dict(list(rows.items())[-_HINTS_MAX:])
        doc = dict(list(doc.items())[-_HINT_SESSIONS:])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
        return mark
    except Exception:
        return ""


def _media_shell_hint(command: str, cwd: str | None, workspace_root: str | None,
                      policy: dict[str, Any]) -> tuple[str, str] | None:
    """``(hint, key)`` naming `ctx view` for the first video or audio file a
    video tool (ffmpeg/ffprobe/ffplay, as the PROGRAM) is given, or None.

    Limits (stated, not solved): a video that exists and is only the OUTPUT
    (``ffmpeg -i in.png v.mp4``) also gets the hint; ``sudo -u USER ffmpeg``
    and commands inside ``if``/``for``/``while`` bodies are not recognised.

    Same gates as a media read (host, ``video_read = "steer"``, inside the
    workspace, not secret, not .ctxignore'd, not a symlink, a header that
    sniffs as a container ``ctx view`` reads), plus: ``ctx view`` can
    actually produce something here (the engines it needs are installed)."""
    flavor = str(policy.get("_flavor") or "")
    if (flavor not in _MEDIA_HOSTS or not workspace_root
            or str(policy.get("media_video_read", "steer")) != "steer"):
        return None
    from ctx import mediaread as mr

    for path in mr.video_tool_args(command, cwd or workspace_root)[:16]:
        if _is_secret_path(path, workspace_root) or _path_outside(path, workspace_root):
            continue
        root = mr.ctx_root(workspace_root)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel.startswith("..") or mr.policy_excluded(root, rel):
            continue
        got = mr.read_head(path, 4096)
        if got is None:
            continue
        from ctx.binfmt import sniff_format

        fmt = sniff_format(got[0][:1024])
        kind = ("video" if fmt in ("mp4", "mov", "mkv", "webm", "avi")
                else "audio" if fmt in ("mp3", "wav", "flac", "ogg", "m4a") else None)
        if kind is None or mr.missing_engines(kind):
            continue
        target, _exact = mr.command_target(rel)
        import hashlib

        return mr.shell_hint(target, kind), hashlib.sha256(rel.encode()).hexdigest()[:16]
    return None


def _media_post_hint(payload: dict[str, Any], flavor: str) -> str | None:
    """PostToolUse half of the video hint: a call whose PreToolUse put the
    hint only in an ask reason (shown to the user, not the model) ran, so the
    model gets it now, as context, once."""
    if flavor not in _MEDIA_HOSTS:
        return None
    command = _tool_command(payload)
    if not command or not ("ffmpeg" in command or "ffprobe" in command or "ffplay" in command):
        return None
    ws_root = _resolve_workspace_root(payload)
    if not ws_root:
        return None
    policy = _load_guard_policy(ws_root)
    policy["_flavor"] = flavor
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    cwd = (ti.get("cwd") or ti.get("Cwd")) if isinstance(ti, dict) else None
    found = _media_shell_hint(command, cwd or ws_root, ws_root, policy)
    if not found:
        return None
    hint, key = found
    session = str(payload.get("session_id") or payload.get("conversation_id") or "unknown")
    if _hint_state(ws_root, session, key) != "pending":
        return None
    _hint_state(ws_root, session, key, "delivered")
    return hint


def _media_decision(base: dict[str, Any], note: str, policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("_media_withheld"):
        note += ":name_withheld"
    policy["_media_note"] = note
    return base


def _media_read_inner(path_str, workspace_root, policy, tool_input, path_key, viewer, flavor):
    from ctx import mediaread as mr

    klass = mr.suffix_class(path_str)
    image_mode = str(policy.get("media_image_read", mr.DEFAULT_IMAGE_MODE))
    video_mode = str(policy.get("media_video_read", mr.DEFAULT_VIDEO_MODE))
    if klass in ("image", "still") and image_mode not in ("advise", "redirect"):
        return None
    if klass in ("video", "audio") and video_mode != "steer":
        return None
    if _LEDGER_DIR_NAME in path_str.replace("\\", "/").split("/"):
        return None  # our own derivatives (overviews, crops, sheets)
    root = mr.ctx_root(workspace_root)
    try:
        rel = os.path.relpath(path_str, root).replace(os.sep, "/")
    except ValueError:
        return None
    if rel.startswith("../") or rel == "..":
        return None
    target, exact = mr.command_target(rel)
    if not exact:
        # Any name is handled; one that cannot be shown safely is not
        # printed (the command names PATH and says what PATH stands for).
        policy["_media_withheld"] = True
    if mr.policy_excluded(root, rel):
        policy["_media_note"] = "media:excluded"
        return None
    got = mr.read_head(path_str)
    if got is None:
        return None  # missing, a symlink, not a regular file: the ordinary path decides
    head, size = got
    allow: dict[str, Any] = dict(DECISION_ALLOW)

    if klass in ("video", "audio"):
        from ctx.binfmt import sniff_format  # rare path; agrees with ctx view by construction

        fmt = sniff_format(head[:1024])
        kind = {"mp4": "video", "mov": "video", "mkv": "video", "webm": "video",
                "avi": "video", "mp3": "audio", "wav": "audio", "flac": "audio",
                "ogg": "audio", "m4a": "audio"}.get(fmt)
        if kind is None:
            return None  # the bytes are not a container ctx view would read
        if fmt in ("mp4", "mov") and klass == "audio":
            kind = "audio"
        return _media_decision(_deny(mr.steer_reason(target, fmt, kind, viewer=viewer)),
                               f"media:steer_{kind}", policy)

    if klass == "still":
        from ctx.binfmt import sniff_format

        fmt = sniff_format(head[:1024])
        if fmt not in ("bmp", "tiff", "ico") or not mr.has_pillow():
            return None
        allow["context"] = mr.convert_note(target, fmt, mr.dims(fmt, head), viewer=viewer)
        return _media_decision(allow, "media:advise_convert", policy)

    fmt = mr.sniff_still(head)
    if fmt is None:
        return None  # not actually an image: the text path decides
    wh = mr.dims(fmt, head)
    if wh is None:
        return _media_decision(allow, "media:unknown_size", policy)
    w, h = wh
    if not mr.host_downscales(w, h):
        # Small enough that the host sends every pixel: neither an overview nor
        # a crop would show more, so nothing is added.
        return _media_decision(allow, "media:small", policy)
    pillow = mr.has_pillow()
    note = "media:advise"
    if image_mode == "redirect":
        why = ""
        if not pillow:
            why = "no_pillow"
        elif not _steering_allows(policy):
            why = "steering_deny"
        elif not path_key or not isinstance(tool_input, dict):
            why = "no_path_field"
        elif size > mr.REDIRECT_MAX_BYTES or w * h > mr.REDIRECT_MAX_PIXELS:
            why = "too_large"
        elif flavor == "claude-code" and mr.read_rules_present(root):
            # The user has Read deny/ask rules: a redirect must not route a
            # read around them (see _to_claude_code_schema: no approval is
            # given, and the host checks rules against the NEW path).
            why = "read_rules"
        if not why:
            facts, why = mr.make_overview(
                root, rel, int(policy.get("media_redirect_budget_ms",
                                          mr.DEFAULT_REDIRECT_BUDGET_MS)))
            if facts is not None:
                d = facts["derived"]
                meta = d.get("meta") or {}
                rows, cols = int(meta.get("rows") or 0), int(meta.get("cols") or 0)
                if rows and cols:
                    out = dict(DECISION_ALLOW)
                    out["_no_approve"] = True
                    out["_rewrite"] = {
                        "fields": {path_key: os.path.join(root, d["path"])},
                        "reason": "CTX_MEDIA: read redirected to a reduced overview "
                                  "([media] image_read = \"redirect\")",
                    }
                    out["context"] = mr.redirect_note(
                        target, w, h, int(d["width"]), int(d["height"]), rows, cols,
                        host=flavor, viewer=viewer)
                    return _media_decision(out, "media:redirect", policy)
                why = "no_grid"
        note = f"media:advise_fallback_{why}"
    allow["context"] = mr.advise_note(target, fmt, w, h, host=flavor, pillow=pillow,
                                      viewer=viewer)
    return _media_decision(allow, note, policy)


def classify_read(
    path_str: str,
    workspace_root: str | None,
    policy: dict[str, Any],
    session_id: str = "unknown",
    tool_input: dict[str, Any] | None = None,
    path_key: str | None = None,
) -> dict[str, str]:
    if workspace_root and not os.path.isabs(path_str):
        path_str = os.path.join(workspace_root, path_str)  # fix: resolve relative paths against workspace_root, not process CWD
    if _is_secret_path(path_str, workspace_root):
        return _force_ask(
            "CTX_CONTEXT_GUARD: secret-bearing path. Reading it requires an explicit "
            "user-visible permission step; it is excluded from automatic capture."
        )
    if _path_outside(path_str, workspace_root):
        return _force_ask(
            "CTX_CONTEXT_GUARD: path resolves outside the active workspace. "
            "Confirm outside-root access explicitly or pass --workspace."
        )
    # Media reads (ctx.mediaread). Only past the secret and workspace checks
    # above, so a media decision can never reach a file the guard refuses;
    # decided from the suffix alone before any system call, so a source-file
    # read pays one string lookup. None means "not a media decision": the
    # read continues down the ordinary path exactly as before.
    if _maybe_media(path_str):
        media = _media_read(path_str, workspace_root, policy, tool_input, path_key)
        if media is not None:
            return media
    try:
        size = os.stat(path_str).st_size
    except OSError:
        return dict(DECISION_ALLOW)  # nonexistent/new file: let the tool error
    # The session ledger itself is bookkeeping, never evidence: reads of it
    # are neither counted nor pressured.
    in_ledger = _LEDGER_DIR_NAME in path_str.replace("\\", "/").split("/")
    limit = int(policy.get("max_inline_bytes", _MAX_INLINE_BYTES_DEFAULT))
    note = str(policy.get("_window_note", ""))  # window pressure, "" when idle
    max_lines = int(policy.get("max_inline_lines", _MAX_INLINE_LINES_DEFAULT))
    budget = int(
        policy.get("session_read_budget_bytes", _SESSION_READ_BUDGET_DEFAULT)
    )
    # Peek the ledger before branching. The large-file branch below returns
    # without ever consulting it, so without this the biggest reads in a
    # session are the only ones that never feel session pressure. Charging
    # zero is a read of the running total; it stays fail-open (0 on any error,
    # which _pressured_window reads as "no pressure").
    seen = 0 if in_ledger else _ledger_charge(workspace_root, session_id, 0)
    span = _range_span(tool_input)
    window = _pressured_window(max_lines, seen, budget)
    if span is not None and span <= window:
        # A StartLine/EndLine read is bounded by its own range -- in lines.
        # That host has no output gate, so the bytes are checked here too.
        nbytes: int | None = size
        if size > limit:
            start0 = max(int(tool_input["StartLine"]) - 1, 0)
            _fit, nbytes = _range_scan(path_str, start0,
                                       int(tool_input["EndLine"]) - start0 + 1, limit)
        if nbytes is not None:
            if not in_ledger:
                _ledger_charge(workspace_root, session_id, nbytes)
            return dict(DECISION_ALLOW)
        return _range_advice(tool_input, path_str, workspace_root, size, limit, window, note)
    if size > limit and _range_style(tool_input) and _steering_allows(policy):
        if not in_ledger and _whole_file_read(tool_input):
            outline = _skeleton_outline_for(path_str, workspace_root, size,
                                            "view_file with StartLine and EndLine")
            if outline:
                return _deny(outline)
        # The requested start is kept (1 for a whole-file read): serving a
        # different region than asked would be silent, since the host shows
        # the model only "Showing lines A-B of N", never the hook's reason.
        return _range_advice(dict(tool_input or {}), path_str, workspace_root,
                             size, limit, window, note)
    if size > limit:
        if not in_ledger and _whole_file_read(tool_input):
            outline = _skeleton_outline_for(
                path_str, workspace_root, size,
                *(("view_file with StartLine and EndLine",) if _range_style(tool_input) else ()))
            if outline:
                # A deny whose reason IS the answer: the outline reaches the
                # model as the tool's text on every host, and no rewrite is
                # attached so the first-page fallback never overrides it.
                return _deny(outline)
        price = _price_note(size, workspace_root)
        decision: dict[str, Any] = _deny(
            f"CTX_CONTEXT_GUARD: file is {size} bytes{price} (> {limit} inline budget).\n"
            + _outline_hint(path_str)
            + "Use: ctx get repo:<relative-path> --lines A:B\n"
            "or:  ctx search repo:<relative-path> '<pattern>' --context 3" + note
        )
        if _steering_allows(policy):
            max_lines = _pressured_window(max_lines, seen, budget)
            decision["_rewrite"] = {
                "fields": _bound_fields(tool_input, max_lines),
                "size": size if _window_note_applies(path_str, tool_input, policy,
                                                     in_ledger) else None,
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
    if total > budget:
        reason = _read_budget_reason(total) + note
        if _steering_allows(policy):
            pressured: dict[str, Any] = dict(DECISION_ALLOW)
            pressured["_rewrite"] = {
                "fields": _bound_fields(tool_input, _pressured_window(max_lines, total, budget)),
                "size": size if _window_note_applies(path_str, tool_input, policy,
                                                     in_ledger) else None,
                "reason": reason,
            }
            return pressured
        return _deny(reason)
    return dict(DECISION_ALLOW)


# The workspace root for the decision currently being built. classify() is the
# only writer and runs once per hook invocation, so this is a hand-off between
# two functions in one call, not shared mutable state across calls.
_APPLY_ROOT: dict[str, Any] = {}


#: Rewrite fields that are upper bounds on a result (Read `limit`, Grep
#: `head_limit`). The caller's own smaller value is kept.
_CAP_FIELDS = frozenset({"limit", "head_limit"})


#: Suffixes the host does not read as lines of text (images, video, audio,
#: documents, notebooks, archives and other binaries): a line-window note
#: about them would be false, so none is attached.
_NOT_LINE_TEXT = _MEDIA_SUFFIXES | frozenset({
    "heic", "heif", "avif", "pdf", "ipynb", "zip", "gz", "tgz", "bz2", "xz", "zst", "7z",
    "tar", "jar", "whl", "exe", "dll", "so", "dylib", "bin", "class", "o", "a", "wasm",
    "sqlite", "db", "pyc"})


def _window_note_applies(path_str: str, tool_input: dict[str, Any] | None,
                         policy: dict[str, Any], in_ledger: bool) -> bool:
    """Only for what the hook knows is a plain text read on a host whose read
    tool and parameters are established: Claude Code's ``Read`` with
    ``offset``/``limit`` (observed live, 2.1.322). Never for ctx's own
    session files, a ``pages`` read, or a suffix the host does not read as
    text lines. Decided from the name and the input alone: no extra read."""
    if policy.get("_flavor") != "claude-code" or policy.get("_read_tool") != "Read":
        return False
    if in_ledger or not isinstance(tool_input, dict) or "pages" in tool_input:
        return False
    dot = path_str.rfind(".")
    suffix = path_str[dot + 1:].lower() if dot >= 0 and "/" not in path_str[dot:] else ""
    return suffix not in _NOT_LINE_TEXT


def _window_note(updated: dict[str, Any], size: Any) -> str | None:
    """What the guard asked the host for, for the model: one line built only
    from integers the guard already holds (the stat size, the offset and
    limit it is about to send), never from the file's name or bytes, so
    neither can shape it, and no extra read is made to produce it. Whether the
    file continues past the window is not known without reading it, so the
    note says how to tell and how to continue, and claims nothing more."""
    limit = updated.get("limit")
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            or not isinstance(size, int) or isinstance(size, bool) or size < 0):
        return None
    off = updated.get("offset")
    start = off if isinstance(off, int) and not isinstance(off, bool) and off > 0 else 1
    end = start + limit - 1
    return (f"CTX_CONTEXT_GUARD: the guard limited this Read to at most {limit} lines "
            f"starting at line {start} (the file is {size:,} bytes). If the result ends "
            f"at line {end}, the file may continue: Read it again with offset={end + 1} "
            f"limit={limit}, or search it for what you need.")


def _apply_rewrite(
    decision: dict[str, Any],
    tool_input: dict[str, Any],
    command_key: str | None = None,
) -> dict[str, Any]:
    """Convert the layer-1 ``_rewrite`` hint into the public ``rewrite``
    field, preserving the original tool_input key names and every unrelated
    field (description, timeout, …) untouched in ``updatedInput``."""
    _decay_taught_reason(decision, _APPLY_ROOT.get("root"))
    decision.pop("_safety", None)
    hint = decision.pop("_rewrite", None)
    if not hint:
        return decision
    updated = dict(tool_input)
    if "command" in hint:
        if not command_key:
            return decision  # no command field to substitute: keep plain decision
        updated[command_key] = hint["command"]
    else:
        for key, cap in hint.get("fields", {}).items():
            own = tool_input.get(key)
            if (
                key in _CAP_FIELDS
                and isinstance(own, int) and not isinstance(own, bool)
                and 0 < own <= cap
            ):
                # A cap narrows a read; it never widens one. Measured on
                # DeepSWE (cattrs, haiku): the model asked `offset=729
                # limit=10` on a 60 KB file and the large-file rewrite handed
                # it 239 lines — every targeted read became a 10 KB page,
                # 106 KB of Read results against naive's 74 KB, from the
                # guard that exists to bound them.
                continue
            updated[key] = cap
    decision["rewrite"] = {"updatedInput": updated, "reason": hint["reason"]}
    if updated.get("limit") != tool_input.get("limit") and not decision.get("context"):
        # A read the guard just narrowed must SAY so to the model. The reason
        # above travels as permissionDecisionReason, which Claude Code shows
        # the user but not the model on an allow: measured live (2.1.322,
        # haiku, an @-mentioned 58 KB log routed through Read), the model got
        # the first 240 lines with no sign of more and answered "No ERROR line
        # exists" about a file whose line 902 was the ERROR.
        note = _window_note(updated, hint.get("size"))
        if note:
            decision["context"] = note
    if "fields" in hint:
        # Hosts that cannot substitute input name these in their deny instead.
        decision["rewrite"]["fields"] = dict(hint["fields"])
    if "command" in hint:
        # Carried for hosts that cannot substitute input and have to name the
        # contained command in a deny reason instead (see _to_antigravity_schema).
        decision["rewrite"]["command"] = hint["command"]
    return decision


def classify(
    payload: dict[str, Any], policy: dict[str, Any] | None = None
) -> dict[str, str]:
    """Classify one intercepted tool call.

    ``policy`` may be supplied by a caller that has already loaded it (the
    hook entry point does, so ``_load_guard_policy`` runs once per hook call
    instead of twice); omitted, it is loaded here exactly as before. The
    decision is a pure function of (payload, policy) either way.
    """
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    workspace_root = _resolve_workspace_root(payload)
    if policy is None:
        policy = _load_guard_policy(workspace_root)
    # The policy dict already carries computed extras (_window_note); the root
    # rides along so remediation can tell a first lesson from a repeat. Set on
    # every path, not just the load path — a caller-supplied policy needs it too.
    policy["_ws_root"] = workspace_root
    _APPLY_ROOT["root"] = workspace_root

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

    kind = _tool_kind(tool_name, policy.get("_flavor"), payload)

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

                # Both probes do I/O (a ledger scan and a repo scan) but are
                # consulted by only two of the recogniser's branches, so they
                # are handed over as thunks and evaluated only if the branch
                # that needs them is reached. Same answers, paid far less often.
                sub = substitute.collapse(
                    command,
                    failure_available=lambda: _failure_available(workspace_root),
                    symbols_resolvable=lambda: _symbols_resolvable(workspace_root))
                if sub is not None and not (
                    decision.get("decision") == "force_ask"
                    and decision.get("reason", "").startswith(
                        "CTX_CONTEXT_GUARD: secret-bearing path"
                    )
                ):  # fix: never let collapse override a secret-path force_ask
                    # Deliberately overrides a DENY as well as an allow, and
                    # that is the product: the flagship case (`grep -rn X .`)
                    # is denied by the canonical layer as unbounded, and the
                    # whole point of the replacement surface is to hand back a
                    # bounded op the agent can actually run instead of a
                    # refusal. Substituting only on allow was tried and is
                    # wrong — it would silently disable the surface on exactly
                    # the commands it exists for.
                    #
                    # What makes the override safe is not the decision it
                    # replaces but the command it installs: every Substitution
                    # emits a bounded `ctx` op, so a denied flood is replaced
                    # by something that cannot flood. That invariant is the
                    # load-bearing one, and it is pinned by test
                    # `test_every_substitution_installs_a_bounded_ctx_op`
                    # rather than left to each recogniser's good manners.
                    decision = dict(DECISION_ALLOW)
                    decision["_rewrite"] = {"command": sub.command, "reason": sub.reason}
                    _note_collapse(workspace_root, sub.shape, sub.rung)
            except Exception:
                pass
        # A recursive search for an identifier runs as itself (captured whole,
        # so the answer is the search's own); a symbol lookup is offered only
        # beside it, as the different question it is, never in its place.
        if isinstance(decision.get("_rewrite"), dict) and "command" in decision["_rewrite"]:
            try:
                from ctx.substitute import symbol_hint

                hint = symbol_hint(command)
                if hint:
                    decision["_rewrite"]["reason"] += "\n" + hint
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
        # Graduated steering — the null plan. Every would-be command rewrite
        # remains shadow-recorded. The one live arm is intentionally narrow:
        # one explicit pytest node, a passive session, no prior flood for its
        # signature, rewrite steering enabled, and a dialect with fail-closed
        # PostToolUse output substitution. This is the measured small-task
        # case where ctx's capture wrapper cost more than naive execution.
        # An unexpected flood is still persisted and replaced by
        # `_emission_gate`, which then records an intervention so this
        # signature does not speculate again in the session.
        if isinstance(decision.get("_rewrite"), dict) and "command" in decision["_rewrite"]:
            try:
                from ctx import reflex

                reflex.note_steer_shadow(workspace_root, command)
                if (
                    bool(policy.get("speculative_native", True))
                    and bool(policy.get("_output_substitution"))
                    and _steering_allows(policy)
                    and not decision.get("_safety")
                    and _single_named_pytest(command)
                    and reflex.steering_would_bypass(workspace_root, command)
                ):
                    reflex.note_steer_decision(
                        workspace_root,
                        command,
                        action="native",
                        reason="single_named_test",
                    )
                    decision = dict(DECISION_ALLOW)
            except Exception:
                pass
        if "ffmpeg" in command or "ffprobe" in command or "ffplay" in command:
            # Claude Code's Read refuses a video before any hook runs
            # (observed, 2.1.322), so the model improvises with ffmpeg in the
            # shell; this is the first tool call a hook sees. Once per file
            # per session, on a path the model sees whatever the outcome: a
            # deny's reason; an allowed or rewritten call's context; and for
            # an ask (whose reason the USER sees) the reason now and, if the
            # call then runs, PostToolUse context (_media_post_hint). Fail-open.
            try:
                found = _media_shell_hint(command, cwd or workspace_root, workspace_root, policy)
                session = str(payload.get("session_id") or payload.get("conversation_id")
                              or "unknown")
                if found and not _hint_state(workspace_root, session, found[1]):
                    hint, key = found
                    raw_decision = decision.get("decision", "allow")
                    if "_rewrite" in decision or raw_decision == "allow":
                        decision["context"] = hint
                        _hint_state(workspace_root, session, key, "delivered")
                    elif raw_decision == "deny":
                        decision["reason"] = str(decision.get("reason", "")) + "\n" + hint
                        _hint_state(workspace_root, session, key, "delivered")
                    else:  # ask: the user reads this; the model gets it post-run
                        decision["reason"] = str(decision.get("reason", "")) + "\n" + hint
                        _hint_state(workspace_root, session, key, "pending")
                        # Where the ask is not emitted (Claude Code), the
                        # hint goes to the model as context instead.
                        decision["_media_hint"] = [hint, key, session]
                    policy["_media_note"] = "media:shell_hint"
            except Exception:
                pass
        return _apply_rewrite(decision, tool_input, command_key)

    if kind == "read":
        session_id = str(
            payload.get("session_id") or payload.get("conversation_id")
            or payload.get("conversationId") or "unknown"
        )
        for key in ("AbsolutePath", "TargetFile", "file_path", "filePath", "path", "Path"):
            v = tool_input.get(key)
            if isinstance(v, str) and v:
                # After the secret-path check, never instead of it.
                if (policy.get("_flavor") == "antigravity"
                        and not _is_secret_path(v, workspace_root)
                        and _path_outside(v, workspace_root)
                        and _agy_mcp_schema_read(payload, v)):
                    return dict(DECISION_ALLOW)
                policy["_read_tool"] = tool_name
                return _apply_rewrite(
                    classify_read(v, workspace_root, policy, session_id, tool_input=tool_input,
                                  path_key=key),
                    tool_input,
                )
        return dict(DECISION_ALLOW)

    if kind is None and policy.get("_flavor") == "codex" and tool_name == "view_image":
        # Codex's image viewer (tool argument `path`, as codex-cli 0.167.1's
        # tool schema declares it). Its hooks reference documents PreToolUse
        # deny, additionalContext and an `updatedInput` replacement-arguments
        # object for local function tools; none of this is live-verified.
        # The read guard's own checks come first: a secret or outside-workspace
        # path is left to the host untouched (this tool is otherwise exempt),
        # so a media decision is only ever made for a call that was allowed.
        v = tool_input.get("path")
        if isinstance(v, str) and v:
            path_str = v if os.path.isabs(v) or not workspace_root else os.path.join(workspace_root, v)
            if (_maybe_media(path_str)
                    and not _is_secret_path(path_str, workspace_root)
                    and not _path_outside(path_str, workspace_root)):
                media = _media_read(path_str, workspace_root, policy, tool_input, "path",
                                    viewer="view_image")
                if media is not None:
                    return _apply_rewrite(media, tool_input)
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
    """Remediation for a native Grep/Glob call denied under ``guard.collapse``.

    It names a command that gives the SAME answer -- the same search, run in
    the shell, where ctx captures the complete result bounded and with an
    address. It used to name ``ctx q 'refs X | group file'`` for an
    identifier and claim a shell grep was "auto-collapsed to the same op";
    refs is a code-symbol lookup that never reads prose or config, so that
    told the agent a different question was the same one. A symbol lookup is
    still mentioned for an identifier, labelled as the different question it is.
    """
    pat = tool_input.get("pattern") or tool_input.get("query") or tool_input.get("Query") or ""
    pat = pat if isinstance(pat, str) else ""
    head = ("CTX_CONTEXT_GUARD: native search is off under the replacement "
            "surface (guard.collapse). ")
    if "grep" not in tool_name.lower():  # Glob / file-name search
        glob = tool_input.get("pattern") or tool_input.get("Pattern") or "<glob>"
        return (head + "List the files with the shell instead -- "
                f"`rg --files -g {shlex.quote(str(glob))}` or "
                f"`find . -name {shlex.quote(str(glob))}`; ctx captures the full "
                "listing, bounded and addressable.")
    shown = shlex.quote(pat) if pat else "'<pattern>'"
    text = (head + f"Run the same search in the shell instead -- `rg -n {shown} <path>` "
            f"or `grep -rn {shown} <path>`; ctx captures the complete result, "
            "bounded and addressable.")
    if re.match(r"^[A-Za-z_]\w*$", pat):
        text += (f" (`ctx q 'refs {pat} | group file'` answers a different question: "
                 "where a code symbol is defined or used. It does not read prose, "
                 "config or comments.)")
    return text


def _classify_native_search(
    tool_name: str, tool_input: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    lowered = tool_name.lower()
    # Replacement surface: with collapse on, the host's native search tool is
    # removed from the surface -- deny and redirect to the same search in the
    # shell, which the guard captures whole (never to a ctx query standing in
    # for it). One code path for every harness whose hook sees a native search.
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


def _rewrite_stays_in_workspace(decision, payload, ws_root):
    """A rewritten command runs in the caller's directory (``ctx run`` takes
    its working directory from the shell that runs it). From a directory
    outside the workspace there is nowhere inside it to run: the call is left
    untouched for the host, and the output gate bounds what it prints."""
    try:
        rw = decision.get("_rewrite") if isinstance(decision, dict) else None
        if not (isinstance(rw, dict) and "command" in rw) or not ws_root:
            return decision
        cwd = payload.get("cwd") or (payload.get("tool_input") or {}).get("cwd")
        if not cwd:
            return decision
        here = os.path.realpath(cwd)
        top = os.path.realpath(ws_root)
        if here == top or here.startswith(top.rstrip(os.sep) + os.sep):
            return decision
        return dict(DECISION_ALLOW)
    except Exception:
        return decision


def _claude_permission_gate(
    decision: dict[str, Any] | None, payload: dict[str, Any], ws_root: str | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The hook never claims the user's rules permit anything (Claude Code).

    Claude Code's own rule engine resolves paths in shell commands, follows
    links and applies settings ctx cannot read; any ``allow`` sent with a
    rewritten input replaces that engine with ctx's, and every gap in ctx's
    is a bypass of the user's settings (review of 9d02cb4, live on 2.1.322).
    So:

    * R1: never ``allow`` together with a rewritten input.
    * R2 (``steering = "auto"``, the default): a shell command is not
      rewritten at all. It leaves exactly as it arrived, no decision, and
      the host decides as it would without ctx; the PostToolUse gate bounds
      the output afterwards (from the host's own full copy where it saved
      one, see :func:`_host_full_output`).
    * R3 (``steering = "rewrite"``, opt-in): rewritten with no decision; the
      host judges the ``ctx run`` wrapper. ctx.ccperm is used only to refuse:
      a visible rule that would deny or ask about the original gives deny or
      ask, and no rewrite.
    * R4: a volume ask or deny on a shell command (unknown output bound, a
      compound ctx cannot vouch for) is not emitted where output can be
      replaced; safety asks and denies stay (:func:`_is_safety_ask`).
    * a plain allow is emitted only for ctx's own verbs
      (:func:`_ctx_self_approved`); a field rewrite (Read ``limit``) and a
      media redirect carry no decision.
    """
    if not isinstance(decision, dict):
        return decision
    steering = str((policy or {}).get("steering", "auto"))
    rewrite = decision.get("rewrite")
    command = _tool_command(payload)
    if not isinstance(rewrite, dict) or not isinstance(rewrite.get("updatedInput"), dict):
        if decision.get("decision") == "allow":
            out = dict(decision)
            out["_permission"] = "allow" if _ctx_self_approved(payload, ws_root) else "none"
            return out
        if command and _is_volume_refusal(decision, command) and (
                steering != "deny" or decision.get("decision") == "force_ask"):
            return _untouched(decision, ws_root)
        return decision
    if "command" not in rewrite:
        out = dict(decision)
        out["_permission"] = "none"
        return out
    if steering != "rewrite":
        return _untouched(decision, ws_root)  # R2
    try:
        from ctx import ccperm

        ti = payload.get("tool_input") or payload.get("toolInput") or {}
        verdict, _why = ccperm.refusal_verdict(
            command or "", {**payload, "tool_input": ti if isinstance(ti, dict) else {}}, ws_root)
    except Exception:
        verdict = "ask"  # cannot evaluate: do not hand the host a wrapper
    if verdict == "deny":
        from ctx.ccperm import DENY_REASON

        return _deny(DENY_REASON)
    if verdict == "ask":
        from ctx.ccperm import ASK_REASON

        return _force_ask(ASK_REASON)
    out = dict(decision)
    out["_permission"] = "none"  # R1: a rewrite never carries an approval
    return out


def _is_volume_refusal(decision: dict[str, Any], command: str) -> bool:
    """An ask or deny on a shell command that exists only because its output
    might be large (unknown output bound, unbounded-output remediation, a
    compound ctx cannot vouch for): pointless where the output gate can
    replace output. Safety asks and denies, and the steer of a never-ending
    command to ``ctx run --bg``, are not volume."""
    if decision.get("decision") not in ("force_ask", "deny") or _is_safety_ask(decision):
        return False
    reason = str(decision.get("reason") or "")
    if reason.startswith("CTX_CONTEXT_GUARD: in-place text edit"):
        return False
    try:
        argv = _unwrap(shlex.split(command))
    except ValueError:
        argv = []
    if argv and _follows_forever(argv):
        return False
    return True


def _codex_gate(decision, payload, ws_root, policy):
    """Codex, the same shape as R2: rewrites are withheld (the emitter drops
    them) and a volume refusal of a shell command is not sent, because Codex
    maps an ask to a deny and its PostToolUse block replaces a large output
    with ctx's digest after the command runs."""
    if not isinstance(decision, dict):
        return decision
    steering = str((policy or {}).get("steering", "auto"))
    command = _tool_command(payload)
    rewrite = decision.get("rewrite")
    if isinstance(rewrite, dict) and "command" in rewrite:
        return _untouched(decision, ws_root)
    if command and _is_volume_refusal(decision, command) and (
                steering != "deny" or decision.get("decision") == "force_ask"):
        return _untouched(decision, ws_root)
    return decision


def _is_safety_ask(decision: dict[str, Any]) -> bool:
    """An ask that is a safety boundary, not a volume one: kept."""
    reason = str(decision.get("reason") or "")
    return bool(decision.get("_safety")) or reason.startswith((
        "CTX_CONTEXT_GUARD: secret-bearing path", "CTX_CONTEXT_GUARD: container escape",
        "CTX_RELAY_INTERRUPT", "CTX_CONTEXT_GUARD: path outside workspace",
        "CTX_CONTEXT_GUARD: one of your Claude Code"))


def _untouched(decision: dict[str, Any], ws_root: str | None = None) -> dict[str, Any]:
    """No rewrite, no decision; advice for the model (``context``) kept. A
    media hint that rode an ask (which is not emitted) becomes context."""
    out: dict[str, Any] = {"decision": "allow", "_permission": "none", "_untouched": "1"}
    if decision.get("context"):
        out["context"] = decision["context"]
    pending = decision.get("_media_hint")
    if isinstance(pending, list) and len(pending) == 3 and not out.get("context"):
        hint, key, session = pending
        out["context"] = hint
        try:
            _hint_state(ws_root, session, key, "delivered")
        except Exception:
            pass
    return out


#: ctx's own verbs that only read. A product decision, written down (R5):
#: on Claude Code the hook approves, with no rewrite, a call of one of these
#: that reads only ctx's store by handle (``ctx get run:...``, ``ctx search
#: run:... PATTERN``, ``ctx stats run:...``, ``ctx diff run:A run:B``): that
#: content already came in through a call the host approved. One that reads
#: workspace paths (a ``repo:`` ref or a file path, a workspace search,
#: ``map``, ``def``, ``refs``, ``q``) only when no Read deny rule, no Read ask
#: rule and no ``blockReadsOutsideWorkingDirectories`` is visible in any
#: settings source, every source ctx knows how to find was readable, and no
#: argument leaves the workspace (absolute, ``..``, or a link out).
#: LIMIT: a Read rule in a source ctx cannot read (server-managed settings,
#: the macOS plist, the Windows registry, a policy helper) does not bind
#: these verbs; an administrator who relies on one should also deny
#: ``Bash(ctx:*)`` and ``mcp__ctx__*`` there.
CTX_SELF_APPROVED_VERBS = frozenset({"get", "search", "stats", "q", "diff", "map", "def", "refs"})
_STORE_HANDLE_RE = re.compile(r"(?:run|job|span|digest|blob|snapshot):[0-9a-f]{4,64}(?:[#@:][\w.:#@-]*)?")
_STORE_ONLY_VERBS = frozenset({"get", "stats", "diff", "search"})


def _store_only(verb: str, args: list[str]) -> bool:
    """Does this ctx verb read only from the store, by handle?"""
    pos = [a for a in args if not a.startswith("-")]
    if verb not in _STORE_ONLY_VERBS or not pos or any(a.startswith("-") for a in args):
        return False
    if verb == "search":
        return bool(_STORE_HANDLE_RE.fullmatch(pos[0]))
    return all(_STORE_HANDLE_RE.fullmatch(a) for a in pos)


def _leaves_workspace(args: list[str], ws_root: str | None, cwd: str | None) -> bool:
    base = cwd or ws_root
    if not base:
        return True
    top = os.path.realpath(ws_root or base)
    for a in args:
        if a.startswith("-"):
            continue
        if a.startswith("ws:"):
            return True  # another workspace
        path = a.split(":", 1)[1] if a.startswith("repo:") else a
        if not path or ":" in path.split("/", 1)[0]:
            continue
        if path.startswith(("/", "~")) or ".." in path.replace("\\", "/").split("/"):
            return True
        real = os.path.realpath(os.path.join(base, path))
        if not (real == top or real.startswith(top.rstrip(os.sep) + os.sep)):
            return True
    return False


def _ctx_self_approved(payload: dict[str, Any], ws_root: str | None = None) -> bool:
    """See :data:`CTX_SELF_APPROVED_VERBS`. Bash: exactly ``ctx <verb>
    <plain words>`` (no assignment, wrapper, path, quote, redirection or
    second command: ``ccperm.strict_segments``), independent of
    :func:`_is_ctx_invocation`. MCP: the op of ctx's own tool."""
    try:
        from ctx import ccperm

        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        ti = payload.get("tool_input") or payload.get("toolInput") or {}
        if _is_ctx_mcp_tool(name):
            if not isinstance(ti, dict):
                return False
            verb = str(ti.get("op") or "")
            args = [str(ti.get("ref") or ("repo:" if verb in ("search", "stats") else ""))]
            args = [a for a in args if a]
            command = None
        elif name == "Bash":
            command = (_tool_command(payload) or "").strip()
            strict = ccperm.strict_segments(command) if command else None
            if not strict or len(strict) != 1:
                return False
            argv = strict[0]
            if len(argv) < 2 or argv[0] != "ctx" or not _is_ctx_invocation(command, payload.get("cwd")):
                return False
            verb, args = argv[1], argv[2:]
        else:
            return False
        if verb not in CTX_SELF_APPROVED_VERBS:
            return False
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ws_root
        rules = ccperm.cached_rules(ws_root, cwd)
        if command is not None:
            verdict, _why = ccperm.refusal_verdict(command, payload, ws_root, rules=rules)
            if verdict != "none":
                return False
        if _store_only(verb, args):
            return True
        # Reads workspace paths: only where no visible Read rule could object.
        if rules.get("unknown") or rules.get("block_outside"):
            return False
        for rule in [*rules.get("deny", []), *rules.get("ask", [])]:
            parsed = ccperm.parse_rule(rule)
            if parsed is None or ccperm._tool_matches(parsed[0], "Read", glob=True):
                return False
        return not _leaves_workspace(args, ws_root, cwd)
    except Exception:
        return False


def claude_code_pre_tool_use_output(emitted: dict[str, Any], decision: Any) -> dict[str, Any]:
    """The one place every Claude Code PreToolUse answer passes through.

    On Claude Code an ``allow`` is emitted only for one of ctx's own
    read-only verbs (:data:`CTX_SELF_APPROVED_VERBS`), never with a
    rewritten input; the gate records it as ``_permission == "allow"``. Any other ``allow`` --
    whatever code produced it, now or merged later -- leaves with no
    decision: rewritten input and added context work without one, and the
    host then applies the user's rules and prompt exactly as without ctx.
    Live on 2.1.322, ``allow`` with rewritten input ran a command past the
    user's deny and ask rules; ``allow`` alone skipped the prompt for a call
    no rule covered (an MCP tool)."""
    try:
        hso = emitted.get("hookSpecificOutput") if isinstance(emitted, dict) else None
        if not isinstance(hso, dict) or hso.get("permissionDecision") != "allow":
            return emitted
        if (isinstance(decision, dict) and decision.get("_permission") == "allow"
                and "updatedInput" not in hso):  # R1: never allow with a rewrite
            return emitted
        hso = {k: v for k, v in hso.items()
               if k not in ("permissionDecision", "permissionDecisionReason")}
        if set(hso) <= {"hookEventName"}:
            return {}
        return {**emitted, "hookSpecificOutput": hso}
    except Exception:
        return {}


def _to_claude_code_schema(decision: dict[str, Any]) -> dict[str, Any]:
    """Translate the canonical decision into Claude Code's PreToolUse hook
    schema (hookSpecificOutput.permissionDecision: allow|deny|ask).

    Layer 2: a decision carrying a ``rewrite`` becomes a transparent input
    substitution — ``permissionDecision: "allow"`` with ``updatedInput``
    directly under ``hookSpecificOutput``, which replaces the tool's
    arguments before it runs (https://code.claude.com/docs/en/hooks)."""
    rewrite = decision.get("rewrite")
    permission = decision.get("_permission", "allow")
    if isinstance(rewrite, dict) and isinstance(rewrite.get("updatedInput"), dict):
        hso: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "updatedInput": rewrite["updatedInput"],
        }
        # R1: a rewritten input never carries a decision of ctx's.
        # No decision otherwise: the host applies its own rules and prompt.
        out = {"hookSpecificOutput": hso}
        _attach_context(out, decision, proceeds=True)
        return out
    if decision.get("decision", "allow") == "allow" and permission == "none":
        # No opinion: the host decides exactly as it would without ctx.
        # Advice for the model still travels, without a decision.
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
        _attach_context(out, decision)
        return out if len(out["hookSpecificOutput"]) > 1 else {}
    mapping = {"allow": "allow", "deny": "deny", "force_ask": "ask"}
    out: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": mapping.get(decision.get("decision", "allow"), "allow"),
        }
    }
    if decision.get("reason"):
        out["hookSpecificOutput"]["permissionDecisionReason"] = decision["reason"]
    _attach_context(out, decision)
    return out


def _attach_context(out: dict[str, Any], decision: dict[str, Any],
                    proceeds: bool = False) -> None:
    """Model-visible advisory text (``hookSpecificOutput.additionalContext``)
    for a call that proceeds: Claude Code presents it as a system reminder
    (observed, 2.1.322); Codex adds it as developer context (hooks reference).
    Only ever attached to a call that is allowed, or rewritten (``proceeds``:
    a canonical deny carrying a rewrite runs, in its rewritten form)."""
    note = decision.get("context")
    if isinstance(note, str) and note and (
            proceeds or decision.get("decision", "allow") == "allow"):
        out["hookSpecificOutput"]["additionalContext"] = note


def _to_codex_schema(decision: dict[str, Any]) -> dict[str, Any]:
    """Translate the canonical decision into Codex's PreToolUse schema.

    Codex accepts ``permissionDecision: allow`` only for an input rewrite that
    also supplies ``updatedInput``.  A normal pass-through therefore emits an
    empty JSON object.  Codex does not currently support ``ask`` at this hook,
    so a canonical ask degrades safely to a deny carrying the reason.
    """
    rewrite = decision.get("rewrite")
    if isinstance(rewrite, dict) and isinstance(rewrite.get("updatedInput"), dict):
        # Withheld. Codex accepts a rewrite only as `permissionDecision:
        # "allow"` with `updatedInput`, and its documentation does not
        # establish that its approval policy still applies to a call a hook
        # allowed (PermissionRequest is a separate event). On Claude Code the
        # same shape was shown live to skip the user's deny and ask rules.
        # So the original command runs as the model asked, under Codex's own
        # approval, and the PostToolUse gate bounds what it prints. Advice
        # still travels as context, without a decision.
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
        _attach_context(out, decision, proceeds=True)
        return out if len(out["hookSpecificOutput"]) > 1 else {}

    raw = decision.get("decision", "allow")
    if raw in ("deny", "ask", "force_ask"):
        reason = str(decision.get("reason", "")).strip()
        if raw in ("ask", "force_ask") and not reason:
            reason = "ctx requires confirmation before this tool call"
        out: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
            }
        }
        if reason:
            out["hookSpecificOutput"]["permissionDecisionReason"] = reason
        return out

    if decision.get("context"):
        # A pass-through that carries advice: no permissionDecision (Codex
        # accepts `allow` only with updatedInput), only the documented
        # additionalContext.
        advised: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
        _attach_context(advised, decision)
        return advised
    return {}


# The Antigravity PreToolUse output contract (https://antigravity.google/docs/hooks,
# and observed live on agy 1.5.5):
#     {"decision": "allow"|"auto_approve"|"deny"|"ask"|"force_ask"|
#                  "deny_unless_prior_grant",
#      "reason": str?, "overwrite": {...}?, "permissionOverrides": [str]?}
# ``overwrite`` is merged (shallow, top level) into ``toolCall.args`` before
# the tool runs, and the result is prefixed "A pre-tool hook changed the
# arguments of this tool call before it ran. Changed: <keys>." A bare {} is
# read as a deny, so every path names a decision. This emitter sends only
# allow, deny and force_ask.
_AGY_PRE_DECISIONS = ("allow", "deny", "ask", "force_ask")
# "...; bounded to the first 240 lines." / "content grep bounded to 60 matches."
_APPLIED_BOUND_RE = re.compile(r"(;\s*|\s*content grep )?bounded to (the first )?\d+ (lines|matches)\.?")
_AGY_PRE_KEYS = ("decision", "reason", "overwrite", "permissionOverrides")


def _agy_overwrite(decision: dict[str, Any], payload: dict[str, Any] | None,
                   ws_root: str | None) -> dict[str, Any] | None:
    """The overwrite response for a rewrite this host will apply, else None.

    Only the changed arguments are sent (the host merges them). A shell
    command rewrite is checked against the user's own permission rules
    (ctx.agyperm): the host evaluates its rules on the rewritten ``ctx run``
    line, so without this a rule that pre-approves ctx would approve
    commands the user's rules ask about or deny.
    """
    rewrite = decision["rewrite"]
    p = payload or {}
    name = str(p.get("tool_name") or p.get("toolName") or "")
    if not can_rewrite_call("antigravity", name, p):
        return None
    original = p.get("tool_input") or p.get("toolInput") or {}
    if not isinstance(original, dict):
        original = {}
    overwrite = {k: v for k, v in rewrite["updatedInput"].items()
                 if k not in original or original[k] != v}
    if not overwrite:
        return None
    verdict, why = "allow", ""
    if "command" in rewrite:
        from ctx.agyperm import rewrite_verdict

        old_cmd = next((str(original[k]) for k in ("CommandLine", "command", "cmd")
                        if isinstance(original.get(k), str)), "")
        verdict, why = rewrite_verdict(old_cmd, str(rewrite["command"]))
        if verdict == "deny":
            decision["_agy_sent"] = "deny"
            return {"decision": "deny", "reason": why}
    decision["_agy_sent"] = verdict
    reason = str(rewrite.get("reason", "")).strip()
    if why:
        reason = f"{why}\n{reason}".strip()
    out: dict[str, Any] = {"decision": verdict, "overwrite": overwrite}
    if reason:
        out["reason"] = reason
    return out


def _to_antigravity_schema(decision: dict[str, Any], payload: dict[str, Any] | None = None,
                           ws_root: str | None = None) -> dict[str, Any]:
    """Layer 2 for the antigravity dialect.

    A decision carrying a ``rewrite`` becomes an argument ``overwrite`` where
    the host applies one (agy 1.5.4+, for the tools in
    ``ctx.governance.REWRITABLE``): the call runs once, bounded, with no
    extra turn. Anywhere else it degrades to a **deny whose reason names the
    contained command**, which keeps the birth gate intact at the cost of a
    turn. With no output substitution on this host (PostToolUse's only legal
    output is ``{}``), the pre-gate is the only enforcement point.
    """
    rewrite = decision.get("rewrite")
    if isinstance(rewrite, dict) and isinstance(rewrite.get("updatedInput"), dict):
        sent = _agy_overwrite(decision, payload, ws_root)
        if sent is not None:
            return sent
        decision["_agy_sent"] = "deny"
        reason = str(rewrite.get("reason", "")).strip()
        cmd = str(rewrite.get("command", "")).strip()
        if cmd:
            sep = "" if (not reason or reason[-1] in ".!?;:") else "."
            reason = f"{reason}{sep} Re-run it as: {cmd}".strip()
        else:
            # A field rewrite (a Read `limit`, a Grep `head_limit`) words the
            # bound as already applied ("bounded to the first N lines"). Here
            # nothing was applied, so that claim is removed and the deny says
            # what to re-issue instead; the retrieval address stays.
            fields = rewrite.get("fields") or {}
            caps = " and ".join(f"{k}={fields[k]}" for k in fields
                                if isinstance(fields.get(k), int))
            reason = _APPLIED_BOUND_RE.sub(".", reason).replace("..", ".")
            reason = (f"{reason} Not applied: this host cannot rewrite this tool's "
                      f"arguments; re-issue the call with {caps or 'a bound'}.").strip()
        return {"decision": "deny", "reason": reason or "run this through ctx"}
    raw = decision.get("decision", "allow")
    out: dict[str, Any] = {
        "decision": raw if raw in _AGY_PRE_DECISIONS else "allow",
    }
    for key in ("reason", "permissionOverrides"):
        if decision.get(key):
            out[key] = decision[key]
    return {k: v for k, v in out.items() if k in _AGY_PRE_KEYS}


#: The emission-governor nudge, verbatim. Named because the Rust shim
#: (native/ctx-hook-native) has to carry the same prose and the two must stay
#: byte-identical — tests/test_native_hook.py compares rendered output only
#: when the binary happens to be built, so
#: tests/test_cross_language_constants.py compares this template against the
#: Rust source directly. ADVISORY text: this is a PostToolUse nudge, never a
#: safety-class decision string (Rule 7).
EMISSION_NUDGE_TEMPLATE = (
    "CTX_EMISSION_GOVERNOR: session output ~{tokens} tokens "
    "(avg {per_turn}/turn). Output volume is the dominant "
    "cost+latency driver. Keep narration terse; cite coordinates "
    "(file:line, run:/span handles) instead of restating content."
)


#: Hook flavor -> the host id the relay addresses. Only Claude Code differs
#: (the hook dialect is ``claude-code``; the host in ``ctx.hosts`` is
#: ``claude``). Spelled here rather than imported because this module must
#: not import :mod:`ctx.hosts` on the hot path.
_RELAY_HOST = {"claude-code": "claude"}


def _relay_subscriber(flavor: str, payload: dict[str, Any]) -> str:
    """This harness's relay address: ``<host>:<session>``, or ``<host>``."""
    host = _RELAY_HOST.get(flavor, flavor)
    session = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    ).strip()[:32]
    session = "".join(c for c in session if c.isalnum() or c in "._-")
    return f"{host}:{session}" if session else host


def _relay_drain(
    payload: dict[str, Any], flavor: str, stage: str, ws_root: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Take whatever the cross-harness relay has queued for this harness.

    Gated on the queue file existing so a workspace that never uses the relay
    pays one ``os.path.exists`` per tool call and imports nothing — the hot
    path is pinned by ``tests/test_hook_hot_path.py`` and this must not widen
    it. Fail-open in every direction: the relay is an advisory channel, and a
    corrupt or unreadable queue must degrade to silence rather than to a
    failed tool call.
    """
    try:
        root = ws_root if ws_root is not None else _resolve_workspace_root(payload)
        if not root:
            return "", []
        if not os.path.exists(session_reads_dir(root, "relay", "relay.jsonl")):
            return "", []
        from ctx import relay

        return relay.drain_quietly(root, _relay_subscriber(flavor, payload), stage=stage)
    except Exception:
        return "", []


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
        doc = read_window_doc(workspace_root)
        cum_output = int(doc.get("cum_output") or 0)
        requests = int(doc.get("requests") or 0)
        if requests <= 0 or cum_output <= 0:
            return None
        policy = _load_guard_policy(workspace_root)
        step = max(1, int(policy.get("emission_nudge_tokens", EMISSION_NUDGE_TOKENS_DEFAULT)))
        tier = cum_output // step
        if tier < 1:
            return None
        per_request = cum_output / requests
        if per_request < 500:  # already terse: no nudge, ever
            return None
        from ctx.engagement import claim_emission_tier

        if not claim_emission_tier(workspace_root, tier):
            return None
        return EMISSION_NUDGE_TEMPLATE.format(
            tokens=f"{cum_output:,}", per_turn=f"{per_request:.0f}"
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


def _search_symbol(tool: str, ti: dict[str, Any]) -> str | None:
    """The bare identifier this tool call is tracing, from EITHER search path.

    Two paths reach here, and for a long time only one was inspected. A shell
    ``grep``/``rg`` arrives as a command string that has to be parsed. The
    host's own search tool (Claude Code ``Grep``, and the same shape elsewhere)
    arrives as a structured ``pattern`` and needs no parsing at all — and it is
    the DEFAULT path, so on a navigation-heavy repository it is most of the
    population. The PostToolUse matcher already routed those events here; the
    governor then discarded them for not being Bash, and the call-graph verbs
    were never offered to an agent that never shelled out.
    """
    if "bash" in tool or "command" in tool:
        for k in ("command", "Command", "CommandLine", "cmd"):
            if isinstance(ti.get(k), str):
                return _grep_symbol(ti[k])
        return None
    # Structured search tools: the pattern is already isolated, so apply the
    # same bare-identifier test the command parser applies to its first
    # positional. A regex or a multi-word phrase is not symbol-tracing.
    if "grep" in tool or "search" in tool:
        for k in ("pattern", "Pattern", "query", "regex"):
            v = ti.get(k)
            if isinstance(v, str):
                term = v.strip("'\"")
                return term if _IDENT_RE.match(term) else None
    return None


def _navigation_nudge(payload: dict[str, Any]) -> str | None:
    """Fire once when the session has traced >= _NAV_THRESHOLD distinct bare
    identifiers — the hand-traced-call-graph pattern — whether it did so with a
    shell grep or with the host's own search tool. Fail-open."""
    try:
        tool = str(payload.get("tool_name") or payload.get("toolName") or "").lower()
        ti = payload.get("tool_input") or payload.get("toolInput") or {}
        if not isinstance(ti, dict):
            return None
        if _gov.is_mcp(tool, payload):
            return None  # an MCP server's search_* is not hand-tracing code
        symbol = _search_symbol(tool, ti)
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


def _record_edit_outcome(payload: dict[str, Any], flavor: str) -> None:
    """Record what happened to the host's own edit. Observation only, fail-open.

    ``_tool_kind`` has always classified Edit/Write/MultiEdit, and PreToolUse
    has always allowed them through -- so this hook watched every edit an agent
    made and never once looked at whether it landed. That is the one number
    needed to decide whether repairing failed edits is worth building, and it
    did not exist.

    Deliberately here rather than in the PreToolUse path: only the *result*
    says whether the needle matched, matched twice, or matched nothing, and
    only PostToolUse carries a result. Hosts whose contract has no PostToolUse
    payload (Antigravity) contribute nothing, which is why the summary reports
    which hosts it heard from instead of implying it heard from all of them.
    """
    tool = str(payload.get("tool_name") or payload.get("toolName") or "")
    if _tool_kind(tool, flavor, payload) != "edit":
        return
    workspace_root = _resolve_workspace_root(payload)
    if not workspace_root:
        return
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    ti = ti if isinstance(ti, dict) else {}
    failure = payload.get("hook_event_name") == "PostToolUseFailure"
    tr = payload.get("tool_response")
    if tr is None:
        tr = payload.get("toolResponse")
    if failure:
        # PostToolUseFailure carries the failure text as `error`, which is
        # the same text the model sees for the failed call.
        stdout, stderr = str(payload.get("error") or ""), ""
    else:
        stdout, stderr = _normalize_tool_response(tr)
    is_error = True if failure else None
    if isinstance(payload.get("is_error"), bool):
        is_error = payload["is_error"]
    if isinstance(tr, dict) and isinstance(tr.get("is_error"), bool):
        is_error = tr["is_error"]

    from ctx.edit_outcomes import append_edit_outcome, classify, resolve_model

    # Claude Code fires PostToolUse only for a tool that succeeded (failures
    # arrive as PostToolUseFailure), and its edit results are structured
    # objects carrying the whole original file -- whose text must not be
    # scanned for failure wording. A success-shaped result there is applied.
    if (
        flavor == "claude-code" and not failure and is_error is not True
        and isinstance(tr, dict) and ("filePath" in tr or "notebook_path" in tr)
    ):
        outcome = "applied"
    else:
        outcome = classify(f"{stdout}\n{stderr}", is_error=is_error)

    def _pick(*keys: str) -> str:
        for key in keys:
            value = ti.get(key)
            if isinstance(value, str):
                return value
        return ""

    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    append_edit_outcome(
        workspace_root,
        tool=tool,
        outcome=outcome,
        path=_pick("file_path", "filePath", "path", "target_file") or None,
        # Lengths only. The strings themselves are the edit's content, and a
        # ledger that quotes them stops being safe to attach to a receipt.
        old_len=len(_pick("old_string", "oldString", "old_str")),
        new_len=len(_pick("new_string", "newString", "new_str", "content")),
        flavor=flavor,
        # Which model made the edit: CTX_MODEL when a launcher set it (ctx
        # orchestrate does), else the transcript's last named model, else
        # "unknown". The format is derived from the tool name inside.
        model=resolve_model(
            transcript_path=transcript if isinstance(transcript, str) else None
        ),
    )


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


def _tool_command(payload: dict[str, Any]) -> str | None:
    """Return the shell command attached to a command-tool event, if any."""
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(ti, dict):
        return None
    for key in ("CommandLine", "command", "Command", "cmd"):
        value = ti.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _command_argv(command: str | None) -> list[str] | None:
    """Losslessly recover argv only for a simple, non-shell expression."""
    if not command or _SHELL_META_RE.search(command):
        return None
    try:
        argv = shlex.split(command)
        return argv or None
    except ValueError:
        return None


def _coerce_text(value: Any) -> str:
    """Last-resort text for a tool_response shape ``_normalize_tool_response``
    could not handle. Never raises; returns "" only if even ``repr`` fails."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    except Exception:
        pass
    try:
        return repr(value)
    except Exception:
        return ""


def _gate_failure_digest(
    ws_root: str | None,
    tool_name: str,
    stdout: str,
    stderr: str,
    exc: BaseException,
) -> str:
    """The bounded stand-in emitted when the emission gate itself breaks.

    Fail-CLOSED contract: over-budget raw bytes never reach the model just
    because gate code has a bug. The content is not discarded either — it is
    retained and named by a handle, in descending order of fidelity:

    1. the artifact store (``ctx get blob:<id>`` — the normal retrieval path);
    2. a spill file under ``<ws>/.ctx-session-reads/gate-fallback/`` when the
       store itself is the thing that broke;
    3. nothing retained — then the excerpt below is all that survives, and the
       digest says so plainly rather than implying a handle that does not work.

    Every step is individually guarded: this function is the net, so it must
    not add a new way to fail. Cost is bounded (one hash, one write).
    """
    raw = stdout if not stderr else (stdout + ("\n" if stdout else "") + stderr)
    data = raw.encode("utf-8", "replace")
    nbytes = len(data)
    handle: str | None = None

    try:
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        ws = resolve_workspace(ws_root or ".")
        blob = Store(
            ws.workspace_id, retention_days=ws.config.store.retention_days
        ).put_blob(data)
        handle = f"ctx get blob:{blob[:12]}"
    except Exception as inner:
        _note_guard_failure(ws_root, op="emission_gate", stage="fallback-store", exc=inner)

    if handle is None and ws_root:
        try:
            import hashlib

            rel = os.path.join(_LEDGER_DIR_NAME, _GATE_FALLBACK_DIR)
            spill_dir = os.path.join(ws_root, rel)
            os.makedirs(spill_dir, exist_ok=True)
            name = hashlib.sha256(data).hexdigest()[:12] + ".txt"
            path = os.path.join(spill_dir, name)
            if not os.path.exists(path):
                tmp = path + f".{os.getpid()}.tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
            handle = "read " + os.path.join(rel, name)
        except Exception as inner:
            _note_guard_failure(ws_root, op="emission_gate", stage="fallback-spill", exc=inner)

    head = data[:_GATE_FAILURE_HEAD_BYTES].decode("utf-8", "replace")
    lines = [
        f"[ctx gate-failed tool={tool_name or '?'} bytes={nbytes} "
        f"error={type(exc).__name__}]",
        "CTX_EMISSION_GATE: the output gate failed internally, so this result "
        "was NOT emitted raw (fail-closed): an unbounded tool result must "
        "never reach the context because gate code broke.",
    ]
    if handle:
        _GATE_STATS["handle"] = handle
        lines.append(f"Full output retained — retrieve on demand with:  {handle}")
    else:
        lines.append(
            "Full output could NOT be retained; only the excerpt below survives."
        )
    lines.append("")
    lines.append(f"head ({min(nbytes, _GATE_FAILURE_HEAD_BYTES)} of {nbytes} bytes):")
    lines.append(head)
    return "\n".join(lines)


# What the emission gate measured and did on this call, for the coverage
# ledger. A hand-off within one hook invocation (the gate is the only writer
# and runs once), the same idiom as _APPLY_ROOT.
_GATE_STATS: dict[str, Any] = {}

#: Kinds whose result the gate never replaces (governance routes observe and
#: exempt; see ctx.governance.OBSERVE_REASONS / EXEMPT_REASONS).
_UNGATED_KINDS = frozenset({"edit", "skill", "control", "schema", "media"})

_RESPONSE_KEYS = ("tool_response", "toolResponse", "tool_output", "toolOutput",
                  "output", "result", "content")


def _tool_response(payload: dict[str, Any]) -> Any:
    for k in _RESPONSE_KEYS:
        if k in payload:
            return payload[k]
    return None


_MEDIA_TYPES = frozenset({"image", "pdf", "audio", "document"})
_MEDIA_SCAN_NODES = 4000  # bound on the walk below; a deeper result is not media-shaped
_DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,", re.I)
_MEDIA_MIME_RE = re.compile(r"^(image|audio|video)/|^application/pdf$", re.I)
# Only for choosing where a digest may be WRITTEN (never for exempting a
# result from the gate): a long unbroken base64-alphabet run.
_ENCODED_RE = re.compile(r"[A-Za-z0-9+/=\r\n]{512,}")


def _looks_encoded(value: str) -> bool:
    return len(value) >= 512 and bool(_ENCODED_RE.fullmatch(value[:4096]))


def _media_reason(tr: Any) -> str | None:
    """Why this tool result is media the gate must not reshape, or None.

    Declared types first; content is never guessed at. The two shapes
    observed live on Claude Code declare themselves: a Bash result carries
    ``isImage`` and a Read result carries ``type``, so text is gated however
    it looks (hex dumps, sequences, base64 a model asked for) and only what
    the host marks as an image or document is left alone. A shape with no
    declared type is walked for declarations only: a content block typed
    image/audio/document, a MIME-keyed image output (a notebook's
    ``"image/png"``), ``isImage``, or a value that is wholly a base64 data:
    URI. When unsure, it is text. Bounded walk; pure.
    """
    if isinstance(tr, dict) and isinstance(tr.get("stdout"), str) and "interrupted" in tr:
        return "isImage" if tr.get("isImage") is True else None
    if isinstance(tr, dict) and isinstance(tr.get("type"), str) and isinstance(tr.get("file"), dict):
        kind = tr["type"].lower()
        if kind in _MEDIA_TYPES:
            return f"type={kind}"
        if kind == "text":
            return None
    stack = [(tr, 0)]
    seen = 0
    while stack and seen < _MEDIA_SCAN_NODES:
        node, depth = stack.pop()
        seen += 1
        if isinstance(node, str):
            if _DATA_URI_RE.match(node[:256]):
                return "data-uri"
        elif isinstance(node, dict):
            if node.get("isImage") is True:
                return "isImage"
            kind = node.get("type")
            if isinstance(kind, str) and kind.lower() in _MEDIA_TYPES:
                return f"type={kind.lower()}"
            for key, value in node.items():
                if isinstance(key, str) and _MEDIA_MIME_RE.match(key) and isinstance(value, str) and value:
                    return f"mime={key.lower()}"
            if depth < 12:
                stack.extend((v, depth + 1) for v in node.values()
                             if isinstance(v, (str, dict, list)))
        elif isinstance(node, list) and depth < 12:
            stack.extend((v, depth + 1) for v in node[:500])
    return None


def _jsize(value: Any) -> int:
    """Serialized size in UTF-8 bytes: the measure the budget is held to."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", "replace"))


_PATHLIKE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|/|~/|[A-Za-z]:[\\/]|\.{1,2}/)\S*$")
#: Structural-reduction ladder: (longest string kept, longest list kept).
_SHRINK_LADDER = ((1024, 20), (256, 8), (64, 3), (32, 1))


def _text_blocks(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        for b in value)


def _digest_slot(value: Any) -> bool:
    """May the digest be written into this value? Text only: never a value
    that is path-like, URL-like or base64-like."""
    if isinstance(value, str):
        return not _PATHLIKE_RE.match(value[:4096]) and not _looks_encoded(value)
    return _text_blocks(value)


def _shrink(value: Any, keep: Any, max_str: int, max_list: int, pointer, log: list) -> Any:
    """``value`` with every over-long string and list reduced in place.

    Types never change: a string stays a string (a pointer to the stored
    result), a list stays a list of the same kind of element. ``keep`` is the
    one object (the digest) that is never reduced.
    """
    if value is keep:
        return value
    if isinstance(value, str):
        if len(value) > max_str:
            log.append(("str", len(value)))
            return pointer(len(value.encode("utf-8", "replace")))
        return value
    if isinstance(value, dict):
        return {k: _shrink(v, keep, max_str, max_list, pointer, log) for k, v in value.items()}
    if isinstance(value, list):
        items = [_shrink(v, keep, max_str, max_list, pointer, log) for v in value[:max_list]]
        if len(value) > max_list:
            log.append(("list", len(value)))
            if all(isinstance(v, str) for v in value):
                items.append(f"[ctx: {len(value) - max_list} more items elided]")
        return items
    return value


def _shape_output(tr: Any, text: str, budget: int,
                  handle: str | None = None) -> tuple[Any, str | None, bool]:
    """Fit a gate digest to a built-in tool's result, under a checked contract.

    Claude Code validates ``updatedToolOutput`` for built-in tools against the
    tool's output schema and silently ignores a value that does not match
    (confirmed live on 2.1.322: a string was dropped for Bash and Read, an
    object in the tool's own shape replaced what the model saw). The one
    obligation, checked rather than assumed: what is returned is in the
    result's own shape AND its serialized size plus any context text is
    within ``budget`` -- or ``shaped`` is False and nothing is replaced.

    * Shapes observed on a live host use an explicit field map: Bash's
      ``stdout`` (``stderr`` emptied, the digest covers both) and a text
      Read's ``file.content``.
    * Every other shape is reduced structurally, without guessing which field
      is "the content": over-long strings become a pointer to the stored
      result, over-long lists are cut (a list of strings gets a marker
      element), types never change, and the digest -- which states the true
      totals -- travels in ``additionalContext``. The digest goes into the
      result itself only when one text field is clearly dominant and is not
      path-, URL- or base64-like.
    * Each candidate is measured; over budget, the next stronger reduction is
      tried; when none fits, ``shaped`` is False.

    Returns ``(value, context, shaped)``. Media never reaches here.
    """
    # ``handle`` is the retrieval command the digest producer returned for
    # this result (``ctx get run:<id>#stdout``). It is passed in, never
    # recovered from ``text``: the digest quotes the raw tool output, so any
    # pattern search over it could pick up an address the tool itself wrote.

    def pointer(nbytes: int) -> str:
        where = handle or "the ctx digest"
        return f"[ctx: {nbytes} bytes elided; full result: {where}]"

    def fits(value: Any, context: str | None) -> bool:
        return _jsize(value) + (len(context.encode("utf-8", "replace")) if context else 0) <= budget

    if isinstance(tr, str):
        return (text, None, True) if fits(text, None) else (None, None, False)

    # Live-observed shapes: explicit field maps.
    mapped: Any = None
    if isinstance(tr, dict) and isinstance(tr.get("stdout"), str) and "interrupted" in tr:
        mapped = dict(tr, stdout=text)
        if isinstance(tr.get("stderr"), str):
            mapped["stderr"] = ""
    elif (isinstance(tr, dict) and tr.get("type") == "text" and isinstance(tr.get("file"), dict)
          and isinstance(tr["file"].get("content"), str)):
        mapped = dict(tr, file=dict(tr["file"], content=text))
    elif _text_blocks(tr):
        mapped = [{"type": "text", "text": text}]
    elif isinstance(tr, dict):
        # One clearly dominant text field (at least half the bytes) may carry
        # the digest; a path, URL or base64 value never does.
        total = _jsize(tr)
        for key, value in tr.items():
            if _digest_slot(value) and _jsize(value) * 2 >= total:
                mapped = dict(tr)
                mapped[key] = text if isinstance(value, str) else [{"type": "text", "text": text}]
                break
    if mapped is not None:
        if fits(mapped, None):
            return mapped, None, True
        keep = text if not isinstance(mapped, list) else None
        for max_str, max_list in _SHRINK_LADDER:
            log: list = []
            candidate = _shrink(mapped, keep, max_str, max_list, pointer, log)
            if fits(candidate, None):
                return candidate, None, True

    # Structural reduction, digest delivered alongside.
    for max_str, max_list in _SHRINK_LADDER:
        log = []
        candidate = _shrink(tr, None, max_str, max_list, pointer, log)
        lists = sorted({n for k, n in log if k == "list"}, reverse=True)[:5]
        strs = sum(1 for k, _ in log if k == "str")
        header = (
            "[ctx output-shaped] The tool result was shortened in place to fit the "
            f"output budget: {strs} long value(s) replaced by a pointer"
            + (f", {len([1 for k, _ in log if k == 'list'])} list(s) cut to {max_list} item(s) "
               f"(true lengths: {', '.join(str(n) for n in lists)})" if lists else "")
            + ". Counts beside them still describe the full result. Digest of the "
            "full result:\n")
        context = header + text
        if fits(candidate, context):
            return candidate, context, True
    return None, None, False


def _shape_replacement(tr: Any, text: str, budget: int = _MAX_TOOL_OUTPUT_BYTES_DEFAULT,
                       handle: str | None = None) -> tuple[Any, bool]:
    """Back-compatible view of :func:`_shape_output`: ``(value, shaped)``."""
    value, _context, shaped = _shape_output(tr, text, budget, handle)
    return (value, True) if shaped else (text, False)


_COVERAGE_LEDGER = "coverage.jsonl"
_COVERAGE_LEDGER_OLD = "coverage.1.jsonl"
COVERAGE_MAX_BYTES = 512 * 1024  # per generation; two generations are kept
COVERAGE_SCHEMA = "ctx.coverage/v1"
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:/@-]")


def _safe_id(value: Any, limit: int = 64) -> str:
    return _SAFE_ID_RE.sub("_", str(value or ""))[:limit]


def _rotate_coverage(ledger_dir: str, path: str) -> None:
    """Rotate ``path`` into the kept generation, once, under concurrency.

    Several hook processes append at once, and each sees the same over-cap
    size. Unserialized, the second one renamed the *fresh* file the first
    had just started over the generation the first had just kept -- losing
    it. So the decision is re-made under an exclusive lock: only a process
    that still sees the file over the cap rotates. Taken only on this rare
    path; ``fcntl`` is imported here, never at module scope (hot path).
    Without ``fcntl`` (Windows) it degrades to the unlocked rotation.
    """
    try:
        import fcntl
    except ImportError:
        os.replace(path, os.path.join(ledger_dir, _COVERAGE_LEDGER_OLD))
        return
    with open(os.path.join(ledger_dir, _COVERAGE_LEDGER + ".lock"), "a") as lock:
        try:
            # Never wait: telemetry must not delay a decision. If another
            # hook holds the lock (or is stopped holding it), it is rotating;
            # this call just appends to whichever file is current.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            if os.stat(path).st_size > COVERAGE_MAX_BYTES:
                os.replace(path, os.path.join(ledger_dir, _COVERAGE_LEDGER_OLD))
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _note_coverage(
    ws_root: str | None,
    flavor: str,
    stage: str,
    payload: dict[str, Any],
    kind: str,
    route: str,
    decision: str,
    raw: int | None = None,
    emitted: int | None = None,
    note: str | None = None,
) -> None:
    """One line per hooked tool call to ``.ctx-session-reads/coverage.jsonl``.

    Identity only, never arguments or content: the tool name; for an MCP tool
    the server and tool; for a skill the skill's name. Plus the stage, the
    declared route, what this call's decision was, and raw versus emitted
    bytes where the hook knows them. Fail-open: telemetry never changes a
    decision and never raises.
    """
    if not ws_root:
        return
    try:
        import time

        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        row: dict[str, Any] = {
            "schema": COVERAGE_SCHEMA,
            "ts": int(time.time()),
            "host": flavor,
            "stage": stage,
            "tool": _safe_id(name),
            "kind": kind,
            "route": route,
            "decision": decision,
        }
        session = payload.get("session_id") or payload.get("conversationId") \
            or payload.get("conversation_id") or payload.get("sessionId")
        if session:
            row["session"] = _safe_id(session, 64)
        if kind == "mcp":
            server, tool = _gov.mcp_identity(name, payload)
            row["mcp_server"], row["mcp_tool"] = _safe_id(server), _safe_id(tool)
        elif kind == "skill":
            ti = payload.get("tool_input") or payload.get("toolInput") or {}
            if isinstance(ti, dict):
                for key in ("skill", "name", "skill_name", "command"):
                    if isinstance(ti.get(key), str) and ti[key].strip():
                        row["skill"] = _safe_id(ti[key].strip().split()[0])
                        break
        if isinstance(raw, int):
            row["raw_bytes"] = raw
        if isinstance(emitted, int):
            row["emitted_bytes"] = emitted
        if note:
            row["note"] = _safe_id(note)
        ledger_dir = session_reads_dir(ws_root)
        os.makedirs(ledger_dir, exist_ok=True)
        # The ledger directory ignores itself, so git (status, ls-files,
        # clean) and ripgrep never report ctx's bookkeeping as the user's
        # files, even in a repository whose own ignore file does not name it.
        # Nothing under it holds command text (sessiondir.PRIVATE_LEDGERS),
        # so a content search that does walk it finds none of its own
        # patterns; a listing that walks it shows it, as it shows .git.
        self_ignore = os.path.join(ledger_dir, ".gitignore")
        if not os.path.exists(self_ignore):
            with open(self_ignore, "w", encoding="utf-8") as fh:
                fh.write("*\n")
        path = os.path.join(ledger_dir, _COVERAGE_LEDGER)
        # Bounded: past COVERAGE_MAX_BYTES the file becomes the single kept
        # generation (coverage.1.jsonl, replacing the previous one) and a
        # fresh file starts. One stat per call; the report never reads more
        # than two generations, whatever the total history.
        try:
            if os.stat(path).st_size > COVERAGE_MAX_BYTES:
                _rotate_coverage(ledger_dir, path)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def _pre_decision_label(decision: dict[str, Any] | None, flavor: str,
                        payload: dict[str, Any] | None = None) -> str:
    """What the input side did to this call, in one word."""
    d = decision or {}
    if isinstance(d.get("rewrite"), dict):
        if flavor == "antigravity":
            # What the emitter actually sent (_to_antigravity_schema notes it).
            return {"allow": "rewritten", "force_ask": "rewritten_asked"}.get(
                str(d.get("_agy_sent", "")), "denied")
        name = str((payload or {}).get("tool_name") or (payload or {}).get("toolName") or "")
        return "rewritten" if can_rewrite_call(flavor, name or None, payload) else "denied"
    return {"deny": "denied", "force_ask": "asked", "ask": "asked"}.get(
        str(d.get("decision", "allow")), "allowed")


def _note_pre_coverage(
    payload: dict[str, Any],
    flavor: str,
    ws_root: str | None,
    policy: dict[str, Any] | None,
    decision: dict[str, Any] | None,
) -> None:
    if not ws_root:
        return
    try:
        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        kind = _governed_kind(name, flavor, payload)
        pol = policy or {}
        pre, _post = _gov.stage_routes(
            kind, DIALECT_CAPS.get(flavor, {}),
            collapse=bool(pol.get("collapse", True)),
            steering=_steering_allows(pol) if pol else True,
            host=flavor, name=name,
        )
        if pre in ("rewrite", "bound") and flavor == "antigravity" and _agy_legacy(payload):
            pre = "deny_steer"
        label = ("advisory" if pol.get("mode") == "advisory"
                 else _pre_decision_label(decision, flavor, payload))
        note = pol.get("_media_note")
        if note and kind == "media":
            # Codex view_image: the table's route is "exempt", but a media
            # decision was made on its input side; record the route it took.
            pre = "media"
        _note_coverage(ws_root, flavor, "pre", payload, kind, pre or "observe", label,
                       note=str(note) if note else None)
    except Exception:
        pass


def _ran_ctx(payload: dict[str, Any]) -> bool:
    """Was this tool call exactly one ctx command? Judged from the call's
    own input (what was run), never from its output; see
    :func:`_is_ctx_invocation`."""
    cwd = None
    ti = payload.get("tool_input") or payload.get("toolInput") or {}
    if isinstance(ti, dict):
        cwd = ti.get("cwd") or ti.get("Cwd")
    return _is_ctx_invocation(_tool_command(payload), cwd or payload.get("cwd"))


#: Largest host-saved output the gate takes in from the host's own file.
_HOST_FULL_MAX = 256 * 1024 * 1024


def _host_full_output(payload: dict[str, Any], tr: dict[str, Any]) -> str | None:
    """The full output Claude Code saved for a large Bash result, or None.

    Observed on 2.1.322: for a 300 KB and a 5 MB output PostToolUse receives
    ``stdout`` = the LAST 30,000 characters (``stdoutHoldsOutputEnd``) plus
    ``persistedOutputPath`` / ``persistedOutputSize``, a file under
    ``<config>/projects/<project>/<session_id>/tool-results/``. Only that
    location is trusted -- never a path the output merely mentions: the file
    must resolve inside the host's projects directory, in a ``tool-results``
    directory of THIS session, be a regular file (not a link) and have
    exactly the size the host reported."""
    path = tr.get("persistedOutputPath")
    size = tr.get("persistedOutputSize")
    session = str(payload.get("session_id") or "")
    if not isinstance(path, str) or not isinstance(size, int) or not session:
        return None
    if size <= 0 or size > _HOST_FULL_MAX or "/" in session or session in (".", ".."):
        return None
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    projects = os.path.realpath(os.path.join(config, "projects"))
    real = os.path.realpath(path)
    if real != os.path.abspath(path) or not real.startswith(projects + os.sep):
        return None
    parts = os.path.relpath(real, projects).split(os.sep)
    if len(parts) != 4 or parts[1] != session or parts[2] != "tool-results":
        return None
    st = os.lstat(real)
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode) or st.st_size != size:
        return None
    with open(real, "rb") as fh:
        data = fh.read(size + 1)
    if len(data) != size:
        return None
    return data.decode("utf-8", "replace")


def _emission_gate(payload: dict[str, Any], flavor: str) -> str | None:
    """The universal output-side gate. When a tool result exceeds the byte
    budget, persist it losslessly and return a bounded digest (with a working
    ``ctx get`` ref) to substitute for the raw output via ``updatedToolOutput``.
    Returns None to pass the result through untouched.

    Fail-CLOSED (this used to be fail-open, which meant a bug in gate code
    silently released unbounded raw output with no telemetry): once the result
    is known to be over budget, no internal error may return None. Failures
    below that point degrade to a bounded digest carrying a retrieval handle —
    "bounded + retrievable", never "everything" and never "nothing".

    Returning None stays legal only for the cases that are genuinely *not*
    gate failures: a flavor with no substitution field, no tool result present,
    a ctx-authored result (recursion guard), advisory mode, and under-budget
    output. A broken *policy* read does not open the gate — the built-in
    default budget is used instead.

    Claude Code (``updatedToolOutput``) and Codex (``decision:block`` + reason,
    https://learn.chatgpt.com/docs/hooks) both have a substitution field.
    Antigravity's published PostToolUse contract has none — its only legal
    output is ``{}`` (https://antigravity.google/docs/hooks) — so there the gate
    **still persists** the over-budget result and then returns None. The raw
    bytes reach the transcript on that host (nothing can stop them), but they
    also reach the store, so the result keeps an address and ``ctx get`` can
    resolve it afterwards. Capture is worth having even where substitution is
    impossible; the alternative is bytes that flood *and* vanish.
    """
    can_substitute = can_substitute_output(flavor)
    _GATE_STATS.clear()

    # -- phase 1: is there anything to gate at all? Pure and cheap; a failure
    # here means we never saw a tool result, so there is nothing to bound.
    try:
        tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
        tr = None
        for k in ("tool_response", "toolResponse", "tool_output", "toolOutput", "output", "result", "content"):
            if k in payload:
                tr = payload[k]
                break
        if tr is None:
            return None
    except Exception:
        return None

    # Resolved early (pure, cheap) so every failure below has a ledger to
    # report into instead of vanishing.
    ws_root: str | None = None
    try:
        ws_root = _resolve_workspace_root(payload)
    except Exception as exc:
        _note_guard_failure(None, op="emission_gate", stage="workspace", exc=exc)

    try:
        stdout, stderr = _normalize_tool_response(tr)
    except Exception as exc:
        # We hold bytes we cannot interpret — coerce rather than pass through.
        _note_guard_failure(ws_root, op="emission_gate", stage="normalize", exc=exc)
        stdout, stderr = _coerce_text(tr), ""
    host_note = ""
    if flavor == "claude-code" and isinstance(tr, dict):
        try:
            full = _host_full_output(payload, tr)
            if full is not None:
                stdout = full
            elif isinstance(tr.get("persistedOutputSize"), int) and \
                    tr["persistedOutputSize"] > len(stdout.encode("utf-8", "replace")):
                host_note = (
                    f"\n[ctx: stored copy is what Claude Code handed the hook: the last "
                    f"{len(stdout)} characters of {tr['persistedOutputSize']} bytes. For a "
                    f"full capture run: ctx run -- {_tool_command(payload) or '<command>'}]")
        except Exception:
            pass

    # Never digest our own digests or ctx's own tool results (recursion /
    # double-wrap guard). "[ctx " covers every ctx header — run: (digest),
    # get / search / stats (retrieval) — so a large `ctx get` slice run via
    # Bash is not itself re-digested. "densified:" is the reflex arc's
    # declared-densification header prepended above the "[ctx run:" line.
    # The header alone is not trusted: tool output can begin with "[ctx " too,
    # and a flood that does would pass ungated. It counts only when ctx is
    # what ran (a ctx MCP tool, or a shell command whose program is ctx).
    if _is_ctx_mcp_tool(tool_name) or (
        stdout.lstrip().startswith(("[ctx ", "densified:")) and _ran_ctx(payload)
    ):
        _GATE_STATS["decision"] = "ctx_output"
        return None

    # -- phase 2: budget. A broken policy read degrades to the built-in
    # default threshold; it must not skip the gate.
    threshold = _MAX_TOOL_OUTPUT_BYTES_DEFAULT
    try:
        policy = _apply_window_pressure(_load_guard_policy(ws_root), ws_root)
        if str(policy.get("mode")) == "advisory":
            _GATE_STATS["decision"] = "advisory"
            return None
        threshold = int(
            policy.get("max_tool_output_bytes", _MAX_TOOL_OUTPUT_BYTES_DEFAULT)
        )
    except Exception as exc:
        _note_guard_failure(ws_root, op="emission_gate", stage="policy", exc=exc)

    try:
        size = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
    except Exception:
        size = threshold + 1  # cannot measure → treat as over budget
    _GATE_STATS["raw"] = size
    _GATE_STATS["threshold"] = threshold
    _GATE_STATS["decision"] = "pass"
    command = _tool_command(payload)
    is_error = bool(
        payload.get("is_error")
        or payload.get("isError")
        or (isinstance(tr, dict) and tr.get("is_error"))
        or (isinstance(tr, dict) and tr.get("isError"))
    )
    if size <= threshold:
        if can_substitute and _single_named_pytest(command or ""):
            try:
                from ctx import reflex

                reflex.note_steer_result(
                    ws_root,
                    command or "",
                    raw_bytes=size,
                    gated=False,
                    is_error=is_error,
                )
            except Exception:
                pass
        return None  # under budget → byte-identical pass-through

    # -- phase 3: over budget. From here None would mean "emit everything
    # raw", so every failure degrades to the bounded fallback instead.
    try:
        # Lazy: only pay the Store/digest import cost on the over-budget path.
        from ctx.digest import digest_output
        from ctx.store import Store
        from ctx.workspace import resolve_workspace

        ws = resolve_workspace(ws_root or ".")
        store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)
        _GATE_STATS.pop("handle", None)
        text, _short = digest_output(
            store,
            ws,
            tool_name,
            stdout,
            stderr,
            is_error=is_error,
            argv=_command_argv(command),
            contained=can_substitute,
            # Name this harness so the relay fan-out can skip it: an agent
            # watching `digest` wants a peer's expensive output, not an echo
            # of the flood it just produced itself.
            producer=_relay_subscriber(flavor, payload),
        )
        if not isinstance(text, str) or not text.strip():
            raise ValueError("digest produced no text")
        if isinstance(_short, str) and re.fullmatch(r"[0-9a-f]{6,64}", _short):
            _GATE_STATS["handle"] = f"ctx get run:{_short}#stdout"
        # A speculative-native result that crossed the gate is now a proven
        # flood for this signature. Feed that wire truth back into the reflex
        # state so the next same-signature call is captured at birth.
        if command and _single_named_pytest(command):
            try:
                from ctx import reflex

                reflex.note_intervention(
                    ws.root, reflex.command_signature(command), _short
                )
                reflex.note_steer_result(
                    ws.root,
                    command,
                    raw_bytes=size,
                    gated=True,
                    is_error=is_error,
                )
            except Exception:
                pass
        # digest_output has now persisted the raw bytes. A host that cannot
        # substitute gets None — the transcript keeps the raw result, but the
        # artifact exists and is addressable.
        if can_substitute and flavor == "codex":
            outcome = "reported_error" if is_error else "not_reported"
            text = (
                "[ctx output-contained] capture=stored; tool_error=" + outcome + "\n"
                "The tool returned output. Codex uses a post-tool block to display "
                "this digest; that block is not evidence of a tool failure. "
                "Retrieve the captured result below before retrying. If a batch "
                "stopped here, check which later calls ran before resuming.\n" + text
            )
        if host_note:
            text = text.rstrip("\n") + host_note
        _GATE_STATS["decision"] = "replaced" if can_substitute else "captured"
        return text if can_substitute else None
    except Exception as exc:
        _note_guard_failure(ws_root, op="emission_gate", stage="digest", exc=exc)
        _GATE_STATS["decision"] = "gate_failed"
        if not can_substitute:
            # Nothing to fail closed *to*: this host has no substitution field,
            # so the raw result was always going to reach the transcript. The
            # failure is recorded above rather than dressed up as containment.
            return None
        try:
            return _gate_failure_digest(ws_root, tool_name, stdout, stderr, exc)
        except Exception:
            # The net itself tore. Still bounded, still explicit.
            return (
                "[ctx gate-failed]\nCTX_EMISSION_GATE: the output gate failed and "
                "the result could not be bounded safely, so it was withheld."
            )


def _agy_event(payload: dict[str, Any]) -> str:
    """``SessionStart`` / ``PreInvocation`` from an agy 1.5.4+ payload, else ""."""
    ev = payload.get("hookEventName") if isinstance(payload, dict) else None
    return ev if isinstance(ev, str) else ""


def main_session_start(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> session-start`` (and, on
    Antigravity, ``pre-invocation``). Runs the capability surface pre-flight
    ('bound before bloat') once, before the first turn, and injects a bounded
    advisory when the discretionary surface exceeds budget; then drains the
    relay. Fail-open: any error emits a no-op, never a blocked session.

    Antigravity (agy 1.5.5, observed live) has both events. SessionStart
    fires once, before the first model call, and a ``systemMessage`` step
    injected there stays in the conversation: a canary injected that way was
    obeyed in the answer of a later model call. So the once-per-session
    advisory rides SessionStart and is paid for once, and PreInvocation
    (before *every* model call) carries only what the relay has pending, as
    an ephemeral message. A payload without ``hookEventName`` comes from an
    agy older than 1.5.4, which may have no SessionStart; there PreInvocation
    keeps doing both, as before."""
    advisory = ""
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read()
        payload = _normalize_payload(json.loads(raw) if raw.strip() else {})
    except Exception:
        payload = {}
    event = _agy_event(payload) if flavor == "antigravity" else ""
    stage = "pre-invocation" if event == "PreInvocation" else "session-start"
    try:
        ws = _resolve_workspace_root(payload) if event != "PreInvocation" else None
        if ws:
            from ctx.config import load_config
            from ctx.surface import preflight

            from pathlib import Path

            sp = load_config(Path(ws)).surface
            if sp.gate != "off":
                advisory = preflight(
                    ws, max_static_tokens=sp.max_static_tokens,
                    default_profile=sp.default_profile, gateway=sp.gateway,
                    probe=sp.probe, host=_RELAY_HOST.get(flavor, flavor))
    except Exception:
        advisory = ""
    try:
        # A fresh session drains everything, interrupts included: there is no
        # turn in flight to stop, so the backlog is simply what this harness
        # missed while it was not running. On Antigravity, PreInvocation fires
        # before every model call, which makes it that host's relay delivery
        # point mid-session: its PostToolUse contract has exactly one legal
        # output and cannot carry advisory text.
        relay_text, _ = _relay_drain(payload if isinstance(payload, dict) else {},
                                     flavor, stage)
        if relay_text:
            advisory = f"{advisory}\n{relay_text}" if advisory else relay_text
    except Exception:
        pass
    if flavor in ("claude-code", "codex"):
        emitted: dict[str, Any] = (
            {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                    "additionalContext": advisory}}
            if advisory else {"continue": True}
        )
    elif event == "SessionStart":
        # Persistent, once: {"systemMessage": {"systemMessage": text}} is the
        # documented SessionStart step shape, verified live on agy 1.5.5.
        emitted = (
            {"injectSteps": [{"systemMessage": {"systemMessage": advisory}}]}
            if advisory else {}
        )
    else:
        # PreInvocation: ephemeral, so per-call text does not accumulate in
        # the transcript (https://antigravity.google/docs/hooks).
        emitted = (
            {"injectSteps": [{"ephemeralMessage": advisory}]} if advisory else {}
        )
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def main_post_tool_use(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> post-tool-use``. Reads one JSON
    payload on stdin, writes exactly one JSON object on stdout: either a
    no-op ``{}`` or a governor nudge (emission or navigation) in the host
    dialect."""
    replacement: str | None = None
    nudge: str | None = None
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        payload = _normalize_payload(parsed)
        if flavor in ("hermes", "omp", "opencode", "dsh"):
            from ctx.native_hooks import normalize
            payload = normalize(flavor, payload)
    except Exception:
        payload = {}
    # Each stage gets its own block. The nudges are advisory; the emission
    # gate is the safety net, and a failure in an advisory governor must not
    # skip it (in the old single-try form it did — which is exactly the
    # fail-open the gate exists to prevent).
    try:
        # Navigation first: it targets a specific, high-cost wrong pattern;
        # emission is the ambient volume backstop.
        nudge = _navigation_nudge(payload) or _emission_nudge(payload)
    except Exception:
        nudge = None
    try:
        media_hint = _media_post_hint(payload, flavor)
        if media_hint:
            nudge = media_hint + ("\n" + nudge if nudge else "")
    except Exception:
        pass
    try:
        # Pure observation, in its own block for the same reason the nudges
        # are: it influences nothing this call returns, so it must never be
        # able to skip the gate below.
        _record_edit_outcome(payload, flavor)
    except Exception:
        pass
    # The governance table decides whether this result is gated at all:
    # edits and skills are observed, exempt kinds are left alone, and
    # everything else -- including any tool name the table has never seen --
    # goes through the gate. Classification is pure and cannot raise past
    # this block; if it somehow does, the call is gated (fail closed).
    kind = "tool"
    post_route = "output_gate"
    try:
        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        kind = _governed_kind(name, flavor, payload)
        post_route = _gov.stage_routes(kind, DIALECT_CAPS.get(flavor, {}), host=flavor)[1]
    except Exception:
        pass
    _GATE_STATS.clear()
    gated = kind not in _UNGATED_KINDS
    # Media (an image or PDF Read, an isImage shell result, any base64
    # payload) is never substituted into: a text digest there is schema-valid
    # and the host would take it as the image. Left exactly as produced.
    media = None
    # Only where the digest is shaped INTO the tool's own structure could it
    # land in an image field; hosts that substitute plain text gate as ever.
    # KNOWN LIMITATION (documented, unchanged from before this package): an
    # MCP tool returning image content blocks over the budget is replaced by
    # a text digest, so the model does not see the image; the bytes stay
    # retrievable through the digest's address. Media handling is its own
    # follow-up.
    if gated and flavor == "claude-code" and kind != "mcp":
        try:
            media = _media_reason(_tool_response(payload))
        except Exception:
            media = "unreadable"
        if media:
            gated = False
    if gated:
        try:
            # Universal emission gate: replace over-budget output with a digest.
            replacement = _emission_gate(payload, flavor)
        except Exception:
            # _emission_gate is written not to raise; if it somehow does, the
            # result is still not released un-gated.
            replacement = (
                "[ctx gate-failed]\nCTX_EMISSION_GATE: the output gate raised; the "
                "raw result was withheld rather than emitted unbounded."
            )
    # Claude Code ignores a built-in tool's replacement that does not match
    # the tool's output shape, so the digest is fitted into that shape. MCP
    # results are not schema-validated and keep the plain string.
    updated_output: Any = replacement
    shape_context: str | None = None
    shaped = True
    budget = int(_GATE_STATS.get("threshold") or _MAX_TOOL_OUTPUT_BYTES_DEFAULT)
    # The relay is drained before shaping on Claude Code (it always has a
    # channel there), so the nudge and relay text that travel in the same
    # additionalContext are inside the budget the shaped result is held to.
    relay_text = ""
    relay_drained = False
    if flavor in ("claude-code", "codex"):
        try:
            relay_text, _ = _relay_drain(payload, flavor, "post-tool-use")
        except Exception:
            relay_text = ""
        relay_drained = True
    extra = sum(len(x.encode("utf-8", "replace")) + 1 for x in (nudge, relay_text) if x)
    if replacement is not None and flavor == "claude-code" and kind != "mcp":
        try:
            updated_output, shape_context, shaped = _shape_output(
                _tool_response(payload), replacement, budget - extra, _GATE_STATS.get("handle"))
        except Exception:
            updated_output, shape_context, shaped = None, None, False
        if not shaped:
            # Nothing valid fits: replacing would be ignored or exceed the
            # budget, so nothing is sent and the ledger says so.
            updated_output, shape_context = None, None
    try:
        raw = _GATE_STATS.get("raw")
        decision = str(_GATE_STATS.get("decision") or ("observed" if not gated else "no_result"))
        if not gated and kind in ("control", "schema", "media"):
            decision = "exempt"
        if media:
            decision = "media_untouched"
            try:  # what reached the context, unbounded: the result as produced
                raw = _jsize(_tool_response(payload))
            except Exception:
                raw = None
        emitted = raw
        if replacement is not None:
            if not shaped:
                decision = "replace_unshaped"
            elif flavor == "claude-code" and kind != "mcp":
                # The measured size of what is actually sent, including any
                # nudge or relay text that shares the additionalContext.
                sent_context = "\n".join(x for x in (shape_context, nudge, relay_text) if x)
                emitted = _jsize(updated_output) + len(sent_context.encode("utf-8", "replace"))
            else:
                emitted = len(replacement.encode("utf-8", "replace"))
        elif isinstance(emitted, int) and flavor in ("claude-code", "codex"):
            # Under budget the result passes as is, but any nudge or relay
            # text travels with it in additionalContext: count what was sent.
            rider = "\n".join(x for x in (nudge, relay_text) if x)
            emitted += len(rider.encode("utf-8", "replace"))
        if not gated and kind == "skill":
            try:
                out, err = _normalize_tool_response(_tool_response(payload))
                raw = emitted = len(out.encode("utf-8", "replace")) + len(err.encode("utf-8", "replace"))
            except Exception:
                pass
        _note_coverage(_resolve_workspace_root(payload), flavor, "post", payload,
                       kind, post_route, decision, raw, emitted,
                       note=f"media:{media}" if media else None)
    except Exception:
        pass
    # The relay's report/advise delivery point, and it is deliberately drained
    # *here* rather than alongside the nudges: a drain marks its signals
    # delivered, so draining on a host that cannot carry the text would
    # silently consume the message. That is the one failure this whole channel
    # exists to prevent, so the drain is gated on there being a real channel
    # for it on this flavor, and pending signals simply wait for a stage that
    # has one. Interrupts are never delivered here — this stage cannot stop
    # anything, and an interrupt shown as advisory text has been demoted to a
    # suggestion. They wait for pre-tool-use.
    native = flavor in ("hermes", "omp", "opencode", "dsh")
    can_carry = flavor in ("claude-code", "codex") or (native and replacement is not None)
    if can_carry and not relay_drained:
        try:
            relay_text, _ = _relay_drain(payload, flavor, "post-tool-use")
        except Exception:
            relay_text = ""

    if native:
        if relay_text and replacement is not None:
            replacement = f"{replacement}\n\n{relay_text}"
        emitted = {"output": replacement} if replacement is not None else {}
    elif flavor == "claude-code":
        hso: dict[str, Any] = {"hookEventName": "PostToolUse"}
        if replacement is not None and updated_output is not None:
            hso["updatedToolOutput"] = updated_output
        context = "\n".join(x for x in (shape_context, nudge, relay_text) if x)
        if context:
            hso["additionalContext"] = context
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
        context = "\n".join(x for x in (nudge, relay_text) if x)
        if context:
            chso["additionalContext"] = context
        if len(chso) > 1:
            emitted["hookSpecificOutput"] = chso
    else:
        # Antigravity's published PostToolUse contract has exactly one legal
        # output: {}. It can neither replace the result nor attach a nudge, so
        # this hook is observational here — the bytes are still captured into
        # the store above (that is what keeps `ctx get` able to resolve them
        # later), but the transcript is left exactly as the host wrote it.
        # Containment on this host happens at PreToolUse or not at all.
        emitted = {}
    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def main_post_tool_use_failure(flavor: str = "claude-code") -> int:
    """Entry point for ``ctx hook <flavor> post-tool-use-failure``.

    Claude Code fires PostToolUse only for tools that succeeded; a failed
    tool -- an edit whose ``old_string`` matched nothing, a command that
    exited non-zero -- fires PostToolUseFailure instead, whose contract can
    add context but cannot replace the result. So this stage only observes:
    it records the edit outcome (the failure half of the edit ledger, which
    PostToolUse alone can never see) and one coverage row with the failure
    text's size, so failure output shows up as the ungoverned bytes it is.
    Always writes ``{}``; every error is swallowed.
    """
    payload: dict[str, Any] = {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        payload = _normalize_payload(parsed)
        payload.setdefault("hook_event_name", "PostToolUseFailure")
    except Exception:
        payload = {}
    try:
        _record_edit_outcome(payload, flavor)
    except Exception:
        pass
    try:
        name = str(payload.get("tool_name") or payload.get("toolName") or "")
        if name:
            kind = _governed_kind(name, flavor, payload)
            size = len(str(payload.get("error") or "").encode("utf-8", "replace"))
            _note_coverage(_resolve_workspace_root(payload), flavor, "post_failure",
                           payload, kind, "observe", "failed", size, size)
    except Exception:
        pass
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    return 0


def _internal_error_setting(
    ws_root: str | None, policy: dict[str, Any] | None
) -> str:
    """What ``[guard] internal_error`` says: ``"deny"``, ``"allow"``, or
    ``"unknown"`` when we could not find out what the user asked for.

    ``deny`` is checked first and unconditionally: an explicit fail-closed
    choice must never be downgraded by a later error in reading the rest of
    the file. ``unknown`` covers a ctx.toml that is present but unreadable or
    unparseable, an unrecognised value, and a policy load that raised — cases
    where the old code silently answered "allow", i.e. inverted the knob under
    exactly the condition it exists for.
    """
    if policy is None:
        try:
            policy = _load_guard_policy(ws_root)
        except Exception:
            return "unknown"
    try:
        value = str(policy.get("internal_error", "allow"))
        source = str(policy.get("_guard_config", "default"))
    except Exception:
        return "unknown"
    if value == "deny":
        return "deny"
    if source == "failed":
        return "unknown"  # the file exists but we cannot read their choice
    if value == "allow":
        return "allow"
    return "unknown"  # a value we do not model is not a licence to allow


def _internal_error_decision(
    ws_root: str | None,
    policy: dict[str, Any] | None,
    stage: str,
    exc: BaseException,
) -> dict[str, str]:
    """Turn an internal guard error into a decision, with a signal.

    Three answers, never a blanket allow:

    * ``deny``    — configured fail-closed; honoured even if the failure was
      in loading the policy itself.
    * ``allow``   — configured (or defaulted, i.e. no ctx.toml at all)
      availability-safe; SPEC §10.2.
    * ``force_ask`` — we could not determine what was configured. Neither
      silently allowing nor hard-denying is honest here, so the unknown is
      handed to the human, which is this guard's existing idiom for
      "unknown bound" (``unknown_command = "force_ask"``).
    """
    _note_guard_failure(ws_root, op="pre_tool_use", stage=stage, exc=exc)
    setting = _internal_error_setting(ws_root, policy)
    if setting == "deny":
        return _deny("CTX_CONTEXT_GUARD: internal guard error (fail-closed policy)")
    if setting == "unknown":
        return _force_ask(
            "CTX_CONTEXT_GUARD: internal guard error and the configured "
            "[guard] internal_error policy could not be read, so the guard "
            "cannot tell whether you asked to fail open or closed. Fix or "
            "remove ctx.toml, or re-run to confirm this call."
        )
    return dict(DECISION_ALLOW)


def _is_ctx_call(payload: dict[str, Any]) -> bool:
    """Is the tool call about to run a ``ctx`` command?

    An interrupt tells the agent to go read an address. Denying the very
    call that would resolve it deadlocks the agent against the message, so
    ctx invocations are exempt — the interrupt is still delivered, it just
    does not block the one action it is asking for.
    """
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if _is_ctx_mcp_tool(name):
        return True
    return _ran_ctx(payload)


def _apply_relay_interrupt(
    decision: dict[str, Any] | None,
    payload: dict[str, Any],
    flavor: str,
    ws_root: str | None,
) -> dict[str, Any] | None:
    """Turn a queued relay interrupt into a stop at this tool-call boundary.

    This is the strongest form of "interrupt" that exists across hook hosts,
    and the module docstring of :mod:`ctx.stream_rules` explains why there is
    no stronger one: a PreToolUse hook cannot observe assistant tokens, so
    nothing here can abort a request mid-stream. What it *can* do is refuse
    the next tool call and say why, naming the address the sender wants read.

    ``force_ask``, not ``deny``: a peer harness is not a safety authority,
    and a wrong interrupt should cost a confirmation rather than a hard
    refusal. A decision that already denies is left alone — the guard's own
    verdict outranks a peer's.
    """
    if decision is not None and decision.get("decision") == "deny":
        return decision
    if _is_ctx_call(payload):
        # Still drain, so the message is not re-delivered forever; just do
        # not let it block the call that resolves it.
        _relay_drain(payload, flavor, "pre-tool-use", ws_root)
        return decision

    text, signals = _relay_drain(payload, flavor, "pre-tool-use", ws_root)
    if not signals:
        return decision
    reason = (
        "CTX_RELAY_INTERRUPT: another harness asked you to stop and read "
        "this before continuing.\n" + text
    )
    updated = dict(decision or {"decision": "allow"})
    updated["decision"] = "force_ask"
    updated["reason"] = reason
    # A rewrite and an interrupt are contradictory instructions for one call.
    # The interrupt is the newer information, so it wins and the rewrite is
    # dropped rather than smuggled through alongside a stop.
    updated.pop("rewrite", None)
    return updated


def main_pre_tool_use(flavor: str = "antigravity") -> int:
    """Entry point for ``ctx hook <flavor> pre-tool-use``. Reads one JSON
    payload on stdin, writes exactly one JSON decision on stdout.

    Flavors: ``antigravity`` (spec schema) and ``claude-code``
    (hookSpecificOutput schema). Classification logic is identical.

    Each stage (read input, load policy, classify) is its own narrow ``try``
    so an error is attributable instead of collapsing into one blanket allow,
    and so the configured internal-error policy is loaded on a path that
    cannot be skipped by an earlier failure.
    """
    payload: dict[str, Any] = {}
    ws_root: str | None = None
    policy: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None

    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        payload = _normalize_payload(parsed)
        if flavor in ("hermes", "omp", "opencode", "dsh"):
            from ctx.native_hooks import normalize
            payload = normalize(flavor, payload)
    except Exception as exc:
        # A payload we cannot read is still an internal error: route it
        # through the policy rather than assuming it was harmless.
        decision = _internal_error_decision(None, None, "input", exc)

    if decision is None:
        try:
            ws_root = _resolve_workspace_root(payload)
            # Loaded ONCE per hook call and threaded into classify(), which
            # used to load it a second time from the same cache.
            policy = _load_guard_policy(ws_root)
            # Runtime dialect capability, never cached in the repo policy.
            # Only hosts that can replace PostToolUse output may take the
            # speculative-native fast path.
            policy["_output_substitution"] = can_substitute_output(flavor)
            # Which host's native-tool table classifies this call.
            policy["_flavor"] = flavor
        except Exception as exc:
            decision = _internal_error_decision(ws_root, None, "policy", exc)

    if decision is None:
        try:
            decision = classify(payload, policy)
        except Exception as exc:
            decision = _internal_error_decision(ws_root, policy, "classify", exc)

    try:
        decision = _apply_relay_interrupt(decision, payload, flavor, ws_root)
    except Exception:
        # An advisory channel must never be able to change a guard decision
        # by failing. Leave the classified decision exactly as it stands.
        pass

    decision = _rewrite_stays_in_workspace(decision, payload, ws_root)
    if flavor == "claude-code":
        decision = _claude_permission_gate(decision, payload, ws_root, policy)
    elif flavor == "codex":
        decision = _codex_gate(decision, payload, ws_root, policy)

    if flavor in ("hermes", "omp", "opencode", "dsh"):
        from ctx.native_hooks import decision_for
        emitted = decision_for(flavor, decision)
    elif flavor == "codex":
        emitted = _to_codex_schema(decision)
    elif flavor == "claude-code":
        emitted = claude_code_pre_tool_use_output(_to_claude_code_schema(decision), decision)
    else:
        try:
            emitted = _to_antigravity_schema(decision, payload, ws_root)
        except Exception as exc:
            # This host reads a bare {} as a deny, so even a failed emitter
            # answers with an explicit decision, per the internal-error policy.
            try:
                emitted = _to_antigravity_schema(
                    _internal_error_decision(ws_root, policy, "emit", exc))
            except Exception:
                emitted = dict(DECISION_ALLOW)

    # Coverage ledger: observation only, after the decision is final (the
    # Antigravity emitter records on the decision what it actually sent).
    _note_pre_coverage(payload, flavor, ws_root, policy, decision)

    sys.stdout.write(json.dumps(emitted, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0
