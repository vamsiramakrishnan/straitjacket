from types import SimpleNamespace

from conftest import make_store, make_ws
from ctx.digest import digest_output, render_run_digest
from ctx.digest.base import StreamView
from ctx.digest.text import _focused_lines
from ctx.retrieval import Selector, get


def context(lines, terms, stderr=()):
    def stream(name, rows):
        text = "\n".join(rows)
        return StreamView(name, len(text.encode()), len(rows), "text/plain", text, True)
    return SimpleNamespace(stdout=stream("stdout", lines), stderr=stream("stderr", stderr),
                           focus_terms=terms)


def test_query_coverage_beats_keyword_repetition():
    ctx = context(["cache " * 100, "cache latency p95=400ms", "latency p50=10ms"],
                  ("cache", "latency"))
    selected, scanned, matches = _focused_lines(ctx)
    assert selected[0] == ("stdout", 2, "cache latency p95=400ms")
    assert (scanned, matches) == (3, 3)


def test_misleading_query_cannot_displace_failure_or_quiet_contradiction():
    rows = ["deployment healthy" for _ in range(20)]
    rows += ["deployment healthy=false", "HTTP/1.1 503 Service Unavailable"]
    selected, _, _ = _focused_lines(context(rows, ("deployment", "healthy=false")))
    assert ("stdout", 21, rows[20]) in selected
    assert ("stdout", 22, rows[21]) in selected
    assert len(selected) <= 4


def test_cross_stream_failure_and_terminal_signal_survive():
    ctx = context(["cache ready", "fatal: final failure"], ("cache",),
                  ["HTTP/1.1 502 Bad Gateway"])
    selected, _, _ = _focused_lines(ctx)
    assert {row[0] for row in selected} == {"stdout", "stderr"}
    assert any("final failure" in row[2] for row in selected)
    assert selected == _focused_lines(ctx)[0]


def test_focus_render_is_bounded_replayable_and_retrievable(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    rows = ["routine progress " * 20 for _ in range(300)]
    rows[150] = "cache latency p95=400ms"
    rows[250] = "HTTP/1.1 503 Service Unavailable"
    _, rid = digest_output(store, ws, "opaque_tool", "\n".join(rows))
    manifest = store.get_manifest(rid)
    baseline, _ = render_run_digest(store, ws, manifest)
    focused, _ = render_run_digest(store, ws, manifest, focus="cache latency")
    assert "cache latency p95=400ms" in focused
    assert "503 Service Unavailable" in focused
    assert "selection: focus/v2" in focused
    assert focused == render_run_digest(store, ws, manifest, focus="cache latency")[0]
    assert baseline == render_run_digest(store, ws, manifest)[0]
    assert len(focused.encode()) < 6000
    original = get(store, ws, f"run:{rid}#stdout", Selector(lines=(151, 151)))
    assert rows[150] in original
