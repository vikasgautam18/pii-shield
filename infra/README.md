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
  │ Azure       │     │ Azure App Insights │────▶│ Log Analytics    │
  │ Managed     │     │ (OTel exporter)    │     │ Workspace        │
  │ Redis (TLS) │     └────────────────────┘     └────────┬─────────┘
  └─────────────┘                                         │
                                                 ┌────────▼─────────┐
                                                 │ Azure Managed    │
                                                 │ Grafana (v12)    │
                                                 │ KQL dashboards   │
                                                 └──────────────────┘
```

| Azure Resource | Purpose | SKU |
|----------------|---------|-----|
| Container Apps Environment | Hosts all 3 apps | Consumption (serverless) |
| Azure Container Registry | Stores Docker image | Basic |
| Azure Managed Redis | Sessions + app registry (Entra-only auth) | Balanced B0, non-HA (dev/test) |
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

3. **Choose names and sizing** in `terraform/terraform.tfvars` (copy `terraform.tfvars.example`). Resource names derive from `project_name` (default `piishield`): the resource group is `<project_name>-rg`, the apps are `<project_name>`, `<project_name>-playground` and `<project_name>-admin`, and globally unique resources (registry, Redis, Grafana) get a random 5-character suffix:

   ```hcl
   subscription_id = "your-subscription-id-here"
   project_name    = "piishield"
   location        = "centralindia"

   redis_sku_name                  = "Balanced_B0"   # Azure Managed Redis, smallest size
   redis_high_availability_enabled = false           # off for dev/test (a node failure can lose data)
   ```

---

## Step 2 — Bootstrap Terraform State Backend

Terraform state contains sensitive values (Redis keys, App Insights connection strings). The state is stored **remotely in Azure Storage** — not in local files or Git.

Run the bootstrap script **once** to create the dedicated storage account:

```bash
cd infra/scripts
./00-bootstrap-state.sh --project-name piishield --location centralindia
```

This creates (names derive from `--project-name`):
- Resource group `<project>-tfstate-rg` (separate from app resources)
- Storage account `<project>tf<random>` with TLS-only, no public blob access and shared-key access disabled
- Blob container `tfstate` for state files (state key `<project>.terraform.tfstate`)
- Entra ID RBAC (`Storage Blob Data Contributor`) for your user
- `CanNotDelete` lock to prevent accidental destruction
- Registration of the Azure resource providers the stack needs, such as `Microsoft.App` and `Microsoft.Dashboard`, which new subscriptions do not have

The script prints the exact `terraform init -backend-config=...` command to run in the next step.

**Options:**

```bash
# Choose the storage account yourself (3-24 lowercase alphanumeric characters, globally unique)
./00-bootstrap-state.sh --storage-account mystateaccount --location eastus
```

---

## Step 3 — Provision Infrastructure (Terraform)

```bash
cd infra/terraform

# Use the command printed by 00-bootstrap-state.sh (the backend settings differ per account):
terraform init \
  -backend-config="resource_group_name=<project>-tfstate-rg" \
  -backend-config="storage_account_name=<storage-account>" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=<project>.terraform.tfstate" \
  -backend-config="use_azuread_auth=true"

