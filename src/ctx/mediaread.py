"""Media reads: what the read guard does when the target is an image, video or audio.

``ctx view`` (``ctx.media``) can crop any region of an image from its original
pixels and turn a video into timestamped frames, but an agent reaches for the
host's native file-read tool, which either sends a downscaled copy of a large
image or cannot show the file at all. This module is the decision the read
guard makes at that moment, and the text it puts in front of the model:

* a large still image the host can display is **allowed unchanged** and the
  guard attaches a short note (what the original is, what the host will do
  to it, the exact command for a sharp crop). ``[media] image_read =
  "redirect"`` opts into replacing the read with a gridded overview instead;
* a video or audio file the read tool cannot show is **denied** with the
  exact ``ctx view`` command that produces something viewable;
* an image too small for either to help gets nothing at all.

Hot-path discipline: the decision "could this be media" is made by
``ctx.hook`` from the path's suffix alone, before any system call and before
this module is even imported, so a source-file read pays one set lookup.
This module imports ``os``, ``re``, ``shlex`` and ``stat`` only (all already
loaded by the hook; ``tests/test_media_read.py`` pins both facts). A
media candidate costs one ``open`` without following symlinks and a bounded
header read; advisory mode never imports an imaging library and never spawns
a process. Redirect mode, which does real work inside a hook with a ten
second timeout, runs ``ctx view`` as a bounded subprocess and falls back to
the advisory on any failure or timeout.

What reaches the model is built only from: integers parsed from the header,
format names from a fixed table, numbers computed here, and the
workspace-relative path - and the path only when it is made of a
conservative character set that cannot start a line, close a quote the note
opened, or read as another command. A file whose name does not qualify gets
no media handling (the read proceeds exactly as before).
"""

from __future__ import annotations

import os
import re
import shlex
import stat as _stat

# --------------------------------------------------------------- the classes
#: Still images the Claude API (and so Claude Code's Read) accepts and shows.
DISPLAYABLE = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
#: Still images ``ctx view`` can render to a viewable PNG/JPEG with Pillow,
#: but which the API does not accept as an image.
CONVERTIBLE = frozenset({"bmp", "tif", "tiff", "ico"})
VIDEO = frozenset({"mp4", "m4v", "mov", "mkv", "webm", "avi"})
AUDIO = frozenset({"mp3", "wav", "flac", "ogg", "oga", "opus", "m4a"})

_CLASS_OF = {**{s: "image" for s in DISPLAYABLE}, **{s: "still" for s in CONVERTIBLE},
             **{s: "video" for s in VIDEO}, **{s: "audio" for s in AUDIO}}

#: ``image_read`` values. ``advise`` is the default (see module docstring).
IMAGE_MODES = ("advise", "redirect", "off")
#: ``video_read`` values (video and audio). ``steer`` is the default.
VIDEO_MODES = ("steer", "off")
DEFAULT_IMAGE_MODE = "advise"
DEFAULT_VIDEO_MODE = "steer"
DEFAULT_REDIRECT_BUDGET_MS = 6000
#: Hosts whose read path this module acts on. Everything else is unchanged.
HOSTS = ("claude-code", "codex")

HEAD_BYTES = 65536          # the most any media decision reads from the file
_JPEG_SCAN = HEAD_BYTES
_MAX_EDGE = 65535           # a header claiming more is treated as unknown
_MAX_PIXELS = 400_000_000

# Redirect pre-checks: past these the overview would risk the hook budget, so
# the advisory is given instead (ctx.media refuses a little further out).
REDIRECT_MAX_BYTES = 32 * 1024 * 1024
REDIRECT_MAX_PIXELS = 40_000_000

# THE image-cost estimate for ctx (the hook's notes, `ctx view`, intake): the
# project's formula (ctx.snapcompact._image_tokens), restated here because
# that module's imports are too heavy for the hook; tests pin the two equal
# for both tiers. (long-edge px, token cap) per tier.
_PATCH_PX = 28
TIERS = {"standard": (1568, 1568), "high-res": (2576, 4784)}
#: Which models take which tier. Source: the model table inside the Claude
#: Code 2.1.322 bundle (read 2026-11-11): these ids declare image_limits
#: 2576 px / 4784 tokens; its other Claude ids (3.x, haiku-4-5, sonnet-4-0
#: to 4-6, opus-4-0 to 4-6) declare none, i.e. the standard tier. Matched as
#: substrings of the model name; anything else is "unknown".
_HIGH_RES_MODELS = ("claude-haiku-5", "claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8",
                    "claude-opus-5", "claude-fable-5", "claude-mythos-5")
