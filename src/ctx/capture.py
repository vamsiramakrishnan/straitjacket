"""Bounded stream capture: the hard cap on what one captured command stores.

A command that never stops printing (``yes``, ``cat /dev/zero``, a runaway
build) used to be spooled straight to disk by the child itself, with no
limit, until the timeout -- one such command filled a machine in seconds.
Capture now reads the child's pipes and writes through :class:`CappedSpool`:

* Each stream keeps at most ``cap`` bytes: the first half (the HEAD, cut back
  to its last line break when one is near) and the most recent half (the
  TAIL, in a ring file of exactly that size, cut forward to its first line
  break when one is near). Everything between is dropped, and counted.
* Byte and line counts stay exact for the whole stream, so the digest can say
  "captured X of Y bytes" and every retrieval verb can map stored lines back
  to the line numbers of the original stream (:class:`CapMap`).
* Memory is one read chunk per stream, whatever the output size.

Under the cap nothing changes: the spool holds exactly the bytes the command
wrote and no capture block is recorded, so the manifest is byte-identical to
an uncapped capture.

The running command: output past the cap is drained and counted, not stored,
until the command exits or the existing timeout stops it. A command whose
output passes :data:`RUNAWAY_FACTOR` times the cap is stopped (its process
group is killed and the run is marked ``outputLimited``): no build an agent
can use prints that much, and at pipe speed a generator reaches it in
seconds instead of burning the whole timeout.
"""

from __future__ import annotations

import os
import select
import signal as signal_mod
import subprocess
import threading
from pathlib import Path
from typing import Any

#: Read size per pipe read; also the bound on memory per stream.
CHUNK = 64 * 1024
#: How far from a cut the head/tail edge looks for a line break to cut at.
ALIGN = 64 * 1024
#: Output (both streams) past this multiple of the per-command cap stops the
#: command. 16 x the 64 MiB default = 1 GiB.
RUNAWAY_FACTOR = 16
#: After the command's own process exits, how long a descendant that still
#: holds its output open is read before the capture is closed.
DESCENDANT_GRACE_S = 1.0
#: Smallest accepted cap (a smaller configured value is raised to this).
MIN_CAP = 4096
DEFAULT_CAP = 64 * 1024 * 1024


def command_cap(configured: Any) -> int:
    """The per-command cap from ``[store] max_capture_bytes``."""
    try:
        value = int(configured)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CAP
    if value <= 0:
        return DEFAULT_CAP
    return max(MIN_CAP, value)


def stream_cap(cap_bytes: int) -> int:
    """The cap is per stream: stdout and stderr may each store up to it, so
    one stream (the usual case: a build or a test run writes to one) gets
    the whole cap instead of half of it with the other stream empty."""
    return max(MIN_CAP, cap_bytes)


