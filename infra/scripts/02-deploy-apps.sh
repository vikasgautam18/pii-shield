#!/usr/bin/env bash
# ── 02-deploy-apps.sh ────────────────────────────────────────────────────────
# Deploy PII Shield container apps to Azure Container Apps.
# Reads infrastructure details from Terraform outputs.
#
# Prerequisites:
#   - Terraform applied (infra/terraform)
#   - Image built and pushed (01-build-push.sh)
#   - Azure CLI logged in with containerapp extension
#
# Usage:
#   cd infra/scripts && ./02-deploy-apps.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$SCRIPT_DIR/../terraform"

# ── Ensure required Azure CLI extensions are installed ───────────────────────

az config set extension.dynamic_install_allow_preview=true  2>/dev/null
az config set extension.use_dynamic_install=yes_without_prompt  2>/dev/null
az extension add --name application-insights --upgrade --allow-preview true -y 2>/dev/null || true
az extension add --name containerapp --upgrade --allow-preview true -y 2>/dev/null || true

IMAGE_TAG="${IMAGE_TAG:-latest}"

# Container app names (override via env vars; defaults come from Terraform outputs)
API_APP_NAME="${API_APP_NAME:-$(cd "$TF_DIR" && terraform output -raw api_app_name 2>/dev/null || echo "pii-shield")}"
PLAYGROUND_APP_NAME="${PLAYGROUND_APP_NAME:-$(cd "$TF_DIR" && terraform output -raw playground_app_name 2>/dev/null || echo "playground")}"
ADMIN_APP_NAME="${ADMIN_APP_NAME:-$(cd "$TF_DIR" && terraform output -raw admin_app_name 2>/dev/null || echo "pii-admin")}"

# App configuration defaults
NLP_ENGINE="${NLP_ENGINE:-onnx}"
TRANSFORMERS_MODEL="${TRANSFORMERS_MODEL:-protectai/bert-base-NER-onnx}"
TRANSFORMERS_SPACY_MODEL="${TRANSFORMERS_SPACY_MODEL:-en_core_web_sm}"
SESSION_TTL_SECONDS="${SESSION_TTL_SECONDS:-30}"
ENCRYPTION_BACKEND="${ENCRYPTION_BACKEND:-pqc}"
REDIS_AUTH_MODE="${REDIS_AUTH_MODE:-entra}"

# ── Read Terraform outputs ───────────────────────────────────────────────────

echo " Reading Terraform outputs..."
RG_NAME=$(cd "$TF_DIR" && terraform output -raw resource_group_name)
ACR_SERVER=$(cd "$TF_DIR" && terraform output -raw acr_login_server)
ACR_USER=$(cd "$TF_DIR" && terraform output -raw acr_admin_username)
ACR_PASS=$(cd "$TF_DIR" && terraform output -raw acr_admin_password)
ACA_ENV=$(cd "$TF_DIR" && terraform output -raw aca_environment_name)
REDIS_URL=$(cd "$TF_DIR" && terraform output -raw redis_url 2>/dev/null || echo "")
REDIS_HOST=$(cd "$TF_DIR" && terraform output -raw redis_hostname 2>/dev/null || echo "")
REDIS_PORT=$(cd "$TF_DIR" && terraform output -raw redis_ssl_port 2>/dev/null || echo "6380")
APPINSIGHTS_CONN=$(cd "$TF_DIR" && terraform output -raw app_insights_connection_string)

LOCATION=$(az group show --name "$RG_NAME" --query location -o tsv)
ACA_ENV_ID=$(az containerapp env show \
  --name "$ACA_ENV" --resource-group "$RG_NAME" \
  --query id -o tsv)

IMAGE="${ACR_SERVER}/pii-shield:${IMAGE_TAG}"

echo "   Resource Group:          $RG_NAME"
echo "   ACR:                     $ACR_SERVER"
echo "   ACA Env:                 $ACA_ENV"
echo "   Image:                   $IMAGE"
echo "   NLP Engine:              $NLP_ENGINE"
echo "   Transformers:            $TRANSFORMERS_MODEL"
echo "   Transformers (spaCy):    $TRANSFORMERS_SPACY_MODEL"
echo "   Session TTL (seconds):   $SESSION_TTL_SECONDS"
echo "   Encryption Backend:      $ENCRYPTION_BACKEND"
echo "   Redis Auth Mode:         $REDIS_AUTH_MODE"
echo "   App Insights:            ${APPINSIGHTS_CONN:0:50}...(redacted for security)"
if [ "$REDIS_AUTH_MODE" = "entra" ]; then
  echo "   Redis Host:              $REDIS_HOST"
  echo "   Redis Port:              $REDIS_PORT"
else
  echo "   Redis URL:               ${REDIS_URL%%@*}... (redacted for security)"
fi

# ── Enable managed OTel agent on ACA environment ────────────────────────────

