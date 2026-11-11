"""Configuration, metrics, and task-discovery utilities for AccioBench (CommerceAgentBench)."""

from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass, field
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import sys
import tempfile
import threading
import tomllib
from typing import Any

VALID_ARMS = frozenset({"raw", "sj"})
KNOWN_CATEGORIES = ("api", "browser", "cli", "file")

_CAB_LOAD_LOCK = threading.Lock()
_TASK_INDEX_CACHE: dict[str, dict[str, Path]] = {}


@dataclass(frozen=True)
class BenchConfig:
    dataset_root: Path  # Path to CommerceAgentBench root (contains bench_core/ and datasets_domain_v1/)
    workdir: Path  # Output/scratch root for runs & ctx state
    model: str = "gemini-3.8-flash"
    provider: str = "vertex"  # "gemini" | "vertex"
    project_id: str = ""  # From --project or VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT
    location: str = "global"  # From --location or VERTEX_LOCATION
    api_key: str = ""  # From --api-key or GEMINI_API_KEY
    judge_provider: str = "vertex"  # "vertex" | "gemini" | "openai" | "mock"
    judge_model: str = "gemini-4-argon"
    arms: tuple[str, ...] = ("sj",)  # ("sj",), ("raw",), or ("raw", "sj")
    workers: int = 4
    slot_offset: int = 0
    max_steps: int = 135
    entry_gate_bytes: int = 24_576
    baseline_raw_cap: int = 400_000
    resume: bool = True
    runtime_dir: Path | None = None  # Optional dir with mock_services/, bin/ (bun/node), logistics_tracker_deps/
    ctx_bin: Path | None = None  # Auto-resolved via shutil.which("ctx") or sys.executable -m ctx
    chrome_bin: Path | None = None  # Optional browser binary override
    thinking_level: str | None = None  # None = untouched model defaults!
    max_output_tokens: int | None = None  # None = untouched model defaults!

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_root", Path(self.dataset_root).expanduser().resolve())
        object.__setattr__(self, "workdir", Path(self.workdir).expanduser().resolve())
        if self.runtime_dir is not None:
            object.__setattr__(self, "runtime_dir", Path(self.runtime_dir).expanduser().resolve())
        if self.ctx_bin is not None:
            object.__setattr__(self, "ctx_bin", Path(self.ctx_bin).expanduser().resolve())
        if self.chrome_bin is not None:
            object.__setattr__(self, "chrome_bin", Path(self.chrome_bin).expanduser().resolve())

        raw_arms = self.arms
        if isinstance(raw_arms, str):
            norm_arms = tuple(a.strip().lower() for a in raw_arms.split(",") if a.strip())
        else:
            norm_arms = tuple(str(a).strip().lower() for a in raw_arms if str(a).strip())
        if not norm_arms:
            norm_arms = ("sj",)
        invalid = [a for a in norm_arms if a not in VALID_ARMS]
        if invalid:
            raise ValueError(f"Unsupported arm(s) {invalid!r}; expected subset of {sorted(VALID_ARMS)}")
        object.__setattr__(self, "arms", norm_arms)

        if not self.project_id:
            env_proj = (
                os.environ.get("VERTEX_PROJECT_ID")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or ""
            ).strip()
            if env_proj:
                object.__setattr__(self, "project_id", env_proj)

        if not self.location:
            object.__setattr__(
                self,
                "location",
                (os.environ.get("VERTEX_LOCATION") or "global").strip() or "global",
            )

        if not self.api_key:
            env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
            if env_key:
                object.__setattr__(self, "api_key", env_key)

    @property
    def suite_dir(self) -> Path:
        return self.dataset_root / "datasets_domain_v1"

    @property
    def runs_dir(self) -> Path:
        return self.workdir / "runs"

    @property
    def ctx_state_home(self) -> Path:
        return self.workdir / "ctx_state"