class CappedSpool:
    """Spool one stream to ``path`` keeping at most ``cap`` bytes.

    The head is written to ``path`` as it arrives (a live reader sees it
    grow); once it is full the rest goes to a ring file beside it holding
    the most recent ``cap - cap // 2`` bytes. :meth:`finish` assembles the
    final spool at ``path`` and returns the capture block, or None when
    nothing was dropped.
    """

    def __init__(self, path: Path, cap: int):
        self.path = Path(path)
        self.head_cap = cap // 2
        self.tail_cap = cap - self.head_cap
        # Unbuffered: a live reader (`ctx job ID`) sees the head as it grows.
        self._fh = self.path.open("wb", buffering=0)
        self._head = 0
        self._head_nl = 0
        self._ring = None
        self._ring_total = 0  # bytes that went past the head, ever
        self.total = 0
        self.newlines = 0
        self._last = b""
        self.lock = threading.Lock()

    def _put(self, data: bytes) -> None:
        view = memoryview(data)
        while view:  # an unbuffered write may be partial
            n = self._fh.write(view)
            if not n:
                raise OSError("capture spool: write made no progress")
            view = view[n:]

    @property
    def ring_path(self) -> Path:
        return self.path.with_name(self.path.name + ".tail")

    @property
    def overflowed(self) -> bool:
        return self._ring_total > self.tail_cap

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self.lock:
            self.total += len(data)
            self.newlines += data.count(b"\n")
            self._last = data[-1:]
            if self._ring is None:
                room = self.head_cap - self._head
                if len(data) <= room:
                    self._put(data)
                    self._head += len(data)
                    self._head_nl += data.count(b"\n")
                    return
                part = data[:room]
                self._put(part)
                self._head += room
                self._head_nl += part.count(b"\n")
                data = data[room:]
                self._fh.flush()
                self._ring = self.ring_path.open("w+b")
            self._ring_write(data)

    def _ring_write(self, data: bytes) -> None:
        size = self.tail_cap
        if len(data) > size:  # only the last `size` bytes can survive
            self._ring_total += len(data) - size
            data = data[len(data) - size:]
        while data:
            off = self._ring_total % size
            n = min(len(data), size - off)
            self._ring.seek(off)
            self._ring.write(data[:n])
            self._ring_total += n
            data = data[n:]

    def _ring_chunks(self, start: int, length: int):
        """``length`` bytes of the tail in stream order from tail offset
        ``start``, read in bounded chunks."""
        size = self.tail_cap
        kept = min(self._ring_total, size)
        base = self._ring_total % size if self._ring_total > size else 0
        pos = start
        end = min(kept, start + length)
        while pos < end:
            off = (base + pos) % size
            n = min(CHUNK, end - pos, size - off)
            self._ring.seek(off)
            chunk = self._ring.read(n)
            if not chunk:
                break
            yield chunk
            pos += len(chunk)

    def finish(self) -> dict[str, Any] | None:
        """Close the spool; ``path`` then holds what is stored. Returns the
        capture block for a capped stream, None when every byte was kept."""
        with self.lock:
            try:
                if self._ring is None:
                    return None
                if not self.overflowed:
                    # Head full, tail not yet wrapped: every byte is present.
                    for chunk in self._ring_chunks(0, self._ring_total):
                        self._put(chunk)
                    return None
                return self._assemble()
            finally:
                self._fh.close()
                if self._ring is not None:
                    self._ring.close()
                    try:
                        self.ring_path.unlink()
                    except OSError:
                        pass

    def _assemble(self) -> dict[str, Any]:
        head = self._head
        head_nl = self._head_nl
        # Head: cut back to its last line break when one is within ALIGN.
        self._fh.flush()
        back = min(ALIGN, head)
        with self.path.open("rb") as rd:
            rd.seek(head - back)
            window = rd.read(back)
        cut = window.rfind(b"\n")
        if cut != -1:
            head = head - back + cut + 1  # the removed part holds no line break
        head_ends_nl = cut != -1 or head == 0
        self._fh.seek(head)
        self._fh.truncate(head)
        # Tail: cut forward to its first line break when one is within ALIGN.
        kept = self.tail_cap
        first = b"".join(self._ring_chunks(0, min(ALIGN, kept)))
        nl = first.find(b"\n")
        skip = nl + 1 if nl != -1 and nl + 1 < kept else 0
        tail_len = kept - skip
        synthetic = bool(head) and not head_ends_nl
        if synthetic:
            # The cut fell inside a line: end the stored head there so the
            # tail never reads as a continuation of it.
            self._put(b"\n")
        tail_nl = 0
        for chunk in self._ring_chunks(skip, tail_len):
            tail_nl += chunk.count(b"\n")
            self._put(chunk)
        total = self.total
        total_lines = self.newlines + (1 if total and self._last != b"\n" else 0)
        head_lines = head_nl + (1 if head and not head_ends_nl else 0)
        tail_first = self.newlines - tail_nl + 1
        tail_lines = tail_nl + (1 if tail_len and self._last != b"\n" else 0)
        return {
            "capped": True,
            "totalBytes": total,
            "totalLines": total_lines,
            "keptBytes": head + tail_len,
            "headBytes": head,
            "headLines": head_lines,
            "tailStartByte": total - tail_len + 1,
            "tailBytes": tail_len,
            "tailFirstLine": tail_first,
            "tailLines": tail_lines,
            "droppedBytes": total - head - tail_len,
            "droppedLines": max(0, tail_first - head_lines - 1),
            "lineBreakAdded": synthetic,
            # The stored tail starts at a line start only when it was cut
            # forward to one; otherwise its first line began in the middle.
            "tailStartsLine": bool(skip),
        }


class CapMap:
    """Map between a capped stream's stored blob and the original stream.

    Blob lines ``1..headLines`` are original lines ``1..headLines``; blob line
    ``headLines + k`` (k >= 1) is original line ``tailFirstLine + k - 1``.
    Original lines strictly between ``headLines`` and ``tailFirstLine`` were
    not stored. A line the cut fell inside is stored up to (or from) the cut.
    """

    def __init__(self, capture: dict[str, Any]):
        self.c = capture
        self.total_bytes = int(capture["totalBytes"])
        self.total_lines = int(capture["totalLines"])
        self.head_lines = int(capture["headLines"])
        self.tail_first = int(capture["tailFirstLine"])
        self.tail_lines = int(capture.get("tailLines", 0))
        self.head_bytes = int(capture["headBytes"])
        self.tail_start = int(capture["tailStartByte"])
        self.tail_bytes = int(capture["tailBytes"])
        self.dropped_bytes = int(capture["droppedBytes"])
        self.dropped_lines = int(capture["droppedLines"])
        self.added = 1 if capture.get("lineBreakAdded") else 0

    @staticmethod
    def of(meta: dict[str, Any] | None) -> "CapMap | None":
        cap = (meta or {}).get("capture")
        return CapMap(cap) if isinstance(cap, dict) and cap.get("capped") else None

    @property
    def last_line(self) -> int:
        return self.tail_first + self.tail_lines - 1 if self.tail_lines else self.head_lines

    @property
    def gap(self) -> tuple[int, int]:
        """Original lines not stored at all (may be empty: a > b)."""
        return self.head_lines + 1, self.tail_first - 1

    def orig_line(self, blob_line: int) -> int:
        if blob_line <= self.head_lines:
            return blob_line
        return self.tail_first + blob_line - self.head_lines - 1

    def blob_line(self, orig: int, *, prefer_tail: bool = False) -> int | None:
        """The stored line holding original line ``orig``, or None."""
        in_head = 1 <= orig <= self.head_lines
        in_tail = self.tail_lines and self.tail_first <= orig <= self.last_line
        if in_tail and not (in_head and not prefer_tail):
            return self.head_lines + orig - self.tail_first + 1
        if in_head:
            return orig
        return None

    def same_side(self, blob_a: int, blob_b: int) -> bool:
        return (blob_a <= self.head_lines) == (blob_b <= self.head_lines)

    def orig_byte_to_blob(self, orig: int) -> int | None:
        """1-based original byte offset -> 1-based stored offset, or None."""
        if 1 <= orig <= self.head_bytes:
            return orig
        if self.tail_bytes and self.tail_start <= orig <= self.total_bytes:
            return self.head_bytes + self.added + orig - self.tail_start + 1
        return None

    @staticmethod
    def _span(a: int, b: int) -> str:
        return f"L{a}" if a == b else f"L{a}-L{b}"

    def kept_text(self) -> str:
        parts = []
        if self.head_lines:
            parts.append(self._span(1, self.head_lines))
        if self.tail_lines:
            parts.append(self._span(self.tail_first, self.last_line))
        return " and ".join(parts) or "nothing"

    def gap_text(self) -> str:
        a, b = self.gap
        lines = (f"{self._span(a, b)} ({self.dropped_lines:,} whole "
                 f"line{'s' if self.dropped_lines != 1 else ''})"
                 if b >= a else "no whole line (the cut fell inside one line)")
        return f"{self.dropped_bytes:,} bytes, {lines}"


