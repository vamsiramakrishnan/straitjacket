"""M-G fact store + M-I Angle-lite joins (docs/ALGEBRA.md).

Glean's idea, not Glean's infra: code knowledge as typed predicates in a
queryable per-workspace SQLite store (``facts.sqlite`` beside the catalog,
WAL), written by derivation adapters, queried by bounded conjunctive
joins — no Datalog engine, no server daemon.

Predicates (tables):

    decl(symbol, kind, file, line_a, line_b, span, scope)   ← M-F skeletons
    imp(file, module)                                       ← M-F skeletons
    fail(test, failure_class, file, line, run_id, generation)
                                                            ← evidence graphs
    changed(file, generation)                               ← EDC §8 generations

The asset nobody else has: the store holds what code *is* (decl/imp,
derived from ``ctx.skeleton`` blobs) beside what code *did* (fail rows
from pytest evidence graphs) and *when* (changed rows keyed by short
generation ids) — static × dynamic × temporal, all content-keyed.

Derivation discipline:
- **Content-keyed and idempotent**: every derivation records a
  fingerprint in the ``derived`` ledger table; re-deriving unchanged
  content is a no-op (row counts stable, byte-identical query results).
- **Short ids, house style**: run ids and generation ids are stored as
  12-hex short ids (``removeprefix("sha256:")[:12]``), the same
  shortening the reflex/intervention plane and run digests use.
- **Generation tiers, honestly labeled**: ``changed(file, gen)`` rows are
  keyed by ``ctx.execution.generation_hash`` (EDC §8 operational
  identity — porcelain + untracked (path,size,mtime)); the porcelain
  file snapshot is computed here with the same exclusions.

Fail-open discipline (this module is a leaf; nothing above it may break):
- ``ctx.skeleton`` absent → decl/imp derivation degrades to a no-op;
  fail/changed planes and their joins keep working (the root-cause join
  degrades to declared file-level precision).
- ``ctx.query`` absent → the library API is fully functional; q-stage
  registration is attempted at import bottom under try/except only.
- Corrupt/garbage ``facts.sqlite`` → recreated empty on next touch.
- Public functions never raise into callers: errors are recorded in
  ``LAST_ERROR`` and surfaced as ``{"ok": False}`` / empty result sets.

Query discipline (M-I, Angle-lite): every join helper is a bounded
conjunctive query — deterministic ORDER BY, declared row cap, pure with
respect to store state. Emission goes through :func:`render_census`
(rows REQUIRED, omission declared) — one bounded digest per answer.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

from ctx.store import Store, canonical_json
from ctx.workspace import Workspace

FACTS_SCHEMA_VERSION = "ctx.facts/v1"
FACTS_DB_NAME = "facts.sqlite"

#: House short-id length — matches run short ids (`sha256:`-stripped
#: 12-hex) and the reflex plane's 12-hex intervention ids.
SHORT_ID = 12

#: Default declared cap for join results (bounded by construction).
DEFAULT_ROW_CAP = 50

#: Per-row byte bound in the census renderer.
_LINE_CAP = 200

#: Bound on the changed-file snapshot walk (mirrors execution.py's cap).
_MAX_CHANGED = 4096

#: Ledger dir excluded from porcelain snapshots (mirrors execution.py:
#: including it would mark a generation changed on our own bookkeeping).
_SNAPSHOT_EXCLUDE_DIR = ".ctx-session-reads"

#: Last swallowed error, for diagnostics only. Never raised to callers.
LAST_ERROR: str | None = None

_LOCATION_RE = re.compile(r"^(?P<file>.+):(?P<line>\d+)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decl (
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    line_a INTEGER NOT NULL,
    line_b INTEGER NOT NULL,
    span TEXT,
    scope TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file, scope, symbol, line_a)
);
CREATE INDEX IF NOT EXISTS decl_file_range ON decl(file, line_a, line_b);
CREATE TABLE IF NOT EXISTS imp (
    file TEXT NOT NULL,
    module TEXT NOT NULL,
    PRIMARY KEY (file, module)
);
CREATE TABLE IF NOT EXISTS fail (
    test TEXT NOT NULL,
    failure_class TEXT,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    generation TEXT,
    PRIMARY KEY (run_id, test, file, line)
);
CREATE INDEX IF NOT EXISTS fail_file_line ON fail(file, line);
CREATE TABLE IF NOT EXISTS changed (
    file TEXT NOT NULL,
    generation TEXT NOT NULL,
    PRIMARY KEY (generation, file)
);
CREATE TABLE IF NOT EXISTS derived (
    key TEXT PRIMARY KEY,          -- file:<rel> | run:<short> | gen:<short> | latest_*
    fingerprint TEXT NOT NULL      -- content key of the derivation input
);
"""


