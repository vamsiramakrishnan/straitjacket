"""udiff/v1 -- a purpose-built view of a pasted plain unified diff.

Applies ONLY to manifests created by :mod:`ctx.intake` (gated on
:func:`ctx.digest.base.from_intake`), and only when the payload is too large
for ``text/v1`` to inline. Command output never reaches this profile, so every
existing command digest stays byte-identical.

The existing ``gitdiff/v1`` profile claims ``diff --git `` output and git
commands. A diff pasted from an editor, a review tool, ``diff -u`` or a patch
email has no such header -- only ``--- old`` / ``+++ new`` / ``@@ ... @@`` --
and used to fall to the generic chunk map. This profile gives it the same
kind of view (files, added and removed line counts, the largest changes) and
a resolvable span per file.

Why not reuse ``gitdiff/v1``'s renderer: it is keyed on ``diff --git a/x b/x``
lines (there are none here), counts ``+``/``-`` lines by prefix over the whole
text (so a removed line that starts ``-- `` is taken for a file header and a
trailing email signature for a deletion), sorts its "largest changes" by path
rather than by size, has no span per file, and renders paths unquoted.
Correcting any of that would change the bytes of command-output digests that
golden tests and published receipts pin; this module leaves it untouched.

Counting follows the hunk headers: after ``@@ -a,b +c,d @@`` exactly ``b`` old
and ``d`` new lines belong to the hunk, so a deleted line that reads ``-- x``
is a deletion, not a file header, and prose after the last hunk is not a
change. Lines that belong to no header or hunk are counted as outside.

Shape::

    intake: <label> ... · unified diff
    diff: <F> files · +<A> -<D> · <H> hunks (counted from the @@ headers) · L1-L<n>
    largest changes (by lines changed):
      ±62 (+40 -22) "src/a.py" · modified · 3 hunks · L10-L180 · span <id>
    other files (k): "b.py" ±3 · "c.py" ±1
    coverage: ...
    next:
      ctx get <handle> --span <id of the largest file>

Every path is quoted by :class:`ctx.digest.shapeutil.Quoter`, and the digest
is fitted to its budget on the text as it is emitted.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import LINE_EXAMINE_CAP, Quoter, Windows, ascii_digits, fit, inline_limit, pct, plain_head
from ctx.textutil import _ANSI_RE, fmt_bytes, fmt_int

DOMINANCE_PERCENT = 60
_EXTENDED = (
    "diff ", "index ", "new file mode", "deleted file mode", "old mode", "new mode",
    "similarity index", "dissimilarity index", "rename from", "rename to", "copy from",
    "copy to", "Binary files ", "GIT binary patch", "Only in ",
)
_DEV_NULL = "/dev/null"


@dataclass
class DiffFile:
    old: str
    new: str
    start: int                 # first line of the file's entry (1-based)
    end: int                   # last line that belongs to the file's headers or hunks
    hunks: int = 0
    adds: int = 0
    dels: int = 0
    short: int = 0             # hunks with fewer lines than their header counts
    binary: bool = False       # "Binary files ... differ" / "GIT binary patch"
    git: bool = False          # opened by a ``diff --git`` line
    headed: bool = False       # has its ---/+++ pair
    created: bool = False      # "new file mode"
    removed: bool = False      # "deleted file mode"
    moved: bool = False        # "rename from/to", "copy from/to": explicit evidence
    mode: bool = False         # "old mode" / "new mode"

    @property
    def path(self) -> str:
        return self.old if (self.new == _DEV_NULL or not self.new) else self.new

    @property
    def status(self) -> str:
        """added / deleted / renamed / modified, from evidence: ``/dev/null``
        or the git ``new/deleted file mode`` lines for the first two, git's
        own ``rename from`` lines for the third. Two paths that merely differ
        (``diff -ru old new`` prints ``old/x`` and ``new/x`` for EVERY file)
        are not a rename."""
        if self.old == _DEV_NULL or self.created:
            base = "added"
        elif self.new == _DEV_NULL or self.removed:
            base = "deleted"
        elif self.moved:
            base = "renamed"
        elif self.hunks == 0 and not self.binary and self.mode:
            base = "mode change only"
        else:
            base = "modified"
        return base + (", binary" if self.binary else "")

    @property
    def changed(self) -> int:
        return self.adds + self.dels


@dataclass(frozen=True)
class DiffParse:
    files: tuple[DiffFile, ...]
    structural: int
    nonblank: int
    lines: int
    hunks: int
    adds: int
    dels: int
    short: int


_DAYS = ("Mon ", "Tue ", "Wed ", "Thu ", "Fri ", "Sat ", "Sun ")


def _header_path(text: str) -> str:
    """The path of a ``---``/``+++`` header line's remainder. Diff tools end it
    with a tab and a timestamp (``2024-01-01 10:00:00`` or ``Thu Jan  1 ...``);
    that part is dropped ONLY when what follows the tab looks like one. A tab
    anywhere else is part of the name, and the name stays whole, so a token
    split by a tab is redacted whole instead of being cut at the tab."""
    head, tab, rest = text.partition("\t")
    if tab:
        r = rest.lstrip()
        if r[:1] in tuple("0123456789") or r.startswith(_DAYS):
            return head.strip()
    return text.strip()


def _normalise(old: str, new: str) -> tuple[str, str]:
    """Drop the ``a/`` ``b/`` prefixes only when both sides carry them, or
    when one side is ``/dev/null`` (a new or deleted file) and the other
    carries its own."""
    if old.startswith("a/") and new.startswith("b/"):
        return old[2:], new[2:]
    if old == _DEV_NULL and new.startswith("b/"):
        return old, new[2:]
    if new == _DEV_NULL and old.startswith("a/"):
        return old[2:], new
    return old, new


def _git_paths(rest: str) -> tuple[str, str]:
    """(old, new) of ``diff --git a/x b/y`` (the part after ``diff --git ``)."""
    k = rest.rfind(" b/")
    if rest.startswith("a/") and k > 0:
        return rest[2:k], rest[k + 3:]
    half = len(rest) // 2
    if len(rest) % 2 == 1 and rest[half] == " " and rest[:half] == rest[half + 1:]:
        return rest[:half], rest[half + 1:]
    return rest, rest


def _binary_paths(line: str) -> tuple[str, str]:
    """(old, new) of ``Binary files a/x and b/y differ``."""
    body = line[len("Binary files "):]
    body = body[: -len(" differ")] if body.endswith(" differ") else body
    old, sep, new = body.partition(" and ")
    return _normalise(old.strip(), new.strip() if sep else old.strip())


def _hunk_counts(header: str) -> tuple[int, int] | None:
    """(old_count, new_count) of ``@@ -a[,b] +c[,d] @@``; None if malformed."""
    parts = header.split(" ", 4)
    if len(parts) < 4 or parts[0] != "@@" or not parts[1].startswith("-") or not parts[2].startswith("+"):
        return None

    def count(spec: str) -> int | None:
        head, comma, tail = spec[1:].partition(",")
        if not comma:
            return 1 if ascii_digits(head) else None
        return int(tail[:9]) if ascii_digits(tail) and ascii_digits(head) else None

    old, new = count(parts[1]), count(parts[2])
    return (old, new) if old is not None and new is not None else None


@functools.lru_cache(maxsize=2)
def parse(text: str) -> DiffParse:
    """One linear pass; each line is classified on at most
    :data:`LINE_EXAMINE_CAP` characters, and every field (a path) is taken
    from the whole line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if "\x1b" in text:
        # ``git diff --color``: lines are recognised, and their fields taken,
        # as the terminal showed them. (Line numbers are unchanged.)
        lines = [_ANSI_RE.sub("", ln) if "\x1b" in ln else ln for ln in lines]
    n = len(lines)
    files: list[DiffFile] = []
    structural = nonblank = 0
    cur: DiffFile | None = None
    cap = LINE_EXAMINE_CAP

    def open_file(f: DiffFile) -> DiffFile:
        files.append(f)
        return f

    def awaiting() -> bool:
        """The open file came from a ``diff --git`` line and has seen neither
        its ---/+++ pair, a hunk nor a binary marker yet."""
        return cur is not None and cur.git and not cur.headed and cur.hunks == 0 and not cur.binary

    i = 0
    while i < n:
        s = lines[i]
        if s.endswith("\r"):
            s = s[:-1]
        t = s[:cap]
        if not t.strip() and len(s) <= cap:
            i += 1
            continue
        nonblank += 1
        if t.startswith("diff --git "):
            old, new = _git_paths(s[len("diff --git "):])
            cur = open_file(DiffFile(old, new, i + 1, i + 1, git=True))
            structural += 1
            i += 1
            continue
        if t.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old = _header_path(s[4:])
            new = _header_path(lines[i + 1][4:])
            old, new = _normalise(old, new)
            if awaiting():
                cur.old, cur.new, cur.headed, cur.end = old, new, True, i + 2
            else:
                cur = open_file(DiffFile(old, new, i + 1, i + 2, headed=True))
            structural += 2
            nonblank += 1
            i += 2
            continue
        if t.startswith("Binary files "):
            old, new = _binary_paths(s)
            if awaiting():
                cur.binary, cur.end = True, i + 1
            else:
                cur = open_file(DiffFile(old, new, i + 1, i + 1, binary=True))
            structural += 1
            i += 1
            continue
        if t.startswith("GIT binary patch"):
            if cur is None:
                cur = open_file(DiffFile("", "", i + 1, i + 1, binary=True, git=True))
            cur.binary = True
            structural += 1
            cur.end = i + 1
            i += 1
            while i < n and lines[i].strip():       # the base-85 payload, up to the blank line
                nonblank += 1
                structural += 1
                cur.end = i + 1
                i += 1
            continue
        if cur is not None and t.startswith("@@ "):
            counts = _hunk_counts(t)
            if counts is not None:
                old_rem, new_rem = counts
                cur.hunks += 1
                structural += 1
                cur.end = i + 1
                i += 1
                while i < n and (old_rem > 0 or new_rem > 0):
                    body = lines[i]
                    c = body[:1]
                    if c == " " or body == "":
                        old_rem -= 1
                        new_rem -= 1
                    elif c == "-":
                        # An unambiguous file header for what follows ends the
                        # hunk, whatever its own header counted.
                        if body.startswith("--- ") and i + 2 < n and lines[i + 1].startswith("+++ ") \
                                and lines[i + 2].startswith("@@ "):
                            break
                        old_rem -= 1
                        cur.dels += 1
                    elif c == "+":
                        new_rem -= 1
                        cur.adds += 1
                    elif c == "\\":
                        pass
                    else:
                        break      # "@@", "diff ...", prose: not a hunk line
                    if body.strip():   # a blank context line is counted for the hunk, not as content
                        nonblank += 1
                        structural += 1
                    cur.end = i + 1
                    i += 1
                if old_rem > 0 or new_rem > 0:
                    cur.short += 1
                while i < n and lines[i].startswith("\\"):   # "\ No newline at end of file"
                    nonblank += 1
                    structural += 1
                    cur.end = i + 1
                    i += 1
                continue
        if t.startswith(_EXTENDED):
            structural += 1
            if cur is not None and cur.git:
                cur.end = i + 1
                if t.startswith("new file mode"):
                    cur.created = True
                elif t.startswith("deleted file mode"):
                    cur.removed = True
                elif t.startswith(("rename from", "rename to", "copy from", "copy to")):
                    cur.moved = True
                elif t.startswith(("old mode", "new mode")):
                    cur.mode = True
        i += 1
    return DiffParse(
        tuple(files), structural, nonblank, n,
        sum(f.hunks for f in files), sum(f.adds for f in files),
        sum(f.dels for f in files), sum(f.short for f in files),
    )


