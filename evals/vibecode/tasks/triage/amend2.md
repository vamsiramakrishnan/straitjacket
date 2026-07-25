# Second design review — keyboard triage & real persistence (phase 3)

The board landed. Now the team wants to actually *work* in it during an
incident: hands on the keyboard, no mouse, and the triage decisions they make at
3am must still be there tomorrow — on someone else's laptop. That last part
means the board can no longer be a browser-only toy: status changes have to live
on the **server**.

This supersedes more of what you built. Read the reversals carefully — leaving
the old thing in place alongside the new one fails.

## The changes

1. **Status changes persist server-side.** Add a small HTTP API to the server
   you are already running: `GET /api/incidents` returns the current list as
   JSON, and `POST /api/incidents/<id>` with a JSON body `{"status": "ack"}`
   updates one incident. The board reads its data from the API on load and
   writes through it on every change. A status change must survive **a full
   server restart**, not just a page reload — so persist it to a file on disk,
   not to `localStorage`.
2. **Keyboard triage.** A roving cursor moves over the visible cards:
   - `j` moves the cursor to the next visible card, `k` to the previous, in the
     board's reading order (lane by lane, left to right, top to bottom). It does
     not wrap past the ends.
   - The card under the cursor carries `data-focused="true"` (and no other card
     does) and is the browser's `document.activeElement`.
   - `1`, `2`, `3` set the focused incident's status to `open`, `ack`,
     `resolved` respectively. The card moves to that lane immediately, the lane
     counts update, and the change is written through to the API.
   - `u` undoes the last status change — including one made by `u`'s own
     predecessor, so repeated `u` walks back the history — and writes the
     revert through to the API too.
3. **Text query, composed with the severity filter.** A text input `#query`
   narrows the board to incidents whose title *or* service contains the typed
   text, case-insensitively. It **composes** with the severity chips: with sev2
   active and `cache` typed, only sev2 incidents matching "cache" are shown.
4. **The URL hash carries both filters**, so a filtered search is shareable:
   `#sev=sev1,sev2&q=cache`. Loading that URL restores both the chips and the
   query box.
5. **Reversal — the count becomes a summary.** `#visible-count` is retired.
   In its place `#result-summary` reads `"<visible> of 8"` — e.g. `3 of 8`. The
   old `#visible-count` element must not exist any more.
6. **Reversal — the theme toggle becomes a three-state cycle.** Clicking
   `#theme-toggle` now cycles `light → dark → system → light`, writing that
   value to `data-theme` on `<html>`, and the chosen value still survives a
   reload.

Everything from phase 1 and phase 2 that is not contradicted above keeps
working: the same eight seed incidents, the three status lanes with live
counts, multi-select severity chips with `aria-pressed`, the modal dialog with
its focus behaviour and `Escape`, and the board — `#incident-table` is still
gone.

## Acceptance (phase 3 — the automated browser test checks these)

Carried forward:
- `#incident-table` does not exist; `#board` does, with lanes `data-column="open"`,
  `"ack"`, `"resolved"`, each holding an element with class `column-count`.
- The eight seed incidents render as cards carrying `data-incident="INC-101"`
  (etc.) in the lane matching their **current** status.
- Severity chips `data-sev="sev1"`…`"sev4"` are multi-select and carry
  `aria-pressed`; sev1+sev2 shows exactly INC-101, INC-103, INC-104, INC-105,
  INC-108.
- Clicking a card opens `#detail-modal` (`role="dialog"`, `aria-modal="true"`)
  with `#detail-title` showing that incident's title; `Escape` closes it.

Reversed:
- `#visible-count` does not exist anywhere in the DOM.
- `#result-summary` reads `8 of 8` unfiltered and `3 of 8` with sev2 active.
- `#theme-toggle` cycles `data-theme` through `light`, `dark`, `system` over
  three clicks and the value survives a reload.

New:
- `GET /api/incidents` returns JSON with the eight incidents and their statuses.
- Pressing `j` sets `data-focused="true"` on exactly one card, and that card is
  `document.activeElement`; pressing `j` again advances to the next visible card
  in reading order; `k` moves back to the first one.
- With the cursor on INC-102 (a sev3 `open` incident), pressing `2` moves it
  into the `ack` lane, and the `open` / `ack` lane counts become `3` / `3`.
- After that change, `GET /api/incidents` reports INC-102 as `ack`.
- Reloading the page keeps INC-102 in the `ack` lane.
- Pressing `u` returns INC-102 to the `open` lane and `GET /api/incidents`
  reports it as `open` again.
- Typing `cache` into `#query` leaves exactly INC-104 (CDN cache miss storm)
  visible, and `#result-summary` reads `1 of 8`.
- With sev2 active and `cache` in `#query`, the hash contains both `sev=sev2`
  and `q=cache`; loading `#sev=sev3&q=search` starts with only INC-102 visible,
  the sev3 chip pressed and `search` in `#query`.
