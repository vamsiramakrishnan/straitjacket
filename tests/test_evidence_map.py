"""The evidence map: every error-like line of a large output accounted for,
and no claim printed that the delivered bytes do not support.

Fixtures: tests/fixtures/evidence_logs holds reduced copies (head, the
decisive lines with 15 either side, tail) of ten synthetic logs, each with the
lines that answer its question (index.json ``answer_text``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_store, make_ws
from ctx.digest import digest_output, render_run_digest
from ctx.digest import evidence_map as E
from ctx.digest.base import StreamView
from ctx.digest.text import _signal_rank, _signal_rank_full
from ctx.textutil import index_lines

FIX = Path(__file__).resolve().parent / "fixtures" / "evidence_logs"
ITEMS = json.loads((FIX / "index.json").read_text(encoding="utf-8"))["items"]
ACCOUNT_RE = re.compile(
    r"^evidence map \(exact[^)]*\): ([\d,]+) error-like lines · ([\d,]+) distinct messages · "
    r"([\d,]+) shown with context · ([\d,]+) listed · ([\d,]+) not listed$", re.M)
CLOSING = "every error-like line is one of these"


def _n(s: str) -> int:
    return int(s.replace(",", ""))


def _view_ctx(text: str, focus=(), whole=True, total_lines=None):
    n = text.count("\n")
    view = StreamView("stdout", len(text.encode()), total_lines or n, "text/plain", text, whole)
    empty = StreamView("stderr", 0, 0, "text/plain", "", True)
    return SimpleNamespace(stdout=view, stderr=empty, focus_terms=tuple(focus))


def _digest(workspace_dir, text, **kw):
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    digest, rid = digest_output(store, ws, "Bash", text, **kw)
    return digest, rid, ws, store


def _excerpt_lines(digest: str) -> list[str]:
    return [ln for ln in digest.splitlines() if re.match(r"^    L\d+: ", ln)]


def _listed_rows(digest: str) -> list[str]:
    return [ln for ln in digest.splitlines() if re.match(r"^    ×", ln)]


def _visible(digest: str) -> str:
    """The part of the digest that is the map's excerpts and list."""
    return "\n".join(_excerpt_lines(digest) + _listed_rows(digest))


def _oracle_messages(lines: list[str]) -> int:
    """Distinct messages by the documented rule, computed independently of
    the renderer: skeleton groups, split by slots with <= 8 values."""
    groups: dict[str, list[tuple[str, ...]]] = {}
    for ln in lines:
        if _signal_rank_full(ln) > 0:
            sk, vals = E.skeleton(E.strip_ansi(ln))
            groups.setdefault(sk, []).append(vals)
    total = 0
    for rows in groups.values():
        split = []
        for j in range(len(rows[0])):
            d = len({r[j] for r in rows})
            split.append(d <= E.FEW_VALUES and (d < len(rows) or len(rows) <= 2))
        total += len({tuple(v for j, v in enumerate(r) if split[j]) for r in rows})
    return total


def _log(rows):
    return "\n".join(rows) + "\n"


def _messages_of(rows):
    emap = E.build(_view_ctx(_log(rows)))
    return emap


# ---------------------------------------------------------------- grouping
@pytest.mark.parametrize("a,b", [
    ("ERROR worker exit code 1", "ERROR worker exit code 137"),
    ("ERROR HTTP/1.1 404 from upstream", "ERROR HTTP/1.1 503 from upstream"),
    ("ERROR connect ECONNREFUSED 10.0.0.1:5432", "ERROR connect ECONNREFUSED 10.0.0.2:6379"),
    ("ERROR migration 0041 failed", "ERROR migration 0042 failed"),
    ("FAILED 0 of 12 tests failed", "FAILED 12 of 12 tests failed"),
    ("ERROR relation 'users2' does not exist", "ERROR relation 'orders7' does not exist"),
    ("error: unknown option '--foo1'", "error: unknown option '--bar2'"),
    ("ERROR cannot open /etc/shadow", "ERROR cannot open /etc/hosts"),
    ("ERROR bad config ./config/prod.yml", "ERROR bad config ./config/dev.yml"),
    ("ERROR compile failed in src/a.c", "ERROR compile failed in src/zzz/b.c"),
])
def test_few_valued_slots_split_and_the_rare_value_is_visible(a, b):
    rows = [f"2025-01-01 10:00:{i % 60:02d} INFO ok {i}" for i in range(200)]
    for i in range(10, 160, 5):
        rows[i] = a
    rows[180] = b
    emap = _messages_of(rows)
    assert len(emap.messages) == 2
    rare = next(m for m in emap.messages if m.count == 1)
    assert rare.first == 181 and emap.messages.index(rare) == 0   # rare ranks first
    ctx = _view_ctx(_log(rows))
    r = E.render(ctx, emap, cap_bytes=1500)
    text = "\n".join(r.lines)
    assert b in text and a in text


