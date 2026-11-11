"""Media views: overview first, zoom on demand, original bytes stay addressable.

``binfmt`` reads a blob's *structure*. This module goes one step further for
images and time-based media and produces **derived views**: a cheap
low-resolution overview that is enough to decide where to look, plus precise
zoom operations that always read the ORIGINAL pixels or frames — never a
previous derivative. The principle is the same one the text path uses.

  image  overview = downscaled copy with an optional labelled grid
         zoom     = crop of a named grid cell / pixel box / percent box
  video  overview = contact sheet of timestamped frames + a metadata census
         zoom     = one frame, a time range, scene changes, a subtitle track

Everything here is a library: no printing, no argv, no workspace policy. Every
operation returns a :class:`Derived` — the bytes plus a :class:`Derivation`
record (source hash, operation, parameters, engine and version, output hash,
bytes and estimated tokens before and after) — so a caller can show what was
produced, what it cost and what was left out.

Dependencies are optional and declared, never assumed:

  * image operations need Pillow (the ``image`` extra);
  * video operations need ``ffmpeg``/``ffprobe`` on ``PATH``; contact sheets
    additionally need Pillow to tile and label.

A missing engine, or a subprocess that exceeds its timeout, raises
:class:`MediaUnavailable` with a human-readable reason; callers print that as
a declared skip. :func:`probe` never raises — it reports what is knowable.

Guard rails: input byte and pixel limits (decompression bombs are refused
before pixels are decoded), bounded frame counts, and a timeout on every
subprocess. Subprocesses are always an argument list — never a shell — and
only container formats sniffed from magic bytes are ever handed to ffmpeg
(a playlist or concat script that names other files is refused), with the
protocol whitelist narrowed to ``file``.

Determinism: for a given engine version the same input and parameters yield
byte-identical output. Nothing model-visible carries a timestamp or an
absolute path.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import shutil
import atexit
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ctx import bounds, binfmt
from ctx.textutil import estimate_tokens

# ----------------------------------------------------------------- limits
MAX_IMAGE_BYTES = 64 * 1024 * 1024
# Under Pillow's own bomb warning threshold (~89.5 Mpx) so its global setting
# is never what we rely on; the check is made from the header, before decode.
MAX_IMAGE_PIXELS = 80_000_000
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
MAX_OUTPUT_EDGE = 8192
MIN_OUTPUT_EDGE = 16

DEFAULT_FIT_EDGE = 1024
DEFAULT_CROP_EDGE = 1280
DEFAULT_FRAME_EDGE = 1024
DEFAULT_SHEET_FRAMES = 12
MAX_SHEET_FRAMES = 36
MAX_RANGE_FRAMES = 24
SHEET_MAX_WIDTH = 1600
MAX_GRID = 26                      # columns are single letters A..Z
MIN_GRID_CELL_PX = 24              # a cell smaller than this cannot be labelled
DEFAULT_SCENE_THRESHOLD = 0.3
MAX_SCENES = 48
MAX_SUBTITLE_BYTES = 256 * 1024
MAX_ANIM_SCAN = 500                # frames walked to total an animation's length
MAX_ANIM_DECODE_PIXELS = 400_000_000   # total pixels decoded across frames
ANIM_BUDGET_S = 20                 # wall clock for one animated-image pass
MAX_VIDEO_EDGE = 16384             # declared frame size limits (untrusted header)
MAX_VIDEO_PIXELS = 64_000_000
MAX_ASPECT = 32                    # longest:shortest side, video and sheet tiles
MAX_TILE_EDGE = 1200
MAX_SHEET_PIXELS = 16_000_000
TEXT_FIELD_CHARS = 80              # any string that came from the media file
SUBS_LINE_CHARS = 120
SUBS_EXCERPT_LINES = 12
SUBS_EXCERPT_TOKENS = 450

PROBE_TIMEOUT_S = 20
FRAME_TIMEOUT_S = 30
SCENE_TIMEOUT_S = 120
SUBTITLE_TIMEOUT_S = 60
SHEET_BUDGET_S = 90

JPEG_QUALITIES = (85, 70, 55, 40)
SCREENSHOT_MAX_COLORS = 2048       # distinct colours in a coarse sample

_PILLOW_HINT = "install the `image` extra: pip install 'ctx-harness[image]'"
_FFMPEG_HINT = "install ffmpeg (provides ffmpeg and ffprobe) and put it on PATH"

# ffmpeg is only handed containers whose magic bytes we recognised.
# Sniffed container -> the ONE ffmpeg demuxer allowed to read it. ffmpeg and
# ffprobe are always told `-f <demuxer>` (never left to auto-detect), so a
# concat script, playlist, tty/ansi text renderer or image-sequence pattern
# cannot be selected by what the bytes look like to ffmpeg.
_DEMUX = {"mp4": "mov", "mov": "mov", "m4a": "mov", "mkv": "matroska",
          "webm": "matroska", "wav": "wav", "avi": "avi", "mp3": "mp3",
          "flac": "flac", "ogg": "ogg"}
_FFMPEG_FORMATS = frozenset(_DEMUX)
# Decoders ffmpeg may open. Raw/bitmap-style decoders (rawvideo, bmp, v210 …)
# are absent on purpose: they turn arbitrary referenced bytes into pixels.
_VIDEO_DECODERS = frozenset({
    "h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "msmpeg4v1", "msmpeg4v2",
    "msmpeg4v3", "mpeg2video", "mpeg1video", "mjpeg", "theora", "vc1", "wmv1",
    "wmv2", "wmv3", "h263", "h263p", "flv1", "prores", "dnxhd", "ffv1", "gif",
    "png", "apng", "webp", "vp6", "vp6f"})
_AUDIO_DECODERS = frozenset({
    "aac", "mp3", "mp3float", "mp2", "opus", "libopus", "vorbis", "flac", "ac3",
    "eac3", "alac", "wmav2", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_u8",
    "pcm_f32le", "pcm_s16be", "amr_nb", "amr_wb"})
_SUBTITLE_DECODERS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text",
                                "text", "microdvd", "subviewer", "jacosub", "sami"})
_DECODE_ALLOW = ",".join(sorted(_VIDEO_DECODERS | _AUDIO_DECODERS | _SUBTITLE_DECODERS))
_IN_NAME = "in"                    # the only file name ffmpeg is ever given
_BITMAP_SUBTITLES = frozenset({"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle",
                               "xsub", "dvb_teletext"})


# ------------------------------------------------------------- exceptions
class MediaError(Exception):
    """The input cannot be processed (bad region, undecodable bytes, …)."""


class MediaLimitError(MediaError):
    """The input exceeds a declared size, pixel or count limit."""


class RegionError(MediaError, ValueError):
    """A region, grid, cell or time expression could not be understood."""


class MediaUnsupported(MediaError):
    """The operation does not apply to this kind of media."""


class MediaUnavailable(Exception):
    """A required engine is missing or timed out — a *declared skip*.

    ``reason`` is human-readable and safe to print. ``needs`` names the
    missing piece (``pillow``, ``ffmpeg``, ``ffprobe``, ``timeout`` or
    ``renderer``) and ``hint`` says how to get it.
    """

    def __init__(self, reason: str, *, needs: str = "", hint: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.needs = needs
        self.hint = hint


# ------------------------------------------------------------ data classes
@dataclass(frozen=True)
class Derivation:
    source_sha256: str
    op: str
    params: dict[str, Any]
    engine: str
    engine_version: str
    output_sha256: str
    bytes_before: int
    bytes_after: int
    tokens_before: int
    tokens_after: int
    tokens_basis: str = "image-patch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256, "op": self.op,
            "params": dict(self.params), "engine": self.engine,
            "engine_version": self.engine_version,
            "output_sha256": self.output_sha256,
            "bytes_before": self.bytes_before, "bytes_after": self.bytes_after,
            "tokens_before": self.tokens_before, "tokens_after": self.tokens_after,
            "tokens_basis": self.tokens_basis,
        }


@dataclass(frozen=True)
class Derived:
    """Derived bytes + how they were made + what was left out."""

    data: bytes
    mime: str
    ext: str
    op: str
    record: Derivation
    width: int | None = None
    height: int | None = None
    omitted: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaProbe:
    kind: str                       # image | video | audio | pdf | other
    format: str
    bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None
    has_alpha: bool | None = None
    animated: bool | None = None
    frames: int | None = None
    orientation: int | None = None  # EXIF, images only (1 = upright)
    duration_s: float | None = None
    fps: float | None = None
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_streams: list[dict[str, Any]] = field(default_factory=list)
    subtitle_streams: list[dict[str, Any]] = field(default_factory=list)
    pages: int | None = None
    engine: str = "header"
    notes: list[str] = field(default_factory=list)
    # Set when ffprobe could not run (missing or timed out); operations that
    # need the census re-raise it as the declared skip it already is.
    unavailable: MediaUnavailable | None = field(default=None, repr=False)
    failed: str | None = field(default=None, repr=False)   # a tool failure, not "empty"
    refused: str | None = field(default=None, repr=False)  # policy: not decoded
    limit: str | None = field(default=None, repr=False)    # declared size limit hit
    anim_starts: list[float] = field(default_factory=list, repr=False)
    streams_known: bool = False     # True once ffprobe (not just a header) answered
    scan_truncated: bool = False    # frames/duration are a lower bound (scan stopped early)

    @property
    def is_timeline(self) -> bool:
        """True for video and for animated images (treated as short videos)."""
        return self.kind == "video" or (self.kind == "image" and bool(self.animated))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind, "format": self.format, "bytes": self.bytes,
            "sha256": self.sha256, "engine": self.engine,
        }
        for key in ("width", "height", "has_alpha", "animated", "frames",
                    "orientation", "duration_s", "fps", "container",
                    "video_codec", "audio_codec", "pages"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.scan_truncated:
            out["scan_truncated"] = True
        if self.kind in ("video", "audio"):
            out["streams_known"] = self.streams_known
        if self.audio_streams:
            out["audio_streams"] = [dict(s) for s in self.audio_streams]
        if self.subtitle_streams:
            out["subtitle_streams"] = [dict(s) for s in self.subtitle_streams]
        if self.notes:
            out["notes"] = list(self.notes)
        return out


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    w: int
    h: int
    label: str = ""
    clipped: bool = False

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "label": self.label}


@dataclass(frozen=True)
class Materialized:
    rel_path: str      # workspace-relative, POSIX, the path a host tool opens
    blob: str          # full sha256 of the stored derivative
    address: str       # `blob:<12 hex>` — retrievable with `ctx get`/`ctx view`
    created: bool      # False when the same derivation was already on disk


# ------------------------------------------------------------ small helpers
def _floor1(n):
    """Pixel/divisor floor (never an emission bound; those use `bounds`)."""
    return max(n, 1)


def _edge(value: object, default: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return max(MIN_OUTPUT_EDGE, min(MAX_OUTPUT_EDGE, n))


def fmt_ts(t: float) -> str:
    """``M:SS.t`` or ``H:MM:SS.t`` — locale-free, one decimal."""
    tenths = max(0, int(round(float(t) * 10)))
    total, tenth = divmod(tenths, 10)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{tenth}"
    return f"{minutes}:{seconds:02d}.{tenth}"


_NUM = re.compile(r"\d+(?:\.\d+)?")


def parse_time(spec: str | float | int) -> float:
    """Seconds from ``12.5``, ``12.5s``, ``250ms``, ``1:23`` or ``1:02:03.5``.

    Only non-negative decimal numbers are accepted; every clock part is plain
    digits (seconds may carry a fraction) and minutes/seconds stay below 60.
    A sign, exponent, empty part or stray character is a usage error."""
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        value = float(spec)
        if not math.isfinite(value) or value < 0:
            raise RegionError(f"time must be a finite value >= 0, got {spec!r}")
        return value + 0.0
    text = str(spec).strip().lower()
    bad = RegionError(f"cannot read time {spec!r}; use seconds (12.5), "
                      "m:ss (1:23) or h:mm:ss (1:02:03)")
    if not text:
        raise bad
    if text.endswith("ms"):
        if not _NUM.fullmatch(text[:-2]):
            raise bad
        return float(text[:-2]) / 1000.0
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or not all(_NUM.fullmatch(x) for x in parts):
            raise bad
        if any("." in x for x in parts[:-1]) or any(float(x) >= 60 for x in parts[1:]):
            raise bad
        value = 0.0
        for part in parts:
            value = value * 60 + float(part)
        return value
    body = text[:-1] if text.endswith("s") else text
    if not _NUM.fullmatch(body):
        raise bad
    return float(body)


def parse_range(spec: str) -> tuple[float, float]:
    """``A-B`` or ``A..B``; each side uses the same grammar as ``--at``
    (seconds ``2.5``, clock ``1:23``, ``1:02:03``). ``A:B`` is refused: with
    ``1:30`` meaning one minute thirty for ``--at``, reading it as "seconds 1
    to 30" for a range would be a trap."""
    text = str(spec).strip()
    for sep in ("..", "-"):
        if sep in text:
            left, _, right = text.partition(sep)
            start, end = parse_time(left), parse_time(right)
            if end <= start:
                raise RegionError(f"range {spec!r} must end after it starts")
            return start, end
    hint = (" (':' alone is ambiguous: write 2-9 for seconds 2 to 9, "
            "1:23-1:30 for clock times)" if ":" in text else "")
    raise RegionError(f"cannot read range {spec!r}; write A-B, e.g. 2-9 or "
                      f"1:23-1:30{hint}")


