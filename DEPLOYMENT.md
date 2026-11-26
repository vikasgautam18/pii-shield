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
script in a browser — Entra ID SSO is configured automatically. One of them,
*PII Shield — Latency (Azure)*, shows where requests spend their time (see
[Requests take several seconds](#requests-take-several-seconds)); its queries are built
from ones that were run against a real workspace, but the dashboard itself has not been deployed yet.

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
| `WEB_CONCURRENCY` | `2` | Gunicorn workers per replica (about one per vCPU) |
| `NLP_THREAD_POOL_SIZE` | `1` | Concurrent NLP inferences per worker. Detection is CPU-bound, so concurrency comes from workers, not threads |
| `NLP_PRIORITY_SECONDS_PER_KCHAR` | `1` | Short texts are served ahead of long documents queued just before them; `0` = first come, first served |
| `NLP_EXPRESS_MAX_CHARS` | `1000` | Texts up to this many characters run on an express thread beside a long document instead of waiting for it; `0` = no express lane |
| `NLP_EXPRESS_THREADS` | `1` | Express threads per worker |
| `ORT_INTRA_OP_THREADS` | `2` (more on a bigger replica, see `API_CPU`) | ONNX Runtime threads one detection uses |
| `CASE_RECOVERY_GATE` | `smart` | `smart` skips the second NER pass when the only words it would re-case are inside e-mail addresses, URLs or IDs, or are ordinary English words; `legacy` runs it for any ALL-CAPS word or run of lowercase words |
| `ONNX_TRIM_PADDING` | `true` | Runs the last, shorter window of a long text at its real length instead of padding it to 512 tokens |
| `FAST_ANALYSIS` | `true` | Linear-time duplicate removal and context enhancer; the results are identical, only faster on documents with many matches |
| `DISABLED_RECOGNIZERS` | (see `.env.example`) | Comma-separated Presidio recognizer class names |
| `SERVER_TIMING` | `true` | `Server-Timing` response header with the time of each step, on `/anonymize_unique` and `/deanonymize`; `false`, `0`, `no` or `off` removes it, and with it the Playground's timing panel (see [Requests take several seconds](#requests-take-several-seconds)) |
| `PII_STEP_SPANS` | `true` | One child span per step in every trace (Tempo, Application Insights); `false` keeps only the spans that existed before. Metrics, the header and the log line are not affected |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` (every trace) | Standard OpenTelemetry settings that decide which traces, and so which step spans, are exported; use them to limit span volume and Application Insights ingestion. `parentbased_traceidratio` with `0.25` keeps about a quarter of the traces. Metrics, logs and the header are not sampled |
| `TRACE_URL_TEMPLATE` | empty | Playground only: a link pattern that contains `{trace_id}`; the timing panel then links each request to its trace |
| `AUTH_ENABLED` | `false` | One switch for sign-in on the API and both UIs (see below) |
| `OIDC_ISSUER_URL` / `OIDC_AUDIENCE` | — | Required when `AUTH_ENABLED=true`: the identity provider's issuer, and the audience the API accepts |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — | Required when `AUTH_ENABLED=true`: the client the UIs sign in with |
| `OIDC_API_SCOPE` | — | Scope that makes the IdP issue a token for this API (Entra: `api://<client-id>/access_as_user`) |
| `AUTH_COOKIE_SECRET` | — | Required when `AUTH_ENABLED=true`: 32+ random characters that sign the UI session cookie |
| `AUTH_USER_ROLES` / `AUTH_ADMIN_ROLES` | `PIIShield.User` / `PIIShield.Admin` | Role names that grant user / admin access |

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

The transformers/onnx engine loads its model on first request. With the default
`API_MIN_REPLICAS=0` the API scales to zero when idle, and the first request after an idle spell
waits for the cold start (image pull, model load, warm-up: up to a minute). Deploy with
`API_MIN_REPLICAS=1 ./02-deploy-apps.sh` to keep one replica warm.

### Requests take several seconds

Detection is CPU-bound: the NER model is 85-95% of a request (713 of 785 ms in the one text we profiled, 2.7 KB on two
vCPUs), and a text that has ALL-CAPS or uncased names goes through it twice. To see where *one* slow request spent its
time, read its **step breakdown**. Every `/anonymize_unique` request is timed in steps that do not overlap, so they add
up to the time the server spent on it (`total`):

| Step | What it covers | Present |
|------|----------------|---------|
| `auth` | Bearer-token validation | only with `AUTH_ENABLED=true` |
| `app_config` | The application's settings from Redis (cached for 5 s per worker) | only with an `X-App-Id` header |
| `queue_wait` | Waiting for a free detection thread | always (close to 0 on an idle replica) |
| `nlp_pass1` | First NLP pass: spaCy tokenizer, tagger and lemmatizer, the NER model, decoding | always |
| `analyze` | Presidio `analyze()` of that pass: regex and validator recognizers, reading the NER result, context scoring, duplicate removal, threshold, allow-lists | always |
| `recovery_pass2` | The second NER pass, over the re-cased text, with its own recognition and merge | only when the pass ran (ALL-CAPS or uncased names) |
| `post_processing` | The pipeline functions after recognition: titles, line breaks, address merge, overlap removal | always |
| `handling` | Applying the strategies (replace / hash / fake / encrypt) and rebuilding the text | always |
| `store_save` | Writing the session mapping to Redis | always |
| `other` | `total` minus the steps above, never below 0: whatever is not timed (the small work between the steps, such as merging allow-lists, the check that decides on the second pass, counting entities, and hand-offs between threads) | always |

A step can have **details**, written `<step>.<detail>`: `nlp_pass1.ner_model` (time inside the ONNX model),
`analyze.context_enhance`, `analyze.dedup` (duplicate removal), and the same three under `recovery_pass2`. A detail is
*part of* its step, so never add it to the steps. What is left of `nlp_pass1` after `ner_model` is spaCy and decoding;
what is left of `analyze` after `context_enhance` and `dedup` is the regex and validator recognizers. `ner_windows`
counts the windows of up to 512 tokens the NER model ran over all passes, and model time grows with it.
`/deanonymize` has a lighter breakdown, in the `Server-Timing` header and the Playground only (no spans, no histogram,
no log fields): `auth`, `app_config`, `store_get`, `restore` (placeholders back to the original values) and `other`.
Everything below about spans, histograms and the log line is about `/anonymize_unique`.

**Reading the breakdown.** The same numbers reach five places:

1. **The log lines.** Every `/anonymize_unique` logs one line. The fields that were always there stay; `total_ms`
   (the `total` of the header), `ner_windows` and `steps` (the steps that ran in execution order, then `other`, in
   milliseconds with one decimal) are new, and so are the second and third lines below:

   ```
   Unique anonymization complete: 4 entities, id=..., chars=99, queue_ms=1, detect_ms=44 (nlp_ms=37, recovery_pass=False, recovery_ms=0) total_ms=48.4 ner_windows=1 steps=queue_wait:0.9,nlp_pass1:36.7,analyze:6.8,post_processing:0.1,handling:0.0,store_save:2.8,other:1.2
   De-anonymization complete: id=... total_ms=0.4
   POST /anonymize_unique -> 200 in 54.4 ms
   ```

   The first two lines are written on success only. The last is logged for every HTTP request, errors included (they
   carry no `Server-Timing`) and `/health` excluded, by a middleware (`app/request_timing.py`); it runs until the last
   response byte was handed to the server, so it is a little more than `total_ms` (54.4 ms against 48.4 ms here).

   Locally: `docker compose logs app | grep "Unique anonymization complete"`. In Grafana, Explore the Loki data source
   with `{service_name="pii-shield"} |= "Unique anonymization complete"`; each line carries the trace id of its
   request. The same values are span attributes (`pii.queue_ms`, `pii.detect_ms`, `pii.nlp_ms`, `pii.recovery_ms`,
   `pii.recovery_pass`, `pii.chars`) and, with `size` and `recovery_pass` labels, the histograms
   `pii.detect.queue_wait` and `pii.detect.duration`.

2. **The `Server-Timing` header**, on successful `/anonymize_unique` and `/deanonymize` responses (a W3C header:
   `name;dur=<ms>`, steps in execution order, each followed by its details, then `other` and `total`). `total` runs
   from the start of authentication (of the handler, with authentication off) to the moment the headers are built, so
   it leaves out reading the request, writing the response and the network:

   ```bash
   curl -si http://localhost:8000/anonymize_unique -H 'Content-Type: application/json' \
     -d '{"text": "Hi, I am Rahul Sharma, call me on +91 98765 43210"}' | grep -iE '^(server-timing|traceresponse):'
   ```

   ```
   server-timing: queue_wait;dur=0.06, nlp_pass1;dur=13.05, nlp_pass1.ner_model;dur=8.82, analyze;dur=0.80, analyze.context_enhance;dur=0.04, analyze.dedup;dur=0.03, post_processing;dur=0.03, handling;dur=0.01, store_save;dur=0.38, other;dur=0.26, total;dur=14.59
   traceresponse: 00-f8ee2d1eb7cda7a91b3be0cfdc7bef13-883a4ebe6e8e358f-01
   ```

   (Both samples are from a development machine, to show the shape; the milliseconds mean nothing for your
   deployment.) The same request with an ALL-CAPS text, which makes the second NER pass run, adds its steps:
   `... analyze;dur=1.20, ..., recovery_pass2;dur=18.78, recovery_pass2.ner_model;dur=14.48,
   recovery_pass2.context_enhance;dur=0.02, recovery_pass2.dedup;dur=0.02, post_processing;dur=0.07, ...`.

   On a deployment, use its URL, and add `-H "Authorization: Bearer $TOKEN"` when authentication is on. The
   `traceresponse` header (every response carries it) names the trace of the request: the second field is the trace
   id, and a last field of `00` means the sampler dropped that trace, so no step spans were exported for it. An error
   response (such as a 404 for an unknown `X-App-Id`) carries `traceresponse` but no `Server-Timing`.
   `SERVER_TIMING=false` removes the `Server-Timing` header.

3. **The Playground.** After *Anonymize* and *De-anonymize* it shows the timing of that request: the
   server total, the round trip as the Playground measured it and the slowest step, then a "⏱ Timing breakdown"
   section, collapsed until you open it, with a chart and a table of the steps with their share (details indented
   under their step), and the trace id, with an *Open trace* link when
   `TRACE_URL_TEMPLATE` is set (an http(s) URL that contains `{trace_id}`; for a trace the sampler dropped it says so
   instead of linking). To make a template for your Grafana, open any trace in Explore, copy the address from the
   browser and replace the trace id in it with `{trace_id}` (no template format was tried here). The panel reads the
   header, so without `Server-Timing` it can only show the round trip.

4. **The Tempo waterfall.** Each request is one trace: the HTTP server span, below it `pii_shield.anonymize` with
   `queue_wait` and the library steps as children, and `auth`, `app_config` and `store_save` beside it. A step span
   carries `pii.step` and, when it has details, `pii.detail.<detail>_ms`; `pii_shield.anonymize` carries
   `pii.ner_windows`. Open a trace by its id (from the header or the Playground), or search with TraceQL (queries of
   this form ran against Tempo's HTTP API on the local stack, on test spans of the same shape; Grafana's Explore
   screen itself was not tried):

   ```
   # the first NLP pass took more than 0.5 s
   { resource.service.name="pii-shield" && name="nlp_pass1" && duration > 500ms }

   # traces whose steps average over 100 ms
   { resource.service.name="pii-shield" && span.pii.step != nil } | avg(duration) > 100ms

   # the steps of each trace
   { resource.service.name="pii-shield" && span.pii.step != nil } | select(span.pii.step)

   # p50 and p95 per step (a metrics query, see below)
   { resource.service.name="pii-shield" && span.pii.step != nil } | quantile_over_time(duration, .5, .95) by (name)
   ```

   Through the API (inside the `otel-lgtm` container) pass `start` and `end` in Unix seconds: without them the same
   search returned no traces.

   ```bash
   docker compose exec -T otel-lgtm curl -s -G localhost:3200/api/search \
     --data-urlencode 'q={ resource.service.name="pii-shield" && name="nlp_pass1" && duration > 500ms }' \
     --data-urlencode "start=$(( $(date +%s) - 3600 ))" --data-urlencode "end=$(date +%s)" --data-urlencode limit=20
   ```

   `select()` returns 3 spans per trace unless you raise `spss` (the API) to the number of steps. The last query is a
   TraceQL metrics query: send it to `/api/metrics/query_range` with a `step` (such as `300`), which returned values
   for spans two hours old, while the instant `/api/metrics/query` returned the series without values. Allow a few
   minutes after the spans arrive (a first attempt about a minute after ingestion returned empty series). Its
   percentiles are coarse, because Tempo's buckets double in size.

5. **The Grafana dashboard "PII Shield — Latency"** (local stack: Grafana at http://localhost:3000, then Dashboards;
   it is provisioned from `observability/grafana/dashboards/pii-shield-latency.json`). Its `size` variable limits the
   metric panels to one text-size class (`small` up to 500 characters, `medium` up to 2,000, `large` up to 8,000,
   `xlarge` above), and `slow_ms` sets what counts as a slow request. Three rows:
   - *Waiting or computing?* request time p95, the share of time requests spent waiting for a detection thread, the
     share of requests that ran a second NER pass, queue wait against detect time (p95), and detect time with and
     without the second pass.
   - *Where the time goes:* the average time per step (stacked), the share of the time per step, the p95 per step,
     the details (NER model, context scoring, duplicate removal: inclusive, never stacked with the steps) and two
     tables with the numbers (calls, average, p95, share).
   - *Slow requests:* the requests slower than `slow_ms` from Tempo (a TraceQL table: open one to see its steps), the
     per-step averages, p95 and call rates from Tempo's span metrics (sampled traces only, and coarse), and the log
     lines of the slow requests from Loki.

   The step panels read `pii_step_duration_milliseconds_{bucket,sum,count}` (labels `step` and `size`; a `step` with
   a dot is a detail) and the others read `pii_detect_*` and `http_server_duration_milliseconds_*`. The buckets reach
   120 s, so a 10 to 20 s request is no longer drawn as exactly 10 s. The step panels stay empty until the API that
   records `pii.step.duration` runs.

   **After an upgrade, pick a time range that starts after the deploy.** The previous build exported every gunicorn
   worker under one `service.instance.id`, so their cumulative counters overwrote each other and every drop counts as
   a reset. Those old series stay inside the window for a while and make the `increase()` and `rate()` panels read far
   too high: right after the upgrade on the local stack, "Requests Analysed" showed 214,913 for about 1,000 real
   requests, and 195 once the range started after the deploy. Each worker now has its own series
   (`<instance>-<pid>`), and the counts agree across the HTTP, detection and step metrics.

**On Azure** you get less. This was checked on a real deployment (Azure Container Apps and Application Insights, the
version of this change without the performance fixes); what was not checked is said below:

- The log lines and the `Server-Timing` and `traceresponse` headers come from the app, whatever the telemetry backend.
  The Container Apps ingress passes both headers through unchanged (seen with `curl -i` against the deployed app).
- The step spans are exported to Application Insights as `dependencies` rows with `type == "InProc"`, `name` = the
  step name, `operation_Id` = the trace id and the step in `customDimensions["pii.step"]` (details in
  `customDimensions["pii.detail.<detail>_ms"]`); the server span is a `requests` row. Both were seen in the workspace,
  and the waterfall query below returns a request with its step spans. The exporter rounds a duration to whole
  milliseconds, so a step shorter than half a millisecond reads 0 there.
- `cloud_RoleName` on Container Apps is `<environment>.<app>` (for example `pii-shield-env.pii-shield`), not the
  `OTEL_SERVICE_NAME`, so the queries and the Azure dashboard select it with `endswith "pii-shield"`.
  `cloud_RoleInstance` is `pii-shield-1` for requests, dependencies and traces, and `<replica>-<pid>` for the metrics
  (one per worker).
- The histograms (`pii.step.duration`, `pii.detect.duration`, `pii.detect.queue_wait`) reach `customMetrics` as sum,
  count, min and max, **without buckets**: `value` and `valueSum` both hold the sum and `valueCount` the count, so an
  average is `sum(value) / sum(valueCount)` (it matched the average of the step spans). There are no exemplars and no
  Tempo span metrics. That gives averages and maxima per step, not percentiles; percentiles per step have to come from
  the span rows. Fractions of a millisecond survive in the metric, not in the span durations.
- `04-deploy-dashboards.sh` uploads a fourth dashboard, "PII Shield — Latency (Azure)": request time from `requests`,
  the step spans and the `pii_shield.anonymize` attributes from `dependencies`, the average per step from
  `customMetrics` (not affected by sampling), the log lines of slow requests from `traces`, and a `trace_id` variable
  to see one request step by step. Its queries are built from the ones checked here, but the dashboard itself has not
  been deployed to Managed Grafana yet.
- The per-step KQL query and the waterfall query below were run against the workspace and return what is described.
  Whether the step spans nest under the request in the portal's end-to-end transaction view was not looked at.

  ```kusto
  // latency per step, from the step spans
  dependencies
  | where timestamp > ago(1h) and cloud_RoleName endswith "pii-shield" and type == "InProc"
  | where isnotempty(tostring(customDimensions["pii.step"]))
  | summarize calls = count(), avg_ms = avg(duration), p50 = percentile(duration, 50), p95 = percentile(duration, 95), max_ms = max(duration)
      by step = tostring(customDimensions["pii.step"])
  | order by avg_ms desc

  // one request's waterfall; the id comes from the traceresponse header
  union requests, dependencies
  | where operation_Id == "<trace id>"
  | project timestamp, itemType, name, duration, id, operation_ParentId
  | order by timestamp asc
  ```

- Span volume is the cost to watch. A request now makes 8 to 11 spans (the server span, `pii_shield.anonymize` and 6 to
  9 steps) where it made 5 before (the server span, `pii_shield.anonymize` and three ASGI `receive` / `send` spans of
  about 0 ms, which are gone), and each span is a row in Application Insights. What that costs has not been measured.
  To reduce it, set `OTEL_TRACES_SAMPLER=parentbased_traceidratio` with `OTEL_TRACES_SAMPLER_ARG=0.25` (a quarter of
  the traces, whole traces at a time; no step spans are created for a trace that is dropped, and a percentile taken
  from the span rows then describes that quarter, while the histograms, the log line and the header still cover every
  request), or set `PII_STEP_SPANS=false` to keep only `pii_shield.anonymize` under the server span.

**What the numbers tell you.**

| You see | It means | What to do |
|---------|----------|------------|
| `queue_wait` large (`queue_ms` in the log line) | The request waited for a free detection thread: the replicas are saturated | Add capacity (below) |
| `nlp_pass1` large, mostly `nlp_pass1.ner_model` | The text is long, or the model is slow on this CPU: look at `ner_windows` (many windows = a long text; few windows and still slow = the model itself) | Shorter texts, or more vCPUs per detection (below) |
| `recovery_pass2` present | The text has ALL-CAPS words or uncased names, so the model ran a second time, about as long as the first (377 ms against 388 ms in the profiled text) | Expected for statements and similar documents; `CASE_RECOVERY_GATE=smart` (the default) already skips it when it cannot find anything |
| `analyze` large while `nlp_pass1.ner_model` is small | The regex and validator recognizers (what is left after `analyze.context_enhance` and `analyze.dedup`) | `python scripts/profile_detection.py --file slow.txt` (inside a replica with `az containerapp exec`, or locally) prints every recognizer's cost, which finds a slow custom regular expression |
| `analyze.dedup` or `analyze.context_enhance` large | Very many matches in the document | `FAST_ANALYSIS=true` (the default) keeps both linear |
| `post_processing` large | The pipeline functions after recognition (1 ms in the profiled text) | Find the document that triggers it and profile it |
| `handling` large | Applying the strategies and rebuilding the text (0.06 ms for `replace` in the profiled text); the `encrypt` strategy's own cost has not been analysed | Compare with the same text under `replace` |
| `store_save` or `app_config` large | The Redis round trip | Check the network path to Redis and its load |
| `auth` large | Bearer-token validation | Check the identity provider's key endpoint and `OIDC_JWKS_CACHE_SECONDS` |
| `other` large | Work nobody timed | Look at the gaps between the spans in the trace |
| Round trip much longer than `total` | Network, writing the response, or the client | Not the server's detection |

- **Adding capacity (`queue_wait` is large):** the replicas are saturated. A replica answers as many requests at
  once as it has workers x `NLP_THREAD_POOL_SIZE` (2 by default) and a request beyond that waits, which is faster
  than sharing the CPU (measured: with 8 clients on 2 vCPUs the median 3 KB document took about 12 s with six
  inferences per worker and about 5 s with one). Add capacity: `API_CPU=4` (2 GiB of memory per vCPU follows), more
  replicas (`API_MAX_REPLICAS`), and a warm one (`API_MIN_REPLICAS=1`, so the first request after an idle spell does
  not wait for a cold start). `API_HTTP_CONCURRENCY` should stay at the number of detections a replica runs at once
  (its default), so the scaler adds a replica before requests queue.
- **One large document:** a request uses `ORT_INTRA_OP_THREADS` threads, so on a replica with more vCPUs give it
  more: `API_CPU=4 WEB_CONCURRENCY=1` runs one four-thread detection (about 1.6x faster for a lone request), while
  `API_CPU=4` with two workers answers twice as many requests at once.

Short texts (up to `NLP_EXPRESS_MAX_CHARS`, 1000 characters) run on an express thread beside the long document
instead of waiting for it. Without that, a 300-character text queued behind a 6 KB document waited seconds for
a tenth of a second of work; the cost is that long documents wait a little longer for the CPU they share.
`NLP_EXPRESS_MAX_CHARS=0` turns the lane off.

What the engine skips, and what it does not. The second NER pass is skipped when the only words it would re-case
are inside e-mail addresses, URLs or IDs, or are ordinary English words (`CASE_RECOVERY_GATE`); the last, shorter
window of a long text is not padded to 512 tokens (`ONNX_TRIM_PADDING`); runs of dots, hex digits and base64
are not fed to the model; and Presidio's quadratic duplicate removal and context enhancer are replaced by
equivalent linear ones (`FAST_ANALYSIS`). A document full of ALL-CAPS names (a bank statement) still needs both
passes, so it gains the least; the remaining levers are more vCPUs, and a faster model file on your CPU:
`ONNX_FUSE=true` fuses the BERT graph before quantization (1.57x per window on an Apple M4; x86 untested, entity
spans change slightly for about one text in ten). Measure with `scripts/bench_onnx_models.py` on the replica and
run the test suites before using it: `ONNX_FUSE=true ./01-build-push.sh`.

### `04-deploy-dashboards.sh` fails with `Insufficient privileges`

You need the **Grafana Admin** role on the Managed Grafana instance. Assign
yourself via Azure portal: *Managed Grafana > Access control (IAM) > Add role
assignment > Grafana Admin*.
