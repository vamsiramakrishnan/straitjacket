"""Binary-format containment: typed digests for images, PDFs, and other blobs.

The harness's promise — capture the bytes, emit a bounded typed view, keep an
address — has to hold for binary too, or a screenshot enters the transcript as
~34k tokens of opaque base64 and a compressor corrupts it. This module reads a
blob's *structure* (never its pixels) into a deterministic digest:

  * format + magic-byte identification (PNG/JPEG/GIF/BMP/WebP/PDF/…);
  * dimensions and colour metadata from the header — **stdlib only**, always on;
  * a content hash (exact identity) and, for images, a **perceptual hash**
    (dhash) so two renders can be diffed at digest-rate without sending pixels;
  * best-effort visible page objects / text operators for PDFs, labelled as a
    heuristic because compressed object streams cannot be classified safely by
    a dependency-free byte scan.

Perceptual hashing needs a pixel decode, so it is gated on Pillow (the
``image`` extra); absent, the digest still carries format + dimensions + exact
hash and says so. Every entry point fails soft — an unreadable or truncated
blob degrades to what could be parsed, never an exception.
"""

from __future__ import annotations

import hashlib
import re
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
IMAGE_FORMATS = frozenset({"png", "jpeg", "gif", "bmp", "webp", "tiff", "ico", "heic", "avif"})
# Containers that carry time-based media. Never decoded here; `ctx.media` owns
# the (optional) ffprobe-backed census. Kept separate from IMAGE_FORMATS so
# `BlobInfo.is_image` and the pixel paths never see them.
MEDIA_FORMATS = frozenset({"mp4", "mov", "m4a", "mkv", "webm", "wav", "avi", "mp3", "flac"})

# ISO base media `ftyp` major brand -> format id. Unknown brands are "mp4":
# the container is the same family and ffprobe names the real codecs.
_FTYP_HEIC = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
                        b"mif1", b"msf1"})
_FTYP_AVIF = frozenset({b"avif", b"avis"})
_FTYP_M4A = frozenset({b"M4A ", b"M4B ", b"M4P "})


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
    text_operator_seen: bool | None = None  # PDF raw-structure heuristic
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.format in IMAGE_FORMATS or self.format == "webp"


def _printable4(b: bytes) -> bool:
    return len(b) == 4 and all(0x20 <= c < 0x7F for c in b)


def _syncsafe(b: bytes) -> int | None:
    if len(b) != 4 or any(c & 0x80 for c in b):
        return None
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _mpeg_frame_header(h: bytes) -> bool:
    """A plausible MPEG audio frame header (sync, version, layer, bitrate, rate)."""
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return False
    return (((h[1] >> 3) & 3) != 1 and ((h[1] >> 1) & 3) != 0
            and (h[2] >> 4) not in (0, 15) and ((h[2] >> 2) & 3) != 3)


def _is_mp3(head: bytes) -> bool:
    """An ID3v2-tagged MPEG audio file, validated past the magic bytes.

    Header fields must be in range (version 2-4, revision 0, undefined flag
    bits clear, syncsafe size) AND the tag must end inside the head, where a
    real frame sync has to follow it. A tag that runs past the head cannot be
    verified and is not claimed. A bare frame sync without a tag is also not
    claimed: four bytes are too weak to reclassify arbitrary output."""
    if len(head) < 10 or head[:3] != b"ID3":
        return False
    if head[3] not in (2, 3, 4) or head[4] != 0 or head[5] & 0x0F:
        return False
    size = _syncsafe(head[6:10])
    if size is None:
        return False
    end = 10 + size + (10 if head[5] & 0x10 else 0)
    return end + 4 <= len(head) and _mpeg_frame_header(head[end:end + 4])


_KNOWN_BRANDS = frozenset(
    [b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7", b"iso8", b"iso9",
     b"mp41", b"mp42", b"mp71", b"avc1", b"M4A ", b"M4B ", b"M4P ", b"M4V ", b"M4VH",
     b"M4VP", b"qt  ", b"3gp4", b"3gp5", b"3gp6", b"3gp7", b"3g2a", b"3g2b", b"3g2c",
     b"mmp4", b"f4v ", b"f4a ", b"dash", b"msdh", b"msix", b"MSNV", b"XAVC", b"heic",
     b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis",
     b"crx ", b"jp2 ", b"mjp2", b"mj2s"])
_KNOWN_BOXES = frozenset(
    [b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot", b"uuid", b"meta", b"moof",
     b"styp", b"sidx", b"ftyp", b"jP  ", b"prft", b"emsg"])


def _is_isobmff(head: bytes) -> bool:
    """`ftyp` box with a sane size, a major or compatible brand ffmpeg-family
    tools know, and - when visible - a recognised box after it."""
    if len(head) < 16 or head[4:8] != b"ftyp":
        return False
    size = int.from_bytes(head[:4], "big")
    if size < 16 or size > 4096 or not _printable4(head[8:12]):
        return False
    brands = [head[8:12]]
    if size <= len(head):
        if (size - 16) % 4:
            return False
        compat = [head[o:o + 4] for o in range(16, size, 4)]
        if not all(_printable4(b) for b in compat):
            return False
        brands += compat
        if size + 8 <= len(head):
            nxt = int.from_bytes(head[size:size + 4], "big")
            if (nxt not in (0, 1) and nxt < 8) or head[size + 4:size + 8] not in _KNOWN_BOXES:
                return False
    return any(b in _KNOWN_BRANDS for b in brands)


