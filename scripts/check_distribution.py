#!/usr/bin/env python3
"""Validate ctx-harness release artifacts outside the source checkout.

Editable installs can borrow templates and contrib files from the repository,
so the normal acceptance suite cannot prove that a release artifact is
self-contained. This check verifies the source distribution's release inputs,
then extracts the wheel into an empty directory and exercises only the
installed package.
"""

from __future__ import annotations

import email.parser
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import venv
import zipfile
from pathlib import Path


REQUIRED_FILES = {
    "ctx/data/native-hooks/bridge.mjs",
    "ctx/data/native-hooks/hermes.py",
    "ctx/data/native-hooks/omp.mjs",
    "ctx/data/native-hooks/opencode.mjs",
    "ctx/data/native-hooks/dsh.mjs",
    "ctx/acp.py",
    "ctx/mcp_edits.py",
    "ctx/data/antigravity/plugin.json",
    "ctx/data/antigravity/hooks.json",
    "ctx/data/codex/config.toml",
    "ctx/data/codex/hooks.json",
    "ctx/data/ctx_agy.py",
    "ctx/data/model-catalog.json",
    "ctx/data/model-prices.json",
    "ctx/prefix-manifest.json",
}

REQUIRED_SDIST_FILES = {
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "contrib/ctx-agy/ctx_agy.py",
    "plugins/antigravity/hooks.json",
    "plugins/antigravity/plugin.json",
    "plugins/codex/config.toml",
    "plugins/codex/hooks.json",
    "src/ctx/__init__.py",
    "src/ctx/data/model-catalog.json",
    "src/ctx/data/model-prices.json",
    "src/ctx/prefix-manifest.json",
}


def _run(executable: Path, cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [str(executable), *args],
        cwd=cwd,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}{proc.stderr}"
        )
    return proc.stdout


def _sdist_version(sdist: Path) -> str:
    if not sdist.is_file() or not sdist.name.endswith(".tar.gz"):
        raise ValueError(f"not a source distribution: {sdist}")
    with tarfile.open(sdist, "r:gz") as archive:
        members = [m for m in archive.getmembers() if m.isfile()]
        roots = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
        if len(roots) != 1:
            raise RuntimeError(f"source distribution has unexpected roots: {sorted(roots)}")
        root = next(iter(roots))
        names = {
            Path(*Path(m.name).parts[1:]).as_posix()
            for m in members
            if len(Path(m.name).parts) > 1
        }
        missing = sorted(REQUIRED_SDIST_FILES - names)
        if missing:
            raise RuntimeError("source distribution is missing:\n  " + "\n  ".join(missing))
        pkg_info = archive.extractfile(f"{root}/PKG-INFO")
        if pkg_info is None:
            raise RuntimeError("source distribution has no PKG-INFO")
        metadata = email.parser.BytesParser().parsebytes(pkg_info.read())
        version = metadata["Version"]
        if not version:
            raise RuntimeError("source distribution PKG-INFO has no Version")
        return version


def check(wheel: Path, sdist: Path | None = None) -> None:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise ValueError(f"not a wheel: {wheel}")

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_FILES - names)
        if missing:
            raise RuntimeError("wheel is missing:\n  " + "\n  ".join(missing))

        metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise RuntimeError(f"expected one METADATA file, found {metadata_names}")
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_names[0]))
        version = metadata["Version"]
        if not version:
            raise RuntimeError("wheel METADATA has no Version")

        if sdist is not None:
            sdist_version = _sdist_version(sdist)
            if sdist_version != version:
                raise RuntimeError(
                    f"artifact version mismatch: wheel={version}, sdist={sdist_version}"
                )

        with tempfile.TemporaryDirectory(prefix="ctx-wheel-smoke-") as temp:
            clean_root = Path(temp)
            venv_root = clean_root / "venv"
            venv.EnvBuilder(with_pip=True).create(venv_root)
            bin_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
            python = bin_dir / ("python.exe" if os.name == "nt" else "python")
            ctx = bin_dir / ("ctx.exe" if os.name == "nt" else "ctx")
            installed = subprocess.run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
                cwd=clean_root,
                text=True,
                capture_output=True,
                timeout=120,
            )
            if installed.returncode != 0:
                raise RuntimeError(f"wheel install failed:\n{installed.stderr}")

            actual = _run(ctx, clean_root, "--version").strip()
            if actual != f"ctx {version}":
                raise RuntimeError(
                    f"runtime version mismatch: expected 'ctx {version}', got {actual!r}"
                )

            for host in ("antigravity", "claude", "codex", "hermes", "omp", "opencode", "dsh"):
                rendered = _run(ctx, clean_root, "wrap", host, "--print-config")
                if not rendered.strip():
                    raise RuntimeError(f"{host} renderer returned no configuration")
                if host in ("hermes", "omp", "opencode", "dsh"):
                    json.loads(rendered)

            # Exercise the two Codex contracts that are easy to get subtly
            # wrong and otherwise fail only when the next Codex session starts.
            # The renderer must keep the executable separate from argv, and a
            # normal PreToolUse pass-through must not emit the rewrite-only
            # permissionDecision="allow" shape.
            probe_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            probe_env["PATH"] = str(bin_dir)
            config_probe = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import pathlib, tomllib; "
                    "from ctx.installer import _ctx_executable, _render_codex_file; "
                    "d=tomllib.loads(_render_codex_file('config.toml', _ctx_executable())); "
                    "s=d['mcp_servers']['ctx-harness']; "
                    "assert pathlib.Path(s['command']).is_file(), s; "
                    "assert s['args'][-2:] == ['mcp', '--bounded-only'], s; "
                    "print(s['command'])",
                ],
                cwd=clean_root,
                env=probe_env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            if config_probe.returncode != 0:
                raise RuntimeError(f"Codex MCP config probe failed:\n{config_probe.stderr}")

            hook_probe = subprocess.run(
                [str(ctx), "hook", "codex", "pre-tool-use"],
                cwd=clean_root,
                env=probe_env,
                input=json.dumps(
                    {
                        "cwd": str(clean_root),
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo hi"},
                    }
                ),
                text=True,
                capture_output=True,
                timeout=30,
            )
            if hook_probe.returncode != 0:
                raise RuntimeError(f"Codex PreToolUse probe failed:\n{hook_probe.stderr}")
            try:
                hook_output = json.loads(hook_probe.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Codex PreToolUse returned invalid JSON: {hook_probe.stdout!r}"
                ) from exc
            if hook_output != {}:
                raise RuntimeError(
                    "Codex pass-through emitted an unsupported decision: "
                    f"{hook_output!r}"
                )

            probe = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from ctx.agysdk import shim_source; "
                    "p=shim_source(); assert p.is_file(), p; print(p)",
                ],
                cwd=clean_root,
                env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                text=True,
                capture_output=True,
                timeout=30,
            )
            if probe.returncode != 0:
                raise RuntimeError(f"Antigravity shim probe failed:\n{probe.stderr}")

    artifacts = wheel.name if sdist is None else f"{wheel.name} + {sdist.name}"
    print(f"distribution OK: {artifacts} (ctx {version}, all host assets present)")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print(
            "usage: python scripts/check_distribution.py path/to/*.whl [path/to/*.tar.gz]",
            file=sys.stderr,
        )
        return 2
    try:
        check(
            Path(args[0]).resolve(),
            Path(args[1]).resolve() if len(args) == 2 else None,
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"distribution check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
