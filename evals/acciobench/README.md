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

### Task Selection & Resume Controls

- `--tasks id1,id2,...`: run explicit task IDs.
- `--category file,api,cli,browser`: filter to one or more task categories.
- `--wave-file plan.json --wave wave1`: run predefined stratified waves.
- `--sample N --seed 42`: deterministic random subsample.
- `--no-cache`: force re-execution instead of resuming from completed `*_meta/result.json` receipts in `--workdir`.
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

---

## 5. Architecture: `--arm raw` vs `--arm sj` & Performance Design

### Paired Arm Definitions

| Dimension | `--arm raw` (Accio Baseline Parity) | `--arm sj` (Straitjacket Smart-Peg) |
|---|---|---|
| Tool schemas | `bash`, `read`, `write`, `edit`, `list`, `glob`, `grep`, `present_files` + `browser_*` (no `state_note`) | Identical tool roster + optional 1-line `state_note` checkpoint parameter |
| Turn-0 prompt | Official `task.md` + workspace/anti-tamper contract | Same + **Birth Gate** compact workspace file manifest (`<=80` files) |
| Output containment | Raw subprocess / file output truncated at `400,000` chars | **Entry Gate** (`24,576` B): large outputs stored via `ctx run --json`, returning structured digests + `ctx get`/`ctx search` handles |
| Redundant call & file guards | None | Readonly call dedup per `mutation_epoch` + content-addressed `sha256` file re-read dedup |
|CLI discovery | Raw `--help` stdout | Concurrent subcommand `--help` signature enrichment (`[cli-lsp subcommand signatures]`) |
| Process & filesystem sandbox | Linux `bubblewrap` (`bwrap`) mount + PID namespace isolation (`/task` mount, isolated `/tmp`, `dataset_root`/sibling `runs`/`mock_case` masked via `--tmpfs`, graceful fallback on macOS) | Identical `bwrap` mount + PID namespace isolation |
| Context residence | Append-only full transcript | `WatermarkResidence` preserving mutating turns & `state_note` checkpoints while compacting stale thoughts, read-only floods, and old UI control listings |
| Finalization check | Direct `present_files` | Structural **Emission Gate** catching missing/0-byte/malformed-JSON deliverables and stray scratch scripts in `/task/outputs` |
| System instruction | None | Concise 4-rule domain-agnostic `SJ_EXECUTION_PROTOCOL` |

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
