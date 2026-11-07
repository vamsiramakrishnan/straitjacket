"""High-watermark multimodal verbatim residence gate for thinking-heavy tool loops.

In thinking-heavy agent protocols where every ``model`` ``functionCall`` part
carries an encrypted ``thoughtSignature`` payload, stripping that signature
from an active ``functionCall`` turn fails protocol validation, while rewriting
a sliding window on every turn invalidates prefix caching on every step.

``WatermarkResidence`` resolves both constraints simultaneously:
  1. Keeps ``contents`` strictly append-only (preserving prefix cache hits)
     until uncompacted ``thought_tokens`` older than the trailing ``k_keep``
     tool-call pairs reach ``thought_watermark`` (default 20,000 tokens).
  2. Rewrites only newly eligible ``(model functionCall, user functionResponse)``
     pairs in-place into plain-text summary turns while leaving previously
     compacted prefix turns byte-identical across subsequent epochs.
  3. Preserves tool outputs verbatim up to ``max_result_chars`` (default 64 KiB),
     surfaces optional compact ``state_note`` tool arguments as ``[state] ...``
     lines so verified facts survive compaction, and lifts multimodal
     ``inlineData`` / ``fileData`` parts from ``functionResponse.parts`` into
     the compacted ``user`` turn so visual evidence stays attached.
"""

from __future__ import annotations

import json
from typing import Any

from ctx import bounds

__all__ = ["WatermarkResidence"]


def _summarize_call(fc: dict[str, Any]) -> tuple[str, str]:
    name = str(fc.get("name") or "tool")
    raw_args = fc.get("args")
    arg_map = raw_args if isinstance(raw_args, dict) else {}
    state_note = str(arg_map.get("state_note") or "").strip()
    args = {k: v for k, v in arg_map.items() if k != "state_note"}
    if name == "bash":
        cmd = str(args.get("command") or "")
        if len(cmd) > 2400:
            cmd = cmd[:2000] + " ... " + cmd[-300:]
        return f"bash({cmd!r})", state_note
    if name == "write":
        fp = str(args.get("file_path") or args.get("path") or "")
        body = str(args.get("content") or "")
        if len(body) <= 6000:
            return f"write(file_path={fp!r}, content={body!r})", state_note
        return (
            f"write(file_path={fp!r}, chars={len(body)}, "
            f"head={body[:600]!r}, tail={body[-400:]!r})"
        ), state_note
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True)
    if len(raw) > 1200:
        raw = raw[:900] + "..." + raw[-200:]
    return f"{name}({raw})", state_note


def _contents_has_media(contents: list[dict[str, Any]]) -> bool:
    for turn in contents:
        for p in turn.get("parts") or []:
            if "inlineData" in p or "fileData" in p:
                return True
            if "functionResponse" in p:
                fr = p["functionResponse"]
                if isinstance(fr, dict):
                    for mp in fr.get("parts") or []:
                        if isinstance(mp, dict) and ("inlineData" in mp or "fileData" in mp):
                            return True
    return False


