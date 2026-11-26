# Profiling the end-to-end call: per-step timings for PII Shield

Research note, 2026-11-25 (branch `vikasgautam18-pii-recognition-performance`). No repo file other than this one was changed.
Question: *how can we profile the end-to-end call and show how much time each high-level step took (recognizing PII, dedup,
handling it, ...)?* Motivation: in UAT, identification sometimes took ~10 s (~50 entity types, 70% regex / 30% NER, ONNX engine,
Azure Container Apps, 2 vCPU replicas).
Citation tags: `[S#]` external doc, `[P#]` installed package source (read in the running app image), `[E#]` experiment run locally
(section 4.0), `file:line` repo facts. Statements marked *(inference)* are my reasoning, not a source.
Scope note: the cost of the encrypt / PQC strategy is deliberately not covered here yet (the owner will look at it later).

## 1. TL;DR

1. **Recommendation:** the library records per-step durations as plain data (`AnonymizeStats.steps`, ms, stable names, no OpenTelemetry import); `app/` fans them out to retroactive child spans under the existing `pii_shield.anonymize` span, one bounded histogram `pii.step.duration{step,size}`, the log line, and a `Server-Timing` (+ `traceresponse`) response header that the Playground renders. Per-recognizer / per-function timings and profilers stay on-demand.
2. **Cost:** about 0.1 ms per request for ~15 timers + up to 9 step spans + ~12 histogram records (measured 0.24 µs / 7 µs / 1.3 µs each on a dev VM; E3 priced 11 spans = 76 µs as an upper bound) against 48 ms (short text, live trace) to 785 ms (2.7 KB, two NER passes) [E3, E5].
3. **What the data already says:** for one 2.7 KB text (95 entities, 2 vCPU) the NER model forward is 91% of the request (713 of 785 ms); ~34 regex recognizers 13 ms, context enhancer 1.6 ms, dedup 0.7 ms, post-processing 1.0 ms, handling 0.06 ms [E5]. Always-on coarse steps plus the NER window count and queue wait will answer most "why slow" questions; per-recognizer detail is a rare need.
4. **Fix first (Phase 0):** `pii.detect.duration` uses the SDK default buckets that stop at 10,000 ms, so any quantile that lands above 10 s reads as exactly 10000 (verified: 12-25 s values gave p50 = p95 = 10000); and all gunicorn workers export with the same `service.instance.id`, so cumulative series collide in Prometheus (verified: 50 instead of 200) [E2, E8].
5. **Azure differs from local:** the Azure exporter sends histograms as sum/count/min/max only (no buckets) and no exemplars, so per-step percentiles there must come from spans (`dependencies`, `type == "InProc"`) with KQL; I could not run KQL (no Azure access) [P3].
6. **Local already has more than we use:** the otel-lgtm image turns *any* span, INTERNAL included, into `traces_spanmetrics_*` and supports TraceQL metrics (`quantile_over_time(duration, ...) by (name)`); both verified, both coarse (power-of-two buckets) [E1].
7. **"Why was THIS request slow":** pyinstrument started *inside* the pool-thread callable (+5% overhead; attributes 87% to `InferenceSession.run`); a middleware-level profiler never sees the pool thread (verified). py-spy needs `SYS_PTRACE`; Pyroscope's default `gil_only=True` would hide ONNX time (docs + inference) [E7, S2, S6].
8. **Owner decisions** are collected in section 5.7 (D1-D9): Server-Timing exposure, Azure span volume/sampling, who may trigger on-demand profiling, instance id and bucket changes.

## 2. Captured today vs not

| Signal | Captured today? | Evidence | Gap |
|---|---|---|---|
| Whole-request duration | **Yes** | `FastAPIInstrumentor.instrument_app(app)` `app/main.py:223`; metric `http.server.duration` -> Prometheus `http_server_duration_milliseconds_*`; p50/p95/p99 panel in `observability/grafana/dashboards/pii-shield.json`, same via `requests` table in `dashboards-azure/pii-shield.json` | none |
| Server span + `http receive`/`http send` spans | **Yes** | Live trace in Tempo: 5 spans per request (SERVER, 1x receive, `pii_shield.anonymize`, 2x send) [E0] | 3 of the 5 are ~0 ms noise |
| `pii_shield.anonymize` span (pool wait + detection + handling) | **Yes** | `app/main.py:408-429`: attrs `pii.language/chars/entities_found/placeholders_created/queue_ms/detect_ms/nlp_ms/recovery_ms/recovery_pass` | no per-step children |
| Queue wait for the detection thread | **Yes** | `_run_in_nlp_pool_timed` `app/main.py:162-178`; span attr `:425`; histogram `pii.detect.queue_wait` `:54,452` | histogram buckets stop at 10 s (see 4.3) |
| `detect_ms` (whole `_run_pipeline`) | **Yes** | `pii_shield/engine.py:673-679`; `app/main.py:426,451`; histogram `pii.detect.duration` `:49` | same bucket problem |
| `nlp_ms` (1st `process_text`: spaCy + NER model together) | **Yes** | `engine.py:343-346`, `AnonymizeStats` `models.py:36-53` | spaCy vs NER model not split |
| `recovery_ms` / `recovery_pass` (2nd NER pass incl. its analyze) | **Yes** | `engine.py:385-405` | the gate (`needs_name_recovery`, `normalize_case`, `has_recoverable_case_change`, `:360-384`) is not inside it |
| `anonymize_ms` (handling: `_assign_replacements` + `_build_text`) | **In the library only** | `engine.py:683-704`; `AnonymizeStats.anonymize_ms`, `Event.anonymize_ms` | **dropped at the API**: `app/main.py` never reads `stats.anonymize_ms` / `total_ms`; no span attr, histogram or log field |
| `on_event` push hook | Exists, **unused** | `engine.py:737` `emit(...)`; `app/main.py` never passes `on_event` | `Event` has no per-step data |
| Presidio `analyze()` total (regex/validator recognizers + context enhancer + dedup + threshold) | **No** | only inferable as `detect_ms - nlp_ms - recovery_ms - post-processing` | add `analyze` (+ `analyze.context_enhance`, `analyze.dedup`) |
| Per recognizer (34 here) | **Offline only** | `scripts/profile_detection.py` monkeypatches `recognizer.analyze`, context enhancer, `remove_duplicates`, NER windows | no runtime/on-demand switch |
| Post-processing (13 functions, `engine.py:415-497`) | **No** | not in `scripts/profile_detection.py` either | add `post_processing` block timer |
| spaCy vs NER model vs HF pre/post | **No** | `profile_detection.py` only counts/times `model.forward` windows | see 4.6 |
| `auth` (JWT validation, JWKS refresh) | **No** | `app/auth.py:434-456`; only the rejection counter `pii.auth.rejections` | add timer (matters when `AUTH_ENABLED=true`) |
| `app_config` (`_fetch_app_data`, Redis pipeline / 5 s cache) | **No** | `app/main.py:279-317,384` | add timer |
| `store_save` (Redis `hset`+`expire`) | **No** | `app/main.py:439`, `app/state_store.py:44-61` | add timer |
| Serialization / response building | **No** (inside server span) | `app/main.py:466-473`; `AnonymizeUniqueResponse` `app/models.py:19` | derive: server span minus the steps |
| One log line per request | **Partly** | `app/main.py:453-465`: `queue_ms, detect_ms, nlp_ms, recovery_pass, recovery_ms` | no handling/analyze/post/store |
| Playground | **No timing at all** | `app/streamlit_app.py:90-97` reads `resp.json()` only | show breakdown |
| Dashboards | **No panel for `pii.detect.*`** | grep of `observability/`: no `pii_detect` / `detect_duration`; only `DEPLOYMENT.md:361-362` mentions the histograms | add panels |
| Runbook | Yes | `DEPLOYMENT.md` "Requests take several seconds" (queue vs compute) | extend with the new steps |