def _ebml_doctype(head: bytes) -> str | None:
    """`matroska` / `webm` when a well-formed EBML header declares it."""
    if len(head) < 8 or head[:4] != b"\x1a\x45\xdf\xa3" or not head[4]:
        return None
    width = 9 - head[4].bit_length()
    if width > 8 or len(head) < 4 + width:
        return None
    body = int.from_bytes(head[4:4 + width], "big") & ((1 << (7 * width)) - 1)
    if body < 4 or body > 1024:
        return None
    region = head[4 + width:4 + width + body]
    at = region.find(b"\x42\x82")
    if at < 0 or at + 3 > len(region):
        return None
    n = region[at + 2]
    if n & 0x80 == 0:
        return None
    value = region[at + 3:at + 3 + (n & 0x7F)]
    return value.decode("ascii") if value in (b"matroska", b"webm") else None


def _riff_media(head: bytes) -> str | None:
    """`wav` / `avi` when the first chunk is what the container requires."""
    if len(head) < 28 or head[:4] != b"RIFF":
        return None
    total = int.from_bytes(head[4:8], "little")
    if total < 36:
        return None
    if head[8:12] == b"WAVE":
        if head[12:16] == b"fmt " and 16 <= int.from_bytes(head[16:20], "little") <= 64:
            return "wav"
    elif head[8:12] == b"AVI ":
        if head[12:16] == b"LIST" and head[20:24] == b"hdrl" and head[24:28] == b"avih":
            return "avi"
    return None


def _is_flac(head: bytes) -> bool:
    """`fLaC`, a STREAMINFO block header (type 0, length 34), plausible
    STREAMINFO fields, and then either a valid next metadata block header or,
    when STREAMINFO is the last block, an audio frame sync."""
    if len(head) < 43 or head[:4] != b"fLaC" or head[4] & 0x7F or head[5:8] != b"\x00\x00\x22":
        return False
    body = head[8:42]
    min_block = int.from_bytes(body[0:2], "big")
    max_block = int.from_bytes(body[2:4], "big")
    rate = (body[10] << 12) | (body[11] << 4) | (body[12] >> 4)
    bits = (((body[12] & 1) << 4) | (body[13] >> 4)) + 1
    if not (16 <= min_block <= max_block <= 65535) or not (1 <= rate <= 655350) or bits < 4:
        return False
    nxt = head[42]
    if head[4] & 0x80:                          # STREAMINFO was the last block
        return nxt == 0xFF and len(head) > 43 and head[43] & 0xFC == 0xF8
    return (nxt & 0x7F) in (1, 2, 3, 4, 5, 6)


_SVG_DOCTYPE = re.compile(rb"<!DOCTYPE\s+svg\b", re.I)
_SVG_ROOT = re.compile(
    rb"<svg(?:\s+[A-Za-z_:][\w:.\-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'))*\s*(/?)>")


def _is_svg(head: bytes) -> bool:
    """A document whose ROOT element is `<svg>` with well-formed attributes:
    after any BOM, XML declaration, comments and an `svg` doctype - never the
    substring in prose, markdown or an HTML page."""
    window = head[:1024]
    if b"\x00" in window:
        return False
    text = window[3:] if window.startswith(b"\xef\xbb\xbf") else window
    while True:
        text = text.lstrip(b" \t\r\n")
        if text.startswith(b"<?"):
            end = text.find(b"?>")
            if end < 0:
                return False
            text = text[end + 2:]
        elif text.startswith(b"<!--"):
            end = text.find(b"-->")
            if end < 0:
                return False
            text = text[end + 3:]
        elif text.startswith(b"<!"):
            if not _SVG_DOCTYPE.match(text):
                return False
            end = text.find(b"]>") + 2 if text.find(b"[", 0, text.find(b">") + 1) >= 0 \
                else text.find(b">") + 1
            if end <= 1:
                return False
            text = text[end:]
        else:
            break
    m = _SVG_ROOT.match(text)
    if not m:
        return False
    rest = text[m.end():]
    if m.group(1):                                   # <svg .../>
        return not rest.strip()
    close = rest.rfind(b"</svg>")
    if close >= 0:
        return not rest[close + 6:].strip()
    # no closing tag yet: only a head cut off mid-document, which must declare xmlns
    return len(head) >= 1024 and b"xmlns" in m.group(0)


def sniff_format(head: bytes) -> str:
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    # A container is only ever named when the payload is also demonstrably not
    # text: the bytes the pre-existing heuristic called "binary" (a NUL in the
    # first KiB). Anything it called "text" keeps its text digest, whatever it
    # starts with.
    if b"\x00" in head[:1024]:
        named = _named_container(head)
        if named:
            return named
        return "binary"
    if _is_svg(head):
        return "svg"
    return "text"


