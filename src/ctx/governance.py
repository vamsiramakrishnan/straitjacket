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
        "view_file": "read", "Read": "read", "read_file": "read",
        "grep_search": "search", "find_by_name": "search", "list_dir": "search",
        "ListDir": "search", "glob_search": "search", "codebase_search": "search",
        "write_to_file": "edit", "replace_file_content": "edit",
        "multi_replace_file_content": "edit",
        "Edit": "edit", "edit_file": "edit", "MultiEdit": "edit",
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


def stage_routes(
    kind: str | None,
    caps: dict[str, bool],
    *,
    collapse: bool = True,
    steering: bool = True,
) -> tuple[str | None, str]:
    """``(pre_route, post_route)`` for a tool kind on a host with ``caps``.

    ``None`` as the pre route means the input side has nothing to do (the
    call is allowed untouched if a pre hook fires at all). ``kind`` None is a
    name the table has never seen: it falls to the output gate wherever the
    host can replace output, and is observed (captured, not replaced)
    elsewhere — never ungoverned.
    """
    can_in = bool(caps.get("input_substitution")) and steering
    gate = "output_gate" if caps.get("output_substitution") else "observe"
    if kind in ("control", "schema", "media"):
        return "exempt", "exempt"
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


def exempt_reason(kind: str | None) -> str:
    return EXEMPT_REASONS.get(kind or "", "")


# ---------------------------------------------------------------- limits
#: What a hook on each host cannot govern, and why. Printed verbatim by
#: ``ctx coverage``; one line each.
LIMITS: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "@-referenced files enter the context with no tool call, so no hook sees them",
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
        "PENDING follow-up: this row is the published contract (deny-and-steer). "
        "Current agy can also rewrite arguments (an `overwrite` response field) "
        "and has SessionStart; adopting those is a separate change",
        "PreToolUse cannot rewrite input: a rewrite becomes a deny naming the command",
        "PostToolUse cannot replace output (its only legal output is {}): "
        "results reach the transcript raw",
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
