# ADR 001: Treat the transcript as an index

## Status

Accepted for v0.1.

## Decision

Potentially unbounded tool results are persisted as immutable artifacts. The model receives a bounded deterministic digest, opaque handle, and exact retrieval commands.

## Consequences

- Prompt history remains prefix-immutable within a conversation epoch.
- Retrieval becomes explicit and measurable.
- Quality depends on digest/search recall, which must be evaluated.
- Extra hops add latency, so search supports multiple patterns and bounded batch inspection.
