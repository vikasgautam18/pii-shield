#!/usr/bin/env bash
# ── 05-teardown-apps.sh ──────────────────────────────────────────────────────
# Delete out-of-band Azure resources that were created by 02-deploy-apps.sh
# but are NOT tracked in Terraform state. Run this BEFORE `terraform destroy`,
# otherwise destroy will fail with errors like:
#
#   ManagedEnvironmentHasContainerApps: The specified environment
#   <project>-env cannot be deleted because it still contains N ContainerApps
#
# What this script removes:
#   - All Container Apps inside the ACA Managed Environment
#   - Redis data-plane access-policy-assignments for those apps' identities
#     (Redis itself is destroyed by Terraform; its policy assignments disappear
#      with it, so this is optional/cosmetic)
#
# Usage:
#   cd infra/scripts && ./05-teardown-apps.sh
#   # then:
#   cd ../terraform && terraform destroy
#
# Env overrides:
#   RG_NAME, ACA_ENV, REDIS_NAME  (otherwise read from Terraform outputs)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

tf_out() {
  (cd "$TF_DIR" && terraform output -raw "$1" 2>/dev/null) || true
}

RG_NAME="${RG_NAME:-$(tf_out resource_group_name)}"
ACA_ENV="${ACA_ENV:-$(tf_out aca_environment_name)}"
if [[ -z "${REDIS_NAME:-}" ]]; then
  REDIS_HOST=$(tf_out redis_hostname)
  REDIS_NAME="${REDIS_HOST%%.*}"
fi

if [[ -z "$RG_NAME" ]]; then
  echo "ERROR: Could not determine RG_NAME. Either:"
  echo "  - Run from a directory with terraform state available, or"
  echo "  - Set RG_NAME env var before running this script"
  exit 1
fi

echo "Target resource group: $RG_NAME"
[[ -n "$ACA_ENV" ]]    && echo "ACA environment:       $ACA_ENV"
[[ -n "$REDIS_NAME" ]] && echo "Redis cache:           $REDIS_NAME"
echo ""

# ── 1. Container Apps ────────────────────────────────────────────────────────

echo "== Container Apps =="
CAPPS=$(az containerapp list -g "$RG_NAME" --query "[].name" -o tsv 2>/dev/null || true)

if [[ -z "$CAPPS" ]]; then
  echo "  (none found)"
else
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    echo "  Deleting container app: $name"
    az containerapp delete -g "$RG_NAME" -n "$name" --yes --only-show-errors >/dev/null \
      && echo "    [OK]" \
      || echo "    [FAIL]"
  done <<< "$CAPPS"
fi
echo ""

# ── 2. Redis access-policy-assignments (best-effort) ─────────────────────────

if [[ -n "$REDIS_NAME" ]]; then
  echo "== Redis access-policy-assignments ($REDIS_NAME) =="
  ASSIGNMENTS=$(az redis access-policy-assignment list \
    -g "$RG_NAME" --name "$REDIS_NAME" \
    --query "[].name" -o tsv 2>/dev/null || true)

  if [[ -z "$ASSIGNMENTS" ]]; then
    echo "  (none found)"
  else
    while IFS= read -r pa; do
      [[ -z "$pa" ]] && continue
      echo "  Deleting policy assignment: $pa"
      az redis access-policy-assignment delete \
        -g "$RG_NAME" --name "$REDIS_NAME" \
        --access-policy-assignment-name "$pa" \
        --yes --only-show-errors >/dev/null \
        && echo "    [OK]" \
        || echo "    [skip — Redis may already be gone]"
    done <<< "$ASSIGNMENTS"
  fi
  echo ""
fi

echo "Done. You can now run: cd $TF_DIR && terraform destroy"
