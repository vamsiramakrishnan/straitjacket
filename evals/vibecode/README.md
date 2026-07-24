# Vibe-Code-Bench-style analog (run through the orchestrator)

**This is not the official [Vals AI Vibe Code Bench](https://vals.ai/benchmarks/vibe-code).**
That benchmark's tasks, grader (a Browser-Use agent), and OpenHands + Docker
environment are proprietary and access-gated — no public dataset, ~$10–20/app to
grade on their infra. You cannot run *their* benchmark without *their* access.

This is a faithful **local analog** that mirrors its shape so we can exercise
**our approach** (the harness collaboration orchestrator) on from-scratch web
builds:

- **Tasks** (`tasks/<name>/spec.md`): one-page natural-language web-app specs,
  each with an explicit "Acceptance" contract of observable behaviors.
- **Build**: `ctx.orchestrator` routes the build — `plan` → Claude Opus
  (`prefer:strong`), `build` → Claude Sonnet (real tools) — and hands the plan
  to the builder as a CAS `checkpoint:`. Only Claude has file/exec tools in this
  environment, so Claude builds; `--planner gemini` routes the (text-only) plan
  node to Gemini to show cross-vendor planning.
- **Grade** (`tasks/<name>/check.py`): a real headless-Chromium (Playwright) UI
  test drives the running app and returns pass/fail per substep. Score = fraction
  of substeps that pass — the same shape as the real benchmark's "% of substeps"
  metric.
- **Closed loop**: failing substeps are fed back to the builder for a bounded
  number of fix rounds (`--fix-rounds`).

## What it proves / doesn't

- **Proves**: our orchestrator can drive a real from-scratch web build to a
  browser-verified result, with model routing (Opus plans, Sonnet builds), CAS
  handoff, and a fix loop — measured pass-rate and real per-model cost.
- **Does NOT prove**: parity with Vals' actual scores. Different tasks, a simpler
  grader (Playwright vs their Browser-Use agent), and a much smaller scope than
  their 5-hour / 1000-turn apps. These are *our* numbers on *our* tasks.

## Run

```bash
# one task
python evals/vibecode/harness.py --task counter --fix-rounds 1
# the whole series (costs real money + minutes per task)
GEMINI_API_KEY=... python evals/vibecode/harness.py --all --fix-rounds 2
# route planning to Gemini instead of Opus (cross-vendor)
python evals/vibecode/harness.py --task todo --planner gemini
```

Needs the `claude` CLI authenticated in the environment (no `ANTHROPIC_API_KEY`
required), Playwright-python (`pip install playwright`), and the preinstalled
Chromium under `/opt/pw-browsers`. Each build runs `claude -p` with
`--permission-mode acceptEdits` and a tool allowlist **inside a `mktemp`
throwaway git repo** — never your real workspace.

## Adding a task

Create `tasks/<name>/spec.md` (an NL spec with an "Acceptance" section) and
`tasks/<name>/check.py` exposing `check(page, base_url) -> list[(label, bool)]`.
The app contract: the build must write an executable `./start.sh` that serves the
app on `$PORT`. Results are the fraction of `check` substeps that pass.
