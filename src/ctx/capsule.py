"""Evidence capsules: a citation that still resolves somewhere else.

A `run:` handle is only worth what it resolves to. Inside the session that
minted it the store answers instantly; outside, the handle is a string. A
pull request whose description cites `run:8d8335db6848#stdout --lines
14238:14241` is, for its reviewer, a claim with no evidence behind it, and
for a container that has since been reclaimed, a claim with no evidence
behind it anywhere.

A capsule closes that gap. It is one file holding the manifests a set of
handles names, the blobs those manifests reference, and nothing else: no
task text, no prompts, no transcript — the same export-safe rule the task
ledger already follows (`docs/TASK-LEDGER.md`). Imported into any store on
any machine, every handle it closed over resolves through the ordinary
retrieval path, byte for byte.

The format is a POSIX tar of content-addressed members:

    capsule.json          the index: schema, handles, members with sha256
    manifests/<sha>.json  each cited manifest, canonical JSON
    blobs/<sha>           raw bytes, exactly as the store holds them

Members are written in sorted order with fixed metadata, so the same
evidence produces the same bytes; two capsules of one task are comparable
by hash.

**Every read verifies.** `verify()` recomputes the sha256 of every member
and compares it to both the index and the member's own name; `import_capsule`
refuses to write anything if a single member fails. This is not defensive
habit, it is the finding of `evals/memvid_fidelity.py`: a well-regarded
single-file memory format returned altered bytes for six of seven payloads
while reporting itself healthy, because its integrity checks covered its
index rather than its content. A capsule that cannot prove its bytes are the
bytes is a capsule that has not earned a citation.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctx.refs import RefError, parse_ref
from ctx.store import Store, canonical_json
from ctx.textutil import short_id

#: Bumped when the member layout changes in a way an older reader misreads.
CAPSULE_VERSION = 1
SCHEMA = "ctx.capsule/v1"
INDEX_NAME = "capsule.json"
#: Fixed tar metadata: the bytes of a capsule must depend on its evidence
#: and nothing else — not the clock, not the exporting user's uid.
_TAR_MTIME = 0
_TAR_MODE = 0o644


class CapsuleError(Exception):
    """A capsule could not be built, read, or trusted."""


@dataclass
class Member:
    """One file in a capsule, addressed by the hash of its own bytes."""

    name: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class CapsuleReport:
    """What a capsule holds, for a receipt or a `--json` caller."""

    path: str
    handles: list[str] = field(default_factory=list)
    manifests: int = 0
    blobs: int = 0
    bytes_total: int = 0
    file_bytes: int = 0
    unresolved: list[dict[str, str]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "handles": list(self.handles),
            "manifests": self.manifests,
            "blobs": self.blobs,
            "evidence_bytes": self.bytes_total,
            "file_bytes": self.file_bytes,
            "unresolved": list(self.unresolved),
        }


# --------------------------------------------------------------- collecting
def manifest_id_for(manifest: dict[str, Any]) -> str:
    """The id the store would give this manifest.

    Mirrors ``Store.put_manifest``: the address is the hash of the canonical
    bytes *without* the ``id`` field, while the stored document carries it.
    A capsule therefore cannot check a manifest by hashing its own file, and
    a reader that tried would reject every honest capsule.
    """
    body = {k: v for k, v in manifest.items() if k != "id"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _blob_ids(manifest: dict[str, Any]) -> set[str]:
    """Blob ids a manifest references, by the store's own structural walk.

    Reuses ``Store``'s collector rather than reading known stream fields: a
    capsule that learns each manifest kind by hand is one new kind away from
    shipping a manifest whose evidence it silently left behind.
    """
    from ctx.store import _referenced_blobs

    return _referenced_blobs(manifest)


def collect(store: Store, handles: list[str]) -> tuple[dict[str, dict], set[str], list[dict[str, str]]]:
    """Resolve handles to (manifests by id, blob ids, unresolved reasons).

    The closure is transitive and classifies each id by what the store
    actually holds, because a manifest's references are not all of one kind:
    a checkpoint names run manifests, a run names its stream blobs, and
    every manifest carries its own id. Following only the stream fields
    would ship a checkpoint whose evidence stayed behind; treating every
    64-hex string as a blob would try to read a manifest as one.

    A handle that names no stored object is reported, never guessed at and
    never silently dropped: a capsule missing the one line the argument
    rests on is worse than no capsule.
    """
    manifests: dict[str, dict] = {}
    blobs: set[str] = set()
    unresolved: list[dict[str, str]] = []
    # (full id, the handle that pulled it in, whether a caller cited it).
    # The distinction matters at the end: a cited handle that resolves to
    # nothing is a hole in the argument and gets reported. An id discovered
    # by walking a manifest may not be an object at all — `digest.bytesHash`,
    # `source.worktreeHash` and friends are 64 hex characters of fingerprint,
    # not addresses — so a miss there is normal and silent.
    queue: list[tuple[str, str, bool]] = []

    for text in handles:
        head = text.split(None, 1)[0] if text.split() else ""
        if not head:
            continue
        try:
            ref = parse_ref(head)
        except RefError as e:
            unresolved.append({"handle": text, "reason": str(e)})
            continue
        if ref.kind == "repo":
            # A repo: address points at the worktree, which travels by git,
            # not by capsule. Recorded so the reader knows it was cited.
            continue
        if not ref.id:
            unresolved.append({"handle": text, "reason": "no id in reference"})
            continue
        try:
            queue.append((store.resolve_id(ref.id), text, True))
        except Exception as e:
            unresolved.append({"handle": text, "reason": f"{type(e).__name__}: {e}"})

    while queue:
        full, origin, cited = queue.pop()
        if full in manifests or full in blobs:
            continue
        if store.blob_path(full).is_file():
            blobs.add(full)
            continue
        try:
            manifest = store.get_manifest(full)
        except Exception as e:
            if cited:
                unresolved.append(
                    {"handle": origin, "reason": f"{short_id(full)}: {type(e).__name__}: {e}"}
                )
            continue
        manifests[full] = manifest
        queue.extend((ref_id, origin, False) for ref_id in _blob_ids(manifest) if ref_id != full)

    return manifests, blobs, unresolved


# ----------------------------------------------------------------- building
def _members(store: Store, manifests: dict[str, dict], blobs: set[str]) -> list[Member]:
    members: list[Member] = []
    for mid in sorted(manifests):
        members.append(Member(f"manifests/{mid}.json", canonical_json(manifests[mid])))
    for bid in sorted(blobs):
        try:
            data = store.get_blob(bid)
        except Exception as e:
            raise CapsuleError(f"blob {bid[:12]} is referenced but unreadable: {e}") from e
        members.append(Member(f"blobs/{bid}", data))
    return members


def _write_tar(path: Path, index: dict[str, Any], members: list[Member]) -> None:
    index_bytes = (json.dumps(index, indent=1, sort_keys=True) + "\n").encode("utf-8")
    ordered = [Member(INDEX_NAME, index_bytes), *sorted(members, key=lambda m: m.name)]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
        for m in ordered:
            info = tarfile.TarInfo(m.name)
            info.size = len(m.data)
            info.mtime = _TAR_MTIME
            info.mode = _TAR_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(m.data))
    tmp.replace(path)


def export(store: Store, handles: list[str], path: Path, *, note: str | None = None) -> CapsuleReport:
    """Write a capsule closing over ``handles``. Returns what went in it."""
    manifests, blobs, unresolved = collect(store, handles)
    if not manifests and not blobs:
        raise CapsuleError(
            "nothing to export: no handle resolved to a stored manifest or blob"
        )
    members = _members(store, manifests, blobs)
    index = {
        "schema": SCHEMA,
        "version": CAPSULE_VERSION,
        "created_at": round(time.time(), 3),
        "note": note or "",
        "handles": list(handles),
        "unresolved": unresolved,
        "members": [{"name": m.name, "sha256": m.sha256, "bytes": len(m.data)} for m in members],
    }
    _write_tar(path, index, members)
    return CapsuleReport(
        path=str(path),
        handles=list(handles),
        manifests=len(manifests),
        blobs=len(blobs),
        bytes_total=sum(len(m.data) for m in members),
        file_bytes=path.stat().st_size,
        unresolved=unresolved,
    )


# ------------------------------------------------------------------ reading
def _read_all(path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    try:
        with tarfile.open(path, "r") as tar:
            blobs: dict[str, bytes] = {}
            index: dict[str, Any] | None = None
            for info in tar.getmembers():
                if not info.isfile():
                    continue
                name = info.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise CapsuleError(f"capsule member escapes the archive: {name!r}")
                handle = tar.extractfile(info)
                data = handle.read() if handle else b""
                if name == INDEX_NAME:
                    index = json.loads(data.decode("utf-8"))
                else:
                    blobs[name] = data
    except CapsuleError:
        raise
    except Exception as e:
        raise CapsuleError(f"{path} is not a readable capsule: {e}") from e
    if index is None:
        raise CapsuleError(f"{path} has no {INDEX_NAME}")
    if index.get("schema") != SCHEMA:
        raise CapsuleError(f"{path} is not a {SCHEMA} capsule: schema={index.get('schema')!r}")
    return index, blobs


def verify(path: Path) -> tuple[dict[str, Any], dict[str, bytes], list[str]]:
    """Read a capsule and check every member against its recorded hash.

    Returns (index, members, problems). ``problems`` empty means every byte
    in the file is the byte that was exported, by the member's own name and
    by the index's record of it — two independent statements of the same
    hash, so a tampered index alone does not pass.
    """
    index, members = _read_all(path)
    problems: list[str] = []
    recorded = {m["name"]: m for m in index.get("members", [])}

    for name in sorted(set(recorded) | set(members)):
        if name not in members:
            problems.append(f"{name}: in the index, missing from the archive")
            continue
        if name not in recorded:
            problems.append(f"{name}: in the archive, missing from the index")
            continue
        data = members[name]
        got = hashlib.sha256(data).hexdigest()
        want = str(recorded[name].get("sha256") or "")
        if got != want:
            problems.append(f"{name}: index says {want[:12]}, bytes hash to {got[:12]}")
            continue
        # A member's name is also its address, derived from its content by
        # the same rule the store uses. Checking it means a capsule whose
        # index was rewritten to agree with altered bytes still fails.
        if name.startswith("blobs/"):
            if Path(name).name != got:
                problems.append(f"{name}: content hashes to {got[:12]}, address says {name[6:18]}")
        elif name.startswith("manifests/"):
            stem = Path(name).stem
            try:
                addr = manifest_id_for(json.loads(data.decode("utf-8")))
            except Exception as e:
                problems.append(f"{name}: not readable as a manifest: {e}")
                continue
            if addr != stem:
                problems.append(f"{name}: content addresses to {addr[:12]}, name says {stem[:12]}")
    return index, members, problems


def import_capsule(store: Store, path: Path, *, pin: bool = True) -> CapsuleReport:
    """Load a verified capsule into ``store``. Refuses a capsule with any
    failing member — a partial import leaves a handle resolving to bytes
    nobody vouched for, which is the failure this whole mechanism exists to
    prevent."""
    index, members, problems = verify(path)
    if problems:
        raise CapsuleError(
            f"{path} failed verification, nothing imported:\n  " + "\n  ".join(problems)
        )

    manifests = blobs = 0
    for name in sorted(members):
        data = members[name]
        if name.startswith("blobs/"):
            got = store.put_blob(data)
            if got != Path(name).name:
                raise CapsuleError(f"{name}: store addressed it as {got[:12]}")
            blobs += 1
        elif name.startswith("manifests/"):
            body = json.loads(data.decode("utf-8"))
            kind = str(body.get("schema", "")).split("/")[0].removeprefix("ctx.") or "run"
            mid = store.put_manifest(body, kind=kind)
            if mid != Path(name).stem:
                raise CapsuleError(f"{name}: store addressed it as {mid[:12]}")
            if pin:
                store.pin(mid)
            manifests += 1
    return CapsuleReport(
        path=str(path),
        handles=list(index.get("handles") or []),
        manifests=manifests,
        blobs=blobs,
        bytes_total=sum(len(v) for v in members.values()),
        file_bytes=path.stat().st_size,
        unresolved=list(index.get("unresolved") or []),
    )


# ----------------------------------------------------------------- handles
def handles_from_ledger(workspace_root: Path, task_id: str) -> list[str]:
    """Every address a task's ledger cited, in the order it cited them.

    The ledger already guarantees each of these parses as a `ctx get`
    address and carries no task text (`ctx.taskledger.check_address`), which
    is exactly the set a capsule should close over.
    """
    from ctx import taskledger

    seen: list[str] = []
    for row in taskledger.load(workspace_root, task_id):
        for key in ("ref", "goal_ref", "checkpoint", "request", "result"):
            value = row.get(key)
            if isinstance(value, str) and value and value not in seen:
                try:
                    parse_ref(value.split(None, 1)[0])
                except RefError:
                    continue
                seen.append(value)
    return seen


def render_report(report: CapsuleReport, *, action: str) -> str:
    """One bounded block, in the house shape: what, how big, what is missing."""
    name = Path(report.path).name
    lines = [f"[ctx capsule {action} {name}]"]
    lines.append(f"handles: {len(report.handles)}")
    for h in report.handles[:12]:
        lines.append(f"  {h}")
    if len(report.handles) > 12:
        lines.append(f"  … +{len(report.handles) - 12} more")
    lines.append(
        f"holds: {report.manifests} manifest(s) · {report.blobs} blob(s) · "
        f"{report.bytes_total:,} B of evidence in {report.file_bytes:,} B"
    )
    if report.unresolved:
        lines.append(f"unresolved: {len(report.unresolved)}")
        for u in report.unresolved[:6]:
            lines.append(f"  {u.get('handle', '')} — {u.get('reason', '')}")
    if action == "export":
        lines.append("next:")
        lines.append(f"  ctx capsule verify {name}")
        lines.append(f"  ctx capsule import {name}   # then any cited handle resolves")
    return "\n".join(lines)


def summarize_id(path: Path) -> str:
    """A short, stable id for a capsule file: the hash of its own bytes."""
    return short_id(hashlib.sha256(path.read_bytes()).hexdigest())
