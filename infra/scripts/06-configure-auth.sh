#!/usr/bin/env bash
# ── 06-configure-auth.sh ──────────────────────────────────────────────────────
# Turn authentication on or off for ALREADY-DEPLOYED container apps without
# redeploying the image. One switch (AUTH_ENABLED) covers the API and both UIs;
# this script applies it to all three so they never disagree.
#
# Prerequisites:
#   - Apps deployed by 02-deploy-apps.sh from an image that contains the UI launcher
#     (their command is `python -m app.ui_launcher <ui>`)
#   - Azure CLI logged in with the containerapp extension
#
# Usage:
#   OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant>/v2.0 \
#   OIDC_AUDIENCE=<api-app-client-id> \
#   OIDC_CLIENT_ID=<ui-client-id> OIDC_CLIENT_SECRET=... AUTH_COOKIE_SECRET=... \
#   OIDC_API_SCOPE=api://<api-app-client-id>/access_as_user \
#     ./06-configure-auth.sh enable
#
#   ./06-configure-auth.sh disable
#
# Optional (same names as 02-deploy-apps.sh): OIDC_ROLES_CLAIM AUTH_USER_ROLES AUTH_ADMIN_ROLES
#   OIDC_ISSUER OIDC_JWKS_URL OIDC_ALGORITHMS OIDC_CLOCK_SKEW_SECONDS OIDC_JWKS_CACHE_SECONDS
#   OIDC_ALLOW_INSECURE_HTTP OIDC_UI_SCOPES OIDC_SERVER_METADATA_URL OIDC_PROMPT
#   PLAYGROUND_REDIRECT_URI / ADMIN_REDIRECT_URI   default: https://<app fqdn>/oauth2callback
#   PLAYGROUND_MAX_REPLICAS / ADMIN_MAX_REPLICAS   restored on `disable` (default 3 / 2)
#
# Rollout order keeps every intermediate state usable:
#   enable : UIs first (they require sign-in; the API is still open), then the API
#   disable: API first (open), then the UIs
# Each update creates a new revision, so the services restart.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  enable|disable) ;;
  *) echo "Usage: $0 enable|disable" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

API_APP_NAME="${API_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw api_app_name 2>/dev/null || echo "pii-shield")}"
PLAYGROUND_APP_NAME="${PLAYGROUND_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw playground_app_name 2>/dev/null || echo "playground")}"
ADMIN_APP_NAME="${ADMIN_APP_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw admin_app_name 2>/dev/null || echo "pii-admin")}"
RG_NAME="${RG_NAME:-$(cd "$TF_DIR" 2>/dev/null && terraform output -raw resource_group_name 2>/dev/null || echo "")}"
PLAYGROUND_MAX_REPLICAS="${PLAYGROUND_MAX_REPLICAS:-3}"
ADMIN_MAX_REPLICAS="${ADMIN_MAX_REPLICAS:-2}"

if [ -z "$RG_NAME" ]; then
  echo "ERROR: Could not determine the resource group. Set RG_NAME or run terraform apply first." >&2
  exit 1
fi

az config set extension.use_dynamic_install=yes_without_prompt 2>/dev/null || true
az extension add --name containerapp --upgrade --allow-preview true -y 2>/dev/null || true

app_fqdn() {  # $1 = app name
  az containerapp show --name "$1" --resource-group "$RG_NAME" \
    --query properties.configuration.ingress.fqdn -o tsv
}

require_launcher() {  # $1 = app name
  local command
  command=$(az containerapp show --name "$1" --resource-group "$RG_NAME" \
    --query "properties.template.containers[0].command" -o tsv 2>/dev/null || true)
  if ! printf '%s' "$command" | grep -q 'app.ui_launcher'; then
    echo "ERROR: $1 still runs the legacy 'streamlit run' command." >&2
    echo "       Redeploy it from an image containing app/ui_launcher.py (02-deploy-apps.sh)" >&2
    echo "       before changing authentication; afterwards this script can toggle it freely." >&2
    exit 1
  fi
}

if [ "$ACTION" = "disable" ]; then
  echo "Disabling authentication (API first, then the UIs)..."
  az containerapp update --name "$API_APP_NAME" --resource-group "$RG_NAME" \
    --set-env-vars AUTH_ENABLED=false --output none
  az containerapp update --name "$PLAYGROUND_APP_NAME" --resource-group "$RG_NAME" \
    --max-replicas "$PLAYGROUND_MAX_REPLICAS" --set-env-vars AUTH_ENABLED=false --output none
  az containerapp update --name "$ADMIN_APP_NAME" --resource-group "$RG_NAME" \
    --max-replicas "$ADMIN_MAX_REPLICAS" --set-env-vars AUTH_ENABLED=false --output none
  echo "Authentication disabled on $API_APP_NAME, $PLAYGROUND_APP_NAME and $ADMIN_APP_NAME."
  echo "(OIDC_* settings and secrets are left in place but ignored while AUTH_ENABLED=false.)"
  exit 0
