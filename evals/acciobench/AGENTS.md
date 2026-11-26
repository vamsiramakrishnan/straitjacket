# AGENTS.md — Reproducing CommerceAgentBench Paired A/B Runs (Accio x Straitjacket)

This guide defines the step-by-step workflow for agents and engineers to set up, validate, run, and inspect the 107-task [CommerceAgentBench](https://github.com/Accio-org/CommerceAgentBench) paired A/B benchmark comparing unmodified Accio baseline (`--arm raw`) against Straitjacket (`--arm sj`).

---

## 1. Environment Setup (One Command)

Run the bootstrapper from the repository root. It creates `.venv-acciobench`, installs `ctx-harness` (`ctx`) + Playwright + data/PDF/spreadsheet libraries (`pandas`, `openpyxl`, `PyYAML`, `pillow`, `pypdf`, `pdfplumber`, `pymupdf`, `beautifulsoup4`, `lxml`), clones `CommerceAgentBench`, installs headless Chromium, stages `bun` + `logistics_tracker` Node dependencies, and extracts `/opt/mock_services` from the pinned `acciolyk/accio_bench` image into `.acciobench/runtime`:

```bash
bash evals/acciobench/bootstrap.sh
```

Activate the virtual environment:

```bash
source .venv-acciobench/bin/activate
```

System packages (Ubuntu/Debian): `bubblewrap` is **required** for scored runs (Linux mount + PID namespace isolation); `poppler-utils` provides the PDF CLI utilities:

```bash
sudo apt-get update && sudo apt-get install -y bubblewrap poppler-utils
```

Without a working `bwrap` (missing, user namespaces disabled, non-Linux host, or `ACCIOBENCH_NO_BWRAP=1`), `run` refuses to start and exits non-zero: the fallback has no filesystem isolation, so the agent could read grader and answer files. The only override is `run --allow-unsandboxed`; it prints a warning, records `sandbox: "none"` in each `result.json`, and every report (table, markdown, JSON `unsandboxed_runs` and `isolation_unknown_runs`, HTML header) states how many trials ran without isolation and how many (results written before this was recorded) have unknown isolation. `doctor` and `validate` still run without bwrap and say that a scored run would be refused.

---

## 2. Zero-Cost Referee & Sandbox Verification (`$0` API Spend)

Always run `doctor` and `validate` before launching paid model trials:

```bash
# 1. Verify Python packages, ctx sidecar, bun/node, Playwright Chromium, dataset & credentials
python evals/acciobench_run.py doctor \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime

# 2. Validate sandbox isolation, mock service startup, ctx Entry Gate, and official grader on 1 task ($0 cost)
python evals/acciobench_run.py validate \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --task file-commerce-ops-thailand-gadgets-1111-promo-plan
```

Expected output: `[PASS]` for `doctor` and `baseline_leakage=False, leaked_procs=0, entry_gate_delivery=digest` for `validate`.

---

## 3. Running the Benchmark (`--arm raw,sj`)

### Option A: Google Cloud Vertex AI (Used for Official 107-Task Runs)

Authenticate with Application Default Credentials (`gcloud auth application-default login`) and set your GCP project:

```bash
export VERTEX_PROJECT_ID="your-gcp-project-id"
export VERTEX_LOCATION="global"

# Full 107-task paired A/B evaluation (214 trials: 107 raw + 107 sj)
python evals/acciobench_run.py run \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --workdir ./.acciobench/runs/full107_paired \
  --provider vertex \
  --model gemini-3.8-flash \
  --judge-provider vertex \
  --judge-model gemini-4-argon \
  --arm raw,sj \
  --workers 6
```

### Option B: Public Gemini API (`GEMINI_API_KEY`)

```bash
export GEMINI_API_KEY="your-gemini-api-key"

python evals/acciobench_run.py run \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --runtime-dir ./.acciobench/runtime \
  --workdir ./.acciobench/runs/full107_paired \
  --provider gemini \
  --model gemini-3.8-flash \
  --judge-provider gemini \
  --judge-model gemini-3.1-pro-preview \
  --arm raw,sj \
  --workers 4
```

### Targeted Runs, Smoke Tests & Sharding

- **Single task paired A/B**:
  ```bash
  python evals/acciobench_run.py run \
    --dataset-root ./.acciobench/CommerceAgentBench \
    --runtime-dir ./.acciobench/runtime \
    --workdir ./.acciobench/runs/single_task \
    --provider vertex \
    --model gemini-3.8-flash \
    --judge-provider vertex \
    --judge-model gemini-4-argon \
    --arm raw,sj \
    --tasks cli-slack-supplier-quote-collection \
    --workers 1
  ```
- **10-task stratified smoke test**: pass `--sample 10 --seed 42`.
- **Category filter**: pass `--category api`, `--category browser`, `--category cli`, or `--category file`.
- **Force fresh execution (ignore cached `result.json` in `--workdir`)**: pass `--no-cache`.

---

## 4. Generating Reports (`report`)

Every `run` automatically writes `<workdir>/acciobench_report.html` upon completion. You can also regenerate terminal tables, Markdown reports, JSON summaries, or the standalone interactive HTML report at any time from `<workdir>`:

```bash
# 1. Terminal ASCII summary table
python evals/acciobench_run.py report \
  --workdir ./.acciobench/runs/full107_paired

# 2. Markdown report
python evals/acciobench_run.py report \
  --workdir ./.acciobench/runs/full107_paired \
  --format markdown \
  --output ./acciobench_report.md

# 3. Interactive HTML report with 3-Way Trajectory Inspector
python evals/acciobench_run.py report \
  --workdir ./.acciobench/runs/full107_paired \
  --dataset-root ./.acciobench/CommerceAgentBench \
  --format html \
  --output ./acciobench_report.html
```

### Tabulating Multiple Models or Runs Side-by-Side (`tabulate`)

Compare multiple model/arm workdirs (and/or pre-computed verdict JSON files) in one table with model-aware pricing (`ctx/data/model-prices.json`, overridable via `.ctx-prices.json`):

```bash
# Terminal ASCII table comparing paired 3.8 runs alongside 3.7+SJ and 4-Hydrogen+SJ
python evals/acciobench_run.py tabulate \
  --run "3.7 Flash + SJ=./.acciobench/runs/full107_37flash_sj:sj" \
  --run "3.8 Flash=./.acciobench/runs/full107_paired" \
  --run "4-Hydrogen + SJ=./.acciobench/runs/full107_4hydrogen_sj:sj"

# Export spreadsheet-ready CSV (overall + api/browser/cli/file slices) or Markdown/JSON
python evals/acciobench_run.py tabulate \
  --run "3.8 Raw=results/full107_paired_verdict.json:raw_paired_dc8dfcc" \
  --run "3.8 + SJ=results/full107_paired_verdict.json:sj_paired_dc8dfcc" \
  --run "4-Hydrogen + SJ=./.acciobench/runs/full107_4hydrogen_sj:sj" \
  --format csv \
  --output ./comparison_table.csv
```

Useful `tabulate` features:
- **Auto-arm expansion**: passing a paired workdir without `:raw`/`:sj` emits one row per arm found.
- **Overlay waves**: chain `LABEL=BASE_DIR+OVERLAY_DIR:sj` to score a targeted post-fix re-run over a full baseline workdir without copying directories.
- **Partial run intersection**: pass `--common-tasks-only` to compare directory-backed runs strictly on their shared task subset.

---

## 5. Reference Results (107 Tasks, `datasets_domain_v1`)

Pricing is Vertex AI GA list (`$1.50 / 1M` uncached input, `$7.50 / 1M` thought + output; cached input `$0.15 / 1M` for 3.7/3.8 Flash and `$0.075 / 1M` for 4-Hydrogen).

| Metric | 3.7 Flash Baseline | 3.7 Flash + SJ | 3.8 Flash (`--arm raw`) | 3.8 Flash + SJ (1-Pass) | 3.8 Flash + SJ (Best-of-Waves) | 4-Hydrogen + SJ (Pre-Fix) | 4-Hydrogen + SJ (Post-Fix) |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Solved Tasks** | `54/107` (`50.5%`) | **`80/107` (`74.8%`)** | `61/107` (`57.0%`) | `88/107` (`82.2%`) | `105/107` (`98.1%`) | `87/107` (`81.3%`) | **`91/107` (`85.0%`)** |
| **Macro Capability** | `0.8273` | `0.9509` | `0.8981` | `0.9526` | `0.9965` | `0.9630` | **`0.9692`** |
| **API (`n=10`)** | `7/10` (`70.0%`) | `9/10` (`90.0%`) | `7/10` (`70.0%`) | `8/10` (`80.0%`) | `10/10` (`100.0%`) | `8/10` (`80.0%`) | `8/10` (`80.0%`) |
| **Browser (`n=28`)** | `15/28` (`53.6%`) | `21/28` (`75.0%`) | `19/28` (`67.9%`) | `24/28` (`85.7%`) | `28/28` (`100.0%`) | `25/28` (`89.3%`) | **`26/28` (`92.9%`)** |
| **CLI (`n=53`)** | `25/53` (`47.2%`) | `36/53` (`67.9%`) | `29/53` (`54.7%`) | `43/53` (`81.1%`) | `51/53` (`96.2%`) | `41/53` (`77.4%`) | **`44/53` (`83.0%`)** |
| **File (`n=16`)** | `7/16` (`43.8%`) | **`14/16` (`87.5%`)** | `6/16` (`37.5%`) | `13/16` (`81.2%`) | `16/16` (`100.0%`) | `13/16` (`81.2%`) | `13/16` (`81.2%`) |
| **Total Steps (Med)** | `4,800` (`37.0`) | `3,899` (`32.0`) | `6,907` (`56.0`) | `6,176` (`50.0`) | `6,104` (`49.0`) | `2,126` (`17.0`) | **`2,128` (`17.0`)** |
| **Turns / Solved Task** | `88.9` | `48.7` | `113.2` | `70.2` | `58.1` | `24.4` | **`23.4`** |
| **Total Tokens (Cache%)** | `~129.0M*` | `150.6M` (`87.5%`) | `589.3M` (`76.1%`) | `282.0M` (`84.1%`) | `278.7M` (`85.1%`) | `109.1M` (`90.3%`) | **`111.1M` (`90.5%`)** |
| **Gen Tokens** | — | `2.67M` | `4.55M` | `5.70M` | `5.57M` | `2.37M` | **`2.39M`** |
| **Suite Cost (USD)** | `~$107.00*` | `$67.13` | `$310.29` | `$143.53` | `$137.64` | `$40.50` | **`$40.72`** |
| **Cost / Solved Task** | `~$1.98` | `$0.84` | `$5.09` | `$1.63` | `$1.31` | `$0.47` | **`$0.45`** |
| **Wall Time (min/Solve)** | `9.17h` (`10.2m`) | `8.23h` (`6.2m`) | `17.87h` (`17.6m`) | `17.72h` (`12.1m`) | `17.07h` (`9.8m`) | `6.88h` (`4.7m`) | **`6.90h` (`4.5m`)** |

---

## 6. Operational Guardrails for Agents

1. **Keep model sampling/thinking parameters untouched**: Do not pass `--thinking-level` or `--max-output-tokens` when reproducing main results; both arms run at native model defaults.
2. **`bwrap` is required on Linux**: Keep `bubblewrap` installed so every trial runs with `/task` mounted at root, isolated `/tmp`, and masked sibling trial directories. A scored run without it is refused; never publish numbers from a run that needed `--allow-unsandboxed` as sandboxed results (its reports say how many trials ran without isolation).
3. **Disk space during 107-task runs**: Each trial stages mock service state under `<workdir>/runs/<task>__<arm>_meta/mock_services`. Ensure at least `15 GB` of free disk space before launching a full 214-trial paired run.
4. **Per-configuration resume**: Completed trials in `--workdir` are automatically reused if their `config_fingerprint` matches (`result.json`). The `sj` fingerprint includes a content hash of the ctx code (the launched `ctx` and the in-process `ctx` package), so editing ctx re-runs `sj` trials even without a version bump; `raw` trials are unaffected. If a trial had a transient grader timeout (`grading_error`), re-running the same command re-grades the existing outputs in place without making new model calls.
