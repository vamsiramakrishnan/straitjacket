# straitjacket on headroom's own proof table (2026-09-15)

headroom's README publishes one table as its "Proof": four scenarios, tokens
before and after `compress()`, 21–57% saved. It comes from one seeded, offline
script in their repo, `benchmarks/index_proof_table.py`. This receipt runs the
same corpus through straitjacket and asks the question that table does not:
what does the model still have?

```bash
git clone https://github.com/headroomlabs-ai/headroom /path/to/headroom
pip install -e '.[dev]' headroom-ai tiktoken
python evals/headroom_proof_table.py --headroom-repo /path/to/headroom
```

## What their number measures

`index_proof_table.py` seeds a random generator, builds four scenarios from
their `real_world_agent_benchmark.py` generators (synthetic GitHub code search,
log search, database rows, file search, a filesystem tree, an issue list),
JSON-dumps each tool result into a `tool` message, and counts tokens with their
`OpenAICompatibleTokenCounter` (tiktoken) before and after `compress()`. No
model is called. No task is scored. Nothing checks what the compressed text
still contains. The published row is a compression ratio on synthetic JSON.

## Setup

- **headroom** at commit `9f32800b` (2026-09-14, reports itself as
  `0.37.1-dev`), library API `headroom.compress(messages, model="gpt-5.6")`,
  in both configurations their script prints: the default config and
  `protect_recent=0`. Both produced identical output on this corpus.
- **straitjacket**: `ctx.digest.digest_output`, the function the PostToolUse
  emission gate calls. A tool result at or under 16 KB passes through
  byte-identical. One over it is stored losslessly and replaced by the bounded
  digest with a `run:` handle. Every payload in this corpus is over 16 KB, so
  every one was gated.
- **Same corpus**: their generators, their seed (`20260902`), their tokenizer,
  their before-count. The before column below matches their README to the
  token.
- **Needles**: what each generator plants for an agent to find. ERROR entries'
  trace ids in a log search, anomalous rows (revenue ≥ 50k or suspended) in a
  database query, open issues labelled bug, the real (non-template) file paths
  in a file search, the distinct repositories in a code search, the top-level
  directories in a tree. A needle is "visible" when its string occurs in the
  text the model receives. That is a lenient check: a short issue number can
  match by accident, and visible is not the same as noticed.

## Result

| scenario | before | headroom: after (saved) · needles visible | sj emission gate: after (saved) · needles visible |
|---|---:|---:|---:|
| Code search (100 results) | 17,199 | 13,597 (21%) · 12/12 | 496 (97%) · 4/12 |
| SRE incident debugging | 55,957 | 24,340 (56%) · 97/108 | 1,018 (98%) · 6/108 |
| Codebase exploration | 58,801 | 33,895 (42%) · 19/31 | 1,154 (98%) · 15/31 |
| GitHub issue triage | 46,067 | 32,429 (30%) · 66/66 | 1,391 (97%) · 6/66 |
| **total** | 178,024 | 104,261 (41%) · 194/217 | 4,059 (98%) · 31/217 |

headroom's after column reproduces their README exactly. It drops 23 of the
217 needles on the way: eleven in the SRE scenario and twelve in codebase
exploration, the two scenarios where its `smart_crusher` transform fired.

straitjacket's digest shows 31 of the 217. The other 186 are not in the text
the model sees. They are behind the handle, and the handle is the point of the
design, so the receipt prices following it. For three hidden needles per gated
payload (27 retrievals), the script ran the search a session would run:

```
ctx search run:<id>#stdout <needle>
```

| | |
|---|---|
| retrievals attempted | 27 |
| found verbatim | 27 |
| tokens per retrieval, median | 139 |
| tokens per retrieval, range | 126–158 |

So the two systems are not on the same axis:

- **headroom** keeps 41% of the tokens and 89% of the needles in the visible
  text, with the missing 11% gone and no address for them.
- **straitjacket** keeps 2% of the tokens and 14% of the needles in the
  visible text, with every one of the rest retrievable for about 140 tokens
  when the session asks for it by name.

The right reading is a budget, not a ratio. A session that needs one specific
needle per payload pays 4,059 + 10 × ~140 ≈ 5,500 tokens under straitjacket
against 104,261 under headroom. A session that needs to *scan* everything (all
47 ERROR trace ids at once, say) pays a `ctx get` of the whole range, which
approaches the original size; the digest tells it how big that is before it
pays. headroom's 41% is what you get when you do not want to choose.

## What this does not show

- No model was run, on either side. Whether an agent *uses* a handle well is
  the question the [DeepSWE receipt](agentbench/deepswe-2026-09-13.md) asks,
  and its answer is "cheaper per turn, not per task, at n=1".
- The corpus is synthetic JSON with regular structure, which is where both
  systems do best. headroom's README says as much about prose.
- The needle check is string presence. It over-counts headroom's 89% (short
  strings can match elsewhere) and under-counts nothing for straitjacket,
  since the digest's own row samples are what produced its 14%.
- headroom's own earlier needle-drop receipts against 0.32.1 are in
  [`headroom-needle-2026-07-19.md`](headroom-needle-2026-07-19.md). This run
  is against a current checkout and does not revise those.

Machine record: [`agentbench/results/headroom_proof_table.json`](agentbench/results/headroom_proof_table.json).
