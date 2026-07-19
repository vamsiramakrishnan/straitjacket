"""M-F tree-sitter skeleton tier (docs/ALGEBRA.md): frozen schema, derived-
blob caching, backend chain (tree-sitter → ctags → ast → none), priced
outline rendering, and the stats integration for non-Python code files."""

import hashlib
import json
import shutil

import pytest

from conftest import make_store, make_ws

HAS_CTAGS = shutil.which("ctags") is not None


def _has_tree_sitter() -> bool:
    for mod in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            __import__(mod)
            return True
        except ImportError:
            continue
    return False


HAS_TS = _has_tree_sitter()

TS_SOURCE = """\
import { helper } from "./util";
import * as fs from "fs";

export interface Greeting {
  text: string;
}

export class Greeter {
  private name: string;

  constructor(name: string) {
    this.name = name;
  }

  greet(loud: boolean): string {
    return helper(this.name, loud);
  }
}

export function makeGreeter(name: string): Greeter {
  return new Greeter(name);
}

export const arrowThing = (x: number): number => x * 2;
"""

GO_SOURCE = """\
package main

import "fmt"

type Server struct {
\tPort int
}

func (s *Server) Start() error {
\tfmt.Println(s.Port)
\treturn nil
}

func main() {
\ts := &Server{Port: 8080}
\t_ = s.Start()
}
"""

RUST_SOURCE = """\
use std::fmt;

pub struct Token {
    pub value: String,
}

pub trait Emit {
    fn emit(&self) -> String;
}

impl Emit for Token {
    fn emit(&self) -> String {
        self.value.clone()
    }
}

pub fn tokenize(s: &str) -> Vec<Token> {
    vec![]
}
"""

PY_SOURCE = """\
import os
from pathlib import Path

def top(a, b):
    return a + b

class Thing:
    def method(self):
        return os.name
"""


def _no_tree_sitter(monkeypatch):
    """Force the chain past the tree-sitter backend regardless of installs."""
    import ctx.skeleton as skel

    def raiser(*a, **k):
        raise skel.BackendUnavailable("disabled for test")

    monkeypatch.setattr(skel, "_tree_sitter_extract", raiser)


def _no_ctags(monkeypatch):
    import ctx.skeleton as skel

    monkeypatch.setattr(skel, "_ctags_path", lambda: None)


@pytest.fixture()
def env(state_home, workspace_dir):
    (workspace_dir / "greeter.ts").write_text(TS_SOURCE, encoding="utf-8")
    (workspace_dir / "server.go").write_text(GO_SOURCE, encoding="utf-8")
    (workspace_dir / "lib.rs").write_text(RUST_SOURCE, encoding="utf-8")
    (workspace_dir / "sample.py").write_text(PY_SOURCE, encoding="utf-8")
    ws = make_ws(workspace_dir)
    return make_store(ws), ws


