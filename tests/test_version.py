"""The CLI, runtime package, and distribution share one version source."""

from pathlib import Path
import subprocess
import sys

import pytest


def test_cli_version_matches_runtime(capsys):
    from ctx import __version__
    from ctx.cli import _build_parser

    with pytest.raises(SystemExit) as stopped:
        _build_parser().parse_args(["--version"])

    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"ctx {__version__}"


def test_release_tag_matches_the_checkout(tmp_path):
    from ctx import __version__

    script = Path(__file__).resolve().parents[1] / "scripts" / "check_release_tag.py"
    expected = f"v{__version__}"
    for tag in (expected, __version__, "v0.0.0"):
        result = subprocess.run(
            [sys.executable, str(script), tag], cwd=tmp_path, capture_output=True, text=True
        )
        if tag == expected:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode == 1
            assert repr(tag) in result.stderr
            assert repr(expected) in result.stderr