@dataclass
class RunMetrics:
    task_id: str
    task_rel: str
    category: str
    arm: str  # "sj" or "raw"
    model_id: str
    wave: str = "custom"
    bucket: str = "unknown"
    steps: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    thought_tokens: int = 0
    output_tokens: int = 0
    max_step_prompt_tokens: int = 0
    message_chars: int = 0
    wall_clock_s: float = 0.0
    tool_calls: dict[str, int] = field(default_factory=dict)
    dup_blocked: int = 0
    digests_delivered: int = 0
    inline_delivered: int = 0
    bytes_kept_out: int = 0
    ctx_retrievals: dict[str, int] = field(default_factory=dict)
    compaction_epochs: int = 0
    thoughts_compacted: int = 0
    file_dedup_hits: int = 0
    state_notes_emitted: int = 0
    state_notes_preserved: int = 0
    mutations_preserved: int = 0
    cli_lsp_enrichments: int = 0
    emission_gate_hits: int = 0
    ui_controls_stripped_chars: int = 0
    loop_gate_triggers: int = 0
    termination: str = ""
    passed: bool = False
    capacity_score: float = 0.0
    raw_verifier_score: float = 0.0
    checks_passed: int = 0
    checks_total: int = 0
    failed_checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Non-empty when the official grader itself failed (timeout, missing or invalid
    # reward.json): the trial is UNGRADED, not failed. Reports leave it out of solve
    # rate and score aggregates; resume re-grades it without re-running the agent.
    grading_error: str = ""
    # config_fingerprint(config) of the configuration that produced this result; resume
    # accepts a cached result only for the same fingerprint ("" = written before it).
    config_fingerprint: str = ""

    @property
    def is_grading_error(self) -> bool:
        return bool(self.grading_error)

    @property
    def gen_tokens(self) -> int:
        return self.thought_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.thought_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gen_tokens"] = self.gen_tokens
        payload["total_tokens"] = self.total_tokens
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMetrics:
        known_names = {f.name for f in dataclasses.fields(cls)}
        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known_names}

        if "arm" not in kwargs:
            legacy_arm = str(data.get("arm_key") or "")
            if "raw" in legacy_arm:
                kwargs["arm"] = "raw"
            elif "sj" in legacy_arm or not legacy_arm:
                kwargs["arm"] = "sj"
            else:
                kwargs["arm"] = legacy_arm

        kwargs.setdefault("task_id", str(data.get("task_id") or ""))
        kwargs.setdefault("task_rel", str(data.get("task_rel") or kwargs["task_id"]))
        kwargs.setdefault("category", str(data.get("category") or _infer_category_from_id(kwargs["task_id"])))
        kwargs.setdefault("model_id", str(data.get("model_id") or ""))

        if "tool_calls" in kwargs and isinstance(kwargs["tool_calls"], dict):
            kwargs["tool_calls"] = {str(k): int(v) for k, v in kwargs["tool_calls"].items()}
        if "ctx_retrievals" in kwargs and isinstance(kwargs["ctx_retrievals"], dict):
            kwargs["ctx_retrievals"] = {str(k): int(v) for k, v in kwargs["ctx_retrievals"].items()}
        if "failed_checks" in kwargs and isinstance(kwargs["failed_checks"], list):
            kwargs["failed_checks"] = [str(x) for x in kwargs["failed_checks"]]
        if "notes" in kwargs and isinstance(kwargs["notes"], list):
            kwargs["notes"] = [str(x) for x in kwargs["notes"]]
        if "grading_error" not in data:
            # Results written before the grading-error state recorded a grader failure
            # only as a "verifier error: ..." note on an otherwise failed trial.
            legacy = next(
                (n for n in kwargs.get("notes") or [] if str(n).startswith("verifier error:")), ""
            )
            if legacy and not kwargs.get("passed"):
                kwargs["grading_error"] = str(legacy)[len("verifier error:"):].strip() or "verifier error"
        else:
            kwargs["grading_error"] = str(data.get("grading_error") or "")

        return cls(**kwargs)


