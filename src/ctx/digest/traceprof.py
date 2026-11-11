"""trace/v1 -- a purpose-built view of a pasted stack trace.

Applies ONLY to manifests created by :mod:`ctx.intake` (gated on
:func:`ctx.digest.base.from_intake`), and only when the payload is too large
for ``text/v1`` to inline. Command output never reaches this profile, so every
existing command digest stays byte-identical.

Decision the digest supports: *what failed, where in my own code, and is it a
chain or a recursion?* The loss that would make it unsafe: a final exception
that is not the one the program died of, a recursion rendered as a thousand
frames (or hidden entirely), or a "your code is here" claim presented as
certain when it is a guess.

Recognised (line-anchored, so a long log with one trace in it is NOT claimed
-- structure must dominate, see :data:`DOMINANCE_PERCENT`):

* Python ``Traceback (most recent call last):`` including chained traces
  ("During handling of the above exception..." / "The above exception was the
  direct cause...") and ``[Previous line repeated N more times]``;
* JavaScript / TypeScript ``Error: ...`` followed by ``    at fn (file:l:c)``;
* the Java family: ``\\tat pkg.Class.method(File.java:12)``, ``Caused by:``,
  ``... N more``;
* Go ``panic: ...`` / ``fatal error: ...`` with ``goroutine N [state]:`` frames;
* Rust ``thread 'x' panicked at ...`` with an optional backtrace.

Shape::

    intake: <label> ... · stack trace (<language>)
    traces: <N> · detail is for the LAST one (#N) L<a>-L<b> · span <id>
    exception: "<Type: message>" · L<n>
    chain: <k> links as printed ...                  (one line each)
    innermost frames (innermost first, <F> in this trace):
      #1 "file":<line> in "func" · L<n>
      #2-#1001 x500 cycle of 2 frames: ...            (recursion collapsed)
    your code (heuristic, a guess): ...
    files in frames: "a.py" x500 ...
    coverage: ...
    next:
      ctx get <handle> --lines <a>:<b>

Every string from the payload goes through :class:`ctx.digest.shapeutil.Quoter`
(single line, control characters removed, capped, quoted), and the digest is
fitted to its budget on the text as it is emitted.
"""

from __future__ import annotations

import functools
from collections import Counter
from dataclasses import dataclass, field

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import (
    LINE_EXAMINE_CAP, Quoter, Windows, ascii_digits, digits_in, fit, inline_limit, pct, plain_head,
)
from ctx.textutil import _ANSI_RE, fmt_bytes, fmt_int

#: A payload is claimed only when at least this share of its non-blank lines
#: is trace structure (headers, frames, source lines under frames).
DOMINANCE_PERCENT = 60
#: A frame cycle is reported when a block of at most this many frames repeats
#: at least ``MIN_CYCLE_REPS`` times in a row.
MAX_CYCLE_PERIOD = 8
MIN_CYCLE_REPS = 3
#: Cheap pre-filter: a payload with none of these cannot hold a trace this
#: profile recognises, and is declined without a line scan.
_MARKERS = (
    "Traceback (most recent call last):", "  at ", "\tat ", 'File "', "panic:",
    "goroutine ", "panicked at", "fatal error:",
)
# (frame path markers, lowercased, '/'-normalised) of library / runtime code.
_LIB_PATH_MARKERS = (
    "/site-packages/", "/dist-packages/", "/lib/python3", "<frozen ", "/node_modules/",
    "/usr/local/go/", "/usr/lib/go", "/go/src/runtime/", "/pkg/mod/", "/rustc/",
    "/.cargo/", "/.rustup/", "/library/std/", "/library/core/", "/library/alloc/",
    "/lib/x86_64-linux-gnu/", "/lib/ruby/",
)
_LIB_PATH_PREFIXES = ("node:", "internal/", "<frozen", "<string>", "<stdin>")
_LIB_FUNC_PREFIXES = (
    "java.", "javax.", "jakarta.", "jdk.", "sun.", "com.sun.", "kotlin.", "kotlinx.", "scala.",
    # common JVM frameworks and drivers: third-party, not "your code"
    "org.springframework.", "org.apache.", "org.hibernate.", "org.postgresql.", "org.mariadb.",
    "org.eclipse.", "org.jboss.", "org.glassfish.", "org.junit.", "org.slf4j.", "org.mockito.",
    "com.mysql.", "com.zaxxer.", "com.fasterxml.", "com.google.", "com.amazonaws.", "com.microsoft.",
    "com.oracle.", "io.netty.", "io.grpc.", "io.micrometer.", "io.vertx.", "io.reactivex.",
    "reactor.", "okhttp3.", "okio.", "ch.qos.", "net.sf.", "oracle.jdbc.", "feign.",
    "runtime.", "testing.", "syscall.", "reflect.", "net/http.", "std::", "core::",
    "alloc::", "<std::", "<core::", "<alloc::", "rust_begin_unwind", "__rust",
)
# How a segment relates to the one printed before it (``{prev}`` is its number).
_RELATION = {
    "during handling": "raised while handling link {prev}",
    "direct cause": "the direct result of link {prev}",
    "caused by": "the cause of link {prev}",
}
# What finds the start of another trace of the same kind with ``ctx search``.
_FIND_TRACES = {
    "python": "Traceback \\(most recent call last\\)", "go": "^(panic|fatal error):",
    "rust": "panicked at",
    # the first line of a trace (its exception), not every frame line
    "javascript": "^[A-Za-z0-9_$.]*(Error|Exception)\\b",
    "java": "^(Exception in thread|[A-Za-z0-9_$.]*(Error|Exception)\\b)",
}
_PY_SOURCE_LINES = 8    # source and caret lines accepted as structure under one python frame
_HEURISTIC = (
    "innermost frame whose path is not under site-packages / node_modules / "
    "stdlib / runtime markers"
)


