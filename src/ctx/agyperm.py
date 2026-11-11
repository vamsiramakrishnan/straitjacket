"""Keep the user's Antigravity permission rules in force across a rewrite.

agy (1.5.5, observed live) checks its permission rules against the command
that will actually run. When the hook rewrites ``cat big.log`` into
``ctx run --passthrough -- cat big.log`` the host sees ``ctx run ...``, not
the command the model proposed. A rule that pre-approves ctx would then
approve every rewritten command, including ones the user's own rules would
have asked about or denied.

This module does **not** re-implement agy's rule engine. It answers
``allow`` only where it can prove agy would have run the *original* command
without asking, or where no rule could approve the *rewritten* command (so
agy asks about it itself). Everything else is ``force_ask``, or ``deny``
where a deny rule could match. A wrong ``allow`` runs something the user
forbade; a wrong ``force_ask`` costs one keystroke.

The documented grammar (https://antigravity.google/docs/permissions/, saved
2026-11-11): Deny > Ask > Allow; ``command(prefix)`` matches word by word,
literally; ``command(regex:p)`` anchors each whitespace-separated token
(``^(?:tok)$``); ``command(*)`` matches everything; command or process
substitution, backticks, arithmetic, brace expansion, non-literal command
names, descriptor or network redirections and exec-capable tool flags
(``git -c``, ``tar --to-command``) disable prefix matching, so only a
full-line match allows; ``timeout``, ``nohup``, ``nice`` and ``env`` are
transparent wrappers whose inner command is evaluated on its own.

``rewrite_verdict`` answers, in this order:

1. ``force_ask`` when any input is beyond what it analyses: a command over
   ``MAX_COMMAND``, a rules file over ``MAX_RULES_FILE`` or unreadable, an
   ``unsandboxed(...)`` or malformed ``command(...)`` rule, rules passed
   inline at launch (``--settings`` on an ancestor process), or no ``HOME``.
2. ``deny`` when any deny rule could match the original anywhere in the line
   (any window of its words, any wrapper's inner command, the raw text).
3. ``force_ask`` when any ask rule could match the same way.
4. ``allow`` when no allow rule, from any source, could match the rewritten
   command: agy then asks about ``ctx run ...`` itself.
5. ``allow`` when the original is *proven* allowed by a rule from the global
   sources (the CLI ``settings.json`` or the shared ``config.json``; a
   per-project file cannot be tied to the active project, so its allow rules
   prove nothing): a literal rule equal to the whole line; or, for a
   *plain* command (words of ``[A-Za-z0-9_./:@%+,=-]`` only, no option word,
   no wrapper or shell as the program, no assignment), ``command(*)``, a
   literal rule whose words are a prefix of the command's, or a regex rule
   with exactly as many tokens as the command, each fully matching.
6. ``force_ask`` otherwise.

Enterprise-managed rules are not visible to this module. They can only add
deny or ask rules on top, which agy still applies to the rewritten command,
or allow rules, which this module never relies on.

Imported lazily, only on the path that emits an Antigravity rewrite of a
shell command; it imports only ``json``, ``os`` and ``re`` (pinned by
tests/test_hook_hot_path.py).
"""

from __future__ import annotations

import json
import os
import re

#: Longest command line analysed; anything longer is asked about.
MAX_COMMAND = 4096
#: Largest rules file read; a larger one is treated as unreadable.
MAX_RULES_FILE = 256 * 1024
#: Most rules considered per list; more is treated as unreadable.
MAX_RULES = 2000

_RULE_RE = re.compile(r"^\s*([A-Za-z_]+)\s*\((.*)\)\s*$", re.DOTALL)
#: A plain word: no quoting, expansion, globbing, redirection or control.
_PLAIN_WORD = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")
_PLAIN_LINE = re.compile(r"^[A-Za-z0-9_./:@%+,= -]+\Z")
#: Words the liberal matcher sees: everything between shell punctuation.
_LOOSE_WORD = re.compile(r"[^\s;&|()<>`$\"'{}\\]+")
#: Programs that run another command or interpret code: never "plain".
_NOT_PLAIN_PROGRAMS = frozenset({
    "env", "timeout", "nohup", "nice", "sudo", "doas", "su", "command", "exec",
    "builtin", "eval", "xargs", "sh", "bash", "zsh", "dash", "ksh", "fish",
    "time", "watch", "stdbuf", "ionice", "chroot", "nsenter", "unshare",
    "setsid", "flock", "parallel", "script",
})

ASK_REASON = ("CTX_CONTEXT_GUARD: rewritten to run through ctx for bounded capture. "
              "agy checks its permission rules against the rewritten command, and ctx "
              "cannot prove your rules allow the original, so this asks instead.")
DENY_REASON = ("CTX_CONTEXT_GUARD: one of your Antigravity deny rules could match this "
               "command, so ctx will not run it either.")


def _global_files(home: str) -> list[str]:
    base = os.path.join(home, ".gemini")
    return [os.path.join(base, "antigravity-cli", "settings.json"),
            os.path.join(base, "config", "config.json")]


def _project_files(home: str) -> list[str]:
    proj = os.path.join(home, ".gemini", "config", "projects")
    try:
        names = sorted(os.listdir(proj))
    except OSError:
        return []
    return [os.path.join(proj, n) for n in names if n.endswith(".json")]