def test_the_reviewers_3000_line_repro():
    rows = [f"2025-03-01 10:{(i // 60) % 60:02d}:{i % 60:02d} INFO worker heartbeat ok seq={i}"
            for i in range(3000)]
    for j in range(41):
        rows[20 + j * 70] = "2025-03-01 10:00:00 ERROR worker exit code 1"
        rows[50 + j * 70] = "2025-03-01 10:00:00 ERROR connect 10.0.0.1:5432 failed: connection refused"
    rows[2980] = "2025-03-01 10:49:40 ERROR worker exit code 137"
    rows[2569] = "2025-03-01 10:42:49 ERROR connect 10.0.0.2:6379 failed: connection refused"
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    assert len(emap.messages) == 4
    text = "\n".join(E.render(ctx, emap, cap_bytes=1500).lines)
    assert "L2981: 2025-03-01 10:49:40 ERROR worker exit code 137" in text
    assert "L2570: 2025-03-01 10:42:49 ERROR connect 10.0.0.2:6379" in text
    assert "every error-like line is one of these 4 messages." in text


def test_unique_id_lines_still_collapse_and_say_so():
    rows = [f"INFO tick {i}" for i in range(600)]
    for i in range(1, 600, 2):
        rows[i] = f"ERROR request req-{i * 7919 % 100003}x failed after retry"
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    assert len(emap.messages) == 1
    m = emap.messages[0]
    assert m.count == 300 and [v.distinct for v in m.varies] == [300]
    text = "\n".join(E.render(ctx, emap, cap_bytes=1500).lines)
    assert "varies: 300 distinct values" in text
    assert "a message marked varies stands for several values" in text


def test_volatile_parts_are_masked_unconditionally():
    sk = E.skeleton
    assert sk("2025-02-11 09:00:13 ERROR deadlock on items")[0] == \
        sk("2026-12-31T23:59:59.123Z ERROR deadlock on items")[0]
    assert sk("ERROR took 512ms at 0x7ffd1234abcd id 5f0c2b8e-1d2a-4b3c-8d9e-0a1b2c3d4e5f")[1] == ()
    assert sk("sshd[1234]: error: bad")[1] == () and sk("pid=99 ERROR x")[1] == ()
    assert sk("ERROR deadlock detected on items")[0] != sk("ERROR deadlock detected on users")[0]


def test_ansi_colour_is_removed_before_detecting_and_grouping():
    red = "\x1b[31mERROR\x1b[0m"
    rows = ["INFO ok"] * 300
    rows[100] = "ERROR disk full"
    rows[150] = f"{red} disk full"
    rows[200] = f"{red} quota table missing"
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    assert emap.signal_lines == 3 and len(emap.messages) == 2
    text = "\n".join(E.render(ctx, emap, cap_bytes=1500).lines)
    assert "\x1b" not in text and "ERROR quota table missing" in text
    only_coloured = ["INFO ok"] * 300
    only_coloured[10] = f"{red} boom failed"
    assert E.build(_view_ctx(_log(only_coloured))) is not None


def test_fast_signal_scan_equals_the_per_line_ranking_on_every_fixture():
    for item in ITEMS:
        text = (FIX / item["file"]).read_text(encoding="utf-8")
        lines = index_lines(text)
        full = [(i, r) for i, ln in enumerate(lines, 1) if (r := _signal_rank_full(ln)) > 0]
        assert E.signal_rows(text, lines) == full, item["file"]
        assert all(_signal_rank(ln) == _signal_rank_full(ln) for ln in lines)
    tricky = ["no errors found", "0 failures", "FAİLED here", "exit code 3", "HTTP/1.1 502 Bad Gateway",
              "status: 503", "Traceback (most recent call last):", "Error: boom", "npm ERR! x",
              'File "a.py", line 3', "quota exceeded", "ok", "ÉRROR ünicode error",
              "2025-01-01 09:00:13 at x.py:12", "--> src/main.rs:3:4"]
    for ln in tricky:
        assert _signal_rank(ln) == _signal_rank_full(ln), ln


