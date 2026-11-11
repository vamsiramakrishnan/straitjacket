"""Keep Claude Code's permission rules in force across a rewrite.

Observed live on Claude Code 2.1.322 (evals receipts under the scratch
liveprobe xA..xF): when a PreToolUse hook returns ``permissionDecision:
"allow"`` with ``updatedInput``, the call runs -- past a user ``deny`` rule on
the original program, past an ``ask`` rule, and with no prompt in default
mode. With ``updatedInput`` and NO decision, the host evaluates its own rules
against the REWRITTEN command only. So a rewrite answered with "allow" makes a
command more permitted than the user's own settings would have made it.

This module decides what a Claude Code rewrite of a shell command may
answer. It never re-implements the host's whole engine; it models the
documented grammar (https://code.claude.com/docs/en/permissions, saved
2026-11-11 as hosts/docs/cc_permissions.md) only as far as needed to answer
in the safe direction:

1. ``deny``  -- a deny rule from any source this module can read could match
   any part of the ORIGINAL command (liberal: every subcommand, nested ones,
   past wrappers and assignments; ``Read``/``Edit`` deny rules on the file
   arguments of file commands).
2. ``ask``   -- the same for an ask rule.
3. ``allow`` -- only when nothing it cannot see could matter and either the
   session's ``permission_mode`` is ``bypassPermissions`` (no prompt would be
   shown anyway), or every subcommand of a command with no shell construct is
   PROVEN allowed: by an allow rule matched exactly as documented (no wrapper
   stripping, no assignment), or by membership in a conservative subset of
   the host's built-in read-only commands with plain arguments inside the
   working directory.
4. ``none``  -- everything else: the rewrite goes out with NO decision and the
   host makes the approval decision itself on the wrapped form.

Never "allow" when any source is unseen or not understood: an unreadable or
oversized settings file, ``--disallowedTools``/``--setting-sources`` or an
unparseable ``--settings`` on the launching command line (or a platform where
the command line cannot be read), managed settings that cannot be read, or a
project/local settings file whose allow rules depend on workspace trust (those
allow rules are never used as proof; their deny and ask rules always count).
Approvals granted during the session are invisible; they only add allows,
which this module never relies on.

Hot path discipline: imported lazily, only when a Claude Code rewrite is
emitted; imports json, os and re only. The parsed rule set is cached under
the ledger directory, keyed by the content hash of every source.
"""

from __future__ import annotations

import json
import os
import re

MAX_COMMAND = 10000           # the host itself prompts for anything longer
MAX_SETTINGS = 512 * 1024
MAX_RULES = 2000

#: Bash input fields a parameter rule (``Bash(run_in_background:true)``) can name.
_BASH_PARAMS = frozenset({"description", "timeout", "run_in_background", "dangerouslyDisableSandbox"})
#: Wrappers the host strips before matching (permissions page, "Wrappers").
_WRAPPERS = frozenset({"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin",
                       "noglob", "xargs"})
#: A conservative subset of the host's built-in read-only commands. `find`,
#: `sort`, `sed`, `git` and `cd` are left out: their read-only status depends
#: on flags, globs or a directory change this module does not model.
#: Commands Claude Code runs without asking in default mode with no rules:
#: each one checked live on 2.1.322 with no hook installed (ls, cat, head,
#: tail, wc, pwd, echo, grep -rn, stat, du -a, diff, which, git status /
#: diff / log / show, find -name ran; `cat ../outside.txt` and
#: `find ... -delete` asked). Never wider than that: installing ctx must not
#: approve what the host would ask about. `sed -n` also ran but is not in the
#: host's documented read-only set, so it is left to the host.
READ_ONLY = frozenset({"cat", "head", "tail", "wc", "ls", "pwd", "echo", "grep", "stat",
                       "du", "diff", "which"})
READ_ONLY_GIT = frozenset({"status", "diff", "log", "show"})
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint",
                           "-fprint0", "-fprintf", "-fls"})
