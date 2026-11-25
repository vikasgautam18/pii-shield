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
  accounts, ACR, Azure Managed Redis, Container Apps Environment, App Insights, and
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

location = "centralindia"

# Azure Managed Redis (replaces classic Azure Cache for Redis, which is blocked for
# new customers and retires on 30 Sep 2028). Balanced_B0 is the smallest size.
redis_sku_name                  = "Balanced_B0"
redis_high_availability_enabled = false   # off for dev/test; turn on for production
```

All resource names are derived:

| Resource | Name pattern |
|----------|--------------|
| Resource group | `<project_name>-rg` |
| Container Apps env | `<project_name>-env` |
| Log Analytics | `<project_name>-logs` |
| Application Insights | `<project_name>-insights` |
| ACR | `<project_name>acr<suffix>` |
| Redis | `<project_name>-redis-<suffix>` (Azure Managed Redis) |
| Grafana | `<project_name>-g-<suffix>` (max 23 characters) |

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

### Optional: require sign-in

Authentication is **off by default**. One switch, `AUTH_ENABLED`, covers the API
and both UIs. Register the application with your identity provider first
(Microsoft Entra ID or any OpenID Connect provider — see
[docs/authentication.md](docs/authentication.md#identity-provider-setup)), then
pass the settings from your shell or CI secret store. Secrets become Container
Apps secrets; nothing is written to a file in the repo:

```bash
export AUTH_ENABLED=true
export OIDC_ISSUER_URL="https://login.microsoftonline.com/<tenant-id>/v2.0"
export OIDC_AUDIENCE="<api-client-id>"
export OIDC_API_SCOPE="api://<api-client-id>/access_as_user"
export OIDC_CLIENT_ID="<ui-client-id>"
export OIDC_CLIENT_SECRET="<ui-client-secret>"
export AUTH_COOKIE_SECRET="$(openssl rand -base64 48)"
./02-deploy-apps.sh
```

The script prints the two redirect URIs (`https://<app>.<env-domain>/oauth2callback`)
to register with the identity provider. With authentication on, the UIs run as a
single replica, because Streamlit keeps sign-in state in process memory. Users
and services are granted access in the identity provider by assigning the
`PIIShield.User` / `PIIShield.Admin` roles, with no PII Shield change.

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

If you deployed with authentication on, the script notices (`GET /me` answers 401 without a token) and checks
that the API rejects anonymous calls. Give it an access token to also check that a valid token is accepted
(without one, the authenticated checks are skipped):

```bash
VERIFY_ACCESS_TOKEN="$(python ../../scripts/get_token.py)" ./03-verify.sh
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
| `SERVER_TIMING` | `true` | `Server-Timing` response header with the time of each step, on `/anonymize_unique` and `/deanonymize`; `false`, `0`, `no` or `off` removes it, and with it the Playground's timing breakdown (see [Requests take several seconds](#requests-take-several-seconds)) |
| `PII_STEP_SPANS` | `true` | One child span per step in every trace (Tempo, Application Insights); `false` keeps only the HTTP server span and `pii_shield.anonymize`. Metrics, the header and the log lines are not affected |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` (every trace) | Standard OpenTelemetry settings that decide which traces, and so which step spans, are exported; use them to limit span volume and Application Insights ingestion. `parentbased_traceidratio` with `0.25` keeps about a quarter of the traces. Metrics, logs and the header are not sampled |
| `OTEL_SERVICE_INSTANCE_ID` | `pii-shield-1` | Instance name in the OpenTelemetry resource of traces and logs. Metrics start from it too (else from `CONTAINER_APP_REPLICA_NAME`, which Azure Container Apps sets, else the host name) and append the process id, so each gunicorn worker exports its own series |
| `TRACE_URL_TEMPLATE` | empty | Playground only: a link pattern that contains `{trace_id}`; the timing panel then links each request to its trace |
| `AUTH_ENABLED` | `false` | One switch for sign-in on the API and both UIs (see below) |
| `OIDC_ISSUER_URL` / `OIDC_AUDIENCE` | — | Required when `AUTH_ENABLED=true`: the identity provider's issuer, and the audience the API accepts |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — | Required when `AUTH_ENABLED=true`: the client the UIs sign in with |
| `OIDC_API_SCOPE` | — | Scope that makes the IdP issue a token for this API (Entra: `api://<client-id>/access_as_user`) |
| `AUTH_COOKIE_SECRET` | — | Required when `AUTH_ENABLED=true`: 32+ random characters that sign the UI session cookie |
| `AUTH_USER_ROLES` / `AUTH_ADMIN_ROLES` | `PIIShield.User` / `PIIShield.Admin` | Role names that grant user / admin access |

