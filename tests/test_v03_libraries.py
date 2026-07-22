"""v0.3 acceptance: rg engine parity, pathspec semantics, secret ruleset,
schema validation."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from conftest import make_store, make_ws

HAS_RG = shutil.which("rg") is not None
HAS_JSONSCHEMA = True
try:
    import jsonschema  # noqa: F401
except ImportError:
    HAS_JSONSCHEMA = False


def _seed_repo(root: Path):
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.py").write_text(
        "def run():\n    raise TimeoutError('risk-api')\n", encoding="utf-8"
    )
    (root / "src" / "util.js").write_text("// TimeoutError note\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\nTimeoutError\n", encoding="utf-8")


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_engine_used_and_matches(state_home, workspace_dir):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_repo(workspace_dir)
    out = search(store, ws, "repo:", ["TimeoutError"])
    assert "src/app.py" in out and "src/util.js" in out
    assert ".env" not in out.split("patterns:")[1].split("coverage:")[0]  # deny glob wins
    assert "snapshot:" in out


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_and_python_engines_agree(state_home, workspace_dir, monkeypatch):
    from ctx.retrieval import search

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    _seed_repo(workspace_dir)

    out_rg = search(store, ws, "repo:", ["TimeoutError"], glob="**/*.py", context=1)
    monkeypatch.setenv("CTX_SEARCH_ENGINE", "python")
    out_py = search(store, ws, "repo:", ["TimeoutError"], glob="**/*.py", context=1)

    def evidence(text):
        return [
            ln for ln in text.splitlines()
            if ln.startswith((" >L", "  L")) or ln.endswith(":") and "/" in ln
        ]

    assert evidence(out_rg) == evidence(out_py)
    assert "matches: 1" in out_rg and "matches: 1" in out_py
    # M-K1 span parity: identical columns ⇒ identical ctx.search/v1 result
    # blobs (the per-result provenance handle agrees across engines).
    def result_blob(text):
        return [ln for ln in text.splitlines() if ln.startswith("result: blob:")]

    assert result_blob(out_rg) == result_blob(out_py) and result_blob(out_rg)


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_scope_and_all_semantics(state_home, tmp_path):
    from ctx.retrieval import search
    from ctx.workspace import resolve_workspace

    root = tmp_path / "mono2"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "ctx.toml").write_text('version = 1\n[scopes.only-a]\nroots = ["a"]\n', encoding="utf-8")
    (root / "a" / "x.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (root / "b" / "y.txt").write_text("alpha\n", encoding="utf-8")
    ws = resolve_workspace(str(root))
    store = make_store(ws)

    out = search(store, ws, "repo:", ["alpha"], scope="only-a")
    assert "a/x.txt" in out and "b/y.txt" not in out

    out_all = search(store, ws, "repo:", ["alpha", "beta"], mode_all=True)
    assert "a/x.txt" in out_all and "b/y.txt" not in out_all


def test_pathspec_gitignore_semantics(workspace_dir):
    ws = make_ws(workspace_dir)
    # Bare name matches at any depth (gitignore rule) — fnmatch alone missed this.
    assert ws.is_ignored(".env")
    assert ws.is_ignored("deep/nested/.env")
    assert ws.is_ignored("a/b/secrets/token.txt")
    assert ws.is_ignored("x/creds.pem")
    assert not ws.is_ignored("src/app.py")


def test_expanded_secret_ruleset(state_home, workspace_dir):
    from ctx.textutil import redact
    from ctx.config import Redaction

    patterns = Redaction().patterns
    samples = {
        "github-token": "github_pat_" + "A" * 70,
        "stripe-key": "sk_live_" + "a1B2" * 6,
        "gitlab-token": "glpat-" + "x" * 22,
        "slack-token": "xoxb-123456789012-abcdefghijkl",
        "anthropic-key": "sk-ant-" + "k" * 24,
        "npm-token": "npm_" + "a" * 36,
    }
    for name, secret in samples.items():
        text, fired = redact(f"deploy failed: token {secret} rejected", patterns)
        assert secret not in text, name
        assert fired, name
        assert "ctx:redacted:" in text


def test_redaction_fires_in_digest_end_to_end(state_home, workspace_dir):
    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    secret = "github_pat_" + "B" * 70
    (workspace_dir / "leak.txt").write_text(f"ERROR auth {secret}\n", encoding="utf-8")
    cap = run_capture(
        ws, [sys.executable, "-c", "print(open('leak.txt').read())"], store=store
    )
    digest, _ = render_run_digest(store, ws, cap.manifest)
    assert secret not in digest
    assert "redaction: applied" in digest


@pytest.mark.skipif(not HAS_JSONSCHEMA, reason="jsonschema not installed")
def test_manifest_validates_against_wire_schema(state_home, workspace_dir):
    import jsonschema

    from ctx.digest import render_run_digest
    from ctx.execution import run_capture

    ws = make_ws(workspace_dir)
    store = make_store(ws)
    cap = run_capture(ws, [sys.executable, "-c", "print('x')"], store=store)
    _, manifest = render_run_digest(store, ws, cap.manifest)

    schema_path = Path(__file__).resolve().parent.parent / "spec" / "schemas" / "invocation-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)  # raises on drift


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_doctor_reports_engines(state_home, workspace_dir):
    from ctx.installer import doctor_report

    ws = make_ws(workspace_dir)
    report = doctor_report(ws)
    assert "ripgrep" in report
    assert "pathspec" in report