# ----------------------------------------------------------------- pumping
class _Pumps:
    """Reader threads that move a child's pipes into capped spools."""

    def __init__(self, proc: subprocess.Popen, spools: dict[str, CappedSpool], limit: int):
        self.proc = proc
        self.spools = spools
        self.limit = limit
        self.stop = threading.Event()
        self.runaway = False
        self.fds = {"stdout": proc.stdout.fileno(), "stderr": proc.stderr.fileno()}
        self.threads = [
            threading.Thread(target=self._pump, args=(self.fds[n], spools[n]), daemon=True)
            for n in ("stdout", "stderr")
        ]
        for t in self.threads:
            t.start()

    def _pump(self, fd: int, spool: CappedSpool) -> None:
        try:
            while not self.stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    return
                spool.write(chunk)
                if not self.runaway and sum(s.total for s in self.spools.values()) > self.limit:
                    self.runaway = True
                    _kill_group(self.proc)
        except (OSError, ValueError):
            return

    def close(self) -> None:
        """Wait for end of output; a descendant that still holds a pipe after
        :data:`DESCENDANT_GRACE_S` keeps writing into a detached drain."""
        for t in self.threads:
            t.join(DESCENDANT_GRACE_S)
        if any(t.is_alive() for t in self.threads):
            self.stop.set()
            for t in self.threads:
                t.join()
        for name, fd in self.fds.items():
            pipe = self.proc.stdout if name == "stdout" else self.proc.stderr
            if self._still_open(fd):
                _drain_detached(fd)
            try:
                pipe.close()
            except OSError:
                pass

    @staticmethod
    def _still_open(fd: int) -> bool:
        """True when the pipe has not reached end of file (a writer remains)."""
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError):
            return False
        if not ready:
            return True
        try:
            return bool(os.read(fd, 1) or b"")  # data pending: a writer remains
        except OSError:
            return False


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal_mod.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _drain_detached(fd: int) -> None:
    """Hand a still-open pipe to a detached ``cat > /dev/null``, so a
    background descendant (``server &``) keeps running as it did when it
    wrote into a file, without its output being stored anywhere."""
    try:
        subprocess.Popen(["cat"], stdin=fd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except OSError:
        pass


def capture_process(
    popen_args: Any,
    *,
    out_path: Path,
    err_path: Path,
    cap_bytes: int,
    wait,
    **popen_kwargs: Any,
) -> dict[str, Any]:
    """Start ``popen_args`` with both streams piped into capped spools at
    ``out_path``/``err_path``; ``wait(proc)`` waits for (or stops) the
    command and returns its own result. Returns ``{"proc", "waited",
    "outputLimited", "capture": {stream: block or None}}``.

    Raises whatever ``subprocess.Popen`` raises; the spools are closed
    either way.
    """
    per_stream = stream_cap(cap_bytes)
    spools = {"stdout": CappedSpool(out_path, per_stream),
              "stderr": CappedSpool(err_path, per_stream)}
    try:
        proc = subprocess.Popen(popen_args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **popen_kwargs)
    except BaseException:
        for s in spools.values():
            s.finish()
        raise
    pumps = _Pumps(proc, spools, cap_bytes * RUNAWAY_FACTOR)
    try:
        waited = wait(proc)
    finally:
        pumps.close()
        capture = {name: s.finish() for name, s in spools.items()}
    return {"proc": proc, "waited": waited, "outputLimited": pumps.runaway,
            "capture": capture}
