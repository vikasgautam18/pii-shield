#!/usr/bin/env bash
# run_indian_banking_tests.sh — run the Indian banking test suite in one go, against the local Docker Compose
# stack or the Azure deployment.
#
#   examples/run_indian_banking_tests.sh [--mode local|azure] [--keep-apps] [--env-file FILE] [--api-url URL]
#                                        [runner options, e.g. --output FILE]
#
#   --mode local   (default) examples/run_indian_banking_tests.py against http://localhost:8000
#   --mode azure   examples/run_indian_banking_tests_azure.py against the deployed API
#
# Steps:
#   1. On first use, creates .venv with the two packages the example tools need (requests, pyyaml).
#   2. Checks that the API answers (azure: waits for an app that scaled to zero to wake up) and whether
#      authentication is on.
#   3. With authentication on, gets an admin-role token (the suite registers applications):
#        local  gives the bundled dev Keycloak's service account the admin role for the length of the run
#        azure  uses the admin test service from the Entra env file that infra/scripts/07-setup-entra.sh writes
#   4. Runs the suite.
#   5. Always, also after a failure or Ctrl-C: deletes the test applications the suite leaves behind (unless
#      --keep-apps) and, locally, takes the admin role away again unless it was already granted before the run.
#
# --env-file and --api-url only apply to --mode azure. Locally the script talks to the dev Keycloak with its
# public dev credentials, so there --api-url and --access-token are refused.
# The exit status is the runner's. The runner exits 0 even when a section reports fewer passes than expected,
# so read its summary.
set -euo pipefail

MODE=local
KEEP_APPS=0
API_URL_ARG=""
ENV_FILE_ARG=""
RUNNER_ARGS=()

KC_URL="http://localhost:8080"
KC_REALM="pii-shield"
PY=".venv/bin/python"

ROOT=""
RUNNER=""
API_URL=""
ENV_FILE=""
ENV_FILE_ERROR=""
AUTH=""
GRANTED=0
RUN_STARTED=0

die() { echo "ERROR: $*" >&2; exit 1; }
bad_usage() { echo "ERROR: $*" >&2; echo "Run with --help for usage." >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: examples/run_indian_banking_tests.sh [--mode local|azure] [options] [runner options]

Runs the Indian banking test suite in one go: prepares .venv, gets an admin token when authentication is on,
runs the suite, then deletes the test applications and undoes any temporary permission change.

  --mode local|azure  local (default): examples/run_indian_banking_tests.py against the Docker Compose stack
                      azure: examples/run_indian_banking_tests_azure.py against the Azure deployment
  --keep-apps         keep the test applications (default: delete them when the run ends)
  --env-file FILE     azure: the Entra env file written by infra/scripts/07-setup-entra.sh
                      (default: $ENTRA_ENV_FILE, else the only *.entra.env in ~/.config/pii-shield)
  --api-url URL       azure: another deployment, https only (default: the runner's own)
  -h, --help          show this help

Other options, for example --output FILE, go to the runner (relative paths are relative to the repository root).
Locally --api-url and --access-token are refused: the script targets the local stack and supplies the token.
EOF
}

# Arguments first, so that nothing is touched when they are refused.
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --keep-apps) KEEP_APPS=1; shift ;;
    --mode|--env-file|--api-url)
      { [ $# -ge 2 ] && [ -n "$2" ]; } || bad_usage "$1 needs a value."
      case "$1" in
        --mode) MODE="$2" ;;
        --env-file) ENV_FILE_ARG="$2" ;;
        *) API_URL_ARG="$2" ;;
      esac
      shift 2 ;;
    --mode=*|--env-file=*|--api-url=*)
      [ -n "${1#*=}" ] || bad_usage "${1%%=*} needs a value."
      case "$1" in
        --mode=*) MODE="${1#*=}" ;;
        --env-file=*) ENV_FILE_ARG="${1#*=}" ;;
        *) API_URL_ARG="${1#*=}" ;;
      esac
      shift ;;
    --a*) bad_usage "$1 is not supported: the script supplies the token itself, and --api-url must be spelled out." ;;
    *) RUNNER_ARGS+=("$1"); shift ;;
  esac
done

case "$MODE" in
  local|azure) ;;
  *) bad_usage "--mode must be local or azure (got '$MODE')." ;;