_STANDARD_MODELS = ("claude-3-", "claude-3.", "claude-haiku-4", "claude-sonnet-4",
                    "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-5", "claude-opus-4-6")
MODEL_ENV = "CTX_MODEL"   # the launcher convention ctx.edit_outcomes resolves first
#: An OBSERVATION, not a constant of nature: Claude Code 2.1.322's Read
#: showed haiku a 3000x2000 PNG as "[Image: original 3000x2000, displayed at
#: 2000x1333 ...]" (live, 2026-11-11). Undocumented and version-bound; an
#: image within it reached the model whole there, so it gets no note.
CLAUDE_READ_EDGE = 2000
CLAUDE_READ_EDGE_OBSERVED = "Claude Code 2.1.322"
# ctx.media.DEFAULT_CROP_EDGE / DEFAULT_FIT_EDGE, pinned by the same test.
CROP_EDGE = 1280
OVERVIEW_EDGE = 1024

_FORMAT_NAME = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WebP",
                "bmp": "BMP", "tiff": "TIFF", "ico": "ICO", "mp4": "MP4", "mov": "QuickTime",
                "mkv": "Matroska", "webm": "WebM", "avi": "AVI", "mp3": "MP3", "wav": "WAV",
                "flac": "FLAC", "ogg": "Ogg", "m4a": "M4A"}


def suffix_class(path: str) -> str | None:
    """``image`` / ``still`` / ``video`` / ``audio`` from the name alone, or None.

    No system call: this is the whole cost of a source-file read."""
    dot = path.rfind(".")
    if dot < 0 or len(path) - dot > 6:
        return None
    return _CLASS_OF.get(path[dot + 1:].lower())


# ------------------------------------------------------------ header facts
def sniff_still(head: bytes) -> str | None:
    """Magic-byte format of a displayable still image, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _u16be(b: bytes, i: int) -> int:
    return int.from_bytes(b[i:i + 2], "big")


def _jpeg_dims(data: bytes) -> tuple[int, int] | None:
    i, n = 2, min(len(data), _JPEG_SCAN)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            return _u16be(data, i + 7), _u16be(data, i + 5)
        if marker in (0xD8, 0xD9, 0xFF) or 0xD0 <= marker <= 0xD7:
            i += 2 if marker != 0xFF else 1
            continue
        seg = _u16be(data, i + 2)
        if seg < 2:
            return None
        i += 2 + seg
    return None


def dims(fmt: str, head: bytes) -> tuple[int, int] | None:
    """Width and height from the header, or None when unknown or implausible.

    Mirrors the header parsers in ``ctx.binfmt`` (pinned by a parity test)."""
    try:
        if fmt == "png" and len(head) >= 24:
            w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        elif fmt == "gif" and len(head) >= 10:
            w, h = int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
        elif fmt == "jpeg":
            wh = _jpeg_dims(head)
            if wh is None:
                return None
            w, h = wh
        elif fmt == "webp" and len(head) >= 30:
            fourcc = head[12:16]
            if fourcc == b"VP8 ":
                w = int.from_bytes(head[26:28], "little") & 0x3FFF
                h = int.from_bytes(head[28:30], "little") & 0x3FFF
            elif fourcc == b"VP8L":
                bits = int.from_bytes(head[21:26], "little")
                w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif fourcc == b"VP8X":
                w = int.from_bytes(head[24:27], "little") + 1
                h = int.from_bytes(head[27:30], "little") + 1
            else:
                return None
        elif fmt == "bmp" and len(head) >= 26:
            w = int.from_bytes(head[18:22], "little", signed=True)
            h = abs(int.from_bytes(head[22:26], "little", signed=True))
        else:
            return None
    except (IndexError, ValueError):
        return None
    if not (0 < w <= _MAX_EDGE and 0 < h <= _MAX_EDGE) or w * h > _MAX_PIXELS:
        return None
    return w, h


def read_head(path: str, n: int = HEAD_BYTES) -> tuple[bytes, int] | None:
    """``(first n bytes, size)`` of a regular file, opened once WITHOUT
    following a symlink at the final component and without blocking on a
    FIFO; None for anything else. Decided from the open descriptor."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            return None
        chunks: list[bytes] = []
        got = 0
        while got < n:
            chunk = os.read(fd, n - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks), st.st_size
    except OSError:
        return None
    finally:
        os.close(fd)


