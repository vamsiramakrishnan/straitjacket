<sub><a href="README.md">« straitjacket / docs</a></sub>

# Prewalk — hand off to a cheap model after the first validated edit

Most of a frontier model's turn on a mutation node is spent reading, not
writing: exploring the codebase, forming a plan, checking assumptions. Once
that plan exists and one edit proves it, the rest of the work — applying the
plan across the remaining files — rarely needs the same model. The classic
handoff (a plan document, handed to a cheaper model) still throws that
exploration away: the cheap model reads the plan but not the codebase, and
routinely re-explores to trust it.

Prewalk moves the handoff point later: after the first edit lands, not after
the plan is written. The cheaper model inherits the plan **and** a validated
edit already on disk — a free, concrete example that the plan works, in place
of a document it has to take on faith.

```
node assigned a frontier model, min_tier="frontier", role="implement"

  ┌────────────────────────┐
  │   attempt 1: frontier   │  plan deeply → write the plan → make ONE edit
  │  "plan, then one edit,  │  → print CTX_PREWALK_HANDOFF → end turn
  │   then hand off"        │
  └───────────┬─────────────┘
              │ handback: prewalk_handoff (not a failure — see the vocab
              │ in docs/TASK-LEDGER.md)
              ▼
  ┌─────────────────────────┐
  │  steward: handoff_cheap  │  cheapest installed model BELOW frontier
  │  (the mirror of escalate)│  — de_escalation_target, not choose_recovery
  └───────────┬──────────────┘
              │ same node, same worktree, edit KEPT (not reset)
              │ next prompt: the plan + the first edit, verbatim
              ▼
  ┌────────────────────────┐
  │  attempt 2: cheap model │  continues the plan to completion
  └────────────────────────┘
```

## How it is detected

The frontier model is asked, in its prompt, to state its plan, make one
edit, then print a single literal line — `CTX_PREWALK_HANDOFF` — and end its
turn. That line is the only signal checked: not whether the model "seems
done", not a keyword in its prose. A model explaining *why* it is stopping
("the task is not complete yet, handing off to continue it") would otherwise
read as a real failure to the ordinary classifier — the sentinel is checked
first, against the raw exit code, and wins over that classification whenever
present. If the model never prints it, either because it ignored the
instruction or because it just finished the whole task itself, the node is
scored exactly as it always was: no sentinel, no handoff, no regression.

## Why the edit is kept, not discarded

Every other steward action but `handoff_cheap` — `retry_same`, `escalate` —
resets an isolated worktree before the next attempt, because a *failed*
attempt's changes are not evidence the next one should inherit. Prewalk is
the one case where the previous attempt did not fail: the edit is real
progress, so the worktree carries it forward untouched into the cheap
model's attempt.

## Why this is not a steward failure-recovery decision

`ctx.steward.decide` and the evolved `choose_recovery` policy answer "how do
I recover from a failure" — auth, safety, rate limits, an incomplete
contract. A prewalk handoff is not a failure: the attempt succeeded at its
narrower goal by design. Forcing it through the failure classifier and
recovery policy would mean explaining a success as a kind of failure, so it
is a separate, deterministic path: `ctx.steward.de_escalation_target` picks
the cheapest installed model below the current tier — the literal mirror of
`escalation_target`, which picks the cheapest model *above* it. The decision
is still written to the ledger as a `ctx.steward/v1` row (`action:
"handoff_cheap"`) before it is acted on, the same invariant every other
steward decision keeps.

## Turning it on

Opt-in, off by default, one line in `ctx.toml`:

```toml
[orchestrate]
prewalk = true
```

It only ever arms for a node the router already assigned a **frontier**
model with a **mutation** role (`role = "implement"` or `"edit"` in
`need_tags`) — a plan/review/verify node is never asked to hand off, since
there is no cheaper continuation for work that has to stay on a strong
model. A node already routed to a cheaper tier is untouched: there is
nothing to save by handing off from economy to economy.

## Limits

- The mechanism relies on prompt compliance, not a hard interrupt. The
  source article's version can stop the frontier model programmatically the
  instant *any* edit lands, mid-turn; this implementation instead asks the
  model to plan, make one edit, then stop on its own. A model that ignores
  the instruction and keeps going degrades safely — it just finishes the
  whole task itself, exactly as it would have without prewalk. There is no
  cost regression from non-compliance, only a missed saving on that run.
- No live-model receipt exists yet for the claimed cost/quality trade —
  unlike `docs/TASK-LEDGER.md`'s resume and recovery numbers, this has not
  been measured against a real frontier-then-cheap run. The mechanism and
  its tests are model-free: they pin what the orchestrator does with a given
  transcript, not what a real model actually writes when asked to hand off.
- One handoff per node. A cheap-model continuation that itself needs to
  hand off further is not offered another `handoff_cheap` — the ordinary
  steward and `max_attempts` govern everything past the first attempt.

---

For the row schema (`ctx.handback/v1` reason `prewalk_handoff`, `ctx.steward/v1`
action `handoff_cheap`), see [Task ledger](TASK-LEDGER.md).