## 3. The call path and proposed step names

Steps are **exclusive** (non-overlapping, in execution order) so their sum plus `other` equals the server time. "Detail" rows are inclusive
sub-measures (reported with a dotted name, never added to the sum). Shares come from [E5] (one 2.7 KB text, 95 entities, recovery pass ran,
default `replace` strategy, `--cpus=2`, median of 7).

| # | Proposed step | Where in code | Measured today? | Proposed | Local sample [E5] |
|---|---|---|---|---|---|
| 1 | `auth` | `authenticate` / `require_user` `app/auth.py:434,467` (global dependency `app/main.py:221`) | No | always-on (stash on `request.state`) | ~0 (AUTH off) |
| 2 | `app_config` | `_fetch_app_data` `app/main.py:279` | No | always-on | µs (cache hit) |
| 3 | `queue_wait` | `_run_in_nlp_pool_timed` `app/main.py:162` | **Yes** (`pii.queue_ms`) | keep, add to steps | 0 (idle) |
| 4 | `nlp_pass1` | `process_text` `engine.py:343-346` | **Yes** (`nlp_ms`) | keep; details `nlp_pass1.ner_model`, `pii.ner_windows` | 388 ms (49%) |
| 5 | `analyze` | `self._analyzer.analyze` `engine.py:347` -> Presidio `analyzer_engine.py:231-258` | No | always-on; details `analyze.context_enhance`, `analyze.dedup` | 15.8 ms for both passes: recognizers 13.1, context 1.6, dedup 0.7 |
| 6 | `recovery_gate` | `engine.py:360-384` | No | fold into `other` | 0.9 ms |
| 7 | `recovery_pass2` | `engine.py:385-410` | **Yes** (`recovery_ms`) | keep (when it ran) | 377 ms (48%) |
| 8 | `post_processing` | 13 functions `engine.py:415-497` | No | always-on as ONE block timer; per-function on-demand | 1.0 ms |
| 9 | `handling` | `_assign_replacements`, `_build_text` `engine.py:557-625`, timed `:683-704` | In library only | always-on, forward to API (replace / hash / fake) | 0.06 ms |
| 10 | `store_save` | `store.save` `app/main.py:439` | No | always-on | Redis RTT |
| 11 | `other` (derived) | server span - sum(steps): entity list/Counter, metric adds, response model + JSON, ASGI | No | derived, never timed in code | - |

Note: the encrypt / PQC strategy's cost is deliberately not covered here yet; `handling` is one timed step regardless of strategy.

## 4. Options

### 4.0 Verified locally (experiments)

The scratch scripts were kept outside the repo and are not part of it. Common form (stack left running, service names
`pii-shield-research-scratch*`, which stay in the local Tempo/Prometheus volume):

```
S=<folder with the scratch scripts>
docker run --rm --network <compose project>_default -v $S:/s --entrypoint python <compose project>-app /s/<script>.py
```

Environment: app image Python 3.12.16 on an **aarch64 Docker Desktop VM** (E5/E7 with `--cpus=2`), grafana/otel-lgtm v0.40.0
(Grafana 13.3.0, Tempo 3.1.1, Prometheus 3.16.0, Pyroscope 2.4.3) [P9]. Absolute numbers are not Azure x86 numbers; use ratios.

| ID | What | Script / extra flags | Result |
|---|---|---|---|
| E0 | Real trace shape today | Tempo `/api/search` then `/api/traces/<id>` for `name="pii_shield.anonymize"` | 5 spans/request; 106-char text: server 48 ms, `pii_shield.anonymize` 43 ms (`detect_ms` 41.9, `nlp_ms` 27.7) |
| E1 | Retroactive child spans (explicit `start_time`/`end_time`) -> Tempo; TraceQL; span metrics | `retro_spans.py 40` | Correct tree (SERVER -> `pii_shield.anonymize` -> 8 INTERNAL children). TraceQL search, per-trace aggregate, `select()` and TraceQL metrics work (6.2). `traces_spanmetrics_{calls_total,latency_*}` exist for INTERNAL spans; buckets 0.002 ... 16.384 s |
| E2 | Histogram buckets | `hist_buckets.py` | Default boundaries end at 10000; 30 values of 12-25 s all in the overflow bucket; Prometheus p50 = p95 = 10000 (true ~18000 / ~24000); mean still right (17861). With `explicit_bucket_boundaries_advisory` to 120000: p50 17500, p95 28500. The live app's `pii_detect_duration_milliseconds_bucket` has `le` 0 ... 10000, +Inf (default) |
| E3 | Cost of one timing record | `span_cost.py`, `-e REAL=http://otel-lgtm:4317` | `perf_counter` pair 83 ns; class timer into dict 244 ns; `@contextmanager` timer 509 ns; real-time span 8.2 µs (10.2 with context=, 9.1 with 1 attribute); retroactive span 7.0 µs; non-recording (`ALWAYS_OFF`) 2.9 µs; 11 retroactive spans 75.7 µs; 45 spans 311 µs; `Histogram.record` (4 attrs) 1.26 µs; 11 records with fresh dicts 14.4 µs; with the real OTLP exporter 8.6 µs/span; a 5,000-span burst printed `Queue full, dropping Span` |
| E4 | Server-Timing + `traceresponse` round trip | `server_timing.py` (real FastAPI + `FastAPIInstrumentor` + uvicorn + `requests`) | Header set via FastAPI `Response`, read with `r.headers["Server-Timing"]`, parsed; `traceresponse: 00-<trace>-<span>-01` emitted after `set_global_response_propagator(TraceResponsePropagator())`; that trace id exists in Tempo |
| E5 | Real per-stage breakdown | `stage_breakdown.py`, `--cpus=2 -e NLP_ENGINE=onnx -e TRANSFORMERS_MODEL=protectai/bert-base-NER-onnx -e TRANSFORMERS_SPACY_MODEL=en_core_web_sm` | 2,694 chars, 95 entities, recovery pass ran: `anonymize()` 785 ms = `nlp_ms` 388 + `recovery_ms` 377 + ~20 other; model `forward` 713 ms over 5 windows; HF postprocess 21 ms; spaCy components ~24 ms; `analyze` 15.8 ms (35 recognizer calls 13.1, context 1.6, `remove_duplicates` 34 calls 0.7); post-processing 1.0 ms; gate helpers 0.9 ms; handling 0.06 ms |
| E6 | ONNX Runtime built-in profile | `ort_profile.py` (INT8 model, 256 tokens, 2 intra-op threads) | `DynamicQuantizeMatMul` 58.5%, `MatMulIntegerToFloat` 16.7%, `LayerNormalization` 11.2%, `Gelu` 6.0%, `Softmax` 2.4%; 1.3 MB JSON for 4 runs; 80 ms wall with profiling on |
| E7 | Profilers on the real engine | `profilers.py` (`pip install --target /s/pylib pyinstrument yappi`, scratch only) | baseline 760 ms; cProfile +6%; pyinstrument +5%; yappi (wall) +11%; details in 4.5 |
| E8 | Forked workers sharing one Resource | `multiproc_metrics.py` (4 children x 50 increments) | same `service.instance.id`: 1 series, latest 50, `increase[2m]` 117.5; per-process id: 4 series, latest sum 200 |
| E9 | Playground mock-up on Streamlit 1.56.0 | `st_mock.py` + `st_test.py` (`streamlit.testing.v1.AppTest`) | script runs, no exception; 3 `st.metric`, 1 `st.expander`, `st.bar_chart(horizontal=True, sort=False)`, `st.dataframe` with `ProgressColumn` (AppTest does not render pixels) |
| E10 | Exemplars | Prometheus `/api/v1/query_exemplars` | exemplars with `trace_id`/`span_id` exist for `http_server_duration_milliseconds_bucket` and `pii_detect_duration_milliseconds_bucket` |

