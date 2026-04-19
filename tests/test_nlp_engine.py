"""Tests for the NLP engine factory (app/nlp_engine.py)."""

import os

import pytest

from app.nlp_engine import create_nlp_engine, get_nlp_engine_name


class TestGetNlpEngineName:
    def test_defaults_to_spacy(self, monkeypatch):
        monkeypatch.delenv("NLP_ENGINE", raising=False)
        assert get_nlp_engine_name() == "spacy"

    def test_reads_env_spacy(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "spacy")
        assert get_nlp_engine_name() == "spacy"

    def test_reads_env_stanza(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "stanza")
        assert get_nlp_engine_name() == "stanza"

    def test_reads_env_transformers(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "transformers")
        assert get_nlp_engine_name() == "transformers"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "Stanza")
        assert get_nlp_engine_name() == "stanza"

    def test_case_insensitive_transformers(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "Transformers")
        assert get_nlp_engine_name() == "transformers"

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "  spacy  ")
        assert get_nlp_engine_name() == "spacy"

    def test_invalid_engine_raises(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "flair")
        with pytest.raises(ValueError, match="Invalid NLP_ENGINE='flair'"):
            get_nlp_engine_name()


class TestCreateNlpEngine:
    def test_creates_spacy_engine(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "spacy")
        engine = create_nlp_engine()
        assert type(engine).__name__ == "SpacyNlpEngine"

    def test_creates_stanza_engine(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "stanza")
        engine = create_nlp_engine()
        assert type(engine).__name__ == "StanzaNlpEngine"

    def test_creates_transformers_engine(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "transformers")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        engine = create_nlp_engine()
        assert type(engine).__name__ == "TransformersNlpEngine"

    def test_transformers_uses_env_model(self, monkeypatch):
        """Verify the engine picks up the model from TRANSFORMERS_MODEL env var."""
        monkeypatch.setenv("NLP_ENGINE", "transformers")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        engine = create_nlp_engine()
        assert engine.is_loaded()

    def test_spacy_detects_person(self, monkeypatch):
        """Smoke test: spaCy engine can detect a PERSON entity."""
        monkeypatch.setenv("NLP_ENGINE", "spacy")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "PERSON" in entity_types

    def test_stanza_detects_person(self, monkeypatch):
        """Smoke test: Stanza engine can detect a PERSON entity."""
        monkeypatch.setenv("NLP_ENGINE", "stanza")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "PERSON" in entity_types

    def test_transformers_detects_person(self, monkeypatch):
        """Smoke test: Transformers engine can detect a PERSON entity."""
        monkeypatch.setenv("NLP_ENGINE", "transformers")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "PERSON" in entity_types

    def test_transformers_detects_location(self, monkeypatch):
        """Smoke test: Transformers engine can detect a LOCATION entity."""
        monkeypatch.setenv("NLP_ENGINE", "transformers")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "LOCATION" in entity_types

    def test_reads_env_onnx(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "onnx")
        assert get_nlp_engine_name() == "onnx"

    def test_creates_onnx_engine(self, monkeypatch):
        monkeypatch.setenv("NLP_ENGINE", "onnx")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        engine = create_nlp_engine()
        assert type(engine).__name__ == "OnnxTransformersNlpEngine"

    def test_onnx_detects_person(self, monkeypatch):
        """Smoke test: ONNX engine can detect a PERSON entity."""
        monkeypatch.setenv("NLP_ENGINE", "onnx")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "PERSON" in entity_types

    def test_onnx_detects_location(self, monkeypatch):
        """Smoke test: ONNX engine can detect a LOCATION entity."""
        monkeypatch.setenv("NLP_ENGINE", "onnx")
        monkeypatch.setenv("TRANSFORMERS_MODEL", "dslim/bert-base-NER")
        monkeypatch.setenv("TRANSFORMERS_SPACY_MODEL", "en_core_web_sm")
        from presidio_analyzer import AnalyzerEngine

        engine = create_nlp_engine()
        analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["en"])
        results = analyzer.analyze(text="John Smith lives in London.", language="en")
        entity_types = {r.entity_type for r in results}
        assert "LOCATION" in entity_types
