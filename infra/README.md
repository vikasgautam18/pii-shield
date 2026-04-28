# PII Shield — Azure Deployment Guide

Deploy PII Shield to **Azure Container Apps** with Terraform (infrastructure) and shell scripts (application lifecycle).

---

## Architecture

```
                     Internet (HTTPS)
                          │
         ┌────────────────┼────────────────┐
         │                │                │
  ┌──────▼──────┐  ┌─────▼──────┐  ┌──────▼───────┐
  │ pii-shield  │  │ playground │  │  pii-admin   │
  │  (API:8000) │  │  (UI:7860) │  │  (UI:7861)   │
  └──────┬──────┘  └─────┬──────┘  └──────┬───────┘
         │               │                │
         │               └── http://pii-shield ──┘
         │                (internal ACA network)
  ┌──────▼──────┐     ┌────────────────────┐     ┌──────────────────┐
  │ Azure Cache │     │ Azure App Insights │────▶│ Log Analytics    │
  │ for Redis   │     │ (OTel exporter)    │     │ Workspace        │
  │ (TLS)       │     └────────────────────┘     └────────┬─────────┘
  └─────────────┘                                         │
                                                 ┌────────▼─────────┐
                                                 │ Azure Managed    │
                                                 │ Grafana (v11)    │
                                                 │ KQL dashboards   │
                                                 └──────────────────┘
```

| Azure Resource | Purpose | SKU |
|----------------|---------|-----|
| Container Apps Environment | Hosts all 3 apps | Consumption (serverless) |
| Azure Container Registry | Stores Docker image | Basic |
| Azure Cache for Redis | Sessions + app registry | Standard C1 (HA) |
| Application Insights | Traces, logs, metrics | Pay-per-use |
| Log Analytics Workspace | Backing store for logs | PerGB2018 |
| Azure Managed Grafana | Observability dashboards (KQL) | Standard |

> See [`docs/architecture-azure.drawio`](../docs/architecture-azure.drawio) for the full visual diagram.

---

## Prerequisites

