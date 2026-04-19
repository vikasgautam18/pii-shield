#!/usr/bin/env bash
# run_local_nodocker.sh — Run PII Shield locally WITHOUT Docker.
#
# This script:
#   1. Checks Python version (3.10-3.12 required; presidio-analyzer 2.2.362 doesn't support 3.14)
#   2. Creates / activates a virtualenv at .venv
#   3. Installs pip dependencies
#   4. Downloads the spaCy NLP model matching $NLP_ENGINE
#   5. Generates an encryption key if missing
#   6. Starts Redis (native install) in the background — or uses $REDIS_URL if already set
#   7. Launches the FastAPI API, Streamlit playground, and Streamlit admin UI
#
# Usage:
#   ./scripts/run_local_nodocker.sh                  # full stack (API + both UIs)
#   ./scripts/run_local_nodocker.sh --api-only       # API only (no Streamlit)
#   ./scripts/run_local_nodocker.sh --no-redis       # assume REDIS_URL is set externally
#   ./scripts/run_local_nodocker.sh --stop           # stop processes started by this script
#   NLP_ENGINE=spacy ./scripts/run_local_nodocker.sh # override the NLP engine
#
# Requirements:
#   - Python 3.10, 3.11, or 3.12 (NOT 3.13+ yet; presidio-analyzer pin)
#   - redis-server on PATH (apt install redis-server | brew install redis | see below)
#
# Windows users: run this from WSL, or use the PowerShell equivalent.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
PID_DIR="${PID_DIR:-$REPO_ROOT/.run}"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/.run/logs}"
NLP_ENGINE="${NLP_ENGINE:-onnx}"   # matches .env.example default

API_PORT="${API_PORT:-8000}"
PLAYGROUND_PORT="${PLAYGROUND_PORT:-7860}"
ADMIN_PORT="${ADMIN_PORT:-7861}"
REDIS_PORT="${REDIS_PORT:-6379}"

MODE="full"                        # full | api-only
USE_REDIS="true"

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --api-only)  MODE="api-only";  shift ;;
    --no-redis)  USE_REDIS="false"; shift ;;
    --stop)      MODE="stop";      shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$PID_DIR" "$LOG_DIR"

