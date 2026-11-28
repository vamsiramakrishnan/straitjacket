"""Container digest profiles: ``ooxml/v1``, ``sqlite/v1``, and ``archive/v1``.

Thin :class:`ctx.digest.base.Profile` adapters over :mod:`ctx.containers`.
``BinaryProfile`` sits at index 0 of ``_PROFILES`` and declines valid
inspectable containers so these profiles render their internal structure,
while truncated or corrupt binary stubs still land safely on ``binary/v1``.
"""

from __future__ import annotations

from ctx import binfmt, containers
from ctx.digest.base import DigestContext, Profile, from_intake
from ctx.digest.shapeutil import fit

_CONTAINER_HEAD_FORMATS = frozenset({"zip", "gzip", "tar", "sqlite"})


def _blob_bytes(ctx: DigestContext) -> bytes:
    meta = ctx.manifest["streams"]["stdout"]
    data = b""
    if ctx.store is not None:
        try:
            data = ctx.store.get_blob(str(meta["blob"]).removeprefix("sha256:"))
        except Exception:
            data = b""
    return data if data else ctx.stdout.head


def container_format_for(ctx: DigestContext) -> str | None:
    """Return refined container format when ``ctx`` holds a valid inspectable
    container, else None."""
    if from_intake(ctx) and not ctx.stdout.media_type.startswith("application/octet-stream"):
        return None
    head_fmt = binfmt.sniff_format(ctx.stdout.head)
    if head_fmt not in _CONTAINER_HEAD_FORMATS:
        return None
    data = _blob_bytes(ctx)
    return containers.sniff_container(data)


class _ContainerProfile(Profile):
    formats: frozenset[str] = frozenset()

    def detect(self, ctx: DigestContext) -> str | None:
        fmt = container_format_for(ctx)
        if fmt is not None and fmt in self.formats:
            return f"structured binary container ({fmt})"
        return None

    def render(self, ctx: DigestContext) -> str:
        data = _blob_bytes(ctx)
        fmt = containers.sniff_container(data)
        info = containers.inspect_container(data, fmt)
        if info is None:
            info = {"format": fmt or "binary", "bytes": len(data), "error": "unreadable container"}
        header = (
            [ctx.intake_line(sizes=False)]
            if from_intake(ctx)
            else [f"cwd: {ctx.manifest['cwd']}", f"command: {ctx.command_line()}"]
        )
        handle = "run:PENDING#stdout"
        policy = ctx.ws.config.redaction

        def build(level: int) -> str:
            body, sugg, notes = containers.render_container_body(
                info,
                address=handle,
                policy=policy,
                header=header,
                level=level,
                focus_terms=ctx.focus_terms,
            )
            return "\n".join(body + self.next_lines(ctx, sugg) + notes)

        return fit(ctx, build, 5)


class OoxmlProfile(_ContainerProfile):
    version = "ooxml/v1"
    formats = containers.OOXML_FORMATS


class SqliteProfile(_ContainerProfile):
    version = "sqlite/v1"
    formats = containers.SQLITE_FORMATS


class ArchiveProfile(_ContainerProfile):
    version = "archive/v1"
    formats = containers.ARCHIVE_FORMATS


__all__ = ["ArchiveProfile", "OoxmlProfile", "SqliteProfile", "container_format_for"]
