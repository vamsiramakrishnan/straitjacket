# Changelog

All notable changes to ctx-harness are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is 0.x
with a minor bump per mechanism wave (see CONTRIBUTING.md).

## [Unreleased]

## [0.38.0] - 2026-09-05

Three mechanisms taken from two outside designs — the Recursive Language
Model write-up (Zhang et al., 2025) and headlong, the Laude Institute's
bash agent microharness — after mapping both against what the harness
already had. Their core moves (context as a variable the model reads
through code, return by reference, an append-only trajectory that is an
index over retrievable raw entries) are the handle model, the checkpoint
address and lossless rescue respectively, so nothing was taken there. What
was missing was narrower, and each piece is small.

`ctx orchestrate` gains an inactivity bound beside its wall clock,
headlong's `SHELLM_INACTIVITY_TIMEOUT` shape: `[orchestrate] idle_timeout`
(seconds, 0 = off, the default) kills a node that emits nothing on either
stream for that long and raises `ctx.orchestrator.NodeStalled`, while a
node that keeps emitting is still bounded by `node_timeout`. Every byte a
host writes is the beacon, read raw rather than by line so a progress
character counts; Claude nodes switch to `--output-format stream-json
--verbose` only while the bound is on (the default launch argv is
unchanged), and `ctx.usage.parse_claude_json` reads that transcript's last
`type: result` event as the same document `json` mode returned. The
steward's vocabulary grows two failure kinds the improve route's first live
run showed were missing: `stalled` (silent for `idle_timeout`) and
`wall_timeout` (still active when `node_timeout` ran out). Both used to be
`transient_transport` — the harvest node's hour of work was filed as a
transport blip and offered a blind same-model retry. The recovery policy
(production seam and evolution seed, kept identical, with two new evaluator
cases) now escalates a stalled node and never re-runs it blind, and re-plans
a wall-timeout node so the coordinator can split it, retrying the same model
in the same worktree only when nobody can re-plan.

The PreToolUse guard learns headlong's docker broker denylist as a
safety-class `force_ask`: a privileged container, a host namespace
(`--pid/--network/--ipc/--uts/--userns/--cgroupns=host`), a host device,
`--cap-add` of `SYS_ADMIN`/`SYS_PTRACE`/`ALL`, the container-engine socket,
or a bind mount whose source is outside the workspace (`-v`, `--mount
type=bind`; `$PWD` resolves, any other shell variable asks). A named volume
or a mount from inside the workspace keeps the volume-class answer
`docker` always had. One predicate, every door: the plain command, the
`> file 2>&1` shortcut, each chain segment, and `ctx run -- <argv>` — which
was allowed outright because the program was `ctx`, and which the deny
remediation itself points at. Closing that door also gave `ctx run -- cat
secrets.json` the secret-path force_ask the plain command had.

Rescue stubs carry the first line of the block they replaced (bounded to
120 characters, control characters and runs of whitespace collapsed),
headlong's one-line tldr on every summarized entry — except deterministic:
a harnessed tool_result opens with its digest header, and that line is a
pure function of the elided bytes, so a rescued transcript reads as an
index instead of a column of identical placeholders, with no model and no
summary involved. Hash, byte count and retrieval path are unchanged.

## [0.37.0] - 2026-09-03

`ctx.taskledger.append()` now holds one OS-level lock (`fcntl.flock`, the
same idiom already used by `ctx.engagement`'s state file and `ctx.hook`'s
read ledger) across its tail-check and write, closing a gap where two
separate OS processes — not just threads inside one orchestrator — could
race on the same task's ledger file. `ctx.orchestrator`'s own
`threading.Lock` only ever protected concurrent launches within one
`ctx orchestrate` run; a second orchestrator process, or a direct
`ctx task send`, held no lock at all.

Opt-in (`[orchestrate] prewalk = true`, docs/PREWALK.md): a node the router
assigned a frontier model with a mutation role is asked to plan, make one
edit, then hand off — printing a literal sentinel line and ending its turn
rather than continuing the whole task. On that signal the SAME node's next
attempt runs on the cheapest installed model below frontier, in the SAME
worktree with the edit kept (never reset, unlike a failed attempt's retry),
and its prompt carries the frontier attempt's plan and validated edit
forward verbatim — the "free in-context example" a plan document alone
cannot give a cheaper model, without which it routinely re-explores to
trust it. The handback for this is a new, seventh reason,
`prewalk_handoff` — a deliberate success, not a failure, so it bypasses
`choose_recovery` entirely for its own deterministic decision,
`ctx.steward.de_escalation_target` (the literal mirror of
`escalation_target`: cheapest installed model *below* the current tier
rather than above it), recorded as a `ctx.steward/v1` row
(`action: "handoff_cheap"`) before it is acted on like every other steward
decision. Detection is a single ctx-defined literal the model is asked to
print, checked against the raw exit code before any of the existing
failure-classification logic runs — deliberately, since a model narrating
why it is stopping ("the task is not complete yet, handing off") would
otherwise read as a real failure to that classifier. A model that ignores
the instruction degrades safely: it just finishes the task itself, exactly
as it would without prewalk, at no cost regression. No live-model receipt
exists yet for the mechanism's actual cost/quality trade; the mechanism and
its tests are model-free, pinning what the orchestrator does with a given
transcript rather than what a real model writes.

`ctx get --snapcompact` (`src/ctx/snapcompact.py`, Delivery plane): an opt-in
transport swap for a bounded `ctx get` slice — render the already-selected
text as a deterministic monospace bitmap PNG (crisp, no anti-aliasing; same
input always renders the same pixels) instead of emitting it as raw text,
store it via the existing content-addressed blob store, and return its
`blob:` ref in the header instead of the text body. Follows a 2026 blog post
(stencil.so/blog/snapcompact) describing this as a cost-reduction technique
for vision-capable models — dense text rendered as an image and read back by
the model instead of tokenized as text. This ships the deterministic
encoding half only: a real monospace TTF (DejaVu Sans Mono, or the first of
Liberation Mono / FreeMono found on the host; Pillow's own bundled font as a
last-resort fallback), a measured (not assumed) character-cell density, and
a cost estimate that combines this repo's own `ctx.textutil.estimate_tokens`
for the raw side with Anthropic's own documented 28x28px vision-tiling
formula for the image side. It does **not** verify — and cannot verify from
this sandbox — that a live vision model actually transcribes the rendered
image back correctly at the blog's claimed ~2-3x cost reduction; that
requires a real model call. Fully opt-in (`--snapcompact` on `ctx get`, or
`{snapcompact: true}` in the MCP `get` selector); default behavior is
unchanged. Requires the `image` extra (`pip install 'ctx-harness[image]'`);
without it, a clear error names the missing extra rather than a bare
`ImportError`.

The edit-outcome ledger (`ctx.edit_outcomes`, `.ctx-session-reads/
edit-outcomes.jsonl`) now records two more fields on every row: the edit's
**format** — a closed vocabulary derived from the tool name
(`search_replace` for Edit/MultiEdit/str_replace_editor, `whole_file` for
Write/create_file, `patch` for apply_patch, `anchored` for `ctx edit apply`,
`other`) — and the **model** that made it. Published edit benchmarks
(Aider's format ladder, hashline's 16-model comparison, EDIT-Bench) all
report that a model's edit success moves by tens of points on the shape of
the edit alone and that the ranking is per model; a ledger without those
two axes could not say whether the anchored format straitjacket already
ships beats a host's native `Edit` for the model actually in use. The model
comes from `CTX_MODEL`, which `ctx orchestrate` now exports (with
`CTX_HOST`) into every host process it launches; outside an orchestrated
run the hook reads the last named model from a bounded tail of the
transcript the PostToolUse payload points at, and records `unknown`
otherwise — never a guess from an unrelated variable. `ctx edit apply` now
records its own outcome into the same ledger, one row per planned file
(`flavor: "ctx"`, `format: "anchored"`), with its two addressable refusals
mapped onto the needle's two failure kinds (target moved or vanished →
`not_found`; target now has more than one copy → `not_unique`). `edit_summary`
gains `by_model` (per (model, format): counts, classified rows, success and
failure rates, with `unknown` outside the success denominator) and
`models_reporting` / `unlabelled_model_rows`; `summarize_rows` and
`load_rows` expose the same over any row sequence. Old rows without the
fields fold into `unknown` and the tool-implied format. New
`evals/edit_format_by_model.py` replays a ledger into the one table the
question needs — success(anchored) − success(search_replace) per model, in
points, refusing to print a number for a cell under 30 classified rows and
labelling hashline's published +15 average as an external bar it does not
reproduce. No live-model receipt is included: the eval's `--fixture` mode
prints invented numbers and says so, and the real table only exists once
sessions with the hook installed have written rows.

Bug-bash round 17 (evals/bugbash-round17-2026-09-04.md): the S6 cell run
live as a naive-vs-harnessed Claude Code pair. Ten defects, all reproduced
by hand before their fix, regression-tested in
`tests/test_round17_mechanisms.py`:

- `hook.py`: the `cmd > file 2>&1` shortcut returned `allow` ahead of the
  secret-path guard, so `cat .env > out.log 2>&1` was allowed while
  `cat .env` force-asks. Both doors now consult one predicate
  (`_names_secret_path`).
- `config.py`: `[engagement] lean_models = 42` raised `TypeError` out of
  `load_config` on every command's path, and `"sonnet"` became six
  one-letter models. Coerced like every other list field; a non-list keeps
  the shipped default.
- `digest/__init__.py`: `digest_output` hard-coded `exitCode: 0`, so an
  errored over-budget tool result digested as `exit 0` and its stored
  manifest recorded a success. The host's error flag now sets exit 1;
  run identity is (bytes, tool, is_error).
- `ladders.py`: `_epoch_rung` indexed past a ladder narrowed to two rungs
  in ctx.toml (`[x] * 0` still evaluates `x`); guarded like its siblings.
- `repomap.py`: the builtin ranker raised `KeyError` on an import edge to
  a listed-but-unreadable file; it now skips edges to files it never read,
  as the networkx ranker already did.
- `surface_gateway.py`: `_rpc`'s deadline bounded only the first byte —
  `select` then `readline()` blocked without a timeout on a backend that
  wrote half a line and hung. Reads are now raw chunks under the deadline
  with line splitting in the client.
- `evidence_outcomes.py`: the `failing_ids` filter tested the whole result
  and never the match, so one FAILED in a verbose run tagged every passing
  id as failing and blocked `followup_join` from associating the fix.
  Decided per line now.
- `scorecard.py`: `u_read and u_read < max_read` skipped the largest
  invalidation, cache_read collapsing to 0.
- `facts.py`: `fails_sites` served a gc-collected run's census from the
  cached `latest_run` pointer with a dead `run:` citation; the pointer is
  honoured only while its manifest still exists.
- `_retrieval/rg_engine.py`: `follow_symlinks = true` never reached
  ripgrep (`--follow`).
- `worktree_isolation.py` (found by CI on this branch, not by either arm):
  every isolated worktree was created at `<tmp>/repo`, so git derived the
  same worktree id for two parallel wave nodes and one read the other's
  half-written `.git/worktrees/repo` entry. Leaf names now carry the node
  id and a unique suffix, and the three mutating worktree commands share
  one process-wide lock.

