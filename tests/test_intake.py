"""`ctx intake`: command-less entry of pasted text, files, stdin and clipboard."""

from __future__ import annotations

import base64
import json
import random
import re
import subprocess
import sys
import types

import pytest
from conftest import make_store, make_ws

from ctx import intake
from ctx.digest.base import INTAKE_TOOL, from_intake
from ctx.intake import IntakeError, intake_bytes, intake_path, render_chunk_page
from ctx.retrieval import Selector, get, search

SPAN_RE = re.compile(r"· span ([0-9a-f]{10})")


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def _budget_bytes(ws) -> int:
    return ws.config.budgets.digest_tokens * 4


def _markdown(sections: int, needle_at: int | None = None) -> bytes:
    r = random.Random(11)
    out = []
    for s in range(1, sections + 1):
        out.append(f"# Section {s}\n\n")
        for _ in range(4):
            out.append(" ".join(r.choice(["alpha", "beta", "gamma", "delta"]) for _ in range(25)) + "\n\n")
        if s == needle_at:
            out.append("the hidden NEEDLE_7731 lives here\n\n")
    return "".join(out).encode()


# ----------------------------------------------------------------- identity
def test_same_bytes_and_label_give_same_address_and_digest(env):
    ws, store = env
    data = _markdown(200)
    a = intake_bytes(store, ws, data, label="spec")
    b = intake_bytes(store, ws, data, label="spec")
    assert a.address == b.address and a.digest == b.digest
    assert a.profile == "chunkmap/v1"
    c = intake_bytes(store, ws, data, label="other")
    assert c.address != a.address


def test_intake_origin_predicate(env):
    ws, store = env
    receipt = intake_bytes(store, ws, _markdown(5), label="notes")
    manifest = store.get_manifest(receipt.address.split(":")[1].split("#")[0])
    assert manifest["argv"][0] == INTAKE_TOOL
    assert from_intake(manifest)
    assert not from_intake({**manifest, "argv": ["cat"]})
    assert not from_intake({**manifest, "shell": True})
    assert not from_intake({**manifest, "source": {"gitHead": "abc", "worktreeHash": None}})


def test_command_output_never_gets_the_chunk_map(env):
    """The profile is gated on intake origin: identical bytes digested as a
    tool result keep their existing text/v1 rendering."""
    from ctx.digest import digest_output

    ws, store = env
    data = _markdown(200).decode()
    digest, _ = digest_output(store, ws, "Read", data)
    assert "profile=text/v1" in digest.splitlines()[0]


def test_labels_cannot_trigger_command_anchored_profiles(env):
    ws, store = env
    receipt = intake_bytes(store, ws, b"x\n" * 2000, label="pytest -q /grep")
    assert receipt.label == "pytest_-q_grep"
    assert receipt.profile in ("chunkmap/v1", "text/v1")


# --------------------------------------------------------------- small payloads
def test_small_payload_is_not_wrapped(env):
    ws, store = env
    r = intake_bytes(store, ws, b"hi there")
    assert r.passthrough and r.digest == "hi there"
    assert r.address.startswith("run:")
    assert get(store, ws, r.address, Selector()).endswith("hi there")


def test_small_payload_is_still_redacted(env):
    ws, store = env
    secret = "ghp_" + "A" * 36
    r = intake_bytes(store, ws, f"token {secret}".encode())
    assert secret not in r.digest
    assert "redact" in r.digest


def test_medium_payload_inlines_through_text_profile(env):
    ws, store = env
    body = b"".join(b"line %d of notes\n" % i for i in range(60))
    r = intake_bytes(store, ws, body)
    assert r.profile == "text/v1" and not r.passthrough
    assert "output (complete):" in r.digest
    assert r.digest.splitlines()[1] == "intake: paste · output (complete):"


def test_empty_payload_is_refused(env):
    ws, store = env
    with pytest.raises(IntakeError):
        intake_bytes(store, ws, b"")