#: Commands whose file arguments Read/Edit rules apply to (liberal).
_FILE_COMMANDS = frozenset({"cat", "head", "tail", "sed", "tee", "grep", "egrep", "fgrep",
                            "less", "more", "wc", "diff", "stat", "awk", "cut", "sort",
                            "nl", "od", "xxd", "strings", "file", "cp", "mv", "rm", "touch",
                            "du", "ls", "find", "rg", "ag", "ack", "tree", "md5sum",
                            "sha256sum", "base64", "hexdump", "jq", "bat", "ctx", "tac",
                            "uniq", "paste", "comm", "cmp", "zcat", "gzip", "tar"})
_PLAIN = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")
_SEPARATORS = re.compile(r"\|&|&&|\|\||[;|&\n]")
_STRUCTURE = re.compile(r"[;&|\n()`{}]|\$\(|<\(|>\(")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RULE = re.compile(r"^\s*([^()\s]+)\s*(?:\((.*)\))?\s*$", re.DOTALL)
_PARAM = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", re.DOTALL)

CLI_FLAGS_UNKNOWN = ("--setting-sources",)
CLI_DENY_FLAGS = ("--disallowedTools", "--disallowed-tools")

DENY_REASON = ("CTX_CONTEXT_GUARD: one of your Claude Code deny rules matches this command "
               "(checked against the command as you wrote it), so ctx will not run it.")
ASK_REASON = ("CTX_CONTEXT_GUARD: one of your Claude Code ask rules matches this command, "
              "so it is put to you; it would run through ctx for bounded capture.")


# ------------------------------------------------------------------ grammar
def parse_rule(rule: object):
    """``(tool, specifier-or-None)`` or None when the rule is not understood."""
    if not isinstance(rule, str):
        return None
    m = _RULE.match(rule)
    if not m:
        return None
    return m.group(1), m.group(2)


def _tool_matches(tool: str, name: str, *, glob: bool) -> bool:
    if tool == name:
        return True
    if glob and "*" in tool:
        return re.fullmatch(".*".join(re.escape(p) for p in tool.split("*")), name) is not None
    return False


def bash_spec_matches(spec: str | None, text: str) -> bool:
    """Does a ``Bash(spec)`` rule match the command text, as documented?

    ``*`` matches any text (spaces included); a trailing `` *`` that is the
    only wildcard also matches the bare command; ``:*`` at the end is the
    same as `` *``; no ``*`` means an exact match."""
    if spec is None or spec.strip() in ("", "*"):
        return True
    if spec.endswith(":*"):
        spec = spec[:-2] + " *"
    rx = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", spec))
    if re.fullmatch(rx, text, re.DOTALL):
        return True
    return spec.endswith(" *") and spec.count("*") == 1 and text == spec[:-2]


def _param_rule(spec: str | None):
    if spec is None:
        return None
    m = _PARAM.match(spec)
    if m and m.group(1) in _BASH_PARAMS:
        return m.group(1), m.group(2).strip()
    return None


# ---------------------------------------------------------------- the command
def _words(text: str) -> list[str]:
    try:
        import shlex

        return shlex.split(text, comments=False)
    except ValueError:
        return text.split()


def _uncomment(text: str) -> str:
    try:
        import shlex

        return " ".join(shlex.split(text, comments=True))
    except ValueError:
        return text.split("#", 1)[0]


_REDIRECT = re.compile(r"^(?:\d*>>?|\d*<|&>>?|\d*>&\d*|<<<?)(.*)$")


def _no_redirects(words: list[str]) -> list[str]:
    """Words without redirection operators and their targets: the host
    matches the command text and checks redirect targets separately."""
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        m = _REDIRECT.match(w)
        if m:
            skip = not m.group(1)  # `> file` (target is the next word) vs `>file`
            continue
        out.append(w)
    return out


