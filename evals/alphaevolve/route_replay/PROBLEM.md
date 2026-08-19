# Receipt-informed route compiler

Improve `choose_route(profile, routes) -> route_id`.

The input profile is privacy-safe structural telemetry: no raw prompt or source
content is available. A route is admissible only when it can run unattended,
provides every capability required by the task, and includes verification for a
mutation. Empirically failed routes are inadmissible for the matching profile.

Completion is a hard gate. Only after every search case completes should the
policy reduce actual dollars where usage is complete, then estimated dollars,
visible context, model turns, tool calls, and latency relative to the complete
general route. Partial or unavailable usage is unknown, not zero. Do not infer
semantic success from process exit alone; the live cases carry explicit
evidence labels.

Keep the policy deterministic, pure, and conservative for unknown or ambiguous
profiles. In particular, lexical coincidences such as `latest` and `testimony`
must not be treated as test tasks.

For general mutations, a three-stage route without a frontier planning turn has
live support only when the profile names a target, names acceptance tests,
states an explicit behavioral contract, and carries no high-risk scope marker.
Any missing signal or high-risk marker must retain the complete route.