# ------------------------------------------------------------- the numbers
def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def tier_tokens(w: int, h: int, tier: str) -> int:
    """Estimated tokens for a ``w``x``h`` image sent whole, on one tier."""
    import math  # media paths only: never on a source-file read

    edge, cap = TIERS[tier]
    fw, fh = float(w), float(h)
    if max(fw, fh) > edge:
        s = edge / max(fw, fh)
        fw, fh = max(1.0, fw * s), max(1.0, fh * s)
    tok = math.ceil(fw / _PATCH_PX) * math.ceil(fh / _PATCH_PX)
    if tok > cap:
        s = math.sqrt(cap / tok)
        fw, fh = max(1.0, fw * s), max(1.0, fh * s)
        tok = math.ceil(fw / _PATCH_PX) * math.ceil(fh / _PATCH_PX)
    return min(tok, cap)


def image_tier(model: str | None = None) -> tuple[str | None, str]:
    """``(tier, model)``: the tier the named model uses, or None when unknown.
    ``model=None`` reads ``CTX_MODEL``; ``""`` means "do not look" (intake,
    whose digest must not depend on the environment)."""
    if model is None:
        model = os.environ.get(MODEL_ENV, "")
    name = str(model or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/@-]{0,63}", name):
        return None, ""
    for prefix in _HIGH_RES_MODELS:
        if prefix in name:
            return "high-res", name
    for prefix in _STANDARD_MODELS:
        if prefix in name:
            return "standard", name
    return None, name


def image_cost(w: int, h: int, *, model: str | None = None) -> tuple[int, int, str]:
    """THE image-cost estimate: ``(low, high, basis)``.

    One tier when the model is known (low == high, the basis names the tier
    and the model); otherwise both tiers as a labelled range. Every caller
    prints the basis with the figure."""
    tier, name = image_tier(model)
    if tier is not None:
        n = tier_tokens(w, h, tier)
        return n, n, f"{tier} tier, {name}"
    lo, hi = tier_tokens(w, h, "standard"), tier_tokens(w, h, "high-res")
    return lo, hi, ("standard-to-high-res tier range, model unknown"
                    if lo != hi else "same on either tier")


def fmt_cost(lo: int, hi: int) -> str:
    return f"~{lo:,}" if lo == hi else f"~{lo:,}-{hi:,}"


def host_view(w: int, h: int) -> tuple[int, int]:
    """The size Claude Code's Read was OBSERVED to show (see
    :data:`CLAUDE_READ_EDGE`)."""
    if max(w, h) <= CLAUDE_READ_EDGE:
        return w, h
    s = CLAUDE_READ_EDGE / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


def host_downscales(w: int, h: int) -> bool:
    """Would Read show the model fewer pixels than the original has?"""
    return max(w, h) > CLAUDE_READ_EDGE


def default_grid(w: int, h: int) -> tuple[int, int]:
    """(rows, cols) exactly as ``ctx.media.default_grid`` (pinned)."""
    ratio = max(w, h) / max(1, min(w, h))
    long = min(6, max(3, round(3 * ratio)))
    return (3, long) if w >= h else (long, 3)


def cell_cost(w: int, h: int, rows: int, cols: int, *,
              model: str | None = None) -> tuple[int, int, str]:
    """Estimated cost of one grid cell cropped from the original."""
    cw, ch = _ceil_div(w, cols), _ceil_div(h, rows)
    if max(cw, ch) > CROP_EDGE:
        s = CROP_EDGE / max(cw, ch)
        cw, ch = max(1, round(cw * s)), max(1, round(ch * s))
    return image_cost(cw, ch, model=model)


def overview_size(w: int, h: int) -> tuple[int, int]:
    if max(w, h) <= OVERVIEW_EDGE:
        return w, h
    s = OVERVIEW_EDGE / max(w, h)
    return max(1, round(w * s)), max(1, round(h * s))


# ---------------------------------------------------------------- the path
#: Letters, digits, space and ``._/@+,:=-`` only: nothing that can start a
#: line, close a quote, glob, expand or chain. Bounded length.
# Pattern strings, not compiled objects: `re` compiles (and caches) them on
# first use, so a process that never meets a media path never pays for them.
_ADDRESS_LIKE = r"(blob|run|sha256):"


