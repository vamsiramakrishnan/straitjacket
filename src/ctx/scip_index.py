"""Generating the SCIP index that `ctx.scip_ingest` was written to read.

``scip_ingest`` has always sat at the top of the ``refs`` engine ladder as the
exact, compiler-backed tier — but only "when present", and nothing in ctx ever
made one. In practice the precise rung was unreachable and every non-Python
answer came from the ladder's floor: a word-boundary regex.

`evals/refs_precision.py` priced that floor against a real index on
``tokio-rs/bytes``: 3,637 reported sites where the compiler front end says 806.
For short, common names it is worse than it sounds — ``buf`` reported 754 sites
of which 55 were real. A regex cannot separate a reference from the same
letters in a comment, a doc example or a string literal, because that
information is not in the text. No amount of tuning reaches it.

So this module does not hand-roll a better approximation. It shells out to the
language's own tooling — the indexers the SCIP ecosystem already ships, each a
compiler front end or a language server in batch mode:

    rust        rust-analyzer scip .        (the language server itself)
    go          scip-go
    typescript  scip-typescript index       (also javascript)
    python      scip-python index
    java        scip-java

Design rules, all inherited from the surrounding code:

* **Absence degrades, never errors.** No indexer for a language is a reported
  fact, not an exception; the ladder keeps its existing rungs underneath.
* **Nothing is written into the repository.** The index lands in the store's
  audit area beside the job spools, for the reason stated there — output that
  is not source does not belong in the worktree.
* **Indexing is explicit.** It costs seconds to minutes and needs the project's
  toolchain, so it happens when someone runs ``ctx index``, never implicitly
  inside a retrieval verb on a hook's latency budget.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ctx.store import Store
from ctx.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Indexer:
    """One language's batch indexer. ``argv`` is completed with the output
    path; ``emits_to_cwd`` marks the tools that insist on writing
    ``index.scip`` beside the project instead of taking a path."""

    language: str
    binary: str
    argv: tuple[str, ...]
    emits_to_cwd: bool = False
    note: str = ""


#: Ordered per language. The first whose binary is on PATH wins.
INDEXERS: tuple[Indexer, ...] = (
    Indexer(
        "rust", "rust-analyzer", ("scip", "."), emits_to_cwd=True,
        note="rustup component add rust-analyzer",
    ),
    Indexer("go", "scip-go", ("--output",), note="go install github.com/sourcegraph/scip-go/cmd/scip-go@latest"),
    Indexer("typescript", "scip-typescript", ("index", "--output"), note="npm i -g @sourcegraph/scip-typescript"),
    Indexer("javascript", "scip-typescript", ("index", "--output"), note="npm i -g @sourcegraph/scip-typescript"),
    Indexer("python", "scip-python", ("index", "--output"), note="npm i -g @sourcegraph/scip-python"),
    Indexer("java", "scip-java", ("index", "--output"), note="see sourcegraph/scip-java"),
)

_BY_LANGUAGE: dict[str, tuple[Indexer, ...]] = {}
for _ix in INDEXERS:
    _BY_LANGUAGE.setdefault(_ix.language, ())
    _BY_LANGUAGE[_ix.language] += (_ix,)


class IndexError_(Exception):
    """Indexing could not run or did not produce an index."""


def index_path(store: Store) -> Path:
    """Where ctx keeps a generated index for this workspace — the store's
    audit area, never the worktree."""
    return Path(store.audit_dir) / "scip" / "index.scip"


def available(language: str) -> Indexer | None:
    """The indexer ctx would use for ``language`` here, or None."""
    for ix in _BY_LANGUAGE.get(language, ()):
        if shutil.which(ix.binary):
            return ix
    return None


def roster() -> dict[str, str | None]:
    """language → binary ctx would run, or None. For `ctx doctor`."""
    out: dict[str, str | None] = {}
    for lang in dict.fromkeys(ix.language for ix in INDEXERS):
        ix = available(lang)
        out[lang] = ix.binary if ix else None
    return out


def dominant_languages(ws: Workspace) -> list[str]:
    """Indexable languages present in the workspace, most files first.

    Uses the skeleton's own extension table so `ctx index` and `ctx map`
    cannot disagree about what language a file is."""
    from ctx.skeleton import language_for

    counts: dict[str, int] = {}
    for rel in ws.list_files(None):
        lang = language_for(rel)
        if lang in _BY_LANGUAGE:
            counts[lang] = counts.get(lang, 0) + 1
    return [lang for lang, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def build(
    ws: Workspace, store: Store, *, language: str, timeout: float = 900.0
) -> tuple[Path, int]:
    """Run ``language``'s indexer over the workspace; return (path, bytes).

    Raises :class:`IndexError_` with an actionable message when no indexer is
    installed, the run fails, or it produces nothing.
    """
    ix = available(language)
    if ix is None:
        hint = ""
        for cand in _BY_LANGUAGE.get(language, ()):
            if cand.note:
                hint = f"; install with: {cand.note}"
                break
        raise IndexError_(f"no SCIP indexer for {language} on PATH{hint}")

    out = index_path(store)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    # `rust-analyzer scip .` writes ./index.scip and takes no output path, so
    # it runs in a scratch cwd is not an option — it must see the project. It
    # is run in the worktree and the result moved out immediately, so the
    # repository never keeps a build artifact.
    stray = ws.root / "index.scip"
    stray_existed = stray.exists()
    argv = [ix.binary, *ix.argv] if ix.emits_to_cwd else [ix.binary, *ix.argv, str(out)]
    try:
        proc = subprocess.run(
            argv, cwd=str(ws.root), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "SCIP_NO_PROGRESS": "1"},
        )
    except subprocess.TimeoutExpired as e:
        raise IndexError_(f"{ix.binary} exceeded {timeout:.0f}s") from e
    except OSError as e:
        raise IndexError_(f"{ix.binary} could not run: {e}") from e

    if ix.emits_to_cwd and stray.is_file() and not stray_existed:
        stray.replace(out)

    # A nonzero exit is a failed index even when a file was left behind:
    # indexers routinely emit partial output before giving up on a
    # compilation or dependency error, and publishing that would install a
    # silently incomplete exact tier — answers missing whole files, in the
    # voice that says it is exact. Verified against the indexer this ships
    # against: `rust-analyzer scip` exits 0 on a successful run that emits
    # duplicate-symbol warnings, so this rejects failures, not noise.
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        if out.is_file():
            out.unlink()
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        detail = " · ".join(t.strip() for t in tail) if tail else "no output"
        raise IndexError_(f"{ix.binary} failed (exit {proc.returncode}): {detail}")

    # Record the tree this index describes. Without it the only currency
    # signal is the index file's own mtime, which cannot see a deletion that
    # touched no surviving file — see `scip_ingest.index_is_current`.
    from ctx.scip_ingest import _SIDECAR_NAME, _source_state

    count, newest = _source_state(ws)
    out.with_name(_SIDECAR_NAME).write_text(
        json.dumps({"language": language, "files": count, "max_mtime_ns": newest}),
        encoding="utf-8",
    )
    return out, out.stat().st_size