#: BenchConfig fields that change what a trial measures. Everything else is left out on
#: purpose: where results go and how fast they are produced (workdir, workers,
#: slot_offset, resume), which arms run (the arm is part of the result's own path),
#: credentials and account routing (api_key, project_id, location), and host paths
#: (dataset_root, runtime_dir, ctx_bin, chrome_bin), which name WHERE an input lives,
#: not what it is, so moving a checkout does not invalidate a workdir.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "model",
    "provider",
    "thinking_level",
    "max_output_tokens",
    "max_steps",
    "entry_gate_bytes",
    "baseline_raw_cap",
    "judge_provider",
    "judge_model",
)


def config_fingerprint(
    config: BenchConfig,
    harness: dict[str, Any] | None = None,
    trial: dict[str, Any] | None = None,
) -> str:
    """A short stable hash of what a trial measured: FINGERPRINT_FIELDS, the harness's own
    behaviour constants (``harness``) and per-trial inputs (``trial``: the task's own
    files, the ctx version, whether the bwrap sandbox is active)."""
    doc = {name: getattr(config, name) for name in FINGERPRINT_FIELDS}
    doc["harness"] = dict(sorted((harness or {}).items()))
    if trial:
        doc["trial"] = dict(sorted(trial.items()))
    blob = json.dumps(doc, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


_SMALL_FILE_BYTES = 1 << 20
_EDGE_BYTES = 64 << 10
_FILE_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}
_DIGEST_SKIP_DIRS = frozenset({"node_modules", ".git", "__pycache__"})
_DIGEST_SKIP_FILES = frozenset({".DS_Store"})


def _file_digest(path: Path, st: os.stat_result) -> str:
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _FILE_DIGEST_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha256()
    with path.open("rb") as fh:
        if st.st_size <= _SMALL_FILE_BYTES:
            h.update(fh.read())
        else:  # large binary assets: size + both ends (cheap, and catches any re-export)
            h.update(str(st.st_size).encode())
            h.update(fh.read(_EDGE_BYTES))
            fh.seek(-_EDGE_BYTES, os.SEEK_END)
            h.update(fh.read(_EDGE_BYTES))
    digest = h.hexdigest()
    _FILE_DIGEST_CACHE[key] = digest
    return digest


def task_inputs_digest(case_dir: Path) -> str:
    """Hash of a task's own directory: the prompt (task.md), task.toml, the workspace the
    agent is given, its services, and the grader, rubric and private answer files (a
    changed grader changes the recorded score, so its results must not be reused).
    Content-hashed, not path-hashed, so moving the dataset does not change it; files over
    1 MiB are hashed by size and both ends; per-file digests are memoized per process."""
    root = Path(case_dir).resolve()
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _DIGEST_SKIP_DIRS)
        for name in sorted(filenames):
            if name in _DIGEST_SKIP_FILES or name.endswith(".pyc"):
                continue
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            h.update(rel.encode("utf-8") + b"\0" + _file_digest(p, st).encode() + b"\n")
    return h.hexdigest()[:16]


_CTX_VERSION_CACHE: dict[str, str] = {}
_CTX_VERSION_LOCK = threading.Lock()


def ctx_version(ctx_bin: Path | None = None) -> str:
    """The version the benchmarked ctx reports (``ctx --version``), asked once per process
    per executable. "unknown" when it cannot be asked."""
    try:
        exe = resolve_ctx_bin(ctx_bin)
    except FileNotFoundError:
        return "unavailable"
    key = str(exe)
    with _CTX_VERSION_LOCK:
        cached = _CTX_VERSION_CACHE.get(key)
        if cached is not None:
            return cached
        argv = [key, "-P", "-m", "ctx", "--version"] if "python" in exe.name.lower() else [key, "--version"]
        try:
            import subprocess

            cp = subprocess.run(argv, capture_output=True, text=True, timeout=30, cwd="/")
            out = (cp.stdout or cp.stderr).strip().splitlines()
            ver = out[0].strip() if (cp.returncode == 0 and out) else f"unknown (exit {cp.returncode})"
        except Exception as exc:
            ver = f"unknown ({type(exc).__name__})"
        _CTX_VERSION_CACHE[key] = ver
        return ver