# ------------------------------------------------------------------ plumbing
def _note_error(where: str, exc: BaseException) -> None:
    global LAST_ERROR
    LAST_ERROR = f"{where}: {type(exc).__name__}: {exc}"


def facts_db_path(store: Store) -> Path:
    """``facts.sqlite`` lives beside the catalog in the workspace store."""
    return Path(store.root) / "indexes" / FACTS_DB_NAME


def _connect(store: Store) -> sqlite3.Connection | None:
    """Open (creating or repairing) the fact store. A corrupt database file
    is unlinked and recreated empty — facts are derived artifacts, always
    recomputable from blobs/manifests/worktree; losing them loses nothing.
    Returns None (fail-open) when even a fresh file cannot be opened."""
    path = facts_db_path(store)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _note_error("facts._connect(mkdir)", e)
        return None
    for attempt in (0, 1):
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            return conn
        except sqlite3.Error as e:
            _note_error("facts._connect", e)
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            if attempt:
                return None
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.unlink(f"{path}{suffix}")
    return None


def _short(h: Any) -> str | None:
    """House short id: strip ``sha256:``, keep 12 hex chars. Tolerates
    already-short input; returns None for empty/None."""
    s = str(h or "").removeprefix("sha256:").strip()
    return s[:SHORT_ID] or None


def _posix(p: str) -> str:
    return p.replace("\\", "/").removeprefix("./")


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT fingerprint FROM derived WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_put(conn: sqlite3.Connection, key: str, fingerprint: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO derived (key, fingerprint) VALUES (?,?)",
        (key, fingerprint),
    )


