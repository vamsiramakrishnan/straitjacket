# Repository addressing

- `repo:` resolves to the active workspace root.
- Paths are always repo-relative in model-visible output.
- In multi-root sessions, use `ws:<alias>/repo:<path>` when the target cannot be inferred.
- Search respects `.gitignore` plus `.ctxignore` by default.
- Returned repository evidence is snapshot-on-read and remains retrievable after the working file changes.
- Symlinks that escape the workspace are rejected unless the user explicitly authorizes outside-root access.
- Nested Git repositories are separate workspaces by default.
