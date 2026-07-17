# ADR 004: The plugin contains the skill

## Status

Accepted.

## Decision

The native Antigravity plugin embeds `skills/ctx-harness/SKILL.md`. The standalone skill distribution is provided only for environments where plugin installation is not possible.

## Consequences

- One plugin install registers routing metadata, hook, and MCP together.
- Repositories must not install both variants.
- The skill source must be generated/copied from one canonical file in the build to prevent drift.