# --------------------------------------------------------------- large payloads
def test_hundreds_of_thousands_of_lines_stay_in_budget(env):
    ws, store = env
    data = b"".join(b"row %d the quick brown fox jumps over the lazy dog\n" % i
                    for i in range(300_000))
    r = intake_bytes(store, ws, data, label="big")
    assert r.profile == "chunkmap/v1"
    assert len(r.digest.encode()) <= _budget_bytes(ws)
    assert r.digest.rstrip().splitlines()[-1].startswith("  ctx search ")
    assert "chunks omitted" in r.digest and "coverage:" in r.digest
    assert r.lines == 300_000


def test_single_multi_megabyte_line_stays_in_budget_and_resolves(env):
    ws, store = env
    payload = base64.b64encode(random.Random(9).randbytes(2_400_000))  # ~3.2 MB, one line
    r = intake_bytes(store, ws, payload, label="blob64")
    assert r.profile == "chunkmap/v1" and r.lines == 1
    assert len(r.digest.encode()) <= _budget_bytes(ws)
    assert "next:" in r.digest
    sids = SPAN_RE.findall(r.digest)
    assert sids
    for sid in sids:
        out = get(store, ws, r.address, Selector(span=sid))
        assert "bytes:" in out and "complete)" in out and "truncated" not in out
        piece = out.split("\n", 2)[2]
        assert piece and piece in payload.decode()


def test_minified_json_line_stays_in_budget(env):
    ws, store = env
    doc = json.dumps([{"id": i, "path": f"src/m{i}.py:3"} for i in range(80_000)],
                     separators=(",", ":")).encode()
    r = intake_bytes(store, ws, doc, label="dump")
    assert len(r.digest.encode()) <= _budget_bytes(ws)


def test_every_chunk_row_resolves(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(300, needle_at=150), label="doc")
    rows = [ln for ln in r.digest.splitlines() if ln.startswith("  #")]
    assert rows and len(rows) == len(SPAN_RE.findall(r.digest))
    for row in rows:
        lines = re.search(r"L(\d+)-L(\d+)", row)
        out = get(store, ws, r.address, Selector(span=SPAN_RE.search(row).group(1)))
        assert f"region: L{lines.group(1)}:{lines.group(2)}" in out
        assert "complete" in out and "truncated" not in out


def test_needle_in_an_omitted_chunk_is_found_by_search(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(300, needle_at=137), label="doc")
    assert "NEEDLE_7731" not in r.digest
    run = r.address.split("#")[0]
    out = search(store, ws, run, ["NEEDLE_7731"])
    assert "NEEDLE_7731" in out and "matches: 1" in out


def test_signal_chunk_is_shown_and_flagged(env):
    ws, store = env
    data = _markdown(300).decode().replace(
        "# Section 211\n", "# Section 211\n\nTraceback (most recent call last):\nValueError: x\n", 1)
    r = intake_bytes(store, ws, data.encode(), label="doc")
    flagged = [ln for ln in r.digest.splitlines() if ln.startswith("  #") and " ! " in ln]
    assert len(flagged) == 1 and "ValueError: x" in flagged[0]   # latest of equal rank


def test_paging_lists_every_chunk_with_matching_spans(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(400), label="doc")
    total = int(re.search(r"chunks: ([\d,]+)", r.digest).group(1).replace(",", ""))
    digest_spans = set(SPAN_RE.findall(r.digest))
    seen, start, pages = [], 1, 0
    while True:
        page = render_chunk_page(store, ws, r.address, start)
        pages += 1
        assert len(page.encode()) <= ws.config.budgets.result_tokens * 4
        seen += [int(m) for m in re.findall(r"^  #(\d+)", page, re.M)]
        nxt = re.search(r"next: ctx intake --chunks \S+ --from (\d+)", page)
        if not nxt:
            break
        start = int(nxt.group(1))
        assert pages < 200
    assert seen == list(range(1, total + 1))
    paged_spans = set()
    start = 1
    while start:
        page = render_chunk_page(store, ws, r.address, start)
        paged_spans |= set(SPAN_RE.findall(page))
        nxt = re.search(r"--from (\d+)$", page)
        start = int(nxt.group(1)) if nxt else 0
    assert digest_spans <= paged_spans


