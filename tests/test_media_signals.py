"""Acceptance: nothing of a viewed file, and no ffmpeg, outlives `ctx view`.

Real signals are sent to a real `ctx view` process that is busy inside a
(fake, hanging) ffprobe, and the filesystem and process table are inspected
afterwards. Needs neither Pillow nor ffmpeg.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import media_headers as H
import pytest

from ctx import media

pytestmark = pytest.mark.skipif(os.name != "posix" or not os.path.isdir("/proc"),
                                reason="needs POSIX signals and /proc")


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"      # a zombie is dead
    except (FileNotFoundError, ProcessLookupError):
        return False


def wait_for(path, seconds=30.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def walk_files(root):
    return [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]


@pytest.fixture()
def harness(tmp_path, state_home, workspace_dir):
    """A workspace holding a 3 MB 'video', a fake ffprobe that hangs (and
    spawns a child of its own), and a runner script for the real CLI."""
    (workspace_dir / "v.mp4").write_bytes(H.mp4_header() + b"\x00" * (3 << 20))
    fake = tmp_path / "fakebin"
    fake.mkdir()
    marks = tmp_path / "marks"
    marks.mkdir()
    sleep = shutil.which("sleep", path="/bin:/usr/bin") or "sleep"   # PATH may be stripped
    (fake / "ffprobe").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        {sleep} 300 &
        echo $! > {marks}/grandchild
        echo $$ > {marks}/ffprobe
        wait
        """))
    (fake / "ffprobe").chmod(0o755)
    (fake / "ffmpeg").write_text("#!/bin/sh\nexit 1\n")
    (fake / "ffmpeg").chmod(0o755)
    runner = tmp_path / "run_view.py"
    runner.write_text(textwrap.dedent("""\
        import sys
        from ctx import media
        media._FD_MODE = sys.argv[3] == "fd"
        from ctx.cli import main
        sys.exit(main(["--workspace", sys.argv[1], "view", sys.argv[2]]))
        """))
    scratch = tmp_path / "tmp"
    scratch.mkdir()

    def start(mode: str) -> subprocess.Popen:
        env = {**os.environ, "TMPDIR": str(scratch), "CTX_STATE_HOME": str(state_home),
               "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.Popen([sys.executable, str(runner), str(workspace_dir), "v.mp4", mode],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return start, scratch, marks


@pytest.mark.parametrize("mode", ["fd", "named"])
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_termination_signals_remove_the_copy_and_kill_ffmpeg(harness, mode, sig):
    start, scratch, marks = harness
    proc = start(mode)
    try:
        ffprobe_pid = int(wait_for(marks / "ffprobe"))
        grandchild = int(wait_for(marks / "grandchild"))
        before = walk_files(scratch)
        if mode == "named":                       # the named copy exists mid-run: that is what must go
            assert any(os.path.getsize(f) > 3_000_000 for f in before)
        else:                                      # descriptor mode: no copy is reachable by path
            assert before == []
        assert alive(ffprobe_pid) and alive(grandchild)
        proc.send_signal(sig)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == -sig                 # died BY the signal: conventional status
    assert os.listdir(scratch) == []               # private directories and copy are gone
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (alive(ffprobe_pid) or alive(grandchild)):
        time.sleep(0.05)
    assert not alive(ffprobe_pid) and not alive(grandchild)


def test_sigkill_leaves_no_copy_reachable_by_path_in_descriptor_mode(harness):
    start, scratch, marks = harness
    proc = start("fd")
    ffprobe_pid = grandchild = None
    try:
        ffprobe_pid = int(wait_for(marks / "ffprobe"))
        grandchild = int(wait_for(marks / "grandchild"))
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=20)
    finally:
        for pid in (ffprobe_pid, grandchild):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)         # SIGKILL cannot be handled: tidy the orphan
                except ProcessLookupError:
                    pass
    assert proc.returncode == -signal.SIGKILL
    assert walk_files(scratch) == []                     # only an empty working directory may remain
    assert media._reap_stale(max_age_s=0, base=str(scratch)) >= 0
    assert os.listdir(scratch) == []


