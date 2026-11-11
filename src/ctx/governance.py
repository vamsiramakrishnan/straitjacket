"""The governance table: which route every native tool call takes, per host.

"Prefer ctx for everything" is a claim about every tool call an agent makes,
so it has to be a declared table rather than a property scattered across a
classifier, three matcher strings and a set of plugin templates. This module
is that table. ``ctx.hook`` reads it on every call (so classification cannot
drift from it), the static registrations are pinned against it by
``tests/test_governance.py``, and ``ctx coverage`` prints it.

A hook cannot do everything. Verified against the published hook contracts,
the routes that exist are:

=============  ============================================================
rewrite        PreToolUse replaces the input in place (``updatedInput``):
               the call runs, but as its bounded ``ctx`` form.
bound          PreToolUse narrows the input (a ``limit`` / ``head_limit``)
               or, past the inline budget, denies with a map of the file.
deny_steer     PreToolUse denies and names the ``ctx`` command to run
               instead (hosts that cannot rewrite input, and native search
               under the replacement surface).
output_gate    PostToolUse replaces an over-budget result with a digest and
               a retrieval address; under budget it passes unchanged.
observe        The hook sees the call and records it, but never changes it
               (edits, skills, and every result on a host that cannot
               replace output).
exempt         Deliberately not hooked, with the reason stated.
=============  ============================================================

No hook can change *which* tool runs, and files a user ``@``-references enter
the context with no tool call at all; those limits are stated per host in
``LIMITS`` rather than implied away.

Hot-path discipline: ``ctx.hook`` imports this module at module scope, so it
imports nothing at all (pinned by ``tests/test_hook_hot_path.py``).
"""

from __future__ import annotations

ROUTES = ("rewrite", "bound", "deny_steer", "output_gate", "observe", "exempt")

#: Every hook dialect the harness speaks, in report order.
HOSTS = ("claude-code", "codex", "antigravity", "hermes", "omp", "opencode", "dsh")

# ------------------------------------------------------------------ kinds
# A kind is what the guard does with a tool. The first four have a PreToolUse
# branch in ``ctx.hook.classify``; the rest are post-stage only.
GUARD_KINDS = ("command", "read", "search", "edit")
#: ``tool``: any result-producing tool (sub-agents, web, LSP, background
#: output readers, MCP); ``skill``: loads instructions that must reach the
#: model verbatim; ``control``/``schema``: exempt (see EXEMPT_REASONS).
KINDS = GUARD_KINDS + ("tool", "mcp", "skill", "control", "schema", "media")

#: Why a kind is exempt. Every exemption carries one line.
EXEMPT_REASONS = {
    "control": "session control or bookkeeping; returns a short status, and "
               "some results are the user's own answer or approval",
    "schema": "returns tool definitions the host parses to load deferred "
              "tools; altering them would break tool loading",
    "media": "image or audio content; media is out of scope for the text gate",
}

#: Why a non-exempt kind is only observed (never replaced) even where the
#: host could replace output.
OBSERVE_REASONS = {
    "edit": "edits are allowed and their outcome is recorded; the result is a "
            "short status (Claude Code's carries the whole original file, "
            "which must never be digested in its place)",
    "skill": "the result is instructions the model must follow verbatim; a "
             "digest in their place would corrupt the skill",
}

