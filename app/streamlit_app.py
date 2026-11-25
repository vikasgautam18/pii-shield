"""Streamlit UI for PII Shield — Anonymize & De-anonymize.

Run:  streamlit run app/streamlit_app.py --server.port 7860
"""

import json
import os

import requests
import streamlit as st

from themes import apply_theme
from timing_view import (
    build_breakdown,
    format_ms,
    format_share,
    parse_server_timing,
    parse_traceresponse,
    trace_caption,
)
from ui_auth import ApiClient, require_login

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
# Optional link to the request's trace, e.g. a Grafana Explore URL; "{trace_id}" is replaced by the 32-hex trace id.
TRACE_URL_TEMPLATE = os.getenv("TRACE_URL_TEMPLATE", "")

st.set_page_config(page_title="PII Shield Playground", page_icon="🛡️", layout="wide")
apply_theme()
# Sign-in gate: a no-op unless AUTH_ENABLED=true. It runs before any API call and
# stops the script (showing a sign-in / access-denied screen) when access is not granted.
auth_session = require_login(required="user", api_base=API_BASE)
api = ApiClient(auth_session)
st.title("🛡️ PII Shield Playground")
st.caption("Detect, anonymize, and restore PII using Microsoft Presidio.")


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FetchError(Exception):
    """An API call failed; raised so st.cache_data never caches a failure (e.g. a 401)."""


@st.cache_data(ttl=10)
def _fetch_apps_cached() -> list[dict]:
    try:
        resp = api.get(f"{API_BASE}/apps", timeout=10)
    except requests.ConnectionError as exc:
        raise _FetchError from exc
    if resp.status_code != 200:
        raise _FetchError(resp.status_code)
    return resp.json()


def fetch_apps() -> list[dict]:
    try:
        return _fetch_apps_cached()
    except _FetchError:
        return []


def app_choices() -> dict[str, str]:
    return {f"{a['app_name']} ({a['app_id']})": a["app_id"] for a in fetch_apps()}


def render_timing(resp: requests.Response) -> None:
    """Show where the API spent the time of ``resp``, read from its Server-Timing and traceresponse headers.

    Call it right after the result is drawn and inside the same button branch: that result does not survive the next
    rerun, so a panel replayed from session state would outlive it and sit next to a newer result.
    """
    round_trip_ms = resp.elapsed.total_seconds() * 1000
    breakdown = build_breakdown(parse_server_timing(resp.headers.get("Server-Timing")))
    if breakdown is None:  # an older API, or SERVER_TIMING=false: all that is known is the round trip
        st.caption(f"⏱ Round trip: {format_ms(round_trip_ms)}")
        return

    total_col, trip_col, slowest_col = st.columns(3)
    total_col.metric(
        "Server total" if breakdown.total_reported else "Sum of steps",
        format_ms(breakdown.total_ms),
        help="Time inside the API, from the start of the request until the response headers were built.",
    )
    outside_ms = round_trip_ms - breakdown.total_ms
    trip_col.metric(
        "Round trip",
        format_ms(round_trip_ms),
        delta=f"{format_ms(outside_ms)} outside the server" if outside_ms >= 0 else None,
        delta_color="off",
        delta_arrow="off",
        help="Measured here: from sending the request until the response headers arrived.",
    )
    if breakdown.slowest is not None:
        slowest_col.metric(
            "Slowest step",
            breakdown.slowest.short_label,
            delta=f"{format_ms(breakdown.slowest.ms)} · {format_share(breakdown.slowest.share)} of total",
            delta_color="off",
            delta_arrow="off",
            help=breakdown.slowest.label,
        )

    with st.expander("⏱ Timing breakdown", expanded=True):
        if breakdown.rows:
            steps = breakdown.steps
            st.bar_chart(  # top-level steps only: details are inclusive, so charting them would double count
                {"Step": [row.short_label for row in steps], "ms": [row.ms for row in steps]},
                x="Step",
                y="ms",
                y_label="",  # on a horizontal chart this is the step axis, which needs no title
                color="primary",
                horizontal=True,
                sort=False,
                height=60 + 28 * len(steps),
            )
            st.dataframe(
                {
                    "Step": [("\u2003↳ " if row.is_detail else "") + row.label for row in breakdown.rows],
                    "ms": [row.ms for row in breakdown.rows],
                    "Share": [row.share * 100 for row in breakdown.rows],
                },
                hide_index=True,
                height="content",  # row height follows the theme font, so a computed pixel height would clip rows
                column_config={
                    "ms": st.column_config.NumberColumn("ms", format="%.2f"),
                    # "percent" would print a varying number of decimals, so scale to 0-100 and fix one
                    "Share": st.column_config.ProgressColumn(
                        "Share of total", format="%.1f%%", min_value=0, max_value=100
                    ),
                },
            )
            note = "From the API's Server-Timing header."
            if any(row.is_detail for row in breakdown.rows):
                note += " Indented rows are part of the step above, not extra time."
            st.caption(note)
        trace_line = trace_caption(parse_traceresponse(resp.headers.get("traceresponse")), TRACE_URL_TEMPLATE)
        if trace_line:
            st.caption(trace_line)


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_anon, tab_deanon = st.tabs(["🔒 Anonymize", "🔓 De-anonymize"])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: Anonymize
# ═══════════════════════════════════════════════════════════════════════════════