def resolve_ctx_bin(explicit: Path | None = None) -> Path:
    """Locate the `ctx` CLI executable or synthesize a `python -m ctx` shim."""
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise FileNotFoundError(f"Explicit ctx executable not found or not executable: {candidate}")

    # 1. Sibling `ctx` next to current Python interpreter (handles unactivated venvs).
    venv_ctx = Path(sys.executable).resolve().parent / "ctx"
    if venv_ctx.is_file() and os.access(venv_ctx, os.X_OK):
        return venv_ctx

    # 2. `ctx` on PATH.
    which_ctx = shutil.which("ctx")
    if which_ctx:
        return Path(which_ctx).resolve()

    # 3. Source-checkout fallback (`sys.executable -m ctx` via a lightweight executable shim).
    repo_src = Path(__file__).resolve().parents[2] / "src"
    has_ctx_pkg = importlib.util.find_spec("ctx") is not None or (repo_src / "ctx" / "__init__.py").is_file()
    if has_ctx_pkg:
        shim_dir = Path(tempfile.gettempdir()) / f"straitjacket_ctx_bin_{os.getuid()}"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_path = shim_dir / "ctx"
        py_exec = shlex.quote(str(Path(sys.executable).resolve()))
        src_q = shlex.quote(str(repo_src))
        script = (
            "#!/usr/bin/env bash\n"
            f'export PYTHONPATH={src_q}${{PYTHONPATH:+:$PYTHONPATH}}\n'
            # -P (see ctx.selfcmd): never import a `ctx` from the caller's directory.
            f'exec {py_exec} -P -m ctx "$@"\n'
        )
        if not shim_path.is_file() or shim_path.read_text(encoding="utf-8", errors="ignore") != script:
            tmp_shim = shim_dir / f".ctx.{os.getpid()}"
            tmp_shim.write_text(script, encoding="utf-8")
            tmp_shim.chmod(0o755)
            tmp_shim.replace(shim_path)
        elif not os.access(shim_path, os.X_OK):
            shim_path.chmod(0o755)
        return shim_path

    raise FileNotFoundError(
        "Could not resolve 'ctx' executable on PATH, in current Python environment, or via 'python -m ctx'."
    )


@dataclass
class _FallbackRuntimeMock:
    name: str
    source_dir: Path
    install_path: str | None = None
    kind: str = "http"
    port: int | None = None
    health_path: str | None = None
    launcher: str = ""
    env: dict[str, str] = field(default_factory=dict)
    bin_names: tuple[str, ...] = ()
    bench_bin: str | None = None
    include_node_modules: bool = False
    isolate: bool = False
    isolation_model: str | None = None


@dataclass
class _FallbackTaskSpec:
    task_id: str
    case_dir: Path
    name: str
    language: str
    timeout_sec: int
    max_actions: int
    verifier_timeout_sec: int
    entrypoint: str
    test_script: str
    rubric: str
    private_dir: Path | None = None
    mock_services: bool = False
    host_published_port: int | None = None
    requires_browser: bool = False
    requires_vision: bool = False
    runtime_mocks: tuple[Any, ...] = ()
    early_terminate: Any = None
    output_file_early_terminate: Any = None


def _effective_suite_dir(dataset_root: Path) -> Path:
    root = Path(dataset_root).expanduser().resolve()
    sub = root / "datasets_domain_v1"
    return sub if sub.is_dir() else root


def _infer_category_from_id(task_id: str) -> str:
    prefix = task_id.split("-", 1)[0].lower()
    return prefix if prefix in KNOWN_CATEGORIES else "unknown"


def _infer_task_category(case_dir: Path, suite_dir: Path, task_id: str) -> str:
    try:
        rel_parts = case_dir.resolve().relative_to(suite_dir.resolve()).parts
        if len(rel_parts) >= 2 and rel_parts[0].lower() in KNOWN_CATEGORIES:
            return rel_parts[0].lower()
    except ValueError:
        pass
    return _infer_category_from_id(task_id)


