"""Layer-B replay: old vs translated signal selection over real sessions.

Reads every recorded Claude-Code transcript on this machine, extracts large
tool outputs (the exploration-scale case: bugfix sessions drowning in test,
grep, and build noise), and compares two deterministic selectors:

- OLD: first keyword match via the seed-era ``_ERROR_RE``;
- NEW: strongest tier via the translated ``_signal_rank``.

Metrics are honest and label-free:
- outputs examined / where each selector fires at all;
- benign-trap selections (a line that only negates failure must not be the
  model's first evidence);
- decisive upgrades (new selector picks a tier-3 line where old picked
  weak-keyword or nothing).

Writes one dated receipt-ready JSON under results/.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ctx.digest.text import _ERROR_RE, _signal_rank  # noqa: E402
from ctx.replay import default_history_paths  # noqa: E402

MIN_LINES = 30


def iter_tool_outputs(path: Path):
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        if '"tool_result"' not in line and '"tool_use_id"' not in line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = doc.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            payload = item.get("content")
            texts = []
            if isinstance(payload, str):
                texts.append(payload)
            elif isinstance(payload, list):
                texts.extend(
                    piece.get("text", "")
                    for piece in payload
                    if isinstance(piece, dict) and piece.get("type") == "text"
                )
            for text in texts:
                lines = text.splitlines()
                if len(lines) >= MIN_LINES:
                    yield lines


def old_first_signal(lines: list[str]) -> int | None:
    for i, ln in enumerate(lines):
        if _ERROR_RE.search(ln):
            return i
    return None


def new_first_signal(lines: list[str]) -> int | None:
    best_i, best_rank = None, 0
    for i, ln in enumerate(lines):
        rank = _signal_rank(ln)
        if rank > best_rank:
            best_i, best_rank = i, rank
    return best_i


def main() -> int:
    stats = Counter()
    examples = {"trap_avoided": [], "decisive_upgrade": []}
    outputs = 0
    for path in default_history_paths():
        for lines in iter_tool_outputs(Path(path)):
            outputs += 1
            o = old_first_signal(lines)
            n = new_first_signal(lines)
            if o is None and n is None:
                stats["no_signal_either"] += 1
                continue
            if o is None and n is not None:
                stats["new_finds_old_misses"] += 1
                continue
            old_line = lines[o]
            if n is None:
                stats["only_old_fires"] += 1
                if _signal_rank(old_line) == -1:
                    stats["old_trap_only"] += 1
                continue
            new_line = lines[n]
            old_trap = _signal_rank(old_line) == -1
            if old_trap:
                stats["old_picked_benign_trap"] += 1
                if len(examples["trap_avoided"]) < 5:
                    examples["trap_avoided"].append(old_line[:110])
            if o != n and _signal_rank(new_line) == 3 and _signal_rank(old_line) < 3:
                stats["decisive_upgrade"] += 1
                if len(examples["decisive_upgrade"]) < 5:
                    examples["decisive_upgrade"].append(new_line[:110])
            elif o == n:
                stats["agree"] += 1
    report = {
        "schema": "ctx.alphaevolve-replay-signal-ranking/v1",
        "recorded_transcript_outputs_large": outputs,
        **{k: stats.get(k, 0) for k in (
            "agree",
            "decisive_upgrade",
            "old_picked_benign_trap",
            "old_trap_only",
            "new_finds_old_misses",
            "only_old_fires",
            "no_signal_either",
        )},
        "examples": examples,
    }
    out = Path(__file__).resolve().parent / "results" / "2026-08-24-replay-signal-ranking.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
