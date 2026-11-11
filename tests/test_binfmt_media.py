"""Acceptance: `binfmt` recognises video, audio and newer image containers,
and nothing it already said about older formats changed.

Pure stdlib: these headers are hand-built, so the tests run on the minimal CI
job with no Pillow and no ffmpeg."""

from __future__ import annotations

import hashlib

import pytest

import media_headers as H
from ctx import binfmt


_SNIFF = [
    ("mp4", H.ftyp(b"isom")),
    ("mp4", H.ftyp(b"mp42")),
    ("mov", H.ftyp(b"qt  ")),
    ("m4a", H.ftyp(b"M4A ")),
    ("heic", H.ftyp(b"heic")),
    ("heic", H.ftyp(b"mif1")),
    ("avif", H.ftyp(b"avif")),
    ("mkv", H.ebml(b"matroska")),
    ("webm", H.ebml(b"webm")),
    ("wav", H.wav()),
    ("avi", H.avi()),
    ("webp", b"RIFF\x24\x08\x00\x00WEBPVP8 " + b"\x00" * 16),
    ("mp3", H.mp3_tagged(0)),
    ("mp3", H.mp3_tagged(300)),
    ("mp3", H.mp3_tagged(900)[:1024]),           # tag ends inside the head, frame follows
    ("flac", H.flac()),
    ("svg", H.SVG),
    ("svg", b'<!DOCTYPE svg PUBLIC "x" "y">\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
    ("svg", b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'),
    ("svg", b"\xef\xbb\xbf  \n<svg xmlns='x'></svg>"),
]


@pytest.mark.parametrize(("fmt", "head"), _SNIFF)
def test_new_containers_are_sniffed(fmt, head):
    assert binfmt.sniff_format(head[:1024]) == fmt
    assert binfmt.inspect(head).format == fmt


@pytest.mark.parametrize("name", sorted(H.NEAR_MISSES))
def test_near_misses_keep_the_classification_they_had_at_the_base_commit(name):
    """Text or binary that merely STARTS like a container must not be
    reclassified: `expected` is what the pre-change sniffer returned, and the
    captured-output digest depends on it."""
    data, expected = H.NEAR_MISSES[name]
    assert binfmt.sniff_format(data[:1024]) == expected
    assert binfmt.inspect(data).format == expected
    from ctx.digest.binprof import _BINARY_FORMATS

    assert (binfmt.sniff_format(data[:1024]) in _BINARY_FORMATS) == (expected == "binary")


def test_media_formats_are_not_images():
    for fmt in binfmt.MEDIA_FORMATS:
        assert fmt not in binfmt.IMAGE_FORMATS
    info = binfmt.inspect(H.mp4_header())
    assert not info.is_image and info.perceptual_hash is None
    assert binfmt.inspect(H.ftyp(b"avif")).is_image  # a still, even if undecodable here
    assert binfmt.inspect(H.SVG).is_image is False


def test_digest_of_a_video_names_its_format_and_address():
    text = binfmt.render_digest(binfmt.inspect(H.mp4_header()), address="ctx get run:X#stdout")
    assert text.startswith("format: mp4\n")
    assert "run:X#stdout" in text and "image:" not in text


def test_binary_profile_still_claims_video_and_audio_output():
    """A NUL-bearing mp4 used to be "binary" and the binary profile took it;
    the new names must not drop it back to the text profile."""
    from ctx.digest.binprof import _BINARY_FORMATS

    assert binfmt.MEDIA_FORMATS <= _BINARY_FORMATS
    assert "svg" not in _BINARY_FORMATS  # SVG stays text


# Hex of a header-only blob per pre-existing format, and the sha256 of the
# digest the PRE-CHANGE binfmt rendered for it (address "ctx get run:X#stdout").
_GOLDEN = {
    "png": ("89504e470d0a1a0a0000000d4948445200000280000001e008060000000000000000000000",
            "ff259d7404103cc7a9933cbc20e3208019ab225298b44044c4b3a51482968ca0"),
    "jpeg": ("ffd8ffc000110801e00280030000000000000000000000000000000000000000",
             "1f2bb7252df7a9b34e23a2b3a8eacd86231a3c8d9ddb6bc8de37c4a924cb9e1b"),
    "gif": ("4749463839614001c80000000000000000000000",
            "100ad958764a1c3118a5b7690df5cd9b278ee40a206d355ddb70cd2530ce6ac9"),
    "bmp": ("424d000000000000000000000000000000001e000000140000000000000000000000",
            "e5fb3f00dcd8989f7a82e1929113c3097aa33b716f5b12a1d8af5602cbb2da7c"),
    "pdf": ("255044462d312e370a312030206f626a203c3c202f54797065202f50616765203e3e20656e"
            "646f626a2042542028782920546a2045540a",
            "331aeec5c9d485fcfa10ee036f9a718abe55c8cb3d51b08adbdd1c5ef89c5dee"),
    "ico": ("000001000000000000000000000000000000000000000000",
            "264d16e6b4e2672e73b4d69caacb186171d17a4989ecf563ca86227975476e8f"),
    "tiff": ("49492a000000000000000000000000000000000000000000",
             "a3d62bffbd9d2f3ae9dec45fa1991da0961447625b9215f68ebc9467a86e4d15"),
    "ogg": ("4f6767530000000000000000000000000000000000000000",
            "f672fcd32803c210f1a152e186d815e8bd54f724a7f0c99b80060f6711280203"),
    "gzip": ("1f8b0000000000000000000000000000000000000000",
             "9d562cd0f50ada4bc4928b8a7cddf3331f6ccce9714f76e55fb656fc18eca9d0"),
    "zip": ("504b03040000000000000000000000000000000000000000",
            "0708c658becdcbe362f309286c5db98aebc31734552b9480f85021efef0b439b"),
    "elf": ("7f454c460000000000000000000000000000000000000000",
            "807ae827f7c55f29b97389343d5a8f7d237676c447b13df62b1fb261459d090d"),
    "webp": ("5249464600000000574542505650385800000000000000007f0200df0100",
             "94638586230e007765182f4e9273a8a495c0b432b1e8fd0fa74c7e032f1a4717"),
    "binary": ("61626300646566616263006465666162630064656661626300646566",
               "085cc68f0d8c1f20a9e6f03572f153add8188974759c66444c74eefcd0067864"),
    "text": ("68656c6c6f20776f726c640a68656c6c6f20776f726c640a68656c6c6f20776f726c640a",
             "35ac1bb2c73bafc2989680f399fa89d49967149a4e481707c8e06f92b5951125"),
}


@pytest.mark.parametrize("name", sorted(_GOLDEN))
def test_existing_format_digests_are_byte_identical(name):
    data_hex, expected = _GOLDEN[name]
    data = bytes.fromhex(data_hex)
    assert binfmt.sniff_format(data[:1024]) == name
    digest = binfmt.render_digest(binfmt.inspect(data), address="ctx get run:X#stdout")
    assert hashlib.sha256(digest.encode()).hexdigest() == expected