# ------------------------------------------------------------------ the model
@dataclass
class Frame:
    file: str
    line: int          # line number inside ``file`` (0 when absent)
    func: str
    at: int            # payload line (1-based) the frame was printed on
    extra: int = 0     # Python "[Previous line repeated N more times]"

    @property
    def sig(self) -> tuple[str, int, str]:
        return (self.file, self.line, self.func)


@dataclass
class Segment:
    kind: str                  # py | at | go | rust
    start: int                 # first payload line of the segment (1-based)
    end: int
    etype: str = ""
    emsg: str = ""
    head_line: int = 0         # payload line the exception text came from; 0 = none
    relation: str = ""         # to the previous segment: "during handling" ...
    frames: list[Frame] = field(default_factory=list)  # in PRINT order
    omitted: int = 0           # Java "... N more"
    truncated: bool = False    # input ended before the segment did
    goroutines: int = 0
    site: Frame | None = None  # Rust panic location
    lang: str = ""
    depth: int = 0             # indentation of the header line (Java nesting)

    @property
    def headline(self) -> str:
        if self.etype and self.emsg:
            return f"{self.etype}: {self.emsg}"
        return self.etype or self.emsg

    def innermost_first(self) -> list[Frame]:
        frames = list(reversed(self.frames)) if self.kind == "py" else list(self.frames)
        if self.site is not None:
            frames.insert(0, self.site)
        return frames


