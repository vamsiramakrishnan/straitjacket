# Integration references

The package layout and behavior were designed against the current Google Antigravity documentation and codelabs available on 2026-07-15.

- Google Antigravity — Plugins: https://antigravity.google/docs/plugins
- Google Antigravity CLI — Plugins & Skills: https://antigravity.google/docs/cli/plugins
- Google Antigravity — Agent Skills: https://antigravity.google/docs/skills
- Google Antigravity — Hooks: https://antigravity.google/docs/hooks
- Google Antigravity — MCP: https://antigravity.google/docs/mcp
- Google Codelab — Authoring Antigravity Skills: https://codelabs.developers.google.com/getting-started-with-antigravity-skills
- Google Codelab — Spec-Driven Development with Antigravity CLI: https://codelabs.developers.google.com/sdd-agy-cli

Operational assumptions to re-verify before implementation release:

- exact native filesystem tool names used by the installed Antigravity version;
- whether the hook process inherits the user's `PATH` consistently on macOS, Linux, Windows, IDE, and CLI;
- current global skill path, which has changed across Antigravity generations;
- whether a future plugin hook adds a supported tool-result transform or external compaction event.
