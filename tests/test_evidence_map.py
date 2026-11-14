"""The evidence map: every error-like line of a large output accounted for.

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
    r"^evidence map \(exact\): ([\d,]+) error-like lines · ([\d,]+) distinct messages · "
    r"([\d,]+) shown with context · ([\d,]+) listed · ([\d,]+) not listed$", re.M)


def _n(s: str) -> int:
    return int(s.replace(",", ""))


def _view_ctx(text: str, focus=()):
    view = StreamView("stdout", len(text.encode()), text.count("\n"), "text/plain", text, True)
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


# ---------------------------------------------------------------- grouping
def test_template_masks_variable_parts_and_keeps_words():
    k = E.message_key
    assert k("2025-02-11 09:00:13 ERROR deadlock detected on items, transaction rolled back") == \
        k("2025-02-11 10:59:01 ERROR deadlock detected on items, transaction rolled back")
    # a different word is a different message
    assert k("2025-02-11 09:00:13 ERROR deadlock detected on items") != \
        k("2025-02-11 09:00:13 ERROR deadlock detected on users")
    # path tails, numbers and quoted values with digits collapse
    assert k("src/cache/cache_55.c:12:3: error: implicit declaration of function 'helper_1'") == \
        k("src/cache/cache_9.c:140:7: error: implicit declaration of function 'helper_77'")
    assert k("job 0x1f3a failed id=5f0c2b8e-1d2a-4b3c-8d9e-0a1b2c3d4e5f") == \
        k("job 0xbeef failed id=11111111-2222-3333-4444-555555555555")
    # quoted words without digits are content, not a variable
    assert k('could not create unique index "users_email_key"') != \
        k('could not create unique index "orders_pkey"')


def test_fast_signal_scan_equals_the_per_line_ranking_on_every_fixture():
    for item in ITEMS:
        text = (FIX / item["file"]).read_text(encoding="utf-8")
        lines = index_lines(text)
        full = [(i, r) for i, ln in enumerate(lines, 1) if (r := _signal_rank_full(ln)) > 0]
        assert E.signal_rows(text, lines) == full, item["file"]
        assert all(_signal_rank(ln) == _signal_rank_full(ln) for ln in lines)
    tricky = ["no errors found", "0 failures", "FAİLED here", "exit code 3", "HTTP/1.1 502 Bad Gateway",
              "status: 503", "Traceback (most recent call last):", "Error: boom", "npm ERR! x",
              'File "a.py", line 3', "quota exceeded", "ok", "ÉRROR ünicode error"]
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
    # recomputed independently of the renderer
    lines = index_lines(text)
    sig = [ln for ln in lines if _signal_rank_full(ln) > 0]
    assert signal == len(sig)
    assert distinct == len({E.message_key(ln) for ln in sig})
    assert shown + listed + unlisted == distinct
    assert listed == len(_listed_rows(digest))
    assert ("all %s distinct messages are above." % f"{distinct:,}" in digest) == (unlisted == 0)
    # at least one decisive line is among the verbatim excerpts, with its
    # real line number
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
    assert "L4" in digest or "migration 0042_add_unique_email failed" in digest


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
def _log(rows):
    return "\n".join(rows) + "\n"


def test_rarer_outranks_frequent_and_higher_tier_outranks_rarer():
    rows = [f"2025-01-01 10:00:{i % 60:02d} INFO tick {i}" for i in range(400)]
    for i in range(10, 390, 3):
        rows[i] = f"2025-01-01 10:00:00 ERROR cache miss storm on shard {i}"
    rows[200] = "2025-01-01 10:00:00 ERROR migration 7 failed"           # tier 1, once
    rows[300] = "FATAL: disk full on /dev/sda1"                           # tier 3, once
    emap = E.build(_view_ctx(_log(rows)))
    order = [m.first for m in emap.messages]
    assert order[0] == 301          # tier 3 first
    assert order[1] == 201          # then the rare tier-1 message
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


# ---------------------------------------------------------------- the sentence
def test_all_messages_sentence_only_when_true():
    few = ["INFO ok"] * 50 + ["ERROR one failed", "ERROR two failed"]
    ctx = _view_ctx(_log(few))
    r = E.render(ctx, E.build(ctx), cap_bytes=1500)
    assert r.unlisted == 0 and any("all 2 distinct messages are above." in ln for ln in r.lines)

    many = ["INFO ok"] * 50 + [f"ERROR subsystem{w} failed" for w in
                                ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
                                 "nu xi omicron pi rho sigma tau upsilon").split()]
    ctx = _view_ctx(_log(many))
    r = E.render(ctx, E.build(ctx), cap_bytes=1500)
    assert r.unlisted > 0
    assert not any("distinct messages are above" in ln for ln in r.lines)
    assert any(f"+{r.unlisted} distinct messages not listed" in ln for ln in r.lines)
    assert r.shown + r.listed + r.unlisted == 20


def test_unlisted_messages_have_a_command_that_lists_them(state_home, workspace_dir):
    from ctx.retrieval import Selector, get

    rows = [f"INFO request {i} handled" for i in range(600)]
    names = [f"worker{c}{d}" for c in "abcdefgh" for d in "xyz"]
    for j, name in enumerate(names):
        rows[10 + j * 20] = f"ERROR {name.replace('worker', 'unit-')} crashed hard"
    digest, rid, ws, store = _digest(workspace_dir, _log(rows))
    m = ACCOUNT_RE.search(digest)
    assert _n(m.group(5)) > 0
    cmd = re.search(r"ctx get (run:[0-9a-f]+#stdout) --span ([0-9a-f]+)", digest)
    assert cmd, digest
    out = get(store, ws, cmd.group(1), Selector(span=cmd.group(2)))
    assert f"messages (exact): {len(names)} distinct" in out
    for name in names:
        assert name.replace("worker", "unit-") in out


# ---------------------------------------------------------------- redaction, cap
def test_secret_inside_a_sample_line_is_masked(state_home, workspace_dir):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    rows = [f"INFO line {i} " + "x" * 40 for i in range(300)]
    rows[150] = f"ERROR auth failed for token={token} retrying"
    for i in range(20, 280, 7):
        rows[i] = f"ERROR auth failed for token={token} at step {i}"
    digest, _, _, _ = _digest(workspace_dir, _log(rows))
    assert "evidence map (exact)" in digest
    assert token not in digest
    assert token[:12] not in digest
    assert "[ctx:redacted:github-token" in digest or "ctx:redacted" in digest
    assert "redaction: applied" in digest


def test_byte_cap_bounds_excerpts_and_list():
    rows = ["INFO ok " + "y" * 100] * 400
    for j in range(40):
        rows[5 + j * 9] = f"ERROR component{chr(97 + j % 26)}{j // 26} failed: " + "z" * 120
    ctx = _view_ctx(_log(rows))
    emap = E.build(ctx)
    for cap in (300, 800, 1500):
        r = E.render(ctx, emap, cap_bytes=cap)
        body = [ln for ln in r.lines if ln.startswith("    ") or ln.startswith("  stdout L")]
        assert sum(len(ln.encode()) + 1 for ln in body) <= cap
        assert r.shown + r.listed + r.unlisted == len(emap.messages)


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
        assert "all N distinct messages are above" in flat
        assert "not shown" in flat
    # sizes on main before this line was added
    assert len(claude) <= 1092
    assert len(codex.encode()) <= 2183
    assert len(skill.encode()) <= 11438
