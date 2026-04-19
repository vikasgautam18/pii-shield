# PII Shield — Infrastructure

This folder contains infrastructure-as-code and lifecycle scripts for deploying
PII Shield to **Azure Container Apps**.

For a full end-to-end deployment walkthrough, see **[../DEPLOYMENT.md](../DEPLOYMENT.md)**.

## Layout

```
infra/
├── terraform/             # Terraform: ACR, Redis, App Insights, ACA env, Grafana
│   ├── main.tf            # Backend + provider (backend uses -backend-config)
│   ├── variables.tf       # All input variables (defaults safe for greenfield)
│   ├── terraform.tfvars.example   # Copy to terraform.tfvars and fill in
│   ├── acr.tf
│   ├── redis.tf
│   ├── log_analytics.tf
│   ├── app_insights.tf
│   ├── container_env.tf
│   ├── grafana.tf
│   └── outputs.tf
└── scripts/
    ├── 00-bootstrap-state.sh    # Provision Terraform state storage account (run once)
    ├── 01-build-push.sh         # Build + push Docker image via ACR cloud build
    ├── 02-deploy-apps.sh        # Deploy 3 container apps (reads Terraform outputs)
    ├── 02-deploy-apps_noref.sh  # Same as 02-deploy-apps.sh, but reads from Azure (no Terraform required)
    ├── 03-verify.sh             # Smoke tests for the deployed services
    └── 04-deploy-dashboards.sh  # Upload Grafana dashboards to Azure Managed Grafana
```

## Architecture

```
                     Internet (HTTPS)
                          |
         +----------------+----------------+
         |                |                |
  +------v------+  +------v-------+ +------v-------+
  | pii-shield  |  | playground   | | pii-admin    |
  |  (API:8000) |  |  (UI:7860)   | |  (UI:7861)   |
  +------+------+  +------+-------+ +------+-------+
         |                |                |
         |                +-- http://pii-shield --+
         |                (internal ACA network)
  +------v------+ +--------------------+ +------------------+
  | Azure Cache | | Azure App Insights |--->| Log Analytics |
  | for Redis   | | (OTel exporter)    |    | Workspace     |
  | (TLS)       | +--------------------+    +-------+-------+
  +-------------+                                   |
                                          +---------v---------+
                                          | Azure Managed     |
                                          | Grafana (v11)     |
                                          | KQL dashboards    |
                                          +-------------------+
```

| Azure Resource | Purpose | SKU (default) |
|----------------|---------|---------------|
| Container Apps Environment | Hosts all 3 apps | Consumption (serverless) |
| Azure Container Registry | Stores Docker image | Basic |
| Azure Cache for Redis | Sessions + app registry | Standard C1 |
| Application Insights | Traces, logs, metrics | Pay-per-use |
| Log Analytics Workspace | Backing store for logs | PerGB2018 |
| Azure Managed Grafana | Observability dashboards (KQL) | Standard |

## Quick start

```bash
# 1. Provision Terraform state backend (once)
./scripts/00-bootstrap-state.sh --project-name piishield
# (or: --storage-account <your-globally-unique-name>)
# Prints the terraform init command to run next.

# 2. Configure deployment values
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — set subscription_id and project_name

# 3. Initialize Terraform with the printed -backend-config flags
terraform init -backend-config="..." -backend-config="..."

# 4. Apply
terraform apply

# 5. Build and deploy
cd ../scripts
./01-build-push.sh
./02-deploy-apps.sh
./03-verify.sh
./04-deploy-dashboards.sh
```

See [../DEPLOYMENT.md](../DEPLOYMENT.md) for the full guide with explanations,
prerequisites, environment variable reference, and troubleshooting.

## Customization

All resource names derive from `project_name` in `terraform.tfvars`.
Globally-unique resources (ACR, Redis, Grafana) get an auto-generated
5-character random suffix; set `name_suffix` in tfvars to pin it.

| Variable | Default | Notes |
|----------|---------|-------|
| `project_name` (TF) | `piishield` | Prefix for all resource names |
| `name_suffix` (TF) | *(random)* | Suffix for globally-unique resources |
| `API_APP_NAME` | `<project_name>` | FastAPI service container app name |
| `PLAYGROUND_APP_NAME` | `<project_name>-playground` | Streamlit user UI |
| `ADMIN_APP_NAME` | `<project_name>-admin` | Streamlit admin UI |
| `IMAGE_TAG` | `latest` | Docker image tag |
| `NLP_ENGINE` | `onnx` | NLP backend (spacy / transformers / onnx) |
| `REDIS_AUTH_MODE` | `entra` | `entra` (managed identity) or `key` |
| `WEB_CONCURRENCY` | `2` | Gunicorn workers per replica |

The `02-deploy-apps.sh` script reads names from Terraform outputs automatically.
Override via env vars if needed.

See `infra/terraform/variables.tf` for the full Terraform variable reference.

## Tear down

```bash
cd terraform
terraform destroy
```

This removes the Container Apps, Redis, ACR, App Insights, and Managed Grafana.
The Terraform state storage account (provisioned by `00-bootstrap-state.sh`) is
**not** destroyed — delete it manually if desired.