def _strip(words: list[str]) -> list[str]:
    """Leading assignments and the host's stripped wrappers, liberally."""
    out = list(words)
    for _ in range(8):
        while out and _ASSIGN.match(out[0]):
            out = out[1:]
        if not out or out[0] not in _WRAPPERS:
            break
        head = out[0]
        out = out[1:]
        if head == "timeout":
            while out and out[0].startswith("-"):
                out = out[1:]
            if out:
                out = out[1:]  # the duration
        elif head in ("nice", "stdbuf"):
            while out and out[0].startswith("-"):
                out = out[2:] if out[0] in ("-n",) else out[1:]
    return out


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def liberal_segments(command: str, _depth: int = 0) -> list[str]:
    """Every piece a deny or ask rule could apply to: subcommands split on
    every operator and nesting construct, raw and with wrappers stripped.

    Safe direction beyond the documented matching: a path-qualified program
    also appears under its bare name (``/usr/bin/rm`` as ``rm``), and the
    script of ``bash -c '...'`` is searched as a command of its own, so a rule
    on the program still catches it there."""
    pieces = re.split(r"\|&|&&|\|\||[;|&\n(){}`]|\$\(|<\(|>\(", command)
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        out.append(piece)
        for words in (_words(piece), _words(_uncomment(piece))):
            words = _no_redirects(words)
            if words:
                out.append(" ".join(words))
                stripped = _strip(words)
                if stripped:
                    out.append(" ".join(stripped))
                    if "/" in stripped[0]:
                        base = stripped[0].rsplit("/", 1)[1]
                        if base:
                            out.append(" ".join([base, *stripped[1:]]))
                    prog = stripped[0].rsplit("/", 1)[-1]
                    if prog in _SHELLS and _depth < 3:
                        for i, w in enumerate(stripped[1:-1], 1):
                            if w.startswith("-") and "c" in w[1:]:
                                out.extend(liberal_segments(stripped[i + 1], _depth + 1))
                                break
    out.append(command.strip())
    return out


def strict_segments(command: str) -> list[list[str]] | None:
    """Subcommands of a command whose only construct is a plain separator
    (``;``, ``&&``, ``||``, ``|``, newline), each made of plain words only;
    None when anything else is present."""
    if any(ch in command for ch in "()`{}$<>\\'\"*?[]~#!") or "|&" in command:
        return None
    segs = []
    for part in _SEPARATORS.split(command):
        words = part.split()
        if not words or not all(_PLAIN.match(w) for w in words):
            return None
        segs.append(words)
    return segs or None


