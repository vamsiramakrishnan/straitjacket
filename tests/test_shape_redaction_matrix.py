"""The Quoter's guarantee, tested as stated in its docstring: no substring the
shared redaction patterns match in the ORIGINAL field or in the CLEANED field
appears in the digest, whole or as a fragment produced by clipping. Nothing
is withheld.

Disguises that defeat both readings (a token glued to a word AND split by
removable characters, etc.) are non-goals and are not asserted here."""

from __future__ import annotations

import csv
import io
import random

import pytest
from conftest import make_store, make_ws
from shape_helpers import csv_text, js_trace, padded, python_trace, unified_diff
from test_shape_review_fixes import KINDS, leaked, secret

from ctx.digest import shapeutil
from ctx.digest.shapeutil import Quoter, _clean, _clean_mapped
from ctx.intake import intake_bytes
from ctx.textutil import REDACTION_PATTERNS


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def take(env, text: str):
    ws, store = env
    return intake_bytes(store, ws, text.encode("utf-8"), label="paste")


def matched_texts(field: str, patterns) -> list[str]:
    """What the shared patterns match in the original field and in its
    cleaned form -- the guarantee's two readings."""
    out: list[str] = []
    for text in (field, _clean(field)):
        for name in patterns:
            out += [m.group(0) for m in REDACTION_PATTERNS[name].finditer(text)]
    return out


def pieces(matched: str, window: int = 6) -> list[str]:
    body = matched[12:]   # past any fixed prefix
    return [body[i:i + window] for i in range(0, max(0, len(body) - window + 1))]


def assert_guarantee(field: str, rendered: str, patterns) -> int:
    n = 0
    for m in matched_texts(field, patterns):
        n += 1
        for frag in pieces(m):
            assert frag not in rendered, (field[:120], frag, rendered)
    return n


# --------------------------------------------------------------- the cleaner
def test_the_mapped_cleaner_equals_the_reference_cleaner():
    rnd = random.Random(1)
    alphabet = ["a", "Z", "7", " ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1b[0m", "\x1b[31;1m", "\x1b]0;t\x07", "\x1bM", "\x1b",
                "​", "‮", " ", " ", "\x85", "́", "﻿", "\x00", "\x7f", "　", "ghp_", "-", "é"]
    for _ in range(3000):
        text = "".join(rnd.choice(alphabet) for _ in range(rnd.randrange(0, 40)))
        cleaned, origin = _clean_mapped(text)
        assert cleaned == _clean(text).replace("run·PENDING", "run:PENDING"), repr(text)
        assert len(origin) == len(cleaned) and origin == sorted(origin)
        assert all(text[o] == c or c == " " for c, o in zip(cleaned, origin)), repr(text)


# ------------------------------------------------------------- the property
CLEANERS = ["\x1b[0m", "\x1b[31;1m", "\x1b]0;t\x07", "​", "‮", "﻿", "\x0b", "\x0c", "\t", " ", "́", "\x7f"]


def generated_field(rnd: random.Random) -> str:
    """Words, spaces, cleaner characters and secrets, in generated positions,
    with cleaner characters before, inside and after the secrets."""
    parts: list[str] = []
    for _ in range(rnd.randrange(1, 6)):
        kind = rnd.choice(["word", "word", "space", "cleaner", "secret", "secret"])
        if kind == "word":
            parts.append("".join(rnd.choice("abcdefghij") for _ in range(rnd.randrange(1, 40))))
        elif kind == "space":
            parts.append(" " * rnd.randrange(1, 4))
        elif kind == "cleaner":
            parts.append(rnd.choice(CLEANERS))
        else:
            tok = secret(rnd, rnd.choice(KINDS))
            if rnd.random() < 0.5:
                cut = rnd.randrange(5, len(tok) - 3)
                tok = tok[:cut] + rnd.choice(CLEANERS) + tok[cut:]
            if rnd.random() < 0.5:
                tok = rnd.choice(CLEANERS) + tok
            if rnd.random() < 0.3:
                tok += rnd.choice(CLEANERS)
            parts.append(" " + tok + " ")
    return "".join(parts)


def test_property_no_match_of_either_reading_survives_the_quoter(state_home, workspace_dir):
    pol = make_ws(workspace_dir).config.redaction
    rnd = random.Random(21)
    seen = 0
    for _ in range(4000):
        field = generated_field(rnd)
        limit = rnd.choice([12, 24, 40, 60, 110, 160])
        pad = rnd.choice([0, 0, 0, rnd.randrange(0, 200)])
        text = "." * pad + " " + field
        out = Quoter(limit, pol)(text)
        seen += assert_guarantee(text, out, pol.patterns)
    assert seen > 2500, "the generator must actually plant matchable secrets"


