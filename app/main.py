import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import StatusCode

import app.telemetry  # noqa: F401  — initializes OTel providers on import
from pii_shield.engine import PiiShieldEngine
from pii_shield.models import EntityConfig
from pii_shield.nlp_engine import get_nlp_engine_name
from pii_shield.pipeline import (
    is_valid_datetime as _is_valid_datetime,
    merge_address_entities as _merge_address_entities,
    reclassify_person_as_location as _reclassify_person_as_location,
    remove_overlapping as _remove_overlapping,
)

logger = logging.getLogger("pii-shield")
tracer = trace.get_tracer("pii-shield")
meter = metrics.get_meter("pii-shield")

pii_entities_detected = meter.create_counter(
    "pii.entities.detected",
    description="Number of PII entities detected across all requests",
    unit="entities",
)
anonymize_request_counter = meter.create_counter(
    "pii.anonymize.requests",
    description="Total anonymization requests",
)
deanonymize_request_counter = meter.create_counter(
    "pii.deanonymize.requests",
    description="Total de-anonymization requests",
)
error_counter = meter.create_counter(
    "pii.errors",
    description="Total errors by endpoint and type",
    unit="errors",
)


def _record_error(
    endpoint: str,
    status_code: int,
    error_type: str,
    detail: str,
    app_id: str = "",
) -> None:
    """Log and count an error."""
    error_counter.add(1, {
        "endpoint": endpoint,
        "status_code": str(status_code),
        "error_type": error_type,
        "app_id": app_id,
    })
    if status_code >= 500:
        logger.error("Error %d on %s (%s): %s", status_code, endpoint, error_type, detail)
    else:
        logger.warning("Client error %d on %s (%s): %s", status_code, endpoint, error_type, detail)


def _observe_registered_apps(_options: metrics.CallbackOptions) -> list[metrics.Observation]:
    """Callback that reads the current count of registered apps from Redis."""
    try:
        return [metrics.Observation(count_apps())]
    except Exception:
        return []


meter.create_observable_gauge(
    "pii.registered.apps",
    callbacks=[_observe_registered_apps],
    description="Number of currently registered applications",
)

from app.models import (
    AnonymizeRequest,
    AnonymizeUniqueResponse,
    AppConfigUpdateRequest,
    AppDetailResponse,
    DeanonymizeRequest,
    DeanonymizeResponse,
    RegisterAppRequest,
    RegisterAppResponse,
)
from pii_shield.operator_config import operator_config_store, ENCRYPTION_BACKEND
from app.redis_store import (
    count_apps,
    delete_app,
    get_app,
    get_app_allow_list,
    get_app_entity_keyword_allow_list,
    get_app_entity_type_allow_list,
    get_app_strategy,
    get_async_redis_client,
    get_redis_client,
    list_apps,
    register_app,
    set_app_allow_list,
    set_app_entity_keyword_allow_list,
    set_app_entity_type_allow_list,
    update_app_config,
)
import app.redis_store as redis_store_mod
from app.state_store import SESSION_TTL_SECONDS, AnonymizationRecord, store
from app.audit_store import record_event, get_audit_log

# Thread pool for asyncio.to_thread().  ONNX Runtime releases the GIL during
# native C++ inference, so multiple threads CAN run truly in parallel — unlike
# pure-Python code.  A pool larger than WEB_CONCURRENCY lets the process handle
# concurrent NLP requests without queuing, which is the #1 driver of p95 blowup.
_NLP_THREAD_POOL_SIZE = int(os.getenv("NLP_THREAD_POOL_SIZE", "6"))


@asynccontextmanager
async def _lifespan(app):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=_NLP_THREAD_POOL_SIZE))
    _start_cache_invalidation_listener()
    # Warm up the NLP model so the first real request doesn't pay JIT/allocation cost.
    engine._run_pipeline(
        text="John Smith at john@example.com",
        language="en",
        allow_list=None,
        entity_type_allow_list=None,
    )
    logger.info("NLP model warm-up complete")
    yield


app = FastAPI(
    title="PII Shield",
    description="An anonymization layer that detects and masks PII using Microsoft Presidio.",
    version="0.1.0",
    lifespan=_lifespan,
)
FastAPIInstrumentor.instrument_app(app)


