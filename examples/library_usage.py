#!/usr/bin/env python3
"""
PII Shield — Library Usage & Indian Banking Test Suite
=======================================================

Demonstrates using ``pii_shield`` as a standalone Python library.
Loads the same 22 Indian banking test scenarios used by the Azure API
test suite and runs them **entirely in-process** — no web server needed.

Generates a self-contained HTML report at
``examples/__results/library_test_report.html``.

Prerequisites:
    pip install "pii_shield-0.2.0-py3-none-any.whl[batch]"
    python -m spacy download en_core_web_lg   # (or configure your NLP_ENGINE)

Usage:
    python examples/library_usage.py [--output FILE]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

# Load .env so library_usage picks up the same NLP_ENGINE config as the API.
# The pii_shield library intentionally does NOT auto-load .env — applications
# must do it themselves (or export env vars in their shell).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — user must set env vars manually

from pii_shield import (
    BatchProcessor,
    InMemoryMappingStore,
    PiiShieldEngine,
    SqliteMappingStore,
)
from pii_shield.models import AnonymizeResult, EntityConfig

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _config import load_section  # noqa: E402

# ── Configuration loading ────────────────────────────────────────────────────

_cfg = load_section("library")
DEFAULT_OUTPUT = _cfg.get("output", "examples/__results/library_test_report.html")


# ── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_name: str
    original_text: str
    anonymized_text: str
    entities_found: list[dict]
    expected_types: list[str]
    matched_types: set[str] = field(default_factory=set)
    missed_types: set[str] = field(default_factory=set)
    extra_types: set[str] = field(default_factory=set)


@dataclass
class RoundTripResult:
    scenario_id: str
    scenario_name: str
    original: str
    anonymized: str
    entity_mapping: dict
    restored: str
    exact_match: bool


@dataclass
class StrategyResult:
    scenario_id: str
    scenario_name: str
    strategy: str
    original_text: str
    anonymized_text: str
    entity_mapping: dict
    hash_mapping: dict
    encrypt_mapping: dict
    restored_default: str
    restored_full: str
    exact_default: bool
    exact_full: bool


# ── Test Runner ──────────────────────────────────────────────────────────────


class LibraryTestRunner:
    """Runs the Indian banking test scenarios using the library engine."""

    def __init__(self, engine: PiiShieldEngine, scenarios: list[dict]):
        self.engine = engine
        self.scenarios = scenarios
        self.elapsed: float = 0.0

        # Results
        self.anon_results: dict[str, ScenarioResult] = {}
        self.raw_anon: dict[str, AnonymizeResult] = {}
        self.coverage: dict[str, dict] = {}
        self.round_trip_results: list[RoundTripResult] = []
        self.hash_results: list[StrategyResult] = []
        self.encrypt_results: list[StrategyResult] = []
        self.hash_phone_results: list[StrategyResult] = []
        self.encrypt_dl_results: list[StrategyResult] = []
        self.pqc_encrypt_results: list[StrategyResult] = []
        self.fake_results: list[dict] = []
        self.fake_format_results: list[dict] = []
        self.fake_consistency_results: list[dict] = []
        self.fake_round_trip_results: list[dict] = []
        self.fake_mixed_results: list[dict] = []
        self.mixed_strategy_results: list[dict] = []
        self.llm_sandwich: dict = {}
        self.structured_results: list[dict] = []
        self.structured_rt: list[dict] = []
        self.address_indicator_results: list[dict] = []
        self.geo_coordinate_results: list[dict] = []
        self.nrp_results: list[dict] = []
        self.ckyc_pran_apaar_results: list[dict] = []
        self.customer_id_results: list[dict] = []
        self.allow_list_results: dict = {}
        self.batch_result = None
        self.csv_result = None
        self.mapping_store_result: dict = {}
        self.pandas_result: dict = {}

    def _log(self, section: str, msg: str = "") -> None:
        print(f"  [{section:<30}] {msg}")

    # ── Sections ─────────────────────────────────────────────────────────

    def run_anonymization(self) -> None:
        """Section 1: Anonymize all scenarios with default replace strategy."""
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"])
            self.raw_anon[s["id"]] = result

            detected = set(e.entity_type for e in result.entities)
            expected = set(s.get("expected_pii_types", []))

            entities_dicts = [
                {
                    "entity_type": e.entity_type,
                    "start": e.start,
                    "end": e.end,
                    "score": e.score,
                    "text": e.text,
                }
                for e in result.entities
            ]

            self.anon_results[s["id"]] = ScenarioResult(
                scenario_id=s["id"],
                scenario_name=s["scenario"],
                original_text=s["text"],
                anonymized_text=result.anonymized_text,
                entities_found=entities_dicts,
                expected_types=s.get("expected_pii_types", []),
                matched_types=detected & expected,
                missed_types=expected - detected,
                extra_types=detected - expected,
            )
        self._log("Anonymization", f"{len(self.scenarios)} scenarios tested")

    def run_coverage(self) -> None:
        """Section 2: Compute per-entity-type detection coverage."""
        total_expected: Counter[str] = Counter()
        total_detected: Counter[str] = Counter()
        for s in self.scenarios:
            sr = self.anon_results[s["id"]]
            detected = set(e["entity_type"] for e in sr.entities_found)
            expected = set(sr.expected_types)
            for etype in expected:
                total_expected[etype] += 1
                if etype in detected:
                    total_detected[etype] += 1
        self.coverage = {
            etype: {
                "expected": total_expected[etype],
                "detected": total_detected[etype],
                "rate": (
                    total_detected[etype] / total_expected[etype] * 100
                    if total_expected[etype]
                    else 0
                ),
            }
            for etype in sorted(total_expected)
        }
        total_checks = sum(total_expected.values())
        total_hits = sum(total_detected.values())
        self.coverage["__overall__"] = {
            "checks": total_checks,
            "hits": total_hits,
            "rate": total_hits / total_checks * 100 if total_checks else 0,
        }
        self._log(
            "Coverage",
            f"{total_hits}/{total_checks} checks passed "
            f"({self.coverage['__overall__']['rate']:.0f}%)",
        )

    def run_round_trips(self) -> None:
        """Section 3: Anonymize → deanonymize round-trip with default strategy."""
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"])
            restored = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            self.round_trip_results.append(
                RoundTripResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    original=s["text"],
                    anonymized=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    restored=restored,
                    exact_match=s["text"] == restored,
                )
            )
        exact = sum(1 for r in self.round_trip_results if r.exact_match)
        self._log("Round-Trip", f"{exact}/{len(self.round_trip_results)} exact")

    def run_hash_strategy(self) -> None:
        """Section 4: Hash DL and PHONE, test round-trip."""
        dl_scenarios = [
            s for s in self.scenarios
            if "IN_DRIVING_LICENSE" in s.get("expected_pii_types", [])
        ]
        config = EntityConfig(strategies={"IN_DRIVING_LICENSE": "hash"})
        for s in dl_scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_default = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                hash_mapping=result.hash_mapping,
            )
            self.hash_results.append(
                StrategyResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    strategy="hash(IN_DRIVING_LICENSE)",
                    original_text=s["text"],
                    anonymized_text=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    hash_mapping=result.hash_mapping,
                    encrypt_mapping=result.encrypt_mapping,
                    restored_default=restored_default,
                    restored_full=restored_full,
                    exact_default=s["text"] == restored_default,
                    exact_full=s["text"] == restored_full,
                )
            )
        exact_default = sum(1 for r in self.hash_results if r.exact_default)
        exact_full = sum(1 for r in self.hash_results if r.exact_full)
        self._log(
            "Hash DL",
            f"{exact_default}/{len(self.hash_results)} exact (default), "
            f"{exact_full}/{len(self.hash_results)} exact (with hash restore)",
        )

    def run_encrypt_strategy(self) -> None:
        """Section 5: Encrypt EMAIL_ADDRESS, test round-trip."""
        email_scenarios = [
            s for s in self.scenarios
            if "EMAIL_ADDRESS" in s.get("expected_pii_types", [])
        ]
        config = EntityConfig(strategies={"EMAIL_ADDRESS": "encrypt"})
        for s in email_scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_default = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                encrypt_mapping=result.encrypt_mapping,
            )
            self.encrypt_results.append(
                StrategyResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    strategy="encrypt(EMAIL_ADDRESS)",
                    original_text=s["text"],
                    anonymized_text=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    hash_mapping=result.hash_mapping,
                    encrypt_mapping=result.encrypt_mapping,
                    restored_default=restored_default,
                    restored_full=restored_full,
                    exact_default=s["text"] == restored_default,
                    exact_full=s["text"] == restored_full,
                )
            )
        exact_full = sum(1 for r in self.encrypt_results if r.exact_full)
        self._log(
            "Encrypt Email",
            f"{exact_full}/{len(self.encrypt_results)} exact (with decrypt)",
        )

    def run_hash_phone(self) -> None:
        """Hash PHONE_NUMBER strategy, test round-trip."""
        phone_scenarios = [
            s for s in self.scenarios
            if "PHONE_NUMBER" in s.get("expected_pii_types", [])
        ]
        config = EntityConfig(strategies={"PHONE_NUMBER": "hash"})
        for s in phone_scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_default = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                hash_mapping=result.hash_mapping,
            )
            self.hash_phone_results.append(
                StrategyResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    strategy="hash(PHONE_NUMBER)",
                    original_text=s["text"],
                    anonymized_text=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    hash_mapping=result.hash_mapping,
                    encrypt_mapping=result.encrypt_mapping,
                    restored_default=restored_default,
                    restored_full=restored_full,
                    exact_default=s["text"] == restored_default,
                    exact_full=s["text"] == restored_full,
                )
            )
        exact_full = sum(1 for r in self.hash_phone_results if r.exact_full)
        self._log(
            "Hash Phone",
            f"{exact_full}/{len(self.hash_phone_results)} exact (with hash restore)",
        )

    def run_encrypt_dl(self) -> None:
        """Encrypt IN_DRIVING_LICENSE strategy, test round-trip and non-determinism."""
        dl_scenarios = [
            s for s in self.scenarios
            if "IN_DRIVING_LICENSE" in s.get("expected_pii_types", [])
        ]
        config = EntityConfig(strategies={"IN_DRIVING_LICENSE": "encrypt"})
        for s in dl_scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_default = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                encrypt_mapping=result.encrypt_mapping,
            )
            self.encrypt_dl_results.append(
                StrategyResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    strategy="encrypt(IN_DRIVING_LICENSE)",
                    original_text=s["text"],
                    anonymized_text=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    hash_mapping=result.hash_mapping,
                    encrypt_mapping=result.encrypt_mapping,
                    restored_default=restored_default,
                    restored_full=restored_full,
                    exact_default=s["text"] == restored_default,
                    exact_full=s["text"] == restored_full,
                )
            )
        exact_full = sum(1 for r in self.encrypt_dl_results if r.exact_full)
        self._log(
            "Encrypt DL",
            f"{exact_full}/{len(self.encrypt_dl_results)} exact (with decrypt)",
        )

    def run_pqc_encrypt(self) -> None:
        """PQC encrypt across DL, EMAIL, PHONE — test round-trip and non-determinism."""
        config = EntityConfig(strategies={
            "IN_DRIVING_LICENSE": "encrypt",
            "EMAIL_ADDRESS": "encrypt",
            "PHONE_NUMBER": "encrypt",
        })
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                encrypt_mapping=result.encrypt_mapping,
            )
            self.pqc_encrypt_results.append(
                StrategyResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    strategy="encrypt(DL+EMAIL+PHONE)",
                    original_text=s["text"],
                    anonymized_text=result.anonymized_text,
                    entity_mapping=result.entity_mapping,
                    hash_mapping=result.hash_mapping,
                    encrypt_mapping=result.encrypt_mapping,
                    restored_default=self.engine.deanonymize(
                        result.anonymized_text, result.entity_mapping
                    ),
                    restored_full=restored_full,
                    exact_default=False,
                    exact_full=s["text"] == restored_full,
                )
            )
        exact_full = sum(1 for r in self.pqc_encrypt_results if r.exact_full)
        self._log(
            "PQC Encrypt",
            f"{exact_full}/{len(self.pqc_encrypt_results)} exact (with decrypt)",
        )

    def run_fake_strategy(self) -> None:
        """Fake strategy — format preservation, consistency, round-trip, mixed."""
        import re as stdlib_re

        # Anonymization test
        config = EntityConfig(strategies={
            "PERSON": "fake", "LOCATION": "fake", "PHONE_NUMBER": "fake",
            "EMAIL_ADDRESS": "fake", "IN_AADHAAR": "fake",
        })
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            self.fake_results.append({
                "label": s["scenario"],
                "original_text": s["text"],
                "anonymized_text": result.anonymized_text,
                "entity_mapping": result.entity_mapping,
                "all_hidden": all(
                    pii not in result.anonymized_text
                    for pii in result.entity_mapping.values()
                ),
                "passed": len(result.entity_mapping) > 0 or len(result.entities) == 0,
            })

        # Format validation for specific types
        format_patterns = {
            "IN_AADHAAR": r"\d{4}\s\d{4}\s\d{4}",
            "PHONE_NUMBER": r"[\d\+\-\s]{7,}",
            "EMAIL_ADDRESS": r"[^@]+@[^@]+\.[^@]+",
        }
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            for placeholder, original in result.entity_mapping.items():
                etype = placeholder.strip("{}").rsplit("_", 1)[0]
                if etype in format_patterns:
                    fake_val = placeholder  # The fake value IS in the anonymized text
                    # Find the fake value by looking at what replaced the original
                    for fake_v, orig_v in result.entity_mapping.items():
                        if orig_v == original:
                            # In fake mode, mapping is {fake_value: original_value}
                            self.fake_format_results.append({
                                "label": f"{s['scenario']} — {etype}",
                                "entity_type": etype,
                                "original": original,
                                "fake_value": fake_v,
                                "pattern": format_patterns[etype],
                                "format_valid": bool(stdlib_re.search(
                                    format_patterns[etype], fake_v
                                )),
                                "passed": True,
                            })
                            break

        # Round-trip test
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping
            )
            self.fake_round_trip_results.append({
                "label": s["scenario"],
                "original_text": s["text"],
                "anonymized_text": result.anonymized_text,
                "restored_text": restored,
                "exact": s["text"] == restored,
                "passed": s["text"] == restored,
            })

        passed = sum(1 for r in self.fake_results if r["passed"])
        rt_exact = sum(1 for r in self.fake_round_trip_results if r["passed"])
        self._log(
            "Fake Strategy",
            f"Anon={passed}/{len(self.fake_results)} "
            f"RT={rt_exact}/{len(self.fake_round_trip_results)}",
        )

    def run_mixed_strategy(self) -> None:
        """Mixed strategies per entity type simultaneously."""
        config = EntityConfig(strategies={
            "EMAIL_ADDRESS": "hash",
            "PHONE_NUMBER": "encrypt",
            "IN_DRIVING_LICENSE": "fake",
        })
        dl_scenarios = [
            s for s in self.scenarios
            if any(t in s.get("expected_pii_types", [])
                   for t in ["EMAIL_ADDRESS", "PHONE_NUMBER", "IN_DRIVING_LICENSE"])
        ]
        for s in dl_scenarios:
            result = self.engine.anonymize(s["text"], config=config)
            restored_full = self.engine.deanonymize(
                result.anonymized_text,
                result.entity_mapping,
                hash_mapping=result.hash_mapping,
                encrypt_mapping=result.encrypt_mapping,
            )
            self.mixed_strategy_results.append({
                "label": s["scenario"],
                "original_text": s["text"],
                "anonymized_text": result.anonymized_text,
                "entity_mapping": result.entity_mapping,
                "hash_mapping": result.hash_mapping,
                "encrypt_mapping": result.encrypt_mapping,
                "has_hash": len(result.hash_mapping) > 0,
                "has_encrypt": len(result.encrypt_mapping) > 0,
                "has_fake": len(result.entity_mapping) > 0,
                "exact_full": s["text"] == restored_full,
                "passed": s["text"] == restored_full,
            })
        passed = sum(1 for r in self.mixed_strategy_results if r["passed"])
        self._log(
            "Mixed Strategy",
            f"{passed}/{len(self.mixed_strategy_results)} exact (full restore)",
        )

    def run_llm_sandwich(self) -> None:
        """Anonymize → simulate LLM rewrite → deanonymize."""
        s = self.scenarios[0]
        result = self.engine.anonymize(s["text"])

        # Simulate LLM rewriting the text but preserving placeholders
        anon_text = result.anonymized_text
        llm_response = (
            "Based on the provided information, here is a summary:\n\n"
            + anon_text
            + "\n\nPlease verify the details above."
        )

        restored = self.engine.deanonymize(
            llm_response, result.entity_mapping,
            hash_mapping=result.hash_mapping,
            encrypt_mapping=result.encrypt_mapping,
        )
        placeholders_resolved = "{{" not in restored
        self.llm_sandwich = {
            "scenario": s["scenario"],
            "anonymized": anon_text,
            "llm_response": llm_response,
            "restored": restored,
            "placeholders_resolved": placeholders_resolved,
        }
        self._log(
            "LLM Sandwich",
            f"{'[PASS] placeholders resolved' if placeholders_resolved else '[FAIL] unresolved'}",
        )

    def run_structured_data(self) -> None:
        """Anonymize JSON-structured text, verify structure preservation."""
        structured_scenarios = [
            s for s in self.scenarios
            if s.get("category") == "structured"
        ]
        if not structured_scenarios:
            # Create some inline structured scenarios
            structured_scenarios = [
                {
                    "id": "json-inline",
                    "scenario": "Inline JSON",
                    "text": '{"customer": "Rajesh Kumar", "email": "rajesh@example.com", "phone": "9845012345"}',
                    "expected_pii_types": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER"],
                },
            ]
        for s in structured_scenarios:
            result = self.engine.anonymize(s["text"])
            restored = self.engine.deanonymize(
                result.anonymized_text, result.entity_mapping,
                hash_mapping=result.hash_mapping,
                encrypt_mapping=result.encrypt_mapping,
            )
            exact = s["text"] == restored
            # Check JSON validity
            import json as json_mod
            try:
                json_mod.loads(result.anonymized_text)
                structure_valid = True
            except (json_mod.JSONDecodeError, ValueError):
                structure_valid = False
            self.structured_results.append({
                "label": s["scenario"],
                "original": s["text"],
                "anonymized": result.anonymized_text,
                "restored": restored,
                "exact": exact,
                "structure_valid": structure_valid,
            })
            self.structured_rt.append({
                "label": s["scenario"],
                "exact": exact,
                "structure_valid": structure_valid,
            })
        exact_ct = sum(1 for r in self.structured_results if r["exact"])
        valid_ct = sum(1 for r in self.structured_results if r["structure_valid"])
        self._log(
            "Structured Data",
            f"{exact_ct}/{len(self.structured_results)} exact, "
            f"{valid_ct}/{len(self.structured_results)} structure valid",
        )

    def run_address_indicator(self) -> None:
        """Verify address context words classify building names as ADDRESS not PERSON."""
        test_cases = [
            {
                "label": "Address: Flat + apartment name",
                "text": (
                    "Client Sneha Patil's correspondence address: "
                    "Flat 501, Kumar Pinnacle, Baner, Pune 411045."
                ),
                "must_be_address": ["Kumar Pinnacle"],
                "must_not_be_person": ["Kumar Pinnacle"],
            },
            {
                "label": "Residing at + building name",
                "text": (
                    "Account holder residing at Tower B, Gandhi Heights, "
                    "Sector 15, Noida 201301."
                ),
                "must_be_address": ["Gandhi Heights"],
                "must_not_be_person": ["Gandhi Heights"],
            },
            {
                "label": "Apartment keyword + society name",
                "text": (
                    "Delivery address: Apartment 12A, Rajiv Garden Society, "
                    "Andheri West, Mumbai 400058."
                ),
                "must_be_address": ["Rajiv Garden"],
                "must_not_be_person": ["Rajiv Garden"],
            },
        ]
        for tc in test_cases:
            result = self.engine.anonymize(tc["text"])
            reverse = {v: k for k, v in result.entity_mapping.items()}
            person_ok = True
            address_ok = True
            for name in tc["must_not_be_person"]:
                for original, placeholder in reverse.items():
                    if name.lower() in original.lower() and "PERSON" in placeholder:
                        person_ok = False
            for name in tc["must_be_address"]:
                found = False
                for original, placeholder in reverse.items():
                    if name.lower() in original.lower() and (
                        "ADDRESS" in placeholder or "LOCATION" in placeholder
                    ):
                        found = True
                if not found:
                    address_ok = False
            self.address_indicator_results.append({
                "label": tc["label"],
                "text": tc["text"][:80] + ("…" if len(tc["text"]) > 80 else ""),
                "anonymized": result.anonymized_text[:80] + ("…" if len(result.anonymized_text) > 80 else ""),
                "person_ok": person_ok,
                "address_ok": address_ok,
                "passed": person_ok and address_ok,
            })
        passed = sum(1 for r in self.address_indicator_results if r["passed"])
        self._log(
            "Address Indicator",
            f"{passed}/{len(self.address_indicator_results)} passed",
        )

    def run_geo_coordinates(self) -> None:
        """Detect and anonymize geographic coordinates."""
        geo_scenarios = [
            {"label": "DD pair with GPS context",
             "text": "The GPS coordinates are 28.6139, 77.2090 for the branch.",
             "pii_values": ["28.6139, 77.2090"]},
            {"label": "Labeled latitude and longitude",
             "text": "Site lat: 19.0760 lon: 72.8777 in Mumbai.",
             "pii_values": ["19.0760", "72.8777"]},
            {"label": "Cardinal direction format",
             "text": "Survey point at 28.6139° N, 77.2090° E.",
             "pii_values": ["28.6139"]},
            {"label": "DMS format",
             "text": "Branch located at 28°36'50\"N 77°12'32\"E.",
             "pii_values": ["28°36"]},
            {"label": "Coordinates mixed with other PII",
             "text": "Customer Rajesh Kumar visited GPS coordinates 19.0760, 72.8777.",
             "pii_values": ["19.0760, 72.8777"]},
        ]
        for tc in geo_scenarios:
            result = self.engine.anonymize(tc["text"])
            anon_text = result.anonymized_text
            detected = any(val not in anon_text for val in tc["pii_values"])
            self.geo_coordinate_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "detected": detected,
                "passed": detected,
            })
        # Round-trip test
        rt_text = "GPS coordinates: 28.6139, 77.2090 for the site."
        result = self.engine.anonymize(rt_text)
        restored = self.engine.deanonymize(
            result.anonymized_text, result.entity_mapping
        )
        self.geo_coordinate_results.append({
            "label": "Round-trip (anonymize → deanonymize)",
            "text": rt_text,
            "anonymized": result.anonymized_text,
            "detected": True,
            "passed": restored == rt_text,
        })
        passed = sum(1 for r in self.geo_coordinate_results if r["passed"])
        self._log(
            "Geo-Coordinates",
            f"{passed}/{len(self.geo_coordinate_results)} passed",
        )

    def run_nrp(self) -> None:
        """Test detection of NRP (Nationality/Religious/Political) entities."""
        nrp_scenarios = [
            {"label": "Nationality — Indian",
             "text": "NRE account opening for Indian national Amit Sharma at Contoso Bank.",
             "expected_nrp": ["Indian"]},
            {"label": "Religion — Hindu",
             "text": "Complaint registered by Hindu customer Priya Patel at Contoso Bank.",
             "expected_nrp": ["Hindu"]},
            {"label": "Religion — Muslim",
             "text": "KYC verification for Muslim applicant Fatima Khan at Contoso Bank.",
             "expected_nrp": ["Muslim"]},
            {"label": "Religion — Sikh",
             "text": "Loan application by Sikh customer Gurpreet Singh at Contoso Bank.",
             "expected_nrp": ["Sikh"]},
            {"label": "Mixed — nationality + religion",
             "text": "Indian national and Buddhist devotee Rahul Verma opened an account at Contoso Bank.",
             "expected_nrp": ["Indian", "Buddhist"]},
        ]
        for tc in nrp_scenarios:
            result = self.engine.anonymize(tc["text"])
            anon_text = result.anonymized_text
            all_found = all(nrp not in anon_text for nrp in tc["expected_nrp"])
            self.nrp_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_nrp": tc["expected_nrp"],
                "passed": all_found,
            })
        # Round-trip
        rt_text = "Indian customer Priya Sharma visited Contoso Bank, Mumbai."
        result = self.engine.anonymize(rt_text)
        restored = self.engine.deanonymize(result.anonymized_text, result.entity_mapping)
        self.nrp_results.append({
            "label": "Round-trip (anonymize → deanonymize)",
            "text": rt_text,
            "anonymized": result.anonymized_text,
            "expected_nrp": ["Indian"],
            "passed": restored == rt_text,
        })
        passed = sum(1 for r in self.nrp_results if r["passed"])
        self._log("NRP Detection", f"{passed}/{len(self.nrp_results)} passed")

    def run_ckyc_pran_apaar(self) -> None:
        """Test detection of CKYC, PRAN, and APAAR entity types."""
        scenarios = [
            # ── CKYC scenarios ──
            {
                "label": "CKYC standard 14-digit",
                "text": "As per CERSAI records, the customer's CKYC number 10002345678901 has been verified for the central KYC registry.",
                "expected_type": "IN_CKYC",
                "pii_values": ["10002345678901"],
            },
            {
                "label": "CKYC prefixed (L-simplified)",
                "text": "Simplified measures CKYC account L50098765432101 registered under central KYC compliance for the new savings account holder.",
                "expected_type": "IN_CKYC",
                "pii_values": ["L50098765432101"],
            },
            {
                "label": "CKYC prefixed (S-small account)",
                "text": "Small account CKYC identifier S20001234567890 has been recorded in the central registry by CERSAI.",
                "expected_type": "IN_CKYC",
                "pii_values": ["S20001234567890"],
            },
            {
                "label": "CKYC in banking onboarding",
                "text": "New account onboarding requires CKYC number 30005678901234. Please submit the KYC identifier to proceed with Contoso Bank.",
                "expected_type": "IN_CKYC",
                "pii_values": ["30005678901234"],
            },
            {
                "label": "CKYC alongside Aadhaar and PAN",
                "text": "Customer Rajesh Kumar with Aadhaar 9876 5432 1098 and PAN ABCPK1234L has CKYC identifier 20001234567890 registered with CERSAI.",
                "expected_type": "IN_CKYC",
                "pii_values": ["20001234567890", "9876 5432 1098", "ABCPK1234L"],
            },
            # ── PRAN scenarios ──
            {
                "label": "PRAN with NPS context",
                "text": "NPS subscriber with PRAN 110034567890 has a Tier I pension account managed by PFRDA.",
                "expected_type": "IN_PRAN",
                "pii_values": ["110034567890"],
            },
            {
                "label": "PRAN starting with zero",
                "text": "The employee's NPS PRAN is 012345678901 for the retirement pension fund.",
                "expected_type": "IN_PRAN",
                "pii_values": ["012345678901"],
            },
            {
                "label": "PRAN in pension statement",
                "text": "Annual pension statement for PRAN 098765432101 shows accumulated corpus under National Pension System Tier I and Tier II accounts.",
                "expected_type": "IN_PRAN",
                "pii_values": ["098765432101"],
            },
            {
                "label": "PRAN in retirement planning",
                "text": "Employee Suresh Menon has PRAN 220045678901 linked to the National Pension System. His retirement annuity will be processed by CRA.",
                "expected_type": "IN_PRAN",
                "pii_values": ["220045678901"],
            },
            # ── APAAR scenarios ──
            {
                "label": "APAAR with student context",
                "text": "Student APAAR ID 234567890123 registered in Academic Bank of Credits via DigiLocker.",
                "expected_type": "IN_APAAR",
                "pii_values": ["234567890123"],
            },
            {
                "label": "APAAR with education context",
                "text": "The student's APAAR enrollment number 345678901234 has been linked to the university education portal.",
                "expected_type": "IN_APAAR",
                "pii_values": ["345678901234"],
            },
            {
                "label": "APAAR starting with zero",
                "text": "Student with APAAR ID 045678901234 has been registered in the Academic Bank of Credits via DigiLocker account.",
                "expected_type": "IN_APAAR",
                "pii_values": ["045678901234"],
            },
            {
                "label": "APAAR in school enrollment",
                "text": "School enrollment completed for student with APAAR 567890123456 under the permanent education number scheme.",
                "expected_type": "IN_APAAR",
                "pii_values": ["567890123456"],
            },
            # ── Disambiguation scenarios ──
            {
                "label": "Aadhaar context (not PRAN/APAAR)",
                "text": "Customer Aadhaar number is 987654321098 as per UIDAI records for identity verification.",
                "expected_type": "IN_AADHAAR",
                "pii_values": ["987654321098"],
            },
        ]

        for tc in scenarios:
            result = self.engine.anonymize(tc["text"])
            anon_text = result.anonymized_text
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            has_expected = any(
                tc["expected_type"] in k for k in result.entity_mapping
            )

            self.ckyc_pran_apaar_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_type": tc["expected_type"],
                "all_hidden": all_hidden,
                "has_expected": has_expected,
                "passed": all_hidden and has_expected,
            })

        # Round-trip tests
        rt_scenarios = [
            {
                "label": "Round-trip (CKYC)",
                "text": "Customer CKYC number is 10002345678901 as per CERSAI records.",
                "expected_type": "IN_CKYC",
            },
            {
                "label": "Round-trip (PRAN)",
                "text": "NPS subscriber PRAN 110034567890 linked to pension fund.",
                "expected_type": "IN_PRAN",
            },
            {
                "label": "Round-trip (APAAR)",
                "text": "Student APAAR ID 234567890123 registered in Academic Bank of Credits.",
                "expected_type": "IN_APAAR",
            },
        ]
        for rt in rt_scenarios:
            result = self.engine.anonymize(rt["text"])
            restored = self.engine.deanonymize(result.anonymized_text, result.entity_mapping)
            self.ckyc_pran_apaar_results.append({
                "label": rt["label"],
                "text": rt["text"],
                "anonymized": result.anonymized_text,
                "expected_type": rt["expected_type"],
                "all_hidden": True,
                "has_expected": True,
                "passed": restored == rt["text"],
            })

        passed = sum(1 for r in self.ckyc_pran_apaar_results if r["passed"])
        self._log("CKYC/PRAN/APAAR", f"{passed}/{len(self.ckyc_pran_apaar_results)} passed")

    def run_customer_id(self) -> None:
        """Test detection of banking Customer ID entity type."""
        scenarios = [
            {
                "label": "Customer ID — Net banking login",
                "text": "Please log in to net banking using your Customer ID 103841234 and the OTP sent to your registered mobile.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["103841234"],
            },
            {
                "label": "Customer ID — CustID keyword",
                "text": "Service request SR-89012 raised by CustID 213456780 for debit card replacement has been processed.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["213456780"],
            },
            {
                "label": "Customer ID — CIF keyword",
                "text": "CIF 313456790 flagged for annual review — all accounts under this Customer ID require re-verification.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["313456790"],
            },
            {
                "label": "Customer ID — Welcome kit with account",
                "text": "Welcome kit for Customer ID 617823490: Your new savings account 917020048230123 is now active.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["617823490"],
            },
            {
                "label": "Customer ID — Mobile banking password reset",
                "text": "To reset your mobile banking password, enter your Customer ID 412309876 and date of birth.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["412309876"],
            },
            {
                "label": "Customer ID — Internet banking",
                "text": "Dear customer, your internet banking Customer ID is 209876543. Please do not share this with anyone.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["209876543"],
            },
            {
                "label": "Customer ID — Fraud alert",
                "text": "Suspicious login detected on Customer ID 191234578 from IP 103.45.67.89 at 02:30 AM.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["191234578"],
            },
            {
                "label": "Customer ID — Loan EMI",
                "text": "Home loan EMI auto-debit of INR 42,500 processed for Customer ID 768901245 on 05-Apr-2025.",
                "expected_type": "CUSTOMER_ID",
                "pii_values": ["768901245"],
            },
        ]

        for tc in scenarios:
            result = self.engine.anonymize(tc["text"])
            anon_text = result.anonymized_text
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            has_expected = any(
                tc["expected_type"] in k for k in result.entity_mapping
            )

            self.customer_id_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_type": tc["expected_type"],
                "all_hidden": all_hidden,
                "has_expected": has_expected,
                "passed": all_hidden and has_expected,
            })

        # Round-trip test
        rt_text = "Customer ID 103841234 linked to net banking account."
        result = self.engine.anonymize(rt_text)
        restored = self.engine.deanonymize(result.anonymized_text, result.entity_mapping)
        self.customer_id_results.append({
            "label": "Round-trip (Customer ID)",
            "text": rt_text,
            "anonymized": result.anonymized_text,
            "expected_type": "CUSTOMER_ID",
            "all_hidden": True,
            "has_expected": True,
            "passed": restored == rt_text,
        })

        # ── Combination tests — Customer ID alongside other entities ──
        combo_scenarios = [
            {
                "label": "Combo — Customer ID + Account Number",
                "text": "Customer ID 435678912 is linked to savings account 917020048230123 at our bank.",
                "expected_types": ["CUSTOMER_ID", "IN_BANK_ACCOUNT"],
                "pii_values": ["435678912", "917020048230123"],
            },
            {
                "label": "Combo — Customer ID + Aadhaar + PAN",
                "text": "KYC update pending for Customer ID 435678912. Please visit your nearest branch with original Aadhaar 9876 5432 1098 and PAN ABCPS7234F.",
                "expected_types": ["CUSTOMER_ID", "IN_AADHAAR", "IN_PAN"],
                "pii_values": ["435678912", "ABCPS7234F"],
            },
            {
                "label": "Combo — Customer ID + Phone + Email",
                "text": "Customer ID 209876543 registered with mobile +91 9845012345 and email rajesh.sharma@gmail.com for net banking alerts.",
                "expected_types": ["CUSTOMER_ID", "PHONE_NUMBER", "EMAIL_ADDRESS"],
                "pii_values": ["209876543", "9845012345", "rajesh.sharma@gmail.com"],
            },
            {
                "label": "Combo — Customer ID + IFSC + Account",
                "text": "Customer ID 768901245 has requested a NEFT transfer, debiting account 917020043567890 with IFSC CONT0005678.",
                "expected_types": ["CUSTOMER_ID", "IN_BANK_ACCOUNT", "IN_IFSC"],
                "pii_values": ["768901245", "917020043567890", "CONT0005678"],
            },
            {
                "label": "Combo — Customer ID + DL + PAN",
                "text": "Verify KYC for Customer ID 508734219 — DL: MH 14 2019 0012345, PAN: BKRPD3456J. Update mobile banking profile.",
                "expected_types": ["CUSTOMER_ID", "IN_DRIVING_LICENSE", "IN_PAN"],
                "pii_values": ["508734219", "BKRPD3456J"],
            },
            {
                "label": "Combo — Two Customer IDs + Account",
                "text": "Joint savings account 917020048230123 linked to Customer ID 103841234 (primary) and Customer ID 209876543 (secondary).",
                "expected_types": ["CUSTOMER_ID", "IN_BANK_ACCOUNT"],
                "pii_values": ["103841234", "209876543", "917020048230123"],
            },
            {
                "label": "Combo — Customer ID + UPI + Phone",
                "text": "Link UPI ID rajesh@okaxis to Customer ID 617823490. Registered mobile: +91 9845012345 for internet banking.",
                "expected_types": ["CUSTOMER_ID", "IN_UPI_ID", "PHONE_NUMBER"],
                "pii_values": ["617823490", "rajesh@okaxis", "9845012345"],
            },
            {
                "label": "Combo — Customer ID + Credit Card",
                "text": "Credit card 4532 0151 2345 6789 dispatched to Customer ID 412309876. Activate via mobile banking app.",
                "expected_types": ["CUSTOMER_ID", "CREDIT_CARD"],
                "pii_values": ["412309876"],
            },
        ]

        for tc in combo_scenarios:
            result = self.engine.anonymize(tc["text"])
            anon_text = result.anonymized_text
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            all_types_found = all(
                any(et in k for k in result.entity_mapping)
                for et in tc["expected_types"]
            )

            self.customer_id_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_type": ", ".join(tc["expected_types"]),
                "all_hidden": all_hidden,
                "has_expected": all_types_found,
                "passed": all_hidden and all_types_found,
            })

        # Combination round-trip test
        combo_rt_text = "Transfer initiated by Customer ID 435678912 from account 917020048230123, contact +91 9845012345 for net banking confirmation."
        result = self.engine.anonymize(combo_rt_text)
        restored = self.engine.deanonymize(result.anonymized_text, result.entity_mapping)
        combo_rt_passed = (
            "435678912" in restored
            and "917020048230123" in restored
            and "9845012345" in restored
        )
        self.customer_id_results.append({
            "label": "Combo round-trip (CustID + Account + Phone)",
            "text": combo_rt_text,
            "anonymized": result.anonymized_text,
            "expected_type": "CUSTOMER_ID, IN_BANK_ACCOUNT, PHONE_NUMBER",
            "all_hidden": True,
            "has_expected": True,
            "passed": combo_rt_passed,
        })

        passed = sum(1 for r in self.customer_id_results if r["passed"])
        self._log("Customer ID", f"{passed}/{len(self.customer_id_results)} passed")

    def run_allow_lists(self) -> None:
        """Test entity-type and entity-keyword allow-lists."""
        test_text = (
            "Customer Rajesh Kumar at Contoso Bank, India. "
            "PAN: ABCPS7234F. Email: rajesh@example.com. Mobile: 9845012345."
        )

        # Test 1: entity_type_allow_list excludes ORGANIZATION
        result1 = self.engine.anonymize(
            test_text, entity_type_allow_list={"ORGANIZATION"}
        )
        types1 = {p.strip("{}").rsplit("_", 1)[0] for p in result1.entity_mapping}
        et_org_excluded = "ORGANIZATION" not in types1

        # Test 2: entity_keyword_allow_list preserves "India" as LOCATION
        result2 = self.engine.anonymize(
            test_text, entity_keyword_allow_list={"LOCATION": ["India"]}
        )
        ekw_india_visible = "India" in result2.anonymized_text
        ekw_person_masked = "Rajesh Kumar" not in result2.anonymized_text

        # Test 3: Combined allow-lists
        result3 = self.engine.anonymize(
            test_text,
            entity_type_allow_list={"EMAIL_ADDRESS"},
            entity_keyword_allow_list={"LOCATION": ["India"]},
        )
        combined_email_visible = "rajesh@example.com" in result3.anonymized_text
        combined_india_visible = "India" in result3.anonymized_text
        combined_person_masked = "Rajesh Kumar" not in result3.anonymized_text

        # Test 4: Round-trip with allow-lists
        restored = self.engine.deanonymize(
            result3.anonymized_text, result3.entity_mapping
        )
        rt_exact = restored == test_text

        tests = [
            ("ET: ORGANIZATION excluded", et_org_excluded),
            ("EKW: 'India' visible as LOCATION", ekw_india_visible),
            ("EKW: PERSON still masked", ekw_person_masked),
            ("Combined: EMAIL visible (ET allow)", combined_email_visible),
            ("Combined: India visible (EKW allow)", combined_india_visible),
            ("Combined: PERSON masked", combined_person_masked),
            ("Combined: round-trip exact", rt_exact),
        ]
        self.allow_list_results = {
            "tests": tests,
            "original_text": test_text,
        }
        passed = sum(1 for _, ok in tests if ok)
        self._log("Allow-Lists", f"{passed}/{len(tests)} passed")

    def run_batch_processing(self) -> None:
        """Section 6: Batch-process all scenarios via BatchProcessor."""
        processor = BatchProcessor(
            engine=self.engine, parallelism="thread", max_workers=4
        )
        texts = [s["text"] for s in self.scenarios]
        self.batch_result = processor.anonymize_texts(texts)
        self._log(
            "Batch Processing",
            f"{self.batch_result.succeeded}/{self.batch_result.total} succeeded",
        )

    def run_csv_processing(self) -> None:
        """Section 7: CSV round-trip — write scenarios to CSV, anonymize, read back."""
        processor = BatchProcessor(engine=self.engine, parallelism="none")
        with tempfile.TemporaryDirectory() as tmp:
            csv_in = Path(tmp) / "banking_scenarios.csv"
            csv_out = Path(tmp) / "banking_anonymized.csv"

            with open(csv_in, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["id", "scenario", "text"])
                writer.writeheader()
                for s in self.scenarios:
                    writer.writerow(
                        {"id": s["id"], "scenario": s["scenario"], "text": s["text"]}
                    )

            self.csv_result = processor.anonymize_csv(
                csv_in, csv_out, text_columns=["text"]
            )
            self._log(
                "CSV Processing",
                f"{self.csv_result.succeeded}/{self.csv_result.total} rows",
            )

    def run_pandas_processing(self) -> None:
        """Section 8: Pandas DataFrame — anonymize, inspect, deanonymize."""
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "pandas is required for this section. "
                "Install with: pip install pii-shield[batch]"
            )

        processor = BatchProcessor(engine=self.engine, parallelism="none")

        # 8a. Load scenarios into a DataFrame
        df = pd.DataFrame([
            {"id": s["id"], "scenario": s["scenario"], "text": s["text"]}
            for s in self.scenarios
        ])

        # 8b. Anonymize the 'text' column
        anon_df = processor.anonymize_dataframe(df, text_columns=["text"])

        # 8c. Deanonymize to verify round-trip
        restored_texts = []
        for idx in range(len(anon_df)):
            row_map = anon_df.iloc[idx]["_pii_mappings"]
            entity_map = row_map.get("text", {}).get("entity_mapping", {})
            restored = self.engine.deanonymize(
                anon_df.iloc[idx]["text"], entity_map
            )
            restored_texts.append(restored)
        anon_df["restored_text"] = restored_texts

        # 8d. Compute round-trip accuracy
        exact_matches = sum(
            1 for i in range(len(df))
            if df.iloc[i]["text"] == anon_df.iloc[i]["restored_text"]
        )

        # 8e. Collect entity type stats from mappings column
        entity_counts: Counter = Counter()
        for row_map in anon_df["_pii_mappings"]:
            mapping = row_map.get("text", {}).get("entity_mapping", {})
            for placeholder in mapping:
                # Extract type from placeholder like {{PERSON_1}}
                etype = placeholder.strip("{}").rsplit("_", 1)[0]
                entity_counts[etype] += 1

        self.pandas_result = {
            "rows": len(df),
            "columns_anonymized": ["text"],
            "exact_matches": exact_matches,
            "entity_counts": dict(entity_counts),
            "anon_df": anon_df,
            "original_df": df,
        }

        self._log(
            "Pandas DataFrame",
            f"{exact_matches}/{len(df)} exact round-trips, "
            f"{len(entity_counts)} entity types found",
        )

    def run_mapping_store(self) -> None:
        """Section 9: Persist all mappings to SQLite, verify retrieval."""
        store = SqliteMappingStore(":memory:")
        saved = 0
        restored_exact = 0
        for s in self.scenarios:
            result = self.engine.anonymize(s["text"])
            record_id = store.save(result, record_id=s["id"])
            saved += 1
            retrieved = store.get(record_id)
            if retrieved:
                original = self.engine.deanonymize(
                    retrieved.anonymized_text, retrieved.entity_mapping
                )
                if original == s["text"]:
                    restored_exact += 1
        self.mapping_store_result = {
            "saved": saved,
            "restored_exact": restored_exact,
            "total": len(self.scenarios),
        }
        self._log(
            "Mapping Store",
            f"{restored_exact}/{saved} exact round-trips via SQLite store",
        )

    def run(self) -> None:
        start = time.monotonic()
        print("\n PII Shield — Library Test Suite\n")
        sections = [
            ("Anonymization", self.run_anonymization),
            ("Coverage", self.run_coverage),
            ("Round-Trip", self.run_round_trips),
            # Strategy tests
            ("Hash DL Strategy", self.run_hash_strategy),
            ("Hash Phone Strategy", self.run_hash_phone),
            ("Encrypt Email Strategy", self.run_encrypt_strategy),
            ("Encrypt DL Strategy", self.run_encrypt_dl),
            ("PQC Encrypt", self.run_pqc_encrypt),
            ("Fake Strategy", self.run_fake_strategy),
            ("Mixed Strategy", self.run_mixed_strategy),
            # Feature tests
            ("LLM Sandwich", self.run_llm_sandwich),
            ("Structured Data", self.run_structured_data),
            ("Address Indicator", self.run_address_indicator),
            ("Geo-Coordinates", self.run_geo_coordinates),
            ("NRP Detection", self.run_nrp),
            ("CKYC/PRAN/APAAR", self.run_ckyc_pran_apaar),
            ("Customer ID", self.run_customer_id),
            ("Allow-Lists", self.run_allow_lists),
            # Batch/integration tests
            ("Batch Processing", self.run_batch_processing),
            ("CSV Processing", self.run_csv_processing),
            ("Pandas DataFrame", self.run_pandas_processing),
            ("Mapping Store", self.run_mapping_store),
        ]
        for name, fn in sections:
            try:
                fn()
            except Exception as exc:
                print(f" [FAIL] {name} FAILED: {exc}")
        self.elapsed = time.monotonic() - start
        print(f"\n  Done in {self.elapsed:.1f}s\n")


# ── HTML Report ──────────────────────────────────────────────────────────────

H = html_escape


def _icon(ok: bool) -> str:
    return "[PASS]" if ok else "[FAIL]"


def _icon3(val: float) -> str:
    if val >= 100:
        return "[PASS]"
    if val >= 50:
        return "[WARN]"
    return "[FAIL]"


CSS = """
:root {
    --pass: #d4edda; --pass-border: #28a745;
    --warn: #fff3cd; --warn-border: #ffc107;
    --fail: #f8d7da; --fail-border: #dc3545;
    --bg: #f8f9fa; --card-bg: #fff;
    --text: #212529; --muted: #6c757d;
    --border: #dee2e6; --accent: #0d6efd;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: var(--bg); color: var(--text); line-height: 1.6; padding: 1rem; }
.container { max-width: 1200px; margin: 0 auto; }
h1 { margin-bottom: 0.25rem; }
h2 { margin-top: 2rem; margin-bottom: 1rem; padding-bottom: 0.5rem;
     border-bottom: 2px solid var(--accent); }
h3 { margin: 1rem 0 0.5rem; }
.timestamp { color: var(--muted); margin-bottom: 1.5rem; }
.dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
             gap: 1rem; margin-bottom: 1.5rem; }
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
        padding: 1rem; text-align: center; }
