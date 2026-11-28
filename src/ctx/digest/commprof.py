"""comm/v1 — structural outline for communications & temporal transcripts:
RFC 5322 email (``.eml``), ``.mbox`` archives, iCalendar (``.ics``), vCard
(``.vcf``), and SRT/WebVTT transcripts (``.srt``, ``.vtt``).

Pure stdlib (``email.parser`` + single-pass linear scanners). Strips base64
MIME attachments from ``.eml`` summaries so threads and transcripts stay crisp
and line-addressed.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from typing import Any

from ctx import bounds
from ctx.digest.base import MAX_PARSE_BYTES, DigestContext, Profile, from_intake
from ctx.digest.intakeprof import mappable_text
from ctx.digest.shapeutil import Quoter, Windows, examine_line, fit, inline_limit
from ctx.textutil import fmt_bytes, fmt_int

_SRT_TIME_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
_VTT_TIME_RE = re.compile(
    r"^((?:\d{2}:)?\d{2}:\d{2}\.\d{3})\s*-->\s*((?:\d{2}:)?\d{2}:\d{2}\.\d{3})"
)
_VTT_SPEAKER_RE = re.compile(r"^<v\s+([^>]{1,48})>(.*)$")
_PREFIX_SPEAKER_RE = re.compile(r"^([A-Z][A-Za-z0-9 ._-]{1,32}):\s+(.+)$")
_MBOX_FROM_RE = re.compile(r"^From \S+ .+\d{4}\s*$")
_EML_HEADER_RE = re.compile(r"^(?:From|To|Cc|Date|Subject|Message-ID|MIME-Version|Return-Path|Received):[ \t]+", re.I)


@dataclass(frozen=True)
class CommItem:
    label: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CommDoc:
    kind: str  # eml | mbox | ics | vcf | srt | vtt
    summary: str
    meta: tuple[tuple[str, str], ...]
    items: tuple[CommItem, ...]
    parsed_lines: int


def _plural(n: int, word: str) -> str:
    return f"{fmt_int(n)} {word}" if n == 1 else f"{fmt_int(n)} {word}s"


def _parse_ics(lines: list[str], total: int) -> CommDoc | None:
    if not any(examine_line(r).strip() == "BEGIN:VCALENDAR" for r in lines[:10]):
        return None
    cal_name = ""
    events: list[CommItem] = []
    in_event = False
    ev_start = 0
    ev_summary = ""
    ev_dtstart = ""
    ev_attendees = 0
    for idx, row in enumerate(lines, 1):
        s = examine_line(row).strip()
        if s.startswith("X-WR-CALNAME:") and not cal_name:
            cal_name = s.split(":", 1)[1].strip()
        elif s == "BEGIN:VEVENT":
            in_event = True
            ev_start = idx
            ev_summary = ""
            ev_dtstart = ""
            ev_attendees = 0
        elif s == "END:VEVENT" and in_event:
            if len(events) < 300:
                att = f" · {ev_attendees} attendees" if ev_attendees else ""
                lbl = f"{ev_dtstart or '?'} {ev_summary or '(untitled)'}{att}".strip()
                events.append(CommItem(lbl, ev_start, idx))
            in_event = False
        elif in_event:
            if s.startswith("SUMMARY:"):
                ev_summary = s.split(":", 1)[1].strip()
            elif s.startswith("DTSTART"):
                ev_dtstart = s.split(":", 1)[1].strip() if ":" in s else ""
            elif s.startswith("ATTENDEE"):
                ev_attendees += 1
    meta = (("calendar", cal_name),) if cal_name else ()
    return CommDoc("ics", _plural(len(events), "event"), meta, tuple(events), total)


def _parse_vcf(lines: list[str], total: int) -> CommDoc | None:
    if not any(examine_line(r).strip() == "BEGIN:VCARD" for r in lines[:10]):
        return None
    contacts: list[CommItem] = []
    in_card = False
    st = 0
    fn = ""
    org = ""
    for idx, row in enumerate(lines, 1):
        s = examine_line(row).strip()
        if s == "BEGIN:VCARD":
            in_card = True
            st = idx
            fn = ""
            org = ""
        elif s == "END:VCARD" and in_card:
            if len(contacts) < 300:
                lbl = f"{fn or '(unnamed)'}" + (f" ({org})" if org else "")
                contacts.append(CommItem(lbl, st, idx))
            in_card = False
        elif in_card:
            if s.startswith("FN:"):
                fn = s.split(":", 1)[1].strip()
            elif s.startswith("ORG:"):
                org = s.split(":", 1)[1].strip().rstrip(";")
    return CommDoc("vcf", _plural(len(contacts), "contact"), (), tuple(contacts), total)


def _parse_subtitles(lines: list[str], total: int) -> CommDoc | None:
    is_vtt = examine_line(lines[0]).lstrip("\ufeff").strip().startswith("WEBVTT")
    rx = _VTT_TIME_RE if is_vtt else _SRT_TIME_RE
    cues: list[tuple[str, str, str, int, int]] = []  # (start_t, end_t, text, line_a, line_b)
    speakers: dict[str, int] = {}
    i = 0
    while i < total:
        m = rx.match(examine_line(lines[i]).strip())
        if not m:
            i += 1
            continue
        start_t, end_t = m.group(1), m.group(2)
        line_a = i + 1
        j = i + 1
        cue_parts: list[str] = []
        while j < total and lines[j].strip():
            txt = lines[j].strip()
            vm = _VTT_SPEAKER_RE.match(txt)
            if vm:
                sp = vm.group(1).strip()
                speakers[sp] = speakers.get(sp, 0) + 1
                cue_parts.append(f"{sp}: {vm.group(2).strip()}")
            else:
                pm = _PREFIX_SPEAKER_RE.match(txt)
                if pm:
                    sp = pm.group(1).strip()
                    speakers[sp] = speakers.get(sp, 0) + 1
                cue_parts.append(txt)
            j += 1
        cues.append((start_t, end_t, " ".join(cue_parts), line_a, j if j >= line_a else line_a))
        i = j + 1
    if len(cues) < 3:
        return None
    span_txt = f"{cues[0][0]} -> {cues[-1][1]}"
    # Group cues into evenly spaced chapters/windows (max 24 buckets).
    step = len(cues) // 20 if len(cues) > 20 else 1
    items: list[CommItem] = []
    for k in range(0, len(cues), step):
        grp = cues[k : k + step]
        st_t, _, sample, la, _ = grp[0]
        _, en_t, _, _, lb = grp[-1]
        items.append(CommItem(f"[{st_t}-{en_t}] {sample}", la, lb))
    meta_list: list[tuple[str, str]] = [("span", span_txt)]
    if speakers:
        top_sp = ", ".join(f"{k}({v})" for k, v in sorted(speakers.items(), key=lambda kv: -kv[1])[:5])
        meta_list.append(("speakers", top_sp))
    kind = "vtt" if is_vtt else "srt"
    return CommDoc(kind, f"{_plural(len(cues), 'cue')} · {span_txt}", tuple(meta_list), tuple(items), total)


def _parse_mbox(lines: list[str], total: int) -> CommDoc | None:
    if not _MBOX_FROM_RE.match(examine_line(lines[0])):
        return None
    starts: list[int] = [1]
    for idx in range(2, total + 1):
        if _MBOX_FROM_RE.match(examine_line(lines[idx - 1])):
            starts.append(idx)
    if len(starts) < 2:
        return None
    items: list[CommItem] = []
    for i, st in enumerate(starts[:250]):
        en = starts[i + 1] - 1 if i + 1 < len(starts) else total
        sender = ""
        subj = ""
        date = ""
        for j in range(st, min(en, st + 30)):
            h = examine_line(lines[j - 1])
            if not h.strip():
                break
            low = h.lower()
            if low.startswith("from:") and not sender:
                sender = h.split(":", 1)[1].strip()
            elif low.startswith("subject:") and not subj:
                subj = h.split(":", 1)[1].strip()
            elif low.startswith("date:") and not date:
                date = h.split(":", 1)[1].strip()
        lbl = f"#{i + 1} {sender or '?'} · {subj or '(no subject)'}" + (f" · {date}" if date else "")
        items.append(CommItem(lbl, st, en if en >= st else st))
    return CommDoc("mbox", _plural(len(starts), "message"), (), tuple(items), total)


def _parse_eml(text: str, lines: list[str], total: int) -> CommDoc | None:
    # Require >= 3 RFC 5322 email headers before the first blank line.
    hdr_hits = 0
    seen_core: set[str] = set()
    body_start = 1
    for idx, row in enumerate(lines[:80], 1):
        head = examine_line(row)
        if not head.strip():
            body_start = idx + 1
            break
        if _EML_HEADER_RE.match(head):
            hdr_hits += 1
            seen_core.add(head.split(":", 1)[0].lower())
    if hdr_hits < 3 or not ({"from", "subject"} <= seen_core or {"from", "to", "date"} <= seen_core):
        return None
    try:
        msg = email.message_from_string(text, policy=email.policy.compat32)
    except Exception:
        return None
    meta: list[tuple[str, str]] = []
    for key in ("From", "To", "Cc", "Date", "Subject"):
        val = msg.get(key)
        if val:
            meta.append((key.lower(), " ".join(str(val).split())))
    parts_summary: list[CommItem] = []
    attachments = 0
    b64_bytes = 0
    if msg.is_multipart():
        for p_idx, part in enumerate(msg.walk(), 1):
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            fname = part.get_filename()
            cte = str(part.get("Content-Transfer-Encoding") or "").lower()
            payload = part.get_payload(decode=False)
            psz = len(payload) if isinstance(payload, (str, bytes)) else 0
            if cte == "base64":
                b64_bytes += psz
            if fname or part.get_content_disposition() == "attachment":
                attachments += 1
                parts_summary.append(CommItem(f"attachment: {fname or '(unnamed)'} ({ctype}, {fmt_bytes(psz)})", body_start, total))
            else:
                parts_summary.append(CommItem(f"part #{p_idx}: {ctype} ({fmt_bytes(psz)})", body_start, total))
    else:
        parts_summary.append(CommItem(f"body: {msg.get_content_type()} (L{body_start}-L{total})", body_start, total))

    bits = [_plural(len(parts_summary), "MIME part")]
    if attachments:
        bits.append(_plural(attachments, "attachment"))
    if b64_bytes:
        bits.append(f"{fmt_bytes(b64_bytes)} base64 omitted")
    return CommDoc("eml", " · ".join(bits), tuple(meta), tuple(parts_summary[:24]), total)


def parse_comm(text: str) -> CommDoc | None:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total < 4:
        return None
    for fn in (_parse_ics, _parse_vcf, _parse_subtitles, _parse_mbox):
        doc = fn(lines, total)
        if doc is not None:
            return doc
    return _parse_eml(text, lines, total)


def _render_comm_lines(doc: CommDoc, q: Quoter, width: int, max_items: int) -> list[str]:
    out = [f"comm ({doc.kind}): {doc.summary}"]
    if doc.meta:
        mb = [f"{k}={q(v, min(width, 48))}" for k, v in doc.meta[:5]]
        out.append("headers: " + " · ".join(mb))
    shown = doc.items[:max_items]
    if shown:
        out.append(f"entries ({len(shown)} of {fmt_int(len(doc.items))} shown):")
        for it in shown:
            out.append(f"  L{it.start_line}-L{it.end_line} {q(it.label, width)}")
        if len(doc.items) > len(shown):
            out.append(f"  … +{fmt_int(len(doc.items) - len(shown))} more entries omitted")
    return out


def outline_file(
    path_str: str,
    policy: Any,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    max_items: int = 30,
) -> str | None:
    try:
        with open(path_str, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return None
    if not data or b"\x00" in data[:4096]:
        return None
    partial = len(data) > max_bytes
    if partial:
        data = data[:max_bytes]
    doc = parse_comm(mappable_text(data.decode("utf-8", "replace"), not partial))
    if doc is None:
        return None
    q = Quoter(68, policy)
    lines = _render_comm_lines(doc, q, 68, bounds.count(max_items))
    lines.extend(q.note())
    return "\n".join(lines)


class CommProfile(Profile):
    version = "comm/v1"

    def detect(self, ctx: DigestContext) -> str | None:
        view = ctx.stdout
        if ctx.stderr.bytes or not view.bytes or not view.text:
            return None
        if view.media_type.startswith("application/octet-stream"):
            return None
        if not from_intake(ctx):
            code = ctx.manifest.get("result", {}).get("exitCode")
            if code not in (0, None):
                return None
        if view.bytes <= inline_limit(ctx):
            return None
        doc = parse_comm(mappable_text(view.text, view.parsed_fully))
        if doc is None:
            return None
        return f"structured {doc.kind} ({doc.summary})"

    def render(self, ctx: DigestContext) -> str:
        view = ctx.stdout
        partial = not view.parsed_fully
        text = mappable_text(view.text, view.parsed_fully)
        doc = parse_comm(text)
        if doc is None:
            return "\n".join(list(ctx.header_lines()) + ["comm: unreadable"])
        handle = "run:PENDING#stdout"
        rid = "run:PENDING"
        total_lines = view.lines
        policy = ctx.ws.config.redaction
        windows = Windows(ctx, text, total_lines)

        def build(level: int) -> str:
            max_items, width = ((24, 70), (14, 58), (8, 46), (5, 36), (3, 28))[level]
            q = Quoter(width, policy)
            out = (
                [ctx.intake_line(total=partial) + f" · {doc.kind}"]
                if from_intake(ctx)
                else list(ctx.header_lines())
            )
            out.extend(_render_comm_lines(doc, q, width, max_items))
            if partial:
                out.append(
                    f"coverage: parsed L1-L{fmt_int(doc.parsed_lines)} of {fmt_int(total_lines)} "
                    f"lines (parse cap {fmt_bytes(MAX_PARSE_BYTES)})"
                )
            else:
                out.append(f"coverage: all {fmt_int(total_lines)} lines parsed")

            sugg: list[str] = []
            if doc.items:
                it0 = doc.items[0]
                end0 = windows.fit_end(it0.start_line, it0.end_line)
                sugg.append(f"ctx get {handle} --lines {it0.start_line}:{end0}")
            sugg.append(f"ctx search {rid} '<pattern>' --context 2")
            return "\n".join(out + self.next_lines(ctx, sugg) + q.note())

        return fit(ctx, build, 5)


__all__ = ["CommDoc", "CommItem", "CommProfile", "outline_file", "parse_comm"]
