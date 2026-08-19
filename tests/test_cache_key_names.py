"""``_short`` / ``_cache_key`` name collisions and cache-invalidation bases (R13).

Two findings, both correctness hazards rather than naming nits.

**One name, two meanings.** ``facts._short`` shortens a *content hash*
(strip ``sha256:``, keep 12 hex); ``lintprof._short`` / ``searchprof._short``
shorten a *filesystem path* to its last two components. Reading either
module you cannot tell which ``_short`` you are looking at, and the two are
not interchangeable in any direction. They are now ``_short_id`` and the
single shared ``ctx.textutil.short_path``.

**Four ``_cache_key``s, two invalidation bases.** ``skeleton`` keys on the
source blob *hash* (content). ``repomap``, ``callgraph`` and ``plan_exec``
key on one shared invalidation fingerprint. Its original ``(path, size,
mtime_ns)`` basis was defeated by ``os.utime`` and timestamp-preserving tools.
Adding ``st_ctime_ns`` still was not portable: an overlay or network filesystem
can report the same nanosecond ctime for both states. The shared basis now
includes a content digest, so caches and the rewrite guard agree on actual
bytes rather than timestamp behavior.
"""

import hashlib
import os
from pathlib import Path

from conftest import make_ws


def _same_size_edit_restoring_mtime(path: Path, new_text: str) -> None:
    """Content changes, size does not, mtime is put back. Exactly what an
    mtime+size cache key cannot see."""
    st = path.stat()
    old = path.read_text(encoding="utf-8")
    assert len(new_text) == len(old), "the point is a same-SIZE edit"
    path.write_text(new_text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert after.st_size == st.st_size and after.st_mtime_ns == st.st_mtime_ns


# ------------------------------------------------------- invalidation basis
def test_stat_fingerprint_survives_an_mtime_restoring_edit(workspace_dir):
    from ctx.workspace import stat_fingerprint

    f = workspace_dir / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    h1 = hashlib.sha256()
    stat_fingerprint(workspace_dir, ["a.py"], h1)

    _same_size_edit_restoring_mtime(f, "x = 2\n")

    h2 = hashlib.sha256()
    stat_fingerprint(workspace_dir, ["a.py"], h2)
    assert h1.hexdigest() != h2.hexdigest()


def test_repomap_and_callgraph_share_one_stat_basis(workspace_dir, state_home):
    """Both caches were keyed by the same hand-copied stat loop; they now
    call one definition, so they cannot drift apart again."""
    from ctx import callgraph, repomap

    (workspace_dir / "a.py").write_text("def f():\n    g()\n", encoding="utf-8")
    ws = make_ws(workspace_dir)

    k1 = repomap._map_cache_key(ws, ["a.py"], 600, "", [], False, "ast")
    c1 = callgraph._unit_key(ws, "a.py")

    _same_size_edit_restoring_mtime(workspace_dir / "a.py", "def f():\n    h()\n")

    assert repomap._map_cache_key(ws, ["a.py"], 600, "", [], False, "ast") != k1
    # The callgraph key is now per file (v2) rather than per corpus, but the
    # basis is still the one shared stat_fingerprint — an mtime-restoring,
    # same-length edit must still invalidate it.
    assert callgraph._unit_key(ws, "a.py") != c1


def test_plan_node_fingerprint_survives_an_mtime_restoring_edit(git_workspace):
    from ctx.plan_exec import _workspace_fingerprint
    from ctx.workspace import resolve_workspace

    f = git_workspace / "hello.py"
    f.write_text("print('hello')\n", encoding="utf-8")  # modify tracked file
    ws = resolve_workspace(str(git_workspace))
    (git_workspace / "note.txt").write_text("aaaa\n", encoding="utf-8")
    f1 = _workspace_fingerprint(ws)
    _same_size_edit_restoring_mtime(git_workspace / "note.txt", "bbbb\n")
    assert _workspace_fingerprint(ws) != f1


def test_skeleton_key_stays_content_addressed(workspace_dir, state_home):
    """The one cache that must NOT move to stat: its key is the blob hash,
    which is strictly stronger. Documented, not reconciled away."""
    from ctx.skeleton import _skeleton_cache_key

    a = _skeleton_cache_key(hashlib.sha256(b"x = 1\n").hexdigest(), "a.py")
    b = _skeleton_cache_key(hashlib.sha256(b"x = 2\n").hexdigest(), "a.py")
    c = _skeleton_cache_key(hashlib.sha256(b"x = 1\n").hexdigest(), "b.py")
    assert len({a, b, c}) == 3


# --------------------------------------------------------- the name collision
def test_short_names_no_longer_collide():
    from ctx.digest import lintprof, searchprof
    from ctx.facts import _short_id
    from ctx.textutil import short_path

    assert _short_id("sha256:" + "a" * 64) == "a" * 12
    assert short_path("src/ctx/digest/lintprof.py") == "digest/lintprof.py"
    assert short_path("a.py") == "a.py"
    assert short_path("src\\ctx\\a.py") == "ctx/a.py"
    # The two digest profiles now share one definition rather than two
    # byte-identical nested copies under a name that means something else
    # in ctx.facts.
    assert lintprof.short_path is short_path
    assert searchprof.short_path is short_path
    assert not hasattr(__import__("ctx.facts", fromlist=["x"]), "_short")


def test_cache_key_names_are_distinct():
    """No two modules may define a bare ``_cache_key`` again — the four
    meant four different keys over two different invalidation bases."""
    import ctx.callgraph
    import ctx.plan_exec
    import ctx.repomap
    import ctx.skeleton

    for mod in (ctx.repomap, ctx.callgraph, ctx.plan_exec, ctx.skeleton):
        assert not hasattr(mod, "_cache_key"), mod.__name__
