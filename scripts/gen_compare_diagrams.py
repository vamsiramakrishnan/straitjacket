#!/usr/bin/env python3
"""Generate the comparison-architecture diagrams in the repo's neo-brutalist theme.

Every diagram is emitted as a dark/light pair (`<name>.svg` / `<name>-light.svg`)
matching the existing `assets/readme/diagrams/*.svg` system: square corners, hard
+6px offset "shadow" rects, amber/green accents, embedded JetBrains Mono. The font
`<defs>` block is lifted verbatim from an existing diagram so glyphs render identically
and nothing external is fetched.

    python scripts/gen_compare_diagrams.py

Writes to assets/readme/diagrams/ and mirrors to site/public/diagrams/.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIAGRAMS = REPO / "assets" / "readme" / "diagrams"
SITE_DIAGRAMS = REPO / "site" / "public" / "diagrams"
FONT_SOURCE = DIAGRAMS / "flow.svg"

FONT = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace"

DARK = dict(
    bg="#0A0C10", frame="#30363D", tab="#F0B429", shadow="#30363D",
    box="#0F141B", stroke="#E6EDF3", title="#E6EDF3", muted="#6E7781",
    secondary="#8B949E", amber="#F0B429", amber_text="#F0B429",
    green="#3FB950", green_text="#3FB950", red="#F85149", red_text="#F85149",
)
LIGHT = dict(
    bg="#FFFFFF", frame="#1C232D", tab="#F0B429", shadow="#1C232D",
    box="#F4F6F8", stroke="#0A0C10", title="#0A0C10", muted="#66707A",
    secondary="#66707A", amber="#F0B429", amber_text="#8A6200",
    green="#3FB950", green_text="#187733", red="#D1242F", red_text="#B0202A",
)


def font_defs() -> str:
    src = FONT_SOURCE.read_text(encoding="utf-8")
    m = re.search(r"<defs>.*?</defs>", src, re.S)
    if not m:
        raise SystemExit("could not extract <defs> font block from flow.svg")
    return m.group(0)


DEFS = font_defs()


class Canvas:
    def __init__(self, width: int, height: int, title: str, desc: str, P: dict):
        self.w, self.h, self.P = width, height, P
        self.parts: list[str] = []
        self.title, self.desc = title, desc

    def esc(self, s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def rect(self, x, y, w, h, fill, stroke=None, sw=2):
        s = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}"{s}/>')

    def box(self, x, y, w, h, shadow=None):
        sh = shadow or self.P["shadow"]
        self.rect(x + 6, y + 6, w, h, sh)
        self.rect(x, y, w, h, self.P["box"], self.P["stroke"], 2)

    def text(self, x, y, s, size=12, weight=400, fill=None, anchor="middle"):
        fill = fill or self.P["title"]
        self.parts.append(
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="{FONT}" '
            f'font-size="{size}" font-weight="{weight}" fill="{fill}">{self.esc(s)}</text>'
        )

    def bullet(self, x, y, s, color=None, textfill=None, size=12):
        color = color or self.P["amber"]
        textfill = textfill or self.P["secondary"]
        self.rect(x, y - 7, 7, 7, color)
        self.text(x + 15, y, s, size=size, fill=textfill, anchor="start")

    def arrow_right(self, x1, x2, y, color=None, dashed=False, sw=3):
        color = color or self.P["amber"]
        d = ' stroke-dasharray="7 5"' if dashed else ""
        self.parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}" stroke-width="{sw}"{d}/>')
        self.parts.append(f'<path d="M{x2} {y-7} L{x2+13} {y} L{x2} {y+7} Z" fill="{color}"/>')

    def arrow_left(self, x1, x2, y, color=None, dashed=False, sw=3):
        color = color or self.P["green"]
        d = ' stroke-dasharray="7 5"' if dashed else ""
        self.parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}" stroke-width="{sw}"{d}/>')
        self.parts.append(f'<path d="M{x2} {y-7} L{x2-13} {y} L{x2} {y+7} Z" fill="{color}"/>')

    def vline(self, x, y1, y2, color=None, sw=3, dashed=False):
        color = color or self.P["amber"]
        d = ' stroke-dasharray="7 5"' if dashed else ""
        self.parts.append(f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{d}/>')

    def hline(self, x1, x2, y, color=None, sw=2, dashed=False):
        color = color or self.P["frame"]
        d = ' stroke-dasharray="7 5"' if dashed else ""
        self.parts.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="{color}" stroke-width="{sw}"{d}/>')

    def spine(self, x, y, h, color=None):
        self.rect(x, y, 12, h, color or self.P["amber"])

    def render(self) -> str:
        frame = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">',
            f'<title id="t">{self.esc(self.title)}</title>',
            f'<desc id="d">{self.esc(self.desc)}</desc>',
            DEFS,
            f'<rect width="{self.w}" height="{self.h}" fill="{self.P["bg"]}"/>',
            f'<rect x="1.5" y="1.5" width="{self.w-3}" height="{self.h-3}" fill="none" '
            f'stroke="{self.P["frame"]}" stroke-width="3"/>',
            f'<rect x="1.5" y="1.5" width="56" height="10" fill="{self.P["tab"]}"/>',
        ]
        return "\n".join(frame + self.parts + ["</svg>"]) + "\n"


def write_pair(name: str, builder):
    for suffix, P in (("", DARK), ("-light", LIGHT)):
        svg = builder(P)
        (DIAGRAMS / f"{name}{suffix}.svg").write_text(svg, encoding="utf-8")
        if SITE_DIAGRAMS.exists():
            (SITE_DIAGRAMS / f"{name}{suffix}.svg").write_text(svg, encoding="utf-8")
    print(f"wrote {name}.svg + {name}-light.svg")


# ---------------------------------------------------------------------------
# Diagram 1: the field as a treemap of contained ideas
# ---------------------------------------------------------------------------
def build_field_treemap(P: dict) -> str:
    W, H = 1200, 620
    c = Canvas(W, H, "The field, contained",
               "Each neighbouring tool does one thing well. straitjacket takes the idea "
               "and drops its cost: the amber strip on each tile is what the harness kept.", P)
    c.text(40, 44, "THE FIELD, CONTAINED", 16, 700, P["title"], "start")
    c.text(1160, 44, "amber = what the harness kept", 12.5, 400, P["muted"], "end")

    # Treemap tiles: (title, does-well, limitation, sj-took, emphasized)
    tiles = [
        # x, y, w, h, title, does_well, limitation, took, emphasis
        (32, 64, 430, 150, "Headroom", "rewriting wire proxy —", "silent evidence drops", "epoch-latched lossless rescue", True),
        (472, 64, 340, 150, "rtk", "bash-hook flood filter —", "lossy on success paths", "failure-asymmetric budgets", False),
        (822, 64, 346, 150, "Caveman", "terse prompting style —", "destroys quiet evidence", "cite-don't-quote handles", False),
        (32, 224, 276, 150, "Compaction", "reclaim a window —", "rewrites history", "checkpoint-then-rescue", False),
        (318, 224, 276, 150, "RAG / vectors", "recall without resend —", "probabilistic, no provenance", "deterministic addresses", False),
        (604, 224, 276, 150, "Ponytail", "the solution ladder —", "advisory, unmeasured", "A/B-adopted + ctx debt", False),
        (890, 224, 278, 150, "Maki", "one script, N ops —", "output vanishes to chat", "ctx eval, addressable", False),
        (32, 384, 1136, 150, "wozcode", "replace the tool surface —", "custom-tool schemas add window cost", "transparent substitution, zero new schema", True),
    ]
    for x, y, w, h, title, does, limit, took, emph in tiles:
        sh = P["amber"] if emph else P["shadow"]
        c.box(x, y, w, h, shadow=sh)
        c.text(x + 18, y + 34, title.upper(), 15, 700, P["title"], "start")
        c.text(x + 18, y + 60, does, 12, 400, P["muted"], "start")
        c.text(x + 18, y + 78, limit, 12, 400, P["muted"], "start")
        # nested "took" strip (treemap nesting)
        c.rect(x + 12, y + h - 46, w - 24, 32, P["amber"])
        c.text(x + 24, y + h - 25, "→ " + took, 12, 700, "#0A0C10", "start")

    c.hline(32, 1168, 560, P["frame"], 2)
    c.text(600, 592, "one corpus cannot referee this system — the harness answers each tool "
                     "on its own axis, losslessly", 12.5, 400, P["muted"], "middle")
    return c.render()


# ---------------------------------------------------------------------------
# Diagram 2: Headroom (rewriting proxy) vs the harness (capture at source)
# ---------------------------------------------------------------------------
def build_headroom_arch(P: dict) -> str:
    W, H = 1200, 430
    c = Canvas(W, H, "Headroom vs the harness",
               "Headroom sits on the wire and rewrites transcript history every request, "
               "compressing tool output lossily. straitjacket captures at the source into an "
               "addressable store and puts a bounded digest on the wire.", P)
    c.text(40, 44, "HEADROOM: REWRITE THE WIRE", 15, 700, P["title"], "start")
    # Top lane: agent - proxy - model
    c.box(40, 66, 210, 84)
    c.text(145, 100, "AGENT LOOP", 14, 700, P["title"])
    c.text(145, 122, "full transcript", 11.5, 400, P["muted"])
    c.box(360, 66, 260, 84, shadow=P["red"])
    c.text(490, 96, "HEADROOM PROXY", 13.5, 700, P["title"])
    c.text(490, 116, "compress messages,", 11.5, 400, P["muted"])
    c.text(490, 133, "rewrite history each call", 11.5, 400, P["muted"])
    c.box(730, 66, 200, 84)
    c.text(830, 100, "MODEL", 14, 700, P["title"])
    c.text(830, 122, "sees rewritten log", 11.5, 400, P["muted"])
    c.arrow_right(250, 347, 108, P["amber"])
    c.arrow_right(620, 717, 108, P["amber"])
    # drop marker
    c.box(980, 66, 188, 84, shadow=P["red"])
    c.text(1074, 96, "QUIET NEEDLE", 13, 700, P["red_text"])
    c.text(1074, 116, "silently dropped", 11.5, 400, P["muted"])
    c.text(1074, 133, "no trace, no address", 11.5, 400, P["muted"])
    c.vline(830, 150, 176, P["red"], 3)
    c.arrow_right(860, 966, 163, P["red"])

    c.hline(32, 1168, 210, P["frame"], 2)

    # Bottom lane: harness — capture at source
    c.text(40, 250, "STRAITJACKET: CAPTURE AT THE SOURCE", 15, 700, P["title"], "start")
    c.box(40, 272, 210, 96)
    c.text(145, 306, "TOOL OUTPUT", 14, 700, P["title"])
    c.text(145, 328, "unbounded bytes", 11.5, 400, P["muted"])
    c.text(145, 346, "never enters raw", 11.5, 400, P["muted"])
    c.box(360, 272, 260, 96, shadow=P["amber"])
    c.text(490, 300, "BIRTH GATE", 13.5, 700, P["amber_text"])
    c.text(490, 320, "immutable artifact store", 11.5, 400, P["muted"])
    c.text(490, 338, "every line addressed", 11.5, 400, P["muted"])
    c.text(490, 356, "quiet needle kept @ L14238", 11.5, 700, P["green_text"])
    c.box(730, 272, 200, 96)
    c.text(830, 306, "MODEL", 14, 700, P["title"])
    c.text(830, 328, "bounded digest", 11.5, 400, P["muted"])
    c.text(830, 346, "+ retrieval address", 11.5, 400, P["muted"])
    c.box(980, 272, 188, 96, shadow=P["green"])
    c.text(1074, 306, "ctx get", 13, 700, P["green_text"])
    c.text(1074, 328, "run:<id>#stdout", 11, 400, P["muted"])
    c.text(1074, 346, "--lines 14238:14241", 11, 400, P["muted"])
    c.arrow_right(250, 347, 320, P["amber"])
    c.arrow_right(620, 717, 320, P["amber"])
    c.arrow_left(980, 935, 320, P["green"])

    c.text(600, 404, "measured 2026-07-19: Headroom 357 tok, needle DROPPED · "
                     "ctx ~520 tok, needle SURVIVED with address", 12.5, 400, P["muted"])
    return c.render()


# ---------------------------------------------------------------------------
# Diagram 3: rtk & Caveman — two source-side losses, one addressable answer
# ---------------------------------------------------------------------------
def build_filters_arch(P: dict) -> str:
    W, H = 1200, 400
    c = Canvas(W, H, "rtk and Caveman vs the harness",
               "rtk filters floods at the shell hook; Caveman prompts the agent to say less. "
               "Both shrink bytes without keeping a route back. The harness keeps the route.", P)
    # rtk column
    c.spine(32, 44, 300)
    c.text(60, 60, "RTK — FILTER AT THE HOOK", 14, 700, P["title"], "start")
    c.box(60, 78, 230, 76)
    c.text(175, 110, "SHELL COMMAND", 13, 700, P["title"])
    c.text(175, 132, "floods stdout", 11.5, 400, P["muted"])
    c.arrow_right(290, 327, 116, P["amber"])
    c.box(360, 78, 230, 76, shadow=P["red"])
    c.text(475, 106, "rtk BASH HOOK", 13, 700, P["title"])
    c.text(475, 126, "<10ms single binary,", 11.5, 400, P["muted"])
    c.text(475, 143, "15-host reach", 11.5, 400, P["muted"])
    c.arrow_right(590, 627, 116, P["amber"])
    c.box(660, 78, 210, 76, shadow=P["red"])
    c.text(765, 106, "TRUNCATED", 13, 700, P["red_text"])
    c.text(765, 126, "lossy on success,", 11.5, 400, P["muted"])
    c.text(765, 143, "no addresses", 11.5, 400, P["muted"])

    # Caveman column
    c.text(60, 196, "CAVEMAN — PROMPT TO SAY LESS", 14, 700, P["title"], "start")
    c.box(60, 214, 230, 76)
    c.text(175, 246, "SYSTEM RULE", 13, 700, P["title"])
    c.text(175, 268, '"narrate tersely"', 11.5, 400, P["muted"])
    c.arrow_right(290, 327, 252, P["amber"])
    c.box(360, 214, 230, 76, shadow=P["red"])
    c.text(475, 242, "AGENT PROSE", 13, 700, P["title"])
    c.text(475, 262, "evidence squeezed", 11.5, 400, P["muted"])
    c.text(475, 279, "into a sentence", 11.5, 400, P["muted"])
    c.arrow_right(590, 627, 252, P["amber"])
    c.box(660, 214, 210, 76, shadow=P["red"])
    c.text(765, 242, "UNRECOVERABLE", 13, 700, P["red_text"])
    c.text(765, 262, "compressed prose", 11.5, 400, P["muted"])
    c.text(765, 279, "does not resolve", 11.5, 400, P["muted"])

    # Harness answer (right, spanning both)
    c.box(918, 120, 250, 128, shadow=P["green"])
    c.text(1043, 152, "THE HARNESS", 14, 700, P["title"])
    c.text(1043, 176, "keep bytes in the store,", 11.5, 400, P["muted"])
    c.text(1043, 194, "carry a cited handle", 11.5, 400, P["muted"])
    c.text(1043, 218, "cite-don't-quote,", 11.5, 700, P["green_text"])
    c.text(1043, 236, "failure-asymmetric budget", 11.5, 700, P["green_text"])
    c.arrow_right(870, 915, 116, P["green"])
    c.arrow_right(870, 915, 252, P["green"])

    c.hline(32, 1168, 328, P["frame"], 2)
    c.text(600, 362, "shrinking bytes is easy; keeping a resolvable route to the bytes you "
                     "dropped is the whole problem", 12.5, 400, P["muted"])
    return c.render()


def main() -> int:
    DIAGRAMS.mkdir(parents=True, exist_ok=True)
    write_pair("field-treemap", build_field_treemap)
    write_pair("headroom-arch", build_headroom_arch)
    write_pair("filters-arch", build_filters_arch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
