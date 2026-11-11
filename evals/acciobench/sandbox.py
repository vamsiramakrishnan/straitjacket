"""Isolated per-slot task sandbox, lean mock-service staging, and official verifier runner."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from .config import BenchConfig, resolve_ctx_bin
from .ports import CompiledPortMap, discover_task_ports

_WORKSPACE_REWRITE_EXTS = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".sh", ".py", ".env"}
)

# Read-only binary assets safe to symlink into per-trial staging directories.
_READONLY_BINARY_EXTS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".ico",
        ".pdf",
        ".xlsx",
        ".xls",
        ".docx",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".woff",
        ".woff2",
        ".ttf",
        ".mp4",
        ".webm",
        ".mp3",
        ".wasm",
    }
)

# Mutable binary files (databases) that must be copied per trial, never symlinked or text-rewritten.
_MUTABLE_BINARY_EXTS = frozenset({".db", ".sqlite", ".sqlite3"})

_SKIP_NAMES = frozenset({".DS_Store", "__pycache__", ".git"})

_SINGLE_SERVICE_PORT_ENVS: tuple[tuple[str, int], ...] = (
    ("ALIBABA_PUBLISH_PORT", 3000),
    ("SHOPIFY_ADMIN_PORT", 3000),
    ("DTC_CAMPAIGN_PORT", 3000),
    ("MOCK_PORT", 3000),
    ("REDDIT_MOCK_PORT", 3001),
    ("SERPAPI_REPLAY_PORT", 4500),
    ("SERPAPI_TRENDS_PORT", 4500),
    ("SERPAPI_HOTELS_PORT", 4501),
)

_PORT_MAP_FILE = "sandbox_ports.json"

_ISOLATED_CLI_STATE_DIRS: tuple[tuple[str, str], ...] = (
    ("STRIPE_MOCK_STATE_DIR", "stripe_cli"),
    ("BOX_MOCK_STATE_DIR", "box_cli"),
    ("DWS_MOCK_STATE_DIR", "dws_doc_cli"),
    ("TODOIST_MOCK_STATE_DIR", "todoist_cli"),
    ("JIRA_MOCK_STATE_DIR", "jira_cli"),
    ("GWS_MOCK_STATE_DIR", "google_workspace_cli"),
    ("NTN_MOCK_STATE_DIR", "notion_cli"),
)


def _runtime_mock_env_key(name: str, suffix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"BENCH_RUNTIME_MOCK_{safe}_{suffix}"


def _resolve_runtime_paths(config: BenchConfig) -> tuple[Path, Path | None, Path | None]:
    """Resolve (golden_mocks_dir, runtime_bin_dir, logistics_node_modules_dir)."""
    candidates: list[Path] = []
    if config.runtime_dir is not None:
        candidates.append(config.runtime_dir)
    candidates.extend(
        [
            config.workdir / "runtime",
            config.dataset_root / ".runtime",
            config.dataset_root / "runtime",
            config.dataset_root.parent / "runtime",
        ]
    )

    eff_runtime: Path | None = None
    for cand in candidates:
        if cand.is_dir():
            eff_runtime = cand
            break

    golden_mocks: Path | None = None
    if eff_runtime is not None and (eff_runtime / "mock_services").is_dir():
        golden_mocks = eff_runtime / "mock_services"
    else:
        cab_mocks = config.dataset_root / "bench_core" / "mock_services"
        if cab_mocks.is_dir():
            golden_mocks = cab_mocks

    if golden_mocks is None:
        fallback_base = config.runtime_dir or (config.dataset_root / "bench_core")
        golden_mocks = fallback_base / "mock_services"

    runtime_bin = (
        (eff_runtime / "bin")
        if (eff_runtime is not None and (eff_runtime / "bin").is_dir())
        else None
    )

    logistics_deps: Path | None = None
    if eff_runtime is not None:
        ld = eff_runtime / "logistics_tracker_deps" / "node_modules"
        if ld.is_dir():
            logistics_deps = ld
    if logistics_deps is None:
        alt_ld = config.dataset_root / "node_modules"
        if alt_ld.is_dir():
            logistics_deps = alt_ld

    return golden_mocks, runtime_bin, logistics_deps


def _copy_or_rewrite_file(
    src_file: Path,
    dst_file: Path,
    *,
    mock_services_str: str,
    mock_state_str: str,
    compiled_ports: CompiledPortMap | None = None,
    is_service_script: bool = False,
) -> None:
    suffix = src_file.suffix.lower()
    if suffix in _READONLY_BINARY_EXTS:
        os.symlink(src_file.resolve(), dst_file)
        return
    if suffix in _MUTABLE_BINARY_EXTS:
        shutil.copy2(src_file, dst_file)
        return

    try:
        raw_bytes = src_file.read_bytes()
    except OSError:
        shutil.copy2(src_file, dst_file)
        return

    if b"\x00" in raw_bytes[:8192]:
        shutil.copy2(src_file, dst_file)
        return

    try:
        txt = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        shutil.copy2(src_file, dst_file)
        return

    new_txt = txt.replace("/opt/mock_services", mock_services_str).replace(
        "/var/lib/mocksvc", mock_state_str
    )
    if compiled_ports is not None:
        new_txt = compiled_ports.rewrite(new_txt, is_service_script=is_service_script)

    if new_txt != txt:
        dst_file.write_text(new_txt, encoding="utf-8")
        shutil.copymode(src_file, dst_file)
    else:
        shutil.copy2(src_file, dst_file)


def _stage_tree_recursive(
    src_dir: Path,
    dst_dir: Path,
    *,
    mock_services_str: str,
    mock_state_str: str,
    compiled_ports: CompiledPortMap | None = None,
    in_service_subtree: bool = False,
    rewrite_text: bool = True,
) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        name = item.name
        if name in _SKIP_NAMES or name.endswith(".pyc"):
            continue
        target = dst_dir / name
        if item.is_symlink():
            link_target = item.resolve()
            if link_target.exists() and not target.exists():
                os.symlink(link_target, target)
            continue
        if item.is_dir():
            if name == "node_modules":
                if not target.exists():
                    os.symlink(item.resolve(), target)
                continue
            child_svc = in_service_subtree or name in ("services", "grader", "logistics_tracker")
            _stage_tree_recursive(
                item,
                target,
                mock_services_str=mock_services_str,
                mock_state_str=mock_state_str,
                compiled_ports=compiled_ports,
                in_service_subtree=child_svc,
                rewrite_text=rewrite_text,
            )
            continue
        if item.is_file():
            if not rewrite_text:
                if item.suffix.lower() in _READONLY_BINARY_EXTS:
                    os.symlink(item.resolve(), target)
                else:
                    shutil.copy2(item, target)
            else:
                _copy_or_rewrite_file(
                    item,
                    target,
                    mock_services_str=mock_services_str,
                    mock_state_str=mock_state_str,
                    compiled_ports=compiled_ports,
                    is_service_script=in_service_subtree,
                )


def _spec_uses_notion(spec: Any, case_dir: Path | None = None) -> bool:
    """Return True when a task spec or its startup scripts launch ``notion_cli``."""
    if any(
        getattr(m, "name", "") == "notion_cli"
        for m in (getattr(spec, "runtime_mocks", None) or ())
    ):
        return True
    root = case_dir
    if root is None and getattr(spec, "case_dir", None):
        root = Path(spec.case_dir)
    if root is None:
        return False
    for rel in ("services/start_services.sh", "files/start_mock_services.sh"):
        script = root / rel
        if script.is_file():
            try:
                txt = script.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "notion_cli" in txt or "ntn-daemon-start" in txt:
                return True
    return False


def _patch_staged_mock_services(dst: Path) -> None:
    """Apply runtime compatibility patches to staged mutable mock service files in ``dst``."""
    box_bench = dst / "box_cli" / "bin" / "box-bench"
    if box_bench.is_file() and not box_bench.is_symlink():
        try:
            txt = box_bench.read_text(encoding="utf-8")
            old_tasks_line = "tasks: db.prepare('SELECT * FROM tasks WHERE is_deleted = 0').all(),"
            new_tasks_line = (
                "tasks: db.prepare('SELECT * FROM tasks WHERE is_deleted = 0').all()"
                ".map((t) => ({ ...t, item_id: t.item_id ?? t.file_id })),"
            )
            if old_tasks_line in txt and new_tasks_line not in txt:
                box_bench.write_text(txt.replace(old_tasks_line, new_tasks_line), encoding="utf-8")
        except OSError:
            pass


def stage_mock_services_tree(
    golden_mocks: Path,
    dst: Path,
    mock_state_dir: Path,
) -> None:
    """Stage mock_services into `dst`, symlinking `node_modules` and read-only binary assets."""
    if not golden_mocks.is_dir():
        raise FileNotFoundError(
            f"Mock services directory not found: {golden_mocks}. "
            "Run 'evals/acciobench/bootstrap.sh' or pass --runtime-dir."
        )
    dst.mkdir(parents=True, exist_ok=True)
    mock_state_dir.mkdir(parents=True, exist_ok=True)
    _stage_tree_recursive(
        golden_mocks,
        dst,
        mock_services_str=str(dst),
        mock_state_str=str(mock_state_dir),
        compiled_ports=None,
        in_service_subtree=False,
        rewrite_text=True,
    )
    _patch_staged_mock_services(dst)


def _direct_cli_state_exports(mock_name: str, mock_state_dir: Path) -> list[str]:
    state_root = mock_state_dir / mock_name
    qroot = shlex.quote(str(state_root))
    lines = [f"mkdir -p {qroot}"]
    if mock_name == "box_cli":
        lines.append(f"export BOX_MOCK_HOME=${{BOX_MOCK_HOME:-{qroot}}}")
    elif mock_name == "jira_cli":
        lines.append(f"export JIRA_MOCK_HOME=${{JIRA_MOCK_HOME:-{qroot}}}")
        lines.append('export JIRA_API_TOKEN="${JIRA_API_TOKEN:-mock-jira-token-bench}"')
    elif mock_name == "todoist_cli":
        lines.append(f"export TODOIST_MOCK_HOME=${{TODOIST_MOCK_HOME:-{qroot}}}")
        lines.append('export TODOIST_TOKEN="${TODOIST_TOKEN:-mock-todoist-token-bench}"')
    elif mock_name == "stripe_cli":
        qdb = shlex.quote(str(state_root / "stripe_mock.sqlite"))
        lines.append(f"export STRIPE_MOCK_DB=${{STRIPE_MOCK_DB:-{qdb}}}")
    elif mock_name == "dws_doc_cli":
        lines.append(f"export DWS_MOCK_HOME=${{DWS_MOCK_HOME:-{qroot}}}")
    elif mock_name == "google_workspace_cli":
        lines.append(f"export GWS_MOCK_HOME=${{GWS_MOCK_HOME:-{qroot}}}")
    elif mock_name == "notion_cli":
        lines.append(f"export NTN_MOCK_DATA_DIR=${{NTN_MOCK_DATA_DIR:-{qroot}}}")
        lines.append('export NTN_MOCK_NO_AUTO_SERVER="${NTN_MOCK_NO_AUTO_SERVER:-1}"')
    return lines


def install_cli_wrappers(
    bin_dir: Path,
    mocks: list[Any],
    mock_services_dir: Path,
    mock_state_dir: Path,
    runtime_bin: Path | None,
) -> None:
    """Install thin CLI wrappers into `bin_dir` for CLI and hybrid HTTP/CLI mocks."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path_export = (
        f'export PATH={shlex.quote(str(runtime_bin))}:"$PATH"\n'
        if runtime_bin is not None
        else ""
    )

    for mock in mocks:
        if mock.kind != "cli" or not mock.bin_names:
            continue
        for bin_name in mock.bin_names:
            wrapper_path = bin_dir / bin_name
            if mock.isolate and mock.isolation_model == "daemon_cli":
                wrapper = "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import base64, json, os, sys, urllib.error, urllib.request",
                        f"URL = 'http://127.0.0.1:{mock.port}/__cli/exec'",
                        "def maybe_file_payload(argv):",
                        "    files = {}",
                        "    for arg in argv:",
                        "        if not arg or arg.startswith('-') or arg in files:",
                        "            continue",
                        "        path = os.path.abspath(arg)",
                        "        try:",
                        "            st = os.stat(path)",
                        "        except OSError:",
                        "            continue",
                        "        if not os.path.isfile(path) or st.st_size > 2 * 1024 * 1024:",
                        "            continue",
                        "        try:",
                        "            with open(path, 'rb') as fh:",
                        "                content = base64.b64encode(fh.read()).decode('ascii')",
                        "        except OSError:",
                        "            continue",
                        "        files[arg] = {'path': path, 'content_b64': content}",
                        "    return files",
                        "argv = sys.argv[1:]",
                        "stdin = None if sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'surrogateescape')",
                        "cwd = os.getcwd() if os.path.isdir(os.getcwd()) else ''",
                        "env = {k: v for k, v in os.environ.items() if k in ('NO_COLOR', 'TERM', 'COLUMNS', 'LINES', 'LANG', 'LC_ALL')}",
                        "payload = {'argv': argv, 'stdin': stdin or '', 'cwd': cwd, 'env': env, 'files': maybe_file_payload(argv)}",
                        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))",
                        "req = urllib.request.Request(URL, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})",
                        "try:",
                        "    with opener.open(req, timeout=180) as resp:",
                        "        result = json.loads(resp.read().decode('utf-8'))",
                        "except urllib.error.URLError as exc:",
                        "    sys.stderr.write(f'Error: CLI mock daemon unavailable: {exc}\\n')",
                        "    sys.exit(127)",
                        "sys.stdout.write(result.get('stdout') or '')",
                        "sys.stderr.write(result.get('stderr') or '')",
                        "sys.exit(int(result.get('exit_code', 1)))",
                        "",
                    ]
                )
            else:
                target_bin = mock_services_dir / mock.name / "bin" / bin_name
                state_lines = "\n".join(_direct_cli_state_exports(mock.name, mock_state_dir))
                wrapper = (
                    f"#!/usr/bin/env bash\nset -euo pipefail\n"
                    f"{path_export}"
                    f"{state_lines}\n"
                    f'exec bun {shlex.quote(str(target_bin))} "$@"\n'
                )
            wrapper_path.write_text(wrapper, encoding="utf-8")
            wrapper_path.chmod(0o755)

    for mock in mocks:
        if mock.kind == "http" and mock.bin_names:
            for bin_name in mock.bin_names:
                target_bin = mock_services_dir / mock.name / "bin" / bin_name
                if target_bin.exists():
                    wp = bin_dir / bin_name
                    wp.write_text(
                        f"#!/usr/bin/env bash\nset -euo pipefail\n"
                        f"{path_export}"
                        f"export PORT={mock.port}\n"
                        f"export SHOPIFY_STORE_URL=http://127.0.0.1:{mock.port}\n"
                        f'exec bun {shlex.quote(str(target_bin))} "$@"\n',
                        encoding="utf-8",
                    )
                    wp.chmod(0o755)