echo " Configuring managed OpenTelemetry agent..."
az containerapp env telemetry app-insights set \
  --name "$ACA_ENV" \
  --resource-group "$RG_NAME" \
  --connection-string "$APPINSIGHTS_CONN" \
  --enable-open-telemetry-traces true \
  --enable-open-telemetry-logs true \
  2>/dev/null || echo "   (OTel agent already configured or command not available — skipping)"

# ── Deploy pii-shield (FastAPI API) ─────────────────────────────────────────

# Build Redis args based on auth mode
if [ "$REDIS_AUTH_MODE" = "entra" ]; then
  REDIS_SECRET_ARGS=""
  REDIS_ENV_ARGS="REDIS_AUTH_MODE=entra REDIS_HOST=$REDIS_HOST REDIS_PORT=$REDIS_PORT"
else
  REDIS_SECRET_ARGS="--secrets redis-url=$REDIS_URL"
  REDIS_ENV_ARGS="REDIS_URL=secretref:redis-url"
fi

echo ""
echo " Deploying pii-shield (FastAPI API)..."
az containerapp create \
  --name "$API_APP_NAME" \
  --resource-group "$RG_NAME" \
  --environment "$ACA_ENV" \
  --image "$IMAGE" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_USER" \
  --registry-password "$ACR_PASS" \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 96 \
  --cpu 2 \
  --memory 4Gi \
  $REDIS_SECRET_ARGS \
  --env-vars \
    $REDIS_ENV_ARGS \
    OTEL_SERVICE_NAME=pii-shield \
    APPLICATIONINSIGHTS_CONNECTION_STRING="$APPINSIGHTS_CONN" \
    ENCRYPTION_BACKEND="$ENCRYPTION_BACKEND" \
    NLP_ENGINE="$NLP_ENGINE" \
    TRANSFORMERS_MODEL="$TRANSFORMERS_MODEL" \
    TRANSFORMERS_SPACY_MODEL="$TRANSFORMERS_SPACY_MODEL" \
    SESSION_TTL_SECONDS="$SESSION_TTL_SECONDS" \
    WEB_CONCURRENCY="${WEB_CONCURRENCY:-2}" \
    NLP_THREAD_POOL_SIZE="${NLP_THREAD_POOL_SIZE:-6}" \
    ORT_INTRA_OP_THREADS="${ORT_INTRA_OP_THREADS:-2}" \
    ORT_INTER_OP_THREADS="${ORT_INTER_OP_THREADS:-1}" \
    DISABLED_RECOGNIZERS="${DISABLED_RECOGNIZERS:-InAadhaarRecognizer,NhsRecognizer,UsBankRecognizer,SgFinRecognizer,AuAbnRecognizer,AuAcnRecognizer,AuTfnRecognizer,AuMedicareRecognizer,MedicalLicenseRecognizer}" \
    CONTEXT_SIMILARITY_FACTOR="${CONTEXT_SIMILARITY_FACTOR:-0.45}"

