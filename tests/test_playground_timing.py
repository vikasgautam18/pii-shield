"""AppTest of the Playground's timing panel: the real app/streamlit_app.py, with the API mocked at ``requests.request``.

Needs Streamlit (the host venv has none, so this is skipped there; run it in the app image).
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SCRIPT = str(APP_DIR / "streamlit_app.py")
API_BASE = "http://api.test"
TRACE_ID = "912d27582286f3a10ed6da832e654c39"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"
INDENT = "\u2003↳ "

ANONYMIZE_TIMING = (
    "app_config;dur=0.41, queue_wait;dur=0.12, nlp_pass1;dur=388.12, nlp_pass1.ner_model;dur=361.03, "
    "analyze;dur=15.80, analyze.context_enhance;dur=1.61, analyze.dedup;dur=0.70, recovery_pass2;dur=377.00, "
    "recovery_pass2.ner_model;dur=351.20, post_processing;dur=1.02, handling;dur=0.06, store_save;dur=2.41, "
    "other;dur=3.20, total;dur=788.30"
)
DEANONYMIZE_TIMING = "app_config;dur=0.40, store_get;dur=1.20, restore;dur=0.30, other;dur=0.50, total;dur=2.40"

ANONYMIZE_BODY = {
    "id": "sess-1234",
    "text": "Mail John Smith at john@example.com",
    "anonymized_text": "Mail {{PERSON_1}} at {{EMAIL_ADDRESS_1}}",
    "entity_mapping": {"{{PERSON_1}}": "John Smith", "{{EMAIL_ADDRESS_1}}": "john@example.com"},
    "hash_mapping": {},
    "encrypt_mapping": {},
}
DEANONYMIZE_BODY = {"id": "sess-1234", "text": "Mail John Smith at john@example.com"}


@pytest.fixture(autouse=True)
def _fake_redis():
    """Override the suite-wide fixture: it imports app.main (and loads the NER model); the Playground needs no Redis."""


def _response(body: Any, headers: dict[str, str] | None = None, *, status: int = 200, elapsed_ms: float = 803.0):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.encoding = "utf-8"
    response.headers.update(headers or {})
    response.elapsed = timedelta(milliseconds=elapsed_ms)
    return response


class FakeApi:
    """Answers the three calls the Playground makes, with the headers a test chooses."""

    def __init__(self, anonymize_headers=None, deanonymize_headers=None, *, anonymize_status=200, deanonymize_ms=9.0):
        self.anonymize_headers = anonymize_headers or {}
        self.deanonymize_headers = deanonymize_headers or {}
        self.anonymize_status = anonymize_status
        self.deanonymize_ms = deanonymize_ms
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, **kwargs):
        path = url.removeprefix(API_BASE)
        self.calls.append((method, path))
        if (method, path) == ("GET", "/apps"):
            return _response([])
        if (method, path) == ("POST", "/anonymize_unique"):
            body = ANONYMIZE_BODY if self.anonymize_status == 200 else {"detail": "boom"}
            return _response(body, self.anonymize_headers, status=self.anonymize_status)
        if (method, path) == ("POST", "/deanonymize"):
            return _response(DEANONYMIZE_BODY, self.deanonymize_headers, elapsed_ms=self.deanonymize_ms)
        raise AssertionError(f"unexpected API call {method} {url}")


@pytest.fixture
def api(monkeypatch):
    import streamlit as st

    st.cache_data.clear()  # the app caches GET /apps for a few seconds, across tests too
    fake = FakeApi({"Server-Timing": ANONYMIZE_TIMING, "traceresponse": TRACEPARENT}, {"Server-Timing": DEANONYMIZE_TIMING})
    monkeypatch.syspath_prepend(str(APP_DIR))  # the script imports themes, ui_auth and timing_view by bare name
    monkeypatch.setenv("API_BASE", API_BASE)
    monkeypatch.delenv("TRACE_URL_TEMPLATE", raising=False)
    monkeypatch.setattr(requests, "request", fake)
    return fake


def _anonymize(api_headers: dict[str, str] | None = None, *, api: FakeApi) -> AppTest:
    if api_headers is not None:
        api.anonymize_headers = api_headers
    at = AppTest.from_file(SCRIPT, default_timeout=30).run()
    assert not at.exception
    at.text_area(key="anon_input").set_value("Mail John Smith at john@example.com").run()
    at.button(key="anon_btn").click().run()
    assert not at.exception
    return at


def _metrics(at: AppTest) -> dict[str, tuple[str, str]]:
    return {metric.label: (metric.value, metric.delta) for metric in at.metric}


def _table(at: AppTest) -> list[tuple[str, float, float]]:
    frame = at.dataframe[0].value
    assert list(frame.columns) == ["Step", "ms", "Share"]
    return list(zip(frame["Step"], frame["ms"], frame["Share"], strict=True))


def _chart_categories(at: AppTest) -> list[str]:
    import pyarrow as pa  # a Streamlit dependency

    charts = at.get("vega_lite_chart")
    assert len(charts) == 1
    proto = charts[0].proto
    spec = json.loads(proto.spec)
    assert (spec["encoding"]["x"]["field"], spec["encoding"]["y"]["field"]) == ("ms", "Step")  # horizontal bars
    assert spec["encoding"]["y"]["sort"] is None  # keep the data order: first step on top
    frame = pa.ipc.open_stream(proto.datasets[0].data.data).read_all().to_pandas()
    return list(frame["Step"])


def test_anonymize_shows_the_timing_panel(api, monkeypatch):
    monkeypatch.setenv("TRACE_URL_TEMPLATE", "https://grafana.test/explore?trace={trace_id}")

    at = _anonymize(api=api)

    # The existing result is untouched.
    assert at.text_area(key="anon_result_sess-1234").value == "Mail {{PERSON_1}} at {{EMAIL_ADDRESS_1}}"
    assert at.session_state["deanon_session"] == "sess-1234"

    assert _metrics(at) == {
        "Server total": ("788 ms", ""),
        "Round trip": ("803 ms", "14.7 ms outside the server"),
        "Slowest step": ("NLP pass 1", "388 ms · 49% of total"),
    }
    assert [expander.label for expander in at.expander] == ["⏱ Timing breakdown"]
    assert at.expander[0].proto.expanded is True

    table = _table(at)
    assert [row[0] for row in table] == [
        "Application settings",
        "Waiting for a detection thread",
        "NLP pass 1 (spaCy + NER model)",
        INDENT + "NER model",
        "Recognizers (regex, validators, scoring, dedup)",
        INDENT + "Context scoring",
        INDENT + "Duplicate removal",
        "Second NER pass (case recovery)",
        INDENT + "NER model",
        "Post-processing",
        "Handling (replace / hash / fake)",
        "Saving the session",
        "Other",
    ]
    by_step = {step: (ms, share) for step, ms, share in table}
    assert by_step["NLP pass 1 (spaCy + NER model)"] == pytest.approx((388.12, 100 * 388.12 / 788.30))  # share in %
    assert by_step["Handling (replace / hash / fake)"][0] == pytest.approx(0.06)
    assert _chart_categories(at) == [  # top-level steps only: the details are inside them
        "App settings", "Queue wait", "NLP pass 1", "Recognizers", "Second NER pass", "Post-processing",
        "Handling", "Save session", "Other",
    ]  # fmt: skip
    slowest = next(metric for metric in at.metric if metric.label == "Slowest step")
    assert slowest.help == "NLP pass 1 (spaCy + NER model)"  # the tile is short; hovering gives the full name

    captions = [caption.value for caption in at.caption]
    assert "From the API's Server-Timing header. Indented rows are part of the step above, not extra time." in captions
    assert f"Trace ID `{TRACE_ID}` · [Open trace](https://grafana.test/explore?trace={TRACE_ID})" in captions


@pytest.mark.parametrize(
    "template, traceresponse, expected",
    [
        ("", TRACEPARENT, f"Trace ID `{TRACE_ID}`"),
        ("https://g.test/{trace_id}", f"00-{TRACE_ID}-00f067aa0ba902b7-00", f"Trace ID `{TRACE_ID}` · not sampled, so no trace was stored"),
        ("g.test/{trace_id}", TRACEPARENT, f"Trace ID `{TRACE_ID}` · TRACE_URL_TEMPLATE must be an http(s) URL containing {{trace_id}}"),
    ],
)  # fmt: skip
def test_trace_caption_follows_the_template_and_the_sampling_flag(api, monkeypatch, template, traceresponse, expected):
    monkeypatch.setenv("TRACE_URL_TEMPLATE", template)

    at = _anonymize({"Server-Timing": ANONYMIZE_TIMING, "traceresponse": traceresponse}, api=api)

    assert [caption.value for caption in at.caption if "Trace ID" in caption.value] == [expected]


def test_no_traceresponse_means_no_trace_caption(api):
    at = _anonymize({"Server-Timing": ANONYMIZE_TIMING}, api=api)

    assert len(at.metric) == 3
    assert not [caption for caption in at.caption if "Trace ID" in caption.value]


def test_without_a_server_timing_header_only_the_round_trip_is_shown(api):
    at = _anonymize({}, api=api)  # an older API, or SERVER_TIMING=false

    assert at.text_area(key="anon_result_sess-1234").value == "Mail {{PERSON_1}} at {{EMAIL_ADDRESS_1}}"
    assert [caption.value for caption in at.caption if "Round trip" in caption.value] == ["⏱ Round trip: 803 ms"]
    assert len(at.metric) == 0 and len(at.expander) == 0 and len(at.dataframe) == 0
    assert not at.get("vega_lite_chart")


@pytest.mark.parametrize(
    "header",
    ['"""\x00;;;,,,dur=9e999', "total;dur=nan, nlp_pass1;dur=-5", "," * 100_000, "x" * 100_000, 'a;desc="never closed'],
)
def test_an_unreadable_server_timing_header_never_breaks_the_page(api, header):
    at = _anonymize({"Server-Timing": header, "traceresponse": "garbage"}, api=api)

    assert at.text_area(key="anon_result_sess-1234").value == "Mail {{PERSON_1}} at {{EMAIL_ADDRESS_1}}"
    assert len(at.metric) == 0 and len(at.expander) == 0
    assert [caption.value for caption in at.caption if "Round trip" in caption.value] == ["⏱ Round trip: 803 ms"]


