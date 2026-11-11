"""Run the media tests in all four Pillow x ffmpeg combinations.

CI only ever runs "neither"; the code paths differ a lot between the four
(frames without Pillow are validated by hand, image operations declare
themselves unavailable, video needs ffmpeg for everything), so a change to
`ctx.media` should be run through this once::

    python tests/media_matrix.py --with-pillow /path/to/venv-with-image-extra \\
                                 --without-pillow /path/to/venv-without

Each argument is a virtualenv directory with `pip install -e '.[dev]'` (the
first also with the `image` extra). "No ffmpeg" is a PATH that holds only a
few basic tools, never ffmpeg or ffprobe. Exit status is non-zero if any
combination fails; every combination prints its pytest summary line.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FILES = ["test_binfmt_media.py", "test_media.py", "test_media_hostile.py", "test_media_race.py",
         "test_media_signals.py", "test_view_cli.py", "test_view_next_lines.py"]
TOOLS = ["sh", "bash", "env", "ls", "cat", "head", "sleep", "rg", "git", "mkdir", "rm", "cp"]


def _path_without_ffmpeg(venv_bin: str, scratch: str) -> str:
    shim = Path(scratch) / "bin"
    shim.mkdir(exist_ok=True)
    for name in TOOLS:
        found = shutil.which(name)
        if found and not (shim / name).exists():
            (shim / name).symlink_to(found)
    return os.pathsep.join([venv_bin, str(shim)])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--with-pillow", required=True, help="virtualenv with the image extra")
    ap.add_argument("--without-pillow", required=True, help="virtualenv without Pillow")
    ns = ap.parse_args()
    tests = Path(__file__).resolve().parent
    repo = tests.parent
    status = 0
    with tempfile.TemporaryDirectory() as scratch:
        for label, venv, ffmpeg in (
            ("both present", ns.with_pillow, True),
            ("neither present", ns.without_pillow, False),
            ("ffmpeg only (no Pillow)", ns.without_pillow, True),
            ("Pillow only (no ffmpeg)", ns.with_pillow, False),
        ):
            vbin = str(Path(venv) / "bin")
            path = (vbin + os.pathsep + os.environ["PATH"]) if ffmpeg \
                else _path_without_ffmpeg(vbin, scratch)
            cp = subprocess.run(
                [str(Path(vbin) / "python"), "-m", "pytest", *[str(tests / f) for f in FILES],
                 "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-rs"],
                cwd=repo, env={**os.environ, "PATH": path}, capture_output=True, text=True)
            lines = cp.stdout.strip().splitlines()
            print(f"{label:28s} {lines[-1] if lines else cp.stderr.strip()[-200:]}")
            if cp.returncode:
                status = 1
                print("\n".join(lines[-30:]))
    return status


if __name__ == "__main__":
    sys.exit(main())
