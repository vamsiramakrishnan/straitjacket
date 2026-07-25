# Todo app

Build a small web app: a to-do list.

Requirements:
- Add a task by typing into a text field and clicking **Add** (or pressing Enter).
- Show all tasks in a list.
- Mark a task **done** (clicking it, or a checkbox) — done tasks are visually
  distinguished.
- **Delete** a task.
- Tasks **persist across a page reload**.

## Acceptance (an automated browser test checks these; make them true)
- The text input has `id="new-task"`; the add button has accessible name `Add`.
- Each task renders inside an element with `class="task"` whose text contains
  the task text.
- A done task's `.task` element has the attribute `data-done="true"` (set it when
  the task is marked done).
- Each task has a control with accessible name `Delete` that removes it.
- Adding "Buy milk" then "Walk dog" shows two `.task` elements containing those
  texts.
- Marking "Buy milk" done sets `data-done="true"` on its `.task`.
- After a page reload both tasks are still present with their done state.
- Deleting "Walk dog" leaves exactly one `.task`.
