#!/usr/bin/env bash
# ── 07-setup-entra.sh ────────────────────────────────────────────────────────
# Create (or remove) the Microsoft Entra ID objects PII Shield needs to run with authentication
# enabled. Meant for a test or demo tenant: it creates test users and test services.
#
# `create` is idempotent. It sets up:
#   1. One app registration, "PII Shield (<project>)", that is both the API and the sign-in
#      client for the Playground and Admin UIs: single tenant, v2 access tokens, delegated
#      scope access_as_user, and the app roles PIIShield.User, PIIShield.Admin and
#      PIIShield.Viewer (a role the app does not recognise, for checking the "no access"
#      screen). "Assignment required" is on, so only assigned users and services can sign in
#      or obtain tokens.
#   2. A client secret for the UIs. The app is pre-authorised for its own scope (no consent
#      prompt) and has admin consent for the OpenID Connect scopes (openid, profile, email).
#   3. Two test services (client credentials): one with the user role, one with the admin role.
#   4. Four test users: user, admin, viewer (unrecognised role) and unassigned. The signed-in
#      user gets the admin role (OPERATOR_ROLE) so you can sign in to both UIs yourself.
#   5. A private env file (mode 0600, outside the repository) with every value that
#      02-deploy-apps.sh, 06-configure-auth.sh and scripts/get_token.py need. Secrets are
#      written only there and are never printed.
#
# Prerequisites: Azure CLI logged in as a user who can manage applications, users and app
# role assignments (for example Global Administrator), and python3.
#
# Usage:
#   ./07-setup-entra.sh create    # needs the Container Apps environment (for redirect URIs)
#   ./07-setup-entra.sh show      # what exists (no secrets)
#   ./07-setup-entra.sh delete    # remove everything this script created
#
#   # then, to turn authentication on for the deployed apps:
#   set -a; . ~/.config/pii-shield/<project>.entra.env; set +a
#   ./06-configure-auth.sh enable
#
# Environment (all optional):
#   PROJECT_NAME            default: Terraform output project_name, else piishield
#   RG_NAME, ACA_ENV        resource group and Container Apps environment (default: Terraform outputs)
#   PLAYGROUND_APP_NAME, ADMIN_APP_NAME   default: Terraform outputs
#   PLAYGROUND_REDIRECT_URI, ADMIN_REDIRECT_URI
#                           default: https://<app>.<environment default domain>/oauth2callback
#   TEST_USER_DOMAIN        default: the tenant's default domain
#   ENTRA_ENV_FILE          default: ~/.config/pii-shield/<project>.entra.env
#   OPERATOR_ROLE           Admin (default), User or none: role given to the signed-in user
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ACTION="${1:-}"
case "$ACTION" in
  create|show|delete) ;;
  *) echo "Usage: $0 create|show|delete" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"
GRAPH="https://graph.microsoft.com/v1.0"
MS_GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v az >/dev/null 2>&1 || die "Azure CLI (az) is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."

tf_out() { (cd "$TF_DIR" 2>/dev/null && terraform output -raw "$1" 2>/dev/null) || true; }

PROJECT_NAME="${PROJECT_NAME:-$(tf_out project_name)}"
PROJECT_NAME="${PROJECT_NAME:-piishield}"
API_DISPLAY="PII Shield (${PROJECT_NAME})"
SVC_USER_DISPLAY="PII Shield test service, user role (${PROJECT_NAME})"
SVC_ADMIN_DISPLAY="PII Shield test service, admin role (${PROJECT_NAME})"
ENV_FILE="${ENTRA_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/pii-shield/${PROJECT_NAME}.entra.env}"
OPERATOR_ROLE="${OPERATOR_ROLE:-Admin}"

# ── Helpers ──────────────────────────────────────────────────────────────────

# Value stored in the private env file (empty when absent).
envget() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1='\\(.*\\)'\$/\\1/p" "$ENV_FILE" | head -n 1
}

