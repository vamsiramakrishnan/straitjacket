"""Hand-built, structurally valid container headers (no Pillow, no ffmpeg)
and near-miss inputs that merely *start* like media.

Shared by the sniffing, media and CLI tests."""

from __future__ import annotations

import struct


def ftyp(brand: bytes = b"isom", *, then_box: bytes = b"free") -> bytes:
    box = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x02\x00" + b"isomiso2"
    return box + struct.pack(">I", 16) + then_box + b"\x00" * 8


def mp4_header() -> bytes:
    return ftyp(b"isom") + b"\x00" * 32


def ebml(doctype: bytes = b"matroska") -> bytes:
    body = b"\x42\x86\x81\x01" + b"\x42\x82" + bytes([0x80 | len(doctype)]) + doctype
    return b"\x1a\x45\xdf\xa3" + bytes([0x80 | len(body)]) + body + b"\x00" * 16


def wav() -> bytes:
    return (b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack("<I", 16)
            + b"\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00")


def avi() -> bytes:
    return (b"RIFF" + struct.pack("<I", 200) + b"AVI LIST" + struct.pack("<I", 100)
            + b"hdrlavih" + b"\x00" * 40)


def mp3_tagged(tag_size: int = 0) -> bytes:
    syncsafe = bytes([(tag_size >> 21) & 0x7F, (tag_size >> 14) & 0x7F,
                      (tag_size >> 7) & 0x7F, tag_size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + b"\x00" * tag_size + b"\xff\xfb\x90\x64" + b"\x00" * 40


def flac() -> bytes:
    streaminfo = (b"\x10\x00\x10\x00" + b"\x00" * 6 + b"\x0a\xc4\x42\xf0" + b"\x00" * 20)
    return b"fLaC\x00\x00\x00\x22" + streaminfo + b"\x81\x00\x00\x04" + b"\x00" * 4


SVG = b'<?xml version="1.0"?>\n<!-- c -->\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>'

# name -> (bytes, what the base commit's sniff_format returned)
NEAR_MISSES: dict[str, tuple[bytes, str]] = {
    "id3_prose": (b"ID3\x03 is a tag format; this is a README line\n", "text"),
    "id3_v2_notes": (b"ID3v2 notes\n", "text"),
    "id3_header_no_frame": (b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + b"junk after the tag", "binary"),
    "id3_concat": (b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10
                   + b"ffconcat version 1.0\nfile 'secrets/notes.txt'\n", "binary"),
    "id3_bad_flags": (b"ID3\x03\x00\x7f\x00\x00\x00\x00" + b"\x00" * 30, "binary"),
    "id3_bad_size": (b"ID3\x03\x00\x00\xff\xff\xff\xff" + b"\x00" * 30, "binary"),
    "svg_in_prose": (b"<svg> tags are explained below\n", "text"),
    "svg_in_html": (b"<!-- header -->\n<html><body><svg></svg></body></html>\n", "text"),
    "svg_later": (b"<html><svg xmlns='x'></svg></html>", "text"),
    "xml_not_svg": (b'<?xml version="1.0"?><root/>', "text"),
    "ftyp_in_prose": (b"abcdftypisom and then some prose\n", "text"),
    "ftyp_tiny": (b"#!/bfty\n", "text"),
    "ftyp_huge_size": (b"\xff\xff\xff\xffftypisom" + b"\x00" * 20, "binary"),
    "ftyp_bad_brand": (b"\x00\x00\x00\x18ftyp\x00\x01\x02\x03" + b"\x00" * 20, "binary"),
    "mkv_prose": (b"\x1a\x45\xdf\xa3hello", "text"),
    "mkv_no_doctype": (b"\x1a\x45\xdf\xa3\x84\x42\x86\x81\x01" + b"\x00" * 20, "binary"),
    "flac_prose": (b"fLaC is a codec name\n", "text"),
    "flac_no_streaminfo": (b"fLaC\x00\x00\x00\x00" + b"\x00" * 20, "binary"),
    "riff_wave_prose": (b"RIFF1234WAVE text\n", "text"),
    "riff_wave_no_fmt": (b"RIFF" + struct.pack("<I", 100) + b"WAVEJUNK" + b"\x00" * 20, "binary"),
    "riff_avi_no_hdrl": (b"RIFF\x10\x00\x00\x00AVI LIST" + b"\x00" * 40, "binary"),
    "latin1_sync": ("ÿûÿ latin one".encode("latin-1"), "text"),
    "bare_mpeg_sync": (b"\xff\xfb\x90\x64" + b"\x00" * 200, "binary"),
    "bare_mpeg_sync_text": (b"\xff\xfb\x90\x64" + bytes(range(1, 250)), "text"),
    "bom_svg_prose": (b"\xef\xbb\xbf<svg/> is self closing\n", "text"),
    "svg_in_prose_with_close": (b"<svg is a tag; see </svg> docs\n", "text"),
    "svg_markdown": (b"<svg width=1>\nsome markdown text\n", "text"),
    "html_doctype_then_svg": (b"<!DOCTYPE html><svg>x</svg>", "text"),
    "ebml_header_without_nul": (ebml()[:-16] + b" and then ordinary text\n", "text"),
    "id3_tag_longer_than_head": (mp3_tagged(5000)[:1024], "binary"),
    "flac_streaminfo_then_text": (b"fLaC\x00\x00\x00\x22" + b"\x00\x10\x00\x10\x00\x00\x00\x00\x00\x00"
                                  + b"\x0a\xc4\x42\xf0" + b"\x00" * 20 + b"plain text follows here", "binary"),
    "ftyp_printable_unknown_brands": (b"\x00\x00\x00\x18ftypabcd\x00\x00\x00\x00abcdefgh"
                                      + b"\x00\x00\x00\x08free\x00" * 4, "binary"),
    "ftyp_known_brand_unknown_next_box": (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
                                          + b"\x00\x00\x00\x10WXYZ" + b"\x00" * 20, "binary"),
}


def reference_movie(path_ref: str) -> bytes:
    """A QuickTime reference movie: one 2x2 rawvideo track whose sample bytes
    live in the EXTERNAL file `path_ref` (an `alis` data reference)."""
    def atom(kind: bytes, payload: bytes = b"") -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    def u32(*values: int) -> bytes:
        return b"".join(struct.pack(">I", v) for v in values)

    ident = u32(0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    ref = path_ref.encode()
    mvhd = atom(b"mvhd", u32(0, 0, 0, 1000, 1000, 0x10000) + b"\x01\x00" + b"\x00" * 10
                + ident + b"\x00" * 24 + u32(2))
    tkhd = atom(b"tkhd", u32(0xF, 0, 0, 1, 0, 1000) + b"\x00" * 16 + ident + u32(2 << 16, 2 << 16))
    mdhd = atom(b"mdhd", u32(0, 0, 0, 1000, 1000) + b"\x55\xc4\x00\x00")
    hdlr = atom(b"hdlr", u32(0, 0) + b"vide" + u32(0, 0, 0) + b"\x00")
    vmhd = atom(b"vmhd", u32(1) + b"\x00" * 8)
    alias = (b"\x00" * 10 + b"\x00" + b"\x00" * 27 + b"\x00" * 12 + bytes([len(ref)])
             + ref.ljust(63, b"\x00") + b"\x00" * 16 + struct.pack(">HH", 1, 1) + b"\x00" * 16
             + struct.pack(">HH", 2, len(ref)) + ref + b"\x00" * 8)
    dinf = atom(b"dinf", atom(b"dref", u32(0, 1) + atom(b"alis", u32(0) + alias)))
    vdesc = (b"\x00" * 6 + struct.pack(">H", 1) + struct.pack(">HH", 0, 0) + b"\x00" * 12
             + struct.pack(">HH", 2, 2) + u32(0x480000, 0x480000, 0) + struct.pack(">H", 1)
             + b"\x00" * 32 + struct.pack(">Hh", 24, -1))
    stbl = atom(b"stbl", atom(b"stsd", u32(0, 1) + atom(b"raw ", vdesc))
                + atom(b"stts", u32(0, 1, 1, 1000)) + atom(b"stsc", u32(0, 1, 1, 1, 1))
                + atom(b"stsz", u32(0, 12, 1)) + atom(b"stco", u32(0, 1, 0)))
    trak = atom(b"trak", tkhd + atom(b"mdia", mdhd + hdlr + atom(b"minf", vmhd + dinf + stbl)))
    return atom(b"ftyp", b"qt  " + u32(0) + b"qt  ") + atom(b"moov", mvhd + trak)
