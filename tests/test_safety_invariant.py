"""The rule-7 / EDC §11.4 invariant: safety is OUTSIDE the plan space.

Safety-class guard decisions (secret-path reads, workspace-escape reads,
committed ctx.toml deny_commands) must be BYTE-IDENTICAL under every
adaptive state the system can reach — empty session, reflex densify latch,
bypass latch, window pressure, reader-capability latch, and all of them at
once. No reflex, circuit, resolver, or epoch state may weaken (or even
reword) a safety denial. This is the property test EDC §11.4 demands:
safety-class ⇒ adaptive:false, enforced by bytes, read-only against
ctx.hook.

Also covered: the contract-load seam surfaces floor<=ceiling violations
loudly (resolver.validate_rendering_policy today; ctx.contracts' loader
when that module ships).
"""

import json

import pytest


def _classify(tool_name, tool_input, workspace):
    from ctx.hook import classify

    return classify(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "workspacePaths": [str(workspace)],
            "session_id": "sess-invariant",
        }
    )


# ------------------------------------------------------- adaptive states
def _state_empty(ws):
    pass


def _state_densify_latched(ws):
    led = ws / ".ctx-session-reads"
    led.mkdir(parents=True, exist_ok=True)
    (led / "reflex.json").write_text(
        json.dumps(
            {
                "densify": {"pytest tests": True, "dangertool": True},
                "interventions": {
                    "dangertool": {"count": 3, "last_run": "abc123", "hints": 4,
                                   "starved": False, "armed": True}
                },
                "outcomes_appended": 3,
            }
        ),
        encoding="utf-8",
    )


def _state_bypass_latched(ws):
    led = ws / ".ctx-session-reads"
    led.mkdir(parents=True, exist_ok=True)
    (led / "reflex.json").write_text(
        json.dumps({"bypass": {"dangertool": True, "pytest tests": True}}),
        encoding="utf-8",
    )


def _state_window_pressure(ws):
    d = ws / ".ctx-session-reads" / "proxy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "window.json").write_text(
        json.dumps(
            {"window_pct": 95.0, "model": "claude-haiku-4-5",
             "context_limit": 200000, "last_input_tokens": 190000}
        ),
        encoding="utf-8",
    )


def _state_resolver_reader(ws):
    from ctx import resolver

    resolver.note_reader_drop(ws)
    resolver.note_reader_evidence(ws, followthrough=0.1, landings=0, confidence=0.9)


def _state_everything(ws):
    _state_densify_latched(ws)
    _state_window_pressure(ws)
    _state_resolver_reader(ws)
    # bypass rides in the same reflex blob as densify
    led = ws / ".ctx-session-reads"
    blob = json.loads((led / "reflex.json").read_text(encoding="utf-8"))
    blob["bypass"] = {"dangertool": True}
    (led / "reflex.json").write_text(json.dumps(blob), encoding="utf-8")


_STATES = {
    "empty": _state_empty,
    "reflex_densify_latched": _state_densify_latched,
    "reflex_bypass_latched": _state_bypass_latched,
    "window_pressure_95pct": _state_window_pressure,
    "resolver_reader_latched": _state_resolver_reader,
    "everything_at_once": _state_everything,
}


# ------------------------------------------------------ safety-class inputs
def _safety_inputs(ws, outside_file):
    return {
        "secret_path_read": ("Read", {"file_path": str(ws / ".env")}),
        "secret_key_read": ("Read", {"file_path": str(ws / "deploy" / "id_rsa")}),
        "workspace_escape_read": ("Read", {"file_path": str(outside_file)}),
        "ctx_toml_deny_command": (
            "run_command",
            {"CommandLine": "dangertool --all", "Cwd": str(ws)},
        ),
        "ctx_toml_deny_command_args": (
            "run_command",
            {"CommandLine": "dangertool run everything", "Cwd": str(ws)},
        ),
    }


