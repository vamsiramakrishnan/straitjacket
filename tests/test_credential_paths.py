"""Well-known credential files are secret paths, through every door.

`.netrc`, `.npmrc` and the other tools' documented credential files were
read without the secret-path treatment. Each is now secret for the Read
tool and for every shell reader, on every host, whether it sits in the home
directory or in the workspace (a project's `.npmrc` can carry a registry
token as literally as the home one). Ordinary project files stay ordinary.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from ctx import hook

CREDENTIAL_FILES = [
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".docker/config.json",
    ".kube/config", ".config/gh/hosts.yml", ".aws/credentials", ".aws/config",
    ".cargo/credentials", ".cargo/credentials.toml", ".gem/credentials", ".terraformrc",
    "terraform.rc", "prod.tfvars", "infra/vars.tfvars.json",
]
ORDINARY = ["package.json", "config.yml", "docker/config.json", "kube/config", "netrc.py",
            "src/npmrc_parser.py", "terraform.rcx", "notes/tfvars.md", "npmrc.md"]
#: Backups, copies and other letter cases of the names, as `.env.bak` is.
VARIANTS = [".npmrc.bak", ".netrc~", ".npmrc.old", ".NPMRC", "TERRAFORM.TFVARS",
            ".pypirc.orig", ".kube/config.backup", "prod.tfvars.example"]
HOSTS = ("claude-code", "antigravity", "codex")
SECRET_REASON = "CTX_CONTEXT_GUARD: secret-bearing path"


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("CTX_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "ctx.toml").write_text("version = 1\n", encoding="utf-8")
    for rel in CREDENTIAL_FILES + ORDINARY + VARIANTS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    return root


def _emit(flavor: str, payload: dict) -> dict:
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        hook.main_pre_tool_use(flavor)
        return json.loads(sys.stdout.getvalue())
    finally:
        sys.stdin, sys.stdout = stdin, stdout


def _payload(flavor, ws, *, command=None, path=None):
    if flavor == "antigravity":
        name, args = (("run_command", {"CommandLine": command}) if command is not None
                      else ("read_file", {"Path": path}))
        return {"conversationId": "c", "cwd": str(ws), "hookEventName": "PreToolUse",
                "workspacePaths": [str(ws)], "toolCall": {"name": name, "args": args, "id": "1"}}
    tool, ti = (("Bash", {"command": command}) if command is not None
                else ("Read", {"file_path": path}))
    return {"session_id": "s", "cwd": str(ws), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": ti}


def _secret_answer(flavor: str, out: dict) -> bool:
    if flavor == "antigravity":
        return out.get("decision") == "force_ask" and out.get("reason", "").startswith(SECRET_REASON)
    hso = out.get("hookSpecificOutput") or {}
    want = "deny" if flavor == "codex" else "ask"
    return (hso.get("permissionDecision") == want
            and hso.get("permissionDecisionReason", "").startswith(SECRET_REASON))


@pytest.mark.parametrize("rel", CREDENTIAL_FILES)
def test_credential_files_are_secret_for_the_read_tool_and_every_reader(ws, rel):
    for flavor in HOSTS:
        assert _secret_answer(flavor, _emit(flavor, _payload(flavor, ws, path=str(ws / rel)))), \
            (flavor, rel)
        for cmd in (f"cat {rel}", f"head -n 3 {rel}", f"grep x {rel}", f"sed -n 1p {rel}",
                    f"sort {rel}", f"sudo cat {rel}", f"cat {rel} | wc -l"):
            out = _emit(flavor, _payload(flavor, ws, command=cmd))
            assert _secret_answer(flavor, out), (flavor, cmd, out)


@pytest.mark.parametrize("name", [".netrc", "_netrc", ".npmrc", ".pypirc", ".docker/config.json",
                                  ".kube/config", ".config/gh/hosts.yml"])
def test_home_and_workspace_copies_follow_one_rule(ws, tmp_path, name):
    home = tmp_path / "home"
    (home / name).parent.mkdir(parents=True, exist_ok=True)
    (home / name).write_text("x\n", encoding="utf-8")
    for flavor in HOSTS:
        for path in (str(home / name), str(ws / name)):
            assert _secret_answer(flavor, _emit(flavor, _payload(flavor, ws, path=path)))
            assert _secret_answer(flavor, _emit(flavor, _payload(flavor, ws, command=f"cat {path}")))


@pytest.mark.parametrize("rel", ORDINARY)
def test_ordinary_project_files_are_not_secret(ws, rel):
    assert not hook._is_secret_path(str(ws / rel), str(ws))
    out = _emit("antigravity", _payload("antigravity", ws, path=str(ws / rel)))
    assert out["decision"] == "allow", out


def test_ctx_get_refuses_the_same_files(ws):
    from ctx.config import load_ctxignore
    from ctx.pathglob import matches

    globs = load_ctxignore(ws)
    for rel in CREDENTIAL_FILES:
        assert any(matches(rel, g) for g in globs), rel
    for rel in ORDINARY:
        assert not any(matches(rel, g) for g in globs), rel


def test_bearer_tokens_are_redacted_and_ordinary_text_is_not():
    from ctx.config import Redaction
    from ctx.textutil import sanitize_for_model

    short_jwt = "eyJhbGciOiJIUzI1NiJ9.e30.ZRrHA1JJJW8opsbCGfG_HACGpVUMN_a9IV7pAx_Zmeo"
    out, fired = sanitize_for_model(f"Authorization: Bearer {short_jwt}", Redaction())
    assert short_jwt not in out and "bearer-token" in fired
    out, fired = sanitize_for_model('curl -H "authorization: bearer abcdefghijklmnop1234"',
                                    Redaction())
    assert "abcdefghijklmnop1234" not in out
    for text in ("Authorization: Bearer <token>", 'WWW-Authenticate: Bearer realm="api"',
                 "the bearer of bad news", "connect(password=password)",
                 "secret = workspace_dir / 'x'", "password=None"):
        assert sanitize_for_model(text, Redaction()) == (text, []), text


def test_bearer_pattern_leaves_the_repositorys_fixtures_alone():
    from pathlib import Path

    from ctx.textutil import REDACTION_PATTERNS

    rx = REDACTION_PATTERNS["bearer-token"]
    root = Path(__file__).resolve().parent
    for p in [*(root / "fixtures").rglob("*"), *(root.parent / "docs").rglob("*.md")]:
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert not rx.search(text), p


@pytest.mark.parametrize("rel", VARIANTS)
def test_backups_and_case_variants_are_secret(ws, rel):
    assert hook._is_secret_path(str(ws / rel), str(ws))
    for flavor in HOSTS:
        assert _secret_answer(flavor, _emit(flavor, _payload(flavor, ws, path=str(ws / rel))))
        assert _secret_answer(flavor, _emit(flavor, _payload(flavor, ws, command=f"cat {rel}")))


def test_bearer_prose_joined_by_hyphens_or_slashes_is_not_a_token():
    from ctx.config import Redaction
    from ctx.textutil import sanitize_for_model

    for text in ("see Bearer authentication-schemes/oauth2-bearer-tokens",
                 "use Bearer token-based-auth for this", "Bearer api/v2/authorization-header"):
        assert sanitize_for_model(text, Redaction()) == (text, []), text
    for token in ("abcdefghijklmnop1234", "550e8400-e29b-41d4-a716-446655440000",
                  "eyJhbGciOiJIUzI1NiJ9.e30.ZRrHA1JJJW8opsbCGfG_HACGpVUMN_a9IV7pAx_Zmeo"):
        out, fired = sanitize_for_model(f"Authorization: Bearer {token}", Redaction())
        assert token not in out and fired, token
