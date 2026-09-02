# The byte that ate the session

[Documentation](README.md) · [Getting started](GETTING-STARTED.md) · [Core concepts](CONCEPTS.md)

One command prints 98,000 tokens. The agent needs one traceback.

This sounds like a compression problem. It is not. Compression asks how to make the 98,000 tokens smaller. The agent problem is different:

> Which facts deserve to stay in the prompt, and how can everything else remain addressable while retained?

That distinction determines the architecture.

Handles address immutable stored bytes. Model-visible retrieval is still
bounded and subject to the current redaction policy.

<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-evidence-fates-mobile.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-evidence-fates-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-evidence-fates.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-evidence-fates-light.svg" width="100%" alt="Three treatments of the same 20,001-line fixture: keeping all 302,628 o200k_base tokens preserves the quiet target but floods context; head-and-tail truncation emits 1,219 and loses it; straitjacket emits 531, preserves the target at line 14,238, and emits a retrieval address.">
</picture>

## The ordinary path

Consider a failing test run near the start of a debugging session:

```text
turn 1  read tests
turn 2  run pytest ──→ 98,000 tokens enter the transcript
turn 3  inspect implementation ──→ the log is sent again
turn 4  inspect caller ──────────→ the log is sent again
turn 5  edit ────────────────────→ the log is sent again
turn 6  rerun ──────────────────→ the old log is still there
turn 7  debug ──────────────────→ both logs are now there
```

The test process produced the bytes once. The conversation rents them on every later turn.

Eventually the host compacts the transcript. Now the system has paid to carry the log repeatedly and may still lose the only line that matters. It combines the cost of preservation with the semantics of deletion.

<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-residency-mobile.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-residency-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-residency.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-residency-light.svg" width="100%" alt="Illustrative seven-turn residency trace using the measured 302,628-token field-needle payload. The native path keeps the raw output resident for six turns. The contained path keeps a 531-token digest and retrieves 21 lines only when needed.">
</picture>

## The contained path

```bash
ctx run -- pytest -q
```

The command still runs. stdout and stderr stream into a local content-addressed artifact. They do not first pass through the model.

A pytest profile extracts typed facts:

- command outcome;
- failed test identities;
- source locations;
- traceback spans;
- coverage of the failure census.

The delivery layer renders those facts within a fixed budget:

```text
[ctx run:8d8335db6848 profile=pytest/v2]
exit: 1
stdout: 4,102 lines · 402.1 KiB · est 98,000 tokens
failures:
  tests/test_auth.py::test_token_expiry  tests/test_auth.py:42
coverage:
  identities: 1/1
  omitted: 4,098 lines
next:
  ctx get run:8d8335db6848#stdout --lines 1280:1300
```

The model receives the failure, its location, an honest omission count, and the next useful read. The artifact store keeps the rest.

The fields vary by profile. A separate, receipt-derived log specimen makes the contract visible:

<picture>
  <source media="(max-width: 640px) and (prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-digest-anatomy-mobile.svg">
  <source media="(max-width: 640px)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-digest-anatomy-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-digest-anatomy.svg">
  <img src="https://raw.githubusercontent.com/vamsiramakrishnan/straitjacket/main/assets/readme/diagrams/ae-digest-anatomy-light.svg" width="100%" alt="Annotated anatomy of a receipt-derived log-template specimen: immutable run identity, successful outcome, template census, quiet needle, coverage receipt, and exact continuation command.">
</picture>

The digest is not a lossy replacement. It is the first page of a lossless protocol.

## Why not summarize it?

A language-model summary has three bad properties for this job:

1. it is probabilistic;
2. it cannot prove which identities it omitted;
3. it cannot recover exact omitted bytes by itself.

straitjacket uses profiles and evidence contracts instead.

A profile understands the output family. A contract defines three classes:

- **Required** facts must remain represented.
- **Elastic** detail may expand or contract with the budget.
- **Retrievable** evidence may stay outside context only when a valid address exists.

The renderer produces a coverage receipt before it declares success.

```text
identities: 37/37
inline detail: 5/37
retrievable detail: 32/37
unrepresented required facts: 0
```

A 200-token digest with 36 missing failures is not a win. It is a small bug.

## Retrieval is a page fault

If the model needs the traceback:

