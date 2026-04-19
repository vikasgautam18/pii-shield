"""Tests for recognizer context configuration loading and application."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from pii_shield.context_config import apply_recognizer_contexts, load_recognizer_contexts


# -- Helpers ------------------------------------------------------------------

class FakeRecognizer:
    """Minimal recognizer stub with a name and context list."""

    def __init__(self, name: str, context: list[str] | None = None):
        self.name = name
        self.context = list(context or [])


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "contexts.yml"
    p.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return p


# -- load_recognizer_contexts -------------------------------------------------

class TestLoadRecognizerContexts:

    def test_load_valid_yaml(self, tmp_path):
        data = {
            "recognizers": {
                "MyRecognizer": {
                    "context_append": ["word1", "word2"],
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert "MyRecognizer" in result
        assert result["MyRecognizer"]["context_append"] == ["word1", "word2"]

    def test_load_replace_mode(self, tmp_path):
        data = {
            "recognizers": {
                "MyRecognizer": {
                    "context": ["only", "these"],
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert result["MyRecognizer"]["context"] == ["only", "these"]
        assert "context_append" not in result["MyRecognizer"]

    def test_load_both_modes(self, tmp_path):
        data = {
            "recognizers": {
                "Rec": {
                    "context": ["a"],
                    "context_append": ["b"],
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert result["Rec"]["context"] == ["a"]
        assert result["Rec"]["context_append"] == ["b"]

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_recognizer_contexts("/nonexistent/path.yml")

    def test_missing_recognizers_key(self, tmp_path):
        path = _write_yaml(tmp_path, {"other": "data"})
        result = load_recognizer_contexts(path)
        assert result == {}

    def test_invalid_context_type(self, tmp_path):
        data = {
            "recognizers": {
                "Rec": {
                    "context": "not a list",
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert result == {}

    def test_unknown_keys_still_loads_valid(self, tmp_path):
        data = {
            "recognizers": {
                "Rec": {
                    "context_append": ["ok"],
                    "invalid_key": True,
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert result["Rec"]["context_append"] == ["ok"]

    def test_multiple_recognizers(self, tmp_path):
        data = {
            "recognizers": {
                "RecA": {"context": ["a1", "a2"]},
                "RecB": {"context_append": ["b1"]},
            },
        }
        path = _write_yaml(tmp_path, data)
        result = load_recognizer_contexts(path)
        assert len(result) == 2


# -- apply_recognizer_contexts ------------------------------------------------

class TestApplyRecognizerContexts:

    def test_append_extends_context(self):
        rec = FakeRecognizer("MyRec", ["existing"])
        overrides = {"MyRec": {"context_append": ["new1", "new2"]}}
        modified = apply_recognizer_contexts([rec], overrides)
        assert rec.context == ["existing", "new1", "new2"]
        assert modified == ["MyRec"]

    def test_replace_replaces_context(self):
        rec = FakeRecognizer("MyRec", ["old1", "old2"])
        overrides = {"MyRec": {"context": ["new1"]}}
        modified = apply_recognizer_contexts([rec], overrides)
        assert rec.context == ["new1"]
        assert modified == ["MyRec"]

    def test_replace_takes_priority_over_append(self):
        rec = FakeRecognizer("MyRec", ["old"])
        overrides = {"MyRec": {"context": ["replaced"], "context_append": ["appended"]}}
        apply_recognizer_contexts([rec], overrides)
        # When both present, context (replace) wins
        assert rec.context == ["replaced"]

    def test_unmatched_recognizer_ignored(self):
        rec = FakeRecognizer("MyRec", ["a"])
        overrides = {"NonExistent": {"context_append": ["x"]}}
        modified = apply_recognizer_contexts([rec], overrides)
        assert modified == []
        assert rec.context == ["a"]

    def test_no_context_attribute(self):
        class NoCtx:
            name = "NoCtxRec"
        overrides = {"NoCtxRec": {"context_append": ["x"]}}
        modified = apply_recognizer_contexts([NoCtx()], overrides)
        assert modified == []

    def test_empty_overrides(self):
        rec = FakeRecognizer("MyRec", ["a"])
        modified = apply_recognizer_contexts([rec], {})
        assert modified == []
        assert rec.context == ["a"]


# -- Integration with PiiShieldEngine ----------------------------------------

class TestEngineContextFile:

    def test_engine_loads_context_file(self, tmp_path):
        data = {
            "recognizers": {
                "InBankAccountRecognizer": {
                    "context_append": ["test_bank_xyz"],
                },
            },
        }
        path = _write_yaml(tmp_path, data)

        from pii_shield.engine import PiiShieldEngine
        engine = PiiShieldEngine(context_file=str(path))

        # Find the recognizer and check context was extended
        for rec in engine._analyzer.registry.recognizers:
            if rec.name == "InBankAccountRecognizer":
                assert "test_bank_xyz" in rec.context
                break
        else:
            pytest.fail("InBankAccountRecognizer not found in registry")

    def test_engine_env_var(self, tmp_path, monkeypatch):
        data = {
            "recognizers": {
                "UsBankAccountRecognizer": {
                    "context_append": ["env_var_bank"],
                },
            },
        }
        path = _write_yaml(tmp_path, data)
        monkeypatch.setenv("RECOGNIZER_CONTEXTS_FILE", str(path))

        from pii_shield.engine import PiiShieldEngine
        engine = PiiShieldEngine()

        for rec in engine._analyzer.registry.recognizers:
            if rec.name == "UsBankAccountRecognizer":
                assert "env_var_bank" in rec.context
                break
        else:
            pytest.fail("UsBankAccountRecognizer not found in registry")

    def test_engine_missing_file_no_crash(self):
        from pii_shield.engine import PiiShieldEngine
        # Should not raise, just log warning
        engine = PiiShieldEngine(context_file="/nonexistent/file.yml")
        assert engine._score_threshold == 0.35

    def test_engine_no_context_file(self):
        from pii_shield.engine import PiiShieldEngine
        engine = PiiShieldEngine()
        # Default behavior, no crash
        assert engine._score_threshold == 0.35