def kill_by_slot_tag(slot_tag: str) -> int:
    """Fallback `/proc` scan terminating processes carrying `SJ_SLOT_TOKEN=<slot_tag>`."""
    if not slot_tag or not Path("/proc").is_dir():
        return 0
    needle = f"SJ_SLOT_TOKEN={slot_tag}".encode("utf-8")
    my_pid = os.getpid()
    my_uid = os.getuid()
    pids: list[int] = []
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return 0

    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != my_uid:
                continue
            env_bytes = Path(f"/proc/{pid}/environ").read_bytes()
            if needle in env_bytes:
                pids.append(pid)
        except OSError:
            continue

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    if pids:
        deadline = time.perf_counter() + 0.25
        alive = list(pids)
        while alive and time.perf_counter() < deadline:
            time.sleep(0.005)
            still_alive: list[int] = []
            for pid in alive:
                try:
                    os.kill(pid, 0)
                    still_alive.append(pid)
                except OSError:
                    pass
            alive = still_alive
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return len(pids)


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _harvest_pid_files(*dirs: Path) -> set[int]:
    files: list[Path] = []
    for base in dirs:
        if base.is_dir():
            files.extend(p for p in base.rglob("*") if p.is_file())
    return _harvest_pid_file_list(files)


def _harvest_pid_file_list(files: list[Path]) -> set[int]:
    pids: set[int] = set()
    my_pid = os.getpid()
    for p in files:
        if "pid" not in p.name.lower():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for tok in re.findall(r"\b(\d{2,8})\b", txt):
            val = int(tok)
            if val > 1 and val != my_pid:
                pids.add(val)
    return pids


