#!/usr/bin/env bash
# 00-bootstrap-state.sh — Create Azure Storage Account for Terraform remote state
#
# Run this ONCE before 'terraform init'. It creates a dedicated resource group,
# storage account, and blob container for state files, secured with Entra ID auth,
# TLS-only, no public blob access, and a delete lock.
#
# Usage:
#   ./00-bootstrap-state.sh --project-name <short-name>
#   ./00-bootstrap-state.sh --storage-account <globally-unique-name>
#   ./00-bootstrap-state.sh --project-name piishield --location eastus
#
# Either --project-name or --storage-account is required.
# When --project-name is given, the storage account name is auto-generated as
# "<project>tf<random>" (globally unique) and the resource group as "<project>-tfstate-rg".
#
# After this completes, run 'terraform init' with the printed -backend-config flags.

set -euo pipefail

# ── Defaults (override via flags) ────────────────────────────────────────────
PROJECT_NAME=""
STATE_RG=""
LOCATION="${LOCATION:-centralindia}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-}"
CONTAINER_NAME="${CONTAINER_NAME:-tfstate}"
STATE_KEY="${STATE_KEY:-}"

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --project-name)       PROJECT_NAME="$2";     shift 2 ;;
    --location)           LOCATION="$2";         shift 2 ;;
    --storage-account)    STORAGE_ACCOUNT="$2";  shift 2 ;;
    --resource-group)     STATE_RG="$2";         shift 2 ;;
    --container)          CONTAINER_NAME="$2";   shift 2 ;;
    --state-key)          STATE_KEY="$2";        shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)  echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# Derive names from project name if provided
if [[ -n "$PROJECT_NAME" ]]; then
  if ! [[ "$PROJECT_NAME" =~ ^[a-z0-9]{3,16}$ ]]; then
    echo "ERROR: --project-name must be 3-16 lowercase alphanumeric chars"
    exit 1
  fi
  # Random 5-char alphanumeric suffix for global uniqueness
  # (use $RANDOM to avoid SIGPIPE issues with tr|head under pipefail)
  RAND_SUFFIX=$(printf '%05x' $((RANDOM * RANDOM % 1048576)))
  STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-${PROJECT_NAME}tf${RAND_SUFFIX}}"
  STATE_RG="${STATE_RG:-${PROJECT_NAME}-tfstate-rg}"
  STATE_KEY="${STATE_KEY:-${PROJECT_NAME}.terraform.tfstate}"
fi

# Final defaults if still unset
STATE_RG="${STATE_RG:-tfstate-rg}"
STATE_KEY="${STATE_KEY:-pii-shield.terraform.tfstate}"

if [[ -z "$STORAGE_ACCOUNT" ]]; then
  echo "ERROR: either --project-name or --storage-account is required"
  echo "  --project-name piishield          (auto-generates a globally-unique storage account)"
  echo "  --storage-account mytfstate12345  (3-24 lowercase alphanumeric chars, globally unique)"
  exit 1
fi

# Validate storage account name
if ! [[ "$STORAGE_ACCOUNT" =~ ^[a-z0-9]{3,24}$ ]]; then
  echo "ERROR: storage account name must be 3-24 lowercase alphanumeric chars (got: $STORAGE_ACCOUNT)"
  exit 1
fi

echo "==============================================================="
echo "  Terraform State Backend - Bootstrap"
echo "==============================================================="
echo ""
echo "  Resource Group:    $STATE_RG"
echo "  Storage Account:   $STORAGE_ACCOUNT"
echo "  Container:         $CONTAINER_NAME"
echo "  State key:         $STATE_KEY"
echo "  Location:          $LOCATION"
echo ""

# ── 0. Register required Azure resource providers ────────────────────────────
# Container Apps, Grafana, etc. are not registered by default in new subscriptions.
echo "> Registering Azure resource providers (idempotent; takes ~1-2 min on first run)..."
PROVIDERS=(
  Microsoft.Storage
  Microsoft.App
  Microsoft.ContainerRegistry
  Microsoft.Cache
  Microsoft.Dashboard
  Microsoft.Insights
  Microsoft.OperationalInsights
  Microsoft.AlertsManagement
  Microsoft.Authorization
)
for p in "${PROVIDERS[@]}"; do
  state=$(az provider show --namespace "$p" --query registrationState -o tsv 2>/dev/null || echo "NotRegistered")
  if [[ "$state" != "Registered" ]]; then
    echo "    registering $p (current state: $state)..."
    az provider register --namespace "$p" --output none
  fi