terraform plan -out=tfplan    # review what will be created
terraform apply tfplan
```

This creates:
- Resource group `<project_name>-rg`
- Azure Container Registry
- Azure Managed Redis (Balanced B0, Entra-only authentication, TLS-only) and an access policy for the signed-in operator
- Log Analytics workspace
- Application Insights
- Container Apps Environment
- Azure Managed Grafana (Standard SKU, major version `grafana_major_version`, default 12) with RBAC roles

> ⏱ **Managed Redis is by far the slowest resource to provision** (the provider allows up to 45 minutes; about 8 minutes in practice in Central India). The other resources are quick.

> **Why Azure Managed Redis?** Classic Azure Cache for Redis (Basic/Standard/Premium) can no longer be created by new customers (blocked since 1 April 2026; a tenant counts as an existing customer only if a subscription in it had a cache before that date) and retires on 30 September 2028.

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
| `playground` (Streamlit Playground) | 1 vCPU / 2 GiB | External (HTTPS) | 0–3 (scale to zero); 0–1 with authentication on |
| `pii-admin` (Streamlit Admin) | 1 vCPU / 2 GiB | External (HTTPS) | 0–2 (scale to zero); 0–1 with authentication on |

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

### Authentication (optional)

Authentication is **off by default**. With `AUTH_ENABLED=true`, the same
`02-deploy-apps.sh` run turns on sign-in for the API and both UIs (OpenID Connect,
e.g. Microsoft Entra ID), using the `OIDC_*` and `AUTH_COOKIE_SECRET` values from
your shell; it stores the secrets as Container Apps secrets and prints the redirect
URIs to register with the identity provider. Because Streamlit keeps sign-in state
in process memory, the two UIs are limited to a single replica while it is on.

To switch an existing deployment without rebuilding the image, use
`./06-configure-auth.sh enable|disable`. Each switch creates new revisions of the three apps, and the script
waits until they are serving (`ROLLOUT_TIMEOUT_SECONDS`, default 600), because `az containerapp update` returns
while the previous revision still answers. In a test or demo tenant,
`./07-setup-entra.sh create` sets up everything the identity provider side needs
(app registration, roles, test users and services) and writes the values for
`06-configure-auth.sh` to a private file; `./07-setup-entra.sh delete` removes it all.
Tenants that enforce MFA registration (Entra Security Defaults) make each new test user register an
authenticator at their first sign-in; the service-principal tokens used by `03-verify.sh` are unaffected.
See [docs/authentication.md](../docs/authentication.md) for identity-provider setup,
granting access to users and services, and troubleshooting.

---

## Step 6 — Verify

```bash
./03-verify.sh
```

Runs health checks and a smoke test against the deployed API. If authentication is
on, pass the same switch: `AUTH_ENABLED=true ./03-verify.sh` also proves the API
rejects anonymous calls, and adding `VERIFY_ACCESS_TOKEN="$(python ../../scripts/get_token.py)"`
proves a valid token is accepted.

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
| `REDIS_AUTH_MODE` | `entra` | Password-free: the API's managed identity signs in to Redis with a Microsoft Entra token |
| `REDIS_HOST`, `REDIS_PORT` | `<name>.<region>.redis.azure.net`, `10000` | Managed Redis endpoint (TLS), from the Terraform outputs |
| `REDIS_ENTRA_USERNAME` | principal (object) ID | The API's managed identity, granted the `default` access policy by `02-deploy-apps.sh` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | `InstrumentationKey=...;IngestionEndpoint=...` | Direct Azure Monitor export via OTel |
| `ENCRYPTION_BACKEND` | `pqc` | Post-quantum encryption (ML-KEM-768 + AES-256-GCM) |
| `NLP_ENGINE` | `transformers` | From build args |
| `TRANSFORMERS_MODEL` | `dslim/bert-base-NER` | From build args |
| `SESSION_TTL_SECONDS` | `86400` | 24-hour session expiry |
| `API_BASE` | `http://pii-shield` | Streamlit UIs → API (internal ACA network) |
| `AUTH_ENABLED` | `false` | Set to `true` at deploy time to require sign-in on the API and both UIs |
| `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `AUTH_COOKIE_SECRET` | — | Required when `AUTH_ENABLED=true`; taken from your shell, `OIDC_CLIENT_SECRET` and `AUTH_COOKIE_SECRET` stored as ACA secrets |

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

Terraform state is stored remotely in Azure Blob Storage (storage account `<project>tf<random>`, container `tfstate`; the names are printed by `00-bootstrap-state.sh`). This provides:

- **Locking** — Automatic blob lease prevents concurrent corruption
- **Encryption** — At rest (Azure default) + in transit (HTTPS only)
- **Access control** — Entra ID auth; no shared storage keys
- **Protection** — `CanNotDelete` resource lock on the storage account

If a Terraform operation is interrupted and the state lock is stuck:

```bash
terraform force-unlock <LOCK_ID>
```

> ⚠️ The state backend lives in `<project>-tfstate-rg`, separate from the `<project>-rg` resource group. Running `terraform destroy` removes application resources but **not** the state backend.

---

## Tear Down

To remove **everything**, in this order:

```bash
# 1. Entra objects (they live in the tenant, not the resource group): app registrations, test users and services
./infra/scripts/07-setup-entra.sh delete

# 2. Container Apps (created by 02-deploy-apps.sh, so not tracked by Terraform)
cd infra/scripts && ./05-teardown-apps.sh

# 3. Everything Terraform manages
cd ../terraform && terraform destroy    # type "yes" to confirm
```

> ⚠️ Step 2 is required: `terraform destroy` fails with `ManagedEnvironmentHasContainerApps` while apps still exist in the Container Apps environment. Skip step 1 if you never ran `07-setup-entra.sh`.

---

## Cost Estimate (~Central India, list prices)

What the defaults in this repo cost (dev/test sizing, apps scale to zero):

| Resource | Monthly Cost |
|----------|-------------|
| Azure Container Registry (Basic) | ~$5 |
| Azure Managed Redis (Balanced B0, non-HA) | ~$12 |
| Azure Managed Grafana (Standard, one active user) | ~$41 (node ~$35 + $6 per active user) |
| Container Apps: API, playground, admin (scale to zero; monthly free grant, then usage-based) | ~$0–15 |
| Log Analytics + Application Insights (low volume) | ~$0 |
| Terraform state storage account | cents |
| **Total** | **~$60–70/mo** |

Larger settings cost more: high-availability Redis, a bigger SKU (`Balanced_B1` is about $24/mo), and an always-warm API (an idle 2 vCPU / 4 GiB replica is roughly $45/mo). The API scales out to `API_MAX_REPLICAS` (default 96); lower it on a small subscription so a load test cannot run up a bill.

> **Visual Studio and other spending-limit subscriptions** have a fixed monthly credit (typically $150 for Visual Studio Enterprise). With the spending limit on, exceeding it disables the subscription until the next cycle, so stay close to the numbers above.

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
│   ├── redis.tf              # Azure Managed Redis (+ operator access policy)
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
    ├── 04-deploy-dashboards.sh # Upload Grafana dashboards
    ├── 05-teardown-apps.sh   # Delete the container apps (run before terraform destroy)
    ├── 06-configure-auth.sh  # Turn authentication on/off for deployed apps
    └── 07-setup-entra.sh     # Create the Entra app registration, roles and test users (test tenants)
```
