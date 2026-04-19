"""Streamlit UI for PII Shield — Admin & Application Registry.

Run: streamlit run app/streamlit_admin.py --server.port 7861
"""

import os

import pandas as pd
import requests
import streamlit as st

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

st.set_page_config(page_title="PII Admin", page_icon="", layout="wide")
st.markdown(
    "<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} "
    "header [data-testid='stStatusWidget'] {display: none;} "
    ".stDeployButton {display: none;}</style>",
    unsafe_allow_html=True,
)
st.title(" PII Admin")
st.caption("Application registration, configuration, and management.")


# ── Helpers ──────────────────────────────────────────────────────────────────

@st.cache_data(ttl=5)
def fetch_apps() -> list[dict]:
    try:
        resp = requests.get(f"{API_BASE}/apps", timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except requests.ConnectionError:
        return []


@st.cache_data(ttl=5)
def fetch_entity_types() -> list[str]:
    try:
        resp = requests.get(f"{API_BASE}/supported-entities", timeout=10)
        return resp.json() if resp.status_code == 200 else []
    except requests.ConnectionError:
        return []


def app_choices() -> dict[str, str]:
    """Return {display_name: app_id} mapping."""
    return {f"{a['app_name']} ({a['app_id']})": a["app_id"] for a in fetch_apps()}


def clear_cache():
    fetch_apps.clear()
    fetch_entity_types.clear()


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_apps, tab_admin, tab_allow, tab_audit = st.tabs(
    [" Registered Apps", " Admin", " Allow-Lists", " Audit Log"]
)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1: Registered Apps
# ═══════════════════════════════════════════════════════════════════════════════

with tab_apps:
    st.subheader("All Registered Applications")
    if st.button(" Refresh", key="refresh_apps"):
        clear_cache()

    apps = fetch_apps()
    if not apps:
        st.info("No applications registered yet.")
    else:
        rows = []
        for a in apps:
            app_id = a["app_id"]
            config_str = ", ".join(f"{k}={v}" for k, v in a["config"].items()) or "(defaults)"
            try:
                et = requests.get(f"{API_BASE}/apps/{app_id}/entity-type-allow-list", timeout=5).json().get("entity_type_allow_list", [])
                ekw = requests.get(f"{API_BASE}/apps/{app_id}/entity-keyword-allow-list", timeout=5).json().get("entity_keyword_allow_list", {})
            except Exception:
                et, ekw = [], {}
            et_str = ", ".join(et) if et else "(none)"
            ekw_parts = [f"{etype}: {', '.join(kws)}" for etype, kws in ekw.items() if kws]
            ekw_str = "; ".join(ekw_parts) if ekw_parts else "(none)"
            rows.append({
                "App Name": a["app_name"],
                "App ID": app_id,
                "Config": config_str,
                "Entity-Type Allow-List": et_str,
                "Entity-Keyword Allow-List": ekw_str,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2: Admin
# ═══════════════════════════════════════════════════════════════════════════════

with tab_admin:
    st.subheader("Application Registration & Configuration")

    # ── Register ─────────────────────────────────────────────────────────
    with st.expander("+ Register New Application", expanded=True):
        reg_name = st.text_input("Application Name", placeholder="e.g. MyLLMApp")
        if st.button("Register", key="register_btn", type="primary"):
            if not reg_name.strip():
                st.warning("Please enter an application name.")
            else:
                resp = requests.post(f"{API_BASE}/apps", json={"app_name": reg_name.strip()})
                if resp.status_code == 201:
                    data = resp.json()
                    clear_cache()
                    st.success(f"[PASS] Registered! App ID: `{data['app_id']}`")
                    st.json(data)
                else:
                    st.error(f"Error: {resp.text}")

    # ── Lookup ───────────────────────────────────────────────────────────
    with st.expander(" Lookup Application"):
        choices = app_choices()
        if choices:
            selected = st.selectbox("Select Application", options=["Pick your application..."] + list(choices.keys()), index=0, key="lookup_sel")
            lookup_id = choices.get(selected, "")
            if st.button("Lookup", key="lookup_btn"):
                if not lookup_id:
                    st.warning("Please select an application.")
                else:
                    resp = requests.get(f"{API_BASE}/apps/{lookup_id}")
                if resp.status_code == 200:
                    st.json(resp.json())
                else:
                    st.error(f"Not found: {lookup_id}")
        else:
            st.info("No applications registered.")

    # ── Update Config ────────────────────────────────────────────────────
    with st.expander(" Update Entity Strategy"):
        choices = app_choices()
        if choices:
            cfg_sel = st.selectbox("Select Application", options=["Pick your application..."] + list(choices.keys()), index=0, key="cfg_sel")
            cfg_app_id = choices.get(cfg_sel, "")
            entity_types = fetch_entity_types()
            col1, col2 = st.columns(2)
            with col1:
                cfg_entity = st.selectbox("Entity Type", options=entity_types, key="cfg_entity")
            with col2:
                cfg_strategy = st.selectbox("Strategy", options=["replace", "hash", "encrypt", "fake"], key="cfg_strategy")
            if st.button("Update Config", key="cfg_btn", type="primary"):
                if not cfg_app_id:
                    st.warning("Please select an application.")
                else:
                    resp = requests.put(
                        f"{API_BASE}/apps/{cfg_app_id}/config",
                        json={"entity_type": cfg_entity, "strategy": cfg_strategy},
                    )
                if resp.status_code == 200:
                    clear_cache()
                    st.success("[PASS] Config updated!")
                    st.json(resp.json())
                else:
                    st.error(f"Error: {resp.text}")
        else:
            st.info("No applications registered.")

    # ── Delete ───────────────────────────────────────────────────────────
    with st.expander(" Delete Application"):
        choices = app_choices()
        if choices:
            del_sel = st.selectbox("Select Application", options=["Pick your application..."] + list(choices.keys()), index=0, key="del_sel")
            del_app_id = choices.get(del_sel, "")
            if del_app_id:
                st.warning(f"[WARN] This will permanently delete app `{del_app_id}`")
            if st.button("Delete", key="del_btn"):
                if not del_app_id:
                    st.warning("Please select an application.")
                else:
                    resp = requests.delete(f"{API_BASE}/apps/{del_app_id}")
                if resp.status_code == 204:
                    clear_cache()
                    st.success(f"[PASS] Deleted: {del_app_id}")
                else:
                    st.error(f"Error: {resp.text}")
        else:
            st.info("No applications registered.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3: Allow-Lists
# ═══════════════════════════════════════════════════════════════════════════════

with tab_allow:
    st.subheader("Per-App Allow-Lists")
    st.caption("Manage entity-type and entity-keyword exclusions. Items on these lists will be skipped during anonymization.")

    choices = app_choices()
    if not choices:
        st.info("No applications registered.")
    else:
        al_sel = st.selectbox("Select Application", options=["Pick your application..."] + list(choices.keys()), index=0, key="al_sel")
        al_app_id = choices.get(al_sel, "")

        if not al_app_id:
            st.info(" Pick an application above to manage its allow-lists.")
        else:
            # ── Entity-Type Allow-List ───────────────────────────────────
            with st.expander(" Entity-Type Allow-List", expanded=True):
                st.caption("Select entity types to **exclude** from anonymization for this app.")
                entity_types = fetch_entity_types()

                try:
                    current_et = requests.get(f"{API_BASE}/apps/{al_app_id}/entity-type-allow-list", timeout=5).json().get("entity_type_allow_list", [])
                except Exception:
                    current_et = []

                selected_et = st.multiselect(
                    "Entity Types to Exclude",
                    options=entity_types,
                    default=[t for t in current_et if t in entity_types],
                    key="et_multiselect",
                )

                if st.button("Save Entity-Type Allow-List", key="et_save", type="primary"):
                    resp = requests.put(
                        f"{API_BASE}/apps/{al_app_id}/entity-type-allow-list",
                        json={"entity_type_allow_list": selected_et},
                    )
                    if resp.status_code == 200:
                        clear_cache()
                        st.success(f"[PASS] Saved {len(selected_et)} entity type(s).")
                    else:
                        st.error(f"Error: {resp.text}")

            # ── Entity-Keyword Allow-List ────────────────────────────────
            with st.expander("[key] Entity-Keyword Allow-List", expanded=True):
                st.caption(
                    "Exclude **specific text** only when detected as a **specific entity type**. "
                    "For example, allow 'Contoso Bank' as ORGANIZATION but still anonymize it if detected as something else."
                )

                try:
                    ekw_data = requests.get(f"{API_BASE}/apps/{al_app_id}/entity-keyword-allow-list", timeout=5).json().get("entity_keyword_allow_list", {})
                except Exception:
                    ekw_data = {}

                ekw_rows = []
                for etype in sorted(ekw_data.keys()):
                    for kw in ekw_data[etype]:
                        ekw_rows.append({"Entity Type": etype, "Keyword": kw})

                if "ekw_df" not in st.session_state or st.session_state.get("ekw_app_id") != al_app_id:
                    st.session_state.ekw_df = pd.DataFrame(ekw_rows) if ekw_rows else pd.DataFrame(columns=["Entity Type", "Keyword"])
                    st.session_state.ekw_app_id = al_app_id

                st.markdown("**Current pairs** *(edit or delete rows directly):*")
                entity_types = fetch_entity_types()
                edited_df = st.data_editor(
                    st.session_state.ekw_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="ekw_editor",
                    column_config={
                        "Entity Type": st.column_config.SelectboxColumn(
                            "Entity Type",
                            options=entity_types,
                            required=True,
                        ),
                        "Keyword": st.column_config.TextColumn(
                            "Keyword",
                            required=True,
                        ),
                    },
                )

                if st.button("Save Entity-Keyword Allow-List", key="ekw_save", type="primary"):
                    ekw_to_save: dict[str, list[str]] = {}
                    if edited_df is not None and len(edited_df) > 0:
                        for _, row in edited_df.iterrows():
                            et = str(row["Entity Type"]).strip() if pd.notna(row["Entity Type"]) else ""
                            kw = str(row["Keyword"]).strip() if pd.notna(row["Keyword"]) else ""
                            if et and kw:
                                ekw_to_save.setdefault(et, []).append(kw)
                    resp = requests.put(
                        f"{API_BASE}/apps/{al_app_id}/entity-keyword-allow-list",
                        json={"entity_keyword_allow_list": ekw_to_save},
                    )
                    if resp.status_code == 200:
                        saved = resp.json().get("entity_keyword_allow_list", {})
                        total = sum(len(v) for v in saved.values())
                        clear_cache()
                        st.session_state.ekw_df = edited_df
                        st.success(f"[PASS] Saved {total} entity-keyword pair(s) across {len(saved)} type(s).")
                    else:
                        st.error(f"Error: {resp.text}")

                if st.button(" Reload from server", key="ekw_reload"):
                    st.session_state.pop("ekw_df", None)
                    st.session_state.pop("ekw_app_id", None)
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4: Audit Log
# ═══════════════════════════════════════════════════════════════════════════════

with tab_audit:
    st.subheader("Application Audit Log")
    st.caption("Track when applications are registered, modified, and deleted.")

    AUDIT_ACTIONS = [
        "(all)",
        "APP_REGISTERED",
        "APP_DELETED",
        "CONFIG_UPDATED",
        "ALLOW_LIST_UPDATED",
        "ENTITY_TYPE_ALLOW_LIST_UPDATED",
        "ENTITY_KEYWORD_ALLOW_LIST_UPDATED",
    ]

    col1, col2 = st.columns(2)
    with col1:
        choices_with_all = {"(all apps)": ""} | app_choices()
        audit_app_sel = st.selectbox("Filter by App", options=["Pick your application..."] + list(choices_with_all.keys()), index=0, key="audit_app")
        audit_app_id = choices_with_all.get(audit_app_sel, "")
    with col2:
        audit_action = st.selectbox("Filter by Action", options=AUDIT_ACTIONS, key="audit_action")

    if st.button(" Refresh Audit Log", key="audit_refresh"):
        pass # Just triggers re-run

    params: dict = {"limit": 200}
    if audit_app_id:
        params["app_id"] = audit_app_id
    if audit_action and audit_action != "(all)":
        params["action"] = audit_action

    try:
        resp = requests.get(f"{API_BASE}/audit-log", params=params, timeout=10)
        if resp.status_code == 200:
            entries = resp.json()
            if entries:
                df = pd.DataFrame(entries)
                if "details" in df.columns:
                    df["details"] = df["details"].astype(str)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("No audit entries found.")
        else:
            st.error(f"Error: {resp.text}")
    except requests.ConnectionError:
        st.error("[WARN] Cannot reach the API server.")


if __name__ == "__main__":
    pass # Run via: streamlit run app/streamlit_admin.py --server.port 7861
