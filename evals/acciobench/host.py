"""Tool execution host supporting paired `raw` baseline and `sj` Straitjacket arms."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import glob as pyglob
import hashlib
import html as htmlmod
import json
import mimetypes
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
from typing import Any
import urllib.parse
import urllib.request

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_SRC = _REPO_ROOT / "src"
for _p in (_REPO_ROOT, _REPO_SRC):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ctx.deliverables import audit_deliverables  # noqa: E402
from ctx.residence import extract_mutation_receipts  # noqa: E402
from ctx.sessiondir import session_reads_path  # noqa: E402

from evals.acciobench.browser import BrowserSession
from evals.acciobench.config import BenchConfig, RunMetrics, resolve_ctx_bin
from evals.acciobench.ports import CompiledPortMap, rewrite_ports_surgical
from evals.acciobench.schemas import (
    MUTATING_BROWSER_TOOLS,
    MUTATING_SHELL_RE,
    READONLY_TOOLS,
    canonical_tool_sig,
)

__all__ = [
    "IMAGE_EXTS",
    "ToolHost",
    "bwrap_sandbox_executable",
    "extract_pdf_text",
    "format_numbered_lines",
    "has_user_deliverables",
    "parse_pdf_pages",
]

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_TASK_PATH_RE = re.compile(r"(^|(?<![A-Za-z0-9_.-]))/task(?=/|$|[^A-Za-z0-9_-])")
_DIGEST_SIZE_RE = re.compile(r"stdout:\s*\d+\s*lines\s*\u00b7\s*([\d.]+)\s*(B|KiB|MiB)")
_SHELL_HISTORY_RE = re.compile(r"\.(?:bash|zsh)_history\b")
_INLINE_PY_C_RE = re.compile(r"\bpython3?(?:\.\d+)?\b[^\n]*\s-c\s")
_PY_PARSE_ERR_RE = re.compile(
    r"(?:SyntaxError:|IndentationError:|unterminated string literal|unexpected EOF while parsing)"
)
_BWRAP_PROBE_CACHE: tuple[bool, str | None] = (False, None)
_ROOT_BWRAP_MOUNTS_CACHE: list[str] | None = None


def _root_bwrap_mounts() -> list[str]:
    global _ROOT_BWRAP_MOUNTS_CACHE
    if _ROOT_BWRAP_MOUNTS_CACHE is not None:
        return list(_ROOT_BWRAP_MOUNTS_CACHE)
    mounts: list[str] = []
    skip = {"proc", "dev", "tmp", "task"}
    try:
        entries = sorted(os.listdir("/"))
    except OSError:
        entries = []
    for entry in entries:
        if entry in skip:
            continue
        p = "/" + entry
        if os.path.islink(p):
            try:
                mounts.extend(["--symlink", os.readlink(p), p])
            except OSError:
                continue
        elif os.path.isdir(p) or os.path.isfile(p):
            mounts.extend(["--ro-bind-try", p, p])
    _ROOT_BWRAP_MOUNTS_CACHE = mounts
    return list(mounts)


def bwrap_sandbox_executable() -> str | None:
    """Return usable ``bwrap`` binary path on Linux when mount + PID namespaces work, else ``None``."""
    global _BWRAP_PROBE_CACHE
    if os.environ.get("ACCIOBENCH_NO_BWRAP", "").strip().lower() in ("1", "true", "yes"):
        return None
    probed, cached_bin = _BWRAP_PROBE_CACHE
    if probed:
        return cached_bin
    if sys.platform != "linux":
        _BWRAP_PROBE_CACHE = (True, None)
        return None
    bwrap_bin = shutil.which("bwrap")
    if not bwrap_bin:
        _BWRAP_PROBE_CACHE = (True, None)
        return None
    try:
        cp = subprocess.run(
            [
                bwrap_bin,
                "--die-with-parent",
                "--unshare-pid",
                *_root_bwrap_mounts(),
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ok_bin = bwrap_bin if cp.returncode == 0 else None
    except Exception:
        ok_bin = None
    _BWRAP_PROBE_CACHE = (True, ok_bin)
    return ok_bin
_UNTITLED_CREATE_RE = re.compile(
    r'(?:Created|Added)\s+(?:page|doc|document|database|sheet)\s+"Untitled"\s*\(([A-Za-z0-9_-]{4,64})\)',
    re.I,
)
_RESOURCE_CLEANUP_RE = re.compile(
    r"\b(?:trash|trashed|deleted|archived|updated|renamed|moved\s+to\s+trash|unlinked|removed)\b",
    re.I,
)
_EXT_SERVICE_TARGET_RE = re.compile(
    r"\b(?:stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)\b"
    r"|https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d+"
    r"|[\"']method[\"']\s*:\s*[\"']tools/call[\"']",
    re.I,
)
_EXT_CLI_LINE_RE = re.compile(
    r"^\s*(?:env\s+[A-Z0-9_]+=\S+\s+)*(stripe|todoist|jira|box|dws|gws|ntn|shopify|gh)\s+(.+?)\s*$",
    re.I,
)
_NON_IDEMPOTENT_CLI_VERB_RE = re.compile(
    r"\b(?:create|add|upload|link|append|insert|send|draft|duplicate|copy)\b",
    re.I,
)
_CLEANUP_CLI_VERB_RE = re.compile(
    r"\b(?:delete|trash|unlink|remove|rm|void|cancel)\b",
    re.I,
)
_READBACK_CLI_VERB_RE = re.compile(
    r"\b(?:list|ls|get|show|view|search|find|status|retrieve|info|read|export|whoami)\b",
    re.I,
)
_MUTATING_CLI_VERB_RE = re.compile(
    r"\b(?:create|update|delete|add|complete|close|reopen|move|assign|comment|transition|"
    r"upload|mkdir|cp|mv|rm|share|collaborations|append|replace|insert|batch|send|draft|"
    r"trash|archive|finalize|pay|void|cancel|confirm|capture|refund|edit|patch|post|put|"
    r"submit|publish|seed|import|modify|link|resolve|star|unstar|restore)\b",
    re.I,
)
_HTTP_MUTATION_RE = re.compile(
    r"\bcurl\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)"
    r"|urllib\.request\.Request\b.*(?:\bdata\s*=|\bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE))"
    r"|\b(?:requests|httpx)\.(?:post|put|patch|delete)\b",
    re.I | re.S,
)
_HTTP_READBACK_RE = re.compile(
    r"\b(?:requests|httpx)\.get\b"
    r"|urllib\.request\.urlopen\b"
    r"|\bcurl\b(?![^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s))[^\n]*https?://",
    re.I,
)
_MCP_READONLY_CALL_RE = re.compile(
    r"[\"']method[\"']\s*:\s*[\"'](?:tools/list|resources/list|resources/read)[\"']"
    r"|[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*\b(?:list|get|read|search|query|fetch|find|inspect|describe|status)[A-Za-z0-9_.-]*[\"']",
    re.I | re.S,
)
_MCP_MUTATING_CALL_RE = re.compile(
    r"[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*(?:create|update|delete|remove|modify|send|reply|draft|star|unstar|archive|trash|label|add|close|reopen|complete|upload|insert|append|replace|batch|transition|assign|comment|link|merge)[A-Za-z0-9_.-]*[\"']",
    re.I | re.S,
)
_NAMED_CREATE_RECEIPT_RE = re.compile(
    r'(?:✓|✔|SUCCESS:?)\s*(Created|Uploaded|Added|Linked)\s+'
    r'(page|file|doc|document|sheet|database|task|issue|draft|folder)\s+"([^"]{2,120})"',
    re.I,
)


def format_numbered_lines(
    lines: list[str],
    start_idx: int,
    total_lines: int,
    extra_note: str = "",
) -> str:
    if not lines:
        return f"(empty slice; Total: {total_lines} lines{extra_note}.)"
    rendered = [f"{i:05d}| {ln}" for i, ln in enumerate(lines, start=start_idx + 1)]
    return "\n".join(rendered) + f"\n\n(Total: {total_lines} lines{extra_note}.)"


def parse_pdf_pages(pages_spec: str | None) -> tuple[int | None, int | None]:
    if not pages_spec:
        return None, None
    s = str(pages_spec).strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r"^(\d+)$", s)
    if m2:
        p = int(m2.group(1))
        return p, p
    return None, None


def extract_pdf_text(pdf_path: Path, pages_spec: str | None) -> tuple[str, str]:
    pdftotext_bin = shutil.which("pdftotext") or "/usr/bin/pdftotext"
    p_start, p_end = parse_pdf_pages(pages_spec)
    cmd = [pdftotext_bin, "-layout"]
    note = ""
    if p_start is not None and p_end is not None:
        cmd.extend(["-f", str(p_start), "-l", str(p_end)])
        note = f", pages {p_start}-{p_end}"
    cmd.extend([str(pdf_path), "-"])
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "ERROR: pdftotext binary is not installed.", note
    if cp.returncode != 0 and not cp.stdout:
        return f"ERROR: pdftotext failed: {cp.stderr.strip()[:300]}", note
    return cp.stdout, note


def has_user_deliverables(outputs_dir: Path) -> bool:
    if not outputs_dir.exists():
        return False
    for p in outputs_dir.rglob("*"):
        if p.is_file() and "mock_audit" not in p.parts and "mock_state" not in p.parts:
            return True
    return False


def _estimate_digest_raw_bytes(digest_text: str) -> int:
    m = _DIGEST_SIZE_RE.search(digest_text)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "KiB":
        return int(val * 1024)
    if unit == "MiB":
        return int(val * 1024 * 1024)
    return int(val)


class ToolHost:
    """Executes tool calls inside an isolated trial sandbox for `arm='sj'` or `arm='raw'`."""

    def __init__(
        self,
        config: BenchConfig,
        run_dir: Path,
        meta_dir: Path,
        port_map: dict[int, int] | CompiledPortMap,
        mock_env: dict[str, str],
        metrics: RunMetrics,
        browser: BrowserSession | None,
        *,
        arm: str = "sj",
        max_steps: int = 135,
        declared_deliverables: list[str] | None = None,
        use_bwrap: bool | None = None,
    ):
        arm_norm = arm.strip().lower()
        if arm_norm not in ("sj", "raw"):
            raise ValueError(f"Unsupported arm {arm!r}; expected 'sj' or 'raw'")

        self.config = config
        self.run_dir = run_dir.resolve()
        self.meta_dir = meta_dir.resolve()
        self.compiled_ports: CompiledPortMap = (
            port_map if isinstance(port_map, CompiledPortMap) else CompiledPortMap(dict(port_map or {}))
        )
        self.port_map: dict[int, int] = self.compiled_ports.mapping
        self.mock_env = dict(mock_env or {})
        self.metrics = metrics
        self.browser = browser
        self.arm = arm_norm
        self.is_sj = arm_norm == "sj"
        self.max_steps = max_steps

        self.completed: bool = False
        self.step: int = 0
        self.last_call_was_dup: bool = False
        self.mutation_epoch: int = 0
        self.seen: dict[tuple[int, str], tuple[int, dict[str, Any]]] = {}
        self.file_reads: dict[tuple[str, str, int, int, str], tuple[int, str]] = {}
        self.micro_py_streak: int = 0
        self.micro_hints: int = 0
        self.http400_streak: int = 0
        self.http400_hints: int = 0
        self.taught_bash_ctx: bool = False
        self.emission_gated_once: bool = False
        self.cli_enriched_cmds: set[str] = set()
        self.declared_deliverables: list[str] = list(declared_deliverables or [])
        self.untrashed_untitled_ids: dict[str, str] = {}
        self.external_mutation_count: int = 0
        self.last_external_mutation_step: int = 0
        self.last_external_readback_step: int = 0
        self._non_idempotent_cmd_counts: dict[str, int] = {}
        self._non_idempotent_receipt_counts: dict[str, int] = {}
        self.duplicate_external_mutations: dict[str, str] = {}

        self.bin_dir = self.meta_dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_tmp = self.meta_dir / "sandbox_tmp"
        self.sandbox_tmp.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "tmp").mkdir(parents=True, exist_ok=True)
        self.ctx_ws = self.meta_dir / "ctxws"
        self.ctx_extract_dir = self.meta_dir / "extracts"
        self._ctx_cmd_prefix: list[str] = []
        bwrap_cand = bwrap_sandbox_executable() if use_bwrap is not False else None
        self._bwrap_bin: str | None = bwrap_cand if (use_bwrap is None or use_bwrap) else None
        self._use_bwrap: bool = self._bwrap_bin is not None

        raw_path_parts: list[str] = [str(self.bin_dir)]
        if self.is_sj:
            ctx_bin = resolve_ctx_bin(config.ctx_bin)
            if "python" in ctx_bin.name.lower():
                self._ctx_cmd_prefix = [str(ctx_bin), "-m", "ctx"]
                shim_exec = f'exec "{ctx_bin}" -m ctx --workspace "{self.ctx_ws}" "$@"'
            else:
                self._ctx_cmd_prefix = [str(ctx_bin)]
                shim_exec = f'exec "{ctx_bin}" --workspace "{self.ctx_ws}" "$@"'
            if ctx_bin.parent.is_dir():
                raw_path_parts.append(str(ctx_bin.absolute().parent))

            self.ctx_ws.mkdir(parents=True, exist_ok=True)
            self.ctx_extract_dir.mkdir(parents=True, exist_ok=True)
            shim = self.bin_dir / "ctx"
            shim.write_text(f"#!/usr/bin/env bash\n{shim_exec}\n", encoding="utf-8")
            shim.chmod(0o755)
            (self.ctx_ws / "ctx.toml").write_text(
                'version = 1\n[workspace]\nallow_outside_root = true\n[guard]\nmode = "advisory"\n',
                encoding="utf-8",
            )
            config.ctx_state_home.mkdir(parents=True, exist_ok=True)

        for venv_bin in (Path(sys.executable).absolute().parent, Path(sys.prefix) / "bin"):
            if venv_bin.is_dir():
                raw_path_parts.append(str(venv_bin))
        if config.runtime_dir is not None and (config.runtime_dir / "bin").is_dir():
            raw_path_parts.append(str((config.runtime_dir / "bin").resolve()))
        for source_path in (self.mock_env.get("PATH", ""), os.environ.get("PATH", "/usr/bin:/bin")):
            for seg in source_path.split(os.pathsep):
                if seg:
                    raw_path_parts.append(seg)
        path_env = os.pathsep.join(dict.fromkeys(raw_path_parts))

        client_env = {
            k: v
            for k, v in self.mock_env.items()
            if k.startswith(("JIRA_", "TODOIST_TOKEN", "STRIPE_API_KEY"))
            and "STATE_DIR" not in k
            and "MOCK_HOME" not in k
            and "MOCK_DB" not in k
        }
        base_env = dict(os.environ)
        base_env.update(client_env)
        if "SJ_SLOT_TOKEN" in self.mock_env:
            base_env["SJ_SLOT_TOKEN"] = self.mock_env["SJ_SLOT_TOKEN"]
        no_proxy_items = [
            x.strip()
            for x in (
                self.mock_env.get("NO_PROXY")
                or base_env.get("NO_PROXY")
                or base_env.get("no_proxy")
                or ""
            ).split(",")
            if x.strip()
        ]
        for host_tok in ("127.0.0.1", "localhost", "0.0.0.0"):
            if host_tok not in no_proxy_items:
                no_proxy_items.append(host_tok)
        no_proxy_val = ",".join(no_proxy_items)
        base_env["NO_PROXY"] = no_proxy_val
        base_env["no_proxy"] = no_proxy_val
        base_env["PATH"] = path_env
        base_env["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"
        base_env["GIT_CEILING_DIRECTORIES"] = (
            f"{self.run_dir.parent}{os.pathsep}{self.run_dir.parent.parent}"
        )
        if self.is_sj:
            base_env["CTX_STATE_HOME"] = str(config.ctx_state_home)
        self.env = base_env
        support_dirs: list[Path] = [Path(sys.prefix).resolve(), _REPO_SRC.resolve()]
        for seg in dict.fromkeys(raw_path_parts):
            pseg = Path(seg)
            if pseg.is_dir():
                support_dirs.append(pseg.resolve())
        self._support_ro_dirs: tuple[Path, ...] = tuple(dict.fromkeys(support_dirs))

    @property
    def res(self) -> RunMetrics:
        return self.metrics

    def has_user_deliverables(self) -> bool:
        return has_user_deliverables(self.run_dir / "outputs")

    def update_window_telemetry(self, model_id: str, step: int, usage: dict[str, Any]) -> None:
        if not self.is_sj:
            return
        try:
            pdir = session_reads_path(self.ctx_ws, "proxy")
            pdir.mkdir(parents=True, exist_ok=True)
            prompt_tok = int(usage.get("promptTokenCount", 0) or 0)
            cached_tok = int(usage.get("cachedContentTokenCount", 0) or 0)
            thought_tok = int(usage.get("thoughtsTokenCount", 0) or 0)
            out_tok = int(usage.get("candidatesTokenCount", 0) or 0)
            limit = 1_048_576
            doc = {
                "model": model_id,
                "requests": step,
                "last_input_tokens": prompt_tok,
                "turn_carriage_tokens": prompt_tok,
                "cached_tokens": cached_tok,
                "last_thought_tokens": thought_tok,
                "last_output_tokens": out_tok,
                "context_limit": limit,
                "window_pct": round(100.0 * prompt_tok / limit, 2) if prompt_tok else 0.0,
                "cum_cache_read": self.metrics.cached_tokens,
                "cum_output": self.metrics.output_tokens + self.metrics.thought_tokens,
                "workspace_id": self.ctx_ws.name,
            }
            tmp = pdir / f"window.json.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
            os.replace(tmp, pdir / "window.json")
        except Exception:
            pass

    def norm(self, raw: str) -> Path:
        p = (raw or "").strip()
        if p.startswith("/task/"):
            p = p[len("/task/") :]
        elif p == "/task":
            p = "."
        elif p == "/tmp":
            return self.sandbox_tmp
        elif p.startswith("/tmp/"):
            rel_tmp = p[len("/tmp/") :]
            cand_stmp = (self.sandbox_tmp / rel_tmp).resolve()
            cand_rtmp = (self.run_dir / "tmp" / rel_tmp).resolve()
            if not cand_stmp.exists() and cand_rtmp.exists():
                return cand_rtmp
            return cand_stmp
        elif p.startswith("/root/"):
            p = "tmp/" + Path(p).name
        elif p.startswith(("/workspace/", "/outputs/")) and not Path(p).exists():
            p = p.lstrip("/")
        elif p.startswith("/"):
            return Path(p).resolve()
        return (self.run_dir / p).resolve()

    def _is_blocked_host_path(self, resolved: Path) -> bool:
        rp = resolved.resolve()
        if rp == self.run_dir or rp.is_relative_to(self.run_dir):
            return False
        if rp == self.sandbox_tmp or rp.is_relative_to(self.sandbox_tmp):
            return False
        for secret_root in (self.config.dataset_root, self.config.workdir):
            try:
                sr = secret_root.resolve()
                if rp == sr or rp.is_relative_to(sr):
                    return True
            except OSError:
                continue
        return False

    def rewrite_ports(self, text: str) -> str:
        if self.compiled_ports:
            return rewrite_ports_surgical(text, self.compiled_ports, is_service_script=False)
        return text

    def rewrite(self, cmd: str) -> str:
        c = _TASK_PATH_RE.sub(lambda m: f"{m.group(1)}{self.run_dir}", cmd)
        return self.rewrite_ports(c)

    def rewrite_guest(self, text: str) -> str:
        return self.rewrite_ports(text) if self._use_bwrap else self.rewrite(text)

    def _guest_workdir(self, wd: Path) -> str:
        wd_res = wd.resolve()
        if wd_res == self.run_dir:
            return "/task"
        if wd_res.is_relative_to(self.run_dir):
            return f"/task/{wd_res.relative_to(self.run_dir).as_posix()}"
        if wd_res == self.sandbox_tmp:
            return "/tmp"
        if wd_res.is_relative_to(self.sandbox_tmp):
            return f"/tmp/{wd_res.relative_to(self.sandbox_tmp).as_posix()}"
        return "/task"

    def _bwrap_mount_args(self, guest_wd: str) -> list[str]:
        args: list[str] = [
            "--die-with-parent",
            "--unshare-pid",
            *_root_bwrap_mounts(),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(self.sandbox_tmp),
            "/tmp",
        ]
        tmp_root = Path("/tmp").resolve()
        mask_candidates: list[Path] = []
        for mdir in (self.config.dataset_root, self.config.workdir):
            try:
                mr = mdir.resolve()
                if mr.is_dir() and mr != Path("/"):
                    mask_candidates.append(mr)
            except OSError:
                continue
        mask_dirs: list[Path] = []
        for mr in sorted(mask_candidates, key=lambda p: (len(p.parts), str(p))):
            if mr not in mask_dirs:
                mask_dirs.append(mr)
                args.extend(["--tmpfs", str(mr)])

        def _under_any(p: Path, roots: list[Path]) -> bool:
            return any(p == r or p.is_relative_to(r) for r in roots)

        masked_or_tmp = [tmp_root, *mask_dirs]
        ds_domain = (self.config.dataset_root / "datasets_domain_v1").resolve()
        wd_resolved = self.config.workdir.resolve()

        ro_rebind: list[Path] = []
        if self.config.runtime_dir is not None and self.config.runtime_dir.is_dir():
            rt = self.config.runtime_dir.resolve()
            if (
                _under_any(rt, masked_or_tmp)
                and not rt.is_relative_to(wd_resolved)
                and not any(m == rt or m.is_relative_to(rt) for m in mask_dirs)
            ):
                ro_rebind.append(rt)
        cab_mocks = (self.config.dataset_root / "real_replica_bench" / "mock_services").resolve()
        if cab_mocks.is_dir():
            ro_rebind.append(cab_mocks)
        for pdir in self._support_ro_dirs:
            if (
                pdir.is_dir()
                and _under_any(pdir, masked_or_tmp)
                and not pdir.is_relative_to(wd_resolved)
                and not pdir.is_relative_to(ds_domain)
                and not any(m == pdir or m.is_relative_to(pdir) for m in mask_dirs)
            ):
                ro_rebind.append(pdir)
        ro_rebind.append(self.bin_dir)
        ms_dir = self.meta_dir / "mock_services"
        if ms_dir.is_dir():
            ro_rebind.append(ms_dir)
        if self.is_sj and self.ctx_extract_dir.is_dir():
            ro_rebind.append(self.ctx_extract_dir)

        seen_ro: set[Path] = set()
        for rp in sorted(ro_rebind, key=lambda p: (len(p.parts), str(p))):
            if rp not in seen_ro and not any(rp.is_relative_to(prev) for prev in seen_ro):
                seen_ro.add(rp)
                args.extend(["--ro-bind", str(rp), str(rp)])

        rw_binds: list[tuple[Path, str]] = [
            (self.run_dir, "/task"),
            (self.run_dir, str(self.run_dir)),
        ]
        mstate_dir = self.meta_dir / "mock_state"
        if mstate_dir.is_dir():
            rw_binds.append((mstate_dir, str(mstate_dir)))
        if self.is_sj:
            rw_binds.append((self.ctx_ws, str(self.ctx_ws)))
            shome = self.config.ctx_state_home.resolve()
            shome.mkdir(parents=True, exist_ok=True)
            rw_binds.append((shome, str(shome)))
        seen_dsts: set[str] = set()
        for src_p, dst_s in sorted(rw_binds, key=lambda item: (len(Path(item[1]).parts), item[1])):
            if dst_s not in seen_dsts:
                seen_dsts.add(dst_s)
                args.extend(["--bind", str(src_p), dst_s])

        args.extend(
            [
                "--chdir",
                guest_wd,
                "--setenv",
                "PWD",
                guest_wd,
                "--setenv",
                "HOME",
                "/task/tmp",
            ]
        )
        return args

    def _run_in_sandbox(
        self,
        wd: Path,
        inner_argv: list[str],
        *,
        timeout: float = 120.0,
        merge_stderr: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        stderr_dest = subprocess.STDOUT if merge_stderr else subprocess.PIPE
        if not self._use_bwrap or self._bwrap_bin is None:
            return subprocess.run(
                inner_argv,
                cwd=str(wd),
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=stderr_dest,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        mount_args = self._bwrap_mount_args(self._guest_workdir(wd))
        payload = b"\0".join(a.encode("utf-8") for a in mount_args) + b"\0"
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, payload)
        finally:
            os.close(w_fd)
        try:
            return subprocess.run(
                [self._bwrap_bin, "--args", str(r_fd), "--", *inner_argv],
                pass_fds=(r_fd,),
                cwd=str(wd),
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=stderr_dest,
                text=True,
                errors="replace",
                timeout=timeout,
            )
        finally:
            os.close(r_fd)

    def _rewrite_browser_url(self, url: str) -> str:
        u = url.strip()
        if u.startswith("file:///task/"):
            u = f"file://{self.run_dir}/{u[len('file:///task/'):]}"
        elif u == "file:///task":
            u = f"file://{self.run_dir}"
        return u

    def ctx(self, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._ctx_cmd_prefix, "--workspace", str(self.ctx_ws), *args],
            cwd=str(self.run_dir),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _format_digest_delivery(
        self,
        digest_text: str,
        raw_bytes_est: int = 0,
        stderr_tail: str = "",
    ) -> str:
        self.metrics.digests_delivered += 1
        text = digest_text
        if stderr_tail:
            text += "\nstderr (tail): " + stderr_tail[-600:]
        est = raw_bytes_est if raw_bytes_est > 0 else _estimate_digest_raw_bytes(digest_text)
        if est > 0:
            self.metrics.bytes_kept_out += max(0, est - len(text.encode("utf-8")))
        m_ref = re.search(r"\b(run:[0-9a-f]{6,64})\b", digest_text)
        ref_ex = m_ref.group(1) if m_ref else "run:<id>"
        self.taught_bash_ctx = True
        text += (
            f"\n[ctx-retrieve] Full output stored at {ref_ex}; call "
            f"ctx_retrieve(handle={ref_ex!r}, mode='get', lines='1:120') "
            "(or json_pointer='/0', mode='search', pattern='...') to inspect omitted records."
        )
        return text

    def capture_large_text(self, label: str, content: str) -> str:
        if not self.is_sj:
            self.metrics.inline_delivered += 1
            return content[: self.config.baseline_raw_cap]
        raw_bytes = len(content.encode("utf-8"))
        if raw_bytes <= self.config.entry_gate_bytes:
            self.metrics.inline_delivered += 1
            return content
        tmp_f = self.ctx_extract_dir / f"{label}_s{self.step}.txt"
        tmp_f.write_text(content, encoding="utf-8")
        return self.ctx_run(f"cat {shlex.quote(str(tmp_f))}", raw_bytes_hint=raw_bytes)

    def _raw_shell_run(self, shell_cmd: str, cwd_override: Path | None = None) -> str:
        if _SHELL_HISTORY_RE.search(shell_cmd):
            return "ERROR: shell history outside /task is unavailable"
        wd_path = cwd_override or self.run_dir
        try:
            cp = self._run_in_sandbox(wd_path, ["bash", "-c", shell_cmd], timeout=120.0)
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds."
        self.metrics.inline_delivered += 1
        out = cp.stdout or ""
        err = cp.stderr or ""
        text = out + (("\n" + err) if err.strip() else "")
        if cp.returncode != 0:
            text = f"[exit {cp.returncode}]\n" + text
        return (text.strip() or "(command executed with no output)")[: self.config.baseline_raw_cap]

    def ctx_run(
        self,
        shell_cmd: str,
        raw_bytes_hint: int = 0,
        cwd_override: Path | None = None,
    ) -> str:
        if _SHELL_HISTORY_RE.search(shell_cmd):
            return "[ctx-guard: host shell history outside /task is unavailable]"
        wd_path = cwd_override or self.run_dir
        cwd_arg = self._guest_workdir(wd_path) if self._use_bwrap else str(wd_path)
        ctx_argv = [
            *self._ctx_cmd_prefix,
            "--workspace",
            str(self.ctx_ws),
            "run",
            "--json",
            "--cwd",
            cwd_arg,
            "--shell",
            shell_cmd,
        ]
        try:
            cp = self._run_in_sandbox(wd_path, ctx_argv, timeout=120.0)
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out after 120 seconds."
        raw = cp.stdout.strip()
        try:
            d = json.loads(raw[raw.index("{") :]) if "{" in raw else {}
        except Exception:
            d = {}
        if not d:
            return (cp.stdout + cp.stderr).strip()[:12000] or "(command executed with no output)"
        code = d.get("command_exit_code")
        if d.get("delivery") == "digest":
            return self._format_digest_delivery(
                d.get("digest", ""),
                raw_bytes_est=raw_bytes_hint,
                stderr_tail=(d.get("stderr") or "").strip(),
            )
        self.metrics.inline_delivered += 1
        out = d.get("stdout") or ""
        err = d.get("stderr") or ""
        text = out + (("\n" + err) if err.strip() else "")
        if code not in (0, None):
            text = f"[exit {code}]\n" + text
        return text.strip() or "(command executed with no output)"

    def ctx_direct(self, args_str: str) -> str:
        try:
            argv = shlex.split(args_str)
        except ValueError as e:
            return f"ERROR: could not parse ctx arguments: {e}"
        if not argv:
            return "ERROR: empty ctx arguments"
        cleaned: list[str] = []
        i = 0
        while i < len(argv):
            if argv[i] == "--workspace" and i + 1 < len(argv):
                i += 2
                continue
            if argv[i].startswith("--workspace="):
                i += 1
                continue
            cleaned.append(self.rewrite(argv[i]))
            i += 1
        if not cleaned:
            return "ERROR: empty ctx subcommand"
        verb = cleaned[0]
        self.metrics.ctx_retrievals[verb] = self.metrics.ctx_retrievals.get(verb, 0) + 1
        try:
            cp = self.ctx(cleaned)
        except subprocess.TimeoutExpired:
            return "ERROR: ctx command timed out."
        text = (cp.stdout + (("\n" + cp.stderr) if cp.stderr.strip() else "")).strip()
        return text[:20000] or f"(ctx {verb}: no output, exit {cp.returncode})"

    def _maybe_enrich_cli_help(self, cmd_rw: str, out: str) -> str:
        if not self.is_sj:
            return out
        s = cmd_rw.strip()
        if (
            s in self.cli_enriched_cmds
            or any(op in s for op in ("&&", "||", ";", "\n", "|", ">"))
            or out.startswith("[exit ")
            or len(out) > 8000
        ):
            return out
        m_help = re.match(r"^(.+?)\s+(?:--help|-h)$", s)
        if not m_help:
            return out
        base_cmd = m_help.group(1).strip()
        self.cli_enriched_cmds.add(s)

        subcmds: list[str] = []
        for grp in re.findall(r"\{([a-zA-Z0-9_,-]{3,240})\}", out):
            for tok in grp.split(","):
                tok = tok.strip()
                if re.match(r"^[a-zA-Z][a-zA-Z0-9_:-]{1,32}$", tok) and tok not in subcmds:
                    subcmds.append(tok)
        in_cmds = False
        for ln in out.splitlines():
            if re.match(r"^\s*(?:available\s+)?(?:sub)?commands\s*:", ln, re.I):
                in_cmds = True
                continue
            if in_cmds:
                if ln.strip() == "" or re.match(r"^[A-Za-z].*:\s*$", ln):
                    if subcmds:
                        in_cmds = False
                    continue
                m_sc = re.match(r"^\s{2,8}([a-z][a-z0-9_:-]{1,30})(?:\s{2,}|\t|$)", ln)
                if m_sc and m_sc.group(1) not in subcmds:
                    subcmds.append(m_sc.group(1))

        if not (1 <= len(subcmds) <= 10):
            return out

        targets = subcmds[:8]

        def _probe(sc: str) -> str | None:
            probe_sh = f"{base_cmd} {shlex.quote(sc)} --help"
            try:
                cp = self._run_in_sandbox(
                    self.run_dir,
                    ["bash", "-c", probe_sh],
                    timeout=2.5,
                    merge_stderr=True,
                )
                if cp.returncode != 0 or not cp.stdout:
                    return None
                usage_ln = ""
                for hl in cp.stdout.splitlines():
                    if hl.lower().strip().startswith("usage:"):
                        usage_ln = hl.strip()
                        break
                flags = list(dict.fromkeys(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9_-]*", cp.stdout)))
                flags = [f for f in flags if f != "--help"]
                if usage_ln or flags:
                    fl_s = ", ".join(flags[:14])
                    entry = f"  {sc}: {usage_ln}" + (f" | flags: {fl_s}" if fl_s else "")
                    return entry[:260]
            except Exception:
                return None
            return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            probed = list(pool.map(_probe, targets))
        lines = [ln for ln in probed if ln]
        if not lines:
            return out
        self.metrics.cli_lsp_enrichments += 1
        return out + ("\n\n[cli-lsp subcommand signatures]\n" + "\n".join(lines))[:2400]

    def _workspace_fs_fingerprint(self) -> dict[str, tuple[int, int]]:
        """Capture fast ``(size, mtime_ns)`` snapshot of ``outputs/`` and ``workspace/``."""
        snap: dict[str, tuple[int, int]] = {}
        count = 0
        for sub in ("outputs", "workspace"):
            root = self.run_dir / sub
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if count >= 600:
                    return snap
                if not p.is_file():
                    continue
                if any(ig in p.parts for ig in ("mock_audit", "mock_state", ".git", "__pycache__")):
                    continue
                try:
                    st = p.stat()
                    snap[p.relative_to(self.run_dir).as_posix()] = (st.st_size, st.st_mtime_ns)
                    count += 1
                except OSError:
                    continue
        return snap

    def _track_untitled_creations(self, out_txt: str) -> None:
        if not out_txt:
            return
        if self.untrashed_untitled_ids and _RESOURCE_CLEANUP_RE.search(out_txt):
            for rid in list(self.untrashed_untitled_ids):
                if rid in out_txt:
                    self.untrashed_untitled_ids.pop(rid, None)
        for m in _UNTITLED_CREATE_RE.finditer(out_txt):
            rid = m.group(1)
            self.untrashed_untitled_ids[rid] = " ".join(m.group(0).split())[:90]

    def _track_external_service_activity(self, cmd: str, out_txt: str) -> None:
        """Track external service mutations, non-idempotent duplicates, and readbacks."""
        if not cmd or out_txt.startswith(("[exit ", "ERROR:")):
            return
        receipts = extract_mutation_receipts(out_txt)
        has_service_target = bool(_EXT_SERVICE_TARGET_RE.search(cmd)) or bool(receipts)
        if not has_service_target:
            return

        has_ext_mut = bool(receipts) or bool(_MCP_MUTATING_CALL_RE.search(cmd))
        has_ext_read = bool(_MCP_READONLY_CALL_RE.search(cmd))
        if _HTTP_MUTATION_RE.search(cmd) and not (has_ext_read and not has_ext_mut):
            has_ext_mut = True
        if _HTTP_READBACK_RE.search(cmd) and not has_ext_mut:
            has_ext_read = True

        cleaned_up = bool(_RESOURCE_CLEANUP_RE.search(out_txt))
        for raw_seg in re.split(r"(?:\n|&&|\|\||;)", cmd):
            seg = raw_seg.strip()
            if not seg or seg.startswith("#") or "--help" in seg or re.search(r"(?:^|\s)-h(?:\s|$)", seg):
                continue
            m_cli = _EXT_CLI_LINE_RE.match(seg)
            if not m_cli:
                continue
            cli_bin = m_cli.group(1).lower()
            rest = " ".join(m_cli.group(2).split())
            if _CLEANUP_CLI_VERB_RE.search(rest):
                has_ext_mut = True
                cleaned_up = True
                for dkey in list(self.duplicate_external_mutations):
                    if any(tok in seg or tok in out_txt for tok in dkey.split() if len(tok) >= 4):
                        self.duplicate_external_mutations.pop(dkey, None)
            elif _NON_IDEMPOTENT_CLI_VERB_RE.search(rest) and "--dry-run" not in rest:
                has_ext_mut = True
                norm_sig = f"{cli_bin} {rest}"[:140]
                cnt = self._non_idempotent_cmd_counts.get(norm_sig, 0) + 1
                self._non_idempotent_cmd_counts[norm_sig] = cnt
                if cnt >= 2:
                    self.duplicate_external_mutations[norm_sig] = (
                        f"Duplicate non-idempotent service command ran {cnt}x (`{norm_sig}`) "
                        "— verify no extra duplicate resource, link, or uploaded file remains."
                    )
            elif _MUTATING_CLI_VERB_RE.search(rest):
                has_ext_mut = True
            elif _READBACK_CLI_VERB_RE.search(rest):
                has_ext_read = True

        if cleaned_up and self.duplicate_external_mutations:
            for dkey in list(self.duplicate_external_mutations):
                quoted = re.findall(r'"([^"]+)"', dkey)
                if quoted and any(q in cmd or q in out_txt for q in quoted):
                    self.duplicate_external_mutations.pop(dkey, None)

        for m_rcpt in _NAMED_CREATE_RECEIPT_RE.finditer(out_txt):
            verb, kind, title = m_rcpt.group(1), m_rcpt.group(2), m_rcpt.group(3).strip()
            if title.lower() == "untitled":
                continue
            rkey = f'{verb.lower()} {kind.lower()} "{title}"'
            rcnt = self._non_idempotent_receipt_counts.get(rkey, 0) + 1
            self._non_idempotent_receipt_counts[rkey] = rcnt
            if rcnt >= 2:
                self.duplicate_external_mutations[rkey] = (
                    f'Duplicate creation/upload receipt `{verb} {kind} "{title}"` appeared {rcnt}x '
                    "— verify total remote file/item count and remove any accidental duplicate."
                )

        if has_ext_mut:
            self.external_mutation_count += 1
            self.last_external_mutation_step = self.step
        elif has_ext_read:
            self.last_external_readback_step = self.step

    def external_state_advisories(self) -> list[str]:
        """Return one-time pre-exit advisories for scratch probes, duplicate creations, or unverified mutations."""
        adv: list[str] = []
        if self.untrashed_untitled_ids:
            items_s = ", ".join(
                f"{rid} ({desc})"
                for rid, desc in list(self.untrashed_untitled_ids.items())[:3]
            )
            adv.append(
                f"Untrashed 'Untitled' creation(s) detected in workspace state: {items_s} "
                "— delete/trash accidental probe creations or confirm they are required."
            )
        for dup_msg in list(self.duplicate_external_mutations.values())[:3]:
            adv.append(dup_msg)
        if (
            self.external_mutation_count > 0
            and self.last_external_readback_step < self.last_external_mutation_step
        ):
            adv.append(
                f"External service state was mutated at step {self.last_external_mutation_step} "
                "without a subsequent read-only check — run one consolidated list/get readback "
                "to confirm final remote item/file/link counts and statuses before finalizing."
            )
        return adv

    def _do_ctx_retrieve(self, args: dict[str, Any]) -> str:
        if not self.is_sj:
            return "ERROR: ctx_retrieve is unavailable in baseline arm"
        raw_handle = str(args.get("handle") or "").strip()
        if not raw_handle:
            return "ERROR: ctx_retrieve requires a non-empty 'handle' (e.g. 'run:8d8335db6848')"
        handle = self.rewrite(raw_handle)
        stream = str(args.get("stream") or "stdout").strip().lower()
        if stream not in ("stdout", "stderr"):
            stream = "stdout"
        mode = str(args.get("mode") or "").strip().lower()
        if not mode:
            if args.get("pattern"):
                mode = "search"
            elif args.get("query"):
                mode = "query"
            else:
                mode = "get"

        target_with_stream = (
            f"{handle}#{stream}"
            if (handle.startswith("run:") and "#" not in handle)
            else handle
        )
        if mode == "get":
            cmd_args = ["get", target_with_stream]
            if args.get("json_pointer") is not None and str(args.get("json_pointer")).strip() != "":
                cmd_args.extend(["--json-pointer", str(args["json_pointer"]).strip()])
            elif args.get("records"):
                cmd_args.extend(["--records", str(args["records"]).strip()])
            elif args.get("lines"):
                cmd_args.extend(["--lines", str(args["lines"]).strip()])
            else:
                cmd_args.extend(["--lines", "1:120"])
            metric_verb = "get"
        elif mode == "search":
            pat = str(args.get("pattern") or args.get("query") or "").strip()
            if not pat:
                return "ERROR: ctx_retrieve mode='search' requires 'pattern'"
            cmd_args = ["search", target_with_stream, pat]
            if bool(args.get("fixed", False)):
                cmd_args.append("--fixed")
            ctx_lines = int(args.get("context") if args.get("context") is not None else 2)
            if ctx_lines > 0:
                cmd_args.extend(["--context", str(min(ctx_lines, 20))])
            metric_verb = "search"
        elif mode == "stats":
            cmd_args = ["stats", handle]
            metric_verb = "stats"
        elif mode in ("query", "q"):
            q_expr = str(args.get("query") or "").strip()
            if not q_expr:
                q_expr = f"records {target_with_stream} | count"
            elif target_with_stream not in q_expr and handle not in q_expr:
                if re.match(r"^(?:records|lines)\b", q_expr):
                    parts = q_expr.split("|", 1)
                    head_tok = parts[0].strip()
                    rest = f" | {parts[1].strip()}" if len(parts) > 1 else ""
                    q_expr = f"{head_tok} {target_with_stream}{rest}"
                else:
                    q_expr = f"records {target_with_stream} | {q_expr}"
            cmd_args = ["q", q_expr]
            metric_verb = "q"
        else:
            return (
                f"ERROR: unsupported ctx_retrieve mode={mode!r}; "
                "expected 'get', 'search', 'stats', or 'query'"
            )

        self.metrics.ctx_retrievals[metric_verb] = self.metrics.ctx_retrievals.get(metric_verb, 0) + 1
        try:
            cp = self.ctx(cmd_args)
        except subprocess.TimeoutExpired:
            return "ERROR: ctx_retrieve timed out."
        text = (cp.stdout + (("\n" + cp.stderr) if cp.stderr.strip() else "")).strip()
        return text[:24000] or f"(ctx {metric_verb}: no output, exit {cp.returncode})"

    def execute_part(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        self.last_call_was_dup = False
        self.metrics.tool_calls[name] = self.metrics.tool_calls.get(name, 0) + 1
        if self.is_sj and str((args or {}).get("state_note") or "").strip():
            self.metrics.state_notes_emitted += 1

        sig = ""
        if self.is_sj:
            sig = canonical_tool_sig(name, args, normalize_meta=True)
            dedup_key = (self.mutation_epoch, sig)
            if name in READONLY_TOOLS and dedup_key in self.seen:
                first_step, prev_resp = self.seen[dedup_key]
                self.metrics.dup_blocked += 1
                self.last_call_was_dup = True
                prev_txt = str((prev_resp.get("response") or {}).get("output", ""))
                m = re.search(r"\brun:([0-9a-f]{6,64})\b", prev_txt)
                handle = (
                    f" Its evidence handle is run:{m.group(1)} (call ctx_retrieve with handle='run:{m.group(1)}')."
                    if m
                    else ""
                )
                msg = (
                    f"[straitjacket] Identical call already ran at step {first_step} with no intervening "
                    f"state changes; its result is already in context.{handle} Do not repeat it."
                )
                fr = {"functionResponse": {"name": name, "response": {"output": msg}}}
                return fr, len(msg), msg[:300]

        fs_before = (
            self._workspace_fs_fingerprint()
            if (self.is_sj and name == "bash")
            else None
        )
        fr, n_chars, head = self._dispatch(name, args)

        if self.is_sj:
            out_txt = str(((fr.get("functionResponse") or {}).get("response") or {}).get("output", ""))
            is_mutating = False
            if name in ("write", "edit") or name in MUTATING_BROWSER_TOOLS:
                is_mutating = True
            elif name == "bash":
                cmd = str((args or {}).get("command") or "")
                self._track_untitled_creations(out_txt)
                self._track_external_service_activity(cmd, out_txt)
                fs_after = self._workspace_fs_fingerprint()
                if (
                    (fs_before is not None and fs_after != fs_before)
                    or bool(extract_mutation_receipts(out_txt))
                    or bool(MUTATING_SHELL_RE.search(cmd))
                ):
                    is_mutating = True

            if is_mutating:
                self.mutation_epoch += 1
            elif name in READONLY_TOOLS and not self.last_call_was_dup:
                self.seen[(self.mutation_epoch, sig)] = (self.step, fr["functionResponse"])

        return fr, n_chars, head

    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        if name == "read":
            return self._do_read(args)
        if name == "browser_screenshot" and self.browser is not None:
            return self._do_screenshot(args)
        try:
            out = self._do_text_tool(name, args)
        except Exception as e:
            out = f"ERROR in {name}: {e}"
        fr = {"functionResponse": {"name": name, "response": {"output": out}}}
        return fr, len(out), out[:350]

    def _do_screenshot(self, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        assert self.browser is not None
        sp = self.norm(str(args["savePath"])) if args.get("savePath") else None
        try:
            data, mime = self.browser.screenshot(args.get("targetId"), str(args.get("format") or "png"), sp)
        except Exception as e:
            err = f"ERROR taking screenshot: {e}"
            return {"functionResponse": {"name": "browser_screenshot", "response": {"output": err}}}, len(err), err
        b64 = base64.b64encode(data).decode("ascii")
        msg = f"Captured screenshot ({mime}, {len(data):,} bytes)" + (
            f" saved to {args.get('savePath')}" if sp else ""
        )
        fr = {
            "functionResponse": {
                "name": "browser_screenshot",
                "response": {"output": msg},
                "parts": [{"inlineData": {"mimeType": mime, "data": b64}}],
            }
        }
        return fr, len(msg) + (len(data) // 3), msg

    def _do_read(self, args: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
        raw = str(args.get("file_path") or args.get("path") or "")
        if raw.strip() in ("/task/task.md", "task.md"):
            tp = self.meta_dir / "mock_case" / "task.md"
            if not tp.exists():
                nested = sorted((self.meta_dir / "mock_case").glob("**/task.md"))
                tp = nested[0] if nested else (self.run_dir / "task.md")
            if tp.exists():
                txt = rewrite_ports_surgical(
                    tp.read_text(encoding="utf-8"),
                    self.compiled_ports,
                    is_service_script=False,
                )
                lines = txt.splitlines()
                out = format_numbered_lines(lines, 0, len(lines))[: self.config.baseline_raw_cap]
                return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        p = self.norm(raw)
        if self._is_blocked_host_path(p) or not p.exists() or not p.is_file():
            err = f"ERROR: File not found: {raw}"
            return {"functionResponse": {"name": "read", "response": {"output": err}}}, len(err), err

        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            data = p.read_bytes()
            mime = mimetypes.guess_type(str(p))[0] or f"image/{ext.lstrip('.')}"
            if mime == "image/jpg":
                mime = "image/jpeg"
            b64 = base64.b64encode(data).decode("ascii")
            msg = f"Loaded image {raw} ({mime}, {len(data):,} bytes)."
            fr = {
                "functionResponse": {
                    "name": "read",
                    "response": {"output": msg},
                    "parts": [{"inlineData": {"mimeType": mime, "data": b64}}],
                }
            }
            return fr, len(msg) + (len(data) // 3), msg

        rkey: tuple[str, str, int, int, str] | None = None
        if self.is_sj:
            try:
                file_sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                rkey = (
                    str(p),
                    str(args.get("pages") or ""),
                    int(args.get("offset") or 0),
                    int(args.get("limit") or -1),
                    file_sha,
                )
                if rkey in self.file_reads:
                    prev_step, prev_handle = self.file_reads[rkey]
                    self.metrics.dup_blocked += 1
                    self.metrics.file_dedup_hits += 1
                    self.last_call_was_dup = True
                    if prev_handle:
                        msg = (
                            f"[straitjacket] Unchanged file {raw} (sha256:{file_sha[:8]}) was already captured at step "
                            f"{prev_step} as {prev_handle}; call ctx_retrieve(handle={prev_handle!r}, mode='get', lines='1:120') "
                            "or mode='search' instead of re-reading."
                        )
                    else:
                        msg = (
                            f"[straitjacket] Unchanged file slice {raw} (sha256:{file_sha[:8]}) was already read in full "
                            f"at step {prev_step} and remains verbatim in context above; do not re-read unchanged files."
                        )
                    return {"functionResponse": {"name": "read", "response": {"output": msg}}}, len(msg), msg[:300]
            except Exception:
                rkey = None

        if ext == ".pdf":
            pages_spec = args.get("pages")
            extracted, note = extract_pdf_text(p, str(pages_spec) if pages_spec is not None else None)
            if extracted.startswith("ERROR:"):
                return (
                    {"functionResponse": {"name": "read", "response": {"output": extracted}}},
                    len(extracted),
                    extracted,
                )
            all_lines = extracted.splitlines()
            off = max(0, int(args.get("offset") or 0))
            lim = int(args.get("limit") or len(all_lines))
            sel_lines = all_lines[off : off + lim]
            formatted = format_numbered_lines(sel_lines, off, len(all_lines), note)
            raw_bytes = len(formatted.encode("utf-8"))
            if self.is_sj and raw_bytes > self.config.entry_gate_bytes:
                tmp_txt = self.ctx_extract_dir / f"pdf_s{self.step}_{p.stem}.txt"
                tmp_txt.write_text(formatted, encoding="utf-8")
                out = self.ctx_run(f"cat {shlex.quote(str(tmp_txt))}", raw_bytes_hint=raw_bytes)
                if rkey is not None:
                    m = re.search(r"\brun:([0-9a-f]{6,64})\b", out)
                    self.file_reads[rkey] = (self.step, f"run:{m.group(1)}" if m else "")
                return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]
            self.metrics.inline_delivered += 1
            out = formatted[: self.config.baseline_raw_cap]
            if rkey is not None:
                self.file_reads[rkey] = (self.step, "")
            return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        content = p.read_text(encoding="utf-8", errors="replace")
        all_lines = content.splitlines()
        off = max(0, int(args.get("offset") or 0))
        lim = int(args.get("limit") or max(1, len(all_lines)))
        sel_lines = all_lines[off : off + lim]
        raw_slice = "\n".join(sel_lines)
        raw_bytes = len(raw_slice.encode("utf-8"))
        if self.is_sj and raw_bytes > self.config.entry_gate_bytes:
            end_ln = off + len(sel_lines)
            sed_target = (
                f"/task/{p.relative_to(self.run_dir).as_posix()}"
                if (self._use_bwrap and p.is_relative_to(self.run_dir))
                else str(p)
            )
            out = self.ctx_run(
                f"sed -n '{off + 1},{max(off + 1, end_ln)}p' {shlex.quote(sed_target)}",
                raw_bytes_hint=raw_bytes,
            )
            if rkey is not None:
                m = re.search(r"\brun:([0-9a-f]{6,64})\b", out)
                self.file_reads[rkey] = (self.step, f"run:{m.group(1)}" if m else "")
            return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

        self.metrics.inline_delivered += 1
        formatted = format_numbered_lines(sel_lines, off, len(all_lines))
        out = formatted[: self.config.baseline_raw_cap]
        if rkey is not None:
            self.file_reads[rkey] = (self.step, "")
        return {"functionResponse": {"name": "read", "response": {"output": out}}}, len(out), out[:350]

    def _do_text_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "ctx_retrieve":
            return self._do_ctx_retrieve(args)

        if name == "bash":
            cmd = str(args.get("command") or "")
            stripped = cmd.strip()
            if (
                self.is_sj
                and (stripped.startswith("ctx ") or stripped == "ctx")
                and not any(op in stripped for op in ("&&", "||", ";", "\n", "|", ">"))
            ):
                return self.ctx_direct(stripped[3:].strip())
            cmd_rw = self.rewrite_guest(cmd)
            wd = self.norm(str(args["workdir"])) if args.get("workdir") else self.run_dir
            if self._is_blocked_host_path(wd) or not wd.is_dir():
                wd = self.run_dir
            if not self.is_sj:
                return self._raw_shell_run(cmd_rw, cwd_override=wd)

            out = self.ctx_run(cmd_rw, cwd_override=wd)
            out = self._maybe_enrich_cli_help(cmd_rw, out)
            if (
                out.startswith("[exit ")
                and _INLINE_PY_C_RE.search(cmd)
                and _PY_PARSE_ERR_RE.search(out)
            ):
                out += (
                    "\n[ctx-hint: bash quote/variable expansion broke inline python -c; "
                    "use a single-quoted heredoc (`python3 - <<'PY' ... PY`) or write a .py file]"
                )
            if re.search(
                r"(?:\bHTTP[/ ]1\.[01]\s+4\d\d\b|\"status\"\s*:\s*4\d\d|\b400 Bad Request\b|\bValidationError\b|validation failed)",
                out,
                re.I,
            ):
                self.http400_streak += 1
                if self.http400_streak >= 2 and self.http400_hints < 2:
                    out += "\n[ctx-hint: repeated validation/4xx error — inspect exact required schema/fields in the API docs or --help before retrying]"
                    self.http400_hints += 1
                    self.http400_streak = 0
            else:
                self.http400_streak = 0
            return out

        if name == "write":
            raw = str(args.get("file_path") or args.get("path") or "")
            p = self.norm(raw)
            if self._is_blocked_host_path(p):
                return f"ERROR: Path not found: {raw}"
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(args.get("content") or "")
            out_root = (self.run_dir / "outputs").resolve()
            if p.suffix.lower() in {".py", ".sh", ".bash", ".js", ".mjs"} and not p.is_relative_to(out_root):
                content = self.rewrite_guest(content)
            p.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to {raw}"

        if name == "edit":
            raw = str(args.get("file_path") or args.get("path") or "")
            p = self.norm(raw)
            if self._is_blocked_host_path(p) or not p.is_file():
                return f"ERROR: File not found: {raw}"
            out_root = (self.run_dir / "outputs").resolve()
            is_helper_script = p.suffix.lower() in {".py", ".sh", ".bash", ".js", ".mjs"} and not p.is_relative_to(
                out_root
            )
            old_s = str(args.get("old_string") or "")
            new_s = str(args.get("new_string") or "")
            if is_helper_script:
                old_s = self.rewrite_guest(old_s)
                new_s = self.rewrite_guest(new_s)
            txt = p.read_text(encoding="utf-8")
            if old_s not in txt:
                return f"ERROR: old_string not found in {raw}"
            p.write_text(txt.replace(old_s, new_s, 1), encoding="utf-8")
            return f"Updated {raw}"

        if name == "list":
            raw_p = str(args.get("path") or ".")
            p = self.norm(raw_p)
            if self._is_blocked_host_path(p) or not p.exists():
                return f"ERROR: Path not found: {raw_p}"
            if p.is_file():
                return raw_p
            show_hid = bool(args.get("show_hidden", False))
            entries = sorted(e for e in os.listdir(p) if show_hid or not e.startswith("."))
            listing = (
                "\n".join(("[dir] " if (p / e).is_dir() else "[file] ") + e for e in entries[:300])
                or "(empty directory)"
            )
            return listing

        if name == "glob":
            base = self.norm(str(args.get("path") or "."))
            if self._is_blocked_host_path(base) or not base.exists():
                return "(no matches)"
            pattern = str(args.get("pattern") or "*")
            hits = sorted(pyglob.glob(str(base / pattern), recursive=True))
            rel = [
                str(Path(h).resolve())
                .replace(str(self.run_dir), "/task")
                .replace(str(self.sandbox_tmp), "/tmp")
                for h in hits
                if not self._is_blocked_host_path(Path(h))
            ]
            return "\n".join(rel[:300]) or "(no matches)"

        if name == "grep":
            target = self.norm(str(args.get("path") or "/task/workspace"))
            if self._is_blocked_host_path(target) or not target.exists():
                return "(no matches)"
            pat = str(args.get("pattern") or "")
            flags = re.I if args.get("ignore_case") else 0
            rx = re.compile(pat, flags)
            files = [target] if target.is_file() else sorted(f for f in target.rglob("*") if f.is_file())
            matches: list[str] = []
            for f in files[:250]:
                if self._is_blocked_host_path(f):
                    continue
                if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".db", ".sqlite"}:
                    continue
                try:
                    for idx, ln in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                        if rx.search(ln):
                            rel_f = (
                                str(f.resolve())
                                .replace(str(self.run_dir), "/task")
                                .replace(str(self.sandbox_tmp), "/tmp")
                            )
                            matches.append(f"{rel_f}:{idx}: {ln[:220]}")
                            if len(matches) >= 200:
                                break
                except Exception:
                    continue
                if len(matches) >= 200:
                    break
            return self.capture_large_text("grep", "\n".join(matches) or "(no matches)")

        if name == "present_files":
            files = args.get("files") or []
            paths = [f.get("path") if isinstance(f, dict) else str(f) for f in files]
            if self.is_sj and not self.emission_gated_once:
                audit = audit_deliverables(
                    self.run_dir,
                    [str(p or "") for p in paths],
                    declared_paths=self.declared_deliverables,
                )
                ext_adv = self.external_state_advisories()
                if audit.should_gate or ext_adv:
                    self.emission_gated_once = True
                    self.metrics.emission_gate_hits += 1
                    msg = audit.format_gate_message()
                    if ext_adv:
                        if not audit.should_gate:
                            msg = "[ctx-emission-gate] Pre-finalization verification check found items to confirm:"
                        for ea in ext_adv:
                            msg += f"\n- [CHECK] {ea}"
                    return msg
            self.completed = True
            return "Deliverables presented: " + ", ".join(str(x) for x in paths)

        if self.browser is not None:
            tid = args.get("targetId")
            if name == "browser_open":
                raw_snap = self.browser.open_tab(self._rewrite_browser_url(str(args.get("url") or "")))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_navigate":
                raw_snap = self.browser.navigate(tid, self._rewrite_browser_url(str(args.get("url") or "")))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_snapshot":
                raw_snap = self.browser.snapshot(tid)
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_click":
                raw_snap = self.browser.click(tid, str(args.get("ref") or ""), str(args.get("button") or "left"))
                return self.capture_large_text("bsnap", raw_snap)
            if name == "browser_batch":
                raw_batch = self.browser.batch(
                    tid,
                    args.get("steps") or [],
                    bool(args.get("stopOnNavigation", False)),
                )
                return self.capture_large_text("bbatch", raw_batch)
            if name == "browser_fill":
                return self.browser.fill_fields(tid, args.get("fields") or [])
            if name == "browser_type":
                raw_type = self.browser.type_text(
                    tid,
                    str(args.get("ref") or ""),
                    str(args.get("text") or ""),
                    bool(args.get("clearFirst", True)),
                    bool(args.get("pressEnter", False)),
                )
                return self.capture_large_text("bsnap", raw_type)
            if name == "browser_act":
                raw_act = self.browser.act(
                    tid,
                    str(args.get("kind") or ""),
                    args.get("ref"),
                    args.get("values"),
                    args.get("fn"),
                )
                return self.capture_large_text("bact", raw_act)
            if name == "browser_console":
                expr = str(args.get("expression") or "")
                if args.get("scriptPath"):
                    sp = self.norm(str(args["scriptPath"]))
                    if sp.is_file():
                        expr = sp.read_text(encoding="utf-8")
                save_p = self.norm(str(args["savePath"])) if args.get("savePath") else None
                raw_con = self.browser.console(tid, expr, save_p)
                return self.capture_large_text("bcon", raw_con)
            if name == "browser_utility":
                paths = [str(self.norm(str(x))) for x in (args.get("paths") or [])]
                return self.browser.utility(
                    tid,
                    str(args.get("kind") or ""),
                    paths,
                    args.get("element"),
                    args.get("operation"),
                )
            if name == "browser_wait":
                return self.browser.wait(
                    tid,
                    args.get("condition"),
                    args.get("timeMs"),
                    args.get("loadState"),
                    args.get("url"),
                )
            if name == "browser_press":
                return self.browser.press(tid, str(args.get("key") or "Enter"))
            if name == "browser_scroll":
                return self.browser.scroll(tid, str(args.get("direction") or "down"))
            if name == "browser_info":
                return self.browser.info()

        if name == "web_fetch":
            chunks: list[str] = []
            for u in (args.get("urls") or [])[:6]:
                fu = rewrite_ports_surgical(str(u), self.compiled_ports, is_service_script=False)
                try:
                    req = urllib.request.Request(fu, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        body = r.read(300_000).decode("utf-8", errors="replace")
                        chunks.append(f"=== {u} (HTTP {r.status}) ===\n{body[:40000]}")
                except Exception as e:
                    chunks.append(f"=== {u} ===\nERROR: {e}")
            return self.capture_large_text("wfetch", "\n\n".join(chunks))

        if name == "web_search":
            q = str(args.get("query") or "")
            ddg = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
            try:
                req = urllib.request.Request(ddg, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    html_txt = r.read(200_000).decode("utf-8", errors="replace")
                titles = re.findall(r'class="result__a"[^>]* href="([^"]+)"[^>]*>(.*?)</a>', html_txt, re.S)
                snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', html_txt, re.S)
                items = []
                for idx, (href, tit) in enumerate(titles[:10], 1):
                    clean_t = re.sub(r"<[^>]+>", "", htmlmod.unescape(tit)).strip()
                    clean_s = (
                        re.sub(r"<[^>]+>", "", htmlmod.unescape(snips[idx - 1])).strip()
                        if idx - 1 < len(snips)
                        else ""
                    )
                    items.append(f"{idx}. {clean_t}\n   URL: {href}\n   {clean_s}")
                return ("\n\n".join(items) or "(no search results returned)")[: self.config.baseline_raw_cap]
            except Exception as e:
                return f"Search error: {e}"

        return f"ERROR: Unknown tool {name}"
