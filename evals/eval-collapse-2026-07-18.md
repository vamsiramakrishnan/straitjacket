# Programmable capture (`ctx eval`): eval set + first measurements

**Date:** 2026-07-18 · v0.19.0 · mechanism shipped this wave (the Maki
absorption), measured the same day. Two layers: a deterministic mechanical
harness (`evals/evalset_collapse.py`, CI-guarded by
`tests/test_evalset_collapse.py`) and a live mechanism-isolated A/B
(`evals/ab_eval_live.py`, n=2 pairs, haiku). Held-out rule respected: the
mechanical scenarios aggregate per-module *pass rates*; the live task asks
for per-module *p95 latency* — same corpus shape, computation the tuning
never saw.

## Layer 1 — mechanical arms (real executions, scripted best-case agent)

Real fixtures (seeded: 30 JSONL run-logs, ~6k records; an 8-file test
package), real subprocesses, the real digest pipeline. The baseline agent
is scripted *perfectly* — batched search, exact slices, zero wasted turns —
which is deliberately unfair to eval: these gaps are floors. What this
layer cannot see: the model's output tokens re-typing data between rounds
and per-round TTFB (~1.5–2s + one suffix cache write, Tura wave); reported
as round counts.

### S-A fan-out aggregate (30 files → per-module rates; cross-file arithmetic)

| arm | rounds | context entry | resend (residency) |
|---|---|---|---|
| naive (`cat runs/*.jsonl`) | 1 | 95,978 tok | 0 |
| rounds (30 bounded `get`s) | 30 | 36,104 tok | 523,463 tok |
| eval (one script) | 1 | **146 tok** | 97 tok |

Eval's answer graded exact against independent recomputation. The rounds
arm's slices were budget-truncated: **the task is not completable from
what entered context** — no existing verb computes cross-file aggregates.
This is the scenario class the verb exists for.

### S-B data-dependent branch (grep → pytest on matched files)

| arm | rounds | context entry | resend |
|---|---|---|---|
| naive | 2 | 296 tok | 12 tok |
| rounds (search + run) | 2 | 388 tok | 118 tok |
| pipeline (`ctx run --shell`, grep\|xargs) | 1 | 266 tok | 0 |
| eval | 1 | 185 tok | 0 |

The honest control: a **bash pipeline under `ctx run --shell` already
collapses stream-shaped chains** — eval's marginal value there is script
provenance, not rounds. Eval's round economy is *marginal* over pipelines
and only *decisive* when intermediates are structured (S-A).

### S-C flood + quiet needle (provenance net)

One script: 20k classification lines, one quiet anomaly, a summary tail.
Digest: **108 est tok vs ~100k raw**. The needle is absent from the digest
but recovered by `ctx search run:` with its line coordinate; the flood
swallowed even the *intended* SUMMARY tail — omitted **with an address**,
recovered via `ctx get --lines 20001`. (Scripting rule this teaches:
print only what the transcript needs.) This is the arm a Maki-style raw
sandbox cannot run: there, flood and needle vanish unaddressed.

### S-D wrong-script recovery (the blind-bet loss cost)

Poisoned record crashes the script mid-corpus (KeyError, file 17).

| arm | rounds | context entry |
|---|---|---|
| eval: fail → 1 slice → fixed rerun | 3 | **299 tok** |
| naive re-pay (raw chain twice) | 2 | 191,945 tok |

Traceback rode the failure budget with `File "<stdin>"` frames; the
poisoned record was located with one bounded slice. **Debug is retrieval,
not re-execution** — the store is what makes the blind bet cheap to lose.

## Layer 2 — live A/B (haiku, n=2 pairs, mechanism-isolated)

Both arms: real `claude -p`, identical fixture/task/tools/turn-cap, `ctx`
installed, one appended doctrine line. Only difference: the line forbids
vs recommends `ctx eval`. Grading mechanical against independent ground
truth (`SLOWEST: catalog p95=865`).