def parse_grid(spec: str) -> tuple[int, int]:
    """``RxC`` -> (rows, cols), each 1..MAX_GRID."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[xX×]\s*(\d{1,2})\s*", str(spec))
    if not m:
        raise RegionError(f"cannot read grid {spec!r}; use RxC, e.g. 3x4 "
                          "(rows x columns)")
    rows, cols = int(m.group(1)), int(m.group(2))
    if not (1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise RegionError(f"grid {spec!r} out of range; each side is 1..{MAX_GRID}")
    return rows, cols


def default_grid(width: int, height: int) -> tuple[int, int]:
    """(rows, cols): 3 cells along the short side, up to 6 along the long."""
    short = 3
    ratio = max(width, height) / _floor1(min(width, height))
    long = min(6, max(3, round(short * ratio)))
    return (short, long) if width >= height else (long, short)


def cell_label(row: int, col: int) -> str:
    """0-based (row, col) -> ``B2`` (column letter, 1-based row)."""
    return f"{chr(ord('A') + col)}{row + 1}"


def _cell_box(row: int, col: int, rows: int, cols: int,
              width: int, height: int) -> tuple[int, int, int, int]:
    """Integer cell edges ``i*W//C``: tiles partition the image exactly, with
    no gap or overlap and the last edge landing on W."""
    return (col * width // cols, row * height // rows,
            (col + 1) * width // cols, (row + 1) * height // rows)


def parse_region(spec: str, size: tuple[int, int],
                 grid: tuple[int, int] | None = None) -> Region:
    """Region grammar against an image of ``size`` (W, H) pixels.

    * ``x,y,w,h``              pixels
    * ``x%,y%,w%,h%``          percentages of width/height (all four with ``%``)
    * ``B2``                   one grid cell: column letter, row number
    * ``A1:B2``                a range of cells (any two opposite corners)

    Cells are resolved against ``grid`` (rows, cols) — ``default_grid`` when
    omitted. A pixel/percent box that overruns the image is clipped (and
    flagged); one entirely outside it is an error.
    """
    width, height = size
    text = str(spec).strip()
    if not text:
        raise RegionError("empty region")
    if "," in text:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 4:
            raise RegionError(f"region {spec!r} needs four values: x,y,w,h")
        pct = [p.endswith("%") for p in parts]
        if any(pct) and not all(pct):
            raise RegionError("do not mix pixels and percentages in one region")
        try:
            vals = [float(p[:-1] if p.endswith("%") else p) for p in parts]
        except ValueError:
            raise RegionError(f"cannot read region {spec!r}") from None
        if not all(math.isfinite(v) for v in vals):
            raise RegionError(f"cannot read region {spec!r}")
        if all(pct):
            if any(v < 0 or v > 100 for v in vals):
                raise RegionError("percentages are 0..100")
            x0 = round(vals[0] * width / 100)
            y0 = round(vals[1] * height / 100)
            x1 = round((vals[0] + vals[2]) * width / 100)
            y1 = round((vals[1] + vals[3]) * height / 100)
            label = f"{parts[0]},{parts[1]},{parts[2]},{parts[3]}"
        else:
            if any(v != int(v) for v in vals):
                raise RegionError("pixel regions are whole numbers")
            x0, y0 = int(vals[0]), int(vals[1])
            x1, y1 = x0 + int(vals[2]), y0 + int(vals[3])
            label = f"{x0},{y0},{int(vals[2])},{int(vals[3])}"
        if x0 < 0 or y0 < 0:
            raise RegionError("region origin cannot be negative")
        if x1 <= x0 or y1 <= y0:
            raise RegionError("region must have positive width and height")
        if x0 >= width or y0 >= height:
            raise RegionError(
                f"region starts outside the {width}x{height} image")
        clipped = x1 > width or y1 > height
        x1, y1 = min(x1, width), min(y1, height)
        return Region(x0, y0, x1 - x0, y1 - y0, label, clipped)

    rows, cols = grid if grid is not None else default_grid(width, height)
    pieces = text.split(":")
    if len(pieces) > 2:
        raise RegionError(f"cannot read region {spec!r}")
    corners: list[tuple[int, int]] = []
    for piece in pieces:
        m = re.fullmatch(r"([A-Za-z])(\d{1,2})", piece.strip())
        if not m:
            raise RegionError(
                f"cannot read region {spec!r}: use a cell (B2), a range "
                "(A1:B2), pixels (x,y,w,h) or percentages (10%,10%,50%,50%)")
        col = ord(m.group(1).upper()) - ord("A")
        row = int(m.group(2)) - 1
        if not (0 <= col < cols and 0 <= row < rows):
            raise RegionError(
                f"cell {piece.strip().upper()} is outside the {rows}x{cols} grid "
                f"(columns A-{chr(ord('A') + cols - 1)}, rows 1-{rows})")
        corners.append((row, col))
    r0 = min(c[0] for c in corners)
    r1 = max(c[0] for c in corners)
    c0 = min(c[1] for c in corners)
    c1 = max(c[1] for c in corners)
    x0, y0, _, _ = _cell_box(r0, c0, rows, cols, width, height)
    _, _, x1, y1 = _cell_box(r1, c1, rows, cols, width, height)
    if len(corners) == 1:
        label = cell_label(r0, c0)
    else:
        label = f"{cell_label(r0, c0)}:{cell_label(r1, c1)}"
    return Region(x0, y0, x1 - x0, y1 - y0, label, False)


# ---------------------------------------------------------- engine presence
def _pillow():
    try:
        import PIL
        from PIL import Image, ImageDraw, ImageFont, ImageOps  # noqa: F401
    except Exception:
        raise MediaUnavailable(
            "Pillow is not installed, so images cannot be resized, cropped "
            "or tiled", needs="pillow", hint=_PILLOW_HINT) from None
    return PIL


def pillow_version() -> str | None:
    try:
        import PIL
        return str(PIL.__version__)
    except Exception:
        return None


def _which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaUnavailable(
            f"{name} was not found on PATH, so video cannot be inspected",
            needs=name, hint=_FFMPEG_HINT)
    return path


_VERSIONS: dict[str, str] = {}


def _tool_version(path: str) -> str:
    """`name version` of an ffmpeg-family binary; a failure is not cached."""
    if path in _VERSIONS:
        return _VERSIONS[path]
    try:
        cp = _run([path, "-version"], PROBE_TIMEOUT_S)
        first = cp.stdout.decode("utf-8", "replace").splitlines()[0].split()
        found = clean_text(f"{first[0]} {first[2]}", 48)
    except Exception:
        return "unknown"
    _VERSIONS[path] = found
    return found


def ffmpeg_version() -> str | None:
    path = shutil.which("ffmpeg")
    return _tool_version(path) if path else None


def capabilities() -> dict[str, str | None]:
    """What this machine can do, for engine disclosure. Values are versions."""
    return {"pillow": pillow_version(), "ffmpeg": ffmpeg_version(),
            "ffprobe": _tool_version(p) if (p := shutil.which("ffprobe")) else None}


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)   # the wrapper AND its children
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# ------------------------------------------------------- lifetime and signals
# What must not outlive this process: private directories (and, where the
# platform has no unnamed files, the named copy inside one) and ffmpeg/ffprobe
# process groups. Normal exit, exceptions and timeouts clean up through the
# context managers; for SIGTERM, SIGHUP and SIGINT a handler is installed only
# while something is live, kills the children's groups, removes the
# directories, puts the previous handlers back, and then lets the signal take
# its conventional effect (death by that signal, so the exit status is 128+n).
# SIGKILL cannot be handled; the design makes it harmless instead: where
# /dev/fd and O_TMPFILE exist the copy is an UNNAMED file reachable only by
# descriptor, so there is nothing on disk to leave behind.
_LOCK = threading.RLock()
_LIVE_DIRS: set[str] = set()
_CHILDREN: set[subprocess.Popen] = set()
_PREVIOUS: dict[int, Any] = {}
_USERS = 0
_REAPED = False
PRIVATE_PREFIX = "ctx-media-"
STALE_AFTER_S = 6 * 3600
_SIGNALS = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGHUP", "SIGINT") if hasattr(signal, n))
# Descriptor mode: hand ffmpeg an inherited descriptor (`/dev/fd/N`) instead of a
# path, so no directory entry for the copy ever has to exist. It works with the
# demuxers pinned here, including the seeking ones (mov/matroska/avi/flac):
# opening /dev/fd/N re-opens the same inode as an ordinary seekable file.
_FD_MODE = os.path.isdir("/dev/fd") and hasattr(os, "pread")


def _purge() -> None:
    """Kill live children and remove live private directories (idempotent)."""
    with _LOCK:
        for proc in list(_CHILDREN):
            _kill_group(proc)
        _CHILDREN.clear()
        for d in list(_LIVE_DIRS):
            shutil.rmtree(d, ignore_errors=True)
        _LIVE_DIRS.clear()


atexit.register(_purge)


def _on_signal(signum: int, frame: Any) -> None:
    _purge()
    with _LOCK:
        previous = dict(_PREVIOUS)
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        _PREVIOUS.clear()
    handler = previous.get(signum, signal.SIG_DFL)
    if callable(handler) and handler is not signal.default_int_handler:
        handler(signum, frame)            # somebody else's handler: let it decide
        return
    if handler == signal.SIG_IGN:
        return
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)          # die BY the signal: conventional status


def _acquire() -> None:
    global _USERS, _REAPED
    with _LOCK:
        _USERS += 1
        if not _REAPED:
            _REAPED = True
            _reap_stale()
        if (not _PREVIOUS and threading.current_thread() is threading.main_thread()):
            for sig in _SIGNALS:
                try:
                    _PREVIOUS[sig] = signal.getsignal(sig)
                    signal.signal(sig, _on_signal)
                except (ValueError, OSError):
                    _PREVIOUS.pop(sig, None)


def _release() -> None:
    global _USERS
    with _LOCK:
        _USERS = max(0, _USERS - 1)
        if _USERS == 0 and _PREVIOUS:
            for sig, handler in _PREVIOUS.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
            _PREVIOUS.clear()


def _reap_stale(max_age_s: float = STALE_AFTER_S, base: str | None = None) -> int:
    """Remove private directories THIS tool left behind (a process killed with
    SIGKILL, say): named ``ctx-media-*``, owned by this user, mode 0700, older
    than ``max_age_s``, and holding only the regular files this tool writes.
    Anything else in the temp directory is never touched."""
    import stat as _stat

    removed = 0
    root = base or tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if not name.startswith(PRIVATE_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            st = os.lstat(path)
            if (not _stat.S_ISDIR(st.st_mode) or _stat.S_IMODE(st.st_mode) != 0o700
                    or (hasattr(os, "getuid") and st.st_uid != os.getuid())
                    or now - st.st_mtime < max_age_s or path in _LIVE_DIRS):
                continue
            entries = os.listdir(path)
            if not set(entries) <= {_IN_NAME, "scenes.txt"}:
                continue
            if any(not _stat.S_ISREG(os.lstat(os.path.join(path, e)).st_mode) for e in entries):
                continue
            for e in entries:
                os.unlink(os.path.join(path, e))
            os.rmdir(path)
            removed += 1
        except OSError:
            continue
    return removed


@contextlib.contextmanager
def _private_dir() -> Iterator[str]:
    """A fresh 0700 directory, removed on exit and on termination signals."""
    _acquire()
    tmp = tempfile.mkdtemp(prefix=PRIVATE_PREFIX)
    with _LOCK:
        _LIVE_DIRS.add(tmp)
    try:
        yield tmp
    finally:
        with _LOCK:
            _LIVE_DIRS.discard(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        _release()


def _run(argv: list[str], timeout: float, cwd: str | None = None,
         src: "_Src | None" = None) -> subprocess.CompletedProcess:
    """One subprocess: argument list, no shell, no stdin, hard timeout, its
    own process group (killed whole on timeout or when this process is told to
    terminate), in a private working dir. With ``src`` in descriptor mode the
    ``-i`` argument becomes ``/dev/fd/N`` and N is inherited by the child."""
    name = Path(argv[0]).name
    pass_fds: tuple[int, ...] = ()
    if src is not None and src.fd is not None and "-i" in argv:
        i = argv.index("-i")
        argv = argv[:i + 1] + [f"/dev/fd/{src.fd}"] + argv[i + 2:]
        pass_fds = (src.fd,)
    _acquire()
    try:
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=cwd, pass_fds=pass_fds,
                start_new_session=(os.name == "posix"))
        except FileNotFoundError:
            raise MediaUnavailable(f"{name} was not found on PATH", needs=name,
                                   hint=_FFMPEG_HINT) from None
        except OSError as exc:
            raise MediaUnavailable(f"{name} could not be started: {exc.strerror or 'error'}",
                                   needs=name, hint=_FFMPEG_HINT) from None
        with _LOCK:
            _CHILDREN.add(proc)
        limit = max(float(timeout), 1.0)
        try:
            out, err = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise MediaUnavailable(
                f"{name} did not finish within {int(limit)}s; the media is too "
                "large or complex for an inline view", needs="timeout") from None
        finally:
            with _LOCK:
                _CHILDREN.discard(proc)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        _release()


def _failure(cp: subprocess.CompletedProcess, tool: str = "ffmpeg") -> str:
    """A tool failure in words that never echo the tool's own text: stderr
    carries absolute workspace and store paths and bytes from the media."""
    if cp.returncode == 0:
        return f"{tool} exited normally but its output was not what was asked for"
    return f"{tool} failed (exit status {cp.returncode})"


# ----------------------------------------------------------- hostile strings
_CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e"
                   "\u2066-\u2069\ufeff]")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]{0,31}")
_LANG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})?")


def clean_text(value: object, limit: int = TEXT_FIELD_CHARS) -> str:
    """A string that came from a media file, made safe to print: one line, no
    control or bidi characters, bounded. It can sit inside a line of the
    digest but cannot start one or add one."""
    text = " ".join(_CTRL.sub(" ", str(value)).split())
    limit = bounds.count(limit, default=TEXT_FIELD_CHARS)
    return text if len(text) <= limit else text[:max(limit - 1, 0)] + "…"


def has_control_chars(text: object) -> bool:
    return bool(_CTRL.search(str(text)))


def _ident(value: object, default: str = "unknown") -> str:
    """Codec / format / pixel-format names: a tight charset or ``unknown``."""
    text = "" if value is None else str(value)
    return text if _IDENT.fullmatch(text) else default


def _lang(value: object) -> str | None:
    text = "" if value is None else str(value)
    return text if _LANG.fullmatch(text) else None


def _dim(value: object) -> int | None:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return n if 0 < n <= 1_000_000 else None


class _Deadline:
    """A wall-clock budget for one operation; exhausting it is a declared skip."""

    def __init__(self, seconds: float, what: str):
        self.seconds, self.what = seconds, what
        self._end = time.monotonic() + seconds

    def left(self) -> float:
        return self._end - time.monotonic()

    def check(self) -> None:
        if self.left() <= 0:
            raise MediaUnavailable(
                f"{self.what} exceeded its {int(self.seconds)}s budget",
                needs="timeout")


# ------------------------------------------------------------------ sources
HEAD_CHUNK = 64 * 1024


def _cap_for_head(head: bytes) -> int:
    """The size cap that fits what the first chunk says the file is: only
    video and audio are allowed to be large; everything else is read at most
    to the image limit (it is never decoded beyond that)."""
    kind = _kind_of(binfmt.sniff_format(head))
    return MAX_VIDEO_BYTES if kind in ("video", "audio") else MAX_IMAGE_BYTES


class _Src:
    """The one thing every operation reads: bytes in memory, a plain file, or -
    for anything that came from the filesystem or the store - an owned
    **snapshot**: the target is opened exactly once and copied (hashing as it
    goes) into a private copy; from then on sniffing, hashing, Pillow, ffprobe
    and ffmpeg all see that same copy. The sha256 recorded in a derivation is
    therefore, by construction, the hash of what was decoded.

    Where the platform allows (``_FD_MODE``) the copy is an unnamed file held
    only by a descriptor: nothing on disk to find, nothing to leave behind
    after SIGKILL, and ffmpeg is handed the inherited descriptor
    (``/dev/fd/N``). Elsewhere it is a named file in a 0700 directory that is
    removed on exit and on termination signals."""

    def __init__(self, source: Any):
        self._sha: str | None = None
        self._tmp_dir: str | None = None
        self._tmp_ctx: Any = None
        self.owned = False
        self.fd: int | None = None
        self.nlink = 1
        if isinstance(source, (bytes, bytearray, memoryview)):
            self.data: bytes | None = bytes(source)
            self.path: Path | None = None
            self.size = len(self.data)
        else:
            self.data = None
            self.path = Path(source)
            try:
                self.size = self.path.stat().st_size
            except OSError as exc:
                raise MediaError(f"cannot read source: {exc.strerror or exc}") from None

    @classmethod
    def from_fd(cls, fd: int, *, limit: int, st_size: int | None = None,
                nlink: int = 1) -> "_Src":
        """Copy an already-open, already-validated descriptor (closing it).
        The first chunk is read and classified BEFORE anything else, so the cap
        that fits the kind applies: a declared size over it is refused with
        nothing more read, and growth past it while copying stops the copy."""
        self = cls.__new__(cls)
        self._sha = None
        self._tmp_dir = None
        self._tmp_ctx = None
        self.data = None
        self.owned = True
        self.fd = None
        self.path = None
        self.nlink = nlink
        _acquire()
        self._acquired = True
        out = None
        try:
            head = os.read(fd, HEAD_CHUNK)
            cap = min(limit, _cap_for_head(head))
            if st_size is not None and st_size > cap:
                raise MediaLimitError(f"input is {st_size:,} bytes; the limit for this "
                                      f"kind of file is {cap:,}")
            out = self._create_copy()
            h = hashlib.sha256()
            total = 0
            chunk = head
            while chunk:
                total += len(chunk)
                if total > cap:
                    raise MediaLimitError(
                        f"input is over the {cap:,}-byte limit for this kind of file")
                h.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view):]
                chunk = os.read(fd, 1 << 20)
        except BaseException:
            if out is not None:
                os.close(out)
            self.close()
            raise
        finally:
            os.close(fd)
        self.size = total
        self._sha = h.hexdigest()
        if _FD_MODE:
            self.fd = out
        else:
            os.close(out)
        return self

    def _create_copy(self) -> int:
        """A writable descriptor on a fresh private copy (see class doc)."""
        if _FD_MODE:
            if hasattr(os, "O_TMPFILE"):
                try:
                    return os.open(tempfile.gettempdir(), os.O_TMPFILE | os.O_RDWR, 0o600)
                except OSError:
                    pass
            tmp = tempfile.mkdtemp(prefix=PRIVATE_PREFIX)
            try:
                path = os.path.join(tmp, _IN_NAME)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.unlink(path)                      # unreachable by path from here on
                return fd
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        tmp = tempfile.mkdtemp(prefix=PRIVATE_PREFIX)
        with _LOCK:
            _LIVE_DIRS.add(tmp)
        self._tmp_dir = tmp
        self.path = Path(tmp) / _IN_NAME
        return os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self._tmp_dir is not None:
            with _LOCK:
                _LIVE_DIRS.discard(self._tmp_dir)
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
        if getattr(self, "_acquired", False):
            self._acquired = False
            _release()

    def __enter__(self) -> "_Src":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pread(self, n: int, offset: int = 0) -> bytes:
        out = []
        while n > 0:
            chunk = os.pread(self.fd, min(n, 1 << 20), offset)  # type: ignore[arg-type]
            if not chunk:
                break
            out.append(chunk)
            n -= len(chunk)
            offset += len(chunk)
        return b"".join(out)

    def head(self, n: int = 4096) -> bytes:
        if self.data is not None:
            return self.data[:n]
        if self.fd is not None:
            return self._pread(n)
        try:
            with self.path.open("rb") as fh:  # type: ignore[union-attr]
                return fh.read(n)
        except OSError as exc:
            raise MediaError(f"cannot read source: {exc.strerror or exc}") from None

    def sha256(self) -> str:
        if self._sha is None:
            h = hashlib.sha256()
            if self.data is not None:
                h.update(self.data)
            else:
                try:
                    with self.path.open("rb") as fh:  # type: ignore[union-attr]
                        while chunk := fh.read(1 << 20):
                            h.update(chunk)
                except OSError as exc:
                    raise MediaError(f"cannot read source: {exc.strerror or exc}") from None
            self._sha = h.hexdigest()
        return self._sha

    def read(self, limit: int) -> bytes:
        if self.size > limit:
            raise MediaLimitError(
                f"input is {self.size:,} bytes; the limit for this operation is "
                f"{limit:,}")
        if self.data is not None:
            return self.data
        if self.fd is not None:
            return self._pread(self.size)
        try:
            return self.path.read_bytes()  # type: ignore[union-attr]
        except OSError as exc:
            raise MediaError(f"cannot read source: {exc.strerror or exc}") from None

    @contextlib.contextmanager
    def sandbox(self) -> Iterator[str]:
        """A private directory to run ffmpeg in, as its cwd. ffmpeg is given
        either the inherited descriptor (descriptor mode: the directory is then
        EMPTY apart from output files) or the bare fixed name ``in``, so no
        user-controlled path component is ever parsed (no ``%d`` patterns,
        ``-`` options, ``concat:`` protocols) and a relative reference inside
        the container finds nothing else to read."""
        if self.owned and self._tmp_dir is not None:
            yield self._tmp_dir
            return
        with _private_dir() as tmp:
            if self.fd is None:
                target = Path(tmp) / _IN_NAME
                if self.path is not None:
                    try:
                        os.symlink(self.path.resolve(), target)
                    except OSError:
                        shutil.copyfile(self.path, target)
                else:
                    target.write_bytes(self.data or b"")
            yield tmp


Snapshot = _Src


def _as_src(source: Any) -> _Src:
    return source if isinstance(source, _Src) else _Src(source)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR_FD_OK = os.open in getattr(os, "supports_dir_fd", set()) and bool(_NOFOLLOW)


def _validate_open(fd: int, limit: int):
    """Decide from the OPEN descriptor, never from the path: a regular file
    within the size cap. (A FIFO or device opened with O_NONBLOCK never blocks
    here and is refused.)"""
    import stat as _stat

    st = os.fstat(fd)
    if not _stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise MediaError("not a regular file")
    return st


# Known, deliberate edges (also listed for the documentation owner):
#  * A HARD LINK inside the workspace to a file outside it is viewed. It is the
#    same inode, and creating the link needed the same access as copying the
#    file; `Snapshot.nlink` (> 1) lets the caller print a one-line note.
#  * A SYMLINK inside the workspace to another file inside it is followed (the
#    path is resolved and confined first, then opened once without following).
#  * Without directory-relative no-follow opens (no `dir_fd`/`O_NOFOLLOW`, i.e.
#    Windows) only the FINAL path component is protected against a swap.
#  * An MP3 with no ID3 tag is reported as generic binary by the sniff: a bare
#    frame sync is too weak a signature to reclassify arbitrary output.
def snapshot_confined(root: Any, rel: str, *, limit: int = MAX_VIDEO_BYTES) -> _Src:
    """Snapshot the file at workspace-relative ``rel`` with ONE open, race-free.

    Containment is structural: starting from a descriptor on ``root``, every
    path component is opened with ``dir_fd`` + ``O_NOFOLLOW`` (``O_DIRECTORY``
    for directories), so no symlink is followed at any component, ``..`` is
    refused, and nothing can resolve outside ``root`` however the tree is
    mutated meanwhile. The final component is opened ``O_NOFOLLOW|O_NONBLOCK``
    and validated from the descriptor. On a platform without ``dir_fd`` or
    ``O_NOFOLLOW`` (Windows) only the final-component open is single and
    validated; intermediate components can then be raced, and are not."""
    parts = [x for x in str(rel).replace("\\", "/").split("/")]
    if not parts or any(x in ("", ".", "..") for x in parts):
        raise MediaError("path is not a plain workspace-relative file name")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    try:
        if _DIR_FD_OK:
            cur = os.open(str(root), flags | os.O_DIRECTORY)
            try:
                for part in parts[:-1]:
                    nxt = os.open(part, flags | os.O_DIRECTORY | _NOFOLLOW, dir_fd=cur)
                    os.close(cur)
                    cur = nxt
                fd = os.open(parts[-1], flags | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                             dir_fd=cur)
            finally:
                os.close(cur)
        else:
            fd = os.open(os.path.join(str(root), *parts),
                         flags | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        import errno

        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise MediaError("a symbolic link (or non-directory) is on the path; "
                             "`ctx view` does not follow links") from None
        raise MediaError(f"cannot read source: {exc.strerror or 'error'}") from None
    st = _validate_open(fd, limit)
    return _Src.from_fd(fd, limit=limit, st_size=st.st_size, nlink=st.st_nlink)


def snapshot_path(path: Any, *, limit: int = MAX_VIDEO_BYTES) -> _Src:
    """Snapshot one file by absolute path (a store blob, or a path policy has
    already allowed outside the workspace): a single open that refuses a
    symlink at the final component and never blocks on a FIFO."""
    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
             | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise MediaError(f"cannot read source: {exc.strerror or 'error'}") from None
    st = _validate_open(fd, limit)
    return _Src.from_fd(fd, limit=limit, st_size=st.st_size, nlink=st.st_nlink)


def _kind_of(fmt: str) -> str:
    if fmt in binfmt.IMAGE_FORMATS or fmt == "svg":
        return "image"
    if fmt in ("mp4", "mov", "mkv", "webm", "avi"):
        return "video"
    if fmt in ("m4a", "mp3", "flac", "wav", "ogg"):
        return "audio"
    if fmt == "pdf":
        return "pdf"
    return "other"


_ALPHA_PIX = re.compile(r"^(yuva|rgba|bgra|argb|abgr|gbrap|ya)")


def _rate(text: Any) -> float | None:
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
        return round(value, 3) if value > 0 and math.isfinite(value) else None
    except (ValueError, ZeroDivisionError):
        return None


# ------------------------------------------------------------------- probe
def probe(source: Any) -> MediaProbe:
    """Classify a payload and report what is knowable. Never raises.

    Header-only facts are always available; Pillow adds animation, alpha and
    EXIF orientation; ffprobe adds duration, frame rate, codecs, audio and
    subtitle streams. Whatever could not be learned is named in ``notes``.
    """
    try:
        src = _as_src(source)
        head = src.head(65536)
        fmt = binfmt.sniff_format(head)
        p = MediaProbe(kind=_kind_of(fmt), format=fmt, bytes=src.size,
                       sha256=src.sha256())
    except MediaError as exc:
        return MediaProbe(kind="other", format="unreadable", bytes=0, sha256="",
                          notes=[str(exc)])
    try:
        if p.kind == "image":
            _probe_image(src, p)
        elif p.kind in ("video", "audio"):
            _probe_av(src, p)
        elif p.kind == "pdf":
            if src.size <= MAX_IMAGE_BYTES:
                p.pages = binfmt.inspect(src.read(MAX_IMAGE_BYTES)).pages
    except Exception as exc:  # a probe is advisory; never a traceback
        p.notes.append(f"probe incomplete: {type(exc).__name__}")
    return p


_ANIMATED_FORMATS = frozenset({"GIF", "WEBP", "PNG", "TIFF"})


def _anim_scan(im, p: MediaProbe) -> None:
    """One bounded pass over an animation's frames: count, start times and
    length. Stops at the frame cap, the total-decoded-pixel budget or the wall
    clock, and says so in ``p.notes``; what was not scanned is not claimed."""
    deadline = _Deadline(ANIM_BUDGET_S, "animated-image scan")
    width, height = im.size
    per = width * height
    starts: list[float] = []
    acc = 0.0
    stopped = None
    index = 0
    while True:
        if len(starts) >= MAX_ANIM_SCAN:
            stopped = f"the {MAX_ANIM_SCAN}-frame cap"
            break
        if (len(starts) + 1) * per > MAX_ANIM_DECODE_PIXELS:
            stopped = "the decoded-pixel budget"
            break
        if deadline.left() <= 0:
            stopped = f"the {ANIM_BUDGET_S}s time budget"
            break
        try:
            im.seek(index)
        except EOFError:
            break
        starts.append(round(acc, 3))
        acc += int(im.info.get("duration", 100) or 100) / 1000.0
        index += 1
    p.frames = len(starts)
    p.scan_truncated = bool(stopped)
    p.animated = len(starts) > 1
    if p.animated:
        p.anim_starts = starts
        p.duration_s = round(acc, 3)
        p.fps = round(len(starts) / acc, 3) if acc else None
        if stopped:
            p.notes.append(f"animation scanned to {len(starts)} frames, stopped by "
                           f"{stopped}; later frames exist and are not shown")
    if stopped and not p.animated:
        p.notes.append(f"animation scan stopped by {stopped} before the frame count "
                       "was known; shown as a still")
    try:
        im.seek(0)
    except EOFError:
        pass


def _probe_image(src: _Src, p: MediaProbe) -> None:
    if p.format == "svg":
        p.notes.append("vector markup: read it as text; ctx does not rasterize SVG")
        return
    if src.size <= MAX_IMAGE_BYTES:
        info = binfmt.inspect(src.read(MAX_IMAGE_BYTES))
        p.width, p.height = info.width, info.height
        p.has_alpha = (None if info.color is None
                       else info.color in ("rgba", "gray+a"))
        if info.format in ("gif",):
            p.has_alpha = None
    else:
        info = binfmt.inspect(src.head(1 << 16))
        p.width, p.height = info.width, info.height
        p.notes.append(f"over the {MAX_IMAGE_BYTES:,}-byte image limit; "
                       "header facts only")
        return
    if p.width and p.height and p.width * p.height > MAX_IMAGE_PIXELS:
        p.notes.append(f"{p.width * p.height:,} pixels exceeds the "
                       f"{MAX_IMAGE_PIXELS:,}-pixel limit; not decoded")
        return
    if pillow_version() is None:
        p.notes.append("animation and orientation unknown: Pillow not installed")
        return
    from PIL import Image

    p.engine = "pillow"
    try:
        with Image.open(io.BytesIO(src.read(MAX_IMAGE_BYTES))) as im:
            p.width, p.height = im.size
            p.has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            p.frames = 1
            p.animated = False
            try:
                p.orientation = int(im.getexif().get(0x0112, 1) or 1)
            except Exception:
                p.orientation = None
            if p.orientation in (5, 6, 7, 8) and p.width and p.height:
                p.width, p.height = p.height, p.width  # as displayed
            if im.format in _ANIMATED_FORMATS:
                try:
                    im.seek(1)
                    animated = True
                except EOFError:
                    animated = False
                if animated:
                    im.seek(0)
                    _anim_scan(im, p)
    except Image.DecompressionBombError:
        p.notes.append("decompression-bomb guard tripped; not decoded")
    except Exception:
        p.notes.append("bytes did not decode as an image")


def _pixel_cap(p: MediaProbe) -> int:
    """Pixels one decoded frame may have: what the container declared (plus
    codec padding), so a stream that declares 64x64 and switches to 4096x2160
    mid-file is refused by the decoder instead of decoded."""
    declared = (p.width or 0) * (p.height or 0)
    return min(MAX_VIDEO_PIXELS, int(declared * 1.25) + 65536) if declared else MAX_VIDEO_PIXELS


def _input_args(fmt: str, *, max_pixels: int = MAX_VIDEO_PIXELS) -> list[str]:
    """Demuxer pinned to what ``sniff_format`` established, protocols narrowed
    to plain files, nested demuxers allow-listed, and (for decoding) only
    allow-listed decoders of bounded frame size. Never auto-detect. ffprobe
    gets exactly the same flags as ffmpeg: it decodes a few frames too."""
    demux = _DEMUX[fmt]
    args = ["-f", demux, "-format_whitelist", demux, "-protocol_whitelist", "file"]
    if demux == "mov":
        args += ["-enable_drefs", "0"]   # external data references stay off
    args += ["-codec_whitelist", _DECODE_ALLOW, "-max_pixels", str(int(max_pixels))]
    return args


def _probe_av(src: _Src, p: MediaProbe) -> None:
    if src.size > MAX_VIDEO_BYTES:
        p.notes.append(f"over the {MAX_VIDEO_BYTES:,}-byte media limit")
        p.limit = "media file is over the size limit"
        return
    if p.format not in _DEMUX:
        return
    try:
        ffprobe = _which("ffprobe")
        with src.sandbox() as wd:
            cp = _run([ffprobe, "-v", "error", *_input_args(p.format),
                       "-print_format", "json", "-show_format", "-show_streams",
                       "-i", _IN_NAME], PROBE_TIMEOUT_S, cwd=wd, src=src)
    except MediaUnavailable as exc:
        p.unavailable = exc
        p.notes.append(f"{exc.reason}; container facts only")
        return
    if cp.returncode != 0:
        p.failed = f"ffprobe could not read this file as {p.format}"
        p.notes.append(p.failed + "; stream facts unknown")
        return
    try:
        doc = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
        if not isinstance(doc, dict):
            raise ValueError
    except ValueError:
        p.failed = "ffprobe output was not understood"
        p.notes.append(p.failed + "; stream facts unknown")
        return
    fmt = doc.get("format") if isinstance(doc.get("format"), dict) else {}
    names = str(fmt.get("format_name", "")).split(",")
    if _DEMUX[p.format] not in names:
        p.failed = (f"ffprobe reports a different container than the {p.format} "
                    "this file claims to be; refusing to decode it")
        p.notes.append(p.failed)
        return
    p.engine = "ffprobe"
    p.streams_known = True
    p.container = _ident(names[0])
    try:
        p.duration_s = round(float(fmt["duration"]), 3)
        if not math.isfinite(p.duration_s) or p.duration_s < 0:
            p.duration_s = None
    except (KeyError, TypeError, ValueError):
        pass
    sub_index = 0
    have_video = False
    streams = doc.get("streams") if isinstance(doc.get("streams"), list) else []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        ctype = stream.get("codec_type")
        codec = _ident(stream.get("codec_name"))
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        if ctype == "video":
            if (stream.get("disposition") or {}).get("attached_pic"):
                continue  # cover art, not a timeline
            if have_video:
                continue
            have_video = True
            w, h = _dim(stream.get("width")), _dim(stream.get("height"))
            rotate = 0
            for side in stream.get("side_data_list") or []:
                if isinstance(side, dict) and "rotation" in side:
                    try:
                        rotate = int(float(side["rotation"]))
                    except (TypeError, ValueError):
                        rotate = 0
            if not rotate:
                try:
                    rotate = int(tags.get("rotate", 0))
                except (TypeError, ValueError):
                    rotate = 0
            if rotate % 180 and w and h:
                w, h = h, w  # ffmpeg auto-rotates; report the displayed size
            p.width, p.height = w, h
            p.video_codec = codec
            p.fps = _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
            p.has_alpha = bool(_ALPHA_PIX.match(str(stream.get("pix_fmt", ""))))
            try:
                p.frames = int(stream["nb_frames"])
            except (KeyError, TypeError, ValueError):
                pass
            if p.duration_s is None:
                try:
                    p.duration_s = round(float(stream["duration"]), 3)
                except (KeyError, TypeError, ValueError):
                    pass
        elif ctype == "audio":
            rate = str(stream.get("sample_rate", ""))
            p.audio_streams.append({
                "codec": codec, "channels": _dim(stream.get("channels")),
                "sample_rate": int(rate) if rate.isdigit() and int(rate) < 10**7 else None,
                "lang": _lang(tags.get("language"))})
            if p.audio_codec is None:
                p.audio_codec = codec
        elif ctype == "subtitle":
            p.subtitle_streams.append({
                "index": sub_index, "codec": codec,
                "lang": _lang(tags.get("language")),
                "text": codec not in _BITMAP_SUBTITLES})
            sub_index += 1
    if not have_video and not p.audio_streams:
        p.failed = "ffprobe found no audio or video stream in this file"
        p.notes.append(p.failed)
        return
    p.kind = "video" if have_video else "audio"
    if have_video:
        if p.video_codec not in _VIDEO_DECODERS:
            p.refused = (f"video codec {p.video_codec} is not on the decode allow-list; "
                         "not decoded")
        elif not (p.width and p.height):
            p.refused = "frame size is not declared; not decoded"
        elif (p.width > MAX_VIDEO_EDGE or p.height > MAX_VIDEO_EDGE
              or p.width * p.height > MAX_VIDEO_PIXELS):
            p.limit = (f"declared frame size {p.width}x{p.height} exceeds the "
                       f"{MAX_VIDEO_PIXELS:,}-pixel / {MAX_VIDEO_EDGE}-edge limit")
        elif max(p.width, p.height) > MAX_ASPECT * min(p.width, p.height):
            p.limit = (f"frame size {p.width}x{p.height} is past the "
                       f"{MAX_ASPECT}:1 aspect-ratio limit")
        for note in (p.refused, p.limit):
            if note:
                p.notes.append(note)


def raw_tokens(p: MediaProbe) -> tuple[int, str]:
    """What this media would cost if handed to a model whole, and the basis.

    Images use the project's image-token formula (``snapcompact``). Video has
    no single price; the estimate is one frame per second at the video's own
    resolution, labelled ``video@1fps``. Anything else falls back to the
    house bytes/4 estimate.
    """
    from ctx.snapcompact import _image_tokens

    if p.kind in ("image", "video") and p.width and p.height:
        per = _image_tokens(p.width, p.height, high_resolution=False)
        if p.kind == "video":
            if p.duration_s:
                return per * _floor1(math.ceil(p.duration_s)), "video@1fps"
        elif not p.animated:
            return per, "image-patch"
        elif p.frames:
            return per * p.frames, ("image-patch x frames (at least)" if p.scan_truncated
                                    else "image-patch x frames")
    return estimate_tokens(p.bytes), "bytes/4"


def _tokens(width: int | None, height: int | None, nbytes: int) -> int:
    from ctx.snapcompact import _image_tokens

    if width and height:
        return _image_tokens(width, height, high_resolution=False)
    return estimate_tokens(nbytes)


# -------------------------------------------------------------- image core
def _open_image(src: _Src):
    """Decode the first frame, apply EXIF orientation, strip metadata.

    Returns ``(image, info)`` where info holds the pre-transform facts.
    Limits are enforced from the header before any pixel is decoded.
    """
    _pillow()
    from PIL import Image, ImageOps

    if binfmt.sniff_format(src.head()) == "svg":
        raise MediaUnavailable(
            "SVG is vector markup and ctx ships no rasterizer; read it as "
            "text with `ctx get`", needs="renderer")
    data = src.read(MAX_IMAGE_BYTES)
    try:
        im = Image.open(io.BytesIO(data))
        width, height = im.size
        if width * height > MAX_IMAGE_PIXELS:
            raise MediaLimitError(
                f"image is {width}x{height} ({width * height:,} pixels); the "
                f"limit is {MAX_IMAGE_PIXELS:,}")
        frames = 1
        if im.format in _ANIMATED_FORMATS:     # never n_frames: it decodes them all
            try:
                im.seek(1)
                frames = 2
            except EOFError:
                pass
        orientation = 1
        try:
            orientation = int(im.getexif().get(0x0112, 1) or 1)
        except Exception:
            pass
        original_format = (im.format or "").lower()
        im.seek(0)
        im.load()
        im = _normalize_mode(im)
        if orientation != 1:
            im = ImageOps.exif_transpose(im)
        im.info = {}
    except Image.DecompressionBombError:
        raise MediaLimitError("decompression-bomb guard: image has far more "
                              "pixels than its size suggests") from None
    except MediaError:
        raise
    except MediaUnavailable:
        raise
    except Exception as exc:
        raise MediaError(f"bytes did not decode as an image ({type(exc).__name__})") from None
    return im, {"frames": frames, "orientation": orientation,
                "format": original_format, "size": (width, height)}


def _normalize_mode(im):
    mode = im.mode
    if mode in ("RGBA", "RGB", "L"):
        return im
    if mode in ("LA", "PA") or (mode == "P" and "transparency" in im.info):
        return im.convert("RGBA")
    if mode in ("I;16", "I;16L", "I;16B", "I", "F"):
        return im.point(lambda v: v * (1 / 256)).convert("L") if mode.startswith("I") \
            else im.convert("L")
    return im.convert("RGB")


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True, compress_level=9)
    return buf.getvalue()


def _jpeg(im, quality: int) -> bytes:
    buf = io.BytesIO()
    (im if im.mode in ("RGB", "L") else im.convert("RGB")).save(
        buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _screenshot_like(im) -> bool:
    """Few distinct colours in a coarse nearest-neighbour sample: flat UI,
    text and diagrams. Photographs and gradients exceed the threshold."""
    from PIL import Image

    sample = im.convert("RGB")
    sample.thumbnail((256, 256), Image.Resampling.NEAREST)
    return sample.getcolors(maxcolors=SCREENSHOT_MAX_COLORS + 1) is not None


def _encode_best(im, *, input_len: int, original_ok: bool,
                 lossy_ok: bool = True) -> tuple[bytes, str] | None:
    """Pick an encoding for the content. ``None`` means "keep the original
    bytes": no candidate was smaller than the input and the original is a
    format a model can take directly."""
    alpha = False
    if im.mode == "RGBA":
        if im.getchannel("A").getextrema()[0] < 255:
            alpha = True
        else:
            im = im.convert("RGB")
    if alpha or _screenshot_like(im):
        out, fmt = _png(im), "png"
        if len(out) >= input_len and not alpha and lossy_ok:
            # A downscaled screenshot gains antialiasing colours and can
            # outgrow its source as PNG; JPEG is the honest smaller choice.
            for quality in JPEG_QUALITIES:
                lossy = _jpeg(im, quality)
                if len(lossy) < len(out):
                    out, fmt = lossy, "jpeg"
                if len(out) < input_len:
                    break
    else:
        out, fmt = b"", "jpeg"
        for quality in JPEG_QUALITIES:
            out = _jpeg(im, quality)
            if len(out) < input_len:
                break
    if len(out) >= input_len and original_ok:
        return None
    return out, fmt


_MIME = {"png": ("image/png", "png"), "jpeg": ("image/jpeg", "jpg"),
         "gif": ("image/gif", "gif"), "webp": ("image/webp", "webp")}
_ORIGINAL_OK = frozenset({"png", "jpeg", "gif", "webp"})


def _engine_pillow() -> tuple[str, str]:
    return "pillow", pillow_version() or "unknown"


def _make_derived(*, src: _Src, src_dims: tuple[int | None, int | None],
                  op: str, params: dict[str, Any], data: bytes, fmt: str,
                  size: tuple[int, int] | None, engine: tuple[str, str],
                  omitted: list[str], meta: dict[str, Any],
                  tokens_before: int | None = None,
                  tokens_basis: str = "image-patch") -> Derived:
    mime, ext = _MIME.get(fmt, ("application/octet-stream", fmt))
    if fmt == "text":
        mime, ext = "text/plain", "txt"
    w, h = size if size else (None, None)
    if tokens_before is None:
        tokens_before = _tokens(src_dims[0], src_dims[1], src.size)
    rec = Derivation(
        source_sha256=src.sha256(), op=op, params=params, engine=engine[0],
        engine_version=engine[1], output_sha256=hashlib.sha256(data).hexdigest(),
        bytes_before=src.size, bytes_after=len(data),
        tokens_before=tokens_before, tokens_after=_tokens(w, h, len(data)),
        tokens_basis=tokens_basis)
    return Derived(data=data, mime=mime, ext=ext, op=op, record=rec, width=w,
                   height=h, omitted=tuple(omitted), meta=meta)


def _finish_image(src: _Src, im, info: dict, *, op: str, params: dict[str, Any],
                  resized: bool, omitted: list[str], meta: dict[str, Any]) -> Derived:
    """Encode ``im`` and record it. May fall back to the original bytes."""
    animated = info["frames"] > 1
    original_ok = (not animated and info["orientation"] == 1 and op == "fit"
                   and info["format"] in _ORIGINAL_OK)
    picked = _encode_best(im, input_len=src.size, original_ok=original_ok,
                          lossy_ok=resized or not original_ok)
    ow, oh = info["size"]
    if info["orientation"] in (5, 6, 7, 8):
        ow, oh = oh, ow
    if picked is None:
        data = src.read(MAX_IMAGE_BYTES)
        fmt = info["format"]
        size = info["size"]
        omitted = [o for o in omitted if not o.startswith("pixels")]
        omitted.append("kept the original bytes: re-encoding was not smaller "
                       "(metadata not stripped)")
        meta = {**meta, "kept_original": True}
    else:
        data, fmt = picked
        size = im.size
        meta = {**meta, "encoding": fmt}
        if len(data) > src.size:
            why = ("exif rotation had to be baked in" if info["orientation"] != 1
                   else "the grid overlay adds detail" if op == "overview"
                   else "the input was already more compact")
            omitted.append(f"output is larger than the input ({why})")
    if animated:
        omitted.append("animation: first frame only (`--sheet` shows the timeline)")
    if info["orientation"] != 1:
        meta = {**meta, "exif_orientation_applied": info["orientation"]}
    return _make_derived(
        src=src, src_dims=(ow, oh), op=op, params=params, data=data, fmt=fmt,
        size=size, engine=_engine_pillow(), omitted=omitted,
        meta={**meta, "source_size": [ow, oh], "resized": resized})


def fit(source: Any, max_edge: int = DEFAULT_FIT_EDGE) -> Derived:
    """Downsize to at most ``max_edge`` on the long side; never upscale.

    Applies EXIF orientation, strips metadata and picks an encoding for the
    content (PNG for screenshot-like or alpha images, JPEG for photographs).
    When no re-encoding would be smaller than the input and the input needs
    no rotation or resize, the original bytes are returned unchanged and the
    omission says so. One declared exception to "never larger than the
    input": an EXIF rotation that must be baked into a tiny, already-optimal
    file.
    """
    edge = _edge(max_edge, DEFAULT_FIT_EDGE)
    src = _as_src(source)
    im, info = _open_image(src)
    w, h = im.size
    resized = max(w, h) > edge
    omitted: list[str] = ["metadata (EXIF/ICC) stripped"]
    if resized:
        from PIL import Image

        scale = edge / max(w, h)
        new = (_floor1(round(w * scale)), _floor1(round(h * scale)))
        im = im.resize(new, Image.Resampling.LANCZOS)
        omitted.insert(0, f"pixels: {w}x{h} reduced to {new[0]}x{new[1]} "
                          "(zoom with --cell/--region for original detail)")
    return _finish_image(src, im, info, op="fit", params={"max_edge": edge},
                         resized=resized, omitted=omitted, meta={})


def crop(source: Any, region: str | Region, *, grid: tuple[int, int] | None = None,
         max_edge: int = DEFAULT_CROP_EDGE) -> Derived:
    """Crop from the ORIGINAL pixels (after EXIF orientation).

    ``region`` uses the grammar of :func:`parse_region`; cells resolve against
    ``grid`` or :func:`default_grid`. The crop is downsized only if its long
    side exceeds ``max_edge``.
    """
    edge = _edge(max_edge, DEFAULT_CROP_EDGE)
    src = _as_src(source)
    im, info = _open_image(src)
    w, h = im.size
    if grid is None and isinstance(region, str) and not ("," in region):
        grid = default_grid(w, h)
    reg = region if isinstance(region, Region) else parse_region(region, (w, h), grid)
    cropped = im.crop(reg.box)
    omitted = [f"pixels outside {reg.label or 'the region'}"]
    resized = max(cropped.size) > edge
    if resized:
        from PIL import Image

        scale = edge / max(cropped.size)
        cw, ch = cropped.size
        new = (_floor1(round(cw * scale)), _floor1(round(ch * scale)))
        cropped = cropped.resize(new, Image.Resampling.LANCZOS)
        omitted.append(f"pixels: crop {cw}x{ch} reduced to {new[0]}x{new[1]}")
    if reg.clipped:
        omitted.append("region overran the image and was clipped to it")
    meta: dict[str, Any] = {"region": reg.to_dict()}
    if grid is not None and reg.label and "," not in reg.label:
        meta["grid"] = f"{grid[0]}x{grid[1]}"
    params = {"region": reg.label or f"{reg.x},{reg.y},{reg.w},{reg.h}",
              "box": [reg.x, reg.y, reg.w, reg.h], "max_edge": edge}
    return _finish_image(src, cropped, info, op="crop", params=params,
                         resized=resized, omitted=omitted, meta=meta)


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has only the fixed bitmap font
        return ImageFont.load_default()


def _label(draw, xy: tuple[int, int], text: str, font) -> None:
    x, y = xy
    left, top, right, bottom = draw.textbbox((x, y), text, font=font)
    draw.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=(0, 0, 0, 190))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))


def overview(source: Any, *, max_edge: int = DEFAULT_FIT_EDGE,
             grid: tuple[int, int] | None = None) -> Derived:
    """A :func:`fit` with labelled grid lines so a model can name a cell.

    The grid is laid over the image proportionally, so cell ``B2`` of the
    overview and ``crop(region="B2", grid=...)`` of the original cover the
    same area.
    """
    edge = _edge(max_edge, DEFAULT_FIT_EDGE)
    src = _as_src(source)
    im, info = _open_image(src)
    w, h = im.size
    explicit = grid is not None
    rows, cols = grid if explicit else default_grid(w, h)
    resized = max(w, h) > edge
    omitted = ["metadata (EXIF/ICC) stripped"]
    if resized:
        from PIL import Image

        scale = edge / max(w, h)
        new = (_floor1(round(w * scale)), _floor1(round(h * scale)))
        im = im.resize(new, Image.Resampling.LANCZOS)
        omitted.insert(0, f"pixels: {w}x{h} reduced to {new[0]}x{new[1]} "
                          "(zoom with --cell/--region for original detail)")
    ow, oh = im.size
    if not explicit:
        # Coarsen the default until its cells are big enough to label; a
        # picture too small for any grid is returned without one, declared.
        while (rows > 1 or cols > 1) and (ow // cols < MIN_GRID_CELL_PX
                                          or oh // rows < MIN_GRID_CELL_PX):
            if rows >= cols:
                rows -= 1
            else:
                cols -= 1
        if ow // cols < MIN_GRID_CELL_PX or oh // rows < MIN_GRID_CELL_PX:
            omitted.append(f"grid: none; a {ow}x{oh} overview is too small to label "
                           "(`--cell`/`--region` still work on the original)")
            return _finish_image(src, im, info, op="overview",
                                 params={"max_edge": edge, "grid": None},
                                 resized=resized, omitted=omitted,
                                 meta={"grid": None, "overview_size": [ow, oh]})
    if ow // cols < MIN_GRID_CELL_PX or oh // rows < MIN_GRID_CELL_PX:
        raise RegionError(
            f"a {rows}x{cols} grid is too dense for a {ow}x{oh} overview "
            f"(cells under {MIN_GRID_CELL_PX}px cannot be labelled); use a "
            "coarser grid or a larger --fit")
    from PIL import Image, ImageDraw

    base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line_w = 2 if max(ow, oh) > 1200 else 1
    for c in range(1, cols):
        x = c * ow // cols
        draw.line([(x, 0), (x, oh)], fill=(255, 0, 255, 170), width=line_w)
    for r in range(1, rows):
        y = r * oh // rows
        draw.line([(0, y), (ow, y)], fill=(255, 0, 255, 170), width=line_w)
    font = _font(max(10, min(18, min(ow // cols, oh // rows) // 5)))
    for r in range(rows):
        for c in range(cols):
            x0, y0, _, _ = _cell_box(r, c, rows, cols, ow, oh)
            _label(draw, (x0 + 3, y0 + 2), cell_label(r, c), font)
    gridded = Image.alpha_composite(base, layer)
    if im.mode == "RGBA" and im.getchannel("A").getextrema()[0] < 255:
        out_im = gridded
    else:
        out_im = gridded.convert("RGB")
    omitted.append("grid lines and cell labels are drawn on the overview only")
    meta = {"grid": f"{rows}x{cols}", "rows": rows, "cols": cols,
            "columns": f"A-{chr(ord('A') + cols - 1)}", "overview_size": [ow, oh]}
    return _finish_image(src, out_im, info, op="overview",
                         params={"max_edge": edge, "grid": f"{rows}x{cols}"},
                         resized=resized, omitted=omitted, meta=meta)


# -------------------------------------------------------------- video core
def _require_timeline(src: _Src, p: MediaProbe) -> None:
    if not p.is_timeline:
        raise MediaUnsupported(f"{p.kind} is not a video; time-based views "
                               "need a video or animated image")
    if p.kind == "video":
        if p.unavailable is not None:
            raise p.unavailable
        if p.failed:
            raise MediaError(p.failed)
        if p.limit:
            raise MediaLimitError(p.limit)
        if p.refused:
            raise MediaUnsupported(p.refused)
        if p.format not in _DEMUX:
            raise MediaUnsupported(f"container {p.format!r} is not handled")
        if not p.streams_known:
            raise MediaError("stream facts are unknown, so the video is not decoded")
        if src.size > MAX_VIDEO_BYTES:
            raise MediaLimitError(f"video is {src.size:,} bytes; the limit is "
                                  f"{MAX_VIDEO_BYTES:,}")


def _engine_video(with_pillow: bool) -> tuple[str, str]:
    ff = ffmpeg_version() or "ffmpeg unknown"
    if with_pillow:
        return "ffmpeg+pillow", f"{ff}; pillow {pillow_version()}"
    return "ffmpeg", ff


def _scale_filter(edge: int) -> str:
    return (f"scale=w='min(iw,{edge})':h='min(ih,{edge})':"
            "force_original_aspect_ratio=decrease:flags=lanczos")


def _ff_frame(src: "_Src", wd: str, fmt: str, t: float, edge: int, *, codec: str,
              timeout: float, px: int = MAX_VIDEO_PIXELS) -> bytes:
    ffmpeg = _which("ffmpeg")
    argv = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
            *_input_args(fmt, max_pixels=px), "-ss", f"{t:.3f}", "-i", _IN_NAME,
            "-frames:v", "1", "-an", "-sn", "-vf", _scale_filter(edge),
            "-f", "image2pipe", "-c:v", codec]
    if codec == "mjpeg":
        argv += ["-q:v", "5", "-pix_fmt", "yuvj420p"]
    argv.append("-")
    # The container's duration can run past its last video frame (audio is
    # often longer), so a seek near the end may find nothing; step back a
    # little rather than fail on the final moments of a clip.
    cp = None
    for back in (0.0, 0.2, 0.6, 1.5):
        at = max(0.0, t - back)
        argv[argv.index("-ss") + 1] = f"{at:.3f}"
        cp = _run(argv, timeout, cwd=wd, src=src)
        if cp.returncode == 0 and cp.stdout:
            return cp.stdout
        if cp.returncode != 0 or at == 0.0:
            break              # a decoder error is a failure; only "nothing there" steps back
    raise MediaError(f"no frame at {fmt_ts(t)}: " + _failure(cp))


def _decode_frame(raw: bytes):
    """ffmpeg's PNG bytes -> a normalised PIL image, or a plain MediaError."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Image.DecompressionBombError:
        raise MediaLimitError("a decoded frame tripped the decompression-bomb guard") from None
    except Exception:
        raise MediaError("ffmpeg produced bytes that are not a readable image") from None
    return _normalize_mode(im)