# ---------------------------------------------------------------- the ten logs
@pytest.mark.parametrize("item", ITEMS, ids=[i["file"] for i in ITEMS])
def test_decisive_line_is_expanded_and_the_accounting_is_exact(state_home, workspace_dir, item):
    text = (FIX / item["file"]).read_text(encoding="utf-8")
    digest, _rid, _, _ = _digest(workspace_dir, text)
    m = ACCOUNT_RE.search(digest)
    assert m, digest
    signal, distinct, shown, listed, unlisted = (_n(g) for g in m.groups())
    lines = index_lines(text)
    assert signal == sum(1 for ln in lines if _signal_rank_full(ln) > 0)
    assert distinct == _oracle_messages(lines)
    assert shown + listed + unlisted == distinct
    assert listed == len(_listed_rows(digest))
    assert (f"{CLOSING} {distinct:,} messages" in digest) == (unlisted == 0)
    assert E.map_supported(digest)
    excerpts = _excerpt_lines(digest)
    hits = [ln for ln in excerpts for a in item["answer_text"] if a.strip() and a.strip() in ln]
    assert hits, digest
    for ln in hits:
        n = int(re.match(r"^    L(\d+): ", ln).group(1))
        assert lines[n - 1].strip() in ln
    assert len(digest.encode()) < 3200


def test_the_old_misleading_parts_are_gone(state_home, workspace_dir):
    text = (FIX / "migration.log").read_text(encoding="utf-8")
    digest, rid, _, _ = _digest(workspace_dir, text)
    assert "2025-02-11 09  " not in digest          # timestamps are not "top files"
    assert "top files" not in digest
    assert "'error' --context" not in digest       # no generic search suggestion
    assert "first error" not in digest
    assert "may be" not in digest and "incomplete" not in digest
    assert "all " not in digest.split("evidence map")[1] or "distinct messages are above" not in digest
    # two exact counts name what each counts
    assert "path:line: severity lines (exact)" in digest and "diagnostics (exact)" not in digest


def test_render_is_deterministic(state_home, workspace_dir):
    text = (FIX / "server.log").read_text(encoding="utf-8")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _, rid = digest_output(store, ws, "Bash", text)
    manifest = store.get_manifest(rid)
    a, _ = render_run_digest(store, ws, manifest)
    b, _ = render_run_digest(store, ws, manifest)
    assert a == b and "evidence map (exact)" in a


# ---------------------------------------------------------------- ranking
def test_rarer_outranks_frequent_and_higher_tier_outranks_rarer():
    rows = [f"2025-01-01 10:00:{i % 60:02d} INFO tick {i}" for i in range(400)]
    for i in range(10, 390, 3):
        rows[i] = f"2025-01-01 10:00:00 ERROR cache miss storm on shard {i}"
    rows[200] = "2025-01-01 10:00:00 ERROR migration failed hard"      # tier 1, once
    rows[300] = "FATAL: disk full"                                       # tier 3, once
    emap = E.build(_view_ctx(_log(rows)))
    order = [m.first for m in emap.messages]
    assert order[0] == 301
    assert order[1] == 201
    freq = next(m for m in emap.messages if m.count > 1)
    assert emap.messages.index(freq) == 2


def test_tie_break_prefers_the_later_last_occurrence_then_the_earlier_first():
    rows = ["INFO ok"] * 100
    rows[10] = "ERROR alpha broke"
    rows[50] = "ERROR beta broke"
    emap = E.build(_view_ctx(_log(rows)))
    assert [m.first for m in emap.messages] == [51, 11]