class WatermarkResidence:
    """High-watermark in-place verbatim residence compactor for tool-loop ``contents``."""

    def __init__(
        self,
        *,
        k_keep: int = 3,
        thought_watermark: int = 20_000,
        max_result_chars: int = 65_536,
        max_epochs: int | None = None,
        media_max_epochs: int | None = None,
    ) -> None:
        self.k_keep = bounds.count(bounds.explicit(k_keep, 3))
        self.thought_watermark = bounds.count(bounds.explicit(thought_watermark, 20_000))
        self.max_result_chars = max(
            4096, bounds.count(bounds.explicit(max_result_chars, 65_536))
        )
        self.max_epochs = None if max_epochs is None else bounds.count(max_epochs)
        self.media_max_epochs = (
            None if media_max_epochs is None else bounds.count(media_max_epochs)
        )
        self._turn_thoughts: dict[int, int] = {}
        self.epochs: int = 0
        self.pairs_compacted: int = 0
        self.thoughts_compacted: int = 0
        self.state_notes_preserved: int = 0

    def record_model_turn(self, turn_idx: int, thought_tokens: int) -> None:
        """Record ``thought_tokens`` carried by an uncompacted ``model`` tool-call turn."""
        self._turn_thoughts[int(turn_idx)] = bounds.count(thought_tokens)

    def maybe_compact(self, contents: list[dict[str, Any]]) -> tuple[int, int]:
        """Compact eligible ``(model, user)`` tool pairs in-place once watermark is reached.

        Returns ``(pairs_compacted_now, thoughts_compacted_now)``.
        """
        epoch_ceiling = (
            self.media_max_epochs
            if (self.media_max_epochs is not None and _contents_has_media(contents))
            else self.max_epochs
        )
        if epoch_ceiling is not None and self.epochs >= epoch_ceiling:
            return 0, 0

        call_idxs: list[int] = []
        for idx in range(len(contents) - 1):
            turn = contents[idx]
            nxt = contents[idx + 1]
            if turn.get("role") != "model" or nxt.get("role") != "user":
                continue
            parts = turn.get("parts") or []
            nxt_parts = nxt.get("parts") or []
            if any("functionCall" in p for p in parts) and any(
                "functionResponse" in p for p in nxt_parts
            ):
                call_idxs.append(idx)

        if len(call_idxs) <= self.k_keep:
            return 0, 0

        eligible = (
            call_idxs[: len(call_idxs) - self.k_keep]
            if self.k_keep > 0
            else list(call_idxs)
        )
        carried_thoughts = sum(self._turn_thoughts.get(i, 0) for i in eligible)
        if carried_thoughts < self.thought_watermark:
            return 0, 0

        n_pairs = 0
        dropped_thoughts = 0
        for idx in eligible:
            m_turn = contents[idx]
            u_turn = contents[idx + 1]
            m_parts = m_turn.get("parts") or []
            u_parts = u_turn.get("parts") or []

            visible_texts = [
                str(p.get("text") or "").strip()
                for p in m_parts
                if "text" in p and not p.get("thought") and str(p.get("text") or "").strip()
            ]
            call_lines: list[str] = []
            state_lines: list[str] = []
            for p in m_parts:
                if "functionCall" in p and isinstance(p["functionCall"], dict):
                    c_sum, s_note = _summarize_call(p["functionCall"])
                    call_lines.append("[called] " + c_sum)
                    if s_note:
                        cleaned_note = " ".join(s_note.splitlines())[:280]
                        if cleaned_note not in state_lines:
                            state_lines.append(cleaned_note)
                            self.state_notes_preserved += 1
            state_prefix = (
                "\n".join(f"[state] {sn}" for sn in state_lines) + "\n"
                if state_lines
                else ""
            )
            prefix = ("\n".join(visible_texts)[:800] + "\n") if visible_texts else ""
            m_summary = (state_prefix + prefix + "\n".join(call_lines)).strip() or "[called]"

            out_chunks: list[str] = []
            media_parts: list[dict[str, Any]] = []
            for p in u_parts:
                if "functionResponse" in p and isinstance(p["functionResponse"], dict):
                    fr = p["functionResponse"]
                    resp = fr.get("response")
                    resp_map = resp if isinstance(resp, dict) else {}
                    txt = str(resp_map.get("output", ""))
                    if len(txt) > self.max_result_chars:
                        half = self.max_result_chars // 2
                        omitted = len(txt) - self.max_result_chars
                        txt = (
                            txt[:half]
                            + f"\n...[omitted {omitted} chars]...\n"
                            + txt[len(txt) - half :]
                        )
                    out_chunks.append(txt)
                    for mp in fr.get("parts") or []:
                        if isinstance(mp, dict) and ("inlineData" in mp or "fileData" in mp):
                            media_parts.append(mp)
                elif "inlineData" in p or "fileData" in p:
                    media_parts.append(p)
                elif "text" in p and str(p.get("text") or "").strip():
                    out_chunks.append(str(p["text"]))

            u_text = "[result]\n" + "\n---\n".join(out_chunks)
            contents[idx] = {"role": "model", "parts": [{"text": m_summary}]}
            contents[idx + 1] = {"role": "user", "parts": [{"text": u_text}, *media_parts]}

            n_pairs += 1
            dropped_thoughts += self._turn_thoughts.pop(idx, 0)

        if n_pairs:
            self.epochs += 1
            self.pairs_compacted += n_pairs
            self.thoughts_compacted += dropped_thoughts
        return n_pairs, dropped_thoughts