def _anim_frames(src: _Src, p: MediaProbe, times: list[float], edge: int):
    """Frames of an animated image nearest each time, each shrunk to ``edge``
    as it is read, in ONE ascending pass bounded by the wall clock."""
    _pillow()
    import bisect

    from PIL import Image

    if not p.anim_starts:
        raise MediaError("animation frames were not scanned")
    deadline = _Deadline(ANIM_BUDGET_S, "animated-image frame pass")
    wanted = [max(0, bisect.bisect_right(p.anim_starts, t) - 1) for t in times]
    got: dict[int, Any] = {}
    try:
        with Image.open(io.BytesIO(src.read(MAX_IMAGE_BYTES))) as im:
            for idx in sorted(set(wanted)):
                deadline.check()
                im.seek(idx)
                tile = im.convert("RGBA")
                tile.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                got[idx] = tile
    except Image.DecompressionBombError:
        raise MediaLimitError("decompression-bomb guard on animated image") from None
    except EOFError:
        raise MediaError("animation ended before the requested frame") from None
    return [got[i] for i in wanted]


def _clamp_time(t: float, duration: float | None) -> tuple[float, bool]:
    """(time inside the clip, whether it had to be moved)."""
    if duration is not None and duration > 0:
        top = max(0.0, duration - 0.05)
        if t > top:
            return top, True
    return max(0.0, t), False


