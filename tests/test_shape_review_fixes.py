"""Regression tests for the review of the paste-shape profiles: redaction that
cannot be defeated by the quoting helper, no exception on any paste, honest
delimiter / distinct / diff / trace claims. Each test fails when the property
it names is removed (see the mutation list in the commit messages)."""

from __future__ import annotations

import csv
import difflib
import io
import random
import string

import pytest
from conftest import make_store, make_ws
from shape_helpers import (
    assert_in_budget, assert_reader_safe, csv_text, next_commands, padded, python_trace, unified_diff,
)

from ctx import digest
from ctx.digest import delimprof, shapeutil, traceprof, udiffprof
from ctx.digest.base import INTAKE_TOOL, DigestContext
from ctx.digest.shapeutil import Quoter, inert
from ctx.intake import intake_bytes
from ctx.textutil import REDACTION_PATTERNS


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def take(env, text: str):
    ws, store = env
    return intake_bytes(store, ws, text.encode("utf-8"), label="paste")


def policy(env):
    return env[0].config.redaction


ZW = "\u200b"           # zero-width space: removed by the cleaner
BIDI = "\u202e"
BODY = string.ascii_letters + string.digits


def secret(rnd: random.Random, kind: str) -> str:
    if kind == "ghp":
        return "ghp_" + "".join(rnd.choice(BODY) for _ in range(36))
    if kind == "pat":
        return "github_pat_" + "".join(rnd.choice(BODY + "_") for _ in range(82))
    if kind == "aws":
        return "AKIA" + "".join(rnd.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    if kind == "sk":
        return "sk-" + "".join(rnd.choice(BODY) for _ in range(30))
    return "eyJ" + "".join(rnd.choice(BODY) for _ in range(30)) + "." + "".join(
        rnd.choice(BODY) for _ in range(20)) + "." + "".join(rnd.choice(BODY) for _ in range(20))


KINDS = ("ghp", "pat", "aws", "sk", "jwt")


def leaked(digest_text: str, planted: str, window: int = 8) -> str | None:
    """A piece of ``planted`` (past its fixed prefix) that appears in the digest."""
    body = planted.split("_", 1)[-1] if planted.startswith(("ghp_", "github_pat_")) else planted[4:]
    for i in range(0, len(body) - window + 1):
        if body[i:i + window] in digest_text:
            return body[i:i + window]
    return None


# ================================================================== item 1
def test_quoter_cannot_be_built_without_a_redaction_policy():
    with pytest.raises(TypeError):
        Quoter(40)           # type: ignore[call-arg]
    with pytest.raises(TypeError):
        inert("x", 40)       # type: ignore[call-arg]


def test_cleaner_characters_around_and_before_a_token_do_not_defeat_redaction(env):
    pol = policy(env)
    rnd = random.Random(1)
    for kind in KINDS:
        tok = secret(rnd, kind)
        for text in (f"token=x{ZW}{tok}", f"token={ZW}{tok}", f"x{BIDI}{tok}{ZW}y", f"{tok}{ZW}",
                     f"a {tok}", f'"{tok}"'):
            out = inert(text, 160, pol)
            assert leaked(out, tok) is None, (kind, text, out)
            assert "redacted" in out


def test_a_token_assembled_by_the_cleaner_is_redacted_too(env):
    pol = policy(env)
    rnd = random.Random(2)
    for kind in KINDS:
        tok = secret(rnd, kind)
        for cut in (5, 9, 14, len(tok) // 2):
            broken = tok[:cut] + ZW + tok[cut:]           # not a token until the ZWSP is gone
            out = inert(f"k {broken}", 160, pol)
            assert leaked(out, tok) is None, (kind, cut, out)


@pytest.mark.parametrize("limit", [12, 24, 60, 110, 160])
def test_a_token_straddling_the_clip_boundary_at_every_offset_leaves_no_fragment(env, limit):
    pol = policy(env)
    rnd = random.Random(3)
    for kind in KINDS:
        tok = secret(rnd, kind)
        for offset in range(0, 260):
            out = inert("." * offset + " " + tok, limit, pol)
            assert leaked(out, tok, window=6) is None, (kind, limit, offset, out)


def test_a_token_across_the_storage_window_leaves_no_fragment(env):
    pol = policy(env)
    rnd = random.Random(4)
    tok = secret(rnd, "ghp")
    for offset in range(shapeutil.WINDOW - 60, shapeutil.WINDOW + 10, 3):
        text = ("." * offset + " " + tok)[: shapeutil.FIELD_KEEP]
        out = Quoter(160, pol)(text, truncated=len(text) >= shapeutil.FIELD_KEEP)
        assert leaked(out, tok, window=6) is None, (offset, out)


def test_the_reviewers_cases_end_to_end(env):
    rnd = random.Random(5)
    ghp, pat, aws = secret(rnd, "ghp"), secret(rnd, "pat"), secret(rnd, "aws")
    # trace message with a zero-width space in front of the token
    r = take(env, padded(python_trace(final=f"ValueError: token=x{ZW}{ghp}")))
    assert r.profile == "trace/v1" and leaked(r.digest, ghp) is None and "redaction: applied" in r.digest
    # token near character 400 of a trace line
    r = take(env, padded(python_trace(final="ValueError: " + "z" * 385 + " " + ghp)))
    assert leaked(r.digest, ghp, window=6) is None
    # CSV: a 93-character fine-grained token, and a space-padded AWS key
    rows = csv_text(300).splitlines()
    rows[0] += ",key,other"
    rows[1:] = [ln + f",{pat},   {aws}   " for ln in rows[1:]]
    r = take(env, "\n".join(rows) + "\n")
    assert r.profile == "delimited/v1"
    assert leaked(r.digest, pat, window=6) is None and leaked(r.digest, aws, window=6) is None


def test_values_used_as_distinct_keys_never_leak_through_the_top_values(env):
    rnd = random.Random(6)
    toks = [secret(rnd, k) for k in ("ghp", "aws")]
    out = ["id,token,n"] + [f"{i},{toks[i % 2]},{i}" for i in range(300)]
    r = take(env, "\n".join(out) + "\n")
    assert r.profile == "delimited/v1" and "2 distinct" in r.digest
    for t in toks:
        assert leaked(r.digest, t, window=6) is None


@pytest.mark.parametrize("seed", range(25))
def test_planted_secrets_never_appear_in_any_digest(env, seed):
    """Property: secrets planted at generated positions, with cleaner
    characters generated around and inside them, in every kind of paste and
    every field each profile renders -- no 8-character piece survives."""
    rnd = random.Random(1000 + seed)
    planted: list[str] = []

    def plant(prefix_pad: int | None = None) -> str:
        tok = secret(rnd, rnd.choice(KINDS))
        planted.append(tok)
        style = rnd.randrange(5)
        pad = " " * rnd.randrange(0, 4)
        if style == 1:
            return f"x{ZW}{tok}"
        if style == 2:
            cut = rnd.randrange(5, len(tok) - 5)
            return tok[:cut] + rnd.choice([ZW, BIDI]) + tok[cut:]
        if style == 3:
            return f"{pad}{tok}{ZW}"
        return "." * (rnd.randrange(0, 90) if prefix_pad is None else prefix_pad) + " " + tok

    which = seed % 3
    if which == 0:        # stack trace: message, path, function
        text = python_trace(recursion=rnd.randrange(0, 30), final=f"ValueError: {plant()}",
                            user=f"/app/{plant().replace(' ', '/')}/core.py")
        text = text.replace("walk", "w " + plant()).replace("<module>", plant())
        text = padded(text)
    elif which == 1:      # delimited data: header, first rows, last row, sampled rows
        rows = [["id", plant(), "city", "n"]]
        for i in range(rnd.randrange(150, 400)):
            row = [str(i), f"name{i}", plant() if rnd.random() < 0.15 or i in (0, 1) else "Oslo", str(i)]
            rows.append(row)
        rows[-1][2] = plant()
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        text = buf.getvalue()
    else:                 # plain diff: paths
        text = unified_diff(10, sizes=[rnd.randrange(5, 80) for _ in range(10)])
        for i in rnd.sample(range(10), 4):
            text = text.replace(f"src/mod{i}.py", f"src/{plant().replace(' ', '/')}/mod{i}.py")
    r = take(env, text)
    assert r.profile in ("trace/v1", "delimited/v1", "udiff/v1"), r.digest
    assert_in_budget(env[0], r)
    assert_reader_safe(r)
    for tok in planted:
        assert leaked(r.digest, tok, window=8) is None, (seed, tok, r.digest)


# ================================================================== item 2
def test_unicode_digits_never_raise(env):
    sup = "\u00b9\u00b2"
    cases = [
        padded(python_trace(final="ValueError: x")).replace("line 12", f"line {sup}"),
        "Error: x\n" + f"    at f (/a.js:{sup}:3)\n" * 200,
        "Traceback (most recent call last):\n" + '  File "/a.py", line 3, in f\n    g()\n' * 150
        + f"  [Previous line repeated {sup} more times]\nValueError: x\n",
        'Exception in thread "main" a.B: x\n' + "\tat a.C.d(C.java:1)\n" * 200 + f"\t... {sup} more\n",
        unified_diff(10).replace("@@ -", f"@@ -{sup},", 3) + "@@ -1,\u0663 +1 @@\n",
        "--- a\n+++ b\n@@ -1," + sup + " +1 @@\n" + " x\n" * 2000,
        "thread 'main' panicked at src/a.rs:" + sup + ":4:\nboom\nstack backtrace:\n" + f"  {sup}: f\n" * 300,
        "\n".join(f"{i},{sup},\u0663\u0664" for i in range(400)),
    ]
    for text in cases:
        r = take(env, text)
        assert r.digest and len(r.digest.encode()) <= env[0].config.budgets.digest_tokens * 4 + 4096


def _frag_pool() -> list[str]:
    return [
        "Traceback (most recent call last):", '  File "', '  File "x.py", line \u00b2, in f', "    at ", "\tat a.B.c(D.java:\u00b9)",
        "    at f (/a.js:\u0663:\u0664)", "\t... \u00b2 more", "Caused by: ", "\tSuppressed: ", "panic: ", "goroutine 1 [running]:",
        "\t/x.go:\u00b9 +0x1", "thread 'main' panicked at ", "stack backtrace:", "   \u00b2: f", "             at x:\u00b9:1",
        "@@ -1,\u00b2 +1 @@", "--- a", "+++ b", "-x", "+y", " z", "\\ No newline", "Binary files a and b differ",
        "diff --git a/x b/y", "rename from x", "GIT binary patch", "a,b,c", "1;2;3", "x\ty\tz", "\"unterminated", "\x00\x01",
        "\u202e\u200b\ufeff", "\ud800" if False else "\u2028", "[Previous line repeated \u00b2 more times]",
    ]


@pytest.fixture()
def gates_open(monkeypatch):
    """Let every profile look at small payloads too, so the fuzz reaches them all."""
    for mod in (traceprof, delimprof, udiffprof):
        monkeypatch.setattr(mod, "inline_limit", lambda ctx: 0)


@pytest.mark.parametrize("seed", range(40))
def test_arbitrary_text_and_byte_noise_never_raises_in_detect_or_render(env, gates_open, seed):
    ws, store = env
    rnd = random.Random(seed)
    pool = _frag_pool()
    if seed % 4 == 0:
        text = rnd.randbytes(rnd.randrange(50, 6000)).decode("utf-8", "replace")
    elif seed % 4 == 1:
        text = "".join(chr(rnd.choice([rnd.randrange(32, 0x2fff), rnd.randrange(0x2000, 0x2100), 0x00b2, 0x0663])) for _ in range(rnd.randrange(50, 3000)))
    else:
        text = "\n".join(rnd.choice(pool) + rnd.choice(["", "1", ":2:3", ")", " (a.js:\u00b2:1)", ",", ";", "|"])
                         for _ in range(rnd.randrange(20, 600)))
    manifest = digest.synthesize_manifest(store, ws, INTAKE_TOOL, text, "", argv=[INTAKE_TOOL, "label:paste"])
    ctx = DigestContext.load(store, ws, manifest, focus="")
    for profile in (traceprof.StackTraceProfile(), delimprof.DelimitedDataProfile(), udiffprof.UnifiedDiffProfile()):
        if profile.detect(ctx):
            out = profile.render(ctx)
            assert isinstance(out, str) and "next:" in out or "next" not in out
    # and the whole path, where a raise would surface to the user
    take(env, text)


def test_parsers_never_raise_on_unicode_digit_lookalikes():
    for text in ("\u00b2", "\u0663", "\uff11", "\u2460", "\u0967"):
        traceprof.parse(f'Traceback (most recent call last):\n  File "a", line {text}, in f\n  [Previous line repeated {text} more times]\n    at f (a:{text}:{text})\n\t... {text} more\nX: y\n')
        udiffprof.parse(f"--- a\n+++ b\n@@ -{text},{text} +{text} @@\n x\n")
        delimprof.sniff(f"a,{text}\n" * 30)


# ================================================================== item 3
def eu_csv(rows: int = 300, header: bool = True) -> str:
    rnd = random.Random(7)
    out = ["Name;Preis;Menge"] if header else []
    out += [f"Artikel {i};{rnd.randrange(1, 999)},{rnd.randrange(10, 99)};{rnd.randrange(1, 9)},{rnd.randrange(1, 9)}"
            for i in range(rows)]
    return "\n".join(out) + "\n"


def test_semicolon_file_with_decimal_commas_is_read_as_semicolon(env):
    r = take(env, eu_csv())
    assert r.profile == "delimited/v1"
    assert "delimited: semicolon · 3 columns" in r.digest
    assert '"Name" | "Preis" | "Menge"' in r.digest
    assert "300 data rows" in r.digest
    assert "the more consistent reading" in r.digest        # and it is true: 300/301 vs 301/301


def test_delimiter_ranking_compares_fractions_exactly():
    text = eu_csv()
    found = delimprof.sniff(text)
    assert [c.delimiter for c in found][0] == ";"
    # 99.67% must not tie with 100% (flooring a negative did exactly that)
    semi = next(c for c in found if c.delimiter == ";")
    comma = next(c for c in found if c.delimiter == ",")
    assert semi.consistent * comma.rows > comma.consistent * semi.rows


# ================================================================== item 4
def test_distinct_values_are_counted_on_the_whole_value(env):
    out = ["url,n"] + [f"https://example.com/a/very/long/path/that/shares/a/prefix/{i},{i}" for i in range(300)]
    r = take(env, "\n".join(out) + "\n")
    assert r.profile == "delimited/v1"
    assert "statistics: all 300 data rows" in r.digest
    assert "300 distinct" in r.digest and "1 distinct" not in r.digest
    # past the stored length the digest of the value, not a prefix, tells values apart
    long_ = ["id,u"] + [f"{i},{'q' * 400}{i}" for i in range(300)]
    assert "300 distinct" in take(env, "\n".join(long_) + "\n").digest


def test_a_count_is_only_called_exact_when_it_is_the_sample_that_is_counted(env):
    big = take(env, csv_text(60_000))
    assert "SAMPLE" in big.digest
    assert "distinct in the sample" in big.digest or "distinct: " in big.digest
    small = take(env, csv_text(300))
    assert "in the sample" not in small.digest and "SAMPLE" not in small.digest


# ================================================================== item 5
def java_suppressed(n: int = 40) -> str:
    out = ['Exception in thread "main" java.lang.RuntimeException: outer']
    out += [f"\tat a.B{i}.c(B{i}.java:{i + 1})" for i in range(n)]
    out += ["\tSuppressed: java.io.IOException: close failed"] + [f"\t\tat a.C{i}.close(C{i}.java:{i + 1})" for i in range(10)]
    out += ["\t\t... 30 more", "Caused by: java.lang.NullPointerException: npe"]
    out += [f"\tat a.D{i}.d(D{i}.java:{i + 1})" for i in range(10)] + ["\t... 30 more"]
    return "\n".join(out) + "\n"


def test_suppressed_exceptions_are_siblings_not_causes(env):
    r = take(env, java_suppressed(150))
    assert r.profile == "trace/v1"
    d = r.digest
    assert "chain: 2 links" in d
    assert 'link 2 (the cause of link 1) "java.lang.NullPointerException: npe"' in d
    assert 'suppressed under link 1: "java.io.IOException: close failed"' in d
    assert "siblings" in d and "not causes" in d
    assert "innermost frames of link 2 of 2, the root cause" in d     # the NPE's frames, not the suppressed ones
    assert "a.D0.d" in d and "a.C0.close" not in d


def test_kotlin_diagnostic_suppressed_exception_is_not_the_root_cause(env):
    lines = ['Exception in thread "DefaultDispatcher-worker-1" java.lang.IllegalStateException: boom']
    lines += [f"\tat com.app.Repo{i}.load(Repo.kt:{i + 3})" for i in range(60)]
    lines += ["\tSuppressed: kotlinx.coroutines.internal.DiagnosticCoroutineContextException: [StandaloneCoroutine]"]
    lines += [f"\t\tat kotlinx.coroutines.scheduling.X{i}.run(X.kt:{i})" for i in range(40)]
    r = take(env, "\n".join(lines) + "\n")
    assert r.profile == "trace/v1"
    assert "root cause" not in r.digest
    assert "suppressed under link 1" in r.digest
    assert "com.app.Repo0.load" in r.digest and "scheduling.X" not in r.digest


def test_a_later_top_level_cause_is_not_attached_to_the_suppressed_exception(env):
    p = traceprof.parse(java_suppressed(150))
    tr = p.traces[0]
    assert [s.etype for s in tr.segs] == ["java.lang.RuntimeException", "java.lang.NullPointerException"]
    assert [(i, s.etype) for i, s in tr.nested] == [(0, "java.io.IOException")]


def test_java_more_lines_are_rendered_not_swallowed(env):
    r = take(env, java_suppressed(150))
    assert "30 frames shared with the enclosing trace" in r.digest
    assert "30 more the JVM elided as shared with the enclosing trace" in r.digest


# ================================================================== item 6
def test_binary_and_mode_only_entries_are_files(env):
    text = "".join(f"Binary files a/img{i}.png and b/img{i}.png differ\n" for i in range(20)) + unified_diff(3, sizes=[20, 30, 40])
    r = take(env, text)
    assert r.profile == "udiff/v1"
    assert "diff: 23 files" in r.digest and "/23 files detailed" in r.digest
    assert "binary" in r.digest
    git = "".join(f"diff --git a/m{i}.sh b/m{i}.sh\nold mode 100644\nnew mode 100755\n" for i in range(12))
    git += "diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt\n"
    git += "diff --git a/i.png b/i.png\nindex 1..2 100644\nBinary files a/i.png and b/i.png differ\n"
    parsed = udiffprof.parse(git + "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n")
    by = {f.path: f for f in parsed.files}
    assert len(parsed.files) == 12 + 1 + 1 + 1
    assert by["m0.sh"].status == "mode change only" and by["new.txt"].status == "renamed"
    assert by["i.png"].status == "modified, binary"


def test_a_miscounted_hunk_never_swallows_the_next_files_header(env):
    d = unified_diff(4, sizes=[20, 20, 20, 20])
    lines = d.split("\n")
    first_hunk_of_file0 = max(i for i, ln in enumerate(lines[: [i for i, x in enumerate(lines) if x.startswith("+++ ")][1]]) if ln.startswith("@@ "))
    import re as _re
    m = _re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", lines[first_hunk_of_file0])
    lines[first_hunk_of_file0] = f"@@ -{m.group(1)},{int(m.group(2)) + 6} +{m.group(3)},{int(m.group(4)) + 6} @@"
    r = take(env, "\n".join(lines))
    assert r.profile == "udiff/v1"
    assert "diff: 4 files" in r.digest
    assert "exact" not in r.digest.split("coverage:")[0].split("diff:")[1].split("\n")[0]
    assert "fewer lines than the @@ header says" in r.digest and "cut short" not in r.digest


def test_directory_prefixes_alone_are_not_a_rename(env):
    text = "".join("".join(difflib.unified_diff([f"l{k}\n" for k in range(60)], [f"m{k}\n" if k % 3 == 0 else f"l{k}\n" for k in range(60)],
                                                f"project.orig/src/f{i}.py", f"project/src/f{i}.py")) for i in range(8))
    assert all(f.status == "modified" for f in udiffprof.parse(text).files)
    r = take(env, text)
    assert "renamed" not in r.digest


# ================================================================== item 7
def test_a_preamble_with_another_field_count_is_skipped_and_said(env):
    body = csv_text(300).splitlines()
    text = "# Exported by Tool 3.1\n# Generated 2026-01-02\n# Rows: 300\n" + "\n".join(body) + "\n"
    r = take(env, text)
    assert r.profile == "delimited/v1"
    assert "300 data rows (exact)" in r.digest
    assert "preamble: skipped 3 leading records at L1-L3" in r.digest and '"# Exported by Tool 3.1"' in r.digest
    assert '"id" | "name"' in r.digest and "(header)" in r.digest
    assert '"# Exported' not in r.digest.split("preamble:")[1].split("\n", 1)[1]   # not a column or a row


@pytest.mark.parametrize("name,make", [
    ("c array", lambda: "static const int T[] = {\n" + "\n".join(f"  {i}, {i * 2}, {i * 3}, {i * 4}," for i in range(300)) + "\n};\n"),
    ("sql insert", lambda: "INSERT INTO t VALUES\n" + "\n".join(f"({i}, 'name{i}', {i * 2})," for i in range(300)) + "\n"),
    ("ini", lambda: "\n".join(f"opt{i} = a, b" for i in range(300)) + "\n"),
    ("names", lambda: "\n".join(f"Smith{i}, John" for i in range(300)) + "\n"),
    ("python tuple list", lambda: "\n".join(f"    ({i}, 'a{i}', {i * 2})," for i in range(300)) + "\n"),
    ("requirements", lambda: "\n".join(f"pkg{i}, >=1.{i}" for i in range(300)) + "\n"),
])
def test_source_code_sql_and_config_are_declined(env, name, make):
    assert take(env, make()).profile != "delimited/v1", name


def test_real_tables_with_text_columns_are_still_claimed(env):
    rows = ["id,name,city,note"] + [f"{i},person {i},Oslo,says hello number {i}" for i in range(300)]
    assert take(env, "\n".join(rows) + "\n").profile == "delimited/v1"


# ================================================================== item 8
@pytest.mark.parametrize("make,pattern", [
    (lambda: "Error: x\n" + "".join(f"    at f{i} (/a/b{i}.js:{i + 1}:2)\n" for i in range(150)) + "\n"
     + "Error: y\n" + "".join(f"    at g{i} (/a/c{i}.js:{i + 1}:2)\n" for i in range(150)), "^[A-Za-z0-9_$.]*(Error|Exception)\\b"),
    (lambda: padded(python_trace(chain=False)) + "\n" + padded(python_trace(chain=False)), "Traceback \\(most recent call last\\)"),
])
def test_the_earlier_traces_hint_fits_the_language(env, make, pattern):
    r = take(env, make() + "".join(f"\nError: z{i}\n    at h (/z.js:1:1)\n    at i (/z.js:2:2)\n" for i in range(8))
             if pattern.startswith("^") else make())
    hint = [ln for ln in r.digest.splitlines() if "earlier traces not listed" in ln]
    if hint:
        assert f"'{pattern}'" in hint[0]


def test_the_search_pattern_in_the_hint_really_finds_the_traces(env):
    from ctx.retrieval import search

    ws, store = env
    text = "".join(f"Error: e{i}\n" + "".join(f"    at f{k} (/a/b{k}.js:{k + 1}:2)\n" for k in range(30)) + "\n" for i in range(9))
    r = take(env, text)
    assert r.profile == "trace/v1"
    hint = next(ln for ln in r.digest.splitlines() if "earlier traces not listed" in ln)
    pat = hint.split("'")[1]
    out = search(store, ws, r.address.split("#")[0], [pat])
    assert "matches:" in out and "matches: 0" not in out


def test_chain_links_and_traces_are_numbered_apart(env):
    d = take(env, padded(python_trace(chain=True))).digest
    assert "innermost frames of link 2 of 2" in d and "link 1" in d
    assert "#1 \"" in d      # frame numbers keep #, links and traces do not use it


def test_indented_prose_under_a_traceback_header_is_not_structure(env):
    words = " ".join(["indented", "prose", "line"])
    text = ("Traceback (most recent call last):\n" + '  File "/app/a.py", line 1, in f\n    x()\n'
            + "".join(f"    {words} {i}\n" for i in range(300)) + "Done.\n")
    assert take(env, text).profile != "trace/v1"


# ================================================================== item 9 (pins)
def test_python_traceback_shows_the_last_printed_frame_as_innermost(env):
    lines = ["Traceback (most recent call last):"]
    for i in range(90):
        lines += [f'  File "/app/step{i}.py", line {i + 1}, in step{i}', f"    call_{i}()"]
    lines.append("RuntimeError: deep")
    d = take(env, "\n".join(lines) + "\n").digest
    first = next(ln for ln in d.splitlines() if ln.startswith("  #1 "))
    assert '"/app/step89.py":90 in "step89"' in first
    outer = next(ln for ln in d.splitlines() if "outermost" in ln)
    assert "step0.py" in outer


def test_the_quoter_removes_bidi_and_zero_width_characters(env):
    pol = policy(env)
    assert inert("a\u202eb\u200bc\ufeffd\u2066e", 40, pol) == '"abcde"'
    assert inert("a\u2028b\u0085c\td", 40, pol) == '"a b c d"'


# ================================================================== item 10
def test_clipped_paths_keep_the_distinguishing_tail(env):
    pol = policy(env)
    out = Quoter(30, pol)("/usr/lib/python3.12/site-packages/urllib3/connectionpool.py", tail=True)
    assert out.startswith('"\u2026') and out.rstrip('"').endswith("connectionpool.py")
    lines = ["Traceback (most recent call last):"]
    for i, lib in enumerate(["urllib3/connectionpool.py", "urllib3/util/retry.py", "requests/adapters.py", "requests/sessions.py"] * 24):
        lines += [f'  File "/usr/lib/python3.12/site-packages/{lib}", line {i + 1}, in f{i}', "    go()"]
    lines.append("ConnectionError: x")
    d = take(env, "\n".join(lines) + "\n").digest
    census = next(ln for ln in d.splitlines() if ln.startswith("files in frames"))
    names = [part.split(" ×")[0] for part in census.split(": ", 1)[1].split(" ; ")[:4]]
    assert len(set(names)) == len(names), names


def test_the_user_code_guess_skips_common_jvm_third_party_packages(env):
    t = ['Exception in thread "main" org.springframework.dao.DataAccessResourceFailureException: db down']
    t += [f"\tat com.acme.repo.UserRepo.find(UserRepo.java:{i + 1})" for i in range(2)]
    t += ["Caused by: org.postgresql.util.PSQLException: Connection refused"]
    t += [f"\tat org.postgresql.core.v3.ConnectionFactoryImpl.openConnectionImpl(ConnectionFactoryImpl.java:{300 + i})" for i in range(80)]
    t += ["\tat com.acme.repo.UserRepo.find(UserRepo.java:40)", "\t... 20 more"]
    d = take(env, "\n".join(t) + "\n").digest
    line = next(ln for ln in d.splitlines() if ln.startswith("your code"))
    assert "UserRepo.java" in line and "postgresql" not in line


# ================================================================== round 3
def decimal_comma_rows(n: int = 300) -> str:
    """Semicolon-delimited; every row carries THREE commas inside the numbers,
    so a columns-first ranking would pick the comma (4 columns)."""
    return "\n".join(["Name;Preis;Menge"] + [f"Artikel {i};{i},234,567,890;{i % 7}" for i in range(n)]) + "\n"


def test_ranking_is_consistency_first_not_columns_first(env):
    found = delimprof.sniff(decimal_comma_rows())
    assert [c.delimiter for c in found][:2] == [";", ","]
    assert found[1].columns > found[0].columns            # the trap: the comma reads wider
    r = take(env, decimal_comma_rows())
    assert "delimited: semicolon · 3 columns" in r.digest


def lines_of(kind: str) -> str:
    n = 300
    if kind == "trailing delimiter export":
        return "\n".join(["id,name,amount,"] + [f"{i},person{i},{i * 3}.5," for i in range(n)]) + "\n"
    if kind == "glossary":
        return "\n".join(["term,definition"] + [f"term{i},a longer explanatory definition of the term number {i} in prose" for i in range(n)]) + "\n"
    if kind == "name to email":
        return "\n".join(f"Person Number{i},person{i}@example.com" for i in range(n)) + "\n"
    if kind == "keyword words in first column":
        return "\n".join(["kw,n,note"] + [f"{['select', 'from', 'where', 'order'][i % 4]},{i},row number {i} of the export" for i in range(n)]) + "\n"
    if kind == "bracket, assignment and comment cells":
        return "\n".join(["a,b,n"] + [f"[{i}],x={i},{i}" if i % 2 else f"-- note {i},y=2,{i}" for i in range(n)]) + "\n"
    if kind == "categorical text":
        return "\n".join(["city,status,owner"] + [f"{['Oslo', 'Lima', 'Kyiv'][i % 3]},{['ok', 'fail'][i % 2]},owner number {i}" for i in range(n)]) + "\n"
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["trailing delimiter export", "glossary", "name to email",
                                  "keyword words in first column", "bracket, assignment and comment cells",
                                  "categorical text"])
def test_ordinary_exports_are_claimed(env, kind):
    assert take(env, lines_of(kind)).profile == "delimited/v1", kind


_W = ["late", "cold", "quiet", "long", "grey", "empty", "slow", "dark", "warm", "thin", "wide"]


@pytest.mark.parametrize("kind,text", [
    ("c array", "static const int T[] = {\n" + "\n".join(f"  {i}, {i * 2}, {i * 3}, {i * 4}," for i in range(300)) + "\n};\n"),
    ("sql dump", "INSERT INTO t VALUES\n" + "\n".join(f"({i}, 'name{i}', {i * 2})," for i in range(300)) + "\n"),
    ("requirements", "\n".join(f"pkg{i}, >=1.{i}" for i in range(300)) + "\n"),
    ("prose with two commas per line", "\n".join(
        f"Well, it was {_W[i * 7 % 11]} on day {i} of the {_W[i * 5 % 11]} winter, and we {_W[i * 3 % 11]} {i * 37 % 101} times"
        for i in range(300)) + "\n"),
    ("names without evidence", "\n".join(f"Smith{i}, John" for i in range(300)) + "\n"),
])
def test_code_sql_and_prose_are_still_declined(env, kind, text):
    assert take(env, text).profile != "delimited/v1", kind


def test_the_hint_for_javascript_and_java_matches_one_line_per_trace(env):
    from ctx.retrieval import search

    ws, store = env
    js = "".join(f"TypeError: e{i}\n" + "".join(f"    at f{k} (/a/b{k}.js:{k + 1}:2)\n" for k in range(30)) + "\n" for i in range(9))
    jv = "".join(f'Exception in thread "t{i}" java.lang.IllegalStateException: e{i}\n'
                 + "".join(f"\tat a.B{k}.c(B{k}.java:{k + 1})\n" for k in range(30))
                 + "Caused by: java.io.IOException: x\n\tat a.C.d(C.java:1)\n\n" for i in range(9))
    for text in (js, jv):
        r = take(env, text)
        hint = next(ln for ln in r.digest.splitlines() if "earlier traces not listed" in ln)
        out = search(store, ws, r.address.split("#")[0], [hint.split("'")[1]])
        assert "matches: 9 " in out or "matches: 9\n" in out, out


# ================================================================== round 4: brackets
def _n(i: int, mod: int = 97) -> int:
    return (i * 37) % mod


@pytest.mark.parametrize("kind,text", [
    ("python calls", "\n".join(f"draw_rect(ctx, {_n(i)}, {i}, 40)" for i in range(300)) + "\n"),
    ("tuple list", "\n".join(f"({i}, 'a{i}', {i * 2})" for i in range(300)) + "\n"),
    ("css", "\n".join(f"a{i} {{ color: red, margin: {i} }}" for i in range(300)) + "\n"),
    ("yaml flow lists", "\n".join(f"- tags: [a{i}, b, {i}]" for i in range(300)) + "\n"),
    ("markdown table without leading pipes", "name | n | note\n---|---|---\n" + "\n".join(f"item{i} | {i} | text {i}" for i in range(300)) + "\n"),
    ("json lines", "\n".join('{"a": %d, "b": "x%d", "c": [1, 2]}' % (i, i) for i in range(300)) + "\n"),
])
def test_code_and_markup_with_commas_is_declined_by_unbalanced_brackets(env, kind, text):
    assert take(env, text).profile != "delimited/v1", kind


@pytest.mark.parametrize("kind,text", [
    ("interval bins", "bin,count\n" + "\n".join(f"[{i}-{i + 5}),{_n(i)}" for i in range(300)) + "\n"),
    ("balanced brackets inside cells", "id,note,n\n" + "\n".join(f"{i},see [ref {i}] and (note),{i * 2}" for i in range(300)) + "\n"),
    ("json-looking first cell that is a plain value", "id,tag,n\n" + "\n".join(f"{i},{{{i}}},{i}" for i in range(300)) + "\n"),
])
def test_data_with_brackets_that_close_in_their_cell_is_claimed(env, kind, text):
    assert take(env, text).profile == "delimited/v1", kind


def test_unbalanced_cells_unit():
    assert delimprof._unbalanced("draw_rect(ctx") and delimprof._unbalanced("(1") and delimprof._unbalanced("a]")
    assert not delimprof._unbalanced("[0-5)") and not delimprof._unbalanced("f(x) + g[y]") and not delimprof._unbalanced("plain")
