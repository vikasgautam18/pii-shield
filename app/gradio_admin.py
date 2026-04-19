"""Gradio UI for PII Shield — Admin & Application Registry tabs.

Run: python app/gradio_admin.py → http://localhost:7861
Main UI is in gradio_app.py → http://localhost:7860
"""

import os

import gradio as gr
import requests

API_BASE = os.getenv("API_BASE", "http://localhost:8000")

BLUE_BUTTON_CSS = """
.primary-btn {
    background: #2563eb !important;
    border-color: #2563eb !important;
    color: white !important;
}
.primary-btn:hover {
    background: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_config(config: dict) -> str:
    if not config:
        return "(default — no overrides)"
    return "\n".join(f" {etype} → {strategy}" for etype, strategy in config.items())


def _api_error_detail(resp: requests.Response) -> str:
    """Extract a human-readable error message from an API error response."""
    try:
        return resp.json().get("detail", resp.text)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _fetch_app_choices() -> list[str]:
    """Return a list of 'AppName (app_id)' strings for dropdown pickers."""
    try:
        resp = requests.get(f"{API_BASE}/apps")
        if resp.status_code == 200:
            return [f"{a['app_name']} ({a['app_id']})" for a in resp.json()]
    except requests.ConnectionError:
        pass
    return []


def _fetch_entity_types() -> list[str]:
    """Return the list of supported PII entity types from the API."""
    try:
        resp = requests.get(f"{API_BASE}/supported-entities")
        if resp.status_code == 200:
            return resp.json()
    except requests.ConnectionError:
        pass
    return []


def _extract_app_id(selection: str) -> str:
    """Extract the app_id from a dropdown choice like 'MyApp (uuid)'."""
    if not selection:
        return ""
    start = selection.rfind("(")
    end = selection.rfind(")")
    if start != -1 and end != -1:
        return selection[start + 1 : end]
    return selection


# ── Registered Apps ──────────────────────────────────────────────────────────

def list_registered_apps() -> list[list[str]]:
    """Fetch all registered apps and return rows for a Dataframe."""
    try:
        resp = requests.get(f"{API_BASE}/apps")
    except requests.ConnectionError:
        return [["[WARN] Cannot reach the API server", "", "", "", ""]]
    if resp.status_code != 200:
        return [[f"[FAIL] Error: {_api_error_detail(resp)}", "", "", "", ""]]
    apps = resp.json()
    if not apps:
        return [["No applications registered yet.", "", "", "", ""]]
    rows = []
    for app_info in apps:
        app_id = app_info["app_id"]
        config_str = ", ".join(
            f"{k}={v}" for k, v in app_info["config"].items()
        ) or "(defaults)"
        # Fetch allow-lists for display
        try:
            et_resp = requests.get(f"{API_BASE}/apps/{app_id}/entity-type-allow-list")
            et_list = et_resp.json().get("entity_type_allow_list", []) if et_resp.status_code == 200 else []
            ekw_resp = requests.get(f"{API_BASE}/apps/{app_id}/entity-keyword-allow-list")
            ekw_dict = ekw_resp.json().get("entity_keyword_allow_list", {}) if ekw_resp.status_code == 200 else {}
        except Exception:
            et_list, ekw_dict = [], {}
        et_str = ", ".join(et_list) if et_list else "(none)"
        ekw_parts = [f"{etype}: {', '.join(kws)}" for etype, kws in ekw_dict.items() if kws]
        ekw_str = "; ".join(ekw_parts) if ekw_parts else "(none)"
        rows.append([app_info["app_name"], app_id, config_str, et_str, ekw_str])
    return rows


# ── Admin functions ──────────────────────────────────────────────────────────

def register_app(app_name: str) -> tuple[str, str]:
    """Register an app and return (status_message, app_id)."""
    if not app_name.strip():
        return "[WARN] Please enter an application name.", ""
    resp = requests.post(f"{API_BASE}/apps", json={"app_name": app_name.strip()})
    if resp.status_code == 201:
        data = resp.json()
        msg = (
            f"[PASS] Registered!\n\n"
            f"App ID: {data['app_id']}\n"
            f"App Name: {data['app_name']}\n\n"
            f"Config:\n{_fmt_config(data['config'])}"
        )
        return msg, data["app_id"]
    return f"[FAIL] Error: {_api_error_detail(resp)}", ""


def lookup_app(app_id: str) -> str:
    """Fetch and display app details."""
    if not app_id.strip():
        return "[WARN] Please enter an App ID."
    resp = requests.get(f"{API_BASE}/apps/{app_id.strip()}")
    if resp.status_code == 200:
        data = resp.json()
        return (
            f"App ID: {data['app_id']}\n"
            f"App Name: {data['app_name']}\n\n"
            f"Config:\n{_fmt_config(data['config'])}"
        )
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


def update_app_config(app_id: str, entity_type: str, strategy: str) -> str:
    """Update a single entity strategy for an app."""
    if not app_id.strip() or not entity_type.strip():
        return "[WARN] App ID and Entity Type are required."
    resp = requests.put(
        f"{API_BASE}/apps/{app_id.strip()}/config",
        json={"entity_type": entity_type.strip(), "strategy": strategy},
    )
    if resp.status_code == 200:
        data = resp.json()
        return (
            f"[PASS] Updated!\n\n"
            f"App ID: {data['app_id']}\n"
            f"App Name: {data['app_name']}\n\n"
            f"Config:\n{_fmt_config(data['config'])}"
        )
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


def delete_app(app_id: str) -> str:
    """Deregister an app."""
    if not app_id.strip():
        return "[WARN] Please enter an App ID."
    resp = requests.delete(f"{API_BASE}/apps/{app_id.strip()}")
    if resp.status_code == 204:
        return f"[PASS] Application '{app_id.strip()}' deleted."
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


# ── Allow-list functions ─────────────────────────────────────────────────────

def load_keyword_allow_list(app_id: str) -> str:
    """Fetch the keyword allow-list for an app and return as newline-separated text."""
    if not app_id.strip():
        return ""
    resp = requests.get(f"{API_BASE}/apps/{app_id.strip()}/allow-list")
    if resp.status_code == 200:
        items = resp.json().get("allow_list", [])
        return "\n".join(items)
    return ""


def save_keyword_allow_list(app_id: str, items_text: str) -> str:
    """Save keyword allow-list from newline-separated text."""
    if not app_id.strip():
        return "[WARN] Please select an application."
    items = [line.strip() for line in items_text.strip().splitlines() if line.strip()]
    resp = requests.put(
        f"{API_BASE}/apps/{app_id.strip()}/allow-list",
        json={"allow_list": items},
    )
    if resp.status_code == 200:
        saved = resp.json().get("allow_list", [])
        return f"[PASS] Saved {len(saved)} keyword(s)."
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


def load_entity_type_allow_list(app_id: str) -> list[str]:
    """Fetch the entity-type allow-list for an app."""
    if not app_id.strip():
        return []
    resp = requests.get(f"{API_BASE}/apps/{app_id.strip()}/entity-type-allow-list")
    if resp.status_code == 200:
        return resp.json().get("entity_type_allow_list", [])
    return []


def save_entity_type_allow_list(app_id: str, selected_types: list[str]) -> str:
    """Save entity-type allow-list."""
    if not app_id.strip():
        return "[WARN] Please select an application."
    resp = requests.put(
        f"{API_BASE}/apps/{app_id.strip()}/entity-type-allow-list",
        json={"entity_type_allow_list": selected_types or []},
    )
    if resp.status_code == 200:
        saved = resp.json().get("entity_type_allow_list", [])
        return f"[PASS] Saved {len(saved)} entity type(s)."
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


def load_entity_keyword_allow_list_rows(app_id: str) -> list[list[str]]:
    """Fetch entity-keyword allow-list and return as Dataframe rows with delete column."""
    if not app_id.strip():
        return []
    resp = requests.get(f"{API_BASE}/apps/{app_id.strip()}/entity-keyword-allow-list")
    if resp.status_code == 200:
        ekw = resp.json().get("entity_keyword_allow_list", {})
        rows = []
        for entity_type in sorted(ekw.keys()):
            for keyword in ekw[entity_type]:
                rows.append([entity_type, keyword, ""])
        return rows
    return []


def save_entity_keyword_allow_list_rows(app_id: str, rows_df) -> str:
    """Save entity-keyword allow-list from Dataframe rows. Rows with empty entity/keyword or marked are skipped."""
    if not app_id.strip():
        return "[WARN] Please select an application."
    ekw: dict[str, list[str]] = {}
    if rows_df is not None and len(rows_df) > 0:
        for _, row in rows_df.iterrows():
            entity_type = str(row.iloc[0]).strip()
            keyword = str(row.iloc[1]).strip()
            delete_flag = str(row.iloc[2]).strip().lower() if len(row) > 2 else ""
            # Skip rows marked for deletion or with empty values
            if not entity_type or not keyword or delete_flag in ("x", "delete", "yes", "", "d", "1"):
                continue
            ekw.setdefault(entity_type, []).append(keyword)
    resp = requests.put(
        f"{API_BASE}/apps/{app_id.strip()}/entity-keyword-allow-list",
        json={"entity_keyword_allow_list": ekw},
    )
    if resp.status_code == 200:
        saved = resp.json().get("entity_keyword_allow_list", {})
        total = sum(len(v) for v in saved.values())
        return f"[PASS] Saved {total} entity-keyword pair(s) across {len(saved)} type(s)."
    if resp.status_code == 404:
        return f"[WARN] Application '{app_id.strip()}' not found."
    return f"[FAIL] Error: {_api_error_detail(resp)}"


def add_entity_keyword_pair(current_df, entity_type: str, keyword: str) -> list[list[str]]:
    """Add a new entity-keyword pair to the Dataframe."""
    rows = []
    if current_df is not None and len(current_df) > 0:
        for _, row in current_df.iterrows():
            rows.append([str(row.iloc[0]), str(row.iloc[1]), ""])
    et = (entity_type or "").strip()
    kw = (keyword or "").strip()
    if et and kw:
        rows.append([et, kw, ""])
    return rows


# ── Audit log functions ──────────────────────────────────────────────────────

AUDIT_ACTIONS = [
    "(all)",
    "APP_REGISTERED",
    "APP_DELETED",
    "CONFIG_UPDATED",
    "ALLOW_LIST_UPDATED",
    "ENTITY_TYPE_ALLOW_LIST_UPDATED",
]


def fetch_audit_log(app_filter: str, action_filter: str) -> list[list[str]]:
    """Fetch the audit log from the API and return rows for a Dataframe."""
    params: dict = {"limit": 200}
    app_id = _extract_app_id(app_filter) if app_filter else ""
    if app_id:
        params["app_id"] = app_id
    if action_filter and action_filter != "(all)":
        params["action"] = action_filter
    try:
        resp = requests.get(f"{API_BASE}/audit-log", params=params)
    except requests.ConnectionError:
        return [["[WARN] Cannot reach the API server", "", "", "", ""]]
    if resp.status_code != 200:
        return [[f"[FAIL] Error: {_api_error_detail(resp)}", "", "", "", ""]]
    entries = resp.json()
    if not entries:
        return [["No audit entries found.", "", "", "", ""]]
    rows = []
    for e in entries:
        rows.append([e["timestamp"], e["action"], e["app_name"], e["app_id"], e["details"]])
    return rows


# ── Build UI ─────────────────────────────────────────────────────────────────

with gr.Blocks(title="PII Shield — Admin", css=BLUE_BUTTON_CSS) as admin_demo:
    gr.Markdown("# PII Shield — Admin")
    gr.Markdown("Application registration, configuration, and management.")

    with gr.Tab("Registered Apps"):
        gr.Markdown("### All Registered Applications")
        apps_output = gr.Dataframe(
            headers=["App Name", "App ID", "Config", "Entity-Type Allow-List", "Entity-Keyword Allow-List"],
            datatype=["str", "str", "str", "str", "str"],
            label="Applications",
            interactive=False,
            wrap=True,
            max_height=400,
        )
        refresh_btn = gr.Button("Refresh", variant="primary", elem_classes=["primary-btn"])

        refresh_btn.click(fn=list_registered_apps, inputs=[], outputs=[apps_output])
        admin_demo.load(fn=list_registered_apps, inputs=[], outputs=[apps_output])

    with gr.Tab("Admin"):
        gr.Markdown("### Application Registration & Config")

        with gr.Accordion("Register New Application", open=True):
            reg_name = gr.Textbox(label="Application Name", placeholder="e.g. MyLLMApp")
            reg_btn = gr.Button("Register", variant="primary", elem_classes=["primary-btn"])
            reg_output = gr.Textbox(label="Result", lines=6, interactive=False)
            reg_app_id = gr.State(value="")

            reg_btn.click(
                fn=register_app,
                inputs=[reg_name],
                outputs=[reg_output, reg_app_id],
            )

        with gr.Accordion("Lookup Application", open=False):
            lookup_picker = gr.Dropdown(
                label="Select Application",
                choices=[],
                interactive=True,
                allow_custom_value=True,
            )
            lookup_refresh_btn = gr.Button("↻ Refresh App List", size="sm")
            lookup_id = gr.Textbox(label="App ID", placeholder="Auto-filled from picker, or paste UUID…")
            lookup_btn = gr.Button("Lookup", variant="primary", elem_classes=["primary-btn"])
            lookup_output = gr.Textbox(label="App Details", lines=6, interactive=False)

            lookup_refresh_btn.click(
                fn=lambda: gr.update(choices=_fetch_app_choices()),
                inputs=[],
                outputs=[lookup_picker],
            )
            lookup_picker.change(
                fn=_extract_app_id,
                inputs=[lookup_picker],
                outputs=[lookup_id],
            )
            lookup_btn.click(
                fn=lookup_app,
                inputs=[lookup_id],
                outputs=[lookup_output],
            )

        with gr.Accordion("Update App Config", open=False):
            cfg_picker = gr.Dropdown(
                label="Select Application",
                choices=[],
                interactive=True,
                allow_custom_value=True,
            )
            cfg_refresh_btn = gr.Button("↻ Refresh App List", size="sm")
            cfg_app_id = gr.Textbox(label="App ID", placeholder="Auto-filled from picker, or paste UUID…")
            cfg_entity = gr.Dropdown(
                label="Entity Type",
                choices=[],
                interactive=True,
                allow_custom_value=True,
            )
            cfg_strategy = gr.Radio(
                choices=["replace", "hash", "encrypt", "fake"],
                value="replace",
                label="Strategy",
            )
            gr.Markdown(
                "**replace** — unique placeholders like `{{PERSON_1}}`&ensp; · &ensp;"
                "**hash** — one-way SHA3-256 hash&ensp; · &ensp;"
                "**encrypt** — reversible encryption&ensp; · &ensp;"
                "**fake** — realistic-looking substitute values",
                elem_classes=["strategy-help"],
            )
            cfg_btn = gr.Button("Update Config", variant="primary", elem_classes=["primary-btn"])
            cfg_output = gr.Textbox(label="Result", lines=6, interactive=False)

            cfg_refresh_btn.click(
                fn=lambda: gr.update(choices=_fetch_app_choices()),
                inputs=[],
                outputs=[cfg_picker],
            )
            cfg_picker.change(
                fn=_extract_app_id,
                inputs=[cfg_picker],
                outputs=[cfg_app_id],
            )
            cfg_btn.click(
                fn=update_app_config,
                inputs=[cfg_app_id, cfg_entity, cfg_strategy],
                outputs=[cfg_output],
            )

        with gr.Accordion("Delete Application", open=False):
            del_picker = gr.Dropdown(
                label="Select Application",
                choices=[],
                interactive=True,
                allow_custom_value=True,
            )
            del_refresh_btn = gr.Button("↻ Refresh App List", size="sm")
            del_app_id = gr.Textbox(label="App ID", placeholder="Auto-filled from picker, or paste UUID…")
            del_btn = gr.Button("Delete", variant="stop")
            del_output = gr.Textbox(label="Result", lines=2, interactive=False)

            del_refresh_btn.click(
                fn=lambda: gr.update(choices=_fetch_app_choices()),
                inputs=[],
                outputs=[del_picker],
            )
            del_picker.change(
                fn=_extract_app_id,
                inputs=[del_picker],
                outputs=[del_app_id],
            )
            del_btn.click(
                fn=delete_app,
                inputs=[del_app_id],
                outputs=[del_output],
            )

    # ── Allow-Lists Tab ──────────────────────────────────────────────────

    with gr.Tab("Allow-Lists"):
        gr.Markdown("### Per-App Allow-Lists")
        gr.Markdown(
            "Manage **entity-type** and **entity-keyword** exclusions. "
            "Items on these lists will be skipped during anonymization."
        )

        al_picker = gr.Dropdown(
            label="Select Application",
            choices=[],
            interactive=True,
            allow_custom_value=True,
        )
        al_refresh_btn = gr.Button("↻ Refresh App List", size="sm")
        al_app_id = gr.Textbox(label="App ID", interactive=False)

        al_refresh_btn.click(
            fn=lambda: gr.update(choices=_fetch_app_choices()),
            inputs=[],
            outputs=[al_picker],
        )
        al_picker.change(
            fn=_extract_app_id,
            inputs=[al_picker],
            outputs=[al_app_id],
        )

        with gr.Accordion("Entity-Type Allow-List", open=True):
            gr.Markdown("Select entity types to **exclude** from anonymization for this app.")
            et_types = gr.CheckboxGroup(
                label="Entity Types to Exclude",
                choices=[],
                interactive=True,
            )
            with gr.Row():
                et_load_btn = gr.Button("Load", variant="secondary")
                et_save_btn = gr.Button("Save", variant="primary", elem_classes=["primary-btn"])
            et_output = gr.Textbox(label="Status", lines=1, interactive=False)

            et_load_btn.click(fn=load_entity_type_allow_list, inputs=[al_app_id], outputs=[et_types])
            et_save_btn.click(fn=save_entity_type_allow_list, inputs=[al_app_id, et_types], outputs=[et_output])

        with gr.Accordion("Entity-Keyword Allow-List", open=True):
            gr.Markdown(
                "Exclude **specific text** only when detected as a **specific entity type**. "
                "For example, allow 'Contoso Bank' as ORGANIZATION but still detect it if classified as something else.\n\n"
                "Edit the table directly to delete rows (clear the cell values), or use the **＋ Add** controls below."
            )

            # Current pairs — interactive so users can delete rows directly
            ekw_current = gr.Dataframe(
                headers=["Entity Type", "Keyword", ""],
                datatype=["str", "str", "str"],
                label="Current Entity-Keyword Pairs (delete a row by clicking then Save)",
                interactive=True,
                wrap=True,
                max_height=300,
                col_count=(3, "fixed"),
            )

            # Add new pair
            gr.Markdown("**Add a new pair:**")
            with gr.Row():
                ekw_entity_type = gr.Dropdown(
                    label="Entity Type",
                    choices=[],
                    interactive=True,
                    allow_custom_value=True,
                    scale=2,
                )
                ekw_keyword = gr.Textbox(
                    label="Keyword",
                    placeholder="e.g. Contoso Bank",
                    interactive=True,
                    scale=3,
                )
                ekw_add_btn = gr.Button("＋ Add", variant="primary", elem_classes=["primary-btn"], scale=1)

            with gr.Row():
                ekw_load_btn = gr.Button("Load from server", variant="secondary")
                ekw_save_btn = gr.Button("Save to server", variant="primary", elem_classes=["primary-btn"])
            ekw_output = gr.Textbox(label="Status", lines=1, interactive=False)

            ekw_load_btn.click(fn=load_entity_keyword_allow_list_rows, inputs=[al_app_id], outputs=[ekw_current])
            ekw_save_btn.click(fn=save_entity_keyword_allow_list_rows, inputs=[al_app_id, ekw_current], outputs=[ekw_output])
            ekw_add_btn.click(fn=add_entity_keyword_pair, inputs=[ekw_current, ekw_entity_type, ekw_keyword], outputs=[ekw_current])

        # Auto-load lists when app selection changes
        al_picker.change(fn=load_entity_type_allow_list, inputs=[al_app_id], outputs=[et_types])
        al_picker.change(fn=load_entity_keyword_allow_list_rows, inputs=[al_app_id], outputs=[ekw_current])

    # ── Audit Log Tab ────────────────────────────────────────────────────

    with gr.Tab("Audit Log"):
        gr.Markdown("### Application Audit Log")
        gr.Markdown("Track when applications are registered, modified, and deleted.")

        with gr.Row():
            audit_app_filter = gr.Dropdown(
                label="Filter by Application",
                choices=[],
                interactive=True,
                allow_custom_value=True,
                value="",
            )
            audit_action_filter = gr.Dropdown(
                label="Filter by Action",
                choices=AUDIT_ACTIONS,
                value="(all)",
                interactive=True,
            )
        audit_refresh_btn = gr.Button("Refresh", variant="primary", elem_classes=["primary-btn"])

        audit_output = gr.Dataframe(
            headers=["Timestamp", "Action", "App Name", "App ID", "Details"],
            datatype=["str", "str", "str", "str", "str"],
            label="Audit Log",
            interactive=False,
            wrap=True,
            max_height=500,
        )

        audit_refresh_btn.click(
            fn=fetch_audit_log,
            inputs=[audit_app_filter, audit_action_filter],
            outputs=[audit_output],
        )
        admin_demo.load(
            fn=fetch_audit_log,
            inputs=[audit_app_filter, audit_action_filter],
            outputs=[audit_output],
        )

    # Populate all pickers on page load
    admin_demo.load(
        fn=lambda: (
            gr.update(choices=_fetch_app_choices()),
            gr.update(choices=_fetch_app_choices()),
            gr.update(choices=_fetch_app_choices()),
            gr.update(choices=_fetch_entity_types()),
            gr.update(choices=_fetch_app_choices()),
            gr.update(choices=_fetch_entity_types()),
            gr.update(choices=_fetch_entity_types()),
            gr.update(choices=[("(all apps)", "")] + [(c, c) for c in _fetch_app_choices()]),
        ),
        inputs=[],
        outputs=[
            lookup_picker, cfg_picker, del_picker, cfg_entity,
            al_picker, et_types, ekw_entity_type,
            audit_app_filter,
        ],
    )


if __name__ == "__main__":
    admin_demo.launch(server_name="0.0.0.0", server_port=7861)
