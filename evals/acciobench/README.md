# AccioBench (`evals/acciobench`) — CommerceAgentBench Paired A/B Suite

`evals/acciobench` evaluates [CommerceAgentBench](https://github.com/Accio-org/CommerceAgentBench)
(107 long-horizon domain tasks: 53 `cli`, 28 `browser`, 16 `file`, and 10 `api`
workflows) with first-class paired A/B support comparing the unmodified Accio
tool surface (`--arm raw`) against Straitjacket's Smart-Peg harness (`--arm sj`)
under identical prompts, untouched model defaults, isolated mock services, and
official `grader/run.sh` verification.

Per [`evals/BENCHMARK.md`](../BENCHMARK.md), **evidence preservation**
(`solved_sj / solved_raw ≈ 1.0`) is the load-bearing gate: step, token, latency,
and cache-hit deltas are meaningful only when task completion and verifier
capacity score match or exceed the raw baseline.

---

## 1. Quickstart (`bootstrap.sh`)

Run the one-command bootstrapper from the repository root to set up a Python
virtualenv, install `ctx-harness` + Playwright + CommerceAgentBench data/document
and grader dependencies (`pandas`, `openpyxl`, `PyYAML`, `pillow`, `pypdf`,
`pdfplumber`, `pymupdf`, `beautifulsoup4`, `lxml`), clone `CommerceAgentBench`,
install headless Chromium, and stage `mock_services` + `bun` into `.acciobench/runtime`:

```bash
bash evals/acciobench/bootstrap.sh
```

Custom locations or existing environments are supported via flags:

```bash
bash evals/acciobench/bootstrap.sh \
  --dataset-root /path/to/CommerceAgentBench \
  --runtime-dir /path/to/runtime \
  --venv /path/to/.venv
```

> **Dockerless machines:** `--skip-docker` (or hosts without Docker) still sets
> up the full `file` / PDF / spreadsheet / public-web workflow immediately;
> stateful `cli`, `api`, and local `browser` mock tasks use `/opt/mock_services`
> extracted once from the official pinned `acciolyk/accio_bench` image.

---

## 2. Validate the Referee Before Spending (`doctor` & `validate`)

Before making any paid model calls, verify your local toolchain and prove the
sandbox + grader pipeline end-to-end at **$0 API cost**:

```bash
# 1. Check Python deps, ctx sidecar, pdftotext, bun/node, Playwright/Chromium, dataset & credentials
python evals/acciobench_run.py doctor \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime

# 2. Model-free referee check on specific tasks or a sample (--limit N)
python evals/acciobench_run.py validate \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --task file-commerce-ops-thailand-gadgets-1111-promo-plan
```

For each task, `validate` deterministically:
1. Stages an isolated `TaskSandbox` on a dedicated port block (`20000 + slot * 100`) and starts any `services/start_services.sh` or `runtime_mocks` daemons.
2. Exercises `ToolHost` (`arm="sj"`) + `ctx run --json` Entry Gate capture on synthetic large output to verify sidecar storage and digest generation.
3. Runs the task's official `grader/run.sh` (`verify_task.py`) against untouched/empty `/task/outputs` with `--judge-provider mock` to confirm **zero baseline setup leakage** (an untouched workspace must not spuriously pass).
4. Tears down the sandbox and verifies **zero leaked child processes** for the slot token.

---

## 3. Running Single-Arm & Paired A/B Benchmarks (`run`)

### Public Gemini API (Zero GCP Setup)

External users only need a standard `GEMINI_API_KEY` from Google AI Studio:

```bash
export GEMINI_API_KEY="your-api-key"

python evals/acciobench_run.py run \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --workdir ./.acciobench/ab_run \
  --provider gemini \
  --model gemini-2.5-flash \
  --judge-provider gemini \
  --judge-model gemini-3.1-pro-preview \
  --arm raw,sj \
  --sample 12 \
  --workers 4
```

### Google Cloud Vertex AI (Application Default Credentials)

```bash
export VERTEX_PROJECT_ID="your-gcp-project"
export VERTEX_LOCATION="global"

python evals/acciobench_run.py run \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --workdir ./.acciobench/full107 \
  --provider vertex \
  --model gemini-3.8-flash \
  --judge-provider vertex \
  --judge-model gemini-4-argon \
  --arm raw,sj \
  --workers 8
```

### Bubblewrap Is Required for Scored Runs

`run` refuses to start when the bwrap sandbox is unavailable (bwrap missing, unable to
create mount + PID namespaces, a non-Linux host, or `ACCIOBENCH_NO_BWRAP=1`): it exits
non-zero before any model call or trial setup and prints why, how to install
bubblewrap (`sudo apt-get install -y bubblewrap` on Debian/Ubuntu) and the override.
Without bubblewrap neither arm has filesystem isolation (see
[`/tmp` without bubblewrap](#tmp-without-bubblewrap)), so a score cannot be trusted.
The only override is the explicit `--allow-unsandboxed` flag on `run`
(`BenchConfig(allow_unsandboxed=True)`); `ACCIOBENCH_NO_BWRAP` alone is not one. The
check sits in `run_trial` itself, so every caller (`run`, `run_suite`, a script) is
covered. With the override, `run` prints one prominent warning at start, every
`result.json` records `sandbox: "none"` (`"bwrap"` when sandboxed). Every result is
`bwrap`, `none`, or unknown (`""`: written before this field existed), and reports
show the last two: table and markdown put a `NO FILESYSTEM ISOLATION` and an
`ISOLATION UNKNOWN` line at the top when either count is non-zero; JSON always carries
`unsandboxed_runs` and `isolation_unknown_runs` (total and per arm); the HTML header
shows both. A cached result without the field whose fingerprint matches the current
run is recorded with the current mode when it is reused (the fingerprint includes
whether bwrap is active, so it ran in that mode); a legacy result reused on `model_id`
alone stays unknown. `doctor` and `validate` cost nothing and still
run without bwrap, but say that a scored run will be refused.

### Task Selection & Resume Controls

- `--tasks id1,id2,...`: run explicit task IDs.
- `--category file,api,cli,browser`: filter to one or more task categories.
- `--wave-file plan.json --wave wave1`: run predefined stratified waves.
- `--sample N --seed 42`: deterministic random subsample.
- `--no-cache`: force re-execution instead of resuming from completed `*_meta/result.json` receipts in `--workdir`.
- **Resume is per configuration**: every `result.json` carries `config_fingerprint`, a
  hash of the settings that change what a trial measures (`model`, `provider`,
  `thinking_level`, `max_output_tokens`, `max_steps`, `entry_gate_bytes`,
  `baseline_raw_cap`, `judge_provider`, `judge_model`, the loop's own gate and
  compaction constants, a content hash of the task's own directory (prompt,
  `task.toml`, workspace, services, and the grader, rubric and private answers, since
  a changed grader changes the recorded score), the version the benchmarked ctx
  reports (`ctx --version`), a content hash of the ctx code itself (below), and
  whether the bwrap sandbox is active). A cached result is reused only when its
  fingerprint matches; worker counts, slot offsets, output paths, credentials and host
  paths are not part of it (moving the dataset does not invalidate a workdir; changing
  its content does).
  **ctx code (`sj` arm only)**: the `sj` fingerprint carries two content hashes, so an
  edit to ctx is detected even when its version stays the same. (1) The ctx the trial
  launches (`--ctx-bin`, else the venv's or `PATH`'s `ctx`, else the source-checkout
  shim): when it is a Python console-script entry point that imports `ctx`, a Python
  interpreter (run as `-P -m ctx`), or the shim, the hash covers the `ctx` package that
  interpreter would import, found with `importlib.util.find_spec` in a child run as
  `python -P` from `/` (nothing is imported or run from a trial's directory). Any
  other `ctx` (a shell wrapper, a compiled binary) falls back to a hash of the
  executable's own bytes, so an edit behind such a wrapper is not seen: pass
  `--no-cache` there. (2) The `ctx` package the harness imports in-process (residence
  compaction, deliverable audits and the hook grammar, all used by the `sj` arm only).
  The `raw` arm runs no ctx code, launched or in-process, so neither hash is part of
  its fingerprint and an edit to ctx never re-runs `raw` trials. Both hashes read file
  contents under relative paths (`__pycache__` and `.pyc` skipped), so moving a
  checkout does not invalidate a workdir; each is computed once per process (about
  25 ms for the launched ctx, most of it one interpreter start, and under 10 ms for the
  in-process package, measured on one machine; about 0 afterwards). Adding these
  hashes changed every `sj` fingerprint once: `sj` results written before them are
  re-run on the next resume (`raw` results are kept).
  A result written before fingerprints existed is reused only if its
  `model_id` equals `--model` (one `[legacy]` line says so); never across models.
  When a result does not match, one `[rerun]` line says why and the previous trial
  (its run directory and `_meta` directory) is moved to
  `<workdir>/superseded/<old fingerprint>/` (or `legacy-<model>/`; a `.2`, `.3`, ...
  suffix when that already holds a trial), where reports do not look. Nothing is ever
  deleted: if the move fails, that trial stops with an error and its directories stay
  where they were. `run` then prints a `[not recorded]` line for the trial and leaves
  the earlier result still in its slot out of the reports it writes (a later
  `report` on the same workdir reads whatever is on disk, so it will show that earlier
  result until the trial is run again). If every selected trial raised, those reports
  show no completed run. `ctx_state/` is shared by all configurations.
  The task-directory hash reads regular files only; a FIFO, a device, a dangling
  symlink or an unreadable file is recorded by what it is instead of being opened.
  It follows directory symlinks, because staging copies through them: a change beneath
  a linked input directory is a change to the task. Each real directory is hashed
  once; a second link to it, or a link back to an ancestor, is recorded by name.
- **Grading errors are not failures**: when the official grader itself fails (timeout,
  missing, unparsable or uninterpretable `reward.json`: not an object, a non-numeric
  or non-finite score, a negative score or one above 1, checks_passed > checks_total,
  neither a score nor checks, a malformed checks_breakdown), `result.json` records `grading_error` (the grader's
  message) with `passed=false` and zero scores, and the agent's `termination` is kept.
  Reports (table, markdown, JSON `grading_error_runs`, HTML header) exclude such trials
  from solve rate and score aggregates and show their count separately. On resume, a
  trial in this state is **re-graded only**: the grader runs again against the outputs
  still in its run directory (same staged case, same port map from
  `sandbox_ports.json`, mock services restarted from the trial's persisted state), with
  no model call and nothing wiped. Only if its run directory is gone is it re-run.
  A re-grade must bind the ports its trial ran with, and trials that ran one after
  another in one worker slot recorded the same ports, so sandboxes in one run take
  turns on a host port: a re-grade waits (up to 30 minutes) for a port another trial
  holds, and if something outside the run is listening on it the trial stays ungraded
  with that reason, rather than being graded against another trial's service.
  Results written before this change that carry a `verifier error:` note and
  `passed=false` load as grading errors.
  A reward `score` is read on a 0..1 scale (the pass threshold is 0.9999 and reports
  treat capacity as a fraction): a score above 1 (beyond a 1e-6 float tolerance, which
  is read as 1.0) is a grading error whether or not checks are present, so a grader
  that reports `85` or `5` is never recorded as a pass. A result already on disk is not
  re-read by this rule.
- **Untouched model defaults**: `thinkingConfig` and `maxOutputTokens` are left unset by default so models run at their native provider defaults. Optional `--thinking-level` and `--max-output-tokens` flags are exposed strictly for explicit ablation studies.

---

## 4. Stratified Reporting (`report`)

Render single-arm metrics, Straitjacket lever telemetry, and task-aligned paired
A/B deltas stratified by **category** (`api`, `browser`, `cli`, `file`) and
**bucket** (`worse`, `better`):

```bash
# Terminal ASCII table (auto-pairs --arm raw vs --arm sj in workdir)
python evals/acciobench_run.py report --workdir ./.acciobench/ab_run

# GitHub-flavored Markdown or JSON export, optionally paired against a historical baseline JSON
python evals/acciobench_run.py report \
  --workdir ./.acciobench/full107 \
  --baseline-json ./baseline_summary.json \
  --format markdown \
  --output ./acciobench_report.md

# Self-contained interactive HTML report + multi-arm Trajectory Deep Dive
# (also auto-generated at <workdir>/acciobench_report.html at the end of every 'run')
python evals/acciobench_run.py report \
  --workdir ./.acciobench/full107 \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --baseline-json ./baseline_summary.json \
  --format html \
  --output ./acciobench_report.html
```

**Unpaired tasks.** A task that one arm has no scorable result for (ungraded, API or
setup error, not run, superseded by a configuration change) is left out of every paired
figure, and out of every HTML view, and is counted as "Unpaired tasks" (with the
missing arm) in the table, markdown, JSON (`unpaired_tasks`, `unpaired`) and HTML
header. A report that fails to render at the end of `run` never fails the run: the
results are already on disk, so `run` names the report that failed, prints the
`report` command that regenerates it, and exits with the run's own status.

---

## 5. Architecture: `--arm raw` vs `--arm sj` & Performance Design

### Paired Arm Definitions

| Dimension | `--arm raw` (Accio Baseline Parity) | `--arm sj` (Straitjacket Smart-Peg) |
|---|---|---|
| Tool schemas | `bash`, `read`, `write`, `edit`, `list`, `glob`, `grep`, `present_files` + `browser_*` (no `state_note`) | Identical tool roster + optional 1-line `state_note` checkpoint parameter |
| Turn-0 prompt | Official `task.md` + workspace/anti-tamper contract | Same + **Birth Gate** compact workspace file manifest (`<=80` files) |
| Output containment | Raw subprocess / file output truncated at `400,000` chars | **Entry Gate** (`24,576` B): large outputs stored via `ctx run --json`, returning structured digests + `ctx get`/`ctx search` handles |
| Redundant call & file guards | None | Readonly call dedup per `mutation_epoch` (a bash signature normalizes only blanks, a trailing `#` comment as the shell reads it and short-flag clusters; `#` inside a word such as `run:ID#stdout` is text, so each `--lines` page is its own call) + content-addressed `sha256` file re-read dedup |
|CLI discovery | Raw `--help` stdout | Concurrent subcommand `--help` signature enrichment (`[cli-lsp subcommand signatures]`) |
| Process & filesystem sandbox | Linux `bubblewrap` (`bwrap`) mount + PID namespace isolation (`/task` mount, isolated `/tmp`, `dataset_root`/sibling `runs`/`mock_case` masked via `--tmpfs`; required for scored runs, see above) | Identical `bwrap` mount + PID namespace isolation |
| Context residence | Append-only full transcript | `WatermarkResidence` preserving mutating turns & `state_note` checkpoints while compacting stale thoughts, read-only floods, and old UI control listings |
| Finalization check | Direct `present_files` | Structural **Emission Gate** catching missing/0-byte/malformed-JSON deliverables and stray scratch scripts in `/task/outputs` |
| System instruction | None | Concise 4-rule domain-agnostic `SJ_EXECUTION_PROTOCOL` |

### `/tmp` without bubblewrap

With `bwrap`, bash sees the run's private directory (`<run>_meta/sandbox_tmp`) as
`/tmp`, the same directory the file tools map `/tmp/...` to. Without it (macOS,
`ACCIOBENCH_NO_BWRAP=1`, `use_bwrap=False`) both arms rewrite `/tmp` paths in commands
and helper scripts by one rule: a `/tmp` path (`/tmp` as a whole component at the
start of a path word, after the start, a blank, a quote, `=`, `:`, `(`, a backtick or
a shell operator, and followed by `/`, a blank, a quote, an operator or the end; so not
`/tmpfoo`, `/tmp.txt`, `/tmp~`, `~/tmp`, `/var/tmp`, `${X}/tmp` or `http://x/tmp/y`) is
the private one unless its first component below `/tmp` exists in the host's `/tmp`
and not in the private directory, in which case it is a host path (the ctx
executable, the run directory or workdir when they live under `/tmp`) and is left
alone. `TMPDIR` points at the private directory as well. So the file tools and bash
round-trip in both directions, `ls /tmp` shows the run's files, and new files land in
the private directory, never the host's `/tmp`.

What the fallback cannot do, by design: it has **no filesystem isolation** (bash can
read and write anything the harness user can, the dataset and grader files included);
a path built at run time (`T=/t; T=${T}mp`, `$(echo /tm)p`) is not seen by the rewrite;
and a name that already exists only in the host's `/tmp` is left to the host, so bash
can read and write it. This is why a scored `run` refuses to start without bubblewrap
unless `--allow-unsandboxed` is passed, and why reports count every trial that ran so.

### Which `ctx` calls run on the host (`sj` arm)

Every command the model issues runs inside the sandbox, with one exception kept for
speed: a `bash` call that is exactly one plain `ctx` call reading only ctx's own store
by a `run:` handle is answered by the harness directly, without starting the sandbox.
Measured on one machine (median of 40 interleaved calls): about 125 ms on the host
against about 130 ms for the same call started inside the sandbox, so the shortcut is
worth about 5 ms; nearly all of a ctx call is ctx itself starting. The larger saving
is that no plain ctx call, on the host or in the sandbox, is wrapped in a second
`ctx run` the way an ordinary command is (about 360 ms when it is). That is `ctx get run:ID[#stdout|#stderr] [--lines|--bytes|--records|--json-pointer|--span V] [--hashlines]`,
`ctx search run:ID PATTERN...`, `ctx stats run:ID`, `ctx diff run:A run:B`,
`ctx intake --chunks run:ID` and `ctx view run:ID ...`. The classification is the
product's own (`ctx.hook.ctx_command_words` / `ctx.hook.ctx_read_kind`, the same
grammar the PreToolUse hook uses to self-approve store reads), not a harness regex:
quoted words are fine when they mean exactly their text (`'foo bar'`, or `"foo bar"`
with no `$`, backtick or backslash inside), but a comment, `$(...)`, backticks, a
redirection, `&`, a here-string, any other double-quoted word, an assignment prefix, `--workspace`, a path, a `repo:`/`blob:`/other handle,
`--glob`/`--scope`/`--symbol`, `q`, `run`, `py`, `seq`, `job` or any verb the grammar
does not know all run inside the sandbox (a plain call with the same argv, anything
else under `bash -c`). `ctx_retrieve` follows the same rule on the argv it builds
from the model's `handle`, `pattern`, `query` and flag values; its `mode='query'`
(`ctx q`, which can name files) always runs in the sandbox. The sandbox's mount
arguments are computed once per working directory and reused. When ctx is launched
as a module (`--ctx-bin` pointing at a Python interpreter, or the source-checkout
shim), every launch, including the `ctx` shim on the model's `PATH`, uses
`python -P -m ctx` (as `ctx.selfcmd` does), so a `ctx.py` or `ctx/` the model writes
into its task directory is never imported.

### Harness Performance Optimizations (Both Arms Benefit Equally)

1. **Thread-Local `BrowserPool` (`browser.py`)**: Cold-starting Chromium per task adds ~2s of overhead. `BrowserPool` launches one headless browser per worker thread and creates an isolated `BrowserContext` (`1440x900`, clean cookies/storage) per trial in `<30ms`, closing only the context between tasks.
2. **Compiled Single-Pass Port Rewriting & Cached Discovery (`ports.py`)**: `discover_task_ports` caches results in memory keyed by `case_dir` so multi-arm runs never rescan task trees, and `CompiledPortMap` rewrites localhost URLs and service port assignments in a single compiled regex pass.
3. **Process-Group Sandbox Teardown & Symlink Staging (`sandbox.py`)**: Mock services spawn in dedicated process groups (`start_new_session=True`) tagged with `SJ_SLOT_TOKEN`, enabling `<5ms` `SIGTERM`/`SIGKILL` process-group cleanup with a `/proc` scan fallback. Read-only binary assets and `node_modules` are symlinked rather than byte-copied per trial.
4. **Parallel `--help` Probing (`host.py`)**: Subcommand `--help` inspections execute concurrently on a bounded `ThreadPoolExecutor(max_workers=4)` (`2.5s` timeout).
5. **Persistent HTTP Keep-Alive (`providers.py`)**: Both `gemini` and `vertex` providers reuse thread-local `http.client.HTTPSConnection` TLS sockets across turns, eliminating per-step handshake latency.

---

## 6. Package Layout

```text
evals/
├── acciobench_run.py          # Top-level CLI entrypoint (`python evals/acciobench_run.py ...`)
└── acciobench/
    ├── __init__.py            # Public package exports
    ├── config.py              # BenchConfig, RunMetrics, dataset loader & task selector
    ├── ports.py               # Cached port discovery & single-pass CompiledPortMap rewriter
    ├── sandbox.py             # Isolated per-slot workspace, mock services & official grader
    ├── schemas.py             # Accio tool declarations, prompt builder & mutation classifiers
    ├── browser.py             # Playwright BrowserSession & worker-scoped BrowserPool
    ├── host.py                # ToolHost supporting both `raw` and `sj` (`ctx` sidecar) arms
    ├── providers.py           # Pluggable `gemini` & `vertex` keep-alive ModelProviders
    ├── loop.py                # Single-trial agent turn loop, residence compaction & grading
    ├── orchestrator.py        # Multi-worker slot pool & paired multi-arm suite runner
    ├── validate.py            # Environment `doctor` & model-free sandbox/grader `validate` referee
    ├── report.py              # Paired A/B & historical baseline reporting (table/markdown/json)
    ├── cli.py                 # Subcommand parser (`doctor`, `validate`, `run`, `report`)
    ├── bootstrap.sh           # One-command external setup script
    └── README.md              # Documentation & reproducibility guide
```
