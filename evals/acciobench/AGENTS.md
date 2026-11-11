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

Optional system packages (Ubuntu/Debian) for full Linux namespace isolation and PDF CLI utilities:

```bash
sudo apt-get update && sudo apt-get install -y bubblewrap poppler-utils
```

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

---

## 5. Reference Results (107 Tasks, `datasets_domain_v1`)

Pricing is Vertex AI GA list (`$1.50 / 1M` uncached input, `$0.15 / 1M` cached input, `$7.50 / 1M` thought + output).

| Metric | 3.7 Flash Baseline | 3.8 Flash (`--arm raw`) | 3.8 Flash + SJ (`--arm sj` 1-Pass) | 3.8 Flash + SJ (Best-of-Waves) |
| --- | --- | --- | --- | --- |
| **Solved Tasks** | `54 / 107` (`50.5%`) | `61 / 107` (`57.0%`) | **`88 / 107` (`82.2%`)** | **`105 / 107` (`98.1%`)** |
| **Macro Capability** | `0.8273` | `0.8981` | **`0.9526`** | **`0.9965`** |
| **API (`n=10`)** | `7 / 10` (`70.0%`) | `6 / 10` (`60.0%`) | **`8 / 10` (`80.0%`)** | **`10 / 10` (`100.0%`)** |
| **Browser (`n=28`)** | `15 / 28` (`53.6%`) | `16 / 28` (`57.1%`) | **`24 / 28` (`85.7%`)** | **`28 / 28` (`100.0%`)** |
| **CLI (`n=53`)** | `25 / 53` (`47.2%`) | `30 / 53` (`56.6%`) | **`43 / 53` (`81.1%`)** | **`51 / 53` (`96.2%`)** |
| **File (`n=16`)** | `7 / 16` (`43.8%`) | `9 / 16` (`56.2%`) | **`13 / 16` (`81.2%`)** | **`16 / 16` (`100.0%`)** |
| **Total Suite Steps** | `4,800` (med `37.0`) | `6,907` (med `56.0`) | **`6,176` (med `50.0`)** | **`6,104` (med `49.0`)** |
| **Turns per Solved Task** | `88.9` | `113.2` | **`70.2` (`-21.0%` vs 3.7)** | **`58.1` (`-34.6%` vs 3.7)** |
| **Total Tokens** | `~129.0M` (cal.) | `589.29M` | **`281.98M` (`-52.1%` vs raw)** | **`278.70M` (`-52.7%` vs raw)** |
| **Uncached Input Tokens** | — | `139.59M` (`76.1%` hit) | **`43.95M` (`-68.5%`, `84.1%` hit)** | **`40.68M` (`-70.9%`, `85.1%` hit)** |
| **Suite Cost (USD)** | `~$107.00` (cal.) | `$310.29` | **`$143.53` (`-53.7%` vs raw)** | **`$137.64` (`-55.6%` vs raw)** |
| **Cost per Solved Task** | `~$1.98 / solve` | `$5.09 / solve` | **`$1.63 / solve` (`-17.7%` vs 3.7)** | **`$1.31 / solve` (`-33.8%` vs 3.7)** |
| **Wall Time per Solved Task** | `10.2 min / solve` | `17.6 min / solve` | **`12.1 min / solve` (`-31.2%` vs raw)** | **`9.8 min / solve` (`-4.2%` vs 3.7)** |

---

## 6. Operational Guardrails for Agents

1. **Keep model sampling/thinking parameters untouched**: Do not pass `--thinking-level` or `--max-output-tokens` when reproducing main results; both arms run at native model defaults.
2. **Use `bwrap` on Linux**: Keep `bubblewrap` installed so every trial runs with `/task` mounted at root, isolated `/tmp`, and masked sibling trial directories.
3. **Disk space during 107-task runs**: Each trial stages mock service state under `<workdir>/runs/<task>__<arm>_meta/mock_services`. Ensure at least `15 GB` of free disk space before launching a full 214-trial paired run.
4. **Per-configuration resume**: Completed trials in `--workdir` are automatically reused if their `config_fingerprint` matches (`result.json`). If a trial had a transient grader timeout (`grading_error`), re-running the same command re-grades the existing outputs in place without making new model calls.
