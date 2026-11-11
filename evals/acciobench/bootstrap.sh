#!/usr/bin/env bash
# One-command reproducible setup for AccioBench (CommerceAgentBench x Straitjacket).
#
# Sets up:
#   1. Python virtual environment + Straitjacket (`ctx`) + Playwright + grader deps
#   2. CommerceAgentBench repository checkout (107 domain tasks + bench_core)
#   3. Headless Chromium for browser_* tasks
#   4. Runtime directory (Bun, logistics_tracker Express deps, and domain mock_services
#      extracted from the pinned official AccioBench container image when Docker is present)
#   5. Model-free `acciobench_run.py doctor` verification

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DATASET_ROOT="${ACCIOBENCH_DATASET_ROOT:-${REPO_ROOT}/.acciobench/CommerceAgentBench}"
RUNTIME_DIR="${ACCIOBENCH_RUNTIME_DIR:-${REPO_ROOT}/.acciobench/runtime}"
VENV_DIR="${ACCIOBENCH_VENV:-${REPO_ROOT}/.venv-acciobench}"
CAB_REPO_URL="https://github.com/Accio-org/CommerceAgentBench.git"
CAB_IMAGE="acciolyk/accio_bench@sha256:1e9cf5c72a56794175b7d06ece036b92e296e6b7e9e9a7fa244026f6acea3859"

USE_VENV=1
SKIP_DOCKER=0
SKIP_BROWSERS=0

usage() {
  cat <<'EOF'
Usage: bash evals/acciobench/bootstrap.sh [options]

Options:
  --dataset-root PATH   Where to clone/find CommerceAgentBench (default: ./.acciobench/CommerceAgentBench)
  --runtime-dir PATH    Where to stage mock_services, bun, and JS deps (default: ./.acciobench/runtime)
  --venv PATH           Python virtualenv path (default: ./.venv-acciobench)
  --no-venv             Install Python dependencies into the active Python environment
  --skip-docker         Skip extracting /opt/mock_services from the official Docker image
  --skip-browsers       Skip `playwright install chromium`
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset-root)
      DATASET_ROOT="$2"
      shift 2
      ;;
    --runtime-dir)
      RUNTIME_DIR="$2"
      shift 2
      ;;
    --venv)
      VENV_DIR="$2"
      shift 2
      ;;
    --no-venv)
      USE_VENV=0
      shift
      ;;
    --skip-docker)
      SKIP_DOCKER=1
      shift
      ;;
    --skip-browsers)
      SKIP_BROWSERS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[ERROR] python3 (>= 3.11) is required on PATH." >&2
  exit 1
fi

"${PYTHON_BIN}" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"[ERROR] Python >= 3.11 required; found {sys.version.split()[0]}")
PY

if [[ "${USE_VENV}" -eq 1 ]]; then
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "==> Creating virtualenv at ${VENV_DIR}"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  fi
  PY="${VENV_DIR}/bin/python"
  PIP="${VENV_DIR}/bin/pip"
else
  PY="${PYTHON_BIN}"
  PIP="${PYTHON_BIN} -m pip"
fi

echo "==> Installing Straitjacket (ctx-harness) and benchmark Python packages"
${PIP} install --upgrade pip >/dev/null
${PIP} install -e "${REPO_ROOT}[dev,image]" \
  playwright \
  "pandas>=2.0" \
  "openpyxl>=3.1" \
  "PyYAML>=6.0" \
  "pypdf>=4.0" \
  "pdfplumber>=0.10" \
  "pymupdf>=1.23" \
  "beautifulsoup4>=4.12" \
  "lxml>=5.0"

mkdir -p "$(dirname "${DATASET_ROOT}")"
if [[ ! -d "${DATASET_ROOT}/datasets_domain_v1" ]]; then
  echo "==> Cloning CommerceAgentBench into ${DATASET_ROOT}"
  git clone --depth 1 "${CAB_REPO_URL}" "${DATASET_ROOT}"
else
  echo "==> Found existing CommerceAgentBench at ${DATASET_ROOT}"
fi
${PIP} install -e "${DATASET_ROOT}"

if [[ "${SKIP_BROWSERS}" -eq 0 ]]; then
  echo "==> Installing headless Chromium via Playwright"
  "${PY}" -m playwright install chromium
fi

mkdir -p "${RUNTIME_DIR}/bin" "${RUNTIME_DIR}/mock_services" "${RUNTIME_DIR}/logistics_tracker_deps"

# Ensure bun is available in RUNTIME_DIR/bin or PATH (used by Bun-backed mock services & CLIs)
if command -v bun >/dev/null 2>&1; then
  ln -sf "$(command -v bun)" "${RUNTIME_DIR}/bin/bun"
