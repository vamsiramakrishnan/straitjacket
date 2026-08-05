"""Binary-format containment: typed digests for images, PDFs, and other blobs.

The harness's promise — capture the bytes, emit a bounded typed view, keep an
address — has to hold for binary too, or a screenshot enters the transcript as
~34k tokens of opaque base64 and a compressor corrupts it. This module reads a
blob's *structure* (never its pixels) into a deterministic digest:

  * format + magic-byte identification (PNG/JPEG/GIF/BMP/WebP/PDF/…);
  * dimensions and colour metadata from the header — **stdlib only**, always on;
  * a content hash (exact identity) and, for images, a **perceptual hash**
    (dhash) so two renders can be diffed at digest-rate without sending pixels;
  * page count / text-extractability for PDFs.

Perceptual hashing needs a pixel decode, so it is gated on Pillow (the
``image`` extra); absent, the digest still carries format + dimensions + exact
hash and says so. Every entry point fails soft — an unreadable or truncated
blob degrades to what could be parsed, never an exception.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

# magic prefix -> format id
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"%PDF-", "pdf"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"OggS", "ogg"),
    (b"\x1f\x8b", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"\x7fELF", "elf"),
]
IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "bmp", "webp", "tiff", "ico"})


@dataclass
class BlobInfo:
    format: str                 # png | jpeg | … | "binary" | "text"
    bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None
    color: str | None = None    # e.g. "rgb", "rgba", "gray", "palette"
    perceptual_hash: str | None = None   # 16-hex dhash, images only, Pillow-gated
    pages: int | None = None    # pdf
    text_extractable: bool | None = None  # pdf
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.format in IMAGE_FORMATS or self.format == "webp"


def sniff_format(head: bytes) -> str:
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # heuristic: NUL byte in the first KiB ⇒ binary of unknown format
    if b"\x00" in head[:1024]:
        return "binary"
    return "text"


# ------------------------------------------------------------- dimensions
def _png_dims(data: bytes) -> tuple[int | None, int | None, str | None]:
    if len(data) < 26:
        return None, None, None
    w, h = struct.unpack(">II", data[16:24])
    color_type = data[25]
    color = {0: "gray", 2: "rgb", 3: "palette", 4: "gray+a", 6: "rgba"}.get(color_type)
    return w, h, color


def _gif_dims(data: bytes) -> tuple[int | None, int | None, str | None]:
    if len(data) < 10:
        return None, None, None
    w, h = struct.unpack("<HH", data[6:10])
    return w, h, "palette"


def _bmp_dims(data: bytes) -> tuple[int | None, int | None, str | None]:
    if len(data) < 26:
        return None, None, None
    w, h = struct.unpack("<ii", data[18:26])
    return w, abs(h), "rgb"


def _jpeg_dims(data: bytes) -> tuple[int | None, int | None, str | None]:
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            comps = data[i + 9] if i + 9 < n else 0
            color = {1: "gray", 3: "rgb", 4: "cmyk"}.get(comps)
            return w, h, color
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2:i + 4])[0] if i + 4 <= n else 0
        i += 2 + seg
    return None, None, None


def _webp_dims(data: bytes) -> tuple[int | None, int | None, str | None]:
    if len(data) < 30:
        return None, None, None
    fourcc = data[12:16]
    try:
        if fourcc == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF, "rgb"
        if fourcc == b"VP8L":
            b = data[21:26]
            bits = int.from_bytes(b, "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "rgba"
        if fourcc == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h, "rgba"
    except struct.error:
        return None, None, None
    return None, None, None


_DIM_PARSERS = {"png": _png_dims, "gif": _gif_dims, "bmp": _bmp_dims,
                "jpeg": _jpeg_dims, "webp": _webp_dims}


def _pdf_info(data: bytes) -> tuple[int | None, bool | None, str | None]:
    """(page_count, text_extractable, version) by lightweight structural scan.
    Deterministic and dependency-free; pypdf/pdfminer (when present) can refine
    this, but the count of /Type /Page objects is a robust floor."""
    version = None
    if data[:8].startswith(b"%PDF-"):
        version = data[5:8].decode("latin-1", "ignore").strip()
    # Count page objects (not /Pages tree nodes). Robust to whitespace variants.
    import re

    pages = len(re.findall(rb"/Type\s*/Page(?![sA-Za-z])", data))
    text_extractable = None
    if pages:
        # crude: a text operator (Tj/TJ) anywhere ⇒ some extractable text
        text_extractable = bool(re.search(rb"\bBT\b|\bTj\b|\bTJ\b", data))
    return (pages or None), text_extractable, version


# ------------------------------------------------------------- perceptual hash
def perceptual_hash(data: bytes) -> str | None:
    """8x8 dhash as 16 hex chars (64 bits), or None without Pillow. Two images
    with a small Hamming distance look alike — the basis of a pixel-free visual
    diff. Deterministic for identical input."""
    try:
        import io

        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.BILINEAR)
        px = list(img.tobytes())  # 72 grayscale bytes (9x8), one per pixel
        bits = 0
        for row in range(8):
            for col in range(8):
                left = px[row * 9 + col]
                right = px[row * 9 + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return f"{bits:016x}"
    except Exception:
        return None


def phash_distance(a: str, b: str) -> int | None:
    """Hamming distance between two dhash hex strings (0..64), or None if either
    is missing/invalid."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- top-level