@dataclass
class Trace:
    segs: list[Segment] = field(default_factory=list)
    #: (index of the link it hangs off, segment): Java ``Suppressed:``
    #: exceptions, which are siblings attached to a link, not causes.
    nested: list[tuple[int, Segment]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.segs[0].kind

    @property
    def lang(self) -> str:
        return {"py": "python", "go": "go", "rust": "rust"}.get(self.kind) or self.segs[0].lang or "javascript"

    @property
    def start(self) -> int:
        return self.segs[0].start

    @property
    def end(self) -> int:
        return max([self.segs[-1].end] + [s.end for _, s in self.nested])

    @property
    def headline_seg(self) -> Segment:
        """The segment holding the exception the program ended with: Python
        prints the final exception last; the JVM and JS print it first."""
        return self.segs[-1] if self.kind == "py" else self.segs[0]

    @property
    def nframes(self) -> int:
        return sum(len(s.frames) + sum(f.extra for f in s.frames) for s in self.segs)


@dataclass(frozen=True)
class Parse:
    traces: tuple[Trace, ...]
    structural: int
    nonblank: int
    lines: int


# -------------------------------------------------------------- line parsers
def _split_exc(text: str) -> tuple[str, str]:
    """``Type: message`` -> (type, message); no recognisable type -> ('', text)."""
    head, sep, rest = text.partition(": ")
    if sep and 0 < len(head) <= 120 and all(c.isalnum() or c in "_.$<>[]-" for c in head):
        return head, rest.strip()
    stripped = text.strip()
    if 0 < len(stripped) <= 120 and all(c.isalnum() or c in "_.$<>[]-" for c in stripped):
        return stripped, ""
    return "", stripped


def _split_loc(loc: str) -> tuple[str, int]:
    """``path:12:5`` / ``File.java:12`` / ``Native Method`` -> (file, line)."""
    a = loc.rsplit(":", 2)
    if len(a) == 3 and ascii_digits(a[1]) and ascii_digits(a[2]):
        return a[0], int(a[1][:9])
    b = loc.rsplit(":", 1)
    if len(b) == 2 and ascii_digits(b[1]):
        return b[0], int(b[1][:9])
    return loc, 0


def _parse_at(stripped: str) -> tuple[str, int, str] | None:
    """(file, line, func) of ``at ...`` frame text, or None when the line is
    not plausibly a frame (prose that happens to start with "at ")."""
    body = stripped[3:].strip()
    if not body:
        return None
    if body.endswith(")"):
        k = body.rfind("(")
        func, loc = (body[:k].strip(), body[k + 1:-1]) if k >= 0 else (body, "")
    else:
        func, loc = "", body
    file, line = _split_loc(loc)
    plausible = line > 0 or loc in ("Native Method", "Unknown Source") or (
        body.endswith(")") and ("." in loc or "/" in loc or ":" in loc)
    )
    return (file, line, func) if plausible else None


def _parse_py_frame(stripped: str) -> tuple[str, int, str]:
    rest = stripped[6:]
    q = rest.find('"')
    file = rest[:q] if q >= 0 else rest
    parts = rest[q + 1:].split(", ", 2) if q >= 0 else []
    line, func = 0, ""
    if len(parts) >= 2 and parts[1].startswith("line ") and ascii_digits(parts[1][5:].strip()):
        line = int(parts[1][5:].strip()[:9])
    if len(parts) == 3 and parts[2].startswith("in "):
        func = parts[2][3:]
    return file, line, func


def _java_header(text: str) -> tuple[str, str, str]:
    """(relation, type, message) of the line before a Java/JS frame run."""
    t = text.strip()
    relation = ""
    for prefix, name in (("Caused by: ", "caused by"), ("Suppressed: ", "suppressed")):
        if t.startswith(prefix):
            relation, t = name, t[len(prefix):]
            break
    if t.startswith('Exception in thread "'):
        k = t.find('" ', 22)
        if k >= 0:
            t = t[k + 2:]
    etype, emsg = _split_exc(t)
    return relation, etype, emsg


# --------------------------------------------------------------------- parse
@functools.lru_cache(maxsize=2)
def parse(text: str) -> Parse:
    """One linear pass over ``text`` (every line examined only up to
    :data:`LINE_EXAMINE_CAP` characters)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if "\x1b" in text:
        # Terminal colour: lines are recognised, and their fields taken, as
        # the terminal showed them. (Line numbers are unchanged.)
        lines = [_ANSI_RE.sub("", ln) if "\x1b" in ln else ln for ln in lines]
    n = len(lines)
    traces: list[Trace] = []
    structural = nonblank = 0

    cur: Segment | None = None
    mode = ""
    pending_rel = ""
    pending_func = ""      # go: function line waiting for its location line
    pending_frame: Frame | None = None   # rust: frame waiting for its "at" line
    header_used = -1       # index of the last line consumed as a segment header
    py_source = 0          # source/caret lines still accepted under the last python frame
    cap = LINE_EXAMINE_CAP

    def close(seg_end: int | None = None) -> None:
        nonlocal cur, mode, pending_func, pending_frame
        if cur is not None and seg_end is not None and seg_end > cur.end:
            cur.end = seg_end
        cur, mode, pending_func, pending_frame = None, "", "", None

    def open_seg(seg: Segment, mode_: str, link: str) -> None:
        """Start ``seg``: as a link of the previous trace's cause chain
        (``"chain"``), as an exception attached to one of its links but not
        its cause (``"nested"``: Java ``Suppressed:``), or as a new trace."""
        nonlocal cur, mode
        cur, mode = seg, mode_
        prior = traces[-1] if traces else None
        if link and prior is not None and prior.segs[-1].kind == seg.kind:
            if link == "nested":
                prior.nested.append((len(prior.segs) - 1, seg))
            else:
                prior.segs.append(seg)
        else:
            traces.append(Trace([seg]))

    i = 0
    while i < n:
        s = lines[i]
        if s.endswith("\r"):
            s = s[:-1]
        t = s[:cap]
        no = i + 1
        if not t.strip() and len(s) <= cap:
            if mode == "at":
                close()
            i += 1
            continue
        nonblank += 1
        indented = t[0] in " \t"
        stripped = s.lstrip(" \t")
        again = True
        while again:
            again = False
            # ---------------------------------------------- open segment modes
            if mode == "py" and cur is not None:
                if indented:
                    if stripped.startswith('File "'):
                        f, ln, fn = _parse_py_frame(stripped)
                        cur.frames.append(Frame(f, ln, fn, no))
                        py_source = _PY_SOURCE_LINES
                        structural += 1
                        cur.end = no
                    elif stripped.startswith("[Previous line repeated") and cur.frames:
                        digits = digits_in(stripped[24:40])
                        cur.frames[-1].extra += int(digits[:9]) if digits else 1
                        py_source = 0
                        structural += 1
                        cur.end = no
                    elif py_source > 0:
                        # The source line(s) and caret marks printed under a
                        # frame -- a few of them. Indented prose after a
                        # "Traceback" header is not structure.
                        py_source -= 1
                        structural += 1
                        cur.end = no
                elif t.startswith("Traceback (most recent call last):"):
                    cur.truncated = True
                    close()
                    again = True
                else:
                    etype, emsg = _split_exc(s)
                    cur.etype, cur.emsg, cur.head_line = etype, emsg, no
                    cur.end = no
                    structural += 1
                    close()
                break
            if mode == "at" and cur is not None:
                if indented and stripped.startswith("at "):
                    fr = _parse_at(stripped)
                    if fr is not None:
                        cur.frames.append(Frame(fr[0], fr[1], fr[2], no))
                        structural += 1
                        cur.end = no
                        break
                if indented and stripped.startswith("... ") and stripped.endswith(" more"):
                    digits = digits_in(stripped[4:20])
                    cur.omitted += int(digits[:9]) if digits else 0
                    structural += 1
                    cur.end = no
                    break
                close()
                again = True
                continue
            if mode == "go" and cur is not None:
                if not indented:
                    if t.startswith("goroutine ") and "[" in t:
                        cur.goroutines += 1
                        pending_func = ""
                        structural += 1
                        cur.end = no
                        break
                    if t.startswith("created by "):
                        pending_func = "\x00"      # its location line is structure, not a frame
                        structural += 1
                        cur.end = no
                        break
                    if s.endswith(")") and "(" in s:
                        pending_func = s[: s.rfind("(")]
                        structural += 1
                        cur.end = no
                        break
                    if t.startswith(("panic:", "fatal error:", "[signal", "exit status",
                                     "runtime:", "...additional frames")):
                        structural += 1
                        cur.end = no
                        break
                    close()
                    again = True
                    continue
                if pending_func and stripped:
                    if pending_func != "\x00" and cur.goroutines <= 1:
                        file, line = _split_loc(stripped.split(" +0x", 1)[0])
                        cur.frames.append(Frame(file, line, pending_func, no))
                    pending_func = ""
                    structural += 1
                    cur.end = no
                    break
                close()
                again = True
                continue
            if mode == "rust" and cur is not None:
                st = stripped
                k = st.find(": ")
                if indented and 0 < k <= 6 and ascii_digits(st[:k]):
                    fr_ = Frame("", 0, st[k + 2:], no)
                    cur.frames.append(fr_)
                    pending_frame = fr_
                    structural += 1
                    cur.end = no
                    break
                if indented and st.startswith("at ") and pending_frame is not None:
                    pending_frame.file, pending_frame.line = _split_loc(st[3:])
                    pending_frame = None
                    structural += 1
                    cur.end = no
                    break
                if t.startswith(("stack backtrace:", "note: ")):
                    structural += 1
                    cur.end = no
                    break
                close()
                again = True
                continue
            # ------------------------------------------------- no open segment
            if t.startswith("Traceback (most recent call last):"):
                seg = Segment("py", no, no, relation=pending_rel)
                open_seg(seg, "py", link="chain" if pending_rel else "")
                pending_rel = ""
                structural += 1
            elif t.startswith("During handling of the above exception"):
                pending_rel = "during handling"
                structural += 1
            elif t.startswith("The above exception was the direct cause"):
                pending_rel = "direct cause"
                structural += 1
            elif indented and stripped.startswith('File "'):
                # A paste that starts below its own "Traceback" line.
                seg = Segment("py", no, no, relation=pending_rel)
                open_seg(seg, "py", link="chain" if pending_rel else "")
                pending_rel = ""
                f, ln, fn = _parse_py_frame(stripped)
                seg.frames.append(Frame(f, ln, fn, no))
                py_source = _PY_SOURCE_LINES
                structural += 1
            elif indented and stripped.startswith("at ") and _parse_at(stripped) is not None:
                fr = _parse_at(stripped)
                relation, etype, emsg, head_line, start, depth = "", "", "", 0, no, 0
                if i > 0 and header_used != i - 1:
                    prev = lines[i - 1].rstrip("\r")
                    if prev.strip() and not (prev[0] in " \t" and prev.lstrip(" \t").startswith(("at ", "... "))):
                        relation, etype, emsg = _java_header(prev)
                        head_line, start, header_used = no - 1, no - 1, i - 1
                        depth = len(prev) - len(prev.lstrip(" \t"))
                        structural += 1
                seg = Segment("at", start, no, etype, emsg, head_line, relation, depth=depth)
                seg.frames.append(Frame(fr[0], fr[1], fr[2], no))
                prior = traces[-1] if traces else None
                link = ""
                if relation and prior is not None and prior.kind == "at" and prior.end + 1 >= start:
                    # A top-level "Caused by:" continues the cause chain. A
                    # "Suppressed:" -- or anything printed deeper than the
                    # trace's own headers -- is attached to an exception, not
                    # a cause of it.
                    top = relation == "caused by" and depth <= prior.segs[0].depth
                    link = "chain" if top else "nested"
                open_seg(seg, "at", link=link)
                structural += 1
                if fr[0].endswith((".java", ".kt", ".scala", ".groovy")) or fr[0] in ("Native Method", "Unknown Source"):
                    seg.lang = "java"
                else:
                    seg.lang = "javascript"
            elif not indented and t.startswith(("panic:", "fatal error:")):
                head, _, rest = s.partition(": ")
                seg = Segment("go", no, no, head, rest.strip(), no)
                open_seg(seg, "go", link="")
                structural += 1
            elif not indented and t.startswith("goroutine ") and "[" in t:
                seg = Segment("go", no, no, goroutines=1)
                open_seg(seg, "go", link="")
                structural += 1
            elif not indented and t.startswith("thread '") and " panicked at " in t:
                at = s.split(" panicked at ", 1)[1].strip()
                msg = ""
                if at.endswith(":") and i + 1 < n:
                    loc, msg = at[:-1], lines[i + 1].strip()
                    i += 1
                    if msg:   # the message line below is structure too
                        structural += 1
                        nonblank += 1
                elif at.startswith("'") and "', " in at:
                    k = at.rfind("', ")
                    msg, loc = at[1:k], at[k + 3:]
                else:
                    loc = at
                seg = Segment("rust", no, no + (1 if msg and at.endswith(":") else 0), "panic", msg, no)
                file, line = _split_loc(loc)
                seg.site = Frame(file, line, "panic site", no)
                open_seg(seg, "rust", link="")
                structural += 1
            else:
                pending_rel = ""   # a connector only links the traceback right after it
        i += 1
    if cur is not None and mode in ("py",):
        cur.truncated = True
    return Parse(tuple(tr for tr in traces if _substantial(tr)), structural, nonblank, n)


# ------------------------------------------------------------ frame analysis
def is_library(frame: Frame, lang: str) -> bool:
    """The user-code heuristic's negative test: does this frame look like a
    library or the runtime? Path and function markers only; a guess."""
    # Every marker carries its own separators ('/site-packages/'), so none of
    # them matches a directory that merely contains the word.
    norm = frame.file.replace("\\", "/").lower()
    if norm.startswith(_LIB_PATH_PREFIXES) or any(m in norm for m in _LIB_PATH_MARKERS):
        return True
    return frame.func.startswith(_LIB_FUNC_PREFIXES)


def find_runs(frames: list[Frame]) -> list[tuple[int, int, int, int, int]]:
    """Repeated frame cycles over ``frames`` as ``(start, period, reps,
    consumed, covered)``: ``consumed`` printed frames starting at ``start``
    stand for ``covered`` frames once Python's own ``[Previous line repeated
    N more times]`` counters are expanded, and ``reps`` full repetitions of a
    block of ``period`` frames. A block of up to :data:`MAX_CYCLE_PERIOD`
    frames repeating at least :data:`MIN_CYCLE_REPS` times in a row is one
    run. Linear in ``len(frames)``: a run is consumed whole, and a candidate
    period is only counted when its first two blocks start equal."""
    ids: dict[tuple[str, int, str], int] = {}
    sig = [ids.setdefault(f.sig, len(ids)) for f in frames]
    n = len(sig)
    runs: list[tuple[int, int, int, int, int]] = []
    i = 0
    while i < n:
        best: tuple[int, int, int, int, int] | None = None  # covered, p, reps, consumed, _
        j = i
        covered = 0
        while j < n and sig[j] == sig[i]:
            covered += 1 + frames[j].extra
            j += 1
        if covered >= MIN_CYCLE_REPS or covered > j - i:
            best = (covered, 1, covered, j - i, covered)
        for p in range(2, MAX_CYCLE_PERIOD + 1):
            if i + p * MIN_CYCLE_REPS > n:
                break
            if sig[i] != sig[i + p]:
                continue
            block = sig[i:i + p]
            r, k = 1, i + p
            while k + p <= n and sig[k:k + p] == block:
                r += 1
                k += p
            if r >= MIN_CYCLE_REPS:
                cov = sum(1 + f.extra for f in frames[i:i + p * r])
                if best is None or cov > best[0]:
                    best = (cov, p, r, p * r, cov)
        if best is None:
            i += 1
            continue
        cov, p, reps, consumed, _ = best
        runs.append((i, p, reps, consumed, cov))
        i += consumed
    return runs


def _fmt_frame(q: Quoter, f: Frame, cap: int) -> str:
    # A path keeps its END when clipped: the head is the same directory for
    # every library file, the tail is what tells them apart.
    where = f"{q(f.file, cap, tail=True)}:{f.line}" if f.line else q(f.file, cap, tail=True)
    return f"{where} in {q(f.func, cap)}" if f.func else where


# ------------------------------------------------------------------- profile
def _substantial(trace: Trace) -> bool:
    """A trace worth a view: two frames, or one frame and an exception line.
    (One bare frame line is not a stack trace, whatever else it resembles.)"""
    frames = sum(len(s.frames) for s in trace.segs)
    named = any(s.etype or s.emsg for s in trace.segs)
    return frames >= 2 or (frames >= 1 and named)


class StackTraceProfile(Profile):
    version = "trace/v1"

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
        if not any(m in head for m in _MARKERS):
            return None
        result = parse(text)
        if not any(_substantial(tr) for tr in result.traces) or result.nonblank == 0:
            return None
        if result.structural * 100 < DOMINANCE_PERCENT * result.nonblank:
            return None
        langs = sorted({tr.lang for tr in result.traces})
        return (
            f"{len(result.traces)} {'/'.join(langs)} stack trace(s) make up "
            f"{result.structural}/{result.nonblank} non-blank lines "
            f"({pct(result.structural, result.nonblank)})"
        )

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        result = parse(text)
        traces = result.traces
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        main = traces[-1]
        hseg = main.headline_seg   # the exception the program ended with
        fseg = main.segs[-1]       # whose frames the digest shows (see Trace.frame_seg)
        parsed_lines = result.lines
        total_lines = view.lines

        spans: dict[tuple[int, int], str | None] = {}

        def span(a: int, b: int) -> str | None:
            if (a, b) not in spans:
                spans[(a, b)] = ctx.mint_span(view, "region", a=a, b=b)
            return spans[(a, b)]

        windows = Windows(ctx, text, total_lines)
        policy = ctx.ws.config.redaction

        def where(a: int, b: int) -> str:
            """A region, with a span only when the span says true things: the
            exact lines when one retrieval holds them, a declared zoom digest
            when the region is past ``max_inline_lines``."""
            n = b - a + 1
            if windows.zoom(a, b):
                sid = span(a, b)
                return f"L{a}-L{b}" + (f" · span {sid}, {fmt_int(n)} lines: resolves as a zoom digest" if sid else "")
            if windows.exact(a, b):
                sid = span(a, b)
                return f"L{a}-L{b}" + (f" · span {sid}" if sid else "")
            return (f"L{a}-L{b} ({fmt_int(n)} lines, more than one exact retrieval holds: "
                    f"read it in --lines windows)")

        # Row items over the final exception's traceback, innermost first.
        inner = fseg.innermost_first()
        run_at = {r[0]: r for r in find_runs(inner)}
        items: list[tuple] = []     # ("f", position, Frame) | ("c", position, run, block)
        pos = idx = 0
        while idx < len(inner):
            run = run_at.get(idx)
            if run is not None:
                _, period, _, consumed, covered = run
                items.append(("c", pos + 1, run, inner[idx: idx + period]))
                pos += covered
                idx += consumed
            else:
                frame = inner[idx]
                items.append(("f", pos + 1, frame))
                pos += 1 + frame.extra
                idx += 1
        total_frames = pos

        files: Counter[str] = Counter()
        for seg in main.segs:
            for f in seg.frames:
                files[f.file] += 1 + f.extra
        census = sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))

        user: Frame | None = None
        user_in_final = True
        for seg in [fseg] + main.segs[-2::-1]:
            user = next((f for f in seg.innermost_first() if not is_library(f, main.lang)), None)
            if user is not None:
                user_in_final = seg is fseg
                break

        if fseg.kind == "py":
            last = fseg.head_line or fseg.end
            win = (windows.fit_start(max(fseg.start, last - 11), last), last)
        else:
            top = fseg.start
            win = (top, windows.fit_end(top, min(top + 11, fseg.end)))
        user_win = None
        if user is not None and user.at and not (win[0] <= user.at <= win[1]):
            ua = bounds.count(user.at - 3) + 1
            user_win = (ua, windows.fit_end(ua, min(user.at + 6, parsed_lines)))
        tail_from = parsed_lines + 1
        scope = " in the parsed part" if partial else ""

        def build(level: int) -> str:
            msg_cap, path_cap = ((110, 60), (90, 48), (70, 40), (50, 32), (30, 24))[level]
            n_frames = (4, 3, 2, 1, 1)[level]
            n_chain = (5, 4, 3, 0, 0)[level]
            n_files = (5, 3, 2, 0, 0)[level]
            n_other = (3, 2, 1, 0, 0)[level]
            q = Quoter(msg_cap, policy)
            out = [ctx.intake_line(total=partial) + f" · stack trace ({main.lang})"]
            tline = f"traces{scope}: {fmt_int(len(traces))}"
            if len(traces) > 1:
                tline += f" · detail is for the LAST one (trace {len(traces)})"
            out.append(tline + f" · {where(main.start, main.end)}")
            others = traces[:-1]
            listed = others[len(others) - min(n_other, len(others)):]
            for k, tr in enumerate(listed, start=len(others) - len(listed) + 1):
                out.append(
                    f"  trace {k} {where(tr.start, tr.end)} · "
                    f"{q(tr.headline_seg.headline or '(no exception line)', msg_cap)}"
                )
            if len(others) > len(listed):
                out.append(
                    f"  … +{fmt_int(len(others) - len(listed))} earlier traces not listed · "
                    f"find them: ctx search {rid} '{_FIND_TRACES.get(main.lang, 'Traceback')}' --context 0"
                )
            if hseg.etype:
                out.append(f"exception: {q(hseg.headline, msg_cap)} · L{hseg.head_line}")
            elif hseg.emsg:
                out.append(f"exception (no recognisable type; the line after the frames): "
                           f"{q(hseg.emsg, msg_cap)} · L{hseg.head_line}")
            else:
                out.append("exception: none printed (the trace is cut off before its final line)")
            m = len(main.segs)
            if m > 1:
                order = ("the last is raised last" if main.kind == "py"
                         else "the first is the outermost, the last the root cause")
                if n_chain:
                    out.append(f"chain: {m} links, numbered in the order printed ({order})")
                    keep = list(range(1, m + 1)) if m <= n_chain else [1] + list(range(m - n_chain + 2, m + 1))
                    prev = 0
                    for si in keep:
                        if prev and si != prev + 1:
                            out.append(f"  … +{si - prev - 1} links between")
                        seg = main.segs[si - 1]
                        rel = f"({_RELATION[seg.relation].format(prev=si - 1)}) " if seg.relation in _RELATION else ""
                        more_ = (f" · +{fmt_int(seg.omitted)} frames shared with the enclosing trace"
                                 if seg.omitted else "")
                        out.append(
                            f"  link {si} {rel}{q(seg.headline or '(no exception line)', msg_cap)}"
                            f" · L{seg.head_line or seg.start}{more_}"
                        )
                        prev = si
                else:
                    out.append(f"chain: {m} links ({order}); see them: ctx get {handle} --lines {main.start}:{min(main.end, main.start + 39)}")
            if main.nested:
                out.append(f"suppressed: {fmt_int(len(main.nested))} attached to a link (siblings printed "
                           f"under it, not causes of anything)")
                for parent, seg in main.nested[: (2 if level <= 1 else 1 if level <= 2 else 0)]:
                    out.append(
                        f"  suppressed under link {parent + 1}: "
                        f"{q(seg.headline or '(no exception line)', msg_cap)} · L{seg.head_line or seg.start}"
                    )
            role = ("the exception raised last" if main.kind == "py"
                    else "the root cause" if m > 1 else "the exception")
            shared = (f" + {fmt_int(fseg.omitted)} more the JVM elided as shared with the enclosing "
                      f"trace" if fseg.omitted else "")
            out.append(
                f"innermost frames{'' if m == 1 else f' of link {m} of {m}'}, {role} "
                f"(innermost first; {fmt_int(total_frames)} printed{shared}):"
            )
            def row(it: tuple) -> str:
                if it[0] == "f":
                    f = it[2]
                    mult = f" ×{fmt_int(1 + f.extra)}" if f.extra else ""
                    return f"  #{it[1]} {_fmt_frame(q, f, path_cap)}{mult} · L{f.at}"
                _, at_pos, (_, period, reps, _, covered), block = it
                body = " ; ".join(_fmt_frame(q, f, path_cap) for f in block[:3])
                more = f" ; … +{len(block) - 3}" if len(block) > 3 else ""
                kind = "repeats" if period == 1 else f"cycle of {period} frames"
                return (f"  #{at_pos}-#{at_pos + covered - 1} ×{fmt_int(reps)} {kind}: {body}{more}"
                        f" · L{block[0].at}")

            out.extend(row(it) for it in items[:n_frames])
            # A repetition is the finding, wherever it sits in the traceback:
            # the largest cycles beyond the rows above are listed, collapsed.
            beyond = sorted((it for it in items[n_frames:] if it[0] == "c"),
                            key=lambda it: (-it[2][4], it[1]))[: (2 if level <= 2 else 1)]
            if beyond:
                out.append("repeated frames further out (collapsed):")
                out.extend(row(it) for it in sorted(beyond, key=lambda it: it[1]))
            if len(items) > n_frames and n_frames > 1 and items[-1][0] == "f" and items[-1] not in beyond:
                f = items[-1][2]
                out.append(f"  … outermost: #{items[-1][1]} {_fmt_frame(q, f, path_cap)} · L{f.at}")
            elif len(items) > n_frames:
                out.append(f"  … +{fmt_int(len(items) - n_frames - len(beyond))} more rows")
            if user is not None:
                where_ = "" if user_in_final else " (in an earlier link of the chain)"
                line = (f"your code (heuristic, a guess; verify): "
                        f"{_fmt_frame(q, user, path_cap)} · L{user.at}{where_}")
                if level <= 1:
                    line += f" — {_HEURISTIC}"
                out.append(line)
            else:
                out.append("your code (heuristic): no frame looks like user code under the "
                           "library/runtime path markers")
            if n_files and census:
                out.append(
                    f"files in frames ({fmt_int(sum(files.values()))} incl. repeats, "
                    f"{fmt_int(len(census))} distinct): "
                    + " ; ".join(f"{q(name, path_cap, tail=True)} ×{fmt_int(c)}" for name, c in census[:n_files])
                    + (f" ; … +{fmt_int(len(census) - n_files)} more" if len(census) > n_files else "")
                )
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
                f" · trace structure {fmt_int(result.structural)}/{fmt_int(result.nonblank)} non-blank "
                f"lines, {fmt_int(outside)} outside traces · {min(n_frames, len(items))}/{fmt_int(len(items))} "
                f"frame rows shown (a row can stand for a collapsed cycle)"
            )
            if q.clipped:
                cov += f" · {q.clipped} quoted string{'' if q.clipped == 1 else 's'} clipped (caps {msg_cap}/{path_cap} chars)"
            out.append(cov)
            sugg = [f"ctx get {handle} --lines {win[0]}:{win[1]}"]
            if user_win is not None and user_win[1] >= user_win[0]:
                sugg.append(f"ctx get {handle} --lines {user_win[0]}:{user_win[1]}")
            if partial:
                sugg.append(f"ctx get {handle} --lines {tail_from}:{tail_from + 39}")
            sid = span(main.start, main.end) if windows.zoom(main.start, main.end) or windows.exact(main.start, main.end) else None
            sugg.append(f"ctx get {handle} --span {sid}" if sid
                        else f"ctx get {handle} --lines {main.start}:{windows.fit_end(main.start, main.end)}")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["StackTraceProfile", "find_runs", "is_library", "parse"]
