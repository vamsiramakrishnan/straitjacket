# Query-aware text selection

The generic text digest automatically uses task context in orchestrator workers
and sessions launched with `ctx wrap claude`. Workers receive their own node goal;
the Claude prompt hook replaces a private session file on each user prompt.
Sessions do not share a workspace-wide last-prompt file. Existing standalone
sessions are not retroactively configured.

An explicit focus overrides that task context:

```sh
ctx run --focus "cache latency" -- your-command
```

The same focus option is available on `ctx py` and `ctx seq`. Other structured
profiles keep their own selection rules. Without explicit or automatic focus,
the text profile keeps its existing behavior. An explicit empty focus disables
automatic selection. Small results still take the complete-output path.

## Selection contract

`text/focus-v2` selects at most four lines. It reserves slots for the strongest
signal and the terminal signal across stderr followed by stdout. Remaining
slots rank lines by distinct normalized query-term matches, then signal rank,
then stable stream/line order. Repeating a keyword does not increase its score.
Query terms are literal relevance hints, never executable instructions.

Each excerpt has its source stream and line number. Suggested retrieval commands
include the preceding line for context. The full capture remains available;
selection changes only the digest. Long excerpts can be clipped by the existing
line-width bound. Existing parse limits and final output budgets still apply.

The selection receipt reports the policy, a query fingerprint, parsed lines
scanned, matching lines, and selected lines. A fingerprint is not anonymization
of a predictable query. Timing is deliberately excluded from deterministic
digest bytes. No-match queries fall back to signal selection or head/tail.

## Evaluation and limits

`tests/test_digest_focus_policy.py` covers repetition, query coverage, a quiet
contradiction explicitly matched by the query, unrelated failure preservation,
cross-stream signals, deterministic replay, bounded output, and retrieval from
the original capture. Run it alongside text-signal and capture tests:

```sh
python -m pytest tests/test_digest_focus_policy.py tests/test_digest_text_signals.py tests/test_capture_and_determinism.py -q
```

This local lexical policy does not guarantee semantic relevance or retention of
every contradiction. Four selected lines are not a complete failure census.
The approach is inspired by query-aware selection in
[Supercompress](https://github.com/Supercompress/Supercompress); it uses no
external service or model weights.

To validate the automatic policy's effect on real work, replay task-separated DeepSWE traces and
measure evidence retention, follow-up retrievals, end-to-end latency, and verified
task success against the unfocused baseline. Those downstream measurements have
not yet been run. Compression percentage alone is not an admission criterion.

## Block selection and retrieval

`src/ctx/digest/block_selection.py` prepares exact source blocks, keeps fenced
code together, and closes heading dependencies before enforcing a byte budget.
Required evidence that cannot fit produces `needs_retrieval`; it is not silently
cut. No phrase, DEBUG level, null value, identifier, or license text is deleted
during preprocessing. Focused generic text digests use `text/focus-v3` to expand
selected evidence into complete blocks when the protected blocks fit; otherwise
they retain the bounded `text/focus-v2` line view. This uses lexical features,
not trained neural weights.

`ctx compress FILE --query "question" --budget-bytes 2048` also exposes the local
selector. It stores the original bytes and returns their retrieval address.
`--require-line N` protects explicit evidence. An unsatisfied budget returns
`needs_retrieval` (exit 3), rather than silently dropping required evidence.

`--cache-prefix FILE` emits a deterministic reference prefix and a separate
evidence/query suffix. Applications can send the same prefix before changing
content to make provider prompt caching possible. This does not manipulate a
model's KV cache, call a provider, or guarantee a cache hit. Keep instructions
outside this reference-data wrapper.

The experimental `block-selection` AlphaEvolve search can change only `rank_blocks(items)`.
Candidates receive numeric features, not source text, answer labels, or test
code. The fixed evaluator rejects missing or duplicated IDs and noninteger IDs.
Search, holdout, and adversarial splits contain six synthetic cases each.
Holdout scores are not returned to managed search. Synthetic results cannot
unlock promotion; real task replay remains required.

```sh
python -m evals.alphaevolve.block_selection.benchmark --output evals/alphaevolve/results/block-selection-local.json
python -m pytest tests/test_block_selection.py tests/test_task_context.py -q
```

The benchmark reports answer-block recall and emitted bytes. Neither is a
measurement of actual follow-up turns, model tokens, dollars, or task completion.
