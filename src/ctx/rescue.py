"""Lossless mid-session rescue: epoch-latched transcript elision (Tier-1).

The one structural edge a rewriting proxy (Headroom) had over the Tier-0
observer was rescuing a session whose transcript is already bloated. This
module takes that capability without the shortcuts that make rewriting
expensive and unsafe:

- **Epoch-latched, not per-request.** When window pressure crosses the
  configured threshold, ONE deterministic elision set is frozen (an
  "epoch"): every tool_result older than ``keep_recent`` tool-results and
  larger than ``min_block_bytes``. Because the transcript is append-only
  and the set is frozen by message index, every subsequent request rewrites
  to a byte-identical prefix — the cache is re-bought once at the smaller
  size and stays stable. (Measured: per-request rewriting pays ~18× more
  cache churn than one epoch. evals + docs/LOSSLESS-RESCUE.md.)
- **Nothing is destroyed.** Every elided block's full bytes are written to
  ``<state_dir>/elided/<sha256>.txt`` BEFORE the stub replaces it, and the
  stub carries the hash, the byte count, and the retrieval path. The agent
  (or a human) can always get the content back. A rescue that cannot
  persist the bytes does not elide them.
- **Deterministic and idempotent.** The stub is a pure function of the
  elided content; applying a plan twice equals applying it once; a longer
  transcript sharing the epoch prefix rewrites to a byte-identical prefix.
- **Explicitly not Tier-0.** The observer's byte-exact invariant is the
  default; rescue only runs when the operator opts in (``--rescue-pct``).

Epoch state persists in ``<state_dir>/rescue.json`` so restarts and
concurrent handler threads agree on the frozen sets.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from ctx.sessiondir import LEDGER_DIR_NAME
from ctx.textutil import short_id

DEFAULT_KEEP_RECENT = 6  # most recent tool_results never elided
DEFAULT_MIN_BLOCK_BYTES = 1024  # small blocks are not worth a stub
_LOCK = threading.Lock()


def _content_text(content: Any) -> str:
    """Canonical text of a tool_result content field (str or block list)."""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _stub_prefix(state_dir: Path) -> str:
    """A path for stubs that RESOLVES from the agent's workspace cwd.

    Under `ctx wrap` the state dir is `<ws>/.ctx-session-reads/proxy` by
    construction, so the workspace-relative prefix is
    `.ctx-session-reads/proxy`. Nonstandard layouts fall back to the
    absolute path — a stub whose address cannot be followed would make the
    'lossless' claim a lie (found by PR review)."""
    parent = Path(state_dir).parent
    if parent.name == LEDGER_DIR_NAME:
        return f"{parent.name}/{Path(state_dir).name}"
    return str(state_dir)


def stub_for(text: str, sha: str, prefix: str) -> str:
    """The replacement content — pure function of the elided bytes."""
    nbytes = len(text.encode("utf-8"))
    return (
        f"[ctx rescue: tool_result elided ({nbytes} bytes, sha256:{short_id(sha)}) — "
        f"full content preserved verbatim at "
        f"{prefix}/elided/{sha}.txt; read that file for any detail]"
    )


def plan_epoch(messages: list[dict], *, keep_recent: int, min_block_bytes: int) -> list[int]:
    """Deterministic elision set for an epoch: ordinals (counting tool_result
    blocks from the start of the transcript) that are old and large. Pure."""
    sizes: list[tuple[int, int]] = []  # (ordinal, bytes)
    ordinal = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = _content_text(block.get("content", ""))
                    sizes.append((ordinal, len(text.encode("utf-8"))))
                    ordinal += 1
    cutoff = max(0, len(sizes) - keep_recent)
    return [o for o, n in sizes[:cutoff] if n >= min_block_bytes]


def apply_elision(
    messages: list[dict], elide: set[int], state_dir: Path
) -> tuple[list[dict], int]:
    """Replace the selected tool_result blocks with stubs, persisting each
    block's full bytes first. Returns (new_messages, blocks_elided). Pure
    with respect to the transcript: same input -> byte-identical output."""
    elided_dir = state_dir / "elided"
    out_msgs: list[dict] = []
    ordinal = 0
    n_elided = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out_msgs.append(msg)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if ordinal in elide:
                    text = _content_text(block.get("content", ""))
                    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    path = elided_dir / f"{sha}.txt"
                    if not path.is_file():  # persist BEFORE replacing
                        elided_dir.mkdir(parents=True, exist_ok=True)
                        tmp = elided_dir / f".{sha}.tmp"
                        tmp.write_text(text, encoding="utf-8")
                        os.replace(tmp, path)
                    new_block = dict(block)
                    new_block["content"] = stub_for(text, sha, _stub_prefix(state_dir))
                    new_content.append(new_block)
                    n_elided += 1
                else:
                    new_content.append(block)
                ordinal += 1
            else:
                new_content.append(block)
        new_msg = dict(msg)
        new_msg["content"] = new_content
        out_msgs.append(new_msg)
    return out_msgs, n_elided


class RescueState:
    """Epoch bookkeeping shared across proxy handler threads. Epochs only
    ever accumulate; each freezes an elision set by tool_result ordinal."""

    def __init__(self, state_dir: Path, threshold_pct: float, *,
                 keep_recent: int = DEFAULT_KEEP_RECENT,
                 min_block_bytes: int = DEFAULT_MIN_BLOCK_BYTES) -> None:
        self.state_dir = Path(state_dir)
        self.threshold_pct = float(threshold_pct)
        self.keep_recent = int(keep_recent)
        self.min_block_bytes = int(min_block_bytes)
        self._path = self.state_dir / "rescue.json"
        self._elide: set[int] = set()
        self._epochs = 0
        self._load()

    def _load(self) -> None:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            self._elide = set(int(x) for x in doc.get("elide") or [])
            self._epochs = int(doc.get("epochs") or 0)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"epochs": self._epochs, "elide": sorted(self._elide)},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception:
            pass

    def maybe_rescue(
        self, body: bytes, window_pct: float | None
    ) -> tuple[bytes, int]:
        """Apply the current epoch's elision to a /messages request body; open
        a new epoch first when pressure has crossed the threshold. Returns
        (body, blocks_elided) — the original body on any parse problem."""
        try:
            doc = json.loads(body)
            messages = doc.get("messages")
            if not isinstance(messages, list):
                return body, 0
            with _LOCK:
                if (
                    window_pct is not None
                    and window_pct >= self.threshold_pct
                ):
                    frozen = plan_epoch(
                        messages,
                        keep_recent=self.keep_recent,
                        min_block_bytes=self.min_block_bytes,
                    )
                    if not set(frozen) <= self._elide:
                        self._elide |= set(frozen)
                        self._epochs += 1
                        self._save()
                elide = set(self._elide)
            if not elide:
                return body, 0
            new_messages, n = apply_elision(messages, elide, self.state_dir)
            if n == 0:
                return body, 0
            doc["messages"] = new_messages
            return json.dumps(doc, ensure_ascii=False).encode("utf-8"), n
        except Exception:
            return body, 0