def _is_port_free(port: int) -> bool:
    """Return True iff ``port`` has no active listener and can bind on 127.0.0.1 and 0.0.0.0."""
    if not (1024 <= port <= 65534):
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.05)
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    except OSError:
        pass
    finally:
        probe.close()

    for host in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def _allocate_free_ports(
    raw_ports: list[int],
    base_port: int,
    *,
    has_notion: bool = False,
    taken: set[int] | frozenset[int] = frozenset(),
) -> dict[int, int]:
    """Deterministically assign free host ports starting at ``base_port``, skipping occupied
    ports and those in ``taken`` (held by another live sandbox that may not be listening yet)."""
    mapping: dict[int, int] = {}
    reserved: set[int] = set(taken)
    stride = 2 if has_notion else 1
    next_cand = max(1024, int(base_port))
    for p in raw_ports:
        cand = next_cand
        while cand < 65000:
            if (
                cand not in reserved
                and _is_port_free(cand)
                and (not has_notion or ((cand + 1) not in reserved and _is_port_free(cand + 1)))
            ):
                mapping[p] = cand
                reserved.add(cand)
                if has_notion:
                    reserved.add(cand + 1)
                next_cand = cand + stride
                break
            cand += 1
        else:
            raise RuntimeError(f"Unable to allocate free host port starting from {base_port}")
    return mapping