# ------------------------------------------------------- generation snapshot
def changed_files_snapshot(ws: Workspace) -> list[str]:
    """Repo-relative files that differ from HEAD right now: a ``git status
    --porcelain`` parse (the execution.generation_hash pattern — renames
    take the new side, untracked directories are walked, the session
    ledger dir is excluded, quoted paths unquoted). Sorted, bounded,
    fail-open to []."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ws.root),
            capture_output=True,
            timeout=15,
        )
        if out.returncode != 0:
            return []
        files: set[str] = set()
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            if len(line) < 4:
                continue
            rel = line[3:]
            if " -> " in rel:  # rename/copy: the new path is the changed one
                rel = rel.split(" -> ", 1)[1]
            if rel.startswith('"') and rel.endswith('"') and len(rel) >= 2:
                rel = rel[1:-1]
            if rel.rstrip("/").split("/")[0] == _SNAPSHOT_EXCLUDE_DIR:
                continue
            p = Path(ws.root) / rel
            if rel.endswith("/") or p.is_dir():
                # Porcelain lists an untracked directory as one entry.
                for sub in sorted(p.rglob("*"))[:_MAX_CHANGED]:
                    if sub.is_file():
                        with contextlib.suppress(ValueError):
                            files.add(sub.relative_to(ws.root).as_posix())
            else:
                files.add(_posix(rel.rstrip("/")))
        return sorted(files)[:_MAX_CHANGED]
    except Exception as e:
        _note_error("facts.changed_files_snapshot", e)
        return []


def current_generation(ws: Workspace) -> str | None:
    """Short id of the current source-state generation (EDC §8), via
    ``ctx.execution.generation_hash``. Read-only, lazy, fail-open None."""
    try:
        from ctx.execution import generation_hash

        return _short(generation_hash(ws.root))
    except Exception as e:
        _note_error("facts.current_generation", e)
        return None


# ---------------------------------------------------------------- derivation
def derive_file(store: Store, ws: Workspace, rel: str) -> dict[str, Any]:
    """Upsert decl/imp facts for one file from its ``ctx.skeleton/v1``.

    Content-keyed on the skeleton's source blob hash + parser: unchanged
    content is a no-op (``skipped: True``). With ``ctx.skeleton`` absent
    the decl/imp plane is honestly unavailable (``skeleton: False``) —
    nothing is recorded, nothing is marked derived, and the fail/changed
    planes are unaffected. Never raises."""
    result = {"ok": False, "file": _posix(rel), "decl": 0, "imp": 0,
              "skeleton": False, "skipped": False}
    try:
        from ctx.skeleton import skeleton_for  # frozen contract, engineer A
    except Exception as e:  # ImportError or a broken module mid-build
        _note_error("facts.derive_file(import ctx.skeleton)", e)
        result["ok"] = True  # degraded, not broken: facts simply unavailable
        return result
    conn = _connect(store)
    if conn is None:
        return result
    try:
        sk = skeleton_for(store, ws, rel)
        if not isinstance(sk, dict) or not isinstance(sk.get("symbols"), list):
            return result
        result["skeleton"] = True
        rel_stored = _posix(str(sk.get("file") or rel))
        result["file"] = rel_stored
        fingerprint = f"{sk.get('blob', '')}|{sk.get('parser', '')}|{sk.get('schema', '')}"
        key = f"file:{rel_stored}"
        if _meta_get(conn, key) == fingerprint:
            result["skipped"] = True
            result["decl"] = conn.execute(
                "SELECT COUNT(*) FROM decl WHERE file=?", (rel_stored,)
            ).fetchone()[0]
            result["imp"] = conn.execute(
                "SELECT COUNT(*) FROM imp WHERE file=?", (rel_stored,)
            ).fetchone()[0]
            result["ok"] = True
            return result
        decl_rows: list[tuple] = []
        for sym in sk["symbols"]:
            try:
                a, b = int(sym["range"][0]), int(sym["range"][1])
                decl_rows.append(
                    (
                        str(sym["name"]),
                        str(sym.get("kind") or "symbol"),
                        rel_stored,
                        a,
                        max(a, b),
                        (str(sym["span"]) if sym.get("span") else None),
                        str(sym.get("scope") or ""),
                    )
                )
            except Exception:
                continue  # one malformed symbol row never poisons the file
        imp_rows: list[tuple] = []
        for m in sk.get("imports") or []:
            mod = m.get("module") if isinstance(m, dict) else m
            if mod:
                imp_rows.append((rel_stored, str(mod)))
        with conn:
            conn.execute("DELETE FROM decl WHERE file=?", (rel_stored,))
            conn.execute("DELETE FROM imp WHERE file=?", (rel_stored,))
            conn.executemany(
                "INSERT OR REPLACE INTO decl (symbol,kind,file,line_a,line_b,span,scope) "
                "VALUES (?,?,?,?,?,?,?)",
                decl_rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO imp (file,module) VALUES (?,?)", imp_rows
            )
            _meta_put(conn, key, fingerprint)
        result.update(ok=True, decl=len(decl_rows), imp=len(imp_rows))
        return result
    except Exception as e:
        _note_error("facts.derive_file", e)
        return result
    finally:
        conn.close()


_LOCUS_LINE_RE = re.compile(r"^(?P<file>[^\s:][^:]*\.py):(?P<line>\d+):(?:\s|$)")


def _deepest_locus(out_lines: list[str], item: Any, store: Store) -> str | None:
    """The deepest ``file.py:N:`` locus line inside this item's traceback
    block (pytest prints frames shallow→deep, so the LAST locus wins).
    Block bounds come from the item's detail_ref selector (``lines:a:b``
    or ``span:<id>``). None when unresolvable — caller falls back to
    item.location. Never raises."""
    try:
        ref = getattr(item, "detail_ref", None)
        sel = str(getattr(ref, "selector", "") or "")
        a = b = None
        if sel.startswith("lines:"):
            a, b = (int(x) for x in sel[6:].split(":", 1))
        elif sel.startswith("span:"):
            span = store.get_span(sel[5:])
            a, b = span.get("a"), span.get("b")
        if not a or not b:
            return None
        best: str | None = None
        for line in out_lines[a - 1 : b]:
            m = _LOCUS_LINE_RE.match(line.strip())
            if m:
                best = f"{m.group('file')}:{m.group('line')}"
        return best
    except Exception:
        return None


def derive_run(
    store: Store, ws: Workspace, run_ref_or_manifest: str | dict[str, Any]
) -> dict[str, Any]:
    """Upsert fail facts from one captured run's pytest evidence graph.

    Accepts a run reference (``run:<id>`` / short id / full id) or an
    invocation manifest dict. Extraction is the REAL extractor
    (``ctx.digest.pytestprof.extract_pytest``) over the run's stored
    stream blobs; failure locations come from ``item.location``
    (``file:line`` traceback locus). Items without a file:line location
    are counted in ``no_location`` and skipped — never invented.

    Content-keyed on the manifest's content address (idempotent); rows
    carry the 12-hex short run id and the short generation id observed at
    derivation time (reflex id space; None when unknown). Never raises."""
    result = {"ok": False, "run": None, "fail": 0, "no_location": 0,
              "outcome": "unknown", "skipped": False}
    conn = None
    try:
        if isinstance(run_ref_or_manifest, dict):
            manifest = run_ref_or_manifest
        else:
            ref = str(run_ref_or_manifest).removeprefix("run:")
            manifest = store.get_manifest(ref)
        mid_full = str(manifest.get("id") or "").removeprefix("sha256:")
        if not mid_full:
            body = {k: v for k, v in manifest.items() if k != "id"}
            mid_full = hashlib.sha256(canonical_json(body)).hexdigest()
        run_id = mid_full[:SHORT_ID]
        result["run"] = run_id
        conn = _connect(store)
        if conn is None:
            return result
        key = f"run:{run_id}"
        if _meta_get(conn, key) == mid_full:
            with conn:
                _meta_put(conn, "latest_run", run_id)
            result["skipped"] = True
            result["fail"] = conn.execute(
                "SELECT COUNT(*) FROM fail WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            result["ok"] = True
            return result

        from ctx.digest.base import DigestContext
        from ctx.digest.pytestprof import extract_pytest

        dctx = DigestContext.load(store, ws, manifest, focus=None)
        graph = extract_pytest(dctx)
        result["outcome"] = graph.outcome
        generation = current_generation(ws)
        rows: list[tuple] = []
        out_lines = (dctx.stdout.text if dctx.stdout is not None else "").splitlines()
        for item in graph.items:
            if item.kind != "failing_test":
                continue
            # Frame semantics (found by the pre-live smoke): digests want
            # the reported locus; the JOIN wants the deepest raise frame.
            # An assertion failing in a test body locates in tests/, but a
            # ValueError raised in src/ locates there only on the block's
            # BOTTOM locus line (pytest prints deepest last). Prefer it.
            deep = _deepest_locus(out_lines, item, store)
            m = _LOCATION_RE.match(deep or str(item.location or ""))
            if not m:
                result["no_location"] += 1
                continue
            rows.append(
                (
                    str(item.id),
                    item.failure_class,
                    _posix(m.group("file")),
                    int(m.group("line")),
                    run_id,
                    generation,
                )
            )
        with conn:
            conn.execute("DELETE FROM fail WHERE run_id=?", (run_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO fail (test,failure_class,file,line,run_id,generation) "
                "VALUES (?,?,?,?,?,?)",
                rows,
            )
            _meta_put(conn, key, mid_full)
            _meta_put(conn, "latest_run", run_id)
        result.update(ok=True, fail=len(rows))
        return result
    except Exception as e:
        _note_error("facts.derive_run", e)
        return result
    finally:
        if conn is not None:
            conn.close()


def derive_generation(
    ws: Workspace,
    gen_hash: str | None = None,
    changed_files: list[str] | None = None,
    *,
    store: Store | None = None,
) -> dict[str, Any]:
    """Upsert changed(file, generation) facts for one generation.

    ``gen_hash`` may be a full ``sha256:…`` from
    ``ctx.execution.generation_hash`` or an already-short id; stored
    short (12 hex, reflex id space). ``changed_files`` None → computed
    here from a ``git status --porcelain`` snapshot
    (:func:`changed_files_snapshot`). Also records the derived
    ``latest_generation`` so queries can default to it. Content-keyed on
    the sorted changed-file list (idempotent). Never raises."""
    result = {"ok": False, "generation": None, "changed": 0, "skipped": False}
    conn = None
    try:
        store = store or Store(ws.workspace_id)
        gen = _short(gen_hash) if gen_hash else current_generation(ws)
        if gen is None:
            return result  # non-git workspace / git unavailable: no generation plane
        result["generation"] = gen
        files = (
            sorted({_posix(str(f)) for f in changed_files})[:_MAX_CHANGED]
            if changed_files is not None
            else changed_files_snapshot(ws)
        )
        conn = _connect(store)
        if conn is None:
            return result
        fingerprint = hashlib.sha256("\n".join(files).encode("utf-8")).hexdigest()
        key = f"gen:{gen}"
        skipped = _meta_get(conn, key) == fingerprint
        with conn:
            if not skipped:
                conn.execute("DELETE FROM changed WHERE generation=?", (gen,))
                conn.executemany(
                    "INSERT OR REPLACE INTO changed (file, generation) VALUES (?,?)",
                    [(f, gen) for f in files],
                )
                _meta_put(conn, key, fingerprint)
            _meta_put(conn, "latest_generation", gen)
        result.update(ok=True, changed=len(files), skipped=skipped)
        return result
    except Exception as e:
        _note_error("facts.derive_generation", e)
        return result
    finally:
        if conn is not None:
            conn.close()


def fact_counts(store: Store) -> dict[str, int]:
    """Row counts per predicate (referee/idempotency instrumentation)."""
    conn = _connect(store)
    if conn is None:
        return {}
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("decl", "imp", "fail", "changed")
        }
    except Exception as e:
        _note_error("facts.fact_counts", e)
        return {}
    finally:
        conn.close()


# ------------------------------------------------------- Angle-lite queries
def _resolve_gen(conn: sqlite3.Connection, generation: str | None) -> str | None:
    if generation:
        return _short(str(generation).removeprefix("gen:"))
    return _meta_get(conn, "latest_generation")


def _run_filter(run: str | None) -> tuple[str, tuple]:
    if not run:
        return "", ()
    return " AND f.run_id = ?", (_short(str(run).removeprefix("run:")),)


def _innermost(decls: list[tuple]) -> tuple:
    """Innermost containing decl: narrowest range, then deepest start, then
    symbol name — fully deterministic under nesting (method inside class)."""
    return sorted(decls, key=lambda d: (d[2] - d[1], -d[1], d[0]))[0]


def failing_in_changed(
    ws: Workspace,
    store: Store,
    generation: str | None = None,
    *,
    run: str | None = None,
    limit: int = DEFAULT_ROW_CAP,
) -> list[dict[str, Any]]:
    """THE ROOT-CAUSE JOIN (docs/ALGEBRA.md M-I)::

        fail(T, C, F, L), decl(S, _, F, R), within(L, R), changed(F, gen)

    Failures whose traceback locus falls inside a declared symbol of a
    file changed in ``generation`` (default: the latest derived one).
    Rows: {test, failure_class, symbol, file, line, span}, sorted by
    (file, line, test), capped at ``limit``.

    Precision tiers, declared per row: with skeleton facts the row names
    the innermost containing symbol (exact ranges). For a changed file
    with NO decl rows at all (skeleton unavailable — degraded mode) the
    join falls back to file-level membership: symbol/span are None and
    the row carries ``precision: "file-level (no skeleton facts)"``. A
    file that HAS decls but none containing the line is excluded — the
    failure is in that file but not inside any indexed symbol."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            gen = _resolve_gen(conn, generation)
            if gen is None:
                return []
            run_sql, run_args = _run_filter(run)
            fails = conn.execute(
                "SELECT DISTINCT f.test, f.failure_class, f.file, f.line "
                "FROM fail f JOIN changed c ON c.file = f.file AND c.generation = ?"
                + run_sql + " ORDER BY f.file, f.line, f.test",
                (gen, *run_args),
            ).fetchall()
            rows: list[dict[str, Any]] = []
            for test, failure_class, file, line in fails:
                decls = conn.execute(
                    "SELECT symbol, line_a, line_b, span FROM decl "
                    "WHERE file=? AND line_a<=? AND line_b>=?",
                    (file, line, line),
                ).fetchall()
                if decls:
                    symbol, _a, _b, span = _innermost(decls)
                    rows.append(
                        {"test": test, "failure_class": failure_class,
                         "symbol": symbol, "file": file, "line": line, "span": span}
                    )
                else:
                    has_any = conn.execute(
                        "SELECT 1 FROM decl WHERE file=? LIMIT 1", (file,)
                    ).fetchone()
                    if has_any is None:  # degraded tier: no skeleton facts at all
                        rows.append(
                            {"test": test, "failure_class": failure_class,
                             "symbol": None, "file": file, "line": line, "span": None,
                             "precision": "file-level (no skeleton facts)"}
                        )
            rows.sort(key=lambda r: (r["file"], r["line"], r["test"]))
            return rows[: max(1, int(limit))]
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.failing_in_changed", e)
        return []


