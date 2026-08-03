"""`[redaction] enabled = false` means what CONFIGURATION.md says it means.

The flag was parsed into the Config object and then read by NOBODY: every
call site reached past it for `.patterns`, so the documented switch was a
setting the docs promised and the code ignored. The fix is the SIGNATURE --
sanitize_for_model takes the config section, not its patterns, so there is
no longer a call shape that can drop the flag on the way in.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import pytest
from conftest import make_store, make_ws

SECRET = "AKIAIOSFODNN7EXAMPLE"


def _redaction(**kw):
    from ctx.config import Redaction

    return Redaction(**{"patterns": ("aws-access-key",), **kw})


def test_enabled_true_redacts():
    from ctx.textutil import sanitize_for_model

    out, fired = sanitize_for_model(f"key={SECRET}", _redaction(enabled=True))
    assert SECRET not in out
    assert fired == ["aws-access-key"]


def test_enabled_false_does_not_redact():
    from ctx.textutil import sanitize_for_model

    out, fired = sanitize_for_model(f"key={SECRET}", _redaction(enabled=False))
    assert SECRET in out, "the documented switch must actually switch"
    assert fired == []


def test_control_stripping_survives_the_switch_being_off():
    """`enabled` governs REDACTION. Control stripping is a separate concern
    and turning redaction off must not also stop sanitizing terminal escapes."""
    from ctx.textutil import sanitize_for_model

    out, _ = sanitize_for_model("a\x1b[31mb", _redaction(enabled=False))
    assert "\x1b[31m" not in out


def test_the_switch_reaches_the_retrieval_emitter(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(f"key={SECRET}\n".encode())
    ref = f"blob:{blob[:12]}"

    ws.config = dataclasses.replace(ws.config, redaction=_redaction(enabled=True))
    assert SECRET not in get(store, ws, ref, Selector(lines=(1, 1)))

    ws.config = dataclasses.replace(ws.config, redaction=_redaction(enabled=False))
    assert SECRET in get(store, ws, ref, Selector(lines=(1, 1)))


def test_the_switch_reaches_the_exact_bytes_path(state_home, workspace_dir):
    """--bytes skips control STRIPPING, not redaction -- and it goes through
    the same switch, so the flag means the same thing on both paths."""
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    raw = f"key={SECRET}\n".encode()
    ref = f"blob:{store.put_blob(raw)[:12]}"

    ws.config = dataclasses.replace(ws.config, redaction=_redaction(enabled=True))
    assert SECRET not in get(store, ws, ref, Selector(bytes=(1, len(raw))))

    ws.config = dataclasses.replace(ws.config, redaction=_redaction(enabled=False))
    assert SECRET in get(store, ws, ref, Selector(bytes=(1, len(raw))))


def test_no_call_site_reaches_past_the_config_section():
    """The invariant behind the fix. Pulling `.patterns` out at a call site is
    exactly how `enabled` got lost, so no call site may do it again."""
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "ctx"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if path.name in ("config.py", "textutil.py"):
            continue  # where patterns are DEFINED and where the pair is read
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"redaction\.patterns\b", line):
                offenders.append(f"{path.relative_to(src)}:{i}: {line.strip()}")
    assert not offenders, (
        "pass ws.config.redaction (the section), not its patterns -- reaching "
        "past the section is how `enabled` stopped working:\n  "
        + "\n  ".join(offenders)
    )


# ------------------------------------------------ the other two docs drifts
def test_seq_accepts_the_documented_step_flag(state_home, workspace_dir, capsys):
    """docs/CLI.md documents a repeatable `--step`; only the positional was
    registered, so every invocation following the docs verbatim died on an
    argparse error."""
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(workspace_dir), "seq",
                   "--step", "echo one", "--step", "echo two"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "one" in out or "step" in out


def test_seq_still_accepts_positionals_and_mixes_them(state_home, workspace_dir, capsys):
    from ctx.cli import main as cli_main

    assert cli_main(["--workspace", str(workspace_dir), "seq", "echo pos"]) == 0
    capsys.readouterr()
    assert cli_main(["--workspace", str(workspace_dir), "seq",
                     "--step", "echo flag", "echo pos"]) == 0


def test_seq_with_no_steps_refuses(state_home, workspace_dir, capsys):
    from ctx.cli import main as cli_main

    rc = cli_main(["--workspace", str(workspace_dir), "seq"])
    assert rc == 2
    assert "at least one step" in capsys.readouterr().err


def test_checkpoint_expands_an_uppercase_ref(state_home, workspace_dir):
    """parse_ref lower-cases the id; ref_text is what the user TYPED, and the
    ref grammar accepts [0-9a-fA-F]. A case-sensitive replace no-opped on
    `run:6A1B3F5B`, freezing an unexpanded abbreviation into a checkpoint
    whose whole promise is exact coordinates."""
    from ctx.checkpoint import create_checkpoint

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"evidence\n")

    short_upper = blob[:8].upper()
    cp_id, doc = create_checkpoint(
        store, ws, goal="g", evidence=[f"blob:{short_upper}"], state="s"
    )
    frozen = store.get_manifest(cp_id)["evidence"][0]["ref"]
    assert frozen == f"blob:{blob[:12]}", frozen
    assert short_upper not in frozen, "the abbreviation must be expanded"


def test_checkpoint_still_expands_a_lowercase_ref(state_home, workspace_dir):
    from ctx.checkpoint import create_checkpoint

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    blob = store.put_blob(b"evidence\n")
    cp_id, _doc = create_checkpoint(
        store, ws, goal="g", evidence=[f"blob:{blob[:8]}"], state="s"
    )
    assert store.get_manifest(cp_id)["evidence"][0]["ref"] == f"blob:{blob[:12]}"
