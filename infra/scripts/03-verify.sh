#!/usr/bin/env bash
# ── 03-verify.sh ──────────────────────────────────────────────────────────────
# Verify PII Shield deployment on Azure Container Apps.
# Runs health checks and smoke tests against the deployed services.
#
# Prerequisites:
#   - Container apps deployed (02-deploy-apps.sh)
#   - Azure CLI logged in
#
# Usage:
#   cd infra/scripts && ./03-verify.sh
#
# Override defaults via env vars:
#   RG_NAME=my-rg API_APP_NAME=my-api ./03-verify.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

# Container app names (override via env vars; defaults come from Terraform outputs)
API_APP_NAME="${API_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw api_app_name 2>/dev/null || echo "pii-shield")}"
PLAYGROUND_APP_NAME="${PLAYGROUND_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw playground_app_name 2>/dev/null || echo "playground")}"
ADMIN_APP_NAME="${ADMIN_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw admin_app_name 2>/dev/null || echo "pii-admin")}"

PASS=0
FAIL=0

check() {
  local label="$1"
  local url="$2"
  local expected_status="${3:-200}"

  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$url" 2>/dev/null || echo "000")
  if [[ "$status" == "$expected_status" ]]; then
    echo "  [PASS] $label (HTTP $status)"
    ((PASS++))
  else
    echo "  [FAIL] $label (HTTP $status, expected $expected_status)"
    ((FAIL++))
  fi
}

# ── Get FQDNs ────────────────────────────────────────────────────────────────

# Prefer Terraform output; fall back to RG_NAME env var
RG_NAME="${RG_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw resource_group_name 2>/dev/null || echo "")}"

if [[ -z "$RG_NAME" ]]; then
  echo "ERROR: Could not determine resource group. Set RG_NAME env var or run 'terraform apply' first."
  exit 1
fi

echo "Fetching service URLs..."
API_FQDN=$(az containerapp show --name "$API_APP_NAME" --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)
PLAYGROUND_FQDN=$(az containerapp show --name "$PLAYGROUND_APP_NAME" --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)
ADMIN_FQDN=$(az containerapp show --name "$ADMIN_APP_NAME" --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)

API_URL="https://$API_FQDN"
PLAYGROUND_URL="https://$PLAYGROUND_FQDN"
ADMIN_URL="https://$ADMIN_FQDN"

echo ""
echo "  API:           $API_URL"
echo "  Playground:    $PLAYGROUND_URL"
echo "  Admin:         $ADMIN_URL"
echo ""

# ── Health checks ────────────────────────────────────────────────────────────

echo "Health checks..."
check "API /health"          "$API_URL/health"
check "Playground reachable" "$PLAYGROUND_URL"
check "Admin reachable"      "$ADMIN_URL"

# ── API smoke tests ──────────────────────────────────────────────────────────

echo ""
echo "API smoke tests..."

# List apps (should return empty list or existing apps)
check "GET /apps" "$API_URL/apps"

# Test anonymization
echo -n "  "
ANON_RESPONSE=$(curl -s -w "\n%{http_code}" --max-time 30 \
  -X POST "$API_URL/anonymize_unique" \
  -H "Content-Type: application/json" \
  -d '{"text": "My name is John Doe and my email is john@example.com", "language": "en"}' \
  2>/dev/null)

ANON_STATUS=$(echo "$ANON_RESPONSE" | tail -1)
ANON_BODY=$(echo "$ANON_RESPONSE" | head -n -1)

if [[ "$ANON_STATUS" == "200" ]]; then
  echo "[PASS] POST /anonymize_unique (HTTP 200)"
  ((PASS++))
  # Check that PII was replaced
  if echo "$ANON_BODY" | grep -q "anonymized_text"; then
    echo "  [PASS] Response contains anonymized_text"
    ((PASS++))
  else
    echo "  [FAIL] Response missing anonymized_text"
    ((FAIL++))
  fi
else
  echo "[FAIL] POST /anonymize_unique (HTTP $ANON_STATUS)"
  ((FAIL++))
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "==============================================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "==============================================================="

if [[ $FAIL -gt 0 ]]; then
  exit 1
fi