# Microsoft Graph through the Azure CLI. Paths start with "/".
gget() {  # gget <path> [jmespath]  -> tsv when a query is given, otherwise json
  if [ $# -ge 2 ]; then
    az rest --method get --url "$GRAPH$1" --query "$2" -o tsv
  else
    az rest --method get --url "$GRAPH$1" -o json
  fi
}
gpost()  { az rest --method post   --url "$GRAPH$1" --body "$2" -o none; }
gpatch() { az rest --method patch  --url "$GRAPH$1" --body "$2" -o none; }
gdel()   { az rest --method delete --url "$GRAPH$1" -o none; }

# New Entra objects take a few seconds to replicate, so retry the calls that depend on them.
# Only errors that look like replication delay or throttling are retried; anything else fails at once.
RETRYABLE='ResourceNotFound|does not exist|not found|temporar|throttl|Too Many Requests|ServiceUnavailable|replicat'

retry() {  # retry <attempts> <delay seconds> command...
  local attempts="$1" delay="$2" i out=""
  shift 2
  for i in $(seq 1 "$attempts"); do
    if out=$("$@" 2>&1); then return 0; fi
    if ! printf '%s' "$out" | grep -qiE "$RETRYABLE"; then break; fi
    if [ "$i" -lt "$attempts" ]; then sleep "$delay"; fi
  done
  printf '%s\n' "$out" >&2
  return 1
}

retry_out() {  # like retry, but prints the command's stdout on success
  local attempts="$1" delay="$2" i out="" err errfile
  shift 2
  errfile=$(mktemp)
  for i in $(seq 1 "$attempts"); do
    if out=$("$@" 2>"$errfile"); then rm -f "$errfile"; printf '%s' "$out"; return 0; fi
    err=$(cat "$errfile")
    if ! printf '%s' "$err" | grep -qiE "$RETRYABLE"; then break; fi
    if [ "$i" -lt "$attempts" ]; then sleep "$delay"; fi
  done
  cat "$errfile" >&2
  rm -f "$errfile"
  return 1
}

uuid5() { python3 -c 'import sys, uuid; print(uuid.uuid5(uuid.NAMESPACE_URL, sys.argv[1]))' "$1"; }
gen_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

read -r -d '' PY_PASSWORD <<'PY' || true
import secrets, string
chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
         secrets.choice(string.digits), secrets.choice("!@#%^*-_=+")]
chars += [secrets.choice(string.ascii_letters + string.digits) for _ in range(20)]
secrets.SystemRandom().shuffle(chars)
print("".join(chars))
PY
gen_password() { python3 -c "$PY_PASSWORD"; }

