"""High-throughput batch processing for PII Shield.

``BatchProcessor`` wraps ``PiiShieldEngine`` with parallelism, progress
tracking, and I/O adapters for CSV, JSONL, and pandas DataFrames.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from pii_shield.engine import PiiShieldEngine
from pii_shield.models import AnonymizeResult, EntityConfig

logger = logging.getLogger("pii-shield")


@dataclass
class BatchResult:
    """Aggregated results of a batch run."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[tuple[int, str, Exception]] = field(default_factory=list)
    results: list[AnonymizeResult] = field(default_factory=list)


def _anonymize_single(args: tuple) -> tuple[int, AnonymizeResult | None, Exception | None]:
    """Worker function for parallel anonymization (must be top-level for pickling)."""
    idx, text, language, config_dict, engine = args
    try:
        config = EntityConfig(strategies=config_dict) if config_dict else None
        result = engine.anonymize(text, language=language, config=config)
        return (idx, result, None)
    except Exception as e:
        return (idx, None, e)


class BatchProcessor:
    """Process large text datasets through PiiShieldEngine.

    Supports multithreaded and multiprocess parallelism.

    Parameters
    ----------
    engine : PiiShieldEngine, optional
        Engine instance.  If not provided, one is created with defaults.
    parallelism : str
        ``"thread"`` (default), ``"process"``, or ``"none"``.
    max_workers : int, optional
        Maximum worker count.  Defaults to ``min(os.cpu_count() or 4, 8)``.
    chunk_size : int
        Items per work chunk for streaming adapters (default 100).
    on_progress : callable, optional
        Called as ``on_progress(completed, total)`` after each item.
    on_error : str
        Error strategy: ``"collect"`` (default — continue, record error),
        ``"raise"`` (stop on first error), or ``"skip"`` (silently skip).
    """

    def __init__(
        self,
        engine: PiiShieldEngine | None = None,
        parallelism: str = "thread",
        max_workers: int | None = None,
        chunk_size: int = 100,
        on_progress: Callable[[int, int], None] | None = None,
        on_error: str = "collect",
    ):
        self.engine = engine or PiiShieldEngine()
        self.parallelism = parallelism
        self.max_workers = max_workers or min(os.cpu_count() or 4, 8)
        self.chunk_size = chunk_size
        self.on_progress = on_progress
        self.on_error = on_error

        if parallelism not in ("thread", "process", "none"):
            raise ValueError(
                f"Invalid parallelism='{parallelism}'. "
                "Must be 'thread', 'process', or 'none'."
            )

    # ------------------------------------------------------------------
    # Core batch methods
    # ------------------------------------------------------------------

    def anonymize_texts(
        self,
        texts: list[str] | Iterator[str],
        language: str = "en",
        config: EntityConfig | None = None,
    ) -> BatchResult:
        """Anonymize a list or iterator of text strings.

        Returns a ``BatchResult`` with per-item results and error details.
        """
        text_list = list(texts)
        total = len(text_list)
        batch = BatchResult(total=total)
        config_dict = config.strategies if config else None

        if self.parallelism == "none":
            for idx, text in enumerate(text_list):
                self._process_one(idx, text, language, config, batch)
                if self.on_progress:
                    self.on_progress(idx + 1, total)
            return batch

        executor_cls = (
            ThreadPoolExecutor
            if self.parallelism == "thread"
            else ProcessPoolExecutor
        )

        def _worker(args: tuple[int, str]):
            idx, text = args
            try:
                result = self.engine.anonymize(
                    text, language=language, config=config
                )
                return (idx, result, None)
            except Exception as e:
                return (idx, None, e)

        # Pre-allocate results list
        batch.results = [None] * total  # type: ignore[list-item]
        completed = 0

        with executor_cls(max_workers=self.max_workers) as executor:
            futures = executor.map(
                _worker,
                [(i, t) for i, t in enumerate(text_list)],
            )
            for idx, result, error in futures:
                completed += 1
                if error is not None:
                    self._handle_error(idx, text_list[idx], error, batch)
                else:
                    batch.results[idx] = result
                    batch.succeeded += 1
                if self.on_progress:
                    self.on_progress(completed, total)

        # Remove None entries from skipped/failed items
        batch.results = [r for r in batch.results if r is not None]
        return batch

    def deanonymize_texts(
        self,
        texts: list[str],
        mappings: list[dict[str, str]],
    ) -> list[str]:
        """Batch deanonymize texts using their respective entity mappings."""
        if len(texts) != len(mappings):
            raise ValueError(
                f"texts ({len(texts)}) and mappings ({len(mappings)}) must be same length"
            )
        return [
            self.engine.deanonymize(text, mapping)
            for text, mapping in zip(texts, mappings)
        ]

    # ------------------------------------------------------------------
    # CSV adapter
    # ------------------------------------------------------------------

    def anonymize_csv(
        self,
        input_path: str | Path,
        output_path: str | Path,
        text_columns: list[str],
        language: str = "en",
        config: EntityConfig | None = None,
        delimiter: str = ",",
    ) -> BatchResult:
        """Anonymize specific columns in a CSV file, streaming row-by-row.

        Writes anonymized CSV to ``output_path``.  Adds a
        ``__pii_mappings`` column containing the JSON-serialised mapping
        for each row (one mapping dict per text column).
        """
        input_path = Path(input_path)
        output_path = Path(output_path)
        batch = BatchResult()

        with open(input_path, newline="", encoding="utf-8") as fin:
            reader = csv.DictReader(fin, delimiter=delimiter)
            if not reader.fieldnames:
                return batch

            out_fields = list(reader.fieldnames) + ["__pii_mappings"]
            with open(output_path, "w", newline="", encoding="utf-8") as fout:
                writer = csv.DictWriter(
                    fout, fieldnames=out_fields, delimiter=delimiter
                )
                writer.writeheader()

                chunk: list[dict] = []
                for row in reader:
                    chunk.append(row)
                    if len(chunk) >= self.chunk_size:
                        self._process_csv_chunk(
                            chunk, writer, text_columns, language, config, batch
                        )
                        chunk = []
                if chunk:
                    self._process_csv_chunk(
                        chunk, writer, text_columns, language, config, batch
                    )

        return batch

    def _process_csv_chunk(
        self,
        chunk: list[dict],
        writer: csv.DictWriter,
        text_columns: list[str],
        language: str,
        config: EntityConfig | None,
        batch: BatchResult,
    ) -> None:
        for row in chunk:
            batch.total += 1
            row_mappings: dict[str, dict] = {}
            try:
                for col in text_columns:
                    if col in row and row[col]:
                        result = self.engine.anonymize(
                            row[col], language=language, config=config
                        )
                        row[col] = result.anonymized_text
                        row_mappings[col] = {
                            "entity_mapping": result.entity_mapping,
                            "hash_mapping": result.hash_mapping,
                            "encrypt_mapping": result.encrypt_mapping,
                        }
                row["__pii_mappings"] = json.dumps(row_mappings)
                writer.writerow(row)
                batch.succeeded += 1
            except Exception as e:
                self._handle_error(
                    batch.total - 1,
                    str({c: row.get(c, "")[:50] for c in text_columns}),
                    e,
                    batch,
                )
            if self.on_progress:
                self.on_progress(batch.total, -1)

    # ------------------------------------------------------------------
    # JSONL adapter
    # ------------------------------------------------------------------

    def anonymize_jsonl(
        self,
        input_path: str | Path,
        output_path: str | Path,
        text_fields: list[str],
        language: str = "en",
        config: EntityConfig | None = None,
    ) -> BatchResult:
        """Anonymize fields in a JSONL (newline-delimited JSON) file.

        ``text_fields`` supports dot-notation for nested fields
        (e.g. ``"message.body"``).

        Writes anonymized JSONL to ``output_path`` and a sidecar
        ``{output_path}.mappings.jsonl`` with per-record mappings.
        """
        input_path = Path(input_path)
        output_path = Path(output_path)
        mappings_path = Path(str(output_path) + ".mappings.jsonl")
        batch = BatchResult()

        with (
            open(input_path, encoding="utf-8") as fin,
            open(output_path, "w", encoding="utf-8") as fout,
            open(mappings_path, "w", encoding="utf-8") as fmap,
        ):
            for line in fin:
                batch.total += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    row_mappings: dict[str, dict] = {}

                    for field_path in text_fields:
                        value = _get_nested(record, field_path)
                        if value and isinstance(value, str):
                            result = self.engine.anonymize(
                                value, language=language, config=config
                            )
                            _set_nested(record, field_path, result.anonymized_text)
                            row_mappings[field_path] = {
                                "entity_mapping": result.entity_mapping,
                                "hash_mapping": result.hash_mapping,
                                "encrypt_mapping": result.encrypt_mapping,
                            }

                    fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fmap.write(json.dumps(row_mappings, ensure_ascii=False) + "\n")
                    batch.succeeded += 1
                except Exception as e:
                    self._handle_error(
                        batch.total - 1, line[:80], e, batch
                    )
                if self.on_progress:
                    self.on_progress(batch.total, -1)

        return batch

    # ------------------------------------------------------------------
    # pandas DataFrame adapter
    # ------------------------------------------------------------------

    def anonymize_dataframe(
        self,
        df: "pandas.DataFrame",
        text_columns: list[str],
        language: str = "en",
        config: EntityConfig | None = None,
    ) -> "pandas.DataFrame":
        """Anonymize columns in a pandas DataFrame.

        Returns a **new** DataFrame with anonymized text columns and an
        additional ``_pii_mappings`` column (list of dicts).

        Requires ``pandas`` (optional dependency).
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "pandas is required for anonymize_dataframe(). "
                "Install it with: pip install pii-shield[batch]"
            )

        result_df = df.copy()
        all_mappings = []

        for idx in range(len(df)):
            row_mappings: dict[str, dict] = {}
            for col in text_columns:
                value = df.iloc[idx][col]
                if isinstance(value, str) and value:
                    result = self.engine.anonymize(
                        value, language=language, config=config
                    )
                    result_df.at[df.index[idx], col] = result.anonymized_text
                    row_mappings[col] = {
                        "entity_mapping": result.entity_mapping,
                        "hash_mapping": result.hash_mapping,
                        "encrypt_mapping": result.encrypt_mapping,
                    }
            all_mappings.append(row_mappings)

        result_df["_pii_mappings"] = all_mappings
        return result_df

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_one(
        self,
        idx: int,
        text: str,
        language: str,
        config: EntityConfig | None,
        batch: BatchResult,
    ) -> None:
        try:
            result = self.engine.anonymize(text, language=language, config=config)
            batch.results.append(result)
            batch.succeeded += 1
        except Exception as e:
            self._handle_error(idx, text, e, batch)

    def _handle_error(
        self,
        idx: int,
        text_preview: str,
        error: Exception,
        batch: BatchResult,
    ) -> None:
        batch.failed += 1
        if self.on_error == "raise":
            raise error
        elif self.on_error == "collect":
            preview = text_preview[:100] if len(text_preview) > 100 else text_preview
            batch.errors.append((idx, preview, error))
            logger.warning("Batch item %d failed: %s", idx, error)
        # "skip" — silently discard


# ---------------------------------------------------------------------------
# Dot-notation helpers for nested JSON fields
# ---------------------------------------------------------------------------


def _get_nested(obj: dict, path: str):
    """Get a value from a nested dict using dot-notation."""
    keys = path.split(".")
    for key in keys:
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return None
    return obj


def _set_nested(obj: dict, path: str, value) -> None:
    """Set a value in a nested dict using dot-notation."""
    keys = path.split(".")
    for key in keys[:-1]:
        if key not in obj or not isinstance(obj[key], dict):
            obj[key] = {}
        obj = obj[key]
    obj[keys[-1]] = value