class UnifiedDiffProfile(Profile):
    version = "udiff/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        if not from_intake(ctx):
            return None
        view = ctx.stdout
        if ctx.stderr.bytes or not view.bytes or not view.text:
            return None
        if view.media_type.startswith("application/octet-stream"):
            return None
        if view.bytes <= inline_limit(ctx):
            return None
        text = mappable_text(view.text, view.parsed_fully)
        head = plain_head(text)
        if "\n+++ " not in head and not head.startswith("--- "):
            return None
        if "@@ " not in head:
            return None
        result = parse(text)
        if not result.files or result.hunks == 0 or result.nonblank == 0:
            return None
        if result.structural * 100 < DOMINANCE_PERCENT * result.nonblank:
            return None
        return (
            f"unified diff of {len(result.files)} files, {result.hunks} hunks: "
            f"{result.structural}/{result.nonblank} non-blank lines are diff structure "
            f"({pct(result.structural, result.nonblank)})"
        )

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        result = parse(text)
        files = result.files
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        parsed_lines = result.lines
        total_lines = view.lines
        scope = " in the parsed part" if partial else ""
        order = sorted(range(len(files)), key=lambda k: (-files[k].changed, k))
        spans: dict[tuple[int, int], str | None] = {}

        windows = Windows(ctx, text, total_lines)
        policy = ctx.ws.config.redaction

        def span(a: int, b: int) -> str | None:
            if (a, b) not in spans:
                spans[(a, b)] = ctx.mint_span(view, "region", a=a, b=b)
            return spans[(a, b)]

        def where(f: DiffFile) -> str:
            """A span only where it says true things: the exact lines when one
            retrieval holds them, a declared zoom digest past
            ``max_inline_lines``."""
            n = f.end - f.start + 1
            if windows.zoom(f.start, f.end):
                sid = span(f.start, f.end)
                return f"L{f.start}-L{f.end}" + (f" · span {sid}, {fmt_int(n)} lines: resolves as a zoom digest" if sid else "")
            if windows.exact(f.start, f.end):
                sid = span(f.start, f.end)
                return f"L{f.start}-L{f.end}" + (f" · span {sid}" if sid else "")
            return (f"L{f.start}-L{f.end} ({fmt_int(n)} lines, more than one exact retrieval holds: "
                    f"read it in --lines windows)")

        tail_from = parsed_lines + 1
        first = files[0]
        largest = files[order[0]]

        def build(level: int) -> str:
            path_cap = (70, 56, 44, 32, 24)[level]
            n_detail = (5, 4, 3, 2, 1)[level]
            n_other = (10, 6, 3, 0, 0)[level]
            q = Quoter(path_cap, policy)
            out = [ctx.intake_line(total=partial) + " · unified diff"]
            out.append(
                f"diff{scope}: {fmt_int(len(files))} files · +{fmt_int(result.adds)} -{fmt_int(result.dels)} · "
                f"{fmt_int(result.hunks)} hunks (line counts follow the @@ headers; see coverage) · "
                f"L{first.start}-L{files[-1].end}"
            )
            out.append("largest changes (by lines changed):")
            for k in order[:n_detail]:
                f = files[k]
                tail = ("" if f.binary and not f.hunks else f" · {f.hunks} hunk{'' if f.hunks == 1 else 's'}") + (
                    f" · {f.short} hunk{'' if f.short == 1 else 's'} shorter than its header" if f.short else "")
                out.append(
                    f"  ±{fmt_int(f.changed)} (+{fmt_int(f.adds)} -{fmt_int(f.dels)}) {q(f.path, path_cap, tail=True)} · "
                    f"{f.status}{tail} · {where(f)}"
                )
            rest = order[n_detail:]
            if rest and n_other:
                names = " · ".join(f"{q(files[k].path, path_cap, tail=True)} ±{fmt_int(files[k].changed)}" for k in rest[:n_other])
                more = f" · … +{fmt_int(len(rest) - n_other)} more" if len(rest) > n_other else ""
                out.append(f"other files ({fmt_int(len(rest))}, by size): {names}{more}")
            elif rest:
                out.append(f"other files: {fmt_int(len(rest))} not listed at this size")
            if len(rest) > n_other:
                out.append(f"every file header: ctx search {rid} '^([+][+][+]|Binary files) ' --context 0")
            outside = result.nonblank - result.structural
            if partial:
                cov = (
                    f"coverage: parsed L1-L{fmt_int(parsed_lines)} of {fmt_int(total_lines)} lines "
                    f"({fmt_bytes(len(text.encode('utf-8')))} of {fmt_bytes(view.bytes)}; parse cap "
                    f"{fmt_bytes(MAX_PARSE_BYTES)}), NOT parsed: L{fmt_int(tail_from)}-L{fmt_int(total_lines)}"
                )
            else:
                cov = f"coverage: all {fmt_int(total_lines)} lines parsed"
            cov += (
                f" · diff structure {fmt_int(result.structural)}/{fmt_int(result.nonblank)} non-blank lines, "
                f"{fmt_int(outside)} outside any file header or hunk · "
                f"{min(n_detail, len(files))}/{fmt_int(len(files))} files detailed"
            )
            if result.short:
                cov += (f" · {fmt_int(result.short)} hunk{'' if result.short == 1 else 's'} "
                        f"{'has' if result.short == 1 else 'have'} fewer lines than the @@ header says "
                        f"(a miscounted header or cut-off input; lines past it are not tallied)")
            if q.clipped:
                cov += f" · {q.clipped} path{'' if q.clipped == 1 else 's'} clipped to {path_cap} chars"
            out.append(cov)
            sugg = []
            usable = windows.zoom(largest.start, largest.end) or windows.exact(largest.start, largest.end)
            sid = span(largest.start, largest.end) if usable else None
            sugg.append(f"ctx get {handle} --span {sid}" if sid
                        else f"ctx get {handle} --lines {largest.start}:{windows.fit_end(largest.start, largest.end)}")
            if partial:
                sugg.append(f"ctx get {handle} --lines {tail_from}:{tail_from + 39}")
            sugg.append(f"ctx search {rid} '^@@ ' --context 0")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["UnifiedDiffProfile", "parse"]