def test_property_holds_at_the_storage_window_edge(state_home, workspace_dir):
    pol = make_ws(workspace_dir).config.redaction
    rnd = random.Random(22)
    for offset in range(shapeutil.WINDOW - 90, shapeutil.WINDOW + 20):
        tok = secret(rnd, rnd.choice(KINDS))
        text = ("." * offset + " " + tok)[: shapeutil.FIELD_KEEP]
        out = Quoter(rnd.choice([24, 60, 160]), pol)(text, truncated=len(text) >= shapeutil.FIELD_KEEP)
        for frag in pieces(tok):
            assert frag not in out, (offset, out)


def test_nothing_is_withheld_and_a_field_nothing_matches_is_shown_cleaned(state_home, workspace_dir):
    pol = make_ws(workspace_dir).config.redaction
    q = Quoter(160, pol)
    for text in ("\x1b[1;31mValueError\x1b[0m: boom", "a b\tc​d", "uuid 123e4567-e89b-12d3-a456-426614174000‍ x",
                 "sha " + "a" * 40 + " y", "https://example.com/products/category/item-1?ref=newsletter‍"):
        out = q(text)
        assert "redacted" not in out and "obfuscated" not in out
    assert q.fired == set() and q.note() == []
    assert q("\x1b[1;31mValueError\x1b[0m: boom") == '"ValueError: boom"'


# ------------------------------------------------- the same, through each profile
FIELD_CPS = ["\x0b", "\x0c", "​", "‮", "\x1b[0m", " ", "\x85", "\t", "﻿", "\x7f", "­", " "]


def shapes(tok: str, cp: str) -> dict[str, str]:
    return {"before": f"abc {cp}{tok}", "inside": f"abc {tok[:12]}{cp}{tok[12:]}", "after": f"{tok}{cp}abc"}


def build(field: str, text_for: str) -> str:
    if field == "trace message":
        return padded(python_trace(final=f"ValueError: {text_for}"))
    if field == "trace path":
        return padded(python_trace(user=f"/app/{text_for}/core.py"))
    if field == "trace func":
        return js_trace(120).replace("getUser", text_for)
    rows = list(csv.reader(io.StringIO(csv_text(300))))
    if field == "csv header":
        rows[0][1] = text_for
    elif field == "csv first row":
        rows[1][2] = text_for
    elif field == "csv last row":
        rows[-1][2] = text_for
    elif field == "csv repeated value":
        for r in rows[1:]:
            r[2] = text_for
    else:
        return unified_diff(10, sizes=[200] + [9] * 9).replace("src/mod0.py", f"src/{text_for}/mod0.py")
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


@pytest.mark.parametrize("field", ["trace message", "trace path", "trace func", "csv header", "csv first row",
                                   "csv last row", "csv repeated value", "diff path"])
def test_every_rendered_field_of_every_profile_meets_the_guarantee(env, field):
    pol = env[0].config.redaction
    rnd = random.Random(12)
    planted = 0
    for cp in FIELD_CPS:
        tok = secret(rnd, "ghp")
        for where, text in shapes(tok, cp).items():
            r = take(env, build(field, text))
            assert r.profile in ("trace/v1", "delimited/v1", "udiff/v1"), (field, repr(cp), where)
            planted += assert_guarantee(text, r.digest, pol.patterns)
    assert planted >= 20


# ----------------------------------------------------------------- the window
@pytest.mark.parametrize("pad", range(4070, 4100, 3))
def test_a_cell_of_spaces_then_a_token_leaves_no_prefix(env, pad):
    rnd = random.Random(pad)
    tok = secret(rnd, "ghp")
    rows = list(csv.reader(io.StringIO(csv_text(300))))
    rows[0][1] = " " * pad + tok          # header name
    rows[1][2] = " " * pad + tok          # first record
    rows[-1][2] = " " * pad + tok         # last record
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    r = take(env, buf.getvalue())
    assert r.profile == "delimited/v1"
    assert leaked(r.digest, tok, window=6) is None, r.digest


@pytest.mark.parametrize("offset", range(4060, 4100, 2))
def test_a_diff_path_cut_by_the_storage_window_leaves_no_token_prefix(env, offset):
    rnd = random.Random(offset)
    tok = secret(rnd, "ghp")
    path = "." * offset + " " + tok
    text = unified_diff(10, sizes=[200] + [9] * 9).replace("src/mod0.py", f"src/{path}", 1)
    r = take(env, text)
    assert r.profile == "udiff/v1"
    assert leaked(r.digest, tok, window=6) is None, r.digest