done
# Wait for registration to complete (each provider takes seconds to ~minutes)
for p in "${PROVIDERS[@]}"; do
  for i in $(seq 1 60); do
    state=$(az provider show --namespace "$p" --query registrationState -o tsv 2>/dev/null || echo "Unknown")
    [[ "$state" == "Registered" ]] && break
    sleep 5
  done
  if [[ "$state" != "Registered" ]]; then
    echo "WARNING: $p is still in state '$state' after 5 minutes — terraform apply may fail."
  fi
done
echo "  All required providers registered."
echo ""

# ── 1. Resource group ────────────────────────────────────────────────────────
echo "> Creating resource group '$STATE_RG'..."
az group create \
  --name "$STATE_RG" \
  --location "$LOCATION" \
  --output none

# ── 2. Storage account ──────────────────────────────────────────────────────
echo "> Creating storage account '$STORAGE_ACCOUNT'..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --https-only true \
  --output none

# ── 3. Entra ID RBAC — Storage Blob Data Contributor ────────────────────────
# Assigned BEFORE creating the container so that blob-data operations work.
echo "> Granting 'Storage Blob Data Contributor' to current user..."
PRINCIPAL_ID=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
if [[ -z "$PRINCIPAL_ID" ]]; then
  echo "  [FAIL] Could not detect signed-in user. Run 'az login' and retry."
  exit 1
fi

SCOPE=$(az storage account show \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --query id -o tsv)

az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$PRINCIPAL_ID" \
  --scope "$SCOPE" \
  --output none 2>/dev/null || echo "  (role may already be assigned)"
echo "  [ok] RBAC role granted to $PRINCIPAL_ID"

# ── 4. Wait for RBAC propagation ────────────────────────────────────────────
# Azure RBAC takes 1-5 minutes to propagate. Poll the data plane until it works.
echo "> Waiting for RBAC propagation (this can take 1-5 minutes)..."
MAX_ATTEMPTS=30   # 30 * 10s = 5 minutes
ATTEMPT=0
while (( ATTEMPT < MAX_ATTEMPTS )); do
  if az storage container list \
       --account-name "$STORAGE_ACCOUNT" \
       --auth-mode login \
       --output none 2>/dev/null; then
    echo "  [ok] RBAC propagated (attempt $((ATTEMPT+1)))"
    break
  fi
  ATTEMPT=$((ATTEMPT+1))
  printf "  ... attempt %d/%d, waiting 10s\r" "$ATTEMPT" "$MAX_ATTEMPTS"
  sleep 10
done
echo ""

if (( ATTEMPT >= MAX_ATTEMPTS )); then
  echo "  [WARN] RBAC did not propagate within 5 minutes."
  echo "  Continuing anyway — you may need to retry 'terraform init' in a few minutes."
fi

# ── 5. Blob container ───────────────────────────────────────────────────────
echo "> Creating blob container '$CONTAINER_NAME'..."
az storage container create \
  --name "$CONTAINER_NAME" \
  --account-name "$STORAGE_ACCOUNT" \
  --auth-mode login \
  --output none 2>/dev/null || echo "  (container may already exist)"
echo "  [ok] Container ready"

# ── 5. Disable shared key access ────────────────────────────────────────────
echo "> Disabling storage account key access (Entra ID only)..."
az storage account update \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$STATE_RG" \
  --allow-shared-key-access false \
  --output none

# ── 6. Delete lock ──────────────────────────────────────────────────────────
echo "> Adding CanNotDelete lock..."
az lock create \
  --name "protect-tfstate" \
  --resource-group "$STATE_RG" \
  --lock-type CanNotDelete \
  --output none 2>/dev/null || true

echo ""
echo "==============================================================="
echo "  [PASS] State backend ready!"
echo ""
echo "  Next steps:"
echo "    cd infra/terraform"
echo "    cp terraform.tfvars.example terraform.tfvars  # then edit values"
echo ""
echo "    terraform init \\"
echo "      -backend-config=\"resource_group_name=$STATE_RG\" \\"
echo "      -backend-config=\"storage_account_name=$STORAGE_ACCOUNT\" \\"
echo "      -backend-config=\"container_name=$CONTAINER_NAME\" \\"
echo "      -backend-config=\"key=$STATE_KEY\" \\"
echo "      -backend-config=\"use_azuread_auth=true\""
echo ""
echo "    terraform plan"
echo "    terraform apply"
echo "==============================================================="
