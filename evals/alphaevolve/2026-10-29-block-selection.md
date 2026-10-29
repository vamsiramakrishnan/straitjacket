# Query-aware evidence selection experiment

The objective is to reduce unnecessary context and retrieval without dropping
answer evidence. The production change uses task hints automatically for generic
text digests in orchestrator workers and `ctx wrap claude` sessions. A separate
experimental block selector preserves source bytes, fenced code, heading
dependencies, and explicit retrieval requirements when protected evidence cannot
fit. It has not replaced the production line selector.

## Local evidence

The frozen evaluator has six search, six holdout, and six adversarial scenarios.
Only search results reach AlphaEvolve. Candidates rank numeric block features;
the evaluator retains control of segmentation, byte budgets, dependency closure,
gold labels, and source text. Invalid permutations receive a negative score.

| Ranking | Search recall | Holdout recall | Adversarial recall |
| --- | ---: | ---: | ---: |
| Seed: relevance then source order | 91.67% | 83.33% | 100% |
| Relevance per byte | 83.33% | 83.33% | 83.33% |

The density baseline is rejected. These percentages measure answer-block recall
on synthetic examples, not agent success, fewer turns, provider tokens, or cost.
Even perfect synthetic recall cannot unlock promotion. Task-separated DeepSWE
replay and downstream task verification remain necessary.

Reproduce the local comparison with:

```sh
python -m evals.alphaevolve.block_selection.benchmark --output evals/alphaevolve/results/block-selection-local.json
```

## Managed configuration and API findings

The requested mixture is `gemini-3.8-flash:0.7,gemini-4-argon:0.3`, with six total
programs including the seed, one concurrent generation, and a 1800-second
duration cap. A finite sample need not include both models. The cap bounds work,
not dollars. Credentials remain in the ignored environment file.

```sh
python -m evals.alphaevolve.portfolio block-selection --env-file .alphaevolve.env --run --confirm-spend --max-programs 6 --concurrency 1 --model-mix gemini-3.8-flash:0.7,gemini-4-argon:0.3
```

The runner now validates its seed before creating resources, rejects nonfinite
model weights, records the starting evaluator fingerprint and full experiment
resource, and persists available results when the controller is interrupted.
It never installs the generated winner into production.

Google's [reference guide](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/reference-guide/api-reference)
lists the requested models and describes an idle timeout. However, the live
[Discovery Engine v1alpha schema](https://discoveryengine.googleapis.com/$discovery/rest?version=v1alpha)
omits `idleTimeout`; a create/get round trip also dropped it. The live schema
offers no immediate pause/stop method. The runner uses the accepted program and
duration caps and does not claim local termination cancels the remote campaign.

An earlier Flash 3.5 controller was interrupted when the model preference
arrived. Its resource is
`projects/440790012685/locations/global/collections/default_collection/engines/alpha-evolve-straitjacket/sessions/13983258531958084561/alphaEvolveExperiments/1749367647896286959`.
One evaluated mutation scored 83.33%, below the seed. The last remote inspection
still reported RUNNING, with three candidates including the seed and two
evaluations. It must not be described as remotely cancelled or as a successful
optimization. The interrupted run is excluded from promotion.

## Flash 3.8 / Argon result

The service accepted both requested model IDs and weights for experiment
`10134924543544101561` in session `2969674059555146770` under the same engine.
Three mutations were evaluated before the installed client's 120-second idle
heuristic ended its local loop. The remote state remained RUNNING, so this is
an incomplete campaign, not a completed six-program run.

| Program | Search recall | Holdout recall | Adversarial recall | Decision |
| --- | ---: | ---: | ---: | --- |
| Seed | 91.67% | 83.33% | 100% | Retain baseline |
| 5651963604019902872 | 91.67% | 83.33% | 100% | Tie; do not promote |
| 5651963604019901001 | 83.33% | 83.33% | 83.33% | Reject regression |
| 5651963604019904232 | 83.33% | 83.33% | 83.33% | Reject regression |

The [review artifact](results/2026-10-29-block-selection-review.json) retains all
four programs, per-case local results, and the remote configuration/status. The
[managed receipt](results/2026-10-29T035635Z-block-selection-managed.json) records
the original controller result; its best-source artifact is the seed. The
starting evaluator fingerprint did not change during this run.

No evolved policy was promoted. Actual turn savings and downstream task success
remain unmeasured. The runner now allows 300 seconds of local controller
inactivity, checks remote status after controller exit, persists that status,
and exits unsuccessfully unless the remote state is COMPLETED. An unavailable
status is UNKNOWN, never successful completion. This correction is covered by
local tests; it has not been verified in a second paid campaign.