# ------------------------------------------------------------ native tables
# Exact native tool names per host. Names the hook sees at runtime are looked
# up here first; ``native_kind`` documents what happens to every other name.
# Sources: Claude Code tools reference (code.claude.com/docs/en/tools-reference,
# read 2026-11-10, CLI 2.1.322) plus the legacy names older releases still
# send; Codex hooks reference (tool coverage table); Antigravity hooks
# reference (supported tools) plus the names this plugin already matched;
# Hermes/OMP/OpenCode/DSH from the names their bridges forward today.
NATIVE: dict[str, dict[str, str]] = {
    "claude-code": {
        "Bash": "command",
        "Read": "read",
        "Grep": "search", "Glob": "search",
        "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
        # Result-producing built-ins: governed by the output gate.
        "Agent": "tool", "Task": "tool",          # sub-agent (Task: pre-2.x name)
        "WebFetch": "tool", "WebSearch": "tool",
        "LSP": "tool", "PowerShell": "tool", "Monitor": "tool", "Workflow": "tool",
        "TaskOutput": "tool", "BashOutput": "tool",   # background output (legacy)
        "LS": "tool", "NotebookRead": "tool",         # legacy listing / notebook read
        "ReadMcpResourceTool": "tool", "ListMcpResourcesTool": "tool",
        "TaskGet": "tool", "TaskList": "tool", "CronList": "tool",
        "ListAgents": "tool", "RemoteTrigger": "tool", "Artifact": "tool",
        "Skill": "skill",
        "ToolSearch": "schema",
        "AskUserQuestion": "control", "EnterPlanMode": "control",
        "ExitPlanMode": "control", "EnterWorktree": "control",
        "ExitWorktree": "control", "EndConversation": "control",
        "TodoWrite": "control", "TaskCreate": "control", "TaskUpdate": "control",
        "TaskStop": "control", "KillShell": "control", "KillBash": "control",
        "CronCreate": "control", "CronDelete": "control",
        "ScheduleWakeup": "control", "PushNotification": "control",
        "SendFeedback": "control", "SendUserFile": "control",
        "SendMessage": "control", "SubagentHandback": "control",
        "ShareOnboardingGuide": "control", "ReportFindings": "control",
        "WaitForMcpServers": "control",
    },
    "codex": {
        # Shell and unified exec both report as Bash.
        "Bash": "command",
        "apply_patch": "edit",
        "read_file": "read",
        "list_dir": "search", "grep_files": "search",
        "write_stdin": "tool",                    # unified-exec poll returns output
        "spawn_agent": "tool", "Agent": "tool", "wait": "tool", "wait_agent": "tool",
        "send_input": "tool", "close_agent": "tool",
        "list_mcp_resources": "tool", "list_mcp_resource_templates": "tool",
        "read_mcp_resource": "tool",
        "update_plan": "control",
        "view_image": "media",
    },
    "antigravity": {
        "run_command": "command",
        # Read/Edit/MultiEdit are not listed: `agy plugin validate` (1.5.5)
        # flags them as Claude Code names that never match on this host.
        "view_file": "read", "read_file": "read",
        "grep_search": "search", "find_by_name": "search", "list_dir": "search",
        "ListDir": "search", "glob_search": "search", "codebase_search": "search",
        "write_to_file": "edit", "replace_file_content": "edit",
        "multi_replace_file_content": "edit", "edit_file": "edit",
        "search_web": "tool", "read_url_content": "tool",
        "invoke_subagent": "tool", "manage_task": "tool",
        "schedule": "control", "list_permissions": "control",
        "ask_permission": "control", "define_subagent": "control",
        "send_message": "control", "manage_subagents": "control",
        "ask_question": "control",
        "generate_image": "media",
    },
    "hermes": {
        # The bridge normalizes Hermes' `terminal` to Bash before classifying.
        "Bash": "command", "terminal": "command",
        "read_file": "read",
        "write_file": "edit", "patch": "edit",
        "search_files": "tool", "process": "tool", "execute_code": "tool",
        "delegate_task": "tool", "web_search": "tool", "web_extract": "tool",
        "skill_view": "skill",
        "todo": "control", "memory": "control", "clarify": "control",
    },
    "omp": {
        "bash": "command", "read": "read", "grep": "search",
        "edit": "edit", "write": "edit",
        "find": "tool", "ls": "tool", "task": "tool", "fetch": "tool",
        "web_search": "tool",
    },
    "opencode": {
        "bash": "command", "read": "read",
        "grep": "search", "glob": "search", "list": "search",
        "edit": "edit", "write": "edit", "patch": "edit", "multiedit": "edit",
        "apply_patch": "edit",
        "task": "tool", "webfetch": "tool", "websearch": "tool",
        "codesearch": "tool", "lsp": "tool", "batch": "tool",
        "skill": "skill",
        "todowrite": "control", "todoread": "control", "question": "control",
    },
    "dsh": {
        "bash": "command",
    },
}

#: Hosts whose MCP tool names carry no reliable namespace. On OpenCode an MCP
#: tool has been named ``<server>_<tool>``, which a word rule cannot tell from
#: a native tool (``github_list_issues`` reads as a "list"), so there only the
#: exact table may assign a guard kind; every other name is gated.
EXACT_ONLY_HOSTS = frozenset({"claude-code", "opencode"})