#: Shown in place of a name that cannot be shown safely (see below).
UNSHOWN_TARGET = "PATH"   # a bare word: no shell meaning if pasted as is
_TEMPLATE_CLAUSE = " (In that command, PATH stands for the file you just opened.)"
_NAME_CAP = 200


def command_target(rel: str) -> tuple[str, bool]:
    """``(token, exact)``: ``rel`` as it must be typed after ``ctx view``.

    ANY name is handled. It is quoted for the shell exactly (``shlex.quote``,
    with ``./`` before a leading ``-`` or an address-like prefix), and it only
    ever appears inside the note's backticked command, never in its prose, so
    a name cannot begin a line or speak in the note's voice. A name that could
    still break out of that frame or be mis-copied (a control, format,
    private-use or non-ASCII space character, a line separator, a backtick, a
    ``CTX_`` marker, or more than 200 characters) is not printed at all: the
    command names :data:`UNSHOWN_TARGET` instead and ``exact`` is False."""
    import unicodedata

    bad = (len(rel) > _NAME_CAP or "`" in rel or "ctx_" in rel.lower()
           or any(unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp")
                  or (unicodedata.category(c) == "Zs" and c != " ") for c in rel))
    if bad:
        return UNSHOWN_TARGET, False
    if rel.startswith("-") or re.match(_ADDRESS_LIKE, rel) or rel == UNSHOWN_TARGET:
        rel = "./" + rel  # a real file named PATH must not read as the placeholder
    return shlex.quote(rel), True


def _templated(fn):
    """Every note that prints a command: when the name was withheld, say in
    one clause that PATH is a placeholder, so the command is never mistaken
    for a literal one."""
    import functools

    @functools.wraps(fn)
    def wrapper(target, *args, **kwargs):
        text = fn(target, *args, **kwargs)
        return text + _TEMPLATE_CLAUSE if target == UNSHOWN_TARGET else text
    return wrapper


def ctx_root(start: str) -> str:
    """The root ``ctx`` itself will resolve from ``start`` (SPEC 5.1 rules
    3-6, without git): nearest ancestor with ``ctx.toml``, else with ``.git``,
    else with ``.agents``, else ``start``. A relative path in a note is
    relative to this, so the command is exact wherever the model runs it."""
    start = os.path.abspath(start)
    for marker, isdir in (("ctx.toml", False), (".git", None), (".agents", True)):
        cur = start
        while True:
            p = os.path.join(cur, marker)
            if (os.path.isfile(p) if isdir is False else os.path.isdir(p) if isdir
                    else os.path.exists(p)):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return start


IGNORE_MAX_BYTES = 64 * 1024
IGNORE_MAX_GLOBS = 1000