def _get_task_index(dataset_root: Path) -> dict[str, Path]:
    suite_dir = _effective_suite_dir(dataset_root)
    key = str(suite_dir)
    with _CAB_LOAD_LOCK:
        cached = _TASK_INDEX_CACHE.get(key)
        if cached is not None:
            return cached
        index: dict[str, Path] = {}
        if suite_dir.is_dir():
            for toml_path in sorted(suite_dir.rglob("task.toml")):
                rel_parts = toml_path.relative_to(suite_dir).parts
                if any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in rel_parts):
                    continue
                try:
                    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                tid = str((raw.get("task") or {}).get("id") or toml_path.parent.name).strip()
                if tid:
                    index[tid] = toml_path.parent.resolve()
        _TASK_INDEX_CACHE[key] = index
        return index


def _load_task_toml_fallback(dataset_root: Path, case_dir: Path) -> _FallbackTaskSpec:
    task_toml = case_dir / "task.toml"
    data = tomllib.loads(task_toml.read_text(encoding="utf-8"))
    task = data.get("task") or {}
    agent = data.get("agent") or {}
    verifier = data.get("verifier") or {}
    environment = data.get("environment") or {}
    tid = str(task.get("id") or case_dir.name)
    hp_raw = environment.get("host_published_port")
    hp = int(hp_raw) if hp_raw is not None else None
    test_default = "grader/run.sh" if (case_dir / "grader" / "run.sh").is_file() else "test.sh"

    mocks: list[_FallbackRuntimeMock] = []
    raw_mocks = environment.get("runtime_mocks")
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(raw_mocks, list):
        for item in raw_mocks:
            entries[str(item).strip()] = {}
    elif isinstance(raw_mocks, dict):
        for item in raw_mocks.get("services") or raw_mocks.get("names") or []:
            entries[str(item).strip()] = {}
        for k, v in raw_mocks.items():
            if k not in ("services", "names") and isinstance(v, dict):
                entries[str(k)] = dict(v)

    for mname, ov in entries.items():
        src_dir = (dataset_root / "bench_core" / "mock_services" / mname).resolve()
        port_raw = ov.get("port")
        port_val = int(port_raw) if port_raw not in (None, "") else None
        bins_raw = ov.get("bin_names") or ()
        bin_names = (bins_raw,) if isinstance(bins_raw, str) else tuple(str(x) for x in bins_raw)
        mocks.append(
            _FallbackRuntimeMock(
                name=mname,
                source_dir=src_dir,
                install_path=str(ov["install_path"]) if ov.get("install_path") else None,
                kind=str(ov.get("kind") or "http"),
                port=port_val,
                health_path=str(ov["health_path"]) if ov.get("health_path") else None,
                launcher=str(ov.get("launcher") or ""),
                env={str(k): str(v) for k, v in (ov.get("env") or {}).items()},
                bin_names=bin_names,
                bench_bin=str(ov["bench_bin"]) if ov.get("bench_bin") else None,
                include_node_modules=bool(ov.get("include_node_modules", False)),
                isolate=bool(ov.get("isolate", False)),
                isolation_model=str(ov["isolation_model"]) if ov.get("isolation_model") else None,
            )
        )

    return _FallbackTaskSpec(
        task_id=tid,
        case_dir=case_dir,
        name=str(task.get("name") or tid),
        language=str(task.get("language") or "en"),
        timeout_sec=int(agent.get("timeout_sec", 900)),
        max_actions=int(agent.get("max_actions", 6)),
        verifier_timeout_sec=int(verifier.get("timeout_sec", 120)),
        entrypoint=str(task.get("entrypoint") or "task.md"),
        test_script=str(task.get("test") or test_default),
        rubric=str(task.get("rubric") or "rubric.json"),
        private_dir=(case_dir / "private") if (case_dir / "private").is_dir() else None,
        mock_services=bool(environment.get("mock_services", False) or mocks),
        host_published_port=hp,
        requires_browser=bool(environment.get("requires_browser", False)),
        requires_vision=bool(environment.get("requires_vision", False)),
        runtime_mocks=tuple(mocks),
    )