The same round's harnessed arm produced zero findings, not from a code bug
but from a harness/print-mode interaction: the main agent fanned out into 7
background bug-hunt agents, then ended its turn to wait for their
completion notifications — a `ScheduleWakeup` call is the last thing in its
transcript, whose tool result says "the harness re-invokes you when the
wakeup fires." Print mode (`claude -p`) is a single-shot process with no
such re-invocation; it instead waits for background subagents up to
`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default well under ten minutes)
after the main turn ends, then kills them — stderr shows "Background tasks
still running after 600s; terminating," and `subagent_stats` confirms all 7
were system-killed with zero completions, discarding real in-progress work.
The naive arm fanned out the same way and survived because its main agent
kept its own turn alive (polling for its subagents' notifications) until
all six had reported; the harnessed main agent took the wakeup tool's
reply at its word and ended its turn. That tool was present in both
children only because the launch inherited the parent remote session's
environment; nothing ctx injects — the ephemeral `--settings` hooks (33-37
ms per call in the transcript, returning plain allow), the output-discipline
prompt, the observer proxy — told the agent to delegate and wait. `ctx wrap
claude` now defaults `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` (wait
indefinitely) for print-mode launches, `ctx orchestrate`'s launcher sets the
same default for its `claude -p` nodes (which its own per-node timeout still
bounds), and `evals/matrix_runner.py` sets it for both arms of a pair so the
ceiling is never what a comparison measures; all three are skipped if the
caller already exported the variable. The runner also stops passing the
parent session's identity (`CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_CHILD_SESSION`) to its arms.

Disabling that timer stops it from killing the subagents, but it does not
touch the belief that made the main agent end its turn to wait for them in
the first place — `ScheduleWakeup` is built into this Claude Code build and
cannot be removed from a print-mode child by environment, and its tool
result still promises "the harness re-invokes you," which is false once a
`claude -p` process has nothing left to do. Both print-mode launch points —
`ctx wrap claude` and `ctx orchestrate`'s per-node launcher — now append a
short system-prompt notice telling the agent plainly that this is a
single-shot run with no supervisor, that no wakeup or notification will
re-invoke it, and that if it delegates to background subagents it must stay
in its turn and collect their results before finishing. It shares
`ctx wrap`'s existing opt-out (`CTX_WRAP_NO_DISCIPLINE=1`, or the caller's
own `--append-system-prompt`) and is skipped for interactive sessions. It is
not one of the byte-pinned prefix assets in `tests/test_prefix_stability.py`.
No live re-run of the round-17 scenario has happened yet with this notice in
place — the tests pin the injection, not a model's behavior in response to it.

The harnessed arm re-run on the fixed tree kept its turn and completed all
eight subagents (the lifecycle defect is closed) but hit the 30-turn cap
collecting them, so its eleven findings were read from the subagent
reports directly (evals/bugbash-round17-2026-09-04.md, "Harnessed arm,
re-run"). All eleven reproduced and are fixed, regression-tested in
`tests/test_round17_harnessed.py`; none overlapped the naive arm's ten:

- `_retrieval/get.py`, `_retrieval/spans.py`: bodies were split with
  `str.splitlines()` while the line index counts only `\n`, so a U+2028
  (or `\r`, `\v`, `\f`, `\x1c`, `\x85`, U+2029) shifted every line
  number after it. `ctx.textutil.index_lines` splits the way the index
  counts; search already did.
- `_retrieval/get.py`: an empty blob or stream is now an answer
  (`--lines 1:0 of 0 (empty)`) instead of a refusal whose suggested fix
  refused again.
- `orchestrator.py`: a host launch owns its process group and a timeout
  SIGKILLs all of it (`_run_bounded`), not just the host CLI.
- `commands/execute.py`: a bare `ctx job <id>` on a finished background
  job returns the run's exit code (3 on failure) instead of 0.
- `reflex.py`: every state mutator now holds one flock across its
  read-modify-write, so parallel hooks from one turn cannot lose each
  other's updates.
- `jobs.py`: the launcher records its supervisor's pid; a supervisor that
  dies before the first state write turns the job `failed` instead of
  leaving `--wait` polling forever.
- `installer.py`: `doctor_checks` closes the three stores it opens.
- `worktree_isolation.py`: `"."` as a declared target is refused with the
  module's own error, not an `IndexError`.
- `surface_gateway.py`: hiding a family that is not revealed reports no
  change.
- `engagement.py`: the symbol-grep list keeps the most recent 64, so the
  count no longer freezes.
- `edit_transactions.py`: a failed rename during rollback, or during the
  forward commit, no longer leaks its staged temp file.

The single-shot notice now also says that every blocking wait on
background work spends a turn and prefers foreground subagents; the
re-run spent fifteen of its thirty turns waiting.

`ctx prune`, and `ctx setup --prune`: the capability-surface audit run as
a setup step with a decision rule. The audit (`ctx surface audit`) and the
enforced compile (`ctx surface compile --profile`) had both shipped; what
was missing was the step between them that runs when the harness is
installed. Prune keeps ctx's own kernel and every capability whose
recommended disclosure level is L0 or L1 (read-only, or observed in use),
defers everything at L2 and above (unused, remote-write, destructive),
compiles that selection into each host's minimal launch config with the
existing emitters, and writes `.ctx-surface/prune-receipt.json` with the
per-turn tokens before and after and the repository's shape (languages by
file count, test-runner markers). Nothing is deleted; a deferred
capability stays reachable through the gateway or `--keep <id>`. Preview
by default, idempotent, and the same rule `ctx surface trim` already
recommended. On a terminal it asks instead of deciding: one selector per
group (MCP servers, skills, agents) listing each capability with its tokens
per turn, authority, observed use and the rule's mark; Enter accepts the
marks, `all` / `none` / `1,3-5` / `+2` / `-3` / `?2` adjust or explain;
then the hosts to compile for; then a confirmation. Kernel capabilities are
reported as kept and never asked about. The receipt records whether the rule
or the user decided each one. `--yes` (or a pipe, or `--json`) takes the
rule's answer without questions. `surface_profiles.compile_profile` accepts
a ready `Profile` for callers that decided the selection themselves.

`evals/improve_route.py`: the self-improvement loop round 17 ran by hand,
as one `ctx orchestrate` route -- hunt (explore, strongest model), verify
(reproduce every claim; write a failing test per confirmed one), harvest
(fix exactly what the tests pin), prove (suite and lint) -- with the task
ledger recording each step and a verdict computed from the four nodes'
JSON yields: precision = reproduced / claimed, promotable only when it
clears 0.8, at least one finding survived, and the suite passed on the
harvested tree. Promotable means "review this diff", never merge. Model-
free tests pin the plan's validity under the router's rules and the gate's
arithmetic; a live receipt is recorded when one exists.

The improvement route's first live run (evals/improve-route-2026-09-05.json,
recorded in evals/bugbash-round17-2026-09-04.md): hunt 158 turns on the
frontier model, verify 138 on the standard model, harvest killed at its
hour timeout after making 69 of 70 verify tests pass across 48 files; the
prove step, run by hand, found 3 regressions in the substitution family,
so the gate held the round. The hand review took 47 files and 64 tests and
refused the substitution changes (they re-decide a settled collapse rule),
a replay-metric redefinition, and a process-wide child subreaper. Among
the fixes taken: the collapse rewrite could override a secret-path
force-ask; `classify_read` resolved a relative path against the process
cwd rather than the workspace; the generated `ctx.toml` pinned 3 of 16
redaction patterns; `ctx seq --keep-going` halted on a step that failed to
spawn; `_authority_ok` failed open on a mistyped ceiling; two more
`splitlines()` sites disagreed with the line index; `install_claude`
never added a hook stage an older install lacked; a proxy retry could pop
a second stale pooled connection; `plan_exec` crashed on an explicit
`null` wall budget; `wait_or_kill(proc, 0)` did not kill an orphaned
grandchild whose leader had already exited. The route now runs one
attempt per node with a 100-turn ceiling and labels its cost estimate a
placeholder; the first run cost 57 times it.

Review round on this wave (Codex, four findings, all confirmed and fixed):
prewalk is armed only when the handoff it asks for can happen (a second
attempt allowed and a cheaper unattended model installed), so a compliant
frontier worker is never told to stop after one edit with nobody to hand
to; the cheap handoff attempt is priced against the remaining budget like
any claim instead of passing on `remaining > 0` alone; the call graph
registers a file under every package root it is importable from (an
unbroken chain of package directories), so a nested `pkg.sub.store` keeps
its outer prefix beside a loose file in the source root; and the job
launcher records its supervisor's pid in its own file rather than
read-modify-writing the supervisor's `meta.json`, which raced however many
times it re-read first.

## [0.36.0] - 2026-09-02

Orchestrated harnesses now collaborate over a task ledger, and a run survives
its orchestrator. `ctx orchestrate` writes a per-task, append-only ledger of
six closed-vocabulary rows — task, claim, handback, steward, verdict, inbox —
under `.ctx-session-reads/tasks/`. Every launch is claimed (host, model,
expected turns and cost) and handed back with a typed reason and failure kind,
its checkpoint, turns and actual cost. A handback other than `done` goes to a
deterministic steward: it classifies why the host stopped, offers the recovery
policy only the actions that exist for that node (a stronger installed model,
a coordinator with re-plans left, another attempt), and appends its decision
before acting. The policy is the AlphaEvolve `choose_recovery` seed, evolved
for lever `recovery-escalation` and never wired in, promoted verbatim as
`ctx.recovery_policy`; it replaces the fixed escalate-one-tier-once rule.
A one-shot host's own `permission auto-denied` / read-only-workspace
execution failure stays escalatable, as the acceptance suite always required;
a login failure now stops instead of buying a stronger model, and a stop
chosen because nothing applicable existed is labelled `stop_blocked`, never
`stop_budget`. Budget is checked against ledger actuals, never below the
estimate where an attempt went unpriced, and a claim whose estimate exceeds
what remains is refused before launch; a claim reserves its estimate until
its handback and the check-and-claim is one step under the ledger lock, so
two nodes of one wave cannot both pass against the same balance. Hosts' turn counts are parsed (Claude
`num_turns`, Codex `turn.completed`) and summarized; a node past
`[orchestrate] expected_turns` hands back `over_turns` as a complexity
signal, and `turn_ceiling` > 0 hard-bounds Claude nodes with `--max-turns`.
`ctx orchestrate --resume <task>` (no task argument needed) rebuilds the plan
from the ledger, restores nodes with a `done` handback without re-running
them, and re-runs nodes that were claimed but never handed back; nodes a
coordinator re-plan added are on the ledger as a further task row, so they
are restored too. `ctx task ls|show|inbox|send` and the MCP
ops `task`/`inbox`/`send` read the ledger and hand a node an address — never
content — which reaches it in its prompt. Ledger rows carry no task text or
output: the goal is a `blob:` address, output a `checkpoint:` address, the
inbox ref must parse as an address (with `ctx get` options at most) and is
bounded, and the inbox note is the one sanitized free-text field. Model-free receipt
over injected hosts: resume saved 6 of 16 launches a naive restart makes with
no node run twice; typed recovery spent 43% less than the fixed rule across
seven failure shapes; the claim check held spend inside a budget the estimate
had said was fine. The skill body now tells a node how to read its inbox and
hand addresses forward, and the MCP tool description glosses the three new
ops; both are injected prefix assets, so this intentional change bumps
`PREFIX_VERSION` to 11 and cold-invalidates every user's prompt cache once.

## [0.35.1] - 2026-08-21

The AlphaEvolve evidence is now presented as one receipt-backed benefit ledger
across the README, documentation, and published site. It separates measured
product paths, deterministic safety/coverage gates, scoped canaries, modeled
or inactive candidates, and rejected regressions instead of combining them
into a misleading product-wide percentage. The site adds a discoverable
AlphaEvolve benefits guide and homepage summary. The optimization charter also
reflects the shipped opt-in worktree path and identifies the measured 21k–23k
tiny-task context surface as the next campaign target.

## [0.35.0] - 2026-08-21

The oh-my-pi mechanism wave turns three previously informal orchestration
ideas into bounded contracts. `ctx edit plan`, `preview`, and `apply` seal an
edit against its workspace and source snapshot, relocate only a unique
byte-identical target, preflight the complete edit set, and attach a
content-addressed post-edit diagnostic receipt. Refusals do not leak source or
replacement text. Python, JSON, and TOML syntax checks ship as the stdlib
diagnostic floor; external LSPs remain an injected adapter rather than a
background process owned by ctx.

Opt-in disjoint mutation waves may now execute in isolated Git worktrees.
Every worker declares its allowed targets and emits a typed yield; ctx captures
tracked, untracked, and binary changes as patches, rejects out-of-target
writes, preflights the whole wave, and applies all patches or none. Non-Git,
dirty, nested, overlapping, undeclared, and later mutation waves retain the
serial shared-workspace path. Worktrees are transaction boundaries, not OS
sandboxes.

Archive promotion and streaming-output rules also have frozen, deterministic
evaluators and adversarial gates. They remain inactive experiments until their
matched canaries pass. The implementation ledger and clean-room provenance are
in `docs/OH-MY-PI-INTEGRATION.md`; the dated receipt records live, simulated,
and skipped evidence separately.

The hook now records what happened to the host's own edits. It has always seen
every `Edit`/`Write`/`MultiEdit` — `_tool_kind` classifies them and PreToolUse
allows them through — and never looked at whether they landed, so the rate of
the field's most-cited harness failure was unknown here. A PostToolUse observer
classifies each edit result into a closed vocabulary (`applied`, `not_found`,
`not_unique`, `other_error`, `unknown`) and appends a privacy-safe row: outcome,
tool, host, a path digest and string *lengths*, never the edited content.
Unrecognised host wording stays `unknown` rather than being forced into a
bucket, so a reworded error surfaces as drift instead of a silent mis-count.
Hosts without a PostToolUse payload (Antigravity) contribute nothing, and the
summary reports which hosts it heard from.

A paired model-free receipt measures the ceiling that rate will be judged
against: of failures where the model had the right region but reproduced its
whitespace differently, a content-based repair resolves 76.1% unambiguously and
refuses 21.5% as ambiguous; of the control failures where the model named
different content, it resolves none; and across 1,669 resolutions it never
landed on the wrong region. Arms are reported separately on purpose — how often
each shape occurs is field data the ledger now collects, not something the
simulation can know. No repair mechanism ships in this change; the instrument
ships first and the mechanism waits on its numbers.

## [0.34.0] - 2026-08-20

A `repo:` line address now names content rather than a position. Line numbers
into a live worktree file went stale the moment anything above them changed, so
re-resolving `ctx get repo:m.py --lines 4:5` after an edit returned different
code, exited 0, and said nothing — the one address family that could not keep
the project's own "same address, same bytes" promise, and the one an agent uses
most while editing. `--lines A:B@anchor` appends a short content digest: it
verifies silently, relocates and declares the move when the content shifted,
and refuses with a re-navigation path when the content is gone. Anchors are
minted on `repo:` line reads, on their continuations, and by `ctx def`, which
now labels its frozen `span:` address and its anchored `live:` one separately
instead of offering the snapshot for both — the previous shape returned the
pre-edit body to a reader asking about current code. `--hashlines` renders
per-line content tags (`L40:a3| …`) for naming individual lines. Immutable
handles mint nothing, since their bytes cannot move. The MCP `get` selector
takes the same grammar as the CLI. A dated model-free receipt replays real edit
shapes over this repository's own source: 99.9% of unanchored re-resolutions
returned different content silently, anchored ones answered correctly in 75.8%
of cases (1,454 of 1,455 by following moved content), refused the rest, and
were never wrong, for about 20% overhead on a bare `repo:` line address.

- The artifact store now proves backend writability and uses a sticky,
  doctor-visible workspace-local fallback when managed sandboxes make the
  default user-state directory read-only. Parallel catalog initialization
  retries lock races without masking other database errors. Published install
  docs now use `pip install ctx-harness`, and the field comparison includes a
  dated TokenSave/WozCode/rtk integration-gap ledger. Generated host guidance
  now states the exact `ctx ask` symbol and run-receipt contracts; this
  intentional prefix change bumps `PREFIX_VERSION` to 10.

## [0.33.0] - 2026-08-19

AlphaEvolve now covers setup DevEx, command-span expansion, and four
orchestration policies. Read-only DAG nodes run in bounded parallel waves,
shared-workspace mutations serialize, high-risk verification moves to an
independent capable host when available, and checkpoint handoffs scale from an
exact address to bounded diagnostic evidence. A 269,696-case generated matrix
completed with zero policy failures. The managed setup now has a dedicated
generative-chat engine and assistant; multiple bounded campaigns are recorded
in the dated orchestration receipt. Automatic command capture also preserves
`env`, timeout, and other launcher wrappers instead of dropping their execution
semantics. `ctx setup` is
the short human front door,
and a successful doctor pass records a privacy-safe managed-config fingerprint.
An unchanged repeat takes a verified no-op; upgrades, failures, host changes,
drift, or `--repair` return to idempotent repair and full verification. The
paired local receipt measured 4.42× lower repeat latency, 8.17× less output, and
zero repeat host-config rewrites across 11/11 successful pairs. The guard now
runs bounded/structured queries directly, captures known noisy reads, and keeps
unknown or mutating commands at the permission boundary. Its 57,313-case
generated matrix found and closed a compound-command rewrite bypass, then
completed with zero classification failures. The portfolio now spans 31 levers
and 20 experiment families.

## [0.32.1] - 2026-08-19

The PyPI release workflow now reads the version source with `runpy` instead of
importing `ctx` from an uninstalled `src/` checkout. The first `v0.32.0`
GitHub release proved both artifacts but stopped at that pre-publish tag check;
no files reached PyPI. This patch preserves that public tag and retries the
same verified package through Trusted Publishing.

## [0.32.0] - 2026-08-19

Codex integration now follows the live host contracts exactly. The MCP stdio
fallback separates the Python executable from `-m ctx` arguments instead of
asking Codex to execute a nonexistent filename containing spaces. PreToolUse
pass-through emits `{}`; explicit `allow` is reserved for responses carrying
`updatedInput`, and unsupported ask decisions degrade safely to deny. Re-running
`ctx wrap codex` repairs older complete configs generated by ctx without
rewriting user-managed TOML. The release artifact gate exercises both contracts.

Graduated steering now promotes its first completion-safe live arm. On Claude
Code and Codex, a passive session may run one explicitly named pytest node
natively when that signature has no prior flood, avoiding the fixed capture
and digest tax that made the measured small named-test case worse than naive.
Whole suites and Antigravity remain birth-gated; the fail-closed PostToolUse
gate captures an unexpected flood, selects the command-aware typed digest,
records an intervention, and disables further speculation for that signature.
Privacy-safe decision/result receipts record byte volume, gate activation, and
error status for live evaluation; `[guard].speculative_native = false` restores
always-capture behavior. The matched local receipt measured 20.15% lower median
latency and 46.67% fewer tool-result bytes on the formerly losing named-test
case; the real fallback gate contained a synthetic failure 48.15x. These are
local path measurements, not billed-token or proven production-savings claims.
The full AlphaEvolve-to-product evidence chain is recorded in the
[dated receipt](evals/alphaevolve/2026-08-18-speculative-native.md).

Orchestration receipts now capture real per-attempt usage from Claude's JSON
result, Codex's JSONL events, and the ctx-owned Antigravity SDK record. The
aggregate includes coordinator, failed, escalated, and replan calls; cached
Codex input is normalized before pricing, and unsupported attempts remain
explicitly partial or unavailable rather than appearing free. Prompt-free
route-replay exports carry the same structured measurement.

The AlphaEvolve portfolio now runs six optimization surfaces through one local
search/holdout/adversarial scorecard, including a typed recovery/escalation
policy. The scorecard evaluates the integrated fast-path policy rather than its
deliberately naive search seed and reports interpretable baseline multipliers.
Two bounded managed campaigns found no candidate better than the reviewed
recovery and receipt-informed routing seeds; ties and hard-gate failures remain
unpromoted and are recorded with the negative live prompt result.

AlphaEvolve deployment now spans 24 named production levers through a versioned
registry and 15 bounded experiment families. New completion-gated campaigns
cover capability-surface selection, digest-profile detection, small-result
pass-through and output delivery, retrieval strategy, command/read birth gates,
engagement/reflex behavior, evidence-plan compilation, repository context,
backgrounding, and cache freshness. A shared evaluator scores robust
multiplicative gains only after safety and completion, fingerprints each
dataset, and exposes wave, managed-readiness, counterfactual-shadow, and
promotion reports. Actual-usage accounting, secret/workspace guards, and
receipt integrity are explicitly immutable oracle planes.

### Installable, verifiable releases

`ctx-harness` now builds a self-contained wheel and source distribution. The
wheel carries the Antigravity, Claude Code, and Codex host assets plus the
optional Antigravity SDK shim; version metadata has one source of truth, and a
distribution smoke test installs the wheel outside the checkout before
exercising `ctx --version` and all three host renderers. CI runs that artifact
test rather than relying only on an editable install.

GitHub releases can publish through PyPI Trusted Publishing with a short-lived
OIDC credential. The workflow rebuilds from the tag, checks its version,
validates both artifacts, and publishes only from the protected `pypi`
environment. Local release instructions and first-publish configuration are in
`docs/RELEASING.md`.

The wrapper setup now dogfoods all locally detected hosts. Generated Codex and
Claude wiring stays machine-local, `ctx doctor` validates the public rewrite
contract, streamed proxy connections are safely reusable, and content-addressed
cache keys prevent same-size, timestamp-preserving edits from serving stale
repository maps or guard policy.

The orchestrator and its deterministic fallback now apply the completion-gated
lesson from the AlphaEvolve portfolio: high-confidence explain/inspect, named
test, diff-review, and explicitly small-edit requests use one- or two-node
routes and skip the coordinator instead of always paying for coordination plus
`explore → frontier plan → implement → verify`. Anchored whole-word
normalization avoids the evolved candidate's `latest`/`testimony` substring
bug; ambiguous work keeps the coordinator and full fallback, and every mutation
keeps verification.

Orchestration now records prompt-free route receipts and keeps semantic success
in a separate explicit-evidence ledger for iterative replay. Automatic routing,
escalation, and coordination exclude interactive-only hosts unless explicitly
pinned. A receipt-informed AlphaEvolve campaign found no policy better than the
reviewed fast-path compiler, so generated code remains unpromoted.

A second live replay iteration expanded the corpus to answer, inspect, review,
test, small-edit, and general-feature routes. It found two false-success paths:
coordinator-authored pins could bypass unattended eligibility, and a host could
exit zero while explicitly reporting permission denial, read-only blockage, or
incomplete verification. Coordinator pins are now advisory unless an API caller
explicitly approves interactive pins; mutation plans require a downstream
verifier; explicit failure reports escalate instead of counting as completion.

For low-risk feature work with a named file, named tests, and an explicit
behavioral contract, a live-proven `explore → implement → verify` route now skips
the frontier planning turn. Underspecified and high-risk work retains the full
route. On the matched disposable task this reduced estimated spend by 70.4%,
estimated visible context by 44.2%, model turns by 25%, and wall time by 52.7%
while passing every acceptance test.

An `evals/alphaevolve/` portfolio provides bounded search paths for evidence
selection, context allocation, turn policy, route policy, and naive fast paths.
Seeds are isolated from production, model-free evaluators enforce completion,
determinism, budgets, and timeouts, and cloud execution requires explicit spend
confirmation. Managed winners remain quarantined until holdout and adversarial
gates pass; production integration is a reviewed implementation, never a copy.

### Binary output gets a typed, addressable profile

`ctx run` now detects image, PDF, archive, executable, and unknown binary
output by magic bytes before text profiles run. The complete bytes remain in
the artifact store; `binary/v1` emits bounded structure, exact SHA-256 identity,
and a working run handle instead of replacement-character text.

`ctx image digest` inspects workspace-confined files without inlining their
payload. `ctx image diff` reports deterministic dHash distance and byte
identity for two decodable images when the optional `[image]` extra is
installed. PDF page objects and text operators are explicitly labelled as
byte-scan heuristics; absence is not reported as proof of a scanned document.

### The prefix budget is measured, not narrated

**What the harness actually costs a session: ~708 tokens.** Not the ~3,800 an
earlier reading of this module produced.

`prefix_assets()` tracks five cache-keyed texts, and only some are resident in
every prompt. That distinction lived in a docstring, and the docstring was
misread: summing all five counts the 2,586-token skill body (loaded only when
the skill triggers) and the whole 638-token explorer agent (only its
description enters the parent prompt; the body travels with the subagent). The
result was a **5.4× overstatement of this project's own overhead**, in the
direction that makes it look worse.

Fixed at the root rather than in prose. The split is now data —
`RESIDENT_ASSETS` and `DEFERRED_ASSETS` — with `resident_bytes()` and
`budget_report()` reading it, and `tests/test_prefix_budget.py` holding it to
the published numbers. A new prefix asset must be classified or the suite
fails, so the budget cannot silently stop describing reality.

The resident total also gains a **ceiling**: under 1,000 tokens, asserted.
That is the number a user pays on every session in every repository forever,
so growth in it should require someone to raise the limit on purpose.

Also corrected below: an entry that claimed a cold-cache cost for a skill-body
edit that is not prefix-resident.

### `q` reaches the bounded tier, and the replacement surface triples

**Prompt-cache impact: `PREFIX_VERSION` 8 → 9** (the MCP tool description is a
prefix asset). Same one-time cold-cache cost as the 7 → 8 bump below; users
upgrading across both pay it once.

**The composition algebra is now an MCP op.** `ctx q` — a total pipeline over
typed record streams, 17 stages, joined by `|`, no loops, no recursion, hard
8-stage cap — shipped CLI-only, on the recorded grounds that MCP wiring would
churn the prefix asset. The cost of that deferral was that the sharpest
turn-compressing surface in the harness (locate → narrow → read in *one* call
rather than three round-trips) was reachable only by shelling out, while the
bounded tier got the heavier `investigate` plan interface instead. One enum
entry plus one options key is a far smaller prefix delta than the tool it was
implicitly being weighed against, so: `op: "q"`, `options.pipeline`.

Totality is the whole argument for why it is safe there — statically boundable
cost is exactly the property `ctx py` lacks, and why py stays CLI-only. Bounds
are inherited rather than re-implemented: `run_query` already bounds its render
against `result_tokens`, which `_dispatch` tightens to the caller's
`maxTokens`. A malformed pipeline **raises** rather than returning its teaching
line as content — as content, a failure description reaches the model as a
*successful* result, which is the fail-open shape this codebase keeps finding.

**The replacement surface goes from 3 command shapes to 8.** rtk's breadth idea
vendored: `head -n N`, `sed -n 'A,Bp'`, `wc -l`, `find -name`, and
`ls -R`/`tree` now collapse to their bounded, addressed equivalents. This was
never an architectural gap — a substitution only ships where a bounded `ctx` op
means the *same* thing, and nobody had walked the common commands looking for
those pairs.

The bar is **equivalence, not plausibility**, and it earned its keep
immediately: the first cut generated `corpus --glob X | files`, which is a type
error (`corpus` already emits `files`; the `files` stage consumes `sites`), so
every substituted `find` and `ls -R` would have handed the agent an invalid
pipeline. The equivalence test caught it before it shipped.

Most of `tests/test_substitute_common_commands.py` is negative cases,
deliberately — a recogniser that fires too eagerly answers a question the
operator did not ask, under their own command, which is this project's own
complaint about the lossy filters. So `tail` is **not** handled (`ctx get` has
no from-the-end window; any mapping would guess), `head -c` is not (byte mode
is a different range unit), `find … -exec`/`-delete` are never rewritten (they
have effects), and a flat `ls` is left alone (cheap and honest).

Still unmeasured, and filed as `ctx debt`: which commands agents actually run.
All five rungs were chosen by inspection — the same guessing the field scan
criticised. A command-frequency corpus is the instrument that would replace it.

### The solution ladder gains its missing rung — and a safety exemption

**Prompt-cache impact: none — correcting an earlier claim in this entry.**
This originally said the rule-13 edit cold-invalidates every prompt cache at a
cost of ~56k tokens / ~$0.21 per model. That was wrong. Rule 13 lives in the
skill **body**, which is loaded when the skill triggers and is *not*
prefix-resident; the bump policy is "only when prefix-resident bytes change".
The manifest needed regenerating, the version did not need bumping, and users
pay nothing for this edit. `PREFIX_VERSION` still moved 7 → 8, which is
harmless and is left in place rather than rewritten.

The underlying mistake — reading "tracked as a cache-keyed asset" as
"resident in every prompt" — is the same one that produced a 5.4×
overstatement of this project's own per-session overhead. It is now fixed at
the root: see *The prefix budget is measured, not narrated* below.

A rung-by-rung audit of our ladder against the
[Ponytail](https://www.alphamatch.ai/blog/ponytail-ai-coding-skill-2026)
decision ladder (written up in `evals/field-devex-2026-08-02.md`) found we had
adopted five of six rungs and were missing one: **"is there a native platform
or runtime feature?"** — the rung that catches a hand-written helper for
something the language or host already does. Added.

Also added: an explicit **exemption list**. The ladder does not apply to trust
boundaries, data loss, security, or accessibility; on those four, write the
fuller version. Ponytail carries the same carve-out and we did not, which left
a foreseeable failure where "prefer the one-liner" meets an input-validation
path.

Rung *order* deliberately still differs from Ponytail's: they check stdlib
before installed dependencies, we check *reuse what exists* first. Reaching for
`hashlib` when the repo already exposes a shared helper is the wrong move even
though both are "simple" — not hypothetical, it is exactly the defect an
automated reviewer found in this branch's rewrite guard.

### Field scan: two neighbours added, two devex gaps admitted

`evals/field-devex-2026-08-02.md` — desk research, explicitly not a
head-to-head, and explicitly not permitted to move any published performance
number. Adds **TokenSave** and **wozcode** to `docs/COMPARISONS.md` and records
two places the field beats us:

- **Distribution.** Every peer installs in one step from a published artifact;
  we are `git clone` → `pip install -e .`. `ctx wrap setup` is the best
  onboarding step in the field and is unreachable until that is fixed.
- **Malleability.** Teaching the harness a new output family means editing
  `src/ctx/digest/` and the `_PROFILES` tuple — carrying a fork. Maki's users
  shape their agent from a user-space `init.lua`. A closed profile registry
  caps a system whose whole thesis is that output families are diverse.

Both are filed as `ctx debt`, not fixed here.

### README and docs: the two surfaces you actually install

The MCP server and the skill now have sections explaining what makes them
*good*, not just what they are: one stable tool with an `op` discriminator
(against the 40+-tool alternative, with the prefix-churn argument and the
discoverability cost both stated), bounds declared in the schema *and* clamped
at runtime, no execution surface, and — for the skill — progressive disclosure,
trigger-condition descriptions, numbered scoreable rules, and the honest note
that advisory means bypassable.

### The call graph gets scope, a second language, and its disclosure back

`ctx callers/callees/impact` were the only code verbs that did not ride the
resolution ladder the rest of the harness already uses. They built a private
index that bound a call to `foo` to *every* in-repo `def foo`, and the cost was
measured on this repo: 152 of 2,964 definition names collide, putting **501 of
3,313 definitions (15%) behind an ambiguous name**.

Three defects fell out of that, all reproduced as regression tests in
`tests/test_callgraph.py`:

- **`callees` merged same-named definitions silently.** `ctx callees render`
  unioned the callees of 22 unrelated `render` methods into one 31-call answer
  — `statusline`'s `_fmt_usd` beside `jsonprof`'s `_dominant_array` — with no
  note. `cmd_callers` emitted the ambiguity note; `cmd_callees` never did.
- **A qualified query silently answered the unqualified question.**
  `in_edges` was keyed by *unqualified* callee name, so
  `ctx callers LogTemplateProfile.detect` returned 32 rows labelled "exact by
  name", including `hosts.detect_all` — which calls the unrelated module-level
  `hosts.detect`. Worse, narrowing the target to one definition is what
  suppressed the ambiguity note, so disambiguating successfully hid the warning.
- **`impact` disclosed nothing at all** and walked an unqualified frontier, so
  one collision at depth 1 pulled its whole cone in: `ctx impact put_blob`
  reported 1,902 reached out of 3,313 definitions.

**Resolution is now tiered and its confidence is on every edge.** A call site
binds to a definition in the calling file (`local`), else in a file the caller
*directly* imports (`import`), else repo-wide (`repo`). Only the first two are
stated as fact; `repo` edges are held back by default with their count and the
`--unscoped` flag that resolves them (CONTRIBUTING §4). Direct, not transitive,
is what discriminates: `ctx.hosts` *does* transitively reach
`ctx.digest.logprof` through installer→hook→digest.

Measured on this repo, `ctx callers LogTemplateProfile.detect`:

| | v1 | v2 |
|---|---|---|
| rows | 32, labelled "exact by name" | 1 |
| answer | incl. `hosts.detect_all` (wrong) | `detect_profile` `digest/__init__.py:61` |
| ambiguity note | none | omission count + `--unscoped` |

That single row is the same site `ctx refs LogTemplateProfile.detect` finds via
jedi — the precise answer was already in the tree, and the call-graph verbs
were the only ones not asking for it.

**Nothing here is hand-rolled that a declared dependency already does.** Nodes
outside Python come from `ctx.skeleton` (tree-sitter, 20 extensions,
content-cached); call sites come from one ast-grep pattern (`$F($$$A)`) over
the 16 languages `ctx.astgrep` already maps; the import graph is the one
`ctx.repomap._grimp_edges` already resolves; traversal uses networkx when
importable. Every rung is an existing optional extra with the stdlib `ast` path
as the always-available fallback (CONTRIBUTING §1), and the rung in force is
printed in the header of every answer.

- **Polyglot edges** — the v0.15.0 note deferred tree-sitter breadth "pending a
  measured win"; the win is that `ctx stats` on `native/ctx-hook-native/src/main.rs`
  returned 13 symbols and spans while `ctx callers lexical_normalize` answered
  *"no definition ... in workspace Python sources"*. It now returns both call
  sites. Rust/Go/TypeScript covered by `tests/test_callgraph_polyglot.py`;
  `CTX_CALLGRAPH_ENGINE=ast` pins the Python-only corpus.
- **New `ctx impls`** — type hierarchy, the question `ctx q 'refs Profile |
  group file'` could only approximate (30 rows mixing import lines with class
  declarations and test files). `ctx impls Profile` returns the 14 subtypes
  with coordinates, plus the inverse `extends:` direction.
