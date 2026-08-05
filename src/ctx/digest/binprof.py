"""Binary/image/PDF profile — a typed digest for non-text output.

Without this, a `cat screenshot.png` (or any tool that emits binary) falls to
text/v1, which treats the bytes as "lines" and produces garbage — or, unwrapped,
enters the transcript as tens of thousands of base64 tokens of opaque noise. The
binary profile detects the format from magic bytes and renders *structure* —
format, dimensions, colour, labelled PDF heuristics, an exact hash, and (for
images) a dHash for render comparison — while the raw bytes stay in the store
behind a `ctx get run:…#stdout` address.
"""

from __future__ import annotations

from ctx import binfmt
from ctx.digest.base import DigestContext, Profile

# Formats worth a typed binary digest. Plain "text" declines (falls through to
# the text/log/table profiles); a NUL-bearing unknown blob is still bounded.
_BINARY_FORMATS = binfmt.IMAGE_FORMATS | {"pdf", "binary", "gzip", "zip", "elf", "ogg"}


class BinaryProfile(Profile):
    version = "binary/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        fmt = binfmt.sniff_format(ctx.stdout.head)
        if fmt in _BINARY_FORMATS:
            return f"binary output ({fmt}) by magic bytes"
        return None

    def render(self, ctx: DigestContext) -> str:
        meta = ctx.manifest["streams"]["stdout"]
        data = b""
        if ctx.store is not None:
            try:
                data = ctx.store.get_blob(str(meta["blob"]).removeprefix("sha256:"))
            except Exception:
                data = b""
        if not data:  # store-free rendering (tests) — degrade to the head
            data = ctx.stdout.head
        info = binfmt.inspect(data)
        header = [f"cwd: {ctx.manifest['cwd']}", f"command: {ctx.command_line()}"]
        body = binfmt.render_digest(info, address="ctx get run:PENDING#stdout")
        return "\n".join(header) + "\n" + body