`02-deploy-apps.sh` does not pass the timing settings, so a deployment runs with their defaults (header, step spans and the playground's panel on, every trace sampled). To change one, set it on the running app, for example
`az containerapp update --name pii-shield --resource-group <rg> --set-env-vars SERVER_TIMING=false`
(the `playground` app for `TRACE_URL_TEMPLATE`); each change creates a new revision.

The complete list of authentication settings, identity-provider setup and
troubleshooting are in [docs/authentication.md](docs/authentication.md).

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

To turn authentication on or off for apps that are already deployed (no image
rebuild), use `06-configure-auth.sh`. It changes all three apps together, in an
order that keeps every intermediate state usable, and each change creates a new
revision (the services restart):

```bash
OIDC_ISSUER_URL=... OIDC_AUDIENCE=... OIDC_CLIENT_ID=... \
OIDC_CLIENT_SECRET=... AUTH_COOKIE_SECRET=... \
./06-configure-auth.sh enable

./06-configure-auth.sh disable
```

Onboard calling services first: once authentication is on, API requests without
a valid token get `401`. See
[docs/authentication.md](docs/authentication.md#enabling-and-disabling) for the
recommended rollout.

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

### Requests take several seconds

Detection is CPU-bound: with the ONNX engine (what the Azure deployment runs) most of a request is the NER model, and a text that runs the second, case-recovery NER pass pays for it twice. Every `/anonymize_unique` request is timed in steps that do not overlap, so with `other` they add up to `total`, the server's time from the start of the request handling (authentication included) until the response headers are built; the steps are listed in the README under [Where a request spends its time](README.md#where-a-request-spends-its-time). To see where *one* slow request spent its time, read its breakdown in one of five places:

1. **The `Server-Timing` header** of a successful `/anonymize_unique` or `/deanonymize` (on a deployment use its URL, and add `-H "Authorization: Bearer $TOKEN"` when authentication is on), with the `traceresponse` header that names the request's trace (a last field of `00` means the sampler dropped it, so it has no step spans). A real response from a Docker container, for a text of 92 characters:

   ```
   $ curl -si http://localhost:8000/anonymize_unique -H 'Content-Type: application/json' \
       -d '{"text": "Please send the signed form to Rahul Sharma, PAN ABCDE1234F, phone +91 98765 43210, tomorrow"}' | grep -iE '^(server-timing|traceresponse):'
   server-timing: queue_wait;dur=0.30, nlp_pass1;dur=36.96, nlp_pass1.ner_model;dur=29.60, analyze;dur=5.46, analyze.context_enhance;dur=0.14, analyze.dedup;dur=0.06, post_processing;dur=0.05, handling;dur=0.02, store_save;dur=2.06, other;dur=1.11, total;dur=45.96
   traceresponse: 00-0745484b59f8776c3754f8753c872b89-b8cba77ef9e964c5-01
   ```

   A text that runs the second NER pass adds `recovery_pass2` and its details: `Dear RAHUL SHARMA, ...` gave `recovery_pass2;dur=22.03` next to `nlp_pass1;dur=28.40`.

2. **The Playground** shows the server total, the round trip and the slowest step under the result, and a collapsed "⏱ Timing breakdown" with a chart, a table and the trace id; `TRACE_URL_TEMPLATE` (an http(s) URL containing `{trace_id}`) turns the trace id into an *Open trace* link. Without `Server-Timing` (an older API, or `SERVER_TIMING=false`) it only shows the round trip.

3. **The log lines** (each carries its request's trace id; locally `docker compose logs app`, in Grafana the Loki query `{service_name="pii-shield"} |= "Unique anonymization complete"`). The first line below repeats the steps; the second is logged for every HTTP request, errors included (they carry no `Server-Timing`) and `/health` excluded, and runs until the last response byte was handed to the server, a little more than `total`:

   ```
   Unique anonymization complete: 3 entities, id=3a57670f-ff15-4f05-b24b-5cc7845df4e3 chars=92 total_ms=46.0 ner_windows=1 steps=queue_wait:0.3,nlp_pass1:37.0,analyze:5.5,post_processing:0.0,handling:0.0,store_save:2.1,other:1.1
   POST /anonymize_unique -> 200 in 49.4 ms
   ```

4. **The trace** of a request is the server span with, below it, `auth` (when it is on), `app_config` (with an `X-App-Id`), `store_save` and `pii_shield.anonymize`, whose children are `queue_wait`, `nlp_pass1`, `analyze`, `recovery_pass2` (when it ran), `post_processing` and `handling`. A step span carries `pii.step` and its details as `pii.detail.<detail>_ms`; open the trace by the id from the header or the Playground, or search Tempo with the TraceQL query `{ resource.service.name="pii-shield" && name="nlp_pass1" && duration > 500ms }` (the first NLP pass took more than 0.5 s).

5. **The histogram `pii.step.duration`** gives trends and percentiles per step: in Prometheus `pii_step_duration_milliseconds_{bucket,sum,count}` with the labels `step` (a name with a dot is a detail, part of its step) and `size` (the text length class), for example the p95 of each top-level step:

   ```promql
   histogram_quantile(0.95, sum by (le, step) (rate(pii_step_duration_milliseconds_bucket{service_name="pii-shield", step!~".*[.].*"}[5m])))
   ```

**After upgrading, read the metrics over a time range that starts after the deploy.** Each gunicorn worker now exports its own series (`service.instance.id` is `<OTEL_SERVICE_INSTANCE_ID, else CONTAINER_APP_REPLICA_NAME, else the host name>-<pid>`); before, the workers of a replica shared one id and overwrote each other's counters in Prometheus, and the old series stay in the window for a while, so `increase()` and `rate()` panels read too high. The histogram buckets now reach 120 s (those of `http.server.duration` too), so a request that took 10 to 20 s no longer reads as exactly 10 s.

**On Azure** (no Azure deployment was available to try this on; the Application Insights facts follow the exporter's source):

- The log lines and both headers come from the app, whatever the telemetry backend.
- INTERNAL step spans become Application Insights `dependencies` rows with type `InProc`, `name` = the step name and the step in `customDimensions["pii.step"]` (details in `customDimensions["pii.detail.<detail>_ms"]`); span durations are rounded to whole milliseconds.
- Histograms reach `customMetrics` as sum, count, min and max, without buckets: averages and maxima per step, no percentiles (those come from the span rows). `cloud_RoleInstance` comes from `service.instance.id`, so metric rows now carry one instance per worker process.
- Span volume is the cost to watch: 8 to 11 spans per request instead of 5. `OTEL_TRACES_SAMPLER=parentbased_traceidratio` with `OTEL_TRACES_SAMPLER_ARG=0.25`, or `PII_STEP_SPANS=false`, reduces it.

**What the numbers tell you**

| You see | It means | What to do |
|---------|----------|------------|
| `queue_wait` large | All `NLP_THREAD_POOL_SIZE` threads of that worker were busy | Add capacity: more replicas (`API_MAX_REPLICAS` caps them) or vCPUs |
| Every step slower than on an idle replica, `queue_wait` small | Concurrent detections share the CPU: the replica is saturated | The same |
| `nlp_pass1` large, mostly `nlp_pass1.ner_model` | The model is the cost: many `ner_windows` mean a long text, one or two a slow or shared CPU | Shorter texts, or more vCPUs |
| `recovery_pass2` present | ALL-CAPS words or lowercase runs that could be an uncased name (most e-mail addresses and URLs have one): the model ran twice | Expected for such texts |
| `analyze` large, `nlp_pass1.ner_model` small | The regex and validator recognizers; very many matches in one text also inflate `analyze.dedup` and `analyze.context_enhance` | Profile that text locally (custom regexes first); send shorter texts |
| `post_processing` or `handling` large | The pipeline functions after recognition; applying the strategies (the PQC `encrypt` does a key encapsulation per value) | Profile that text; compare it under `replace` |
| `store_save` or `app_config` large | The Redis round trip | Check the network path to Redis and its load |
| `auth` large | Bearer-token validation | Check the identity provider's key endpoint and `OIDC_JWKS_CACHE_SECONDS` |
| `other` large | Work nobody timed | Look at the gaps between the spans in the trace |
| Round trip much longer than `total` | Outside the server: network, ingress, a replica starting from zero (see *Cold-start latency* above) or the client | Not the server's detection |

### `04-deploy-dashboards.sh` fails with `Insufficient privileges`

You need the **Grafana Admin** role on the Managed Grafana instance. Assign
yourself via Azure portal: *Managed Grafana > Access control (IAM) > Add role
assignment > Grafana Admin*.
