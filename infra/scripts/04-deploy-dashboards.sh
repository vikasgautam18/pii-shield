#!/usr/bin/env bash
# ── 04-deploy-dashboards.sh ─────────────────────────────────────────────────
# Upload PII Shield Grafana dashboards to Azure Managed Grafana.
#
# Prerequisites:
#   - Azure Managed Grafana provisioned (terraform apply)
#   - Azure CLI logged in with amg extension
#   - Current user has Grafana Admin role
#
# Usage:
#   cd infra/scripts && ./04-deploy-dashboards.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$SCRIPT_DIR/../.."
DASHBOARD_DIR="$REPO_ROOT/observability/grafana/dashboards-azure"

# ── Ensure amg extension installed ───────────────────────────────────────────

az config set extension.dynamic_install_allow_preview=true  2>/dev/null
az config set extension.use_dynamic_install=yes_without_prompt  2>/dev/null
az extension add --name amg --upgrade --allow-preview true -y 2>/dev/null || true

# ── Configuration ────────────────────────────────────────────────────────────

# Read RG and Grafana names from Terraform outputs by default (override via env vars)
SCRIPT_DIR_TF="$SCRIPT_DIR/../terraform"
RG_NAME="${RG_NAME:-$(cd "$SCRIPT_DIR_TF" 2>/dev/null && terraform output -raw resource_group_name 2>/dev/null || echo "")}"
GRAFANA_NAME="${GRAFANA_NAME:-$(cd "$SCRIPT_DIR_TF" 2>/dev/null && terraform output -raw grafana_name 2>/dev/null || echo "")}"
FOLDER_NAME="${FOLDER_NAME:-PII Shield}"

if [[ -z "$RG_NAME" || -z "$GRAFANA_NAME" ]]; then
  echo "ERROR: Could not determine RG_NAME or GRAFANA_NAME. Either:"
  echo "  - Run 'terraform apply' first (outputs will be read automatically), or"
  echo "  - Set RG_NAME and GRAFANA_NAME env vars before running this script"
  exit 1
fi

echo " Deploying Grafana dashboards..."
echo "   Grafana:   $GRAFANA_NAME"
echo "   RG:        $RG_NAME"
echo "   Folder:    $FOLDER_NAME"
echo ""

# ── Get Grafana endpoint ─────────────────────────────────────────────────────

GRAFANA_URL=$(az grafana show --name "$GRAFANA_NAME" --resource-group "$RG_NAME" \
  --query "properties.endpoint" -o tsv 2>/dev/null) || true

if [[ -z "$GRAFANA_URL" ]]; then
  echo "[FAIL] Cannot find Grafana instance '$GRAFANA_NAME' in '$RG_NAME'."
  echo "   Run 'terraform apply' first to provision Azure Managed Grafana."
  exit 1
fi

echo "   URL:       $GRAFANA_URL"
echo ""

# ── Create dashboard folder ─────────────────────────────────────────────────

echo " Creating folder '$FOLDER_NAME'..."
az grafana folder create \
  --name "$GRAFANA_NAME" \
  --resource-group "$RG_NAME" \
  --title "$FOLDER_NAME" \
  2>/dev/null || echo "   (folder already exists)"

# ── Resolve Azure resource references for templating ────────────────────────
# The dashboard JSON files reference a specific subscription/RG/App Insights
# from when they were authored. We rewrite those references to match the
# current deployment before uploading.

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
APP_INSIGHTS_NAME=$( (cd "$SCRIPT_DIR_TF" && terraform output -raw app_insights_name) 2>/dev/null || true)
if [[ -z "$APP_INSIGHTS_NAME" ]]; then
  APP_INSIGHTS_NAME=$(az resource list -g "$RG_NAME" \
    --resource-type "Microsoft.Insights/components" --query "[0].name" -o tsv 2>/dev/null || true)
fi
if [[ -z "$APP_INSIGHTS_NAME" ]]; then
  echo "[FAIL] Could not determine App Insights name. Set APP_INSIGHTS_NAME env var."
  exit 1
fi

# Sentinel placeholder values that exist in the committed dashboard JSON files.
# These are intentionally NOT real Azure identifiers — they are rewritten to
# the live deployment values just before upload. Keep them in sync with the
# strings used in observability/grafana/dashboards-azure/*.json.
SRC_SUBSCRIPTION="00000000-0000-0000-0000-000000000000"
SRC_RG="__SOURCE_RG__"
SRC_APPI="__SOURCE_APPI__"

echo "   Templating dashboards:"
echo "     subscription:  $SRC_SUBSCRIPTION  →  $SUBSCRIPTION_ID"
echo "     resource grp:  $SRC_RG  →  $RG_NAME"
echo "     app insights:  $SRC_APPI  →  $APP_INSIGHTS_NAME"
echo ""

TMP_DASH_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DASH_DIR"' EXIT

# ── Upload dashboards ───────────────────────────────────────────────────────

for DASHBOARD_FILE in "$DASHBOARD_DIR"/*.json; do
  BASENAME=$(basename "$DASHBOARD_FILE")
  TEMPLATED="$TMP_DASH_DIR/$BASENAME"

  # Order matters: replace App Insights name first (most specific), then RG,
  # then subscription — to avoid partial collisions. Also strip the `version`
  # field so Grafana doesn't reject re-uploads with "version-mismatch".
  python3 - "$DASHBOARD_FILE" "$TEMPLATED" \
    "$SRC_APPI" "$APP_INSIGHTS_NAME" \
    "$SRC_RG" "$RG_NAME" \
    "$SRC_SUBSCRIPTION" "$SUBSCRIPTION_ID" <<'PY'
import json, sys
src, dst, sa, da, sr, dr, ss, ds = sys.argv[1:9]
text = open(src).read().replace(sa, da).replace(
    f"/resourceGroups/{sr}/", f"/resourceGroups/{dr}/").replace(ss, ds)
data = json.loads(text)
data.pop("version", None)
data.pop("id", None)  # let Grafana assign a fresh internal id too
data.pop("uid", None) # avoid version-mismatch on re-uploads
json.dump(data, open(dst, "w"))
PY

  TITLE=$(python3 -c "import json; print(json.load(open('$TEMPLATED'))['title'])")

  echo " Uploading: $TITLE ($BASENAME)..."
  az grafana dashboard create \
    --name "$GRAFANA_NAME" \
    --resource-group "$RG_NAME" \
    --folder "$FOLDER_NAME" \
    --title "$TITLE" \
    --definition @"$TEMPLATED" \
    2>/dev/null && echo " [PASS] $TITLE uploaded" \
    || echo " [WARN] Failed to upload $TITLE — try importing manually via Grafana UI"
done

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Grafana Dashboards Deployed"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Grafana URL:  $GRAFANA_URL"
echo "  Folder:       $FOLDER_NAME"
echo ""
echo "  Dashboards:"
for DASHBOARD_FILE in "$DASHBOARD_DIR"/*.json; do
  TITLE=$(python3 -c "import json; print(json.load(open('$DASHBOARD_FILE'))['title'])")
  echo "    - $TITLE"
done
echo ""
echo "  NOTE: On first load, you may need to select the correct"
echo "  Prometheus datasource in each dashboard's settings."
echo "═══════════════════════════════════════════════════════════════"