def policy_excluded(root: str, rel: str) -> bool:
    """Is ``rel`` excluded from capture by ``.ctxignore``? (The built-in
    secret globs are already enforced by the read guard's secret-path rule.)
    ``ctx view`` would refuse such a file, so no note may point at it."""
    path = os.path.join(root, ".ctxignore")
    if not os.path.isfile(path):
        return False
    try:
        # Bounded: this runs inside a hook with a 10 s host timeout. Past the
        # caps the answer is "excluded", the safe branch: no media handling,
        # the read proceeds exactly as it would have without this module.
        with open(path, "rb") as fh:
            data = fh.read(IGNORE_MAX_BYTES + 1)
        if len(data) > IGNORE_MAX_BYTES:
            return True
        globs = [ln.strip() for ln in data.decode("utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        if len(globs) > IGNORE_MAX_GLOBS:
            return True
    except (OSError, UnicodeDecodeError):
        return True  # cannot tell: treat as excluded (no note), never as allowed
    from ctx import pathglob

    for glob in globs:
        if pathglob.matches(rel, glob) or (
                glob.endswith("/**") and pathglob.matches(rel, glob[:-3])):
            return True
    return False


_SETTINGS_MAX = 256 * 1024


def read_rules_present(root: str) -> bool:
    """Does any Claude Code settings file this hook can see hold a ``deny``
    or ``ask`` permission rule for Read? (user, project and local settings;
    rules passed with ``--settings`` or by managed policy are NOT visible
    here.) Unreadable or oversized counts as present: the safe branch."""
    # The user scope lives where the host keeps it: CLAUDE_CONFIG_DIR when set
    # (Claude Code's own override), else ~/.claude. Both are checked when the
    # variable is set, so a stale default file cannot hide a rule either way.
    user_dirs = [os.path.join(os.path.expanduser("~"), ".claude")]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        user_dirs.insert(0, os.path.expanduser(os.environ["CLAUDE_CONFIG_DIR"]))
    paths = [os.path.join(d, "settings.json") for d in user_dirs] + [
             os.path.join(root, ".claude", "settings.json"),
             os.path.join(root, ".claude", "settings.local.json")]
    import json

    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read(_SETTINGS_MAX + 1)
            if len(data) > _SETTINGS_MAX:
                return True
            perms = json.loads(data.decode("utf-8")).get("permissions") or {}
            for key in ("deny", "ask"):
                for rule in perms.get(key) or []:
                    if str(rule).strip().startswith("Read"):
                        return True
        except Exception:
            return True
    return False


# ----------------------------------------------------------- engine checks
def which(name: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d or ".", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def has_pillow() -> bool:
    """Is Pillow importable here? Looked up on ``sys.path``, never imported."""
    import sys

    for d in sys.path:
        if d and os.path.isfile(os.path.join(d, "PIL", "__init__.py")):
            return True
    return False


_PILLOW_HINT = "pip install 'ctx-harness[image]'"


# --------------------------------------------------------------- the notes
def _cols(n: int) -> str:
    return chr(ord("A") + n - 1)


def _example_cell(rows: int, cols: int) -> str:
    return f"{_cols(min(2, cols))}{min(2, rows)}"


def _cell_est(w: int, h: int, rows: int, cols: int) -> str:
    lo, hi, basis = cell_cost(w, h, rows, cols)
    return f"one cell {fmt_cost(lo, hi)} tok est., {basis}"


@_templated
def advise_note(target: str, fmt: str, w: int, h: int, *, host: str,
                pillow: bool, viewer: str = "Read") -> str:
    """The note attached to an image read that proceeds unchanged. The file
    name never appears in prose: only inside the backticked command, quoted
    for the shell (see :func:`command_target`)."""
    rows, cols = default_grid(w, h)
    cell = _example_cell(rows, cols)
    name = _FORMAT_NAME.get(fmt, fmt.upper())
    if host == "claude-code":
        hw, hh = host_view(w, h)
        lo, hi, basis = image_cost(hw, hh)
        what = (f"CTX_MEDIA: the image this {viewer} opens is a {w}x{h} {name}. {viewer} "
                f"gives you a downscaled copy ({hw}x{hh} as observed on "
                f"{CLAUDE_READ_EDGE_OBSERVED}; {fmt_cost(lo, hi)} tok est., {basis}), so "
                "small text and fine detail may be unreadable.")
    else:
        what = (f"CTX_MEDIA: the image this {viewer} opens is a {w}x{h} {name}; the copy "
                "you are shown may be downscaled, so small text and fine detail may be "
                "unreadable.")
    if not pillow:
        return what + (" A sharp crop of any region is available from `ctx view` once "
                       f"Pillow is installed ({_PILLOW_HINT}); it is not installed here.")
    est = f" ({_cell_est(w, h, rows, cols)})" if host == "claude-code" else ""
    return (what + f" For any region at the original resolution, run "
            f"`ctx view {target} --grid {rows}x{cols} --cell {cell}`{est} and "
            f"{viewer} the file it prints after `open:`. Cells run A-{_cols(cols)} "
            f"left to right and 1-{rows} top to bottom; the same command without "
            "--grid/--cell shows the whole image with that grid drawn on it.")


@_templated
def redirect_note(target: str, w: int, h: int, ow: int, oh: int, rows: int, cols: int,
                  *, host: str, viewer: str = "Read") -> str:
    """The note attached to a read that was redirected to an overview."""
    cell = _example_cell(rows, cols)
    est = f" ({_cell_est(w, h, rows, cols)})" if host == "claude-code" else ""
    return (f"CTX_MEDIA: this {viewer} was redirected. What you see is a REDUCED "
            f"OVERVIEW ({ow}x{oh}, with a labelled {rows}x{cols} grid: columns "
            f"A-{_cols(cols)} left to right, rows 1-{rows} top to bottom), not the "
            f"original {w}x{h} image, and small text may be unreadable in it. The "
            f"original is unchanged. For any cell at the original resolution, run "
            f"`ctx view {target} --grid {rows}x{cols} --cell {cell}`{est} and {viewer} "
            "the file it prints after `open:`.")


@_templated
def convert_note(target: str, fmt: str, wh: tuple[int, int] | None, *,
                 viewer: str = "Read") -> str:
    name = _FORMAT_NAME.get(fmt, fmt.upper())
    size = f"{wh[0]}x{wh[1]} " if wh else ""
    return (f"CTX_MEDIA: the image this {viewer} opens is a {size}{name}, a format "
            f"{viewer} may not be able to show. `ctx view {target}` renders it as a "
            f"viewable overview with a labelled grid; {viewer} the file it prints after "
            "`open:`.")


@_templated
def steer_reason(target: str, fmt: str, kind: str, *, viewer: str = "Read") -> str:
    """The deny reason for a video or audio read."""
    name = _FORMAT_NAME.get(fmt, fmt.upper())
    missing = missing_engines(kind)
    if kind == "audio":
        head = (f"CTX_MEDIA: this file is audio ({name}). {viewer} cannot play audio "
                "and ctx does not transcribe it.")
        if missing:
            return head + (f" ctx view could report its duration and codecs, but "
                           f"{' and '.join(missing)} is not on PATH here.")
        return head + f" For its duration and codecs: `ctx view {target}`."
    head = f"CTX_MEDIA: this file is a video ({name}). {viewer} cannot display video."
    if missing:
        return head + (f" ctx view could make a sheet of timestamped frames, but "
                       f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                       "not installed here (ffmpeg provides ffmpeg and ffprobe; Pillow: "
                       f"{_PILLOW_HINT}). Once installed: `ctx view {target}`.")
    return head + (f" Run `ctx view {target}` for a contact sheet of timestamped "
                   f"frames, then {viewer} the file it prints after `open:`. Zoom in "
                   "time with --at <seconds> or --range A-B on the same command.")


def missing_engines(kind: str) -> list[str]:
    """What ``ctx view`` would need and this machine lacks, for ``kind``."""
    missing = [t for t in ("ffmpeg", "ffprobe") if which(t) is None]
    if kind == "audio":
        return [t for t in missing if t == "ffprobe"]
    if not has_pillow():
        missing.append("Pillow")
    return missing


@_templated
def shell_hint(target: str, kind: str) -> str:
    """The line added to an ffmpeg/ffprobe call on a video or audio file."""
    if kind == "audio":
        return ("CTX_MEDIA: for this audio file's duration and codecs without parsing "
                f"ffprobe output, run `ctx view {target}` (audio is never transcribed).")
    return (f"CTX_MEDIA: to look at this video, run `ctx view {target}`: one contact "
            "sheet of timestamped frames to Read (the file it prints after `open:`), then "
            "--at <seconds> or --range A-B to zoom. No frame extraction needed.")


# -------------------------------------------------------- the shell hint
VIDEO_TOOLS = frozenset({"ffmpeg", "ffprobe", "ffplay"})
_WRAPPERS = frozenset({"env", "sudo", "nice", "nohup", "time", "timeout", "command",
                       "stdbuf", "exec"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})


def video_tool_args(command: str, cwd: str, *, _depth: int = 0) -> list[str]:
    """Absolute paths of the media-suffixed arguments of every simple command
    in ``command`` whose PROGRAM is a video tool (ffmpeg, ffprobe, ffplay).

    Not a word search: ``echo ffmpeg v.mp4`` and ``grep ffmpeg notes v.mp4``
    name no video tool as a program. Follows ``cd DIR &&`` for relative paths
    and one level of ``bash -c '...'``; wrappers (env, sudo, timeout ...) and
    leading VAR=value assignments are skipped. Bounded: 64 simple commands,
    256 words. Never raises on odd input (it returns what it understood)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        lex.commenters = ""
        words = []
        for w in lex:
            words.append(w)
            if len(words) > 256:
                break
    except ValueError:
        return []
    out: list[str] = []
    simple: list[list[str]] = [[]]
    for w in words:
        if w in _SEPARATORS or set(w) <= set(";&|()"):
            simple.append([])
        else:
            simple[-1].append(w)
    here = cwd
    for argv in simple[:64]:
        i = 0
        while i < len(argv) and ("=" in argv[i] and not argv[i].startswith("-")
                                 and argv[i].split("=", 1)[0].isidentifier()):
            i += 1
        while i < len(argv) and os.path.basename(argv[i]) in _WRAPPERS:
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or argv[i][:1].isdigit()
                                     or ("=" in argv[i] and argv[i].split("=", 1)[0].isidentifier())):
                i += 1
        if i >= len(argv):
            continue
        prog, rest = os.path.basename(argv[i]), argv[i + 1:]
        if prog == "cd" and rest:
            here = rest[0] if os.path.isabs(rest[0]) else os.path.join(here, rest[0])
            continue
        if prog in _SHELLS and _depth == 0 and "-c" in rest:
            j = rest.index("-c")
            if j + 1 < len(rest):
                out.extend(video_tool_args(rest[j + 1], here, _depth=1))
            continue
        if prog in VIDEO_TOOLS:
            for a in rest:
                if suffix_class(a) in ("video", "audio"):
                    out.append(os.path.normpath(a if os.path.isabs(a) else os.path.join(here, a)))
    return out


# ---------------------------------------------------------------- redirect
_OVERVIEW_REL = r"\.ctx-session-reads/media/overview-([0-9a-f]{12})\.(png|jpg)"


def make_overview(root: str, rel: str, budget_ms: int) -> tuple[dict | None, str]:
    """Run ``ctx view --json`` on ``rel`` (in ``root``) within ``budget_ms``.

    Returns ``(facts, "")`` for a verified overview, else ``(None, why)``.
    Reuses ``ctx view``'s own entry point for everything that reads the file
    (one no-follow open, a private snapshot, the workspace policy). The result
    is accepted only if it names a file this run wrote under the session
    media directory, reached without a symlink at any component, whose bytes
    hash to the digest ``ctx view`` reported.
    """
    import json
    import subprocess
    import sys

    arg = rel if not (rel.startswith("-") or re.match(_ADDRESS_LIKE, rel)) else "./" + rel
    try:
        # `-P` (Python 3.11+, the project's floor): do NOT prepend the working
        # directory to sys.path. The child runs in the workspace root, and a
        # plain `-m ctx` there would import a repository's own `ctx/` package
        # instead of the installed tool: code from the workspace, run by the
        # hook on the first image read. `-P` keeps the installed tool's own
        # resolution (site-packages, an editable install, PYTHONPATH the user
        # set) intact, which `-I` would not for a user-site install.
        # A relative or empty PYTHONPATH entry would name the workspace again.
        env = dict(os.environ)
        if "PYTHONPATH" in env:
            kept = [e for e in env["PYTHONPATH"].split(os.pathsep) if os.path.isabs(e)]
            if kept:
                env["PYTHONPATH"] = os.pathsep.join(kept)
            else:
                env.pop("PYTHONPATH")
        cp = subprocess.run(
            [sys.executable, "-P", "-m", "ctx", "view", "--json", arg],
            cwd=root, capture_output=True, timeout=max(0.5, budget_ms / 1000.0),
            stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "spawn"
    if cp.returncode != 0:
        return None, "view_failed"
    try:
        facts = json.loads(cp.stdout.decode("utf-8"))
        d = facts["derived"]
        if facts.get("target") != rel or d.get("op") != "overview":
            return None, "unexpected"
        m = re.fullmatch(_OVERVIEW_REL, str(d.get("path") or ""))
        digest = str(d.get("output_sha256") or "")
        if not m or not re.fullmatch(r"[0-9a-f]{64}", digest) or m.group(1) != digest[:12]:
            return None, "unexpected"
        if not _verify_owned(root, m.group(0), digest):
            return None, "unverified"
        return facts, ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, "unexpected"


def _verify_owned(root: str, rel: str, digest: str) -> bool:
    import hashlib

    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return False
        if _stat.S_ISLNK(st.st_mode):
            return False
    got = read_head(cur, REDIRECT_MAX_BYTES)
    if got is None or len(got[0]) != got[1]:
        return False
    return hashlib.sha256(got[0]).hexdigest() == digest


__all__ = [
    "AUDIO", "CONVERTIBLE", "DISPLAYABLE", "HOSTS", "IMAGE_MODES", "VIDEO", "VIDEO_MODES",
    "video_tool_args", "missing_engines", "UNSHOWN_TARGET", "advise_note", "cell_cost", "fmt_cost", "image_cost", "image_tier", "tier_tokens", "command_target", "convert_note", "ctx_root",
    "default_grid", "dims", "has_pillow", "host_downscales", "host_view", "make_overview",
    "overview_size", "policy_excluded", "read_head", "redirect_note", "sniff_still",
    "steer_reason", "suffix_class", "tokens", "which",
]
