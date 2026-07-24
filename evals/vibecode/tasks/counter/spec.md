# Counter app

Build a small web app: a persistent counter.

Requirements:
- A single page shows a number, starting at **0**.
- A **"+"** button increments the number by 1; a **"−"** button decrements it.
- A **"Reset"** button sets it back to 0.
- The value **persists across a page reload** (localStorage or a backend — your choice).

## Acceptance (an automated browser test checks these; make them true)
- The current value is shown in an element with `id="value"` (its text is the number).
- The increment button has accessible name/text `+`, the decrement `−` (or `-`), the reset `Reset`.
- On load the value is `0`.
- Clicking `+` three times shows `3`; clicking `−` once shows `2`.
- After a full page reload the value is still `2`.
- Clicking `Reset` shows `0`.