def _probed(source: Any, probe_result: MediaProbe | None) -> tuple[_Src, MediaProbe]:
    src = _as_src(source)
    p = probe_result if probe_result is not None else probe(src)
    return src, p


def frame_at(source: Any, t: float | str, *, max_edge: int = DEFAULT_FRAME_EDGE,
             probe_result: MediaProbe | None = None) -> Derived:
    """One frame at time ``t`` (seconds or a time expression), at most
    ``max_edge`` on the long side, never upscaled. A time past the end is moved
    to the last moment and the result says so (``meta["clamped"]``)."""
    edge = _edge(max_edge, DEFAULT_FRAME_EDGE)
    requested = parse_time(t)
    src, p = _probed(source, probe_result)
    _require_timeline(src, p)
    when, clamped = _clamp_time(requested, p.duration_s)
    before, basis = raw_tokens(p)
    omitted = [f"every other moment of the {fmt_ts(p.duration_s or 0)} timeline "
               "(`--range A-B` for a stretch)"]
    if clamped:
        omitted.append(f"time {fmt_ts(requested)} is past the end of the "
                       f"{fmt_ts(p.duration_s or 0)} clip; the last moment is shown")
    if p.kind == "image":
        im = _anim_frames(src, p, [when], edge)[0]
        picked = _encode_best(im, input_len=1 << 60, original_ok=False)
        data, fmt = picked  # type: ignore[misc]
        size = im.size
        engine = _engine_pillow()
    else:
        with_pillow = pillow_version() is not None
        with src.sandbox() as wd:
            raw = _ff_frame(src, wd, p.format, when, edge,
                            codec="png" if with_pillow else "mjpeg",
                            timeout=FRAME_TIMEOUT_S, px=_pixel_cap(p))
        if with_pillow:
            im = _decode_frame(raw)
            picked = _encode_best(im, input_len=1 << 60, original_ok=False)
            data, fmt = picked  # type: ignore[misc]
            size = im.size
        else:
            data, fmt = raw, "jpeg"
            size = _jpeg_size(raw)
        engine = _engine_video(with_pillow)
    return _make_derived(
        src=src, src_dims=(p.width, p.height), op="frame",
        params={"t": round(when, 3), "max_edge": edge}, data=data, fmt=fmt,
        size=size, engine=engine, omitted=omitted,
        meta={"t": round(when, 3), "requested_t": round(requested, 3),
              "clamped": clamped, "label": fmt_ts(when),
              "duration_s": p.duration_s},
        tokens_before=before, tokens_basis=basis)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    w, h, _ = binfmt._jpeg_dims(data)
    return (w, h) if w and h else None


