"""Intake: artifactize content ctx does not govern, once, at first entry.

Everything else enters the store as the output of a command ctx ran or a
tool result a hook caught. A pasted 3 MB log, a JSON dump or a long document
reached the conversation with no such door, so it flooded the window. Intake
is that door: raw bytes in, a bounded digest and a retrieval handle out.

* **Text** goes through the existing digest machinery
  (:func:`ctx.digest.synthesize_manifest` + :func:`ctx.digest.render_run_digest`),
  never a second digester. Shape profiles (JSON, logs, …) apply as they do to
  tool output; a large payload none of them claims gets the intake-only
  ``chunkmap/v1`` table instead of a head/tail window. The manifest is
  stamped with :data:`ctx.digest.base.INTAKE_TOOL` so profiles can tell
  intake from command output with :func:`ctx.digest.base.from_intake`.
* **Binary** goes to the store as a leased blob and is described by
  :func:`describe_binary` -- the ONE seam a media package replaces.
* **Identity** is a pure function of (bytes, label, name): the same payload
  under the same label mints the same handle and byte-identical digest bytes.
  The session's task query never steers it (focus is explicit or empty) and
  the engagement filter that trims ``next:`` lines for passive sessions is
  not applied: the handle and its continuations are the whole point of an
  intake, and a filter that depends on session state would break identity.
* **A digest pays for itself**: a payload smaller than the scaffolding that
  would wrap it is delivered verbatim (still redacted, still stored and
  addressable through the receipt).

Nothing here calls a model.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.digest.base import INTAKE_TOOL
from ctx.store import Store
from ctx.textutil import (
    bounded,
    encode_exact,
    estimate_tokens,
    fmt_bytes,
    fmt_int,
    sanitize_for_model,
    short_id,
)
from ctx.workspace import Workspace

INTAKE_SCHEMA = "ctx.intake/v1"
#: Lease reason on everything intake stores. Distinct from ``retention`` so
#: an explicit ``ctx gc --retention-days N`` (which overrides the default
#: retention leases) does not collect a payload the user handed over on
#: purpose; it still expires after the configured retention window.
LEASE_REASON = "intake"

_SAFE_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_LABEL_CHARS = 48


class IntakeError(Exception):
    """A declared refusal: nothing was stored. The message says why."""


class ClipboardUnavailable(IntakeError):
    """No clipboard tool is installed, or every one that is failed."""


def _safe_token(value: str | None, default: str | None) -> str | None:
    """Labels and names ride in argv and in model-visible text, so they are
    reduced to a shell- and profile-inert alphabet: no whitespace, slash,
    ``=`` or quote, which also keeps argv-anchored profiles (pytest, grep,
    cargo…) from mistaking a label for a command."""
    if value is None:
        return default
    token = _SAFE_RE.sub("_", value).strip("._")[:_LABEL_CHARS]
    return token or default


@dataclass(frozen=True)
class IntakeReceipt:
    kind: str                 # "text", or a binfmt format (png, pdf, zip, binary, …)
    address: str              # run:<short>#stdout for text, blob:<short> for binary
    label: str
    name: str | None
    bytes: int
    lines: int | None         # None for binary
    raw_tokens: int           # estimated tokens if the payload were inlined raw
    delivered_tokens: int     # estimated tokens of ``digest``
    profile: str              # digest profile that rendered it
    digest: str               # the bounded, redacted, model-visible text
    passthrough: bool = False  # True when ``digest`` is the payload itself
    lossy: bool = False       # True when invalid UTF-8 was replaced before storing
    encoding: str = "utf-8"   # source text encoding; stored as UTF-8 either way

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": INTAKE_SCHEMA,
            "kind": self.kind,
            "address": self.address,
            "label": self.label,
            "name": self.name,
            "bytes": self.bytes,
            "lines": self.lines,
            "tokens": {"raw": self.raw_tokens, "delivered": self.delivered_tokens},
            "profile": self.profile,
            "passthrough": self.passthrough,
            "lossy": self.lossy,
            "encoding": self.encoding,
            "digest": self.digest,
        }


# ------------------------------------------------------------- classification
_UTF16_BOMS = ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def _utf16_text(data: bytes) -> tuple[str, str] | None:
    """(text, encoding) for byte-order-marked UTF-16 that decodes strictly --
    what Windows tools and PowerShell redirection write -- else None. Its NUL
    bytes would otherwise make it look binary."""
    for bom, encoding in _UTF16_BOMS:
        if data.startswith(bom) and len(data) % 2 == 0:
            try:
                return data[len(bom):].decode(encoding), encoding
            except UnicodeDecodeError:
                return None
    return None


def binary_kind(data: bytes) -> str | None:
    """The binfmt format of a binary payload, or None for text.

    Magic bytes decide, except for BMP's two-byte ``BM`` magic, which plain
    text ("BMW…") starts with too: that is binary only if it also fails the
    text test. A NUL in the first 8 KiB (the window ``decode_stream`` uses)
    is binary of unknown format.
    """
    from ctx import binfmt

    if _utf16_text(data) is not None:
        return None
    fmt = binfmt.sniff_format(data[:1024])
    has_nul = b"\x00" in data[:8192]
    if fmt == "text":
        return "binary" if has_nul else None
    if fmt == "bmp" and not has_nul:
        try:
            data.decode("utf-8")
            return None
        except UnicodeDecodeError:
            pass
    return fmt


# ------------------------------------------------------------------ the seam
def describe_binary(
    store: Store, ws: Workspace, data: bytes, *, kind: str, label: str, name: str | None
) -> IntakeReceipt:
    """Store a binary payload under a lease and describe it.

    THE SEAM for media: a later package replaces this body (image/video
    handling) and nothing else in intake needs to change. Contract it must
    keep: the bytes are stored losslessly and leased (:data:`LEASE_REASON`),
    the digest is deterministic, bounded by ``digest_tokens`` and sanitized,
    and the address resolves through ``ctx get``.
    """
    from ctx import binfmt

    blob = store.put_blob(data)
    store.lease(blob, LEASE_REASON, ttl_days=store.retention_days)
    address = f"blob:{short_id(blob)}"
    header = f"[ctx {address} profile=binary/v1]"
    lines = [header, f"intake: {label}" + (f" · {name}" if name else "")]
    lines.append(binfmt.render_digest(binfmt.inspect(data), address=address))
    text, redactions = sanitize_for_model("\n".join(lines), ws.config.redaction)
    if redactions:
        text += "\nredaction: applied [" + ", ".join(redactions) + "]"
    text = bounded(text, ws.config.budgets.digest_tokens,
                   truncation_continuation=f"ctx get {address} --bytes 1:64")
    return IntakeReceipt(
        kind=kind, address=address, label=label, name=name, bytes=len(data),
        lines=None, raw_tokens=estimate_tokens(len(data)),
        delivered_tokens=estimate_tokens(len(encode_exact(text))),
        profile="binary/v1", digest=text,
    )


# ------------------------------------------------------------------- intake
def intake_bytes(
    store: Store,
    ws: Workspace,
    data: bytes,
    *,
    label: str = "paste",
    name: str | None = None,
    focus: str | None = None,
) -> IntakeReceipt:
    """Store ``data`` and return its receipt (bounded digest + handle)."""
    if not data:
        raise IntakeError("nothing to intake: the payload is empty")
    label = _safe_token(label, "paste") or "paste"
    name = _safe_token(Path(name).name if name else None, None)

    kind = binary_kind(data)
    if kind is not None:
        return describe_binary(store, ws, data, kind=kind, label=label, name=name)

    from ctx.digest import render_run_digest, synthesize_manifest

    encoding = "utf-8"
    utf16 = _utf16_text(data)
    if utf16 is not None:
        # Stored as its UTF-8 transcoding (the store's text form); the
        # source encoding is part of identity and named in the digest.
        (text, encoding), lossy = utf16, False
    else:
        try:
            text, lossy = data.decode("utf-8"), False
        except UnicodeDecodeError:
            # The digest machinery stores text; a lossy view is declared,
            # never silent. The receipt says so; the stored blob is that view.
            text, lossy = data.decode("utf-8", "replace"), True

    argv = [INTAKE_TOOL, f"label:{label}"] + ([f"name:{name}"] if name else [])
    if encoding != "utf-8":
        argv.append(f"encoding:{encoding}")
    manifest = synthesize_manifest(store, ws, INTAKE_TOOL, text, "", argv=argv)
    digest, final = render_run_digest(
        store, ws, manifest,
        # Explicit, never None: None lets text/v1 borrow the session's task
        # query as focus, which would make the digest a function of session
        # state rather than of (bytes, label, name).
        focus=focus if focus is not None else "",
        op="intake",
    )
    run_id = str(final["id"]).removeprefix("sha256:")
    store.lease(run_id, LEASE_REASON, ttl_days=store.retention_days)
    short = short_id(run_id)
    address = f"run:{short}#stdout"
    profile = str(final["digest"]["profile"])
    stream = final["streams"]["stdout"]

    budgets = ws.config.budgets
    budget = budgets.result_tokens if "output (complete):" in digest else budgets.digest_tokens
    digest = bounded(digest, budget, truncation_continuation=f"ctx get {address}")

    passthrough = False
    if "output (complete):" in digest:
        # A digest must pay for itself: when the wrapping costs more than the
        # content it wraps, deliver the content (redacted, like every
        # model-visible byte). The handle stays in the receipt.
        plain, redactions = sanitize_for_model(text, ws.config.redaction)
        if redactions:
            plain += "\nredaction: applied [" + ", ".join(redactions) + "]"
        scaffold = len(encode_exact(digest)) - len(encode_exact(plain))
        if scaffold >= len(encode_exact(plain)):
            digest, passthrough = plain, True

    return IntakeReceipt(
        kind="text", address=address, label=label, name=name,
        bytes=int(stream["bytes"]), lines=int(stream["lines"]),
        raw_tokens=estimate_tokens(int(stream["bytes"])),
        delivered_tokens=estimate_tokens(len(encode_exact(digest))),
        profile=profile, digest=digest, passthrough=passthrough, lossy=lossy,
        encoding=encoding,
    )


def intake_path(
    store: Store,
    ws: Workspace,
    path: str | os.PathLike[str],
    *,
    label: str = "file",
    name: str | None = None,
    focus: str | None = None,
) -> IntakeReceipt:
    """Intake a file, under the same confinement ``ctx image`` applies.

    The path must resolve inside the workspace (unless the workspace config
    sets ``allow_outside_root``) and must not be excluded by the ignore /
    secret-path policy under either its own name or its target's. A file
    elsewhere -- a download, an attachment -- is handed over explicitly on
    stdin instead (``ctx intake - < FILE``), which keeps the decision to read
    an out-of-tree file with the person or agent typing the redirection.
    """
    from ctx.workspace import PathEscapeError, WorkspaceError

    try:
        full = ws.confine(str(path), must_exist=True)
    except PathEscapeError as exc:
        raise IntakeError(
            f"{exc}; to intake a file outside the workspace, pipe it: ctx intake - < FILE"
        ) from None
    except WorkspaceError as exc:
        raise IntakeError(str(exc)) from None
    asked = ws.relativize_as_asked(str(path))
    for candidate in (asked, ws.relativize(full)):
        if ws.is_ignored(candidate):
            raise IntakeError(f"path is excluded from capture by policy: {candidate}")
    if not full.is_file():
        raise IntakeError(f"not a file: {asked}")
    try:
        data = full.read_bytes()
    except OSError as exc:
        raise IntakeError(f"cannot read {asked}: {exc.strerror or exc}") from None
    return intake_bytes(store, ws, data, label=label,
                        name=name if name is not None else Path(asked).name, focus=focus)


# ------------------------------------------------------------- chunk paging
def render_chunk_page(store: Store, ws: Workspace, ref_text: str, start: int = 1) -> str:
    """Every chunk of a stored text stream, one bounded page at a time.

    The digest lists the first, last and signal-bearing chunks; this lists
    all of them from ``start`` on, with the same span ids, until the result
    budget is spent, and names the next page. Works on any text ``run:``
    stream or ``blob:``, not only on intakes.
    """
    from ctx import bounds
    from ctx.digest.base import MAX_PARSE_BYTES
    from ctx.digest.intakeprof import (
        build_map, chunk_row, mappable_text, mint_chunk_span, unmapped_tail,
    )
    from ctx.refs import RefError, parse_ref

    try:
        ref = parse_ref(ref_text)
    except RefError as exc:
        raise IntakeError(str(exc)) from None
    if ref.workspace_alias:
        raise IntakeError("--chunks does not follow ws: aliases; run it in that workspace")
    if ref.kind == "run":
        manifest = store.get_manifest(ref.id or "")
        stream = ref.stream or "stdout"
        meta = manifest["streams"].get(stream)
        if meta is None:
            raise IntakeError(f"run:{short_id(manifest['id'])} has no stream {stream!r}")
        blob = store.resolve_id(str(meta["blob"]).removeprefix("sha256:"), kinds=("blob",))
        handle = f"run:{short_id(manifest['id'])}#{stream}"
    elif ref.kind == "blob":
        blob = store.resolve_id(ref.id or "", kinds=("blob",))
        handle = f"blob:{short_id(blob)}"
    else:
        raise IntakeError("--chunks takes a run: or blob: handle")

    path = store.blob_path(blob)
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(MAX_PARSE_BYTES)
    if b"\x00" in data[:8192]:
        raise IntakeError(f"{handle} is binary: it has no chunk map; use ctx get {handle} --bytes A:B")
    if ref.kind == "run":
        total_lines = int(meta["lines"])
    else:
        total_lines = data.count(b"\n") + (0 if (not data or data.endswith(b"\n")) else 1)
    budgets = ws.config.budgets
    policy = ws.config.redaction
    partial = size > MAX_PARSE_BYTES
    text = mappable_text(data.decode("utf-8", "replace"), not partial)
    chunks = build_map(text, total_lines, budgets)
    n = len(chunks)
    if n == 0:
        return f"[ctx intake chunks {handle}]\nchunks: 0 (empty)"
    if start < 1 or start > n:
        raise IntakeError(f"--from {start} is outside the map: chunks are #1-#{n}")

    def register(kind: str, **kw: Any) -> str:
        return store.register_span(blob, kind, **kw)

    scope = ""
    if partial:
        tail_text, tail_cmd = unmapped_tail(text, chunks, total_lines, size, handle, budgets)
        scope = (
            f" · mapped: bytes 1-{fmt_int(chunks[-1].end_byte)} · {tail_text}, "
            f"reach it with {tail_cmd}"
        )

    # Rows are redacted one by one as they are added, so the page is fitted
    # on the bytes it will emit, and the continuation names the chunk after
    # the last row actually emitted.
    limit = bounds.budget_bytes(budgets.result_tokens)
    rows: list[str] = []
    fired: set[str] = set()

    def render(rows: list[str], fired: set[str]) -> str:
        end = start + len(rows) - 1
        lines = [
            f"[ctx intake chunks {handle}]",
            f"chunks: {fmt_int(n)} · showing #{start}-#{end} (! rows show their signal line){scope}",
            *rows,
        ]
        if fired:
            lines.append("redaction: applied [" + ", ".join(sorted(fired)) + "]")
        if end < n:
            lines.append(f"next: ctx intake --chunks {handle} --from {end + 1}")
        return "\n".join(lines)

    for chunk in chunks[start - 1:]:
        row, names = sanitize_for_model(chunk_row(chunk, mint_chunk_span(register, chunk)), policy)
        trial_fired = fired | set(names)
        if rows and len(encode_exact(render(rows + [row], trial_fired))) > limit:
            break
        rows.append(row)
        fired = trial_fired
    return render(rows, fired)


# ---------------------------------------------------------------- clipboard
# (tool, argv) in probe order. Each is tried only when installed; one that is
# installed but fails (wl-paste outside Wayland, xclip without a display)
# falls through to the next. Absence of all of them is a declared outcome.
CLIPBOARD_READERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pbpaste", ("pbpaste",)),
    ("wl-paste", ("wl-paste", "--no-newline")),
    ("xclip", ("xclip", "-selection", "clipboard", "-out")),
    ("xsel", ("xsel", "--clipboard", "--output")),
    ("powershell", ("powershell", "-NoProfile", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw")),
    ("powershell.exe", ("powershell.exe", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw")),
)
CLIPBOARD_WRITERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pbcopy", ("pbcopy",)),
    ("wl-copy", ("wl-copy",)),
    ("xclip", ("xclip", "-selection", "clipboard", "-in")),
    ("xsel", ("xsel", "--clipboard", "--input")),
    ("powershell", ("powershell", "-NoProfile", "-Command",
                    "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())")),
    ("powershell.exe", ("powershell.exe", "-NoProfile", "-Command",
                        "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())")),
)
_CLIPBOARD_TIMEOUT = 10

# Indirection points so tests can stand in for the external tools.
_which = shutil.which
_run = subprocess.run


def _try_tools(table, payload: bytes | None) -> tuple[str, bytes]:
    tried: list[str] = []
    for tool, argv in table:
        exe = _which(argv[0])
        if not exe:
            continue
        tried.append(tool)
        try:
            proc = _run([exe, *argv[1:]], input=payload, capture_output=True,
                        timeout=_CLIPBOARD_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return tool, proc.stdout or b""
    names = ", ".join(t for t, _ in table)
    if tried:
        raise ClipboardUnavailable(f"every clipboard tool failed (tried {', '.join(tried)})")
    raise ClipboardUnavailable(f"no clipboard tool found (looked for {names})")


def read_clipboard() -> tuple[str, bytes]:
    """(tool used, clipboard bytes). Raises :class:`ClipboardUnavailable`."""
    tool, data = _try_tools(CLIPBOARD_READERS, None)
    if tool.startswith("powershell"):
        data = data.replace(b"\r\n", b"\n")
    return tool, data


def write_clipboard(text: str) -> str:
    """Put ``text`` on the clipboard; returns the tool used. Raises
    :class:`ClipboardUnavailable`."""
    return _try_tools(CLIPBOARD_WRITERS, encode_exact(text))[0]


__all__ = [
    "INTAKE_SCHEMA", "LEASE_REASON", "IntakeError", "ClipboardUnavailable",
    "IntakeReceipt", "binary_kind", "describe_binary", "intake_bytes", "intake_path",
    "render_chunk_page", "read_clipboard", "write_clipboard",
]
