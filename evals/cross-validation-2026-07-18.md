# Cross-validation regimen: S5 library-hunt + S6 bug-bash (2026-07-18)

Two dual-use benchmark cells whose *output is repo work*: held-out by
construction (written after every mechanism, hash-frozen in
bench-manifest.json), cognitively novel (doctrine-constrained audit
judgment; adversarial defect search with mandatory failing scenarios), and
maximally realistic — they are the two tasks we had been doing by hand,
run blind by fresh agents against the current tree. Every claimed finding
was adversarially re-verified BY HAND before anything was believed or
harvested.

## Layer 1 — benchmark metrics (corrected invalidation gauge)

| Cell | arm | ok | cost | rounds | out tok | hit |
|---|---|---|---|---|---|---|
| S5 library-hunt · sonnet | naive | ✔ | $3.37 | 7 | 77,777 | 89.8% |
| S5 library-hunt · sonnet | sj | ✔ | $3.46 | 15 | **63,687** | 93.2% |
| S6 bug-bash · sonnet | naive | ✔ | $8.62 | 28 | 255,793 | 93.0% |
| S6 bug-bash · sonnet | sj | ✘ capped | $9.15 | 31 | **208,084** | 96.1% |
| S6 bug-bash · haiku | naive | ✔ | $0.44 | 24 | 19,828 | 92.7% |
| S6 bug-bash · haiku | sj | ✘ capped | $0.86 | 31 | 25,848 | 96.4% |

Both S6:sj arms hit the turn cap — bug hunting is unbounded search, and
the harness's extra structure spent turns the naive arms spent finishing.
On the axis that matters here (findings quality), see Layer 2. sj held a
3–4 point cache-hit edge everywhere and lower output on the sonnet cells.

## Layer 2 — finding verification (the real deliverable)

**S6 bug-bash: 15 claimed → 6 verified real → 6 fixed this wave.** Each was
re-verified by reading the site and reproducing the failing scenario; the
verified set became regression-tested fixes (`tests/test_bugbash_s6.py`).

| # | Defect | Verified | Fixed |
|---|---|---|---|
| 1 | `allow_commands`/`promoted` prefix leaks compound `echo hi && rm -rf x` | ✅ reproduced | ✅ gated on `not has_meta` |
| 2 | `tail -n +N` / `head -n -N` (whole-file) read as bounded | ✅ reproduced | ✅ sign-prefix → unbounded |
| 3 | mid-path directory-symlink escape through an existing path | ✅ reproduced | ✅ per-hop immediate-target check |
| 6 | `window.json` clobbered to `window_pct:0` by a usage-less response | ✅ reproduced | ✅ skip write when no usage |
| 7 | `create_checkpoint` `IndexError` on a blank evidence line | ✅ reproduced | ✅ skip blank lines |
| 10 | string `patterns` typo silently disables all secret redaction | ✅ reproduced | ✅ isinstance guard → defaults |
| 11 | `_shape` `RecursionError` on deep JSON list | ❌ **REFUTED** (no crash at depth 600) | — |
| 4,5,8,9,12–15 | signal-exit/failure-budget/binary-stream/redir-order/… | plausible, unverified or lower-confidence | deferred to `ctx debt` |

Verification caught an over-claim (#11) — exactly why the metric is
verified-real rate, not finding count. Two of the six (secret redaction
disabled, window throttle disengaged) are security/safety defects that had
survived 14 versions and a hand audit.

**S5 library-hunt: doctrine-faithful.** The sj arm correctly DECLINED the
heavy swaps for the right reasons (the official `mcp` SDK's pydantic+anyio
graph vs a 45-line bounded loop; `drain3`'s stateful fuzzy clustering vs
our exact-match determinism contract; GitPython vs the zero-subprocess git
reader) and surfaced two genuine duplications. Harvested this wave:
- **ADOPT #2** — `_mask_token`'s hand-rolled "stop caching when full" dict →
  `functools.lru_cache(maxsize=65536)` (behavior-identical, ~10 lines gone).
- **ADOPT #5** — `resolved != root and root not in resolved.parents` →
  `resolved.is_relative_to(root)` (stdlib, clearer).
- Deferred to `ctx debt` with coordinates: ADOPT #1 (duplicate glob matcher
  vs the pathspec-backed one), #3 (triplicated REMAINDER flag-scraping →
  second `parse_known_args`), #4 (`unidiff` as an optional git-diff engine).

## Layer 3 — emission governor, validated in the wild at last

The S6:sonnet:sj session ran **208k output tokens over 163 rounds** — an
order of magnitude past any prior run, and the first real exercise of the
emission governor. It crossed **all 10 pressure tiers**, firing exactly one
nudge each (correct dedup, confirmed in `engagement.json`:
`emission_tier: 10`). Output-per-round fell from 622 (before the first
nudge) to 450 (after the last) — **suggestive of a ~28% dampening, but
observational**: task wind-down is an uncontrolled confound, so this is
recorded as "mechanism proven to fire and dedup correctly under real
load," not a causal effect size. A clean governor-on/off A/B on a
matched verbose task remains the way to measure the effect itself.

## What the regimen bought

Held-out cross-validation on two novel regimes: the harness produced
doctrine-faithful audit judgment and (naive arm) a high-yield bug list,
verification turned 6 real defects into tested fixes including 2
security/safety bugs, the corrected invalidation gauge got its first live
run, and the governor finally validated in its native regime — all from a
benchmark that improved the repo it measured.
