# tokenomics — the triage-channel referee

Rebuild of the [tokenomics-benchmark-multi-llms][src] suite as a referee that can
actually be reported. Same dataset, same arm shapes, same `$/solved` framing —
run against the real `ctx` CLI, with live model calls only.

[src]: https://github.com/lexha-redstone/tokenomics-benchmark-multi-llms

## The question

When a candidate solution fails its tests, the failure has to reach the next
model somehow. There are three channels:

| Channel | Mechanism | Triage API cost |
|---|---|---|
| `raw` | forward the unittest stderr verbatim | $0 |
| `llm` | pay a cheap model to compress it | per repair loop |
| `sj` | forward the digest from `ctx run --` | $0 |
| `sj_hop` | that digest, plus the spans it cites, resolved with `ctx get` | $0 |

Three of the four are free; the interesting question is whether they hold
accuracy against `llm` while costing nothing, and how much smaller the repair
prompt gets. That is the only variable this eval manipulates.

`sj_hop` exists because `sj` alone tests the digest in the mode straitjacket is
least suited to. The digest is a *census plus addresses*: it names every failing
test and tells you where the bytes are, on the assumption that whoever reads it
can follow an address. A single stateless repair call cannot — it sees
`stderr:L2` and has to guess what was there. `sj_hop` resolves those citations
locally before handing the channel on, which is what an agent does with the same
digest. The `sj` vs `sj_hop` delta therefore separates two very different
claims: *the digest dropped decisive evidence* from *the consumer had no way to
go get it*. Only the first would be a defect in the digest.

The hop is bounded the way the skill prescribes — a window either side of each
cited line, merged, capped at 100 lines per retrieval — and it is free: `ctx
get` reads the local store and makes no API call.

## Arms

Two families. Inside a family the ladder and prompts are byte-identical and the
task list is the same, so a pass-rate delta is attributable to the channel.
Across families nothing is attributable — the ladder changes too.

| Family | Ladder | Arms |
|---|---|---|
| `cascade` | `3.5-flash-lite` → `3.6-flash(low)` | `cascade_raw`, `cascade_llm`, `cascade_sj`, `cascade_sjhop` |
| `smart_repair` | `3.6-flash(low)` → `3.5-flash-lite` → `3.6-flash(medium)` | `smart_raw`, `smart_llm`, `smart_sj`, `smart_sjhop` |

The Claude tiers from the original suite (`Escalation Shield`, `Ultra-Sweet
Hybrid`) are **not** run here — no Anthropic credential in this environment.
They are absent from the results rather than estimated.

## Running it

The dataset is not redistributed here. `BigCodeBench-Hard-v0.1.4.jsonl` is the
148-task Hard split of [BigCodeBench][bcb] (Apache-2.0); drop it in this
directory before running. Fields used: `task_id`, `complete_prompt`, `test`.

[bcb]: https://huggingface.co/datasets/bigcode/bigcodebench-hard

```bash
pip install -e .                       # ctx must import or the runner exits
export GEMINI_API_KEY=...

# the sandbox interpreter needs the BigCodeBench library set, or tasks die of
# ModuleNotFoundError and you measure your venv instead of the models
python evals/tokenomics/runner.py --n 30 --arms all \
    --sandbox-python /path/to/bench-env/bin/python

python evals/tokenomics/report.py --results evals/tokenomics/results
```

## What this fixes relative to the suite it mirrors

Four defects there made the published tables unreportable. Each has a
structural counter here, not a convention:

1. **straitjacket never ran.** `ctx` was absent from `requirements.txt` and the
   import sat inside a bare `except`, so the "SJ" arm silently measured a
   12-line regex. Here `import ctx` is asserted at startup and the `sj` arm
   shells out to the real `ctx run --` CLI, using its digest verbatim.

2. **A simulator produced the numbers.** `_fallback_dispatch` was entered
   silently on any API failure and decided pass/fail from
   `"straitjacket" in prompt.lower()`, +10 points. Here there is no fallback:
   `call_model` retries, then raises, and the task is recorded `errored`.
   `report.py` refuses to render any arm not tagged `provenance: live`.

3. **Every arm changed models *and* triage *and* topology at once**, so nothing
   was attributable. Here the families hold everything else fixed and the report
   runs a paired McNemar test on the same tasks.

4. **The report was prose over data it never read** — its tables were 2× away
   from the committed JSON. Here `report.py` recomputes every cell from the
   per-task records; there is no path for a number to be typed in.

Two further bugs found while porting, both live in the original too, and both
inflate the apparent failure rate rather than any arm's advantage:

- **Truncated code fences.** `extract_code` only matched a *balanced* ```` ``` ````
  pair, so a response cut off at `max_output_tokens` fell through to raw text
  and put the literal ```` ```python ```` line into `prog.py`. The task then died
  of `SyntaxError` and was scored as a model failure. Fixed, and
  `finish_reason: MAX_TOKENS` is now counted in the report.
- **Thinking tokens ate the answer budget.** A flat `max_tokens=2560` with
  reasoning enabled left too little for the visible answer. Each thinking level
  now gets its own headroom.

A third, environmental: the original's sandbox lacked the libraries its own
`requirements.txt` declared, so tasks failed on `ModuleNotFoundError` and were
then excluded post-hoc via an "effective pass rate" denominator. Here the
sandbox is provisioned up front and any surviving import failure is reported as
`infra_error` — counted in the table, never quietly removed from the
denominator.

## Reading the output

- **Pass rate carries a Wilson 95% interval.** At N=30 that interval is roughly
  ±17pp. Differences smaller than that are not resolved by this run, and the
  report says so rather than ranking them.
- **Triage cost is exact, not estimated.** `raw` and `sj` make no triage call;
  their $0.0000 is by construction, not measurement.
- **Tokens are the primary unit.** USD is derived at render time from a price
  table stored in each results file. The table carried over from the original is
  *unverified*; re-price with `report.py --prices` before quoting dollars.
