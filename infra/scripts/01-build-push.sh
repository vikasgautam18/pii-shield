#!/usr/bin/env bash
# ── 01-build-push.sh ─────────────────────────────────────────────────────────
# Build the PII Shield Docker image in Azure Container Registry (cloud build).
# Reads ACR details from Terraform outputs.
#
# Prerequisites:
#   - Terraform applied (infra/terraform)
#   - Azure CLI logged in
#
# Usage:
#   cd infra/scripts && ./01-build-push.sh [--nlp-engine ENGINE] [--model MODEL]
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"
REPO_ROOT="$SCRIPT_DIR/../.."

# Defaults (match .env)
NLP_ENGINE="${NLP_ENGINE:-onnx}"
TRANSFORMERS_MODEL="${TRANSFORMERS_MODEL:-protectai/bert-base-NER-onnx}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Parse CLI args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --nlp-engine)   NLP_ENGINE="$2"; shift 2 ;;
    --model)        TRANSFORMERS_MODEL="$2"; shift 2 ;;
    --tag)          IMAGE_TAG="$2"; shift 2 ;;
    *)              echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Read Terraform outputs
echo " Reading Terraform outputs..."
ACR_NAME=$(cd "$TF_DIR" && terraform output -raw acr_login_server)
RG_NAME=$(cd "$TF_DIR" && terraform output -raw resource_group_name)

echo " Building image in ACR: ${ACR_NAME}/pii-shield:${IMAGE_TAG}"
echo "   NLP_ENGINE=$NLP_ENGINE"
echo "   TRANSFORMERS_MODEL=$TRANSFORMERS_MODEL"
echo ""

az acr build \
  --registry "${ACR_NAME%%.*}" \
  --resource-group "$RG_NAME" \
  --image "pii-shield:${IMAGE_TAG}" \
  --build-arg "NLP_ENGINE=${NLP_ENGINE}" \
  --build-arg "TRANSFORMERS_MODEL=${TRANSFORMERS_MODEL}" \
  "$REPO_ROOT"

echo ""
echo "[PASS] Image pushed: ${ACR_NAME}/pii-shield:${IMAGE_TAG}"
