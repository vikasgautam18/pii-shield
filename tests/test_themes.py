"""Tests for app.themes — env-var-driven theme resolution.

Because ACTIVE_THEME is computed at module import time, each test forces a
fresh import via importlib.reload after monkey-patching the env var.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_themes():
    import app.themes as themes
    return importlib.reload(themes)


def test_default_theme_is_maroon_when_env_unset(monkeypatch):
    monkeypatch.delenv("PII_SHIELD_THEME", raising=False)
    themes = _reload_themes()
    assert themes.ACTIVE_THEME == "maroon"


def test_env_var_overrides_to_default(monkeypatch):
    monkeypatch.setenv("PII_SHIELD_THEME", "default")
    themes = _reload_themes()
    assert themes.ACTIVE_THEME == "default"


def test_env_var_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PII_SHIELD_THEME", "MAROON")
    themes = _reload_themes()
    assert themes.ACTIVE_THEME == "maroon"


def test_invalid_theme_raises_value_error(monkeypatch):
    monkeypatch.setenv("PII_SHIELD_THEME", "neon")
    with pytest.raises(ValueError, match="not a known theme"):
        _reload_themes()


def test_apply_theme_function_exists(monkeypatch):
    monkeypatch.delenv("PII_SHIELD_THEME", raising=False)
    themes = _reload_themes()
    assert callable(themes.apply_theme)