- **New `ctx cycles`** — circular imports between files, or mutual recursion
  with `--calls`. Operational, not aesthetic: a circular import is why the
  module fails to load. On this repo it finds 5 import cycles, including
  `query → filesets → facts → query` (the last two lazy, inside functions, so
  no single file reads as circular). Components come from Tarjan via networkx
  when importable, else an **iterative** stdlib implementation — recursion
  depth in Tarjan is the longest path, so the textbook recursive form turns a
  5,000-file import chain from a diagnostic into a `RecursionError`. Both
  engines are asserted to return identical output.
  Call cycles use scoped edges only: an unscoped edge does not merely add a
  row to a cycle search, it fuses unrelated components into one phantom cycle.
- **Call-site lines on every caller row.** v1 printed the caller's *definition*
  range, so seeing the actual call cost another read; two calls from one
  function collapsed to one row. `digest_output` now shows both
  `digest/__init__.py:249` and `:250`.
- **First-party and test callers are grouped**, not interleaved: 26 of 37
  `callers put_blob` rows were tests, sorted in among the 11 production callers
  that were the answer.
- **Per-file caching.** The cache key was one `stat_fingerprint` over the whole
  corpus, so any edit rebuilt everything — 170 ms warm against **1,754 ms**
  after a single-file touch, on the hot path of an agent that edits constantly.
  Units are keyed per file: the same edit now costs **446 ms**, a 3.9× cut,
  while covering 259 files instead of 95.
