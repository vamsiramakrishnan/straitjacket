#!/usr/bin/env python3
"""Generate the Addressable Evidence visual system.

The visuals are deterministic, dependency-free SVGs. Each primitive is emitted
as desktop and compact dark/light pairs under assets/readme/diagrams, then
mirrored to the docs site.

    python scripts/gen_addressable_evidence_visuals.py
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "readme" / "diagrams"
SITE_OUT = ROOT / "site" / "public" / "diagrams"
FONT_SOURCE = OUT / "flow.svg"

# Deliberate system stack. Evidence text uses the embedded JetBrains Mono face;
# display text uses the ubiquitous Arial-compatible metrics instead of naming
# an unembedded web font that an <img>-loaded SVG cannot inherit from the page.
SANS = "Arial, Helvetica, sans-serif"
MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace"

DARK = {
    "bg": "#0D0F12",
    "surface": "#15181D",
    "surface2": "#1C2026",
    "ink": "#ECEAE3",
    "muted": "#8D939A",
    "faint": "#7D848D",
    "grid": "#272C33",
    "blue": "#6F96FF",
    "blue2": "#263A68",
    "amber": "#F0B429",
    "amber2": "#58451D",
    "red": "#FF7768",
    "red2": "#542925",
    "magenta": "#CF70E8",
    "green": "#63D3A4",
}

LIGHT = {
    "bg": "#F4F1E8",
    "surface": "#FCFAF4",
    "surface2": "#EAE6DB",
    "ink": "#16181D",
    "muted": "#62686C",
    "faint": "#5F6461",
    "grid": "#D8D4C9",
    "blue": "#174EB8",
    "blue2": "#DCE6FF",
    "amber": "#7A5200",
    "amber2": "#F4E4B9",
    "red": "#A82C20",
    "red2": "#F4D3CC",
    "magenta": "#A846C0",
    "green": "#0D6B49",
}


def font_defs() -> str:
    source = FONT_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"<defs>.*?</defs>", source, re.S)
    if not match:
        raise SystemExit("could not extract embedded font from flow.svg")
    return match.group(0)


DEFS = font_defs()


def load_receipts() -> dict[str, object]:
    field = json.loads((ROOT / "evals" / "field-needle-record.json").read_text(encoding="utf-8"))
    anchor = json.loads((ROOT / "evals" / "anchor-drift-2026-08-20.json").read_text(encoding="utf-8"))
    arms = {item["tool"]: item for item in field["arms"]}
    sj = arms["sj (ctx run logtemplate/v1)"]
    handle_match = re.search(r"\[ctx (run:[0-9a-f]+)", sj["output_excerpt"])
    if not handle_match:
        raise SystemExit("field-needle record has no ctx run handle")
    return {
        "lines": field["corpus"]["lines"],
        "raw_tokens": field["corpus"]["raw_tokens_o200k"],
        "needle_line": field["corpus"]["quiet_needle_line"],
        "truncated_tokens": arms["caveman (head+tail trunc)"]["out_tokens"],
        "digest_tokens": sj["out_tokens"],
        "run_handle": handle_match.group(1),
        "anchor": anchor,
    }


R = load_receipts()


class Canvas:
    def __init__(self, width: int, height: int, title: str, desc: str, p: dict[str, str]):
        self.width = width
        self.height = height
        self.title = title
        self.desc = desc
        self.p = p
        self.parts: list[str] = []

    @staticmethod
    def esc(value: str) -> str:
        return html.escape(str(value), quote=True)

    def rect(self, x, y, width, height, fill, stroke=None, sw=1, opacity=1):
        border = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'fill="{fill}" opacity="{opacity}"{border}/>'
        )

    def line(self, x1, y1, x2, y2, stroke, sw=1, dash=None, opacity=1):
        dashed = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"{dashed}/>'
        )

    def path(self, d, stroke, sw=2, fill="none", dash=None, opacity=1):
        dashed = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<path d="{d}" stroke="{stroke}" stroke-width="{sw}" fill="{fill}" '
            f'opacity="{opacity}"{dashed}/>'
        )

    def circle(self, cx, cy, radius, fill, stroke=None, sw=1):
        border = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.parts.append(f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{fill}"{border}/>')

    def text(
        self,
        x,
        y,
        value,
        size=14,
        fill=None,
        weight=400,
        anchor="start",
        family=SANS,
        spacing=0,
    ):
        self.parts.append(
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" '
            f'font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'letter-spacing="{spacing}" fill="{fill or self.p["ink"]}">'
            f'{self.esc(value)}</text>'
        )

    def multiline(self, x, y, lines, size=14, fill=None, weight=400, leading=20, family=SANS):
        for index, value in enumerate(lines):
            self.text(x, y + index * leading, value, size, fill, weight, family=family)

    def label(self, x, y, value, color=None):
        self.text(x, y, value.upper(), 11, color or self.p["muted"], 700, spacing=1.5)

    def arrow(self, x1, y1, x2, y2, color, sw=2, dash=None):
        self.line(x1, y1, x2, y2, color, sw, dash)
        if abs(x2 - x1) >= abs(y2 - y1):
            direction = 1 if x2 > x1 else -1
            points = f"{x2},{y2} {x2-9*direction},{y2-5} {x2-9*direction},{y2+5}"
        else:
            direction = 1 if y2 > y1 else -1
            points = f"{x2},{y2} {x2-5},{y2-9*direction} {x2+5},{y2-9*direction}"
        self.parts.append(f'<polygon points="{points}" fill="{color}"/>')

    def grid(self, x=32, y=28, width=None, height=None, step=64):
        width = width or self.width - x * 2
        height = height or self.height - y * 2
        for gx in range(x, x + width + 1, step):
            self.line(gx, y, gx, y + height, self.p["grid"], 1, opacity=0.42)
        for gy in range(y, y + height + 1, step):
            self.line(x, gy, x + width, gy, self.p["grid"], 1, opacity=0.42)

    def header(self, eyebrow: str, title: str, subtitle: str | None = None):
        self.label(48, 48, eyebrow, self.p["amber"])
        self.text(48, 88, title, 31, self.p["ink"], 750)
        if subtitle:
            self.text(48, 116, subtitle, 14, self.p["muted"], 400)

    def panel(self, x, y, width, height, fill=None, stroke=None):
        self.rect(x, y, width, height, fill or self.p["surface"], stroke or self.p["grid"], 1)

    def render(self) -> str:
        body = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" aria-labelledby="t d">',
            f'<title id="t">{self.esc(self.title)}</title>',
            f'<desc id="d">{self.esc(self.desc)}</desc>',
            DEFS,
            f'<rect width="{self.width}" height="{self.height}" fill="{self.p["bg"]}"/>',
            *self.parts,
            "</svg>",
        ]
        return "\n".join(body) + "\n"


def residency(p: dict[str, str]) -> str:
    c = Canvas(
        1200,
        700,
        "The expensive byte is the one that survives",
        "An illustrative seven-turn residency trace using the measured 302,628-token "
        "field-needle payload and its 521-token addressable digest. Native execution "
        "keeps the payload resident; containment retrieves one bounded region on demand.",
        p,
    )
    c.grid()
    c.header(
        "01 / residency trace",
        "THE EXPENSIVE BYTE IS THE ONE THAT SURVIVES",
        "Illustrative trace · payload and digest sizes from evals/field-needle-record.json",
    )

    c.panel(48, 146, 1104, 116)
    metrics = [
        (76, f'{R["raw_tokens"]:,}', "RAW TOKENS"),
        (382, "× 6", "RESIDENT TURNS"),
        (650, f'{R["raw_tokens"] * 6:,}', "UNCACHED TOKEN-TURNS"),
        (1012, "≈", "MODELLED LOAD", p["red"]),
    ]
    for item in metrics:
        x, value, label, *color = item
        c.text(x, 205, value, 34, color[0] if color else p["ink"], 750)
        c.label(x, 232, label, color[0] if color else p["muted"])

    left = 166
    step = 136
    top = 330
    for turn in range(1, 8):
        x = left + (turn - 1) * step
        c.text(x, 302, f"T{turn}", 12, p["muted"], 700, "middle", MONO)
        c.line(x, 316, x, 605, p["grid"], 1)

    c.label(48, top + 28, "native")
    c.label(48, top + 164, "contained", p["blue"])
    c.text(48, top + 54, "resident", 11, p["faint"], family=MONO)
    c.text(48, top + 190, "addressed", 11, p["faint"], family=MONO)

    # Native: initial reads, then a large result persists.
    c.rect(left - 30, top + 14, 62, 32, p["surface2"])
    c.text(left, top + 35, "read", 10, p["muted"], 700, "middle", MONO)
    for turn in range(2, 8):
        x = left + (turn - 1) * step
        c.rect(x - 56, top, 112, 60, p["red2"], p["red"], 1)
        c.text(x, top + 26, "log", 10, p["red"], 700, "middle", MONO)
        c.text(x, top + 45, f'{R["raw_tokens"] // 1000}k', 12, p["ink"], 700, "middle", MONO)
    c.line(left + step - 56, top + 74, left + step * 6 + 56, top + 74, p["red"], 2)
    c.text(left + step * 3.5, top + 96, "the same bytes remain resident", 12, p["red"], 650, "middle")

    # Contained: digest persists, an exact region appears only on demand.
    c.rect(left - 30, top + 150, 62, 32, p["surface2"])
    c.text(left, top + 171, "read", 10, p["muted"], 700, "middle", MONO)
    for turn in range(2, 8):
        x = left + (turn - 1) * step
        c.rect(x - 38, top + 144, 76, 44, p["blue2"], p["blue"], 1)
        c.text(x, top + 162, "digest", 9, p["blue"], 700, "middle", MONO)
        c.text(x, top + 179, str(R["digest_tokens"]), 11, p["ink"], 700, "middle", MONO)
    retrieve_x = left + 3 * step
    c.rect(retrieve_x - 48, top + 198, 96, 32, p["amber2"], p["amber"], 1)
    c.text(retrieve_x, top + 219, "get 21L", 10, p["amber"], 700, "middle", MONO)
    c.arrow(retrieve_x, top + 190, retrieve_x, top + 198, p["amber"], 2)

    c.panel(48, 630, 1104, 38, p["surface"])
    c.label(66, 654, "working-set rule", p["blue"])
    c.text(
        246,
        654,
        "keep the conclusion resident · page exact evidence only when the next decision needs it",
        13,
        p["ink"],
        550,
    )
    return c.render()


def evidence_fates(p: dict[str, str]) -> str:
    c = Canvas(
        1200,
        650,
        "Three fates for one test log",
        "The measured field-needle payload across three paths. Keeping everything floods "
        "context. Head-tail truncation drops the quiet needle. Addressable evidence keeps "
        "the needle and an exact path to every omitted byte.",
        p,
    )
    c.grid()
    c.header(
        "02 / evidence fates",
        f'THREE FATES FOR THE SAME {R["lines"]:,}-LINE LOG',
        f'Measured by evals/field-needle-record.json · quiet needle at L{R["needle_line"]:,}',
    )

    cards = [
        (48, "KEEP EVERYTHING", f'{R["raw_tokens"]:,}', "visible tokens", p["red"], p["red2"]),
        (420, "HEAD + TAIL", f'{R["truncated_tokens"]:,}', "visible tokens", p["amber"], p["amber2"]),
        (792, "ADDRESSABLE", f'{R["digest_tokens"]:,}', "visible tokens", p["blue"], p["blue2"]),
    ]
    for x, title, value, unit, color, tint in cards:
        c.panel(x, 150, 340, 430)
        c.label(x + 24, 182, title, color)
        c.text(x + 24, 230, value, 38, p["ink"], 750)
        c.text(x + 24, 254, unit, 12, p["muted"], 500)
        c.line(x + 24, 276, x + 316, 276, p["grid"], 1)

        if title == "KEEP EVERYTHING":
            for row in range(10):
                for col in range(16):
                    xx = x + 24 + col * 18
                    yy = 302 + row * 17
                    color2 = p["red"] if row == 7 and col == 11 else p["faint"]
                    c.rect(xx, yy, 10, 8, color2)
            c.text(x + 24, 492, "quiet needle preserved", 12, p["green"], 650)
            c.text(x + 24, 516, "raw bytes resident", 12, p["red"], 650)
            c.text(x + 24, 548, "EXACT  ✓    SMALL  ✕", 11, p["muted"], 700, family=MONO)
        elif title == "HEAD + TAIL":
            for row in range(4):
                for col in range(16):
                    c.rect(x + 24 + col * 18, 302 + row * 17, 10, 8, p["faint"])
            c.line(x + 24, 382, x + 316, 382, p["amber"], 3, "8 6")
            c.text(x + 24, 410, "head / tail only", 11, p["amber"], 700, family=MONO)
            c.circle(x + 240, 456, 6, p["red"])
            c.text(x + 258, 461, f'needle L{R["needle_line"]:,}', 11, p["red"], 700, family=MONO)
            c.text(x + 24, 516, "quiet needle lost", 12, p["red"], 650)
            c.text(x + 24, 548, "EXACT  ✕    SMALL  ✓", 11, p["muted"], 700, family=MONO)
        else:
            c.rect(x + 24, 302, 292, 72, tint, color, 1)
            c.text(x + 40, 326, "templates 11 → 20,000L", 10, color, 700, family=MONO)
            c.text(x + 40, 348, f'quiet needle  L{R["needle_line"]:,}', 10, p["ink"], 700, family=MONO)
            c.text(x + 40, 366, "address  available", 10, p["muted"], 400, family=MONO)
            c.rect(x + 24, 424, 292, 52, p["surface2"], p["grid"], 1)
            c.text(x + 40, 446, f'{R["run_handle"]}#stdout', 10, color, 700, family=MONO)
            c.text(x + 40, 465, f'--lines {R["needle_line"]}:{R["needle_line"] + 3}', 10, p["muted"], 400, family=MONO)
            c.arrow(x + 170, 382, x + 170, 416, color, 2, "5 4")
            c.text(x + 24, 516, "omission has an address", 12, p["green"], 650)
            c.text(x + 24, 548, "EXACT  ✓    SMALL  ✓", 11, p["muted"], 700, family=MONO)

    c.label(48, 618, "receipt")
    c.text(136, 618, f'field-needle-2026-07-20 · exact quiet needle retained at L{R["needle_line"]:,}', 12, p["ink"], 550)
    return c.render()


def digest_anatomy(p: dict[str, str]) -> str:
    c = Canvas(
        1200,
        690,
        "Anatomy of an evidence digest",
        "A receipt-derived field-needle log specimen annotated with immutable identity, outcome, "
        "template coverage, the quiet structural needle, and an exact continuation command.",
        p,
    )
    c.grid()
    c.header(
        "03 / digest specimen",
        "THIS IS AN INDEX, NOT A SUMMARY",
        "Receipt-derived specimen · each line states a fact or makes an omission reversible.",
    )
    c.panel(48, 152, 660, 470, p["surface"])

    code = [
        (f'[ctx {R["run_handle"]} profile=logtemplate/v1]', p["blue"]),
        ("exit: 0", p["green"]),
        (f'stdout: {R["lines"]:,} lines · 979.1 KiB', p["muted"]),
        ("templates:", p["ink"]),
        ("  11 cover 20,000/20,001 lines", p["amber"]),
        ("exceptional:", p["ink"]),
        (f'  L{R["needle_line"]} fell back to legacy gateway', p["red"]),
        ("coverage:", p["ink"]),
        ("  quiet needle: represented + addressed", p["green"]),
        ("next:", p["ink"]),
        (f'  ctx get {R["run_handle"]}#stdout', p["blue"]),
        (f'    --lines {R["needle_line"]}:{R["needle_line"] + 3}', p["blue"]),
    ]
    start_y = 196
    for index, (value, color) in enumerate(code, 1):
        y = start_y + (index - 1) * 31
        c.text(70, y, f"{index:02}", 10, p["faint"], 400, family=MONO)
        c.text(108, y, value, 12, color, 500, family=MONO)

    notes = [
        (196, 188, "IMMUTABLE IDENTITY", "same handle, same bytes", p["blue"]),
        (227, 258, "OUTCOME", "success is still evidence", p["green"]),
        (320, 328, "TEMPLATE CENSUS", "20,000 ordinary lines covered", p["amber"]),
        (382, 398, "QUIET NEEDLE", "rare without an ERROR keyword", p["red"]),
        (444, 468, "COVERAGE RECEIPT", "represented and addressable", p["green"]),
        (537, 548, "EXACT CONTINUATION", "copy, run, retrieve", p["blue"]),
    ]
    for source_y, note_y, title, detail, color in notes:
        c.circle(742, source_y - 5, 4, color)
        c.path(f"M708 {source_y-5} H730 V{note_y-5} H750", color, 1)
        c.label(764, note_y, title, color)
        c.text(764, note_y + 22, detail, 12, p["muted"], 450)

    c.panel(48, 642, 1104, 28, p["surface"])
    c.text(66, 661, "same evidence + same contract + same plan → byte-identical digest", 12, p["ink"], 650, family=MONO)
    return c.render()


def anchor_drift(p: dict[str, str]) -> str:
    c = Canvas(
        1200,
        610,
        "An address that survives an edit",
        "A content-anchored repository address verifies at the original position, follows "
        "the same content when it moves, and refuses when the content no longer exists.",
        p,
    )
    c.grid()
    c.header(
        "04 / anchor drift",
        "AN ADDRESS SHOULD NAME CONTENT, NOT A POSITION",
        "Line numbers are coordinates. The anchor supplies identity.",
    )
    xs = [48, 420, 792]
    titles = [
        ("01 / RECORD", "L40:52", p["blue"]),
        ("02 / EDIT", "+6 lines above", p["amber"]),
        ("03 / RESOLVE", "moved → L46:58", p["green"]),
    ]
    for x, (label, metric, color) in zip(xs, titles):
        c.panel(x, 154, 340, 340)
        c.label(x + 22, 184, label, color)
        c.text(x + 22, 222, metric, 24, p["ink"], 750, family=MONO)

    def code_frame(x, start, highlight, anchor):
        base_y = 256
        for row in range(15):
            line_no = start + row
            yy = base_y + row * 13
            is_hot = highlight[0] <= line_no <= highlight[1]
            if is_hot:
                c.rect(x + 22, yy - 10, 296, 12, p["blue2"])
            c.text(x + 30, yy, str(line_no), 8, p["faint"], 400, family=MONO)
            sample = "def verify_token(" if line_no == highlight[0] else ("    …" if is_hot else "·")
            c.text(x + 76, yy, sample, 8, p["blue"] if is_hot else p["faint"], 500, family=MONO)
        c.rect(x + 22, 460, 296, 26, p["surface2"], p["grid"], 1)
        c.text(x + 34, 478, anchor, 10, p["blue"], 700, family=MONO)

    code_frame(xs[0], 38, (40, 52), "@07407f1c")
    # Edit frame: inserted lines and shifted content.
    c.rect(xs[1] + 22, 258, 296, 58, p["amber2"], p["amber"], 1)
    c.text(xs[1] + 36, 282, "+ audit import", 10, p["amber"], 700, family=MONO)
    c.text(xs[1] + 36, 302, "+ five policy lines", 10, p["amber"], 700, family=MONO)
    c.arrow(xs[0] + 340, 330, xs[1] - 12, 330, p["amber"], 2)
    code_frame(xs[2], 44, (46, 58), "@07407f1c ✓")
    c.arrow(xs[1] + 340, 330, xs[2] - 12, 330, p["green"], 2)

    c.panel(48, 512, 1104, 72, p["surface"])
    anchor = R["anchor"]
    total = anchor["total"]
    outcomes = [
        (72, f'{total["cases"]:,} CASES', f'{anchor["files"]} files · four edit shapes', p["blue"]),
        (282, f'{total["verified"]} VERIFIED', "same content, same position", p["green"]),
        (470, f'{total["relocated"]:,} RELOCATED', "content followed", p["green"]),
        (696, f'{total["refused"]:,} REFUSED', "addressed content gone", p["red"]),
        (922, f'{total["anchored_wrong"]} WRONG', f'+{anchor["cost"]["overhead_pct"]}% address chars', p["amber"]),
    ]
    for x, label, detail, color in outcomes:
        c.label(x, 540, label, color)
        c.text(x, 562, detail, 10.5, p["muted"], 500)
    return c.render()


def host_lanes(p: dict[str, str]) -> str:
    c = Canvas(
        1200,
        740,
        "Host enforcement lanes",
        "Claude Code and Codex rewrite noisy calls and replace oversized output. "
        "Antigravity denies known command floods, while connector results can only be "
        "persisted after return. The ctx-owned Antigravity SDK is bounded by construction.",
        p,
    )
    c.grid()
    c.header(
        "05 / host enforcement",
        "THE HOST CONTRACT DETERMINES THE GATE",
        "Known command floods and connector results take different paths.",
    )

    stages = [(320, "CALL"), (530, "BIRTH"), (740, "RETURN"), (950, "MODEL")]
    for x, label in stages:
        c.label(x, 158, label, p["muted"])
        c.line(x, 172, x, 650, p["grid"], 1)

    lanes = [
        (200, "CLAUDE CODE", [("pytest", p["surface2"]), ("rewrite", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
        (300, "CODEX", [("pytest", p["surface2"]), ("rewrite", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
        (400, "ANTIGRAVITY / CMD", [("pytest", p["surface2"]), ("deny", p["amber2"]), ("ctx run ↻", p["amber2"]), ("bounded", p["surface"])]),
        (500, "ANTIGRAVITY / MCP", [("connector", p["surface2"]), ("allow", p["surface2"]), ("persist", p["red2"]), ("raw*", p["surface"])]),
        (600, "ANTIGRAVITY SDK", [("task", p["surface2"]), ("bounded tool", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
    ]
    for y, host, cells in lanes:
        c.label(48, y + 30, host, p["ink"])
        for index, ((value, fill), (x, _)) in enumerate(zip(cells, stages)):
            stroke = p["grid"]
            if value in {"rewrite", "digest"}:
                stroke = p["blue"]
            elif value in {"deny", "ctx run ↻"}:
                stroke = p["amber"]
            elif value in {"persist", "raw*"}:
                stroke = p["red"]
            elif value == "bounded":
                stroke = p["green"]
            c.rect(x - 54, y, 108, 52, fill, stroke, 1)
            text_color = p["muted"] if stroke == p["grid"] else stroke
            c.text(x, y + 31, value, 10, text_color, 700, "middle", MONO)
            if index < len(cells) - 1:
                dash = "5 4" if host == "ANTIGRAVITY / CMD" and index == 1 else None
                c.arrow(x + 58, y + 26, stages[index + 1][0] - 62, y + 26, stroke, 2, dash)

    c.panel(48, 678, 1104, 34, p["surface"])
    c.text(66, 700, "* denial costs one turn; agent re-issues ctx run; connector output cannot be substituted", 11, p["red"], 600, family=MONO)
    return c.render()


def mobile_header(c: Canvas, eyebrow: str, lines: list[str], subtitle: str) -> None:
    """A compact header whose type survives a phone-width render."""
    c.label(32, 42, eyebrow, c.p["amber"])
    c.multiline(32, 78, lines, 27, c.p["ink"], 750, 32)
    c.text(32, 126 if len(lines) == 1 else 150, subtitle, 14, c.p["muted"], 450)


def residency_mobile(p: dict[str, str]) -> str:
    c = Canvas(
        640,
        900,
        "Token residency on a narrow screen",
        "A compact trace: 302,628 raw tokens remain resident across six turns, while a "
        "521-token digest remains and one exact 21-line region is retrieved on demand.",
        p,
    )
    c.grid(x=24, y=24, width=592, height=852)
    mobile_header(c, "01 / residency trace", ["THE BYTE THAT SURVIVES", "IS THE EXPENSIVE ONE"], "Illustrative trace · measured payload and digest sizes")

    c.panel(32, 178, 576, 126)
    c.text(52, 226, f'{R["raw_tokens"]:,}', 34, p["ink"], 750)
    c.label(52, 251, "raw tokens")
    c.text(342, 226, f'{R["digest_tokens"]}', 34, p["blue"], 750)
    c.label(342, 251, "digest tokens", p["blue"])
    c.text(52, 281, f'≈ {R["raw_tokens"] * 6:,} uncached token-turns over six resident turns', 15, p["red"], 650)

    checkpoints = [(2, 70), (4, 216), (6, 362), (7, 508)]
    c.label(32, 348, "native / resident", p["red"])
    for turn, x in checkpoints:
        c.text(x, 376, f"T{turn}", 15, p["muted"], 700, "middle", MONO)
        c.rect(x - 54, 394, 108, 74, p["red2"], p["red"], 1)
        c.text(x, 424, "log", 15, p["red"], 700, "middle", MONO)
        c.text(x, 450, "302k", 18, p["ink"], 750, "middle", MONO)
    c.line(70, 484, 508, 484, p["red"], 3)
    c.text(289, 510, "the same payload stays resident", 15, p["red"], 650, "middle")

    c.label(32, 564, "contained / addressed", p["blue"])
    for turn, x in checkpoints:
        c.text(x, 592, f"T{turn}", 15, p["muted"], 700, "middle", MONO)
        c.rect(x - 54, 610, 108, 62, p["blue2"], p["blue"], 1)
        c.text(x, 638, "digest", 14, p["blue"], 700, "middle", MONO)
        c.text(x, 660, "521", 16, p["ink"], 750, "middle", MONO)
    c.rect(162, 700, 108, 44, p["amber2"], p["amber"], 1)
    c.text(216, 728, "get 21 lines", 14, p["amber"], 700, "middle", MONO)
    c.arrow(216, 680, 216, 700, p["amber"], 2)

    c.panel(32, 786, 576, 80, p["surface"])
    c.label(52, 816, "working-set rule", p["blue"])
    c.multiline(52, 842, ["Keep the conclusion resident.", "Page exact evidence only when the next decision needs it."], 15, p["ink"], 550, 20)
    return c.render()


def evidence_fates_mobile(p: dict[str, str]) -> str:
    c = Canvas(
        640,
        1260,
        "Three fates for one log on a narrow screen",
        "The measured 20,001-line field-needle log kept raw, truncated, and rendered "
        "as addressable evidence.",
        p,
    )
    c.grid(x=24, y=24, width=592, height=1212)
    mobile_header(c, "02 / evidence fates", ["THREE FATES FOR ONE", "20,001-LINE LOG"], f'Measured receipt · quiet needle at L{R["needle_line"]:,}')

    cards = [
        (180, "KEEP EVERYTHING", f'{R["raw_tokens"]:,}', p["red"], p["red2"]),
        (520, "HEAD + TAIL", f'{R["truncated_tokens"]:,}', p["amber"], p["amber2"]),
        (860, "ADDRESSABLE", f'{R["digest_tokens"]}', p["blue"], p["blue2"]),
    ]
    for y, title, metric, color, tint in cards:
        c.panel(32, y, 576, 294)
        c.label(54, y + 34, title, color)
        c.text(54, y + 86, metric, 40, p["ink"], 750)
        c.text(54, y + 112, "visible tokens", 15, p["muted"], 500)
        c.line(54, y + 134, 586, y + 134, p["grid"], 1)
        if title == "KEEP EVERYTHING":
            c.rect(54, y + 158, 532, 34, tint, color, 1)
            c.text(70, y + 181, "all 20,001 lines enter the transcript", 15, color, 700, family=MONO)
            c.text(54, y + 230, f'quiet needle L{R["needle_line"]:,} preserved', 16, p["green"], 650)
            c.text(54, y + 260, "EXACT  ✓     SMALL  ✕", 16, p["muted"], 700, family=MONO)
        elif title == "HEAD + TAIL":
            c.line(54, y + 168, 586, y + 168, color, 4, "10 8")
            c.text(54, y + 205, "middle discarded", 15, color, 700, family=MONO)
            c.circle(414, y + 204, 7, p["red"])
            c.text(436, y + 210, f'needle L{R["needle_line"]:,} lost', 15, p["red"], 700, family=MONO)
            c.text(54, y + 260, "EXACT  ✕     SMALL  ✓", 16, p["muted"], 700, family=MONO)
        else:
            c.rect(54, y + 152, 532, 64, tint, color, 1)
            c.text(72, y + 178, f'11 templates cover 20,000 lines · needle L{R["needle_line"]:,}', 14, p["ink"], 700, family=MONO)
            c.text(72, y + 202, f'{R["run_handle"]}#stdout · exact address retained', 14, color, 700, family=MONO)
            c.text(54, y + 260, "EXACT  ✓     SMALL  ✓", 16, p["muted"], 700, family=MONO)

    c.label(32, 1202, "receipt")
    c.text(122, 1202, "field-needle-2026-07-20 · exact quiet needle retained", 14, p["ink"], 600)
    return c.render()


def digest_anatomy_mobile(p: dict[str, str]) -> str:
    c = Canvas(
        640,
        1160,
        "Digest anatomy on a narrow screen",
        "A receipt-derived field-needle digest specimen followed by six evidence roles.",
        p,
    )
    c.grid(x=24, y=24, width=592, height=1112)
    mobile_header(c, "03 / digest specimen", ["THIS IS AN INDEX,", "NOT A SUMMARY"], "Receipt-derived specimen · every omission remains reversible")
    c.panel(32, 180, 576, 520, p["surface"])
    code = [
        (f'[ctx {R["run_handle"]} profile=logtemplate/v1]', p["blue"]),
        ("exit: 0", p["green"]),
        (f'stdout: {R["lines"]:,} lines · 979.1 KiB', p["muted"]),
        ("templates:", p["ink"]),
        ("  11 cover 20,000/20,001 lines", p["amber"]),
        ("exceptional:", p["ink"]),
        (f'  L{R["needle_line"]} fell back to legacy gateway', p["red"]),
        ("coverage:", p["ink"]),
        ("  quiet needle: represented + addressed", p["green"]),
        ("next:", p["ink"]),
        (f'  ctx get {R["run_handle"]}#stdout', p["blue"]),
        (f'    --lines {R["needle_line"]}:{R["needle_line"] + 3}', p["blue"]),
    ]
    for index, (value, color) in enumerate(code, 1):
        y = 220 + (index - 1) * 38
        c.text(52, y, f"{index:02}", 13, p["faint"], 500, family=MONO)
        c.text(92, y, value, 15, color, 550, family=MONO)

    notes = [
        ("IMMUTABLE IDENTITY", "same handle, same bytes", p["blue"]),
        ("OUTCOME", "success is still evidence", p["green"]),
        ("TEMPLATE CENSUS", "20,000 ordinary lines covered", p["amber"]),
        ("QUIET NEEDLE", "rare without an ERROR keyword", p["red"]),
        ("COVERAGE RECEIPT", "represented and addressable", p["green"]),
        ("EXACT CONTINUATION", "copy, run, retrieve", p["blue"]),
    ]
    for index, (title, detail, color) in enumerate(notes):
        col = index % 2
        row = index // 2
        x = 32 + col * 294
        y = 730 + row * 118
        c.panel(x, y, 282, 96, p["surface"])
        c.label(x + 18, y + 30, title, color)
        c.text(x + 18, y + 62, detail, 15, p["muted"], 500)
    c.panel(32, 1094, 576, 34, p["surface"])
    c.text(50, 1117, "same evidence + contract + plan → byte-identical digest", 13, p["ink"], 650, family=MONO)
    return c.render()


def anchor_drift_mobile(p: dict[str, str]) -> str:
    c = Canvas(
        640,
        1190,
        "Anchor drift on a narrow screen",
        "A content address recorded at lines 40 to 52 follows the same content to lines "
        "46 to 58 after six lines are inserted above it.",
        p,
    )
    c.grid(x=24, y=24, width=592, height=1142)
    mobile_header(c, "04 / anchor drift", ["NAME CONTENT,", "NOT A POSITION"], "Line numbers are coordinates. The anchor supplies identity.")
    stages = [
        (180, "01 / RECORD", "L40:52", "L40  def verify_token(\n…       11 addressed lines\nL52  return claims", p["blue"]),
        (410, "02 / EDIT", "+6 lines above", "+ audit import\n+ five policy lines\ncontent bytes unchanged", p["amber"]),
        (640, "03 / RESOLVE", "moved → L46:58", "L46  def verify_token(\n…       same 11 lines\nL58  return claims", p["green"]),
    ]
    for y, label, metric, specimen, color in stages:
        c.panel(32, y, 576, 196)
        c.label(52, y + 32, label, color)
        c.text(52, y + 76, metric, 27, p["ink"], 750, family=MONO)
        for row, line in enumerate(specimen.splitlines()):
            c.text(52, y + 116 + row * 24, line, 15, color if row != 1 else p["muted"], 600, family=MONO)
        if label != "02 / EDIT":
            c.text(424, y + 168, "@07407f1c" + (" ✓" if label.startswith("03") else ""), 15, p["blue"], 700, family=MONO)

    anchor = R["anchor"]
    total = anchor["total"]
    metrics = [
        ("1,920 CASES", "40 files · four edit shapes", p["blue"]),
        (f'{total["verified"]} VERIFIED', "same position", p["green"]),
        (f'{total["relocated"]:,} RELOCATED', "content followed", p["green"]),
        (f'{total["refused"]:,} REFUSED', "content gone", p["red"]),
        (f'{total["anchored_wrong"]} WRONG', f'+{anchor["cost"]["overhead_pct"]}% address chars', p["amber"]),
    ]
    for index, (label, detail, color) in enumerate(metrics):
        col = index % 2
        row = index // 2
        x = 32 + col * 294
        y = 880 + row * 92
        c.panel(x, y, 282, 72, p["surface"])
        c.label(x + 16, y + 28, label, color)
        c.text(x + 16, y + 52, detail, 14, p["muted"], 500)
    return c.render()


def host_lanes_mobile(p: dict[str, str]) -> str:
    c = Canvas(
        640,
        1060,
        "Host enforcement lanes on a narrow screen",
        "Five host paths show where a noisy call is rewritten, denied, persisted, or "
        "bounded by construction.",
        p,
    )
    c.grid(x=24, y=24, width=592, height=1012)
    mobile_header(c, "05 / host enforcement", ["THE HOST CONTRACT", "DETERMINES THE GATE"], "A denial, a substitution, and an observation are different paths.")
    lanes = [
        ("CLAUDE CODE", [("pytest", p["surface2"]), ("rewrite", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
        ("CODEX", [("pytest", p["surface2"]), ("rewrite", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
        ("ANTIGRAVITY / CMD", [("pytest", p["surface2"]), ("deny", p["amber2"]), ("ctx run ↻", p["amber2"]), ("bounded", p["surface"])]),
        ("ANTIGRAVITY / MCP", [("connector", p["surface2"]), ("allow", p["surface2"]), ("persist", p["red2"]), ("raw*", p["surface"])]),
        ("ANTIGRAVITY SDK", [("task", p["surface2"]), ("bounded", p["blue2"]), ("digest", p["blue2"]), ("bounded", p["surface"])]),
    ]
    for row, (host, cells) in enumerate(lanes):
        y = 184 + row * 156
        c.label(32, y, host, p["ink"])
        for index, (value, fill) in enumerate(cells):
            x = 32 + index * 146
            stroke = p["grid"]
            if value in {"rewrite", "digest"}:
                stroke = p["blue"]
            elif value in {"deny", "ctx run ↻"}:
                stroke = p["amber"]
            elif value in {"persist", "raw*"}:
                stroke = p["red"]
            elif value == "bounded":
                stroke = p["green"]
            c.rect(x, y + 22, 122, 64, fill, stroke, 1)
            text_color = p["muted"] if stroke == p["grid"] else stroke
            c.text(x + 61, y + 60, value, 14, text_color, 700, "middle", MONO)
            if index < 3:
                dash = "5 4" if host == "ANTIGRAVITY / CMD" and index == 1 else None
                c.arrow(x + 124, y + 54, x + 142, y + 54, stroke, 2, dash)
    c.panel(32, 984, 576, 42, p["surface"])
    c.text(48, 1011, "* deny costs one turn · connector output cannot be substituted", 13, p["red"], 650, family=MONO)
    return c.render()


BUILDERS = {
    "ae-residency": residency,
    "ae-residency-mobile": residency_mobile,
    "ae-evidence-fates": evidence_fates,
    "ae-evidence-fates-mobile": evidence_fates_mobile,
    "ae-digest-anatomy": digest_anatomy,
    "ae-digest-anatomy-mobile": digest_anatomy_mobile,
    "ae-anchor-drift": anchor_drift,
    "ae-anchor-drift-mobile": anchor_drift_mobile,
    "ae-host-lanes": host_lanes,
    "ae-host-lanes-mobile": host_lanes_mobile,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when generated assets are stale")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if SITE_OUT.exists():
        SITE_OUT.mkdir(parents=True, exist_ok=True)
    stale: list[Path] = []
    for name, builder in BUILDERS.items():
        for suffix, palette in (("", DARK), ("-light", LIGHT)):
            content = builder(palette)
            path = OUT / f"{name}{suffix}.svg"
            if args.check:
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    stale.append(path)
            else:
                path.write_text(content, encoding="utf-8")
            if SITE_OUT.exists():
                site_path = SITE_OUT / path.name
                if args.check:
                    if not site_path.exists() or site_path.read_text(encoding="utf-8") != content:
                        stale.append(site_path)
                else:
                    site_path.write_text(content, encoding="utf-8")
        verb = "checked" if args.check else "wrote"
        print(f"{verb} {name}.svg + {name}-light.svg")
    if stale:
        for path in stale:
            print(f"STALE {path.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
