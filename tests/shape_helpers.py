"""Shared payload generators and digest invariants for the intake shape
profiles (stack trace, delimited data, plain unified diff).

The checks here are written against the READER of a digest, not against the
implementation: a model that treats a line beginning ``next:`` as a command,
that takes a printed span or ``--lines`` range at its word, and that has a
fixed byte budget.
"""

from __future__ import annotations

import csv
import io
import random
import re

from ctx.retrieval import Selector, get
from ctx.textutil import index_lines

SPAN_RE = re.compile(r"span ([0-9a-f]{10})")
LINES_CMD_RE = re.compile(r"^  ctx get (\S+) --lines (\d+):(\d+)$")
SPAN_CMD_RE = re.compile(r"^  ctx get (\S+) --span ([0-9a-f]{10})$")
SECRET = "ghp_" + "A1b2C3d4E5" * 4          # github-token shape, 44 chars
KEY20 = "sk-" + "Zq9" * 8                   # generic-api-token shape (24 chars)

#: Prefixes a digest line may legitimately start with (any profile). Content
#: from the payload only ever appears AFTER one of these, never at a line start.
KNOWN_PREFIXES = (
    "[ctx run:", "intake: ", "traces", "  #", "exception:", "chain:", "innermost frames",
    "your code", "files in frames", "coverage:", "next:", "  ctx ", "  … ", "redaction: applied",
    "delimited:", "also fits:", "statistics:", "columns", "first records:", "last record:",
    "  L", "ragged:", "parser-rejected:", "blank lines:", "diff", "largest changes",
    "other files", "every file header", "repeated frames", "  trace ", "  link ", "suppressed:",
    "  suppressed under", "preamble:", "exception (no recognisable", "  1 ", "  2 ", "  3 ", "  4 ", "  5 ", "  6 ", "  7 ", "  8 ",
    "  9 ", "  10 ", "  11 ", "  12 ", "  ±",
)


def digest_budget(ws) -> int:
    return ws.config.budgets.digest_tokens * 4


def run_of(receipt) -> str:
    return receipt.address.split("#")[0]


def next_commands(digest: str) -> list[str]:
    """The commands of the real ``next:`` block (the line that is exactly
    ``next:`` and the indented ``ctx`` lines after it)."""
    lines = digest.splitlines()
    assert lines.count("next:") == 1, "exactly one next: block"
    out = []
    for ln in lines[lines.index("next:") + 1:]:
        if ln.startswith("redaction: applied"):
            break
        assert ln.startswith("  ctx "), f"non-command line inside next: {ln!r}"
        out.append(ln.strip())
    assert out, "next: carries at least one command"
    return out


def assert_reader_safe(receipt, *, forbidden: tuple[str, ...] = ()) -> None:
    """Structure the reader relies on is intact and unforged."""
    lines = receipt.digest.splitlines()
    assert lines[0].startswith("[ctx run:") and lines[0].endswith("]")
    for ln in lines[1:]:
        assert not ln.startswith("[ctx "), f"forged header: {ln!r}"
        assert any(ln.startswith(p) for p in KNOWN_PREFIXES), f"line starts with payload text: {ln!r}"
        if ln.startswith("next:"):
            assert ln == "next:", f"forged next: line {ln!r}"
        for word in forbidden:
            assert not ln.lstrip().startswith(word), f"forged line start {word!r}: {ln!r}"
    handle = receipt.address
    for cmd in next_commands(receipt.digest):
        assert handle in cmd or cmd.startswith("ctx search " + run_of(receipt)), cmd


def assert_in_budget(ws, receipt) -> None:
    assert len(receipt.digest.encode()) <= digest_budget(ws), len(receipt.digest.encode())
    next_commands(receipt.digest)   # still there, whatever was fitted away