def test_focus_terms_boost_matching_messages():
    rows = ["INFO ok"] * 100
    rows[10] = "ERROR payment gateway timeout"
    rows[20] = "ERROR payment gateway timeout"
    rows[50] = "FATAL kernel oops"
    plain = E.build(_view_ctx(_log(rows)))
    focused = E.build(_view_ctx(_log(rows), focus=("gateway",)))
    assert plain.messages[0].first == 51
    assert focused.messages[0].first == 11


def test_adjacent_messages_share_one_excerpt():
    rows = ["INFO ok"] * 60
    rows[30] = "ERROR first thing failed"
    rows[31] = "ERROR second thing failed"
    ctx = _view_ctx(_log(rows))
    r = E.render(ctx, E.build(ctx), cap_bytes=1500)
    assert r.windows == 1 and r.shown == 2
    assert sum(1 for ln in r.lines if ln.startswith("  stdout L")) == 1


def _wordy(n: int, rare_at=()):
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron".split()
    rows = ["INFO ok"] * max(400, n * 7 + 50)
    for j in range(n):
        name = words[j % 15] + words[(j // 15) % 15]
        for t in range(3):
            rows[5 + j * 7 + t * 3 % 7] = f"ERROR subsystem {name} failed to start"
    for k, at in enumerate(rare_at):
        rows[at] = f"FATAL decisive {words[k]} broke"
    return rows


def test_excerpts_never_push_a_message_off_the_list():
    rows = _wordy(30, rare_at=(390, 395))
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    rare = [i for i, m in enumerate(emap.messages) if m.count == 1]
    assert rare == [0, 1]
    no_excerpts = E.render(ctx, emap, cap_bytes=900, k=0)
    worst = list(range(len(emap.messages)))[::-1][:3]        # a wrong picker
    r = E.render(ctx, emap, cap_bytes=900, order=worst, labels={i: "x" for i in worst})
    text = "\n".join(r.lines)
    assert "FATAL decisive alpha broke" in text and "FATAL decisive beta broke" in text
    assert r.listed + r.shown >= no_excerpts.listed
    assert r.shown + r.listed + r.unlisted == len(emap.messages)


# ---------------------------------------------------------------- the sentence
def test_closing_sentence_only_when_true():
    few = ["INFO ok"] * 50 + ["ERROR one failed", "ERROR two failed"]
    ctx = _view_ctx(_log(few))
    r = E.render(ctx, E.build(ctx), cap_bytes=1500)
    assert r.unlisted == 0 and any(f"{CLOSING} 2 messages." in ln for ln in r.lines)

    ctx = _view_ctx(_log(_wordy(60)))
    emap = E.build(ctx)
    r = E.render(ctx, emap, cap_bytes=600)
    assert r.unlisted > 0
    assert not any(CLOSING in ln for ln in r.lines)
    assert any(f"+{r.unlisted} distinct messages not listed" in ln for ln in r.lines)
    assert r.shown + r.listed + r.unlisted == len(emap.messages)


def test_partial_parse_never_claims_the_whole(state_home, workspace_dir, monkeypatch):
    import ctx.digest.base as base
    from ctx.retrieval import Selector, get

    monkeypatch.setattr(base, "MAX_PARSE_BYTES", 30_000)
    rows = [f"INFO line {i} " + "x" * 40 for i in range(2000)]
    rows[10] = "ERROR early thing failed"
    rows[-1] = "FATAL: the real cause at the very end"
    digest, rid, ws, store = _digest(workspace_dir, _log(rows))
    head = next(ln for ln in digest.splitlines() if ln.startswith("evidence map"))
    assert "later lines are not in this map" in head and "stdout lines 1-" in head
    assert CLOSING not in digest
    assert "lines past the parse cap are not mapped" in digest
    assert E.map_supported(digest)
    cmd = re.search(r"map them: ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", digest)
    out = get(store, ws, cmd.group(1), Selector(span=cmd.group(2)))
    for _ in range(6):                    # each part is one parse cap long
        if "FATAL: the real cause" in out:
            break
        nxt = re.search(r"next part: ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", out)
        out = get(store, ws, nxt.group(1), Selector(span=nxt.group(2)))
    assert "L2000  FATAL: the real cause at the very end" in out
    assert "later lines are not in this listing" not in out


def test_unlisted_messages_have_a_command_that_lists_them(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    rows = [f"INFO request {i} handled" for i in range(1500)]
    names = [f"unit{c}{d}" for c in "abcdefghijklmnopqrst" for d in "xyz"]
    for j, name in enumerate(names):
        rows[10 + j * 20] = f"ERROR {name.replace('unit', 'unit-')} crashed hard, giving up now"
    digest, rid, ws, store = _digest(workspace_dir, _log(rows))
    m = ACCOUNT_RE.search(digest)
    assert _n(m.group(5)) > 0
    cmd = re.search(r"ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", digest)
    assert cmd, digest
    out = get(store, ws, cmd.group(1), Selector(span=cmd.group(2)))
    assert f"{len(names)} distinct" in out
    more = re.search(r"next: ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", out)
    assert more                                          # paged, 40 rows a page
    out += get(store, ws, more.group(1), Selector(span=more.group(2)))
    for name in names:
        assert name.replace("unit", "unit-") in out


# ---------------------------------------------------------------- cut lines
def test_long_lines_are_windowed_on_the_signal_and_every_cut_is_marked():
    rows = ["INFO ok"] * 300
    rows[100] = "x" * 220 + " FATAL: database corrupted " + "y" * 200
    ctx = _view_ctx(_log(rows))
    text = "\n".join(E.render(ctx, E.build(ctx), cap_bytes=1500).lines)
    shown = next(ln for ln in text.splitlines() if ln.startswith("    L101: "))
    assert "FATAL: database corrupted" in shown
    assert shown.split("L101: ")[1].startswith("… +") and "chars" in shown
    assert shown.rstrip().endswith("chars")
    rows[100] = "ERROR " + "z" * 300
    ctx = _view_ctx(_log(rows))
    text = "\n".join(E.render(ctx, E.build(ctx), cap_bytes=1500).lines)
    assert re.search(r"L101: ERROR z+ … \+\d+ chars", text)


# ---------------------------------------------------------------- redaction, cap
def test_secret_inside_a_sample_line_is_masked(state_home, workspace_dir):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    rows = [f"INFO line {i} " + "x" * 40 for i in range(300)]
    rows[150] = f"ERROR auth failed for token={token} retrying"
    rows[151] = "INFO dsn postgres://admin:SuperSecretPw99@db.internal/app"
    digest, _, _, _ = _digest(workspace_dir, _log(rows))
    assert "evidence map (exact)" in digest
    assert token not in digest and token[:12] not in digest
    assert "SuperSecretPw99" not in digest
    assert "redaction: applied" in digest and "url-credentials" in digest


def test_excerpt_redaction_happens_before_the_cut_and_count():
    token = "ghp_" + "Q" * 36
    rows = ["INFO ok"] * 200
    rows[50] = f"ERROR bad token {token} rejected"
    ctx = _view_ctx(_log(rows))
    r = E.render(ctx, E.build(ctx), cap_bytes=1500)
    text = "\n".join(r.lines)
    assert token not in text and "github-token" in r.fired


def test_byte_cap_bounds_excerpts_and_list():
    rows = ["INFO ok " + "y" * 100] * 400
    for j in range(40):
        rows[5 + j * 9] = f"ERROR component{chr(97 + j % 26)}{chr(97 + j // 26)} failed: " + "z" * 120
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    for cap in (400, 800, 1500):
        r = E.render(ctx, emap, cap_bytes=cap)
        body = [ln for ln in r.lines if ln.startswith("    ") or ln.startswith("  stdout L")]
        assert sum(len(ln.encode()) + 1 for ln in body) <= cap
        assert r.shown + r.listed + r.unlisted == len(emap.messages)
    assert E.map_cap(SimpleNamespace(evidence_map_bytes=10)) == 400
    assert E.map_cap(SimpleNamespace(evidence_map_bytes=10**9)) == 8000


@pytest.mark.parametrize("shape", ["casc", "wide"])
def test_outer_budget_never_leaves_an_unsupported_claim(state_home, workspace_dir, shape):
    if shape == "casc":
        rows = [f"2025-02-11 09:{i % 60:02d}:00 INFO tick {i} nominal" for i in range(3000)]
        rows[200] = "ERROR bootstrap: cannot read /etc/app/app.yml: no such file"
        for j in range(40):
            rows[2000 + j * 25] = f"ERROR module m{chr(97 + j % 26)}{j} failed to initialise"
    else:
        rows = [("ERROR module " if i % 8 == 1 else "2025-01-01 INFO ")
                + " ".join(f"w{(i * 31 + k * 7) % 97}x{k}" for k in range(30)) for i in range(2000)]
    digest, _, _, _ = _digest(workspace_dir, _log(rows))
    assert E.map_supported(digest)
    for tokens in range(40, 1400, 23):
        out = E.deliver(digest, tokens)
        assert E.map_supported(out), (tokens, out)
        if "evidence map (exact" in out:
            sec = E.map_section(digest)
            assert "\n".join(sec[0][sec[1]:sec[2]]) in out      # the map whole


def test_failure_and_success_paths_share_one_budget(state_home, workspace_dir):
    rows = [f"INFO line {i} " + "x" * 60 for i in range(3000)]
    for j in range(60):
        rows[7 + j * 40] = f"ERROR subsystem s{j % 30}x failed to start"
    ok, _, ws, _ = _digest(workspace_dir, _log(rows))
    bad, _, _, _ = _digest(workspace_dir, _log(rows), is_error=True)
    assert "evidence map (exact" in ok
    limit = ws.config.budgets.digest_tokens * 4 + E.budget_tokens(ws, ok) * 4 + 8
    assert len(ok.encode()) <= limit and len(bad.encode()) <= limit
    assert E.map_supported(ok) and E.map_supported(bad)


def test_small_outputs_get_no_map(state_home, workspace_dir):
    digest, _, _, _ = _digest(workspace_dir, "INFO a\nERROR b failed\n")
    assert "evidence map" not in digest


# ---------------------------------------------------------------- other fixes
def test_timestamp_prefixes_are_not_top_files(state_home, workspace_dir):
    rows = [f"2025-02-11 09:{i // 60:02d}:{i % 60:02d} ERROR thing {i % 3} broke" for i in range(300)]
    rows += [f"src/app{i}.c:{i}:1: error: bad" for i in range(5)]
    digest, _, _, _ = _digest(workspace_dir, _log(rows))
    assert "2025-02-11 09  " not in digest
    assert "src/app0.c" in digest


def test_cards_state_the_digest_guarantee_without_growing():
    from ctx.installer import _render_claude_md, _render_codex_file

    root = Path(__file__).resolve().parent.parent
    claude = _render_claude_md("ctx")
    codex = _render_codex_file("AGENTS.md", "ctx")
    skill = (root / "plugins/antigravity/skills/ctx-harness/SKILL.md").read_text(encoding="utf-8")
    for card in (claude, codex, skill):
        flat = " ".join(card.split())
        assert "verbatim with real line numbers" in flat
        assert '"every error-like line is one of these N messages" is true' in flat
        assert '"varies" message stands for several values, listed on its row or by its command' in flat
        assert "no others exist" not in flat
        assert "not shown" in flat
    assert "Reuse ranks above stdlib deliberately" in skill
    assert len(claude) <= 1092
    assert len(codex.encode()) <= 2183
    assert len(skill.encode()) <= 11438


# ---------------------------------------------------------------- varies (round 3)
def _codes_log():
    rows = [f"2025-03-01 10:{(i // 60) % 60:02d}:{i % 60:02d} INFO worker heartbeat ok"
            for i in range(3000)]
    for j in range(1000):
        rows[2 + j * 3] = f"2025-03-01 10:00:00 ERROR worker exit code {1000 + (j % 499)}"
    rows[2981] = "2025-03-01 10:49:40 ERROR worker exit code 137"
    return _log(rows)


def test_many_valued_slot_shows_its_rare_values_and_a_command_that_lists_its_lines(
        state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    digest, rid, ws, store = _digest(workspace_dir, _codes_log())
    assert "varies: 500 distinct values; seen at most twice:" in digest
    assert "137 ×1 L2982" in digest
    m = re.search(r"all (?:its lines|lines of L\d+): ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", digest)
    assert m
    nxt = digest.split("\nnext:\n", 1)[1]
    assert f"ctx get {m.group(1)} --span {m.group(2)}" in nxt        # top collapsed in next:
    out = get(store, ws, m.group(1), Selector(span=m.group(2)))
    assert "message lines (exact): 1,000" in out
    seen = set(re.findall(r"^L(\d+): ", out, re.M))
    for _ in range(30):
        more = re.search(r"next: ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", out)
        if not more:
            break
        out = get(store, ws, more.group(1), Selector(span=more.group(2)))
        seen |= set(re.findall(r"^L(\d+): ", out, re.M))
    assert len(seen) == 1000 and "2982" in seen


def test_few_valued_collapsed_slot_prints_every_value():
    rows = ["2025-03-01 10:00:00 INFO applying batch ok"] * 2000
    rows[9] = "2025-03-01 10:00:09 ERROR migration 0041 failed"
    rows[54] = "2025-03-01 10:00:54 ERROR migration 0042 failed"
    rows[89] = "2025-03-01 10:01:29 ERROR migration 0043 failed"
    ctx = _view_ctx(_log(rows))
    text = "\n".join(E.render(ctx, E.build(ctx), cap_bytes=1500).lines)
    assert "varies: 0041 ×1 L10 · 0042 ×1 L55 · 0043 ×1 L90" in text
    assert "distinct values)" not in text


@pytest.mark.parametrize("n", [8, 9])
def test_the_8_9_boundary(n):
    rows = ["INFO ok"] * 400
    for j in range(n):
        rows[10 + j * 30] = f"ERROR job j{j:02d}x aborted"
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    text = "\n".join(E.render(ctx, emap, cap_bytes=1500).lines)
    assert len(emap.messages) == 1
    if n == 8:
        for j in range(n):
            assert f"j{j:02d}x ×1 L{11 + j * 30}" in text
    else:
        assert "varies: 9 distinct values; seen at most twice: j00x ×1 L11" in text
        assert "(+6 more)" in text


def test_a_quoted_string_is_one_value():
    rows = ['ts=2025-03-01T10:00:00Z level=info msg="tick ok"'] * 2000
    for j in range(30):
        rows[10 + j * 50] = 'ts=2025-03-01T10:00:00Z level=error msg="quota exceeded" svc=api'
    rows[1990] = 'ts=2025-03-01T10:00:00Z level=error msg="deadline exceeded" svc=api'
    emap = E.build(_view_ctx(_log(rows)))
    assert len(emap.messages) == 2
    assert E.skeleton('level=error msg="quota exceeded" svc=api')[1] == \
        ("level=error", 'msg="quota exceeded"', "svc=api")
    assert E.skeleton("Can't resolve './x' here")[1] == ("Can't", "'./x'")


def test_rows_never_print_one_distinct_value_or_an_empty_cut():
    rows = ["INFO ok"] * 600
    for j in range(40):
        rows[5 + j * 13] = (f"FAILED tests/test_cache.py::test_case_number_{j}_with_a_long_name "
                            f"in shard {j % 2}")
    ctx = _view_ctx(_log(rows))
    text = "\n".join(E.render(ctx, E.build(ctx), cap_bytes=1500).lines)
    assert "(1 distinct values)" not in text
    assert "+0 chars" not in text
    assert E._shown_line("x" * 50, 32, set()) == "x" * 32 + " … +18 chars"


def test_8_bit_csi_is_stripped():
    assert E.strip_ansi("\x9b31mERROR\x9b0m boom") == "ERROR boom"
    rows = ["INFO ok"] * 300
    rows[50] = "\x9b31mERROR\x9b0m disk failed"
    ctx = _view_ctx(_log(rows))
    text = "\n".join(E.render(ctx, E.build(ctx), cap_bytes=1500).lines)
    assert "\x9b" not in text and "L51: ERROR disk failed" in text


def test_a_budget_too_small_for_the_map_says_so(state_home, workspace_dir):
    digest, _, _, _ = _digest(workspace_dir, _codes_log())
    out = E.deliver(digest, 20)
    assert "evidence map omitted: budget" in out and "evidence map (exact" not in out


def test_url_credentials_with_an_empty_user_are_redacted():
    from ctx.textutil import REDACTION_PATTERNS, redact

    out, fired = redact("cache redis://:hunter2pass@cache:6379/0 up", tuple(REDACTION_PATTERNS))
    assert "hunter2pass" not in out and fired == ["url-credentials"]
    assert redact("see http://host:8080/x", tuple(REDACTION_PATTERNS))[1] == []
