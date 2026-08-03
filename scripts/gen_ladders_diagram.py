#!/usr/bin/env python3
"""Generate the ladders-of-efficiency diagram (dark/light pair).

The system has nine conditionality ladders — nine places where it escalates
only as far as the work demands — and until now the only picture of them lived
inside `docs/LADDERS.md`. The README showed exactly one (capture) and the site
homepage showed none, so the idea that unifies the mechanisms was the one thing
a reader never saw.

The honest part of this diagram is the third column. `docs/LADDERS.md` is a
*conditionality audit*, not a feature tour: it records which ladders are
measured, which are merely enforced, and which are neither. A diagram that drew
nine confident ladders and quietly omitted that would be advertising, so
measurement status is drawn as prominently as the rungs.

    python scripts/gen_ladders_diagram.py

Reuses the Canvas/theme from gen_compare_diagrams.py so the visual system,
embedded font and dark/light pairing stay identical. Writes to
assets/readme/diagrams/ and mirrors to site/public/diagrams/.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_compare_diagrams import Canvas, write_pair  # noqa: E402

# The ladders come from `ctx.ladders`, not from a copy here. A diagram drawn
# from its own list is a fourth place the truth can live, and this project has
# spent a lot of this branch fixing exactly that shape of defect. Status is
# DERIVED the same way `ctx ladders` derives it — a ladder is "measured" when
# it declares a signal, "not scored" when it declares why it cannot be.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ctx.ladders import LADDERS as REGISTRY  # noqa: E402


def rows():
    for lad in REGISTRY:
        status = "measured" if lad.measurable else "unmeasured"
        yield lad.name, list(lad.rungs), lad.traversed_by, status


STATUS_TEXT = {
    "measured": "measured",
    "partial": "partial",
    "unmeasured": "not scored",
}


def build(P: dict) -> str:
    W, H = 1200, 740
    c = Canvas(
        W, H,
        "The ladders of efficiency",
        "Nine ladders, each escalating only as far as the work demands: solution, "
        "capture, emission budgets, graduated engagement, window pressure, guard "
        "modes, policy epochs, deployment tiers and model tiers. Each row shows its "
        "rungs left to right, who climbs it (the model, the hook, or a static "
        "setting), and whether its traversal is actually measured — three are, four "
        "are partial, two are not scored at all.",
        P,
    )

    c.text(40, 44, "THE LADDERS OF EFFICIENCY", 16, 700, P["title"], "start")
    c.text(1160, 44, "cheapest rung first · escalate only on evidence", 12.5, 400,
           P["muted"], "end")

    # Column guides
    x_name, x_rungs, x_who, x_stat = 40, 250, 940, 1050
    c.text(x_name, 74, "LADDER", 10.5, 700, P["muted"], "start")
    c.text(x_rungs, 74, "RUNGS  →  escalate only as far as needed", 10.5, 700,
           P["muted"], "start")
    c.text(x_who, 74, "CLIMBED BY", 10.5, 700, P["muted"], "start")
    c.text(x_stat, 74, "MEASURED?", 10.5, 700, P["muted"], "start")
    c.hline(x_name, 1160, 84, P["frame"], 2)

    colour = {
        "measured": (P["green"], P["green_text"]),
        "partial": (P["amber"], P["amber_text"]),
        "unmeasured": (P["muted"], P["muted"]),
    }

    y = 112
    row_h = 58
    for name, rungs, who, status in rows():
        accent, text_fill = colour[status]

        # The spine: a short amber bar marks where the ladder starts.
        c.spine(x_name - 12, y - 14, 22, accent)
        c.text(x_name + 10, y, name, 13, 700, P["title"], "start")

        # Rungs, left to right, cheapest first.
        rx = x_rungs
        for i, rung in enumerate(rungs):
            width = 8 * len(rung) + 22
            if rx + width > x_who - 30:
                break
            # First rung is filled (the one you should already be on).
            c.box(rx, y - 18, width, 26)
            c.text(rx + width / 2, y, rung, 11,
                   700 if i == 0 else 400,
                   P["title"] if i == 0 else P["secondary"])
            rx += width
            if i < len(rungs) - 1 and rx + 26 < x_who - 30:
                c.arrow_right(rx + 3, rx + 14, y - 5, accent, dashed=(i > 0), sw=2)
                rx += 30

        c.text(x_who, y, who, 11.5, 400, P["secondary"], "start")
        c.rect(x_stat, y - 12, 8, 8, accent)
        c.text(x_stat + 16, y, STATUS_TEXT[status], 11.5, 700, text_fill, "start")
        y += row_h

    # The honest footer: this is an audit, not a feature list.
    c.hline(x_name, 1160, y - 24, P["frame"], 2)
    # DERIVED. This sentence was hardcoded and went stale the moment the
    # registry became the source of truth -- it still read "three have
    # receipts" while the data said four. A caption that can disagree with its
    # own diagram is the drift this refactor exists to end.
    n_measured = sum(1 for lad in REGISTRY if lad.measurable)
    n_unscored = len(REGISTRY) - n_measured
    c.text(600, y + 4,
           f"{n_measured} of {len(REGISTRY)} ladders declare a traversal signal; "
           f"{n_unscored} cannot be scored and say why. Run `ctx ladders` for "
           "what THIS workspace recorded.",
           12.5, 400, P["muted"])
    c.text(600, y + 26,
           "A ladder nobody measures is a ladder nobody knows is being climbed — "
           "which is why this is an audit, not a feature list.",
           12.5, 400, P["muted"])
    return c.render()


def main() -> int:
    write_pair("ladders-efficiency", build)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