esac
if [ "$MODE" = local ]; then
  [ -z "$API_URL_ARG" ] || bad_usage "--api-url is not supported with --mode local: the script targets the local stack."
  [ -z "$ENV_FILE_ARG" ] || bad_usage "--env-file only applies to --mode azure."
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Azure only. Sets ENV_FILE, or ENV_FILE_ERROR when no env file can be chosen.
resolve_env_file() {
  local dir
  if [ -n "$ENV_FILE_ARG" ]; then
    ENV_FILE="$ENV_FILE_ARG"
  elif [ -n "${ENTRA_ENV_FILE:-}" ]; then
    ENV_FILE="$ENTRA_ENV_FILE"
  else
    dir="${XDG_CONFIG_HOME:-$HOME/.config}/pii-shield"
    set -- "$dir"/*.entra.env
    if [ "$#" -gt 1 ]; then
      ENV_FILE_ERROR="There are several Entra env files in $dir; choose one with --env-file."
    elif [ -f "$1" ]; then
      ENV_FILE="$1"
    else
      ENV_FILE_ERROR="No Entra env file found in $dir. infra/scripts/07-setup-entra.sh creates it, or pass --env-file."
    fi
    return 0
  fi
  # The script changes directory later, so a relative path is made absolute while it still means something.
  case "$ENV_FILE" in /*) ;; *) ENV_FILE="$PWD/$ENV_FILE" ;; esac
  if [ ! -f "$ENV_FILE" ]; then
    ENV_FILE_ERROR="Entra env file not found: $ENV_FILE"
    ENV_FILE=""
  fi
}

if [ "$MODE" = local ]; then
  RUNNER=examples/run_indian_banking_tests.py
  API_URL="http://localhost:8000"
else
  RUNNER=examples/run_indian_banking_tests_azure.py
  API_URL="${API_URL_ARG:-$(sed -n 's/^DEFAULT_API_URL = "\(.*\)"$/\1/p' "$ROOT/$RUNNER")}"
  API_URL="${API_URL%/}"
  [ -n "$API_URL" ] || die "Could not find DEFAULT_API_URL in $RUNNER; pass --api-url."
  case "$API_URL" in
    https://*) ;;
    *) bad_usage "--api-url must start with https:// (the token must not travel in the clear)." ;;
  esac
  resolve_env_file
  # A file the user named that does not work is an error now; a missing default only matters if auth is on.
  if [ -n "$ENV_FILE_ERROR" ] && { [ -n "$ENV_FILE_ARG" ] || [ -n "${ENTRA_ENV_FILE:-}" ]; }; then
    die "$ENV_FILE_ERROR"
  fi
fi

cd "$ROOT"

# Grants or revokes the admin role of the dev Keycloak's service account. Prints granted, revoked or unchanged
# (unchanged when the account already was in the requested state).
kc_admin_role() {
  KC_ADMIN_USER=admin KC_ADMIN_PASSWORD=admin "$PY" - "$1" "$KC_URL" "$KC_REALM" <<'PYEOF'
import os
import sys

import requests

action, kc, realm = sys.argv[1:4]
SERVICE_ACCOUNT, ROLE = "service-account-pii-shield-svc", "PIIShield.Admin"
try:
    login = requests.post(f"{kc}/realms/master/protocol/openid-connect/token", timeout=30, data={
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.environ["KC_ADMIN_USER"], "password": os.environ["KC_ADMIN_PASSWORD"]})
    login.raise_for_status()
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    admin = f"{kc}/admin/realms/{realm}"

    def get(path, **kwargs):
        response = requests.get(admin + path, headers=headers, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    user_id = get("/users", params={"username": SERVICE_ACCOUNT, "exact": "true"})[0]["id"]
    mappings = f"{admin}/users/{user_id}/role-mappings/realm"
    held = any(role["name"] == ROLE for role in get(f"/users/{user_id}/role-mappings/realm"))
    if held == (action == "grant"):
        print("unchanged")
    else:
        send = requests.post if action == "grant" else requests.delete
        send(mappings, headers=headers, json=[get(f"/roles/{ROLE}")], timeout=30).raise_for_status()
        print("granted" if action == "grant" else "revoked")
except (requests.RequestException, KeyError, IndexError) as exc:
    sys.exit(f"Keycloak admin request failed: {exc}")
PYEOF
}

# Reads one KEY='value' line of the Entra env file, as 07-setup-entra.sh does, without executing the file.
env_value() { sed -n "s/^$1='\\(.*\\)'\$/\\1/p" "$ENV_FILE" | head -n 1; }

check_entra_env() {
  local key
  for key in OIDC_ISSUER_URL OIDC_AUDIENCE SVC_ADMIN_CLIENT_ID SVC_ADMIN_CLIENT_SECRET; do
    [ -n "$(env_value "$key")" ] || die "$key is missing from $ENV_FILE (infra/scripts/07-setup-entra.sh writes it)."
  done
}

# The OIDC_* values the script does not set are blanked, so that ones left in your shell cannot change the result.
get_token() {
  if [ "$MODE" = local ]; then
    OIDC_ISSUER_URL="$KC_URL/realms/$KC_REALM" OIDC_ALLOW_INSECURE_HTTP=true \
      OIDC_CLIENT_ID=pii-shield-svc OIDC_CLIENT_SECRET=dev-only-service-client-secret \
      OIDC_SCOPE='' OIDC_TOKEN_URL='' OIDC_TOKEN_AUDIENCE='' "$PY" scripts/get_token.py
  else
    OIDC_ISSUER_URL="$(env_value OIDC_ISSUER_URL)" OIDC_CLIENT_ID="$(env_value SVC_ADMIN_CLIENT_ID)" \
      OIDC_CLIENT_SECRET="$(env_value SVC_ADMIN_CLIENT_SECRET)" OIDC_SCOPE="api://$(env_value OIDC_AUDIENCE)/.default" \
      OIDC_TOKEN_URL='' OIDC_TOKEN_AUDIENCE='' "$PY" scripts/get_token.py
  fi
}

# A fresh token, rather than the one the run started with: locally they last only a few minutes.
# Only called from finish, which runs through the EXIT trap, so shellcheck sees no caller.
# shellcheck disable=SC2329
delete_test_apps() {
  local prefix
  if [ "$AUTH" = on ]; then
    API_ACCESS_TOKEN="$(get_token)" || return 1
    export API_ACCESS_TOKEN
  fi
  for prefix in __test indian-banking-tests; do
    "$PY" scripts/cleanup_test_apps.py --api-url "$API_URL" --prefix "$prefix" --timeout 60 || return 1
  done
}

# Runs through the EXIT trap below, which shellcheck does not count as a call.
# shellcheck disable=SC2329
finish() {
  local status=$? outcome
  trap - EXIT
  trap '' INT TERM  # cleanup must complete
  set +e
  echo
  if [ "$RUN_STARTED" = 1 ] && [ "$KEEP_APPS" = 0 ]; then
    echo "==> Deleting the test applications"
    delete_test_apps ||
      echo "WARNING: could not delete them all; see 'Running the bundled test suites' in docs/authentication.md" >&2
  fi
  if [ "$GRANTED" = 1 ]; then
    echo "==> Taking the admin role away from the dev service account"
    if outcome="$(kc_admin_role revoke)"; then
      echo "    admin role $outcome"
    else
      echo "WARNING: the admin role is still granted. Remove it in the Keycloak console ($KC_URL, admin / admin):" >&2
      echo "         realm $KC_REALM, Users, service-account-pii-shield-svc, Role mapping." >&2
    fi
  fi
  if [ "$status" = 0 ]; then
    echo "==> Finished. The runner exits 0 even when a section reports fewer passes than expected, so read its summary."
  else
    echo "==> Finished with exit status $status."
  fi
  exit "$status"
}

echo "==> Mode: $MODE ($API_URL)"

if [ ! -x "$PY" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 not found."
  echo "==> Creating .venv"
  python3 -m venv .venv
fi
if ! "$PY" -c 'import requests, yaml' 2>/dev/null; then
  echo "==> Installing requests and pyyaml into .venv"
  "$PY" -m pip install -q requests pyyaml
fi

if [ "$MODE" = local ]; then
  curl -fsS --max-time 10 "$API_URL/health" >/dev/null 2>&1 ||
    die "The API is not reachable at $API_URL. Start the stack first, for example: docker compose --profile auth-dev up -d"
else
  echo "==> Contacting the API (an app that scaled to zero needs about a minute to wake up)"
  curl -fsS --max-time 150 --retry 1 --retry-delay 5 "$API_URL/health" >/dev/null 2>&1 ||
    die "The API did not answer at $API_URL. Check the deployment, for example with infra/scripts/03-verify.sh."
fi

# /me answers 200 without a token only when authentication is off.
case "$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 "$API_URL/me" || true)" in
  200) AUTH=off ;;
  401) AUTH=on ;;
  *) die "Unexpected answer from $API_URL/me; is that the PII Shield API?" ;;
esac

if [ "$AUTH" = on ] && [ "$MODE" = azure ]; then
  [ -n "$ENV_FILE" ] || die "$ENV_FILE_ERROR"
  check_entra_env
fi

trap 'finish' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$AUTH" = on ]; then
  if [ "$MODE" = local ]; then
    echo "==> Authentication is on: giving the dev service account the admin role"
    outcome="$(kc_admin_role grant)" ||
      die "Could not update the bundled dev Keycloak at $KC_URL, the only identity provider this mode supports (docker compose --profile auth-dev up -d). For another one, fetch a token yourself and run $RUNNER; see docs/authentication.md."
    if [ "$outcome" = granted ]; then GRANTED=1; fi
    echo "    admin role $outcome"
  else
    echo "==> Authentication is on: using the admin test service from $ENV_FILE"
  fi
  token="$(get_token)" || die "Could not get a token."
  export API_ACCESS_TOKEN="$token"
else
  unset API_ACCESS_TOKEN
  echo "==> Authentication is off: no token needed"
fi

echo "==> Running $RUNNER"
RUN_STARTED=1
RUNNER_RC=0
"$PY" "$RUNNER" --api-url "$API_URL" ${RUNNER_ARGS[@]+"${RUNNER_ARGS[@]}"} || RUNNER_RC=$?
exit "$RUNNER_RC"
