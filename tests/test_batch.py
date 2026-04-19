"""Tests for pii_shield.batch — BatchProcessor with CSV, JSONL, DataFrame adapters."""

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pii_shield.batch import BatchProcessor, BatchResult, _get_nested, _set_nested
from pii_shield.models import AnonymizeResult, EntityConfig


# ---------------------------------------------------------------------------
# Fixtures — use a mock engine to avoid NLP model loading overhead
# ---------------------------------------------------------------------------


class FakeEngine:
    """Lightweight engine mock for batch tests."""

    def anonymize(self, text, language="en", config=None, **kwargs):
        # Simple fake: wrap each word > 3 chars in placeholder
        words = text.split()
        mapping = {}
        anonymized = text
        counter = 0
        for word in words:
            if len(word) > 5 and word.isalpha():
                counter += 1
                placeholder = f"{{{{ENTITY_{counter}}}}}"
                mapping[placeholder] = word
                anonymized = anonymized.replace(word, placeholder, 1)
        return AnonymizeResult(
            anonymized_text=anonymized,
            entity_mapping=mapping,
        )

    def deanonymize(self, text, entity_mapping, **kwargs):
        result = text
        for k, v in entity_mapping.items():
            result = result.replace(k, v)
        return result


@pytest.fixture()
def fake_engine():
    return FakeEngine()


@pytest.fixture()
def processor(fake_engine):
    return BatchProcessor(engine=fake_engine, parallelism="none")


# ---------------------------------------------------------------------------
# Dot-notation helpers
# ---------------------------------------------------------------------------

