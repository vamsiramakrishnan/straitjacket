"""Evidence capsules: a citation that still resolves somewhere else.

The cases here are the ones `evals/memvid_fidelity.py` found a third-party
single-file memory format failing: trailing whitespace, CRLF, payloads too
small to page, and an integrity check that passes while the content differs.
A capsule that fails any of them is not worth attaching to a pull request.
"""

from __future__ import annotations

import json
import tarfile

import pytest

from conftest import make_store, make_ws

from ctx import capsule


@pytest.fixture()
def repo(workspace_dir):
    (workspace_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return workspace_dir


def _elsewhere(tag: str = "elsewhere"):
    """A store that has never seen this session's evidence — the reviewer's
    machine, modelled by a workspace id the exporter never wrote under."""
    from ctx.store import Store

    return Store(f"ws_{tag}")


def _run(store, payload: bytes, *, stderr: bytes = b"") -> str:
    """A stored run whose stdout is exactly ``payload``."""
    out = store.put_blob(payload)
    err = store.put_blob(stderr)
    return store.put_manifest(
        {
            "schema": "ctx.invocation/v1",
            "argv": ["cat", "corpus.log"],
            "result": {"exitCode": 0},
            "streams": {
                "stdout": {"blob": f"sha256:{out}", "bytes": len(payload)},
                "stderr": {"blob": f"sha256:{err}", "bytes": len(stderr)},
            },
        },
        kind="run",
    )


# --------------------------------------------------------------- round trip
@pytest.mark.parametrize(
    "name,payload",
    [
        ("tiny", b"alpha bravo charlie delta echo"),
        ("empty", b""),
        ("one-trailing-newline", b"".join(b"line %d\n" % i for i in range(2000))),
        ("two-trailing-newlines", b"".join(b"line %d\n" % i for i in range(500)) + b"\n"),
        ("crlf", b"".join(b"line %d\r\n" % i for i in range(500))),
        ("trailing-spaces", b"".join(b"line %d   \n" % i for i in range(500))),
        ("blank-lines", b"a\n\n\nb\n" * 400),
        ("no-trailing-newline", b"x" * 5000),
        ("binary", bytes(range(256)) * 40),
        ("utf8", "héllo wörld ✅\n".encode("utf-8") * 300),
    ],
)
def test_the_bytes_that_come_back_are_the_bytes_that_went_in(state_home, repo, tmp_path, name, payload):
    """The whole mechanism is this assertion. Every case here is one the
    memvid probe (`evals/memvid_fidelity.py`) found altered in transit."""
    ws = make_ws(repo)
    store = make_store(ws)
    run_id = _run(store, payload)

    path = tmp_path / f"{name}.ctxcap"
    report = capsule.export(store, [f"run:{run_id[:12]}#stdout"], path)
    # Two streams, content-addressed: distinct bytes give two blobs, and an
    # empty stdout beside an empty stderr is legitimately one.
    assert report.manifests == 1 and not report.unresolved
    assert report.blobs == (1 if payload == b"" else 2)

    # A different machine: a store that has never seen this evidence.
    other = _elsewhere()
    with pytest.raises(Exception):
        other.get_manifest(run_id)

    capsule.import_capsule(other, path)
    back = other.get_manifest(run_id)
    blob = back["streams"]["stdout"]["blob"].removeprefix("sha256:")
    assert other.get_blob(blob) == payload


def test_a_capsule_is_byte_stable(state_home, repo, tmp_path):
    """Same evidence, same bytes: two capsules of one task compare by hash,
    so a reviewer can tell a re-export from a different claim."""
    ws = make_ws(repo)
    store = make_store(ws)
    run_id = _run(store, b"line one\nline two\n")
    handles = [f"run:{run_id[:12]}#stdout"]

    a, b = tmp_path / "a.ctxcap", tmp_path / "b.ctxcap"
    capsule.export(store, handles, a)
    capsule.export(store, handles, b)
    # The files themselves, not their parsed indexes: a wall-clock field in
    # the index would pass a parsed comparison and still break the promise.
    assert a.read_bytes() == b.read_bytes()
    index = json.loads(tarfile.open(a).extractfile("capsule.json").read())
    assert "created_at" not in index


# ---------------------------------------------------------------- integrity
def _rewrite(path, replacements: dict[str, bytes]):
    """Rebuild a capsule with some members replaced — a tampered file."""
    members = []
    with tarfile.open(path, "r") as tar:
        for info in tar.getmembers():
            data = tar.extractfile(info).read()
            members.append((info.name, replacements.get(info.name, data)))
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, __import__("io").BytesIO(data))


