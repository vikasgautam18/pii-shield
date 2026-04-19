"""Gradio UI for PII Shield — Anonymize & De-anonymize tabs.

Run:  python app/gradio_app.py          → http://localhost:7860
Admin UI is in gradio_admin.py          → http://localhost:7861
"""

import json
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

def _fetch_app_choices() -> list[str]:
    """Return a list of 'AppName (app_id)' strings for dropdown pickers."""
    try:
        resp = requests.get(f"{API_BASE}/apps")
        if resp.status_code == 200:
            return [f"{a['app_name']} ({a['app_id']})" for a in resp.json()]
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

def _api_error_detail(resp: requests.Response) -> str:
    """Extract a human-readable error message from an API error response."""
    try:
        return resp.json().get("detail", resp.text)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


# ── Tab 1: Anonymize ─────────────────────────────────────────────────────────

def anonymize(text: str, app_id: str) -> tuple[str, str, str, str]:
    """Returns (detected_piis, anonymized_text, session_id, entity_mapping_json)."""
    if not text.strip():
        return "", "", "", ""

    headers = {}
    if app_id.strip():
        headers["X-App-Id"] = app_id.strip()

    try:
        resp = requests.post(
            f"{API_BASE}/anonymize_unique",
            json={"text": text, "language": "en"},
            headers=headers,
        )
        if resp.status_code != 200:
            raise gr.Error(f"Anonymization failed: {_api_error_detail(resp)}")
    except requests.ConnectionError:
        raise gr.Error("Cannot reach the PII Shield API server.")

    data = resp.json()

    pii_lines_parts: list[str] = []
    for placeholder, original in data["entity_mapping"].items():
        pii_lines_parts.append(f"{placeholder}  →  {original}")
    for _hash, original in data.get("hash_mapping", {}).items():
        pii_lines_parts.append(f"(hashed)  →  {original}")
    for _token, original in data.get("encrypt_mapping", {}).items():
        pii_lines_parts.append(f"(encrypted)  →  {original}")
    pii_lines = "\n".join(pii_lines_parts)

    all_mappings = {
        **data["entity_mapping"],
        **data.get("hash_mapping", {}),
        **data.get("encrypt_mapping", {}),
    }

    return pii_lines, data["anonymized_text"], data["id"], json.dumps(all_mappings)


# ── Tab 2: De-anonymize ──────────────────────────────────────────────────────

def deanonymize(text: str, session_id: str, mapping_json: str, app_id: str,
                include_hashed: bool = False, include_encrypted: bool = False) -> tuple[str, str]:
    """Returns (entity_mapping_display, restored_text)."""
    if not text.strip() or not session_id:
        return "No active session — anonymize text in the Anonymize tab first.", ""

    mapping: dict = json.loads(mapping_json) if mapping_json else {}
    mapping_lines = "\n".join(
        f"{placeholder}  →  {original}"
        for placeholder, original in mapping.items()
    ) or "(no entities)"

    headers = {}
    if app_id.strip():
        headers["X-App-Id"] = app_id.strip()

    try:
        resp = requests.post(
            f"{API_BASE}/deanonymize",
            json={
                "id": session_id,
                "text": text,
                "include_hashed": include_hashed,
                "include_encrypted": include_encrypted,
            },
            headers=headers,
        )
        if resp.status_code != 200:
            raise gr.Error(f"De-anonymization failed: {_api_error_detail(resp)}")
    except requests.ConnectionError:
        raise gr.Error("Cannot reach the PII Shield API server.")

    return mapping_lines, resp.json()["text"]


# ── Build UI ─────────────────────────────────────────────────────────────────

with gr.Blocks(title="PII Shield", css=BLUE_BUTTON_CSS) as demo:
    gr.Markdown("# PII Shield")
    gr.Markdown("Detect, anonymize, and restore PII using Microsoft Presidio.")

    session_id = gr.State(value="")
    entity_mapping_json = gr.State(value="")

    with gr.Tab("Anonymize"):
        with gr.Row():
            anon_app_picker = gr.Dropdown(
                label="Application (optional)",
                choices=[],
                interactive=True,
                allow_custom_value=True,
                scale=1,
            )
            anon_app_refresh = gr.Button("↻", size="sm", scale=0)
        anon_app_id = gr.Textbox(label="App ID", interactive=False, visible=False)
        with gr.Row():
            anon_input = gr.Textbox(
                label="Input Text",
                placeholder="Enter text containing PII…",
                lines=5,
            )
        with gr.Row():
            anon_btn = gr.Button("Anonymize", variant="primary", elem_classes=["primary-btn"])
        with gr.Row():
            pii_output = gr.Textbox(label="Detected PIIs", lines=5, interactive=False)
            anon_output = gr.Textbox(label="Anonymized Text", lines=5, interactive=False)

        anon_app_refresh.click(
            fn=lambda: gr.update(choices=_fetch_app_choices()),
            inputs=[],
            outputs=[anon_app_picker],
        )
        anon_app_picker.change(
            fn=_extract_app_id,
            inputs=[anon_app_picker],
            outputs=[anon_app_id],
        )
        anon_btn.click(
            fn=anonymize,
            inputs=[anon_input, anon_app_id],
            outputs=[pii_output, anon_output, session_id, entity_mapping_json],
        )

    with gr.Tab("De-anonymize"):
        with gr.Row():
            deanon_app_picker = gr.Dropdown(
                label="Application (optional)",
                choices=[],
                interactive=True,
                allow_custom_value=True,
                scale=1,
            )
            deanon_app_refresh = gr.Button("↻", size="sm", scale=0)
        deanon_app_id = gr.Textbox(label="App ID", interactive=False, visible=False)
        with gr.Row():
            deanon_input = gr.Textbox(
                label="Anonymized Text",
                placeholder="Paste anonymized text (with {{ENTITY_N}} placeholders)…",
                lines=5,
            )
        with gr.Row():
            include_hashed_cb = gr.Checkbox(label="Restore hashed entities", value=True)
            include_encrypted_cb = gr.Checkbox(label="Restore encrypted entities", value=True)
        with gr.Row():
            deanon_btn = gr.Button("De-anonymize", variant="primary", elem_classes=["primary-btn"])
        with gr.Row():
            mapping_output = gr.Textbox(
                label="PII ↔ Placeholder Mapping", lines=5, interactive=False
            )
            deanon_output = gr.Textbox(
                label="De-anonymized Text", lines=5, interactive=False
            )

        deanon_app_refresh.click(
            fn=lambda: gr.update(choices=_fetch_app_choices()),
            inputs=[],
            outputs=[deanon_app_picker],
        )
        deanon_app_picker.change(
            fn=_extract_app_id,
            inputs=[deanon_app_picker],
            outputs=[deanon_app_id],
        )
        deanon_btn.click(
            fn=deanonymize,
            inputs=[deanon_input, session_id, entity_mapping_json, deanon_app_id,
                    include_hashed_cb, include_encrypted_cb],
            outputs=[mapping_output, deanon_output],
        )

    # Populate app pickers on page load
    demo.load(
        fn=lambda: (
            gr.update(choices=_fetch_app_choices()),
            gr.update(choices=_fetch_app_choices()),
        ),
        inputs=[],
        outputs=[anon_app_picker, deanon_app_picker],
    )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