### 4.1 Comparison

| # | Option | Granularity | Overhead (evidence) | Where you see it | Pool thread / multi-process | Verdict |
|---|---|---|---|---|---|---|
| 1 | Manual stage timers -> `AnonymizeStats.steps` | any step you wrap | 0.24 µs per timer [E3] | log line, anything fed from it | per-call recorder, no locks; library stays OTel-free | **Always-on base** |
| 2 | OTel child spans (retroactive) | one span per top-level step | 7 µs each; 11 = 76 µs [E3] | Tempo waterfall, TraceQL, Tempo span metrics; Azure `dependencies` + transaction view | built on the event-loop thread after `await`; sampling applies | **Always-on (sampled)** |
| 3 | Histogram `pii.step.duration{step,size}` | per step, aggregated | 1.3 µs per record [E3] | Prometheus/Grafana; Azure `customMetrics` (avg/max only) | per-process series; needs distinct instance id [E8] | **Always-on, after Phase 0** |
| 4 | `Server-Timing` (+ `traceresponse`, or JSON `timings`) | per request, per top-level step | ~0.3 KB header | Playground, curl, browser devtools | none | **Always-on for Playground; policy D1** |
| 5 | Whole-request profilers: pyinstrument, cProfile, yappi | every Python frame | +5% / +6% / +11% [E7] | HTML / pstats / callgrind | must start inside the pool thread (pyinstrument) | **On-demand** |
| 6 | Sampling / continuous: py-spy, Pyroscope, Scalene | stacks over time | py-spy: external, low (README); others not measured | flamegraph; Pyroscope in Grafana | `--subprocesses`; Pyroscope after fork | **Runbook / optional** |
| 7 | Native: ORT profile, spaCy/HF wrappers | ONNX ops; spaCy component; HF stage | ORT profile 1.3 MB per 4 runs [E6]; wrappers 0.24 µs | JSON (chrome trace), log | session options fixed at creation | **On-demand** + 2 cheap always-on details |
| 8 | Presidio hooks / per-recognizer | per recognizer | 34 timers ~9 µs *(inference from E3)* | dict -> attribute / on-demand | per-call dict | **On-demand**; top-N slowest optional |

### 4.2 Option 1 - manual stage timers (the base)

- **Primitive:** a class-based timer (`__enter__/__exit__`) costs 244 ns, `@contextmanager` 509 ns, a bare `perf_counter` pair 83 ns [E3]; ~15 timers per request are noise.
- **Thread safety:** the engine is shared by up to two detection threads per worker (main pool + express lane, `app/nlp_pool.py:43-90`, `app/main.py:154-159`). Give each `anonymize()` call its own `StepRecorder`; do not put state on the engine. The existing `threading.local` hand-off (`engine.py:176,502,505`) works for three scalars but is awkward for a growing structure, and `detect()` also calls `_run_pipeline`; pass the recorder as an argument (default `None`).
- **Detail hooks inside Presidio:** `fast_remove_duplicates` (`fast_analysis.py:44`), `KeyValueContextEnhancer.enhance_using_context` (`context_enhancer.py:78`) and `_trim_padding`'s `trimmed` forward (`onnx_nlp_engine.py:73-108`) are *our* code, so they can add to a "current recorder" held in a `ContextVar` that `anonymize()` sets and resets in `finally`; with none set they cost one lookup. Dedup runs once per pattern recognizer (`pattern_recognizer.py:266` [P4]) plus the analyzer's final call (`analyzer_engine.py:258`): 34 calls per request in [E5], so `analyze.dedup` is the inclusive total. It exists only while `FAST_ANALYSIS=true` (`fast_analysis.py:80`).
- **Exclusive vs inclusive:** top-level steps tile the request and carry `(offset_ms, duration_ms)`; details are durations only. Always publish `other = total - sum(top-level)` so a new un-timed function shows up as `other` instead of silently disappearing.
- **Wall clock:** spans need wall-clock time, timers use `perf_counter`: capture `time.time_ns()` once when the pool callable starts and add offsets.

### 4.3 Options 2 and 3 - OpenTelemetry spans and histograms

**Retroactive spans (library stays OTel-free).** `tracer.start_span(name, context=ctx, start_time=ns)` then `span.end(end_time=ns)` after the pool call returns [E1]; the library only returns numbers.
- **Cost / budget:** 7.0 µs per retroactive span, 8.2 µs real-time, 2.9 µs when the sampler drops it; 11 spans 76 µs, 45 spans 311 µs [E3] (the design needs at most 9). CPU is not the constraint; **volume** is: today a request makes 5 spans [E0], the proposal makes up to 14 (11 after `exclude_spans`). `FastAPIInstrumentor.instrument_app(..., exclude_spans=["receive","send"])` removes the three ~0 ms ones (`fastapi/__init__.py:246` [P2]).
- **Export queue:** `BatchSpanProcessor` defaults: queue 2048, delay 5000 ms, batch 512 (`opentelemetry/sdk/environment_variables`, `trace/export/__init__.py` [P1]); a burst of 5,000 spans created back-to-back (~8.5 µs each) dropped spans (`Queue full, dropping Span`) [E3]. `app/telemetry.py:99` uses defaults.
- **Sampling:** the SDK sampler defaults to `parentbased_always_on` and honours `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` (`trace/sampling.py` docstring [P1]); `app/telemetry.py:77` (`TracerProvider(resource=_resource)`) passes no sampler, so env controls it. Azure ships `ApplicationInsightsSampler` (`export/trace/_sampling.py:26` [P3]). Metrics and logs are not sampled; span-derived metrics are (Tempo docs: choose where to generate span metrics if you sample [S7]).
- **Azure mapping [P3]:** SERVER/CONSUMER -> `requests`; INTERNAL/CLIENT/PRODUCER -> `dependencies` (`export/trace/_exporter.py:260-272,358-367`); INTERNAL gets `type = "InProc"` (`:484-485`); `duration = end_time - start_time`, so retroactive timestamps survive (`:240-250`); `name` = span name, `operation_Id` = trace id, `operation_ParentId` = parent span id; `cloud_RoleName` = `service.name` (`_utils.py:267-271`); attributes -> `customDimensions` strings, key <= 150 chars, value truncated to 8,192 chars (`_utils.py:353-372`); span events -> one `traces` row each, exception events -> `exceptions` (`:536-575`). Microsoft: spans live in `requests` and `dependencies`; the portal's end-to-end transaction view lists dependencies and exceptions [S10]. That INTERNAL children nest in that waterfall is *(inference)*, not seen.
- **Tempo [E1, P9]:** waterfall and nesting correct; `traces_spanmetrics_*` generated for INTERNAL spans (`tempo-config.yaml`: `span-metrics` and `service-graphs` enabled, remote-write to Prometheus with exemplars); default span-metrics buckets 0.002 ... 16.384 s [S7]. Percentiles from them are coarse: for `nlp_pass1` injected as 400 ms x U(0.7, 1.4) (true p95 ~550 ms) TraceQL metrics returned 676 ms and span metrics 910 ms *(true value is my arithmetic)*. For exact percentiles use KQL `percentile()` or a histogram with finer bounds.