#: Name prefixes that mark a tool as coming from an MCP server. ``mcp__`` is
#: the convention Claude Code, Codex, Antigravity and current Hermes share;
#: ``mcp_`` is the single-underscore form Hermes used before it aligned.
MCP_PREFIXES = ("mcp__", "mcp_")


def is_mcp(name: str, payload: dict | None = None) -> bool:
    """Is this tool served by an MCP server (never a native file tool)?

    Claude Code (2.1.274+) states it outright with an ``mcp_server`` object;
    otherwise the name decides: an MCP prefix, or a ``__`` namespace
    separator, which no native tool on any supported host uses.
    """
    if payload and isinstance(payload.get("mcp_server"), dict):
        return True
    low = name.lower()
    return low.startswith(MCP_PREFIXES) or "__" in name


def mcp_identity(name: str, payload: dict | None = None) -> tuple[str, str]:
    """``(server, tool)`` for an MCP tool name, best-effort and bounded."""
    server = ""
    if payload and isinstance(payload.get("mcp_server"), dict):
        server = str(payload["mcp_server"].get("name") or "")
    body = name
    for prefix in MCP_PREFIXES:
        if body.lower().startswith(prefix):
            body = body[len(prefix):]
            break
    if "__" in body:
        head, _, tool = body.partition("__")
        return (server or head)[:64], tool[:64]
    return server[:64], body[:64]


def table_kind(host: str, name: str) -> str | None:
    """The kind the exact table assigns, or None when the name is not listed."""
    return NATIVE.get(host, {}).get(name)


#: Hosts whose argument rewriting is verified per tool rather than for the
#: whole PreToolUse contract: only the tools named here are rewritten there;
#: every other tool on that host stays deny-and-steer. Antigravity's
#: ``overwrite`` response field was observed live on agy 1.5.5 for
#: run_command (CommandLine) and read_file (StartLine/EndLine) and nothing
#: else: its search and listing tools left the default toolset in 1.2.7, so
#: their overwrite could not be observed, and view_file was not offered.
REWRITABLE: dict[str, frozenset[str]] = {
    "antigravity": frozenset({"run_command", "read_file"}),
}


def can_rewrite(host: str, name: str | None, caps: dict[str, bool]) -> bool:
    """Can ``host``'s PreToolUse rewrite the arguments of tool ``name``?"""
    if not caps.get("input_substitution"):
        return False
    only = REWRITABLE.get(host)
    return only is None or (name or "") in only


def stage_routes(
    kind: str | None,
    caps: dict[str, bool],
    *,
    collapse: bool = True,
    steering: bool = True,
    host: str | None = None,
    name: str | None = None,
    shell_rewrite: bool = False,
) -> tuple[str | None, str]:
    """``(pre_route, post_route)`` for a tool kind on a host with ``caps``.

    ``None`` as the pre route means the input side has nothing to do (the
    call is allowed untouched if a pre hook fires at all). ``kind`` None is a
    name the table has never seen: it falls to the output gate wherever the
    host can replace output, and is observed (captured, not replaced)
    elsewhere — never ungoverned. On a host listed in ``REWRITABLE`` the
    rewrite routes apply only to the tools named there, so callers pass
    ``host`` and ``name``.
    """
    can_in = bool(caps.get("input_substitution")) and steering
    if host is not None:
        can_in = can_in and can_rewrite(host, name, caps)
    gate = "output_gate" if caps.get("output_substitution") else "observe"
    if kind in ("control", "schema", "media"):
        return "exempt", "exempt"
    if host == "claude-code" and kind == "command" and not shell_rewrite:
        return None, gate  # R2: runs as written unless steering = "rewrite"
    if host in REWRITE_WITHHELD and kind in ("command", "read"):
        return None, gate  # runs as itself under the host's approval; gated after
    if kind == "command":
        return ("rewrite" if can_in else "deny_steer"), gate
    if kind == "read":
        return ("bound" if can_in else "deny_steer"), gate
    if kind == "search":
        if collapse:
            return "deny_steer", gate
        return ("bound" if can_in else "deny_steer"), gate
    if kind == "edit":
        return "observe", "observe"
    if kind == "skill":
        return None, "observe"
    return None, gate  # tool, mcp, or never seen


#: Hosts where the hook could rewrite input but does not: the host takes a
#: rewrite only together with an approval its own policy may not re-check.
REWRITE_WITHHELD = frozenset({"codex"})

