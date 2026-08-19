"""The CLI, runtime package, and distribution share one version source."""

import pytest


def test_cli_version_matches_runtime(capsys):
    from ctx import __version__
    from ctx.cli import _build_parser

    with pytest.raises(SystemExit) as stopped:
        _build_parser().parse_args(["--version"])

    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"ctx {__version__}"
