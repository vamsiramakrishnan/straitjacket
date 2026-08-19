# Managed portfolio iteration — 2026-08-18

## Bounds

- Gemini Enterprise engine: `gemini-enterprise-17847033_1784703340159`
- Generation model: `gemini-3.5-flash`
- Per campaign: one seed plus 11 evolved programs
- Evaluator concurrency: 2
- Generated code: timeout-bounded pure-Python sandbox
- Promotion: independent local holdout and adversarial gates required

## Recovery and escalation

Experiment:
`projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/17968323607576300335/alphaEvolveExperiments/14283683220929299572`

The seed scored `117.7925`. Ten evolved candidates tied it and one candidate
violated the hard completion contract and scored `-1000000`. No candidate beat
the seed, so no recovery policy was promoted.

The useful result is a confirmed plateau: authentication, permission, and
safety failures stop without another model attempt; missing evidence retrieves;
incomplete contracts and failed verification replan; capability limits may
escalate. The current deterministic policy already occupies the best point
found under this frozen search corpus.

## Receipt-informed routing

Experiment:
`projects/440790012685/locations/global/collections/default_collection/engines/gemini-enterprise-17847033_1784703340159/sessions/3644744797040338366/alphaEvolveExperiments/14283683220929298986`

The seed scored `156.48648`. Of 11 evolved candidates:

- one tied the seed;
- three selected incomplete or empirically inadmissible routes and received
  completion penalties from `-103000` to `-108000`;
- seven were sandbox-invalid and scored `-1000000`.

The tied program (`12938628388003921682`) passed local search
(`156.486479`), holdout (`151.011355`), and adversarial (`100.0`) evaluation,
but selected the same route for every case and produced no metric improvement.
It was not promoted.

The production-shaped seed currently reduces the frozen complete-general
baseline by 64.45% blended dollars, 55.48% visible tokens, 46.88% model turns,
53.57% tool calls, and 56.99% latency on search cases. That dollar figure is
only partly empirical: 1 of 8 selected-case costs is calibrated by complete
actual usage and 7 remain estimates. It must not be described as a measured
live cost reduction.

## Decision and next corpus increment

Both managed campaigns stopped without a promotable winner. The next run should
not spend against the same plateau. First add repeated actual-usage receipts for
answer, review, small-edit, and explicit-feature routes, plus direct naive arms.
Then evolve against the refreshed medians. A generated tie or a lower estimate
is never sufficient; live task completion and actual dollars decide release.
