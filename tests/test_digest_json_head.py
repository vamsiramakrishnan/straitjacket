"""Acceptance: JsonProfile head-N record inlining (json/v1).

A shape line alone forces a re-fetch to see any data; the head block inlines
the first records + a span to the rest, deterministically.
"""

import json


def _ctx_for(tmp_path, text, argv=("mcp__x__list",)):
    from ctx.digest.base import DigestContext, StreamView
    from ctx.workspace import resolve_workspace

    (tmp_path / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    ws = resolve_workspace(str(tmp_path))
    out = StreamView("stdout", len(text.encode()), 1, "application/json", text, True)
    err = StreamView("stderr", 0, 0, "text/plain", "", True)
    manifest = {
        "argv": list(argv), "cwd": ".", "shell": False,
        "result": {"exitCode": 0, "signal": None, "timedOut": False},
        "streams": {"stdout": {"blob": "sha256:x"}, "stderr": {"blob": "sha256:y"}},
    }
    return DigestContext(ws=ws, manifest=manifest, stdout=out, stderr=err)


def test_top_level_array_head(tmp_path):
    from ctx.digest.jsonprof import JsonProfile

    doc = [{"sha": f"c{i:03d}", "n": i, "nested": {"a": 1}} for i in range(60)]
    text = json.dumps(doc)
    p = JsonProfile()
    ctx = _ctx_for(tmp_path, text)
    assert p.detect(ctx)
    body = p.render(ctx)
    assert "records (head):" in body
    # exactly HEAD_RECORDS lines, each carrying the sha scalar; nested dropped
    assert body.count('"sha":"c') == 5
    # nested container excluded from the projected RECORD lines (it still
    # appears in the shape summary, which is fine).
    record_lines = [ln for ln in body.splitlines() if ln.startswith('  {"')]
    assert record_lines and all("nested" not in ln for ln in record_lines)
    assert "… +55 more records · ctx get run:PENDING#stdout --json-pointer /5" in body


def test_dict_wrapping_ptr_prefix(tmp_path):
    doc = {"total": 42, "items": [{"id": i} for i in range(10)]}
    body = _rendered(tmp_path, json.dumps(doc))
    assert "records (head):" in body
    assert "--json-pointer /items/5" in body  # ptr prefix is the list-valued key


def test_head_is_deterministic(tmp_path):
    from ctx.digest.jsonprof import JsonProfile

    text = json.dumps([{"b": 2, "a": 1, "z": i} for i in range(20)])
    p1, p2 = JsonProfile(), JsonProfile()
    c1, c2 = _ctx_for(tmp_path, text), _ctx_for(tmp_path, text)
    p1.detect(c1)
    p2.detect(c2)
    assert p1.render(c1) == p2.render(c2)  # byte-identical
    # sorted keys inside each projected record
    assert '{"a":1,"b":2,"z":0}' in p1.render(c1)


def test_no_head_when_no_array(tmp_path):
    body = _rendered(tmp_path, json.dumps({"a": 1, "b": {"c": 2}}))
    assert "records (head):" not in body  # a scalar/object doc has no page-able array


def _rendered(tmp_path, text):
    from ctx.digest.jsonprof import JsonProfile

    p = JsonProfile()
    ctx = _ctx_for(tmp_path, text)
    p.detect(ctx)
    return p.render(ctx)
