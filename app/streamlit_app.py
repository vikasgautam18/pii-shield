"""Streamlit UI for PII Shield — Anonymize & De-anonymize.

Run:  streamlit run app/streamlit_app.py --server.port 7860
"""

import json
import os

import requests
import streamlit as st

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

st.set_page_config(page_title="PII Shield Playground", page_icon="", layout="wide")
st.markdown(
    "<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} "
    "header [data-testid='stStatusWidget'] {display: none;} "
    ".stDeployButton {display: none;}</style>",
    unsafe_allow_html=True,
)
st.title(" PII Shield Playground")
st.caption("Detect, anonymize, and restore PII using Microsoft Presidio.")


# ── Helpers ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def fetch_apps() -> list[dict]:
    try:
        resp = requests.get(f"{API_BASE}/apps", timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except requests.ConnectionError:
        return []


def app_choices() -> dict[str, str]:
    return {f"{a['app_name']} ({a['app_id']})": a["app_id"] for a in fetch_apps()}


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_anon, tab_deanon = st.tabs(["[lock] Anonymize", " De-anonymize"])

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
                resp = requests.post(
                    f"{API_BASE}/anonymize_unique",
                    json={"text": anon_input, "language": "en"},
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code == 200:
                    data = resp.json()

                    # Store session for de-anonymize tab
                    st.session_state.session_id = data["id"]
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
                        st.markdown("**Detected PIIs:**")
                        pii_lines = []
                        for placeholder, original in data["entity_mapping"].items():
                            pii_lines.append(f"`{placeholder}` → {original}")
                        for _hash, original in data.get("hash_mapping", {}).items():
                            pii_lines.append(f"*(hashed)* → {original}")
                        for _token, original in data.get("encrypt_mapping", {}).items():
                            pii_lines.append(f"*(encrypted)* → {original}")
                        st.markdown("\n\n".join(pii_lines) if pii_lines else "No PII detected.")

                    with col2:
                        st.markdown("**Anonymized Text:**")
                        st.text_area(
                            "Result",
                            value=data["anonymized_text"],
                            height=200,
                            disabled=True,
                            key=f"anon_result_{data['id']}",
                            label_visibility="collapsed",
                        )

                    st.code(data["id"], language=None)
                else:
                    st.error(f"Anonymization failed: {resp.text}")
            except requests.ConnectionError:
                st.error("[WARN] Cannot reach the PII Shield API server.")


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

    # Pre-fill from anonymize tab if available
    default_text = st.session_state.get("anonymized_text", "")
    default_session = st.session_state.get("session_id", "")

    deanon_input = st.text_area(
        "Anonymized Text",
        value=default_text,
        placeholder="Paste anonymized text (with {{ENTITY_N}} placeholders)…",
        height=150,
        key="deanon_input",
    )

    session_id = st.text_input(
        "Session ID",
        value=default_session,
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
                resp = requests.post(
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
                        st.markdown("**De-anonymized Text:**")
                        st.text_area(
                            "Result",
                            value=result["text"],
                            height=200,
                            disabled=True,
                            key="deanon_result",
                            label_visibility="collapsed",
                        )
                else:
                    st.error(f"De-anonymization failed: {resp.text}")
            except requests.ConnectionError:
                st.error("[WARN] Cannot reach the PII Shield API server.")
