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
import re
import shlex
import shutil
import stat
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
    # A scored run refuses to start without the bwrap sandbox unless this is set
    # (`run --allow-unsandboxed`); results then record sandbox="none".
    allow_unsandboxed: bool = False

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
    # How the trial was sandboxed: "bwrap" (mount + PID namespaces), "none" (path/env
    # confinement only, no filesystem isolation; allowed only by --allow-unsandboxed),
    # "" for results written before this was recorded.
    sandbox: str = ""

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
    files, the ctx version and code hashes on the sj arm, whether the bwrap sandbox is
    active)."""
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
    # Staging copies through directory symlinks (shutil.copytree's default), so the walk
    # follows them too: what a trial receives is what is hashed. Each real directory is
    # hashed once, under the first path that reaches it; any further path to it (a second
    # link, or a link back to an ancestor, which would otherwise never end) is recorded
    # by name only.
    seen_dirs: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        try:
            st_dir = os.stat(dirpath)
            dir_key = (st_dir.st_dev, st_dir.st_ino)
        except OSError:
            dir_key = None
        if dir_key is None or dir_key in seen_dirs:
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            marker = "unreadable-dir" if dir_key is None else "dir-already-hashed"
            h.update(rel_dir.encode("utf-8") + b"\0" + marker.encode() + b"\n")
            dirnames[:] = []
            continue
        seen_dirs.add(dir_key)
        dirnames[:] = sorted(d for d in dirnames if d not in _DIGEST_SKIP_DIRS)
        for name in sorted(filenames):
            if name in _DIGEST_SKIP_FILES or name.endswith(".pyc"):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            # Only a regular file is opened: a FIFO would block the read forever and a
            # device has no content to hash. Whatever cannot be read is recorded by
            # what it is, so the digest stays stable and the trial decides what a
            # missing or unreadable input means.
            try:
                st = p.stat()
                entry = _file_digest(p, st) if stat.S_ISREG(st.st_mode) else f"not-a-file:{stat.S_IFMT(st.st_mode):o}"
            except OSError as exc:
                entry = f"unreadable:{type(exc).__name__}"
            h.update(rel.encode("utf-8") + b"\0" + entry.encode() + b"\n")
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


_CODE_ID_CACHE: dict[str, str] = {}
_CODE_ID_LOCK = threading.Lock()
# Printed by the probe below; run with ``-P`` and cwd="/" so nothing is imported from the
# caller's (or a trial's) directory. ``find_spec`` of a top-level name does not import it.
_FIND_CTX_PROBE = (
    "import importlib.util as u\n"
    "s = u.find_spec('ctx')\n"
    "locs = list(s.submodule_search_locations or []) if s is not None else []\n"
    "print(locs[0] if locs else (s.origin or '') if s is not None else '')\n"
)
_PY_IMPORTS_CTX_RE = re.compile(rb"^\s*(?:from\s+ctx(?:\.\w+)*\s+import\b|import\s+ctx\b)", re.M)
_PIP_TRAMPOLINE_RE = re.compile(rb"^'''exec' \"?([^\"\s]+)\"?", re.M)


def _code_digest(location: Path) -> str:
    """Content hash of a package directory (``__pycache__``/``.pyc`` skipped, relative
    paths only, so a moved checkout hashes the same) or of a single module file."""
    if location.is_dir():
        return task_inputs_digest(location)
    return _file_digest(location, location.stat())[:16]


def _ctx_shim_path() -> Path:
    return Path(tempfile.gettempdir()) / f"straitjacket_ctx_bin_{os.getuid()}" / "ctx"


def _entry_point_interpreter(exe: Path) -> Path | None:
    """The Python interpreter of a console-script entry point that imports ``ctx``
    (``#!/path/python``, ``#!/usr/bin/env python3`` or pip's ``/bin/sh`` trampoline for
    long paths); ``None`` for anything else (a shell wrapper, a compiled binary)."""
    try:
        with exe.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if not head.startswith(b"#!") or not _PY_IMPORTS_CTX_RE.search(head):
        return None
    first = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    try:
        words = shlex.split(first)
    except ValueError:
        return None
    cand: str | None = None
    if words and Path(words[0]).name == "env":
        cand = next((w for w in words[1:] if not w.startswith("-") and "=" not in w), None)
        cand = shutil.which(cand) if cand else None
    elif words and "python" in Path(words[0]).name.lower():
        cand = words[0]
    else:
        m = _PIP_TRAMPOLINE_RE.search(head)
        cand = m.group(1).decode("utf-8", "replace") if m else None
    if not cand or "python" not in Path(cand).name.lower() or not Path(cand).is_file():
        return None
    return Path(cand)


def _find_ctx_package(interpreter: Path, extra_pythonpath: str = "") -> Path | None:
    """Where ``interpreter -P -m ctx`` would import ``ctx`` from, without importing it."""
    import subprocess

    env = dict(os.environ)
    if extra_pythonpath:
        env["PYTHONPATH"] = extra_pythonpath + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        cp = subprocess.run(
            [str(interpreter), "-P", "-c", _FIND_CTX_PROBE],
            capture_output=True, text=True, timeout=30, cwd="/", env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = (cp.stdout or "").strip().splitlines()
    if cp.returncode != 0 or not found or not found[-1].strip():
        return None
    loc = Path(found[-1].strip())
    return loc if loc.exists() else None


def ctx_code_identity(ctx_bin: Path | None = None) -> str:
    """Identity of the code the ``sj`` arm launches as ctx, computed once per process per
    executable: ``pkg:<hash>`` of the ``ctx`` package that executable imports (a
    console-script entry point, a Python interpreter run as ``-P -m ctx``, or the
    source-checkout shim), else ``exe:<hash>`` of the executable's own bytes (a shell
    wrapper or a compiled binary), else ``unavailable``. Nothing is imported or run from a
    trial directory: the probe runs the interpreter with ``-P`` from ``/``."""
    try:
        exe = resolve_ctx_bin(ctx_bin)
    except FileNotFoundError:
        return "unavailable"
    key = str(exe)
    with _CODE_ID_LOCK:
        hit = _CODE_ID_CACHE.get(key)
        if hit is not None:
            return hit
        pkg: Path | None = None
        if exe == _ctx_shim_path():
            repo_src = Path(__file__).resolve().parents[2] / "src"
            pkg = _find_ctx_package(Path(sys.executable).resolve(), str(repo_src))
        else:
            interp = exe if "python" in exe.name.lower() else _entry_point_interpreter(exe)
            if interp is not None:
                pkg = _find_ctx_package(interp)
        try:
            ident = f"pkg:{_code_digest(pkg)}" if pkg is not None else f"exe:{_code_digest(exe)}"
        except OSError:
            ident = "unavailable"
        _CODE_ID_CACHE[key] = ident
        return ident


def harness_ctx_code_identity() -> str:
    """Content hash of the ``ctx`` package this process imports (the ``sj`` arm's
    residence, deliverable audits and hook grammar run in-process), once per process."""
    with _CODE_ID_LOCK:
        hit = _CODE_ID_CACHE.get("<in-process>")
        if hit is not None:
            return hit
        try:
            import ctx  # noqa: PLC0415

            origin = Path(getattr(ctx, "__file__", "") or "")
            loc = origin.parent if origin.name == "__init__.py" else origin
            ident = f"pkg:{_code_digest(loc)}"
        except Exception as exc:
            ident = f"unavailable ({type(exc).__name__})"
        _CODE_ID_CACHE["<in-process>"] = ident
        return ident


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
