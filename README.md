# pii-shield

An intelligent anonymization layer that sits between your AI application and LLM — detecting PII, enforcing privacy actions, and enabling reversible de-anonymization via a secure store.

## Features

- **PII Detection** — Uses [Microsoft Presidio Analyzer](https://microsoft.github.io/presidio/analyzer/) to identify entities such as names, emails, phone numbers, addresses, and Indian-specific IDs (Driving License, Aadhaar, PIN Code, UPI ID). Adjacent location entities are automatically merged into composite ADDRESS entities.
- **PII Anonymization** — Uses [Microsoft Presidio Anonymizer](https://microsoft.github.io/presidio/anonymizer/) to replace detected entities with placeholders.
- **Configurable Anonymization Strategy** — Per-entity-type strategy (`replace`, `hash`, `encrypt`, or `fake`) configurable at runtime via per-app config endpoints (`PUT /apps/{app_id}/config`). All entity types default to `replace`. Encryption supports post-quantum (ML-KEM-768 + AES-256-GCM) and legacy Fernet backends.
- **REST API** — `POST /anonymize_unique`, `POST /deanonymize`, and app registration endpoints built with FastAPI.
- **Application Registration** — Register consuming applications via `POST /apps`. Each app gets its own per-entity PII strategy config stored in Redis. Pass `X-App-Id` header to use app-specific settings.
- **Reversible Anonymization** — Anonymize text with unique placeholders, pass it through an LLM, then restore the original PII via a session-based in-memory store.
- **Observability** — OpenTelemetry traces, metrics, and logs with dual-backend support: Azure Monitor (production) or OTLP gRPC (local dev). Pre-built Grafana dashboards for both environments.
- **Standalone Library** — Use `pii_shield` as an importable Python package for batch processing without FastAPI, Redis, or Docker. See [Using as a Library](#using-as-a-library) below.
- **Docker Support** — `docker-compose.yml` orchestrates Redis, Grafana LGTM, and the PII Shield app.
- **Azure Deployment** — Terraform IaC + shell scripts for Azure Container Apps, Redis, App Insights, and Managed Grafana. See [Azure Deployment Guide](infra/README.md).

## Using as a Library

PII Shield can be imported directly into your Python application for batch PII processing — no web server required.

### Install

```bash
# Core library (spaCy NLP backend, default)
pip install pii_shield-0.2.0-py3-none-any.whl

# Or install from source in editable mode
pip install -e .

# With optional extras
pip install "pii_shield-0.2.0-py3-none-any.whl[batch]"   # + pandas, tqdm
pip install "pii_shield-0.2.0-py3-none-any.whl[onnx]"    # ONNX Runtime backend
pip install "pii_shield-0.2.0-py3-none-any.whl[pqc]"     # Post-quantum encryption

# Download an NLP model (required — not bundled)
python -m spacy download en_core_web_lg                    # for spaCy (default)
```

### NLP Engine Configuration

The library reads `NLP_ENGINE` from the environment (default: `spacy`). Set it before importing:

| `NLP_ENGINE` | Install extra | Model download | Notes |
|---|---|---|---|
| `spacy` (default) | — | `python -m spacy download en_core_web_lg` | Best accuracy, larger model |
| `onnx` | `[onnx]` | `python -m spacy download en_core_web_sm` | 2-3× faster CPU inference; HF model auto-downloads |
| `transformers` | `[transformers]` | `python -m spacy download en_core_web_sm` | PyTorch BERT NER |
| `stanza` | `[stanza]` | `python -c "import stanza; stanza.download('en')"` | Stanford NLP |

For `onnx` / `transformers`, also set:
```bash
export TRANSFORMERS_MODEL=protectai/bert-base-NER-onnx   # HuggingFace model name
export TRANSFORMERS_SPACY_MODEL=en_core_web_sm           # tokenizer (default)
```

#### Optional: INT8 Quantized Model (ONNX only)

For ~2-3× faster CPU inference, generate an INT8 quantized model locally:

```bash
python scripts/quantize_model.py protectai/bert-base-NER-onnx models/onnx-int8
```

The engine automatically uses the quantized model from `models/onnx-int8` when present. To disable quantization and use FP32, set `QUANTIZE_MODEL=false` in your environment. The Docker image includes the quantized model by default.

### Environment Variables

| Variable | Default | When needed |
|---|---|---|
| `NLP_ENGINE` | `spacy` | Always (selects NLP backend) |
| `TRANSFORMERS_MODEL` | `dslim/bert-base-NER` | Only for `onnx` / `transformers` |
| `TRANSFORMERS_SPACY_MODEL` | `en_core_web_sm` | Only for `onnx` / `transformers` |
| `ENCRYPTION_BACKEND` | `pqc` | Only when using `encrypt` strategy |
| `PQC_ENCAPSULATION_KEY` | Auto-generated | PQC encrypt (set for production persistence) |
| `PQC_DECAPSULATION_KEY` | Auto-generated | PQC decrypt (set for production persistence) |
| `PII_SHIELD_ENCRYPTION_KEY` | — | Only when `ENCRYPTION_BACKEND=fernet` |
| `QUANTIZE_MODEL` | `true` | ONNX only — set `false` to skip INT8 model |
| `QUANTIZED_MODEL_DIR` | `models/onnx-int8` | ONNX only — path to quantized model |

> **Note:** The library does **not** auto-load `.env` files. Set env vars in your shell or programmatically before importing.

### Quick Start

```python
from pii_shield import PiiShieldEngine

engine = PiiShieldEngine()  # loads NLP model — create once, reuse!

# Anonymize
result = engine.anonymize("Rahul Sharma's Aadhaar is 2345 6789 0123")
print(result.anonymized_text)   # "<PERSON_1>'s Aadhaar is <IN_AADHAAR_1>"
print(result.entity_mapping)    # {"<PERSON_1>": "Rahul Sharma", "<IN_AADHAAR_1>": "2345 6789 0123"}

# De-anonymize
original = engine.deanonymize(result.anonymized_text, result.entity_mapping)
```

### Batch Processing

```python
from pii_shield import PiiShieldEngine, BatchProcessor

engine = PiiShieldEngine()
processor = BatchProcessor(engine=engine, parallelism="thread", max_workers=4)

# Process a list of texts
batch = processor.anonymize_texts(["Rahul from Mumbai", "Priya called from +919876543210"])
print(f"{batch.succeeded}/{batch.total} texts processed")

# Process a CSV file (streams row-by-row)
processor.anonymize_csv("input.csv", "output.csv", text_columns=["notes", "description"])

# Process a pandas DataFrame
import pandas as pd
df = pd.DataFrame({"text": ["Rahul lives at MG Road 560001"]})
result_df = processor.anonymize_dataframe(df, text_columns=["text"])
```

### Mapping Stores

Persist entity mappings for later de-anonymization:

```python
from pii_shield import PiiShieldEngine, SqliteMappingStore

engine = PiiShieldEngine()
store = SqliteMappingStore("mappings.db")

result = engine.anonymize("Rahul Sharma lives in Bangalore")
record_id = store.save(result)

# Later — retrieve and restore
saved = store.get(record_id)
original = engine.deanonymize(saved.anonymized_text, saved.entity_mapping)
```

Available stores: `InMemoryMappingStore`, `SqliteMappingStore`, `JsonFileMappingStore`.

### Detection Only

```python
entities = engine.detect("Priya's email is priya@example.com, UPI: priya@okaxis")
for e in entities:
    print(f"{e.entity_type}: '{e.text}' (score={e.score:.2f})")
```

See [`examples/library_usage.py`](examples/library_usage.py) for a full test suite using the Indian banking dataset (22 scenarios) with HTML report generation.

## Prerequisites

- Python 3.10+
- Redis (for application registration; see Docker Setup below)

> For the full list of requirements (including Azure deployment roles, environment
> variables, and NLP model downloads), see **[docs/prerequisites.md](docs/prerequisites.md)**.

## Local Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Download the NLP model (choose one)
# Option A: spaCy (default)
python -m spacy download en_core_web_lg

# Option B: Stanza
python -c "import stanza; stanza.download('en')"

# Option C: Transformers (BERT-based NER)
python -m spacy download en_core_web_sm
python -c "from transformers import AutoTokenizer, AutoModelForTokenClassification; AutoTokenizer.from_pretrained('dslim/bert-base-NER'); AutoModelForTokenClassification.from_pretrained('dslim/bert-base-NER')"
```

### Choosing the NLP Engine

PII Shield supports three NLP backends for Presidio Analyzer: **spaCy** (default), **Stanza**, and **Transformers** (HuggingFace BERT models). Set the `NLP_ENGINE` variable in your `.env` file:

```bash
# .env
NLP_ENGINE=spacy   # or "stanza" or "transformers"
```

| Engine | Model | Strengths |
|--------|-------|-----------|
| `spacy` | `en_core_web_lg` | Fast, mature, great for production workloads |
| `stanza` | `en` (Stanford NLP) | High accuracy NER, strong on formal/academic text |
| `transformers` | `dslim/bert-base-NER` (default) | BERT-based NER, supports custom HuggingFace models for domain-specific detection |

#### Transformers Engine Configuration

When using the `transformers` engine, two additional environment variables are available:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSFORMERS_MODEL` | `dslim/bert-base-NER` | HuggingFace token classification model for NER |
| `TRANSFORMERS_SPACY_MODEL` | `en_core_web_sm` | spaCy model used for tokenization |

You can swap in any HuggingFace NER model (e.g., `ai4bharat/IndicNER` for Indian language support).

## Docker Setup

The easiest way to run PII Shield with Redis is via Docker Compose:

```bash
# Default (spaCy engine)
docker compose up --build

# With Stanza engine
NLP_ENGINE=stanza docker compose up --build

# With Transformers engine (BERT-based NER)
NLP_ENGINE=transformers docker compose up --build

# With a custom HuggingFace model
NLP_ENGINE=transformers TRANSFORMERS_MODEL=ai4bharat/IndicNER docker compose up --build
```

This starts:
- **Redis** on port 6379 (data persisted via Docker volume)
- **PII Shield app** on port 8000

To run only Redis (and the app locally):

```bash
docker compose up redis -d
uvicorn app.main:app --reload
```

The app connects to Redis via the `REDIS_URL` environment variable (default: `redis://localhost:6379/0`).

## Running the Server

```bash
uvicorn app.main:app --reload
```

The server starts at **http://localhost:8000**. Interactive API docs are available at **http://localhost:8000/docs**.

## Gradio UI

Two web interfaces are included in the Docker Compose stack. They start automatically alongside the main API.

| UI | URL | Purpose |
|----|-----|---------|
| PII Shield UI | http://localhost:7860 | Anonymize & de-anonymize text |
| Admin UI | http://localhost:7861 | Application registration & config |

### Running outside Docker

You can also run the UIs standalone (the FastAPI server must be running first):

```bash
python app/gradio_app.py           # http://localhost:7860
python app/gradio_admin.py         # http://localhost:7861
```

Set the `API_BASE` environment variable if the API is not at `http://localhost:8000`.

### PII Shield UI (Anonymize & De-anonymize)

Opens at **http://localhost:7860** with:

- **Anonymize tab** — Enter text (and optionally an Application ID) → see detected PIIs and anonymized output using app-specific or global config.
- **De-anonymize tab** — Paste anonymized text (and optionally an Application ID) → see the PII ↔ placeholder mapping and restored text.

The session ID is passed automatically between tabs via Gradio state.

### Admin UI (Application Management)

Opens at **http://localhost:7861** with:

- **Registered Apps tab** — View all registered applications and their configurations.
- **Admin tab** — Register applications, look up app details, update per-entity anonymization strategies, and delete apps.

## API Usage

### `POST /anonymize_unique`

Detects PII and assigns a unique numbered identifier to each distinct PII value (e.g. `{{PERSON_1}}`, `{{PERSON_2}}`). Returns a mapping from placeholders back to the original values.

**Request body:**

```json
{
  "text": "Call John Smith at john.smith@example.com or Jane Doe at jane.doe@example.com.",
  "language": "en"
}
```

**Response:**

```json
{
  "text": "Call John Smith at john.smith@example.com or Jane Doe at jane.doe@example.com.",
  "anonymized_text": "Call {{PERSON_2}} at {{EMAIL_ADDRESS_2}} or {{PERSON_1}} at {{EMAIL_ADDRESS_1}}.",
  "entity_mapping": {
    "{{PERSON_1}}": "Jane Doe",
    "{{PERSON_2}}": "John Smith",
    "{{EMAIL_ADDRESS_1}}": "jane.doe@example.com",
    "{{EMAIL_ADDRESS_2}}": "john.smith@example.com"
  }
}
```

### `POST /deanonymize`

Restores original PII values in text that was anonymized via `/anonymize_unique`. The text you pass in can differ from the original anonymized output — for example, it may have been rewritten by an LLM — as long as it still contains the `{{ENTITY_TYPE_N}}` placeholders.

**Request body:**

```json
{
  "id": "a1b2c3d4-...",
  "text": "Call {{PERSON_2}} at {{EMAIL_ADDRESS_2}} or {{PERSON_1}} at {{EMAIL_ADDRESS_1}}."
}
```

**Response:**

```json
{
  "text": "Call John Smith at john.smith@example.com or Jane Doe at jane.doe@example.com.",
  "id": "a1b2c3d4-..."
}
```

### `POST /apps`

Register a new application. Returns a unique `app_id` and the default config (all entities default to `"replace"`).

**Request body:**

```json
{
  "app_name": "MyApp"
}
```

**Response (201):**

```json
{
  "app_id": "b2f7c8a1-...",
  "app_name": "MyApp",
  "config": {
    "IN_DRIVING_LICENSE": "replace"
  }
}
```

### `GET /apps/{app_id}`

Get details and config for a registered application.

### `PUT /apps/{app_id}/config`

Update the anonymization strategy for a specific entity type within an app.

**Request body:**

```json
{
  "entity_type": "IN_DRIVING_LICENSE",
  "strategy": "hash"
}
```

> **Note:** Entities configured with `"hash"` cannot be restored via `/deanonymize` — the hash is one-way. They are excluded from the `entity_mapping` returned by `/anonymize_unique`.

### `DELETE /apps/{app_id}`

Deregister an application (returns 204).

### `X-App-Id` Header

Pass the `X-App-Id` header on `/anonymize_unique` or `/deanonymize` to use an app's config:

```bash
curl -X POST http://localhost:8000/anonymize_unique \
  -H "Content-Type: application/json" \
  -H "X-App-Id: b2f7c8a1-..." \
  -d '{"text": "DL: MH 14 2019 0012345"}'
```

When the header is omitted, global config applies. If the `app_id` is not found, a 404 is returned.

### Python Example

```bash
python examples/anonymize_request.py
```

See [`examples/anonymize_request.py`](examples/anonymize_request.py) for the full source.

## Azure Deployment

PII Shield can be deployed to Azure using **Terraform** (infrastructure) and **shell scripts** (application lifecycle). The Azure deployment includes:

- **Azure Container Apps** — 3 apps (API, Gradio UI, Gradio Admin) with auto-scaling and HTTPS
- **Azure Cache for Redis** — TLS-only session and app registry store
- **Azure Container Registry** — Remote Docker image builds (no local Docker required)
- **Application Insights + Log Analytics** — Full observability via Azure Monitor OTel exporter
- **Azure Managed Grafana** — Pre-built KQL dashboards for metrics, per-app analytics, and error monitoring

```bash
cd infra/scripts
./00-bootstrap-state.sh   # One-time: create Terraform state backend
cd ../terraform
terraform init && terraform apply
cd ../scripts
./01-build-push.sh        # Build image in ACR
./02-deploy-apps.sh       # Deploy all container apps
./03-verify.sh            # Health checks
./04-deploy-dashboards.sh # Upload Grafana dashboards
```

See the full [Azure Deployment Guide](infra/README.md) for prerequisites, configuration, costs, and teardown instructions.

## Running Tests

```bash
pip install pytest httpx
pytest tests/ -v
```

## Design Choices

| Decision | Rationale |
|---|---|
| **In-memory dict store (UUID-keyed)** | Zero external dependencies, sub-microsecond lookups. Each `/anonymize_unique` call gets a UUID session ID stored in a `dict`. Trade-off: data is lost on restart — swap for Redis or a database in production. |
| **`/anonymize_unique` returns a session `id`** | Decouples anonymization from de-anonymization. The client can pass the anonymized text through an LLM pipeline and call `/deanonymize` later with the same `id`. |
| **`/deanonymize` accepts `id` + arbitrary `text`** | The text may have been rewritten by an LLM — we only replace the placeholders we find, leaving everything else intact. This supports the "sandwich" pattern: *anonymize → LLM → deanonymize*. |
| **Thread-safe store (`threading.Lock`)** | Uvicorn runs async handlers on an event loop but may use thread pools. The lock ensures safe concurrent access with negligible overhead for an in-memory dict. |
| **Longest-placeholder-first replacement** | Avoids `{{PERSON_1}}` matching inside `{{PERSON_10}}`. Sorting by length descending guarantees correct substitution. |

## License

See [LICENSE](LICENSE) for details.