**Histogram `pii.step.duration`.**
- **Cardinality:** `step` (<= 11) x `size` (4, `_size_class` `app/main.py:186`) x `recovery_pass` (2) is ~90 series x ~18 buckets per worker process. Do **not** add `app_id` / `app_name` (the existing histograms do, `app/main.py:444-450`); multiply by the number of apps and workers otherwise.
- **Default boundaries end at 10,000 ms** (`_DEFAULT_EXPLICIT_BUCKET_HISTOGRAM_AGGREGATION_BOUNDARIES`, `aggregation.py:448-466` [P1]); larger values fall in the overflow bucket, and Prometheus returns "the upper bound of the second highest bucket" for a quantile in the highest bucket [S8]. Verified: p50 = p95 = 10000 for values of 12-25 s [E2]. **The existing `pii.detect.duration` and `pii.detect.queue_wait` histograms are affected today** (live `le` labels, [E2]); exactly the ~10 s UAT symptom would be clipped.
- **Fix:** `meter.create_histogram(..., explicit_bucket_boundaries_advisory=[...120000])` (SDK 1.40 `metrics/_internal/__init__.py:196-226` [P1]); verified p50 17500 / p95 28500 [E2]. A `View` with `ExplicitBucketHistogramAggregation` also works *(not run)*.
- **Azure [P3]:** `Histogram` -> `value = sum`, `count`, `min`, `max` only (`export/metrics/_exporter.py:252-256`); preferred temporality DELTA for Counter and Histogram (`:71-77`), so rows are additive; no buckets, no exemplars. Averages (`sum/count`) and max work; percentiles do not.
- **Exemplars:** SDK 1.40 default filter is `trace_based` (`metrics/_internal/__init__.py:427` [P1]); Prometheus here stores exemplars (`--enable-feature=exemplar-storage`, `run-prometheus.sh` [P9]) and has them for our histograms [E10]; the Grafana Prometheus datasource links them to Tempo (`exemplarTraceIdDestinations`, `grafana-datasources.yaml` [P9]). The UI click-through itself was not exercised. Azure: no exemplars.
- **Multi-process (important, pre-existing):** `app/telemetry.py:38-43` builds one `Resource` at import (pre-fork) with a constant `service.instance.id` (`pii-shield-1`); `Dockerfile:36-39` starts `${WEB_CONCURRENCY:-6}` workers. In OTLP cumulative mode their series collide in Prometheus [E8]: latest value 50 instead of 200, `increase()` wrong. Per-process ids fix it. Azure is unaffected (DELTA).

### 4.4 Option 4 - `Server-Timing`, `traceresponse`, or a JSON field

- **Spec [S1]:** `Server-Timing = #metric`; `metric-name = token`; params `dur` (ms recommended) and `desc` (token or quoted-string); no start times ("impossible to map a meaningful startTime"); keep names short; multiple entries with the same name allowed. Browsers expose it as `PerformanceServerTiming` on resource/navigation entries; **same-origin by default**, cross-origin needs `Timing-Allow-Origin`; the server may restrict metrics to authenticated users (§4). The spec lists "server timing in developer tools" as a use case (§7.1); how a given browser draws it was not checked here.
- **Our Playground is server-side Python:** `ApiClient.request` returns the raw `requests.Response` (`app/ui_auth.py:111-116`), so `resp.headers["Server-Timing"]` is readable without CORS/TAO [E4]. Those only matter if a browser app calls the API directly.
- **Compare:** header = no change to `AnonymizeUniqueResponse` (`app/models.py:19`), ~0.3 KB, ignored by unaware clients, but flat (no nesting) and must stay short. JSON `timings` field = richer and typed, but changes the response contract. Trace id via `traceresponse` = one line in `app/telemetry.py`, W3C draft format [P2], lets the UI link the exact trace [E4].
- **Recommendation:** `Server-Timing` with top-level steps + `traceresponse`; keep JSON untouched. Policy for who gets the header is D1.

### 4.5 Option 5 - whole-request and continuous profilers

