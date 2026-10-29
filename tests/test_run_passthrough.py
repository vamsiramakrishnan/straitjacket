"""`ctx run --passthrough`: the hook rewrite must be native-shaped where
capture buys nothing.

Measured on DeepSWE (evals/agentbench, haiku): the agent's own commands
produced a median of 91-277 bytes, yet every routed one came back as a
receipt (header, command echo, status line) and a failing `python -c`
reported ctx's exit 3 in place of the command's 1. Tool results grew 2-8x
and the model re-ran identical failing checks. In passthrough mode small,
complete output is printed verbatim with the run handle on one trailing
line, and the exit status is the wrapped command's own; large output still
digests, and the artifact is stored either way.
"""

from __future__ import annotations

import re
import sys

HANDLE_RE = re.compile(r"\[ctx run:[0-9a-f]+ · exit (\d+)\]$")


def _run(workspace_dir, *argv):
    from ctx.cli import main as cli_main

    return cli_main(["--workspace", str(workspace_dir), "run", *argv])


def test_small_output_is_verbatim_with_the_commands_own_exit(state_home, workspace_dir, capsys):
    rc = _run(
        workspace_dir, "--passthrough", "--",
        sys.executable, "-c", "import sys; print('hello'); print('warn', file=sys.stderr); sys.exit(4)",
    )
    out = capsys.readouterr().out
    assert rc == 4  # the command's status, not ctx's 3
    lines = out.rstrip("\n").split("\n")
    assert lines[0] == "hello"
    assert "warn" in lines[1:-1]
    m = HANDLE_RE.match(lines[-1])
    assert m and m.group(1) == "4"
    assert "profile=" not in out and "command:" not in out  # no receipt


def test_without_the_flag_the_receipt_and_exit_three_are_unchanged(state_home, workspace_dir, capsys):
    rc = _run(workspace_dir, "--", sys.executable, "-c", "print('hello'); raise SystemExit(4)")
    out = capsys.readouterr().out
    assert rc == 3
    assert out.startswith("[ctx run:") and "profile=" in out.splitlines()[0]


def test_shell_form_and_success_status(state_home, workspace_dir, capsys):
    rc = _run(workspace_dir, "--passthrough", "--shell", "--", "printf 'a\\nb\\n' | tr a z")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("z\nb\n")
    assert HANDLE_RE.match(out.rstrip("\n").split("\n")[-1])


def test_large_output_still_digests_but_keeps_the_native_exit(state_home, workspace_dir, capsys):
    # Past the inline budget the digest is doing real work: keep it. The exit
    # status is still the command's, so the host never sees a foreign code.
    code = "import sys; print('x' * 60000); sys.exit(2)"
    rc = _run(workspace_dir, "--passthrough", "--", sys.executable, "-c", code)
    out = capsys.readouterr().out
    assert rc == 2
    assert out.startswith("[ctx run:") and "profile=" in out.splitlines()[0]
    assert "x" * 60000 not in out


def test_passthrough_output_is_retrievable_by_handle(state_home, workspace_dir, capsys):
    from ctx.cli import main as cli_main

    _run(workspace_dir, "--passthrough", "--", sys.executable, "-c", "print('needle-7f3a')")
    out = capsys.readouterr().out
    handle = re.search(r"\[ctx run:([0-9a-f]+)", out).group(1)
    rc = cli_main(["--workspace", str(workspace_dir), "get", f"run:{handle}#stdout"])
    assert rc == 0
    assert "needle-7f3a" in capsys.readouterr().out


def test_emission_parity_small_output_is_at_most_raw_plus_one_trailer_line(state_home, workspace_dir, capsys):
    # The invariant behind the flag: a rewritten command whose output fits
    # the inline budget may not cost the transcript more than native execution
    # plus one handle line. This is what bounds the "receipt inflation" the
    # DeepSWE receipt measured at 2-8x.
    raw = "line one\nline two\nline three\n"
    rc = _run(workspace_dir, "--passthrough", "--", sys.executable, "-c", f"import sys; sys.stdout.write({raw!r}); sys.exit(0)")
    out = capsys.readouterr().out
    assert rc == 0
    body, _, trailer = out.rstrip("\n").rpartition("\n")
    assert body + "\n" == raw
    assert HANDLE_RE.match(trailer) and len(trailer) <= 40
    assert len(out) <= len(raw) + 41