def test_altered_bytes_are_caught_even_when_the_index_agrees(state_home, repo, tmp_path):
    """memvid's deep verify passed while the content differed, because it
    checked its index. A capsule checks the bytes against two independent
    records: the index, and the member's own content address."""
    ws = make_ws(repo)
    store = make_store(ws)
    payload = b"".join(b"line %d\n" % i for i in range(500))
    run_id = _run(store, payload)
    path = tmp_path / "c.ctxcap"
    capsule.export(store, [f"run:{run_id[:12]}#stdout"], path)

    index, members, problems = capsule.verify(path)
    assert problems == []

    blob_name = next(n for n in members if n.startswith("blobs/") and members[n] == payload)
    tampered = payload.replace(b"line 250\n", b"line 250 \n")  # one trailing space
    assert tampered != payload and len(tampered) == len(payload) + 1

    # The index rewritten to match, the way a lossy store's own checks would.
    new_index = json.loads(tarfile.open(path).extractfile("capsule.json").read())
    for m in new_index["members"]:
        if m["name"] == blob_name:
            m["sha256"] = __import__("hashlib").sha256(tampered).hexdigest()
            m["bytes"] = len(tampered)
    _rewrite(path, {
        blob_name: tampered,
        "capsule.json": (json.dumps(new_index, indent=1, sort_keys=True) + "\n").encode(),
    })

    _, _, problems = capsule.verify(path)
    assert problems and "address says" in " ".join(problems)

    other = _elsewhere("tamper")
    with pytest.raises(capsule.CapsuleError, match="nothing imported"):
        capsule.import_capsule(other, path)
    # and it really imported nothing
    with pytest.raises(Exception):
        other.get_manifest(run_id)


def test_a_truncated_or_foreign_file_is_refused(state_home, tmp_path):
    (tmp_path / "junk.ctxcap").write_bytes(b"not a tar at all")
    with pytest.raises(capsule.CapsuleError, match="not a readable capsule"):
        capsule.verify(tmp_path / "junk.ctxcap")


def test_a_capsule_member_cannot_escape_the_archive(state_home, tmp_path):
    path = tmp_path / "evil.ctxcap"
    import io

    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        body = b"{}"
        for name in ("capsule.json", "../../etc/passwd"):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    with pytest.raises(capsule.CapsuleError, match="escapes the archive"):
        capsule.verify(path)


# ------------------------------------------------------------------ handles
def test_an_unresolvable_handle_is_reported_not_dropped(state_home, repo, tmp_path):
    ws = make_ws(repo)
    store = make_store(ws)
    run_id = _run(store, b"real evidence\n")
    path = tmp_path / "d.ctxcap"
    report = capsule.export(
        store, [f"run:{run_id[:12]}#stdout", "run:ffffffffffff#stdout", "repo:calc.py"], path
    )
    assert report.manifests == 1
    assert [u["handle"] for u in report.unresolved] == ["run:ffffffffffff#stdout"]
    index, _members, problems = capsule.verify(path)
    assert problems == [] and index["unresolved"]
    assert "repo:calc.py" in index["handles"]  # cited, travels by git


def test_exporting_nothing_is_an_error_not_an_empty_file(state_home, repo, tmp_path):
    store = make_store(make_ws(repo))
    with pytest.raises(capsule.CapsuleError, match="nothing to export"):
        capsule.export(store, ["run:ffffffffffff#stdout"], tmp_path / "e.ctxcap")


def test_handles_come_off_the_ledger_in_citation_order(state_home, repo):
    from ctx import taskledger

    task_id = taskledger.new_task_id()
    taskledger.append(repo, taskledger.task_row(
        task_id, goal_ref="repo:calc.py", nodes=[], budget_usd=1.0,
        task_kind="fix", source="test"))
    taskledger.append(
        repo,
        taskledger.verdict_row(task_id, "n1", passed=True, evidence_kind="run", ref="run:abc123def456#stdout"),
    )
    taskledger.append(
        repo,
        taskledger.verdict_row(task_id, "n2", passed=False, evidence_kind="run", ref="run:0123456789ab#stderr"),
    )
    # The goal reference is a citation too, and it comes first.
    assert capsule.handles_from_ledger(repo, task_id) == [
        "repo:calc.py",
        "run:abc123def456#stdout",
        "run:0123456789ab#stderr",
    ]