def resolve_everything(store, ws, receipt, payload_text: str) -> int:
    """Follow every ``--lines`` command and every printed span; each must
    return exactly what it claims. Returns how many were resolved."""
    src = index_lines(payload_text)
    max_inline = ws.config.budgets.max_inline_lines
    n = 0
    for cmd in next_commands(receipt.digest):
        m = LINES_CMD_RE.match("  " + cmd)
        if m:
            a, b = int(m.group(2)), int(m.group(3))
            assert 1 <= a <= b <= len(src), f"--lines {a}:{b} outside the payload ({len(src)} lines)"
            out = get(store, ws, receipt.address, Selector(lines=(a, b)))
            _assert_lines_exact(out, src, a, b)
            n += 1
        m = SPAN_CMD_RE.match("  " + cmd)
        if m:
            n += _resolve_span(store, ws, receipt, m.group(2), src, max_inline)
    for sid in sorted(set(SPAN_RE.findall(receipt.digest))):
        n += _resolve_span(store, ws, receipt, sid, src, max_inline)
    return n


def _assert_lines_exact(out: str, src: list[str], a: int, b: int) -> None:
    got = dict(re.findall(r"^L(\d+): (.*)$", out, re.M))
    assert got, out[:200]
    for k in range(a, b + 1):
        assert got.get(str(k)) == src[k - 1], (k, got.get(str(k)), src[k - 1])


def _resolve_span(store, ws, receipt, sid: str, src: list[str], max_inline: int) -> int:
    out = get(store, ws, receipt.address, Selector(span=sid))
    m = re.search(r"region: L(\d+):(\d+) \(([\d,]+) lines", out)
    assert m, out[:300]
    a, b = int(m.group(1)), int(m.group(2))
    assert 1 <= a <= b <= len(src)
    if b - a + 1 <= max_inline:
        assert "complete" in out
        _assert_lines_exact(out, src, a, b)
    else:
        assert "zoom digest" in out
    return 1


# ------------------------------------------------------------ payload makers
def python_trace(recursion: int = 0, chain: bool = True, *, final: str = "ValueError: boom",
                 user: str = "/app/svc/core.py") -> str:
    out: list[str] = []
    if chain:
        out += ["Traceback (most recent call last):", f'  File "{user}", line 41, in load',
                "    return cfg[key]", "KeyError: 'timeout'", "",
                "During handling of the above exception, another exception occurred:", ""]
    out += ["Traceback (most recent call last):", '  File "/app/main.py", line 12, in <module>', "    run()"]
    for _ in range(recursion):
        out += [f'  File "{user}", line 8, in walk', "    return visit(node)",
                f'  File "{user}", line 15, in visit', "    return walk(node.child)"]
    out += ['  File "/usr/lib/python3.12/json/decoder.py", line 12, in decode', "    raise", final]
    return "\n".join(out) + "\n"


def padded(trace: str, min_bytes: int = 4200) -> str:
    """Repeat distinct library frames so the paste exceeds the inline limit."""
    if len(trace.encode()) >= min_bytes:
        return trace
    lines = trace.rstrip("\n").split("\n")
    extra: list[str] = []
    i = 0
    while len("\n".join(lines + extra).encode()) < min_bytes:
        extra += [f'  File "/usr/lib/python3.12/site-packages/lib{i}/mod.py", line {i + 3}, in fn{i}',
                  f"    call_{i}()"]
        i += 1
    return "\n".join(lines[:-1] + extra + lines[-1:]) + "\n"


def js_trace(n: int = 120) -> str:
    out = ["TypeError: Cannot read properties of undefined (reading 'id')",
           "    at getUser (/app/src/users.js:42:19)", "    at async handler (/app/src/routes/user.ts:17:5)"]
    out += [f"    at Layer.handle (/app/node_modules/express/lib/router/layer.js:{95 + i}:5)" for i in range(n)]
    out.append("    at node:internal/process/task_queues:95:5")
    return "\n".join(out) + "\n"