def _resolve_case_dir(dataset_root: Path, task_id_or_dir: str) -> Path:
    root = Path(dataset_root).expanduser().resolve()
    suite_dir = _effective_suite_dir(root)
    raw = str(task_id_or_dir).strip()
    if not raw:
        raise ValueError("task_id_or_dir must be non-empty")

    direct_candidates = [
        Path(raw).expanduser(),
        suite_dir / raw,
        root / raw,
    ]
    for cand in direct_candidates:
        if (cand / "task.toml").is_file():
            return cand.resolve()

    index = _get_task_index(root)
    if raw in index:
        return index[raw]

    # Cache might be stale if caller created a new task dir dynamically; refresh once.
    with _CAB_LOAD_LOCK:
        _TASK_INDEX_CACHE.pop(str(suite_dir), None)
    index = _get_task_index(root)
    if raw in index:
        return index[raw]

    raise FileNotFoundError(f"Task not found: {task_id_or_dir!r} (searched {suite_dir})")


def load_cab_task(dataset_root: Path, task_id_or_dir: str) -> Any:
    """Load a CommerceAgentBench `TaskSpec` by task ID or directory path."""
    root = Path(dataset_root).expanduser().resolve()
    suite_dir = _effective_suite_dir(root)
    case_dir = _resolve_case_dir(root, task_id_or_dir)

    with _CAB_LOAD_LOCK:
        has_bench_core_dir = (root / "bench_core").is_dir()
        if has_bench_core_dir:
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)

        bench_cli = None
        if has_bench_core_dir or importlib.util.find_spec("bench_core") is not None:
            try:
                from bench_core import cli as bench_cli_mod
                from bench_core import constants as bench_constants

                bench_cli = bench_cli_mod
                if has_bench_core_dir:
                    bench_constants.PROJECT_ROOT = root
                    bench_cli.PROJECT_ROOT = root
                bench_cli.DATASETS_DIR = suite_dir
            except Exception:
                bench_cli = None

        if bench_cli is not None:
            try:
                rel_or_abs = (
                    case_dir.relative_to(suite_dir).as_posix()
                    if case_dir.is_relative_to(suite_dir)
                    else str(case_dir)
                )
                return bench_cli.load_task(rel_or_abs)
            except SystemExit as exc:
                msg = str(exc)
                if "not found" in msg.lower() or "missing" in msg.lower():
                    raise FileNotFoundError(msg) from None
                raise ValueError(msg) from None

    return _load_task_toml_fallback(root, case_dir)


def discover_all_tasks(dataset_root: Path) -> list[str]:
    """Return all task IDs under `dataset_root` in deterministic sorted order."""
    root = Path(dataset_root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"Dataset root does not exist: {root}")
    index = _get_task_index(root)
    return sorted(index.keys())


def _split_csv_items(items: list[str] | None) -> list[str]:
    if not items:
        return []
    out: list[str] = []
    for raw in items:
        for part in str(raw).split(","):
            tok = part.strip()
            if tok:
                out.append(tok)
    return out


