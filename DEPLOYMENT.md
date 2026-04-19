# PII Shield — Azure Deployment Guide

End-to-end guide to deploy PII Shield to **Azure Container Apps** using Terraform
and the provided lifecycle scripts.

---

## Prerequisites

| Tool | Install |
|------|---------|
| Azure CLI ≥ 2.60 | https://learn.microsoft.com/cli/azure/install-azure-cli |
| Terraform ≥ 1.5 | https://developer.hashicorp.com/terraform/install |
| Docker | (optional — only needed for local builds; Azure Container Registry has cloud build) |
| Bash | macOS/Linux/WSL |

You also need:

- **Azure subscription** with permission to create resource groups, storage
  accounts, ACR, Redis Cache, Container Apps Environment, App Insights, and
  Managed Grafana.
- An **Entra ID account** signed into the Azure CLI (`az login`).

---

## 1. Bootstrap Terraform state storage (run once)

Terraform state is stored in an Azure Storage Account, secured with Entra ID
auth and TLS-only access. The `00-bootstrap-state.sh` script provisions it.

```bash
cd infra/scripts

# Easiest: derive all names from a project prefix (auto-generates a globally-unique storage account name)
./00-bootstrap-state.sh --project-name piishield

# Or supply the storage account name explicitly
./00-bootstrap-state.sh --storage-account mytfstate12345
```

You can override defaults with flags:

```bash
./00-bootstrap-state.sh \
  --project-name   piishield \
  --location       centralindia \
  --resource-group my-tfstate-rg
```

The script prints the exact `terraform init -backend-config=...` command for the
next step. **Save it.**

---

## 2. Configure Terraform variables

```bash
cd ../terraform
cp terraform.tfvars.example terraform.tfvars
```

Open `terraform.tfvars` and fill in:

```hcl
subscription_id = "00000000-0000-0000-0000-000000000000"

# Prefix for all resources (3-16 lowercase alphanumeric chars).
# A random 5-char suffix is auto-appended to globally-unique resources
# (ACR, Redis, Grafana). Set `name_suffix` to pin it.
project_name = "piishield"

location       = "centralindia"
redis_capacity = 1            # C1 = 1 GB. Use 3 for production load.
```

All resource names are derived:

| Resource | Name pattern |
|----------|--------------|
| Resource group | `<project_name>-rg` |
| Container Apps env | `<project_name>-env` |
| Log Analytics | `<project_name>-logs` |
| Application Insights | `<project_name>-insights` |
| ACR | `<project_name>acr<suffix>` |
| Redis | `<project_name>-redis-<suffix>` |
| Grafana | `<project_name>-grafana-<suffix>` |

> **Note:** ACR, Redis, and Grafana names must be globally unique across Azure.
> The random `name_suffix` handles this automatically. To get reproducible names
> (e.g. for re-deploys), set `name_suffix = "prod01"` in tfvars.

---

## 3. Initialize and apply Terraform

Use the `-backend-config` flags printed by `00-bootstrap-state.sh`:

```bash
terraform init \
  -backend-config="resource_group_name=piishield-tfstate-rg" \
  -backend-config="storage_account_name=piishieldtfabcde" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=piishield.terraform.tfstate" \
  -backend-config="use_azuread_auth=true"

terraform plan
terraform apply
```

> Redis provisioning takes ~15-20 minutes. Other resources are fast.

---

## 4. Build and push the Docker image

`01-build-push.sh` uses ACR cloud build (no local Docker required):

```bash
cd ../scripts
./01-build-push.sh
```

Override defaults via env vars:

```bash
NLP_ENGINE=onnx \
TRANSFORMERS_MODEL=protectai/bert-base-NER-onnx \
IMAGE_TAG=v1.0.0 \
./01-build-push.sh
```

Build takes ~5-7 minutes the first time.

---

## 5. Deploy the container apps

`02-deploy-apps.sh` reads from Terraform outputs and creates 3 container apps:

| App | Purpose | Port | Default name |
|-----|---------|------|--------------|
| API | FastAPI PII Shield service | 8000 | `pii-shield` |
| Playground | Streamlit user UI | 7860 | `playground` |
| Admin | Streamlit admin UI | 7861 | `pii-admin` |

