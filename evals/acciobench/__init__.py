"""AccioBench (CommerceAgentBench) modular benchmark harness for Straitjacket."""

from __future__ import annotations

import importlib
from typing import Any

from .config import (
    BenchConfig,
    RunMetrics,
    discover_all_tasks,
    load_cab_task,
    resolve_ctx_bin,
    select_tasks,
)
from .ports import CompiledPortMap, discover_task_ports, rewrite_ports_surgical
from .sandbox import TaskSandbox

__all__ = [
    "BenchConfig",
    "BrowserPool",
    "BrowserSession",
    "CompiledPortMap",
    "ModelProvider",
    "RunMetrics",
    "SJ_EXECUTION_PROTOCOL",
    "TaskSandbox",
    "ToolHost",
    "build_html_report_payload",
    "build_prompt",
    "build_tools",
    "canonical_tool_sig",
    "create_provider",
    "discover_all_tasks",
    "discover_task_ports",
    "format_workspace_inventory",
    "is_mutating_tool_call",
    "load_cab_task",
    "load_run_results",
    "render_html_report",
    "render_summary_report",
    "resolve_ctx_bin",
    "rewrite_ports_surgical",
    "run_doctor",
    "run_suite",
    "run_trial",
    "select_tasks",
    "validate_task_referee",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "SJ_EXECUTION_PROTOCOL": (".schemas", "SJ_EXECUTION_PROTOCOL"),
    "build_prompt": (".schemas", "build_prompt"),
    "build_tools": (".schemas", "build_tools"),
    "canonical_tool_sig": (".schemas", "canonical_tool_sig"),
    "format_workspace_inventory": (".schemas", "format_workspace_inventory"),
    "is_mutating_tool_call": (".schemas", "is_mutating_tool_call"),
    "BrowserPool": (".browser", "BrowserPool"),
    "BrowserSession": (".browser", "BrowserSession"),
    "ToolHost": (".host", "ToolHost"),
    "ModelProvider": (".providers", "ModelProvider"),
    "create_provider": (".providers", "create_provider"),
    "run_trial": (".loop", "run_trial"),
    "run_suite": (".orchestrator", "run_suite"),
    "load_run_results": (".report", "load_run_results"),
    "render_summary_report": (".report", "render_summary_report"),
    "build_html_report_payload": (".html_report", "build_html_report_payload"),
    "render_html_report": (".html_report", "render_html_report"),
    "run_doctor": (".validate", "run_doctor"),
    "validate_task_referee": (".validate", "validate_task_referee"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    mod = importlib.import_module(mod_name, __name__)
    return getattr(mod, attr_name)