# ── Helpers ──────────────────────────────────────────────────────────────────
log()   { printf "\033[1;34m[%s]\033[0m %s\n" "$(date +%H:%M:%S)" "$*"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
fail()  { printf "\033[1;31m[FAIL]\033[0m %s\n" "$*"; exit 1; }
ok()    { printf "\033[1;32m[ok]\033[0m %s\n" "$*"; }

stop_pid() {
  local name="$1"
  local pidfile="$PID_DIR/${name}.pid"
  if [[ -f "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      log "Stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
}

# ── Stop mode ────────────────────────────────────────────────────────────────
if [[ "$MODE" == "stop" ]]; then
  for name in api playground admin redis; do
    stop_pid "$name"
  done
  ok "Stopped all local services."
  exit 0
fi

# ── 1. Python version check ──────────────────────────────────────────────────
log "Checking Python..."
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    major=${version%%.*}
    minor=${version##*.}
    if [[ "$major" -eq 3 && "$minor" -ge 10 && "$minor" -le 12 ]]; then
      PYTHON="$candidate"
      log "  Using $candidate (version $version)"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  fail "Need Python 3.10, 3.11, or 3.12 on PATH. Found none compatible.
  Ubuntu/Debian:   sudo apt install python3.12 python3.12-venv
  macOS:           brew install python@3.12
  Windows (WSL):   use the apt command above"
fi

# ── 2. Virtualenv ────────────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating virtualenv at $VENV_DIR..."
  "$PYTHON" -m venv "$VENV_DIR" || fail "venv creation failed.
  On Debian/Ubuntu you may need:  sudo apt install ${PYTHON}-venv"
fi
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ── 3. Dependencies ──────────────────────────────────────────────────────────
STAMP="$VENV_DIR/.deps_installed_$(md5sum requirements.txt | cut -c1-8)"
if [[ ! -f "$STAMP" ]]; then
  log "Installing dependencies (this takes a few minutes on first run)..."
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt
  pip install -e .
  touch "$STAMP"
  ok "Dependencies installed."
else
  log "Dependencies already installed (stamp: $(basename "$STAMP"))."
fi

# ── 4. Env file (must come before model download — NLP_ENGINE may be set here) ──
if [[ ! -f .env ]]; then
  log "Creating .env from .env.example..."
  cp .env.example .env
  # Generate a Fernet key in case someone switches to ENCRYPTION_BACKEND=fernet later
  KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  # macOS sed and GNU sed differ — use portable form
  if command -v gsed >/dev/null 2>&1; then SED=gsed; else SED=sed; fi
  $SED -i.bak "s|^PII_SHIELD_ENCRYPTION_KEY=.*|PII_SHIELD_ENCRYPTION_KEY=$KEY|" .env && rm -f .env.bak
  ok ".env created with generated PII_SHIELD_ENCRYPTION_KEY"
fi

# Source .env so NLP_ENGINE / TRANSFORMERS_MODEL / QUANTIZED_MODEL_DIR / etc.
# are visible to both this script AND the child uvicorn/streamlit processes.
# Shell-level NLP_ENGINE (set before invoking the script) wins over .env.
log "Loading .env into the shell environment..."
PRE_NLP_ENGINE="${NLP_ENGINE:-}"
set -a
# shellcheck source=/dev/null
source .env
set +a
# Restore caller's NLP_ENGINE override if they set one explicitly.
if [[ -n "$PRE_NLP_ENGINE" ]]; then
  NLP_ENGINE="$PRE_NLP_ENGINE"
  export NLP_ENGINE
fi
NLP_ENGINE="${NLP_ENGINE:-onnx}"
export NLP_ENGINE
ok "NLP_ENGINE=$NLP_ENGINE"

# ── 5. NLP model ─────────────────────────────────────────────────────────────
log "Ensuring NLP model is available (NLP_ENGINE=$NLP_ENGINE)..."
case "$NLP_ENGINE" in
  spacy)
    python -c "import spacy; spacy.load('en_core_web_lg')" 2>/dev/null \
      || python -m spacy download en_core_web_lg
    ;;
  onnx)
    # Tokenizer model
    python -c "import spacy; spacy.load('en_core_web_sm')" 2>/dev/null \
      || python -m spacy download en_core_web_sm
    # Pre-download + INT8 quantize the ONNX NER model so the API doesn't
    # block on first request (and avoids silent FP32 fallback).
    TRANSFORMERS_MODEL="${TRANSFORMERS_MODEL:-protectai/bert-base-NER-onnx}"
    QUANTIZED_MODEL_DIR="${QUANTIZED_MODEL_DIR:-models/onnx-int8}"
    QUANTIZE_MODEL="${QUANTIZE_MODEL:-true}"
    if [[ "$QUANTIZE_MODEL" == "true" && ! -f "$QUANTIZED_MODEL_DIR/model_quantized.onnx" ]]; then
      log "Quantized ONNX model not found at $QUANTIZED_MODEL_DIR — building it now (one-time, ~1-2 min)..."
      mkdir -p "$(dirname "$QUANTIZED_MODEL_DIR")"
      python scripts/quantize_model.py "$TRANSFORMERS_MODEL" "$QUANTIZED_MODEL_DIR" \
        || warn "Quantization failed — engine will fall back to FP32 download at runtime."
    elif [[ "$QUANTIZE_MODEL" == "true" ]]; then
      log "  Quantized model already present at $QUANTIZED_MODEL_DIR."
    else
      # Pre-download the FP32 ONNX model into the HF cache.
      log "  Pre-downloading FP32 ONNX model $TRANSFORMERS_MODEL into HF cache..."
      python -c "from optimum.onnxruntime import ORTModelForTokenClassification; from transformers import AutoTokenizer; ORTModelForTokenClassification.from_pretrained('$TRANSFORMERS_MODEL'); AutoTokenizer.from_pretrained('$TRANSFORMERS_MODEL')" \
        || warn "Could not pre-download $TRANSFORMERS_MODEL — will be retried at runtime."
    fi
    ;;
  transformers)
    python -c "import spacy; spacy.load('en_core_web_sm')" 2>/dev/null \
      || python -m spacy download en_core_web_sm
    TRANSFORMERS_MODEL="${TRANSFORMERS_MODEL:-dslim/bert-base-NER}"
    log "  Pre-downloading transformers model $TRANSFORMERS_MODEL into HF cache..."
    python -c "from transformers import AutoModelForTokenClassification, AutoTokenizer; AutoModelForTokenClassification.from_pretrained('$TRANSFORMERS_MODEL'); AutoTokenizer.from_pretrained('$TRANSFORMERS_MODEL')" \
      || warn "Could not pre-download $TRANSFORMERS_MODEL — will be retried at runtime."
    ;;
  stanza)
    python -c "import stanza; stanza.download('en')" 2>/dev/null || true
    ;;
  *)
    warn "Unknown NLP_ENGINE=$NLP_ENGINE — skipping model download."
    ;;