def test_paging_refuses_out_of_range_and_binary(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(50), label="doc")
    with pytest.raises(IntakeError):
        render_chunk_page(store, ws, r.address, 10_000)
    b = intake_bytes(store, ws, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with pytest.raises(IntakeError):
        render_chunk_page(store, ws, b.address, 1)


# --------------------------------------------------------------------- binary
def test_binary_payload_gets_a_leased_address_that_survives_gc(env):
    ws, store = env
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x08\x08\x02\x00\x00\x00"
    r = intake_bytes(store, ws, png, label="shot")
    assert r.kind == "png" and r.address.startswith("blob:")
    assert r.digest == intake_bytes(store, ws, png, label="shot").digest
    # An explicit zero-day horizon overrides default retention leases; the
    # intake lease is a different reason and keeps the payload.
    store.gc(0, override_retention=True)
    out = get(store, ws, r.address, Selector(bytes=(1, 8)))
    assert "PNG" in out


def test_text_payload_survives_explicit_gc(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(80), label="doc")
    store.gc(0, override_retention=True)
    assert "Section 1" in get(store, ws, r.address, Selector(lines=(1, 3)))


def test_text_starting_with_bmp_magic_is_text(env):
    ws, store = env
    assert intake.binary_kind(b"BMW owners manual\n") is None
    assert intake.binary_kind(b"BM\x00\x00") == "bmp"
    assert intake.binary_kind(b"plain" + b"\x00" * 3) == "binary"


# ------------------------------------------------------------------ paths
def test_path_is_confined_and_policy_checked(env, tmp_path):
    ws, store = env
    (ws.root / "notes.md").write_bytes(_markdown(3))
    r = intake_path(store, ws, "notes.md")
    assert r.name == "notes.md" and r.label == "file"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret-ish\n")
    with pytest.raises(IntakeError, match="pipe it"):
        intake_path(store, ws, str(outside))
    (ws.root / ".env").write_text("A=1\n")
    with pytest.raises(IntakeError, match="excluded"):
        intake_path(store, ws, ".env")
    with pytest.raises(IntakeError):
        intake_path(store, ws, "missing.txt")


# -------------------------------------------------------------- clipboard
def _fake_tools(monkeypatch, present: dict[str, tuple[int, bytes]], calls: list):
    monkeypatch.setattr(intake, "_which", lambda name: f"/fake/{name}" if name in present else None)

    def run(argv, input=None, **kw):
        name = argv[0].rsplit("/", 1)[-1]
        calls.append((name, input))
        rc, out = present[name]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=b"")

    monkeypatch.setattr(intake, "_run", run)


def test_clipboard_read_falls_through_failing_tools(monkeypatch):
    calls: list = []
    _fake_tools(monkeypatch, {"wl-paste": (1, b""), "xclip": (0, b"from xclip")}, calls)
    assert intake.read_clipboard() == ("xclip", b"from xclip")
    assert [c[0] for c in calls] == ["wl-paste", "xclip"]


def test_clipboard_powershell_crlf_is_normalized(monkeypatch):
    _fake_tools(monkeypatch, {"powershell.exe": (0, b"a\r\nb\r\n")}, [])
    assert intake.read_clipboard() == ("powershell.exe", b"a\nb\n")


def test_clipboard_absent_is_declared(monkeypatch):
    _fake_tools(monkeypatch, {}, [])
    with pytest.raises(intake.ClipboardUnavailable, match="no clipboard tool"):
        intake.read_clipboard()
    with pytest.raises(intake.ClipboardUnavailable):
        intake.write_clipboard("x")
    _fake_tools(monkeypatch, {"xsel": (1, b"")}, [])
    with pytest.raises(intake.ClipboardUnavailable, match="failed"):
        intake.read_clipboard()


def test_clipboard_write(monkeypatch):
    calls: list = []
    _fake_tools(monkeypatch, {"pbcopy": (0, b"")}, calls)
    assert intake.write_clipboard("digest text") == "pbcopy"
    assert calls == [("pbcopy", b"digest text")]


def test_cli_clip_and_copy(env, monkeypatch, capsys):
    from ctx.cli import main

    ws, _ = env
    calls: list = []
    _fake_tools(monkeypatch, {"pbpaste": (0, _markdown(200)), "pbcopy": (0, b"")}, calls)
    rc = main(["--workspace", str(ws.root), "intake", "--clip", "--copy", "--json"])
    cap = capsys.readouterr()
    assert rc == 0
    doc = json.loads(cap.out)
    assert doc["schema"] == "ctx.intake/v1" and doc["label"] == "clipboard"
    assert doc["copied"] == "pbcopy"
    assert calls[-1] == ("pbcopy", doc["digest"].encode())