def java_trace(n: int = 120) -> str:
    out = ['Exception in thread "main" java.lang.IllegalStateException: Failed to start',
           "\tat com.acme.app.Main.start(Main.java:41)", "\tat com.acme.app.Main.main(Main.java:12)",
           "Caused by: java.io.UncheckedIOException: cannot read config",
           "\tat com.acme.cfg.Loader.load(Loader.java:88)"]
    out += [f"\tat org.springframework.core.X{i}.run(X.java:{i + 10})" for i in range(n)]
    out += ["\t... 12 more", "Caused by: java.io.FileNotFoundException: /etc/acme.conf",
            "\tat java.base/java.io.FileInputStream.open0(Native Method)", "\t... 40 more"]
    return "\n".join(out) + "\n"


def go_trace(n: int = 80) -> str:
    out = ["panic: runtime error: index out of range [5] with length 3", "", "goroutine 1 [running]:",
           "main.process(...)", "\t/home/dev/proj/main.go:21", "main.main()", "\t/home/dev/proj/main.go:12 +0x1d"]
    for i in range(n):
        out += [f"github.com/foo/bar/pkg.(*Server).handle{i}(0xc000010000, 0x1)",
                f"\t/home/dev/go/pkg/mod/github.com/foo/bar@v1.2.3/pkg/server.go:{100 + i} +0x1d5"]
    out += ["", "goroutine 6 [chan receive]:", "main.worker()", "\t/home/dev/proj/worker.go:9 +0x25", "exit status 2"]
    return "\n".join(out) + "\n"


def rust_trace(n: int = 60) -> str:
    out = ["thread 'main' panicked at src/lib.rs:17:9:", "called `Option::unwrap()` on a `None` value",
           "stack backtrace:", "   0: rust_begin_unwind", "             at /rustc/abc123/library/std/src/panicking.rs:645:5",
           "   1: myapp::parse::run", "             at ./src/lib.rs:17:9"]
    for i in range(n):
        out += [f"  {i + 2}: std::rt::lang_start::{i}", f"             at /rustc/abc123/library/std/src/rt.rs:{100 + i}:18"]
    return "\n".join(out) + "\n"


def csv_text(rows: int = 400, delimiter: str = ",", *, header: bool = True, seed: int = 5) -> str:
    r = random.Random(seed)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    if header:
        w.writerow(["id", "name", "city", "amount", "created", "status"])
    for i in range(1, rows + 1):
        w.writerow([i, r.choice(["alice", "bob", "carol"]), r.choice(["Oslo", "Lima, Peru", "Kyiv"]),
                    f"{r.randrange(1, 100000) / 100:.2f}", f"2024-{r.randrange(1, 13):02d}-{r.randrange(1, 28):02d}",
                    r.choice(["ok", "ok", "fail"])])
    return buf.getvalue()


def unified_diff(files: int = 8, *, sizes: list[int] | None = None) -> str:
    """A plain unified diff whose hunk headers are exact (built with difflib)."""
    import difflib
    out: list[str] = []
    for f in range(files):
        n = (sizes[f] if sizes else 5 + f * 3)
        old = [f"line {k} of file {f}\n" for k in range(40 + n)]
        new = list(old)
        for k in range(n):
            new[(k * 7) % len(new)] = f"changed {k} in file {f}\n"
        new += [f"appended {k}\n" for k in range(n // 2)]
        out += list(difflib.unified_diff(old, new, f"src/mod{f}.py", f"src/mod{f}.py", n=2))
    return "".join(out)


def count_diff(diff: str) -> tuple[int, int, int]:
    """(files, adds, dels) read independently of the profile: from the hunk
    structure difflib emitted."""
    files = adds = dels = 0
    for ln in diff.split("\n"):
        if ln.startswith("+++ "):
            files += 1
        elif ln.startswith("+") and not ln.startswith("+++ "):
            adds += 1
        elif ln.startswith("-") and not ln.startswith("--- "):
            dels += 1
    return files, adds, dels