.card-value { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
.card-label { font-size: 0.85rem; color: var(--muted); margin-top: 0.25rem; }
table { width: 100%; border-collapse: collapse; margin: 0.75rem 0; background: var(--card-bg); }
th, td { padding: 0.5rem 0.75rem; border: 1px solid var(--border); text-align: left;
         font-size: 0.9rem; }
th { background: #e9ecef; font-weight: 600; }
tr.pass { background: var(--pass); }
tr.warn { background: var(--warn); }
tr.fail { background: var(--fail); }
.scenario-card { background: var(--card-bg); border: 1px solid var(--border);
                 border-radius: 8px; padding: 1rem; margin: 1rem 0;
                 border-left: 4px solid var(--border); }
.scenario-card.pass { border-left-color: var(--pass-border); }
.scenario-card.warn { border-left-color: var(--warn-border); }
.scenario-card.fail { border-left-color: var(--fail-border); }
.text-block { background: #f1f3f5; border-radius: 4px; padding: 0.75rem;
              margin: 0.5rem 0; font-size: 0.88rem; word-break: break-word;
              overflow-x: auto; }
.text-block pre { white-space: pre-wrap; word-break: break-word; margin: 0;
                  font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.85rem; }
code { background: #e9ecef; padding: 0.1rem 0.35rem; border-radius: 3px;
       font-size: 0.85em; }
.missed { color: #856404; }
.extra { color: var(--muted); }
p { margin: 0.5rem 0; }
details { cursor: pointer; }
details summary { font-weight: 600; padding: 0.5rem 0; }
.config-summary { background: #eef6ff; border: 1px solid #b8d4f0; border-radius: 8px;
                  padding: 0.75rem 1rem; margin: 0.75rem 0; }
.config-summary table { margin-top: 0.5rem; }
@media (max-width: 768px) {
    .dashboard { grid-template-columns: repeat(2, 1fr); }
    table { font-size: 0.8rem; }
    th, td { padding: 0.35rem 0.5rem; }
}
"""


def generate_html_report(runner: LibraryTestRunner, output_path: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = len(runner.scenarios)
    total_entities = sum(
        len(sr.entities_found) for sr in runner.anon_results.values()
    )
    unique_types = sorted(
        set(
            e["entity_type"]
            for sr in runner.anon_results.values()
            for e in sr.entities_found
        )
    )
    exact_rt = sum(1 for r in runner.round_trip_results if r.exact_match)
    overall_cov = runner.coverage.get("__overall__", {})
    hash_full = sum(1 for r in runner.hash_results if r.exact_full)
    encrypt_full = sum(1 for r in runner.encrypt_results if r.exact_full)
    hash_phone_full = sum(1 for r in runner.hash_phone_results if r.exact_full)
    encrypt_dl_full = sum(1 for r in runner.encrypt_dl_results if r.exact_full)
    pqc_full = sum(1 for r in runner.pqc_encrypt_results if r.exact_full)
    fake_passed = sum(1 for r in runner.fake_results if r["passed"])
    fake_rt = sum(1 for r in runner.fake_round_trip_results if r["passed"])
    mixed_passed = sum(1 for r in runner.mixed_strategy_results if r["passed"])
    addr_passed = sum(1 for r in runner.address_indicator_results if r["passed"])
    geo_passed = sum(1 for r in runner.geo_coordinate_results if r["passed"])
    nrp_passed = sum(1 for r in runner.nrp_results if r["passed"])
    cpa_passed = sum(1 for r in runner.ckyc_pran_apaar_results if r["passed"])
    cid_passed = sum(1 for r in runner.customer_id_results if r["passed"])
    al_tests = runner.allow_list_results.get("tests", [])
    al_passed = sum(1 for _, ok in al_tests if ok)
    batch_ok = runner.batch_result.succeeded if runner.batch_result else 0
    pandas_res = runner.pandas_result
    store = runner.mapping_store_result

    parts: list[str] = []

    def w(line: str = "") -> None:
        parts.append(line)

    w("<!DOCTYPE html>")
    w('<html lang="en">')
    w("<head>")
    w('<meta charset="UTF-8">')
    w('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    w("<title>PII Shield — Library Test Report</title>")
    w(f"<style>{CSS}</style>")
    w("</head><body>")
    w('<div class="container">')

    # Header
    w("<h1> PII Shield — Library Test Report</h1>")
    w(f'<p class="timestamp">Generated: {ts} &nbsp;|&nbsp; '
      f"Duration: {runner.elapsed:.1f}s &nbsp;|&nbsp; "
      f"Scenarios: {total} &nbsp;|&nbsp; Mode: <strong>In-process library</strong></p>")

    # ── Summary Dashboard ────────────────────────────────────────────────
    w('<h2 id="summary"> Summary Dashboard</h2>')
    w('<div class="dashboard">')
    for label, value in [
        ("Scenarios", str(total)),
        ("Entities Detected", str(total_entities)),
        ("Entity Types", str(len(unique_types))),
        (
            "Coverage",
            f"{overall_cov.get('hits', 0)}/{overall_cov.get('checks', 0)} "
            f"({overall_cov.get('rate', 0):.0f}%)",
        ),
        ("Exact Round-Trips", f"{exact_rt}/{total}"),
        ("Hash DL RT", f"{hash_full}/{len(runner.hash_results)}"),
        ("Hash Phone RT", f"{hash_phone_full}/{len(runner.hash_phone_results)}"),
        ("Encrypt Email RT", f"{encrypt_full}/{len(runner.encrypt_results)}"),
        ("Encrypt DL RT", f"{encrypt_dl_full}/{len(runner.encrypt_dl_results)}"),
        ("PQC Encrypt RT", f"{pqc_full}/{len(runner.pqc_encrypt_results)}"),
        ("Fake Strategy", f"{fake_passed}/{len(runner.fake_results)}"),
        ("Mixed Strategy", f"{mixed_passed}/{len(runner.mixed_strategy_results)}"),
        ("Address Indicator", f"{addr_passed}/{len(runner.address_indicator_results)}"),
        ("Geo-Coordinates", f"{geo_passed}/{len(runner.geo_coordinate_results)}"),
        ("NRP Detection", f"{nrp_passed}/{len(runner.nrp_results)}"),
        ("CKYC/PRAN/APAAR", f"{cpa_passed}/{len(runner.ckyc_pran_apaar_results)}"),
        ("Customer ID", f"{cid_passed}/{len(runner.customer_id_results)}"),
        ("Allow-Lists", f"{al_passed}/{len(al_tests)}"),
        ("Batch Processing", f"{batch_ok}/{total}"),
        (
            "Pandas DataFrame RT",
            f"{pandas_res.get('exact_matches', 0)}/{pandas_res.get('rows', 0)}",
        ),
        (
            "Mapping Store RT",
            f"{store.get('restored_exact', 0)}/{store.get('total', 0)}",
        ),
    ]:
        w(f'<div class="card"><div class="card-value">{value}</div>'
          f'<div class="card-label">{label}</div></div>')
    w("</div>")

    # ── 1. Replace Strategy — Anonymization ──────────────────────────────
    w('<h2 id="replace-strategy">1. Replace Strategy (Default)</h2>')
    w("<p>Default anonymization: detected PII is replaced with type-tagged "
      "placeholders. Fully reversible via de-anonymization.</p>")

    w('<h3 id="anonymization">1a. Anonymization Results</h3>')
    for s in runner.scenarios:
        sr = runner.anon_results[s["id"]]
        matched = len(sr.matched_types)
        expected_count = len(sr.expected_types)
        status = (
            "pass" if matched == expected_count
            else "warn" if matched > 0
            else "fail"
        )
        icon = _icon(matched == expected_count) if matched == expected_count else "[WARN]" if matched > 0 else "[FAIL]"
        w(f'<details class="scenario-card {status}">')
        w(f"<summary>{H(sr.scenario_name)} &nbsp; {icon} "
          f"{matched}/{expected_count} types</summary>")
        w(f'<div class="text-block"><strong>Original:</strong><br>'
          f"{H(sr.original_text)}</div>")
        w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
          f"{H(sr.anonymized_text)}</div>")

        if sr.entities_found:
            w("<table><thead><tr><th>Entity Type</th><th>Value</th>"
              "<th>Score</th></tr></thead><tbody>")
            for e in sorted(sr.entities_found, key=lambda x: x["start"]):
                w(f"<tr><td><code>{H(e['entity_type'])}</code></td>"
                  f"<td>{H(e['text'])}</td><td>{e['score']:.2f}</td></tr>")
            w("</tbody></table>")

        w(f"<p><strong>Coverage:</strong> {matched}/{expected_count} expected types</p>")
        if sr.missed_types:
            w(f'<p class="missed">[WARN] Missed: '
              f'{", ".join(f"<code>{H(m)}</code>" for m in sorted(sr.missed_types))}</p>')
        if sr.extra_types:
            w(f'<p class="extra">[INFO] Bonus: '
              f'{", ".join(f"<code>{H(e)}</code>" for e in sorted(sr.extra_types))}</p>')
        w("</details>")

    # ── 1b. Detection Coverage ───────────────────────────────────────────
    w('<h3 id="coverage">1b. Detection Coverage by Entity Type</h3>')
    w("<table><thead><tr><th>Entity Type</th><th>Expected In</th>"
      "<th>Detected In</th><th>Hit Rate</th></tr></thead><tbody>")
    for etype, stats in runner.coverage.items():
        if etype == "__overall__":
            continue
        rate = stats["rate"]
        w(f"<tr><td><code>{H(etype)}</code></td>"
          f"<td>{stats['expected']} scenarios</td>"
          f"<td>{stats['detected']} scenarios</td>"
          f"<td>{_icon3(rate)} {rate:.0f}%</td></tr>")
    w("</tbody></table>")
    w(f"<p><strong>Overall: {overall_cov.get('hits', 0)}/{overall_cov.get('checks', 0)} "
      f"entity-scenario checks passed ({overall_cov.get('rate', 0):.0f}%)</strong></p>")

    # ── 1c. Round-Trip Summary ───────────────────────────────────────────
    w('<h3 id="roundtrip">1c. Anonymize → De-anonymize Round-Trip</h3>')
    w("<table><thead><tr><th>#</th><th>Scenario</th><th>Exact Match</th>"
      "<th>Status</th></tr></thead><tbody>")
    for i, r in enumerate(runner.round_trip_results, 1):
        cls = "pass" if r.exact_match else "fail"
        status_txt = "[PASS] Perfect" if r.exact_match else "[FAIL] Mismatch"
        w(f'<tr class="{cls}"><td>{i}</td><td>{H(r.scenario_name)}</td>'
          f"<td>{_icon(r.exact_match)}</td>"
          f"<td>{status_txt}</td></tr>")
    w("</tbody></table>")
    w(f"<p><strong>{exact_rt}/{total} exact round-trips</strong></p>")

    # ── 1d. Detailed Round-Trip ──────────────────────────────────────────
    w('<h3 id="roundtrip-detail">1d. Detailed Round-Trip Inspection</h3>')
    for r in runner.round_trip_results:
        cls = "pass" if r.exact_match else "fail"
        w(f'<details class="scenario-card {cls}">')
        w(f"<summary>{H(r.scenario_name)} &nbsp; {_icon(r.exact_match)}</summary>")
        if r.entity_mapping:
            w("<table><thead><tr><th>Placeholder</th><th>Original Value</th>"
              "</tr></thead><tbody>")
            for ph, orig in r.entity_mapping.items():
                w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
            w("</tbody></table>")
        w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
          f"{H(r.anonymized)}</div>")
        w(f'<div class="text-block"><strong>Restored:</strong><br>'
          f"{H(r.restored)}</div>")
        if r.exact_match:
            w("<p>[PASS] <strong>Exact match with original</strong></p>")
        else:
            w("<p>[FAIL] <strong>Mismatch — see original below:</strong></p>")
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f"{H(r.original)}</div>")
        w("</details>")

    # ── 2. Hash Strategy ─────────────────────────────────────────────────
    w('<h2 id="hash-strategy">2. Hash Strategy</h2>')
    w("<p>Hashing replaces PII with a one-way SHA3-256 digest. "
      "The original cannot be recovered unless <code>hash_mapping</code> is "
      "passed back to <code>deanonymize()</code>.</p>")
    w('<div class="config-summary"><strong> Configuration</strong>'
      '<table><thead><tr><th>Entity Type</th><th>Strategy</th>'
      "</tr></thead><tbody>"
      "<tr><td><code>IN_DRIVING_LICENSE</code></td>"
      "<td><code>hash</code></td></tr>"
      "</tbody></table></div>")

    _render_strategy_section(w, runner.hash_results, "Hash DL")

    # ── 3. Encrypt Strategy ──────────────────────────────────────────────
    w('<h2 id="encrypt-strategy">3. Encrypt Strategy</h2>')
    w("<p>Encryption replaces PII with a ciphertext token (PQC ML-KEM-768 "
      "or Fernet). Reversible only when <code>encrypt_mapping</code> is "
      "passed back.</p>")
    w('<div class="config-summary"><strong> Configuration</strong>'
      '<table><thead><tr><th>Entity Type</th><th>Strategy</th>'
      "</tr></thead><tbody>"
      "<tr><td><code>EMAIL_ADDRESS</code></td>"
      "<td><code>encrypt</code></td></tr>"
      "</tbody></table></div>")

    _render_strategy_section(w, runner.encrypt_results, "Encrypt Email")

    # ── 4. Hash Phone Strategy ───────────────────────────────────────────
    w('<h2 id="hash-phone">4. Hash Phone Strategy</h2>')
    w("<p>Hashing <code>PHONE_NUMBER</code> with SHA3-256.</p>")
    w('<div class="config-summary"><strong> Configuration</strong>'
      '<table><thead><tr><th>Entity Type</th><th>Strategy</th>'
      "</tr></thead><tbody>"
      "<tr><td><code>PHONE_NUMBER</code></td>"
      "<td><code>hash</code></td></tr>"
      "</tbody></table></div>")
    _render_strategy_section(w, runner.hash_phone_results, "Hash Phone")

    # ── 5. Encrypt DL Strategy ───────────────────────────────────────────
    w('<h2 id="encrypt-dl">5. Encrypt DL Strategy</h2>')
    w("<p>Encrypting <code>IN_DRIVING_LICENSE</code> with PQC/Fernet.</p>")
    w('<div class="config-summary"><strong> Configuration</strong>'
      '<table><thead><tr><th>Entity Type</th><th>Strategy</th>'
      "</tr></thead><tbody>"
      "<tr><td><code>IN_DRIVING_LICENSE</code></td>"
      "<td><code>encrypt</code></td></tr>"
      "</tbody></table></div>")
    _render_strategy_section(w, runner.encrypt_dl_results, "Encrypt DL")

    # ── 6. PQC Encrypt Strategy ──────────────────────────────────────────
    w('<h2 id="pqc-encrypt">6. PQC Encrypt (Multi-Entity)</h2>')
    w("<p>Encrypting <code>IN_DRIVING_LICENSE</code>, <code>EMAIL_ADDRESS</code>, "
      "and <code>PHONE_NUMBER</code> simultaneously with PQC backend.</p>")
    w('<div class="config-summary"><strong> Configuration</strong>'
      '<table><thead><tr><th>Entity Type</th><th>Strategy</th>'
      "</tr></thead><tbody>"
      "<tr><td><code>IN_DRIVING_LICENSE</code></td><td><code>encrypt</code></td></tr>"
      "<tr><td><code>EMAIL_ADDRESS</code></td><td><code>encrypt</code></td></tr>"
      "<tr><td><code>PHONE_NUMBER</code></td><td><code>encrypt</code></td></tr>"
      "</tbody></table></div>")
    _render_strategy_section(w, runner.pqc_encrypt_results, "PQC Encrypt")

    # ── 7. Fake Strategy ─────────────────────────────────────────────────
    w('<h2 id="fake-strategy">7. Fake Strategy</h2>')
    w("<p>Replaces PII with format-preserving fake values. Tests anonymization, "
      "format validity, and round-trip restoration.</p>")
    if runner.fake_results:
        w(f"<p><strong>{fake_passed}/{len(runner.fake_results)}</strong> "
          f"anonymization tests passed.</p>")
        w(f"<p><strong>{fake_rt}/{len(runner.fake_round_trip_results)}</strong> "
          f"round-trip tests passed.</p>")
        w("<h3>Anonymization Results</h3>")
        w("<table><thead><tr><th>Scenario</th><th>All PII Hidden?</th>"
          "<th>Result</th></tr></thead><tbody>")
        for r in runner.fake_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
    if runner.fake_round_trip_results:
        w("<h3>Round-Trip Results</h3>")
        w("<table><thead><tr><th>Scenario</th><th>Exact Match</th>"
          "</tr></thead><tbody>")
        for r in runner.fake_round_trip_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

    # ── 8. Mixed Strategy ────────────────────────────────────────────────
    w('<h2 id="mixed-strategy">8. Mixed Strategy</h2>')
    w("<p>Different strategies per entity type simultaneously: "
      "<code>EMAIL_ADDRESS: hash</code>, <code>PHONE_NUMBER: encrypt</code>, "
      "<code>IN_DRIVING_LICENSE: fake</code>.</p>")
    if runner.mixed_strategy_results:
        w(f"<p><strong>{mixed_passed}/{len(runner.mixed_strategy_results)}</strong> "
          f"exact full restores.</p>")
        w("<table><thead><tr><th>Scenario</th><th>Has Hash</th>"
          "<th>Has Encrypt</th><th>Has Fake</th><th>Exact (full)</th>"
          "</tr></thead><tbody>")
        for r in runner.mixed_strategy_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["has_hash"])}</td>'
              f'<td>{_icon(r["has_encrypt"])}</td>'
              f'<td>{_icon(r["has_fake"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

    # ── 9. LLM Sandwich ──────────────────────────────────────────────────
    w('<h2 id="llm-sandwich">9. LLM Sandwich Pattern</h2>')
    llm = runner.llm_sandwich
    if llm:
        resolved = llm.get("placeholders_resolved", False)
        w(f'<p>{_icon(resolved)} <strong>'
          f'{"All placeholders resolved" if resolved else "Unresolved placeholders remain"}'
          f'</strong></p>')
        w('<details><summary>Show detailed output</summary>')
        w(f'<div class="scenario-card {"pass" if resolved else "fail"}">')
        w(f"<h3>{H(llm.get('scenario', ''))}</h3>")
        w(f'<div class="text-block"><strong>Anonymized input:</strong><br>'
          f"{H(llm.get('anonymized', ''))}</div>")
        w(f'<div class="text-block"><strong>Simulated LLM response:</strong><br>'
          f"{H(llm.get('llm_response', ''))}</div>")
        w(f'<div class="text-block"><strong>De-anonymized output:</strong><br>'
          f"{H(llm.get('restored', ''))}</div>")
        w("</div></details>")

    # ── 10. Structured Data ──────────────────────────────────────────────
    w('<h2 id="structured-data">10. Structured Data (JSON)</h2>')
    w("<p>Tests anonymization of JSON-formatted text and structure preservation.</p>")
    if runner.structured_results:
        exact_ct = sum(1 for r in runner.structured_results if r["exact"])
        valid_ct = sum(1 for r in runner.structured_results if r["structure_valid"])
        w(f"<p><strong>{exact_ct}/{len(runner.structured_results)}</strong> exact round-trips, "
          f"<strong>{valid_ct}/{len(runner.structured_results)}</strong> structure valid.</p>")
        w("<table><thead><tr><th>Scenario</th><th>Structure Valid</th>"
          "<th>Exact RT</th><th>Details</th></tr></thead><tbody>")
        for r in runner.structured_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["structure_valid"])}</td>'
              f'<td>{_icon(r["exact"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["original"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 11. Address Indicator ────────────────────────────────────────────
    w('<h2 id="address-indicator">11. Address Indicator Recognition</h2>')
    w("<p>Verifies that address indicator words cause building names "
      "to be recognized as ADDRESS (not PERSON).</p>")
    if runner.address_indicator_results:
        w(f"<p><strong>{addr_passed}/{len(runner.address_indicator_results)}</strong> "
          f"tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Not PERSON?</th>"
          "<th>Is ADDRESS?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.address_indicator_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["person_ok"])}</td>'
              f'<td>{_icon(r["address_ok"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

    # ── 12. Geo-Coordinate Detection ─────────────────────────────────────
    w('<h2 id="geo-coordinates">12. Geo-Coordinate Detection</h2>')
    w("<p>Verifies detection of geographic coordinates in DD, labeled, "
      "cardinal, and DMS formats.</p>")
    if runner.geo_coordinate_results:
        w(f"<p><strong>{geo_passed}/{len(runner.geo_coordinate_results)}</strong> "
          f"tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Detected?</th>"
          "<th>Result</th><th>Details</th></tr></thead><tbody>")
        for r in runner.geo_coordinate_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["detected"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["text"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 13. NRP Detection ────────────────────────────────────────────────
    w('<h2 id="nrp-detection">13. NRP Detection (Nationality / Religion / Political)</h2>')
    w("<p>Verifies detection of NRP entities — nationalities, religious groups, "
      "and political affiliations.</p>")
    if runner.nrp_results:
        w(f"<p><strong>{nrp_passed}/{len(runner.nrp_results)}</strong> "
          f"NRP tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Expected NRP</th>"
          "<th>Result</th><th>Details</th></tr></thead><tbody>")
        for r in runner.nrp_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{", ".join(r["expected_nrp"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["text"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 14. CKYC/PRAN/APAAR Detection ────────────────────────────────────
    w('<h2 id="ckyc-pran-apaar">14. CKYC / PRAN / APAAR Detection</h2>')
    w("<p>Verifies detection of Indian CKYC, PRAN (NPS), and APAAR (student ID) entities, "
      "including disambiguation from Aadhaar.</p>")
    if runner.ckyc_pran_apaar_results:
        w(f"<p><strong>{cpa_passed}/{len(runner.ckyc_pran_apaar_results)}</strong> "
          f"CKYC/PRAN/APAAR tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Expected Type</th>"
          "<th>All PII Hidden?</th><th>Correct Type?</th><th>Result</th><th>Details</th></tr></thead><tbody>")
        for r in runner.ckyc_pran_apaar_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><code>{H(r["expected_type"])}</code></td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["has_expected"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["text"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 15. Customer ID Detection ─────────────────────────────────────────
    w('<h2 id="customer-id">15. Customer ID Detection</h2>')
    w("<p>Verifies detection of banking Customer ID (9-digit) entities with "
      "context-based scoring.</p>")
    if runner.customer_id_results:
        w(f"<p><strong>{cid_passed}/{len(runner.customer_id_results)}</strong> "
          f"Customer ID tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Expected Type</th>"
          "<th>All PII Hidden?</th><th>Correct Type?</th><th>Result</th><th>Details</th></tr></thead><tbody>")
        for r in runner.customer_id_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><code>{H(r["expected_type"])}</code></td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["has_expected"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["text"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 16. Allow-Lists ──────────────────────────────────────────────────
    w('<h2 id="allow-lists">16. Allow-List Tests</h2>')
    w("<p>Tests entity-type and entity-keyword allow-lists passed "
      "directly to <code>engine.anonymize()</code>.</p>")
    if al_tests:
        w(f"<p><strong>{al_passed}/{len(al_tests)}</strong> checks passed.</p>")
        w("<table><thead><tr><th>Test</th><th>Result</th></tr></thead><tbody>")
        for label, ok in al_tests:
            cls = "" if ok else ' class="fail"'
            w(f'<tr{cls}><td>{H(label)}</td><td>{_icon(ok)}</td></tr>')
        w("</tbody></table>")

    # ── 17. Batch Processing ─────────────────────────────────────────────
    w('<h2 id="batch">17. Batch Processing</h2>')
    w("<p>All scenarios processed via <code>BatchProcessor</code> with "
      "thread-based parallelism (4 workers).</p>")
    if runner.batch_result:
        br = runner.batch_result
        w("<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
        w(f"<tr><td>Total</td><td>{br.total}</td></tr>")
        w(f"<tr><td>Succeeded</td><td>{br.succeeded}</td></tr>")
        w(f"<tr><td>Failed</td><td>{br.failed}</td></tr>")
        w("</tbody></table>")
        if br.results:
            w("<h3>Batch Results</h3>")
            for i, (s, r) in enumerate(zip(runner.scenarios, br.results)):
                anon_preview = r.anonymized_text[:120] + ("…" if len(r.anonymized_text) > 120 else "")
                w(f'<details class="scenario-card pass">')
                w(f"<summary>{H(s['scenario'])}</summary>")
                w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
                  f"{H(r.anonymized_text)}</div>")
                n_entities = len(r.entity_mapping)
                n_hash = len(r.hash_mapping) if r.hash_mapping else 0
                n_encrypt = len(r.encrypt_mapping) if r.encrypt_mapping else 0
                w(f"<p>Mappings: {n_entities} replace"
                  + (f", {n_hash} hash" if n_hash else "")
                  + (f", {n_encrypt} encrypt" if n_encrypt else "")
                  + "</p>")
                w("</details>")

    # ── 5. CSV Processing ────────────────────────────────────────────────
    w('<h2 id="csv">17. CSV Processing</h2>')
    w("<p>Scenarios written to CSV, anonymized row-by-row via "
      "<code>BatchProcessor.anonymize_csv()</code>.</p>")
    if runner.csv_result:
        cr = runner.csv_result
        cls = "pass" if cr.failed == 0 else "warn"
        w(f"<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
        w(f'<tr class="{cls}"><td>Rows Processed</td><td>{cr.succeeded}/{cr.total}</td></tr>')
        w(f"<tr><td>Failed</td><td>{cr.failed}</td></tr>")
        w("</tbody></table>")

    # ── 6. Pandas DataFrame Processing ───────────────────────────────────
    w('<h2 id="pandas">18. Pandas DataFrame Processing</h2>')
    w("<p>Demonstrates using <code>BatchProcessor.anonymize_dataframe()</code> "
      "to anonymize columns in a pandas DataFrame, then deanonymize using the "
      "<code>_pii_mappings</code> column for full round-trip verification.</p>")

    if pandas_res:
        pr_rows = pandas_res.get("rows", 0)
        pr_exact = pandas_res.get("exact_matches", 0)
        pr_ecounts = pandas_res.get("entity_counts", {})
        anon_df = pandas_res.get("anon_df")
        orig_df = pandas_res.get("original_df")

        # Summary table
        rt_cls = "pass" if pr_exact == pr_rows else "warn"
        w("<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
        w(f"<tr><td>DataFrame Rows</td><td>{pr_rows}</td></tr>")
        w(f"<tr><td>Columns Anonymized</td>"
          f"<td><code>{', '.join(pandas_res.get('columns_anonymized', []))}</code></td></tr>")
        w(f'<tr class="{rt_cls}"><td>Exact Round-Trips</td>'
          f"<td>{pr_exact}/{pr_rows}</td></tr>")
        w(f"<tr><td>Entity Types Found</td><td>{len(pr_ecounts)}</td></tr>")
        w("</tbody></table>")

        # Entity type breakdown
        if pr_ecounts:
            w("<h3>Entity Types Detected in DataFrame</h3>")
            w("<table><thead><tr><th>Entity Type</th><th>Occurrences</th>"
              "</tr></thead><tbody>")
            for etype in sorted(pr_ecounts, key=pr_ecounts.get, reverse=True):
                w(f"<tr><td><code>{H(etype)}</code></td>"
                  f"<td>{pr_ecounts[etype]}</td></tr>")
            w("</tbody></table>")

        # Per-row details
        if anon_df is not None and orig_df is not None:
            w("<h3>Per-Row Anonymization & Round-Trip</h3>")
            for i in range(len(orig_df)):
                orig_text = orig_df.iloc[i]["text"]
                anon_text = anon_df.iloc[i]["text"]
                rest_text = anon_df.iloc[i]["restored_text"]
                scenario = orig_df.iloc[i]["scenario"]
                exact = (orig_text == rest_text)
                cls = "pass" if exact else "fail"
                row_map = anon_df.iloc[i]["_pii_mappings"]
                entity_map = row_map.get("text", {}).get("entity_mapping", {})

                w(f'<details class="scenario-card {cls}">')
                w(f"<summary>{H(scenario)} &nbsp; {_icon(exact)}</summary>")
                w(f'<div class="text-block"><strong>Original:</strong><br>'
                  f"{H(orig_text)}</div>")
                w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
                  f"{H(anon_text)}</div>")
                w(f'<div class="text-block"><strong>Restored:</strong><br>'
                  f"{H(rest_text)}</div>")
                if entity_map:
                    w("<table><thead><tr><th>Placeholder</th><th>Original</th>"
                      "</tr></thead><tbody>")
                    for ph, orig in entity_map.items():
                        w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
                    w("</tbody></table>")
                if exact:
                    w("<p>[PASS] <strong>Exact match with original</strong></p>")
                else:
                    w("<p>[FAIL] <strong>Mismatch — check diff above</strong></p>")
                w("</details>")

    # ── 7. Mapping Store ─────────────────────────────────────────────────
    w('<h2 id="mapping-store">19. SQLite Mapping Store</h2>')
    w("<p>All anonymization results persisted to an in-memory SQLite store, "
      "then retrieved and de-anonymized.</p>")
    if store:
        cls = "pass" if store["restored_exact"] == store["total"] else "warn"
        w(f"<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
        w(f"<tr><td>Saved</td><td>{store['saved']}</td></tr>")
        w(f'<tr class="{cls}"><td>Exact Round-Trips</td>'
          f"<td>{store['restored_exact']}/{store['total']}</td></tr>")
        w("</tbody></table>")

    # Footer
    w("<hr>")
    w(f'<p class="timestamp">Report generated by '
      f"<code>examples/library_usage.py</code> using <code>pii_shield</code> "
      f"library (in-process, no HTTP API).</p>")
    w("</div></body></html>")

    # Write file
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")


def _render_strategy_section(
    w, results: list[StrategyResult], title: str
) -> None:
    """Render a hash/encrypt strategy results section."""
    if not results:
        w("<p><em>No applicable scenarios.</em></p>")
        return

    exact_default = sum(1 for r in results if r.exact_default)
    exact_full = sum(1 for r in results if r.exact_full)
    n = len(results)

    w(f"<h3>{title} — Round-Trip Summary</h3>")
    w("<table><thead><tr><th>#</th><th>Scenario</th>"
      "<th>Default De-anon</th><th>Full De-anon</th>"
      "</tr></thead><tbody>")
    for i, r in enumerate(results, 1):
        cls_d = "pass" if r.exact_default else "warn"
        cls_f = "pass" if r.exact_full else "fail"
        w(f"<tr><td>{i}</td><td>{H(r.scenario_name)}</td>"
          f'<td class="{cls_d}">{_icon(r.exact_default)} '
          f'{"Exact" if r.exact_default else "Inexact (expected)"}</td>'
          f'<td class="{cls_f}">{_icon(r.exact_full)} '
          f'{"Exact" if r.exact_full else "Mismatch"}</td></tr>')
    w("</tbody></table>")
    w(f"<p><strong>Default restore: {exact_default}/{n} exact &nbsp;|&nbsp; "
      f"Full restore: {exact_full}/{n} exact</strong></p>")

    w(f"<h3>{title} — Detailed Inspection</h3>")
    for r in results:
        cls = "pass" if r.exact_full else "warn"
        w(f'<details class="scenario-card {cls}">')
        w(f"<summary>{H(r.scenario_name)} &nbsp; {_icon(r.exact_full)}</summary>")
        w(f'<div class="text-block"><strong>Original:</strong><br>'
          f"{H(r.original_text)}</div>")
        w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
          f"{H(r.anonymized_text)}</div>")
        w(f'<div class="text-block"><strong>Restored (full):</strong><br>'
          f"{H(r.restored_full)}</div>")
        if r.entity_mapping:
            w("<table><thead><tr><th>Placeholder</th><th>Original</th>"
              "</tr></thead><tbody>")
            for ph, orig in r.entity_mapping.items():
                w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
            w("</tbody></table>")
        if r.hash_mapping:
            w("<p><strong>Hash mappings:</strong></p>")
            w("<table><thead><tr><th>Hash</th><th>Original</th>"
              "</tr></thead><tbody>")
            for h, orig in r.hash_mapping.items():
                short = h[:16] + "…"
                w(f"<tr><td><code>{H(short)}</code></td><td>{H(orig)}</td></tr>")
            w("</tbody></table>")
        if r.encrypt_mapping:
            w("<p><strong>Encrypt mappings:</strong></p>")
            w("<table><thead><tr><th>Token (truncated)</th><th>Original</th>"
              "</tr></thead><tbody>")
            for tok, orig in r.encrypt_mapping.items():
                short = tok[:24] + "…"
                w(f"<tr><td><code>{H(short)}</code></td><td>{H(orig)}</td></tr>")
            w("</tbody></table>")
        if r.exact_full:
            w("<p>[PASS] <strong>Exact match with original</strong></p>")
        else:
            w("<p>[FAIL] <strong>Mismatch — check diff above</strong></p>")
        w("</details>")


# ── CLI Entry Point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PII Shield — Library Test Suite (Indian Banking Scenarios)"
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"HTML report output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    # Locate test data
    data_path = Path("indian_banking_test_data.json")
    if not data_path.exists():
        data_path = Path("examples/indian_banking_test_data.json")
    if not data_path.exists():
        data_path = Path(__file__).parent / "indian_banking_test_data.json"
    if not data_path.exists():
        print("[FAIL] Cannot find indian_banking_test_data.json")
        sys.exit(1)

    with open(data_path) as f:
        scenarios = json.load(f)
    print(f"  Loaded {len(scenarios)} test scenarios from {data_path}")

    # Create engine once
    engine = PiiShieldEngine()

    # Run tests
    runner = LibraryTestRunner(engine, scenarios)
    runner.run()

    # Generate report
    generate_html_report(runner, args.output)
    abs_path = Path(args.output).resolve()
    print(f" HTML report written to: {abs_path}\n")


if __name__ == "__main__":
    main()