```bash
ctx get run:8d8335db6848#stdout --lines 1280:1300
```

If it needs a named error:

```bash
ctx search run:8d8335db6848 "MissingTenantError"
```

The prompt acts like a small working set. The artifact store acts like durable backing storage. A handle connects them.

The comparison is operational, not metaphorical: retrieval happens only on demand, returns a bounded page, and may return a narrower continuation rather than materialize an oversized region.

## Determinism is a billing feature

Two identical failures should render identically.

straitjacket normalizes volatile details such as terminal decoration, temporary paths, and irrelevant timing fields where the profile permits it. The same evidence, contract, and delivery plan produce the same digest bytes.

That buys:

- stable prompt prefixes;
- meaningful run-to-run diffs;
- reproducible evaluation;
- no model call on the digest hot path.

`ctx diff run:8d8335db6848 run:5a67c9de0123` can then report the behavioural delta instead of asking the model to compare two logs in attention.

## Capture has to happen before the flood

Once 98,000 tokens enter the transcript, any repair mechanism is late.

`ctx setup` installs a birth-time classifier. It separates operations into four practical classes:

```text
known bounded read ─────→ pass through
known noisy read ───────→ rewrite through ctx capture
known mutation ─────────→ preserve approval boundary
unknown operation ──────→ apply configured guard policy
```

Supported hosts also provide an entry-time safety net for oversized results that were not predictable from the call itself.

Host contracts are not equal:

| Host | Pre-execution | Post-execution |
|---|---|---|
| Claude Code | can rewrite arguments | can replace output |
| Codex | implemented and contract-tested | implemented and contract-tested; live CLI receipt pending |
| Antigravity | can allow or deny, not rewrite | cannot replace output |

On Antigravity, straitjacket denies a known flood and returns the bounded replacement command. That costs a turn, but prevents the bytes. A large connector result can only be observed after return because the host exposes no replacement field. See [Host capabilities](HOST-CAPABILITIES.md).

## There are four places to act

Every byte has a short career:

1. **Birth** — an operation is about to produce it.
2. **Entry** — it crosses a tool or host boundary.
3. **Residence** — it occupies active context over later turns.
4. **Emission** — the model may paste it into a deliverable.

One store serves all four gates, but the policy differs:

- prevent predictable floods at birth;
- catch unpredicted floods at entry;
- preserve only decision-relevant views in residence;
- cite evidence instead of reciting it at emission.

The earlier the gate, the cheaper the intervention.

## Live files need stronger addresses

An immutable run handle always means the same bytes. A repository line address does not.

Suppose the model records `src/auth.py:40:52`. Another edit inserts six lines above it. The same coordinates now return different code with exit status zero.

That is worse than a missing address. It is plausible false evidence.

straitjacket adds a short content anchor:

```bash
ctx get repo:src/auth.py --lines 40:52@07407f1c
```

Resolution follows three steps:

```text
content still at 40:52? ── yes ─→ return it
           │ no
           ▼
same content moved? ───── yes ─→ return new location and report the move
           │ no
           ▼
refuse
```

The address names content first and position second.

## Bounded investigation, not bounded intelligence

Containment does not mean starving the model. It means moving deterministic work out of the conversation.

If five searches are already known, `ctx seq` can run them beside the repository. If the question has a typed shape, `ctx ask` can compile it. If the investigation is a bounded DAG, `ctx plan run` can execute it locally and return one causally organized digest.

This removes scheduling, parsing, and joining from the expensive conversational loop. It does not precompute through uncertainty.

> Batch within one hypothesis. Return when evidence can change the hypothesis.

That boundary matters. A perfectly optimized investigation of the wrong theory is still wrong.

## The honest boundary

straitjacket contains evidence. It does not sandbox execution.

Commands run with the invoking user's authority. A captured deletion is still a deletion. A deterministic digest is not a security boundary. The guard preserves mutation approvals; it does not launder mutations into reads.

It also does not promise that every task improves. Small outputs should pass through. Cheap, already-bounded operations may be faster natively. The evaluation suite includes these losing regimes because the correct policy is conditional.

## The whole design in one sentence

Keep complete evidence outside the prompt, keep decision-relevant facts inside it, and keep exact addresses between them.

Next: [install it and run the first capture](GETTING-STARTED.md).
