"""Every range ctx returns is edge-aware: a secret cut by a range edge is
blanked, so no answer -- and no concatenation of answers -- shows a piece of
something redaction would have caught whole.

Ranges come from three places, all covered: the boundaries ctx prints itself
(chunk rows and pages: ``bytes A:B``, ``L a-L b``, ``--span``; the
``next: ctx get … --bytes``/``--lines`` continuations of a range answer),
and arbitrary boundaries a caller picks. Answers keep their declared width:
the blanks are one ``*`` per byte, so continuations still tile the artifact.
"""

from __future__ import annotations

import random
import re
import string

import pytest
from conftest import make_store, make_ws

from ctx.intake import intake_bytes, render_chunk_page
from ctx.retrieval import Selector, get

FRAG = 5


def _rand(r, n, alphabet=string.ascii_letters + string.digits):
    return "".join(r.choice(alphabet) for _ in range(n))


def _secrets():
    r = random.Random(21)
    up = string.ascii_uppercase + string.digits
    s = {
        "github-token": ("ghp_", _rand(r, 36)),
        "aws-access-key": ("AKIA", _rand(r, 16, up)),
        "anthropic-key": ("sk-ant-", _rand(r, 30)),
        "slack-token": ("xoxb-", _rand(r, 24)),
        "generic-api-token": ("sk-", _rand(r, 30)),
        "google-api-key": ("AIza", _rand(r, 35)),
    }
    a, c, d = _rand(r, 24), _rand(r, 12), _rand(r, 14)
    s["jwt"] = ("eyJ", a + "." + c + "." + d)
    return s


SECRETS = _secrets()


def _no_fragment(text: str, body: str, where: str) -> None:
    for part in body.split("."):
        for i in range(len(part) - FRAG):
            frag = part[i:i + FRAG + 1]
            assert frag not in text, (where, frag)


def _body(answer: str) -> str:
    """The content of a ``ctx get`` answer: after the header/selector lines,
    without a trailing ``next:`` or redaction note."""
    lines = answer.split("\n")
    keep = [ln for ln in lines[1:] if not ln.startswith(("selector:", "next:", "redaction:",
                                                         "bytes:", "region:", "[ctx:truncated"))]
    return "\n".join(re.sub(r"^L\d+: ", "", ln) for ln in keep)


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def _follow(store, ws, handle, sel_kind, a, b, hops=60):
    """Answers for a range and every continuation ctx prints after it."""
    outs = []
    for _ in range(hops):
        ans = get(store, ws, handle, Selector(**{sel_kind: (a, b)}))
        outs.append(ans)
        m = re.search(rf"next: ctx get \S+ --{sel_kind} (\d+):(\d+)", ans)
        if not m:
            break
        a, b = int(m.group(1)), int(m.group(2))
    return outs


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_ctx_printed_boundaries_on_one_long_line(env, name):
    ws, store = env
    prefix, body = SECRETS[name]
    secret = prefix + body
    r = random.Random(5)
    line = "".join("a" * r.randrange(1, 70) + "|" + secret + "|" for _ in range(500))
    rec = intake_bytes(store, ws, line.encode(), label="rng")
    handle = rec.address
    answers = []
    page_start = 1
    while page_start:
        page = render_chunk_page(store, ws, handle, page_start)
        for a, b in re.findall(r"bytes (\d+):(\d+)", page):
            answers.append(get(store, ws, handle, Selector(bytes=(int(a), int(b)))))
        for sid in re.findall(r"· span ([0-9a-f]{10})", page):
            answers.append(get(store, ws, handle, Selector(span=sid)))
        m = re.search(r"--from (\d+)$", page)
        page_start = int(m.group(1)) if m else 0
    answers += _follow(store, ws, handle, "bytes", 1, 3000)
    for ans in answers:
        _no_fragment(ans, body, f"{name}/answer")
    # ordered concatenation of the --bytes answers for the printed pieces
    joined = "".join(_body(a) for a in answers if "selector: --bytes" in a)
    _no_fragment(joined, body, f"{name}/joined")


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_arbitrary_caller_boundaries(env, name):
    ws, store = env
    prefix, body = SECRETS[name]
    secret = prefix + body
    text = ("filler " * 30 + "|" + secret + "|" + " filler" * 30 + "\n") * 20
    rec = intake_bytes(store, ws, text.encode(), label="arb")
    pos = text.index(secret) + 1  # 1-based
    for k in range(1, len(secret)):
        cut = pos + k - 1
        left = get(store, ws, rec.address, Selector(bytes=(max(1, cut - 300), cut)))
        right = get(store, ws, rec.address, Selector(bytes=(cut + 1, cut + 300)))
        _no_fragment(left, body, f"{name}/left@{k}")
        _no_fragment(right, body, f"{name}/right@{k}")
        _no_fragment(_body(left) + _body(right), body, f"{name}/joined@{k}")
        # declared width holds: the body is exactly the bytes asked for
        assert len(_body(left).encode()) == cut - max(1, cut - 300) + 1


def test_lines_window_starting_inside_a_private_key(env):
    ws, store = env
    r = random.Random(7)
    key = [_rand(r, 64, string.ascii_letters + string.digits + "+/") for _ in range(40)]
    text = ("intro\n" * 10 + "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(key)
            + "\n-----END RSA PRIVATE KEY-----\n" + "outro\n" * 50)
    rec = intake_bytes(store, ws, text.encode(), label="pem")
    begin = 11
    for a in range(begin + 1, begin + 40, 3):
        for ans in _follow(store, ws, rec.address, "lines", a, a + 7):
            for ln in key:
                _no_fragment(ans, ln, f"pem lines {a}")
            # line count of the answer is what it declares
            m = re.search(r"selector: --lines (\d+):(\d+)", ans)
            assert len([x for x in ans.split("\n") if re.match(r"L\d+: ", x)]) == int(m.group(2)) - int(m.group(1)) + 1


def test_ordinary_ranges_are_untouched(env):
    """No secret, no change: a range answer is exactly the stored bytes."""
    ws, store = env
    text = "".join(f"line {i} with ordinary words and numbers {i * 7}\n" for i in range(3000))
    rec = intake_bytes(store, ws, text.encode(), label="plain")
    data = text.encode()
    for a, b in ((1, 500), (777, 4100), (60000, 62000)):
        ans = get(store, ws, rec.address, Selector(bytes=(a, b)))
        assert _body(ans).encode() == data[a - 1:b]