def _inline_settings() -> bool:
    """Was agy launched with rules passed inline (``--settings``)? Looked up
    on the ancestor processes; where that cannot be read, assume it was not
    (agy's own check of the rewritten command still applies to inline rules)."""
    try:
        pid = os.getppid()
        for _ in range(6):
            if pid <= 1:
                return False
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read(65536).split(b"\0")
            if any(a == b"--settings" or a.startswith(b"--settings=") for a in argv):
                return True
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read(4096)
            pid = int(stat[stat.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return False
    return False


def load_rules(home: str | None = None) -> tuple[dict[str, dict[str, list[str]]], bool]:
    """``({"global"|"project": {"allow"|"deny"|"ask": [bodies]}}, unknown)``.

    ``unknown`` is True when any source could not be fully understood.
    """
    home = home if home is not None else os.environ.get("HOME", "")
    rules = {scope: {"allow": [], "deny": [], "ask": []} for scope in ("global", "project")}
    if not home:
        return rules, True
    unknown = False
    for scope, files in (("global", _global_files(home)), ("project", _project_files(home))):
        for path in files:
            try:
                if os.path.getsize(path) > MAX_RULES_FILE:
                    unknown = True
                    continue
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, UnicodeDecodeError):
                unknown = True
                continue
            if not isinstance(data, dict):
                unknown = True
                continue
            for key in ("permissions", "permission"):
                block = data.get(key)
                if block is None:
                    continue
                if not isinstance(block, dict):
                    unknown = True
                    continue
                for verdict in ("allow", "deny", "ask"):
                    entries = block.get(verdict)
                    if entries is None:
                        continue
                    if not isinstance(entries, list) or len(entries) > MAX_RULES:
                        unknown = True
                        continue
                    for entry in entries:
                        m = _RULE_RE.match(entry) if isinstance(entry, str) else None
                        if not m:
                            unknown = True
                            continue
                        if m.group(1) == "unsandboxed":
                            unknown = True  # a command rule form this module does not model
                        elif m.group(1) == "command":
                            body = m.group(2).strip()
                            if not body or len(body) > MAX_COMMAND:
                                unknown = True
                            else:
                                rules[scope][verdict].append(body)
    return rules, unknown


# ------------------------------------------------------------- matching
def _regex_tokens(body: str) -> list[re.Pattern] | None:
    try:
        return [re.compile(f"^(?:{t})$") for t in body[len("regex:"):].split()]
    except re.error:
        return None


def _could_match(body: str, line: str) -> bool:
    """Could rule ``body`` apply to ``line`` under *any* reading? Over-approximates:
    any window of the line's words, any inner command, the raw text."""
    if body == "*":
        return True
    words = _LOOSE_WORD.findall(line)
    bases = [os.path.basename(w) or w for w in words]
    if body.startswith("regex:"):
        pats = _regex_tokens(body)
        if pats is None:
            return True
        try:
            if re.fullmatch(body[len("regex:"):], line):
                return True  # the whole raw line, as agy reads it under constructs
        except re.error:
            return True
        if not pats:
            return True
        n = len(pats)
        for seq in (words, bases):
            for i in range(len(seq) - n + 1):
                if all(p.match(seq[i + j]) for j, p in enumerate(pats)):
                    return True
        return False
    want = _LOOSE_WORD.findall(body)
    if not want:
        return True
    if body == line:
        return True
    head = os.path.basename(want[0]) or want[0]
    n = len(want)
    for i in range(len(words) - n + 1):
        # ``*`` inside a literal rule is read as "any word": over-matching
        # a deny or ask rule is the safe direction.
        if ((words[i] == want[0] or bases[i] == head or want[0] == "*")
                and all(w == "*" or w == words[i + 1 + j] for j, w in enumerate(want[1:]))):
            return True
    return False


def _plain_words(line: str) -> list[str] | None:
    """The words of a plain single command, or None if it is anything else."""
    if not _PLAIN_LINE.match(line):
        return None  # newlines, tabs and every shell metacharacter end here
    words = line.split(" ")
    words = [w for w in words if w]
    if not words or not all(_PLAIN_WORD.match(w) for w in words):
        return None
    if "=" in words[0] or os.path.basename(words[0]) in _NOT_PLAIN_PROGRAMS:
        return None
    if any(w.startswith("-") for w in words[1:]):
        return None  # an option may be one that runs a subcommand (git -c ...)
    return words


def _proves_allowed(body: str, line: str) -> bool:
    """Does allow rule ``body`` certainly allow ``line``?"""
    if body == line:
        return True  # a full-line literal match allows under every reading
    words = _plain_words(line)
    if words is None:
        return False
    if body == "*":
        return True
    if body.startswith("regex:"):
        pats = _regex_tokens(body)
        return bool(pats) and len(pats) == len(words) and all(
            p.match(w) for p, w in zip(pats, words))
    want = body.split()
    return bool(want) and all(_PLAIN_WORD.match(w) for w in want) and words[:len(want)] == want


def rewrite_verdict(original: str, rewritten: str, home: str | None = None) -> tuple[str, str]:
    """``(decision, reason)`` for sending ``rewritten`` in place of ``original``:
    ``allow`` (reason empty), ``force_ask`` or ``deny``."""
    line = (original or "").strip()
    if not line or len(line) > MAX_COMMAND or len(rewritten) > 4 * MAX_COMMAND:
        return "force_ask", ASK_REASON
    rules, unknown = load_rules(home)
    every = {v: rules["global"][v] + rules["project"][v] for v in ("allow", "deny", "ask")}
    if any(_could_match(b, line) for b in every["deny"]):
        return "deny", DENY_REASON
    if unknown or _inline_settings():
        return "force_ask", ASK_REASON
    if any(_could_match(b, line) for b in every["ask"]):
        return "force_ask", ASK_REASON
    if not any(_could_match(b, rewritten) for b in every["allow"]):
        return "allow", ""  # nothing pre-approves the rewrite: agy asks itself
    if any(_proves_allowed(b, line) for b in rules["global"]["allow"]):
        return "allow", ""
    return "force_ask", ASK_REASON