API_FQDN=$(az containerapp show \
  --name "$API_APP_NAME" \
  --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo " [PASS] pii-shield deployed: https://$API_FQDN"

# ── Assign managed identity for Entra Redis auth ────────────────────────────
if [ "$REDIS_AUTH_MODE" = "entra" ]; then
  echo "[key] Configuring managed identity for Redis Entra auth..."

  # Enable system-assigned managed identity on pii-shield
  az containerapp identity assign \
    --name "$API_APP_NAME" \
    --resource-group "$RG_NAME" \
    --system-assigned \
    2>/dev/null || echo "   (system identity already assigned)"

  PRINCIPAL_ID=$(az containerapp identity show \
    --name "$API_APP_NAME" \
    --resource-group "$RG_NAME" \
    --query principalId -o tsv)

  REDIS_ID=$(az redis show \
    --name "${REDIS_HOST%%.*}" \
    --resource-group "$RG_NAME" \
    --query id -o tsv)

  # Assign the Redis "Data Owner" access policy to the managed identity.
  # NOTE: Azure Cache for Redis uses its own access-policy system for data-plane
  # auth (GET/SET), NOT Azure RBAC. Built-in policies: "Data Owner" /
  # "Data Contributor" / "Data Reader".
  echo "   Assigning Redis 'Data Owner' access policy to principal ${PRINCIPAL_ID:0:8}..."
  ASSIGNMENT_NAME="pii-shield-app-$(echo "$PRINCIPAL_ID" | cut -c1-8)"
  az redis access-policy-assignment create \
    --resource-group "$RG_NAME" \
    --name "${REDIS_HOST%%.*}" \
    --access-policy-name "Data Owner" \
    --object-id "$PRINCIPAL_ID" \
    --object-id-alias "$ASSIGNMENT_NAME" \
    --policy-assignment-name "$ASSIGNMENT_NAME" \
    --output none 2>&1 | grep -v "already exists" || true

  # Set REDIS_ENTRA_USERNAME to the managed identity principal (object) ID
  # so the app authenticates with the correct identity
  az containerapp update \
    --name "$API_APP_NAME" \
    --resource-group "$RG_NAME" \
    --set-env-vars "REDIS_ENTRA_USERNAME=$PRINCIPAL_ID" \
    2>/dev/null || echo "   (env var update failed — set REDIS_ENTRA_USERNAME manually)"

  echo " [PASS] Managed identity configured (principal: ${PRINCIPAL_ID:0:8}...)"
fi

# ── Configure HTTP concurrency scaling rule ──────────────────────────────────
echo " Configuring auto-scale rule (HTTP concurrency = 4 per replica)..."
az containerapp update \
  --name "$API_APP_NAME" \
  --resource-group "$RG_NAME" \
  --scale-rule-name http-concurrency \
  --scale-rule-type http \
  --scale-rule-http-concurrency 4 \
  2>/dev/null || echo "   (scale rule already configured or command not available — skipping)"

# ── Deploy playground (User UI — Streamlit) ──────────────────────────────────
# Uses YAML to work around Azure CLI bug with dash args (e.g. -m)

echo ""
echo " Deploying playground (User UI)..."

PLAYGROUND_YAML=$(mktemp)
cat > "$PLAYGROUND_YAML" <<EOF
location: ${LOCATION}
type: Microsoft.App/containerApps
properties:
  managedEnvironmentId: ${ACA_ENV_ID}
  configuration:
    ingress:
      external: true
      allowInsecure: false
      targetPort: 7860
    registries:
      - server: ${ACR_SERVER}
        username: ${ACR_USER}
        passwordSecretRef: acr-password
    secrets:
      - name: acr-password
        value: "${ACR_PASS}"
  template:
    scale:
      minReplicas: 0
      maxReplicas: 3
    containers:
      - image: ${IMAGE}
        name: ${PLAYGROUND_APP_NAME}
        command:
          - streamlit
          - run
          - app/streamlit_app.py
          - "--server.port=7860"
          - "--server.address=0.0.0.0"
          - "--server.headless=true"
          - "--client.toolbarMode=minimal"
        resources:
          cpu: 1
          memory: 2Gi
        env:
          - name: API_BASE
            value: http://${API_APP_NAME}
EOF

az containerapp create \
  --name "$PLAYGROUND_APP_NAME" \
  --resource-group "$RG_NAME" \
  --yaml "$PLAYGROUND_YAML"
rm -f "$PLAYGROUND_YAML"

PLAYGROUND_FQDN=$(az containerapp show \
  --name "$PLAYGROUND_APP_NAME" \
  --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo " [PASS] playground deployed: https://$PLAYGROUND_FQDN"

# ── Deploy pii-admin (Admin UI — Streamlit) ──────────────────────────────────

echo ""
echo " Deploying pii-admin (Admin UI)..."

ADMIN_YAML=$(mktemp)
cat > "$ADMIN_YAML" <<EOF
location: ${LOCATION}
type: Microsoft.App/containerApps
properties:
  managedEnvironmentId: ${ACA_ENV_ID}
  configuration:
    ingress:
      external: true
      allowInsecure: false
      targetPort: 7861
    registries:
      - server: ${ACR_SERVER}
        username: ${ACR_USER}
        passwordSecretRef: acr-password
    secrets:
      - name: acr-password
        value: "${ACR_PASS}"
  template:
    scale:
      minReplicas: 0
      maxReplicas: 2
    containers:
      - image: ${IMAGE}
        name: ${ADMIN_APP_NAME}
        command:
          - streamlit
          - run
          - app/streamlit_admin.py
          - "--server.port=7861"
          - "--server.address=0.0.0.0"
          - "--server.headless=true"
          - "--client.toolbarMode=minimal"
        resources:
          cpu: 1
          memory: 2Gi
        env:
          - name: API_BASE
            value: http://${API_APP_NAME}
EOF

az containerapp create \
  --name "$ADMIN_APP_NAME" \
  --resource-group "$RG_NAME" \
  --yaml "$ADMIN_YAML"
rm -f "$ADMIN_YAML"

ADMIN_FQDN=$(az containerapp show \
  --name "$ADMIN_APP_NAME" \
  --resource-group "$RG_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo " [PASS] pii-admin deployed: https://$ADMIN_FQDN"

# ── Clean up old Gradio apps ────────────────────────────────────────────────

echo ""
echo " Removing old Gradio container apps..."
az containerapp delete --name gradio-app --resource-group "$RG_NAME" --yes 2>/dev/null && echo "   Deleted gradio-app" || echo "   gradio-app not found (already removed)"
az containerapp delete --name gradio-admin --resource-group "$RG_NAME" --yes 2>/dev/null && echo "   Deleted gradio-admin" || echo "   gradio-admin not found (already removed)"

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  PII Shield — Azure Container Apps Deployment Complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  API:           https://$API_FQDN"
echo "  Playground:    https://$PLAYGROUND_FQDN"
echo "  Admin:         https://$ADMIN_FQDN"
echo ""
echo "  Run ./03-verify.sh to run smoke tests."
echo "═══════════════════════════════════════════════════════════════"
