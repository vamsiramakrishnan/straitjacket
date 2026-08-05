"""Performance investigation for src/ctx/store.py (perf task, 2026-07-19).

House rule (CONTRIBUTING.md / evals/hotpath-profile-2026-07-18.md): every
optimization needs a measured receipt, and a measured-no-win change gets
reverted and reported as rejected rather than merged "because it should be
faster." This file carries three things:

  1. Correctness pins for the three changes actually shipped in store.py:
     - resolve_id()'s hex-prefix range-scan fast path (replacing a plain
       ``LIKE 'prefix%'`` that never used the id index — see the docstring
       on resolve_id for why).
     - gc()'s batched catalog delete (one transaction + executemany instead
       of a `with self.db:` commit per dead object).
     - line_index()'s in-process cache (``Store._line_index_cache``): found
       while investigating mmap for read_blob_lines/get_blob — every call
       was re-reading and re-parsing the on-disk ``.idx`` sidecar from
       scratch even when nothing had changed, because blobs (and therefore
       their line index) are immutable once written.
  2. Correctness pins for the alternatives that were investigated and
     *rejected*, so the investigation doesn't have to be redone blind if
     someone revisits this file later.
  3. A benchmark harness (the `test_benchmark_*` functions) that prints
     timing numbers for the report but asserts only correctness/shape —
     these must never fail just because a machine happened to be fast or
     slow, per the "asserts only correctness" instruction.

Perf receipts (interactive investigation, container Python 3.11.15 /
SQLite 3.45.1, warm page cache):

    operation                            corpus                before        after         verdict
    ------------------------------------ --------------------- ------------- ------------- -----------------
    line_index build (bytes.find loop)   100 MB / 1.52M lines   ~290 ms       ~290 ms       kept (see rejects)
    line_index()/read_blob_lines(),      20 MB blob, repeated   ~624 us/call  ~0.2 us/call  ADOPTED (~3600x)
      2nd+ call on the same blob         calls, same blob       (line_index)  (line_index)
                                                                 ~519 us/call  ~16 us/call   (~32x, read_blob_lines)
    resolve_id() prefix lookup           50k objects, 1 kind    ~3.2-3.8 ms   ~5.5-10 us    ADOPTED  (~500x)
    gc() catalog sweep                   50k obj / ~25k dead    3973 ms       1061 ms       ADOPTED  (~3.7x)
    "newest N of a kind" query           50k objects/4107 runs  2.148 ms      0.012 ms      ADOPTED  (~180x)
      (perf task 2026-07-24; LIMIT 40)   objects_kind + TEMP    covering objects_kind_recent
                                         B-TREE FOR ORDER BY    seek, no sort

    Every production reader of the kind index really asks for "the newest N
    of this kind" (facts.py, policy.py, repomap.py, installer.py):
    ``WHERE kind='run' ORDER BY created_at DESC, id LIMIT ?``. objects(kind)
    answered only the WHERE half and left SQLite sorting every matching row
    in a temp b-tree to take the first few; objects(kind, created_at DESC,
    id) carries the order and the selected column, so the plan becomes a
    covering seek that stops at the LIMIT. The single-column objects(kind)
    index was DROPPED in the same change: it is a strict prefix of the
    composite, the planner picked the composite even while both existed, and
    keeping both cost 50.4 vs 44.6 us per single-row INSERT and ~0.7 MB per
    50k objects. Dropping an index destroys no data and is reversible — an
    older ctx opening the same catalog recreates it from its own IF NOT
    EXISTS schema. (Not to be confused with the rejected objects(created_at)
    index below: that one was for gc's unselective full-range scan, which a
    non-covering index makes slower, not for a LIMIT-ed ordered lookup.)

    REJECTED (measured, no win, not shipped):
    - line_index via bytes.split(b"\\n") + cumulative offsets: ~1.5% faster
      median (288 vs 292 ms) but ~15x peak memory (12.5 MB -> 186.7 MB on
      a 100 MB blob, tracemalloc) from materializing every line as its own
      bytes object. Not worth it.
    - line_index via bytes.splitlines(keepends=True): a *behavior* change,
      not just perf — bytes.splitlines() also breaks on a lone b"\\r"
      (see test_line_index_splitlines_alternative_diverges_on_lone_cr
      below), which the production b"\\n"-only scan does not. Rejected
      outright regardless of speed, since it would silently change what a
      "line" is for any blob containing bare carriage returns (e.g.
      progress-bar output).
    - mmap-backed get_blob()/read_blob_lines() above a size threshold:
      mmap.mmap() was slower than plain open()+seek()+read() at every
      corpus size tried (a 10-line slice, a 1000-line slice, a 100k-line
      slice, and a full 100 MB read) in this container. No size threshold
      tested showed a crossover, so no mmap path was added; the byte-
      equality pin below is kept as the receipt that the *option* was
      sound (would have been correct to add) even though it lost.
      (First pass at this benchmark showed mmap *winning* a 1000-line
      slice by ~17x — that turned out to be the line_index() disk-reload
      cost above leaking into the "plain" arm's timing, not a real mmap
      advantage; after the in-process cache above removed that confound,
      plain read won again, consistently.)
    - sqlite indexes on leases(expires_at) / objects(created_at) for gc's
      two full-table gc queries: EXPLAIN QUERY PLAN showed the new index
      changed "SCAN objects" into "SEARCH ... USING INDEX objects_created"
      but *slower* in practice (8.2 ms -> 22.1 ms) because the index isn't
      covering — every matching row needs a second lookup back into the
      table, and the created_at cutoff is not selective enough (~1/3 of
      rows match) to pay for that indirection. The leases(expires_at)
      index was a wash (6.7 ms -> 6.5 ms, noise). Neither index was added.
    - WAL + synchronous=NORMAL (existing pragmas): already the standard
      recommended pairing for this write pattern (many small transactions,
      crash-safe, no full fsync-per-commit); nothing to change.
    - Task 4 (hoist per-call regex/constant work, slots=True on frozen
      dataclasses): store.py has no regex and no dataclasses at all
      (grepped) — not applicable, nothing to hoist.
"""

