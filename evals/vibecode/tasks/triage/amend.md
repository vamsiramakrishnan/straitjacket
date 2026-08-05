# Design review — mid-build reshape (phase 2)

We showed phase 1 to the on-call team. The table works, but it does not match
how they actually triage: they think in **status lanes**, they filter by
**several severities at once**, and the side panel gets lost on a laptop screen.

Reshape the app. This **supersedes** the conflicting parts of phase 1 — some of
what you were asked to build must now be replaced, not merely added to. Keep
everything phase 1 asked for that is not contradicted here (same seed data, same
counts, same URL-hash sharing).

## The changes

1. **The table becomes a board.** Replace the single table with three status
   lanes — `open`, `ack`, `resolved` — side by side. Each incident is a card in
   the lane matching its status. The old table must be gone, not hidden behind
   a tab.
2. **Each lane shows its own count**, live, reflecting the current filter.
3. **Severity chips are now multi-select.** Clicking sev1 then sev2 shows sev1
   *and* sev2 incidents. Clicking an active chip removes just that severity.
   This reverses the phase-1 single-select rule.
4. **The detail panel becomes a modal dialog.** Clicking a card opens a real
   modal: it dims the board, closes on `Escape` and on a close button, and moves
   keyboard focus inside itself when it opens.
5. **Theme toggle.** A control that flips the app between light and dark and
   remembers the choice across a reload.
6. **The URL hash keeps working** and now encodes the multi-selection, so a
   two-severity view is still shareable and restorable.

## Acceptance (phase 2 — the automated browser test checks these)

Superseded / reversed:
- `#incident-table` no longer exists anywhere in the DOM.
- The board is `#board`. It contains exactly three lanes, elements carrying
  `data-column="open"`, `data-column="ack"` and `data-column="resolved"`.
- Every incident is a card carrying `data-incident="INC-101"` (etc.) **inside
  the lane matching its status** — e.g. `[data-column="ack"]` contains INC-103
  and INC-105, and no others.
- Each lane contains an element with class `column-count` whose text is the
  number of currently visible cards in that lane (`4`, `2`, `2` unfiltered).
- **Multi-select:** clicking `data-sev="sev1"` then `data-sev="sev2"` leaves
  exactly five cards visible (INC-101, INC-103, INC-104, INC-105, INC-108) and
  both chips at `aria-pressed="true"`. Clicking `data-sev="sev1"` again leaves
  the three sev2 cards and only sev2 pressed.

Still true from phase 1:
- The eight seed incidents, unchanged, all present on first load.
- `#visible-count` contains the number of currently visible incidents.
- Severity chips are still buttons with `data-sev="sev1"` … `data-sev="sev4"`
  carrying `aria-pressed`.

New:
- Clicking a card opens `#detail-modal`, which has `role="dialog"` and
  `aria-modal="true"` and is visible; `#detail-title` contains that incident's
  title.
- When the modal is open, `document.activeElement` is inside `#detail-modal`.
- Pressing `Escape` closes the modal (`#detail-modal` is no longer visible).
- `#theme-toggle` switches the theme: the `<html>` element's `data-theme`
  attribute flips between `light` and `dark`, and the chosen value survives a
  page reload.
- With sev1 and sev2 both active, the URL hash contains `sev=sev1,sev2`
  (severities comma-separated in ascending order). Loading the page at
  `#sev=sev2,sev4` starts with exactly the sev2 and sev4 incidents visible
  (INC-103, INC-104, INC-106, INC-108) and both those chips pressed.
