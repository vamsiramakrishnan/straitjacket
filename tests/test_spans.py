"""v0.5 acceptance: deterministic zoom spans (SPEC §6.4 --span selector).

The contract that beats raw-refill retrieval: span tokens are minted at
omission points, content-derived (replayable), and resolution is bounded at
every level — a span can never re-flood the transcript.
"""

import re
import sys

import pytest

from conftest import make_store, make_ws


def _capture_log(ws, store, n=20000, needle_at=14237):
    from ctx.execution import run_capture

    lines = []
    for i in range(n):
        if i == needle_at:
            lines.append(f"ERROR worker-7 request req-{i} deadline exceeded")
        else:
            lines.append(f"INFO worker-{i % 16} request req-{i} completed")
    (ws.root / "log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run_capture(
        ws,
        [sys.executable, "-c", "import sys; sys.stdout.write(open('log.txt').read())"],
        store=store,
    )


def test_digest_emits_span_tokens_at_omission_points(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "logtemplate/v1"
    tokens = re.findall(r"· span ([0-9a-f]{10})", digest)
    assert tokens, "template lines must carry point-attached span tokens"


def test_span_ids_deterministic_across_redigest_and_stores(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.store import Store

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store)
    d1, _ = render_run_digest(store, ws, cap.manifest)
    d2, _ = render_run_digest(store, ws, cap.manifest)
    assert d1 == d2  # span tokens are content-derived, not random

    # A fresh store connection resolves the same token (persisted, no TTL).
    tok = re.search(r"· span ([0-9a-f]{10})", d1).group(1)
    fresh = Store(ws.workspace_id)
    span = fresh.get_span(tok)
    assert span["kind"] == "template"


def test_template_span_resolves_bounded_with_coordinates(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store)
    digest, m = render_run_digest(store, ws, cap.manifest)
    tok = re.search(r"· span ([0-9a-f]{10})", digest).group(1)
    short = str(m["id"]).removeprefix("sha256:")[:12]

    out = get(store, ws, f"run:{short}#stdout", Selector(span=tok))
    assert "occurrences (exact): 19,999" in out
    assert "shown: 20" in out
    assert out.count("\nL") <= 25  # bounded: never re-floods
    assert "L1:" in out  # exact artifact coordinates preserved


def test_region_span_small_returns_exact_lines(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store, n=500)
    blob = cap.manifest["streams"]["stdout"]["blob"]
    sid = store.register_span(blob, "region", a=10, b=14)
    short = cap.manifest_id[:12]
    out = get(store, ws, f"run:{short}#stdout", Selector(span=sid))
    assert "complete" in out
    assert "L10:" in out and "L14:" in out and "L15:" not in out


def test_region_span_large_zooms_never_floods(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store)
    blob = cap.manifest["streams"]["stdout"]["blob"]
    sid = store.register_span(blob, "region", a=1, b=20000)
    short = cap.manifest_id[:12]
    out = get(store, ws, f"run:{short}#stdout", Selector(span=sid))
    assert "zoom digest" in out
    assert "ERROR worker-7" in out  # the needle is exceptional inside the region
    # Recursive descent affordances present, output stays bounded.
    subs = re.findall(r"span ([0-9a-f]{10})", out)
    assert len(subs) >= 2
    assert len(out.encode()) <= ws.config.budgets.result_tokens * 4 + 400

    # Follow one sub-span: still bounded, still resolvable.
    out2 = get(store, ws, f"run:{short}#stdout", Selector(span=subs[-1]))
    assert len(out2.encode()) <= ws.config.budgets.result_tokens * 4 + 400


def test_unknown_span_is_actionable(state_home, workspace_dir):
    from ctx.retrieval import Selector, get
    from ctx.store import UnknownIdError

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _capture_log(ws, store, n=200)
    with pytest.raises(UnknownIdError, match="unknown span"):
        get(store, ws, f"run:{cap.manifest_id[:12]}#stdout", Selector(span="deadbeef00"))


def test_pytest_failure_carries_region_span(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    fake = (
        "============================= test session starts ==============================\n"
        "collected 2 items\n\ntest_a.py .F\n\n"
        "=================================== FAILURES ===================================\n"
        "_____________________________ test_boom _____________________________\n"
        "E   AssertionError: boom detail line\n"
        "FAILED test_a.py::test_boom - AssertionError\n"
        "========================= 1 failed, 1 passed in 0.02s =========================\n"
    )
    (workspace_dir / "fake.txt").write_text(fake, encoding="utf-8")
    cap = run_capture(
        ws, [sys.executable, "-c", "import sys; sys.stdout.write(open('fake.txt').read())"], store=store
    )
    digest, m = render_run_digest(store, ws, cap.manifest)
    tok_m = re.search(r"first failure stdout:L\d+-L\d+: \S+ · span ([0-9a-f]{10})", digest)
    assert tok_m, digest
    short = str(m["id"]).removeprefix("sha256:")[:12]
    out = get(store, ws, f"run:{short}#stdout", Selector(span=tok_m.group(1)))
    assert "boom detail line" in out
