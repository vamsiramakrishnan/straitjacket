"""Expand a verified example only when an explicit structural rule reproduces it."""
from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
import tempfile

from ctx import anchors, astgrep
from ctx.edit_transactions import REQUEST_SCHEMA, create_edit_plan, preview_edit_plan
from ctx.edit_verification import VerificationError, file_digests, read_evidence, validate_verification
from ctx.store import canonical_json

MAX_FILES = 64
MAX_BYTES = 8 * 1024 * 1024


def _transform(ws, sources, pattern, replacement, language):
    # The engine reads frozen copies. Offsets must never be interpreted
    # against files an editor changed after ast-grep inspected them.
    with tempfile.TemporaryDirectory(prefix="ctx-expand-") as temp:
        root = Path(temp)
        for rel, data in sources.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        isolated = replace(ws, root=root, git=None)
        raw = astgrep._run_astgrep(isolated, ["run", "--pattern", pattern, "--rewrite", replacement,
                                             "--lang", language, "--json=stream", "."])
    by_file = {}
    for row in astgrep._parse_stream(raw):
        rel = str(row.get("file", "")).removeprefix("./")
        if rel not in sources:
            raise VerificationError("structural engine returned an out-of-scope file")
        offsets = row.get("range", {}).get("byteOffset", {})
        start, end = offsets.get("start"), offsets.get("end")
        repl = row.get("replacement")
        if (type(start) is not int or type(end) is not int or not isinstance(repl, str)
                or not 0 <= start <= end <= len(sources[rel])):
            raise VerificationError("structural engine returned invalid byte offsets")
        by_file.setdefault(rel, []).append((start, end, repl.encode("utf-8")))
    result = dict(sources)
    for rel, edits in by_file.items():
        edits.sort()
        if any(a[1] > b[0] or a[0] == b[0] for a, b in zip(edits, edits[1:])):
            raise VerificationError("structural matches overlap")
        data = sources[rel]
        for start, end, repl in reversed(edits):
            data = data[:start] + repl + data[end:]
            if len(data) > MAX_BYTES:
                raise VerificationError("expanded file exceeds byte cap")
        result[rel] = data
    return result


def plan_expansion(ws, store, verification_ref, *, pattern, replacement, language, glob):
    """Return a previewed anchored plan; never modify the source workspace."""
    if not glob or glob in {"*", "**", "**/*"} or ".." in Path(glob).parts or Path(glob).is_absolute():
        raise VerificationError("expansion requires a narrower workspace-relative glob")
    if not pattern or not language or max(len(pattern), len(replacement)) > 64000:
        raise VerificationError("expansion needs a bounded structural rule and language")
    proof = validate_verification(ws, store, verification_ref)
    receipt = read_evidence(store, proof["editReceipt"], "ctx.edit-receipt/v1")
    if len(receipt["files"]) != 1:
        raise VerificationError("expansion requires one demonstrated file")
    example = receipt["files"][0]
    plan = read_evidence(store, receipt.get("planRef", ""), "ctx.edit-plan/v1")
    sample_path = example["path"]
    source = plan["edits"][0]
    if source["sourceFileSha256"] != example["beforeSha256"]:
        raise VerificationError("example relocated since planning; demonstrate on a fresh snapshot")
    source_id = source["sourceBlob"].removeprefix("sha256:")
    if store.blob_path(source_id).stat().st_size > MAX_BYTES:
        raise VerificationError("example exceeds byte cap")
    before = store.get_blob(source_id)
    after = ws.confine(sample_path, must_exist=True).read_bytes()
    if _transform(ws, {sample_path: before}, pattern, replacement, language)[sample_path] != after:
        raise VerificationError("structural rule does not reproduce the verified example")
    paths = [p for p in ws.list_files() if fnmatchcase(p, glob) and not ws.is_ignored(p)]
    if not paths or len(paths) > MAX_FILES:
        raise VerificationError("expansion scope must contain 1..64 files")
    sources = {}
    total = 0
    for p in paths:
        target = ws.confine(p, must_exist=True)
        if target.stat().st_size + total > MAX_BYTES:
            raise VerificationError("expansion input exceeds byte cap")
        data = target.read_bytes()
        total += len(data)
        if total > MAX_BYTES:
            raise VerificationError("expansion input exceeds byte cap")
        sources[p] = data
    import hashlib
    expected = {p: "sha256:" + hashlib.sha256(data).hexdigest() for p, data in sources.items()}
    transformed = _transform(ws, sources, pattern, replacement, language)
    edits = []
    for p, data in sorted(sources.items()):
        if transformed[p] == data:
            continue
        lines = data.decode("utf-8").splitlines()
        if not lines:
            raise VerificationError("cannot expand an empty source")
        edits.append({"path": p, "span": f"1:{len(lines)}@" + anchors.anchor(lines),
                      "replacement": transformed[p].decode("utf-8")})
    if not edits:
        raise VerificationError("no remaining matches within scope")
    if file_digests(ws, paths) != expected:
        raise VerificationError("source changed while computing expansion")
    planned = create_edit_plan(ws, store, {"schema": REQUEST_SCHEMA, "edits": edits})
    if any(e["sourceFileSha256"] != expected[e["path"]] for e in planned["edits"]):
        raise VerificationError("source changed while sealing expansion")
    # The ordinary apply transaction owns CAS, rollback, and diagnostics.
    preview = preview_edit_plan(ws, store, planned)
    return {**preview, "exampleVerification": verification_ref, "scope": glob,
            "engine": astgrep.engine_id(),
            "planRef": "blob:" + store.put_blob(canonical_json(planned))}