from __future__ import annotations

import array
import hashlib
import mmap
import random
import sqlite3
import time

import pytest

from ctx.store import AmbiguousIdError, Store, UnknownIdError

# --------------------------------------------------------------------------
# Reference (non-production) implementations used only to pin the
# investigation's findings — never imported by store.py itself.
# --------------------------------------------------------------------------


def _line_index_find_loop(data: bytes) -> array.array:
    """The production algorithm, reproduced standalone for comparison."""
    arr = array.array("Q")
    arr.append(0)
    pos = data.find(b"\n")
    while pos != -1:
        arr.append(pos + 1)
        pos = data.find(b"\n", pos + 1)
    if arr[-1] != len(data):
        arr.append(len(data))
    return arr


def _line_index_split_on_n(data: bytes) -> array.array:
    """Rejected alternative: same b"\\n"-only delimiter semantics, ~1.5%
    faster median but ~15x peak memory. Kept only for the parity pin."""
    parts = data.split(b"\n")
    arr = array.array("Q")
    arr.append(0)
    offset = 0
    for part in parts[:-1]:
        offset += len(part) + 1
        arr.append(offset)
    if arr[-1] != len(data):
        arr.append(len(data))
    return arr


def _line_index_splitlines_keepends(data: bytes) -> array.array:
    """Rejected alternative: also splits on a lone b"\\r", diverging from
    the production b"\\n"-only scan. Kept only to demonstrate the bug."""
    arr = array.array("Q")
    arr.append(0)
    offset = 0
    for line in data.splitlines(keepends=True):
        offset += len(line)
        arr.append(offset)
    if arr[-1] != len(data):
        arr.append(len(data))
    return arr


LINE_INDEX_EDGE_CASES = {
    "empty": b"",
    "no_trailing_newline": b"abc\ndef\nghi",
    "trailing_newline": b"abc\ndef\n",
    "single_huge_line": b"x" * 500_000,
    "only_newlines": b"\n\n\n",
    "lone_cr_no_lf": b"abc\rdef\nghi",
    "crlf": b"abc\r\ndef\r\n",
}