#: Host ports held by the live sandboxes of this process: a fresh trial's allocated ports
#: and a re-grade's restored ones. A re-grade must bind the exact ports its trial ran with
#: (they are baked into its staged files), and trials that ran one after another in the
#: same worker slot share port numbers. Two sandboxes that want the same port therefore
#: take turns; otherwise the second one's service fails to bind while its readiness probe
#: is answered by the first one's, and it is graded against another trial's state.
_HELD_PORTS: set[int] = set()
_PORTS_CHANGED = threading.Condition()
#: How long a re-grade waits for a port another trial of this process holds, and how long
#: it then lets a listener outside this process (a service still shutting down) go away.
_REGRADE_PORT_WAIT_S = 1800.0
_PORT_FREE_GRACE_S = 20.0


def _host_ports(port_map: dict[int, int], has_notion: bool) -> set[int]:
    """Every host port a sandbox with ``port_map`` binds (Notion tasks also use port+1)."""
    ports = set(port_map.values())
    if has_notion:
        ports |= {p + 1 for p in port_map.values()}
    return ports


def _claim_fresh_ports(raw_ports: list[int], base_port: int, *, has_notion: bool) -> tuple[dict[int, int], set[int]]:
    """Allocate ports for a new trial and hold them until :func:`_release_ports`."""
    with _PORTS_CHANGED:
        mapping = _allocate_free_ports(raw_ports, base_port, has_notion=has_notion, taken=_HELD_PORTS)
        held = _host_ports(mapping, has_notion)
        _HELD_PORTS.update(held)
    return mapping, held


def _claim_saved_ports(
    ports: set[int],
    label: str,
    *,
    wait_s: float | None = None,
    grace_s: float | None = None,
) -> None:
    """Hold exactly ``ports`` for a re-grade: wait until no live sandbox of this process
    holds any of them, then require that nothing else on the host is listening on them.
    Raises ``RuntimeError`` (and holds nothing) if either does not happen in time, so the
    trial stays ungraded instead of being graded against someone else's service."""
    wait_s = _REGRADE_PORT_WAIT_S if wait_s is None else wait_s
    grace_s = _PORT_FREE_GRACE_S if grace_s is None else grace_s
    if not ports:
        return
    deadline = time.monotonic() + wait_s
    announced = False
    with _PORTS_CHANGED:
        while ports & _HELD_PORTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"port(s) {sorted(ports & _HELD_PORTS)} needed to re-grade {label} stayed "
                    f"held by another trial for {wait_s:.0f}s"
                )
            if not announced:
                announced = True
                print(
                    f"  [regrade] {label}: waiting for port(s) {sorted(ports & _HELD_PORTS)} "
                    "held by another trial",
                    file=sys.stderr,
                    flush=True,
                )
            _PORTS_CHANGED.wait(timeout=min(remaining, 5.0))
        _HELD_PORTS.update(ports)
    grace_deadline = time.monotonic() + grace_s
    while True:
        busy = sorted(p for p in ports if not _is_port_free(p))
        if not busy:
            return
        if time.monotonic() >= grace_deadline:
            _release_ports(ports)
            raise RuntimeError(
                f"port(s) {busy} needed to re-grade {label} are in use outside this run"
            )
        time.sleep(0.2)


def _release_ports(ports: set[int]) -> None:
    if not ports:
        return
    with _PORTS_CHANGED:
        _HELD_PORTS.difference_update(ports)
        _PORTS_CHANGED.notify_all()


