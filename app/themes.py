"""Deployment-time theme selection for the Streamlit UIs.

The theme is fixed for the whole deployment via the ``PII_SHIELD_THEME``
environment variable. End users cannot change it; to switch themes the
operator updates the env var and restarts the container.

Valid values:
    maroon   - Burgundy/maroon brand look (default)
    default  - Streamlit's native look with only essential chrome fixes

Wiring in a Streamlit page::

    from themes import apply_theme

    st.set_page_config(...)
    apply_theme()

The active theme is resolved exactly once at module import. Invalid values
raise ``ValueError`` so the container fails to boot loudly rather than
silently serving the wrong skin.
"""

from __future__ import annotations

import os
from typing import Literal

import streamlit as st

ThemeKey = Literal["default", "maroon"]

# ── 1. Shared chrome / UX CSS used by BOTH themes ───────────────────────────
# These rules are not brand-specific — they hide Streamlit toolbar bits we
# never want shown, scale dataframe text to match baseFontSize, force dark
# text in disabled textareas, define the .pii-section-label utility class
# the apps use, and tighten top whitespace.
_CHROME_CSS = """
  #MainMenu {visibility: hidden;}
  footer {visibility: hidden;}
  header [data-testid='stStatusWidget'] {display: none;}
  .stDeployButton {display: none;}

  /* Dataframe / table cells are sized in px by Streamlit; scale to baseFontSize. */
  [data-testid='stDataFrame'] div[role='gridcell'],
  [data-testid='stDataFrame'] div[role='columnheader'],
  [data-testid='stTable'] td,
  [data-testid='stTable'] th { font-size: 1rem !important; }

  /* Force dark-black text in (disabled) text areas — e.g. Anonymized output. */
  [data-testid='stTextArea'] textarea,
  [data-testid='stTextArea'] textarea:disabled,
  [data-testid='stTextAreaRootElement'] textarea {
      color: #000000 !important;
      -webkit-text-fill-color: #000000 !important;
      opacity: 1 !important;
  }

  /* Section labels rendered via st.markdown(<p class='pii-section-label'>…). */
  .pii-section-label {
      font-size: 1.15rem;
      font-weight: 700;
      margin: 0 0 0.35rem 0;
  }

  /* Reduce top whitespace of the main block by ~50%. */
  [data-testid='stMainBlockContainer'],
  .stMainBlockContainer,
  .block-container { padding-top: 3rem !important; }
"""

# ── 2. Streamlit default look — just the shared chrome, no brand overrides ──
_DEFAULT_CSS = f"<style>{_CHROME_CSS}</style>"

# ── 3. Maroon brand overlay (current production styling) ────────────────────
_MAROON_CSS = f"""<style>
  {_CHROME_CSS}

  /* Brand surfaces. */
  body, .stApp {{
      background-color: #FFFFFF !important;
      color: #1F1F1F !important;
  }}
  [data-testid='stSidebar'] > div:first-child,
  [data-testid='stExpander'] {{
      background-color: #F5F0EC !important;
  }}

  /* Heftier tab labels for a bolder nav feel. */
  .stTabs [data-baseweb='tab'] {{ font-size: 1.05rem; font-weight: 600; }}
  .stTabs [aria-selected='true'] {{
      color: #97144D !important;
      border-color: #97144D !important;
  }}

  /* Pill-shaped primary buttons in maroon. */
  .stButton > button[kind='primary'] {{
      background-color: #97144D !important;
      border-color: #97144D !important;
      color: #FFFFFF !important;
      border-radius: 999px !important;
      padding: 0.5rem 1.25rem !important;
  }}
  .stButton > button[kind='primary']:hover {{
      background-color: #7A0F3D !important;
      border-color: #7A0F3D !important;
  }}

  /* Page title (st.title → h1) in maroon. */
  h1, [data-testid='stHeading'] h1 {{ color: #97144D !important; }}

  /* Links. */
  a {{ color: #97144D !important; }}

  /* Widget focus borders. */
  input:focus, textarea:focus, select:focus,
  [data-testid='stTextInput'] input:focus,
  [data-testid='stTextArea'] textarea:focus {{
      border-color: #97144D !important;
      box-shadow: 0 0 0 1px #97144D !important;
  }}
</style>"""

_THEMES: dict[str, str] = {
    "default": _DEFAULT_CSS,
    "maroon": _MAROON_CSS,
}

_FALLBACK: ThemeKey = "maroon"


def _resolve_theme() -> ThemeKey:
    """Read PII_SHIELD_THEME, normalise, validate. Raises ValueError on typo."""
    raw = os.getenv("PII_SHIELD_THEME", _FALLBACK).strip().lower()
    if raw not in _THEMES:
        raise ValueError(
            f"PII_SHIELD_THEME={raw!r} is not a known theme. "
            f"Valid values: {sorted(_THEMES)}"
        )
    return raw  # type: ignore[return-value]


# Resolved once per process at import time — env var changes need a restart.
ACTIVE_THEME: ThemeKey = _resolve_theme()


def apply_theme() -> None:
    """Inject the deployment-fixed theme CSS into the current Streamlit page.

    Call once, immediately after ``st.set_page_config()``.
    """
    st.markdown(_THEMES[ACTIVE_THEME], unsafe_allow_html=True)