# --------------------------------------------------------------------------
# 1. Correctness pins for shipped changes
# --------------------------------------------------------------------------


def test_line_index_edge_cases_match_split_on_n_alternative(state_home):
    """The one alternative that was byte-identical in semantics (split on
    b"\\n" only) must produce exactly the same array as the production
    find-loop on every edge case — proving the rejection was purely a
    memory/perf call, not a hidden correctness gap."""
    store = Store("perf-line-index")
    for name, blob in LINE_INDEX_EDGE_CASES.items():
        h = store.put_blob(blob)
        produced = store.line_index(h)
        reference = _line_index_find_loop(blob)
        alt = _line_index_split_on_n(blob)
        assert list(produced) == list(reference), name
        assert list(produced) == list(alt), name


def test_line_index_splitlines_alternative_diverges_on_lone_cr(state_home):
    """Documents *why* bytes.splitlines(keepends=True) was rejected: it is
    not equivalent to the production algorithm for a blob containing a bare
    b"\\r" not followed by b"\\n" (e.g. a progress-bar line)."""
    blob = LINE_INDEX_EDGE_CASES["lone_cr_no_lf"]
    store = Store("perf-line-index-2")
    h = store.put_blob(blob)
    produced = list(store.line_index(h))
    splitlines_alt = list(_line_index_splitlines_keepends(blob))
    assert produced == [0, 8, 11]  # \n-only: one boundary, at index 7+1
    assert splitlines_alt == [0, 4, 8, 11]  # splitlines also breaks at \r
    assert produced != splitlines_alt


def test_resolve_id_hex_fastpath_matches_reference_like_scan(state_home):
    """resolve_id()'s new id-range scan must return exactly what the
    original ``id LIKE 'prefix%'`` scan would, for every prefix length and
    every ambiguity outcome (unique / ambiguous / unknown)."""
    store = Store("perf-resolve-id")
    ids = [hashlib.sha256(f"obj-{i}".encode()).hexdigest() for i in range(400)]
    with store.db:
        store.db.executemany(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,0,'{}')",
            [(i, "blob" if n % 2 == 0 else "run") for n, i in enumerate(ids)],
        )

    def reference_like(short, kinds=None):
        if kinds:
            q = (
                "SELECT id FROM objects WHERE id LIKE ? AND kind IN "
                f"({','.join('?' * len(kinds))}) ORDER BY id"
            )
            rows = store.db.execute(q, (short + "%", *kinds)).fetchall()
        else:
            rows = store.db.execute(
                "SELECT id FROM objects WHERE id LIKE ? ORDER BY id", (short + "%",)
            ).fetchall()
        return [r[0] for r in rows]

    rng = random.Random(11)
    for plen in (6, 8, 12, 20, 40, 64):
        for _ in range(15):
            full = rng.choice(ids)
            short = full[:plen]
            if plen == 64:
                # Unchanged pre-existing short-circuit: a full 64-char id is
                # returned as-is without touching the db or the kind filter
                # at all (not part of this perf change) — not comparable to
                # the LIKE-scan reference below.
                assert store.resolve_id(short, kinds=("snapshot",)) == short
                continue
            for kinds in (None, ("blob",), ("run",), ("blob", "run"), ("snapshot",)):
                expected = reference_like(short, kinds)
                if not expected:
                    with pytest.raises(UnknownIdError):
                        store.resolve_id(short, kinds=kinds)
                elif len(expected) > 1:
                    with pytest.raises(AmbiguousIdError):
                        store.resolve_id(short, kinds=kinds)
                else:
                    assert store.resolve_id(short, kinds=kinds) == expected[0]


def test_resolve_id_nonhex_input_falls_back_and_still_works(state_home):
    """A non-hex short id (never a real object id, but not rejected either)
    must still take the LIKE path and behave exactly as before: no crash,
    correct UnknownIdError."""
    store = Store("perf-resolve-id-nonhex")
    store.put_blob(b"anything")
    with pytest.raises(UnknownIdError):
        store.resolve_id("not-hex!")