def sample_times(start: float, end: float, *, every: float | None = None,
                 count: int | None = None, cap: int = MAX_RANGE_FRAMES
                 ) -> tuple[list[float], int]:
    """Timestamps over ``[start, end)`` and how many were dropped by ``cap``.

    ``count`` N samples the midpoint of N equal segments; ``every`` S samples
    start, start+S, … below ``end``. With neither, a count is chosen from the
    span. The result never exceeds ``cap`` (the dropped number is declared).
    """
    if not end > start:
        raise RegionError("time range must end after it starts")
    cap = _floor1(int(cap))
    if every is not None:
        step = float(every)
        if not math.isfinite(step) or step <= 0:
            raise RegionError("--every must be a positive number of seconds")
        want = _floor1(math.ceil((end - start) / step - 1e-9))
        times = [start + i * step for i in range(min(want, cap))]
        return times, max(0, want - cap)
    n = bounds.count(count) if count is not None else min(cap, DEFAULT_SHEET_FRAMES)
    if n == 0:
        raise RegionError("--count must be at least 1")
    want = n
    n = min(n, cap)
    seg = (end - start) / n
    return [start + (i + 0.5) * seg for i in range(n)], max(0, want - cap)


def frames(source: Any, start: float | str, end: float | str, *,
           every: float | None = None, count: int | None = None,
           max_edge: int = DEFAULT_FRAME_EDGE) -> tuple[list[Derived], int]:
    """Individual frames across a time range. Returns ``(frames, omitted)``,
    where ``omitted`` counts requested frames dropped by MAX_RANGE_FRAMES."""
    a, b = parse_time(start), parse_time(end)
    src, p = _probed(source, None)
    _require_timeline(src, p)
    times, dropped = sample_times(a, b, every=every, count=count,
                                  cap=MAX_RANGE_FRAMES)
    return [frame_at(source, t, max_edge=max_edge, probe_result=p) for t in times], dropped