#: What a rewrite does to the host's own approval. Printed by `ctx coverage`.
APPROVAL: dict[str, str] = {
    "claude-code": (
        "ctx never approves a rewritten call and, by default (steering = \"auto\"), does "
        "not rewrite shell commands at all: each command reaches Claude Code exactly as "
        "written, with no decision, so its rules, links, directory settings and prompts "
        "apply as without ctx; the output gate then bounds the result, taking the full "
        "output from the file Claude Code saved for it (its tool-results directory for "
        "this session only). No ask or deny is sent for output volume; safety asks "
        "(secret paths, container escapes, in-place edits, repository deny rules) stay. "
        "steering = \"rewrite\" opts in to capture at birth: the command is rewritten with "
        "no decision, so Claude Code's prompts and rules see the `ctx run` wrapper, and a "
        "rule from a source ctx cannot read never sees the original; ctx only refuses "
        "(deny/ask) where a visible rule would refuse the original. The only allow ctx "
        "sends is for its own read-only verbs, the commands it prints for the model to run next (get, search, "
        "stats, q, diff, map, def, refs, intake --chunks, view): by run: handle always; "
        "over workspace paths or other handles only when no Read deny/ask rule or "
        "outside-directory block is visible and every settings source was readable. "
        "view writes only into ctx's store, a private temporary directory and "
        ".ctx-session-reads/media/. A Read rule in a source ctx cannot read does not bind "
        "these verbs"),
    "codex": (
        "rewrites withheld and no ask or deny for volume: commands run as written under "
        "Codex's own approval; its PostToolUse block replaces a large output with ctx's "
        "digest after the command ran (the command still ran in full)"),
    "antigravity": (
        "rewrite stays the default (the host cannot replace output); the user's rules are "
        "evaluated against the original (ctx.agyperm). Whether the hook's allow skips "
        "Antigravity's own checks: not observed by this package"),
    "hermes": ("the bridge returns modified args and no approval of its own; whatever "
               "Hermes checks then sees the wrapper (not verified live)"),
    "omp": ("the bridge replaces the input and grants no approval of its own "
            "(not verified live)"),
    "opencode": ("the bridge mutates args before OpenCode's permission check, which then "
                 "matches the wrapper, not the original program (not verified live)"),
    "dsh": "no rewrite: the bridge blocks with the exact retry, which DSH's policy then judges",
}


def exempt_reason(kind: str | None) -> str:
    return EXEMPT_REASONS.get(kind or "", "")


# ---------------------------------------------------------------- limits
#: What a hook on each host cannot govern, and why. Printed verbatim by
#: ``ctx coverage``; one line each.
LIMITS: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "@-referenced files enter the context with no tool call, so no hook sees them "
        "(2.1.322 observed: a large mention is not inlined, only referenced); opt-in "
        "[media] file_mentions = true makes `ctx wrap claude` set "
        "CLAUDE_CODE_DISABLE_FILE_MENTIONS=1 so the model reads them with Read",
        "images pasted from the clipboard or attached to a prompt enter with no tool "
        "call: no hook sees them, so no media note or overview applies",
        "a failed tool fires PostToolUseFailure, which cannot replace output: "
        "failure output is observed, never digested",
        "built-in results must keep their output shape; a result the gate "
        "cannot reshape is reported as replace_unshaped (the host ignores it)",
        "Monitor event lines arrive as notifications, not tool results; a Bash "
        "PreToolUse can deny a Monitor command but its rewrite has no effect",
        "no hook can change which tool runs; native Grep/Glob are denied with "
        "the ctx command named, not converted",
    ),
    "codex": (
        "hosted tools (WebSearch) do not use the local hook path",
        "write_stdin polls do not rerun PreToolUse",
        "PreToolUse 'ask' is unsupported, so a confirmation degrades to a deny",
    ),
    "antigravity": (
        "PreToolUse rewrites arguments (`overwrite`, verified live on agy 1.5.5) "
        "only for run_command and read_file; every other tool, and an agy older "
        "than 1.5.4 (no hookEventName in the payload), is denied with the ctx "
        "command named",
        "agy checks its permission rules against the rewritten command (`ctx run "
        "...`), so where a rule pre-approves ctx but no rule covers the original "
        "command, the rewrite is sent as force_ask rather than approved",
        "PostToolUse cannot replace output (its only legal output is {}): the "
        "result of a tool that was not rewritten reaches the transcript raw",
        "grep_search, find_by_name and list_dir left agy's default toolset in "
        "1.2.7; the model searches through run_command, which the command route "
        "governs",
        "live agy 1.5.5 PostToolUse payloads carry no tool result at all, so the "
        "post stage records the call but captures nothing (decision no_result)",
        "only tools named in the plugin's matchers are seen",
    ),
    "hermes": (
        "MCP tools are recognised by the mcp__/mcp_ prefix",
    ),
    "omp": (
        "native tool names are taken from the bridge; unlisted names are gated",
    ),
    "opencode": (
        "MCP tool names may carry no namespace, so only listed native names "
        "get a guard kind; every other tool is gated",
    ),
    "dsh": (
        "sealed tool arguments cannot be rewritten: a rewrite becomes a block "
        "naming the exact retry",
        "a downstream canonical-value rewrite is left alone, so the gate skips it",
    ),
}