def test_resolve_id_full_64char_and_too_short_unchanged(state_home):
    store = Store("perf-resolve-id-lengths")
    full = store.put_blob(b"payload")
    assert store.resolve_id(full) == full
    assert store.resolve_id("sha256:" + full) == full
    from ctx.store import StoreError

    with pytest.raises(StoreError):
        store.resolve_id("abc")


def test_gc_batched_delete_matches_row_by_row_reference(state_home, tmp_path):
    """gc()'s batched executemany delete must leave the catalog in exactly
    the state the original per-object `with self.db:` loop would have —
    same surviving ids, same removed counts, leases cleaned identically."""

    def build(store, n, seed):
        rng = random.Random(seed)
        now = time.time()
        rows = []
        for i in range(n):
            h = hashlib.sha256(f"o-{seed}-{i}".encode()).hexdigest()
            kind = ["run", "blob", "snapshot"][i % 3]
            created = now - rng.uniform(0, 90 * 86400)
            rows.append((h, kind, created, "{}"))
        with store.db:
            store.db.executemany(
                "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,?,?)", rows
            )
        leases = []
        for i in range(0, n, 7):
            leases.append((rows[i][0], "pin", None))
        for i in range(1, n, 4):
            leases.append((rows[i][0], "retention", now + rng.choice([-5, 40]) * 86400))
        with store.db:
            store.db.executemany(
                "INSERT INTO leases (id, reason, expires_at) VALUES (?,?,?)", leases
            )
        return rows

    def gc_row_by_row_reference(store, retention_days):
        """The pre-optimization algorithm: one `with self.db:` commit per
        dead object, reproduced standalone as the correctness reference."""
        now = time.time()
        cutoff = now - retention_days * 86400
        leased = {
            r[0]
            for r in store.db.execute(
                "SELECT id FROM leases WHERE expires_at IS NULL OR expires_at > ?", (now,)
            )
        }
        recent = {
            r[0] for r in store.db.execute("SELECT id FROM objects WHERE created_at >= ?", (cutoff,))
        }
        live = leased | recent
        removed_blobs = removed_manifests = 0
        for obj_id, kind in store.db.execute("SELECT id, kind FROM objects").fetchall():
            if obj_id in live:
                continue
            if kind == "blob":
                removed_blobs += 1
            else:
                removed_manifests += 1
            with store.db:
                store.db.execute("DELETE FROM objects WHERE id=?", (obj_id,))
                store.db.execute("DELETE FROM leases WHERE id=?", (obj_id,))
        return {"blobs_removed": removed_blobs, "manifests_removed": removed_manifests}

    store_a = Store("perf-gc-reference", state_root=tmp_path / "state-a")
    store_b = Store("perf-gc-shipped", state_root=tmp_path / "state-b")
    build(store_a, 600, seed=101)
    build(store_b, 600, seed=101)  # identical content, same seed

    result_ref = gc_row_by_row_reference(store_a, retention_days=30)
    result_shipped = store_b.gc(retention_days=30)  # the real, batched method

    assert result_ref == result_shipped
    remaining_a = sorted(r[0] for r in store_a.db.execute("SELECT id FROM objects"))
    remaining_b = sorted(r[0] for r in store_b.db.execute("SELECT id FROM objects"))
    assert remaining_a == remaining_b
    leases_a = sorted(store_a.db.execute("SELECT id, reason FROM leases"))
    leases_b = sorted(store_b.db.execute("SELECT id, reason FROM leases"))
    assert leases_a == leases_b


def test_gc_pin_still_survives_with_batched_delete(state_home, workspace_dir):
    """End-to-end sanity check using the real put_blob/pin/gc surface (not
    just direct catalog rows), matching the pre-existing gc acceptance
    tests' shape."""
    store = Store("perf-gc-pin")
    keep = store.put_blob(b"keep-me")
    drop = store.put_blob(b"drop-me")
    store.pin(keep)
    with store.db:
        store.db.execute("UPDATE objects SET created_at = 0")
    result = store.gc(retention_days=1)
    assert result["blobs_removed"] == 1
    assert store.get_blob(keep) == b"keep-me"
    with pytest.raises(UnknownIdError):
        store.get_blob(drop)