def _named_container(head: bytes) -> str | None:
    riff = _riff_media(head)
    if riff:
        return riff
    if _is_isobmff(head):
        brand = head[8:12]
        if brand in _FTYP_AVIF:
            return "avif"
        if brand in _FTYP_HEIC:
            return "heic"
        if brand in _FTYP_M4A:
            return "m4a"
        if brand == b"qt  ":
            return "mov"
        return "mp4"
    doctype = _ebml_doctype(head)
    if doctype:
        return "webm" if doctype == "webm" else "mkv"
    if _is_flac(head):
        return "flac"
    if _is_mp3(head):
        return "mp3"
    return None


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
    """Visible page-object count, visible text operator, and PDF version.

    This is deliberately a labelled heuristic. Object streams may be compressed
    and incremental PDFs may retain superseded objects, so absence is never
    interpreted as "scanned" and the count is not a semantic page guarantee.
    """
    version = None
    if data[:8].startswith(b"%PDF-"):
        version = data[5:8].decode("latin-1", "ignore").strip()
    # Count page objects (not /Pages tree nodes). Robust to whitespace variants.
    import re

    pages = len(re.findall(rb"/Type\s*/Page(?![sA-Za-z])", data))
    text_operator_seen = None
    if pages:
        text_operator_seen = bool(re.search(rb"\bBT\b|\bTj\b|\bTJ\b", data))
    return (pages or None), text_operator_seen, version


# ------------------------------------------------------------- perceptual hash
def _pillow_details(
    data: bytes,
) -> tuple[int, int, str, str] | None:
    """Width, height, mode, and 64-bit dHash, or None without Pillow/decode."""
    try:
        import io

        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            mode = {
                "1": "mono", "L": "gray", "LA": "gray+a", "P": "palette",
                "RGB": "rgb", "RGBA": "rgba", "CMYK": "cmyk",
            }.get(source.mode, source.mode.lower())
            resampling = getattr(Image, "Resampling", Image).BILINEAR
            image = source.convert("L").resize((9, 8), resampling)
        px = list(image.tobytes())  # 72 grayscale bytes (9x8), one per pixel
        bits = 0
        for row in range(8):
            for col in range(8):
                left = px[row * 9 + col]
                right = px[row * 9 + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return width, height, mode, f"{bits:016x}"
    except Exception:
        return None


def perceptual_hash(data: bytes) -> str | None:
    """8x8 dHash as 16 hex chars (64 bits), or None without Pillow."""
    details = _pillow_details(data)
    return details[3] if details else None


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
    fmt = sniff_format(data[:1024])  # widen to match sniff_format's own 1 KiB NUL-heuristic window
    info = BlobInfo(format=fmt, bytes=len(data), sha256=sha)
    if fmt in IMAGE_FORMATS:
        parser = _DIM_PARSERS.get(fmt)
        if parser is not None:
            try:
                info.width, info.height, info.color = parser(data)
            except Exception:
                pass
        details = _pillow_details(data)
        if details:
            width, height, color, info.perceptual_hash = details
            info.width = info.width or width
            info.height = info.height or height
            info.color = info.color or color
    elif fmt == "pdf":
        try:
            info.pages, info.text_operator_seen, ver = _pdf_info(data)
            if ver:
                info.extra["pdf_version"] = ver
        except Exception:
            pass
    return info


def render_digest(info: BlobInfo, *, address: str | None = None) -> str:
    """Bounded, deterministic, human-readable digest — structure, never pixels."""
    from ctx.textutil import estimate_tokens, fmt_bytes

    lines = [f"format: {info.format}",
             f"bytes: {info.bytes:,} ({fmt_bytes(info.bytes)}) · "
             f"~{estimate_tokens(info.bytes):,} tok if inlined raw"]
    if info.is_image:
        dims = (
            f"{info.width}×{info.height}" if info.width and info.height
            else "dimensions unavailable"
        )
        if info.color and info.width and info.height:
            dims += f" {info.color}"
        lines.append(f"image: {dims}")
        if info.perceptual_hash:
            lines.append(f"phash: {info.perceptual_hash}  (dhash/8x8 — `ctx image diff` to compare)")
        else:
            lines.append("phash: unavailable (install the `image` extra: pip install 'ctx-harness[image]')")
    if info.format == "pdf":
        pages = (
            f"visible page objects: {info.pages} (heuristic)"
            if info.pages else "visible page objects: unknown"
        )
        lines.append(f"pdf: {pages}")
        if info.text_operator_seen:
            lines.append("pdf text operators: visible in raw structure")
        else:
            lines.append("pdf text layer: unknown (object streams may be compressed)")
        if info.extra.get("pdf_version"):
            lines.append(f"pdf version: {info.extra['pdf_version']}")
    lines.append(f"sha256: {info.sha256[:16]}… (exact identity)")
    if address:
        lines.append(f"bytes kept in store — retrieve: {address}")
    return "\n".join(lines)


__all__ = [
    "BlobInfo", "IMAGE_FORMATS", "MEDIA_FORMATS", "sniff_format", "inspect", "render_digest",
    "perceptual_hash", "phash_distance",
]