elif [[ ! -x "${RUNTIME_DIR}/bin/bun" ]]; then
  if command -v curl >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1; then
    echo "==> Installing Bun into ${RUNTIME_DIR}/bun"
    BUN_INSTALL="${RUNTIME_DIR}/bun" curl -fsSL https://bun.sh/install | bash
    ln -sf "${RUNTIME_DIR}/bun/bin/bun" "${RUNTIME_DIR}/bin/bun"
  else
    echo "[WARN] bun not found and curl/unzip unavailable; install Bun from https://bun.sh for CLI/HTTP mocks."
  fi
fi

if command -v node >/dev/null 2>&1; then
  ln -sf "$(command -v node)" "${RUNTIME_DIR}/bin/node"
fi

# Install Express + cookie-parser for logistics_tracker service tasks
cat > "${RUNTIME_DIR}/logistics_tracker_deps/package.json" <<'JSON'
{
  "name": "logistics-tracker-mock",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "express": "^4.18.2",
    "cookie-parser": "^1.4.6"
  }
}
JSON

if [[ ! -d "${RUNTIME_DIR}/logistics_tracker_deps/node_modules/express" ]]; then
  if command -v npm >/dev/null 2>&1; then
    echo "==> Installing logistics_tracker Node dependencies"
    (cd "${RUNTIME_DIR}/logistics_tracker_deps" && npm install --no-audit --no-fund --silent)
  elif [[ -x "${RUNTIME_DIR}/bin/bun" ]]; then
    echo "==> Installing logistics_tracker dependencies via bun"
    (cd "${RUNTIME_DIR}/logistics_tracker_deps" && "${RUNTIME_DIR}/bin/bun" install --silent)
  else
    echo "[WARN] Neither npm nor bun found; skipping logistics_tracker_deps install."
  fi
fi

# Populate mock_services from CommerceAgentBench repo + pinned Docker image (if available)
if [[ -d "${DATASET_ROOT}/real_replica_bench/mock_services" ]]; then
  cp -rn "${DATASET_ROOT}/real_replica_bench/mock_services/." "${RUNTIME_DIR}/mock_services/" 2>/dev/null || true
fi

if [[ "${SKIP_DOCKER}" -eq 0 && ! -f "${RUNTIME_DIR}/mock_services/registry.py" ]]; then
  if command -v docker >/dev/null 2>&1; then
    echo "==> Extracting /opt/mock_services from pinned official image (${CAB_IMAGE})"
    CID="$(docker create --platform linux/amd64 "${CAB_IMAGE}" true)"
    trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT
    docker cp "${CID}:/opt/mock_services/." "${RUNTIME_DIR}/mock_services/"
    docker rm -f "${CID}" >/dev/null 2>&1 || true
    trap - EXIT
  else
    echo "[WARN] Docker not detected; skipped extracting /opt/mock_services from ${CAB_IMAGE}."
    echo "       Pure file/spreadsheet/PDF tasks work immediately; stateful CLI/API/browser mock tasks require runtime_dir/mock_services."
  fi
fi

if ! command -v pdftotext >/dev/null 2>&1; then
  echo "[WARN] pdftotext not found on PATH. Install poppler-utils (Ubuntu/Debian) or poppler (macOS) for PDF tasks."
fi

if [[ -f "${REPO_ROOT}/evals/acciobench_run.py" ]]; then
  echo "==> Running model-free environment doctor"
  "${PY}" "${REPO_ROOT}/evals/acciobench_run.py" doctor \
    --dataset-root "${DATASET_ROOT}" \
    --runtime-dir "${RUNTIME_DIR}" || true
fi

cat <<EOF

Bootstrap complete!
  Dataset root : ${DATASET_ROOT}
  Runtime dir  : ${RUNTIME_DIR}
  Python bin   : ${PY}

Next steps:
  1. Prove the referee (model-free, \$0 API cost):
     ${PY} evals/acciobench_run.py validate \\
       --dataset-root "${DATASET_ROOT}" \\
       --runtime-dir "${RUNTIME_DIR}" \\
       --limit 2

  2. Run a paired A/B benchmark (raw baseline vs Straitjacket sj):
     export GEMINI_API_KEY="..."
     ${PY} evals/acciobench_run.py run \\
       --dataset-root "${DATASET_ROOT}" \\
       --runtime-dir "${RUNTIME_DIR}" \\
       --workdir ./.acciobench/runs_ab \\
       --provider gemini \\
       --judge-provider gemini \\
       --judge-model gemini-3.1-pro-preview \\
       --arm raw,sj \\
       --sample 10 \\
       --workers 4

  3. Render stratified A/B report:
     ${PY} evals/acciobench_run.py report \\
       --workdir ./.acciobench/runs_ab \\
       --format markdown
EOF