def test_a_total_alone_still_shows_the_server_total(api):
    at = _anonymize({"Server-Timing": "total;dur=12", "traceresponse": TRACEPARENT}, api=api)

    assert _metrics(at) == {"Server total": ("12.0 ms", ""), "Round trip": ("803 ms", "791 ms outside the server")}
    assert len(at.dataframe) == 0 and not at.get("vega_lite_chart")
    assert [caption.value for caption in at.caption if "Trace ID" in caption.value] == [f"Trace ID `{TRACE_ID}`"]


def test_a_missing_total_is_labelled_as_the_sum_of_the_steps(api):
    at = _anonymize({"Server-Timing": "nlp_pass1;dur=30, nlp_pass1.ner_model;dur=20, handling;dur=10"}, api=api)

    assert _metrics(at)["Sum of steps"] == ("40.0 ms", "")
    assert "Server total" not in _metrics(at)
    assert [round(share) for _, _, share in _table(at)] == [75, 50, 25]  # the detail is not part of the sum


def test_a_failed_call_shows_the_error_and_no_panel(api):
    api.anonymize_status = 500

    at = _anonymize(api=api)

    assert [error.value for error in at.error] == ['Anonymization failed: {"detail": "boom"}']
    assert len(at.metric) == 0 and len(at.expander) == 0