| Tool | Measures | Overhead | Pool thread / gunicorn | Wired here |
|---|---|---|---|---|
| **pyinstrument** 5.1.3 [S3] | sampled stacks, **wall clock**, 1 ms default, `PyEval_SetProfile` C extension; HTML / JSON / speedscope / pstats | **+5%** [E7] (docs' own table: 30% on a Django benchmark) | per thread. Docs warn the FastAPI middleware only profiles `async` code, not code run on another thread [S3]; verified: started on the caller thread the report had no `pii_shield` frame, started inside the worker it did [E7] | wrap the callable run by `_run_in_nlp_pool_timed` (`app/main.py:162-178`), admin-only, return HTML |
| **cProfile** (3.12) | deterministic call counts/times | **+6%** here (one native call dominates) | 3.12 builds it on `sys.monitoring` (tool "cProfile" verified), enabled on one thread it recorded frames from another [E7]; noisy under concurrency | one-off scripts |
| **yappi** 1.7.6 [S4] | tracing, `wall` or `cpu` clock, multithread + asyncio aware | **+11%** [E7] | saw both threads [E7]; can start from any thread | on-demand admin |
| **py-spy** [S2] | external sampler; `record`/`top`/`dump`; `--subprocesses` (gunicorn pools), `--gil`, `--native` (Linux x86-64/ARM/aarch64), `--idle`; flamegraph / speedscope / raw; supports 3.3-3.15 | "extremely low", not measured | attaches to a PID: needs `SYS_PTRACE` in Docker/Kubernetes [S2]. ACA "doesn't allow privileged containers" [S10]; whether `SYS_PTRACE` can be added there is **not documented** | local `cap_add: SYS_PTRACE`; Azure unverified |
| **Scalene** [S5] | sampling; separates Python vs native vs system time; claims 10-20% | not measured | not tried | local deep-dive only |
| **line_profiler**, `sys.monitoring` (PEP 669) [S9] | per line / custom tracer | not measured | `sys.monitoring` events are set globally (docs section "Setting events globally") | not recommended: custom code for no gain over the options above |
| **Pyroscope** [S6] | continuous sampling via `pyroscope-io` | not measured | call `pyroscope.configure()` after the fork, e.g. gunicorn `post_fork` [S6]; the repo uses `--preload` | otel-lgtm bundles Pyroscope 2.4.3 + a Grafana datasource `pyroscope` [P9]; compose publishes only 3000/4317/4318, so port 4040 is reachable inside the compose network only. Azure Managed Grafana: not verified |

**Wall vs CPU, and native code.** Almost all our time is one native call: cProfile `tottime` for `InferenceSession.run` was 0.698 s of 0.826 s; pyinstrument showed `InferenceSession.run` 0.690 s of 0.793 s (87%) and `AnalyzerEngine.analyze` 0.031 s, with "CPU time 1.501 s vs duration 0.794 s" because ONNX Runtime uses two threads [E7]. Sampling profilers attribute native time to the Python call site, not to ONNX ops (see 4.6). **Pyroscope defaults `oncpu=True` and `gil_only=True`** [S6]; py-spy's README says `--gil` "will miss activity in extensions that release the GIL while still active" [S2]. ONNX Runtime releases the GIL during `run` *(inference, not verified)*, so set `gil_only=False` and consider `oncpu=False` or the NER time may vanish from the flamegraph.

### 4.6 Option 6 - attributing time inside native components

- **ONNX Runtime:** `SessionOptions.enable_profiling = True` then `session.end_profiling()` writes a JSON chrome trace with per-op durations [S11]. [E6] on the INT8 model: matmul kernels ~75%, LayerNorm 11%, Gelu 6%. The file is ~0.3 MB per inference, session options are fixed at creation (`onnx_nlp_engine.py:111`), so use it **only** as a separate scratch session / runbook step, never per request.
- **spaCy 3.8.18:** no built-in per-component timing; `Language.__call__` loops `for name, proc in self.pipeline` without timers (`language.py:1044`); the only timer is `evaluate()` (`:1453-1465`) and the `spacy benchmark speed` CLI [P6]. Wrapping the `onnx_ner` entry of `nlp._components` works [E5]: spaCy (tok2vec, tagger, attribute_ruler, lemmatizer) ~24 ms vs NER ~742 ms for two passes, i.e. spaCy ~3%.
- **Hugging Face pipeline 4.57.6:** `ChunkPipeline.run_single` calls `self.preprocess`, `self.forward`, `self.postprocess` per window (`pipelines/base.py:1472-1498`; `token_classification.py:281,340,366` [P5]); no timer, but instance-level wrapping works [E5]: preprocess ~0, `model.forward` 713 ms (5 windows), postprocess 21 ms. Cheapest always-on hooks: sum of `forward` time inside the existing `trimmed` wrapper (`onnx_nlp_engine.py:73-108`, installed when `ONNX_TRIM_PADDING=true`, else wrap `model.forward` directly) -> `nlp_pass1.ner_model`, and the window count -> `pii.ner_windows` (explains why time scales with text length).

### 4.7 Option 7 - Presidio hooks and per-recognizer time

- **What exists:** `AnalyzerEngine(app_tracer=..., log_decision_process=...)` (`analyzer_engine.py:54-70` [P4]) writes decision strings, including `nlp_artifacts.to_json()`, to logger `decision_process` at INFO (`app_tracer.py`); it is for explaining results, not timing, and serialises large objects. No timing hook; `analyze()` loops `for recognizer in recognizers: recognizer.analyze(...)` (`:231-245`), then `_enhance_using_context` (`:247`), then `EntityRecognizer.remove_duplicates` (`:258`).
- **Cheap attribution:** wrap the *instance* attribute `recognizer.analyze` (Presidio calls it on the instance); `scripts/profile_detection.py` and [E5] do exactly this. 34 timers ~ 9 µs per request *(inference from E3)*.
- **Budget for ~34-50 recognizers:** one span per recognizer = 34 x 7 µs ~ 0.25 ms CPU (fine), but 34-50 extra rows per request in Azure `dependencies` and Tempo (not fine as a default). A histogram with a `recognizer` label is ~50 x 18 buckets x workers series, and in Azure collapses to sum/count/min/max. Better, in order: (1) **on-demand only** (header/env), (2) always-on aggregate dict emitted as **one** attribute with the top 3 slowest (`pii.slow_recognizers`, <= 8,192 chars in Azure [P3]), (3) a counter `pii.recognizer.slow{recognizer}` incremented above a threshold (bounded labels).

### 4.8 Showing the breakdown

Section 6 gives the Playground (Streamlit 1.56: `st.metric`, `st.expander`, `st.bar_chart`, `st.dataframe` + `ProgressColumn`, `st.caption`, verified by AppTest [E9, P8]), Grafana (PromQL, TraceQL) and Application Insights (KQL).

## 5. Recommended design and phased plan

### 5.1 Steps

- **Always-on (stable names, top-level, exclusive):** `auth`, `app_config`, `queue_wait`, `nlp_pass1`, `analyze`, `recovery_pass2` (only if it ran), `post_processing`, `handling`, `store_save`; derived `other`.
- **Always-on details (inclusive, no spans):** `nlp_pass1.ner_model`, `analyze.context_enhance`, `analyze.dedup`, counter `pii.ner_windows`, boolean `pii.recovery_pass` (exists).
- **On-demand only:** per recognizer; per post-processing function; per HF stage; pyinstrument HTML for one request; ORT op profile; py-spy.

### 5.2 Wiring (file by file)

| File | Change |
|---|---|
| `pii_shield/observability.py` | `StepRecorder` (class-based timer; `step(name)` records offset + duration, `add(name, ms)` for details, `as_dict()`), `Event.steps` |
| `pii_shield/models.py` | `AnonymizeStats.steps: dict[str, float]` and `timeline: list[tuple[str, float, float]]` (name, offset_ms, duration_ms) |
| `pii_shield/engine.py` | create the recorder in `anonymize()`, pass to `_run_pipeline(..., steps=None)`; wrap `nlp_pass1`, `analyze`, `recovery_pass2`, `post_processing`, `handling`; set/reset the `ContextVar` in `finally` |
| `fast_analysis.py`, `context_enhancer.py`, `onnx_nlp_engine.py` | detail timers via the `ContextVar` (no-op without a recorder) |
| `app/main.py` | time `app_config`, `store_save`; read `auth` from `request.state`; merge steps; emit spans, histogram, log field, headers |
| `app/auth.py` | stash `request.state.auth_ms` in `authenticate` (`:434-456`) |
| `app/timing.py` (new) | `emit_step_spans()`, `server_timing_header()`, `pii.step.duration` instrument |
| `app/telemetry.py` | `set_global_response_propagator(TraceResponsePropagator())`; per-process `service.instance.id` (D4) |
| `app/streamlit_app.py` | parse `Server-Timing`, show 6.4 |
| `observability/grafana/dashboards*/` | new panels; the local compose file mounts each dashboard file individually (`docker-compose.yml`), so a new dashboard also needs a mount line |
| `tests/` | see 5.5 |

Spans and header are built in `app/` after `await _run_in_nlp_pool_timed(...)` returns; the library only returns numbers, so the "no `opentelemetry` import in `pii_shield/`" rule holds. Emission sketch (verified pattern, [E1]):

```python
def emit_step_spans(parent, anchor_ns, steps):          # steps: [(name, offset_ms, duration_ms)]
    ctx = trace.set_span_in_context(parent)
    for name, offset_ms, dur_ms in steps:
        start = anchor_ns + int(offset_ms * 1e6)
        child = tracer.start_span(name, context=ctx, start_time=start, attributes={"pii.step": name})
        child.end(end_time=start + int(dur_ms * 1e6))
```

### 5.3 Overhead budget

~15 timers x 0.24 µs = 3.7 µs; up to 9 step spans x 7 µs = 63 µs (E3 measured 11 = 75.7 µs); ~12 histogram records = 14.4 µs; headers/log ~ 10 µs *(inference)*. **Total ~ 0.1 ms per request**, i.e. 0.2% of the shortest live request seen (48 ms [E0]) and 0.01% of [E5]'s 785 ms. On 2-vCPU x86 assume 2-3x per-span cost *(inference)*: still < 0.5%. Memory: queue of at most 2048 spans. Per-recognizer always-on aggregate (optional): ~9 µs. Real costs to watch are export volume and Azure ingestion (D2), not CPU.

### 5.4 Thread-safety and process model

One recorder per `anonymize()` call, owned by the thread that runs it (main pool or express lane); details reach it through a `ContextVar` set inside that call, not through the engine. Spans/histograms are created on the event-loop thread after the `await`, with the trace context that `_run_in_nlp_pool_timed` already copies (`app/main.py:162-179`). Nothing starts at import (`--preload` safe, as the new instrument is a proxy like the existing counters, `app/telemetry.py` docstring); `init_telemetry()` stays per worker.

### 5.5 Test strategy

1. **Recorder:** nesting, repeated names summed, `finally` on exception, `other >= 0`, no step exceeds `detect_ms`.
2. **Thread ownership:** extend `tests/test_detection_timing.py` (pattern of `test_the_stats_belong_to_the_thread...`): two threads, plain vs ALL-CAPS text, assert each result's steps (`recovery_pass2` only in the second).
3. **Names are an API:** assert the set of top-level names equals the allowed list (dashboards depend on it).
4. **Header:** `TestClient` POST `/anonymize_unique`; header matches the W3C ABNF (`name;dur=<float>` list), values equal the span/histogram values.
5. **Spans:** `InMemorySpanExporter`: children's parent is `pii_shield.anonymize`, `start >= parent.start`, `end <= parent.end`, names in the allowed set, count <= 9.
6. **Histogram:** `InMemoryMetricReader`: only `step`, `size`, `recovery_pass` attributes; bounds include 120000.
7. **Library stays OTel-free:** subprocess `import pii_shield` and assert no `opentelemetry` in `sys.modules` (no such guard exists today; only `tests/test_telemetry.py` mentions it).
8. **Dashboards:** `tests/test_dashboard_json.py` auto-discovers `dashboards/*.json`; datasource uids must be in {prometheus, loki, tempo}, so a Pyroscope panel would need that set widened.

### 5.6 Phased plan (effort = engineer-days *(inference)*)

| Phase | Content | Effort | Overhead |
|---|---|---|---|
| **0 Trust the numbers** | wider histogram bounds for `pii.detect.*`; per-process `service.instance.id`; `exclude_spans` | 0.5 d | none |
| **1 Library steps + log** | `StepRecorder`, `AnonymizeStats.steps`, API steps (`auth`, `app_config`, `store_save`), log field, tests 1-3 | 1-1.5 d | ~5 µs |
| **2 Spans + histogram + header** | retroactive spans, `pii.step.duration`, `Server-Timing`, `traceresponse`, tests 4-6, local + Azure dashboard panels | 1-1.5 d | ~0.1 ms |
| **3 Playground** | timing expander (6.4), trace id display | 0.5-1 d | none server-side |
| **4 On-demand deep profile (optional)** | per-recognizer / per-function mode generalising `scripts/profile_detection.py`; admin-only pyinstrument-in-pool-thread; runbook for ORT profile / py-spy; decide on Pyroscope | 1-2 d | only when switched on |

Core (phases 0-3): ~3-4.5 days.

### 5.7 Decisions needed from the owner

- **D1** Who receives `Server-Timing`: everyone, authenticated users, or opt-in header (spec §4 allows restricting).
- **D2** Azure span volume: always, only sampled traces (`OTEL_TRACES_SAMPLER=parentbased_traceidratio`), or only slow requests; and whether to drop spans < 1 ms (biases averages).
- **D3** Change bucket boundaries of the existing `pii.detect.*` histograms (new `le` series).
- **D4** Per-process `service.instance.id` (changes series identity and Azure `cloud_RoleInstance`); alternative: leave and accept collisions locally.
- **D5** On-demand trigger (request header vs env flag vs admin endpoint), who may use it, and whether profiler HTML may be returned in production.
- **D6** Extending the public library API (`AnonymizeStats.steps`, `Event.steps`) and the step names.
- **D7** `exclude_spans=["send","receive"]`.
- **D8** Playground: always show the breakdown or keep it in a collapsed expander (mock-up uses an expander); trace link target (Grafana URL template).
- **D9** Continuous profiling (Pyroscope): where it would be hosted in Azure.

## 6. How to view it

### 6.1 Grafana (local, Prometheus datasource `prometheus`)

Works today for INTERNAL spans through Tempo's span metrics (these forms, with a fixed `[30m]` range instead of `$__rate_interval`, ran against the scratch data, [E1]):

```promql
# average ms per step (stacked time series); after Phase 2 filter span_name to the allowed list
1000 * sum by (span_name) (rate(traces_spanmetrics_latency_sum{service="pii-shield"}[$__rate_interval]))
     / sum by (span_name) (rate(traces_spanmetrics_latency_count{service="pii-shield"}[$__rate_interval]))
# coarse p95 from power-of-two buckets
1000 * histogram_quantile(0.95, sum by (le, span_name) (rate(traces_spanmetrics_latency_bucket{service="pii-shield"}[$__rate_interval])))
```

After Phase 2, from the proposed histogram (the name follows the same mapping as the live `pii_detect_duration_milliseconds_*`; the `histogram_quantile`/`sum/count` forms were run on scratch metrics, [E2]; this exact metric does not exist yet, so not run):

```promql
histogram_quantile(0.95, sum by (le, step) (rate(pii_step_duration_milliseconds_bucket{service_name="pii-shield"}[$__rate_interval])))
sum by (step) (rate(pii_step_duration_milliseconds_sum{service_name="pii-shield"}[$__rate_interval]))   # stacked share
```

### 6.2 TraceQL (Tempo datasource `tempo`; run through the Tempo HTTP API, not the Grafana UI)

Always pass `start`/`end` (Unix seconds): the same search without them returned 0 traces, also minutes after ingestion. Inside the otel-lgtm container, against the scratch traces (`SVC=pii-shield-research-scratch`; use `pii-shield` for the real service):

```bash
q() { curl -s -G localhost:3200"$1" --data-urlencode "q=$2" --data-urlencode start=$START --data-urlencode end=$NOW "${@:3}"; }
q /api/search  "{ resource.service.name=\"$SVC\" && name=\"nlp_pass1\" && duration > 500ms }" --data-urlencode limit=100   # 9 of 40 traces
q /api/search  "{ resource.service.name=\"$SVC\" && span.pii.step != nil } | avg(duration) > 100ms"                                       # 25 traces
q /api/search  "{ resource.service.name=\"$SVC\" && span.pii.step != nil } | select(span.pii.step)" --data-urlencode limit=1 --data-urlencode spss=20   # 8 spans in the trace
q /api/metrics/query "{ resource.service.name=\"$SVC\" && span.pii.step != nil } | quantile_over_time(duration, .5, .95) by (name)"   # 16 series
q /api/metrics/query_range "{ resource.service.name=\"$SVC\" && span.pii.step != nil } | avg_over_time(duration) by (name)" --data-urlencode step=300   # 8 series
```

Verified output for `quantile_over_time ... by (name)`: `nlp_pass1` p50 0.39 s / p95 0.68 s, `recognizers` 0.063 / 0.124, `dedup` 0.008 / 0.016, ... (seconds) [E1]. The `select()` default is 3 spans per trace (my observation); `spss=20` returned all 8 (`spss` is in the Tempo API docs [S7]). `query_range` worked with `step=300` and `step=300s`. **Gotcha:** my first attempts, within about a minute of ingestion, returned empty `series` for metrics; the same syntax worked a few minutes later, so allow for ingestion delay (Tempo docs: `query_backend_after`, default 15 m, splits live-store and backend queries [S7]; I did not confirm that this was the cause). The `.../configure-traceql-metrics` page [S7] lists no extra processor requirement for Tempo 3.1.1 and none was configured here. The Grafana UI path was not exercised.

### 6.3 Application Insights / Azure Managed Grafana (KQL, **not run**: no Azure access)

Schema facts are from the exporter source [P3] and Microsoft docs [S10]: step spans are `dependencies` rows with `type == "InProc"`, `name` = step, `duration` in ms, `operation_Id` = trace id, `customDimensions["pii.step"]`, `cloud_RoleName` = `service.name`. The repo's Azure dashboards already use `percentile(duration, 50)` on `requests` (`dashboards-azure/pii-shield.json`) and the Azure Monitor datasource `azureLogAnalytics` query type, deployed by `infra/scripts/04-deploy-dashboards.sh`.

```kusto
dependencies
| where timestamp > ago(1h) and cloud_RoleName endswith "shield" and type == "InProc"
| where isnotempty(tostring(customDimensions["pii.step"]))
| summarize calls = count(), avg_ms = avg(duration), p50 = percentile(duration, 50), p95 = percentile(duration, 95), max_ms = max(duration)
    by step = tostring(customDimensions["pii.step"])
| order by avg_ms desc

union requests, dependencies                      // one request's waterfall; id from the traceresponse header
| where operation_Id == "<trace id>"
| project timestamp, itemType, name, duration, id, operation_ParentId
| order by timestamp asc
```

`customMetrics` for the histogram (average only): `... | summarize avg_ms = sum(valueSum) / sum(valueCount) by step = tostring(customDimensions["step"])`. The `AppMetrics` doc page maps `ItemCount` to `valueCount`; `valueSum` was **not** confirmed, and the repo's dashboards only use `value`. Prefer the span query for percentiles.

### 6.4 Playground mock-up (verified to run on Streamlit 1.56.0 with AppTest, [E9])

```
 Server total 788 ms │ Round trip 803 ms (+15 ms network+serialize) │ Slowest step: nlp_pass1 388 ms
 ▸ ⏱ Timing breakdown
   nlp_pass1       ████████████████████  387.6 ms  49%
   recovery_pass2  ███████████████████▌  377.1 ms  48%
   recognizers...  ▏ ... store_save 2.4 ms · handling 0.1 ms
   Server-Timing header, ms · trace id 912d2758…  [Open in Grafana]
```

```python
# app/streamlit_app.py, inside `if resp.status_code == 200:` after `data = resp.json()`  (mock-up, not applied)
def parse_server_timing(header: str) -> dict[str, float]:       # keep `desc` out of the header: this split ignores quoted strings
    out = {}
    for item in header.split(","):
        name, *params = [p.strip() for p in item.strip().split(";")]
        dur = dict(p.split("=", 1) for p in params if "=" in p).get("dur")
        if name and dur:
            out[name] = float(dur)
    return out

timing = parse_server_timing(resp.headers.get("Server-Timing", ""))
if timing:
    total = timing.pop("total", sum(timing.values()))
    c1, c2, c3 = st.columns(3)
    c1.metric("Server total", f"{total:.0f} ms")
    c2.metric("Round trip", f"{resp.elapsed.total_seconds() * 1000:.0f} ms")
    slowest = max(timing, key=timing.get)
    c3.metric("Slowest step", slowest, delta=f"{timing[slowest]:.0f} ms", delta_color="off")
    df = pd.DataFrame({"step": list(timing), "ms": list(timing.values())})
    df["share"] = df["ms"] / df["ms"].sum()
    with st.expander("⏱ Timing breakdown"):
        st.bar_chart(df, x="step", y="ms", horizontal=True, sort=False)
        st.dataframe(df, hide_index=True, column_config={
            "ms": st.column_config.NumberColumn("ms", format="%.1f"),
            "share": st.column_config.ProgressColumn("share", format="percent", min_value=0, max_value=1)})
```

Streamlit pieces used (all exist in 1.56.0 [P8]): `st.metric` (value + delta), `st.expander`, `st.bar_chart(data, x, y, horizontal, sort)`, `st.dataframe(column_config=...)`, `st.caption`. `pandas` is already installed (Streamlit dependency).

## 7. Risks, gotchas, open questions

1. **Pre-existing metric collision** across workers (4.3, [E8]) makes today's local Grafana counters and histograms unreliable in multi-worker runs *(inference for the live stack: 6 workers, same instance id)*; fix is D4.
2. **10 s bucket ceiling** clips exactly the case being investigated (4.3, [E2]); fix is D3.
3. **Azure has no buckets/exemplars/span-metrics**; local and Azure views differ by design: histograms (averages) + spans (percentiles) there, Tempo span metrics + exemplars here.
4. **Retroactive span nesting:** children must lie inside `pii_shield.anonymize`; the parent starts before the pool submit (`app/main.py:408`), so `queue_wait` and the library steps are laid out from the submit/start times; a test (5.5 #5) pins it.
5. **Sampling skews span-derived numbers** (Tempo span metrics, App Insights portal shows only retained telemetry [S7, S10]); histograms and Server-Timing do not.
6. **Span volume / ingestion cost in Azure** is unmeasured *(inference: ~3x spans per request; D2)*. BSP queue 2048 can drop on bursts [E3]; tune `OTEL_BSP_MAX_QUEUE_SIZE` if needed.
7. **`FAST_ANALYSIS=false` or another Presidio version** removes `analyze.dedup` (our function is not installed, `fast_analysis.py:80`); document, do not fake.
8. **Server-Timing leaks internals** (spec §4); gate it (D1). Timing correlates with input length/content; low risk, noted.
9. **Two detection threads per worker** (main + express lane): never keep per-engine mutable timing state; the existing `threading.local` is per thread, so it is safe, but a recorder argument is cleaner.
10. **Numbers are from an aarch64 Docker VM**, one text, one run set; ACA x86 2-vCPU will differ. Re-run `stage_breakdown.py` on a replica (the repo's `scripts/profile_detection.py` already supports `az containerapp exec`, `DEPLOYMENT.md`).
11. **On-demand profiling is an attack surface** (CPU, HTML exposing code paths): admin role + env flag + rate limit (D5).
12. **Open:** can py-spy attach in ACA (capabilities)? Is there a Pyroscope home in Azure? Is the ~10 s UAT case queueing (`queue_wait`), model windows (long text, two passes) or a pathological regex? Phase 1-2 answers this per request without guessing.

## 8. Sources

External (accessed 2026-11-25):

- **S1** W3C Server Timing, https://www.w3.org/TR/server-timing/ : ABNF, `dur`/`desc`, no start times, same-origin + `Timing-Allow-Origin`, devtools use case.
- **S2** py-spy README, https://github.com/benfred/py-spy (raw `README.md`): record/top/dump, `--subprocesses`, `--gil`, `--native` table, `SYS_PTRACE` in Docker/Kubernetes, Python 3.3-3.15.
- **S3** pyinstrument 5.1.3 docs, https://pyinstrument.readthedocs.io/en/latest/guide.html and `/how-it-works.html` : FastAPI middleware caution, 1 ms sampling, wall clock, `PyEval_SetProfile`, async via contextvars.
- **S4** yappi README, https://github.com/sumerc/yappi : thread/asyncio aware tracing profiler, wall/cpu clocks.
- **S5** Scalene README, https://github.com/plasma-umass/scalene : Python vs native vs system time, sampling, "no more than 10-20%" claim (vendor claim).
- **S6** Grafana Pyroscope Python client, https://grafana.com/docs/pyroscope/latest/configure-client/language-sdks/python/ : `pyroscope-io`, options `oncpu`/`gil_only`, configure after fork (gunicorn `post_fork`).
- **S7** Grafana Tempo: https://grafana.com/docs/tempo/latest/metrics-from-traces/metrics-queries/configure-traceql-metrics/ ; https://grafana.com/docs/tempo/latest/metrics-from-traces/span-metrics/ ; raw `docs/sources/tempo/configuration/_index.md` (span-metrics `histogram_buckets` default 0.002 ... 16.384) and `api_docs/_index.md` (`spss`).
- **S8** Prometheus `histogram_quantile`, https://prometheus.io/docs/prometheus/latest/querying/functions/ : highest-bucket behaviour.
- **S9** Python 3.12 `sys.monitoring`, https://docs.python.org/3.12/library/sys.monitoring.html : tool identifiers, global events.
- **S10** Microsoft Learn: Application Insights data model https://learn.microsoft.com/en-us/azure/azure-monitor/app/data-model-complete (spans in `requests`/`dependencies`, dependency fields); transaction diagnostics https://learn.microsoft.com/en-us/azure/azure-monitor/app/transaction-diagnostics (end-to-end view, sampling note); AppMetrics https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/appmetrics ; Container Apps https://learn.microsoft.com/en-us/azure/container-apps/containers ("doesn't allow privileged containers mode").
- **S11** ONNX Runtime profiling tools, https://onnxruntime.ai/docs/performance/tune-performance/profiling-tools.html : `enable_profiling`, JSON trace.

Installed package source in the running app image (`/usr/local/lib/python3.12/site-packages/`):

- **P1** opentelemetry-sdk 1.40.0: `metrics/_internal/aggregation.py:448-466` (default bounds), `metrics/_internal/__init__.py:196-226` (advisory), `:427` (exemplar filter), `trace/export/__init__.py` + `environment_variables/__init__.py` (BSP defaults 2048 / 5000 / 512), `trace/sampling.py` (env samplers).
- **P2** opentelemetry-instrumentation 0.61b0: `propagators.py:38-47,115` (`traceresponse`), `asgi/__init__.py:1009` (response propagator), `fastapi/__init__.py:246` (`exclude_spans`).
- **P3** azure-monitor-opentelemetry-exporter 1.0.0b51: `export/trace/_exporter.py:240-272,358-367,484-485,536-575`; `export/metrics/_exporter.py:71-77,252-256`; `_utils.py:267-271,353-372`; `export/trace/_sampling.py:26`.
- **P4** presidio-analyzer 2.2.362: `analyzer_engine.py:54-70,225-258`; `app_tracer.py`.
- **P5** transformers 4.57.6: `pipelines/base.py:1472-1498`; `pipelines/token_classification.py:281,340,366`.
- **P6** spaCy 3.8.18: `language.py:1044,1453-1465`; `cli/benchmark_speed.py`.
- **P8** Streamlit 1.56.0 (signatures, `AppTest`); onnxruntime 1.32.0 (`SessionOptions.enable_profiling`, run in [E6]).
- **P9** grafana/otel-lgtm v0.40.0 container files: `/otel-lgtm/tempo-config.yaml`, `run-prometheus.sh`, `prometheus.yaml`, `otelcol-config.yaml`, `run-pyroscope.sh`, `grafana/conf/provisioning/datasources/grafana-datasources.yaml`.

Repo files read: `app/main.py`, `app/telemetry.py`, `app/nlp_pool.py`, `app/auth.py`, `app/state_store.py`, `app/ui_auth.py`, `app/streamlit_app.py`, `app/models.py`, `pii_shield/engine.py`, `models.py`, `observability.py`, `fast_analysis.py`, `context_enhancer.py`, `onnx_nlp_engine.py`, `scripts/profile_detection.py`, `tests/test_detection_timing.py`, `tests/test_dashboard_json.py`, `DEPLOYMENT.md`, `Dockerfile`, `docker-compose.yml`, `observability/grafana/**`, `infra/scripts/04-deploy-dashboards.sh`.

### Not verified

- Anything on Azure itself: KQL was not run; table/column names come from the exporter source and Microsoft docs; `customMetrics` `valueSum`; how INTERNAL children render in the end-to-end transaction view; ingestion volume and cost; sampling behaviour in the portal; Azure Managed Grafana rendering.
- Grafana UI behaviour (exemplar click-through, TraceQL in Explore); only data, config and HTTP APIs were checked. A "Open in Grafana" link format was not tested.
- Browser devtools rendering of `Server-Timing` and `PerformanceServerTiming` (spec only).
- py-spy, Scalene, line_profiler, Pyroscope (`pyroscope-io` on aarch64/ACA, overhead) were not executed; py-spy inside ACA (capabilities) is unknown. "ONNX Runtime releases the GIL in `run`" is an inference.
- ORT per-op profile used random tokens (256) on a scratch session, not production windows; overhead of profiling on the production session was not measured.
- A `View`-based bucket override was not run. W3C Trace Context response-header page was not read (cited via the installed source comment).
- All timings: one 2.7 KB synthetic text, aarch64 Docker VM, `--cpus=2`, median of 5-7 runs; no ACA x86 measurements; the UAT ~10 s case was not reproduced.
- Streamlit AppTest proves the script runs, not how the chart looks.
- Encrypt / PQC cost: deliberately out of scope.
