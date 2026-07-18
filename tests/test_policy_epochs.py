"""Acceptance: learned policy epochs (run telemetry → committed policy).

``ctx.policy.compile_policy`` scans run manifests, promotes command
signatures observed >= min_runs times with p95 total stream bytes inside the
inline cap, and demotes any signature with a single observation > 4x the cap
(even when its p95 passes). ``write_policy`` renders a deterministic,
reviewable ``ctx-policy.toml``; the hook consumes it fail-open, treating
promoted signatures as allow prefixes while demoted signatures stay
governed.
"""

import json
import tomllib

from conftest import make_store, make_ws

CAP = 16384  # budgets.max_inline_bytes default
HUGE = 4 * CAP + 1


def _capture(ws, store, argv):
    from ctx.execution import run_capture

    return run_capture(ws, argv, store=store)


def _seed_run(store, ws, argv, nbytes, salt):
    """Publish a synthetic ctx.invocation/v1 manifest without spawning a
    process. ``salt`` varies the content so identical invocations do not
    dedupe to one content-addressed manifest."""
    manifest = {
        "schema": "ctx.invocation/v1",
        "workspaceId": ws.workspace_id,
        "cwd": ".",
        "argv": list(argv),
        "shell": False,
        "result": {"exitCode": 0, "signal": None, "timedOut": False},
        "streams": {
            "stdout": {
                "blob": "sha256:" + format(salt, "064x"),
                "bytes": nbytes,
                "lines": 1,
                "mediaType": "text/plain",
                "encoding": "utf-8",
            },
            "stderr": {
                "blob": "sha256:" + "0" * 64,
                "bytes": 0,
                "lines": 0,
                "mediaType": "text/plain",
                "encoding": "utf-8",
            },
        },
        "source": {"gitHead": None, "worktreeHash": None},
        "digest": {
            "profile": "text/v1",
            "policy": "default/v1",
            "focusHash": "sha256:" + "0" * 64,
            "bytesHash": "sha256:" + "0" * 64,
        },
    }
    return store.put_manifest(manifest, kind="run")


def _classify(tool_name, tool_input, workspace):
    from ctx.hook import classify

    return classify(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "workspacePaths": [str(workspace)],
        }
    )


def _cmd(command, workspace):
    return _classify(
        "run_command", {"CommandLine": command, "Cwd": str(workspace)}, workspace
    )


