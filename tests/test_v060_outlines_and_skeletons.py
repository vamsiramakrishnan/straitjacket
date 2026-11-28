"""Tests for v0.6.0: stdlib regex code skeletons, outside-root code outlines,
structured plain-text PreToolUse outlines, and PostToolUse line-number wrapper
stripping."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import make_store, make_ws


def _disable_external_skeleton_backends(monkeypatch) -> None:
    import ctx.skeleton as skel

    def raiser(*a, **k):
        raise skel.BackendUnavailable("disabled")

    monkeypatch.setattr(skel, "_tree_sitter_extract", raiser)
    monkeypatch.setattr(skel, "_ctags_path", lambda: None)


# ------------------------------------------------------------------ skeletons
def test_stdlib_regex_skeleton_go_structs_methods_consts(state_home, workspace_dir, monkeypatch):
    from ctx.skeleton import skeleton_for, skeleton_outline

    _disable_external_skeleton_backends(monkeypatch)
    src = (
        'package client\n\n'
        'import (\n'
        '\t"context"\n'
        '\t"net/http"\n'
        ')\n\n'
        'const (\n'
        '\tdefaultProjectNumber = uint64(913411032810)\n'
        '\terrUnattended = "blocked"\n'
        ')\n\n'
        'type quotaRoundTripper struct {\n'
        '\tbase http.RoundTripper\n'
        '}\n\n'
        'func (q *quotaRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {\n'
        '\treturn q.base.RoundTrip(req)\n'
        '}\n\n'
        'func checkUnattended() error {\n'
        '\treturn nil\n'
        '}\n'
    )
    (workspace_dir / "client.go").write_text(src, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    sk = skeleton_for(store, ws, "client.go")
    assert sk["parser"] == "regex"
    assert sk["language"] == "go"
    assert sk["imports"] == ["context", "net/http"]
    by_name = {s["name"]: s for s in sk["symbols"]}
    assert by_name["defaultProjectNumber"]["kind"] == "constant"
    assert by_name["errUnattended"]["kind"] == "constant"
    assert by_name["quotaRoundTripper"]["kind"] == "struct"
    assert by_name["RoundTrip"]["kind"] == "method"
    assert by_name["RoundTrip"]["scope"] == "quotaRoundTripper"
    assert by_name["checkUnattended"]["kind"] == "function"
    assert by_name["RoundTrip"]["range"][1] > by_name["RoundTrip"]["range"][0]
    outline = skeleton_outline(sk, 1200)
    assert "parser regex" in outline
    assert "quotaRoundTripper" in outline and "checkUnattended" in outline


@pytest.mark.parametrize(
    "filename,source,expected_names",
    [
        (
            "service.ts",
            'import { a } from "./a";\n'
            'export interface Cfg { port: number; }\n'
            'export class Svc {\n  run(x: number): number {\n    return x;\n  }\n}\n'
            'export function start(): void {}\n'
            'export const helper = (v: number) => v + 1;\n',
            {"Cfg", "Svc", "run", "start", "helper"},
        ),
        (
            "lib.rs",
            'use std::io;\n'
            'pub struct Engine { pub id: u32 }\n'
            'pub trait Runner { fn step(&self); }\n'
            'impl Runner for Engine {\n  fn step(&self) {}\n}\n'
            'pub fn boot() {}\n',
            {"Engine", "Runner", "step", "boot"},
        ),
        (
            "App.java",
            'import java.util.List;\n'
            'public class App {\n  public int compute(int x) {\n    return x * 2;\n  }\n}\n'
            'interface Service {}\n',
            {"App", "compute", "Service"},
        ),
        (
            "engine.cc",
            '#include <vector>\n'
            'namespace core {\nclass Engine {\npublic:\n  int run() { return 0; }\n};\n}\n'
            'int helper(int a) {\n  return a;\n}\n',
            {"Engine", "helper"},
        ),
        (
            "tool.sh",
            '#!/usr/bin/env bash\nsource common.sh\nbuild_all() {\n  echo ok\n}\n',
            {"build_all"},
        ),
    ],
)
def test_stdlib_regex_skeleton_multilanguage(
    state_home, workspace_dir, monkeypatch, filename, source, expected_names
):
    from ctx.skeleton import skeleton_for

    _disable_external_skeleton_backends(monkeypatch)
    (workspace_dir / filename).write_text(source, encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    sk = skeleton_for(store, ws, filename)
    assert sk["parser"] == "regex"
    found = {s["name"] for s in sk["symbols"]}
    assert expected_names <= found, f"{filename}: missing {expected_names - found}; got {found}"


def test_outside_root_code_file_gets_pretooluse_outline_without_path_or_secret_leak(
    tmp_path, monkeypatch
):
    from ctx import hook

    _disable_external_skeleton_backends(monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "trusted_head"
    outside.mkdir()
    secret_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAtokenSUPERSECRETBODY1234567890\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    go_file = outside / "big_client.go"
    body = (
        "package big\n\n"
        "/*\n" + secret_key + "*/\n"
        "const defaultQuota = 42\n"
        + "\n".join(
            f"func Handler{i}(x int) int {{\n\t// {'padding ' * 40}\n\treturn x + {i}\n}}"
            for i in range(60)
        )
    )
    go_file.write_text(body, encoding="utf-8")
    assert go_file.stat().st_size > 16384

    monkeypatch.setenv("CTX_ALLOW_OUTSIDE_READ_PREFIXES", str(outside))
    policy = hook._load_guard_policy(str(ws))
    dec = hook.classify_read(
        str(go_file),
        str(ws),
        policy,
        tool_input={"Path": str(go_file)},
    )
    assert dec["decision"] == "deny"
    reason = dec["reason"]
    assert "[ctx skeleton big_client.go]" in reason
    assert "Handler0" in reason and "defaultQuota" in reason
    assert str(outside) not in reason  # no absolute path leak
    assert "SUPERSECRETBODY" not in reason  # pre-slice redaction


# --------------------------------------------------- structured text outlines
def test_textoutline_delimited_sections(tmp_path):
    from ctx.config import Redaction
    from ctx.digest.textoutline import outline_file

    f = tmp_path / "deck.txt"
    blocks = [
        f"--- Slide {i} (slide_id_{i}) ---\nTitle {i}\n" + ("bullet detail line\n" * 18)
        for i in range(1, 53)
    ]
    f.write_text("\n".join(blocks), encoding="utf-8")
    out = outline_file(str(f), Redaction())
    assert out is not None
    assert "sectioned text: 52 sections" in out
    assert "Slide 1" in out and "Slide 52" in out


def test_textoutline_kv_record_stream_and_redaction(tmp_path):
    from ctx.config import Redaction
    from ctx.digest.textoutline import outline_file

    f = tmp_path / "issues.txt"
    recs = ["Showing first 30 issue(s) matching query:"]
    for i in range(1, 31):
        recs.append(
            f"Issue ID: {590000 + i}\n"
            f"Status: {'ASSIGNED' if i % 2 else 'NEW'}\n"
            f"Priority: P2\n"
            f"Title: Fix subsystem {i} with ghp_1234567890abcdefghijklmnopqrstuvwx12\n\n---\n"
        )
    f.write_text("\n".join(recs), encoding="utf-8")
    out = outline_file(str(f), Redaction())
    assert out is not None
    assert "record stream: 30 records" in out
    assert "Showing first 30 issue(s)" in out
    assert "590001" in out and "590030" in out
    assert "ghp_1234567890" not in out


def test_textoutline_timestamped_thread(tmp_path):
    from ctx.config import Redaction
    from ctx.digest.textoutline import outline_file

    f = tmp_path / "thread.txt"
    msgs = [
        f"**alice** at 2026-11-27T10:{i:02d}:00Z [id: m{i}]:\n"
        f"  Message body {i} discussing release plan.\n"
        + ("  extra context line\n" * 10)
        for i in range(1, 25)
    ]
    f.write_text("\n".join(msgs), encoding="utf-8")
    out = outline_file(str(f), Redaction())
    assert out is not None
    assert "message thread: 24 messages" in out
    assert "2026-11-27T10:01:00Z" in out and "2026-11-27T10:24:00Z" in out
    assert "alice" in out


def test_textoutline_aligned_table(tmp_path):
    from ctx.config import Redaction
    from ctx.digest.textoutline import outline_file

    f = tmp_path / "events.txt"
    rows = [f"{'ID':<14}  {'SUMMARY':<30}  {'START':<26}  {'STATUS':<10}"]
    for i in range(1, 60):
        rows.append(
            f"{f'evt_{i:04d}':<14}  {f'Weekly Sync {i}':<30}  {f'2026-11-{(i % 28) + 1:02d}T10:00:00Z':<26}  {'confirmed':<10}"
        )
    f.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out = outline_file(str(f), Redaction())
    assert out is not None
    assert "aligned table: 59 rows × 4 columns" in out
    assert "Weekly Sync 1" in out and "Weekly Sync 59" in out

    # Also verify underlined mixed-case CLI tables (tabwriter / tabulate shape).
    uf = tmp_path / "events_underlined.txt"
    urows = [
        f"{'ID':<14}  {'Summary':<30}  {'Start':<26}  {'Visibility':<10}",
        f"{'-' * 14}  {'-' * 30}  {'-' * 26}  {'-' * 10}",
    ]
    for i in range(1, 35):
        urows.append(
            f"{f'evt_{i:04d}':<14}  {f'Team Sync {i}':<30}  {f'2026-11-{(i % 28) + 1:02d}T09:00:00Z':<26}  {'public':<10}"
        )
    uf.write_text("\n".join(urows) + "\n", encoding="utf-8")
    uout = outline_file(str(uf), Redaction())
    assert uout is not None
    assert "aligned table: 34 rows × 4 columns" in uout
    assert "Team Sync 1" in uout and "Team Sync 34" in uout


def test_textoutline_declines_unstructured_prose_and_logs(tmp_path):
    from ctx.config import Redaction
    from ctx.digest.textoutline import outline_file

    prose = tmp_path / "plain.txt"
    prose.write_text("".join(f"ordinary prose line {i} with no sections\n" for i in range(500)))
    assert outline_file(str(prose), Redaction()) is None

    log = tmp_path / "app.txt"
    log.write_text("".join(f"{i:06d} INFO worker ok\n" for i in range(500)))
    assert outline_file(str(log), Redaction()) is None


def test_pretooluse_routes_large_structured_txt_to_outline_with_search_hint(tmp_path):
    from ctx import hook

    f = tmp_path / "slides_export.txt"
    blocks = [
        f"--- Slide {i} (s{i}) ---\nAgenda item {i}\n" + ("word " * 80 + "\n")
        for i in range(1, 55)
    ]
    f.write_text("\n".join(blocks), encoding="utf-8")
    policy = hook._load_guard_policy(str(tmp_path))
    dec = hook.classify_read(
        str(f), str(tmp_path), policy, tool_input={"Path": str(f)}
    )
    assert dec["decision"] == "deny"
    assert "sectioned text: 54 sections" in dec["reason"]
    assert "ctx search repo:slides_export.txt" in dec["reason"]


# ----------------------------------------- PostToolUse line-number stripping
def test_strip_line_number_wrapper_preserves_exact_line_count():
    from ctx.digest import strip_line_number_wrapper

    raw = "Showing lines 1-12 of 500\n" + "\n".join(
        f"{i}: line content {i}" for i in range(1, 13)
    ) + "\n"
    cleaned = strip_line_number_wrapper(raw)
    assert cleaned is not None
    assert cleaned.count("\n") == raw.count("\n")
    lines = cleaned.splitlines()
    assert lines[0] == ""
    assert lines[1] == "line content 1"
    assert lines[12] == "line content 12"


def test_strip_line_number_wrapper_declines_non_sequential_lists():
    from ctx.digest import strip_line_number_wrapper

    yaml_ports = "\n".join(f"{8000 + i * 10}: service_{i}" for i in range(20)) + "\n"
    assert strip_line_number_wrapper(yaml_ports) is None


def test_posttooluse_numbered_markdown_and_csv_unlock_structured_profiles(
    state_home, workspace_dir
):
    from ctx.digest import digest_output
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)

    # 1. Line-numbered Markdown with Showing lines banner + YAML frontmatter
    md_lines = [
        "---",
        "title: Architecture RFC",
        "owner: platform",
        "---",
        "# Architecture RFC",
        "",
    ]
    for sec in range(1, 12):
        md_lines.append(f"## Section {sec}: Subsystem {sec}")
        md_lines.extend([f"- Detail bullet {j} for subsystem {sec}." + (" filler" * 18) for j in range(12)])
    numbered_md = (
        f"Showing lines 1-{len(md_lines)} of {len(md_lines)}\n"
        + "\n".join(f"{i}: {ln}" for i, ln in enumerate(md_lines, 1))
        + "\n"
    )
    digest, short = digest_output(store, ws, "read_file", numbered_md)
    assert "profile=markdown/v1" in digest
    assert 'title="Architecture RFC"' in digest
    # Line coordinates in digest must address the exact line in run:<short>#stdout!
    assert "L6-" in digest  # `# Architecture RFC` is raw line 5 -> blob line 6
    fetched = get(store, ws, f"run:{short}#stdout", Selector(lines=(6, 6)))
    assert "# Architecture RFC" in fetched

    # 2. Arrow-numbered CSV (Claude Code Read style: `     1→col1,col2,...`)
    csv_rows = ["id,region,amount,status"] + [
        f"{i},us-east-{i % 4},{i * 100},{'OK' if i % 3 else 'WARN'}"
        + "," + ("note" * 15)
        for i in range(1, 300)
    ]
    csv_rows[0] = "id,region,amount,status,notes"
    numbered_csv = "\n".join(f"{i:6d}→{row}" for i, row in enumerate(csv_rows, 1)) + "\n"
    csv_digest, csv_short = digest_output(store, ws, "Read", numbered_csv)
    assert "profile=delimited/v1" in csv_digest
    assert "299 data rows" in csv_digest
    fetched_hdr = get(store, ws, f"run:{csv_short}#stdout", Selector(lines=(1, 1)))
    assert "id,region,amount,status,notes" in fetched_hdr


def test_regex_skeleton_multiline_raw_string_and_block_comment_braces(monkeypatch):
    from ctx import skeleton

    _disable_external_skeleton_backends(monkeypatch)
    monkeypatch.delenv("CTX_NO_REGEX_SKELETON", raising=False)

    # Go function with a multiline raw backtick string containing `}` and a
    # fake top-level `func` inside it, plus a multiline block comment with `}`.
    go_src = "\n".join([
        "package demo",
        "",
        "func RenderTemplate() string {",
        "\t/* comment with brace }",
        "\tfunc FakeInComment() {}",
        "\t*/",
        "\ttpl := `hello {",
        "} closing brace inside raw string",
        "func FakeInRawString() {}",
        "`",
        "\treturn tpl",
        "}",
        "",
        "func AfterTemplate() int {",
        "\treturn 42",
        "}",
    ])
    syms, _, backend = skeleton._extract(go_src, "go", "demo.go")
    assert backend == "regex"
    by_name = {s["name"]: s for s in syms}
    assert "FakeInComment" not in by_name
    assert "FakeInRawString" not in by_name
    assert by_name["RenderTemplate"]["range"] == [3, 12]
    assert by_name["AfterTemplate"]["range"] == [14, 16]

    # Rust function with lifetime `'a` and nested block comments `/* /* } */ */`.
    rust_src = "\n".join([
        "pub fn parse<'a>(input: &'a str) -> &'a str {",
        "    /* outer /* nested } */ still comment } */",
        "    let c = '}';",
        "    input",
        "}",
    ])
    rsyms, _, rbackend = skeleton._extract(rust_src, "rust", "demo.rs")
    assert rbackend == "regex"
    assert rsyms[0]["name"] == "parse"
    assert rsyms[0]["range"] == [1, 5]

    # TypeScript function with inline object return type `{value: number} {`
    # plus semicolon-free expression-bodied arrow followed by another function.
    ts_src = "\n".join([
        "export function make({seed}: {seed: number}): {value: number} {",
        "  const v = seed + 1;",
        "  return { value: v };",
        "}",
        "",
        "export const inc = (x: number) => x + 1",
        "export function run() {",
        "  return inc(1);",
        "}",
        "",
        "export const multi = (x: number) => (",
        "  x * 2",
        ")",
        "export const blockArrow = (x: number) => {",
        "  return x * 3;",
        "}",
    ])
    tsyms, _, tbackend = skeleton._extract(ts_src, "typescript", "demo.ts")
    assert tbackend == "regex"
    tmap = {s["name"]: s for s in tsyms}
    assert tmap["make"]["range"] == [1, 4]
    assert tmap["inc"]["range"] == [6, 6]
    assert tmap["run"]["range"] == [7, 9]
    assert tmap["multi"]["range"] == [11, 13]
    assert tmap["blockArrow"]["range"] == [14, 16]

    # Bodyless Go type alias and TS type alias followed by functions must not
    # borrow the following function's braces.
    go_alias = "\n".join([
        "package x",
        "type ID string",
        "func Run() {",
        "\tprintln(1)",
        "}",
    ])
    gasyms, _, _ = skeleton._extract(go_alias, "go", "alias.go")
    gamap = {s["name"]: s for s in gasyms}
    assert gamap["ID"]["range"] == [2, 2]
    assert gamap["Run"]["range"] == [3, 5]

    ts_alias = "\n".join([
        "export type ID = string",
        "export function run() {",
        "  return 1;",
        "}",
    ])
    tasyms, _, _ = skeleton._extract(ts_alias, "typescript", "alias.ts")
    tamap = {s["name"]: s for s in tasyms}
    assert tamap["ID"]["range"] == [1, 1]
    assert tamap["run"]["range"] == [2, 4]

    # Multiline Go signature with >4 parameter lines + JS regex literal with `}`.
    go_multi = "\n".join([
        "package demo",
        "func BigSig(",
        "\ta int,",
        "\tb int,",
        "\tc int,",
        "\td int,",
        "\te int,",
        "\tf int,",
        ") int {",
        "\treturn a + b + c + d + e + f",
        "}",
    ])
    gmsyms, _, _ = skeleton._extract(go_multi, "go", "multi.go")
    assert gmsyms[0]["name"] == "BigSig"
    assert gmsyms[0]["range"] == [2, 11]

    js_re = "\n".join([
        "export function hasClosingBrace(s) {",
        "  const re = /}/;",
        "  const re2 = /[}]/g;",
        "  return re.test(s) || re2.test(s);",
        "}",
    ])
    jrsyms, _, _ = skeleton._extract(js_re, "javascript", "re.js")
    assert jrsyms[0]["name"] == "hasClosingBrace"
    assert jrsyms[0]["range"] == [1, 5]


def test_posttooluse_numbered_json_jsonl_and_notebook_structured_retrieval(
    state_home, workspace_dir
):
    import json
    from ctx.digest import digest_output
    from ctx.retrieval import Selector, get

    ws = make_ws(workspace_dir)
    store = make_store(ws)

    # 1. Line-numbered pretty-printed JSON array -> json/v1 + working --json-pointer
    arr = [{"id": i, "name": f"item-{i}", "payload": "x" * 180} for i in range(90)]
    raw_json = json.dumps(arr, indent=2).splitlines()
    numbered_json = (
        f"Showing lines 1-{len(raw_json)} of {len(raw_json)}\n"
        + "\n".join(f"{i}: {ln}" for i, ln in enumerate(raw_json, 1))
        + "\n"
    )
    j_digest, j_short = digest_output(store, ws, "read_file", numbered_json)
    assert "profile=json/v1" in j_digest
    fetched_ptr = get(store, ws, f"run:{j_short}#stdout", Selector(json_pointer="/5"))
    assert '"id": 5' in fetched_ptr
    assert '"name": "item-5"' in fetched_ptr

    # 2. Line-numbered JSONL -> working --records 2:3 (ignoring banner & N: prefixes)
    jsonl_lines = [json.dumps({"seq": i, "msg": "row-" + ("y" * 160)}) for i in range(1, 120)]
    numbered_jsonl = (
        f"Showing lines 1-{len(jsonl_lines)} of {len(jsonl_lines)}\n"
        + "\n".join(f"{i}: {ln}" for i, ln in enumerate(jsonl_lines, 1))
        + "\n"
    )
    _, jl_short = digest_output(store, ws, "read_file", numbered_jsonl)
    fetched_rec = get(store, ws, f"run:{jl_short}#stdout", Selector(records=(2, 3)))
    assert '"seq": 2' in fetched_rec
    assert '"seq": 3' in fetched_rec
    assert "Showing lines" not in fetched_rec

    # 3. Line-numbered Jupyter notebook (.ipynb) -> notebook/v1 + working --part cell:1
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "name": "python3"}},
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Notebook Heading\n", "Summary line.\n"],
            },
            *[
                {
                    "cell_type": "code",
                    "execution_count": i,
                    "metadata": {},
                    "source": [f"x_{i} = {i}  # " + ("z" * 160) + "\n"],
                    "outputs": [],
                }
                for i in range(1, 75)
            ],
        ],
    }
    nb_lines = json.dumps(nb, indent=2).splitlines()
    numbered_nb = "\n".join(f"{i}: {ln}" for i, ln in enumerate(nb_lines, 1)) + "\n"
    nb_digest, nb_short = digest_output(store, ws, "read_file", numbered_nb)
    assert "profile=notebook/v1" in nb_digest
    fetched_cell = get(store, ws, f"run:{nb_short}#stdout", Selector(part="cell:1"))
    assert "Notebook Heading" in fetched_cell
