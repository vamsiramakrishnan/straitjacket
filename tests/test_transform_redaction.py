"""Text that is transformed before it is shown is redacted BEFORE the
transform: number masking (log templates), path shortening, case folding,
and private-key body lines shown without their BEGIN line (digests and
search context)."""

from __future__ import annotations

import json
import random
import re
import string

import pytest
from conftest import make_store, make_ws

from ctx.digest import digest_output
from ctx.retrieval import search
from ctx.textutil import short_path

FRAG = 5
R = random.Random(31)
TOKEN = "ghp_" + "".join(R.choice(string.ascii_letters + string.digits) for _ in range(36))
KEY = ["".join(R.choice(string.ascii_letters + string.digits + "+/") for _ in range(64)) for _ in range(12)]


def _no_fragment(text: str, body: str, where: str) -> None:
    for i in range(len(body) - FRAG):
        assert body[i:i + FRAG + 1] not in text, (where, body[i:i + FRAG + 1])


@pytest.fixture()
def env(state_home, workspace_dir):
    ws = make_ws(workspace_dir)
    return ws, make_store(ws)


def _log(lines_with_secret: list[str]) -> str:
    base = [f"2024-01-01T00:00:{i % 60:02d}Z INFO worker processed item {i}" for i in range(300)]
    return "\n".join(base[:150] + lines_with_secret + base[150:]) + "\n"


def test_log_template_is_built_from_the_redacted_line(env):
    ws, store = env
    line = f"2024-01-01T00:00:00Z ERROR auth failed token={TOKEN}"
    digest, _ = digest_output(store, ws, "Bash", _log([line, line]))
    assert "logtemplate" in digest.splitlines()[0]
    _no_fragment(digest, TOKEN[4:], "template")
    assert "[ctx:redacted:github-token:" in digest


def test_log_digest_never_shows_a_private_key_body_line(env):
    ws, store = env
    block = ["-----BEGIN RSA PRIVATE KEY-----"] + KEY + ["-----END RSA PRIVATE KEY-----"]
    digest, _ = digest_output(store, ws, "Bash", _log(block + block))
    for ln in KEY:
        _no_fragment(digest, ln, "pem in log digest")


def test_search_context_inside_a_private_key_is_blanked(env):
    ws, store = env
    text = "\n".join(["header"] * 5 + ["match here -----BEGIN RSA PRIVATE KEY-----"] + KEY
                     + ["-----END RSA PRIVATE KEY-----"] + ["tail"] * 5) + "\n"
    _, short = digest_output(store, ws, "Bash", text)
    out = search(store, ws, f"run:{short}", ["match here"], context=4)
    for ln in KEY:
        _no_fragment(out, ln, "search context")
    assert "match here" in out


def test_path_shortening_redacts_first():
    path = "deploy/aws_secret_access_key=" + "Ab/Cd" + "x" * 34 + "/config.yml"
    shown = short_path(path)
    assert "xxxxxxxx" not in shown
    assert short_path("src/ctx/digest/lintprof.py") == "digest/lintprof.py"


def test_case_folded_jsonl_level_is_redacted_first(env):
    ws, store = env
    akia = "AKIA" + "QWERTYUIOPASDFGH"
    rows = [json.dumps({"level": akia if i % 2 else "info", "i": i}) for i in range(60)]
    digest, _ = digest_output(store, ws, "Bash", "\n".join(rows) + "\n")
    assert "jsonl" in digest.splitlines()[0]
    assert akia.lower() not in digest and "qwertyuiop" not in digest


def test_ordinary_log_templates_are_unchanged(env):
    """No secret: the template rendering is the masked line, as before."""
    ws, store = env
    digest, _ = digest_output(store, ws, "Bash", _log([]))
    assert re.search(r"× L\d+: <\*> INFO worker processed item <\*>", digest)


# --------------------------- log templates: digest, span zoom, ctx diff
def _digit_token(name: str) -> tuple[str, str]:
    r = random.Random(name)
    body = "".join(r.choice("ab12cd34EF56gh78") for _ in range(36))
    prefix = {"github-token": "ghp_", "anthropic-key": "sk-ant-", "huggingface-token": "hf_",
              "generic-api-token": "sk-", "aws-access-key": "AKIA"}[name]
    if name == "aws-access-key":
        body = "".join(r.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    return prefix + body, body


TPL_PATTERNS = ["github-token", "anthropic-key", "huggingface-token", "aws-access-key",
                "generic-api-token"]


def _secret_log(token: str, n_secret: int = 3) -> str:
    base = [f"2024-01-01T00:00:{i % 60:02d}Z INFO worker processed item {i}" for i in range(300)]
    secret = [f"2024-01-01T00:00:0{i}Z ERROR auth failed id={i} token={token} retry"
              for i in range(n_secret)]
    return "\n".join(base[:100] + secret + base[100:]) + "\n"


@pytest.mark.parametrize("name", TPL_PATTERNS)
def test_template_in_the_digest(env, name):
    ws, store = env
    token, body = _digit_token(name)
    digest, _ = digest_output(store, ws, "Bash", _secret_log(token))
    assert "logtemplate" in digest.splitlines()[0]
    _no_fragment(digest, body, f"{name} digest")
    assert f"[ctx:redacted:{name}:" in digest


@pytest.mark.parametrize("name", TPL_PATTERNS)
def test_template_in_span_zoom(env, name):
    from ctx._retrieval.spans import _zoom_region

    ws, store = env
    token, body = _digit_token(name)
    _, short = digest_output(store, ws, "Bash", _secret_log(token))
    manifest = store.get_manifest(short)
    blob = str(manifest["streams"]["stdout"]["blob"]).removeprefix("sha256:")
    out = "\n".join(_zoom_region(store, blob, 1, 303, 240))
    assert "templates:" in out
    _no_fragment(out, body, f"{name} zoom")


@pytest.mark.parametrize("name", TPL_PATTERNS)
def test_template_in_run_diff(env, name):
    from ctx.rundiff import run_diff

    ws, store = env
    token, body = _digit_token(name)
    _, a = digest_output(store, ws, "Bash", _secret_log(token, 0))
    _, b = digest_output(store, ws, "Bash", _secret_log(token, 3))
    out = run_diff(store, ws, f"run:{a}", f"run:{b}")
    assert "only in B" in out, out
    _no_fragment(out, body, f"{name} diff")
