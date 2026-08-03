# Scoring the ladders from the bug-bash corpus

**Method:** no new agent runs. The 29 bug-bash arm workspaces already on disk
each left their own `.ctx-session-reads/` ledgers, so they are a corpus of
real sessions at zero additional cost — the measurement equivalent of mining
the arms for defects rather than spawning fresh ones.

    ctx ladders --corpus <dir-of-recorded-sessions>

**Result: 6 measured · 2 instrumented but silent · 1 not scored**, from a
starting point where the registry could score none of the five in question.

## What changed, and what it cost

Four of the five had signals already being written and nobody had connected
them:

| Ladder | Signal found | Honest caveat |
|---|---|---|
| **Emission budgets** | `plan-emissions.jsonl` `visible_tokens` | **Derived.** Buckets emitted size against the configured budgets — the tier a size *falls under*, not a record of which check bound it. The gate still does not log the tier it applied. |
| **Guard modes** | `guard-policy-cache.json` `policy.mode` | One value per workspace; only a corpus shows a distribution. |
| **Policy epochs** | `guard-policy-cache.json` promoted/demoted lists | One policy holds many commands, so a record yields a *list* of rungs. |
| **Deployment tiers** | filesystem probe of what `ctx wrap` installed | Probed rather than recorded — harder to falsify than a ledger entry claiming a tier. |

The fifth, **the solution ladder, stays unscored and should.** The rung is
chosen inside the model's reasoning and never crosses a tool boundary. The
A/B measured its *outcome* (−28% turns); nothing observes its traversal, and
inventing a proxy would be the confident-histogram failure this registry
already made once.

## Two ladders are instrumented and silent

`window pressure` (needs the observer proxy running) and `model tiers` (needs
`ctx orchestrate`) declare working signals that saw nothing in this corpus,
because these arms exercised neither. That is reported as *silent*, not as
unmeasured — "the instrument exists and saw nothing" and "there is no
instrument" are different facts, and collapsing them is how a dry ladder
starts looking like a working one.

## What the corpus actually says

The distributions are as interesting for their monoculture as their shape:
every workspace ran `guarded` at the `plugin` tier, so those two rows measure
the harness's defaults rather than any real spread. **75% of plan emissions
landed under the digest budget** — the budgets are rarely the binding
constraint — and the capture ladder never left its first two rungs: no `seq`,
no `py`, no `job` in 200 substitutions.

## Full output

```
[ctx ladders · corpus of 29 workspace(s) under /tmp/claude-0/-home-user-straitjacket/54e49b43-784c-56ca-be16-bd5831e06a33/scratchpad]

Solution — what code to write · climbed by model
  not scored: the rung is chosen inside the model's reasoning and never crosses a tool boundary, so nothing observes it; the A/B measured the ladder's OUTCOME (-28% turns) and not its traversal

Capture — how work executes · climbed by model
  200 record(s) across 20/29 workspaces
  native read         125   62.5%  ###################
  run                  75   37.5%  ###########
  --shell               0    0.0%  
  seq                   0    0.0%  
  py                    0    0.0%  
  job                   0    0.0%  
  note: substitution rungs, mapped onto the capture rung they land on

Emission budgets — how many bytes may be emitted · climbed by hook
  232 record(s) across 15/29 workspaces
  under digest        174   75.0%  ######################
  digest..result       31   13.4%  ####
  result..turn         16    6.9%  ##
  over turn            11    4.7%  #
  note: BUCKETED by emitted size against the configured budgets (480/1200/2800). This is the tier an emission's size falls under, not a record of which check bound it -- the gate still does not log the tier it applied. A derived rung, labelled as one, the same way window pressure buckets a percentage

Graduated engagement — how hard to steer · climbed by hook
  25 record(s) across 25/29 workspaces
  passive               4   16.0%  #####
  active               21   84.0%  #########################
  note: current level; a point sample, not a history

Window pressure — how tight the budgets are · climbed by hook
  instrumented, silent: proxy/window.json carried no usable records in any workspace

Guard modes — what the guard may refuse · climbed by static
  18 record(s) across 18/29 workspaces
  advisory              0    0.0%  
  guarded              18  100.0%  ##############################
  strict                0    0.0%  
  note: the resolved mode for this workspace. A point sample, not a traversal -- the useful question is the distribution ACROSS workspaces, which is what `ctx ladders --corpus` answers

Policy epochs — how a bloated window is reclaimed · climbed by hook
  18 record(s) across 18/29 workspaces
  unknown              18  100.0%  ##############################
  promoted              0    0.0%  
  demoted               0    0.0%  
  note: counts of promoted/demoted commands in the committed policy. `planMode` (normal|dense|bypass) rides on every intervention and is tempting to read as this ladder; it is a different axis (plan density) and mapping it here would report the wrong thing under this name

Deployment tiers — how strongly containment is enforced · climbed by static
  18 record(s) across 18/29 workspaces
  skill                 0    0.0%  
  plugin               18  100.0%  ##############################
  native                0    0.0%  
  hardened              0    0.0%  
  note: probed from what `ctx wrap` actually installed in the workspace, not from a ledger. Static per workspace, so the informative form is the corpus distribution

Model tiers — which model does the work · climbed by static
  instrumented, silent: route.jsonl carried no usable records in any workspace

6 measured · 2 instrumented but silent · 1 not scored
```

## Limits

Same as `evals/command-corpus-2026-08-03.md`, and they bind harder here: one
repository, one agent family, one guard mode, one deployment tier. These
numbers describe **this corpus**, not agents in general. The instrument is
the deliverable; the distribution is a sample.