def test_cli_clip_without_tools_is_not_a_crash(env, monkeypatch, capsys):
    from ctx.cli import main

    ws, _ = env
    _fake_tools(monkeypatch, {}, [])
    assert main(["--workspace", str(ws.root), "intake", "--clip"]) == 1
    assert "no clipboard tool" in capsys.readouterr().err


# -------------------------------------------------------------------- CLI
def _ctx(args, cwd, stdin: bytes | None = None, env_extra=None):
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "ctx", *args], cwd=cwd, input=stdin,
                          capture_output=True, env=env, timeout=120)


def test_cli_reads_piped_stdin_without_an_argument(env, state_home):
    ws, _ = env
    data = _markdown(200)
    p = _ctx(["intake", "--label", "spec"], ws.root, stdin=data)
    assert p.returncode == 0, p.stderr
    out = p.stdout.decode()
    assert "profile=chunkmap/v1" in out
    again = _ctx(["intake", "--label", "spec", "-"], ws.root, stdin=data)
    assert again.stdout == p.stdout
    handle = re.search(r"--chunks (\S+) --from", out).group(1)
    page = _ctx(["intake", "--chunks", handle, "--from", "2"], ws.root)
    assert page.returncode == 0 and b"#2 " in page.stdout


def test_cli_small_paste_prints_payload_and_names_the_handle(env, state_home):
    ws, _ = env
    p = _ctx(["intake"], ws.root, stdin=b"short note\n")
    assert p.returncode == 0
    assert p.stdout == b"short note\n"
    assert b"stored whole as run:" in p.stderr