@pytest.fixture()
def guard_ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ctx.toml").write_text(
        'version = 1\n[guard]\ndeny_commands = ["dangertool"]\n', encoding="utf-8"
    )
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("outside", encoding="utf-8")
    return ws, outside


@pytest.mark.parametrize("state_name", sorted(_STATES))
@pytest.mark.parametrize(
    "input_name",
    ["secret_path_read", "secret_key_read", "workspace_escape_read",
     "ctx_toml_deny_command", "ctx_toml_deny_command_args"],
)
def test_safety_decision_byte_identical_under_adaptive_state(
    guard_ws, state_name, input_name
):
    ws, outside = guard_ws
    tool_name, tool_input = _safety_inputs(ws, outside)[input_name]

    baseline = _classify(tool_name, tool_input, ws)
    baseline_bytes = json.dumps(baseline, sort_keys=True).encode("utf-8")
    # Sanity: these ARE safety-class denials, not allows.
    assert baseline["decision"] in ("deny", "force_ask")

    _STATES[state_name](ws)
    adapted = _classify(tool_name, tool_input, ws)
    adapted_bytes = json.dumps(adapted, sort_keys=True).encode("utf-8")

    assert adapted_bytes == baseline_bytes, (
        f"safety decision for {input_name} changed under {state_name}: "
        f"{baseline} -> {adapted}"
    )


def test_safety_decisions_stable_under_repeated_classification(guard_ws):
    """The classification itself mutates session counters (engagement,
    reflex sightings); safety decisions must not drift with them."""
    ws, outside = guard_ws
    _state_everything(ws)
    for tool_name, tool_input in _safety_inputs(ws, outside).values():
        first = _classify(tool_name, tool_input, ws)
        for _ in range(3):
            assert _classify(tool_name, tool_input, ws) == first


def test_secret_read_carries_no_window_note_under_pressure(guard_ws):
    """Usability-class read denials append the window note under pressure;
    a safety-class force_ask must never acquire one (it would prove the
    adaptive layer touched the reason bytes)."""
    ws, outside = guard_ws
    _state_window_pressure(ws)
    d = _classify("Read", {"file_path": str(ws / ".env")}, ws)
    assert d["decision"] == "force_ask"
    assert "window" not in d["reason"].lower()
    assert "tightened" not in d["reason"]


def test_resolver_never_imported_by_hook():
    """Structural half of the invariant: the safety plane (ctx.hook) has no
    import edge into the delivery resolver — plans cannot reach guard
    decisions even by accident."""
    from pathlib import Path

    import ctx.hook as hook_mod

    src = Path(hook_mod.__file__).read_text(encoding="utf-8")
    assert "from ctx import resolver" not in src
    assert "from ctx.resolver" not in src
    assert "import ctx.resolver" not in src


# ----------------------------------------------- floor<=ceiling load seam
def test_resolver_load_seam_rejects_floor_above_ceiling():
    from ctx import resolver

    with pytest.raises(ValueError):
        resolver.validate_rendering_policy(
            {"evidence_floor": 1000, "hard_ceiling": 999}
        )
    # Valid and partially-specified tables load silently.
    resolver.validate_rendering_policy({"evidence_floor": 100, "hard_ceiling": 100})
    resolver.validate_rendering_policy({"evidence_floor": 100})
    resolver.validate_rendering_policy({})


def test_contracts_loader_rejects_floor_above_ceiling_if_present():
    """The Evidence Contract loader (another engineer's deliverable) must
    enforce the same seam; skip gracefully until it ships."""
    contracts = pytest.importorskip("ctx.contracts")
    loader = None
    for name in ("load_contract", "load_contracts", "parse_contract"):
        loader = getattr(contracts, name, None)
        if loader is not None:
            break
    if loader is None:
        pytest.skip("ctx.contracts present but no recognizable loader yet")
    bad = (
        'schema = "ctx.contract/v1"\nfamily = "pytest"\n'
        "[rendering]\nevidence_floor = 1000\nhard_ceiling = 10\n"
    )
    with pytest.raises(Exception):
        loader(bad)