with tab_anon:
    st.subheader("Anonymize Text")

    choices = app_choices()
    anon_app_sel = st.selectbox(
        "Application (optional)",
        options=["(no application)"] + list(choices.keys()),
        index=0,
        key="anon_app",
    )
    anon_app_id = choices.get(anon_app_sel, "")

    anon_input = st.text_area(
        "Input Text",
        placeholder="Enter text containing PII…",
        height=150,
        key="anon_input",
    )

    if st.button("Anonymize", key="anon_btn", type="primary"):
        if not anon_input.strip():
            st.warning("Please enter some text.")
        else:
            headers = {}
            if anon_app_id:
                headers["X-App-Id"] = anon_app_id
            try:
                resp = api.post(
                    f"{API_BASE}/anonymize_unique",
                    json={"text": anon_input, "language": "en"},
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()

                    # Store session for de-anonymize tab (write directly into widget keys
                    # so subsequent reruns show the new value even after the widget was
                    # already instantiated in a prior render).
                    st.session_state.session_id = data["id"]
                    st.session_state["deanon_session"] = data["id"]
                    st.session_state["deanon_input"] = data["anonymized_text"]
                    st.session_state["deanon_app"] = anon_app_sel
                    all_mappings = {
                        **data["entity_mapping"],
                        **data.get("hash_mapping", {}),
                        **data.get("encrypt_mapping", {}),
                    }
                    st.session_state.entity_mapping = all_mappings
                    st.session_state.anonymized_text = data["anonymized_text"]
                    st.session_state.anon_run_id = data["id"]  # unique per run

                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("<p class='pii-section-label'>Detected PIIs:</p>", unsafe_allow_html=True)
                        pii_lines = []
                        for placeholder, original in data["entity_mapping"].items():
                            pii_lines.append(f"`{placeholder}` → {original}")
                        for _hash, original in data.get("hash_mapping", {}).items():
                            pii_lines.append(f"*(hashed)* → {original}")
                        for _token, original in data.get("encrypt_mapping", {}).items():
                            pii_lines.append(f"*(encrypted)* → {original}")
                        st.markdown("\n\n".join(pii_lines) if pii_lines else "No PII detected.")

                    with col2:
                        st.markdown("<p class='pii-section-label'>Anonymized Text:</p>", unsafe_allow_html=True)
                        st.text_area(
                            "Result",
                            value=data["anonymized_text"],
                            height=200,
                            disabled=True,
                            key=f"anon_result_{data['id']}",
                            label_visibility="collapsed",
                        )

                    st.markdown("<p class='pii-section-label'>Session ID:</p>", unsafe_allow_html=True)
                    st.code(data["id"], language=None)
                    render_timing(resp)
                else:
                    st.error(f"Anonymization failed: {resp.text}")
            except requests.ConnectionError:
                st.error("⚠️ Cannot reach the PII Shield API server.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: De-anonymize
# ═══════════════════════════════════════════════════════════════════════════════

with tab_deanon:
    st.subheader("De-anonymize Text")

    choices = app_choices()
    deanon_app_sel = st.selectbox(
        "Application (optional)",
        options=["(no application)"] + list(choices.keys()),
        index=0,
        key="deanon_app",
    )
    deanon_app_id = choices.get(deanon_app_sel, "")

    # Pre-fill from anonymize tab if available — the anonymize handler writes
    # directly into the widget keys ("deanon_input", "deanon_session"), so the
    # widgets below pick those up on rerun without needing a `value=` default.

    deanon_input = st.text_area(
        "Anonymized Text",
        placeholder="Paste anonymized text (with {{ENTITY_N}} placeholders)…",
        height=150,
        key="deanon_input",
    )

    session_id = st.text_input(
        "Session ID",
        key="deanon_session",
    )

    col1, col2 = st.columns(2)
    with col1:
        include_hashed = st.checkbox("Restore hashed entities", value=True, key="inc_hash")
    with col2:
        include_encrypted = st.checkbox("Restore encrypted entities", value=True, key="inc_enc")

    if st.button("De-anonymize", key="deanon_btn", type="primary"):
        if not deanon_input.strip() or not session_id.strip():
            st.warning("Please enter anonymized text and a session ID.")
        else:
            headers = {}
            if deanon_app_id:
                headers["X-App-Id"] = deanon_app_id
            try:
                resp = api.post(
                    f"{API_BASE}/deanonymize",
                    json={
                        "id": session_id.strip(),
                        "text": deanon_input,
                        "include_hashed": include_hashed,
                        "include_encrypted": include_encrypted,
                    },
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    result = resp.json()

                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("**PII ↔ Placeholder Mapping:**")
                        mapping = st.session_state.get("entity_mapping", {})
                        if mapping:
                            for placeholder, original in mapping.items():
                                st.markdown(f"`{placeholder}` → {original}")
                        else:
                            st.caption("(no mapping available)")

                    with col2:
                        st.markdown("<p class='pii-section-label'>De-anonymized Text:</p>", unsafe_allow_html=True)
                        st.text_area(
                            "Result",
                            value=result["text"],
                            height=200,
                            disabled=True,
                            key="deanon_result",
                            label_visibility="collapsed",
                        )
                    render_timing(resp)
                else:
                    st.error(f"De-anonymization failed: {resp.text}")
            except requests.ConnectionError:
                st.error("⚠️ Cannot reach the PII Shield API server.")