| run | arm | turns | duration | cost | out tok | cache-write tok | strict format | content |
|---|---|---|---|---|---|---|---|---|
| 1 | no-eval | 5 | 24.6s | $0.0787 | 1,660 | 29,224 | ✅ | ✅ |
| 1 | eval | 4 | 18.1s | **$0.0289** | 1,039 | **6,191** | ❌ | ✅ |
| 2 | no-eval | 4 | 22.4s | $0.0309 | 1,224 | 6,697 | ✅ | ✅ |
| 2 | eval | 3 | 15.8s | $0.0262 | 1,102 | 6,079 | ❌ | ⚠️ in tool output only |

Three findings, none of them the one we expected:

1. **The benefit is real but behavioral.** The eval-doctrine arm was
   cheaper (−63%, −15%), faster (−26%, −29%), and one turn shorter in both
   runs. Run 1's no-eval arm flailed — `jq` attempt, `awk` attempt, then a
   python heredoc — and its 29k cache-write tokens are that flailing made
   visible on the wire. The doctrine collapsed the *approach* to one
   script on the first try.
2. **The verb was not adopted (0/2 eval-doctrine arms; 0/3 counting the
   wrapped run below).**
   Both arms wrote raw `python3 << EOF` heredocs through Bash. In a bare
   session nothing steers toward the verb; the model takes the familiar
   path. The measured win is therefore the *one-script discipline*
   (a Ponytail-class prompt effect), not yet the verb's gate/provenance.
3. **The terse doctrine leaked into the deliverable.** 2/2 eval-arm runs
   failed the strict "finish with `SLOWEST: ...`" format (run 1: prose
   restatement, correct values; run 2: answer left inside the tool output
   entirely). "Print only what the transcript needs" must be scoped to
   *scripts*, not final answers — a concrete skill-wording fix.

### Layer 2b — the same task under `ctx wrap` (hooks active, n=1)

| condition | turns | duration | cost | strict format | used `ctx eval` |
|---|---|---|---|---|---|
| wrapped (`ctx wrap claude`) | **3** | 17.3s | $0.0289 | ✅ | ❌ (script file + python) |

The wrapped session matched the eval-arm's economy (3 turns, $0.029),
produced the **only fully strict-correct cheap run of the five**, and
*still* did not adopt the verb: it wrote `analyze_p95.py` to its scratch
dir and ran python on it (both hook stages fired; nothing steered a
python invocation toward `ctx eval`). Three conditions, one conclusion:
**the one-script move is model-natural; the verb is not yet
discoverable.** The mechanism's gate/provenance benefits are real
(layer 1) but currently unclaimed in live sessions — a teaching-surface
problem, not a mechanism problem.

## What this changes

- **Adoption gap is the bottleneck, not the mechanism.** On the
  claude-code host the eval verb currently has no teaching surface (the
  skill ships in the Antigravity plugin; wrap injects one discipline
  line; the MCP server is bounded-only by policy). Follow-ups: teach the
  chain-collapse move in the hook's remediation for denied
  `python -c`/heredoc floods, and scope the terse-output rule to scripts.
- **S-B's pipeline control belongs in the doctrine**: stream-shaped →
  `ctx run --shell` pipeline; structured/branching → `ctx eval`; declared
  N-step → `ctx seq`.

## Caveats (read before citing)

- Live layer: n=2 pairs, one model (haiku), one task family, one seed.
  Directional, not conclusive. Sonnet and more seeds pending.
- Mechanical layer measures *transcript byte flow*, not model attention or
  output-token spend; its baseline agent is a perfect-play floor.
- The strict-format failures make the eval arms' task-success rate 0/2 by
  the letter of the task; content was correct in run 1 and present-but-
  buried in run 2. Both readings are reported; pick the strict one when
  skeptical.
- Grader and doctrine lines were written by the same author as the
  mechanism (no blind grading).