```bash
./02-deploy-apps.sh
```

Override app names via env vars:

```bash
API_APP_NAME=my-pii-api \
PLAYGROUND_APP_NAME=my-playground \
ADMIN_APP_NAME=my-admin \
./02-deploy-apps.sh
```

Use `02-deploy-apps_noref.sh` if you provisioned infrastructure without
Terraform — it reads names from env vars and looks up details directly via
Azure CLI:

```bash
RG_NAME=my-rg \
ACR_NAME=myacr \
ACA_ENV=pii-shield-env \
REDIS_NAME=myredis \
APP_INSIGHTS_NAME=pii-shield-insights \
./02-deploy-apps_noref.sh
```

---

## 6. Verify the deployment

```bash
./03-verify.sh
```

You should see:

```
[PASS] API /health (HTTP 200)
[PASS] Playground reachable (HTTP 200)
[PASS] Admin reachable (HTTP 200)
[PASS] POST /anonymize_unique (HTTP 200)
[PASS] Response contains anonymized_text
```

---

## 7. Deploy Grafana dashboards (optional but recommended)

```bash
./04-deploy-dashboards.sh
```

This uploads the dashboards from `observability/grafana/dashboards-azure/` into
the Azure Managed Grafana instance. Open the URL printed at the end of the
script in a browser — Entra ID SSO is configured automatically.

---

## Environment variable reference

Application behavior is controlled via env vars. See `.env.example` for the
authoritative list. Highlights:

| Variable | Default | Purpose |
|----------|---------|---------|
| `NLP_ENGINE` | `onnx` | Backend: `spacy`, `transformers`, `onnx`, `stanza` |
| `TRANSFORMERS_MODEL` | `protectai/bert-base-NER-onnx` | HF model for transformers/onnx |
| `ENCRYPTION_BACKEND` | `pqc` | `pqc` (ML-KEM-768) or `fernet` |
| `REDIS_AUTH_MODE` | `entra` | `entra` (managed identity) or `key` |
| `SESSION_TTL_SECONDS` | `300` | How long anonymization sessions live in Redis |
| `WEB_CONCURRENCY` | `2` | Gunicorn workers per replica |
| `NLP_THREAD_POOL_SIZE` | `6` | Concurrent NLP inferences per worker |
| `DISABLED_RECOGNIZERS` | (see `.env.example`) | Comma-separated Presidio recognizer class names |

---

## Updating a running deployment

After code changes:

```bash
# Rebuild image
./01-build-push.sh --tag v1.1.0

# Update API only (leave UIs alone)
az containerapp update --name pii-shield --resource-group <rg> \
  --image <acr-server>/pii-shield:v1.1.0
```

Or re-run `./02-deploy-apps.sh` to update all 3 apps.

---

## Tear down

```bash
cd infra/terraform
terraform destroy
```

> This removes the Container Apps, Redis, ACR, and all other resources in the
> resource group. The Terraform state storage account is **not** destroyed —
> delete it manually if desired.

---

## Troubleshooting

### `terraform init` fails with backend authentication error

You're not signed in to Azure CLI as a user with `Storage Blob Data Contributor`
on the state storage account. The bootstrap script grants this automatically;
if you ran it as a different user, assign manually:

```bash
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee <your-object-id> \
  --scope <storage-account-resource-id>
```

### `02-deploy-apps.sh` fails with `redis_url` not found

The script defaults to `REDIS_AUTH_MODE=entra`. If you want key-based auth,
your Redis instance must have `redis_access_keys_enabled = true` in
`variables.tf`. Re-apply Terraform after changing.

### Cold-start latency on first request

The transformers/onnx engine loads its model on first request. Set
`min-replicas=1` (instead of 0) in `02-deploy-apps.sh` to keep one replica warm.

### `04-deploy-dashboards.sh` fails with `Insufficient privileges`

You need the **Grafana Admin** role on the Managed Grafana instance. Assign
yourself via Azure portal: *Managed Grafana > Access control (IAM) > Add role
assignment > Grafana Admin*.