# ------------------------------------------------- completeness, not just integrity
def test_a_capsule_missing_a_blob_is_caught_even_if_the_index_was_repacked(state_home, repo, tmp_path):
    """Every member can hash correctly while the capsule cannot answer the
    handle it advertises: drop a blob *and* its index row and the name-level
    checks agree with each other. The recorded closure is what notices."""
    store = make_store(make_ws(repo))
    run_id = _run(store, b"the evidence the argument rests on\n")
    path = tmp_path / "f.ctxcap"
    capsule.export(store, [f"run:{run_id[:12]}#stdout"], path)

    index, members, problems = capsule.verify(path)
    assert problems == []

    blob_name = next(n for n in members if n.startswith("blobs/") and b"argument" in members[n])
    kept = {n: d for n, d in members.items() if n != blob_name}
    new_index = json.loads(tarfile.open(path).extractfile("capsule.json").read())
    new_index["members"] = [m for m in new_index["members"] if m["name"] != blob_name]
    kept.pop("capsule.json", None)
    kept["capsule.json"] = (json.dumps(new_index, indent=1, sort_keys=True) + "\n").encode()

    import io as _io
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(kept):
            info = tarfile.TarInfo(name)
            info.size = len(kept[name])
            tar.addfile(info, _io.BytesIO(kept[name]))

    _, _, problems = capsule.verify(path)
    assert any("in the closure, missing from the capsule" in p for p in problems)
    with pytest.raises(capsule.CapsuleError, match="nothing imported"):
        capsule.import_capsule(_elsewhere("hole"), path)


def test_a_capsule_with_no_closure_is_not_trusted(state_home, repo, tmp_path):
    store = make_store(make_ws(repo))
    run_id = _run(store, b"evidence\n")
    path = tmp_path / "g.ctxcap"
    capsule.export(store, [f"run:{run_id[:12]}#stdout"], path)
    index = json.loads(tarfile.open(path).extractfile("capsule.json").read())
    del index["closure"]
    _rewrite(path, {"capsule.json": (json.dumps(index, indent=1, sort_keys=True) + "\n").encode()})
    _, _, problems = capsule.verify(path)
    assert any("records no closure" in p for p in problems)


# ------------------------------------------------------- checkpoints and task text
def test_a_checkpoint_brings_its_evidence_but_not_its_prose(state_home, repo, tmp_path):
    """A checkpoint stores evidence refs abbreviated to 12 characters, and
    its own body is the goal and reasoning in the operator's words. Export
    has to follow the short refs, and must not ship the prose."""
    from ctx.checkpoint import create_checkpoint

    ws = make_ws(repo)
    store = make_store(ws)
    run_id = _run(store, b"stack trace the checkpoint cites\n")
    cp_id, _doc = create_checkpoint(
        store, ws,
        goal="SECRET GOAL: rewrite the billing path before the audit",
        state="halfway",
        decisions=["CONFIDENTIAL decision about a customer"],
        evidence=[f"run:{run_id[:12]}#stdout the failing case"],
    )

    path = tmp_path / "cp.ctxcap"
    report = capsule.export(store, [f"checkpoint:{cp_id[:12]}"], path)

    # The evidence travelled...
    assert report.manifests == 1 and report.blobs == 2
    assert report.excluded and report.excluded[0]["schema"] == "ctx.checkpoint/v1"
    blob = capsule.verify(path)[1]
    body = b"".join(blob.values())
    assert b"stack trace the checkpoint cites" in body
    # ...and the prose did not.
    assert b"SECRET GOAL" not in body and b"CONFIDENTIAL" not in body

    other = _elsewhere("cp")
    capsule.import_capsule(other, path)
    assert other.get_manifest(run_id)["streams"]["stdout"]


def test_include_task_text_is_available_when_asked_for(state_home, repo, tmp_path):
    from ctx.checkpoint import create_checkpoint

    ws = make_ws(repo)
    store = make_store(ws)
    run_id = _run(store, b"evidence\n")
    cp_id, _ = create_checkpoint(store, ws, goal="THE GOAL", evidence=[f"run:{run_id[:12]}#stdout"])
    path = tmp_path / "cp2.ctxcap"
    report = capsule.export(store, [f"checkpoint:{cp_id[:12]}"], path, include_task_text=True)
    assert report.manifests == 2 and not report.excluded
    body = b"".join(capsule.verify(path)[1].values())
    assert b"THE GOAL" in body