# ---------------------------------------------------------------------------
# In-memory app-data cache (avoids repeated Redis round-trips on hot path)
# ---------------------------------------------------------------------------
_app_cache: dict[str, tuple[float, dict]] = {}
_APP_CACHE_TTL = 5.0  # seconds
_APP_CACHE_MAX_SIZE = 1000
_CACHE_INVALIDATION_CHANNEL = "pii-shield:cache-invalidate"


def _start_cache_invalidation_listener() -> None:
    """Start a background thread that subscribes to Redis pub/sub for cache
    invalidation messages from other Gunicorn workers.

    Each worker has its own in-memory ``_app_cache``.  When any worker
    updates app config, it publishes the ``app_id`` to the channel so
    every other worker can evict the stale entry immediately — instead
    of waiting up to ``_APP_CACHE_TTL`` seconds for natural expiry.
    """
    import threading

    def _listener() -> None:
        while True:
            try:
                r = get_redis_client()
                pubsub = r.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(_CACHE_INVALIDATION_CHANNEL)
                logger.info("Cache invalidation listener started on channel %s",
                            _CACHE_INVALIDATION_CHANNEL)
                for message in pubsub.listen():
                    if message["type"] == "message":
                        app_id = message["data"]
                        if isinstance(app_id, bytes):
                            app_id = app_id.decode()
                        _app_cache.pop(app_id, None)
            except Exception:
                logger.warning("Cache invalidation listener error, reconnecting in 2s",
                               exc_info=True)
                time.sleep(2)

    t = threading.Thread(target=_listener, daemon=True, name="cache-invalidate")
    t.start()


def _publish_cache_invalidation(app_id: str) -> None:
    """Publish a cache invalidation message so all workers evict this app."""
    try:
        r = get_redis_client()
        r.publish(_CACHE_INVALIDATION_CHANNEL, app_id)
    except Exception:
        logger.warning("Failed to publish cache invalidation for %s", app_id,
                       exc_info=True)


async def _fetch_app_data(app_id: str) -> dict | None:
    """Fetch all app data in a single Redis pipeline call, with in-memory caching.

    Returns a dict with keys: app_id, app_name, config, allow_list,
    entity_type_allow_list — or None if the app doesn't exist.
    Replaces 5 separate Redis GETs with 1 pipelined round-trip (cached 5 s).
    """
    now = time.monotonic()
    cached = _app_cache.get(app_id)
    if cached and (now - cached[0]) < _APP_CACHE_TTL:
        return cached[1]

    r = get_async_redis_client()
    pipe = r.pipeline(transaction=False)
    pipe.get(f"app:{app_id}:name")
    pipe.get(f"app:{app_id}:config")
    pipe.get(f"app:{app_id}:allow_list")
    pipe.get(f"app:{app_id}:entity_type_allow_list")
    pipe.get(f"app:{app_id}:entity_keyword_allow_list")
    name, config_raw, allow_raw, entity_type_allow_raw, entity_kw_allow_raw = await pipe.execute()

    if name is None:
        return None

    result = {
        "app_id": app_id,
        "app_name": name,
        "config": json.loads(config_raw or "{}"),
        "allow_list": json.loads(allow_raw or "[]"),
        "entity_type_allow_list": json.loads(entity_type_allow_raw or "[]"),
        "entity_keyword_allow_list": json.loads(entity_kw_allow_raw or "{}"),
    }

    # Evict stale entries if cache grows too large
    if len(_app_cache) > _APP_CACHE_MAX_SIZE:
        stale = [k for k, (t, _) in _app_cache.items() if (now - t) > _APP_CACHE_TTL]
        for k in stale:
            del _app_cache[k]

    _app_cache[app_id] = (now, result)
    return result


def invalidate_app_cache(app_id: str) -> None:
    """Remove an app from the in-memory cache and notify other workers."""
    _app_cache.pop(app_id, None)
    _publish_cache_invalidation(app_id)


engine = PiiShieldEngine()
# Backward-compat alias: tests use `from app.main import analyzer`
analyzer = engine._analyzer
logger.info("PiiShieldEngine ready (NLP engine: %s)", get_nlp_engine_name())


@app.get("/supported-entities")
async def supported_entities() -> list[str]:
    """Return the list of PII entity types the analyzer can detect."""
    return engine.supported_entities