# ------------------------------------------------------------- compilation
def test_promotion_needs_min_runs(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    for i in range(6):  # trailing arg varies so manifests do not dedupe
        _capture(ws, store, ["echo", "alpha", str(i)])
    for i in range(2):
        _capture(ws, store, ["echo", "beta", str(i)])

    from ctx.policy import compile_policy

    policy = compile_policy(store, ws)
    assert policy["schema"] == "ctx.policy/v1"
    sigs = {e["signature"]: e for e in policy["promoted"]}
    assert set(sigs) == {"echo alpha"}  # beta seen only twice: not promoted
    assert sigs["echo alpha"]["runs"] == 6
    assert 0 < sigs["echo alpha"]["p95_bytes"] <= CAP
    assert policy["demoted"] == []


def test_single_flood_demotes_despite_small_p95(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    # 20 tiny observations + 1 flood: nearest-rank p95 over 21 values is the
    # 20th smallest (= 10 bytes, passes the cap), yet the one >4x-cap
    # observation demotes the signature.
    for i in range(20):
        _seed_run(store, ws, ["flooder", "run", str(i)], 10, salt=i)
    _seed_run(store, ws, ["flooder", "run", "boom"], HUGE, salt=999)

    from ctx.policy import _p95, compile_policy

    assert _p95([10] * 20 + [HUGE]) == 10  # p95 itself would have passed
    policy = compile_policy(store, ws)
    assert policy["demoted"] == ["flooder run"]
    assert all(e["signature"] != "flooder run" for e in policy["promoted"])


def test_max_p95_bytes_parameter_tightens_promotion(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    for i in range(6):
        _seed_run(store, ws, ["echo", "alpha", str(i)], 100, salt=i)

    from ctx.policy import compile_policy

    assert compile_policy(store, ws)["promoted"]  # default cap: promoted
    assert compile_policy(store, ws, max_p95_bytes=50)["promoted"] == []


def test_epoch_id_deterministic_across_compiles(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    for i in range(6):
        _seed_run(store, ws, ["sleeper", "x", str(i)], 40, salt=i)
    _seed_run(store, ws, ["flooder", "run"], HUGE, salt=777)

    from ctx.policy import compile_policy

    p1 = compile_policy(store, ws)
    p2 = compile_policy(store, ws)
    assert p1 == p2
    assert len(p1["epoch"]) == 12
    int(p1["epoch"], 16)  # hex content hash


def test_command_signature_shapes():
    from ctx.policy import command_signature

    assert command_signature(["git", "status", "--short"]) == "git status"
    assert command_signature(["pytest", "-q"]) == "pytest"
    assert command_signature(["python3", "script.py"]) == "python3"
    assert command_signature(["/usr/bin/cargo", "build"]) == "cargo build"
    assert command_signature([]) is None


# ------------------------------------------------- written TOML + hook loop
def test_written_toml_parses_and_hook_honors_epoch(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    for i in range(6):
        _seed_run(store, ws, ["sleeper", "x", str(i)], 40, salt=i)
    _seed_run(store, ws, ["flooder", "run"], HUGE, salt=777)

    from ctx.policy import compile_policy, write_policy

    # Steering deny keeps assertions crisp: no rewrite fields ride along.
    (workspace_dir / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    # Before the epoch exists, both unknown commands are governed.
    assert _cmd("sleeper x --now", workspace_dir)["decision"] == "force_ask"
    assert _cmd("flooder run --once", workspace_dir)["decision"] == "force_ask"

    policy = compile_policy(store, ws)
    path = write_policy(ws, policy)
    assert path == workspace_dir / "ctx-policy.toml"
    doc = tomllib.loads(path.read_text(encoding="utf-8"))  # must parse
    assert doc["schema"] == "ctx.policy/v1"
    assert doc["epoch"] == policy["epoch"]
    assert doc["demoted"] == ["flooder run"]
    assert [e["signature"] for e in doc["promoted"]] == ["sleeper x"]

    # Promoted signature now allows a matching unknown command outright...
    assert _cmd("sleeper x --now", workspace_dir) == {"decision": "allow"}
    # ...while the demoted signature stays governed.
    assert _cmd("flooder run --once", workspace_dir)["decision"] == "force_ask"


def test_belt_demoted_wins_over_promoted(workspace_dir):
    # A conflicting (hand-edited) epoch listing a signature on both sides:
    # demoted is checked first, so promotion never applies.
    (workspace_dir / "ctx-policy.toml").write_text(
        'schema = "ctx.policy/v1"\n'
        'epoch = "deadbeef0000"\n'
        'demoted = ["evil cmd"]\n\n'
        "[[promoted]]\n"
        'signature = "evil cmd"\n'
        "runs = 9\n"
        "p95_bytes = 5\n",
        encoding="utf-8",
    )
    d = _cmd("evil cmd --x", workspace_dir)
    assert d["decision"] == "force_ask"


def test_render_policy_is_deterministic_and_commented():
    from ctx.policy import render_policy

    policy = {
        "schema": "ctx.policy/v1",
        "epoch": "abc123def456",
        "promoted": [{"signature": "git status", "runs": 7, "p95_bytes": 512}],
        "demoted": ["flooder run"],
    }
    text = render_policy(policy)
    assert text == render_policy(json.loads(json.dumps(policy)))
    assert "compiled" in text.lower() and "commit" in text.lower()
    assert "epoch: abc123def456" in text
    parsed = tomllib.loads(text)
    assert parsed["promoted"][0]["signature"] == "git status"


# ---------------------------------------------------------------- fail-open
def test_corrupt_policy_toml_fails_open(workspace_dir):
    (workspace_dir / "ctx-policy.toml").write_text(
        "this is {{ not toml", encoding="utf-8"
    )
    # Built-in classification is untouched: pytest stays canonical deny...
    assert _cmd("pytest -q", workspace_dir)["decision"] == "deny"
    # ...unknown commands stay force_ask, and bounded ones stay exact allows.
    assert _cmd("sleeper x", workspace_dir)["decision"] == "force_ask"
    assert _cmd("echo hi", workspace_dir) == {"decision": "allow"}