def _grid_for_sheet(n: int, w: int, h: int) -> tuple[int, int]:
    """(rows, cols) for n tiles of aspect w:h, roughly square overall."""
    ratio = w / _floor1(h)
    cols = _floor1(min(n, round(math.sqrt(n * ratio))))
    return math.ceil(n / cols), cols


def _sheet_geometry(src_w: int, src_h: int, rows: int, cols: int, max_width: int,
                    gap: int) -> tuple[int, int, int, int]:
    """(tile_w, tile_h, sheet_w, sheet_h), every number checked BEFORE any
    allocation: header-declared sizes are untrusted."""
    if max(src_w, src_h) > MAX_ASPECT * min(src_w, src_h):
        raise MediaLimitError(f"frame size {src_w}x{src_h} is past the "
                              f"{MAX_ASPECT}:1 aspect-ratio limit")
    tile_w = max(48, min(src_w, (max_width - gap * (cols + 1)) // cols))
    tile_h = _floor1(round(tile_w * src_h / src_w))
    if tile_h > MAX_TILE_EDGE:
        tile_h = MAX_TILE_EDGE
        tile_w = _floor1(round(tile_h * src_w / src_h))
    sheet_w = cols * tile_w + gap * (cols + 1)
    sheet_h = rows * tile_h + gap * (rows + 1)
    if (max(sheet_w, sheet_h) > MAX_OUTPUT_EDGE
            or sheet_w * sheet_h > MAX_SHEET_PIXELS):
        raise MediaLimitError(
            f"a {sheet_w}x{sheet_h} contact sheet exceeds the "
            f"{MAX_SHEET_PIXELS:,}-pixel / {MAX_OUTPUT_EDGE}-edge limit")
    return tile_w, tile_h, sheet_w, sheet_h


def contact_sheet(source: Any, *, count: int | None = None,
                  start: float | str | None = None, end: float | str | None = None,
                  every: float | None = None, max_width: int = SHEET_MAX_WIDTH,
                  probe_result: MediaProbe | None = None) -> Derived:
    """N frames tiled into one image with timestamps drawn on each tile.

    Frames are the midpoints of equal segments of ``[start, end)`` (default:
    the whole video), or one every ``every`` seconds. ``meta`` and the omission
    list describe what was ACTUALLY sampled, including a range cut short by
    the frame cap or clamped to the clip. Needs ffmpeg for video and Pillow to
    tile and label; animated images need only Pillow.
    """
    src, p = _probed(source, probe_result)
    _require_timeline(src, p)
    pil = _pillow()
    from PIL import Image, ImageDraw

    if not p.duration_s or p.duration_s <= 0:
        raise MediaError("duration is unknown, so frames cannot be spaced; "
                         "use --at with an explicit time")
    req_a = parse_time(start) if start is not None else 0.0
    req_b = parse_time(end) if end is not None else p.duration_s
    a = min(req_a, p.duration_s)
    b = min(req_b, p.duration_s)
    if not b > a:
        raise RegionError(f"range is outside the {fmt_ts(p.duration_s)} timeline")
    times, dropped = sample_times(a, b, every=every, count=count, cap=MAX_SHEET_FRAMES)
    n = len(times)
    src_w, src_h = p.width or 640, p.height or 360
    rows, cols = _grid_for_sheet(n, src_w, src_h)
    gap = 2
    max_width = _edge(max_width, SHEET_MAX_WIDTH)
    tile_w, tile_h, sheet_w, sheet_h = _sheet_geometry(src_w, src_h, rows, cols,
                                                       max_width, gap)
    sheet = Image.new("RGB", (sheet_w, sheet_h), (24, 24, 24))
    font = _font(max(10, min(16, tile_w // 12)))
    labels: list[str] = []
    deadline = _Deadline(SHEET_BUDGET_S, "contact sheet")
    anim = p.kind == "image"
    anim_imgs = _anim_frames(src, p, times, max(tile_w, tile_h)) if anim else []
    ctx = contextlib.nullcontext(None) if anim else src.sandbox()
    with ctx as wd:
        for i, t in enumerate(times):
            deadline.check()
            if anim:
                tile = anim_imgs[i].convert("RGB")
            else:
                raw = _ff_frame(src, wd, p.format, _clamp_time(t, p.duration_s)[0],
                                max(tile_w, tile_h), codec="png",
                                timeout=min(FRAME_TIMEOUT_S, deadline.left()),
                                px=_pixel_cap(p))
                tile = _decode_frame(raw).convert("RGB")
            if tile.size != (tile_w, tile_h):
                tile = tile.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            r, c = divmod(i, cols)
            x = gap + c * (tile_w + gap)
            y = gap + r * (tile_h + gap)
            sheet.paste(tile, (x, y))
            text = f"{i + 1}  {fmt_ts(t)}"
            labels.append(text)
            overlay = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
            d = ImageDraw.Draw(overlay)
            _label(d, (4, tile_h - (font.size if hasattr(font, "size") else 12) - 8),
                   text, font)
            sheet.paste(overlay, (x, y), overlay)
    picked = _encode_best(sheet, input_len=1 << 60, original_ok=False)
    data, fmt = picked  # type: ignore[misc]
    before, basis = raw_tokens(p)
    step = every if every is not None else (b - a) / n
    sampled_end = min(b, a + n * step) if every is not None else b
    omitted = [f"{n} sampled frames, one every ~{step:.3g}s over "
               f"{fmt_ts(a)}-{fmt_ts(sampled_end)}; the rest of the timeline is not shown "
               "(`--at T` for one frame, `--range A-B` to zoom)"]
    if dropped:
        if every is not None:
            omitted.append(f"--every {step:g} asks for {n + dropped} frames; only the "
                           f"first {n} (to {fmt_ts(sampled_end)}) were sampled; the range "
                           f"continues to {fmt_ts(b)}")
        else:
            omitted.append(f"{dropped} requested frames beyond the {MAX_SHEET_FRAMES}-frame cap")
    if req_b > p.duration_s + 1e-6 or req_a > p.duration_s:
        omitted.append(f"range {fmt_ts(req_a)}-{fmt_ts(req_b)} was clamped to the "
                       f"{fmt_ts(p.duration_s)} clip")
    if p.kind == "video" and p.streams_known:
        if p.audio_streams:
            omitted.append(f"audio: {len(p.audio_streams)} stream(s), not analysed")
        if p.subtitle_streams:
            omitted.append(f"subtitles: {len(p.subtitle_streams)} track(s) (`--subs`)")
    engine = _engine_pillow() if anim else _engine_video(True)
    return _make_derived(
        src=src, src_dims=(p.width, p.height), op="sheet",
        params={"count": n, "start": round(a, 3), "end": round(b, 3),
                "every": every, "max_width": max_width},
        data=data, fmt=fmt, size=sheet.size, engine=engine, omitted=omitted,
        meta={"times": [round(t, 3) for t in times], "labels": labels,
              "rows": rows, "cols": cols, "tile": [tile_w, tile_h],
              "range": [round(a, 3), round(sampled_end, 3)],
              "requested_range": [round(req_a, 3), round(req_b, 3)],
              "range_end": round(b, 3), "truncated": bool(dropped),
              "frames": n, "duration_s": p.duration_s,
              "interval_s": round(step, 6), "pillow": pil.__version__},
        tokens_before=before, tokens_basis=basis)


_SRT_START = re.compile(rb"\s*\d+\s*\r?\n\d\d:\d\d:\d\d[,.]\d+ --> ")
_PTS = re.compile(r"pts_time:([0-9.]+)")
_SCORE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def scene_changes(source: Any, *, threshold: float = DEFAULT_SCENE_THRESHOLD,
                  start: float | str | None = None, end: float | str | None = None,
                  limit: int = MAX_SCENES,
                  probe_result: MediaProbe | None = None) -> Derived:
    """Timestamps where the picture changes sharply (ffmpeg's scene score).

    The result is text, one ``seconds score`` pair per line. At most
    ``limit`` changes are kept - the highest-scoring ones, in time order -
    and the number dropped is declared. ffmpeg must report ``progress=end``;
    anything else is a failure, not "no changes".
    """
    src, p = _probed(source, probe_result)
    if p.kind != "video":
        raise MediaUnsupported("scene detection needs a video (not an "
                               f"{p.kind}); animated images use --sheet")
    _require_timeline(src, p)
    thr = min(1.0, max(0.01, float(threshold)))
    a = parse_time(start) if start is not None else 0.0
    b = parse_time(end) if end is not None else None
    ffmpeg = _which("ffmpeg")
    argv = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
            *_input_args(p.format, max_pixels=_pixel_cap(p))]
    if a:
        argv += ["-ss", f"{a:.3f}"]
    argv += ["-i", _IN_NAME]
    if b is not None:
        argv += ["-t", f"{max(0.0, b - a):.3f}"]
    argv += ["-an", "-sn", "-vf",
             f"scale=320:-2,select='gt(scene,{thr})',metadata=print:file=scenes.txt",
             "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
    with src.sandbox() as wd:
        report = Path(wd) / "scenes.txt"
        report.unlink(missing_ok=True)
        cp = _run(argv, SCENE_TIMEOUT_S, cwd=wd, src=src)
        try:
            lines = report.read_text(encoding="utf-8", errors="replace").splitlines() \
                if report.is_file() else None
        except OSError:
            lines = None
    if (cp.returncode != 0 or lines is None
            or "progress=end" not in cp.stdout.decode("utf-8", "replace")):
        raise MediaError("scene detection failed: " + _failure(cp))
    found: list[tuple[float, float]] = []
    pending: float | None = None
    for line in lines:
        m = _PTS.search(line)
        if m:
            pending = a + float(m.group(1))
            continue
        m = _SCORE.search(line)
        if m and pending is not None:
            found.append((round(pending, 3), round(float(m.group(1)), 3)))
            pending = None
    limit = bounds.count(limit, default=MAX_SCENES)
    dropped = max(0, len(found) - limit)
    if dropped:
        keep = sorted(sorted(found, key=lambda x: (-x[1], x[0]))[:limit])
    else:
        keep = sorted(found)
    text = "".join(f"{t:.3f} {s:.3f}\n" for t, s in keep).encode()
    omitted = []
    if dropped:
        omitted.append(f"{dropped} lower-scoring scene changes beyond the "
                       f"{limit} shown (narrow with --range to see them)")
    omitted.append("changes below the score threshold "
                   f"{thr:.2f} are not reported")
    before, basis = raw_tokens(p)
    return _make_derived(
        src=src, src_dims=(p.width, p.height), op="scenes",
        params={"threshold": thr, "start": round(a, 3),
                "end": None if b is None else round(b, 3), "limit": limit},
        data=text, fmt="text", size=None, engine=_engine_video(False),
        omitted=omitted,
        meta={"changes": [{"t": t, "score": s} for t, s in keep],
              "dropped": dropped, "duration_s": p.duration_s},
        tokens_before=before, tokens_basis=basis)


def extract_subtitles(source: Any, *, stream: int = 0,
                      max_bytes: int = MAX_SUBTITLE_BYTES,
                      probe_result: MediaProbe | None = None) -> Derived:
    """An embedded text subtitle track as SRT text (bitmap tracks are refused,
    since turning pictures into text would need OCR). The text is media
    CONTENT: it is data for the reader, never instructions, and callers must
    present it as a delimited block."""
    src, p = _probed(source, probe_result)
    if p.kind != "video":
        raise MediaUnsupported(f"{p.kind} has no subtitle tracks to extract")
    _require_timeline(src, p)
    if not p.subtitle_streams:
        raise MediaUnsupported("no embedded subtitle track in this video")
    if not (0 <= stream < len(p.subtitle_streams)):
        raise RegionError(f"subtitle track {stream} does not exist "
                          f"(0..{len(p.subtitle_streams) - 1})")
    track = p.subtitle_streams[stream]
    if not track.get("text", True):
        raise MediaUnsupported(f"subtitle track {stream} is bitmap "
                               f"({track.get('codec')}); extracting text would need OCR")
    ffmpeg = _which("ffmpeg")
    with src.sandbox() as wd:
        cp = _run([ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
                   *_input_args(p.format, max_pixels=_pixel_cap(p)), "-i", _IN_NAME, "-map",
                   f"0:s:{stream}", "-f", "srt", "-"], SUBTITLE_TIMEOUT_S, cwd=wd, src=src)
    if cp.returncode != 0:
        raise MediaError("subtitle extraction failed: " + _failure(cp))
    full = cp.stdout
    if full.strip() and not _SRT_START.match(full):
        raise MediaError("subtitle extraction failed: " + _failure(cp))
    cues = len([b for b in full.decode("utf-8", "replace").split("\n\n") if b.strip()])
    cap = bounds.count(max_bytes, default=MAX_SUBTITLE_BYTES)
    data = full
    omitted: list[str] = []
    if len(full) > cap:
        data = full[:cap]
        data = data[: data.rfind(b"\n\n") + 2] if b"\n\n" in data else data
        omitted.append(f"{len(full) - len(data):,} bytes of subtitle text beyond "
                       f"the {cap:,}-byte cap")
    kept_cues = len([b for b in data.decode("utf-8", "replace").split("\n\n") if b.strip()])
    d = _make_derived(
        src=src, src_dims=(p.width, p.height), op="subs",
        params={"stream": stream, "max_bytes": cap}, data=data, fmt="text",
        size=None, engine=_engine_video(False), omitted=omitted,
        meta={"cues": cues, "cues_kept": kept_cues,
              "lang": _lang(track.get("lang")), "codec": _ident(track.get("codec"))},
        tokens_before=estimate_tokens(len(full)), tokens_basis="bytes/4")
    return dataclasses.replace(d, ext="srt")


# ------------------------------------------------------------ materialization
def materialize(ws: Any, store: Any, derived: Derived, *,
                ttl_days: int | None = None) -> Materialized:
    """Store a derivative (with a retention lease) and write it where a host's
    file-viewing tool can open it by a workspace-relative path.

    The name derives from the operation and the output hash, so the same
    derivation always lands on the same path; re-running is a no-op that
    touches nothing but the lease. Files live under the git-ignored
    ``.ctx-session-reads/media/`` directory, like the output gate's spill
    files. The raw store path is never exposed.
    """
    from ctx.sessiondir import LEDGER_DIR_NAME, session_reads_path
    from ctx.workspace import WorkspaceError

    digest = derived.record.output_sha256
    blob = store.put_blob(derived.data)
    store.lease(blob, "media-derivative",
                ttl_days if ttl_days is not None
                else getattr(store, "retention_days", 30))
    name = f"{derived.op}-{digest[:12]}.{derived.ext}"
    rel = f"{LEDGER_DIR_NAME}/media/{name}"
    try:
        target = ws.confine(rel)
    except WorkspaceError as exc:
        raise MediaError(f"cannot write the session file: {exc}") from None
    created = False
    try:
        intact = False
        if target.is_file() and target.stat().st_size == len(derived.data):
            intact = hashlib.sha256(target.read_bytes()).hexdigest() == digest
        if not intact:
            session_reads_path(ws.root, "media").mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{name}.{os.getpid()}.tmp")
            tmp.write_bytes(derived.data)
            os.replace(tmp, target)
            created = True
    except OSError as exc:
        raise MediaError(f"cannot write the session file: {exc.strerror or exc}") from None
    return Materialized(rel_path=rel, blob=blob, address=f"blob:{blob[:12]}",
                        created=created)


__all__ = [
    "Derivation", "Derived", "Snapshot", "snapshot_confined", "snapshot_path", "MediaError", "MediaLimitError", "MediaProbe",
    "MediaUnavailable", "MediaUnsupported", "Materialized", "Region",
    "RegionError", "capabilities", "cell_label", "clean_text", "has_control_chars", "contact_sheet", "crop",
    "default_grid", "extract_subtitles", "fit", "fmt_ts", "frame_at", "frames",
    "materialize", "overview", "parse_grid", "parse_range", "parse_region",
    "parse_time", "probe", "raw_tokens", "sample_times", "scene_changes",
]
