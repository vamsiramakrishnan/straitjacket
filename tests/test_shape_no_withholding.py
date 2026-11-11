"""Realistic secret-free pastes: nothing may be withheld or noted as redacted,
and the decisive line must be shown (terminal colour codes removed)."""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import random
import re
import uuid

import pytest
from conftest import make_store, make_ws

from ctx.intake import intake_bytes


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def take(env, text: str):
    ws, store = env
    return intake_bytes(store, ws, text.encode("utf-8"), label="paste")


def csvdoc(rows, hdr):
    b = io.StringIO()
    w = csv.writer(b, lineterminator="\n")
    w.writerow(hdr)
    w.writerows(rows)
    return b.getvalue()


def untouched(r) -> None:
    assert "obfuscated" not in r.digest and "redaction:" not in r.digest and "[ctx:redacted" not in r.digest, r.digest


SP = "/home/dev/.venv/lib/python3.12/site-packages"


def test_ansi_coloured_python_trace_shows_its_exception_line(env):
    t = ["\x1b[31mTraceback (most recent call last):\x1b[0m"]
    for i in range(60):
        t += [f'  File \x1b[35m"{SP}/sqlalchemy/engine/base_connection{i}.py"\x1b[0m, line \x1b[1m{100 + i}\x1b[0m, '
              f'in \x1b[36m_execute_context_internal\x1b[0m', "    self._handle_dbapi_exception(e)"]
    t += ["\x1b[1;31msqlalchemy.exc.OperationalError\x1b[0m: (psycopg2.OperationalError) connection to server failed"]
    r = take(env, "\n".join(t) + "\n")
    assert r.profile == "trace/v1"
    untouched(r)
    exc = next(ln for ln in r.digest.splitlines() if ln.startswith("exception"))
    assert "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server failed" in exc
    assert "\x1b" not in r.digest and "[31m" not in r.digest and "[0m" not in r.digest


def test_ansi_coloured_node_trace_shows_every_row(env):
    t = ["\x1b[31mTypeError: Cannot read properties of undefined (reading 'organizationIdentifier')\x1b[39m"]
    t += [f"    at \x1b[90mUserRepositoryImplementation.findByOrganizationIdentifier{i}\x1b[39m "
          f"(/srv/app/src/repositories/UserRepositoryImplementation.ts:{i + 3}:11)" for i in range(80)]
    r = take(env, "\n".join(t) + "\n")
    assert r.profile == "trace/v1"
    untouched(r)
    assert "Cannot read properties of undefined (reading 'organizationIdentifier')" in r.digest
    rows = [ln for ln in r.digest.splitlines() if ln.startswith("  #")]
    assert rows and all('"/srv/app/src/repositories/UserRepositoryImplementation.ts"' in ln for ln in rows[:3])
    assert not any("[ctx:redacted" in ln for ln in rows)


def test_diff_file_names_with_no_break_spaces_are_shown(env):
    out = []
    for f in range(15):
        name = f"config/secrets template{f}.yaml"
        out += ["--- a/" + name + "\t2026-01-01\n", "+++ b/" + name + "\t2026-01-02\n", "@@ -1,2 +1,2 @@\n",
                "-cert: A\n", "+cert: B\n", " x\n"] * 20
    r = take(env, "".join(out))
    assert r.profile == "udiff/v1"
    untouched(r)
    assert '"config/secrets template0.yaml"' in r.digest


def test_csv_cells_mixing_invisible_characters_with_ids_urls_and_hashes_are_shown(env):
    rnd = random.Random(5)
    uu = [str(uuid.UUID(int=rnd.getrandbits(128))) for _ in range(400)]
    docs = {
        "uuid with tab": csvdoc([[i, f"ref\t{uu[i]}", rnd.choice("xy")] for i in range(400)], ["id", "ref", "c"]),
        "url with zwj": csvdoc([[i, f"https://example.com/products/category/electronics/item-{i}?ref=newsletter_2026 \U0001F469‍\U0001F4BB",
                                 rnd.choice("xy")] for i in range(400)], ["id", "url", "c"]),
        "git id with nbsp": csvdoc([[i, f"commit {hashlib.sha1(str(i).encode()).hexdigest()}", rnd.choice("xy")]
                                    for i in range(400)], ["id", "ref", "c"]),
        "sku with nbsp": csvdoc([[f"Widget {i} ITEMCODE{rnd.randrange(10 ** 12):012d}", rnd.choice("ab"), i] for i in range(400)],
                                ["desc", "cat", "n"]),
        "bom and hashes": "﻿" + csvdoc([[f"SKU-{rnd.randrange(10 ** 9):09d}-ABCDEFGH{i}", hashlib.sha256(str(i).encode()).hexdigest(),
                                              f"Widget {i}", f"{i * 1.5:.2f}"] for i in range(400)], ["sku", "sha256", "name", "price"]),
    }
    for name, text in docs.items():
        r = take(env, text)
        assert r.profile == "delimited/v1", name
        untouched(r)
    r = take(env, docs["uuid with tab"])
    assert "ref " + uu[0][:8] in r.digest          # the cell is shown, cleaned (clipped to the cell width)
    r = take(env, docs["git id with nbsp"])
    assert "commit " + hashlib.sha1(b"0").hexdigest()[:8] in r.digest


def test_withheld_fields_across_realistic_pastes_are_zero(env):
    """The count the reviewer measured: quoted fields that are a redaction marker."""
    texts = []
    texts.append("\n".join(["\x1b[31mTraceback (most recent call last):\x1b[0m"]
                           + [f'  File \x1b[35m"{SP}/a/b{i}.py"\x1b[0m, line {i + 1}, in f\x1b[0m' + "\n    x()" for i in range(60)]
                           + ["\x1b[1;31mKeyError\x1b[0m: 'k'"]) + "\n")
    texts.append("".join(["--- a/x y\t2026\n", "+++ b/x y\t2026\n"] + ["@@ -1 +1 @@\n", "-a\n", "+b\n"] * 300))
    n = 0
    for t in texts:
        r = take(env, t)
        n += len(re.findall(r'"\[ctx:redacted:', r.digest))
    assert n == 0
