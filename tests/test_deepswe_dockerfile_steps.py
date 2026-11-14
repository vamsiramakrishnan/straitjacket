"""DeepSWE adapter: Dockerfile `RUN` continuation handling.

The adapter replays a task's Dockerfile without Docker, running each RUN
command through `bash -c`. Docker deletes a trailing backslash together
with its newline, so a continued instruction is one command line. Joining
the pieces with a newline instead ends the command at each line break: a
bare argument line then runs as a command (exit 127) and a line that opens
with `&&` is a syntax error (exit 2). The texts below are synthetic copies
of the shapes that broke eight corpus tasks.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_ADAPTER = Path(__file__).resolve().parent.parent / "evals" / "agentbench" / "adapters" / "deepswe.py"
_spec = importlib.util.spec_from_file_location("deepswe_adapter_under_test", _ADAPTER)
deepswe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deepswe)


def _steps(tmp_path: Path, text: str) -> tuple[dict[str, str], list[str]]:
    df = tmp_path / "Dockerfile"
    df.write_text(text, encoding="utf-8")
    return deepswe._dockerfile_steps(df)


def _bash_ok(cmd: str) -> bool:
    return subprocess.run(["bash", "-n", "-c", cmd], capture_output=True).returncode == 0


def test_leading_and_chain_on_continuation_lines(tmp_path):
    # exit 2: a continuation line that starts with `&&`
    _, runs = _steps(tmp_path, (
        "FROM base\n"
        "RUN python3 -m pip install -r requirements.txt \\\n"
        "    && python3 -m pip install -r test-requirements.txt \\\n"
        '    && python3 -m pip install -e ".[yaml]"\n'
    ))
    assert len(runs) == 1
    assert "\n" not in runs[0]
    assert runs[0].split() == (
        'python3 -m pip install -r requirements.txt && python3 -m pip install -r '
        'test-requirements.txt && python3 -m pip install -e ".[yaml]"'
    ).split()
    assert _bash_ok(runs[0])


def test_argument_per_line_stays_one_command(tmp_path):
    # exit 127: arguments on their own continued lines
    _, runs = _steps(tmp_path, (
        "RUN pip install --no-cache-dir \\\n"
        '    "pytest>=9.0.0" \\\n'
        '    "anyio[trio]>=3.2.1" \\\n'
        "    coverage\n"
    ))
    assert runs == ['pip install --no-cache-dir     "pytest>=9.0.0"     "anyio[trio]>=3.2.1"     coverage']


def test_comment_and_blank_lines_inside_a_continued_run(tmp_path):
    _, runs = _steps(tmp_path, (
        "RUN apt-get update \\\n"
        "    # a note\n"
        "\n"
        "    && apt-get install -y gcc\n"
        "RUN echo after\n"
    ))
    assert len(runs) == 2
    assert "#" not in runs[0]
    assert runs[0].split() == "apt-get update && apt-get install -y gcc".split()
    assert runs[1] == "echo after"


def test_trailing_operator_then_continuation_still_one_command(tmp_path):
    _, runs = _steps(tmp_path, (
        "RUN python -m pip install \\\n"
        '      "pytest>=8" && \\\n'
        "    python -c \"print('ok')\"\n"
    ))
    assert len(runs) == 1
    assert "\n" not in runs[0]
    assert _bash_ok(runs[0])


def test_continued_run_executes_as_a_single_command(tmp_path):
    """Run the joined step with a stub `pip`: it must be called once with all
    arguments, not once bare and then with each argument run as a command."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "pip"
    stub.write_text('#!/bin/sh\necho "pip:$*"\n', encoding="utf-8")
    stub.chmod(0o755)
    _, runs = _steps(tmp_path, (
        "RUN pip install \\\n"
        "      alpha \\\n"
        "      beta \\\n"
        "    && pip install gamma\n"
    ))
    proc = subprocess.run(["bash", "-o", "pipefail", "-c", runs[0]], capture_output=True, text=True,
                          env={"PATH": f"{stub_dir}:/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert [ln.split() for ln in proc.stdout.splitlines()] == [
        ["pip:install", "alpha", "beta"], ["pip:install", "gamma"]]


def test_install_chained_before_clean_tree_assertion_is_kept(tmp_path):
    # The image's cleanliness assertion is dropped, but the reporter install
    # chained in front of it must still run (the grader imports it).
    _, runs = _steps(tmp_path, (
        "RUN python3 -m pip install --no-cache-dir 'junitxml==0.7' \\\n"
        "    && command -v subunit2junitxml \\\n"
        '    && test -z "$(git status --porcelain)"\n'
        'RUN test -z "$(git status --porcelain)"\n'
        'RUN git status --porcelain && test -z "$(git status --porcelain)"\n'
    ))
    assert len(runs) == 1
    assert runs[0].split() == "python3 -m pip install --no-cache-dir 'junitxml==0.7' && command -v subunit2junitxml".split()


def test_single_line_and_env_steps_unchanged(tmp_path):
    env, runs = _steps(tmp_path, (
        "FROM base\n"
        "# comment\n"
        "ARG BASE_SHA=abc123\n"
        "ENV FOO=bar BAZ=\"q q\"\n"
        "RUN pip install x\n"
        "RUN git clone https://example.invalid/r .\n"
    ))
    assert env == {"BASE_SHA": "abc123", "FOO": "bar", "BAZ": "q q"}
    assert runs == ["pip install x"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_dangling_continuation_at_end_of_file_is_kept(tmp_path):
    _, runs = _steps(tmp_path, "RUN echo hi \\\n")
    assert runs == ["echo hi"]
