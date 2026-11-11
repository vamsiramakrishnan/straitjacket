"""Intake never shows a fragment of a secret, wherever a clip falls.

The chunk map and its pager shorten payload text in several places: a chunk
label (first line or heading path, ``LABEL_CHARS``), a flagged row's signal
line (``SIGNAL_ROW_CHARS``), the decisive / terminal signal lines
(``EVIDENCE_LINE_CHARS``), byte-range pieces of one long line, and the
label / name tokens. Redaction runs on the whole payload before any of
those cuts, so for every shared redaction pattern, planted before, across
(at every offset) and after each clip width, no run of the secret's random
part longer than a few characters may appear in the digest or in any page.
"""

from __future__ import annotations

import random
import re
import string

import pytest
from conftest import make_store, make_ws

from ctx.digest.chunkmap import LABEL_CHARS
from ctx.digest.intakeprof import SIGNAL_ROW_CHARS
from ctx import intake
from ctx.intake import intake_bytes, render_chunk_page
from ctx.textutil import EVIDENCE_LINE_CHARS, REDACTION_PATTERNS

FRAGMENT = 5  # longest run of a secret's random part allowed to show


def _rand(r: random.Random, n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(r.choice(alphabet) for _ in range(n))


def _secrets(seed: int = 7) -> dict[str, tuple[str, str]]:
    """pattern name -> (secret, its random part)."""
    r = random.Random(seed)
    up = string.ascii_uppercase + string.digits
    hexd = "0123456789abcdef"
    out = {}

    def add(name, prefix, body, suffix=""):
        out[name] = (prefix + body + suffix, body)

    add("aws-access-key", "AKIA", _rand(r, 16, up))
    add("aws-secret-key", "aws_secret_access_key=", _rand(r, 40))
    add("github-token", "ghp_", _rand(r, 36))
    add("gitlab-token", "glpat-", _rand(r, 20))
    add("slack-token", "xoxb-", _rand(r, 24))
    add("stripe-key", "sk_live_", _rand(r, 24))
    add("twilio-key", "SK", _rand(r, 32, hexd))
    b = _rand(r, 22)
    add("sendgrid-key", "SG.", b, "." + _rand(r, 43))
    add("npm-token", "npm_", _rand(r, 36))
    add("pypi-token", "pypi-AgEIcHlwaS5vcmc", _rand(r, 40))
    add("huggingface-token", "hf_", _rand(r, 34))
    add("anthropic-key", "sk-ant-", _rand(r, 30))
    add("jwt", "eyJ", _rand(r, 24), "." + _rand(r, 12) + "." + _rand(r, 12))
    add("google-api-key", "AIza", _rand(r, 35))
    add("generic-api-token", "sk-", _rand(r, 30))
    for name, (secret, _) in out.items():
        m = REDACTION_PATTERNS[name].search(" " + secret + " ")
        assert m and m.group(0) == secret, name
    return out


SECRETS = _secrets()


def _fragments(body: str) -> set[str]:
    return {body[i:i + FRAGMENT + 1] for i in range(len(body) - FRAGMENT)}


def _all_output(store, ws, r) -> str:
    out = [r.digest]
    start = 1
    while start:
        page = render_chunk_page(store, ws, r.address, start)
        out.append(page)
        m = re.search(r"--from (\d+)$", page)
        start = int(m.group(1)) if m else 0
    return "\n".join(out)


def _assert_no_fragment(text: str, body: str, where: str) -> None:
    for frag in _fragments(body):
        assert frag not in text, (where, frag)


def _offsets(width: int, length: int) -> list[int]:
    """Secret start columns: wholly before the cut, straddling it at every
    offset, and wholly after it."""
    before = [max(1, width - length - 3)]
    straddle = list(range(max(1, width - length + 1), width))
    after = [width + 2]
    return before + straddle + after


def _paragraph(first: str, r: random.Random) -> str:
    """A paragraph big enough to be its own chunk, starting with ``first``."""
    filler = "\n".join(f"plain filler text line number {i} of the paragraph" for i in range(48))
    return first + "\n" + filler + "\n\n"


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_chunk_labels_and_headings_never_show_a_fragment(env, name):
    ws, store = env
    secret, body = SECRETS[name]
    r = random.Random(1)
    for kind, width, lead in (("label", LABEL_CHARS, ""), ("heading", LABEL_CHARS, "# ")):
        paras = []
        for off in _offsets(width, len(secret)):
            pad = "p" * (off - 1 - len(lead))
            paras.append(_paragraph(lead + pad + " " + secret + " tail words here", r))
        rec = intake_bytes(store, ws, "".join(paras).encode(), label=f"{kind}")
        assert rec.profile == "chunkmap/v1"
        _assert_no_fragment(_all_output(store, ws, rec), body, f"{name}/{kind}")


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_signal_rows_never_show_a_fragment(env, name):
    ws, store = env
    secret, body = SECRETS[name]
    r = random.Random(2)
    lead = "ERROR: "
    paras = []
    for off in _offsets(SIGNAL_ROW_CHARS, len(secret)):
        pad = "q" * max(0, off - 1 - len(lead))
        paras.append(_paragraph("plain first line", r).replace(
            "plain filler text line number 7 ", f"{lead}{pad} {secret} more ", 1))
    rec = intake_bytes(store, ws, "".join(paras).encode(), label="signals")
    out = _all_output(store, ws, rec)
    assert " ! " in out
    _assert_no_fragment(out, body, f"{name}/signal-row")


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_decisive_signal_line_never_shows_a_fragment(env, name):
    ws, store = env
    secret, body = SECRETS[name]
    lead = "Traceback: failed "
    for off in _offsets(EVIDENCE_LINE_CHARS, len(secret))[::3] + [EVIDENCE_LINE_CHARS - 1]:
        pad = "z" * max(0, off - 1 - len(lead))
        text = "\n".join(["ordinary line"] * 300 + [f"{lead}{pad} {secret} end"]) + "\n"
        rec = intake_bytes(store, ws, text.encode(), label="decisive")
        assert "decisive signal" in rec.digest
        _assert_no_fragment(_all_output(store, ws, rec), body, f"{name}/decisive@{off}")


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_byte_pieces_of_one_long_line_never_show_a_fragment(env, name):
    """Secrets planted densely along one long line: piece boundaries cut
    through some of them, and each piece's label is its first characters."""
    ws, store = env
    secret, body = SECRETS[name]
    r = random.Random(3)
    parts = []
    for i in range(160):
        parts.append("a" * r.randrange(1, 90) + " " + secret + " ")
    rec = intake_bytes(store, ws, "".join(parts).encode(), label="oneline")
    assert rec.profile == "chunkmap/v1" and rec.lines == 1
    _assert_no_fragment(_all_output(store, ws, rec), body, f"{name}/byte-pieces")


def test_private_key_split_across_chunks_never_shows_its_body(env):
    ws, store = env
    r = random.Random(4)
    key_lines = [_rand(r, 64, string.ascii_letters + string.digits + "+/") for _ in range(120)]
    text = ("intro line\n" * 40 + "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(key_lines)
            + "\n-----END RSA PRIVATE KEY-----\n" + "outro line\n" * 400)
    rec = intake_bytes(store, ws, text.encode(), label="pem")
    out = _all_output(store, ws, rec)
    for ln in key_lines:
        _assert_no_fragment(out, ln, "private-key body")


def test_label_and_name_are_redacted_before_they_are_clipped(env):
    ws, store = env
    secret, body = SECRETS["github-token"]
    rec = intake_bytes(store, ws, b"x\n" * 3000, label="note " + "p" * 20 + " " + secret,
                       name="dir/file " + "q" * 25 + " " + secret + ".txt")
    for text in (rec.label, rec.name or "", rec.digest):
        _assert_no_fragment(text, body, "label/name")


def test_reviewer_example(env):
    ws, store = env
    token = "ghp_Q7w8E9r0T1Q7w8E9r0T1Q7w8E9r0T1Q7w8E9r0T1"
    text = f"Note: the deploy key is {token}\n" + "".join(
        f"filler words line {i}\n" for i in range(400))
    rec = intake_bytes(store, ws, text.encode())
    out = _all_output(store, ws, rec)
    assert "ghp_Q7w8" not in out and "Q7w8E9r0" not in out
    assert "Note: the deploy key is [ctx:redacted:github-t" in out
    assert "redaction: applied [github-token]" in out


# ------------------------------------------- shared profiles: clip_redacting
def test_clip_redacting_is_identity_unless_a_secret_straddles_the_cut():
    from ctx.textutil import clip_redacting

    r = random.Random(9)
    for _ in range(2000):
        s = "".join(r.choice(string.ascii_letters + " _-./:") for _ in range(r.randrange(0, 400)))
        assert clip_redacting(s, 160) == s[:160]
    for name, (secret, body) in SECRETS.items():
        for off in _offsets(160, len(secret)):
            line = "w" * (off - 1) + " " + secret + " tail"
            out = clip_redacting(line, 160)
            assert len(out) == len(line[:160])           # same width, always
            if off + len(secret) <= 160 or off >= 160:   # wholly inside / outside:
                assert out == line[:160]                 # untouched (the emission
            else:                                        # pass redacts the inside)
                _assert_no_fragment(out, body, name)


@pytest.mark.parametrize("name", ["github-token", "aws-access-key", "anthropic-key", "jwt"])
def test_text_profile_head_window_never_shows_a_fragment(env, name):
    from ctx.digest import digest_output

    ws, store = env
    secret, body = SECRETS[name]
    for off in _offsets(160, len(secret)):
        line = "w" * (off - 1) + " " + secret + " tail"
        text = "\n".join([line] + ["middle"] * 900 + [line]) + "\n"
        digest, _ = digest_output(store, ws, "Bash", text)
        _assert_no_fragment(digest, body, f"{name}@{off}")


# ------------------------------------------- spans whose edges ctx chose
def _resolve_all_spans(store, ws, rec) -> list[str]:
    from ctx.retrieval import Selector, get

    outs = []
    start = 1
    while start:
        page = render_chunk_page(store, ws, rec.address, start)
        for sid in re.findall(r"· span ([0-9a-f]{10})", page):
            outs.append(get(store, ws, rec.address, Selector(span=sid)))
        m = re.search(r"--from (\d+)$", page)
        start = int(m.group(1)) if m else 0
    return outs


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_adjacent_pieces_cannot_reassemble_a_secret(env, name):
    """Piece boundaries fall inside planted secrets; resolving every piece
    the map hands out must not yield a head in one piece and a tail in the
    next (nor the secret inside either)."""
    ws, store = env
    secret, body = SECRETS[name]
    r = random.Random(11)
    # "|" around each secret: a word boundary in no pattern's class and not a
    # separator the piece cutter prefers, so cuts land wherever the byte
    # budget puts them -- inside secrets, among other places.
    line = "".join("a" * r.randrange(1, 70) + "|" + secret + "|" for _ in range(700))
    rec = intake_bytes(store, ws, line.encode(), label="pieces")
    outs = _resolve_all_spans(store, ws, rec)
    assert len(outs) > 10
    joined = "".join(o.split("\n", 2)[2] for o in outs)   # piece bodies, concatenated
    _assert_no_fragment(joined, body, f"{name}/joined")
    for o in outs:
        _assert_no_fragment(o, body, f"{name}/piece")
    # and a piece boundary really did fall inside a secret
    ends = [int(m) for m in re.findall(r"bytes: \d+:(\d+)", "\n".join(outs))]
    starts = [m.start() + 1 for m in re.finditer(re.escape(secret), line)]
    assert any(s <= c < s + len(secret) - 1 for c in ends for s in starts)


def test_region_chunks_inside_a_private_key_are_masked(env):
    ws, store = env
    r = random.Random(12)
    key = [_rand(r, 64, string.ascii_letters + string.digits + "+/") for _ in range(150)]
    text = ("intro line\n" * 30 + "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(key)
            + "\n-----END RSA PRIVATE KEY-----\n" + "outro line\n" * 300)
    rec = intake_bytes(store, ws, text.encode(), label="pem")
    outs = _resolve_all_spans(store, ws, rec)
    assert len(outs) >= 3
    for out in outs:
        for ln in key:
            _assert_no_fragment(out, ln, "pem region")


# --------------------------------------------- policy: off means off
def test_clip_redacting_honours_the_policy():
    from types import SimpleNamespace

    from ctx.textutil import clip_redacting, redaction_scope

    secret = SECRETS["github-token"][0]
    line = "w" * 140 + " " + secret + " tail"
    off = SimpleNamespace(enabled=False, patterns=tuple(REDACTION_PATTERNS))
    narrowed = SimpleNamespace(enabled=True, patterns=("aws-access-key",))
    assert clip_redacting(line, 160, off) == line[:160]
    assert clip_redacting(line, 160, narrowed) == line[:160]
    assert clip_redacting(line, 160) != line[:160]
    with redaction_scope(off):
        assert clip_redacting(line, 160) == line[:160]


def test_digest_with_redaction_off_is_untouched(state_home, workspace_dir):
    from ctx.digest import digest_output

    (workspace_dir / "ctx.toml").write_text("version = 1\n[redaction]\nenabled = false\n")
    ws = make_ws(workspace_dir)
    store = make_store(ws)
    secret = SECRETS["github-token"][0]
    line = "w" * 140 + " " + secret + " tail"
    digest, _ = digest_output(store, ws, "Bash", "\n".join([line] + ["mid"] * 900 + [line]) + "\n")
    assert line[:160] in digest and "*****" not in digest


# --------------------------------------------- NUL in a text payload
def test_real_nul_characters_are_not_shown_as_redacted(env):
    ws, store = env
    secret, body = SECRETS["github-token"]
    text = ("x" * 9000 + "\n" + "row\x00with nul\n" + "plain line\n" * 200
            + f"nul\x00then {secret} after\n" + "tail line\n" * 300)
    data = text.encode()
    assert intake.binary_kind(data) is None          # NUL past 8 KiB: text path
    rec = intake_bytes(store, ws, data, label="nul")
    out = _all_output(store, ws, rec)
    assert "\x00" not in out
    _assert_no_fragment(out, body, "nul+secret")
    from ctx.digest.intakeprof import build_map

    chunks = build_map(text, rec.lines, ws.config.budgets)
    assert chunks[0].start_byte == 1 and chunks[-1].end_byte == len(data)
    for a, b in zip(chunks, chunks[1:]):
        assert b.start_byte == a.end_byte + 1
    nul_rows = [ln for ln in out.splitlines() if "row" in ln and "with nul" in ln]
    assert all("redacted" not in ln for ln in nul_rows)


# ------------------------- the redaction note claims only what is visible
@pytest.mark.parametrize("seed", range(6))
def test_redaction_note_names_only_visible_markers(env, seed):
    from ctx.textutil import shown_marker_names

    ws, store = env
    r = random.Random(100 + seed)
    names = sorted(SECRETS)
    lines = []
    for i in range(300):
        if r.random() < 0.15:
            name = r.choice(names)
            col = r.choice([0, 10, 30, 47, 60, 71, 140, 300])
            lines.append(("ERROR " if r.random() < 0.3 else "") + "x" * col + " " + SECRETS[name][0] + " tail")
        else:
            lines.append(" ".join(r.choice(["alpha", "beta", "gamma", "delta"]) for _ in range(r.randrange(3, 30))))
    rec = intake_bytes(store, ws, ("\n".join(lines) + "\n").encode(), label=f"note{seed}")
    outs = [rec.digest]
    start = 1
    while start:
        page = render_chunk_page(store, ws, rec.address, start)
        outs.append(page)
        m = re.search(r"--from (\d+)$", page)
        start = int(m.group(1)) if m else 0
    for out in outs:
        for note in re.findall(r"^redaction: applied \[(.*)\]$", out, re.M):
            listed = [n.strip() for n in note.split(",")]
            body = "\n".join(ln for ln in out.splitlines() if not ln.startswith("redaction:"))
            visible = shown_marker_names(body, REDACTION_PATTERNS)
            assert set(listed) <= set(visible), (listed, visible)