#: Media reads: what the read route does when the file is an image, video or
#: audio (``ctx.mediaread``; ``[media]`` in ctx.toml). Printed by ``ctx
#: coverage`` under each host; "unchanged" is a declared non-behavior, not a gap
#: left implicit. Evidence is stated per line: observed live, or contract only.
MEDIA_READS: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "Read of a large PNG/JPEG/GIF/WebP: allowed unchanged + note (size, the host's "
        "downscale, the exact `ctx view --cell` crop command) [image_read = advise, "
        "default]; opt-in image_read = redirect rewrites it to a gridded overview "
        "(falls back to the note without Pillow, on error or past the time budget); "
        "an image the host sends whole gets nothing. Observed live on 2.1.322",
        "Read of video (MP4/MOV/MKV/WebM/AVI) or audio: the guard denies it with the "
        "exact `ctx view` command (or what is missing) [video_read = steer, default], "
        "but OBSERVED on 2.1.322 Read refuses a binary extension in its own input "
        "validation BEFORE PreToolUse runs, so there the model gets the host's "
        "'cannot read binary files' error and no hook sees the call; the hooks "
        "reference says such validation rejections fire no PostToolUseFailure "
        "either. The hint therefore rides the model's next ffmpeg/ffprobe call on "
        "that file (one line naming `ctx view`)",
        "Read of BMP/TIFF/ICO: allowed unchanged + a note naming `ctx view` "
        "(only when Pillow is installed)",
        "the video hint: fires on a video that exists and is only the command's output "
        "(`ffmpeg -i in.png v.mp4`); misses `sudo -u USER ffmpeg` and commands inside "
        "if/for/while bodies",
        "up to 200 characters of a visible file name appear inside the quoted command "
        "(inherent to naming the file); longer or unsafe names are replaced by PATH",
        "a redirect never approves the read: it returns updatedInput with no "
        "permissionDecision, so the host runs its own permission flow, on the "
        "REWRITTEN path (2.1.322 liveprobe: an allow+rewrite bypassed a user deny/ask "
        "rule). It is skipped when a visible settings file has a Read deny/ask rule; "
        "rules passed by --settings or managed policy are not visible to the hook, and "
        "a path-specific Read rule from those is not checked against the overview path",
        "media reads skip the session read-budget ledger (an image is priced in "
        "tokens, not bytes); the overview is hash-verified before the redirect, but "
        "the host opens it later, a time-of-check window inside the session directory "
        "(exploiting it needs write access to the workspace; not tested)",
        "media handling runs only for a read the guard was going to allow: secret, "
        "outside-workspace, .ctxignore'd or symlinked files keep the "
        "ordinary read path",
    ),
    "codex": (
        "view_image of a large image: allowed + additionalContext note [advise]; "
        "image_read = redirect replaces its `path` via updatedInput. Contract only "
        "(hooks reference), not observed live",
        "view_image of video or audio: denied with the exact `ctx view` command. "
        "Contract only",
        "media passed to the shell (cat, xxd ...) keeps the command guard; the "
        "reference does not say how Codex shows images otherwise, so nothing else changes",
    ),
    "antigravity": (
        "unchanged: no media handling (its hooks reference is silent on image and "
        "video reads)",
    ),
    "hermes": ("unchanged: no media handling",),
    "omp": ("unchanged: no media handling",),
    "opencode": ("unchanged: no media handling",),
    "dsh": ("unchanged: no media handling",),
}