# Body for PATCH /applications/{id}: everything about the API / sign-in app registration.
read -r -d '' PY_APP_BODY <<'PY' || true
import json, os
e = os.environ
app_id = e["APP_ID"]
body = {
    "displayName": e["API_DISPLAY"],
    "signInAudience": "AzureADMyOrg",
    "identifierUris": ["api://" + app_id],
    "api": {
        "requestedAccessTokenVersion": 2,
        "oauth2PermissionScopes": [{
            "id": e["SCOPE_ID"],
            "value": "access_as_user",
            "type": "User",
            "isEnabled": True,
            "adminConsentDisplayName": "Use PII Shield",
            "adminConsentDescription": "Allow the application to use PII Shield on behalf of the signed-in user.",
            "userConsentDisplayName": "Use PII Shield",
            "userConsentDescription": "Allow the application to use PII Shield on your behalf.",
        }],
    },
    "appRoles": [
        {"id": e["ROLE_USER_ID"], "value": "PIIShield.User", "displayName": "PII Shield User",
         "description": "Can anonymize and de-anonymize text and read registered applications.",
         "allowedMemberTypes": ["User", "Application"], "isEnabled": True},
        {"id": e["ROLE_ADMIN_ID"], "value": "PIIShield.Admin", "displayName": "PII Shield Admin",
         "description": "Can also register applications, edit their configuration and allow-lists, and read the audit log.",
         "allowedMemberTypes": ["User", "Application"], "isEnabled": True},
        {"id": e["ROLE_VIEWER_ID"], "value": "PIIShield.Viewer", "displayName": "PII Shield Viewer (no access)",
         "description": "A role PII Shield does not recognise. Used to check the 'no access' screen.",
         "allowedMemberTypes": ["User"], "isEnabled": True},
    ],
    "web": {
        "redirectUris": [u for u in e["REDIRECT_URIS"].split(",") if u],
        "implicitGrantSettings": {"enableIdTokenIssuance": False, "enableAccessTokenIssuance": False},
    },
    "requiredResourceAccess": [
        {"resourceAppId": e["MS_GRAPH_APP_ID"], "resourceAccess": [
            {"id": "37f7f235-527c-4136-accd-4a02d197296e", "type": "Scope"},
            {"id": "14dad69e-099b-42c9-810b-d002981feec1", "type": "Scope"},
            {"id": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0", "type": "Scope"},
        ]},
    ],
}
print(json.dumps(body))
PY

app_object_id()  { az ad app list --filter "displayName eq '$1'" --query "[0].id" -o tsv; }

ensure_app() {  # ensure_app <display name> -> sets APP_OBJ_ID and APP_ID (creating the registration if needed)
  local display="$1"
  APP_OBJ_ID=$(app_object_id "$display")
  if [ -z "$APP_OBJ_ID" ]; then
    echo "  creating app registration '$display'"
    APP_OBJ_ID=$(az ad app create --display-name "$display" --sign-in-audience AzureADMyOrg --query id -o tsv)
  else
    echo "  app registration '$display' already exists"
  fi
  APP_ID=$(az ad app show --id "$APP_OBJ_ID" --query appId -o tsv)
  [ -n "$APP_ID" ] || die "could not read the application id of '$display'"
}

ensure_sp() {  # ensure_sp <appId> -> prints the service principal object id
  local app_id="$1" sp
  sp=$(az ad sp list --filter "appId eq '$app_id'" --query "[0].id" -o tsv)
  if [ -z "$sp" ]; then
    sp=$(retry_out 6 10 az ad sp create --id "$app_id" --query id -o tsv) || die "could not create the service principal for $app_id"
  fi
  printf '%s' "$sp"
}

ensure_secret() {  # ensure_secret <env key> <appId> <label> -> prints the secret (reused from the env file when present)
  local key="$1" app_id="$2" label="$3" secret
  secret=$(envget "$key")
  if [ -z "$secret" ]; then
    secret=$(retry_out 6 10 az ad app credential reset --id "$app_id" --append --display-name "$label" --years 1 --query password -o tsv) \
      || die "could not create a client secret for $label"
  fi
  printf '%s' "$secret"
}

ensure_grant() {  # ensure_grant <client SP id> <resource SP id> <scope string>: tenant-wide (admin) consent
  local client="$1" resource="$2" scope="$3" id
  id=$(az rest --method get --url "$GRAPH/oauth2PermissionGrants" \
        --url-parameters "\$filter=clientId eq '$client' and resourceId eq '$resource'" \
        --query "value[0].id" -o tsv)
  if [ -n "$id" ]; then
    gpatch "/oauth2PermissionGrants/$id" "{\"scope\":\"$scope\"}"
  else
    retry 6 10 gpost "/oauth2PermissionGrants" \
      "{\"clientId\":\"$client\",\"consentType\":\"AllPrincipals\",\"resourceId\":\"$resource\",\"scope\":\"$scope\"}"
  fi
}

grant_service_role() {  # grant_service_role <service SP id> <API SP id> <role id>: application permission + consent
  local svc_sp="$1" api_sp="$2" role="$3" existing
  existing=$(gget "/servicePrincipals/$api_sp/appRoleAssignedTo" "value[?principalId=='$svc_sp' && appRoleId=='$role'].id | [0]")
  if [ -z "$existing" ]; then
    retry 6 10 gpost "/servicePrincipals/$api_sp/appRoleAssignedTo" \
      "{\"principalId\":\"$svc_sp\",\"resourceId\":\"$api_sp\",\"appRoleId\":\"$role\"}"
  fi
}

ensure_user_role() {  # ensure_user_role <user object id> <API SP id> <role id | none>: exactly one role, or none
  local user="$1" api_sp="$2" want="$3" existing aid arole have=false
  existing=$(gget "/users/$user/appRoleAssignments" "value[?resourceId=='$api_sp'].[id,appRoleId]")
  while IFS=$'\t' read -r aid arole; do
    [ -n "$aid" ] || continue
    if [ "$arole" = "$want" ]; then
      have=true
    else
      gdel "/users/$user/appRoleAssignments/$aid"
    fi
  done <<<"$existing"
  if [ "$want" != "none" ] && [ "$have" != "true" ]; then
    retry 6 10 gpost "/users/$user/appRoleAssignments" \
      "{\"principalId\":\"$user\",\"resourceId\":\"$api_sp\",\"appRoleId\":\"$want\"}"
  fi
}

ensure_user() {  # ensure_user <suffix> <display name> -> sets USER_OBJ_ID, USER_UPN and USER_PASSWORD
  local suffix="$1" display="$2"
  USER_UPN="${PROJECT_NAME}-test-${suffix}@${TEST_USER_DOMAIN}"
  USER_PASSWORD=$(envget "TEST_$(upper "$suffix")_PASSWORD")
  USER_OBJ_ID=$(az ad user list --filter "userPrincipalName eq '$USER_UPN'" --query "[0].id" -o tsv)
  if [ -z "$USER_OBJ_ID" ]; then
    [ -n "$USER_PASSWORD" ] || USER_PASSWORD=$(gen_password)
    echo "  creating user $USER_UPN"
    USER_OBJ_ID=$(az ad user create --display-name "$display" --user-principal-name "$USER_UPN" \
      --password "$USER_PASSWORD" --force-change-password-next-sign-in false --query id -o tsv)
  elif [ -z "$USER_PASSWORD" ]; then
    USER_PASSWORD=$(gen_password)
    echo "  resetting the password of $USER_UPN (it was not in $ENV_FILE)"
    az ad user update --id "$USER_OBJ_ID" --password "$USER_PASSWORD" --force-change-password-next-sign-in false
  else
    echo "  user $USER_UPN already exists"
  fi
}

resolve_tenant_domain() {
  TEST_USER_DOMAIN="${TEST_USER_DOMAIN:-$(gget /domains "value[?isDefault].id | [0]")}"
  [ -n "$TEST_USER_DOMAIN" ] || die "Could not determine the tenant's default domain. Set TEST_USER_DOMAIN."
}

resolve_redirect_uris() {
  if [ -n "${PLAYGROUND_REDIRECT_URI:-}" ] && [ -n "${ADMIN_REDIRECT_URI:-}" ]; then return 0; fi
  local rg aca_env pg ad domain
  rg="${RG_NAME:-$(tf_out resource_group_name)}"
  aca_env="${ACA_ENV:-$(tf_out aca_environment_name)}"
  pg="${PLAYGROUND_APP_NAME:-$(tf_out playground_app_name)}"
  ad="${ADMIN_APP_NAME:-$(tf_out admin_app_name)}"
  if [ -z "$rg" ] || [ -z "$aca_env" ] || [ -z "$pg" ] || [ -z "$ad" ]; then
    die "Cannot work out the redirect URIs. Set RG_NAME, ACA_ENV, PLAYGROUND_APP_NAME and ADMIN_APP_NAME (or have Terraform outputs available), or set PLAYGROUND_REDIRECT_URI and ADMIN_REDIRECT_URI."
  fi
  domain=$(az containerapp env show --name "$aca_env" --resource-group "$rg" --query properties.defaultDomain -o tsv)
  [ -n "$domain" ] || die "Could not read the default domain of Container Apps environment '$aca_env'."
  PLAYGROUND_REDIRECT_URI="${PLAYGROUND_REDIRECT_URI:-https://${pg}.${domain}/oauth2callback}"
  ADMIN_REDIRECT_URI="${ADMIN_REDIRECT_URI:-https://${ad}.${domain}/oauth2callback}"
}

emit() { printf "%s='%s'\n" "$1" "$2"; }

# ── create ───────────────────────────────────────────────────────────────────

do_create() {
  az account show >/dev/null 2>&1 || die "Not logged in. Run: az login"
  local tenant_id operator_id operator_role_id graph_sp_id
  tenant_id=$(az account show --query tenantId -o tsv)
  resolve_tenant_domain
  resolve_redirect_uris

  local role_user role_admin role_viewer scope_id
  role_user=$(uuid5 "pii-shield/${PROJECT_NAME}/role/PIIShield.User")
  role_admin=$(uuid5 "pii-shield/${PROJECT_NAME}/role/PIIShield.Admin")
  role_viewer=$(uuid5 "pii-shield/${PROJECT_NAME}/role/PIIShield.Viewer")
  scope_id=$(uuid5 "pii-shield/${PROJECT_NAME}/scope/access_as_user")

  echo "Microsoft Entra setup for '${PROJECT_NAME}' (tenant ${tenant_id}, domain ${TEST_USER_DOMAIN})"
  echo ""
  echo "1/5 API and sign-in app registration"
  ensure_app "$API_DISPLAY"
  local api_obj_id="$APP_OBJ_ID" api_app_id="$APP_ID" api_sp_id
  api_sp_id=$(ensure_sp "$api_app_id")
  retry 8 10 gpatch "/applications/$api_obj_id" "$(
    APP_ID="$api_app_id" API_DISPLAY="$API_DISPLAY" SCOPE_ID="$scope_id" \
    ROLE_USER_ID="$role_user" ROLE_ADMIN_ID="$role_admin" ROLE_VIEWER_ID="$role_viewer" \
    REDIRECT_URIS="$PLAYGROUND_REDIRECT_URI,$ADMIN_REDIRECT_URI" MS_GRAPH_APP_ID="$MS_GRAPH_APP_ID" \
    python3 -c "$PY_APP_BODY"
  )" || die "could not configure the app registration"
  # The app is its own sign-in client, so pre-authorise it for its own scope: no consent prompt.
  retry 6 10 gpatch "/applications/$api_obj_id" \
    "{\"api\":{\"preAuthorizedApplications\":[{\"appId\":\"$api_app_id\",\"delegatedPermissionIds\":[\"$scope_id\"]}]}}" \
    || die "could not pre-authorise the app for its own scope"
  retry 6 10 gpatch "/servicePrincipals/$api_sp_id" '{"appRoleAssignmentRequired":true}' \
    || die "could not turn on 'assignment required'"
  graph_sp_id=$(az ad sp list --filter "appId eq '$MS_GRAPH_APP_ID'" --query "[0].id" -o tsv)
  if [ -n "$graph_sp_id" ]; then
    ensure_grant "$api_sp_id" "$graph_sp_id" "openid profile email" || die "could not grant consent for openid profile email"
  fi
  local ui_secret cookie_secret
  ui_secret=$(ensure_secret OIDC_CLIENT_SECRET "$api_app_id" "${PROJECT_NAME}-ui")
  cookie_secret=$(envget AUTH_COOKIE_SECRET)
  [ -n "$cookie_secret" ] || cookie_secret=$(gen_secret)

  echo "2/5 test services (client credentials)"
  local svc_user_id svc_user_secret svc_admin_id svc_admin_secret svc_sp
  ensure_app "$SVC_USER_DISPLAY"
  svc_user_id="$APP_ID"
  svc_sp=$(ensure_sp "$svc_user_id")
  svc_user_secret=$(ensure_secret SVC_USER_CLIENT_SECRET "$svc_user_id" "${PROJECT_NAME}-service")
  grant_service_role "$svc_sp" "$api_sp_id" "$role_user"
  ensure_app "$SVC_ADMIN_DISPLAY"
  svc_admin_id="$APP_ID"
  svc_sp=$(ensure_sp "$svc_admin_id")
  svc_admin_secret=$(ensure_secret SVC_ADMIN_CLIENT_SECRET "$svc_admin_id" "${PROJECT_NAME}-service")
  grant_service_role "$svc_sp" "$api_sp_id" "$role_admin"

  echo "3/5 test users"
  local u_user_upn u_user_pw u_admin_upn u_admin_pw u_viewer_upn u_viewer_pw u_none_upn u_none_pw
  ensure_user user "PII Shield test user"
  ensure_user_role "$USER_OBJ_ID" "$api_sp_id" "$role_user"
  u_user_upn="$USER_UPN"; u_user_pw="$USER_PASSWORD"
  ensure_user admin "PII Shield test admin"
  ensure_user_role "$USER_OBJ_ID" "$api_sp_id" "$role_admin"
  u_admin_upn="$USER_UPN"; u_admin_pw="$USER_PASSWORD"
  ensure_user viewer "PII Shield test viewer (unrecognised role)"
  ensure_user_role "$USER_OBJ_ID" "$api_sp_id" "$role_viewer"
  u_viewer_upn="$USER_UPN"; u_viewer_pw="$USER_PASSWORD"
  ensure_user unassigned "PII Shield test unassigned"
  ensure_user_role "$USER_OBJ_ID" "$api_sp_id" none
  u_none_upn="$USER_UPN"; u_none_pw="$USER_PASSWORD"

  echo "4/5 role for the signed-in user (${OPERATOR_ROLE})"
  case "$OPERATOR_ROLE" in
    Admin) operator_role_id="$role_admin" ;;
    User)  operator_role_id="$role_user" ;;
    none)  operator_role_id="none" ;;
    *) die "OPERATOR_ROLE must be Admin, User or none (got '$OPERATOR_ROLE')." ;;
  esac
  operator_id=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
  if [ -n "$operator_id" ]; then
    ensure_user_role "$operator_id" "$api_sp_id" "$operator_role_id"
  else
    echo "  (could not identify the signed-in user; assign yourself a role in the portal)"
  fi

  echo "5/5 private env file"
  local dir tmp
  dir=$(dirname "$ENV_FILE")
  if [ ! -d "$dir" ]; then mkdir -p "$dir"; chmod 700 "$dir"; fi
  tmp=$(mktemp "$dir/.entra.XXXXXX")
  chmod 600 "$tmp"
  {
    echo "# Generated by infra/scripts/07-setup-entra.sh. Contains secrets: keep it private (mode 0600)."
    echo "# Load with:  set -a; . \"$ENV_FILE\"; set +a"
    emit ENTRA_TENANT_ID "$tenant_id"
    emit OIDC_ISSUER_URL "https://login.microsoftonline.com/${tenant_id}/v2.0"
    emit OIDC_AUDIENCE "$api_app_id"
    emit OIDC_CLIENT_ID "$api_app_id"
    emit OIDC_CLIENT_SECRET "$ui_secret"
    emit OIDC_API_SCOPE "api://${api_app_id}/access_as_user"
    emit AUTH_COOKIE_SECRET "$cookie_secret"
    emit PLAYGROUND_REDIRECT_URI "$PLAYGROUND_REDIRECT_URI"
    emit ADMIN_REDIRECT_URI "$ADMIN_REDIRECT_URI"
    emit SVC_USER_CLIENT_ID "$svc_user_id"
    emit SVC_USER_CLIENT_SECRET "$svc_user_secret"
    emit SVC_ADMIN_CLIENT_ID "$svc_admin_id"
    emit SVC_ADMIN_CLIENT_SECRET "$svc_admin_secret"
    emit TEST_USER_UPN "$u_user_upn"
    emit TEST_USER_PASSWORD "$u_user_pw"
    emit TEST_ADMIN_UPN "$u_admin_upn"
    emit TEST_ADMIN_PASSWORD "$u_admin_pw"
    emit TEST_VIEWER_UPN "$u_viewer_upn"
    emit TEST_VIEWER_PASSWORD "$u_viewer_pw"
    emit TEST_UNASSIGNED_UPN "$u_none_upn"
    emit TEST_UNASSIGNED_PASSWORD "$u_none_pw"
  } > "$tmp"
  mv "$tmp" "$ENV_FILE"

  echo ""
  echo "Done. Secrets and test-user passwords are in $ENV_FILE (mode 0600)."
  echo "  API / sign-in app:  $API_DISPLAY  (client id $api_app_id)"
  echo "  Redirect URIs:      $PLAYGROUND_REDIRECT_URI"
  echo "                      $ADMIN_REDIRECT_URI"
  echo "  Test users:         $u_user_upn (user), $u_admin_upn (admin),"
  echo "                      $u_viewer_upn (unrecognised role), $u_none_upn (unassigned)"
  echo "Enable authentication on the deployed apps with:"
  echo "  set -a; . \"$ENV_FILE\"; set +a; ./06-configure-auth.sh enable"
}