fi

# ── enable ───────────────────────────────────────────────────────────────────

missing=()
for var in OIDC_ISSUER_URL OIDC_AUDIENCE OIDC_CLIENT_ID OIDC_CLIENT_SECRET AUTH_COOKIE_SECRET; do
  if [ -z "${!var:-}" ]; then missing+=("$var"); fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "ERROR: enabling authentication requires: ${missing[*]}" >&2
  exit 1
fi
if [ "${#AUTH_COOKIE_SECRET}" -lt 32 ]; then
  echo "ERROR: AUTH_COOKIE_SECRET must be at least 32 characters." >&2
  echo "       Generate one with: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"" >&2
  exit 1
fi

require_launcher "$PLAYGROUND_APP_NAME"
require_launcher "$ADMIN_APP_NAME"

PLAYGROUND_REDIRECT_URI="${PLAYGROUND_REDIRECT_URI:-https://$(app_fqdn "$PLAYGROUND_APP_NAME")/oauth2callback}"
ADMIN_REDIRECT_URI="${ADMIN_REDIRECT_URI:-https://$(app_fqdn "$ADMIN_APP_NAME")/oauth2callback}"

api_env=("AUTH_ENABLED=true" "OIDC_ISSUER_URL=$OIDC_ISSUER_URL" "OIDC_AUDIENCE=$OIDC_AUDIENCE")
for var in OIDC_ROLES_CLAIM AUTH_USER_ROLES AUTH_ADMIN_ROLES OIDC_ISSUER OIDC_JWKS_URL \
           OIDC_ALGORITHMS OIDC_CLOCK_SKEW_SECONDS OIDC_JWKS_CACHE_SECONDS OIDC_ALLOW_INSECURE_HTTP; do
  if [ -n "${!var:-}" ]; then api_env+=("$var=${!var}"); fi
done

configure_ui() {  # $1 = app name, $2 = redirect URI
  local app="$1" redirect_uri="$2" var
  local ui_env=(
    "AUTH_ENABLED=true"
    "OIDC_ISSUER_URL=$OIDC_ISSUER_URL"
    "OIDC_CLIENT_ID=$OIDC_CLIENT_ID"
    "OIDC_CLIENT_SECRET=secretref:oidc-client-secret"
    "AUTH_COOKIE_SECRET=secretref:auth-cookie-secret"
    "UI_REDIRECT_URI=$redirect_uri"
  )
  for var in OIDC_API_SCOPE OIDC_UI_SCOPES OIDC_SERVER_METADATA_URL OIDC_PROMPT OIDC_ALLOW_INSECURE_HTTP; do
    if [ -n "${!var:-}" ]; then ui_env+=("$var=${!var}"); fi
  done

  echo "Configuring $app..."
  az containerapp secret set --name "$app" --resource-group "$RG_NAME" \
    --secrets "oidc-client-secret=$OIDC_CLIENT_SECRET" "auth-cookie-secret=$AUTH_COOKIE_SECRET" \
    --output none
  # Streamlit keeps sign-in state in process memory: run a single replica (see docs/authentication.md).
  az containerapp update --name "$app" --resource-group "$RG_NAME" \
    --max-replicas 1 --set-env-vars "${ui_env[@]}" --output none
}

echo "Enabling authentication (UIs first, then the API)..."
configure_ui "$PLAYGROUND_APP_NAME" "$PLAYGROUND_REDIRECT_URI"
configure_ui "$ADMIN_APP_NAME" "$ADMIN_REDIRECT_URI"
echo "Configuring $API_APP_NAME..."
az containerapp update --name "$API_APP_NAME" --resource-group "$RG_NAME" \
  --set-env-vars "${api_env[@]}" --output none

echo ""
echo "Authentication enabled (issuer: $OIDC_ISSUER_URL)."
echo "Make sure your identity provider allows these redirect and post-logout URIs:"
echo "  $PLAYGROUND_REDIRECT_URI"
echo "  $ADMIN_REDIRECT_URI"
echo "API callers now need 'Authorization: Bearer <access token>'."
echo "Verify with:  AUTH_ENABLED=true VERIFY_ACCESS_TOKEN=<token> ./03-verify.sh"
