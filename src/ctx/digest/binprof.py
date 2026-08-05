"""Binary/image/PDF profile — a typed digest for non-text output.

Without this, a `cat screenshot.png` (or any tool that emits binary) falls to
text/v1, which treats the bytes as "lines" and produces garbage — or, unwrapped,
enters the transcript as tens of thousands of base64 tokens of opaque noise. The
binary profile detects the format from magic bytes and renders *structure* —
format, dimensions, colour, page count, an exact hash, and (for images) a
perceptual hash for pixel-free diffing — while the raw bytes stay in the store
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
        # Failure evidence must never be swallowed by a "looks like a valid
        # image" digest: a process can write a PNG to stdout, print a fatal
        # error to stderr, and exit nonzero. Since this profile wins before
        # every text profile, it carries the exit status and a bounded stderr
        # excerpt itself, mirroring the text fallback.
        header.append(_status_line(ctx))
        # include_perceptual=False keeps the digest identity independent of the
        # optional Pillow extra (see binfmt.render_digest).
        body = binfmt.render_digest(info, address="ctx get run:PENDING#stdout",
                                    include_perceptual=False)
        parts = ["\n".join(header), body]
        stderr_block = _bounded_stderr(ctx)
        if stderr_block:
            parts.append(stderr_block)
        return "\n".join(parts)


# --- failure evidence helpers -------------------------------------------------
_STDERR_HEAD = 12   # lines kept from the top of a stderr flood
_STDERR_TAIL = 12   # lines kept from the bottom (the fatal line usually lands here)


def _status_line(ctx: DigestContext) -> str:
    r = ctx.manifest.get("result") or {}
    code = r.get("exitCode")
    status = f"exit {code}" if code is not None else f"signal {r.get('signal')}"
    if r.get("timedOut"):
        status += " · timed out"
    return f"status: {status}"


def _bounded_stderr(ctx: DigestContext) -> str | None:
    """A deterministic, bounded stderr excerpt — head + tail with an elision
    marker — so diagnostics survive without reintroducing a flood."""
    view = getattr(ctx, "stderr", None)
    if view is None or not getattr(view, "bytes", 0):
        return None
    lines = list(view.text_lines)
    if not lines:
        return None
    if len(lines) <= _STDERR_HEAD + _STDERR_TAIL:
        kept = lines
    else:
        elided = len(lines) - _STDERR_HEAD - _STDERR_TAIL
        kept = lines[:_STDERR_HEAD] + [f"… {elided} stderr lines elided …"] + lines[-_STDERR_TAIL:]
    return "--- stderr ---\n" + "\n".join(kept)