| Tool | Install |
|------|---------|
| **Azure CLI** ≥ 2.60 | [Install guide](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) |
| **Terraform** ≥ 1.5 | [Install guide](https://developer.hashicorp.com/terraform/install) |
| **Docker** (optional) | Only needed if building locally instead of in ACR |

```bash
# Install the Container Apps CLI extension
az extension add --name containerapp --upgrade --allow-preview true

# Register required Azure providers
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

---

## Step 1 — Configure

1. **Login to Azure:**

   ```bash
   az login
   ```

2. **Set your subscription ID** in `terraform/terraform.tfvars`:

   ```hcl
   subscription_id = "your-subscription-id-here"
   ```

   > Run `az account show --query id -o tsv` to get your subscription ID.

3. **Review defaults** in `terraform/terraform.tfvars`. All resources are created in the `pii-shield` resource group in `centralindia` by default:

   ```hcl
   resource_group_name = "pii-shield"
   location            = "centralindia"
   acr_name            = "piishieldacr"       # must be globally unique
   redis_name          = "pii-shield-redis"
   aca_env_name        = "pii-shield-env"
   log_analytics_name  = "pii-shield-logs"
   app_insights_name   = "pii-shield-insights"
   ```

---

## Step 2 — Bootstrap Terraform State Backend

Terraform state contains sensitive values (Redis keys, App Insights connection strings). The state is stored **remotely in Azure Storage** — not in local files or Git.

Run the bootstrap script **once** to create the dedicated storage account:

```bash
cd infra/scripts
./00-bootstrap-state.sh
```

This creates:
- Resource group `terraform-state-rg` (separate from app resources)
- Storage account `piishieldtfstate` with TLS-only, no public blob access
- Blob container `tfstate` for state files
- Entra ID RBAC (`Storage Blob Data Contributor`) for your user
- `CanNotDelete` lock to prevent accidental destruction

**Options:**

```bash
# Use a different storage account name or region
./00-bootstrap-state.sh --storage-account mystateaccount --location eastus
```

> ⚠️ The storage account name must be globally unique. If `piishieldtfstate` is taken, pass a different name and update the `backend` block in `terraform/main.tf` to match.

---

## Step 3 — Provision Infrastructure (Terraform)

```bash
cd infra/terraform

terraform init        # connects to remote state backend
terraform plan        # review what will be created
terraform apply       # type "yes" to confirm
```

This creates:
- Resource group `pii-shield`
- Azure Container Registry
- Azure Cache for Redis (Standard, TLS-only)
- Log Analytics workspace
- Application Insights
- Container Apps Environment
- Azure Managed Grafana (v11, Standard SKU) with RBAC roles

> ⏱ **Redis provisioning takes ~15-20 minutes.** Other resources are fast.

---

## Step 4 — Build & Push Docker Image

```bash
cd infra/scripts
./01-build-push.sh
```

This builds the PII Shield Docker image **remotely in ACR** (no need to upload the ~3.8 GB image from your machine).

**Options:**

```bash
# Use a different NLP engine or model
./01-build-push.sh --nlp-engine spacy
./01-build-push.sh --model ai4bharat/IndicNER

# Tag a specific version
./01-build-push.sh --tag v1.0.0
```

---

## Step 5 — Deploy Container Apps

```bash
./02-deploy-apps.sh
```

This deploys all three container apps:

| App | CPU / Memory | Ingress | Scale |
|-----|-------------|---------|-------|
| `pii-shield` | 2 vCPU / 4 GiB | External (HTTPS) | 0–96 (scale to zero) |
| `playground` (Streamlit Playground) | 1 vCPU / 2 GiB | External (HTTPS) | 0–3 (scale to zero) |
| `pii-admin` (Streamlit Admin) | 1 vCPU / 2 GiB | External (HTTPS) | 0–2 (scale to zero) |

On completion, the script prints all service URLs:

```
═══════════════════════════════════════════════════════════════
  PII Shield — Azure Container Apps Deployment Complete
═══════════════════════════════════════════════════════════════

  API:           https://pii-shield.*.azurecontainerapps.io
  Playground:    https://playground.*.azurecontainerapps.io
  Admin:         https://pii-admin.*.azurecontainerapps.io
═══════════════════════════════════════════════════════════════
```

> **HTTPS is automatic** — Azure provides free TLS certificates on `*.azurecontainerapps.io`.

---

## Step 6 — Verify

```bash
./03-verify.sh
```

Runs health checks and a smoke test against the deployed API.

---

## Step 7 — Deploy Grafana Dashboards

Terraform provisions Azure Managed Grafana in Step 3. After `terraform apply`:

```bash
cd infra/scripts
./04-deploy-dashboards.sh
```

This uploads 3 dashboards to a "PII Shield" folder in Managed Grafana:

| Dashboard | Description |
|-----------|-------------|
| PII Shield Analytics (Azure) | Request rates, entity categories, HTTP latency |
| PII Shield — By Application (Azure) | Per-app breakdown with dynamic app selector |
| PII Shield — Errors (Azure) | Error rates, error logs, traces |

Dashboards use **KQL queries** against the Azure Monitor datasource (App Insights / Log Analytics). The Grafana instance's system-assigned identity is pre-configured with the required RBAC roles (Monitoring Reader, Log Analytics Reader).

---

## Environment Variables

The deploy script configures these automatically. For reference:

| Variable | Value | Notes |
|----------|-------|-------|
| `REDIS_URL` | `rediss://:KEY@host:6380/0` | Stored as ACA secret (URL-encoded key) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `InstrumentationKey=...;IngestionEndpoint=...` | Direct Azure Monitor export via OTel |
| `ENCRYPTION_BACKEND` | `pqc` | Post-quantum encryption (ML-KEM-768 + AES-256-GCM) |
| `NLP_ENGINE` | `transformers` | From build args |
| `TRANSFORMERS_MODEL` | `dslim/bert-base-NER` | From build args |
| `SESSION_TTL_SECONDS` | `86400` | 24-hour session expiry |
| `API_BASE` | `http://pii-shield` | Streamlit UIs → API (internal ACA network) |

> **Note:** The app exports telemetry directly to Application Insights using the `azure-monitor-opentelemetry-exporter` SDK — not via a managed OTel sidecar. The Container Apps OTel agent configuration is for platform-level metrics only.

---

## Updating the Application

After code changes, rebuild and redeploy:

```bash
# Rebuild the image
./01-build-push.sh

# Update the running container apps (pulls new image)
az containerapp update --name pii-shield --resource-group pii-shield --image piishieldacr.azurecr.io/pii-shield:latest
az containerapp update --name playground --resource-group pii-shield --image piishieldacr.azurecr.io/pii-shield:latest
az containerapp update --name pii-admin --resource-group pii-shield --image piishieldacr.azurecr.io/pii-shield:latest
```

---

## State Management

Terraform state is stored remotely in Azure Blob Storage (`piishieldtfstate` / `tfstate` container). This provides:

- **Locking** — Automatic blob lease prevents concurrent corruption
- **Encryption** — At rest (Azure default) + in transit (HTTPS only)
- **Access control** — Entra ID auth; no shared storage keys
- **Protection** — `CanNotDelete` resource lock on the storage account

If a Terraform operation is interrupted and the state lock is stuck:

```bash
terraform force-unlock <LOCK_ID>
```

> ⚠️ The state backend lives in `terraform-state-rg`, separate from the `pii-shield` resource group. Running `terraform destroy` removes application resources but **not** the state backend.

---

## Tear Down

To remove **everything** (all Azure resources):

```bash
cd infra/terraform
terraform destroy    # type "yes" to confirm
```

> ⚠️ This also deletes the Container Apps, even though they were created by scripts. They live in the resource group that Terraform manages.

---

## Cost Estimate (~Central India)

| Resource | Monthly Cost |
|----------|-------------|
| Container App: pii-shield (2 vCPU, 0–96 replicas) | ~$95 |
| Container App: playground (scale to zero) | ~$0–48 |
| Container App: pii-admin (scale to zero) | ~$0–48 |
| Azure Cache for Redis (Standard C1) | ~$100 |
| Azure Container Registry (Basic) | ~$5 |
| Application Insights | ~$0–10 |
| Azure Managed Grafana (Standard) | ~$20 |
| **Total** | **~$220–326/mo** |

---

## File Structure

```
infra/
├── README.md                 ← this file
├── terraform/
│   ├── main.tf               # Provider, backend, resource group
│   ├── variables.tf          # Input variables
│   ├── terraform.tfvars      # Your configuration
│   ├── acr.tf                # Container Registry
│   ├── redis.tf              # Azure Cache for Redis
│   ├── log_analytics.tf      # Log Analytics workspace
│   ├── app_insights.tf       # Application Insights
│   ├── container_env.tf      # Container Apps Environment
│   ├── grafana.tf            # Azure Managed Grafana + RBAC
│   └── outputs.tf            # Values used by scripts
└── scripts/
    ├── 00-bootstrap-state.sh # Create state backend (run once)
    ├── 01-build-push.sh      # Build image in ACR
    ├── 02-deploy-apps.sh     # Deploy all container apps
    ├── 03-verify.sh          # Health checks & smoke tests
    └── 04-deploy-dashboards.sh # Upload Grafana dashboards
```
