"""One parser for ``git status --porcelain`` (format v1).

Six call sites used to spawn ``git status --porcelain``; three of them
carried their own ad-hoc parser and the three disagreed. The disagreements
were not cosmetic — this feeds change detection, so a parser that drops or
mangles a path makes the harness miss a real edit:

* **Quoted paths were never unescaped.** Git quotes a path (C style, per
  ``quote.c``) whenever it contains a space, a quote, a backslash, a control
  character, or — with the default ``core.quotePath=true`` — any non-ASCII
  byte. The old parsers stripped the surrounding quotes and stopped there, so
  ``?? "caf\\303\\251.py"`` became the literal 14-character string
  ``caf\\303\\251.py``: a path that does not exist. Downstream that silently
  became "no such file" (fingerprint/generation blind to its edits) or a
  bogus entry in the changed-file set.

* **``" -> "`` was split out of every entry, not just renames.** Porcelain
  v1 only writes ``ORIG -> PATH`` when the status is a rename or copy
  (``R``/``C``). Splitting unconditionally corrupted any path that happens to
  contain the four characters ``" -> "`` — e.g. the untracked file
  ``a -> b.txt`` was reported as ``b.txt"``.

* **Untracked-only parsers ignored renames entirely**, which is correct for
  their purpose but meant "the parser" behaved differently depending on which
  copy ran.

The documented v1 record is ``XY<space>PATH`` where ``XY`` is exactly two
status characters (``??`` for untracked, ``!!`` for ignored). Records are
newline-terminated; embedded newlines cannot survive unquoted, so splitting
on ``\\n`` is safe.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PorcelainEntry", "unquote_bytes", "parse", "changed_paths"]

# git quote.c: the escapes emitted by quote_c_style().
_ESCAPES = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("t"): 0x09,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
    ord('"'): 0x22,
}


@dataclass(frozen=True)
class PorcelainEntry:
    """One record of ``git status --porcelain`` v1.

    ``path`` is the *current* path — the destination side of a rename/copy,
    which is the one that changed on disk. ``orig_path`` keeps the source
    side for ``R``/``C`` records and is None otherwise. Both are decoded and
    unescaped; an untracked directory keeps its trailing ``/`` exactly as git
    wrote it (porcelain collapses a wholly-untracked directory to one entry).
    """

    x: str
    y: str
    path: str
    orig_path: str | None = None

    @property
    def untracked(self) -> bool:
        return self.x == "?" and self.y == "?"

    @property
    def ignored(self) -> bool:
        return self.x == "!" and self.y == "!"

    @property
    def renamed(self) -> bool:
        return self.orig_path is not None

    @property
    def is_dir_entry(self) -> bool:
        """Porcelain collapsed a whole untracked/ignored directory to one
        record; the caller must walk it or edits inside stay invisible."""
        return self.path.endswith("/")


def unquote_bytes(field: bytes) -> bytes:
    """Undo git's C-style path quoting. Non-quoted input is returned as-is."""
    if not field.startswith(b'"'):
        return field
    out = bytearray()
    i = 1
    n = len(field)
    while i < n:
        c = field[i]
        if c == 0x22:  # closing quote
            break
        if c != 0x5C:  # ordinary byte
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = field[i]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 1
        elif 0x30 <= e <= 0x37:  # 1..3 octal digits
            val = 0
            digits = 0
            while digits < 3 and i < n and 0x30 <= field[i] <= 0x37:
                val = val * 8 + (field[i] - 0x30)
                i += 1
                digits += 1
            out.append(val & 0xFF)
        else:  # not a git-emitted escape; keep the byte verbatim
            out.append(e)
            i += 1
    return bytes(out)


def _decode(raw: bytes) -> str:
    """Bytes → str that still round-trips to the filesystem. UTF-8 when it
    decodes (the overwhelming case), surrogateescape otherwise, so a
    non-UTF-8 filename can still be opened rather than being replaced by
    U+FFFD and turned into a path that does not exist."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "surrogateescape")


def _split_rename(field: bytes) -> tuple[bytes, bytes]:
    """``ORIG -> PATH`` → the two still-quoted sides. Each side is quoted
    independently by git, so the separator is found *after* the first side's
    closing quote — never by a blind search that a path containing
    ``" -> "`` would win."""
    if field.startswith(b'"'):
        i = 1
        n = len(field)
        while i < n:
            if field[i] == 0x5C:
                i += 2
                continue
            if field[i] == 0x22:
                break
            i += 1
        head, tail = field[: i + 1], field[i + 1 :]
        if tail.startswith(b" -> "):
            return head, tail[4:]
        return head, tail
    orig, sep, new = field.partition(b" -> ")
    if not sep:
        return field, b""
    return orig, new


def parse(raw: bytes) -> list[PorcelainEntry]:
    """Parse raw ``git status --porcelain`` (v1) bytes. Never raises;
    malformed records are skipped rather than guessed at."""
    entries: list[PorcelainEntry] = []
    for line in raw.split(b"\n"):
        if len(line) < 4 or line[2] != 0x20:
            continue
        x = chr(line[0])
        y = chr(line[1])
        field = line[3:]
        orig_raw: bytes | None = None
        if x in ("R", "C") or y in ("R", "C"):
            orig_raw, field = _split_rename(field)
        if not field:
            continue
        entries.append(
            PorcelainEntry(
                x=x,
                y=y,
                path=_decode(unquote_bytes(field)),
                orig_path=_decode(unquote_bytes(orig_raw)) if orig_raw else None,
            )
        )
    return entries


def changed_paths(
    raw: bytes,
    *,
    untracked_only: bool = False,
    exclude_top: str | None = None,
) -> list[str]:
    """Repo-relative paths named by a porcelain snapshot, in git's order.

    Renames/copies contribute their destination path (the side that exists on
    disk now). ``exclude_top`` drops entries whose first path component
    matches — the harness's own ledger directory, which mutates on every
    scored command and would otherwise bump every generation.
    """
    out: list[str] = []
    for e in parse(raw):
        if untracked_only and not e.untracked:
            continue
        if e.ignored:
            continue
        rel = e.path
        if exclude_top and rel.rstrip("/").split("/")[0] == exclude_top:
            continue
        out.append(rel)
    return out
