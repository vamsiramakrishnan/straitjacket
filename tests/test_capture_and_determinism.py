"""Acceptance: capture fidelity and byte-identical digest determinism."""

import json
import sys

from conftest import make_store, make_ws


def _run(ws, store, argv, **kw):
    from ctx.execution import run_capture

    return run_capture(ws, argv, store=store, **kw)


def test_stdout_stderr_separated(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "import sys; sys.stdout.write('to-out\\n'); sys.stderr.write('to-err\\n'); sys.exit(3)"
    cap = _run(ws, store, [sys.executable, "-c", script])
    m = cap.manifest
    assert m["result"]["exitCode"] == 3
    out = store.get_blob(m["streams"]["stdout"]["blob"].removeprefix("sha256:"))
    err = store.get_blob(m["streams"]["stderr"]["blob"].removeprefix("sha256:"))
    assert out == b"to-out\n"
    assert err == b"to-err\n"


def test_binary_and_invalid_utf8_preserved(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "import sys; sys.stdout.buffer.write(b'\\x00\\xff\\xfebinary')"
    cap = _run(ws, store, [sys.executable, "-c", script])
    meta = cap.manifest["streams"]["stdout"]
    assert meta["mediaType"] == "application/octet-stream"
    raw = store.get_blob(meta["blob"].removeprefix("sha256:"))
    assert raw == b"\x00\xff\xfebinary"


def test_manifest_matches_schema_shape(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "print('x')"])
    m = cap.manifest
    for key in ("schema", "id", "workspaceId", "cwd", "argv", "shell", "result", "streams", "digest"):
        assert key in m, key
    assert m["schema"] == "ctx.invocation/v1"
    assert m["id"].startswith("sha256:")
    assert set(m["streams"]) == {"stdout", "stderr"}


def test_digest_byte_identical_across_replays(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "print('alpha'); print('ERROR: broke a thing')"
    d1, m1 = render_run_digest(store, ws, _run(ws, store, [sys.executable, "-c", script]).manifest)
    d2, m2 = render_run_digest(store, ws, _run(ws, store, [sys.executable, "-c", script]).manifest)
    assert d1 == d2
    assert m1["digest"]["bytesHash"] == m2["digest"]["bytesHash"]


def test_digest_contains_no_absolute_paths_or_store_paths(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "print('ok')"])
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert str(workspace_dir) not in digest
    assert str(state_home) not in digest
    assert digest.startswith("[ctx run:")


def test_ansi_stripped_from_digest(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = r"print('\x1b[31mERROR red text\x1b[0m')"
    cap = _run(ws, store, [sys.executable, "-c", script])
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert "\x1b" not in digest
    assert "ERROR red text" in digest


def test_focus_changes_identity_not_raw_artifact(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "print('needle in line one'); print('hay')"
    cap1 = _run(ws, store, [sys.executable, "-c", script])
    cap2 = _run(ws, store, [sys.executable, "-c", script])
    _, m_plain = render_run_digest(store, ws, cap1.manifest)
    d_focus, m_focus = render_run_digest(store, ws, cap2.manifest, focus="needle")
    assert m_plain["digest"]["focusHash"] != m_focus["digest"]["focusHash"]
    assert "needle" in d_focus
    # Raw stream blobs identical regardless of focus.
    assert m_plain["streams"]["stdout"]["blob"] == m_focus["streams"]["stdout"]["blob"]


def test_pytest_profile_detected_and_time_free(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "============================= test session starts ==============================\n"
        "collected 3 items\n\n"
        "test_a.py ..F\n\n"
        "=================================== FAILURES ===================================\n"
        "_____________________________ test_timeout _____________________________\n"
        "E   TimeoutError: risk-api deadline\n"
        "FAILED test_a.py::test_timeout - TimeoutError: risk-api deadline\n"
        "========================= 1 failed, 2 passed in 0.07s =========================\n"
    )
    # Write the fixture to a file so argv (echoed in the digest header)
    # stays clean — the timing string must be stripped from the summary.
    fixture = workspace_dir / "fake-pytest.txt"
    fixture.write_text(fake, encoding="utf-8")
    script = "import sys; sys.stdout.write(open('fake-pytest.txt').read())"
    cap = _run(ws, store, [sys.executable, "-c", script])
    digest, m = render_run_digest(store, ws, cap.manifest)
    # Failing fixture → the pytest/v2 evidence render (pass paths stay v1).
    assert m["digest"]["profile"] == "pytest/v2"
    assert "passed 2" in digest and "failed 1" in digest
    assert "0.07s" not in digest  # no timing noise in stable digests
    assert "TimeoutError" in digest


def test_json_profile(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    script = "import json; print(json.dumps({'items': [1, 2, 3], 'ok': True}))"
    cap = _run(ws, store, [sys.executable, "-c", script])
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "json/v1"
    assert "shape (exact)" in digest


def test_atomic_manifests_are_valid_json(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run(ws, store, [sys.executable, "-c", "print('x')"])
    path = store.manifest_dir / (cap.manifest_id + ".json")
    json.loads(path.read_text(encoding="utf-8"))
    assert not list(store.manifest_dir.glob(".tmp-*"))
