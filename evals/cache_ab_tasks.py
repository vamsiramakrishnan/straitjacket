"""Frozen coding-suite fixtures and an independent, model-free referee.

This is a task referee, not a hostile-Python operating-system sandbox. Model
edits are confined to declared source files; graders never trust edited tests.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from evals.coding_suite import BY_NAME, REGISTRY

TASKS = tuple(task.name for task in REGISTRY)
EDITABLE = {
    "quiet_flood": ("app/router.py",),
    "keyword_flood": ("app/router.py",),
    "traceback": ("pipeline.py",),
    "bigtest": ("mathlib.py",),
    "multifile": ("api.py", "textutil.py"),
}
DIAGNOSTICS = {
    "quiet_flood": "python3 diagnose.py", "keyword_flood": "python3 diagnose.py",
    "traceback": "python3 run.py", "bigtest": "python3 tests/verify.py",
    "multifile": "python3 tests/verify.py",
}
CONFIG = """[store]
max_capture_bytes = 1048576
[picker]
mode = "local"
"""
EXTRA_CHECKS = {
    "quiet_flood": "from app.router import route\nassert route('eu') == 'primary-eu'\nassert route('xx') == 'primary-xx'\nassert route('apac') == 'fallback-apac'\n",
    "keyword_flood": "from app.router import route\nassert route('eu') == 'primary-eu'\nassert route('xx') == 'primary-xx'\nassert route('apac') == 'fallback-apac'\n",
    "traceback": "from pipeline import normalize, load\nassert normalize({}) == 2.0\nassert normalize({'ratio': 0}) == 0\nassert load({'ratio': -2.5}) == -5.0\n",
    "bigtest": "from mathlib import clamp, is_even, last, merge, sign\nassert all(is_even(n) == (n % 2 == 0) for n in range(-8, 9))\nassert [sign(n) for n in (-3, 0, 3)] == [-1, 0, 1]\nassert last(['a', 'b']) == 'b'\nassert clamp(-10, -3, 9) == -3\nassert merge({'a': 1}, {'a': 2}) == {'a': 2}\ntry:\n    last([])\nexcept IndexError:\n    pass\nelse:\n    raise AssertionError('empty last must fail')\n",
    "multifile": "from api import make_slug\nfrom textutil import slugify\nassert make_slug('  A B  ') == 'a-b'\nassert slugify('MiXeD') == 'mixed'\nassert slugify('') == ''\n",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Fixture:
    name: str
    root: Path
    prompt: str
    editable: tuple[str, ...]
    initial: Mapping[str, bytes]

    def manifest(self) -> dict:
        return {
            "task": self.name, "prompt": self.prompt, "editable": list(self.editable),
            "files": {path: sha256(data) for path, data in sorted(self.initial.items())},
            "referee_sha256": sha256(EXTRA_CHECKS[self.name].encode()),
        }


def build_fixture(task_name: str, root: Path) -> Fixture:
    task = BY_NAME[task_name]
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("fixture directory must be empty")
    root.mkdir(parents=True, exist_ok=True)
    task.build(root)
    (root / "ctx.toml").write_text(CONFIG, encoding="utf-8")
    initial = {p.relative_to(root).as_posix(): p.read_bytes()
               for p in root.rglob("*") if p.is_file()}
    return Fixture(task_name, root, task.prompt, EDITABLE[task_name], MappingProxyType(initial))


def integrity_errors(fixture: Fixture) -> list[str]:
    errors = []
    for rel, original in fixture.initial.items():
        path = fixture.root / rel
        if any(p.is_symlink() for p in (path, *path.parents) if p != fixture.root.parent):
            errors.append(f"symlink: {rel}")
        elif not path.is_file():
            errors.append(f"missing: {rel}")
        elif rel not in fixture.editable and path.read_bytes() != original:
            errors.append(f"immutable file changed: {rel}")
    for path in fixture.root.rglob("*"):
        rel = path.relative_to(fixture.root).as_posix()
        if path.is_symlink():
            errors.append(f"symlink: {rel}")
        elif path.is_file() and rel not in fixture.initial:
            # ctx maintains its session bookkeeping here; it is never copied
            # into the referee and is unavailable through allowed read tools.
            if not rel.startswith(".ctx-session-reads/"):
                errors.append(f"undeclared file: {rel}")
    return sorted(set(errors))


def _grader_env() -> dict[str, str]:
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1"}


def grade(fixture: Fixture, *, timeout_s: float = 10.0) -> dict:
    errors = integrity_errors(fixture)
    if errors:
        return {"accepted": False, "details": errors, "exit_code": None}
    nonce = "CACHE_AB_REFEREE_" + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix="sj-referee-") as td:
        root = Path(td)
        for rel, original in fixture.initial.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((fixture.root / rel).read_bytes() if rel in fixture.editable else original)
        wrapper = (
            "import sys, runpy\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "try:\n"
            f"    runpy.run_path({str(root / 'tests/verify.py')!r}, run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    if exc.code not in (None, 0):\n"
            "        raise\n"
            + EXTRA_CHECKS[fixture.name]
            + f"print({nonce!r})\n"
        )
        try:
            proc = subprocess.run([sys.executable, "-I", "-B", "-c", wrapper],
                                  cwd=root, env=_grader_env(), capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"accepted": False, "details": ["referee timeout"], "exit_code": None}
    accepted = proc.returncode == 0 and nonce in proc.stdout.splitlines()
    return {"accepted": accepted, "exit_code": proc.returncode,
            "details": [] if accepted else ["referee rejected candidate"],
            "stdout": proc.stdout.replace(nonce, "[referee completed]"), "stderr": proc.stderr}


def apply_gold(fixture: Fixture) -> None:
    """Calibration only. Never invoke from the live agent runner."""
    replacements = {
        "quiet_flood": [("app/router.py", "'apac': 'primary-apac'", "'apac': 'fallback-apac'")],
        "keyword_flood": [("app/router.py", "'apac': 'primary-apac'", "'apac': 'fallback-apac'")],
        "traceback": [("pipeline.py", "cfg['ratio']", "cfg.get('ratio', 1.0)")],
        "bigtest": [("mathlib.py", "n % 2 == 1", "n % 2 == 0"),
                    ("mathlib.py", "xs[len(xs)]", "xs[-1]"),
                    ("mathlib.py", "return 1 if n > 0 else -1", "return 0 if n == 0 else (1 if n > 0 else -1)")],
        "multifile": [("textutil.py", "s.strip().replace", "s.strip().lower().replace")],
    }
    for rel, old, new in replacements[fixture.name]:
        path = fixture.root / rel
        content = path.read_text(encoding="utf-8")
        if old not in content:
            raise ValueError("gold calibration expects an untouched fixture")
        path.write_text(content.replace(old, new), encoding="utf-8")