def test_cli_help_parity():
    from ctx import cli, cliux

    assert "intake" in cli._COMMANDS
    assert cliux.help_line("intake")
    p = subprocess.run([sys.executable, "-m", "ctx", "intake", "--help"],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    for flag in ("--label", "--name", "--focus", "--json", "--clip", "--copy", "--chunks", "--from"):
        assert flag in p.stdout


def test_cli_flag_conflicts(env, capsys):
    from ctx.cli import main

    ws, _ = env
    assert main(["--workspace", str(ws.root), "intake", "--from", "3", "-"]) == 2
    assert main(["--workspace", str(ws.root), "intake", "--chunks", "run:abcdef", "x"]) == 2


# ------------------------------------------------- review fixes (round 2)
NO_COMMAND = re.compile(r"^(cwd: |command: |exit[: ]|status: |stdout: |stderr: )", re.M)


def test_intake_digests_never_describe_a_command(env):
    """Every profile path: chunk map, text inline, json, log, binary-magic
    text. A paste is not a command that ran and exited 0."""
    ws, store = env
    payloads = {
        "map": _markdown(200),
        "inline": b"".join(b"note line %d\n" % i for i in range(40)),
        "json": json.dumps([{"id": i, "v": [i, i]} for i in range(20_000)],
                           separators=(",", ":")).encode(),
        "log": b"".join(b"2024-01-01T00:00:%02dZ INFO svc n=%d\n" % (i % 60, i) for i in range(400)),
        "bmw": b"BMW owners manual\n" + b"chapter text line\n" * 400,
    }
    for label, data in payloads.items():
        r = intake_bytes(store, ws, data, label=label, name=f"{label}.txt")
        assert not NO_COMMAND.search(r.digest), (label, r.digest[:400])
        assert f"intake: {label} · {label}.txt" in r.digest, (label, r.digest[:200])
    assert intake_bytes(store, ws, payloads["bmw"], label="bmw").profile == "chunkmap/v1"


def _secret_doc(n: int, seed: int) -> bytes:
    r = random.Random(seed)
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = []
    for _ in range(n):
        out.append("# AKIA" + "".join(r.choice(alpha) for _ in range(16)) + "\n\n")
        out.append(("word " * r.randrange(60, 400)).strip() + "\n\n")
    return "".join(out).encode()


def _walk_pages(store, ws, handle):
    seen, start, pages = [], 1, 0
    while start:
        page = render_chunk_page(store, ws, handle, start)
        pages += 1
        assert len(page.encode()) <= ws.config.budgets.result_tokens * 4, page[-300:]
        assert "truncated" not in page
        rows = [int(m) for m in re.findall(r"^  #(\d+)", page, re.M)]
        assert rows and rows[0] == start
        seen += rows
        nxt = re.search(r"^next: ctx intake --chunks \S+ --from (\d+)$", page, re.M)
        if nxt:
            assert int(nxt.group(1)) == rows[-1] + 1
        start = int(nxt.group(1)) if nxt else 0
        assert pages < 500
    return seen


@pytest.mark.parametrize("seed", range(6))
def test_secret_bearing_labels_keep_budget_followups_and_paging(env, seed):
    """Fitting is measured on the redacted text: a 20-char key becomes a
    41-char marker, which an un-redacted fit could not absorb."""
    ws, store = env
    r = intake_bytes(store, ws, _secret_doc(150 + 20 * seed, seed), label="sec")
    assert r.profile == "chunkmap/v1"
    assert "AKIA" not in r.digest
    assert len(r.digest.encode()) <= _budget_bytes(ws)
    assert "truncated" not in r.digest
    tail = r.digest.rstrip().splitlines()
    assert tail[-1] == "redaction: applied [aws-access-key]"
    assert tail[-2].startswith("  ctx search ")
    total = int(re.search(r"chunks: ([\d,]+)", r.digest).group(1).replace(",", ""))
    assert _walk_pages(store, ws, r.address) == list(range(1, total + 1))


def _traceback(depth: int = 90) -> bytes:
    out = ["Traceback (most recent call last):"]
    for i in range(depth):
        out += [f'  File "/srv/app/mod{i % 7}.py", line {10 + i}, in func_{i}',
                f"    return func_{i + 1}(x, y={i})"]
    out.append("RecursionError: maximum recursion depth exceeded")
    return ("\n".join(out) + "\n").encode()


def test_traceback_paste_surfaces_its_decisive_line(env):
    ws, store = env
    data = _traceback()
    r = intake_bytes(store, ws, data, label="trace")
    assert r.profile == "chunkmap/v1"
    d = r.digest
    last_line = r.lines
    assert f"decisive signal L{last_line}: RecursionError: maximum recursion depth exceeded" in d
    rows = [ln for ln in d.splitlines() if ln.startswith("  #")]
    assert "RecursionError" in rows[-1] and " ! " in rows[-1]
    first_next = d.split("next:\n", 1)[1].splitlines()[0].strip()
    sid = SPAN_RE.search(rows[-1]).group(1)
    assert first_next == f"ctx get {r.address} --span {sid}"
    assert "RecursionError" in get(store, ws, r.address, Selector(span=sid))


CAP = 8 * 1024
_CAP_PAYLOADS = {
    # the cap lands inside the first (only) line
    "one-line": b"abcdefghij" * 1500,
    # ... inside the first line, with a short line after it
    "one-line-then-tail": b"abcdefghij" * 1500 + b"\nshort tail line\n",
    # ... inside a long line that follows complete lines
    "straddler-after-lines": b"".join(b"line %04d ok\n" % i for i in range(300))
    + b"x" * 9000 + b"\nafter\n",
    # ... inside an ordinary short line
    "short-lines": b"".join(b"row %05d of a long paste\n" % i for i in range(1500)),
    # ... exactly on a line boundary
    "on-boundary": b"y" * (CAP - 1) + b"\n" + b"z" * 5000 + b"\n",
    # ... in the middle of a two-byte UTF-8 character
    "split-char": b"a" + "é".encode() * 6000,
}


def _bytes_body(out: str) -> str:
    """The body of a ``ctx get --bytes`` answer, minus header and next: line."""
    body = out.split("\n", 2)[2]
    if "\nnext: ctx get " in body:
        body = body.rsplit("\nnext: ctx get ", 1)[0]
    return body


def _run(store, ws, cmd: str) -> str:
    import shlex

    argv = shlex.split(cmd)
    assert argv[0] == "ctx", cmd
    if argv[1] == "intake":
        assert argv[2] == "--chunks"
        return render_chunk_page(store, ws, argv[3], int(argv[5]) if len(argv) > 5 else 1)
    assert argv[1] == "get", cmd
    flag, value = argv[3], argv[4]
    if flag == "--span":
        return get(store, ws, argv[2], Selector(span=value))
    a, b = (int(x) for x in value.split(":"))
    key = {"--bytes": "bytes", "--lines": "lines"}[flag]
    return get(store, ws, argv[2], Selector(**{key: (a, b)}))


@pytest.mark.parametrize("case", sorted(_CAP_PAYLOADS))
def test_every_byte_past_the_parse_cap_is_reachable(env, monkeypatch, case):
    """Execute what the digest and the chunk pages print and rebuild the
    payload from the answers: the map's own chunks (through their spans)
    plus the unmapped tail (through the printed --bytes chain)."""
    from ctx.digest import base as digest_base
    from ctx.digest import intakeprof

    monkeypatch.setattr(digest_base, "MAX_PARSE_BYTES", CAP)
    monkeypatch.setattr(intakeprof, "MAX_PARSE_BYTES", CAP)
    ws, store = env
    data = _CAP_PAYLOADS[case]
    r = intake_bytes(store, ws, data, label="cap")
    d = r.digest
    assert r.profile == "chunkmap/v1", d
    assert " total · " in d.splitlines()[1]
    assert re.search(r"L(\d+)-L(\d+)", d.split("NOT mapped:", 1)[1]) is None or all(
        int(x) <= int(y) for x, y in re.findall(
            r"L([\d,]+)-L([\d,]+)", d.split("NOT mapped:", 1)[1].replace(",", "")))
    m = re.search(r"mapped: bytes 1-([\d,]+) .*NOT mapped: bytes ([\d,]+)-([\d,]+) ", d)
    assert m, d
    mapped, first, total = (int(g.replace(",", "")) for g in m.groups())
    assert first == mapped + 1 and total == len(data) and mapped <= CAP

    # every printed follow-up runs and answers
    for cmd in re.findall(r"^  (ctx .+)$", d.split("next:\n", 1)[1], re.M):
        if "<pattern>" not in cmd:
            assert _run(store, ws, cmd)

    # the mapped part, rebuilt from every chunk's span, page by page
    mapped_text, start = "", 1
    while start:
        page = render_chunk_page(store, ws, r.address, start)
        assert "NOT mapped: bytes" in page
        for sid in SPAN_RE.findall(page):
            out = _run(store, ws, f"ctx get {r.address} --span {sid}")
            if "\nbytes: " in out:
                mapped_text += out.split("\n", 2)[2]
            else:
                body = out.split("\n")[2:]
                mapped_text += "".join(re.sub(r"^L\d+: ", "", ln, count=1) + "\n" for ln in body)
        nxt = re.search(r"^next: (ctx intake --chunks .+)$", page, re.M)
        start = int(nxt.group(1).rsplit(" ", 1)[1]) if nxt else 0
    if not data[:mapped].endswith(b"\n"):
        mapped_text = mapped_text[:-1] if mapped_text.endswith("\n") and len(
            mapped_text.encode()) > mapped else mapped_text
    assert mapped_text.encode() == data[:mapped], case

    # the tail, by following the printed --bytes chain to its end
    cmd = re.search(r"^  (ctx get \S+ --bytes \d+:\d+)$", d, re.M).group(1)
    tail, hops = "", 0
    while cmd:
        out = _run(store, ws, cmd)
        tail += _bytes_body(out)
        nxt = re.search(r"^next: (ctx get \S+ --bytes \d+:\d+)$", out, re.M)
        cmd = nxt.group(1) if nxt else None
        hops += 1
        assert hops < 100
    assert mapped_text.encode() + tail.encode() == data, case


def test_parse_cap_mid_line_never_prints_an_impossible_range(env, monkeypatch):
    """The reviewer's case: the cap inside the only line used to print
    ``NOT mapped: L2-L1`` and ``ctx get … --lines 2:41``."""
    from ctx.digest import base as digest_base
    from ctx.digest import intakeprof

    monkeypatch.setattr(digest_base, "MAX_PARSE_BYTES", CAP)
    monkeypatch.setattr(intakeprof, "MAX_PARSE_BYTES", CAP)
    ws, store = env
    r = intake_bytes(store, ws, _CAP_PAYLOADS["one-line"], label="cap")
    assert "L2-L1" not in r.digest and "--lines 2:" not in r.digest
    assert "mapped: bytes 1-8,192 = part of L1" in r.digest
    assert "NOT mapped: bytes 8,193-15,000 (6.6 KiB, rest of L1)" in r.digest
    page = render_chunk_page(store, ws, r.address, 1)
    assert "L2-L1" not in page and "--lines" not in page


def test_session_query_never_changes_an_intake(env, monkeypatch):
    """Without the explicit empty focus, text/v1 would borrow the session's
    task query and the address would depend on session state."""
    import ctx.task_context as task_context

    ws, store = env
    data = b"".join(b"note line %d\n" % i for i in range(40))
    plain = intake_bytes(store, ws, data, label="n")
    monkeypatch.setattr(task_context, "current_query", lambda *a, **k: "note line focus")
    steered = intake_bytes(store, ws, data, label="n")
    assert steered.address == plain.address and steered.digest == plain.digest


def test_byte_span_mint_refuses_ranges_past_the_stream(env):
    from ctx.digest import synthesize_manifest
    from ctx.digest.base import DigestContext

    ws, store = env
    m = synthesize_manifest(store, ws, INTAKE_TOOL, "x" * 100, "", argv=[INTAKE_TOOL])
    ctx = DigestContext.load(store, ws, m, focus="")
    assert ctx.mint_span(ctx.stdout, "bytes", a=1, b=100)
    assert ctx.mint_span(ctx.stdout, "bytes", a=1, b=101) is None
    assert ctx.mint_span(ctx.stdout, "bytes", a=0, b=10) is None
    assert ctx.mint_span(ctx.stdout, "bytes", a=20, b=10) is None


def test_name_is_shown_in_the_chunk_map(env):
    ws, store = env
    r = intake_bytes(store, ws, _markdown(200), label="spec", name="/home/u/Downloads/design.md")
    assert r.name == "design.md"
    assert r.digest.splitlines()[1].startswith("intake: spec · design.md · ")
    assert "/home/" not in r.digest


def test_utf16_with_bom_is_text(env):
    ws, store = env
    body = "".join(f"ligne {i} — café\r\n" for i in range(300))
    for bom, enc in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        data = bom + body.encode(enc)
        assert intake.binary_kind(data) is None
        r = intake_bytes(store, ws, data, label="win")
        assert r.kind == "text" and r.encoding == enc and not r.lossy
        assert f"decoded from {enc}" in r.digest
        hit = search(store, ws, r.address.split("#")[0], ["ligne 250 "])
        assert "café" in hit


def test_real_hanging_clipboard_tool_is_abandoned(monkeypatch, tmp_path):
    """Drive the real helper (real which, real subprocess) at a stub that
    never exits: the timeout path must fire and be declared."""
    import os
    import time

    if os.name != "posix":
        pytest.skip("POSIX shell stub")
    stub = tmp_path / "bin" / "xclip"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nsleep 30\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub.parent))
    monkeypatch.setattr(intake, "_CLIPBOARD_TIMEOUT", 1)
    t = time.monotonic()
    with pytest.raises(intake.ClipboardUnavailable, match="tried xclip"):
        intake.read_clipboard()
    assert time.monotonic() - t < 10


def _telemetry_events(store) -> list[dict]:
    path = store.audit_dir / "telemetry.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def test_record_delivery_default_books_telemetry_and_false_suppresses_it(env):
    ws, store = env
    data = b"".join(b"line %d of a log\n" % i for i in range(4000))
    default = intake_bytes(store, ws, data)
    events = _telemetry_events(store)
    assert [e["op"] for e in events] == ["intake"] and events[0]["est_tokens_avoided"] > 0
    quiet = intake_bytes(store, ws, data + b"more\n", record_delivery=False)
    assert len(_telemetry_events(store)) == 1                 # no new event
    # identity and digest bytes do not depend on the flag
    again = intake_bytes(store, ws, data, record_delivery=False)
    assert again.address == default.address and again.digest == default.digest
    assert quiet.address != default.address
