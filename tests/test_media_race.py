"""Acceptance: a media target is read exactly once.

The target is opened a single time (no symlink followed at any component, no
blocking on a FIFO), validated from the open descriptor, copied into a private
0700 directory while being hashed, and everything else - sniff, the recorded
sha256, Pillow, ffprobe, ffmpeg - runs on that private copy. A file swapped at
the target path while `ctx view` runs therefore can neither change what is
decoded nor make the derivation record lie.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading

import pytest

from ctx import media
from ctx.cli import main

HAS_PILLOW = True
try:
    from PIL import Image
except Exception:
    HAS_PILLOW = False
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_both = pytest.mark.skipif(not (HAS_PILLOW and HAS_FFMPEG), reason="Pillow and ffmpeg required")


def _clip(path, source: str):
    cp = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source,
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], capture_output=True)
    if cp.returncode != 0:
        pytest.skip("ffmpeg could not build the clip")
    return path


@pytest.fixture()
def ws(state_home, workspace_dir):
    return workspace_dir


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# ------------------------------------------------------ the open itself
def test_symlink_components_are_never_followed(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.bin").write_bytes(b"secret")
    (ws / "real").mkdir()
    (ws / "real" / "f.bin").write_bytes(b"ok bytes")
    (ws / "linkdir").symlink_to(outside, target_is_directory=True)
    (ws / "final").symlink_to(outside / "f.bin")
    with media.snapshot_confined(ws, "real/f.bin") as snap:
        assert snap.read(100) == b"ok bytes" and snap.size == 8
        assert snap.sha256() == hashlib.sha256(b"ok bytes").hexdigest()
    for rel in ("linkdir/f.bin", "final"):
        with pytest.raises(media.MediaError, match="symbolic link"):
            media.snapshot_confined(ws, rel)
    for bad in ("../x", "real/../real/f.bin", "", "real//f.bin", "./real/f.bin"):
        with pytest.raises(media.MediaError):
            media.snapshot_confined(ws, bad)


def test_fifo_and_devices_are_refused_without_blocking(ws):
    os.mkfifo(ws / "pipe.mkv")
    done = []

    def attempt():
        try:
            media.snapshot_confined(ws, "pipe.mkv")
        except media.MediaError as exc:
            done.append(str(exc))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive() and done and "not a regular file" in done[0]
    with pytest.raises(media.MediaError, match="not a regular file"):
        media.snapshot_path("/dev/null")


def test_size_cap_is_enforced_from_the_descriptor(ws):
    (ws / "big.bin").write_bytes(b"x" * 100)
    with pytest.raises(media.MediaLimitError):
        media.snapshot_confined(ws, "big.bin", limit=50)


@pytest.mark.parametrize("fd_mode", [True, False])
def test_snapshot_is_private_and_later_changes_do_not_reach_it(ws, monkeypatch, tmp_path, fd_mode):
    monkeypatch.setattr(media, "_FD_MODE", fd_mode and media._FD_MODE)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(media.tempfile, "tempdir", str(scratch))
    (ws / "f.bin").write_bytes(b"first")
    snap = media.snapshot_confined(ws, "f.bin")
    (ws / "f.bin").write_bytes(b"SECOND-and-longer")
    assert snap.read(100) == b"first" and snap.sha256() == hashlib.sha256(b"first").hexdigest()
    if snap.fd is not None:                       # descriptor mode: nothing on disk at all
        assert snap.path is None and list(scratch.rglob("*")) == []
    else:                                         # named mode: a 0700 directory holding only `in`
        folder = snap.path.parent
        assert (os.stat(folder).st_mode & 0o777) == 0o700 and sorted(os.listdir(folder)) == sorted(["in", media._MARKER])
    snap.close()
    assert list(scratch.iterdir()) == []


# --------------------------------------- the record is the hash of what was decoded
@needs_both
def test_recorded_hash_and_pixels_come_from_the_same_single_read(ws, tmp_path):
    a = _clip(tmp_path / "a.mp4", "testsrc=duration=3:size=320x240:rate=10")
    b = _clip(tmp_path / "b.mp4", "color=c=green:duration=3:size=320x240:rate=10")
    target = ws / "t.mp4"
    shutil.copy(a, target)
    snap = media.snapshot_confined(ws, "t.mp4")
    shutil.copy(b, target)                       # swapped after the one read
    got = media.frame_at(snap, 1)
    want = media.frame_at(a, 1)
    assert got.data == want.data and got.record.source_sha256 == sha(a) != sha(b)
    assert media.probe(snap).sha256 == sha(a)
    snap.close()


@needs_both
def test_swap_loop_never_shows_outside_content_or_a_false_hash(ws, tmp_path, capsys):
    """The reviewer's race: the target path flips between an in-workspace file
    and a symlink to a file OUTSIDE the workspace while `ctx view` runs. Every
    produced frame must be the in-workspace file's, with its hash, and nothing
    of the outside file may ever appear."""
    inside = _clip(tmp_path / "inside.mp4", "testsrc=duration=3:size=320x240:rate=10")
    outside = _clip(tmp_path / "outside.mp4", "color=c=green:duration=3:size=320x240:rate=10")
    (ws / "race").mkdir()
    target = ws / "race" / "target.mp4"
    shutil.copy(inside, target)
    reference = media.frame_at(inside, 1).data
    outside_frame = media.frame_at(outside, 1).data
    assert reference != outside_frame
    stop = threading.Event()

    def swapper():
        n = 0
        while not stop.is_set():
            n += 1
            tmp = ws / "race" / ".swap"
            if n % 2:
                shutil.copy(inside, tmp)
            else:
                if tmp.exists() or tmp.is_symlink():
                    tmp.unlink()
                tmp.symlink_to(outside)
            os.replace(tmp, target)

    t = threading.Thread(target=swapper, daemon=True)
    t.start()
    ok = refused = 0
    try:
        for _ in range(30):
            rc = main(["--workspace", str(ws), "view", "race/target.mp4", "--at", "1", "--json"])
            out = capsys.readouterr().out
            doc = json.loads(out)
            if rc == 0:
                ok += 1
                der = doc["derived"]
                assert der["source_sha256"] == sha(inside) == doc["source"]["sha256"]
                frame = (ws / der["path"]).read_bytes()
                assert frame == reference and frame != outside_frame
            else:
                refused += 1
                assert doc["status"] == "error"
    finally:
        stop.set()
        t.join(10)
    assert ok >= 1, "the swap loop never let a read through; the test proved nothing"
    assert ok + refused == 30


@needs_both
def test_a_fifo_swapped_in_does_not_hang_the_view(ws, tmp_path, capsys):
    os.mkfifo(ws / "t.mp4")
    result = []

    def attempt():
        result.append(main(["--workspace", str(ws), "view", "t.mp4"]))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(15)
    assert not t.is_alive() and result == [1]
    assert "not a regular file" in capsys.readouterr().err


@needs_both
def test_blob_and_run_targets_are_snapshotted_and_integrity_checked(ws, tmp_path, capsys):
    from conftest import make_store, make_ws

    clip = _clip(tmp_path / "c.mp4", "testsrc=duration=2:size=160x120:rate=10")
    store = make_store(make_ws(ws))
    blob = store.put_blob(clip.read_bytes())
    rc = main(["--workspace", str(ws), "view", f"blob:{blob[:12]}", "--at", "1", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["derived"]["source_sha256"] == blob
    # a damaged store file is refused, not decoded
    store.blob_path(blob).write_bytes(clip.read_bytes()[:-5] + b"XXXXX")
    rc = main(["--workspace", str(ws), "view", f"blob:{blob[:12]}", "--json"])
    err = json.loads(capsys.readouterr().out)
    assert rc == 1 and "do not match their id" in err["error"]
