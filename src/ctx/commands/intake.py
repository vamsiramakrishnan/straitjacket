"""`ctx intake` — store pasted text, a file, stdin or the clipboard; print a digest."""

from __future__ import annotations

import sys


def _err(msg: str) -> None:
    print(f"ctx intake: {msg}", file=sys.stderr)


def cmd_intake(ws, ns) -> int:
    import json

    from ctx.intake import (
        ClipboardUnavailable,
        IntakeError,
        intake_bytes,
        intake_path,
        read_clipboard,
        render_chunk_page,
        write_clipboard,
    )
    from ctx.store import Store, UnknownIdError
    from ctx.textutil import write_exact

    store = Store(ws.workspace_id, retention_days=ws.config.store.retention_days)

    if ns.chunks is not None:
        if ns.path is not None or ns.clip:
            _err("--chunks lists a stored handle; it takes no PATH or --clip")
            return 2
        try:
            write_exact(render_chunk_page(store, ws, ns.chunks, ns.start))
        except (IntakeError, UnknownIdError) as exc:
            _err(str(exc))
            return 1
        return 0
    if ns.start != 1:
        _err("--from pages a chunk listing; use it with --chunks HANDLE")
        return 2

    label = ns.label
    try:
        if ns.clip:
            if ns.path is not None:
                _err("--clip reads the clipboard; it takes no PATH")
                return 2
            tool, data = read_clipboard()
            receipt = intake_bytes(store, ws, data, label=label or "clipboard",
                                   name=ns.name, focus=ns.focus)
        elif ns.path is not None and ns.path != "-":
            receipt = intake_path(store, ws, ns.path, label=label or "file",
                                  name=ns.name, focus=ns.focus)
        else:
            if ns.path is None and sys.stdin.isatty():
                _err("nothing to intake: give a PATH, pipe into stdin (or pass -), or use --clip")
                return 2
            data = sys.stdin.buffer.read()
            receipt = intake_bytes(store, ws, data, label=label or "paste",
                                   name=ns.name, focus=ns.focus)
    except ClipboardUnavailable as exc:
        _err(f"--clip: {exc}")
        return 1
    except IntakeError as exc:
        _err(str(exc))
        return 1

    copied: str | None = None
    if ns.copy:
        try:
            copied = write_clipboard(receipt.digest)
        except ClipboardUnavailable as exc:
            # Declared, not fatal: the digest is printed either way.
            _err(f"--copy: {exc}; the digest is printed only")

    if ns.json:
        doc = receipt.to_json()
        if ns.copy:
            doc["copied"] = copied
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        write_exact(receipt.digest, newline=not receipt.digest.endswith("\n"))
        if receipt.passthrough:
            # The payload was smaller than a digest of it, so it was printed
            # as-is; the handle would otherwise be invisible.
            _err(f"stored whole as {receipt.address} (printed as-is: smaller than a digest)")
    if copied:
        _err(f"digest copied to the clipboard ({copied}); paste it instead of the payload")
    return 0


__all__ = ["cmd_intake"]