# ------------------------------------------------------------------- sources
def _ancestor_argvs(limit: int = 8) -> list[list[str]] | None:
    """Command lines of the processes above the hook, or None if unreadable."""
    out = []
    try:
        pid = os.getppid()
        for _ in range(limit):
            if pid <= 1:
                break
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read(65536)
            out.append([a.decode("utf-8", "replace") for a in raw.split(b"\0") if a])
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read(4096)
            pid = int(stat[stat.rindex(b")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return out


def _split_rule_list(value: str) -> list[str]:
    """``--disallowedTools`` values: comma/space separated, parentheses kept."""
    out, buf, depth = [], [], 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (ch == "," or ch.isspace()):
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _cli_rules(argvs) -> tuple[list[dict], list[str], bool]:
    """``(settings documents, extra deny rules, unknown)`` from the command
    line that launched Claude Code."""
    if argvs is None:
        return [], [], True
    docs, deny, unknown = [], [], False
    for argv in argvs:
        if not any("claude" in os.path.basename(a) for a in argv[:2]):
            continue
        i = 0
        while i < len(argv):
            a = argv[i]
            name, _, inline = a.partition("=")
            if name in CLI_FLAGS_UNKNOWN:
                unknown = True
            if name == "--settings":
                value = inline or (argv[i + 1] if i + 1 < len(argv) else "")
                doc = _load_settings_value(value)
                if doc is None:
                    unknown = True
                else:
                    docs.append(doc)
            if name in CLI_DENY_FLAGS:
                vals = [inline] if inline else []
                j = i + 1
                while not inline and j < len(argv) and not argv[j].startswith("-"):
                    vals.append(argv[j])
                    j += 1
                for v in vals:
                    deny.extend(_split_rule_list(v))
            i += 1
    return docs, deny, unknown


def _load_settings_value(value: str):
    value = value.strip()
    try:
        if value.startswith("{"):
            data = json.loads(value)
        else:
            if os.path.getsize(value) > MAX_SETTINGS:
                return None
            with open(value, encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _settings_files(root: str | None, cwd: str | None) -> list[tuple[str, str]]:
    """``(path, scope)`` for every settings file Claude Code could load.
    Scopes: ``user`` and ``managed`` (allow rules usable as proof), ``other``
    (deny/ask only: project and local files, whose allow rules depend on
    workspace trust and git state this module does not establish)."""
    home = os.environ.get("HOME", "")
    config = os.environ.get("CLAUDE_CONFIG_DIR") or (os.path.join(home, ".claude") if home else "")
    files: list[tuple[str, str]] = []
    if config:
        files.append((os.path.join(config, "settings.json"), "user"))
        files.append((os.path.join(config, "settings.local.json"), "other"))
    seen = set()
    for start in (cwd, root):
        d = os.path.abspath(start) if start else None
        for _ in range(16):
            if not d or d in seen:
                break
            seen.add(d)
            files.append((os.path.join(d, ".claude", "settings.json"), "other"))
            files.append((os.path.join(d, ".claude", "settings.local.json"), "other"))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    for base in ("/etc/claude-code", "/Library/Application Support/ClaudeCode"):
        files.append((os.path.join(base, "managed-settings.json"), "managed"))
        dropin = os.path.join(base, "managed-settings.d")
        try:
            names = sorted(os.listdir(dropin))
        except FileNotFoundError:
            names = []
        except OSError:
            files.append((dropin, "unreadable"))
            names = []
        files.extend((os.path.join(dropin, n), "managed") for n in names if n.endswith(".json"))
    return files


def load_rules(root: str | None, cwd: str | None, argvs=None) -> dict:
    """Every rule this module can see, plus whether anything is unknown.

    ``{"deny": [...], "ask": [...], "allow": [...] (proof-grade only),
    "unknown": bool, "reasons": [...]}``."""
    rules = {"deny": [], "ask": [], "allow": [], "unknown": False, "reasons": [],
             "block_outside": False}
    managed_only = False
    docs: list[tuple[dict, str]] = []
    for path, scope in _settings_files(root, cwd):
        if scope == "unreadable":
            rules["unknown"] = True
            rules["reasons"].append(f"unreadable {path}")
            continue
        try:
            if os.path.getsize(path) > MAX_SETTINGS:
                rules["unknown"] = True
                rules["reasons"].append(f"oversized {path}")
                continue
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeDecodeError):
            rules["unknown"] = True
            rules["reasons"].append(f"unreadable {path}")
            continue
        if not isinstance(data, dict):
            rules["unknown"] = True
            continue
        docs.append((data, scope))
        perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        if perms.get("blockReadsOutsideWorkingDirectories") is True or \
                data.get("blockReadsOutsideWorkingDirectories") is True:
            rules["block_outside"] = True
        if scope == "managed" and perms.get("allowManagedPermissionRulesOnly") is True:
            managed_only = True
        if data.get("allowManagedPermissionRulesOnly") is True and scope == "managed":
            managed_only = True
    cli_docs, cli_deny, cli_unknown = _cli_rules(_ancestor_argvs() if argvs is None else argvs)
    if cli_unknown:
        rules["unknown"] = True
        rules["reasons"].append("launch command line not fully readable or not modelled")
    docs.extend((d, "cli") for d in cli_docs)
    rules["deny"].extend(cli_deny)
    for data, scope in docs:
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        for kind in ("deny", "ask", "allow"):
            vals = perms.get(kind, [])
            if not isinstance(vals, list) or len(vals) > MAX_RULES:
                rules["unknown"] = True
                continue
            good = []
            for v in vals:
                if parse_rule(v) is None:
                    # Not understood: it could be anything, so nothing is proven.
                    rules["unknown"] = True
                    rules["reasons"].append(f"rule not understood: {str(v)[:60]!r}")
                else:
                    good.append(v)
            if kind == "allow":
                if scope == "managed" or (scope in ("user", "cli") and not managed_only):
                    rules["allow"].extend(good)
            else:
                rules[kind].extend(good)
    return rules


# ------------------------------------------------------------------ matching
def _path_pattern_matches(pattern: str, arg: str, cwd: str | None) -> bool:
    """Liberal match of a Read/Edit path pattern against a command argument."""
    if not pattern or pattern in ("*", "**"):
        return True
    absolute = os.path.normpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(arg)))
    rel = arg[2:] if arg.startswith("./") else arg
    p = pattern
    if p.startswith("~/"):
        p, target = os.path.expanduser(p), absolute
    elif p.startswith("//"):
        p, target = p[1:], absolute
    elif p.startswith("/") or p.startswith("./"):
        # Prefix removal, not lstrip: lstrip("./") turned "./.env" into "env".
        p, target = (p[2:] if p.startswith("./") else p[1:]), rel
    else:
        target = rel
    rx = (re.escape(p).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
          .replace(r"\*", "[^/]*").replace(r"\?", "[^/]"))
    candidates = {target, rel, absolute, os.path.basename(arg), os.path.normpath(rel)}
    is_dir = False
    try:
        real = os.path.realpath(absolute)  # a link is judged by where it leads
        base = os.path.realpath(cwd or os.getcwd())
        candidates.add(real)
        if real.startswith(base.rstrip(os.sep) + os.sep):
            candidates.add(os.path.relpath(real, base))
        is_dir = os.path.isdir(real)
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        if re.fullmatch(rx, candidate) or re.fullmatch(rx + "(?:/.*)?", candidate):
            return True
    if is_dir:
        # A directory reaches everything under it: refuse when the rule's
        # fixed prefix lies inside it (or it inside that prefix).
        fixed = re.split(r"[*?\[]", p, maxsplit=1)[0].rstrip("/")
        if not fixed:
            return True  # the rule reaches every path
        fixed_abs = fixed if fixed.startswith("/") else os.path.normpath(
            os.path.join(os.path.realpath(cwd or os.getcwd()), fixed))
        for d in (real, absolute):
            d = d.rstrip("/")
            if (fixed_abs + "/").startswith(d + "/") or (d + "/").startswith(fixed_abs + "/"):
                return True
    return False


def _rule_hits(rule: str, segments: list[str], file_args: list[str], tool_input: dict,
               cwd: str | None) -> bool:
    parsed = parse_rule(rule)
    if parsed is None:
        return False  # filtered at load time, where it marks the rules unknown
    tool, spec = parsed
    if _tool_matches(tool, "Bash", glob=True):
        param = _param_rule(spec)
        if param is not None:
            name, value = param
            if name not in tool_input:
                return False
            got = tool_input[name]
            got = str(got).lower() if isinstance(got, bool) else str(got)
            return bash_spec_matches(value, got)
        return any(bash_spec_matches(spec, s) for s in segments)
    if tool in ("Read", "Edit") and file_args:
        if spec is None:
            return True
        return any(_path_pattern_matches(spec.strip(), a, cwd) for a in file_args)
    return False


def _file_args(command: str) -> list[str]:
    out = []
    for piece in re.split(r"\|&|&&|\|\||[;|&\n(){}`]|\$\(|<\(|>\(", command):
        words = _strip(_words(piece.strip()))
        if words and os.path.basename(words[0]) in _FILE_COMMANDS:
            for w in words[1:]:
                if not w.startswith("-"):
                    out.append(w.split(":", 1)[1] if w.startswith("repo:") else w)
                elif "=" in w:
                    out.append(w.split("=", 1)[1])  # --files0-from=.env
                elif len(w) > 2 and not w.startswith("--"):
                    out.append(w[2:])  # -f.env (liberal: refusal only)
        for m in re.finditer(r"[<>]\s*([^\s<>|;&]+)", piece):
            out.append(m.group(1))
    return out


def _inside(arg: str) -> bool:
    return not (arg.startswith("/") or arg.startswith("~") or ".." in arg.split("/"))


def _proven(words: list[str], allow: list[str]) -> bool:
    text = " ".join(words)
    for rule in allow:
        parsed = parse_rule(rule)
        if parsed is None or parsed[0] != "Bash" or _param_rule(parsed[1]) is not None:
            continue
        if bash_spec_matches(parsed[1], text):
            return True
    prog = words[0]
    inside = all(_inside(w) for w in words[1:] if not w.startswith("-"))
    if prog in READ_ONLY and inside:
        return True
    if prog == "git" and len(words) > 1 and words[1] in READ_ONLY_GIT and inside and not any(
            w.startswith(("--output", "--ext-diff", "--textconv")) for w in words[2:]):
        return True
    if prog == "find" and inside and not any(w in _FIND_ACTIONS for w in words[1:]):
        return True
    return False


def refusal_verdict(command: str, payload: dict, root: str | None, rules: dict | None = None,
                    ) -> tuple[str, str]:
    """``("deny"|"ask"|"none", why)``: would a rule ctx can see deny or ask
    about the ORIGINAL ``command``? Refusal only: this module never proves a
    command allowed (Claude Code's own engine resolves paths, follows links
    and reads settings ctx cannot; an approval from ctx would replace it).
    Liberal in the safe direction: links are resolved, a directory argument
    matches rules on what is under it, ``--opt=FILE`` and ``-fFILE`` values
    count as paths, and with ``blockReadsOutsideWorkingDirectories`` a path
    that resolves outside the project is refused."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else root
    if not isinstance(command, str) or len(command) > MAX_COMMAND:
        return "ask", "command too long to analyse"
    if rules is None:
        rules = cached_rules(root, cwd)
    segments = liberal_segments(command)
    files = list(dict.fromkeys(a for s in segments for a in _file_args(s)))
    if any(_rule_hits(r, segments, files, tool_input, cwd) for r in rules["deny"]):
        return "deny", "deny rule"
    if rules.get("block_outside") and any(_outside(f, cwd, root) for f in files):
        return "deny", "blockReadsOutsideWorkingDirectories"
    if any(_rule_hits(r, segments, files, tool_input, cwd) for r in rules["ask"]):
        return "ask", "ask rule"
    if rules["unknown"]:
        return "none", "; ".join(rules["reasons"]) or "unknown source"
    return "none", "no visible rule refuses it"


#: Kept for callers of the old name; it never answers "allow" any more.
rewrite_verdict = refusal_verdict


def _outside(arg: str, cwd: str | None, root: str | None) -> bool:
    base = cwd or root or os.getcwd()
    try:
        real = os.path.realpath(os.path.join(base, os.path.expanduser(arg)))
    except (OSError, ValueError):
        return True
    for top in {os.path.realpath(base), os.path.realpath(root or base)}:
        if real == top or real.startswith(top.rstrip(os.sep) + os.sep):
            return False
    return True


# -------------------------------------------------------------------- cache
def cached_rules(root: str | None, cwd: str | None) -> dict:
    """``load_rules``, cached under the ledger directory by the content hash
    of every source (files and launch command line). Fail-open to a fresh
    load; a cache that cannot be written changes nothing."""
    import hashlib

    h = hashlib.sha256()
    for path, scope in _settings_files(root, cwd):
        h.update(path.encode() + b"\0" + scope.encode() + b"\0")
        try:
            if os.path.getsize(path) <= MAX_SETTINGS:
                with open(path, "rb") as fh:
                    h.update(fh.read())
        except OSError:
            h.update(b"-")
    argvs = _ancestor_argvs()
    h.update(json.dumps(argvs).encode())
    key = h.hexdigest()
    cache_path = None
    if root:
        from ctx.sessiondir import session_reads_dir

        cache_path = session_reads_dir(root, "ccperm-cache.json")
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("key") == key and isinstance(cached.get("rules"), dict):
                return cached["rules"]
        except (OSError, ValueError, AttributeError):
            pass
    rules = load_rules(root, cwd, argvs)
    if cache_path:
        try:
            os.makedirs(os.path.dirname(cache_path), mode=0o700, exist_ok=True)
            tmp = cache_path + f".{os.getpid()}.tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                      "w", encoding="utf-8") as fh:
                json.dump({"key": key, "rules": rules}, fh)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return rules


# ------------------------------------------------------------------- doctor
WHOLESALE = ("Bash", "Bash(*)", "Bash(ctx:*)", "Bash(ctx *)", "Bash(ctx*)", "Bash(ctx run:*)",
             "Bash(ctx run *)", "Bash(ctx run*)")


def wholesale_wrapper_allows(root: str | None) -> list[str]:
    """Allow rules that approve every wrapped command (``ctx run ...``)."""
    found = []
    for path, _scope in _settings_files(root, root):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        allow = perms.get("allow") if isinstance(perms, dict) else None
        for rule in allow if isinstance(allow, list) else []:
            parsed = parse_rule(rule)
            if not parsed or not _tool_matches(parsed[0], "Bash", glob=True):
                continue
            spec = parsed[1]
            if spec is None or (_param_rule(spec) is None and bash_spec_matches(
                    spec, "ctx run --passthrough --shell -- 'rm -rf x'")):
                found.append(f"{path}: {rule}")
    return found