def untouched_failures(
    ws: Workspace,
    store: Store,
    generation: str | None = None,
    *,
    run: str | None = None,
    limit: int = DEFAULT_ROW_CAP,
) -> list[dict[str, Any]]:
    """Flake/suspect triage::

        fail(T, C, F, L), not changed(F, gen)

    Failures located in files with no changed() row for ``generation``
    (default latest) — code nobody touched this generation. Returns []
    when no generation is known (an unknown changed-set must not be
    reported as 'nothing changed'). Rows sorted (file, line, test)."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            gen = _resolve_gen(conn, generation)
            if gen is None:
                return []
            run_sql, run_args = _run_filter(run)
            rows = conn.execute(
                "SELECT DISTINCT f.test, f.failure_class, f.file, f.line FROM fail f "
                "WHERE NOT EXISTS (SELECT 1 FROM changed c "
                "WHERE c.generation=? AND c.file=f.file)"
                + run_sql + " ORDER BY f.file, f.line, f.test LIMIT ?",
                (gen, *run_args, max(1, int(limit))),
            ).fetchall()
            return [
                {"test": t, "failure_class": c, "file": f, "line": ln}
                for t, c, f, ln in rows
            ]
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.untouched_failures", e)
        return []


def shared_cause_groups(
    ws: Workspace,
    store: Store,
    *,
    run: str | None = None,
    limit: int = DEFAULT_ROW_CAP,
) -> list[dict[str, Any]]:
    """Shared-cause grouping as a query (EDC §12.3's labels, derived)::

        fail(T1, C, F, _), fail(T2, C, F, _), T1 != T2

    Two grouping axes, both size ≥ 2: (file, failure_class) pairs, and —
    where skeleton facts exist — failures whose loci fall inside the same
    declared symbol (shared frame symbol; symbol-range precision).
    Rows: {group, file, failure_class|symbol, count, tests}, sorted by
    (-count, group, file, key)."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            run_sql, run_args = _run_filter(run)
            fails = conn.execute(
                "SELECT DISTINCT f.test, f.failure_class, f.file, f.line FROM fail f "
                "WHERE 1=1" + run_sql + " ORDER BY f.file, f.line, f.test",
                run_args,
            ).fetchall()
            by_class: dict[tuple[str, str], list[str]] = {}
            by_symbol: dict[tuple[str, str], list[str]] = {}
            for test, failure_class, file, line in fails:
                if failure_class:
                    by_class.setdefault((file, failure_class), []).append(test)
                decls = conn.execute(
                    "SELECT symbol, line_a, line_b, span FROM decl "
                    "WHERE file=? AND line_a<=? AND line_b>=?",
                    (file, line, line),
                ).fetchall()
                if decls:
                    by_symbol.setdefault((file, _innermost(decls)[0]), []).append(test)
            rows: list[dict[str, Any]] = []
            for (file, cls), tests in by_class.items():
                uniq = sorted(set(tests))
                if len(uniq) >= 2:
                    rows.append({"group": "file+class", "file": file,
                                 "failure_class": cls, "count": len(uniq),
                                 "tests": uniq})
            for (file, symbol), tests in by_symbol.items():
                uniq = sorted(set(tests))
                if len(uniq) >= 2:
                    rows.append({"group": "symbol", "file": file, "symbol": symbol,
                                 "count": len(uniq), "tests": uniq})
            rows.sort(
                key=lambda r: (-r["count"], r["group"], r["file"],
                               str(r.get("failure_class") or r.get("symbol") or ""))
            )
            return rows[: max(1, int(limit))]
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.shared_cause_groups", e)
        return []


def symbol_neighbors(
    ws: Workspace,
    store: Store,
    symbol: str,
    *,
    limit: int = DEFAULT_ROW_CAP,
) -> list[dict[str, Any]]:
    """Neighborhood of a symbol from decl/imp facts, v1 precision tier.

    Rows (each labeled with its honest precision):
    - ``decl``: where the symbol is declared — exact skeleton line range.
    - ``importer``: files importing the declaring file's module — v1 is
      the file→module import edge, NOT symbol-precise (a file importing
      the module may never touch this symbol).
    - ``scope-sibling``: decls sharing the declaring file+scope — a
      structural heuristic, not a reference graph."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            decls = conn.execute(
                "SELECT symbol, kind, file, line_a, line_b, span, scope FROM decl "
                "WHERE symbol=? ORDER BY file, line_a",
                (str(symbol),),
            ).fetchall()
            rows: list[dict[str, Any]] = []
            imp_all = conn.execute(
                "SELECT file, module FROM imp ORDER BY file, module LIMIT ?",
                (_MAX_CHANGED,),
            ).fetchall()
            seen_importers: set[tuple[str, str]] = set()
            sibling_rows: list[dict[str, Any]] = []
            for name, kind, file, a, b, span, scope in decls:
                rows.append(
                    {"rel": "decl", "symbol": name, "kind": kind, "file": file,
                     "lines": f"{a}-{b}", "span": span,
                     "precision": "exact (skeleton line range)"}
                )
                stem = Path(file).stem
                dotted = _posix(file)
                dotted = dotted[:-3].replace("/", ".") if dotted.endswith(".py") else stem
                for imp_file, module in imp_all:
                    if imp_file == file:
                        continue
                    if module in (stem, dotted) or module.endswith("." + stem):
                        key = (imp_file, module)
                        if key not in seen_importers:
                            seen_importers.add(key)
                            rows.append(
                                {"rel": "importer", "file": imp_file, "module": module,
                                 "precision": "file→module import edge "
                                              "(v1: module-level, not symbol-precise)"}
                            )
                for s_name, s_kind, s_a, s_b, s_span in conn.execute(
                    "SELECT symbol, kind, line_a, line_b, span FROM decl "
                    "WHERE file=? AND scope=? AND symbol != ? ORDER BY line_a, symbol",
                    (file, scope, name),
                ).fetchall():
                    sibling_rows.append(
                        {"rel": "scope-sibling", "symbol": s_name, "kind": s_kind,
                         "file": file, "lines": f"{s_a}-{s_b}", "span": s_span,
                         "precision": "same file+scope (v1 structural heuristic)"}
                    )
            rows.extend(sibling_rows)
            return rows[: max(1, int(limit))]
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.symbol_neighbors", e)
        return []


def fails_sites(
    ws: Workspace,
    store: Store,
    *,
    run: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Failure sites for the q algebra: {file, line, test, failure_class,
    text}. ``run`` None → the last derived run; if none is derived yet the
    newest captured run manifest is derived on demand (bounded, fail-open).
    Sorted (file, line, test)."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            run_id = _short(str(run).removeprefix("run:")) if run else _meta_get(
                conn, "latest_run"
            )
        finally:
            conn.close()
        if run_id is not None:
            rows = _fails_for(store, run_id)
            if rows:
                return rows[: max(1, int(limit))]
        # Not derived yet: derive on demand (explicit ref, or newest capture).
        derived = derive_run(store, ws, f"run:{run_id}") if run_id else _derive_newest(
            store, ws
        )
        if not derived.get("ok"):
            return []
        rows = _fails_for(store, str(derived.get("run") or run_id))
        return rows[: max(1, int(limit))]
    except Exception as e:
        _note_error("facts.fails_sites", e)
        return []


def _fails_for(store: Store, run_id: str) -> list[dict[str, Any]]:
    conn = _connect(store)
    if conn is None:
        return []
    try:
        got = conn.execute(
            "SELECT test, failure_class, file, line FROM fail WHERE run_id=? "
            "ORDER BY file, line, test",
            (run_id,),
        ).fetchall()
        return [
            {"file": f, "line": ln, "test": t, "failure_class": c,
             "text": f"{c or '?'}: {t}"}
            for t, c, f, ln in got
        ]
    finally:
        conn.close()


def _derive_newest(store: Store, ws: Workspace) -> dict[str, Any]:
    """Derive the newest captured run ('last' semantics). created_at is
    operational metadata — it selects WHICH run to derive, never enters
    fact content."""
    try:
        row = store.db.execute(
            "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC, id LIMIT 1"
        ).fetchone()
        if row is None:
            return {"ok": False}
        return derive_run(store, ws, row[0])
    except Exception as e:
        _note_error("facts._derive_newest", e)
        return {"ok": False}


def sites_in_changed(
    ws: Workspace,
    store: Store,
    sites: list[dict[str, Any]],
    generation: str | None = None,
) -> list[dict[str, Any]]:
    """Filter arbitrary (file, line) sites through the root-cause join:
    keep sites inside a declared symbol of a changed file (annotated with
    symbol/span), with the same declared file-level degradation as
    :func:`failing_in_changed` when a changed file has no decl rows."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            gen = _resolve_gen(conn, generation)
            if gen is None:
                return []
            changed = {
                r[0]
                for r in conn.execute(
                    "SELECT file FROM changed WHERE generation=?", (gen,)
                ).fetchall()
            }
            out: list[dict[str, Any]] = []
            for site in sites:
                file = _posix(str(site.get("file") or ""))
                if file not in changed:
                    continue
                try:
                    line = int(site.get("line"))
                except (TypeError, ValueError):
                    continue
                decls = conn.execute(
                    "SELECT symbol, line_a, line_b, span FROM decl "
                    "WHERE file=? AND line_a<=? AND line_b>=?",
                    (file, line, line),
                ).fetchall()
                if decls:
                    symbol, _a, _b, span = _innermost(decls)
                    out.append({**site, "symbol": symbol, "span": span})
                elif (
                    conn.execute(
                        "SELECT 1 FROM decl WHERE file=? LIMIT 1", (file,)
                    ).fetchone()
                    is None
                ):
                    out.append(
                        {**site, "symbol": None, "span": None,
                         "precision": "file-level (no skeleton facts)"}
                    )
            return out
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.sites_in_changed", e)
        return []


def decls_rows(
    ws: Workspace,
    store: Store,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Declared symbols as a bounded stream. Rows: {symbol, kind, file,
    line, line_b, scope, span}, sorted (file, line, symbol)."""
    try:
        conn = _connect(store)
        if conn is None:
            return []
        try:
            sql = ("SELECT symbol, kind, file, line_a, line_b, scope, span FROM decl "
                   + ("WHERE kind=? " if kind else "")
                   + "ORDER BY file, line_a, symbol LIMIT ?")
            args: tuple = (kind, max(1, int(limit))) if kind else (max(1, int(limit)),)
            return [
                {"symbol": s, "kind": k, "file": f, "line": a, "line_b": b,
                 "scope": sc, "span": sp}
                for s, k, f, a, b, sc, sp in conn.execute(sql, args).fetchall()
            ]
        finally:
            conn.close()
    except Exception as e:
        _note_error("facts.decls_rows", e)
        return []


# ------------------------------------------------------------ census render
def render_census(
    rows: list[dict[str, Any]],
    *,
    kind: str = "rows",
    total: int | None = None,
    cap: int = DEFAULT_ROW_CAP,
) -> str:
    """One bounded digest for a query answer, EDC-style: every row shown is
    a census line (REQUIRED identity, never re-parsed text), every row not
    shown is declared. Deterministic: rows render in list order with each
    row's own key order (queries build rows in fixed key order)."""
    rows = list(rows)
    total = len(rows) if total is None else max(int(total), len(rows))
    shown = rows[: max(0, int(cap))]
    lines = [f"[facts census · {kind} · {len(shown)} of {total} rows]"]
    for r in shown:
        parts = []
        for k, v in r.items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            parts.append(f"{k}={v}")
        lines.append(("  " + " · ".join(parts))[:_LINE_CAP])
    if not shown:
        lines.append("  (no rows)")
    omitted = total - len(shown)
    if omitted > 0:
        lines.append(f"  … +{omitted} rows omitted (declared)")
    return "\n".join(lines)


# ------------------------------------------------------------- q stages (M-H)
# Stage functions follow the FROZEN ctx.query convention:
# fn(qc, stream, args) -> Stream, with qc carrying .ws/.store. ctx.query is
# imported lazily inside each stage — the stages only ever run when the
# query engine itself invoked them, so the import cannot fail there; the
# library API above never touches ctx.query.
def _stage_fails(qc, stream, args: list[str]):
    from ctx.query import Stream

    run: str | None = None
    for a in args:
        if a == "last" or a.startswith("--"):
            continue
        run = a
    return Stream("sites", fails_sites(qc.ws, qc.store, run=run))


def _stage_in_changed(qc, stream, args: list[str]):
    from ctx.query import Stream

    generation: str | None = None
    for a in args:
        if not a.startswith("--"):
            generation = a
    # Live-path auto-derive (found by the pre-live smoke, not the referee:
    # the referee derived explicitly). With no explicit generation, ensure
    # the current worktree's changed() facts exist — derive_generation is
    # content-keyed/idempotent, so repeated calls are cheap no-ops — and
    # derive decl facts for the changed files so the join has symbol
    # precision instead of the file-level degradation.
    if generation is None:
        try:
            derived = derive_generation(qc.ws, store=qc.store)
            for rel in changed_files_snapshot(qc.ws) if derived.get("ok") else []:
                derive_file(qc.store, qc.ws, rel)
        except Exception as e:
            _note_error("facts.stage_in_changed.autoderive", e)
    rows = sites_in_changed(qc.ws, qc.store, stream.rows, generation)
    return Stream("sites", rows, omitted=stream.omitted)


def _stage_decls(qc, stream, args: list[str]):
    from ctx.query import Stream, _flag

    kind = _flag(args, "--kind", None)
    return Stream("symbols", decls_rows(qc.ws, qc.store, kind=kind))


def _stage_shared_cause(qc, stream, args: list[str]):
    from ctx.query import Stream

    groups: dict[tuple[str, str], list[str]] = {}
    for r in stream.rows:
        file = str(r.get("file") or "")
        cls = str(r.get("failure_class") or "?")
        who = str(r.get("test") or r.get("symbol") or r.get("text") or "?")
        groups.setdefault((file, cls), []).append(who)
    rows = [
        {"group": "file+class", "file": file, "failure_class": cls,
         "count": len(sorted(set(tests))), "tests": sorted(set(tests))}
        for (file, cls), tests in groups.items()
        if len(set(tests)) >= 2
    ]
    rows.sort(key=lambda r: (-r["count"], r["file"], r["failure_class"]))
    return Stream("records", rows, omitted=stream.omitted)


def register_facts_stages(register: Callable | None = None) -> bool:
    """Register the fact-store stages against the q registry (late-bound).
    With ``register`` given (tests / alternative registries) no import of
    ctx.query happens. Returns False instead of raising when the registry
    is absent or refuses — the library API is unaffected either way."""
    try:
        if register is None:
            from ctx.query import register_stage as register  # frozen contract
        register(
            "fails", _stage_fails, input_kinds=(), output_kind="sites",
            doc="fails [run:<id>|last] — failing-test sites from the fact store",
        )
        register(
            "in-changed", _stage_in_changed, input_kinds=("sites",),
            output_kind="sites",
            doc="in-changed [gen:<id>] — sites inside symbols of files changed "
                "in a generation (the root-cause join)",
        )
        register(
            "decls", _stage_decls, input_kinds=(), output_kind="symbols",
            doc="decls [--kind k] — declared symbols from the fact store",
        )
        register(
            "shared-cause", _stage_shared_cause, input_kinds=("sites",),
            output_kind="records",
            doc="shared-cause — group failure sites by (file, failure class)",
        )
        return True
    except Exception as e:
        _note_error("facts.register_facts_stages", e)
        return False


# Late-binding registration at import bottom (docs/ALGEBRA.md M-H): ctx.query
# imports ctx.facts lazily; equally, importing ctx.facts standalone must not
# require ctx.query to exist or be healthy.
try:
    Q_STAGES_REGISTERED = register_facts_stages()
except Exception:  # pragma: no cover — register_facts_stages already shields
    Q_STAGES_REGISTERED = False


__all__ = [
    "FACTS_SCHEMA_VERSION",
    "FACTS_DB_NAME",
    "DEFAULT_ROW_CAP",
    "LAST_ERROR",
    "facts_db_path",
    "changed_files_snapshot",
    "current_generation",
    "derive_file",
    "derive_run",
    "derive_generation",
    "fact_counts",
    "failing_in_changed",
    "untouched_failures",
    "shared_cause_groups",
    "symbol_neighbors",
    "fails_sites",
    "sites_in_changed",
    "decls_rows",
    "render_census",
    "register_facts_stages",
    "Q_STAGES_REGISTERED",
]