esac
ok "NLP model ready."

# ── 6. Redis ─────────────────────────────────────────────────────────────────
if [[ "$USE_REDIS" == "true" ]]; then
  if command -v redis-cli >/dev/null 2>&1 && redis-cli -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG; then
    ok "Redis already running on port $REDIS_PORT."
  elif command -v redis-server >/dev/null 2>&1; then
    log "Starting redis-server on port $REDIS_PORT..."
    redis-server --port "$REDIS_PORT" --daemonize no \
      >"$LOG_DIR/redis.log" 2>&1 &
    echo $! > "$PID_DIR/redis.pid"
    sleep 1
    redis-cli -p "$REDIS_PORT" ping | grep -q PONG && ok "Redis started."
  else
    fail "redis-server not installed. Install it first:
  Ubuntu/Debian:  sudo apt install redis-server
  macOS (brew):   brew install redis
  Fedora/RHEL:    sudo dnf install redis
  Windows:        use WSL or Memurai (https://www.memurai.com)
  Or run:         ./scripts/run_local_nodocker.sh --no-redis REDIS_URL=redis://yourhost:6379/0"
  fi
  export REDIS_URL="${REDIS_URL:-redis://localhost:$REDIS_PORT/0}"
else
  [[ -n "${REDIS_URL:-}" ]] || fail "--no-redis requires REDIS_URL to be set in the environment."
  ok "Using external REDIS_URL=$REDIS_URL"
fi

# ── 7. Launch apps ───────────────────────────────────────────────────────────
log "Starting FastAPI API on port $API_PORT..."
nohup uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT" \
  >"$LOG_DIR/api.log" 2>&1 &
echo $! > "$PID_DIR/api.pid"
sleep 2

# Health check
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://localhost:$API_PORT/health" >/dev/null 2>&1; then
    ok "API is healthy → http://localhost:$API_PORT"
    break
  fi
  sleep 2
done

if [[ "$MODE" == "full" ]]; then
  log "Starting Streamlit playground on port $PLAYGROUND_PORT..."
  API_BASE="http://localhost:$API_PORT" \
  nohup streamlit run app/streamlit_app.py \
    --server.port "$PLAYGROUND_PORT" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --client.toolbarMode minimal \
    >"$LOG_DIR/playground.log" 2>&1 &
  echo $! > "$PID_DIR/playground.pid"

  log "Starting Streamlit admin on port $ADMIN_PORT..."
  API_BASE="http://localhost:$API_PORT" \
  nohup streamlit run app/streamlit_admin.py \
    --server.port "$ADMIN_PORT" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --client.toolbarMode minimal \
    >"$LOG_DIR/admin.log" 2>&1 &
  echo $! > "$PID_DIR/admin.pid"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "==============================================================="
echo "  PII Shield is running locally (no Docker)"
echo "==============================================================="
echo "  API:           http://localhost:$API_PORT"
echo "  Swagger docs:  http://localhost:$API_PORT/docs"
if [[ "$MODE" == "full" ]]; then
echo "  Playground:    http://localhost:$PLAYGROUND_PORT"
echo "  Admin UI:      http://localhost:$ADMIN_PORT"
fi
echo ""
echo "  Logs:          $LOG_DIR/"
echo "  Stop all:      ./scripts/run_local_nodocker.sh --stop"
echo "==============================================================="