def test_line_index_in_process_cache_matches_fresh_disk_read(state_home, tmp_path):
    """The in-process cache must never diverge from what a cold process
    (no cache warm) would read straight off the on-disk .idx sidecar —
    and repeated calls on a warm cache must return the identical array
    object (proving the cache path is actually taken, not just correct)."""
    root = tmp_path / "state"
    store = Store("perf-line-cache", state_root=root)
    blob = b"a\nbb\nccc\n" * 5000
    h = store.put_blob(blob)

    first = store.line_index(h)
    second = store.line_index(h)
    assert first is second  # same object: served from _line_index_cache

    # A second Store instance (no warm in-process cache) reading the same
    # on-disk state must reconstruct the identical array from the .idx
    # sidecar that the first instance's cold call already wrote.
    cold_store = Store("perf-line-cache", state_root=root)
    cold = cold_store.line_index(h)
    assert list(cold) == list(first)


# --------------------------------------------------------------------------
# 2. mmap-path byte-equality (investigated, rejected on perf — see module
#    docstring; kept as the receipt that the option was at least correct).
# --------------------------------------------------------------------------


def test_mmap_read_byte_identical_to_plain_read_path(state_home):
    store = Store("perf-mmap")
    rng = random.Random(5)
    blob = b"\n".join(
        bytes(rng.randint(32, 126) for _ in range(rng.randint(10, 200))) for _ in range(20_000)
    )
    h = store.put_blob(blob)
    idx = store.line_index(h)
    n_lines = len(idx) - 1
    path = store.blob_path(h)

    plain = store.read_blob_lines(h, 100, 150)
    with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        mmapped = bytes(mm[idx[99] : idx[150]])
    assert plain == mmapped

    plain_whole = store.get_blob(h)
    with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        mmapped_whole = bytes(mm[:])
    assert plain_whole == mmapped_whole == blob
    assert n_lines > 0  # sanity: corpus actually produced multiple lines


# --------------------------------------------------------------------------
# 3. Benchmark harness — prints numbers for the report, asserts only
#    correctness/shape so it can never fail on a slow/fast machine.
# --------------------------------------------------------------------------


def _gen_line_corpus(total_bytes: int) -> bytes:
    """Fast, deterministic corpus: a repeated fixed-content line. Content
    doesn't matter for a byte-offset-scan benchmark, only newline
    positions and total size, so this avoids paying random-byte generation
    cost in the checked-in test suite."""
    line = b"the quick brown fox jumps over the lazy dog 0123456789\n"
    reps = max(1, total_bytes // len(line))
    return line * reps


def test_benchmark_line_index_build(state_home):
    store = Store("perf-bench-line-index")
    data = _gen_line_corpus(20_000_000)  # 20 MB: real signal, still test-suite-fast
    expected_lines = data.count(b"\n")
    h = store.put_blob(data)

    t0 = time.perf_counter()
    idx = store.line_index(h)
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[bench] line_index build, {len(data)/1e6:.1f} MB: {dt_ms:.1f} ms")

    assert len(idx) - 1 == expected_lines


def test_benchmark_resolve_id_prefix_lookup(state_home):
    store = Store("perf-bench-resolve-id")
    n = 50_000
    ids = [hashlib.sha256(f"bench-{i}".encode()).hexdigest() for i in range(n)]
    with store.db:
        store.db.executemany(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,0,'{}')",
            [(i, ["run", "blob", "snapshot", "search", "checkpoint"][n_ % 5], ) for n_, i in enumerate(ids)],
        )
    rng = random.Random(9)
    sample = rng.sample(ids, 25)

    t0 = time.perf_counter()
    for full in sample:
        got = store.resolve_id(full[:12])
        assert got == full
    dt_us = (time.perf_counter() - t0) / len(sample) * 1e6
    print(f"[bench] resolve_id prefix lookup, {n} objects: {dt_us:.1f} us/lookup avg")