class TestDotNotation:
    def test_get_nested_simple(self):
        assert _get_nested({"a": 1}, "a") == 1

    def test_get_nested_deep(self):
        assert _get_nested({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_get_nested_missing(self):
        assert _get_nested({"a": 1}, "b") is None

    def test_set_nested_simple(self):
        obj = {"a": 1}
        _set_nested(obj, "a", 2)
        assert obj["a"] == 2

    def test_set_nested_deep(self):
        obj = {"a": {"b": {"c": 3}}}
        _set_nested(obj, "a.b.c", 99)
        assert obj["a"]["b"]["c"] == 99

    def test_set_nested_creates_parents(self):
        obj = {}
        _set_nested(obj, "a.b.c", 42)
        assert obj["a"]["b"]["c"] == 42


# ---------------------------------------------------------------------------
# anonymize_texts
# ---------------------------------------------------------------------------

class TestAnonymizeTexts:
    def test_basic_batch(self, processor):
        result = processor.anonymize_texts(
            ["Hello Rahul", "Testing simple text", "Another sentence"]
        )
        assert result.total == 3
        assert result.succeeded == 3
        assert result.failed == 0

    def test_empty_batch(self, processor):
        result = processor.anonymize_texts([])
        assert result.total == 0
        assert result.succeeded == 0

    def test_progress_callback(self, processor):
        progress = []
        processor.on_progress = lambda done, total: progress.append((done, total))
        processor.anonymize_texts(["text one", "text two"])
        assert len(progress) == 2
        assert progress[-1] == (2, 2)

    def test_error_collect(self, fake_engine):
        def bad_anonymize(text, **kwargs):
            if "FAIL" in text:
                raise ValueError("Forced error")
            return AnonymizeResult(anonymized_text=text)

        fake_engine.anonymize = bad_anonymize
        proc = BatchProcessor(engine=fake_engine, parallelism="none", on_error="collect")
        result = proc.anonymize_texts(["good text", "FAIL here", "also good"])
        assert result.succeeded == 2
        assert result.failed == 1
        assert len(result.errors) == 1
        assert result.errors[0][0] == 1  # index of failed item

    def test_error_raise(self, fake_engine):
        def bad_anonymize(text, **kwargs):
            raise ValueError("boom")

        fake_engine.anonymize = bad_anonymize
        proc = BatchProcessor(engine=fake_engine, parallelism="none", on_error="raise")
        with pytest.raises(ValueError, match="boom"):
            proc.anonymize_texts(["any text"])

    def test_error_skip(self, fake_engine):
        call_count = 0
        def bad_anonymize(text, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("skip this")
            return AnonymizeResult(anonymized_text=text)

        fake_engine.anonymize = bad_anonymize
        proc = BatchProcessor(engine=fake_engine, parallelism="none", on_error="skip")
        result = proc.anonymize_texts(["ok1", "fail", "ok2"])
        assert result.succeeded == 2
        assert result.failed == 1
        assert len(result.errors) == 0  # skip mode doesn't collect


# ---------------------------------------------------------------------------
# deanonymize_texts
# ---------------------------------------------------------------------------

class TestDeanonymizeTexts:
    def test_round_trip(self, processor):
        anon = processor.anonymize_texts(["Hello Rahul Sharma"])
        texts = [r.anonymized_text for r in anon.results]
        mappings = [r.entity_mapping for r in anon.results]
        restored = processor.deanonymize_texts(texts, mappings)
        assert "Rahul" in restored[0]
        assert "Sharma" in restored[0]

    def test_length_mismatch_raises(self, processor):
        with pytest.raises(ValueError, match="same length"):
            processor.deanonymize_texts(["a", "b"], [{}])


# ---------------------------------------------------------------------------
# anonymize_csv
# ---------------------------------------------------------------------------

class TestAnonymizeCsv:
    def test_csv_round_trip(self, processor, tmp_path):
        in_path = tmp_path / "in.csv"
        out_path = tmp_path / "out.csv"

        with open(in_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "comment"])
            w.writeheader()
            w.writerow({"id": "1", "comment": "Hello Rahul Sharma"})
            w.writerow({"id": "2", "comment": "Simple text"})

        result = processor.anonymize_csv(in_path, out_path, text_columns=["comment"])
        assert result.total == 2
        assert result.succeeded == 2

        with open(out_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert "__pii_mappings" in rows[0]
        # Verify the mapping is valid JSON
        mappings = json.loads(rows[0]["__pii_mappings"])
        assert "comment" in mappings

    def test_csv_preserves_non_text_columns(self, processor, tmp_path):
        in_path = tmp_path / "in.csv"
        out_path = tmp_path / "out.csv"

        with open(in_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["id", "name", "score"])
            w.writeheader()
            w.writerow({"id": "1", "name": "TestName", "score": "95"})

        processor.anonymize_csv(in_path, out_path, text_columns=["name"])

        with open(out_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["id"] == "1"
        assert rows[0]["score"] == "95"


# ---------------------------------------------------------------------------
# anonymize_jsonl
# ---------------------------------------------------------------------------

class TestAnonymizeJsonl:
    def test_jsonl_basic(self, processor, tmp_path):
        in_path = tmp_path / "in.jsonl"
        out_path = tmp_path / "out.jsonl"

        lines = [
            json.dumps({"text": "Hello Rahul Sharma", "id": 1}),
            json.dumps({"text": "Simple words", "id": 2}),
        ]
        in_path.write_text("\n".join(lines))

        result = processor.anonymize_jsonl(
            in_path, out_path, text_fields=["text"]
        )
        assert result.total == 2
        assert result.succeeded == 2

        out_lines = out_path.read_text().strip().split("\n")
        assert len(out_lines) == 2

        # Check mappings sidecar exists
        mappings_path = Path(str(out_path) + ".mappings.jsonl")
        assert mappings_path.exists()

    def test_jsonl_nested_fields(self, processor, tmp_path):
        in_path = tmp_path / "in.jsonl"
        out_path = tmp_path / "out.jsonl"

        record = {"message": {"body": "Priya Sharma called"}, "id": 1}
        in_path.write_text(json.dumps(record))

        result = processor.anonymize_jsonl(
            in_path, out_path, text_fields=["message.body"]
        )
        assert result.succeeded == 1

        out_record = json.loads(out_path.read_text().strip())
        assert "message" in out_record
        assert "body" in out_record["message"]


# ---------------------------------------------------------------------------
# anonymize_dataframe
# ---------------------------------------------------------------------------

class TestAnonymizeDataframe:
    def test_dataframe_basic(self, processor):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({
            "id": [1, 2],
            "comment": ["Hello Rahul Sharma", "Simple text"],
        })
        result_df = processor.anonymize_dataframe(df, text_columns=["comment"])
        assert "_pii_mappings" in result_df.columns
        assert len(result_df) == 2
        # Original df unchanged
        assert "Rahul" in df.iloc[0]["comment"]

    def test_dataframe_preserves_other_columns(self, processor):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({
            "id": [1],
            "text": ["Hello Rahul Sharma"],
            "score": [42],
        })
        result_df = processor.anonymize_dataframe(df, text_columns=["text"])
        assert result_df.iloc[0]["id"] == 1
        assert result_df.iloc[0]["score"] == 42


# ---------------------------------------------------------------------------
# Parallelism modes
# ---------------------------------------------------------------------------

class TestParallelism:
    def test_thread_parallelism(self, fake_engine):
        proc = BatchProcessor(engine=fake_engine, parallelism="thread", max_workers=2)
        result = proc.anonymize_texts(["text one", "text two", "text three"])
        assert result.succeeded == 3

    def test_invalid_parallelism_raises(self, fake_engine):
        with pytest.raises(ValueError, match="Invalid parallelism"):
            BatchProcessor(engine=fake_engine, parallelism="invalid")
