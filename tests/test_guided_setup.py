"""Acceptance: `ctx wrap setup` guides rather than dumps.

The old flow printed each installer's output and stopped. That answers "what
files did you write" but not the two questions a developer actually has — *did
that work*, and *what do I do now* — and it answers neither when something
fails, which is exactly when being left high and dry hurts.

So the guided flow is pinned here: four labelled steps, verification through the
doctor's own checks rather than a second opinion, a concrete next action, and —
the part that matters — an honest, actionable failure path with a non-zero exit.
"""

import pytest

from ctx.wrap import guided_setup


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    from ctx.workspace import resolve_workspace

    return resolve_workspace(str(d))


def _run(ws, capsys, **kw) -> tuple[int, str]:
    code = guided_setup(ws, **kw)
    return code, capsys.readouterr().out


def test_all_four_steps_are_labelled(ws, capsys):
    _, out = _run(ws, capsys, hosts=["claude"])
    for step in ("[1/4] What you have", "[2/4] Harnessing",
                 "[3/4] Verifying", "[4/4] What now"):
        assert step in out


def test_it_verifies_with_the_doctors_own_checks(ws, capsys, monkeypatch):
    """Verification must not be a second opinion about what healthy means."""
    import ctx.installer as inst

    seen = []
    real = inst.doctor_checks
    monkeypatch.setattr(inst, "doctor_checks",
                        lambda w, **k: seen.append(w) or real(w, **k))
    _run(ws, capsys, hosts=["claude"])
    assert seen, "guided setup did not run the doctor checks"


def test_success_ends_with_a_concrete_next_action(ws, capsys):
    code, out = _run(ws, capsys, hosts=["claude"])
    assert code == 0
    assert "checks passed" in out
    assert "ctx run --" in out       # the thing to try right now
    assert "ctx gain" in out         # how to see it working
    assert "undo:" in out            # how to get out again


def test_failure_is_named_and_actionable_and_exits_nonzero(ws, capsys, monkeypatch):
    """The load-bearing case. A failed check must be printed, counted, and must
    not report success — a setup that says 'done' while broken is worse than one
    that says nothing."""
    import ctx.installer as inst

    monkeypatch.setattr(inst, "doctor_checks", lambda w, **k: [
        ("ctx on PATH", False, "not found"),
        ("store writable", True, ""),
    ])
    code, out = _run(ws, capsys, hosts=["claude"])
    assert code == 1                              # non-zero: scripts can see it
    assert "✗ ctx on PATH" in out
    assert "not found" in out
    assert "1/2 checks passed" in out
    assert "fix the checks above first" in out
    assert "Nothing else to do" not in out        # never claims success


def test_no_agent_installed_explains_rather_than_failing(ws, capsys, monkeypatch):
    """A machine with no coding-agent CLI is a normal state, not an error: the
    config is inert until a CLI reads it, and setup should say so instead of
    printing an empty report."""
    import ctx.wrap as wrap_mod

    monkeypatch.setattr(wrap_mod, "_guided_survey", lambda w: ([], [], []))
    code, out = _run(ws, capsys)
    assert "no coding-agent CLI found on PATH" in out
    assert "inert until a CLI reads it" in out
    assert code == 0
    assert not (ws.root / ".ctx/hosts/hermes.json").exists()


def test_optional_hosts_are_offered_never_done(ws, capsys, monkeypatch):
    """A host ctx would have to *build* (a venv, off the network) is offered
    with its cost, never configured implicitly."""
    import ctx.wrap as wrap_mod
    from ctx.hosts import DetectedHost, host_by_name

    spec = host_by_name("antigravity-sdk")
    fake = DetectedHost(spec=spec, installed=False, path=None,
                        model=spec.default_model, price=None, version=None)
    monkeypatch.setattr(wrap_mod, "_guided_survey", lambda w: ([], [], [fake]))
    _, out = _run(ws, capsys)
    assert "optional" in out
    assert "ctx wrap antigravity-sdk" in out


def test_plain_mode_stays_scriptable(ws, capsys, monkeypatch):
    """Scripts that parse the installer report keep a way to get just that."""
    from ctx.wrap import wrap_setup

    monkeypatch.setenv("CTX_SETUP_PLAIN", "1")
    wrap_setup(ws.root, ["claude"])
    out = capsys.readouterr().out
    assert "[1/4]" not in out
    assert "── claude ──" in out