def _validate_app_id(app_id: str | None) -> dict | None:
    """If an app_id is provided, look it up or raise 404. Returns app dict or None."""
    if app_id is None:
        return None
    result = get_app(app_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    return result


@app.post("/anonymize_unique", response_model=AnonymizeUniqueResponse)
async def anonymize_unique_text(
    request: AnonymizeRequest,
    x_app_id: str | None = Header(default=None),
) -> AnonymizeUniqueResponse:
    """Detect and anonymize PII, assigning a unique identifier to each distinct entity."""
    try:
        # Single pipelined + cached fetch replaces 5 separate Redis GETs
        app_data = (await _fetch_app_data(x_app_id)) if x_app_id else None
        if x_app_id and app_data is None:
            raise HTTPException(status_code=404, detail=f"Application '{x_app_id}' not found.")
        app_name = app_data["app_name"] if app_data else ""

        # Merge per-request allow-list with per-app allow-list
        allow_list = list(request.allow_list)
        if app_data:
            allow_list.extend(app_data["allow_list"])

        # Merge per-request entity-type allow-list with per-app entity-type allow-list
        entity_type_allow_list: set[str] = set(request.entity_type_allow_list)
        if app_data:
            entity_type_allow_list.update(app_data["entity_type_allow_list"])

        # Merge per-app entity-keyword allow-list
        entity_keyword_allow_list: dict[str, list[str]] = {}
        if app_data:
            entity_keyword_allow_list = app_data.get("entity_keyword_allow_list", {})

        # Build per-entity strategy config from app config + global defaults
        _app_config = app_data["config"] if app_data else {}
        entity_config = EntityConfig(strategies=dict(_app_config))

        with tracer.start_as_current_span("pii_shield.anonymize") as span:
            span.set_attribute("pii.language", request.language)
            # Offload CPU-bound NLP inference to a thread so the event loop stays free
            anon_result = await asyncio.to_thread(
                engine.anonymize,
                text=request.text,
                language=request.language,
                config=entity_config,
                allow_list=allow_list or None,
                entity_type_allow_list=entity_type_allow_list or None,
                entity_keyword_allow_list=entity_keyword_allow_list or None,
            )
            span.set_attribute("pii.entities_found", len(anon_result.entities))
            span.set_attribute("pii.placeholders_created", len(anon_result.entity_mapping))

        record = AnonymizationRecord(
            original_text=request.text,
            anonymized_text=anon_result.anonymized_text,
            entity_mapping=anon_result.entity_mapping,
            app_id=x_app_id or "",
            hash_mapping=anon_result.hash_mapping,
            encrypt_mapping=anon_result.encrypt_mapping,
        )
        record_id = await store.save(record)

        for entity in anon_result.entities:
            pii_entities_detected.add(1, {"endpoint": "/anonymize_unique", "entity_type": entity.entity_type, "app_id": x_app_id or "", "app_name": app_name})
        anonymize_request_counter.add(1, {"endpoint": "/anonymize_unique", "app_id": x_app_id or "", "app_name": app_name})
        logger.info(
            "Unique anonymization complete: %d entities, id=%s",
            len(anon_result.entities),
            record_id,
        )

        return AnonymizeUniqueResponse(
            id=record_id,
            text=request.text,
            anonymized_text=anon_result.anonymized_text,
            entity_mapping=anon_result.entity_mapping,
            hash_mapping=anon_result.hash_mapping,
            encrypt_mapping=anon_result.encrypt_mapping,
        )
    except HTTPException as exc:
        error_type = "not_found" if exc.status_code == 404 else "validation"
        _record_error("/anonymize_unique", exc.status_code, error_type, exc.detail, x_app_id or "")
        raise
    except Exception as e:
        current_span = trace.get_current_span()
        current_span.record_exception(e)
        current_span.set_status(StatusCode.ERROR, str(e))
        _record_error("/anonymize_unique", 500, "internal", str(e), x_app_id or "")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/deanonymize", response_model=DeanonymizeResponse)
async def deanonymize_text(
    request: DeanonymizeRequest,
    x_app_id: str | None = Header(default=None),
) -> DeanonymizeResponse:
    """Restore original PII values in text that was anonymized via /anonymize_unique.

    The provided text may differ from the original anonymized output (e.g. it was
    processed by an LLM) — only the placeholders are replaced.
    """
    try:
        app_data = (await _fetch_app_data(x_app_id)) if x_app_id else None
        if x_app_id and app_data is None:
            raise HTTPException(status_code=404, detail=f"Application '{x_app_id}' not found.")
        app_name = app_data["app_name"] if app_data else ""

        record = await store.get(request.id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"No anonymization session found for id '{request.id}'. Sessions expire after {SESSION_TTL_SECONDS} seconds.",
            )

        restored_text = request.text
        with tracer.start_as_current_span("pii_shield.deanonymize") as span:
            span.set_attribute("pii.record_id", request.id)
            span.set_attribute("pii.placeholders_count", len(record.entity_mapping))
            restored_text = engine.deanonymize(
                text=restored_text,
                entity_mapping=record.entity_mapping,
                hash_mapping=record.hash_mapping if request.include_hashed else None,
                encrypt_mapping=record.encrypt_mapping if request.include_encrypted else None,
            )

        deanonymize_request_counter.add(1, {"app_id": x_app_id or "", "app_name": app_name})
        logger.info("De-anonymization complete: id=%s", request.id)

        return DeanonymizeResponse(text=restored_text, id=request.id)
    except HTTPException as exc:
        error_type = "not_found" if exc.status_code == 404 else "validation"
        _record_error("/deanonymize", exc.status_code, error_type, exc.detail, x_app_id or "")
        raise
    except Exception as e:
        current_span = trace.get_current_span()
        current_span.record_exception(e)
        current_span.set_status(StatusCode.ERROR, str(e))
        _record_error("/deanonymize", 500, "internal", str(e), x_app_id or "")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Application registration
# ---------------------------------------------------------------------------


@app.post("/apps", response_model=RegisterAppResponse, status_code=201)
async def register_application(request: RegisterAppRequest) -> RegisterAppResponse:
    """Register a new application and return its ID with default config."""
    result = register_app(request.app_name)
    record_event("APP_REGISTERED", result["app_id"], request.app_name, {"config": result["config"]})
    return RegisterAppResponse(**result)


@app.get("/apps", response_model=list[AppDetailResponse])
async def list_applications() -> list[AppDetailResponse]:
    """List all registered applications."""
    return [AppDetailResponse(**a) for a in list_apps()]


@app.get("/apps/{app_id}", response_model=AppDetailResponse)
async def get_application(app_id: str) -> AppDetailResponse:
    """Get details and config for a registered application."""
    result = get_app(app_id)
    if result is None:
        _record_error("/apps/{app_id}", 404, "not_found", f"Application '{app_id}' not found.")
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    return AppDetailResponse(**result)


@app.put("/apps/{app_id}/config", response_model=AppDetailResponse)
async def update_application_config(
    app_id: str, request: AppConfigUpdateRequest
) -> AppDetailResponse:
    """Update the anonymization strategy for a specific entity type in an app."""
    if request.strategy not in ("replace", "hash", "encrypt", "fake"):
        detail = f"Invalid strategy '{request.strategy}'. Must be 'replace', 'hash', 'encrypt', or 'fake'."
        _record_error("/apps/{app_id}/config", 400, "validation", detail)
        raise HTTPException(status_code=400, detail=detail)
    result = update_app_config(app_id, request.entity_type, request.strategy)
    if result is None:
        _record_error("/apps/{app_id}/config", 404, "not_found", f"Application '{app_id}' not found.")
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    invalidate_app_cache(app_id)
    record_event("CONFIG_UPDATED", app_id, result["app_name"], {"entity_type": request.entity_type, "strategy": request.strategy})
    return AppDetailResponse(**result)


@app.delete("/apps/{app_id}", status_code=204)
async def delete_application(app_id: str) -> None:
    """Deregister an application."""
    app_info = get_app(app_id)
    if not delete_app(app_id):
        _record_error("/apps/{app_id}", 404, "not_found", f"Application '{app_id}' not found.")
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    invalidate_app_cache(app_id)
    record_event("APP_DELETED", app_id, app_info["app_name"] if app_info else "")


# ---------------------------------------------------------------------------
# Per-app allow-list endpoints
# ---------------------------------------------------------------------------


@app.get("/apps/{app_id}/allow-list")
async def get_allow_list(app_id: str) -> dict:
    """Return the allow-list for an application."""
    app_info = get_app(app_id)
    if app_info is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    return {"app_id": app_id, "allow_list": get_app_allow_list(app_id)}


@app.put("/apps/{app_id}/allow-list")
async def update_allow_list(app_id: str, body: dict) -> dict:
    """Set the allow-list for an application.

    Body: ``{"allow_list": ["Contoso Bank", "Woodgrove Bank"]}``
    """
    allow_list = body.get("allow_list")
    if not isinstance(allow_list, list):
        raise HTTPException(status_code=400, detail="Body must contain 'allow_list' as a list of strings.")
    app_info = get_app(app_id)
    result = set_app_allow_list(app_id, allow_list)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    invalidate_app_cache(app_id)
    record_event("ALLOW_LIST_UPDATED", app_id, app_info["app_name"], details={"allow_list": result})
    return {"app_id": app_id, "allow_list": result}


# ---------------------------------------------------------------------------
# Per-app entity-type allow-list endpoints
# ---------------------------------------------------------------------------


@app.get("/apps/{app_id}/entity-type-allow-list")
async def get_entity_type_allow_list(app_id: str) -> dict:
    """Return the entity-type allow-list for an application."""
    app_info = get_app(app_id)
    if app_info is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    return {"app_id": app_id, "entity_type_allow_list": get_app_entity_type_allow_list(app_id)}


@app.put("/apps/{app_id}/entity-type-allow-list")
async def update_entity_type_allow_list(app_id: str, body: dict) -> dict:
    """Set the entity-type allow-list for an application.

    Body: ``{"entity_type_allow_list": ["EMAIL_ADDRESS", "PHONE_NUMBER"]}``
    """
    entity_type_allow_list = body.get("entity_type_allow_list")
    if not isinstance(entity_type_allow_list, list):
        raise HTTPException(
            status_code=400,
            detail="Body must contain 'entity_type_allow_list' as a list of strings.",
        )
    result = set_app_entity_type_allow_list(app_id, entity_type_allow_list)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    invalidate_app_cache(app_id)
    app_info = get_app(app_id)
    record_event("ENTITY_TYPE_ALLOW_LIST_UPDATED", app_id, app_info["app_name"] if app_info else "", details={"entity_type_allow_list": result})
    return {"app_id": app_id, "entity_type_allow_list": result}


# ---------------------------------------------------------------------------
# Per-app entity-keyword allow-list endpoints
# ---------------------------------------------------------------------------


@app.get("/apps/{app_id}/entity-keyword-allow-list")
async def get_entity_keyword_allow_list_endpoint(app_id: str) -> dict:
    """Return the entity-keyword allow-list for an application.

    Format: ``{entity_type: [keyword1, keyword2, ...]}``
    """
    app_info = get_app(app_id)
    if app_info is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    return {"app_id": app_id, "entity_keyword_allow_list": get_app_entity_keyword_allow_list(app_id)}


@app.put("/apps/{app_id}/entity-keyword-allow-list")
async def update_entity_keyword_allow_list_endpoint(app_id: str, body: dict) -> dict:
    """Set the entity-keyword allow-list for an application.

    Body: ``{"entity_keyword_allow_list": {"ORGANIZATION": ["Contoso Bank"], "LOCATION": ["India"]}}``
    """
    ekw = body.get("entity_keyword_allow_list")
    if not isinstance(ekw, dict):
        raise HTTPException(
            status_code=400,
            detail="Body must contain 'entity_keyword_allow_list' as a dict of {entity_type: [keywords]}.",
        )
    result = set_app_entity_keyword_allow_list(app_id, ekw)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found.")
    invalidate_app_cache(app_id)
    app_info = get_app(app_id)
    record_event("ENTITY_KEYWORD_ALLOW_LIST_UPDATED", app_id, app_info["app_name"] if app_info else "", details={"entity_keyword_allow_list": result})
    return {"app_id": app_id, "entity_keyword_allow_list": result}


# ---------------------------------------------------------------------------
# Audit log endpoint
# ---------------------------------------------------------------------------


@app.get("/audit-log")
async def audit_log(
    app_id: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return audit log entries, newest first. Optional filters: app_id, action, limit."""
    return get_audit_log(app_id=app_id, action=action, limit=limit)