# ── show ─────────────────────────────────────────────────────────────────────

do_show() {
  az account show >/dev/null 2>&1 || die "Not logged in. Run: az login"
  resolve_tenant_domain
  echo "Project: ${PROJECT_NAME}   tenant domain: ${TEST_USER_DOMAIN}"
  if [ -f "$ENV_FILE" ]; then echo "Env file: $ENV_FILE (present)"; else echo "Env file: $ENV_FILE (absent)"; fi
  echo ""
  echo "App registrations (client ids):"
  local display id suffix upn user_id
  for display in "$API_DISPLAY" "$SVC_USER_DISPLAY" "$SVC_ADMIN_DISPLAY"; do
    id=$(az ad app list --filter "displayName eq '$display'" --query "[0].appId" -o tsv)
    printf '  %-62s %s\n' "$display" "${id:-(not found)}"
  done
  echo ""
  echo "Test users:"
  for suffix in user admin viewer unassigned; do
    upn="${PROJECT_NAME}-test-${suffix}@${TEST_USER_DOMAIN}"
    user_id=$(az ad user list --filter "userPrincipalName eq '$upn'" --query "[0].id" -o tsv)
    printf '  %-62s %s\n' "$upn" "$(if [ -n "$user_id" ]; then echo present; else echo '(not found)'; fi)"
  done
}