def test_in_process_handlers_are_installed_only_while_busy_and_restored(tmp_path):
    seen = {}

    def mine(signum, frame):
        seen["called"] = signum

    old = {s: signal.signal(s, mine) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        (tmp_path / "f.bin").write_bytes(b"hello")
        snap = media.snapshot_confined(tmp_path, "f.bin")
        assert signal.getsignal(signal.SIGTERM) is media._on_signal      # busy: ours
        snap.close()
        assert signal.getsignal(signal.SIGTERM) is mine                  # idle: restored
        assert signal.getsignal(signal.SIGINT) is mine
        snap = media.snapshot_confined(tmp_path, "f.bin")
        os.kill(os.getpid(), signal.SIGTERM)                             # a foreign handler still decides
        time.sleep(0.2)
        assert seen.get("called") == signal.SIGTERM and signal.getsignal(signal.SIGTERM) is mine
        snap.close()
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def test_stale_private_directories_are_reaped_but_nothing_else(tmp_path):
    old = time.time() - 10 * 3600

    def make(name, mode=0o700, files=("in",), age=old):
        d = tmp_path / name
        d.mkdir()
        for f in files:
            (d / f).write_bytes(b"x" * 100)
        os.chmod(d, mode)
        os.utime(d, (age, age))
        return d

    stale = make("ctx-media-stale1")
    stale_scenes = make("ctx-media-stale2", files=("scenes.txt",))
    young = make("ctx-media-young", age=time.time())
    wrong_mode = make("ctx-media-open", mode=0o755)
    foreign_name = make("other-dir")
    unknown_content = make("ctx-media-extra", files=("in", "mine.txt"))
    with_subdir = make("ctx-media-sub", files=())
    (with_subdir / "d").mkdir()
    os.utime(with_subdir, (old, old))
    assert media._reap_stale(max_age_s=3600, base=str(tmp_path)) == 2
    assert not stale.exists() and not stale_scenes.exists()
    for keep in (young, wrong_mode, foreign_name, unknown_content, with_subdir):
        assert keep.exists(), keep.name


# ---------------------------------------- the cap follows the kind of file
def test_the_cap_that_fits_the_kind_applies_and_copying_stops_early(tmp_path, monkeypatch):
    reads = []
    real_read = os.read
    monkeypatch.setattr(media.os, "read", lambda fd, n: (reads.append(n), real_read(fd, n))[1])
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 100_000)
    # an image-shaped file far over the image cap: refused from its declared size
    # after ONE head chunk - the rest of the file is never read or copied
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20 + b"\x00" * (6 << 20)
    (tmp_path / "big.png").write_bytes(png)
    with pytest.raises(media.MediaLimitError, match="limit"):
        media.snapshot_confined(tmp_path, "big.png")
    assert sum(reads) <= media.HEAD_CHUNK
    reads.clear()
    # unknown/text content: the same image-sized cap, not the video cap
    (tmp_path / "big.txt").write_bytes(b"plain text\n" * 20_000)
    with pytest.raises(media.MediaLimitError):
        media.snapshot_confined(tmp_path, "big.txt")
    # a video-shaped file of the same size is allowed (video has its own, larger cap)
    (tmp_path / "v.mp4").write_bytes(H.mp4_header() + b"\x00" * 300_000)
    with media.snapshot_confined(tmp_path, "v.mp4") as snap:
        assert snap.size > 300_000 and media.probe(snap).format == "mp4"
    # growth while copying (declared size lied) stops the copy at the cap
    monkeypatch.setattr(media, "MAX_VIDEO_BYTES", 150_000)
    (tmp_path / "grow.mp4").write_bytes(H.mp4_header() + b"\x00" * 400_000)
    with pytest.raises(media.MediaLimitError):
        media.snapshot_confined(tmp_path, "grow.mp4", limit=150_000)


def test_hard_links_are_noted(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"data")
    os.link(tmp_path / "a.bin", tmp_path / "b.bin")
    with media.snapshot_confined(tmp_path, "b.bin") as snap:
        assert snap.nlink == 2
    (tmp_path / "a.bin").unlink()
    with media.snapshot_confined(tmp_path, "b.bin") as snap:
        assert snap.nlink == 1