def test_deanonymize_shows_a_lighter_panel_and_it_replaces_the_anonymize_one(api):
    at = _anonymize(api=api)
    assert "Slowest step" in _metrics(at) and at.text_area(key="anon_result_sess-1234")

    at.button(key="deanon_btn").click().run()  # the rerun drops the anonymize result, so its panel must go with it
    assert not at.exception

    assert not [area for area in at.text_area if area.key.startswith("anon_result_")]
    assert at.text_area(key="deanon_result").value == "Mail John Smith at john@example.com"
    assert _metrics(at) == {
        "Server total": ("2.40 ms", ""),
        "Round trip": ("9.00 ms", "6.60 ms outside the server"),
        "Slowest step": ("Load session", "1.20 ms · 50% of total"),
    }
    assert [step for step, _, _ in _table(at)] == [
        "Application settings", "Loading the session", "Restoring the original text", "Other",
    ]  # fmt: skip
    assert _chart_categories(at) == ["App settings", "Load session", "Restore text", "Other"]
    assert "From the API's Server-Timing header." in [caption.value for caption in at.caption]  # no rows to indent
    assert len(at.expander) == 1  # not one per tab
    assert api.calls.count(("POST", "/anonymize_unique")) == 1


def test_the_sign_in_gate_still_runs_before_any_api_call(api, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")  # no OIDC configuration here, so the gate stops the script

    at = AppTest.from_file(SCRIPT, default_timeout=30).run()

    assert not at.exception
    assert any("OIDC configuration is missing" in error.value for error in at.error)
    assert api.calls == []
    assert "anon_btn" not in [button.key for button in at.button] and len(at.metric) == 0
