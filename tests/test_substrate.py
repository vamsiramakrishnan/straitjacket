"""Acceptance: M-K1 span-precise sites + per-result search provenance, and
M-K3 records algebra (docs/SUBSTRATE.md §4). The engine-parity extension
for columns lives beside the shipped rg/python parity test in
``test_v03_libraries.py``; this suite covers the typed surfaces."""

from __future__ import annotations

import json

import pytest

from conftest import make_store, make_ws


@pytest.fixture()
def qws(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    (workspace_dir / "src").mkdir()
    (workspace_dir / "src" / "app.py").write_text(
        "x = 1\ny = needle_here\nz = 3  # needle_here again\n", encoding="utf-8"
    )
    return ws, store


def run_q(qws, query):
    from ctx.query import run_query

    ws, store = qws
    return run_query(ws, store, query)


# ------------------------------------------------------------- M-K1: spans
def test_q_search_sites_carry_columns(qws):
    ws, store = qws
    out, code = run_q(qws, "search needle_here --glob 'src/*.py'")
    assert code == 0 and "rows (census): 2" in out
    # The minted result blob carries the span-precise rows.
    blob_short = out.splitlines()[0].split("blob:")[1].rstrip("]")
    payload = json.loads(store.get_blob(store.resolve_id(blob_short, kinds=("blob",))))
    rows = payload["rows"]
    assert rows[0]["line"] == 2 and rows[0]["col_a"] == 5
    text = (ws.root / "src" / "app.py").read_text(encoding="utf-8")
    line = text.splitlines()[rows[0]["line"] - 1]
    assert line[rows[0]["col_a"] - 1 : rows[0]["col_b"] - 1] == "needle_here"


def test_search_emission_mints_result_blob(qws):
    from ctx.retrieval import search

    ws, store = qws
    out = search(store, ws, "repo:", ["needle_here"])
    line = next(ln for ln in out.splitlines() if ln.startswith("result: blob:"))
    short = line.split("blob:")[1].strip()
    payload = json.loads(store.get_blob(store.resolve_id(short, kinds=("blob",))))
    assert payload["format"] == "ctx.search/v1"
    assert payload["total"] == 2
    site = payload["sites"][0]
    assert site["target"] == "src/app.py" and site["line"] == 2
    text = (ws.root / "src" / "app.py").read_text(encoding="utf-8")
    ln_text = text.splitlines()[site["line"] - 1]
    assert ln_text[site["col_a"] - 1 : site["col_b"] - 1] == "needle_here"


def test_search_blob_deterministic_across_calls(qws):
    from ctx.retrieval import search

    ws, store = qws
    out1 = search(store, ws, "repo:", ["needle_here"])
    out2 = search(store, ws, "repo:", ["needle_here"])
    blob1 = [ln for ln in out1.splitlines() if ln.startswith("result: blob:")]
    blob2 = [ln for ln in out2.splitlines() if ln.startswith("result: blob:")]
    assert blob1 == blob2


# ----------------------------------------------------------- M-K3: records
@pytest.fixture()
def records_blob(qws):
    ws, store = qws
    doc = {
        "results": [
            {"level": "ERROR", "ms": 40, "rule": "r1"},
            {"level": "WARN", "ms": 10, "rule": "r2"},
            {"level": "ERROR", "ms": 30, "rule": "r1"},
        ]
    }
    blob_id = store.put_blob(json.dumps(doc).encode("utf-8"))
    return f"blob:{blob_id[:12]}"


def test_records_source_with_pointer_where_group_count(qws, records_blob):
    out, code = run_q(
        qws,
        f"records {records_blob} --pointer /results | group level | count",
    )
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.startswith("group=")]
    assert lines == ["group=ERROR · n=2", "group=WARN · n=1"]


def test_records_jsonl_and_wrapped_scalars(qws):
    ws, store = qws
    text = '{"a": 1}\n\n"loose"\n{"a": 2}\n'
    blob_id = store.put_blob(text.encode("utf-8"))
    out, code = run_q(qws, f"records blob:{blob_id[:12]} --jsonl | count")
    assert code == 0 and "n=3" in out