def test_benchmark_gc_sweep(state_home):
    store = Store("perf-bench-gc")
    n = 50_000
    rng = random.Random(13)
    now = time.time()
    rows = []
    for i in range(n):
        h = hashlib.sha256(f"gc-bench-{i}".encode()).hexdigest()
        kind = ["run", "blob", "snapshot"][i % 3]
        created = now - rng.uniform(0, 90 * 86400)
        rows.append((h, kind, created, "{}"))
    with store.db:
        store.db.executemany(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,?,?)", rows
        )
    leases = []
    for i in range(0, n, 10):
        leases.append((rows[i][0], "pin", None))
    for i in range(1, n, 3):
        leases.append((rows[i][0], "retention", now + rng.choice([-5, 40]) * 86400))
    with store.db:
        store.db.executemany("INSERT INTO leases (id, reason, expires_at) VALUES (?,?,?)", leases)

    t0 = time.perf_counter()
    result = store.gc(retention_days=30)
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"[bench] gc() sweep, {n} objects: {dt_ms:.1f} ms, removed={result}")

    assert set(result) == {"blobs_removed", "manifests_removed"}
    assert result["blobs_removed"] + result["manifests_removed"] > 0
    remaining = store.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
    assert remaining + result["blobs_removed"] + result["manifests_removed"] == n


def test_benchmark_mmap_vs_plain_read(state_home):
    """Prints the mmap-vs-plain-read timing receipt behind the "REJECTED"
    entry in the module docstring; asserts only byte equality."""
    store = Store("perf-bench-mmap")
    data = _gen_line_corpus(20_000_000)
    h = store.put_blob(data)
    path = store.blob_path(h)
    idx = store.line_index(h)
    mid = (len(idx) - 1) // 2

    t0 = time.perf_counter()
    for _ in range(20):
        plain = store.read_blob_lines(h, mid, mid + 1000)
    t_plain = (time.perf_counter() - t0) / 20 * 1e6

    t0 = time.perf_counter()
    for _ in range(20):
        with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            mmapped = bytes(mm[idx[mid - 1] : idx[mid + 1000]])
    t_mmap = (time.perf_counter() - t0) / 20 * 1e6

    print(f"[bench] 1000-line slice of {len(data)/1e6:.0f} MB blob: "
          f"plain={t_plain:.1f} us  mmap={t_mmap:.1f} us")
    assert plain == mmapped


# --------------------------------------------------------------------------
# objects(kind, created_at DESC, id) — the "newest N of this kind" index
# (perf task, 2026-07-24). See the receipts row in the module docstring.
# --------------------------------------------------------------------------

