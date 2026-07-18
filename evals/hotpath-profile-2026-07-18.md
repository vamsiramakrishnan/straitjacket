# Hot-path profile: slowest and most-called components (2026-07-18)

Context: before re-running the scenario matrix, the previous run was killed
and replaced with cheap local simulations (cProfile + synthetic wire tests)
to find where time actually goes. This doc records what was measured, what
was refuted, and what was changed. All numbers from the session container
(Python 3.11, warm page cache).

## The harness's own hot paths (cProfile)

| Path | Cost | Most-called component | Verdict |
|---|---|---|---|
| `ctx run` digest of a 20k-line log | **854 ms** | `logprof._mask_token` → 180,011 per-char digit genexpr calls under 20,001 `mask_line` calls | **The** hot path. Fixed. |
| `ctx get` (first call on a blob) | 17 ms | `line_index` build 15 ms (fsync 4 ms) | Fine; amortized — later gets ~2 ms |
| `ctx search` (rg engine) | 7 ms | ripgrep subprocess | Fine |
| hook classify (warm) | 0.31 ms | `_load_guard_policy` 0.19 ms | Negligible vs ~10 ms Python startup |

Fix shipped in v0.7.1: `_mask_token` now short-circuits with a compiled
`\d` regex (C-speed scan instead of a Python genexpr per character) and
memoizes token→mask in a bounded dict (real logs repeat volatile tokens
across thousands of lines; the mask is a pure function, so the cache is
behavior-invisible). Re-profile: **854 ms → 139 ms (~6×)** on the same
fixture, identical templates.

The deeper lesson: the per-line regex cascade (7 mask patterns × every
digit-bearing token) is the digest tier's only super-linear-feeling cost.
Everything else in the harness is milliseconds. If a future profile shows
digest pain again, the next lever is line-level dedup before masking
(identical raw lines share a mask), not a faster language.

## Hypotheses about the v0.7 rematch slowdown (simulated, then judged)

The sjv7 arm ran 57% longer than naive. Three suspects were simulated
before touching anything:

- **H1 — forced identity encoding (proxy stripped Accept-Encoding):
  refuted.** Localhost transfer delta is negligible; even over a 50 Mbps
  WAN it's ~+25 ms per response. Still fixed properly in v0.7.1 (relay
  compressed bytes untouched, decompress only the observer's copy) because
  byte-mutation of the negotiation violated the relay's own invariant.
- **H2 — per-request TCP/TLS handshake (no connection reuse): refuted**
  for localhost (fresh vs pooled ≈ 0.0 ms delta). Real for remote
  upstreams, so v0.7.1 adds a small warm-connection pool anyway.
- **H3 — hook overhead: refuted.** ~90 ms per turn (Python startup
  dominates), two orders of magnitude below the gap.
- **Actual cause — output-token volume: confirmed.** naive 41,689 vs sjv7
  69,276 all-models output tokens; at ~80 tok/s generation each arm's
  token volume ≈ its entire wall-clock. The harness disciplined *intake*
  (reads, digests) but wrap mode carried no *emission* discipline — the
  Caveman lesson coming home. v0.7.1 injects a terse-narration /
  cite-coordinates system prompt in wrap print mode (`CTX_WRAP_NO_DISCIPLINE=1`
  to opt out).

## Instrumentation added so the next diagnosis is measurement, not simulation

Every `wire.jsonl` record now carries per-exchange timing and connection
provenance:

```json
{"seq": 12, "ms": {"connect": 0.0, "ttfb": 412.3, "total": 9120.7}, "reused_conn": true, ...}
```

- `connect`: TCP+TLS establishment (0 when the pooled connection was reused)
- `ttfb`: request-sent → response headers (≈ provider queue + prefill)
- `total`: request received → last relayed byte (total − ttfb ≈ generation
  time, i.e. output volume made visible per exchange)

Reading the matrix run's wire logs now attributes wall-clock per request to
connect vs prefill vs generation directly — no more inference from
aggregates.
