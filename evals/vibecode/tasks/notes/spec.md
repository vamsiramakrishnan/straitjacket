# Notes app

Build a small web app: notes with search.

Requirements:
- Create a note with a **title** and **body**.
- Show all notes in a list (title visible).
- A **search** box filters the visible notes by title (case-insensitive
  substring).
- Notes **persist across a page reload**.

## Acceptance (an automated browser test checks these; make them true)
- Title input has `id="note-title"`, body input has `id="note-body"`, the create
  button has accessible name `Add note`.
- The search box has `id="search"`.
- Each note renders inside an element with `class="note"` whose text contains its
  title.
- Creating notes titled "Groceries" and "Workout" shows two `.note` elements.
- Typing "gro" into `#search` shows only the "Groceries" note (one `.note`
  visible); clearing the search shows both again.
- After a page reload both notes are still present.
