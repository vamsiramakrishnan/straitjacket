"""logtemplate/v1 acceptance: detection, template mining, masking,
byte-identical determinism, compression, and registry precedence."""

import json
import sys

from conftest import make_store, make_ws


def _run(ws, store, argv, **kw):
    from ctx.execution import run_capture

    return run_capture(ws, argv, store=store, **kw)


def _run_text(ws, store, text: str):
    """Capture fixed stdout content via a file so argv stays clean."""
    fixture = ws.root / "_fixture.txt"
    fixture.write_text(text, encoding="utf-8")
    return _run(ws, store, [sys.executable, "-c", "import sys; sys.stdout.write(open('_fixture.txt').read())"])


# Reused from test_capture_and_determinism: pytest output must stay pytest/v1.
_PYTEST_FAKE = (
    "============================= test session starts ==============================\n"
    "collected 3 items\n\n"
    "test_a.py ..F\n\n"
    "=================================== FAILURES ===================================\n"
    "_____________________________ test_timeout _____________________________\n"
    "E   TimeoutError: risk-api deadline\n"
    "FAILED test_a.py::test_timeout - TimeoutError: risk-api deadline\n"
    "========================= 1 failed, 2 passed in 0.07s =========================\n"
)

_NEEDLE = (
    "2026-07-17 09:15:23 ERROR payment worker crashed: "
    "TimeoutError contacting risk-api req-99999"
)
_NEEDLE_LINE = 3211  # 1-based coordinate of the needle in the synthetic log


def _make_log(n: int = 5000) -> str:
    lines = []
    for i in range(n):
        if i == _NEEDLE_LINE - 1:
            lines.append(_NEEDLE)
        elif i % 3 == 0:
            lines.append(
                f"2026-07-17 09:15:{i % 60:02d} INFO request req-{10_000 + i} "
                f"completed status=200 in {i % 97}ms"
            )
        elif i % 3 == 1:
            lines.append(f"2026-07-17 09:15:{i % 60:02d} INFO cache hit key=user:{i} ttl={i % 300}s")
        else:
            lines.append(f"2026-07-17 09:15:{i % 60:02d} DEBUG heartbeat seq={i} node=10.0.0.{i % 255}")
    return "\n".join(lines) + "\n"


def _detect(store, ws, cap):
    from ctx.digest.base import DigestContext
    from ctx.digest.logprof import LogTemplateProfile

    ctx = DigestContext.load(store, ws, cap.manifest, focus=None)
    return LogTemplateProfile().detect(ctx)


# ------------------------------------------------------------------ detection
def test_detects_synthetic_log_and_reports_fraction(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run_text(ws, store, _make_log())
    reason = _detect(store, ws, cap)
    assert reason is not None
    assert "200/200" in reason and "100%" in reason
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "logtemplate/v1"
    assert "templates:" in digest and "coverage:" in digest


def test_needle_line_verbatim_with_coordinate(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run_text(ws, store, _make_log())
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert "exceptional:" in digest
    assert f"L{_NEEDLE_LINE}: {_NEEDLE}" in digest


def test_digest_byte_identical_across_runs(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = _make_log()
    d1, m1 = render_run_digest(store, ws, _run_text(ws, store, text).manifest)
    d2, m2 = render_run_digest(store, ws, _run_text(ws, store, text).manifest)
    assert d1 == d2
    assert m1["digest"]["bytesHash"] == m2["digest"]["bytesHash"]


# -------------------------------------------------------------------- masking
def test_mask_token_volatile_classes():
    from ctx.digest.logprof import _mask_token

    assert _mask_token("14237") == "<*>"
    assert _mask_token("4.5") == "<*>"
    assert _mask_token("deadbeef42cafe") == "<*>"
    assert _mask_token("f47ac10b-58cc-4372-a567-0e02b2c3d479") == "<*>"
    assert _mask_token("2026-07-17T12:34:56.789Z") == "<*>"
    assert _mask_token("[12:34:56]") == "[<*>]"
    assert _mask_token("123ms") == "<*>"
    assert _mask_token("4.5s") == "<*>"
    assert _mask_token("10.0.0.1:8080") == "<*>"
    assert _mask_token("req-14237") == "req-<*>"
    assert _mask_token("completed") == "completed"


def test_lines_differing_only_in_request_id_share_template(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = (
        "2026-07-17 09:00:00 INFO request req-14237 completed\n"
        "2026-07-17 09:00:01 INFO request req-99881 completed\n"
        + "".join(f"2026-07-17 09:00:{i % 60:02d} DEBUG heartbeat tick\n" for i in range(98))
    )
    cap = _run_text(ws, store, text)
    digest, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "logtemplate/v1"
    assert "2× L1: <*> <*> INFO request req-<*> completed" in digest


# ---------------------------------------------------------------- compression
def test_compression_below_five_percent(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = _make_log()
    cap = _run_text(ws, store, text)
    digest, _ = render_run_digest(store, ws, cap.manifest)
    raw = len(text.encode("utf-8"))
    assert len(digest.encode("utf-8")) < raw * 0.05


# ----------------------------------------------------------------- precedence
def test_does_not_fire_on_pytest_output(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = _run_text(ws, store, _PYTEST_FAKE)
    assert _detect(store, ws, cap) is None
    _, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "pytest/v1"


def test_does_not_fire_on_small_output(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "".join(f"2026-07-17 09:00:{i % 60:02d} INFO tiny burst {i}\n" for i in range(60))
    cap = _run_text(ws, store, text)
    assert _detect(store, ws, cap) is None
    _, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] != "logtemplate/v1"


def test_jsonl_stream_still_selects_jsonl(state_home, workspace_dir):
    from ctx.digest import render_run_digest

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    text = "".join(
        json.dumps({"level": "INFO", "msg": "request completed", "req": i}) + "\n"
        for i in range(200)
    )
    cap = _run_text(ws, store, text)
    _, m = render_run_digest(store, ws, cap.manifest)
    assert m["digest"]["profile"] == "jsonl/v1"