# ── delete ───────────────────────────────────────────────────────────────────

do_delete() {
  az account show >/dev/null 2>&1 || die "Not logged in. Run: az login"
  resolve_tenant_domain
  local suffix upn id display
  echo "Removing the Entra objects created for '${PROJECT_NAME}'..."
  for suffix in user admin viewer unassigned; do
    upn="${PROJECT_NAME}-test-${suffix}@${TEST_USER_DOMAIN}"
    if [ -n "$(az ad user list --filter "userPrincipalName eq '$upn'" --query "[0].id" -o tsv)" ]; then
      echo "  deleting user $upn"
      az ad user delete --id "$upn" || echo "  (could not delete $upn)"
    fi
  done
  for display in "$SVC_USER_DISPLAY" "$SVC_ADMIN_DISPLAY" "$API_DISPLAY"; do
    for id in $(az ad app list --filter "displayName eq '$display'" --query "[].id" -o tsv); do
      echo "  deleting app registration '$display'"
      az ad app delete --id "$id" || echo "  (could not delete $display)"
    done
  done
  if [ -f "$ENV_FILE" ]; then
    rm -f "$ENV_FILE"
    echo "  removed $ENV_FILE"
  fi
  echo "Done."
}

case "$ACTION" in
  create) do_create ;;
  show)   do_show ;;
  delete) do_delete ;;
esac