def test_records_run_stream(qws):
    from ctx.execution import run_capture

    ws, store = qws
    cap = run_capture(
        ws,
        ['printf \'{"ok": true}\\n{"ok": false}\\n\''],
        shell=True,
        timeout=30,
        store=store,
    )
    short = str(cap.manifest["id"]).removeprefix("sha256:")[:12]
    out, code = run_q(qws, f"records run:{short}#stdout --jsonl | count")
    assert code == 0 and "n=2" in out


def test_records_teaches_on_non_json(qws):
    ws, store = qws
    blob_id = store.put_blob(b"plain text, not json")
    out, code = run_q(qws, f"records blob:{blob_id[:12]}")
    assert code == 2 and "--jsonl" in out


def test_records_bad_pointer_teaches(qws, records_blob):
    out, code = run_q(qws, f"records {records_blob} --pointer /nope")
    assert code == 2 and "--pointer" in out


# ----------------------------------------------- M-K3: distinct / histogram
def test_distinct_unique_sorted(qws, records_blob):
    out, code = run_q(qws, f"records {records_blob} --pointer /results | distinct level")
    assert code == 0
    recs = [ln for ln in out.splitlines() if ln.startswith("level=")]
    assert recs == ["level=ERROR", "level=WARN"]


def test_histogram_categorical(qws, records_blob):
    out, code = run_q(qws, f"records {records_blob} --pointer /results | histogram rule")
    assert code == 0
    recs = [ln for ln in out.splitlines() if ln.startswith("bucket=")]
    assert recs == ["bucket=r1 · n=2", "bucket=r2 · n=1"]


def test_histogram_numeric_buckets(qws, records_blob):
    out, code = run_q(
        qws,
        f"records {records_blob} --pointer /results | histogram ms --buckets 3",
    )
    assert code == 0
    recs = [ln for ln in out.splitlines() if ln.startswith("bucket=")]
    assert recs == ["bucket=10–20 · n=1", "bucket=20–30 · n=0", "bucket=30–40 · n=2"]


def test_histogram_over_sites(qws):
    out, code = run_q(qws, "search needle_here --glob 'src/*.py' | histogram file")
    assert code == 0 and "bucket=src/app.py · n=2" in out


def test_distinct_rejects_text_kind(qws):
    _, code = run_q(qws, "search needle_here | get | distinct file")
    assert code == 2  # text is terminal; distinct is representation-only


def test_totality_records_stages_are_closed(qws):
    from ctx.query import STAGES

    assert STAGES["distinct"].closure == "closed"
    assert STAGES["histogram"].closure == "closed"
    assert STAGES["corpus"].closure == "source"
    assert STAGES["records"].closure == "source"


# ------------------------------------- pytest detection is word-anchored
def test_pytest_detection_is_word_anchored():
    """A command that merely MENTIONS pytest's name — an interpreter under
    a pytest-named directory (uv tool shims), a /tmp/pytest-of-root
    fixture path — is not a test run. The word must be a program basename
    or module target."""
    from ctx.digest.pytestprof import argv_invokes_pytest

    invokes = [
        ["pytest", "-q"],
        ["/usr/bin/pytest", "tests"],
        ["python3", "-m", "pytest", "-q"],
        ["bash", "-c", "python -m pytest -q | tail -5"],
        ["py.test", "tests/"],
        ["/x/tools/pytest/bin/python", "-m", "pytest"],  # module target wins
    ]
    for argv in invokes:
        assert argv_invokes_pytest(argv), argv
    mentions_only = [
        ["/root/.local/share/uv/tools/pytest/bin/python", "-c", "print(1)"],
        ["cat", "/tmp/pytest-of-root/pytest-1/out.txt"],
        ["python3", "-c", "print('v 1.2.3')"],
    ]
    for argv in mentions_only:
        assert not argv_invokes_pytest(argv), argv
