# Incident triage console (phase 1)

Build a single-page web app: an on-call **incident triage console** for a small
SRE team. It is a data-dense UI — a filterable, sortable table with a detail
panel — and it must be driven entirely from the seed dataset below (hard-code
it; no network calls, no database).

## Seed data (use exactly these eight incidents, in this order)

| id | title | severity | status | service | opened |
|---|---|---|---|---|---|
| INC-101 | Checkout 5xx spike | sev1 | open | checkout | 2026-07-01 |
| INC-102 | Slow search queries | sev3 | open | search | 2026-07-02 |
| INC-103 | Payment webhook retries | sev2 | ack | payments | 2026-07-03 |
| INC-104 | CDN cache miss storm | sev2 | open | edge | 2026-07-04 |
| INC-105 | Auth token refresh loop | sev1 | ack | auth | 2026-07-05 |
| INC-106 | Report export timeout | sev4 | resolved | reports | 2026-07-06 |
| INC-107 | Mobile crash on cold start | sev3 | resolved | mobile | 2026-07-07 |
| INC-108 | Queue backlog growth | sev2 | open | queue | 2026-07-08 |

## What it does

- Shows every incident as a row in a table, with its id, title, severity,
  status, service and opened date.
- A row of **severity filter chips** (sev1 … sev4) narrows the table. In this
  phase the chips are **single-select**: clicking a chip shows only that
  severity; clicking the active chip again clears the filter.
- Clicking a column header **sorts** by that column, toggling ascending /
  descending.
- Clicking a row opens a **detail panel** beside the table showing that
  incident's full record.
- A live **count** of currently visible rows.
- The current filter is reflected in the **URL hash**, so a filtered view can be
  shared and restored by reloading.

Design it like a real ops tool: legible density, clear severity affordance,
obvious selected state. Visual polish counts, but the acceptance contract below
is what is machine-checked.

## Acceptance (an automated browser test checks these; make them true)

- The table is `#incident-table`; each incident is a row element carrying
  `data-incident="INC-101"` (etc.) with the incident title in its text.
- All eight incidents render on first load.
- Severity chips are buttons with `data-sev="sev1"` … `data-sev="sev4"`. The
  active chip has `aria-pressed="true"`; inactive chips `aria-pressed="false"`.
- Clicking `data-sev="sev2"` leaves exactly the three sev2 incidents
  (INC-103, INC-104, INC-108) visible in `#incident-table`; clicking it again
  restores all eight.
- Single-select: with sev2 active, clicking `data-sev="sev1"` leaves exactly the
  two sev1 incidents visible (**not** five) — the previous chip deactivates.
- `#visible-count` contains the number of currently visible incidents.
- The element `#incident-table` carries `data-sort-key` and `data-sort-dir`
  attributes reflecting the current sort. Clicking the `severity` column header
  (`[data-sort="severity"]`) sets `data-sort-key="severity"`; clicking it again
  flips `data-sort-dir` between `asc` and `desc`.
- With `data-sort-key="severity"` and `data-sort-dir="asc"`, the first row in
  `#incident-table` is a sev1 incident.
- Clicking a row opens `#detail-panel`, which then has `data-open="true"`, and
  `#detail-title` contains that incident's title.
- With sev2 active, the URL hash contains `sev=sev2`. Loading the page at
  `#sev=sev1` starts with only the two sev1 incidents visible and the sev1 chip
  pressed.
