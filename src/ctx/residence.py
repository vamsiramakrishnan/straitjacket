"""Cache-economic multimodal verbatim residence gate for thinking-heavy tool loops.

In thinking-heavy agent protocols where every ``model`` ``functionCall`` part
carries an encrypted ``thoughtSignature`` payload, stripping that signature
from an active ``functionCall`` turn fails protocol validation, while rewriting
a sliding window on every turn invalidates prefix caching on every step.

``WatermarkResidence`` resolves both constraints from prefix-cache first principles:
  1. Keeps ``contents`` strictly append-only (preserving prefix cache hits)
     until reclaimable dead tokens (uncompacted ``thought_tokens`` plus stripped
     ephemeral UI control tables / duplicate snapshots) both clear the platform
     implicit-cache minimum (``min_cache_tokens = 6_144``) and amortize the
     one-time cache-miss cost of rewriting the invalidated cached suffix over
     the expected remaining trajectory horizon.
  2. Pins multimodal (``inlineData`` / ``fileData``) turns in place by default so
     visual deliberation signatures remain attached without arbitrary epoch caps,
     unpinning media turns only under high context-window pressure (``> 50%``).
  3. Rewrites only newly eligible ``(model functionCall, user functionResponse)``
     pairs in-place into plain-text summary turns while leaving previously
     compacted prefix turns byte-identical across all subsequent epochs.
  4. Preserves tool outputs verbatim up to ``max_result_chars`` (default 64 KiB),
     surfaces optional ``state_note`` arguments as ``[state] ...`` lines, and
     tags state-changing calls with ``[mutated]`` plus a frozen boundary
     ``[prior-mutations]`` ledger so side effects are never forgotten after compaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import json
import re
from typing import Any

from ctx import bounds

__all__ = [
    "WatermarkResidence",
    "_extract_computed_receipts",
    "_extract_state_notes_from_part",
    "_inject_compaction_recap",
    "_is_fact_bearing_state_note",
    "extract_mutation_receipts",
]

_UI_CONTROLS_RE = re.compile(
    r"(=== Interactive Elements \(\d+\) ===\n).*?(\n\n=== Visible Page Text ===\n)",
    re.S,
)

_DEFAULT_MUTATING_TOOLS = frozenset(
    {
        "write",
        "edit",
        "write_file",
        "edit_file",
        "patch",
        "delete",
        "browser_click",
        "browser_fill",
        "browser_type",
        "browser_act",
        "browser_batch",
        "browser_utility",
        "browser_press",
    }
)
_DEFAULT_MUTATING_BASH_RE = re.compile(
    r"(?:>|>>|\btee\b|\bmv\b|\bcp\b|\brm\b|\bmkdir\b|\bsed\s+-i\b|"
    r"\.write_text\(|\.write\(|json\.dump|csv\.writer|open\([^)]*['\"][wa]['\"]|"
    r"\bcurl\b.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data|-d\s|-F\s)|"
    r"urllib\.request\.Request\b.*(?:\bdata\s*=|\bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE))|"
    r"\b(?:requests|httpx)\.(?:post|put|patch|delete)\b|"
    r"[\"']method[\"']\s*:\s*[\"']tools/call[\"'].*[\"']name[\"']\s*:\s*[\"'][A-Za-z0-9_.-]*(?:create|update|delete|remove|modify|send|reply|draft|star|unstar|archive|trash|label|add|close|reopen|complete|upload|insert|append|replace|batch|transition|assign|comment|link|merge)[A-Za-z0-9_.-]*[\"']|"
    r"\b(?:stripe|todoist|jira|box|dws|gws|ntn|shopify|gh|git)\b.*\b(?:create|update|delete|add|complete|close|reopen|move|assign|comment|transition|upload|mkdir|cp|mv|rm|share|collaborations|append|replace|insert|batch|send|draft|trash|archive|finalize|pay|void|cancel|confirm|capture|refund|edit|patch|post|put|submit|publish|seed|import|modify|link|resolve)\b)",
    re.I | re.S,
)

_RECEIPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # CLI human receipts, e.g. `✓ Created page "Untitled" (page_123)` or `✓ Page "Untitled" moved to trash`
    re.compile(
        r"(?:✓|✔|SUCCESS:?)\s*((?:Created|Updated|Deleted|Uploaded|Archived|Restored|Moved|Sent|Saved|Resolved|Linked|Added|Closed|Completed|Page|Issue|Task|File)\b[^\n]{0,110})",
        re.I,
    ),
    # Structured JSON/dict mutation statuses with nearby id
    re.compile(
        r"['\"]id['\"]\s*:\s*['\"]([A-Za-z0-9_.-]{3,64})['\"][^\n]{0,200}?['\"]status['\"]\s*:\s*['\"](created|updated|deleted|draft_created|uploaded|sent|archived|trashed)['\"]",
        re.I,
    ),
    re.compile(
        r"['\"]status['\"]\s*:\s*['\"](created|updated|deleted|draft_created|uploaded|sent|archived|trashed)['\"][^\n]{0,200}?['\"]id['\"]\s*:\s*['\"]([A-Za-z0-9_.-]{3,64})['\"]",
        re.I,
    ),
    # HTTP 201 Created
    re.compile(r"\bHTTP/1\.[01]\s+201\s+Created\b", re.I),
)


def extract_mutation_receipts(output_text: str) -> list[str]:
    """Extract concise empirical mutation receipts from tool output text."""
    if not output_text:
        return []
    receipts: list[str] = []
    seen: set[str] = set()
    for pat in _RECEIPT_PATTERNS:
        for m in pat.finditer(output_text):
            groups = [g for g in m.groups() if g]
            if len(groups) == 2:
                label = f"{groups[0]} ({groups[1]})"
            elif len(groups) == 1:
                label = " ".join(groups[0].split())[:110]
            else:
                label = " ".join(m.group(0).split())[:80]
            if label and label not in seen:
                seen.add(label)
                receipts.append(label)
                if len(receipts) >= 4:
                    return receipts
    return receipts


_FACT_BEARING_NOTE_RE = re.compile(
    r"(?:\bassumed\s*:|=|\d|"
    r"\b(?:verified|confirmed|written|wrote|found|matched|total|count|sum|"
    r"min|max|mean|median|avg|rate|status|passed|failed|created|updated|"
    r"deleted|uploaded|sent|resolved|linked|empty|none|null|true|false)\b)",
    re.I,
)
_GENERIC_INTENT_PREFIX_RE = re.compile(
    r"^(?:check(?:ing)?|read(?:ing)?|inspect(?:ing)?|list(?:ing)?|look(?:ing)?|"
    r"search(?:ing)?|run(?:ning)?|open(?:ing)?|view(?:ing)?|navigat(?:e|ing)|"
    r"click(?:ing)?|scroll(?:ing)?|wait(?:ing)?|fetch(?:ing)?|get(?:ting)?|"
    r"prob(?:e|ing)|explor(?:e|ing)|examin(?:e|ing)|test(?:ing)?|try(?:ing)?)\b",
    re.I,
)
_COMPUTED_KV_SUMMARY_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.-]{0,36}\s*=\s*[$€£¥+-]?\d[\d,.%]*|"
    r"[A-Za-z_][A-Za-z0-9_.\s-]{0,36}\s+=\s+[$€£¥+-]?\d[\d,._%\sA-Za-z-]{0,50})$"
)
_DELIVERABLE_CONTRACT_RE = re.compile(
    r"(\[deliverable-contract\][\s\S]{1,800}?)(?=\n\n\[|\Z)"
)


def _is_fact_bearing_state_note(note: str) -> bool:
    """Return True if ``note`` records a concrete fact, value, verification, or ``assumed:`` choice."""
    cleaned = " ".join(str(note or "").splitlines()).strip()
    if not cleaned:
        return False
    if _FACT_BEARING_NOTE_RE.search(cleaned):
        return True
    if _GENERIC_INTENT_PREFIX_RE.match(cleaned):
        return False
    return ":" in cleaned


def _order_verified_notes(notes: Sequence[str]) -> list[str]:
    """Deduplicate fact-bearing notes while prioritizing ``assumed:`` entries first."""
    assumed: list[str] = []
    facts: list[str] = []
    seen: set[str] = set()
    for raw in notes:
        cleaned = " ".join(str(raw or "").splitlines()).strip()[:280]
        if not cleaned or not _is_fact_bearing_state_note(cleaned) or cleaned in seen:
            continue
        seen.add(cleaned)
        if cleaned.lower().startswith("assumed:") or "assumed:" in cleaned.lower():
            assumed.append(cleaned)
        else:
            facts.append(cleaned)
    combined = assumed + facts
    return combined[-12:]


def _extract_state_notes_from_part(part: dict[str, Any] | str) -> list[str]:
    """Extract fact-bearing ``state_note`` entries from a model part, turn dict, or text string."""
    raw_notes: list[str] = []
    if isinstance(part, str):
        for ln in part.splitlines():
            s = ln.strip()
            if s.startswith("[state]"):
                raw_notes.append(s[len("[state]") :].strip())
            elif s:
                raw_notes.append(s)
        return _order_verified_notes(raw_notes)
    if not isinstance(part, dict):
        return []
    if isinstance(part.get("parts"), list):
        for sub in part["parts"]:
            raw_notes.extend(_extract_state_notes_from_part(sub))
        return _order_verified_notes(raw_notes)
    if "functionCall" in part and isinstance(part["functionCall"], dict):
        args = part["functionCall"].get("args")
        if isinstance(args, dict) and args.get("state_note"):
            raw_notes.append(str(args["state_note"]))
    if "text" in part and not part.get("thought"):
        for ln in str(part.get("text") or "").splitlines():
            s = ln.strip()
            if s.startswith("[state]"):
                raw_notes.append(s[len("[state]") :].strip())
    return _order_verified_notes(raw_notes)


def _extract_computed_receipts(output_text: str) -> list[str]:
    """Extract compact ``[data-shape:...]``, ``[write-receipt:...]``, ``[section:...]``, or ``k = v`` receipts."""
    if not output_text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in output_text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        if s.startswith(("[data-shape:", "[write-receipt:", "[section:", "[section-receipt:")):
            item = s[:140]
        elif _COMPUTED_KV_SUMMARY_RE.match(s):
            item = s[:100]
        else:
            continue
        if item not in seen:
            seen.add(item)
            found.append(item)
            if len(found) >= 8:
                break
    return found


def _extract_deliverable_contract_from_contents(
    contents: list[dict[str, Any]], deliverable_contract: str = ""
) -> str:
    """Resolve explicit ``deliverable_contract`` or extract ``[deliverable-contract]`` from ``contents[0]``."""
    explicit = str(deliverable_contract or "").strip()
    if explicit:
        return explicit[:600]
    if not contents or not isinstance(contents[0], dict):
        return ""
    for p in contents[0].get("parts") or []:
        if isinstance(p, dict) and "text" in p:
            txt = str(p.get("text") or "")
            m = _DELIVERABLE_CONTRACT_RE.search(txt)
            if m:
                return " ".join(m.group(1).strip().splitlines())[:600]
    return ""


def _inject_compaction_recap(
    target: str | dict[str, Any] | list[dict[str, Any]] = "",
    state_notes: Sequence[str] | None = None,
    mutations: Sequence[str] | None = None,
    *,
    verified_notes: Sequence[str] = (),
    computed_receipts: Sequence[str] = (),
    deliverable_contract: str = "",
    idx: int | None = None,
) -> str:
    """Format and inject a ``[compaction-recap]`` block preserving verified notes, receipts, and contract."""
    raw_notes = list(state_notes) if state_notes is not None else list(verified_notes)
    ordered_notes = _order_verified_notes(raw_notes)
    dedup_receipts: list[str] = []
    seen_r: set[str] = set()
    for r in computed_receipts:
        rs = " ".join(str(r or "").splitlines()).strip()[:140]
        if rs and rs not in seen_r:
            seen_r.add(rs)
            dedup_receipts.append(rs)
    recent_receipts = dedup_receipts[-10:]
    recent_muts = [str(m).strip() for m in (mutations or ()) if str(m).strip()][-12:]
    contract_str = " ".join(str(deliverable_contract or "").splitlines()).strip()[:600]

    recap_lines: list[str] = []
    if recent_muts:
        recap_lines.append(
            f"[prior-mutations ({len(recent_muts)})] " + " | ".join(recent_muts)
        )
    if ordered_notes or recent_receipts or contract_str:
        recap_lines.append("[compaction-recap]")
        if ordered_notes:
            recap_lines.append("[verified-notes: " + " | ".join(ordered_notes) + "]")
        if recent_receipts:
            recap_lines.append("[computed-receipts: " + " | ".join(recent_receipts) + "]")
        if contract_str:
            if contract_str.startswith("[deliverable-contract"):
                recap_lines.append(contract_str)
            else:
                recap_lines.append(f"[deliverable-contract: {contract_str}]")
    recap_block = "\n".join(recap_lines)

    if isinstance(target, str):
        if not recap_block:
            return target
        return f"{recap_block}\n{target}".strip() if target else recap_block
    if isinstance(target, dict):
        parts = target.get("parts") or []
        cur = str(parts[0].get("text") or "") if parts and isinstance(parts[0], dict) else ""
        merged = f"{recap_block}\n{cur}".strip() if recap_block else cur
        target["parts"] = [{"text": merged}]
        return merged
    if isinstance(target, list) and target:
        pos = len(target) - 1 if idx is None else idx
        if 0 <= pos < len(target) and isinstance(target[pos], dict):
            return _inject_compaction_recap(
                target[pos],
                ordered_notes,
                recent_muts,
                computed_receipts=recent_receipts,
                deliverable_contract=contract_str,
            )
    return recap_block


def _default_is_mutating(name: str, args: dict[str, Any]) -> bool:
    if name in _DEFAULT_MUTATING_TOOLS:
        return True
    if name == "bash":
        return bool(_DEFAULT_MUTATING_BASH_RE.search(str(args.get("command") or "")))
    return False


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


def _compact_mutation_label(call_summary: str) -> str:
    one_line = " ".join(call_summary.splitlines()).strip()
    if len(one_line) <= 120:
        return one_line
    return one_line[:95] + "..." + one_line[-20:]


def _turn_has_media(turn: dict[str, Any]) -> bool:
    for p in turn.get("parts") or []:
        if "inlineData" in p or "fileData" in p:
            return True
        if "functionResponse" in p and isinstance(p["functionResponse"], dict):
            for mp in p["functionResponse"].get("parts") or []:
                if isinstance(mp, dict) and ("inlineData" in mp or "fileData" in mp):
                    return True
    return False


def _contents_has_media(contents: list[dict[str, Any]]) -> bool:
    return any(_turn_has_media(turn) for turn in contents)


def _turn_text_chars(turn: dict[str, Any]) -> int:
    total = 0
    for p in turn.get("parts") or []:
        if "text" in p and not p.get("thought"):
            total += len(str(p.get("text") or ""))
        elif "functionCall" in p and isinstance(p["functionCall"], dict):
            total += len(json.dumps(p["functionCall"], ensure_ascii=False))
        elif "functionResponse" in p and isinstance(p["functionResponse"], dict):
            resp = p["functionResponse"].get("response")
            if isinstance(resp, dict):
                total += len(str(resp.get("output", "")))
            for mp in p["functionResponse"].get("parts") or []:
                if isinstance(mp, dict) and "inlineData" in mp:
                    total += 1032  # ~258 tokens per inline image tile
        elif "inlineData" in p:
            total += 1032
    return total


class WatermarkResidence:
    """Cache-economic in-place verbatim residence compactor for tool-loop ``contents``."""

    def __init__(
        self,
        *,
        k_keep: int = 3,
        min_cache_tokens: int = 6_144,
        cache_read_ratio: float = 0.10,
        window_tokens: int = 1_048_576,
        pin_media_turns: bool | None = None,
        max_result_chars: int = 65_536,
        strip_superseded_ui_controls: bool = True,
        is_mutating_call: Callable[[str, dict[str, Any]], bool] | None = None,
        thought_watermark: int | None = None,
        char_watermark: int | None = None,
        max_epochs: int | None = None,
        media_max_epochs: int | None = None,
    ) -> None:
        self.k_keep = bounds.count(bounds.explicit(k_keep, 3))
        self.min_cache_tokens = max(1024, bounds.count(bounds.explicit(min_cache_tokens, 6_144)))
        ratio = float(cache_read_ratio)
        self.cache_read_ratio = min(max(ratio, 0.01), 0.99)
        self.window_tokens = max(32_768, bounds.count(bounds.explicit(window_tokens, 1_048_576)))
        explicit_legacy = any(
            x is not None for x in (thought_watermark, char_watermark, max_epochs, media_max_epochs)
        )
        self.pin_media_turns = (not explicit_legacy) if pin_media_turns is None else bool(pin_media_turns)
        self.thought_watermark = (
            None if thought_watermark is None else bounds.count(thought_watermark)
        )
        self.char_watermark = (
            None if char_watermark is None else max(8192, bounds.count(char_watermark))
        )
        self.max_result_chars = max(
            4096, bounds.count(bounds.explicit(max_result_chars, 65_536))
        )
        self.max_epochs = None if max_epochs is None else bounds.count(max_epochs)
        self.media_max_epochs = (
            None if media_max_epochs is None else bounds.count(media_max_epochs)
        )
        self.strip_superseded_ui_controls = bool(strip_superseded_ui_controls)
        self._is_mutating = is_mutating_call or _default_is_mutating
        self._turn_thoughts: dict[int, int] = {}
        self._seen_ui_body_hashes: dict[str, int] = {}
        self._compacted_mutations: list[str] = []
        self._compacted_state_notes: list[str] = []
        self._compacted_computed_receipts: list[str] = []
        self._last_prompt_tokens: int = 0
        self._last_cached_tokens: int = 0
        self._retained_cache_floor: int = 0
        self._awaiting_post_compact_usage: bool = False
        self._expected_retained_prefix_tokens: int = 0
        self._compacted_first_idx: int = 0
        self._single_tip_cache: bool = False
        self._last_compact_turn: int | None = None
        self._sub_block_cache_steps: int = 0
        self._has_observed_cache: bool = False
        self.epochs: int = 0
        self.pairs_compacted: int = 0
        self.thoughts_compacted: int = 0
        self.state_notes_preserved: int = 0
        self.mutations_preserved: int = 0
        self.ui_controls_stripped_chars: int = 0

    @property
    def single_tip_cache_detected(self) -> bool:
        """Whether empirical prefix-cache telemetry detected single-tip rolling invalidation."""
        return bool(self._single_tip_cache)

    @single_tip_cache_detected.setter
    def single_tip_cache_detected(self, val: bool) -> None:
        self._single_tip_cache = bool(val)

    def get_telemetry(self) -> dict[str, Any]:
        """Return a snapshot of compaction and prefix-cache calibration telemetry."""
        return {
            "epochs": self.epochs,
            "pairs_compacted": self.pairs_compacted,
            "thoughts_compacted": self.thoughts_compacted,
            "state_notes_preserved": self.state_notes_preserved,
            "mutations_preserved": self.mutations_preserved,
            "ui_controls_stripped_chars": self.ui_controls_stripped_chars,
            "single_tip_cache_detected": bool(self._single_tip_cache),
            "retained_cache_floor": self._retained_cache_floor,
            "sub_block_cache_steps": self._sub_block_cache_steps,
            "last_compact_turn": self._last_compact_turn,
        }

    def _inject_compaction_recap(
        self,
        contents: list[dict[str, Any]],
        state_notes: list[str] | None = None,
        mutations: list[str] | None = None,
        *,
        computed_receipts: list[str] | None = None,
        deliverable_contract: str = "",
    ) -> str:
        """Build and inject a structured ``[compaction-recap]`` block into ``contents``."""
        notes = self._compacted_state_notes if state_notes is None else state_notes
        muts = self._compacted_mutations if mutations is None else mutations
        receipts = (
            self._compacted_computed_receipts
            if computed_receipts is None
            else computed_receipts
        )
        return _inject_compaction_recap(
            contents,
            notes,
            muts,
            computed_receipts=receipts,
            deliverable_contract=deliverable_contract,
        )

    def record_model_turn(self, turn_idx: int, thought_tokens: int) -> None:
        """Record ``thought_tokens`` carried by an uncompacted ``model`` tool-call turn."""
        self._turn_thoughts[int(turn_idx)] = bounds.count(thought_tokens)

    def observe_cache_usage(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Record live API ``(prompt_tokens, cached_tokens)`` telemetry to calibrate prefix retention.

        Multi-checkpoint caches retain ``contents[:first_idx]`` across in-place mid-array
        rewrites, whereas single-tip rolling caches drop ``cached_tokens`` back to the
        static Turn-0 floor when ``contents[first_idx]`` changes. Measuring post-compaction
        cache retention empirically prices the true invalidation miss without backend flags.
        """
        p_tok = bounds.count(prompt_tokens)
        c_tok = bounds.count(cached_tokens)
        if self._awaiting_post_compact_usage:
            self._awaiting_post_compact_usage = False
            exp = self._expected_retained_prefix_tokens
            min_probe = max(self.min_cache_tokens * 2, (self._retained_cache_floor or 1) * 2)
            if self._compacted_first_idx > 1 and exp >= min_probe:
                if c_tok < int(0.55 * exp):
                    self._single_tip_cache = True
                    self._retained_cache_floor = c_tok
                elif c_tok >= int(0.65 * exp):
                    self._single_tip_cache = False
            elif self._retained_cache_floor == 0 or (0 < c_tok < self._retained_cache_floor):
                self._retained_cache_floor = c_tok
        elif self._has_observed_cache and self.epochs < 2:
            delta_c = c_tok - self._last_cached_tokens
            if 256 < delta_c < 3_000:
                self._sub_block_cache_steps += 1
                if self._sub_block_cache_steps >= 3:
                    self._single_tip_cache = True
            if self._retained_cache_floor == 0 and c_tok > 0:
                self._retained_cache_floor = c_tok
        elif self._retained_cache_floor == 0 and c_tok > 0:
            self._retained_cache_floor = c_tok
        self._last_prompt_tokens = p_tok
        self._last_cached_tokens = c_tok
        self._has_observed_cache = True

    def _transform_output_text(
        self,
        txt: str,
        seen_hashes: dict[str, int],
        turn_idx: int,
    ) -> tuple[str, int]:
        """Return ``(compacted_text, stripped_ephemeral_chars)`` using ``seen_hashes``."""
        orig_len = len(txt)
        ui_stripped = 0
        if self.strip_superseded_ui_controls and "=== Interactive Elements (" in txt:
            before_ui = len(txt)
            m_ui = _UI_CONTROLS_RE.search(txt)
            if m_ui and (len(m_ui.group(0)) - len(m_ui.group(1)) - len(m_ui.group(2))) > 100:
                txt = (
                    txt[: m_ui.start()]
                    + m_ui.group(1)
                    + "[superseded element refs — use latest snapshot for clicks]"
                    + m_ui.group(2)
                    + txt[m_ui.end() :]
                )
            marker = "\n=== Visible Page Text ===\n"
            if marker in txt:
                head_part, body_part = txt.split(marker, 1)
                if len(body_part) > 400:
                    bh = hashlib.sha256(body_part.encode("utf-8")).hexdigest()[:16]
                    if bh in seen_hashes:
                        txt = (
                            head_part
                            + marker
                            + "[visible page text identical to earlier snapshot above]"
                        )
                    else:
                        seen_hashes[bh] = turn_idx
            ui_stripped = max(0, before_ui - len(txt))

        if len(txt) > self.max_result_chars:
            half = self.max_result_chars // 2
            omitted = len(txt) - self.max_result_chars
            txt = (
                txt[:half]
                + f"\n...[omitted {omitted} chars]...\n"
                + txt[len(txt) - half :]
            )
        return txt, max(ui_stripped, orig_len - len(txt))

    def _estimate_pair_compaction(
        self,
        contents: list[dict[str, Any]],
        idx: int,
        preview_hashes: dict[str, int],
    ) -> tuple[int, int]:
        """Return ``(reclaimable_dead_tokens, retained_tokens)`` for tool pair at ``idx``."""
        thoughts = self._turn_thoughts.get(idx, 0)
        m_chars = _turn_text_chars(contents[idx])
        retained_u_chars = 0
        stripped_u_chars = 0
        for p in contents[idx + 1].get("parts") or []:
            if "functionResponse" in p and isinstance(p["functionResponse"], dict):
                resp = p["functionResponse"].get("response")
                raw_txt = str(resp.get("output", "")) if isinstance(resp, dict) else ""
                comp_txt, stripped = self._transform_output_text(raw_txt, preview_hashes, idx)
                retained_u_chars += len(comp_txt)
                stripped_u_chars += stripped
            elif "text" in p:
                retained_u_chars += len(str(p.get("text") or ""))
        dead_tokens = thoughts + (stripped_u_chars // 4)
        retained_tokens = max(1, (min(m_chars, 3200) + retained_u_chars) // 4)
        return dead_tokens, retained_tokens

    def _estimate_total_carried_tokens(self, contents: list[dict[str, Any]]) -> int:
        text_tokens = sum(_turn_text_chars(t) for t in contents) // 4
        thought_tokens = sum(self._turn_thoughts.values())
        return text_tokens + thought_tokens

    def maybe_compact(
        self,
        contents: list[dict[str, Any]],
        *,
        remaining_steps: int | None = None,
        has_final_output_write: bool = False,
        min_turn_gap: int = 6,
        deliverable_contract: str = "",
    ) -> tuple[int, int]:
        """Compact eligible ``(model, user)`` tool pairs in-place when cache-amortized.

        Returns ``(pairs_compacted_now, thoughts_compacted_now)``.
        """
        if remaining_steps is not None and int(remaining_steps) <= 10:
            return 0, 0

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

        older = (
            call_idxs[: len(call_idxs) - self.k_keep]
            if self.k_keep > 0
            else list(call_idxs)
        )

        est_total_tokens = self._estimate_total_carried_tokens(contents)
        under_window_pressure = est_total_tokens >= (self.window_tokens // 2)

        if self.pin_media_turns and not under_window_pressure:
            eligible = [
                idx
                for idx in older
                if not (_turn_has_media(contents[idx]) or _turn_has_media(contents[idx + 1]))
            ]
        else:
            eligible = older

        if not eligible:
            return 0, 0

        legacy_watermark_mode = (
            self.thought_watermark is not None or self.char_watermark is not None
        )
        turn_idx = (
            len(contents)
            if legacy_watermark_mode
            else (self.pairs_compacted + len(call_idxs))
        )
        uncompacted_thoughts = sum(self._turn_thoughts.values())
        gap_steps = max(0, int(min_turn_gap))
        if (
            not under_window_pressure
            and self._last_compact_turn is not None
            and gap_steps > 0
        ):
            if (turn_idx - self._last_compact_turn) < gap_steps and uncompacted_thoughts < 25_000:
                return 0, 0

        first_idx = eligible[0]
        cutoff_idx = older[-1] + 2
        eligible_set = set(eligible)

        if legacy_watermark_mode:
            carried_thoughts = sum(self._turn_thoughts.get(i, 0) for i in eligible)
            carried_chars = 0
            if self.char_watermark is not None:
                for i in eligible:
                    for p in contents[i + 1].get("parts") or []:
                        if "functionResponse" in p and isinstance(p["functionResponse"], dict):
                            resp = p["functionResponse"].get("response")
                            if isinstance(resp, dict):
                                carried_chars += len(str(resp.get("output", "")))
            tw_ok = self.thought_watermark is not None and carried_thoughts >= self.thought_watermark
            cw_ok = self.char_watermark is not None and carried_chars >= self.char_watermark
            if not (tw_ok or cw_ok):
                return 0, 0
        else:
            preview_hashes = dict(self._seen_ui_body_hashes)
            dead_tokens = 0
            cand_retained: dict[int, int] = {}
            for i in eligible:
                d_tok, r_tok = self._estimate_pair_compaction(contents, i, preview_hashes)
                dead_tokens += d_tok
                cand_retained[i] = r_tok
            for m_idx in range(first_idx, min(cutoff_idx, len(contents))):
                if m_idx not in eligible_set and contents[m_idx].get("role") == "model":
                    dead_tokens += self._turn_thoughts.get(m_idx, 0)

            if not under_window_pressure:
                if dead_tokens < self.min_cache_tokens:
                    return 0, 0
                # On multi-checkpoint caches, contents[:first_idx] stays cached and only
                # contents[first_idx : -2] is invalidated; on empirically detected single-tip
                # rolling caches, rewriting contents[first_idx] invalidates down to the
                # observed Turn-0 floor.
                cached_limit = max(first_idx, len(contents) - 2)
                suffix_invalidated = 0
                t_cursor = first_idx
                while t_cursor < cached_limit:
                    if t_cursor in eligible_set:
                        suffix_invalidated += cand_retained[t_cursor]
                        t_cursor += 2
                    else:
                        suffix_invalidated += (
                            _turn_text_chars(contents[t_cursor]) // 4
                            + self._turn_thoughts.get(t_cursor, 0)
                        )
                        t_cursor += 1

                total_pairs_seen = self.pairs_compacted + len(call_idxs)
                if self._single_tip_cache:
                    if self._last_cached_tokens > self._retained_cache_floor:
                        invalidated_cached_tokens = max(
                            suffix_invalidated,
                            self._last_cached_tokens - self._retained_cache_floor,
                        )
                    else:
                        full_prefix_est = sum(
                            (_turn_text_chars(contents[i]) // 4) + self._turn_thoughts.get(i, 0)
                            for i in range(cached_limit)
                        )
                        invalidated_cached_tokens = max(
                            suffix_invalidated,
                            max(0, full_prefix_est - self._retained_cache_floor),
                        )
                elif (
                    first_idx == 1
                    and total_pairs_seen > 12
                    and self._last_cached_tokens > self._retained_cache_floor
                ):
                    invalidated_cached_tokens = max(
                        suffix_invalidated,
                        self._last_cached_tokens - self._retained_cache_floor - dead_tokens,
                    )
                else:
                    invalidated_cached_tokens = suffix_invalidated

                damped_cap = max(
                    self.k_keep + 1,
                    24 - max(0, (total_pairs_seen - 24) // 3),
                )
                expected_horizon = max(self.k_keep + 1, min(total_pairs_seen, damped_cap))
                if self._single_tip_cache:
                    # EOQ inter-epoch interval: compacting after m=len(eligible) steps instead
                    # of waiting to 2m saves cached reads over the next m steps at the cost of
                    # one full prefix-cache reset.
                    expected_horizon = min(expected_horizon, max(len(eligible), self.k_keep + 1))
                if has_final_output_write:
                    expected_horizon = min(expected_horizon, 4)
                if remaining_steps is not None:
                    expected_horizon = min(expected_horizon, max(0, int(remaining_steps)))

                expected_savings = expected_horizon * self.cache_read_ratio * dead_tokens
                rewrite_miss_cost = (1.0 - self.cache_read_ratio) * invalidated_cached_tokens
                if expected_savings < rewrite_miss_cost:
                    return 0, 0

        est_prev_req = sum(
            (_turn_text_chars(contents[i]) // 4) + self._turn_thoughts.get(i, 0)
            for i in range(max(0, len(contents) - 2))
        )
        est_before_first = sum(
            (_turn_text_chars(contents[i]) // 4) + self._turn_thoughts.get(i, 0)
            for i in range(first_idx)
        )
        static_overhead = max(0, self._last_prompt_tokens - est_prev_req)
        resolved_contract = (
            deliverable_contract.strip()
            or _extract_deliverable_contract_from_contents(contents)
        )

        n_pairs = 0
        dropped_thoughts = 0
        last_eligible_idx = eligible[-1]
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
            fr_outputs: list[str] = []
            for up in u_parts:
                if "functionResponse" in up and isinstance(up["functionResponse"], dict):
                    resp_d = up["functionResponse"].get("response")
                    fr_outputs.append(
                        str(resp_d.get("output", "")) if isinstance(resp_d, dict) else ""
                    )

            for rcpt in _extract_computed_receipts("\n".join(fr_outputs)):
                if rcpt not in self._compacted_computed_receipts:
                    self._compacted_computed_receipts.append(rcpt)

            call_lines: list[str] = []
            state_lines: list[str] = []
            fc_idx = 0
            for p in m_parts:
                if "functionCall" in p and isinstance(p["functionCall"], dict):
                    fc = p["functionCall"]
                    fc_name = str(fc.get("name") or "tool")
                    fc_args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                    c_sum, s_note = _summarize_call(fc)
                    paired_out = fr_outputs[fc_idx] if fc_idx < len(fr_outputs) else ""
                    fc_idx += 1
                    receipts = extract_mutation_receipts(paired_out)
                    if self._is_mutating(fc_name, fc_args) or receipts:
                        call_lines.append("[mutated] " + c_sum)
                        mut_lbl = _compact_mutation_label(c_sum)
                        if receipts:
                            mut_lbl = f"{mut_lbl} -> {'; '.join(receipts[:2])}"
                        self._compacted_mutations.append(mut_lbl)
                        self.mutations_preserved += 1
                    else:
                        call_lines.append("[called] " + c_sum)
                    if s_note and _is_fact_bearing_state_note(s_note):
                        cleaned_note = " ".join(s_note.splitlines())[:280]
                        if cleaned_note not in state_lines:
                            state_lines.append(cleaned_note)
                            self.state_notes_preserved += 1
                        if cleaned_note not in self._compacted_state_notes:
                            self._compacted_state_notes.append(cleaned_note)

            ledger_prefix = ""
            if idx == last_eligible_idx:
                recap_lines: list[str] = []
                if self._compacted_mutations:
                    recent_muts = self._compacted_mutations[-12:]
                    extra_cnt = len(self._compacted_mutations) - len(recent_muts)
                    head_tag = (
                        f"[prior-mutations ({len(self._compacted_mutations)} total, latest {len(recent_muts)})] "
                        if extra_cnt > 0
                        else f"[prior-mutations ({len(recent_muts)})] "
                    )
                    recap_lines.append(head_tag + " | ".join(recent_muts))
                ordered_notes = _order_verified_notes(self._compacted_state_notes)
                recent_receipts = self._compacted_computed_receipts[-10:]
                if ordered_notes or recent_receipts or resolved_contract:
                    recap_lines.append("[compaction-recap]")
                    if ordered_notes:
                        recap_lines.append(
                            "[verified-notes: " + " | ".join(ordered_notes) + "]"
                        )
                    if recent_receipts:
                        recap_lines.append(
                            "[computed-receipts: " + " | ".join(recent_receipts) + "]"
                        )
                    if resolved_contract:
                        recap_lines.append(resolved_contract)
                if recap_lines:
                    ledger_prefix = "\n".join(recap_lines) + "\n"

            state_prefix = (
                "\n".join(f"[state] {sn}" for sn in state_lines) + "\n"
                if state_lines
                else ""
            )
            prefix = ("\n".join(visible_texts)[:800] + "\n") if visible_texts else ""
            m_summary = (
                (ledger_prefix + state_prefix + prefix + "\n".join(call_lines)).strip()
                or "[called]"
            )

            out_chunks: list[str] = []
            media_parts: list[dict[str, Any]] = []
            for p in u_parts:
                if "functionResponse" in p and isinstance(p["functionResponse"], dict):
                    fr = p["functionResponse"]
                    resp = fr.get("response")
                    resp_map = resp if isinstance(resp, dict) else {}
                    raw_txt = str(resp_map.get("output", ""))
                    orig_len = len(raw_txt)
                    txt, _ = self._transform_output_text(raw_txt, self._seen_ui_body_hashes, idx)
                    if self.strip_superseded_ui_controls and "=== Interactive Elements (" in raw_txt:
                        self.ui_controls_stripped_chars += max(0, orig_len - len(txt))
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

        for m_idx in range(first_idx, min(cutoff_idx, len(contents))):
            if m_idx not in eligible_set and contents[m_idx].get("role") == "model":
                m_parts_cur = contents[m_idx].get("parts") or []
                kept_parts: list[dict[str, Any]] = []
                stripped_t_chars = 0
                had_thought_part = False
                for p in m_parts_cur:
                    if isinstance(p, dict) and p.get("thought"):
                        had_thought_part = True
                        stripped_t_chars += len(str(p.get("text") or ""))
                    else:
                        kept_parts.append(p)
                popped_t = self._turn_thoughts.pop(m_idx, 0)
                if had_thought_part:
                    contents[m_idx]["parts"] = kept_parts or [{"text": "[called]"}]
                if popped_t > 0:
                    dropped_thoughts += popped_t
                elif stripped_t_chars > 0:
                    dropped_thoughts += stripped_t_chars // 4

        if n_pairs:
            self.epochs += 1
            self.pairs_compacted += n_pairs
            self.thoughts_compacted += dropped_thoughts
            self._last_compact_turn = (
                len(contents) if legacy_watermark_mode else self.pairs_compacted
            )
            if self._last_prompt_tokens > 0:
                self._compacted_first_idx = first_idx
                self._expected_retained_prefix_tokens = static_overhead + est_before_first
                self._awaiting_post_compact_usage = True
        return n_pairs, dropped_thoughts