# The catalog schema exactly as it stood before the composite index: used to
# build a pre-change database on disk and to time the "before" arm.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT NOT NULL,
    reason TEXT NOT NULL,
    expires_at REAL,
    PRIMARY KEY (id, reason)
);
"""

# The shape every production reader of the kind index actually uses
# (facts.py, policy.py, repomap.py, installer.py).
RECENT_RUNS_SQL = (
    "SELECT id FROM objects WHERE kind='run' ORDER BY created_at DESC, id LIMIT 40"
)


def _object_indexes(db) -> set[str]:
    return {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='objects'"
        )
    }


def test_objects_kind_recent_index_exists_and_supersedes_objects_kind(state_home):
    """The composite exists; the single-column prefix index is gone.

    objects(kind) is a strict prefix of objects(kind, created_at DESC, id):
    any plan it could serve, the composite serves at least as well, so
    carrying both only paid extra INSERT and file-size cost."""
    store = Store("perf-kind-index")
    names = _object_indexes(store.db)
    assert "objects_kind_recent" in names
    assert "objects_kind" not in names
    sql = store.db.execute(
        "SELECT sql FROM sqlite_master WHERE name='objects_kind_recent'"
    ).fetchone()[0]
    assert "kind" in sql and "created_at DESC" in sql and "id" in sql


def test_recent_runs_query_plan_is_a_covering_index_seek(state_home):
    """EXPLAIN QUERY PLAN: index seek, no temp b-tree sort — the before/after
    that motivated the index (2.213 ms -> 0.012 ms on a 50k catalog)."""
    store = Store("perf-kind-index-plan")
    now = time.time()
    with store.db:
        store.db.executemany(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,?,'{}')",
            [
                (hashlib.sha256(f"plan-{i}".encode()).hexdigest(),
                 "run" if i % 3 == 0 else "blob", now - i)
                for i in range(2000)
            ],
        )
    plan = " ".join(r[3] for r in store.db.execute("EXPLAIN QUERY PLAN " + RECENT_RUNS_SQL))
    assert "objects_kind_recent" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_legacy_catalog_opens_cleanly_and_upgrades_in_place(state_home, tmp_path):
    """A catalog written before this change must open, keep every row, and
    pick up the new index — no destructive migration, no rebuild."""
    root = tmp_path / "legacy" / "workspaces" / "legacy-ws" / "indexes"
    root.mkdir(parents=True)
    now = time.time()
    legacy_ids = [hashlib.sha256(f"legacy-{i}".encode()).hexdigest() for i in range(50)]
    conn = sqlite3.connect(root / "catalog.sqlite3")
    conn.executescript(LEGACY_SCHEMA)
    with conn:
        conn.executemany(
            "INSERT INTO objects (id, kind, created_at, meta) VALUES (?,?,?,'{}')",
            [(h, "run", now - n) for n, h in enumerate(legacy_ids)],
        )
        conn.execute(
            "INSERT INTO leases (id, reason, expires_at) VALUES (?,'pin',NULL)",
            (legacy_ids[0],),
        )
    assert "objects_kind" in _object_indexes(conn)
    conn.close()

    store = Store("legacy-ws", state_root=tmp_path / "legacy")
    # Rows and leases survive; index swapped; newest-first order still right.
    assert store.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 50
    assert store.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1
    assert "objects_kind_recent" in _object_indexes(store.db)
    assert "objects_kind" not in _object_indexes(store.db)
    newest = [r[0] for r in store.db.execute(RECENT_RUNS_SQL)]
    assert newest[:3] == legacy_ids[:3]
    # And the store still works normally on top of the upgraded catalog.
    h = store.put_blob(b"after the upgrade")
    assert store.resolve_id(h[:12]) == h
    assert store.get_blob(h[:12]) == b"after the upgrade"
    assert set(store.gc(retention_days=30)) == {"blobs_removed", "manifests_removed"}


def test_benchmark_recent_runs_query(state_home, tmp_path):
    """Prints the receipt behind the composite index; asserts only that both
    index layouts return exactly the same rows."""
    n = 50_000
    rng = random.Random(3)
    now = time.time()
    kinds = ["blob"] * 40 + ["run"] * 5 + ["snapshot"] * 10 + ["search"] * 5
    rows = [
        (hashlib.sha256(f"kindbench-{i}".encode()).hexdigest(),
         rng.choice(kinds), now - rng.random() * 1e6, "{}")
        for i in range(n)
    ]

    legacy = sqlite3.connect(tmp_path / "legacy-catalog.sqlite3")
    legacy.executescript(LEGACY_SCHEMA)
    with legacy:
        legacy.executemany("INSERT OR IGNORE INTO objects VALUES (?,?,?,?)", rows)
    legacy.execute("ANALYZE")

    store = Store("perf-bench-kind-index")
    with store.db:
        store.db.executemany("INSERT OR IGNORE INTO objects VALUES (?,?,?,?)", rows)
    store.db.execute("ANALYZE")

    results = {}
    for tag, db in (("before (objects_kind)", legacy), ("after (composite)", store.db)):
        results[tag] = db.execute(RECENT_RUNS_SQL).fetchall()
        samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            db.execute(RECENT_RUNS_SQL).fetchall()
            samples.append((time.perf_counter() - t0) * 1000)
        samples.sort()
        plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + RECENT_RUNS_SQL))
        print(f"[bench] recent runs, {n} objects, {tag}: "
              f"{samples[len(samples) // 2]:.3f} ms median  plan={plan}")
    legacy.close()

    assert results["before (objects_kind)"] == results["after (composite)"]