- **`ctx refs` rejects an out-of-grammar target** instead of degrading to a
  textual scan. Given `repo:<path>:<Symbol>` (which is `ctx def`'s grammar) the
  ladder fell past SCIP and jedi to a word-boundary regex that matched the
  symbol's own name inside the argument and returned argparse string literals
  as "references". A degradation that turns a more precise question into a less
  precise answer is worse than an error.

#### Engine selection follows its own documentation again

`ctx.skeleton._TS_GRAMMAR_MODULES` documents the individual grammar wheels as
"the maintained, offline path", and the bundles (`tree_sitter_language_pack`,
`tree_sitter_languages`) as lagging the core API and fetching parsers at
runtime — a network 403 in a sandbox. Selection then tried the bundles
**first**, so a stale bundle that merely imported outranked a current wheel.
The order now matches the note; the bundles stay as the fallback for languages
no wheel is declared for. Both directions are pinned in
`tests/test_treesitter_backend.py`.

Dependency floors now track the versions CI actually exercises rather than the
oldest release that once worked: `tree-sitter>=0.25` (was `0.22`, against 0.26
current), `ast-grep-py>=0.45` (was `0.37`), `jedi>=0.20`, `grimp>=3.15`,
`networkx>=3.4`, and the grammar wheels to their current majors. The old floors
permitted resolving an install two API generations behind anything under test.

*Considered and declined, with a receipt:* `griffe` resolves class bases to
canonical paths and would have been the obvious library for `ctx impls`. A
differential run over every base in this repo found **zero disagreements** with
the tier resolver already in place, while griffe costs 0.79 s of load, a new
dependency, and Python-only coverage — the tier resolver answers for Rust, Go
and TypeScript through the same path. `PyCG` was also evaluated and is
unusable: 0.0.8 ships a `PyCG/` package directory whose own modules import
`pycg`, so it fails at import.

### Routing gains dimensions beyond price — and a provenance rule

Routing chose between models on capability tier and price alone. Both are
coarse: they cannot express that one model greps instead of dumping, or that
the cheapest model per token was the most expensive arm in a real build.

- **New `ctx/data/model-catalog.json` + `ctx.catalog`** carry specialities,
  anti-specialities, latency class, measured throughput, benchmark slots and
  this repo's own observed-behaviour receipts, overridable per repo with a
  `.ctx-catalog.json` (merged per model, so tuning one does not restate the
  table).
- **Every quantitative claim carries a `source`**, enforced by
  `lint_catalog()` and `tests/test_model_catalog.py`. Benchmarks ship *empty*
  rather than invented: public scores for these model versions are not in this
  repo's evidence base, and a fabricated number is indistinguishable from a real
  one at the point of use while silently steering every routing decision.
  `declared-heuristic` is a legitimate source value and means exactly that.
- **Absent data reads as UNKNOWN, never as bad.** An unmeasured model scores
  neutral rather than being deprioritised, and unknown latency sorts as
  `moderate` rather than optimistically as `fast`.
- Measured throughput ships for `gemini-3.6-flash` (91.3 output tok/s) and
  `gemini-3.5-flash-lite` (58.8), both n=8 from isolated single-agent runs. The
  Claude models are deliberately absent: the only wall-clock data here mixes
  model time with Playwright grading, and publishing that as throughput would be
  false precision.

**Prompt-cache impact: `PREFIX_VERSION` 6 -> 7.** `SKILL.md` gained a surface
section (skill vs CLI vs allowed MCP servers) and a progressive-disclosure index
so a reference is loaded only when the task needs it. That rewrites the injected
prefix once for every user — taken deliberately, because the index is what stops
the new `references/model-catalog.md` from being read on every task.

Breaking, taken deliberately while the user count is zero: two command names
were wrong, so they were fixed at the source rather than described around.

- **`ctx eval` is now `ctx py`.** It runs a Python script; `eval` reads as
  shell-eval and taught the wrong thing every time an agent saw it. The hook's
  teaching string, the skill, and all docs move with it.
- **`ctx investigate` is gone; use `ctx plan run`.** Its own docstring said it
  was "Same execution as `ctx plan run`, plus the epochal-control ledger" — one
  behaviour behind two names. `plan run` absorbed the ledger and the
  `--replans` / `--advise` flags; `investigate` survives where it is still the
  right word: the artifact family and the `investigate/v1` digest an
  investigation *produces*.
- **Prompt-cache impact:** both names appear in cache-keyed prefix assets, so
  `PREFIX_VERSION` goes 4 → 5 and every model's prompt prefix is rewritten once
  on first use. Cost is one cold prefix per model; taken now precisely because
  it is free today and would not be later.
- `ctx --help` no longer lists 34 commands as a wall (see the CLI front-door
  entry below); the count is now 33.

## [0.31.0] - 2026-07-24

Harness collaboration: `ctx wrap` stops knowing three hosts by name, and starts
routing work across the harnesses it finds by what their models cost.

- **M-M · Data-driven host registry** (`src/ctx/hosts.py`): one `HostSpec` per
  coding-agent CLI states how to detect it on PATH, how to resolve its model,
  which installer/wrapper wires it, and whether its output side can substitute
  (enforced) or only nudge. Adding a host is a data edit. Each detected CLI is
  joined to `ctx.pricing` so it carries a model→price tier. The three shipped
  hosts move in verbatim; extra CLIs (Gemini, Cursor, aider, opencode) are
  detected and priced but marked not-yet-harnessable rather than silently
  dropped.
- **`ctx wrap detect`** prints an installed/model/price table across every
  registered CLI; **`ctx wrap setup` is now detection-driven** — it configures
  the harnessable CLIs it finds and names the ones it skipped, while
  **`ctx wrap all`** forces every supported host (the old behaviour). The
  low-level `setup_hosts` primitive is unchanged.
- **M-M · Harness collaboration orchestrator** (`src/ctx/orchestrator.py`,
  `ctx orchestrate "<task>"`): **task coordination, not open-loop calling.** A
  cheap coordinator — the cheapest installed harness priced by its *coordinator
  model* (Antigravity on Gemini-flash-lite), guided by the routing skill —
  splits the task into a `ctx.route/v1` DAG. Each node is routed by **capability
  × price** at the *(harness, model)* level (`hosts.pick_model`) — the model that
  clears the node's `min_tier` and covers its roles: explore/verify to an economy
  model, implementation to a standard model, and the plan node to the frontier
  flagship. The DAG is validated (acyclic, bounded, budgeted) and
  **priced up front, shown, then run in a closed loop**: ready nodes run in
  parallel waves; each dependent sees only its upstreams' `ctx.checkpoint/v1`
  digests (addressed evidence, never raw bytes); a failed node escalates once to
  a stronger harness; between waves the coordinator may patch the plan with
  follow-up nodes. When no coordinator can run, a deterministic model-routed
  fallback DAG (explore→plan→implement→verify) is used, so orchestration works
  offline. Bounded by `max_waves` / `max_replans` / `budget_usd`; fail-open
  throughout; a single installed harness degrades with zero claimed saving.
- **Routing is by model, not just harness.** `HostSpec` carries a `models`
  catalog — each harness runs several models spanning tiers (Claude:
  opus-4.8/sonnet-4.6/haiku-4.5; Codex: gpt-5.6 sol/terra/luna; Antigravity:
  gemini-3.1-pro/3.6-flash/3.5-flash-lite), researched from each CLI's model
  list. `hosts.pick_model` chooses the `(harness, model)` that clears a node's
  tier and covers its roles, with a `prefer` knob: **planning takes the frontier
  flagship (Opus) via `prefer:"strong"`**, while **implementation is
  complexity-adaptive** — `standard` (Gemini 3.6 Flash) for real work, `economy`
  (Gemini 3.5 Flash-lite) for a simple edit (`[orchestrate] implement_tier`, or
  the coordinator's per-task judgment). Routes deliberately per model even within
  one harness (Claude-only: explore→Haiku, plan→Opus, implement→Sonnet). Coverage
  scores on model roles (not host strengths, which had pulled work onto
  broadly-tagged hosts). Nodes can pin `"host"`/`"model"`/`"prefer"`; escalation
  bumps to a stronger model; the catalog is in the routing skill. New
  gemini-3.6-flash / gemini-3.5-flash-lite price rows.
- **Routing skill** (`references/harness-collaboration.md`): the `ctx.route/v1`
  contract and capability×price routing rules, kept in lockstep with
  `ROUTING_CONTRACT` so the coordinator behaves the same from the skill or the
  inlined prompt.
- **`[orchestrate]` config block** (`ctx.config.OrchestratePolicy`): closed-loop
  bounds (`max_nodes`/`max_waves`/`max_replans`/`budget_usd`/`node_timeout`),
  `fallback_only`, `confirm` gate, and per-node token estimates.
- **Live cross-vendor collaboration on a real task, proven**
  (`evals/live-collab-antigravity-claude-2026-07-24.md`): Gemini (Antigravity's
  model, via the API) plans, Claude — running as-is with its own Edit/Bash tools,
  no `ANTHROPIC_API_KEY` — implements from the plan's `checkpoint:` and runs the
  test itself; a failing test goes **green**, verified outside the model. Real
  tokens billed (~$0.11), both providers exercised, through the actual
  `run_route` loop. Also fired the **failure-escalation** path on a real failure
  (`--dangerously-skip-permissions` refused under root → node re-routed).
  Surfaced and fixed a real gap: launch-time model ids differ from
  display/pricing ids — Claude wants `haiku`, the Gemini API serves
  `gemini-3.5-flash-lite`. Added `ModelChoice.cli_id` (`launch_id`), threaded
  through `run_route`; Codex corrected to `codex exec` (flag order still
  unverified — Codex absent).
- **Offline receipt** (`evals/orchestrator-cost-routing-2026-07-24.md`): the
  deterministic cost model, stated against multiple baselines honestly — routing
  is ~79% under running the whole task on Opus, but **≈break-even vs a flat
  Sonnet run** (the Opus plan node cancels the bulk savings). The mechanism is a
  quality allocator (flagship money only on planning), not a dollar-saver against
  a sane baseline. The full live billed A/B remains TO-BUILD.
- Tests: `tests/test_hosts.py` (capability tiers, `pick_model` gating + prefer,
  cheapest-coordinator), `tests/test_orchestrator.py` (route-IR validation —
  cycles/unknown-deps/budget/node-cap, topological waves, deterministic priced
  plan, coordinator JSON parse, and the closed loop — parallel handoff, failure
  escalation, dependent-skip, bounded re-plan).
- **MCP tool description now documents all 14 ops** (`mcp.py`): the `op` enum
  declared 14 operations while the prose catalogue in the tool description
  listed 9 — `callers`, `callees`, `impact`, `diff` and `investigate` were
  callable but undiscoverable to a model reading the tool definition. Each now
  carries a gloss alongside the existing nine, and
  `test_tool_description_documents_every_enum_op` asserts every enum member is
  described, so the catalogue cannot drift from the enum again.
  **Cache impact:** the tool description is a prefix-resident asset, so
  `PREFIX_VERSION` moves 5 → 6 and every user pays one cold prompt-cache write
  per model on first use after upgrading.

## [0.30.0] - 2026-07-21

Building the toolchains that were "not available" — tree-sitter and SCIP.

- **M-K4 · SCIP ingestion, shipped** (`scip_ingest.py`, `_vendor/scip_pb2`):
  an opportunistic `index.scip` reader adds **precise, compiler-backed
  references** at the top of the refs engine ladder (**SCIP (exact) →
  jedi → ast**), disclosed per node (`ctx refs` / `code.refs` show `engine
  scip (exact)`). `find_index` reads `index.scip` at the workspace root or
  `$CTX_SCIP_INDEX`; the index is only read, never generated. The protobuf
  runtime is the `[scip]` extra; the SCIP bindings are vendored
  (`src/ctx/_vendor/scip_pb2.py` generated from the committed `scip.proto`);
  either absent → the ingester degrades to None and the ladder falls
  through — absence costs nothing. `resolve_refs` is now the single ladder
  used by `ctx refs`, `ctx q refs`, and the `code.refs` op.
  - **Precision receipt** (`evals/scip_precision.py` + `.md`): on an
    ambiguity fixture (a name also in a comment, a string, and a shadowing
    local), SCIP scores 100% precision / 0 false positives vs the textual
    rung's 50% / 4 false positives. Tested with a committed real
    `index.scip` (`tests/fixtures/scip_sample.scip`, from `scip-python`),
    so CI needs only protobuf, not the indexer.
- **Tree-sitter grammar-wheel backend** (`skeleton.py`): the skeleton
  tier's tree-sitter extractor gains a third, offline-safe path —
  individual `tree_sitter_<lang>` grammar wheels via the modern core API
  (the bundle `tree-sitter-language-pack` fetches parsers at runtime, a
  sandbox 403). It carries a JS/TS skeleton that stdlib `ast` cannot parse
  and ctags need not. The `[code]` extra now pins the grammar wheels
  (`tree-sitter-python/javascript/typescript`) instead of the unreliable
  language-pack.
- CI `full` job installs `.[dev,map,fast,code,scip]` so both new backends
  are exercised, not just skipped. Suite: 994 passed (venv with all
  extras); tests skip-if-absent so the minimal job stays green.

## [0.29.0] - 2026-07-20

Finishing the designed-not-built bucket (M-K/M-L), with receipts.

- **`ctx ask` intent family completed** (`ask.py`, `plan_ops.py`, `cli.py`):
  four new intents join locate/impact/diagnose. `trace` (structural call
  path — refs → callers → callees → transitive reach) and `compare`
  (behavioral run-diff via the new `evidence.diff` plan op) are observe-
  class. `verify` (changes → related tests → run the suite) and `review`
  (changes → symbols → tests → run → root-cause join + counterevidence)
  are **execute-class**: CLI runs them, the bounded MCP tier rejects
  `test.run` (`execute_on_observe_tier`), and each intent discloses its
  class. New `--against`/`--command` flags; compare/verify slots teach when
  missing. All seven compile deterministically to `ctx.plan/v1`.
- **M-K3 `records_opportunity` ledger** (`hook.py`): a jq / `sort|uniq -c`
  / awk-projection pipeline is detected, taught the `ctx q records`
  collapse, and recorded to `.ctx-session-reads/records-adoption.jsonl` —
  the demand denominator. (The jq physical compile target stays deferred:
  pure speed, no capability gain.)
- **M-K5 comby decline-corpus gate** (`plan_ops.py`): `ast.rewrite.preview`
  now records `comby_candidate` entries (engine absent, or no structural
  match) to `.ctx-session-reads/rewrite-declines.jsonl`. Instrumentation
  ONLY — the comby rung stays unbuilt until this corpus shows real demand.
- **M-K4 SCIP ingestion: deferred, with reason** — no SCIP toolchain or
  protobuf in this environment to produce a real `.scip` test fixture, so
  building an untested ingester is the speculative code the project
  refuses. Recorded in docs/SUBSTRATE.md.
- **Evals**: M-K2 scoped-scan receipt (`evals/corpus_scoped_scan.py` +
  receipt) — corpus reduces the eligible set 178→9 files (94.9%), a 13.1×
  ast-grep wall speedup even on the fast engine (the slow Semgrep arm is
  declared, not run — Semgrep absent here). Plus a Sonnet addendum to the
  3-arm diagnosis receipt: a stronger model adopts `ctx ask` once the card
  is in context (as haiku did), but on a no-flood task adoption still
  costs turns — the A/B/C payoff referee needs a flood-bearing task.
- Skill/AGENTS teach all seven intents (skill BODY change — invocation-
  loaded, no prefix-cache cost; manifest regenerated at PREFIX_VERSION 4).

## [0.28.0] - 2026-07-20

The skill catches up to the engines, plus a measured three-arm receipt.

- **Skill vocabulary refresh** (`plugins/antigravity/skills/ctx-harness/`,
  Codex `AGENTS.md` block): `SKILL.md` and `references/verbs.md` stopped at
  the pre-M-J `run/search/get/stats` vocabulary. They now teach `ctx ask`
  (intents locate/impact/diagnose), `ctx q` (the composition algebra incl.
  `corpus`/`records`/`distinct`/`histogram`), and `ctx plan run`/`plan`.
  **PREFIX_VERSION 3 → 4**: the skill body/frontmatter are prefix-resident,
  so this is a one-time full-prefix cache rewrite per user (the injected-
  prefix stability contract; `prefixassets.py` manifest regenerated).
- **Claude Code teaching surface** (`installer.py`): `install_claude` now
  upserts a compact ctx verb card into the workspace `CLAUDE.md` (marker-
  delimited, idempotent, mirroring the Codex `AGENTS.md` block). Measured
  gap — the shipped verbs had no teaching surface on Claude Code, so agents
  never invoked them (see the receipt below); with the card in context,
  they do.
- **Three-arm diagnosis receipt** (`evals/ask_diagnose_3arm.py`,
  `evals/ask-diagnose-3arm-2026-07-20.md`): real coding agents (Haiku),
  naive vs Headroom vs straitjacket vs straitjacket+card, on a seeded
  single-bug diagnosis with a model-free grader. Findings: on a no-flood
  task all arms solve it and containment is bounded overhead (the expected
  low-complexity regime); and the vocabulary is adopted only when it
  reaches the agent (0 `ctx ask`/`ctx q` bare; both invoked once the card
  is in `CLAUDE.md`). Reusable 3/4-arm harness with a transcript-derived
  adoption counter.

## [0.27.0] - 2026-07-20

The `ctx ask` wave (ROADMAP M-L, docs/ASK.md): a repository question
compiles into a typed intent preset — a frozen `ctx.plan/v1` template
with typed slots — executed on the shipped plan tier. Collapses the
*decision cost* of exploration (which verbs, in what order) the way M-J
collapsed its *turn cost*. The adopted core of an external retrieval
proposal, audited: the natural-language parser, `reveal`/`audit` verbs,
the whole-surface rebrand, and the entity/relation ontology were cut;
what shipped is the elegant, testable spine.

- **Phase 0 · thin observe ops** (`plan_ops.py`):
  - `evidence.failures` — failure census from CAPTURED facts, never a
    rerun. Freshness against the current generation is computed and
    DECLARED: stale facts carry `fresh: false` + a note proposing (never
    running) a refresh — the observe invariant made legible, using the
    same `generation_hash` semantics as the rest of the system.
  - `code.symbols` — structured symbol rows (identity · kind · range ·
    span) from skeleton-derived facts; census before detail, no outline
    text. An input warms facts for exactly those files (content-keyed).
  - `code.context` — terminal bounded materialization (sites get
    line±context, symbols their clamped range); emits `text`, the
    refinement boundary at the plan tier.
- **Phase 1 · intents + `ctx ask`** (`ask.py`, `cli.py`): `locate`,
  `impact`, `diagnose` as deterministic slot→`ctx.plan/v1` presets
  (`json.dumps(sort_keys=True)` ⇒ stable plan id ⇒ stable node-cache
  keys). **No natural-language parser**: `--intent` is a flag; the
  subject is `--symbol` or the question's sole identifier-shaped token
  (dotted/snake/CamelCase — capitalized English is skipped), inferred
  only when unambiguous and always disclosed. A missing/ambiguous slot
  is a teaching error that SUGGESTS an intent and never guesses-and-runs.
  The interpretation (`intent:`/`subject:`) rides above the digest, never
  behind `--trace`. `ctx ask "q" --intent <i> [--symbol X] [--run r]
  [--depth N] [--plan]`.
- Every intent is observe-class end to end (diagnose reads captured
  failures, never reruns); counterevidence is a structural join node
  (rendered even when empty); the only text-emitting node is
  `code.context` (bytes materialize once, terminally — the closure law).
- Verified end to end: on a seeded regression (`raise` in a changed
  function, its failing run captured), `ctx ask --intent diagnose` names
  the culprit symbol with plane attribution in one digest, no rerun.
- Tests: `test_ask.py` (compiler determinism, teaching-not-guessing,
  no-rerun invariant at compile time and end to end, freshness
  declaration, terminal materialization). Suite 968 passed / 0 failed.

## [0.26.0] - 2026-07-20

The substrate wave (ROADMAP M-K, docs/SUBSTRATE.md): the operator classes
beneath the semantic layers, from the audited external "evidence algebra"
proposal. Phases K1–K3 + K5.3 shipped; K4 (SCIP) and K5 (comby, gated on a
decline corpus) remain designed; K6 (watch warming) waits for the broker.

- **M-K1 · span-precise sites** (`rg_engine.py`, `search.py`, `query.py`):
  search results carry 1-based half-open `[col_a, col_b)` character
  columns — captured from the rg `--json` submatches already on the wire,
  and from `finditer` spans in the Python engine (leftmost match per line,
  parity by construction; pattern-index recovery is span-anchored, the
  whole-line re-match demoted to labeled fallback). Every `ctx search`
  emission now mints a `ctx.search/v1` result blob (`result: blob:<id>`)
  so a search is citable as one handle — engine parity extends to
  byte-identical blobs.
- **M-K2 · the file-set algebra** (`filesets.py`, new): the missing
  `file_select` operator class. `ctx q 'corpus [--ext E]… [--glob G]…
  [--exclude G]… [--changed] [--max N]'` and the `repo.files` plan op
  emit a bounded eligible file set with a coverage receipt (`considered ·
  selected · engine [· gen]`) that survives combinators and rides the
  minted payload. Engine ladder git ls-files → **fd** (opportunistic, run
  `--no-ignore` so `ws.is_ignored` stays the single ignore authority —
  listings byte-identical across engines by construction;
  `CTX_FILES_ENGINE` kill-switch) → os.walk. `--changed` binds to the
  generation snapshot, never mtime (SUBSTRATE §2.4). Scoping
  `semantic.*` to a `repo.files` result confines the engine to the
  selected set — *select files before scanning*.
- **M-K3 · the records algebra** (`query.py`): `records <run:|blob:>
  [--jsonl] [--pointer /p]` opens stored JSON/JSONL artifacts (compiler
  output, test JSON, SARIF, lockfiles) as the `records` kind, where the
  shipped combinators plus new total stages `distinct <field>` and
  `histogram <field> [--buckets N]` (numeric buckets or categorical
  census, capped with declared omission) absorb the jq class without
  importing the jq language. All four new stages carry derived closure
  classes; the digest-closure pins extend to them.
- **M-K5.3 · text-tool steering** (`hook.py`): `sed`/`awk`-family
  commands leave the unknown-command limbo. Read-only invocations steer
  into bounded `ctx run` capture like grep/find; **in-place** invocations
  (`sed -i`, `gawk -i inplace` — detected in plain argv and inside
  compound expressions, which is where every `{…}` awk program lands)
  force_ask with a preview-first remediation and are never auto-rewritten
  into a capture that would still mutate files.
- Tests: `test_filesets.py` (engine parity incl. fd skip-if-absent,
  generation-bound `--changed`, receipts), `test_substrate.py` (span
  blobs, records/distinct/histogram, totality), closure pins, rg/python
  column-parity extension, sed/awk steering cases.
- **Word-anchored pytest detection** (`pytestprof.py`, `facts.py`): the
  profile claim and the facts-tier family detection matched `"pytest"`
  as a raw substring of the joined argv, so a command whose INTERPRETER
  lives under a pytest-named directory (uv tool shims:
  `…/tools/pytest/bin/python -c …`) or whose args carry pytest-named
  paths (`/tmp/pytest-of-root/…`) was misclaimed as a test run — the
  replay doctrine's "a file containing test markers is not a test run",
  violated at birth. Detection is now word-anchored (program basename or
  `-m` module target; never an interior path component), shared via
  `argv_invokes_pytest`, and regression-pinned.
- **Environment-robust fixtures**: three fixtures invoked a bare
  `python3 -m pytest` (the one interpreter NOT guaranteed to carry
  pytest) — `test_plan_exec`'s diagnosis plan, `evals/plan_collapse.py`'s
  plan arm (its other two arms already used `sys.executable`), and
  `test_reflex`'s ground-truth run — all now `sys.executable`. The
  scaffold-slim overhead budget in `test_lint_and_gain` is now relative
  to the rendered command line (a venv-deep interpreter path must not
  fail a fixed byte budget). Full suite green under both a clean venv
  and a uv-tool pytest shim.

## [0.25.0] - 2026-07-19

The compiled-evidence-plans wave (ROADMAP M-J, docs/EVIDENCE-PLANS.md):
repository exploration moves from an LLM-mediated control loop toward one
model round per hypothesis epoch — the model compiles a typed, total,
bounded DAG of evidence operations; the harness validates, prices, and
executes it locally; one causally organized digest returns.

- **`ctx.plan/v1` IR** (`plan_ir.py`): model-authored JSON DAG, statically
  validated (cycle-free by construction — edges reference earlier steps
  only; ≤24 nodes; mandatory foreach caps ≤64; `when` guard micro-grammar;
  closed rejection vocabulary) and priced before execution
  (`ctx plan validate|price`, the PRICED-CONTEXT idiom).
- **22 logical operators** (`plan_ops.py`) over shipped machinery: the q
  stage registry (`code.search/refs/callers/callees/impact`, combinators,
  `q.pipe`), facts Angle-lite joins (`evidence.join` — the root-cause join
  `failing_in_changed`, counterevidence via `untouched_failures`),
  skeleton outlines, `repo.changed` (now deriving decl facts for changed
  files, upgrading the join to symbol precision), `test.run` (birth-gate
  capture + failing census + `run:` handle). Ops declare capability class
  (observe|execute), cost class, and engine requirements.
- **Executor + `investigate/v1` digest** (`plan_exec.py`): plan-order
  execution (deterministic bytes by construction), per-node
  `ctx.plan-node/v1` blobs, typed skip declarations (guards, engine
  absences, error cascades, wall-budget exhaustion — the digest always
  renders), `ctx.investigation/v1` manifests, ranked conclusion candidates
  with plane attribution (dynamic/temporal/static/semantic), REQUIRED
  counterevidence (empty form declared), coverage attestation with
  per-node engine disclosure, contract-checked at the selection seam
  (`contracts/investigate.toml`). Expensive external-engine scans are
  node-cached on a content-sensitive workspace fingerprint.
- **ast-grep tier** (`astgrep.py`, opportunistic binary): structural
  `ast.search` with span-shaped sorted matches; degraded tier is a
  metavariable-anchored regex honestly labeled `textual`. Probe rejects
  shadow-utils `sg`. `ast.rewrite.preview` mints the full patch as an
  addressable blob; `apply` is transactional (`git apply`) and refuses on
  generation drift. No lossy fallback for rewrites, by design.
- **Semgrep tier** (`semgrep_engine.py`, `[sem]` extra): hermetic by
  construction (local rules confined to the workspace, `--metrics=off`,
  no version check, no registry fetch); findings normalized/sorted into
  typed rows with dataflow-trace frames; absence is a declared skip.
- **EvidenceGraph v2 relations** (additive): typed `(from, relation, to)`
  triples from a closed vocabulary; a graph without relations serializes
  byte-identically to v1, so every pinned golden and cache key holds.
- **CLI + MCP**: `ctx plan validate|price|run|ops`, `ctx plan run`
  (epochal control: replans beyond the `[plan]` allowance get a declared
  banner + reflex-plane ledger event, never a block). MCP op
  `investigate` accepts observe-class plans only; execute-class ops are
  typed rejections at tier=mcp (SPEC §10.4 preserved; tool description
  bytes unchanged — no prefix-version bump).
- Declared debt e319eef641: physical operator selection is
  availability-based (the shipped `_select_engine` idiom); the
  telemetry-compiled `[plan_engines]` cost-table epoch (EVIDENCE-PLANS
  P4) lands once plan-node telemetry accumulates.
- 43 new acceptance tests (IR totality, end-to-end diagnosis, byte
  determinism, addressability, tier enforcement, fake-binary engine
  contracts, generation-guarded apply); full suite 757 passed on both the
  full and minimal (no-binaries) matrices.

Second batch, same wave:

- **ast-grep-py library rung**: `ast.search` now degrades through three
  disclosed tiers — ast-grep binary (structural) → `ast-grep-py` library
  (structural, in-process, added to the `[code]` extra) → labeled
  metavariable-anchored regex. `engine_id()` precedence feeds node cache
  keys; rewrites stay binary-only by design.
- **Measured evidence** (`evals/plan-collapse-2026-07-19.md`, runnable
  `evals/plan_collapse.py`, CI-guarded): on a seeded auth-regression
  diagnosis, boundary crossings collapse 6 (naive) → 4 (harnessed) → 1
  (plan); append-only resend cost 1,704 → 1,336 → **189 tok** (9.0× under
  naive, 7.1× under harnessed-interactive); the plan digest body is
  byte-identical across re-runs (cache-aligned) where naive pytest output
  carries a volatile wall-clock token. Headroom comparison cited from
  prior measurements and explicitly labeled derived, not head-to-head.
- **Skill progressive disclosure**: plan authoring ships as
  `references/evidence-plans.md` (loaded on demand only); the SKILL.md
  body gains a one-line pointer; frontmatter untouched, prefix manifest
  regenerated without a PREFIX_VERSION bump — zero always-in-prompt
  footprint growth.
- **Fix**: repo search no longer scans the `.ctx-session-reads/` ledger
  (both rg and python engines) — the ledger is bookkeeping, never
  evidence, and it grows as the harness runs, so scanning it made
  identical searches non-byte-identical (found by the plan-collapse
  cache-stability probe).

## [0.24.0] - 2026-07-19

The coverage-corpus wave: rtk's breadth question answered the house way —
real corpora measured before any profile was built, hypotheses killed on
the record (evals/coverage-corpus-2026-07-19.md).

- **`evals/coverage_corpus.py`** — the rtk-corpus method made re-runnable:
  every corpus (live toolchain capture or labeled fixture replay) goes
  through a stub binary carrying the real tool's name, so `ctx run`
  exercises true argv-anchored detection, shape dispatch, slim inline, and
  budgets; emits the raw/digest/ratio/profile table per corpus.
- **`cargotest/v1`** (SPEC §9 Cargo row): exact suite-aggregated census,
  one line per failing test with coordinates, first panic location+message
  inlined; detection anchored on the libtest `test result:` shape so
  compile-error runs fall through to lint/build. Measured: 150-test crate
  with 6 failures went from "names one failure" (text/v1, 117 tok) to the
  full failing census (203 tok).
- **`table/v1`** (SPEC §9 tabular row): shape-detected caps-header aligned
  tables (docker/podman ps, kubectl/oc get, MCP-delivered tables); exact
  row×column count, low-cardinality column value censuses, minority rows
  cited verbatim with coordinates. Measured: 180-pod `kubectl get pods`
  under text/v1 hid 13 of 14 broken pods in the omitted middle; table/v1
  names the exact state distribution at equal budget — tabular needle-drop
  100% → 0%.
- **Killed by measurement** (reasons in the eval): mvn/gradle profile
  (logtemplate/v1 already surfaces every failure via rarity), AWS parsers
  (json/v1 shape census, 150.9×), pip/gh listing profiles (slim inline
  correct at ~1.0×), ps aux (no census worth its tokens).
- **straitjacket-bench charter** (evals/BENCHMARK.md): the paired-corpus
  benchmark design adopted from external review — retrieval quality
  (SWE-Explore, pending dataset verification), downstream correctness
  (SWE-bench Verified subset), hostile-output stress (Terminal-Bench
  slice), and SJ-EvidenceBench invariant adversaries; metrics (evidence
  density, retrieval regret, evidence preservation as the load-bearing
  gate), pathology-stratified sampling, and four evaluation tiers mapped
  to existing infrastructure. Inventory verified: 8 of 10 EvidenceBench
  scenarios already existed as tests; the two gaps shipped
  (tests/test_evidencebench.py, `sj_canary` marker): machine-format
  negotiation baselines (JSON/JSONL/SARIF claimed structurally, JUnit XML
  bounded+deterministic fallthrough) and stdout/stderr descriptor-graph
  classification — whose first probe caught and fixed a real defect:
  `cmd 2>&1 > file` was classified proven-small although POSIX sends
  stderr to the console (hook `_REDIR_ALL_RE` now order-aware).
- **`ctx replay`** (ROADMAP M-F, session-history learning loop): replay
  recorded Claude Code transcripts through the real steering + digest
  code, open-loop and workspace-free — interception verdicts, wire
  residency recorded-vs-simulated, evidence sufficiency (downstream-used
  facts scored inline vs one-hop), and `--gaps` (the empirical coverage
  priority list mined from real sessions). Read-only by construction;
  read results counted under the read path, never shape-digested.
  Measured: the naive dev session replays at 46% residency saved; spec3
  harnessed archives replay at zero delta with 71/71 and 21/21
  downstream-used facts inline (figures regenerated after review fixes:
  already-harnessed digests are fact-scored — they ARE the regression
  surface — and read-path results are excluded). Pathway mining receipts:
  evals/pathways-spec3-2026-07-19.md (70% of commands are pytest; 15
  starvations, zero retrieval-verb adoption — command-channel
  continuations filed as the fix).

## [0.22.0] - 2026-07-19

The Evidence Delivery Controller wave (docs/EDC.md, all 24 sections
specified and adversarially reviewed before build — seven defects died on
paper). Built by seven parallel engineers in two increments; 612 tests.

- **Evidence core**: typed EvidenceGraph/Item/Ref with volatile quarantine
  and coverage attestation (ctx.evidence); TOML Evidence Contracts with
  loss severities and floor<=ceiling load validation (ctx.contracts);
  selection-seam validation — coverage computed over typed facts, never
  re-parsed text.
- **pytest/v2 extract/render split** (the layering law made real):
  extraction emits attested graphs (failure class + one-line summary per
  census row now DEFAULT — hierarchy levels 3-4); rendering through
  contracts: FAIL_CENSUS, DENSE (grouped under extracted keys only),
  FLOOD (histograms + first-N census + complete census minted as a
  derived blob: artifact); degradation cascade never truncates identities
  outside declared FLOOD; pass path byte-identical pytest/v1.
- **Delivery Policy Resolver** (ctx.resolver): the single choke point
  replacing seven hand-rolled budget sites; DeliveryPlan with plan_id and
  closed reason vocabulary; floor applied after multipliers; reader
  capability with latching and confidence floor; plan receipts to
  telemetry. Safety invariant test: guard decisions byte-identical under
  every adaptive state.
- **Controller state, shadow-first** (EDC 5-7+6b): source generations
  with untracked-content hashing (ledger-dir excluded, capped,
  deterministic); per-family signature tables closing the scope-flag
  defect; narrowing relation + positives; v2 intervention/outcome ledger
  with deterministic ids, hypothesis windows, censored expiry; shadow
  circuit machine (episode semantics + hysteresis); graduated-steering
  shadow ledger. Replay gates vs archived transcripts ALL PASS: the r1
  8x slicer loop collapses to one episode/one transition; edit cadence
  scores as verification; 7 narrowing positives.
- **Seeded referee + scorecard v2**: spec3 --repeats/--gates with median
  aggregation and frozen-constants checksum; per-family behavioral
  blocks, coverage tables, episode narratives, formula-labeled
  counterfactuals; censored events excluded from denominators.
- **Perf with receipts** (rejected optimizations documented): resolve_id
  ~500x (index-seekable range scan), line-index repeat access ~3600x
  (in-process cache; the mmap "win" was this confound), gc 3.7x
  (batched); retrieval.py modularized into ctx/_retrieval behind a
  byte-compatible facade; MCP schema drift fixed (call-graph ops
  declared, diff wired) + bounded workspace cache.

## [0.21.0] - 2026-07-18

The reflex wave: closed-loop conditionality (docs/REFLEX.md), built
against the spec3 receipt where every conditional fired to spec on the
flood axis while the failure lived on the uninstrumented information
axis. Every intervention is now a hypothesis about the model's next
action; the system scores the hypothesis per event and adapts on the axis
the evidence names.

- **`pytest/v1` failing-test census** (debt 74db82e027): one line per
  failing test — node id, output coordinates, traceback span — rendered
  above and outliving the inline first-failure detail under budget
  pressure; overflow declared with a continuation span. Dense mode adds
  one evidence line per test. Bare `-q` summaries, `--tb=line/no`, and
  pipe-truncated output all parse (the spec3 "summary line not found"
  breakage fixed); all-pass runs byte-identical to before.
- **Reflex arc v1** (`ctx.reflex`, hook + cli wiring): slicer-normalized
  command signatures (`pytest -v`, `… | head -100`, `… --tb=short | tail`
  → one signature); starvation detector — a signature re-issued after its
  digest-with-omissions appends an outcome event and latches densify for
  the session; landing detector on `ctx get`/`search` of known handles.
  Reflexes act through rendering only (`densified: re-run detected` header
  on the printed digest; dense flag never in digest meta — content
  identity stays a pure function of bytes). All state fail-open,
  replay-deterministic from the command sequence. Outcome ledger:
  `.ctx-session-reads/reflex-outcomes.jsonl` (frozen schema).
- **Behavioral-anomalies scorecard**: `ctx stats --session` renders
  starvation/landing/densify counts per signature when present — the
  single-session instrument that would have caught spec3 without a
  benchmark. Summary line flags `⚠ N starvation/M landings`.
- **Slow-loop epoch schema**: `ctx policy compile` aggregates reflex
  outcomes into `[digest_density]` — signatures with ≥2 starvations and
  landings < starvations start dense in future epochs; address-following
  readers keep lean digests. Additive to ctx.policy/v1 (hook parser
  verified tolerant); consumption deliberately deferred.

## [0.20.0] - 2026-07-18

The measurement-driven wave: three mechanisms built in parallel by
independent engineers against the receipts of the eval-collapse
measurements (evals/eval-collapse-2026-07-18.md) and the conditionality
audit (docs/LADDERS.md), then assembled with the audit's consistency fixes.

- **Head/tail evidence windows** (`digest/text.py`): large text/v1 digests
  now show the first `digest_head_lines` AND last `digest_tail_lines`
  lines (both configurable via `[budgets]` in ctx.toml, default 5/5), each
  with real coordinates; the omitted middle carries a deterministic region
  span plus a `ctx get --lines` continuation. Motivated by a measured
  failure: CLIs put conclusions at the END of output, and the S-C flood
  scenario's own SUMMARY line was being omitted. Budget fitting shrinks
  tail first, then head; small-output and error-signal paths byte-identical
  to before.
- **Long-runner backgrounding** (`jobs.py`, `run --bg`/`--bg-after T`,
  `job`, `jobs`): every `--bg*` run starts under a detached supervisor
  spooling to the store; finish within T → the normal digest, byte-for-byte
  identical to a foreground run including the same `run:` id. Outlive T →
  the transcript gets `job:<id>` immediately; `ctx job <id>` shows a
  bounded live tail (never a flood), `--wait` blocks then digests,
  `--kill` finalizes what spooled. Finalized jobs are ordinary `run:`
  artifacts — search/get address them identically; job ids, pids, and
  timestamps never enter content identity. Six launch/kill/finalize races
  identified and closed (single-writer meta, idempotent finalization,
  orphan adoption).
- **Adoption steering** (hook + skill, shipped mid-wave as its own commit):
  eval-opportunity detection (python heredoc/-c) appends the collapse
  teaching to remediations at every friction point and ledgers each
  opportunity fail-open (`.ctx-session-reads/eval-adoption.jsonl`) — the
  adoption ratio's denominator. Doctrine scoping fix: terseness governs
  scripts and narration, never the final deliverable.
- **Conditionality audit applied** (docs/LADDERS.md): seq emissions now
  respect the engagement filter like run/eval (edge 1); timeouts and
  signal deaths get the failure budget in `run` (edge 4, parity with
  eval); seq marks signal-death steps as failures (S6 finding). Remaining
  audit items (pressure-aware budgets via a single resolve_budget choke
  point, hint follow-through telemetry, MCP schema drift) are the next
  wave's candidates, ranked in the doc.
- Skill: verb index + rule 15 (never idle on a long runner) + long-runners
  reference section. Prefix manifest regenerated; PREFIX_VERSION unchanged
  (invocation-tier assets only — no cache impact).

## [0.19.0] - 2026-07-18

Programmable capture: the Maki absorption. Maki (maki.sh) demonstrated the
strongest form of tool-chain collapse — the model writes one script that
chains N operations, and intermediates never enter the transcript (their
demo: 1300× context reduction). `ctx seq` already performed this collapse
for *declared* trees; this wave generalizes it to *computed* control flow
(branch on a result, loop over files, aggregate before emitting) while
keeping what a raw interpreter sandbox drops: provenance. Maki's script and
its intermediates vanish into the chat log with no address; here every
piece keeps one.

- **`ctx py`** (`ctx.pyeval`): a Python script runs under birth-gate
  capture and only its bounded digest returns. The script is stored first
  as a content-addressed blob, cited in the digest header
  (`script blob:<id>`) and in the final manifest (`eval.script`) —
  reproduce with `ctx get blob:<id> | python3 -I -`. Streams are the usual
  span-addressable blobs; the existing profile registry digests the output
  (flood → bounded digest with continuation coordinates; small result →
  complete inline). Failure asymmetry applies: a failing script's
  traceback rides on the failure budget, and frames are deterministic and
  path-free (`File "<stdin>"` — the script feeds stdin, never a temp
  file). `python -I` isolated mode blocks cwd/PYTHONPATH injection.
  Sub-steps that deserve their own handles call `ctx run` from inside the
  script. Trust envelope identical to `ctx run` (bounded capture, not OS
  isolation — that remains the broker's job, Phase 3). Deterministic:
  identical script + identical worktree → byte-identical digest.
- **Capture runner**: `run_capture` gains `stdin_bytes` (spooled to disk
  and fed as the child's stdin — never a pipe, so no deadlock and no size
  limit) and `record_argv` (normalized model-visible argv, so the
  host-specific interpreter path never appears in manifests or digests).
- **Telemetry attribution**: `render_run_digest` takes an `op` name so
  `ctx gain` reports eval under its own by-verb row; `op` never
  participates in digest bytes or content identity.
- Skill body rule 14 + verb index teach the seq/eval split (declared →
  `seq`, computed → `eval`); prefix manifest regenerated — the skill body
  is invocation-tier, so PREFIX_VERSION stays 3 and there is **no cache
  impact**.
- **Eval set + first measurements** (`evals/evalset_collapse.py`,
  `evals/ab_eval_live.py`, results in `evals/eval-collapse-2026-07-18.md`,
  smoke-guarded by `tests/test_evalset_collapse.py`): mechanical arms on
  real fixtures (fan-out aggregate 146 tok vs 96k naive with the
  best-play baseline provably unable to finish; bash-pipeline control
  showing `run --shell` already covers stream-shaped chains; flood/needle
  provenance net; 299-tok wrong-script recovery vs 192k re-pay) plus a
  live mechanism-isolated A/B (haiku, n=2) and a wrapped condition. Live
  findings recorded honestly: the one-script discipline wins (−15–63%
  cost, fewer turns, −79% cache churn at best) but the verb itself went
  unadopted (0/3 sessions) and the terse doctrine leaked into final
  deliverables — both filed in the debt ledger with coordinates.

## [0.18.0] - 2026-07-18

The universal emission gate: one output-side gate for every faucet. Prior
waves plugged faucets one tool at a time (Bash wrapped, Read/Grep/Glob
input-bounded) — a per-tool if-ladder that never terminates. This wave
replaces it with a single PostToolUse gate that dispatches on output *shape*,
not tool name: a new tool needs no new code. Motivated by measurement — a
routine `mcp__github__list_commits(perPage=100)` returns ~79 KB / ~19.8k
tokens and is re-sent every turn; its `json/v1` digest is ~0.4–1.4 KB
(≈57–190×), and the full payload stays retrievable.

- **Universal PostToolUse gate** (`ctx.hook._emission_gate`, claude-code):
  any tool result over `budgets.max_tool_output_bytes` (default 16384) is
  replaced — via the documented `hookSpecificOutput.updatedToolOutput` — with
  a bounded deterministic digest carrying a working `ctx get run:<short>`
  ref. Under budget → byte-identical no-op. The raw bytes are persisted
  losslessly first (lossy-in-window, lossless-on-disk); nothing the model
  needed is ever destroyed, only relocated to an addressable artifact.
- **Shape-dispatched, name-agnostic**: the gate synthesizes `argv=[tool_name]`
  and reuses the existing digest registry (`digest.digest_output`), so MCP
  JSON lands on `json/v1`, grep-shaped output on `search/v1`, prose on
  `text/v1` — no per-tool branches. Idempotent (never re-digests its own
  output or `ctx`'s), fail-open (any error → pass-through), deterministic
  (content-addressed id is a pure function of bytes + tool name).
- **`json/v1` head-N record inlining**: a shape line alone forced a re-fetch;
  the digest now inlines the first records' scalar fields + a json-pointer
  span to the rest (mirrors `search/v1`'s top-matches+span). Byte-stable.
- **`search/v1`** now recognizes a synthesized `argv=[tool_name]` (native
  `Grep`, mcp `*search_code` / `*grep*`) so those faucets reach it through
  the gate; narrow suffix/exact match preserves the log-line theft guard.
- **Matchers broadened** to every emitting faucet — Claude Code PostToolUse
  `Bash|Read|Grep|Glob|WebFetch|WebSearch|Task|mcp__.*` (Edit/Write/Todo
  excluded as tiny), Antigravity nudge-path likewise. Antigravity stays
  nudge-only (output-replacement contract unverified upstream). Matcher
  strings are host settings, not prefix assets → no `PREFIX_VERSION` bump.
- Removed the now-unwired `_post_hook_exe` native-shim selector: the gate
  needs the Store/digest layer, so PostToolUse runs in Python. A shim that
  measures bytes and re-execs only over budget is a possible follow-up.

## [0.17.0] - 2026-07-18

The native-search wave: close the model-ignoring gap. Measurement showed
the model navigates with the *native* `Grep`/`Glob` tools — not shell
`grep` — so our `Bash|Read` matcher never saw the flood, and the
navigation governor never fired. This wave intercepts the tools the model
actually reaches for.

- Matcher extended to `Bash|Read|Grep|Glob` (Claude Code) and
  `…|grep_search|glob_search|codebase_search` (Antigravity). The tools the
  model uses to navigate are now in scope, not just shell commands.
- Native content-mode `Grep` with no `head_limit` gets one injected
  transparently via `updatedInput` (`head_limit: 60`) — the tool still
  runs, the model adopts nothing, and an unbounded flood becomes a bounded
  slice with a pointer to the structured digest. `files_with_matches` /
  `count` / already-bounded greps pass through raw. Under strict
  `steering = "deny"` the same case is redirected to `ctx run -- grep`
  instead (never silently rewritten).
- `search/v1` digest profile: a wrapped `grep`/`rg` (via `ctx run`) is now
  rendered as *search results* — exact match count, per-file histogram,
  top hits with coordinates, and a span to the full set — instead of the
  generic text profile's byte counts. Sibling of `lint/v1`; the two share
  the `file:line:content` shape, so `search/v1` is argv-anchored to actual
  `grep`/`rg`/`ack`/`ag` invocations (a content-ratio trigger was tried and
  dropped — it stole log and lint lines) and ordered *after* `lint/v1` so
  diagnostics claim their own output first.
- No prefix asset changed (matcher strings are host-settings, not
  resident prompts), so no `PREFIX_VERSION` bump: zero cold-cache cost.

## [0.16.0] - 2026-07-18

The call-graph wave: edges, done in-doctrine. We had nodes (`def`/`refs`);
this adds the edges that turn a recursive grep-and-read trace into one
query — the one capability that makes tokensave enviable, built the
straitjacket way (pure stdlib `ast`, zero new deps, deterministic,
worktree-hash cached, no daemon, span-backed, addressable).

- `ctx callers <Symbol>` — direct callers, each with file:line.
- `ctx callees <Symbol>` — in-repo functions it calls.
- `ctx impact <Symbol> [--depth N]` — transitive callers (blast radius),
  grouped by hop distance; bounded recursion (≤6). "What breaks if I change
  this?" in one call. On our own repo, `ctx impact register_span` returns
  the full 179-node reachable set in ~0.8s (cached thereafter).
- Name-resolved edges (a call to `foo` binds to any in-repo `def foo`):
  approximate but disclosed like the ctags map engine; ambiguous names
  report every candidate, never hidden (SPEC §8). Python-only for now;
  tree-sitter breadth deferred to an optional `[polyglot]` extra pending a
  measured win.
- Ships CLI-first + skill-taught (bump-free). The MCP `op` enum is a prefix
  asset, so exposing the verbs there is a deliberate future PREFIX_VERSION
  decision, not paid on spec (same discipline as the v0.9.0 priced outline).

## [0.15.0] - 2026-07-18

The cross-validation wave: two dual-use benchmark cells (S5 library-hunt,
S6 bug-bash) whose output is repo work — held-out by construction, novel
regimes, findings adversarially re-verified by hand before harvest
(evals/cross-validation-2026-07-18.md).

- **6 real defects found and fixed** (of 15 S6 claims; verification
  refuted 1 and deferred 8 to `ctx debt`). All regression-tested in
  tests/test_bugbash_s6.py:
  - compound-command bypass: `allow_commands=["echo"]` let
    `echo hi && rm -rf x` through — prefix allows now gated on `not
    has_meta`.
  - `tail -n +N` / `head -n -N` (whole-file reads) were classified bounded
    — sign-prefixed counts now route to the unbounded path.
  - mid-path directory-symlink escape survived `confine` when the full
    path already existed — now checks each symlink's immediate (one-hop,
    lexical) target.
  - `window.json` was clobbered to `window_pct:0` by any usage-less
    response, silently disengaging the window-pressure throttle — the
    write is now skipped when a response carries no usage.
  - `create_checkpoint` crashed (`IndexError`) on a blank evidence line.
  - a string `patterns` typo in ctx.toml silently disabled ALL secret
    redaction (chars iterated as patterns) — now isinstance-guarded to
    the full default set. (Two of these — redaction, window throttle —
    are security/safety bugs that survived 14 versions + a hand audit.)
- **Library adoptions** from the doctrine-faithful S5 audit: `_mask_token`'s
  hand-rolled bounded dict → `functools.lru_cache`; the containment check →
  `Path.is_relative_to`. Three larger candidates deferred to `ctx debt`.
- **Metrology fix**: cache-read invalidations are judged within
  reconstructed transcript threads, not a single global max — parallel
  tool-call models no longer produce false invalidations (declared
  metrology debt resolved).
- **Emission governor validated in the wild**: a 208k-output bug-hunt
  session crossed all 10 pressure tiers, one nudge each, correct dedup —
  the first real-load exercise of the mechanism.

## [0.14.0] - 2026-07-18

The cleanup wave: audit with receipts, debt paid down, and Rust exactly
where measurement says it makes sense.

- **Audit results:** lint debt was 4 findings (fixed, ruff clean); type
  debt 43 mypy findings → 24 (real fixes in proxy/hook/codeverbs; the
  runtime-safe residual is declared in `ctx debt` with coordinates).
  Hand-rolled-vs-library review: the stdlib-first doctrine holds — every
  remaining hand-rolled piece is deliberate, documented, and has an
  opportunistic accelerator path (rg, ctags, orjson, jedi, grimp).
- **Real bug found by the audit:** the no-`--settings` fallback path
  merged only PreToolUse hooks, silently dropping the emission governor —
  fixed to merge every stage.
- **Rust where it makes sense (`native/ctx-hook-native`):** CPython's
  startup floor is a measured ~29 ms and PostToolUse fires on every
  Bash/Read/Edit/Write (~80 spawns/session ≈ 2.7 s). The Rust shim does
  identical work in ~3 ms (12×), is selected opportunistically
  (CTX_NATIVE_HOOK / PATH), and is parity-tested byte-for-byte against
  the canonical Python — including shared flock'd tier state and both
  host dialects. A full Rust rewrite remains declined by measurement:
  hook time is ~1% of session wall-clock.
- Price tables deduplicated (matrix_report now imports ctx.scorecard's).
- **README overhauled:** quickstart, the four-gate model, current verb
  table (seq/gain/debt/outlines), the five-system stack comparison with
  receipts, and the regime scoreboard.

## [0.13.0] - 2026-07-18

The Tura wave: round economy. Wire replay over five real sessions showed
32% of tool-bearing rounds were mechanical bash-after-bash chains (70% on
lint-fix, 65% on creation) — each ~1.5-2s ttfb plus a suffix cache write.

- `ctx seq`: declared command trees — N steps, one round, `&&` semantics,
  every step a full birth-gate capture addressable as `run:<id>`; failing
  step's digest rides in full, green trees stay terse. The runtime-owner
  advantage (Tura's macro execution) taken at harness level, losslessly.
- Scorecard: `rounds` is now the headline metric; `rescue_recovery`
  (first rescued round, rounds after, blocks elided) adopted from Tura's
  best measurement.
- **Backward planning adopted into the discipline prompt after a held-out
  A/B win on every axis (haiku, fresh task): -17% cost, -16% turns, -14%
  time, -18% output, 9 tests vs 7. PREFIX_VERSION 3 — one cold cache
  write per model, disclosed.** Skill rule 14 teaches the same plus seq.
- Benchmark manifest (`evals/bench-manifest.json` + test): task
  definitions frozen behind hashes; held-out rule recorded — a mechanism
  tuned against a task records its win only on an unseen variant.
- Declared in `ctx debt`: state-projection context (needs a runtime
  channel hosts do not expose) and an invalidation-metric investigation
  (first nonzero readings look like parallel-request interleaving, not
  real prefix regressions).

## [0.12.0] - 2026-07-18

The open-threads wave: both remaining designed-but-unbuilt items, each
gated by an isolated live experiment (both on haiku, one variable per test).

- **Solution ladder adopted into the wrap discipline prompt** after a
  measured A/B win on a creation task: -28% turns, -33% time, -17% cost,
  -28% output tokens, 9% less product code with MORE test code (effort
  floor held), quality green. **This is a prefix-resident change:
  PREFIX_VERSION 2 — every user pays one cold cache write per model on
  first post-upgrade session.** The same ladder is skill rule 13, paired
  with debt declaration.
- **Emission governor validated live** for the first time: fired exactly
  once at the 20k tier on a verbose doc-gen session, correct dedup, zero
  quality damage (non-inferior on every axis; efficiency effect size needs
  longer sessions and stays under scorecard watch).
- **`ctx debt`**: declared-omission ledger for engineering decisions
  (append-only committed JSONL, content-derived idempotent ids,
  add/list/resolve) — SPEC §8's no-silent-omission rule applied to scope.
- **Deliverable-level scorecard metrics**: LOC delta, files touched, and
  untracked-file line counts from git, in the summary line and history —
  over-engineering and effort-thinning are now measured regressions.
- **Skill shipped for real progressive disclosure**: body now advertises
  its reference tier (`references/verbs.md` — full verb/flag detail — plus
  routing-policy and repository-addressing pointers), carries a compact
  verb index covering everything since v0.2, and the prefix manifest
  splits the skill into frontmatter (prefix-resident, cache-relevant) vs
  body (invocation-loaded, tracked but bump-free) so future body
  improvements are not mispriced as cache invalidations.

## [0.11.0] - 2026-07-18

The rtk-inspired wave, hypotheses revised by real-corpus measurement before
building (evals/rtk-corpus-2026-07-18.md — two reversals: diagnostics
needed structure not compression; small outputs were being inflated by our
own scaffold).

- lint/v1 digest profile: eslint/ruff(rustc-style)/tsc/cargo/go/mypy
  diagnostics rendered as exact censuses (by severity, rule, file) with a
  span-backed first-diagnostic region — decision-grade structure at ~2x
  the blind text digest's budget.
- Scaffold-slim inline emission: small complete outputs emit command +
  exit + unindented content (~20 token overhead, was 100-400; pip digests
  were literally 2x the size of the output they contained).
- Failure-asymmetric budgets: `[budgets] failure_budget_factor` (default
  2.0) — failing runs get twice the emission budget; success is
  boilerplate, failure is evidence.
- `ctx gain`: cumulative containment savings by verb from telemetry, with
  token and dollar framing.

## [0.10.0] - 2026-07-18

Lossless mid-session rescue (docs/LOSSLESS-RESCUE.md) — the rewriting
proxy's last structural edge, taken without its costs. Opt-in Tier-1:
`ctx wrap claude --rescue-pct 70` (or `ctx proxy --rescue-pct`); the
default proxy remains the byte-exact Tier-0 observer.

- Epoch-latched elision: at a window-pressure crossing, ONE deterministic
  set freezes (tool_results older than the 6 most recent, >1 KiB); every
  subsequent request rewrites to a byte-identical prefix, so the cache is
  re-bought once at the smaller size and stays stable. Simulated with
  measured prices and real S4 wire shapes: ~18× less cache overhead than
  per-request rewriting, 18 turns of lossless runway per 27k elided.
- Nothing destroyed: elided bytes persist verbatim to
  `<state>/elided/<sha256>.txt` before the stub exists; stubs carry hash,
  size, and retrieval path; `rescued: N` disclosed on every wire record;
  startup banner marks the mode non-byte-exact. Fail-open on any parse
  problem. Property-tested: determinism, grown-transcript prefix
  stability, epoch latching across restarts.

## [0.9.0] - 2026-07-18

The priced-context wave (thesis: docs/PRICED-CONTEXT.md — metadata as
economic signposting; every mechanism survived a measured cheap test, and
the rejects are recorded there too).

- **Price tags in guard steering (M1).** Oversized-read deny/rewrite
  reasons now carry the cost in the agent's native currency —
  "~30k tok ≈ 15% of window" — computed from the stat the hook already
  performs and the proxy's window ground truth when present (measured
  cost: 0.003 ms). Coarse buckets by design: precision only needs to
  cross decision thresholds (`textutil.fmt_tokens_coarse`).
- **Priced symbol outlines (M2).** `ctx stats repo:<file>.py` returns the
  menu instead of an aggregate: every top-level symbol and method with
  line range, ~token price, and a resolvable span handle (snapshot-backed,
  deterministic). Measured 12.8–54.5× cheaper than the file it describes
  across src/ctx. The guard's oversized-read remediation names this verb —
  degrading a read is now structured-lossy, not truncated-lossy.
- **Priced map survivors (M3).** `ctx map` entries carry "~⟨tok⟩ tok · ⟨n⟩d"
  for ranked survivors only (flat inventories were measured and rejected:
  5× waste that scales with repo size). Map cache format bumped to
  ctx.map/v3.
- Deliberately NOT changed: the MCP tool description — advertising the new
  verb there would cold-invalidate every user's prompt cache; the prefix
  manifest holds at version 1 and the outline is discoverable through
  mid-stream steering instead. (The prefix-stability contract shaping its
  first real decision.)
- Benchmark harness: fixture agents can no longer hijack the host's
  editable install (`PIP_REQUIRE_VIRTUALENV=1` in matrix runner env — an
  S4 overhaul agent actually did this).

## [0.8.0] - 2026-07-18

The measurement-loop wave: six mechanisms that convert benchmark
postmortems into runtime feedback, each grounded in a measured failure
from evals/matrix-2026-07-18.md.

- **Prefix-stability contract (A).** Every injected prefix byte (wrap
  discipline prompt, explorer agent, MCP tool description, skill) is
  locked behind `src/ctx/prefix-manifest.json` + `PREFIX_VERSION`; the
  golden-hash test fails on unacknowledged change, because a 9-token edit
  measurably cost one full cold cache rewrite per model (~56k tokens).
- **Session scorecard (D) + effort mix (F).** `wire.jsonl` now records the
  request model and a tool_use census (names only); `ctx.scorecard`
  computes token classes, cold-prefix vs true invalidations vs suffix
  growth, ttfb/generation split, per-model usage, and edit-share.
  `ctx wrap` prints a one-line scorecard at session end and appends
  history to `.ctx-session-reads/scorecards.jsonl`; `ctx stats --session`
  renders the full card.
- **Graduated engagement (C).** Sessions start passive under
  `[engagement] mode = "auto"`: digests carry no "next:" affordances until
  a measured signal graduates the session (hook call count, window
  pressure, or a digest that actually truncated). Lean models (haiku by
  default) keep a single suggestion even when active — measured: haiku
  over-executes affordances as work items. Filtering happens at the
  emission boundary only; stored digests remain byte-identical pure
  functions (SPEC §8).
- **Emission governor (B).** New `ctx hook <host> post-tool-use` stage —
  the symmetric partner of the read-budget governor. When proxy-measured
  cumulative output crosses a 20k-token tier AND the per-request average
  is verbose, it injects one terse-narration nudge (Claude Code
  `additionalContext`; Antigravity decision dialect), exactly once per
  tier. Registered by `ctx wrap` and the plugin hooks template.
- **Anticipatory inlining (E).** The pytest digest inlines the first
  failure region (budget-gated, separator-bounded, deterministic) so the
  most common follow-up costs zero retrieval hops — each avoided hop is
  ~2s of ttfb plus a suffix cache write.

No injected prefix text changed in this release: the prefix manifest holds
at version 1, so v0.8.0 causes no cache cold-write.

## [0.7.1] - 2026-07-18

Benchmark-diagnosis fixes, all three grounded in measured evidence rather
than suspicion. The proxy now passes `Accept-Encoding` through untouched and
decompresses only the observer's private copy (`_Decoder`, zlib
auto-detect; unknown encodings fail open to no-usage) — the earlier
forced-identity workaround is gone. The proxy keeps a small pool of warm
upstream connections (TLS handshake amortization for remote upstreams;
stale pooled connections retry once on a fresh socket) and stamps every
`wire.jsonl` record with `ms: {connect, ttfb, total}` and `reused_conn`, so
per-exchange latency attribution is now ground truth instead of guesswork.
`ctx wrap claude` injects an emission-discipline system prompt in print
mode (the v0.7 rematch showed the entire wall-clock gap was output-token
volume: 69k vs 42k tokens ≈ the whole duration delta at ~80 tok/s); opt out
with `CTX_WRAP_NO_DISCIPLINE=1` or by supplying your own
`--append-system-prompt`. The profiled digest hot path
(`logprof._mask_token`: 180k per-character digit scans over 20k lines) now
uses a compiled digit regex plus a bounded token-mask memo — same masks,
~6× faster (0.82s → 0.14s on the 20k-line profile fixture).

## [0.7.0] - 2026-07-18

Tier-0 wire observer shipped: `ctx proxy` is a localhost-only pass-through
proxy for Anthropic-API traffic (byte-exact relay, SSE unbuffered) with a
fail-open observation tap writing `window.json` (provider-reported
input/cache/output usage, window fullness) and `wire.jsonl` (per-exchange
block census and tool_result sizes — no bodies, no auth headers);
`ctx wrap claude --proxy` supervises it per-session and injects
`ANTHROPIC_BASE_URL` into the child env only, failing open to an unproxied
session. Validated against the live production API. In progress: adaptive
guard, symbol-addressed code verbs (`ctx def`/`refs`/`diag`, jedi-backed
`[code]` extra with ast fallback), and learned policy epochs
(telemetry → committed policy).

## [0.6.0] - 2026-07-17

The roadmap's mechanism wave (M-A, M-C, M-D, Gate 3): the `ctx-explorer`
quarantine agent gives sub-agent exploration provenance — evidence via ctx
verbs, cite-don't-quote, mandatory checkpoint-shaped reports where a claim
without a handle is labeled a hypothesis; the cumulative session read-budget
governor closes the death-by-a-thousand-small-reads hole (graduated pressure
past `session_read_budget_bytes`, byte-identical behavior below it);
`ctx map` produces a deterministic budget-fitted codebase map
(reference-graph ranking, evidence weighting from recent captured runs,
worktree-hash caching, `engine grimp+networkx` when the optional `[map]`
extra imports, builtin otherwise); `ctx diff run:A run:B` emits run-to-run
regression digests (exit/stream/failure-set/template deltas with minted
spans). Library swaps landed with the fallback doctrine intact: flock'd
read ledger, opportunistic orjson (`[fast]` extra), drain3 evaluated and
declined. Measured (evals/overhaul-3arm-2026-07-17.md, v0.6 rematch): on
the full repo-overhaul benchmark the harnessed arm was **40% cheaper than
naive ($2.21 vs $3.70) and faster (6.1 vs 7.2 min) at quality parity**,
reversing round 1's cost sign — the ungoverned-fork externality is gone.
168 tests.

## [0.5.0] - 2026-07-17

Deterministic zoom spans (SPEC 6.4): digests attach content-derived span
tokens (`sha256(blob|kind|params)[:10]`) exactly at every omission point;
resolving a span is structurally bounded — small regions return exact
lines, large regions return a zoom sub-digest minting further sub-spans —
so retrieval can never re-flood the transcript. PR #1 review hardening:
`ws:<alias>` routing via committed `[aliases]`, lease-aware gc with
time-bounded retention, single-file `repo:<file>` selectors. The honest
measurement pass recorded the N=5 matched-warm-cache A/B (**cost parity
within noise, ~13% overhead, 5/5 correct both arms, zero denials**) and the
Headroom 0.32.0 needle-drop head-to-head: on a quiet structural needle
(no error keywords in 20,001 lines) **Headroom silently dropped it
(347,595 → 68 tokens, no trace); logtemplate/v1 preserved it verbatim with
its coordinate** — 100% vs 0% needle-drop rate. Roadmap and the four-gate
unified architecture were written down; the three-arm overhaul benchmark
(naive vs straitjacket vs Headroom) recorded no quality degradation from
context mediation in any arm. 131 tests.

## [0.4.0] - 2026-07-17

The transparent-steering wave: complete substitution steering rewrites
flooding commands instead of denying them, in both host dialects (Claude
Code `updatedInput`, Antigravity allow+updatedInput) — zero denial
round-trips, `steering = "deny"` reproduces the v0.3 contract
byte-identically. Claude Code support landed end-to-end: the PreToolUse
hook adapter (`ctx hook claude-code pre-tool-use`) and `ctx wrap` for
one-command harnessed sessions (ephemeral `--settings` injection, zero
residue). `logtemplate/v1` added deterministic Drain-style log template
mining (5,000-line log → 0.27% of raw bytes with the single ERROR needle
preserved at its exact coordinate), and the zero-hop inline threshold
widened to the result budget. Measured (evals/ab-claude-code-2026-07-17.md):
the full evidence workflow showed **456 model-visible tokens vs ~222,000
raw — a 487× reduction on first exposure**; the v0.4 rematch beat naive
6 turns/$0.072 vs 9/$0.186 on matched warm caches (later corrected by the
N=5 batch to cost parity within variance). 114+ tests.

## [0.3.0] - 2026-07-17

Library-grade engines, tiered so the hook hot path stays stdlib-only:
`repo:` searches use ripgrep (`rg --json`) when installed — SIMD prefilter,
parallel walk, deterministic ordering enforced, ~3x on the work portion —
with a transparent builtin fallback (`CTX_SEARCH_ENGINE=python` forces it);
`.ctxignore` matching moved to pathspec for true gitignore semantics;
secret redaction expanded from 3 to 16 vendored gitleaks-grade patterns;
manifests validated against the vendored invocation-v1 JSON Schema in tests
and `ctx doctor`; doctor discloses the active search engine and ignore
matcher. 78 tests.

## [0.2.0] - 2026-07-17

Performance and capability wave: search core rewritten to whole-text
matching (**13x on sparse patterns over 500k lines**, end-to-end CLI
271→149 ms), on-disk line-offset indexes so `ctx get --lines` touches only
the requested byte range, zero-subprocess git identity, MCP connection
caching. New capability: zero-hop digests (small complete outputs inline
verbatim), four new deterministic profiles (gotest/v1, jest/v1, build/v1,
gitdiff/v1), hook v2 (wrapper unwrapping, `bash -c` classification,
redirection allowance, repo-configured allow/deny prefixes),
`ctx checkpoint` (pinned content-addressed task epochs), `ctx get --symbol`
via stdlib ast, and the telemetry ledger (raw vs emitted bytes per op,
surfaced in doctor, never in digests). 70 tests.

## [0.1.0] - 2026-07-17

Initial implementation of the CTX harness specification (Phases 1–2):
pure-stdlib runtime with workspace resolution, a content-addressed artifact
store (SQLite WAL catalog outside the repo), birth-time capture runner
emitting `ctx.invocation/v1` manifests, and the four model-facing verbs
(run/search/get/stats) with deterministic ordering, token budgets,
continuation coordinates, and snapshot-on-read evidence. Deterministic
digest profiles (text, json, jsonl, pytest) with ANSI stripping and secret
redaction; the stdlib-only PreToolUse context guard (~40 ms, fail-open,
exactly one JSON decision); a bounded single-tool MCP stdio server; the
Antigravity plugin package with installer, `ctx init`, `ctx doctor`, and
lease-aware gc; vendored normative spec, acceptance suite, ADRs, and wire
schemas under `spec/`. 51 tests.
