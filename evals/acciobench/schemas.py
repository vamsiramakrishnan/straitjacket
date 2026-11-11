"""Tool declarations, prompt construction, and mutation classification for AccioBench."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from typing import Any

from ctx.deliverables import format_deliverable_contract
from evals.acciobench.ports import CompiledPortMap, rewrite_ports_surgical

__all__ = [
    "MUTATING_BROWSER_TOOLS",
    "MUTATING_SHELL_RE",
    "READONLY_TOOLS",
    "SJ_EXECUTION_PROTOCOL",
    "build_prompt",
    "build_tools",
    "canonical_tool_sig",
    "format_workspace_inventory",
    "is_mutating_tool_call",
]

SJ_EXECUTION_PROTOCOL: str = (
    "## Execution & Verification Protocol\n"
    "1. Scope & Parallelize Discovery: Scope each turn's reasoning to the current unresolved step rather than rehearsing downstream deliverables early. Batch independent `read`, `list`, `glob`, or `grep` calls in parallel in a single turn (up to 8 files at once). Read text/JSON/CSV/MD/YAML files under 500 lines whole once (omit `offset`/`limit`), and never re-read unchanged files already in context.\n"
    "2. Interface Once, Retrieve Full Evidence, Batch Execute: Inspect any CLI `--help`, API/MCP schema, or web page structure once upfront. When a large output returns a `run:<id>` digest, call `ctx_retrieve` (`mode='get'` or `'search'`) to inspect omitted records/lines rather than guessing from the preview or re-running commands. Consolidate multi-item queries, pagination, parsing, and sectioned computation into ONE Python heredoc (`python3 - <<'PY' ... PY`) printing clearly labeled `[section: <name>]` headers (avoid inline `python3 -c \"...\"` quoting). On browser tasks, prefer `browser_batch` with `include_step_text=true` to execute `click -> read -> back` loops, toggles, and fills in one turn; inspect `[dom-diff]` after actions and do not repeat `browser_snapshot` when no UI change occurred.\n"
    "3. Deliberate on Semantics, Offload Mechanics to Python: Use reasoning to resolve ambiguity, constraints, and edge cases, then offload mechanical computation (>=2-number arithmetic, unit/currency/date math, sorting, aggregation, and schema serialization) to Python so structured deliverables (`/task/outputs/*.json`, `.csv`, `.yaml`, `.md`, `.html`) are written directly via `json.dump` / `csv.writer` rather than hand-typed.\n"
    "4. Pinned Retrospective State & Clean Finalization: Populate a concise 1-line retrospective `state_note` on tool calls recording verified facts from prior steps (`key=val`, counts, IDs, completed mutations) or explicit provisional choices (`assumed: <choice>`). Run ONE consolidated verification check of `/task/outputs/*` and all modified service/UI state (counts, links, uploads, labels) against task requirements, remove temporary scripts or scratch probe items, and call `present_files`.\n"
    "5. Multi-Item Transfers & Modal Commits: Batch multi-item CLI transfers in one Python/bash loop rather than separate turns. In browser modals and forms (Labels, Filters, Drafts, Tasks, Calendar), use parent-label selectors for nested labels instead of `'Parent/Child'`, click `Apply`/`Save`/`Create`, and verify `[row_id=... starred=true labels=[...]]` in snapshot before finishing."
)

MUTATING_SHELL_RE: re.Pattern[str] = re.compile(
    r"(?:>|>>|\btee\b|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\bsed\s+-i\b|"
    r"/messages/post|/contacts/(?:create|update)|\.write_text\(|\.write\(|json\.dump|csv\.writer|"
    r"open\([^)]*['\"][wa]['\"]|"
    r"\bcurl\b.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)|"
    r"urllib\.request\.Request\b.*(?:\bdata\s*=|\bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE))|"
    r"\b(?:requests|httpx)\.(?:post|put|patch|delete)\b|"
    r"[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*(?:create|update|delete|remove|modify|send|reply|draft|star|unstar|archive|trash|label|add|close|reopen|complete|upload|insert|append|replace|batch|transition|assign|comment|link|merge)[A-Za-z0-9_.-]*[\"']|"
    r"\b(?:stripe|todoist|jira|box|dws|gws|ntn|shopify)\b.*\b(?:create|update|delete|add|complete|close|reopen|move|assign|comment|transition|upload|mkdir|cp|mv|rm|share|collaborations|append|replace|insert|batch|send|draft|trash|archive|finalize|pay|void|cancel|confirm|capture|refund|edit|patch|post|put|submit|publish|seed|import|modify|link|resolve|star|unstar|restore)\b)",
    re.I | re.S,
)

READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "ctx_retrieve",
        "read",
        "list",
        "glob",
        "grep",
        "browser_snapshot",
        "browser_info",
        "web_fetch",
    }
)

MUTATING_BROWSER_TOOLS: frozenset[str] = frozenset(
    {
        "browser_open",
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_type",
        "browser_act",
        "browser_batch",
        "browser_utility",
        "browser_press",
        "browser_console",
    }
)

_STATE_NOTE_PROP: dict[str, str] = {
    "type": "STRING",
    "description": (
        "Concise 1-line retrospective checkpoint (<160 chars) of verified facts from prior steps "
        "(`key=val`, counts, IDs, mutations) or explicit provisional choices (`assumed: <choice>`), "
        "never unexecuted plans"
    ),
}


def build_tools(
    *,
    requires_browser: bool,
    requires_network: bool,
    enable_state_note: bool = True,
    enable_sj_tools: bool | None = None,
) -> list[dict[str, Any]]:
    """Build function declarations for the AccioBench tool surface."""
    sn = {"state_note": _STATE_NOTE_PROP} if enable_state_note else {}
    sj_ext = enable_state_note if enable_sj_tools is None else bool(enable_sj_tools)
    decls: list[dict[str, Any]] = [
        {
            "name": "bash",
            "description": "Execute a shell command in the workspace directory. Use for python3, curl, CLI tools, jq, etc.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "command": {"type": "STRING", "description": "Shell command string to execute"},
                    "description": {"type": "STRING", "description": "Short summary of what this command does"},
                    "workdir": {"type": "STRING", "description": "Optional working directory (defaults to /task)"},
                    **sn,
                },
                "required": ["command"],
            },
        },
        {
            "name": "read",
            "description": "Read file contents from the workspace (supports text, JSON/CSV/HTML, PDFs, and images).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "file_path": {"type": "STRING", "description": "Path to file, e.g. '/task/workspace/README.md'"},
                    "offset": {"type": "INTEGER", "description": "Optional 0-based line offset for text files"},
                    "limit": {"type": "INTEGER", "description": "Optional max lines to read for text files"},
                    "pages": {"type": "STRING", "description": "Optional 1-based page range for PDFs, e.g. '1-5'"},
                    **sn,
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "write",
            "description": "Write text content to a destination file (e.g. '/task/outputs/answer.json').",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "file_path": {"type": "STRING", "description": "Destination file path"},
                    "content": {"type": "STRING", "description": "Full file content to write"},
                    **sn,
                },
                "required": ["file_path", "content"],
            },
        },
        {
            "name": "edit",
            "description": "Replace an exact string in an existing file.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "file_path": {"type": "STRING", "description": "Target file path"},
                    "old_string": {"type": "STRING", "description": "Exact substring to replace"},
                    "new_string": {"type": "STRING", "description": "Replacement string"},
                    **sn,
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
        {
            "name": "list",
            "description": "List files and directories in a given path.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "path": {"type": "STRING", "description": "Directory path, e.g. '/task/workspace'"},
                    "show_hidden": {"type": "BOOLEAN", "description": "Include hidden files"},
                    **sn,
                },
                "required": ["path"],
            },
        },
        {
            "name": "glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "pattern": {"type": "STRING", "description": "Glob pattern, e.g. '**/*.json'"},
                    "path": {"type": "STRING", "description": "Base directory to search in"},
                    **sn,
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "grep",
            "description": "Search for a regex pattern in files under a file or directory.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "pattern": {"type": "STRING", "description": "Regex pattern to search for"},
                    "path": {"type": "STRING", "description": "File or directory path (defaults to /task/workspace)"},
                    "ignore_case": {"type": "BOOLEAN", "description": "Case-insensitive match"},
                    **sn,
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "present_files",
            "description": "Present the final generated deliverables to complete the task.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "files": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {
                                    "type": "STRING",
                                    "description": "Path to deliverable, e.g. '/task/outputs/answer.json'",
                                },
                                "label": {"type": "STRING", "description": "Human-readable label"},
                            },
                            "required": ["path"],
                        },
                    },
                    **sn,
                },
                "required": ["files"],
            },
        },
    ]

    if sj_ext:
        decls.append(
            {
                "name": "ctx_retrieve",
                "description": (
                    "Retrieve exact lines, JSON records/pointers, pattern matches, shape stats, or pipeline queries "
                    "from a captured `run:<id>` evidence handle without re-executing the command."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "handle": {
                            "type": "STRING",
                            "description": "Evidence handle from a digest, e.g. 'run:8d8335db6848'",
                        },
                        "mode": {
                            "type": "STRING",
                            "description": "'get' (default: slice lines/records/json_pointer), 'search', 'stats', or 'query'",
                        },
                        "lines": {
                            "type": "STRING",
                            "description": "1-based line span 'A:B' for mode='get' (e.g. '1:120')",
                        },
                        "records": {
                            "type": "STRING",
                            "description": "0-based JSONL record span 'A:B' for mode='get' (e.g. '0:30')",
                        },
                        "json_pointer": {
                            "type": "STRING",
                            "description": "RFC 6901 JSON pointer for mode='get' (e.g. '/15' or '/items/0')",
                        },
                        "pattern": {
                            "type": "STRING",
                            "description": "Regex or substring pattern for mode='search'",
                        },
                        "fixed": {
                            "type": "BOOLEAN",
                            "description": "Literal substring match for mode='search'",
                        },
                        "context": {
                            "type": "INTEGER",
                            "description": "Context lines around matches for mode='search' (default 2)",
                        },
                        "query": {
                            "type": "STRING",
                            "description": "Pipeline expression for mode='query', e.g. 'records | count'",
                        },
                        "stream": {
                            "type": "STRING",
                            "description": "'stdout' (default) or 'stderr'",
                        },
                        **sn,
                    },
                    "required": ["handle"],
                },
            }
        )

    if requires_browser:
        decls.extend(
            [
                {
                    "name": "browser_open",
                    "description": "Open a new browser tab at the given URL and return its targetId and interactive page snapshot.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "url": {"type": "STRING", "description": "URL to open"},
                            **sn,
                        },
                        "required": ["url"],
                    },
                },
                {
                    "name": "browser_navigate",
                    "description": "Navigate an existing browser tab to a URL and return updated page snapshot.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId (e.g. 'vtab-1')"},
                            "url": {"type": "STRING", "description": "Destination URL"},
                            **sn,
                        },
                        "required": ["url"],
                    },
                },
                {
                    "name": "browser_snapshot",
                    "description": "Capture visible text and interactive element refs ([e1], [e2], ...) for the current page.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            **sn,
                        },
                    },
                },
                {
                    "name": "browser_click",
                    "description": "Click an element by its snapshot ref (e.g. 'e4') or CSS selector and return the updated page snapshot (no separate browser_snapshot call needed).",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "ref": {
                                "type": "STRING",
                                "description": "Element ref from browser_snapshot (e.g. 'e4') or CSS selector",
                            },
                            "button": {"type": "STRING", "description": "Mouse button ('left', 'right', 'middle')"},
                            **sn,
                        },
                        "required": ["ref"],
                    },
                },
                {
                    "name": "browser_fill",
                    "description": "Fill multiple form fields in a single batch call.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "fields": {
                                "type": "ARRAY",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "ref": {"type": "STRING", "description": "Element ref (e.g. 'e2') or CSS selector"},
                                        "type": {
                                            "type": "STRING",
                                            "description": "Field type ('text', 'select', 'checkbox', 'radio')",
                                        },
                                        "value": {"type": "STRING", "description": "Value to set"},
                                    },
                                    "required": ["ref", "value"],
                                },
                            },
                            **sn,
                        },
                        "required": ["fields"],
                    },
                },
                {
                    "name": "browser_type",
                    "description": "Type text into a single input element (when pressEnter=true, also returns the updated page snapshot).",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "ref": {"type": "STRING", "description": "Element ref or CSS selector"},
                            "text": {"type": "STRING", "description": "Text to enter"},
                            "clearFirst": {"type": "BOOLEAN", "description": "Clear existing value before typing"},
                            "pressEnter": {"type": "BOOLEAN", "description": "Press Enter after typing"},
                            **sn,
                        },
                        "required": ["ref", "text"],
                    },
                },
                {
                    "name": "browser_act",
                    "description": "Perform an action on the page: kind='select' (dropdown option, returns updated snapshot), kind='evaluate' (run JS fn), kind='check'/'uncheck'/'hover'.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "kind": {
                                "type": "STRING",
                                "description": "Action kind: 'select', 'evaluate', 'check', 'uncheck', 'hover', 'click'",
                            },
                            "ref": {
                                "type": "STRING",
                                "description": "Element ref or CSS selector (for select/check/hover)",
                            },
                            "values": {
                                "type": "ARRAY",
                                "items": {"type": "STRING"},
                                "description": "Option values/labels for kind='select'",
                            },
                            "fn": {
                                "type": "STRING",
                                "description": "JavaScript expression/function for kind='evaluate'",
                            },
                            **sn,
                        },
                        "required": ["kind"],
                    },
                },
                {
                    "name": "browser_console",
                    "description": "Execute JavaScript in the page context (supports async/await, DOM inspection, fetch, and bulk actions) and return the result.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "expression": {
                                "type": "STRING",
                                "description": "JavaScript expression or IIFE to evaluate in the page",
                            },
                            "scriptPath": {"type": "STRING", "description": "Optional path to a .js file to execute"},
                            "savePath": {"type": "STRING", "description": "Optional file path to save the returned result"},
                            **sn,
                        },
                    },
                },
                {
                    "name": "browser_utility",
                    "description": "Upload local files to a file input (kind='upload') or inspect/clear cookies (kind='cookies').",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "kind": {"type": "STRING", "description": "'upload' or 'cookies'"},
                            "paths": {
                                "type": "ARRAY",
                                "items": {"type": "STRING"},
                                "description": "Local file paths to upload (e.g. ['/task/workspace/img.jpg'])",
                            },
                            "element": {"type": "STRING", "description": "CSS selector or ref for <input type='file'>"},
                            "operation": {"type": "STRING", "description": "For cookies: 'get' or 'clear'"},
                            **sn,
                        },
                        "required": ["kind"],
                    },
                },
                {
                    "name": "browser_wait",
                    "description": "Wait for timeMs, page loadState, or URL substring.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "condition": {"type": "STRING", "description": "'time', 'loadState', or 'url'"},
                            "timeMs": {"type": "INTEGER", "description": "Milliseconds to wait"},
                            "loadState": {
                                "type": "STRING",
                                "description": "'load', 'domcontentloaded', or 'networkidle'",
                            },
                            "url": {"type": "STRING", "description": "URL substring to wait for"},
                            **sn,
                        },
                    },
                },
                {
                    "name": "browser_screenshot",
                    "description": "Capture a visual screenshot of the current browser tab.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "savePath": {"type": "STRING", "description": "Optional path to save PNG/JPEG screenshot"},
                            "format": {"type": "STRING", "description": "'png' or 'jpeg'"},
                            **sn,
                        },
                    },
                },
                {
                    "name": "browser_press",
                    "description": "Press a keyboard key (e.g. 'Enter', 'Escape', 'Tab').",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "key": {"type": "STRING", "description": "Key name"},
                            **sn,
                        },
                        "required": ["key"],
                    },
                },
                {
                    "name": "browser_scroll",
                    "description": "Scroll the current page ('down', 'up', 'bottom', 'top').",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "direction": {"type": "STRING", "description": "'down', 'up', 'bottom', or 'top'"},
                            **sn,
                        },
                        "required": ["direction"],
                    },
                },
                {
                    "name": "browser_info",
                    "description": "List open browser tabs and their current URLs and titles.",
                    "parameters": {"type": "OBJECT", "properties": {**sn}},
                },
                {
                    "name": "web_fetch",
                    "description": "Fetch text/HTML from one or more HTTP(S) URLs.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "urls": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "URLs to fetch"},
                            **sn,
                        },
                        "required": ["urls"],
                    },
                },
            ]
        )
        if sj_ext:
            decls.append(
                {
                    "name": "browser_batch",
                    "description": (
                        "Execute multiple UI actions sequentially on the current tab in one turn "
                        "(e.g. click -> read -> back loops, starring/selecting rows, toggling checkboxes, filling inputs) "
                        "and return compact per-step digests plus one updated page snapshot with [dom-diff]."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "targetId": {"type": "STRING", "description": "Browser tab targetId"},
                            "steps": {
                                "type": "ARRAY",
                                "description": "Ordered UI steps to execute sequentially (up to 25 steps)",
                                "items": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "action": {
                                            "type": "STRING",
                                            "description": "'click', 'fill', 'type', 'select', 'check', 'uncheck', 'press', 'wait', 'back', 'forward', 'scroll', 'navigate', or 'read'",
                                        },
                                        "ref": {
                                            "type": "STRING",
                                            "description": "Element ref from snapshot (e.g. 'e14') or CSS selector (e.g. '#star-3')",
                                        },
                                        "value": {
                                            "type": "STRING",
                                            "description": "Value/text/option/key/ms/URL/direction for fill, type, select, press, wait, navigate, or scroll",
                                        },
                                        "pressEnter": {
                                            "type": "BOOLEAN",
                                            "description": "For action='type', press Enter after typing",
                                        },
                                    },
                                    "required": ["action"],
                                },
                            },
                            "stopOnNavigation": {
                                "type": "BOOLEAN",
                                "description": "Stop remaining steps if the page URL changes (default false)",
                            },
                            "stopOnError": {
                                "type": "BOOLEAN",
                                "description": "Stop remaining steps immediately when a step raises an error (default true)",
                            },
                            "include_step_text": {
                                "type": "BOOLEAN",
                                "description": "Capture compact per-step URL/title/DOM-delta/visible-text digests (default true)",
                            },
                            **sn,
                        },
                        "required": ["steps"],
                    },
                }
            )
        if requires_network:
            decls.append(
                {
                    "name": "web_search",
                    "description": "Search the public web for a query and return top results with titles, URLs, and snippets.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "query": {"type": "STRING", "description": "Search query"},
                            **sn,
                        },
                        "required": ["query"],
                    },
                }
            )

    return [{"functionDeclarations": decls}]


def build_prompt(case_dir: Path, port_map: dict[int, int] | CompiledPortMap) -> str:
    """Construct the task prompt with standard workspace contract and port remapping."""
    entry_file = case_dir / "task.md"
    if not entry_file.is_file():
        toml_path = case_dir / "task.toml"
        if toml_path.is_file():
            raw_toml = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            ep = str((raw_toml.get("task") or {}).get("entrypoint") or "task.md")
            entry_file = case_dir / ep
    task_prompt = entry_file.read_text(encoding="utf-8")
    task_prompt = rewrite_ports_surgical(task_prompt, port_map, is_service_script=False)
    workspace_contract = (
        "本次工作的标准目录约定：\n"
        "- 任务输入目录：`/task/workspace`。\n"
        "- 标准工作目录：`/task`。\n"
        "- 标准产物目录：`/task/outputs`。\n"
        "- 如需创建或修改任何本地文件，请在标准工作目录内完成，并把最终文件保存到标准产物目录。\n"
        "- 不要把最终产物只保存在 `/tmp`、桌面、下载目录或其它临时路径；这些路径不会作为标准结果归档。\n\n"
    )
    anti_tamper = (
        "\n## 操作规范（重要）\n\n"
        "本环境中的各项服务与平台均为真实的线上第三方系统。请始终像真实用户那样，"
        "**只通过它们提供的正常用户界面、或任务中明确说明的 CLI / API 来使用它们**。\n"
        "**不得对这些服务本身做任何越权改动或绕过其正常业务流程**，包括但不限于：\n"
        "- 读取或修改服务的源码、配置、数据库或内部状态文件；\n"
        "- 杀死、重启、替换或重新部署服务进程；\n"
        "- 直接写入或伪造服务的内部数据；\n"
        "- 调用任务正常流程之外的内部 / 管理 / 调试接口；\n"
        "- 任何绕过服务自身校验与业务规则的手段。\n"
        "请始终通过正常、合规的操作真实地完成工作。\n"
    )
    return workspace_contract + task_prompt + anti_tamper


_CLI_QUICKREF: str = (
    "[cli-quickref] "
    "stripe: `<resource> <op> [--k=v | -d k=v]`; negative/credit adjustment = `stripe invoiceitems create --customer=ID --currency=usd --amount=-CENTS`; `invoices finalize-invoice ID` | "
    "jira: `issue list [--raw]|view|create|edit|comment add|move KEY \"Exact State\"|link IN OUT TYPE` | "
    "dws: `doc read|list|create|comment --node ...`; `doc block list|insert|update|delete --node ...`; `schema` | "
    "box: `folders:items 0|folders:create|files:get|files:upload|collaborations:create|comments:create` | "
    "gws: `sheets get-text --spreadsheet-id ID`; `slides ... --presentation-id ID` | "
    "ntn: `pages get|create|update`; `datasources resolve|query`; `files list`; `api` | "
    "todoist: `list|show|add|modify ID [--section-name S --label-names L]|close|delete`"
)


def format_workspace_inventory(run_dir: Path, task_markdown: str = "") -> str:
    """Render compact Turn-0 Birth Gate workspace files, deliverable contract, and CLI quickref."""
    ws_dir = (run_dir / "workspace").resolve()
    rd_dir = run_dir.resolve()
    inv = ""
    if ws_dir.is_dir():
        wfiles = sorted(wf for wf in ws_dir.rglob("*") if wf.is_file() and not wf.name.startswith("."))
        if 1 <= len(wfiles) <= 80:
            items: list[str] = []
            for wf in wfiles:
                rel_f = wf.relative_to(rd_dir).as_posix()
                sz = wf.stat().st_size
                sz_s = (
                    f"{sz} B"
                    if sz < 1024
                    else f"{sz / 1024:.1f} KB"
                    if sz < 1024 * 1024
                    else f"{sz / (1024 * 1024):.1f} MB"
                )
                items.append(f"/task/{rel_f} ({sz_s})")
            raw_inv = f"[workspace files ({len(wfiles)}): " + ", ".join(items) + "]"
            inv = raw_inv if len(raw_inv) <= 1800 else (raw_inv[:1760] + ", ...]")

    resolved_md = (task_markdown or "").strip()
    if not resolved_md:
        for cand in (
            rd_dir / "TASK.md",
            rd_dir / "task.md",
            rd_dir / "mock_case" / "task.md",
            ws_dir / "TASK.md",
            ws_dir / "task.md",
        ):
            if cand.is_file():
                try:
                    resolved_md = cand.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    resolved_md = ""
                if resolved_md:
                    break

    if not inv and not resolved_md:
        return ""

    parts: list[str] = []
    if inv:
        parts.append(inv)
    if resolved_md:
        contract = str(format_deliverable_contract(resolved_md, rd_dir) or "").strip()
        if contract:
            parts.append(contract)
    parts.append(_CLI_QUICKREF)
    return "\n".join(parts)


def canonical_tool_sig(name: str, args: dict[str, Any], *, normalize_meta: bool = True) -> str:
    """Produce a deterministic signature for duplicate-call detection."""
    if normalize_meta:
        clean = {k: v for k, v in (args or {}).items() if k not in ("state_note", "description")}
    else:
        clean = args or {}
    return f"{name}:{json.dumps(clean, sort_keys=True, ensure_ascii=False)}"


def is_mutating_tool_call(name: str, args: dict[str, Any]) -> bool:
    """Classify whether a tool call mutates persistent file, service, or UI state."""
    if name in ("write", "edit"):
        return True
    if name in MUTATING_BROWSER_TOOLS and name not in ("browser_open", "browser_navigate"):
        return True
    if name == "bash":
        return bool(MUTATING_SHELL_RE.search(str((args or {}).get("command") or "")))
    return False