# ------------------------------------------------------------ frozen schema
@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_schema_conformance(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    assert set(sk) == {"schema", "file", "blob", "language", "parser", "symbols", "imports"}
    assert sk["schema"] == "ctx.skeleton/v1"
    assert sk["file"] == "greeter.ts"  # repo-relative, POSIX
    expected = hashlib.sha256(TS_SOURCE.encode("utf-8")).hexdigest()
    assert sk["blob"] == f"sha256:{expected}"  # keyed by SOURCE blob hash
    assert sk["language"] == "typescript"
    assert sk["parser"] == "ctags"
    assert isinstance(sk["imports"], list)
    for sym in sk["symbols"]:
        assert set(sym) == {"name", "kind", "signature", "range", "scope", "span"}
        a, b = sym["range"]
        assert 1 <= a <= b
        assert sym["span"]  # minted span, bodies retrievable
    # Deterministic ordering: by range then name.
    keys = [(s["range"][0], s["range"][1], s["name"], s["kind"]) for s in sk["symbols"]]
    assert keys == sorted(keys)


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_determinism_and_cache_hit(env, monkeypatch):
    import ctx.skeleton as skel
    from ctx.store import canonical_json

    _no_tree_sitter(monkeypatch)
    store, ws = env
    calls = {"n": 0}
    real = skel._run_ctags

    def counting(argv):
        calls["n"] += 1
        return real(argv)

    monkeypatch.setattr(skel, "_run_ctags", counting)
    sk1 = skel.skeleton_for(store, ws, "greeter.ts")
    sk2 = skel.skeleton_for(store, ws, "greeter.ts")
    assert calls["n"] == 1  # same source bytes ⇒ zero recompute (cache hit)
    assert sk1 == sk2
    assert canonical_json(sk1) == canonical_json(sk2)  # same skeleton bytes
    # The derived blob itself is in the store, content-keyed.
    skel_hash = hashlib.sha256(canonical_json(sk1)).hexdigest()
    assert store.get_blob(skel_hash) == canonical_json(sk1)


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_changed_content_recomputes(env, monkeypatch, workspace_dir):
    import ctx.skeleton as skel

    _no_tree_sitter(monkeypatch)
    store, ws = env
    calls = {"n": 0}
    real = skel._run_ctags

    def counting(argv):
        calls["n"] += 1
        return real(argv)

    monkeypatch.setattr(skel, "_run_ctags", counting)
    sk1 = skel.skeleton_for(store, ws, "greeter.ts")
    (workspace_dir / "greeter.ts").write_text(
        TS_SOURCE + "\nexport function extra(): void {}\n", encoding="utf-8"
    )
    sk2 = skel.skeleton_for(store, ws, "greeter.ts")
    assert calls["n"] == 2  # new bytes ⇒ new parse
    assert sk1["blob"] != sk2["blob"]
    assert "extra" in {s["name"] for s in sk2["symbols"]}


# ------------------------------------------------------- ctags backend live
@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_ctags_typescript_symbols_and_scopes(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    by_name = {s["name"]: s for s in sk["symbols"]}
    assert {"Greeter", "Greeting", "greet", "makeGreeter", "arrowThing"} <= set(by_name)
    assert by_name["Greeter"]["kind"] == "class"
    assert by_name["Greeting"]["kind"] == "interface"
    assert by_name["greet"]["scope"] == "Greeter"  # scope → parent
    assert by_name["makeGreeter"]["scope"] is None
    assert by_name["makeGreeter"]["signature"].startswith("export function makeGreeter")
    # Container range extended over its scoped children (best-effort ranges).
    assert by_name["Greeter"]["range"][1] >= by_name["greet"]["range"][0]
    assert sk["imports"] == []  # imports never come from ctags (non-python)


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_ctags_go_symbols(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "server.go")
    assert sk["parser"] == "ctags" and sk["language"] == "go"
    syms = {(s["name"], s["kind"]) for s in sk["symbols"]}
    assert ("Server", "struct") in syms
    assert ("Start", "func") in syms
    assert ("main", "func") in syms
    start = next(s for s in sk["symbols"] if s["name"] == "Start")
    assert start["scope"] == "Server"  # ctags scope main.Server → parent Server
    assert start["range"] == [9, 12]  # go parser emits exact end


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_ctags_rust_symbols(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "lib.rs")
    assert sk["parser"] == "ctags" and sk["language"] == "rust"
    by = {(s["name"], s["scope"]) for s in sk["symbols"]}
    assert ("Token", None) in by  # struct
    assert ("tokenize", None) in by
    assert ("emit", "Token") in by  # impl method scoped to its type
    assert ("emit", "Emit") in by  # trait method scoped to the trait


# ------------------------------------------------------------ outline (EDC)
@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_outline_census_before_detail(env, monkeypatch):
    from ctx.skeleton import skeleton_for, skeleton_outline

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    out = skeleton_outline(sk, 1200)
    assert out.startswith("[ctx skeleton repo:greeter.ts]")
    assert "parser ctags" in out
    # REQUIRED census: every symbol identity present, priced and addressable.
    for sym in sk["symbols"]:
        assert sym["name"] in out
        assert f"span {sym['span']}" in out
    assert "L" in out and "ctx get repo:greeter.ts --lines" in out


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_outline_budget_cap_declares_omission(env, monkeypatch):
    from ctx.skeleton import skeleton_for, skeleton_outline
    from ctx.textutil import estimate_tokens

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    full = skeleton_outline(sk, 10_000)
    capped = skeleton_outline(sk, 90)
    assert capped != full
    assert "omitted (budget)" in capped  # declared, never silent
    assert estimate_tokens(len(capped.encode())) <= 90
    # Census-of-census: the omission line carries group-by-kind counts.
    if "symbols omitted (budget):" in capped:
        tail = capped.split("symbols omitted (budget):", 1)[1]
        assert ":" in tail


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_outline_deterministic(env, monkeypatch):
    from ctx.skeleton import skeleton_for, skeleton_outline

    _no_tree_sitter(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    assert skeleton_outline(sk, 300) == skeleton_outline(sk, 300)


# --------------------------------------------------------- python ast exact
def test_python_ast_backend_exact_ranges_and_imports(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    _no_ctags(monkeypatch)
    store, ws = env
    sk = skeleton_for(store, ws, "sample.py")
    assert sk["parser"] == "ast"
    by_name = {s["name"]: s for s in sk["symbols"]}
    assert by_name["top"]["range"] == [4, 5]  # exact end_lineno
    assert by_name["top"]["kind"] == "function"
    assert by_name["Thing"]["range"] == [7, 9]
    assert by_name["Thing"]["kind"] == "class"
    assert by_name["method"]["range"] == [8, 9]
    assert by_name["method"]["scope"] == "Thing"
    assert by_name["method"]["kind"] == "method"
    assert by_name["top"]["signature"] == "def top(a, b):"
    assert sk["imports"] == ["os", "pathlib"]


# ----------------------------------------------------------- fallback chain
def test_fallback_chain_ctags_absent(env, monkeypatch):
    from ctx.skeleton import skeleton_for

    _no_tree_sitter(monkeypatch)
    _no_ctags(monkeypatch)
    store, ws = env
    py = skeleton_for(store, ws, "sample.py")
    assert py["parser"] == "ast"  # python degrades to stdlib ast
    ts = skeleton_for(store, ws, "greeter.ts")
    assert ts["parser"] == "none"  # non-python with no backend: declared none
    assert ts["symbols"] == [] and ts["imports"] == []
    assert ts["language"] == "typescript"


def test_unknown_language_is_none_parser(env, monkeypatch, workspace_dir):
    from ctx.skeleton import skeleton_for

    (workspace_dir / "notes.txt").write_text("just prose\n", encoding="utf-8")
    store, ws = env
    sk = skeleton_for(store, ws, "notes.txt")
    assert sk["parser"] == "none" and sk["language"] is None
    assert sk["symbols"] == []


# ------------------------------------------------------------- tree-sitter
@pytest.mark.skipif(not HAS_TS, reason="tree-sitter bindings not installed ([code] extra)")
def test_tree_sitter_python_extraction():
    from ctx.skeleton import _tree_sitter_extract

    symbols, imports = _tree_sitter_extract(PY_SOURCE, "python")
    by_name = {s["name"]: s for s in symbols}
    assert by_name["top"]["kind"] == "function"
    assert by_name["Thing"]["kind"] == "class"
    assert by_name["method"]["scope"] == "Thing"
    assert by_name["top"]["range"][0] == 4
    assert "os" in imports and "pathlib" in imports


@pytest.mark.skipif(not HAS_TS, reason="tree-sitter bindings not installed ([code] extra)")
def test_tree_sitter_typescript_extraction():
    from ctx.skeleton import _tree_sitter_extract

    symbols, imports = _tree_sitter_extract(TS_SOURCE, "typescript")
    by_name = {s["name"]: s for s in symbols}
    assert by_name["Greeter"]["kind"] == "class"
    assert by_name["greet"]["scope"] == "Greeter"
    assert by_name["Greeting"]["kind"] == "interface"
    assert by_name["makeGreeter"]["kind"] == "function"
    assert by_name["arrowThing"]["kind"] == "function"  # arrow value
    assert "./util" in imports and "fs" in imports


@pytest.mark.skipif(not HAS_TS, reason="tree-sitter bindings not installed ([code] extra)")
def test_tree_sitter_backend_selected_when_importable(env):
    from ctx.skeleton import skeleton_for

    store, ws = env
    sk = skeleton_for(store, ws, "greeter.ts")
    assert sk["parser"] == "tree-sitter"


# ----------------------------------------------------------- abs-path leaks
@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_no_absolute_path_in_skeleton_or_outline(env, monkeypatch, tmp_path):
    from ctx.skeleton import skeleton_for, skeleton_outline
    from ctx.store import canonical_json

    _no_tree_sitter(monkeypatch)
    store, ws = env
    for rel in ("greeter.ts", "server.go", "lib.rs"):
        sk = skeleton_for(store, ws, rel)
        raw = canonical_json(sk).decode("utf-8")
        out = skeleton_outline(sk, 1200)
        for leak in (str(ws.root), str(tmp_path), "/tmp"):
            assert leak not in raw
            assert leak not in out


# ----------------------------------------------- stats integration (M-F §4)
def test_python_stats_outline_byte_compat(env):
    """The existing exact Python outline path is untouched — stats() output
    for a .py file is byte-identical to _stats_outline's rendering."""
    from ctx._retrieval.stats import _stats_outline
    from ctx.retrieval import stats

    store, ws = env
    via_stats = stats(store, ws, "repo:sample.py")
    direct = _stats_outline(store, ws, "sample.py")
    assert via_stats == direct
    assert "outline (priced):" in via_stats
    assert "[ctx stats repo:sample.py]" in via_stats


@pytest.mark.skipif(not HAS_CTAGS, reason="universal-ctags not installed")
def test_stats_typescript_routes_to_skeleton_outline(env, monkeypatch):
    from ctx.retrieval import stats

    _no_tree_sitter(monkeypatch)
    store, ws = env
    out = stats(store, ws, "repo:greeter.ts")
    assert "[ctx skeleton repo:greeter.ts]" in out
    assert "Greeter" in out and "makeGreeter" in out
    assert "span " in out


def test_stats_unsupported_file_falls_through_unchanged(env, monkeypatch, workspace_dir):
    from ctx.retrieval import stats

    (workspace_dir / "notes.txt").write_text("just prose\n", encoding="utf-8")
    store, ws = env
    out = stats(store, ws, "repo:notes.txt")
    assert "[ctx skeleton" not in out
    assert "files (exact):" in out  # the pre-existing aggregate path


def test_stats_ts_without_any_backend_falls_through(env, monkeypatch):
    from ctx.retrieval import stats

    _no_tree_sitter(monkeypatch)
    _no_ctags(monkeypatch)
    store, ws = env
    out = stats(store, ws, "repo:greeter.ts")
    assert "[ctx skeleton" not in out
    assert "files (exact):" in out