# ------------------------------------------------------------ prompt gate
#: What the UserPromptSubmit hook can do about an oversized prompt, per host
#: (the prompt gate, ``ctx.promptgate``). Printed by ``ctx coverage``. The
#: honest limit is the same on every host that has one: a prompt hook can add
#: context or block the prompt, and cannot replace or shorten it. A host not
#: listed has no ctx prompt gate and an oversized prompt reaches the model
#: unmodified.
PROMPT_GATE: dict[str, dict[str, str]] = {
    "claude-code": {
        "event": "UserPromptSubmit",
        "advise": "stores the paste and adds a digest + handle as context "
                  "(delivered whole up to 10,000 chars; the gate stays under it)",
        "block": "stores the paste and stops the prompt before any model call; "
                 "the user resends their message with the handle",
        "limit": "cannot shorten or replace the prompt: in advise mode the paste still "
                 "reaches the model this turn",
        "evidence": "observed live on Claude Code 2.1.322 (context limit, block); "
                    "the pasted-region markers are from documentation only",
    },
    "codex": {
        "event": "UserPromptSubmit",
        "advise": "stores the paste and adds a digest + handle as developer context "
                  "(Codex caps added context near 2,500 tokens; the gate stays under it)",
        "block": "stores the paste and returns decision=block; the user resends their "
                 "message with the handle",
        "limit": "cannot shorten or replace the prompt: in advise mode the paste still "
                 "reaches the model this turn; non-managed hooks need the user's trust "
                 "before they run",
        "evidence": "from OpenAI's documentation only: contract-tested, NOT live-verified",
    },
}


#: Hosts whose plugin forwards every tool to the hook (both stages).
FORWARDS_ALL = frozenset({"codex", "hermes", "omp", "opencode", "dsh"})


# --------------------------------------------- Claude Code registrations
#: The PreToolUse matcher: exactly the tools with an input-side route.
#: Exact-match list form (letters and ``|`` only), per the hooks reference.
def claude_pre_tools() -> tuple[str, ...]:
    table = NATIVE["claude-code"]
    return tuple(n for n in table if table[n] in GUARD_KINDS)


def claude_post_exempt() -> tuple[str, ...]:
    """Tools the PostToolUse matcher deliberately leaves out: the exempt ones."""
    table = NATIVE["claude-code"]
    return tuple(n for n in table if table[n] in ("control", "schema", "media"))


def claude_pre_matcher() -> str:
    return "|".join(claude_pre_tools())


def claude_post_matcher() -> str:
    """Every tool except the exempt ones, including names not yet invented.

    A negative lookahead rather than an allow-list: Claude Code evaluates a
    matcher containing regex characters as an unanchored JavaScript RegExp,
    so this fires for any tool *not* exactly named below — a built-in added
    in a later release falls to the output gate instead of to nothing.
    """
    return "^(?!(?:" + "|".join(claude_post_exempt()) + ")$)"


#: Files ctx itself writes under the workspace (``.ctx-session-reads/``,
#: self-ignored for git and ripgrep). None holds command text or tool
#: content; a listing or a count over the whole tree includes them, as it
#: includes ``.git``. Always: ``.gitignore``, ``coverage.jsonl``,
#: ``engagement.json``, ``<session>.count``; after edits:
#: ``edit-outcomes.jsonl``; when they occur: ``guard-failures.jsonl``,
#: ``failure-available.json``, ``eval-adoption.jsonl``,
#: ``records-adoption.jsonl``, ``collapse.jsonl``, ``route.jsonl``,
#: ``plan-emissions.jsonl``, ``media/`` (image overviews), ``gate-fallback/``,
#: ``proxy/window.json``, ``relay/``, ``tasks/``, and ``store/`` only when
#: the state directory is not writable.
WORKSPACE_FOOTPRINT = (
    ".gitignore", "coverage.jsonl", "engagement.json", "<session>.count", "edit-outcomes.jsonl",
    "guard-failures.jsonl", "failure-available.json", "eval-adoption.jsonl",
    "records-adoption.jsonl", "collapse.jsonl", "route.jsonl", "plan-emissions.jsonl",
    "media/", "gate-fallback/", "proxy/window.json", "relay/", "tasks/", "store/ (fallback)",
)
