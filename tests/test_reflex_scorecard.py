"""Acceptance: axis-discovery instrument (REFLEX) — the scorecard's
behavioral-anomalies section and the slow-loop [digest_density] epoch table.

The scorecard folds ``.ctx-session-reads/reflex-outcomes.jsonl`` (and the
existing eval-adoption ledger) into an anomalies block that would have
caught the spec3 starvation loop from a single session; ``compile_policy``
aggregates the same ledger into a ``[digest_density]`` policy section that
hook.py's fail-open loader must tolerate unchanged.
"""

import json
import tomllib

from conftest import make_store, make_ws

# ---------------------------------------------------------------- fixtures


def _wire_record(seq, *, msgs=4, cre=400, read=50_000, inp=2, out=100):
    return {
        "seq": seq,
        "path": "/v1/messages",
        "status": 200,
        "messages": msgs,
        "model": "claude-haiku-4-5",
        "tools": {"Bash": 1},
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": cre,
        },
        "ms": {"connect": 0.0, "ttfb": 1000.0, "total": 2000.0},
    }


def _make_session(tmp_path, name="reads"):
    """A .ctx-session-reads-shaped dir: proxy/wire.jsonl inside, ledgers
    beside it. Returns (session_reads_dir, proxy_dir)."""
    reads = tmp_path / name
    proxy = reads / "proxy"
    proxy.mkdir(parents=True)
    records = [_wire_record(1), _wire_record(2, msgs=6, read=60_000)]
    (proxy / "wire.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )
    return reads, proxy


def _ev(event, signature, action="none", run=None, ts=1000.0):
    return {
        "ts": ts,
        "event": event,
        "signature": signature,
        "run": run,
        "action": action,
    }


def _seed_reflex(ledger_dir, events):
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "reflex-outcomes.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )


# ------------------------------------------------- scorecard anomalies (a)
def test_anomalies_block_counts_and_attribution(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    events = [_ev("starvation", "python -m pytest") for _ in range(7)]
    events.append(_ev("starvation", "python -m pytest", action="densify"))
    _seed_reflex(reads, events)

    sc = compute_scorecard(proxy)
    an = sc["anomalies"]
    assert an["starvation"] == 8
    assert an["starvation_signatures"] == {"python -m pytest": 8}
    assert an["landings"] == 0
    assert an["ratio"] == "8:0"
    assert an["densified"] == 1

    text = render_scorecard(sc)
    assert (
        "anomalies: 8 starvation (1 signature: 'python -m pytest') "
        "· 0 landings · densified: yes" in text
    )


def test_anomalies_multi_signature_and_friction(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    _seed_reflex(
        reads,
        [
            _ev("starvation", "pytest"),
            _ev("starvation", "pytest"),
            _ev("starvation", "go test"),
            _ev("landing", "cargo test"),
            _ev("friction", "rm"),
        ],
    )
    an = compute_scorecard(proxy)["anomalies"]
    assert an["starvation"] == 3
    assert an["starvation_signatures"] == {"go test": 1, "pytest": 2}
    assert an["landings"] == 1
    assert an["friction"] == 1
    assert an["densified"] == 0
    text = render_scorecard(compute_scorecard(proxy))
    assert "3 starvation (2 signatures: 'go test', 'pytest')" in text
    assert "1 friction" in text
    assert "densified: no" in text


def test_eval_adoption_counts_render(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard

    reads, proxy = _make_session(tmp_path)
    (reads / "eval-adoption.jsonl").write_text(
        "".join(
            json.dumps({"op": "eval_opportunity", "taught": t, "ts": 1.0}) + "\n"
            for t in (True, False, False)
        ),
        encoding="utf-8",
    )
    an = compute_scorecard(proxy)["anomalies"]
    assert an["eval_opportunities"] == 3
    assert an["eval_taught"] == 1
    assert "eval adoption: 3 opportunities · 1 taught" in render_scorecard(
        compute_scorecard(proxy)
    )


# ----------------------------------------------------- zero events → quiet (b)
def test_zero_events_no_block_byte_identical(tmp_path):
    from ctx.scorecard import compute_scorecard, render_scorecard, summary_line

    _, bare_proxy = _make_session(tmp_path, "bare")  # no ledgers at all
    reads, proxy = _make_session(tmp_path, "empty")
    (reads / "reflex-outcomes.jsonl").write_text("", encoding="utf-8")

    sc_bare = compute_scorecard(bare_proxy)
    sc_empty = compute_scorecard(proxy)
    assert "anomalies" not in sc_bare
    assert "anomalies" not in sc_empty
    # An empty ledger renders byte-identically to a ledger-less session:
    # the scorecard must not grow noise.
    assert render_scorecard(sc_empty) == render_scorecard(sc_bare)
    assert "anomalies" not in render_scorecard(sc_bare)
    assert "⚠" not in summary_line(sc_bare)


# ------------------------------------------------------------ corruption (c)
def test_corrupt_ledger_lines_skipped_no_crash(tmp_path):
    from ctx.scorecard import compute_scorecard

    reads, proxy = _make_session(tmp_path)
    (reads / "reflex-outcomes.jsonl").write_text(
        "this is {{ not json\n"
        + json.dumps(_ev("starvation", "pytest")) + "\n"
        + '"a bare string, not an object"\n'
        + json.dumps(_ev("starvation", "pytest", action="densify")) + "\n"
        + json.dumps({"event": "wormhole", "signature": "??"}) + "\n",
        encoding="utf-8",
    )
    an = compute_scorecard(proxy)["anomalies"]
    assert an["starvation"] == 2
    assert an["densified"] == 1


def test_wholly_corrupt_ledger_section_absent(tmp_path):
    from ctx.scorecard import compute_scorecard

    reads, proxy = _make_session(tmp_path)
    (reads / "reflex-outcomes.jsonl").write_bytes(b"\x00\xff garbage {{{")
    (reads / "eval-adoption.jsonl").write_text("also } not [ json\n")
    sc = compute_scorecard(proxy)
    assert sc is not None  # the wire scorecard still computes
    assert "anomalies" not in sc


# -------------------------------------------------------- summary flag (d)
def test_summary_flag_only_when_starvation(tmp_path):
    from ctx.scorecard import compute_scorecard, summary_line

    reads, proxy = _make_session(tmp_path)
    _seed_reflex(reads, [_ev("landing", "pytest"), _ev("landing", "go test")])
    sc = compute_scorecard(proxy)
    assert sc["anomalies"]["landings"] == 2  # section present...
    assert "⚠" not in summary_line(sc)  # ...but landings earn no warning

    _seed_reflex(
        reads, [_ev("starvation", "python -m pytest") for _ in range(8)]
    )
    sc = compute_scorecard(proxy)
    assert "· ⚠ 8 starvation/0 landings" in summary_line(sc)


# --------------------------------------------- slow loop: [digest_density] (e)
def _seed_run(store, ws, argv, nbytes, salt):
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


def test_compile_policy_digest_density_promotion(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_reflex(
        workspace_dir / ".ctx-session-reads",
        # promoted: repeated starvation, zero landings
        [_ev("starvation", "python -m pytest") for _ in range(3)]
        # omitted: landings >= starvations (the lean form is working)
        + [_ev("starvation", "go test")] * 2
        + [_ev("landing", "go test")] * 2
        # omitted: single starvation is below the 2-event threshold
        + [_ev("starvation", "cargo test")]
        # omitted: landings only never promote
        + [_ev("landing", "npm test")] * 3
        # friction events score guards, not density
        + [_ev("friction", "python -m pytest")],
    )
    from ctx.policy import compile_policy

    p1 = compile_policy(store, ws)
    p2 = compile_policy(store, ws)
    assert p1 == p2  # deterministic epoch across compiles of same inputs
    assert len(p1["epoch"]) == 12
    int(p1["epoch"], 16)
    assert p1["digest_density"] == {"python -m pytest": "dense"}


def test_digest_density_absent_without_ledger(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    from ctx.policy import compile_policy, render_policy

    policy = compile_policy(store, ws)
    assert "digest_density" not in policy
    assert "[digest_density]" not in render_policy(policy)


def test_digest_density_changes_epoch(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    from ctx.policy import compile_policy

    before = compile_policy(store, ws)["epoch"]
    _seed_reflex(
        workspace_dir / ".ctx-session-reads",
        [_ev("starvation", "python -m pytest")] * 2,
    )
    after = compile_policy(store, ws)["epoch"]
    assert before != after  # the density table is part of the content hash


# --------------------------------- rendered TOML through hook's parser path (f)
def test_rendered_density_parses_and_hook_sections_intact(
    state_home, workspace_dir
):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    for i in range(6):
        _seed_run(store, ws, ["sleeper", "x", str(i)], 40, salt=i)
    _seed_run(store, ws, ["flooder", "run"], 4 * 16384 + 1, salt=777)
    _seed_reflex(
        workspace_dir / ".ctx-session-reads",
        [_ev("starvation", "python -m pytest")] * 2,
    )
    from ctx.policy import compile_policy, render_policy, write_policy

    policy = compile_policy(store, ws)
    assert policy["digest_density"] == {"python -m pytest": "dense"}
    text = render_policy(policy)
    assert text == render_policy(json.loads(json.dumps(policy)))  # stable

    path = write_policy(ws, policy)
    # Same parse path hook.py uses (tomllib.loads, fail-open): the sections
    # its loader reads — schema, promoted, demoted — must be intact with
    # [digest_density] present in the file.
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    assert str(doc.get("schema", "")) == "ctx.policy/v1"
    promoted = [
        item.get("signature") if isinstance(item, dict) else item
        for item in doc.get("promoted") or []
    ]
    demoted = list(doc.get("demoted") or [])
    assert promoted == ["sleeper x"]
    assert demoted == ["flooder run"]
    assert doc["digest_density"] == {"python -m pytest": "dense"}


def test_hook_classify_tolerates_density_section(state_home, workspace_dir):
    """End-to-end: the live hook loader reads a ctx-policy.toml carrying
    [digest_density] and still honors promoted/demoted (fail-open proven
    against hook.py's own code, not a simulation)."""
    (workspace_dir / "ctx.toml").write_text(
        'version = 1\n[guard]\nsteering = "deny"\n', encoding="utf-8"
    )
    (workspace_dir / "ctx-policy.toml").write_text(
        'schema = "ctx.policy/v1"\n'
        'epoch = "deadbeef0000"\n'
        'demoted = ["flooder run"]\n\n'
        "[[promoted]]\n"
        'signature = "sleeper x"\n'
        "runs = 6\n"
        "p95_bytes = 40\n\n"
        "[digest_density]\n"
        '"python -m pytest" = "dense"\n',
        encoding="utf-8",
    )
    from ctx.hook import classify

    def _cmd(command):
        return classify(
            {
                "tool_name": "run_command",
                "tool_input": {"CommandLine": command, "Cwd": str(workspace_dir)},
                "workspacePaths": [str(workspace_dir)],
            }
        )

    assert _cmd("sleeper x --now") == {"decision": "allow"}
    assert _cmd("flooder run --once")["decision"] == "force_ask"
