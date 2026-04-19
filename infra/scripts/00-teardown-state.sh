#!/usr/bin/env bash
# 00-teardown-state.sh — Remove the Terraform state backend resources
#
# Removes everything provisioned by 00-bootstrap-state.sh:
#   - CanNotDelete lock on the state resource group
#   - Storage account (and its blob container + RBAC role assignments)
#   - Resource group itself
#
# WARNING: This permanently deletes your Terraform state. Make sure no active
# deployments rely on it (run 'terraform destroy' first if there are live resources).
#
# Usage:
#   ./00-teardown-state.sh --project-name <short-name>
#   ./00-teardown-state.sh --resource-group <state-rg> [--storage-account <name>]
#   ./00-teardown-state.sh --project-name piishield --yes   # skip confirmation
#
# When --project-name is given, the resource group is assumed to be
# "<project>-tfstate-rg". Override with --resource-group if different.

set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
PROJECT_NAME=""
STATE_RG=""
STORAGE_ACCOUNT=""
LOCK_NAME="${LOCK_NAME:-protect-tfstate}"
ASSUME_YES="false"

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --project-name)     PROJECT_NAME="$2";    shift 2 ;;
    --resource-group)   STATE_RG="$2";        shift 2 ;;
    --storage-account)  STORAGE_ACCOUNT="$2"; shift 2 ;;
    --lock-name)        LOCK_NAME="$2";       shift 2 ;;
    -y|--yes)           ASSUME_YES="true";    shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)  echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Derive RG name from project name if not explicitly given
if [[ -n "$PROJECT_NAME" && -z "$STATE_RG" ]]; then
  if ! [[ "$PROJECT_NAME" =~ ^[a-z0-9]{3,16}$ ]]; then
    echo "ERROR: --project-name must be 3-16 lowercase alphanumeric chars"
    exit 1
  fi
  STATE_RG="${PROJECT_NAME}-tfstate-rg"
fi

if [[ -z "$STATE_RG" ]]; then
  echo "ERROR: either --project-name or --resource-group is required"
  echo "  --project-name piishield               (assumes RG = piishield-tfstate-rg)"
  echo "  --resource-group my-tfstate-rg"
  exit 1
fi

# Verify the resource group exists
if ! az group show --name "$STATE_RG" --output none 2>/dev/null; then
  echo "ERROR: resource group '$STATE_RG' not found (already deleted?)"
  exit 1
fi

# Auto-detect storage account if not provided
if [[ -z "$STORAGE_ACCOUNT" ]]; then
  STORAGE_ACCOUNT=$(az storage account list \
    --resource-group "$STATE_RG" \
    --query "[0].name" -o tsv 2>/dev/null || true)
fi

echo "==============================================================="
echo "  Terraform State Backend - TEARDOWN"
echo "==============================================================="
echo ""
echo "  Resource Group:    $STATE_RG"
echo "  Storage Account:   ${STORAGE_ACCOUNT:-<none found>}"
echo "  Lock to remove:    $LOCK_NAME"
echo ""
echo "  WARNING: This permanently deletes the Terraform state files."
echo "  Make sure you have run 'terraform destroy' on any live infrastructure"
echo "  that depends on this state before proceeding."
echo ""

if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "Type the resource group name '$STATE_RG' to confirm deletion: " CONFIRM
  if [[ "$CONFIRM" != "$STATE_RG" ]]; then
    echo "Aborted (input did not match)."
    exit 1
  fi
fi

# ── 1. Remove the resource group lock ────────────────────────────────────────
echo "> Removing lock '$LOCK_NAME' on resource group..."
LOCK_ID=$(az lock show \
  --name "$LOCK_NAME" \
  --resource-group "$STATE_RG" \
  --query id -o tsv 2>/dev/null || true)

if [[ -n "$LOCK_ID" ]]; then
  az lock delete --ids "$LOCK_ID" --output none
  echo "  [ok] Lock removed"
else
  echo "  (no lock found - already removed or never created)"
fi

# Also check for any locks at the storage account scope (defensive cleanup)
if [[ -n "$STORAGE_ACCOUNT" ]]; then
  SA_ID=$(az storage account show \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$STATE_RG" \
    --query id -o tsv 2>/dev/null || true)
  if [[ -n "$SA_ID" ]]; then
    SA_LOCKS=$(az lock list --resource "$SA_ID" --query "[].id" -o tsv 2>/dev/null || true)
    if [[ -n "$SA_LOCKS" ]]; then
      echo "> Removing locks on storage account..."
      while IFS= read -r LID; do
        [[ -n "$LID" ]] && az lock delete --ids "$LID" --output none && echo "  [ok] Removed $LID"
      done <<< "$SA_LOCKS"
    fi
  fi
fi

# ── 2. Delete the resource group (cascades to all child resources) ──────────
echo "> Deleting resource group '$STATE_RG' (this may take a few minutes)..."

# Lock removal can take 15-60s to propagate. Retry the delete a few times.
MAX_DELETE_ATTEMPTS=6
DELETE_ATTEMPT=0
while (( DELETE_ATTEMPT < MAX_DELETE_ATTEMPTS )); do
  if az group delete --name "$STATE_RG" --yes --no-wait 2>/dev/null; then
    echo "  [ok] Delete request accepted"
    break
  fi
  DELETE_ATTEMPT=$((DELETE_ATTEMPT+1))
  if (( DELETE_ATTEMPT < MAX_DELETE_ATTEMPTS )); then
    echo "  ... lock propagation pending, retrying in 15s ($DELETE_ATTEMPT/$MAX_DELETE_ATTEMPTS)"
    sleep 15
  fi
done

if (( DELETE_ATTEMPT >= MAX_DELETE_ATTEMPTS )); then
  echo "  [FAIL] Could not delete resource group after $MAX_DELETE_ATTEMPTS attempts."
  echo "  Check for remaining locks:  az lock list --resource-group $STATE_RG -o table"
  exit 1
fi

echo ""
echo "==============================================================="
echo "  [PASS] Teardown initiated"
echo ""
echo "  Resource group deletion is running asynchronously."
echo "  Monitor with:"
echo "    az group show --name $STATE_RG --query properties.provisioningState -o tsv"
echo ""
echo "  When complete (returns 'ResourceGroupNotFound'), all of these are gone:"
echo "    - Resource group: $STATE_RG"
echo "    - Storage account: ${STORAGE_ACCOUNT:-<auto-detected>}"
echo "    - Blob container, RBAC role assignments, locks"
echo "==============================================================="