def _load_waves_from_file(wave_file: Path) -> dict[str, list[str]]:
    wf = Path(wave_file).expanduser().resolve()
    if not wf.is_file():
        raise FileNotFoundError(f"Wave file not found: {wf}")
    payload = json.loads(wf.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if isinstance(payload.get("waves"), dict):
            return {
                str(wname): [str(t) for t in tids]
                for wname, tids in payload["waves"].items()
                if isinstance(tids, list)
            }
        if isinstance(payload.get("task_ids"), list):
            cname = str(payload.get("collection_id") or wf.stem)
            return {cname: [str(t) for t in payload["task_ids"]]}
        if all(isinstance(v, list) for v in payload.values()):
            return {str(k): [str(t) for t in v] for k, v in payload.items()}
    if isinstance(payload, list):
        return {wf.stem: [str(t) for t in payload]}
    raise ValueError(f"Unrecognized wave file JSON structure in {wf}")


def select_tasks(
    dataset_root: Path,
    *,
    tasks: list[str] | None = None,
    categories: list[str] | None = None,
    wave_file: Path | None = None,
    wave: str | None = None,
    sample: int | None = None,
    seed: int = 42,
) -> tuple[list[str], dict[str, str]]:
    """Select benchmark task IDs and their wave assignments deterministically."""
    root = Path(dataset_root).expanduser().resolve()
    suite_dir = _effective_suite_dir(root)
    index = _get_task_index(root)
    if not index:
        raise ValueError(f"No tasks discovered under {suite_dir}")

    waves_by_name: dict[str, list[str]] = {}
    wave_lookup: dict[str, str] = {}
    if wave_file is not None:
        waves_by_name = _load_waves_from_file(wave_file)
        for wname, tids in waves_by_name.items():
            for tid in tids:
                wave_lookup.setdefault(tid, wname)

    explicit_tasks = _split_csv_items(tasks)
    wave_spec = (wave or "").strip()

    if explicit_tasks:
        resolved_ids: list[str] = []
        seen: set[str] = set()
        for item in explicit_tasks:
            if item in index:
                tid = item
            else:
                case_dir = _resolve_case_dir(root, item)
                spec_toml = tomllib.loads((case_dir / "task.toml").read_text(encoding="utf-8"))
                tid = str((spec_toml.get("task") or {}).get("id") or case_dir.name)
            if tid not in seen:
                seen.add(tid)
                resolved_ids.append(tid)
        candidate_ids = resolved_ids
    elif wave_file is not None:
        if not wave_spec or wave_spec.lower() == "all":
            ordered: list[str] = []
            seen = set()
            for tids in waves_by_name.values():
                for tid in tids:
                    if tid in index and tid not in seen:
                        seen.add(tid)
                        ordered.append(tid)
            candidate_ids = ordered
        else:
            requested_waves = [w.strip() for w in wave_spec.split(",") if w.strip()]
            if all(w in waves_by_name for w in requested_waves):
                ordered = []
                seen = set()
                for wname in requested_waves:
                    for tid in waves_by_name[wname]:
                        if tid in index and tid not in seen:
                            seen.add(tid)
                            ordered.append(tid)
                candidate_ids = ordered
            else:
                missing_waves = [w for w in requested_waves if w not in waves_by_name]
                raise ValueError(
                    f"Unknown wave(s) {missing_waves!r} in {wave_file}; available: {sorted(waves_by_name)}"
                )
    elif wave_spec and wave_spec.lower() != "all":
        parts = [p.strip() for p in wave_spec.split(",") if p.strip()]
        if all(p in index for p in parts):
            candidate_ids = list(dict.fromkeys(parts))
        else:
            raise ValueError(
                f"Cannot resolve wave={wave_spec!r} without --wave-file (and not matching task IDs)"
            )
    else:
        candidate_ids = sorted(index.keys())

    cat_filters = {c.lower() for c in _split_csv_items(categories)}
    if cat_filters:
        unknown_cats = cat_filters - set(KNOWN_CATEGORIES)
        if unknown_cats:
            raise ValueError(
                f"Unknown category filter(s) {sorted(unknown_cats)!r}; expected {list(KNOWN_CATEGORIES)}"
            )
        candidate_ids = [
            tid
            for tid in candidate_ids
            if _infer_task_category(index[tid], suite_dir, tid) in cat_filters
        ]

    if sample is not None:
        if sample < 0:
            raise ValueError(f"sample must be >= 0, got {sample}")
        if sample == 0:
            return [], {}
        if sample < len(candidate_ids):
            rng = random.Random(seed)
            sampled_set = set(rng.sample(candidate_ids, sample))
            # Preserve canonical order while selecting the exact deterministic RNG subset.
            candidate_ids = [tid for tid in candidate_ids if tid in sampled_set]

    default_wave = wave_spec if (wave_spec and wave_spec in waves_by_name) else "custom"
    wave_map = {tid: wave_lookup.get(tid, default_wave) for tid in candidate_ids}
    return candidate_ids, wave_map