class TaskSandbox:
    """Manages isolated workspace, mock services, port mapping, and official verifier for one trial."""

    def __init__(
        self,
        config: BenchConfig,
        spec: Any,
        run_dir: Path,
        meta_dir: Path,
        slot: int,
    ) -> None:
        self.config = config
        self.spec = spec
        self.run_dir = Path(run_dir).resolve()
        self.meta_dir = Path(meta_dir).resolve()
        self.slot = int(slot)
        self.slot_tag = f"sjslot_{self.slot}_{os.getpid()}_{secrets.token_hex(4)}"
        self.verifier_token = secrets.token_urlsafe(24)

        self.port_map: dict[int, int] = {}
        self.compiled_ports: CompiledPortMap = CompiledPortMap({})
        case_path = Path(spec.case_dir).resolve()
        self.has_mocks: bool = bool(getattr(spec, "runtime_mocks", ())) or (
            case_path / "services"
        ).is_dir()
        self._has_notion: bool = _spec_uses_notion(spec, case_path)

        self.remapped_mocks: list[Any] = []
        self.mock_services_dir = self.meta_dir / "mock_services"
        self.mock_state_dir = self.meta_dir / "mock_state"
        # Preserve `<parent>/<case_name>` beneath `mock_case/` so task scripts and verifiers
        # inspecting `task_dir.name` or `dirname(dirname(start_services.sh))` see canonical names.
        case_parent = case_path.parent.name or "case"
        self.mock_case_dir = self.meta_dir / "mock_case" / case_parent / case_path.name
        self.mock_private_dir = self.mock_case_dir / "private"
        self.primary_mock_url: str | None = None
        self.runtime_mock_urls: dict[str, str] = {}

        self._golden_mocks, self._runtime_bin, self._logistics_deps = _resolve_runtime_paths(config)
        self._spawned_pgids: set[int] = set()
        self._spawned_pids: set[int] = set()
        self._setup_ran = False
        self._start_attempted = False
        self._held_ports: set[int] = set()

    def setup(self) -> None:
        raw_ports = discover_task_ports(self.spec)
        base_port = 20000 + self.slot * 100
        has_notion = _spec_uses_notion(self.spec)
        self._has_notion = has_notion
        _release_ports(self._held_ports)
        self.port_map, self._held_ports = _claim_fresh_ports(raw_ports, base_port, has_notion=has_notion)
        self.compiled_ports = CompiledPortMap(self.port_map)

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        # The staged case files have these ports baked in; a later re-grade reuses them.
        (self.meta_dir / _PORT_MAP_FILE).write_text(
            json.dumps({str(k): v for k, v in self.port_map.items()}), encoding="utf-8"
        )

        src_ws = Path(self.spec.case_dir) / "workspace"
        dst_ws = self.run_dir / "workspace"
        if dst_ws.exists():
            shutil.rmtree(dst_ws)
        if src_ws.is_dir():
            shutil.copytree(src_ws, dst_ws)
        else:
            dst_ws.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "tmp").mkdir(parents=True, exist_ok=True)

        if self.compiled_ports:
            for wf in dst_ws.rglob("*"):
                if wf.is_file() and wf.suffix.lower() in _WORKSPACE_REWRITE_EXTS:
                    try:
                        txt = wf.read_text(encoding="utf-8")
                        new_txt = self.compiled_ports.rewrite(txt, is_service_script=False)
                        if new_txt != txt:
                            wf.write_text(new_txt, encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        pass

        self.mock_case_dir.mkdir(parents=True, exist_ok=True)
        mock_svcs_str = str(self.mock_services_dir)
        mock_state_str = str(self.mock_state_dir)

        for item in Path(self.spec.case_dir).iterdir():
            if item.name in ("workspace", "private") or item.name in _SKIP_NAMES:
                continue
            dst = self.mock_case_dir / item.name
            is_svc = item.name in ("services", "grader", "logistics_tracker")
            if item.is_dir():
                _stage_tree_recursive(
                    item,
                    dst,
                    mock_services_str=mock_svcs_str,
                    mock_state_str=mock_state_str,
                    compiled_ports=self.compiled_ports,
                    in_service_subtree=is_svc,
                    rewrite_text=True,
                )
            elif item.is_file():
                _copy_or_rewrite_file(
                    item,
                    dst,
                    mock_services_str=mock_svcs_str,
                    mock_state_str=mock_state_str,
                    compiled_ports=self.compiled_ports,
                    is_service_script=is_svc,
                )

        ws_link = self.mock_case_dir / "workspace"
        if not ws_link.exists() and not ws_link.is_symlink():
            os.symlink(dst_ws, ws_link)

        lt_dir = self.mock_case_dir / "services" / "logistics_tracker"
        if (
            lt_dir.is_dir()
            and self._logistics_deps is not None
            and not (lt_dir / "node_modules").exists()
        ):
            os.symlink(self._logistics_deps, lt_dir / "node_modules")

        priv_src = getattr(self.spec, "private_dir", None)
        if priv_src and Path(priv_src).is_dir():
            self.mock_private_dir.mkdir(parents=True, exist_ok=True)
            for pitem in Path(priv_src).iterdir():
                if pitem.name in _SKIP_NAMES:
                    continue
                pdst = self.mock_private_dir / pitem.name
                if pitem.is_dir() and pitem.name == "mock_runtime":
                    _stage_tree_recursive(
                        pitem,
                        pdst,
                        mock_services_str=mock_svcs_str,
                        mock_state_str=mock_state_str,
                        compiled_ports=self.compiled_ports,
                        in_service_subtree=False,
                        rewrite_text=True,
                    )
                elif pitem.is_dir():
                    _stage_tree_recursive(
                        pitem,
                        pdst,
                        mock_services_str=mock_svcs_str,
                        mock_state_str=mock_state_str,
                        compiled_ports=None,
                        in_service_subtree=False,
                        rewrite_text=False,
                    )
                elif pitem.is_file():
                    if pitem.suffix.lower() in _READONLY_BINARY_EXTS:
                        os.symlink(pitem.resolve(), pdst)
                    else:
                        shutil.copy2(pitem, pdst)

        self.has_mocks = bool(getattr(self.spec, "runtime_mocks", ())) or (
            self.mock_case_dir / "services"
        ).is_dir()
        if not self.has_mocks:
            self._setup_ran = True
            return

        stage_mock_services_tree(self._golden_mocks, self.mock_services_dir, self.mock_state_dir)
        self._remap_mocks()
        self._start_services()
        self._setup_ran = True

    def prepare_regrade(self) -> None:
        """Re-attach to a FINISHED trial so its outputs can be graded again.

        Nothing is restaged or reset: the run directory (the agent's workspace and
        outputs), the staged case under ``mock_case``, the staged mock services and
        their state are used exactly as the trial left them, with the trial's own port
        map (``sandbox_ports.json``). Mock services, if the task has any, are started
        again from that persisted state. Raises ``FileNotFoundError`` when the trial's
        directories are gone, in which case the caller must re-run the trial.

        LIMIT: a task whose service start script re-seeds its state on start would be
        graded against re-seeded state; the staged scripts are the dataset's own."""
        run_sh = self.mock_case_dir / self.spec.test_script
        if not self.run_dir.is_dir() or not (self.run_dir / "outputs").is_dir() or not run_sh.is_file():
            raise FileNotFoundError(f"trial directories for {self.spec.task_id} are gone; cannot re-grade")
        ports_file = self.meta_dir / _PORT_MAP_FILE
        if ports_file.is_file():
            saved = json.loads(ports_file.read_text(encoding="utf-8"))
            self.port_map = {int(k): int(v) for k, v in saved.items()}
        elif discover_task_ports(self.spec):
            raise FileNotFoundError(f"no recorded port map for {self.spec.task_id}; cannot re-grade")
        else:
            self.port_map = {}
        self.compiled_ports = CompiledPortMap(self.port_map)
        self._has_notion = _spec_uses_notion(self.spec)
        self.has_mocks = bool(getattr(self.spec, "runtime_mocks", ())) or (
            self.mock_case_dir / "services"
        ).is_dir()
        if self.has_mocks:
            # The saved ports are the only ones this trial's staged files work with, and
            # another trial may be using the same numbers right now: take turns.
            _release_ports(self._held_ports)
            wanted = _host_ports(self.port_map, self._has_notion)
            self._held_ports = set()
            _claim_saved_ports(wanted, str(self.spec.task_id))
            self._held_ports = wanted
            self._remap_mocks()
            self._start_services(keep_existing_tmp=True)
        self._setup_ran = True

    def _remap_mocks(self) -> None:
        mock_svcs_str = str(self.mock_services_dir)
        self.remapped_mocks = []
        self.runtime_mock_urls = {}
        for m in getattr(self.spec, "runtime_mocks", ()) or ():
            new_p = self.port_map.get(m.port, m.port) if m.port is not None else None
            new_install = str(self.mock_services_dir / m.name)
            new_launcher = self.compiled_ports.rewrite(
                m.launcher.replace("/opt/mock_services", mock_svcs_str),
                is_service_script=True,
            )
            new_env = {
                k: self.compiled_ports.rewrite(str(v), is_service_script=False)
                for k, v in (m.env or {}).items()
            }
            rm = dataclasses.replace(
                m,
                source_dir=self.mock_services_dir / m.name,
                install_path=new_install,
                port=new_p,
                launcher=new_launcher,
                env=new_env,
            )
            self.remapped_mocks.append(rm)
            if new_p is not None:
                self.runtime_mock_urls[m.name] = f"http://127.0.0.1:{new_p}"

        if getattr(self.spec, "host_published_port", None) is not None:
            hp = self.port_map.get(self.spec.host_published_port, self.spec.host_published_port)
            self.primary_mock_url = f"http://127.0.0.1:{hp}"
        elif len(self.runtime_mock_urls) == 1:
            self.primary_mock_url = next(iter(self.runtime_mock_urls.values()))
        elif self.port_map:
            first_new = next(iter(self.port_map.values()))
            self.primary_mock_url = f"http://127.0.0.1:{first_new}"

        install_cli_wrappers(
            self.meta_dir / "bin",
            self.remapped_mocks,
            self.mock_services_dir,
            self.mock_state_dir,
            self._runtime_bin,
        )

    def service_env(self) -> dict[str, str]:
        mock_scratch_work = self.meta_dir / "mock_workdir"
        mock_scratch_work.mkdir(parents=True, exist_ok=True)
        (mock_scratch_work / "tmp").mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        path_parts: list[str] = [str(self.meta_dir / "bin")]
        if self._runtime_bin is not None:
            path_parts.append(str(self._runtime_bin))
        try:
            ctx_exec = resolve_ctx_bin(self.config.ctx_bin)
            path_parts.append(str(ctx_exec.parent))
        except FileNotFoundError:
            pass
        path_parts.append(str(Path(sys.executable).resolve().parent))
        path_parts.append(env.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
        env["PATH"] = os.pathsep.join(dict.fromkeys(p for p in path_parts if p))

        env["SJ_SLOT_TOKEN"] = self.slot_tag
        env["CCB_MOCK_SERVICES_ROOT"] = str(self.mock_services_dir)
        env["CCB_DAEMON_CLI_START"] = str(
            self.mock_services_dir / "cli_daemon" / "start-daemon-cli-mock"
        )
        env["BENCH_TASK_ID"] = str(self.spec.task_id)
        env["BENCH_TASK_DIR"] = str(self.mock_case_dir)
        env["CONTAINER_TASK_DIR"] = str(self.mock_case_dir)
        priv_str = str(self.mock_private_dir) if self.mock_private_dir.exists() else ""
        env["BENCH_PRIVATE_DIR"] = priv_str
        env["CONTAINER_PRIVATE_DIR"] = priv_str
        env["BENCH_RUNTIME_MOCKS_DIR"] = str(self.mock_services_dir)
        env["BENCH_MOCK_ROOT"] = str(self.mock_services_dir)
        env["BENCH_RUNTIME_MOCKS_JSON"] = json.dumps(
            self._build_runtime_mock_metadata(),
            ensure_ascii=False,
        )
        env["BENCH_WORKDIR"] = str(self.run_dir)
        env["CONTAINER_WORKDIR"] = str(self.run_dir)
        env["BENCH_OUTPUT_DIR"] = str(self.run_dir / "outputs")
        env["CONTAINER_OUTPUT_DIR"] = str(self.run_dir / "outputs")
        env["MOCK_VERIFIER_TOKEN"] = self.verifier_token
        env["CCB_INTERNAL_BENCH_TOKEN"] = self.verifier_token
        env.pop("NTN_MOCK_BACKEND_PORT", None)
        no_proxy_items = [
            x.strip()
            for x in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",")
            if x.strip()
        ]
        for host_tok in ("127.0.0.1", "localhost", "0.0.0.0"):
            if host_tok not in no_proxy_items:
                no_proxy_items.append(host_tok)
        no_proxy_val = ",".join(no_proxy_items)
        env["NO_PROXY"] = no_proxy_val
        env["no_proxy"] = no_proxy_val

        for st_key, sub in _ISOLATED_CLI_STATE_DIRS:
            sd = self.mock_state_dir / sub
            sd.mkdir(parents=True, exist_ok=True)
            env[st_key] = str(sd)

        for rm in self.remapped_mocks:
            mdir = str(self.mock_services_dir / rm.name)
            env[_runtime_mock_env_key(rm.name, "DIR")] = mdir
            if rm.port is not None:
                env[_runtime_mock_env_key(rm.name, "PORT")] = str(rm.port)
                env[_runtime_mock_env_key(rm.name, "URL")] = f"http://127.0.0.1:{rm.port}"
                if rm.name == "notion_cli":
                    env["NTN_MOCK_BACKEND_PORT"] = str(int(rm.port) + 1)
            if rm.health_path:
                env[_runtime_mock_env_key(rm.name, "HEALTH_PATH")] = rm.health_path
            for k, v in (rm.env or {}).items():
                env[str(k)] = str(v)

        if self._has_notion and "NTN_MOCK_BACKEND_PORT" not in env:
            ntn_port = self.port_map.get(3300)
            if ntn_port is not None:
                env["NTN_MOCK_BACKEND_PORT"] = str(int(ntn_port) + 1)
                self.runtime_mock_urls.setdefault("notion_cli", f"http://127.0.0.1:{ntn_port}")

        for env_var, def_p in _SINGLE_SERVICE_PORT_ENVS:
            if def_p in self.port_map and len(self.remapped_mocks) <= 1:
                env[env_var] = str(self.port_map[def_p])

        return env

    # Compatibility alias for legacy callers.
    _service_env = service_env

    def _build_runtime_mock_metadata(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for mock in self.remapped_mocks:
            install = getattr(mock, "install_path", None)
            if install and str(install).startswith("/opt/mock_services/"):
                cdir = str(install)
            else:
                cdir = str(self.mock_services_dir / mock.name)
            out.append(
                {
                    "name": mock.name,
                    "kind": mock.kind,
                    "dir": cdir,
                    "port": mock.port,
                    "health_path": mock.health_path,
                    "bin_names": list(mock.bin_names),
                    "bench_bin": mock.bench_bin,
                    "isolate": mock.isolate,
                    "isolation_model": mock.isolation_model,
                }
            )
        return out

    def _synthesize_runtime_mock_script(self, mock_wd: str) -> str:
        root_str = str(self.config.dataset_root)
        if (self.config.dataset_root / "bench_core").is_dir() and root_str not in sys.path:
            sys.path.insert(0, root_str)
        from bench_core.cli import _runtime_mock_start_command

        fake_spec = dataclasses.replace(self.spec, runtime_mocks=tuple(self.remapped_mocks))
        gen_script = _runtime_mock_start_command(
            fake_spec, str(self.mock_services_dir), mock_wd
        )
        return (
            gen_script.replace("/opt/mock_services", str(self.mock_services_dir))
            .replace("/var/lib/mocksvc", str(self.mock_state_dir))
            .replace(
                'export MOCK_VERIFIER_TOKEN="$MOCK_VERIFIER_TOKEN"',
                'export MOCK_VERIFIER_TOKEN="$MOCK_VERIFIER_TOKEN"\n'
                'export CCB_INTERNAL_BENCH_TOKEN="$MOCK_VERIFIER_TOKEN"',
            )
        )

    def _run_service_launcher(
        self,
        cmd: list[str],
        env: dict[str, str],
        log_path: Path,
        label: str,
    ) -> None:
        self._start_attempted = True
        with log_path.open("w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.mock_case_dir),
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._spawned_pgids.add(proc.pid)
            try:
                rc = proc.wait(timeout=90)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=5)
                tail = log_path.read_text(encoding="utf-8", errors="ignore")[-500:]
                raise RuntimeError(
                    f"{label} timed out after 90s for {self.spec.task_id}: {tail}"
                ) from exc

        if rc != 0:
            tail = log_path.read_text(encoding="utf-8", errors="ignore")[-500:]
            raise RuntimeError(f"{label} failed (exit {rc}) for {self.spec.task_id}: {tail}")

    def _start_services(self, *, keep_existing_tmp: bool = False) -> None:
        """Start the task's services. ``keep_existing_tmp`` (re-grade): files already in
        ``/task/tmp`` belong to the finished trial and are neither harvested as pid files
        nor removed; only files the start itself wrote there are."""
        tmp_d = self.run_dir / "tmp"
        preexisting: set[Path] = (
            {p for p in tmp_d.rglob("*")} if (keep_existing_tmp and tmp_d.is_dir()) else set()
        )
        env = self.service_env()
        ss = self.mock_case_dir / "services" / "start_services.sh"
        legacy_ss = self.mock_case_dir / "files" / "start_mock_services.sh"
        log_path = self.meta_dir / "start_services.log"

        if ss.is_file():
            self._run_service_launcher(["bash", str(ss)], env, log_path, "start_services.sh")
        elif legacy_ss.is_file():
            self._run_service_launcher(
                ["bash", str(legacy_ss)], env, log_path, "start_mock_services.sh"
            )
        elif self.remapped_mocks:
            mock_wd = str(self.meta_dir / "mock_workdir")
            env["BENCH_WORKDIR"] = mock_wd
            env["CONTAINER_WORKDIR"] = mock_wd
            gen_script = self._synthesize_runtime_mock_script(mock_wd)
            self._run_service_launcher(
                ["bash", "-lc", gen_script], env, log_path, "generated runtime mocks"
            )

        # Harvest any explicit PID files before scrubbing /task/tmp for agent isolation.
        if preexisting:
            self._spawned_pids.update(_harvest_pid_files(self.meta_dir / "mock_workdir" / "tmp"))
            new_files = [p for p in tmp_d.rglob("*") if p not in preexisting and p.is_file()]
            self._spawned_pids.update(_harvest_pid_file_list(new_files))
        else:
            self._spawned_pids.update(
                _harvest_pid_files(self.run_dir / "tmp", self.meta_dir / "mock_workdir" / "tmp")
            )

        # Unlink temporary pid/log files written into run_dir/tmp so /task/tmp stays clean.
        if tmp_d.is_dir():
            for tf in list(tmp_d.rglob("*")):
                if tf in preexisting:
                    continue
                if tf.is_file() or tf.is_symlink():
                    try:
                        tf.unlink()
                    except OSError:
                        pass

    def grade(self, reward_json: Path, *, judge_token: str = "") -> dict[str, Any]:
        reward_path = Path(reward_json).resolve()
        reward_path.parent.mkdir(parents=True, exist_ok=True)
        env = self.service_env()

        cab_root = str(self.config.dataset_root)
        existing_py = env.get("PYTHONPATH", "")
        py_path = f"{cab_root}{os.pathsep}{existing_py}" if existing_py else cab_root

        env.update(
            {
                "REWARD_JSON": str(reward_path),
                "OUTPUT_DIR": str(self.run_dir / "outputs"),
                "WORKSPACE_DIR": str(self.run_dir / "workspace"),
                "RUN_DIR": str(self.run_dir),
                "RUBRIC_JSON": str(self.mock_case_dir / self.spec.rubric),
                "TASK_MD": str(self.mock_case_dir / self.spec.entrypoint),
                "PRIVATE_DIR": (
                    str(self.mock_private_dir) if self.mock_private_dir.exists() else ""
                ),
                "BENCH_ROOT": cab_root,
                "PYTHONPATH": py_path,
            }
        )

        judge_provider = (self.config.judge_provider or "").strip().lower()
        env["BENCH_LLM_JUDGE_PROVIDER"] = judge_provider
        env["BENCH_LLM_JUDGE_MODEL"] = self.config.judge_model

        if judge_provider in ("vertex", "iai"):
            if judge_token:
                env["VERTEX_ACCESS_TOKEN"] = judge_token
            proj = (
                self.config.project_id
                or env.get("VERTEX_PROJECT_ID")
                or env.get("GOOGLE_CLOUD_PROJECT")
                or ""
            )
            if proj:
                env["VERTEX_PROJECT_ID"] = proj
            env["VERTEX_LOCATION"] = (
                self.config.location or env.get("VERTEX_LOCATION") or "global"
            )
        elif judge_provider == "gemini":
            gkey = (
                judge_token
                or self.config.api_key
                or env.get("BENCH_LLM_JUDGE_API_KEY")
                or env.get("GEMINI_API_KEY")
                or ""
            )
            if gkey:
                env["GEMINI_API_KEY"] = gkey
                env["BENCH_LLM_JUDGE_API_KEY"] = gkey
        elif judge_provider == "openai":
            okey = (
                judge_token
                or env.get("BENCH_LLM_JUDGE_API_KEY")
                or env.get("OPENAI_API_KEY")
                or self.config.api_key
                or ""
            )
            if okey:
                env["OPENAI_API_KEY"] = okey
                env["BENCH_LLM_JUDGE_API_KEY"] = okey

        if self.primary_mock_url:
            env["MOCK_SITE_URL"] = self.primary_mock_url
            env["MOCK_URL"] = self.primary_mock_url
        for name, url in self.runtime_mock_urls.items():
            safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
            env[f"MOCK_SITE_URL_{safe}"] = url

        run_sh = self.mock_case_dir / self.spec.test_script
        verifier_timeout = max(90, int(getattr(self.spec, "verifier_timeout_sec", 120) or 120))
        verifier_log = self.meta_dir / "verifier.log"

        with verifier_log.open("w", encoding="utf-8") as vf:
            proc = subprocess.Popen(
                ["bash", str(run_sh)],
                cwd=str(self.mock_case_dir),
                env=env,
                stdout=vf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=verifier_timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=5)
                rc = -9

        log_text = verifier_log.read_text(encoding="utf-8", errors="ignore")
        if reward_path.exists():
            try:
                return json.loads(reward_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return {"_error": f"invalid reward.json: {exc}; {log_text[-300:]}", "_exit": rc}
        return {"_error": log_text[-400:], "_exit": rc}

    def cleanup(self) -> None:
        """Stop this sandbox's services, then give its ports back to waiting sandboxes."""
        try:
            self._terminate_services()
        finally:
            held, self._held_ports = self._held_ports, set()
            _release_ports(held)

    def _terminate_services(self) -> None:
        """Terminate mock service process groups in <5ms, falling back to `/proc` scan only if needed."""
        need_proc_fallback = False

        if self._spawned_pgids or self._spawned_pids:
            for pgid in list(self._spawned_pgids):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    self._spawned_pgids.discard(pgid)
                except PermissionError:
                    need_proc_fallback = True
                except OSError:
                    self._spawned_pgids.discard(pgid)

            for pid in list(self._spawned_pids):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    self._spawned_pids.discard(pid)
                except PermissionError:
                    need_proc_fallback = True
                except OSError:
                    self._spawned_pids.discard(pid)

            deadline = time.perf_counter() + 0.15
            while (self._spawned_pgids or self._spawned_pids) and time.perf_counter() < deadline:
                for pgid in list(self._spawned_pgids):
                    if not _pgid_alive(pgid):
                        self._spawned_pgids.discard(pgid)
                for pid in list(self._spawned_pids):
                    if not _pid_alive(pid):
                        self._spawned_pids.discard(pid)
                if self._spawned_pgids or self._spawned_pids:
                    time.sleep(0.002)

            for pgid in list(self._spawned_pgids):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    need_proc_fallback = True
                self._spawned_pgids.discard(pgid)

            for pid in list(self._spawned_pids):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    need_proc_fallback = True
                self._spawned_pids.discard(pid)
        elif not self._setup_ran or self._start_attempted:
            need_proc_fallback = True

        # Detached `nohup` CLI/HTTP daemons (`start-daemon-cli-mock`, `bun`, `node`) outlive the
        # short-lived launcher PGID; always sweep `SJ_SLOT_TOKEN` whenever services were started.
        if need_proc_fallback or self._start_attempted:
            kill_by_slot_tag(self.slot_tag)