def inspect(data: bytes) -> BlobInfo:
    """Structural digest of a blob. Never raises; parses what it can."""
    sha = hashlib.sha256(data).hexdigest()
    fmt = sniff_format(data[:64])
    info = BlobInfo(format=fmt, bytes=len(data), sha256=sha)
    if fmt in _DIM_PARSERS:
        try:
            info.width, info.height, info.color = _DIM_PARSERS[fmt](data)
        except Exception:
            pass
        info.perceptual_hash = perceptual_hash(data)
    elif fmt == "pdf":
        try:
            info.pages, info.text_extractable, ver = _pdf_info(data)
            if ver:
                info.extra["pdf_version"] = ver
        except Exception:
            pass
    return info


def render_digest(info: BlobInfo, *, address: str | None = None,
                  include_perceptual: bool = True) -> str:
    """Bounded, deterministic, human-readable digest — structure, never pixels.

    ``include_perceptual`` gates the dHash line. It is presentation, not
    identity: the hash is present only when Pillow is installed, so a body that
    carried it would give the *same captured bytes* two different digest
    identities depending on an optional extra. The captured-run profile
    therefore renders with ``include_perceptual=False`` (identity stays a pure
    function of format + dimensions + sha256, all stdlib); the interactive
    ``ctx image`` command, which does not feed a run identity, keeps it on."""
    from ctx.textutil import estimate_tokens, fmt_bytes

    lines = [f"format: {info.format}",
             f"bytes: {info.bytes:,} ({fmt_bytes(info.bytes)}) · "
             f"~{estimate_tokens(info.bytes):,} tok if inlined raw"]
    if info.is_image and info.width:
        dims = f"{info.width}×{info.height}"
        if info.color:
            dims += f" {info.color}"
        lines.append(f"image: {dims}")
        if include_perceptual:
            if info.perceptual_hash:
                lines.append(f"phash: {info.perceptual_hash}  (dhash/8x8 — `ctx image diff` to compare)")
            else:
                lines.append("phash: unavailable (install the `image` extra: pip install 'ctx-harness[image]')")
    if info.format == "pdf":
        p = f"{info.pages} pages" if info.pages else "page count unknown"
        if info.text_extractable is not None:
            p += " · text-extractable" if info.text_extractable else " · scanned (no text layer)"
        lines.append(f"pdf: {p}")
        if info.extra.get("pdf_version"):
            lines.append(f"pdf version: {info.extra['pdf_version']}")
    lines.append(f"sha256: {info.sha256[:16]}… (exact identity)")
    if address:
        lines.append(f"bytes kept in store — retrieve: {address}")
    return "\n".join(lines)


__all__ = [
    "BlobInfo", "IMAGE_FORMATS", "sniff_format", "inspect", "render_digest",
    "perceptual_hash", "phash_distance",
]
