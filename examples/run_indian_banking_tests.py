#!/usr/bin/env python3
"""
PII Shield — Indian Banking Test Suite (CLI + HTML Report)

Standalone Python script equivalent of indian_banking_tests.ipynb.
Runs all test scenarios against a live PII Shield API and generates
a detailed HTML report.

Prerequisites:
    1. Services must be running: docker compose up -d
    2. pip install requests  (usually already available)

Usage:
    python examples/run_indian_banking_tests.py [--api-url URL] [--output FILE]

Defaults:
    --api-url   http://localhost:8000
    --output    examples/__results/test_report.html
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path
from typing import Any

import re

import requests

# ── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_OUTPUT = "examples/__results/test_report.html"

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
    has_hashed_entities: bool


@dataclass
class StructuredRTResult:
    scenario_id: str
    scenario_name: str
    original: str
    anonymized: str
    restored: str
    exact_match: bool
    structure_valid: bool


@dataclass
class AppRoundTripRow:
    scenario_name: str
    has_dl: bool
    retail_exact: bool
    audit_exact: bool


@dataclass
class EdgeCaseResult:
    name: str
    expected_status: int
    actual_status: int
    passed: bool


# ── API Helpers ──────────────────────────────────────────────────────────────


class PIIShieldClient:
    """Thin wrapper around PII Shield HTTP API."""

    def __init__(self, base_url: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, app_id: str | None = None) -> dict:
        return {"X-App-Id": app_id} if app_id else {}

    # ── App management ───────────────────────────────────────────────────

    def list_apps(self) -> list[dict]:
        return requests.get(f"{self.base_url}/apps", timeout=self.timeout).json()

    def register_app(self, name: str) -> dict:
        resp = requests.post(
            f"{self.base_url}/apps", json={"app_name": name}, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def get_app(self, app_id: str) -> dict:
        return requests.get(
            f"{self.base_url}/apps/{app_id}", timeout=self.timeout
        ).json()

    def update_app_config(
        self, app_id: str, entity_type: str, strategy: str
    ) -> dict:
        resp = requests.put(
            f"{self.base_url}/apps/{app_id}/config",
            json={"entity_type": entity_type, "strategy": strategy},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_app(self, app_id: str) -> None:
        requests.delete(
            f"{self.base_url}/apps/{app_id}", timeout=self.timeout
        )

    # ── Anonymization ────────────────────────────────────────────────────

    def anonymize(
        self, text: str, language: str = "en", app_id: str | None = None
    ) -> dict:
        """Call /anonymize_unique and build a synthetic entities_found list."""
        resp = requests.post(
            f"{self.base_url}/anonymize_unique",
            json={"text": text, "language": language},
            headers=self._headers(app_id),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        entities_found: list[dict] = []
        for placeholder, original in data.get("entity_mapping", {}).items():
            entity_type = placeholder.strip("{}").rsplit("_", 1)[0]
            start = text.find(original)
            if start >= 0:
                entities_found.append(
                    {
                        "entity_type": entity_type,
                        "start": start,
                        "end": start + len(original),
                        "score": 1.0,
                    }
                )
        for _hash, original in data.get("hash_mapping", {}).items():
            start = text.find(original)
            if start >= 0:
                entities_found.append(
                    {
                        "entity_type": "(hashed)",
                        "start": start,
                        "end": start + len(original),
                        "score": 1.0,
                    }
                )
        for _token, original in data.get("encrypt_mapping", {}).items():
            start = text.find(original)
            if start >= 0:
                entities_found.append(
                    {
                        "entity_type": "(encrypted)",
                        "start": start,
                        "end": start + len(original),
                        "score": 1.0,
                    }
                )
        data["entities_found"] = entities_found
        return data

    def anonymize_unique(
        self, text: str, language: str = "en", app_id: str | None = None
    ) -> dict:
        resp = requests.post(
            f"{self.base_url}/anonymize_unique",
            json={"text": text, "language": language},
            headers=self._headers(app_id),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def deanonymize(
        self,
        session_id: str,
        text: str,
        app_id: str | None = None,
        include_hashed: bool = False,
        include_encrypted: bool = False,
    ) -> dict:
        resp = requests.post(
            f"{self.base_url}/deanonymize",
            json={
                "id": session_id,
                "text": text,
                "include_hashed": include_hashed,
                "include_encrypted": include_encrypted,
            },
            headers=self._headers(app_id),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def raw_post(
        self, path: str, json_body: dict, headers: dict | None = None
    ) -> requests.Response:
        return requests.post(
            f"{self.base_url}{path}",
            json=json_body,
            headers=headers or {},
            timeout=self.timeout,
        )

    # ── Allow-lists ──────────────────────────────────────────────────────

    def set_entity_type_allow_list(self, app_id: str, types: list[str]) -> dict:
        resp = requests.put(
            f"{self.base_url}/apps/{app_id}/entity-type-allow-list",
            json={"entity_type_allow_list": types},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_entity_type_allow_list(self, app_id: str) -> list[str]:
        resp = requests.get(
            f"{self.base_url}/apps/{app_id}/entity-type-allow-list",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("entity_type_allow_list", [])

    def set_entity_keyword_allow_list(self, app_id: str, ekw: dict[str, list[str]]) -> dict:
        resp = requests.put(
            f"{self.base_url}/apps/{app_id}/entity-keyword-allow-list",
            json={"entity_keyword_allow_list": ekw},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_entity_keyword_allow_list(self, app_id: str) -> dict[str, list[str]]:
        resp = requests.get(
            f"{self.base_url}/apps/{app_id}/entity-keyword-allow-list",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("entity_keyword_allow_list", {})

    # ── Connectivity check ───────────────────────────────────────────────

    def ensure_app(self, app_name: str) -> str:
        """Return app_id for *app_name*, creating it if needed."""
        existing = self.list_apps()
        matches = [a for a in existing if a["app_name"] == app_name]
        if matches:
            return matches[0]["app_id"]
        return self.register_app(app_name)["app_id"]


# ── Test Runner ──────────────────────────────────────────────────────────────


class TestRunner:
    """Orchestrates all test sections and collects results."""

    def __init__(self, client: PIIShieldClient, scenarios: list[dict]):
        self.client = client
        self.scenarios = scenarios

        # Results populated by run()
        self.app_id: str = ""
        self.anon_results: dict[str, ScenarioResult] = {}
        self.raw_anon_results: dict[str, dict] = {}
        self.coverage: dict[str, dict] = {}
        self.round_trip_results: list[RoundTripResult] = []
        self.hash_dl_results: list[dict] = []
        self.encrypt_dl_results: dict = {}
        self.encrypt_no_flag: list[dict] = []
        self.encrypt_with_flag: list[dict] = []
        self.encrypt_nondeterministic: dict = {}
        self.app_round_trip: list[AppRoundTripRow] = []
        self.edge_cases: list[EdgeCaseResult] = []
        self.llm_sandwich: dict = {}
        self.structured_results: dict[str, ScenarioResult] = {}
        self.structured_rt: list[StructuredRTResult] = []
        self.retail_id: str = ""
        self.audit_id: str = ""
        self.allow_list_results: dict = {}
        self.elapsed: float = 0.0

        # Strategy-specific test results
        self.encrypt_email_results: list[dict] = []
        self.encrypt_email_no_flag: list[dict] = []
        self.encrypt_email_with_flag: list[dict] = []
        self.hash_phone_results: list[dict] = []
        self.hash_phone_no_flag: list[dict] = []
        self.hash_phone_with_flag: list[dict] = []
        self.mixed_strategy_results: list[dict] = []
        self.address_indicator_results: list[dict] = []
        self.case_robustness_results: list[dict] = []
        self.address_completeness_results: list[dict] = []
        self.account_phone_results: list[dict] = []
        self.person_initials_results: list[dict] = []
        self.person_titles_results: list[dict] = []
        self.nrp_alignment_results: list[dict] = []
        self.multiline_results: list[dict] = []
        self.mixed_case_results: list[dict] = []
        self.address_units_results: list[dict] = []
        self.key_value_results: list[dict] = []
        self.geo_coordinate_results: list[dict] = []
        self.nrp_results: list[dict] = []
        self.us_entity_results: list[dict] = []
        self.ckyc_pran_apaar_results: list[dict] = []
        self.customer_id_results: list[dict] = []
        self.pqc_encrypt_results: list[dict] = []
        self.pqc_encrypt_no_flag: list[dict] = []
        self.pqc_encrypt_with_flag: list[dict] = []
        self.pqc_encrypt_nondeterministic: dict = {}
        self.compliance_id: str = ""
        self.support_id: str = ""

        # Detailed inspection data per strategy
        self.hash_dl_detail: list[dict] = []
        self.hash_phone_detail: list[dict] = []
        self.encrypt_dl_detail: list[dict] = []
        self.encrypt_email_detail: list[dict] = []
        self.pqc_encrypt_detail: list[dict] = []

        # Fake strategy test results
        self.fake_strategy_results: list[dict] = []
        self.fake_format_results: list[dict] = []
        self.fake_consistency_results: list[dict] = []
        self.fake_round_trip_results: list[dict] = []
        self.fake_mixed_results: list[dict] = []

    # ── Helpers ──────────────────────────────────────────────────────────

    def _log(self, section: str, msg: str = "") -> None:
        tag = f"[{section}]"
        print(f"  {tag:<40} {msg}")

    def _build_scenario_result(
        self, scenario: dict, raw: dict
    ) -> ScenarioResult:
        entities = raw.get("entities_found", [])
        detected = set(e["entity_type"] for e in entities)
        expected = set(scenario.get("expected_pii_types", []))
        return ScenarioResult(
            scenario_id=scenario["id"],
            scenario_name=scenario["scenario"],
            original_text=scenario["text"],
            anonymized_text=raw["anonymized_text"],
            entities_found=entities,
            expected_types=scenario.get("expected_pii_types", []),
            matched_types=detected & expected,
            missed_types=expected - detected,
            extra_types=detected - expected,
        )

    def _check_expectations(self, cases: list[tuple]) -> list[dict]:
        """Anonymize each case and check it against its expectations.

        A case is ``(label, text, expected, must_mask, must_keep)``:
        ``expected`` maps a value to its entity type, ``must_mask`` lists text
        that must not survive in the output, and ``must_keep`` text that must.
        """
        def entity_type_of(mapping: dict, value: str) -> str:
            # Prefer the entity that is exactly the value, else one containing it.
            for exact in (True, False):
                for placeholder, original in mapping.items():
                    if (original == value) if exact else (value in original):
                        return placeholder.strip("{}").rsplit("_", 1)[0]
            return "(not detected)"

        def show(value: str) -> str:
            return value.replace("\n", " ⏎ ")

        results = []
        for label, text, expected, must_mask, must_keep in cases:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            output = anon["anonymized_text"]
            actual = {value: entity_type_of(mapping, value) for value in expected}
            leaked = [s for s in must_mask if s in output]
            lost = [s for s in must_keep if s not in output]
            restored = self.client.deanonymize(anon["id"], output)
            correct_type = actual == expected
            round_trip = restored["text"] == text
            shown = show(text)
            results.append({
                "label": label,
                "text": shown[:90] + ("…" if len(shown) > 90 else ""),
                "expected": "; ".join(f"{show(v)} → {t}" for v, t in expected.items()),
                "actual": "; ".join(f"{show(v)} → {t}" for v, t in actual.items()),
                "leaked": ", ".join(leaked) or "—",
                "must_keep": ", ".join(repr(s) for s in must_keep) or "—",
                "full_text": text,
                "full_anonymized": output,
                "mapping": mapping,
                "restored": restored["text"],
                "no_leak": not leaked,
                "correct_type": correct_type,
                "kept": not lost,
                "round_trip": round_trip,
                "passed": not leaked and correct_type and not lost and round_trip,
            })
        return results

    # ── Section runners ──────────────────────────────────────────────────

    def run_setup(self) -> None:
        self.app_id = self.client.ensure_app("indian-banking-tests")
        self._log("Setup", f"app_id={self.app_id}")

    def run_anonymization(self) -> None:
        for s in self.scenarios:
            raw = self.client.anonymize(s["text"], app_id=self.app_id)
            self.raw_anon_results[s["id"]] = raw
            self.anon_results[s["id"]] = self._build_scenario_result(s, raw)
        self._log("Anonymization", f"{len(self.scenarios)} scenarios tested")

    def run_coverage(self) -> None:
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
        for s in self.scenarios:
            anon = self.client.anonymize_unique(s["text"], app_id=self.app_id)
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=self.app_id
            )
            has_hashed = any(
                e["entity_type"] == "IN_DRIVING_LICENSE"
                for e in self.raw_anon_results[s["id"]]["entities_found"]
            )
            self.round_trip_results.append(
                RoundTripResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    original=s["text"],
                    anonymized=anon["anonymized_text"],
                    entity_mapping=anon["entity_mapping"],
                    restored=restored["text"],
                    exact_match=s["text"] == restored["text"],
                    has_hashed_entities=has_hashed,
                )
            )
        exact = sum(1 for r in self.round_trip_results if r.exact_match)
        self._log(
            "Round-Trip",
            f"{exact}/{len(self.round_trip_results)} exact matches",
        )

    def run_hash_dl(self) -> None:
        hash_dl_app_id = self.client.ensure_app("__testHashDLTestApp")
        self.client.update_app_config(
            hash_dl_app_id, "IN_DRIVING_LICENSE", "hash"
        )
        dl_scenarios = [
            s
            for s in self.scenarios
            if "IN_DRIVING_LICENSE" in s["expected_pii_types"]
        ]
        for s in dl_scenarios:
            anon = self.client.anonymize_unique(
                s["text"], app_id=hash_dl_app_id
            )
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=hash_dl_app_id
            )
            dl_placeholders = [
                f"{k} → {v}"
                for k, v in anon["entity_mapping"].items()
                if "DRIVING_LICENSE" in k
            ]
            dl_in_entity = any(
                "DRIVING_LICENSE" in k for k in anon["entity_mapping"]
            )
            self.hash_dl_results.append(
                {
                    "scenario": s["scenario"],
                    "description": s["text"][:100] + ("…" if len(s["text"]) > 100 else ""),
                    "dl_hashed": not dl_in_entity,
                    "exact": s["text"] == restored["text"],
                    "dl_info": (
                        ", ".join(dl_placeholders)
                        or "(hashed — not in mapping)"
                    ),
                }
            )
            self.hash_dl_detail.append(
                {
                    "scenario": s["scenario"],
                    "original_text": s["text"],
                    "anonymized_text": anon["anonymized_text"],
                    "restored_text": restored["text"],
                    "entity_mapping": anon.get("entity_mapping", {}),
                    "hash_mapping": anon.get("hash_mapping", {}),
                    "dl_hashed": not dl_in_entity,
                }
            )
        self._log("Hash DL", f"{len(dl_scenarios)} DL scenarios tested")

    def run_encrypt_dl(self) -> None:
        encrypt_dl_app_id = self.client.ensure_app("__testEncryptDLTestApp")
        self.client.update_app_config(
            encrypt_dl_app_id, "IN_DRIVING_LICENSE", "encrypt"
        )
        dl_scenarios = [
            s
            for s in self.scenarios
            if "IN_DRIVING_LICENSE" in s["expected_pii_types"]
        ]
        encrypt_results: dict[str, dict] = {}

        # Anonymize
        anon_rows = []
        for s in dl_scenarios:
            anon = self.client.anonymize_unique(
                s["text"], app_id=encrypt_dl_app_id
            )
            encrypt_results[s["id"]] = anon
            dl_in_mapping = any(
                "DRIVING_LICENSE" in k for k in anon["entity_mapping"]
            )
            encrypt_count = len(anon.get("encrypt_mapping", {}))
            anon_rows.append(
                {
                    "scenario": s["scenario"],
                    "dl_in_mapping": dl_in_mapping,
                    "encrypt_count": encrypt_count,
                }
            )
        self.encrypt_dl_results = {
            "anon_rows": anon_rows,
            "encrypt_results": encrypt_results,
        }

        # De-anonymize without include_encrypted
        for s in dl_scenarios:
            anon = encrypt_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=encrypt_dl_app_id
            )
            cipher_tokens = list(anon.get("encrypt_mapping", {}).keys())
            cipher_present = any(t in restored["text"] for t in cipher_tokens)
            originals = list(anon.get("encrypt_mapping", {}).values())
            dl_restored = any(o in restored["text"] for o in originals)
            self.encrypt_no_flag.append(
                {
                    "scenario": s["scenario"],
                    "dl_restored": dl_restored,
                    "cipher_present": cipher_present,
                }
            )

        # De-anonymize with include_encrypted=True
        for s in dl_scenarios:
            anon = encrypt_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"],
                anon["anonymized_text"],
                app_id=encrypt_dl_app_id,
                include_encrypted=True,
            )
            originals = list(anon.get("encrypt_mapping", {}).values())
            dl_restored = any(o in restored["text"] for o in originals)
            self.encrypt_with_flag.append(
                {
                    "scenario": s["scenario"],
                    "dl_restored": dl_restored,
                    "exact": s["text"] == restored["text"],
                }
            )
            self.encrypt_dl_detail.append(
                {
                    "scenario": s["scenario"],
                    "original_text": s["text"],
                    "anonymized_text": anon["anonymized_text"],
                    "restored_text": restored["text"],
                    "entity_mapping": anon.get("entity_mapping", {}),
                    "encrypt_mapping": anon.get("encrypt_mapping", {}),
                    "exact": s["text"] == restored["text"],
                }
            )

        # Non-determinism check
        s = dl_scenarios[0]
        anon1 = self.client.anonymize_unique(
            s["text"], app_id=encrypt_dl_app_id
        )
        anon2 = self.client.anonymize_unique(
            s["text"], app_id=encrypt_dl_app_id
        )
        tokens1 = set(anon1.get("encrypt_mapping", {}).keys())
        tokens2 = set(anon2.get("encrypt_mapping", {}).keys())
        self.encrypt_nondeterministic = {
            "tokens_found": bool(tokens1 and tokens2),
            "different": not (tokens1 & tokens2) if tokens1 and tokens2 else False,
        }

        self._log("Encrypt DL", f"{len(dl_scenarios)} DL scenarios tested")

    def run_multi_tenant(self) -> None:
        self.retail_id = self.client.ensure_app("__testRetailBankingApp")
        self.audit_id = self.client.ensure_app("__testInternalAuditApp")
        self.client.update_app_config(
            self.audit_id, "IN_DRIVING_LICENSE", "hash"
        )

        for s in self.scenarios:
            has_dl = "IN_DRIVING_LICENSE" in s["expected_pii_types"]
            retail_exact = False
            audit_exact = False
            for label, app_id in [
                ("retail", self.retail_id),
                ("audit", self.audit_id),
            ]:
                anon = self.client.anonymize_unique(
                    s["text"], app_id=app_id
                )
                restored = self.client.deanonymize(
                    anon["id"], anon["anonymized_text"], app_id=app_id
                )
                match = s["text"] == restored["text"]
                if label == "retail":
                    retail_exact = match
                else:
                    audit_exact = match
            self.app_round_trip.append(
                AppRoundTripRow(
                    scenario_name=s["scenario"],
                    has_dl=has_dl,
                    retail_exact=retail_exact,
                    audit_exact=audit_exact,
                )
            )

        retail_ok = sum(1 for r in self.app_round_trip if r.retail_exact)
        audit_ok = sum(1 for r in self.app_round_trip if r.audit_exact)
        self._log(
            "Multi-Tenant",
            f"Retail={retail_ok}/{len(self.scenarios)} "
            f"Audit={audit_ok}/{len(self.scenarios)}",
        )

    def run_edge_cases(self) -> None:
        sample = self.scenarios[0]["text"]

        # Invalid app_id → 404
        resp = self.client.raw_post(
            "/anonymize_unique",
            {"text": sample, "language": "en"},
            headers={"X-App-Id": "nonexistent-app-id"},
        )
        self.edge_cases.append(
            EdgeCaseResult(
                "Invalid app_id on /anonymize_unique", 404, resp.status_code,
                resp.status_code == 404,
            )
        )

        # No app_id → 200
        resp = self.client.raw_post(
            "/anonymize_unique", {"text": sample, "language": "en"}
        )
        self.edge_cases.append(
            EdgeCaseResult(
                "No app_id (default config)", 200, resp.status_code,
                resp.status_code == 200,
            )
        )

        # Invalid app_id on /deanonymize → 404
        anon = self.client.anonymize_unique(sample)
        resp = self.client.raw_post(
            "/deanonymize",
            {"id": anon["id"], "text": anon["anonymized_text"]},
            headers={"X-App-Id": "nonexistent-app-id"},
        )
        self.edge_cases.append(
            EdgeCaseResult(
                "Invalid app_id on /deanonymize", 404, resp.status_code,
                resp.status_code == 404,
            )
        )

        passed = sum(1 for e in self.edge_cases if e.passed)
        self._log(
            "Edge Cases",
            f"{passed}/{len(self.edge_cases)} passed",
        )

    def run_llm_sandwich(self) -> None:
        scenario = next(
            s for s in self.scenarios if s["id"] == "credit-card-dispute"
        )
        anon = self.client.anonymize_unique(
            scenario["text"], app_id=self.retail_id
        )
        placeholders = list(anon["entity_mapping"].keys())
        person_ph = next((p for p in placeholders if "PERSON" in p), "Customer")
        location_ph = next(
            (p for p in placeholders if "LOCATION" in p or "ADDRESS" in p), "home"
        )
        email_ph = next(
            (p for p in placeholders if "EMAIL" in p), "their email"
        )

        llm_response = (
            f"DISPUTE SUMMARY: {person_ph} has raised a dispute for an "
            f"unauthorized transaction. The cardholder confirmed being at "
            f"their residence in {location_ph} during the transaction window. "
            f"Please contact the customer at {email_ph} for further "
            f"verification. Action: Block card immediately and initiate "
            f"chargeback process."
        )

        restored = self.client.deanonymize(
            anon["id"], llm_response, app_id=self.retail_id
        )

        # Check that no placeholders remain in restored text
        has_placeholders = "{{" in restored["text"] and "}}" in restored["text"]

        self.llm_sandwich = {
            "scenario": scenario["scenario"],
            "anonymized": anon["anonymized_text"],
            "llm_response": llm_response,
            "restored": restored["text"],
            "placeholders_resolved": not has_placeholders,
        }
        self._log(
            "LLM Sandwich",
            "✅ placeholders resolved"
            if not has_placeholders
            else "⚠️ unresolved placeholders",
        )

    def run_structured_data(self) -> None:
        structured_scenarios = [
            s for s in self.scenarios if s["id"].startswith("json-")
        ]
        for s in structured_scenarios:
            raw = self.client.anonymize(s["text"], app_id=self.app_id)
            self.structured_results[s["id"]] = self._build_scenario_result(
                s, raw
            )

        for s in structured_scenarios:
            anon = self.client.anonymize_unique(
                s["text"], app_id=self.app_id
            )
            restored = self.client.deanonymize(
                anon["id"],
                anon["anonymized_text"],
                app_id=self.app_id,
                include_hashed=True,
                include_encrypted=True,
            )
            restored_text = restored["text"]
            is_exact = s["text"] == restored_text
            try:
                json.loads(restored_text)
                structure_valid = True
            except json.JSONDecodeError:
                structure_valid = False

            self.structured_rt.append(
                StructuredRTResult(
                    scenario_id=s["id"],
                    scenario_name=s["scenario"],
                    original=s["text"],
                    anonymized=anon["anonymized_text"],
                    restored=restored_text,
                    exact_match=is_exact,
                    structure_valid=structure_valid,
                )
            )

        exact = sum(1 for r in self.structured_rt if r.exact_match)
        valid = sum(1 for r in self.structured_rt if r.structure_valid)
        self._log(
            "Structured Data",
            f"{exact}/{len(self.structured_rt)} exact, "
            f"{valid}/{len(self.structured_rt)} structurally valid",
        )

    def run_encrypt_email(self) -> None:
        """Test EMAIL_ADDRESS with encrypt strategy."""
        encrypt_email_app_id = self.client.ensure_app("__testEncryptEmailTestApp")
        self.client.update_app_config(
            encrypt_email_app_id, "EMAIL_ADDRESS", "encrypt"
        )
        email_scenarios = [
            s
            for s in self.scenarios
            if "EMAIL_ADDRESS" in s.get("expected_pii_types", [])
        ]
        encrypt_results: dict[str, dict] = {}

        # Anonymize — email should appear in encrypt_mapping
        for s in email_scenarios:
            anon = self.client.anonymize_unique(
                s["text"], app_id=encrypt_email_app_id
            )
            encrypt_results[s["id"]] = anon
            email_in_entity = any(
                "EMAIL" in k for k in anon["entity_mapping"]
            )
            encrypt_count = len(anon.get("encrypt_mapping", {}))
            self.encrypt_email_results.append(
                {
                    "scenario": s["scenario"],
                    "email_in_entity_mapping": email_in_entity,
                    "encrypt_count": encrypt_count,
                }
            )

        # De-anonymize without include_encrypted — email stays ciphertext
        for s in email_scenarios:
            anon = encrypt_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=encrypt_email_app_id
            )
            originals = list(anon.get("encrypt_mapping", {}).values())
            email_restored = any(o in restored["text"] for o in originals)
            cipher_tokens = list(anon.get("encrypt_mapping", {}).keys())
            cipher_present = any(t in restored["text"] for t in cipher_tokens)
            self.encrypt_email_no_flag.append(
                {
                    "scenario": s["scenario"],
                    "email_restored": email_restored,
                    "cipher_present": cipher_present,
                }
            )

        # De-anonymize with include_encrypted=True — email restored
        for s in email_scenarios:
            anon = encrypt_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"],
                anon["anonymized_text"],
                app_id=encrypt_email_app_id,
                include_encrypted=True,
            )
            originals = list(anon.get("encrypt_mapping", {}).values())
            email_restored = any(o in restored["text"] for o in originals)
            self.encrypt_email_with_flag.append(
                {
                    "scenario": s["scenario"],
                    "email_restored": email_restored,
                    "exact": s["text"] == restored["text"],
                }
            )
            self.encrypt_email_detail.append(
                {
                    "scenario": s["scenario"],
                    "original_text": s["text"],
                    "anonymized_text": anon["anonymized_text"],
                    "restored_text": restored["text"],
                    "entity_mapping": anon.get("entity_mapping", {}),
                    "encrypt_mapping": anon.get("encrypt_mapping", {}),
                    "exact": s["text"] == restored["text"],
                }
            )

        self._log(
            "Encrypt Email",
            f"{len(email_scenarios)} email scenarios tested",
        )

    def run_hash_phone(self) -> None:
        """Test PHONE_NUMBER with hash strategy."""
        hash_phone_app_id = self.client.ensure_app("__testHashPhoneTestApp")
        self.client.update_app_config(
            hash_phone_app_id, "PHONE_NUMBER", "hash"
        )
        phone_scenarios = [
            s
            for s in self.scenarios
            if "PHONE_NUMBER" in s.get("expected_pii_types", [])
        ]
        hash_results: dict[str, dict] = {}

        # Anonymize — phone should appear in hash_mapping
        for s in phone_scenarios:
            anon = self.client.anonymize_unique(
                s["text"], app_id=hash_phone_app_id
            )
            hash_results[s["id"]] = anon
            phone_in_entity = any(
                "PHONE" in k for k in anon["entity_mapping"]
            )
            hash_count = len(anon.get("hash_mapping", {}))
            self.hash_phone_results.append(
                {
                    "scenario": s["scenario"],
                    "description": s["text"][:100] + ("…" if len(s["text"]) > 100 else ""),
                    "phone_in_entity_mapping": phone_in_entity,
                    "hash_count": hash_count,
                }
            )

        # De-anonymize without include_hashed — phone stays hashed
        for s in phone_scenarios:
            anon = hash_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=hash_phone_app_id
            )
            originals = list(anon.get("hash_mapping", {}).values())
            phone_restored = any(o in restored["text"] for o in originals)
            self.hash_phone_no_flag.append(
                {
                    "scenario": s["scenario"],
                    "phone_restored": phone_restored,
                }
            )

        # De-anonymize with include_hashed=True — phone restored
        for s in phone_scenarios:
            anon = hash_results[s["id"]]
            restored = self.client.deanonymize(
                anon["id"],
                anon["anonymized_text"],
                app_id=hash_phone_app_id,
                include_hashed=True,
            )
            originals = list(anon.get("hash_mapping", {}).values())
            phone_restored = any(o in restored["text"] for o in originals)
            self.hash_phone_with_flag.append(
                {
                    "scenario": s["scenario"],
                    "phone_restored": phone_restored,
                    "exact": s["text"] == restored["text"],
                }
            )
            self.hash_phone_detail.append(
                {
                    "scenario": s["scenario"],
                    "original_text": s["text"],
                    "anonymized_text": anon["anonymized_text"],
                    "restored_text": restored["text"],
                    "entity_mapping": anon.get("entity_mapping", {}),
                    "hash_mapping": anon.get("hash_mapping", {}),
                    "exact": s["text"] == restored["text"],
                }
            )

        self._log(
            "Hash Phone",
            f"{len(phone_scenarios)} phone scenarios tested",
        )

    def run_mixed_strategy_apps(self) -> None:
        """Test multi-tenant apps with mixed per-entity strategies.

        ComplianceApp:       EMAIL=encrypt, PHONE=hash, DL=hash
        CustomerSupportApp:  All defaults (replace)
        """
        self.compliance_id = self.client.ensure_app("__testComplianceApp")
        self.support_id = self.client.ensure_app("__testCustomerSupportApp")

        # Configure ComplianceApp
        self.client.update_app_config(
            self.compliance_id, "EMAIL_ADDRESS", "encrypt"
        )
        self.client.update_app_config(
            self.compliance_id, "PHONE_NUMBER", "hash"
        )
        self.client.update_app_config(
            self.compliance_id, "IN_DRIVING_LICENSE", "hash"
        )

        for s in self.scenarios:
            has_email = "EMAIL_ADDRESS" in s.get("expected_pii_types", [])
            has_phone = "PHONE_NUMBER" in s.get("expected_pii_types", [])
            has_dl = "IN_DRIVING_LICENSE" in s.get("expected_pii_types", [])

            row: dict[str, Any] = {
                "scenario": s["scenario"],
                "has_email": has_email,
                "has_phone": has_phone,
                "has_dl": has_dl,
            }

            for label, app_id in [
                ("compliance", self.compliance_id),
                ("support", self.support_id),
            ]:
                anon = self.client.anonymize_unique(
                    s["text"], app_id=app_id
                )
                # Default RT (no include flags)
                restored_default = self.client.deanonymize(
                    anon["id"], anon["anonymized_text"], app_id=app_id
                )
                # Full RT (include both)
                restored_full = self.client.deanonymize(
                    anon["id"],
                    anon["anonymized_text"],
                    app_id=app_id,
                    include_hashed=True,
                    include_encrypted=True,
                )
                row[f"{label}_default_exact"] = (
                    s["text"] == restored_default["text"]
                )
                row[f"{label}_full_exact"] = (
                    s["text"] == restored_full["text"]
                )

            self.mixed_strategy_results.append(row)

        comp_default = sum(
            1 for r in self.mixed_strategy_results
            if r["compliance_default_exact"]
        )
        comp_full = sum(
            1 for r in self.mixed_strategy_results
            if r["compliance_full_exact"]
        )
        supp_default = sum(
            1 for r in self.mixed_strategy_results
            if r["support_default_exact"]
        )
        self._log(
            "Mixed-Strategy Apps",
            f"Compliance default={comp_default} full={comp_full} "
            f"| Support={supp_default}/{len(self.scenarios)}",
        )

    def run_address_indicator(self) -> None:
        """Test that address indicator words (Address:, Flat, residing at, etc.)
        cause apartment/building names to be recognized as ADDRESS instead of PERSON."""
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
            {
                "label": "Full demat scenario from test data",
                "text": next(
                    s["text"] for s in self.scenarios
                    if s["id"] == "demat-account"
                ),
                "must_be_address": ["Kumar Pinnacle"],
                "must_not_be_person": ["Kumar Pinnacle"],
            },
        ]

        for tc in test_cases:
            anon = self.client.anonymize_unique(tc["text"])
            mapping = anon["entity_mapping"]
            reverse = {v: k for k, v in mapping.items()}

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
                "text": tc["text"][:80] + "…",
                "anonymized": anon["anonymized_text"][:80] + "…",
                "person_ok": person_ok,
                "address_ok": address_ok,
                "passed": person_ok and address_ok,
            })

        passed = sum(1 for r in self.address_indicator_results if r["passed"])
        self._log(
            "Address Indicator",
            f"{passed}/{len(self.address_indicator_results)} passed",
        )

    def run_case_robustness(self) -> None:
        """Test that PII is detected regardless of how the text is cased.

        Cased NER models (dslim/bert-base-NER and its ONNX derivatives) rely on
        capitalisation, so ALL-CAPS names come back mislabelled ORGANIZATION or
        truncated, and all-lowercase names are missed outright — a silent leak.
        Each name is sent in Title, UPPER and lower casing and must be found as
        PERSON every time, with the round-trip restoring the original casing.

        Also guards the two precision risks of case recovery: case-sensitive
        identifiers (PAN, IFSC) must survive, and ordinary prose must not gain
        spurious entities from re-casing.
        """
        name_cases = [
            ("Three-part name", "Rajesh Kumar Sharma", "{} residing at Kanakia Zen World has some concerns"),
            ("Two-part name", "Sanjay Gupta", "Cheque issued by {} bounced due to insufficient funds"),
            ("South Indian name", "Priya Venkatesan", "Applicant {} signed the form at the Mumbai branch"),
            ("Name before PAN", "Anil Deshmukh", "The account holder is {} and the PAN is ABCPS1234K"),
            ("Name in request", "Meera Nair", "Kindly update the records for {} before Friday"),
        ]

        for label, name, template in name_cases:
            for casing, transform in (
                ("Title", lambda s: s),
                ("UPPER", str.upper),
                ("lower", str.lower),
            ):
                text = transform(template.format(name))
                anon = self.client.anonymize_unique(text)
                mapping = anon["entity_mapping"]

                persons = " ".join(
                    v.lower() for k, v in mapping.items() if "PERSON" in k
                ).strip()
                expected = transform(name).lower()
                detected = bool(persons) and all(
                    part in persons for part in expected.split()
                )
                # The bug being guarded against: the name coming back as an
                # organisation/location rather than a person.
                mislabelled = any(
                    any(part in v.lower() for part in expected.split())
                    and ("ORGANIZATION" in k or "NRP" in k)
                    for k, v in mapping.items()
                )
                restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
                exact = restored["text"] == text

                self.case_robustness_results.append({
                    "label": f"{label} ({casing})",
                    "text": text[:80] + ("…" if len(text) > 80 else ""),
                    "anonymized": anon["anonymized_text"][:80]
                    + ("…" if len(anon["anonymized_text"]) > 80 else ""),
                    "full_text": text,
                    "full_anonymized": anon["anonymized_text"],
                    "mapping": mapping,
                    "restored": restored["text"],
                    "detected": detected,
                    "not_mislabelled": not mislabelled,
                    "round_trip": exact,
                    "passed": detected and not mislabelled and exact,
                })

        # Case-sensitive identifiers must survive re-casing untouched.
        guard_text = (
            "THE ACCOUNT HOLDER IS SUNITA KRISHNAN AND HER PAN IS ABCPS1234K, "
            "IFSC SBIN0001234"
        )
        anon = self.client.anonymize_unique(guard_text)
        mapping = anon["entity_mapping"]
        pan_ok = any("IN_PAN" in k and v == "ABCPS1234K" for k, v in mapping.items())
        ifsc_ok = any("IN_IFSC" in k and v == "SBIN0001234" for k, v in mapping.items())
        restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
        self.case_robustness_results.append({
            "label": "ALL-CAPS preserves PAN + IFSC",
            "text": guard_text[:80] + "…",
            "anonymized": anon["anonymized_text"][:80] + "…",
            "full_text": guard_text,
            "full_anonymized": anon["anonymized_text"],
            "mapping": mapping,
            "restored": restored["text"],
            "detected": pan_ok and ifsc_ok,
            "not_mislabelled": True,
            "round_trip": restored["text"] == guard_text,
            "passed": pan_ok and ifsc_ok and restored["text"] == guard_text,
        })

        # Normally-cased prose must not gain entities from case recovery.
        for label, text, forbidden in (
            (
                "Cased prose keeps org separate",
                "Kavitha visited Contoso Bank in Chennai.",
                "kavitha visited contoso bank",
            ),
            (
                "Lowercase prose without PII",
                "the loan application is still pending approval from the credit team",
                "credit team",
            ),
            (
                "Lowercase prose without PII (banking)",
                "please reset my internet banking password immediately",
                "internet banking password",
            ),
        ):
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            # No entity may span the whole forbidden phrase.
            clean = not any(v.lower() == forbidden for v in mapping.values())
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.case_robustness_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "anonymized": anon["anonymized_text"][:80]
                + ("…" if len(anon["anonymized_text"]) > 80 else ""),
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "detected": clean,
                "not_mislabelled": clean,
                "round_trip": restored["text"] == text,
                "passed": clean and restored["text"] == text,
            })

        passed = sum(1 for r in self.case_robustness_results if r["passed"])
        self._log(
            "Case Robustness",
            f"{passed}/{len(self.case_robustness_results)} passed",
        )

    def run_address_completeness(self) -> None:
        """Test that the whole address lands inside one ADDRESS span.

        A flat/unit number left outside the span (``F3003``, ``A-101``, the
        ``12`` of ``12 MG Road``) is an exposed identifier, and a building name
        alone never merged because NER tags it LOCATION, ORGANIZATION or NRP
        interchangeably.  Each case asserts the fragments that must be covered
        and, where relevant, that nothing outside an address is swallowed.
        """
        test_cases = [
            {
                "label": "Flat number + society, no city",
                "text": "Rajesh Sharma residing at F3003, Kanakia Zen World has some concerns",
                "must_cover": ["F3003", "Kanakia Zen"],
                "must_not_cover": [],
            },
            {
                "label": "Flat number + society + city + PIN",
                "text": (
                    "Rajesh Sharma residing at F3003, Kanakia Zen World, "
                    "Kandivali East, Mumbai 400101"
                ),
                "must_cover": ["F3003", "Kanakia Zen World", "Mumbai", "400101"],
                "must_not_cover": [],
            },
            {
                "label": "Hyphenated unit number",
                "text": "He lives at A-101, Prestige Shantiniketan, Whitefield, Bengaluru 560048",
                "must_cover": ["A-101", "Prestige Shantiniketan", "560048"],
                "must_not_cover": [],
            },
            {
                "label": "Flat keyword + tower",
                "text": "Address: Flat 302, Tower B, Lodha Amara, Thane West, Mumbai 400604",
                "must_cover": ["302", "Lodha Amara", "400604"],
                "must_not_cover": [],
            },
            {
                "label": "House number before road",
                "text": "Send it to 12 MG Road, Bengaluru 560001",
                "must_cover": ["12", "MG Road", "560001"],
                "must_not_cover": [],
            },
            {
                "label": "Society name in ALL-CAPS",
                "text": "RESIDING AT F3003, KANAKIA ZEN WORLD, MUMBAI 400101",
                "must_cover": ["F3003", "KANAKIA ZEN WORLD"],
                "must_not_cover": [],
            },
        ]

        for tc in test_cases:
            anon = self.client.anonymize_unique(tc["text"])
            mapping = anon["entity_mapping"]
            address_values = [
                v for k, v in mapping.items() if "ADDRESS" in k or "LOCATION" in k
            ]
            covered = " | ".join(address_values)

            missing = [f for f in tc["must_cover"] if f.lower() not in covered.lower()]
            leaked = [
                f for f in tc["must_not_cover"] if f.lower() in covered.lower()
            ]
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])

            self.address_completeness_results.append({
                "label": tc["label"],
                "text": tc["text"][:80] + ("…" if len(tc["text"]) > 80 else ""),
                "address": covered[:80] + ("…" if len(covered) > 80 else "") or "—",
                "missing": ", ".join(missing) or "—",
                "full_text": tc["text"],
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "complete": not missing,
                "no_overreach": not leaked,
                "round_trip": restored["text"] == tc["text"],
                "passed": not missing and not leaked and restored["text"] == tc["text"],
            })

        # Outside address context, ordinary ORGANIZATION / LOCATION mentions
        # must keep their own type rather than being folded into an address.
        for label, text, phrase in (
            ("Employer is not an address", "She works at Contoso Manufacturing on weekdays", "Contoso Manufacturing"),
            ("Sponsor is not an address", "The Kanakia Zen group sponsors the event", "Kanakia Zen"),
        ):
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            not_address = not any(
                "ADDRESS" in k and phrase.lower() in v.lower()
                for k, v in mapping.items()
            )
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.address_completeness_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "address": "— (expected none)",
                "missing": "—",
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "complete": not_address,
                "no_overreach": not_address,
                "round_trip": restored["text"] == text,
                "passed": not_address and restored["text"] == text,
            })

        passed = sum(1 for r in self.address_completeness_results if r["passed"])
        self._log(
            "Address Completeness",
            f"{passed}/{len(self.address_completeness_results)} passed",
        )

    def run_account_phone_disambiguation(self) -> None:
        """Test that a bare number is classified as an account or a phone.

        A bare run of 9-18 digits is a valid Indian bank account number, and
        the phone recognizer claims the same digits whenever they also form a
        mobile ("9876543210") or a landline without its leading 0
        ("5498721032").  Scores cannot separate them: the phone pattern scores
        0.60 against the account pattern's 0.10, and "number" sits in the
        phone recognizer's context list, so "bank account number" boosts the
        phone score to 1.00.  Only the surrounding words carry the answer.
        """
        cases = [
            ("Bank account number", "His bank account number is 9876543210",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("Account number, no bank word", "His account number is 9876543210",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("A/C abbreviation", "Credit A/C no 9876543210 today",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("IFSC follows the digits", "Transfer to 9876543210 IFSC SBIN0001234",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("NEFT follows the digits", "NEFT credit to 9876543210 via RTGS today",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("Mobile cue", "His mobile number is 9876543210",
             "9876543210", "PHONE_NUMBER"),
            ("No cue at all", "Call me on 9876543210",
             "9876543210", "PHONE_NUMBER"),
            ("Account word AFTER a phone", "Call me on 9876543210 for account queries",
             "9876543210", "PHONE_NUMBER"),
            ("+91 prefix stays a phone", "Contact him at +91 98765 43210",
             "+91 98765 43210", "PHONE_NUMBER"),
            ("Leading zero stays a phone", "Reach me on 09876543210",
             "09876543210", "PHONE_NUMBER"),
            ("12-digit account", "His bank account number is 123456789012",
             "123456789012", "IN_BANK_ACCOUNT"),
            ("Nearest cue wins (account)",
             "His bank account number is 9876543210 and mobile is 9123456780",
             "9876543210", "IN_BANK_ACCOUNT"),
            ("Nearest cue wins (mobile)",
             "His bank account number is 9876543210 and mobile is 9123456780",
             "9123456780", "PHONE_NUMBER"),
            ("Reported: landline-shaped account number",
             "My Account number is 5498721032.",
             "5498721032", "IN_BANK_ACCOUNT"),
            ("11-digit account number", "My account number is 54987210321.",
             "54987210321", "IN_BANK_ACCOUNT"),
            ("Landline-shaped account, then mobile",
             "My account number is 5498721032 and my mobile is 9876543210",
             "5498721032", "IN_BANK_ACCOUNT"),
            ("Landline-shaped phone", "My phone number is 5498721032.",
             "5498721032", "PHONE_NUMBER"),
            ("Helpline after an account word", "Account helpline 9876543210",
             "9876543210", "PHONE_NUMBER"),
        ]

        for label, text, value, expected in cases:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            actual = next(
                (
                    k.strip("{}").rsplit("_", 1)[0]
                    for k, v in mapping.items()
                    if v == value
                ),
                "(not detected)",
            )
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            passed = actual == expected and restored["text"] == text
            self.account_phone_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "value": value,
                "expected": expected,
                "actual": actual,
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "correct_type": actual == expected,
                "round_trip": restored["text"] == text,
                "passed": passed,
            })

        passed = sum(1 for r in self.account_phone_results if r["passed"])
        self._log(
            "Account vs Phone",
            f"{passed}/{len(self.account_phone_results)} passed",
        )

    def run_person_initials(self) -> None:
        """Test that a surname after dotted initials is not left exposed.

        Cased NER models end the entity at dotted initials: queried directly,
        "Mr. R.K. Sharma" returns only ``PERSON 'R.K.'`` and the surname is
        never detected.  The same name without periods returns one complete
        span, which isolates the periods as the trigger.
        """
        cases = [
            ("Two initials + surname", "Mr. R.K. Sharma has written a complaint",
             "R.K. Sharma"),
            ("No title, two initials", "R.K. Sharma has written a complaint",
             "R.K. Sharma"),
            ("Three initials + two names", "Dr. A.P.J. Abdul Kalam visited the branch",
             "A.P.J. Abdul Kalam"),
            ("Single initial + surname", "Ms. S. Iyer called the branch", "S. Iyer"),
            ("Spaced initials", "Cheque signed by R. K. Sharma today", "R. K. Sharma"),
            ("Three spaced initials", "Report by A. P. J. Kalam filed today",
             "A. P. J. Kalam"),
            ("Initials mid-sentence", "Complaint filed by R.K. Sharma yesterday",
             "R.K. Sharma"),
            ("Undotted initials", "The cheque was signed by R K Sharma", "R K Sharma"),
            ("Ordinary name unaffected", "Mr. Rajesh Sharma has written a complaint",
             "Rajesh Sharma"),
        ]

        for label, text, expected in cases:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            persons = [v for k, v in mapping.items() if "PERSON" in k]
            captured = expected in persons
            # The surname must not survive in the anonymized output.
            surname = expected.split()[-1]
            leaked = surname in anon["anonymized_text"]
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.person_initials_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "expected": expected,
                "actual": ", ".join(persons) or "(none)",
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "full_span": captured,
                "no_leak": not leaked,
                "round_trip": restored["text"] == text,
                "passed": captured and not leaked and restored["text"] == text,
            })

        # Guard: an unrelated capitalised word after initials must not be eaten.
        guard = "Send it to A.B. Next week we will follow up"
        anon = self.client.anonymize_unique(guard)
        mapping = anon["entity_mapping"]
        over_reach = any("Next" in v for v in mapping.values())
        restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
        self.person_initials_results.append({
            "label": "Guard: clause after initials not absorbed",
            "text": guard,
            "expected": "'Next' must not be part of a name",
            "actual": ", ".join(mapping.values()) or "(none)",
            "full_text": guard,
            "full_anonymized": anon["anonymized_text"],
            "mapping": mapping,
            "restored": restored["text"],
            "full_span": not over_reach,
            "no_leak": not over_reach,
            "round_trip": restored["text"] == guard,
            "passed": not over_reach and restored["text"] == guard,
        })

        passed = sum(1 for r in self.person_initials_results if r["passed"])
        self._log(
            "Person Initials",
            f"{passed}/{len(self.person_initials_results)} passed",
        )

    def run_person_titles(self) -> None:
        """Test Indian honorific and professional prefixes.

        Only the Western titles were known to the pipeline.  "CA Abhay Sharma"
        and "CS Priya" came back as ORGANIZATION — still redacted by default,
        but leaked outright for an app allow-listing ORGANIZATION — while
        "Er. Ram" tagged the abbreviation itself as PERSON and left a stray
        "." entity behind.  Titles stay outside the span, matching the
        existing behaviour for "Mr. Rajesh Sharma".
        """
        cases = [
            ("Dr.", "Dr. Ajay has approved the loan", "Ajay"),
            ("Er.", "Er. Ram has approved the loan", "Ram"),
            ("CA (no dot)", "CA Abhay has approved the loan", "Abhay"),
            ("CA + surname", "CA Abhay Sharma has approved the loan", "Abhay Sharma"),
            ("CS (no dot)", "CS Priya has approved the loan", "Priya"),
            ("Adv.", "Adv. Meera has approved the loan", "Meera"),
            ("Prof.", "Prof. Anil has approved the loan", "Anil"),
            ("Shri", "Shri Ramesh has approved the loan", "Ramesh"),
            ("Smt.", "Smt. Kavita has approved the loan", "Kavita"),
            ("Capt.", "Capt. Vikram has approved the loan", "Vikram"),
            ("Col.", "Col. Rana has approved the loan", "Rana"),
            ("Pt.", "Pt. Ravi has approved the loan", "Ravi"),
            ("Justice", "Justice Khanna has approved the loan", "Khanna"),
            ("Stacked titles", "Lt. Col. Vikram Rana called", "Vikram Rana"),
            ("Title + full name", "Er. Ram Kumar filed the report", "Ram Kumar"),
        ]

        for label, text, expected_name in cases:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            persons = [v for k, v in mapping.items() if "PERSON" in k]
            as_person = expected_name in persons
            # No entity may be a bare title or a lone punctuation mark.
            junk = [
                v for v in mapping.values()
                if not v.strip() or not any(c.isalnum() for c in v)
            ]
            name_leaked = expected_name.split()[0] in anon["anonymized_text"]
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.person_titles_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "expected": expected_name,
                "actual": ", ".join(f"{k.strip('{}')}={v}" for k, v in mapping.items())
                or "(none)",
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "as_person": as_person,
                "no_junk": not junk,
                "round_trip": restored["text"] == text,
                "passed": (
                    as_person and not junk and not name_leaked
                    and restored["text"] == text
                ),
            })

        # Control: a genuine organisation must keep its own type.
        control = "Contoso Manufacturing has approved the loan"
        anon = self.client.anonymize_unique(control)
        mapping = anon["entity_mapping"]
        still_org = any("ORGANIZATION" in k for k in mapping)
        restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
        self.person_titles_results.append({
            "label": "Control: real organisation unaffected",
            "text": control,
            "expected": "ORGANIZATION",
            "actual": ", ".join(f"{k.strip('{}')}={v}" for k, v in mapping.items())
            or "(none)",
            "full_text": control,
            "full_anonymized": anon["anonymized_text"],
            "mapping": mapping,
            "restored": restored["text"],
            "as_person": still_org,
            "no_junk": True,
            "round_trip": restored["text"] == control,
            "passed": still_org and restored["text"] == control,
        })

        passed = sum(1 for r in self.person_titles_results if r["passed"])
        self._log(
            "Person Titles",
            f"{passed}/{len(self.person_titles_results)} passed",
        )

    def run_nrp_alignment(self) -> None:
        """Test that NRP fires only when the demonym describes a person.

        NRP (nationality / religious / political group) is personal data only
        when it describes a person.  The NER model emits it for any demonym, so
        aggregate business language — "South Indian branches", "Indian banking
        sector" — was redacted even though it identifies nobody.  Used
        predicatively ("the customer is Indian") or before a singular person
        noun ("a Muslim woman") it describes an individual and must stay.
        """
        descriptive = [
            ("Branches, not a person",
             "Our South Indian branches have shown 20% growth this quarter."),
            ("Region, not a person",
             "The North Indian region has the highest loan disbursement."),
            ("Sector, not a person",
             "Indian banking sector is growing rapidly."),
            ("Generic customer segment",
             "South Indian customers prefer mobile banking."),
            ("Market, not a person",
             "The South Indian market has high potential for home loans."),
            ("Areas, not a person",
             "South Indian rural areas need more ATM coverage."),
            ("Business segment",
             "The bank is targeting South Indian SME customers."),
            ("Loans, not a person",
             "South Indian agriculture loans are performing well."),
            ("Portfolio, not a person",
             "The South Indian credit card portfolio is expanding."),
            ("Customer segment (NRI)",
             "South Indian NRI customers are our focus segment."),
            ("Adoption rate, not a person",
             "The South Indian digital banking adoption is high."),
            ("Services, not a person",
             "South Indian wealth management services are in demand."),
        ]

        for label, text in descriptive:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            nrp = [v for k, v in mapping.items() if "NRP" in k]
            unchanged = anon["anonymized_text"] == text
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.nrp_alignment_results.append({
                "label": label,
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "expectation": "no redaction",
                "nrp": ", ".join(nrp) or "—",
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "as_expected": unchanged,
                "round_trip": restored["text"] == text,
                "passed": unchanged and restored["text"] == text,
            })

        personal = [
            ("Predicate position", "The customer is Indian and lives in Mumbai.", "Indian"),
            ("End of sentence", "He is a South Indian Hindu.", "South Indian Hindu"),
            ("Before a preposition", "Rajesh is a Tamil Brahmin from Chennai.", "Tamil Brahmin"),
            ("Singular person noun", "The applicant is a Muslim woman aged 34.", "Muslim"),
            ("Attributive, no copula", "The Muslim woman filed a complaint.", "Muslim"),
            ("Religious role noun",
             "Indian national and Buddhist devotee Rahul Verma opened an account.",
             "Buddhist"),
            ("Label value", "Nationality: Indian", "Indian"),
            ("Singular customer", "A South Indian customer raised the issue.", "South Indian"),
            ("Occupation after copula (Tamil)",
             "The account holder is a Tamil speaker from Coimbatore.", "Tamil"),
            ("Occupation after copula (Punjabi)",
             "The borrower is a Punjabi farmer seeking a crop loan.", "Punjabi"),
            ("Occupation after copula (Gujarati)",
             "He is a Gujarati businessman with three accounts.", "Gujarati"),
            ("Caste + occupation", "He is a Rajput landowner from Rajasthan.", "Rajput"),
        ]

        for label, text, expected_nrp in personal:
            anon = self.client.anonymize_unique(text)
            mapping = anon["entity_mapping"]
            nrp = [v for k, v in mapping.items() if "NRP" in k]
            detected = any(expected_nrp in v for v in nrp)
            restored = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.nrp_alignment_results.append({
                "label": f"{label} (must stay NRP)",
                "text": text[:80] + ("…" if len(text) > 80 else ""),
                "expectation": f"NRP '{expected_nrp}'",
                "nrp": ", ".join(nrp) or "—",
                "full_text": text,
                "full_anonymized": anon["anonymized_text"],
                "mapping": mapping,
                "restored": restored["text"],
                "as_expected": detected,
                "round_trip": restored["text"] == text,
                "passed": detected and restored["text"] == text,
            })

        passed = sum(1 for r in self.nrp_alignment_results if r["passed"])
        self._log(
            "NRP Alignment",
            f"{passed}/{len(self.nrp_alignment_results)} passed",
        )

    def run_multiline_context(self) -> None:
        """Test that context on one line does not leak into another line.

        In forms, lists and chat messages every line is its own statement.  A
        keyword on one line used to relabel an entity on the next: in the
        two-line "residing at Mumbai." / "My name is Mr. R.K. Sharma." the word
        "residing" turned "R.K." into a LOCATION, so the surname was never
        joined to it and leaked.  A line that introduces the next one ("Aadhaar
        number:", "Correspondence Address") still describes the value beneath
        it, and an address wrapped over two lines must stay one address.
        """
        # (label, text, {value: expected entity type}, must stay masked,
        #  must stay in the output)
        cases = [
            # A keyword on a neighbouring line must not relabel the entity.
            ("Reported: location word on previous line",
             "Customer RAJESH KUMAR SHARMA residing at Mumbai.\n"
             "My name is Mr. R.K. Sharma.",
             {"R.K. Sharma": "PERSON"}, ["Sharma", "SHARMA"], []),
            ("Location label on previous line",
             "Branch: Andheri West\nCustomer: Priya Menon",
             {"Priya Menon": "PERSON", "Andheri West": "LOCATION"},
             ["Priya", "Andheri"], []),
            ("Form without colons",
             "Branch Andheri West\nCustomer Priya Menon",
             {"Priya Menon": "PERSON"}, ["Priya"], []),
            ("IFSC on the next line",
             "Mobile: 9876543210\nIFSC: SBIN0001234",
             {"9876543210": "PHONE_NUMBER"}, ["9876543210", "SBIN0001234"], []),
            ("Account word on previous line",
             "Please update my account\n9876543210 is my new mobile",
             {"9876543210": "PHONE_NUMBER"}, ["9876543210"], []),
            ("Employer line after an address",
             "Address: Flat 301, Kumar Pinnacle, Baner, Pune 411045\n"
             "Employer: Infosys Technologies",
             {"Infosys Technologies": "ORGANIZATION"},
             ["Infosys", "Kumar Pinnacle", "411045"], ["\nEmployer: "]),
            ("Aadhaar label wins over APAAR keyword elsewhere",
             "Student: Ananya Rao\nAadhaar: 234567890123",
             {"234567890123": "IN_AADHAAR", "Ananya Rao": "PERSON"},
             ["Ananya", "234567890123"], ["\nAadhaar: "]),
            ("Title-like surname ending a line",
             "Surname: Kumari\nPriya Sharma called yesterday",
             {"Priya Sharma": "PERSON"}, ["Kumari", "Priya", "Sharma"], []),
            ("Name wrapped across lines",
             "Beneficiary name: Sunita\nKumari",
             {"Sunita": "PERSON", "Kumari": "PERSON"}, ["Sunita", "Kumari"], []),
            ("Aadhaar followed by a numbered list",
             "Aadhaar: 2345 6789 0123\n1. Submit the form",
             {"2345 6789 0123": "IN_AADHAAR"}, ["2345", "6789 0123"],
             ["\n1. Submit the form"]),
            # A line that introduces the next one still counts.
            ("Aadhaar label on the line above",
             "Aadhaar number:\n234567890123",
             {"234567890123": "IN_AADHAAR"}, ["234567890123"], []),
            ("Account label on the line above",
             "Account number:\n9876543210",
             {"9876543210": "IN_BANK_ACCOUNT"}, ["9876543210"], []),
            ("APAAR heading several lines up",
             "APAAR details\nName: Ananya Rao\nDOB: 12/03/2008\nID: 123456789012",
             {"123456789012": "IN_APAAR"}, ["123456789012", "Ananya"], []),
            ("Customer ID heading without a colon",
             "Customer ID\n123456789",
             {"123456789": "CUSTOMER_ID"}, ["123456789"], []),
            ("Address wrapped onto a second line",
             "Address: Flat 301, Kumar Pinnacle,\nBaner, Pune 411045",
             {"Baner": "ADDRESS"}, ["Kumar", "Baner", "Pune", "411045"], []),
            ("Address under a heading",
             "Correspondence Address\n"
             "shivam residency, survey 45, kharadi, pune 411014",
             {"kharadi": "ADDRESS"},
             ["shivam", "residency", "kharadi", "411014"], []),
            ("Aadhaar number wrapped across lines",
             "Aadhaar: 2345 6789\n0123",
             {"2345 6789\n0123": "IN_AADHAAR"}, ["2345", "0123"], []),
            # Guards: separate lines must stay separate entities.
            ("Guard: list of cities stays separate",
             "Cities covered: Pune,\nMumbai,\nDelhi",
             {"Pune": "LOCATION", "Mumbai": "LOCATION", "Delhi": "LOCATION"},
             ["Pune", "Mumbai", "Delhi"], [",\n"]),
            ("Guard: locations on consecutive lines",
             "I visited Pune\nMumbai is next",
             {"Pune": "LOCATION", "Mumbai": "LOCATION"},
             ["Pune", "Mumbai"], ["\n"]),
            ("Guard: name on a new line not merged into address",
             "Customer residing at Mumbai\nMy name is Rahul",
             {"Mumbai": "LOCATION", "Rahul": "PERSON"},
             ["Mumbai", "Rahul"], ["\nMy name is "]),
        ]

        self.multiline_results = self._check_expectations(cases)
        passed = sum(1 for r in self.multiline_results if r["passed"])
        self._log(
            "Multi-line Context",
            f"{passed}/{len(self.multiline_results)} passed",
        )

    def run_mixed_case_names(self) -> None:
        """Test names typed with only the first word capitalised.

        A cased NER model stops at the first uncased word, so in "My name is
        Venkata narasimha raju." only "Venkata" was masked and the middle and
        last names leaked.  The whole name must now be masked, while ordinary
        words typed after a name ("Ramesh paid electricity bill") must stay.
        """
        cases = [
            ("Reported: middle and last name lowercase",
             "My name is Venkata narasimha raju.",
             {"Venkata narasimha raju": "PERSON"},
             ["Venkata", "narasimha", "raju"], []),
            ("Three-part name at sentence start",
             "Rajesh kumar sharma has applied for a home loan",
             {"Rajesh kumar sharma": "PERSON"},
             ["Rajesh", "kumar", "sharma"], ["has applied for a home loan"]),
            ("Name after a role word",
             "Customer Priya ramesh iyer called the branch yesterday",
             {"Priya ramesh iyer": "PERSON"},
             ["Priya", "ramesh", "iyer"], ["called the branch"]),
            ("First name alone read as a place",
             "Transfer INR 5,000 to Suresh babu naidu today",
             {"Suresh babu naidu": "PERSON"},
             ["Suresh", "babu", "naidu"], ["today"]),
            ("Form field",
             "Account holder: Sunita devi agarwal",
             {"Sunita devi agarwal": "PERSON"},
             ["Sunita", "devi", "agarwal"], ["Account holder: "]),
            ("Signatory",
             "The cheque was signed by Mohammed faisal khan",
             {"Mohammed faisal khan": "PERSON"},
             ["Mohammed", "faisal", "khan"], ["signed by "]),
            ("Surname before a verb",
             "Kavitha subramaniam opened a savings account",
             {"Kavitha subramaniam": "PERSON"},
             ["Kavitha", "subramaniam"], ["opened a savings account"]),
            ("Only the surname lowercase",
             "Rajesh Kumar sharma submitted his documents",
             {"Rajesh Kumar sharma": "PERSON"},
             ["Rajesh", "Kumar", "sharma"], ["submitted his documents"]),
            ("Four-part name",
             "Pamidighantam venkata subba rao is the nominee",
             {"Pamidighantam venkata subba rao": "PERSON"},
             ["Pamidighantam", "venkata", "subba", "rao"], ["is the nominee"]),
            ("Name on a form line",
             "Name: Venkata narasimha raju\nMobile: 9876543210",
             {"Venkata narasimha raju": "PERSON", "9876543210": "PHONE_NUMBER"},
             ["narasimha", "raju", "9876543210"], ["\nMobile: "]),
            ("Two names joined by 'and'",
             "Faisal ahmed and Sunil das visited today",
             {"Faisal ahmed": "PERSON", "Sunil das": "PERSON"},
             ["Faisal", "ahmed", "Sunil", "das"], [" and ", "visited today"]),
            ("Surname with an apostrophe",
             "Anil d'souza has a pending CIBIL dispute.",
             {"Anil d'souza": "PERSON"},
             ["Anil", "souza"], ["has a pending CIBIL dispute"]),
            ("Accented surname",
             "Sofía martínez opened an NRE account last week.",
             {"Sofía martínez": "PERSON"},
             ["Sofía", "martínez"], ["opened an NRE account"]),
            # Guards: ordinary words typed after a name must stay unmasked.
            ("Guard: verb after a name",
             "Ramesh paid electricity bill using UPI",
             {"Ramesh": "PERSON"}, ["Ramesh"], ["paid electricity bill using UPI"]),
            ("Guard: relation word after a name",
             "Kavitha mother is the joint holder.",
             {"Kavitha": "PERSON"}, ["Kavitha"], ["mother is the joint holder"]),
            ("Guard: title after a name",
             "Rahul sir will call you back.",
             {"Rahul": "PERSON"}, ["Rahul"], ["sir will call you back"]),
            ("Guard: booking sentence",
             "Meera booked movie tickets for Sunday",
             {"Meera": "PERSON"}, ["Meera"], ["booked movie tickets for"]),
            ("Guard: shopping sentence",
             "Rahul bought new shoes from the mall",
             {"Rahul": "PERSON"}, ["Rahul"], ["bought new shoes from the mall"]),
            ("Guard: prose next to a bank name",
             "NOTE: Kavitha visited Contoso Bank yesterday",
             {"Kavitha": "PERSON"}, ["Kavitha"], ["visited"]),
            ("Guard: correctly cased name unchanged",
             "My name is Venkata Narasimha Raju.",
             {"Venkata Narasimha Raju": "PERSON"},
             ["Venkata", "Narasimha", "Raju"], []),
        ]

        self.mixed_case_results = self._check_expectations(cases)
        passed = sum(1 for r in self.mixed_case_results if r["passed"])
        self._log(
            "Mixed-case Names",
            f"{passed}/{len(self.mixed_case_results)} passed",
        )

    def run_address_units(self) -> None:
        """Test that the whole unit designation lands inside the ADDRESS.

        Flat numbers, block letters and their labels have no recognizer, and
        the "." of "no." was read as the end of a sentence, so "my address is
        Flat no. 302, C 23, Prestige Towers, Bangalore." left "no. 302, C"
        exposed.  The whole designation must now be masked, while "no" in an
        ordinary sentence, a date and a separate sentence stay as they were.
        """
        cases = [
            ("Reported: flat number and block",
             "my address is Flat no. 302, C 23, Prestige Towers, Bangalore.",
             {"Flat no. 302, C 23": "ADDRESS"},
             ["Flat", "302", "C 23", "Prestige", "Bangalore"], ["my address is "]),
            ("Hyphenated block number",
             "My address is Flat No. 302, C-23, Prestige Towers, Bangalore.",
             {"Flat No. 302, C-23": "ADDRESS"},
             ["Flat", "302", "C-23", "Prestige"], ["My address is "]),
            ("Address followed by another sentence",
             "Send it to Flat no. 302, C 23, Prestige Towers, Bangalore. My mobile is 9876543210.",
             {"Flat no. 302, C 23": "ADDRESS", "9876543210": "PHONE_NUMBER"},
             ["Flat", "302", "C 23", "9876543210"], ["Send it to ", ". My mobile is "]),
            ("Hyderabad house number",
             "Residing at H.No. 12-3-456, Street No. 5, Banjara Hills, Hyderabad 500034",
             {"H.No. 12-3-456": "ADDRESS"},
             ["H.No", "12-3-456", "Street No. 5", "Banjara"], ["Residing at "]),
            ("Door number with a slash",
             "Correspondence address: Door No. 45/2, 3rd Cross, Jayanagar 4th Block, Bangalore 560011",
             {"Door No. 45/2": "ADDRESS"},
             ["Door", "45/2", "3rd Cross", "Jayanagar"], ["Correspondence address: "]),
            ("Floor and wing",
             "Address: D.No. 45/2, 2nd Floor, B Wing, Lodha Park, Worli, Mumbai 400018",
             {"D.No. 45/2, 2nd Floor, B Wing": "ADDRESS"},
             ["D.No", "45/2", "2nd Floor", "B Wing", "Lodha"], ["Address: "]),
            ("Plot and sector",
             "Please update my address to Plot No. 17, Sector 21, Kharghar, Navi Mumbai 410210",
             {"Plot No. 17": "ADDRESS"},
             ["Plot", "17", "Sector 21", "Kharghar"], ["Please update my address to "]),
            ("Flat and tower letters",
             "Deliver the cheque book to Flat 5B, Tower C, DLF Phase 2, Gurgaon",
             {"Flat 5B, Tower C": "ADDRESS"},
             ["Flat", "5B", "Tower C", "DLF"], ["Deliver the cheque book to "]),
            ("Abbreviated apartment",
             "I live in Apt. 1204, Lodha Bellissimo, Worli, Mumbai",
             {"Apt. 1204": "ADDRESS"},
             ["Apt", "1204", "Lodha", "Worli"], ["I live in "]),
            ("House number and floor",
             "Address: #302, 2nd Floor, Brigade Road, Bangalore",
             {"302, 2nd Floor": "ADDRESS"},
             ["302", "2nd Floor", "Brigade"], ["Address: "]),
            ("Room in a chawl",
             "My address is Room no. 4, Sai Kripa Chawl, Dharavi, Mumbai",
             {"Room no. 4": "ADDRESS"},
             ["Room", "no. 4", "Sai Kripa", "Dharavi"], ["My address is "]),
            ("House number and sector",
             "New address: House No. 221, Sector 15, Chandigarh 160015.",
             {"House No. 221": "ADDRESS"},
             ["House", "221", "Sector 15", "160015"], ["New address: "]),
            ("Shop opposite a landmark",
             "Address: Shop 4, Opp. City Mall, MG Road, Pune 411001",
             {"Shop 4, Opp. City Mall": "ADDRESS"},
             ["Shop", "Opp", "City Mall", "411001"], ["Address: "]),
            ("Unit designation wrapped over lines",
             "Address: Flat no. 302,\nC 23, Prestige Towers,\nBangalore 560001",
             {"Flat no. 302,\nC 23": "ADDRESS"},
             ["Flat", "302", "C 23", "Prestige", "560001"], ["Address: "]),
            # Guards: "no", dates and separate sentences stay as they were.
            ("Guard: order number",
             "Order no. 12345 shipped to Bangalore yesterday.",
             {"Bangalore": "LOCATION"},
             ["Bangalore"], ["Order no. 12345 shipped to", "yesterday"]),
            ("Guard: meeting room",
             "Room no. 4 is booked for the meeting in Mumbai.",
             {"Mumbai": "LOCATION"},
             ["Mumbai"], ["Room no. 4 is booked for the meeting in"]),
            ("Guard: flat rent",
             "My flat rent of 25000 is due in Pune.",
             {"Pune": "LOCATION"},
             ["Pune"], ["My flat rent of 25000 is due in"]),
            ("Guard: 'no' before a building name",
             "There is no Prestige Towers, Bangalore in our records.",
             {"Bangalore": "LOCATION"},
             ["Prestige", "Bangalore"], ["There is no ", "in our records"]),
            ("Guard: date before an address",
             "Delivered on 12/05/2024, Prestige Towers, Bangalore.",
             {"12/05/2024": "DATE_TIME", "Prestige Towers, Bangalore": "ADDRESS"},
             ["12/05/2024", "Prestige"], ["Delivered on "]),
            ("Guard: separate sentences",
             "I live in Pune. Mumbai is where I work.",
             {"Pune": "LOCATION", "Mumbai": "LOCATION"},
             ["Pune", "Mumbai"], ["I live in ", " is where I work."]),
        ]

        self.address_units_results = self._check_expectations(cases)
        passed = sum(1 for r in self.address_units_results if r["passed"])
        self._log(
            "Address Units",
            f"{passed}/{len(self.address_units_results)} passed",
        )

    def run_key_value_context(self) -> None:
        """Test that the key of a key=value pair counts as a context word.

        spaCy keeps a URL, and a key=value pair written without spaces, as one
        token, so in "https://api.com?aadhaar=987654321098&pan=ABCPK1234L" the
        key "aadhaar" was never seen as context.  An Aadhaar number without
        separators scores below the threshold without it, so it leaked while
        the PAN next to it was masked.  Keys that are not context words
        ("txn_id=") must change nothing.
        """
        cases = [
            ("Reported: Aadhaar in a URL query",
             "my website is https://api.com?aadhaar=987654321098&pan=ABCPK1234L.",
             {"987654321098": "IN_AADHAAR", "ABCPK1234L": "IN_PAN"},
             ["987654321098", "ABCPK1234L"], ["my website is ", "?aadhaar=", "&pan="]),
            ("Key=value in plain text",
             "aadhaar=987654321098",
             {"987654321098": "IN_AADHAAR"}, ["987654321098"], ["aadhaar="]),
            ("Key:value without a space",
             "Aadhaar:987654321098, PAN:ABCPK1234L",
             {"987654321098": "IN_AADHAAR", "ABCPK1234L": "IN_PAN"},
             ["987654321098", "ABCPK1234L"], ["Aadhaar:", ", PAN:"]),
            ("camelCase key",
             "aadhaarNumber=987654321098",
             {"987654321098": "IN_AADHAAR"}, ["987654321098"], ["aadhaarNumber="]),
            ("snake_case key in a request line",
             "GET /kyc?aadhaar_no=987654321098&mobile=9876543210 HTTP/1.1",
             {"987654321098": "IN_AADHAAR", "9876543210": "PHONE_NUMBER"},
             ["987654321098", "9876543210"],
             ["GET /kyc?aadhaar_no=", "&mobile=", " HTTP/1.1"]),
            ("UID key in a URL",
             "https://api.com?uid=987654321098",
             {"987654321098": "IN_AADHAAR"}, ["987654321098"], ["?uid="]),
            ("Form-encoded body",
             "pan=ABCPK1234L&aadhaar=987654321098",
             {"ABCPK1234L": "IN_PAN", "987654321098": "IN_AADHAAR"},
             ["ABCPK1234L", "987654321098"], ["pan=", "&aadhaar="]),
            ("Query string with a name",
             "POST /kyc?name=rahul&aadhaar=987654321098 HTTP/1.1",
             {"rahul": "PERSON", "987654321098": "IN_AADHAAR"},
             ["rahul", "987654321098"], ["POST /kyc?name=", "&aadhaar=", " HTTP/1.1"]),
            ("Log line with a name",
             "log: user=rahul uid=987654321098 status=ok",
             {"rahul": "PERSON", "987654321098": "IN_AADHAAR"},
             ["rahul", "987654321098"], ["log: user=", " uid=", " status=ok"]),
            ("ALL-CAPS log line",
             "LOG: USER=RAHUL UID=987654321098 STATUS=OK",
             {"RAHUL": "PERSON", "987654321098": "IN_AADHAAR"},
             ["RAHUL", "987654321098"], ["LOG: USER=", " UID=", " STATUS=OK"]),
            ("Baseline: key as a separate word",
             "My aadhaar is 987654321098",
             {"987654321098": "IN_AADHAAR"}, ["987654321098"], ["My aadhaar is "]),
            # Guards: keys that are not context words change nothing.
            ("Guard: transaction id",
             "txn_id=987654321098&amount=500",
             {}, [], ["txn_id=987654321098&amount=500"]),
            ("Guard: order number",
             "Order 987654321098 dispatched today",
             {}, [], ["Order 987654321098 dispatched today"]),
            ("Guard: URL without IDs",
             "Search https://example.com/search?q=account&page=2 for help",
             {"https://example.com/search?q=account&page=2": "URL"},
             ["example.com"], ["Search ", " for help"]),
        ]

        self.key_value_results = self._check_expectations(cases)
        passed = sum(1 for r in self.key_value_results if r["passed"])
        self._log(
            "Key-value Context",
            f"{passed}/{len(self.key_value_results)} passed",
        )

    def run_geo_coordinates(self) -> None:
        """Test detection and anonymization of geographic coordinates.

        Covers: decimal degree pairs, labeled lat/lon, cardinal direction,
        DMS format, mixed PII with coordinates, and round-trip.
        """
        geo_scenarios = [
            {
                "label": "DD pair with GPS context",
                "text": "The GPS coordinates are 28.6139, 77.2090 for the branch.",
                "pii_values": ["28.6139, 77.2090"],
            },
            {
                "label": "DD pair with negative values",
                "text": "Office location coordinates: -33.8688, 151.2093.",
                "pii_values": ["-33.8688, 151.2093"],
            },
            {
                "label": "Labeled latitude and longitude",
                "text": "Site lat: 19.0760 lon: 72.8777 in Mumbai.",
                "pii_values": ["19.0760", "72.8777"],
            },
            {
                "label": "Cardinal direction format",
                "text": "Survey point at 28.6139° N, 77.2090° E.",
                "pii_values": ["28.6139"],
            },
            {
                "label": "DMS format",
                "text": "Branch located at 28°36'50\"N 77°12'32\"E.",
                "pii_values": ["28°36"],
            },
            {
                "label": "Coordinates mixed with other PII",
                "text": (
                    "Customer Rajesh Kumar (rajesh@example.com, Aadhaar 9876 5432 1098) "
                    "visited the branch at GPS coordinates 19.0760, 72.8777."
                ),
                "pii_values": ["19.0760, 72.8777"],
            },
            {
                "label": "Coordinates in address context",
                "text": (
                    "New ATM installation at coordinates 12.9716, 77.5946 "
                    "near MG Road, Bangalore 560001."
                ),
                "pii_values": ["12.9716, 77.5946"],
            },
        ]

        for tc in geo_scenarios:
            anon = self.client.anonymize_unique(tc["text"])
            anon_text = anon["anonymized_text"]

            detected = any(
                val not in anon_text for val in tc["pii_values"]
            )

            self.geo_coordinate_results.append({
                "label": tc["label"],
                "text": tc["text"][:80] + ("…" if len(tc["text"]) > 80 else ""),
                "anonymized": anon_text[:80] + ("…" if len(anon_text) > 80 else ""),
                "original_full": tc["text"],
                "anonymized_full": anon_text,
                "detected": detected,
                "passed": detected,
            })

        # Round-trip test
        rt_text = "GPS coordinates: 28.6139, 77.2090 for the site."
        anon = self.client.anonymize_unique(rt_text)
        deanon = self.client.deanonymize(
            anon["id"], anon["anonymized_text"],
        )
        rt_match = deanon["text"] == rt_text
        self.geo_coordinate_results.append({
            "label": "Round-trip (anonymize → deanonymize)",
            "text": rt_text[:80],
            "anonymized": anon["anonymized_text"][:80],
            "original_full": rt_text,
            "anonymized_full": anon["anonymized_text"],
            "detected": True,
            "passed": rt_match,
        })

        passed = sum(1 for r in self.geo_coordinate_results if r["passed"])
        self._log(
            "Geo-Coordinates",
            f"{passed}/{len(self.geo_coordinate_results)} passed",
        )

    def run_nrp(self) -> None:
        """Test detection of NRP (Nationality/Religious/Political) entities."""
        nrp_scenarios = [
            {
                "label": "Nationality — Indian",
                "text": "NRE account opening for Indian national Amit Sharma at Contoso Bank.",
                "expected_nrp": ["Indian"],
            },
            {
                "label": "Religion — Hindu",
                "text": "Complaint registered by Hindu customer Priya Patel at Contoso Bank.",
                "expected_nrp": ["Hindu"],
            },
            {
                "label": "Religion — Muslim",
                "text": "KYC verification for Muslim applicant Fatima Khan at Contoso Bank.",
                "expected_nrp": ["Muslim"],
            },
            {
                "label": "Religion — Sikh",
                "text": "Loan application by Sikh customer Gurpreet Singh at Contoso Bank.",
                "expected_nrp": ["Sikh"],
            },
            {
                "label": "Mixed — nationality + religion",
                "text": "Indian national and Buddhist devotee Rahul Verma opened an account at Contoso Bank.",
                "expected_nrp": ["Indian", "Buddhist"],
            },
        ]

        for tc in nrp_scenarios:
            anon = self.client.anonymize_unique(tc["text"])
            anon_text = anon["anonymized_text"]

            detected_nrp = [
                v for k, v in anon["entity_mapping"].items()
                if "NRP" in k
            ]
            all_found = all(
                nrp not in anon_text for nrp in tc["expected_nrp"]
            )

            self.nrp_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_nrp": tc["expected_nrp"],
                "detected_nrp": detected_nrp,
                "passed": all_found,
            })

        # Round-trip test
        rt_text = "Indian customer Priya Sharma visited Contoso Bank, Mumbai."
        anon = self.client.anonymize_unique(rt_text)
        deanon = self.client.deanonymize(anon["id"], anon["anonymized_text"])
        self.nrp_results.append({
            "label": "Round-trip (anonymize → deanonymize)",
            "text": rt_text,
            "anonymized": anon["anonymized_text"],
            "expected_nrp": ["Indian"],
            "detected_nrp": [],
            "passed": deanon["text"] == rt_text,
        })

        passed = sum(1 for r in self.nrp_results if r["passed"])
        self._log("NRP Detection", f"{passed}/{len(self.nrp_results)} passed")

    def run_us_entities(self) -> None:
        """Test detection of US entity types (SSN, ITIN, Passport, DL)."""
        us_scenarios = [
            # ── SSN scenarios ──
            {
                "label": "US SSN with context",
                "text": "NRI customer Social Security Number SSN: 219-09-9999 for wire transfer.",
                "expected_type": "US_SSN",
                "pii_values": ["219-09-9999"],
            },
            {
                "label": "SSN in W-2 employment context",
                "text": "The W-2 form shows SSN 456-78-9012 for the NRI employee posted in Bengaluru.",
                "expected_type": "US_SSN",
                "pii_values": ["456-78-9012"],
            },
            {
                "label": "SSN in mortgage/loan application",
                "text": "Applicant provided Social Security Number 287-14-5633 on the home loan pre-approval form.",
                "expected_type": "US_SSN",
                "pii_values": ["287-14-5633"],
            },
            {
                "label": "SSN in I-9 employment verification",
                "text": "For I-9 verification of the new hire, SSN 321-54-9876 has been recorded in the HR portal.",
                "expected_type": "US_SSN",
                "pii_values": ["321-54-9876"],
            },
            # ── ITIN scenarios ──
            {
                "label": "US ITIN with context",
                "text": "US taxpayer identification number ITIN: 955-70-1234 for NRE account.",
                "expected_type": "US_ITIN",
                "pii_values": ["955-70-1234"],
            },
            {
                "label": "ITIN in 1040-NR tax filing",
                "text": "Non-resident alien filed form 1040-NR with ITIN 988-71-5432 for US rental income.",
                "expected_type": "US_ITIN",
                "pii_values": ["988-71-5432"],
            },
            {
                "label": "ITIN with full label",
                "text": "Individual Taxpayer Identification Number: 900-78-5612 assigned for FATCA reporting.",
                "expected_type": "US_ITIN",
                "pii_values": ["900-78-5612"],
            },
            # ── US Passport scenarios ──
            {
                "label": "US Passport (alphanumeric)",
                "text": "KYC verification using US passport C12345678 for NRI account opening.",
                "expected_type": "US_PASSPORT",
                "pii_values": ["C12345678"],
            },
            {
                "label": "US Passport (nine-digit numeric)",
                "text": "NRI presented US Passport number: 445566771 at the consulate for attestation.",
                "expected_type": "US_PASSPORT",
                "pii_values": ["445566771"],
            },
            {
                "label": "US Passport in travel context",
                "text": "Immigration officer scanned US passport 556677889 at Mumbai international airport.",
                "expected_type": "US_PASSPORT",
                "pii_values": ["556677889"],
            },
            # ── US Driver License scenarios ──
            {
                "label": "US DL (Florida format)",
                "text": "Florida driver license: G123-456-78-901-0 submitted as photo ID for the NRE account.",
                "expected_type": "US_DRIVER_LICENSE",
                "pii_values": ["G123"],
            },
            {
                "label": "US DL (California format)",
                "text": "My California driver license number is B1234568 used for address verification.",
                "expected_type": "US_DRIVER_LICENSE",
                "pii_values": ["B1234568"],
            },
            # ── Mixed US + India PII ──
            {
                "label": "Mixed US + India PII",
                "text": "NRI Vikram Mehta, SSN 323-45-6789, PAN ABCPM5678K, Aadhaar 6789 0123 4567, email vikram@example.com.",
                "expected_type": "US_SSN",
                "pii_values": ["323-45-6789", "ABCPM5678K", "6789 0123 4567"],
            },
            {
                "label": "NRI remittance with SSN + IFSC",
                "text": "Wire transfer for SSN 456-78-9012 to Contoso Bank IFSC CNTS0001234, beneficiary account 9876543210.",
                "expected_type": "US_SSN",
                "pii_values": ["456-78-9012"],
            },
            {
                "label": "ITIN + Indian mobile in FCNR context",
                "text": "FCNR deposit opened by ITIN holder 912-70-1234, contact +91 98765 43210 for confirmation.",
                "expected_type": "US_ITIN",
                "pii_values": ["912-70-1234"],
            },
        ]

        for tc in us_scenarios:
            anon = self.client.anonymize_unique(tc["text"])
            anon_text = anon["anonymized_text"]
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            has_expected = any(
                tc["expected_type"] in k for k in anon["entity_mapping"]
            )

            self.us_entity_results.append({
                "label": tc["label"],
                "text": tc["text"],
                "anonymized": anon_text,
                "expected_type": tc["expected_type"],
                "all_hidden": all_hidden,
                "has_expected": has_expected,
                "passed": all_hidden,
            })

        # Round-trip tests
        rt_scenarios = [
            {
                "label": "Round-trip (US SSN + Indian PAN)",
                "text": "Customer SSN: 219-09-9999 and PAN ABCPK1234L.",
                "expected_type": "US_SSN",
            },
            {
                "label": "Round-trip (ITIN in tax context)",
                "text": "Tax return for ITIN 988-71-5432 filed with IRS.",
                "expected_type": "US_ITIN",
            },
            {
                "label": "Round-trip (US Passport for KYC)",
                "text": "KYC completed with US passport C12345678 at the branch.",
                "expected_type": "US_PASSPORT",
            },
        ]
        for rt in rt_scenarios:
            anon = self.client.anonymize_unique(rt["text"])
            deanon = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.us_entity_results.append({
                "label": rt["label"],
                "text": rt["text"],
                "anonymized": anon["anonymized_text"],
                "expected_type": rt["expected_type"],
                "all_hidden": True,
                "has_expected": True,
                "passed": deanon["text"] == rt["text"],
            })

        passed = sum(1 for r in self.us_entity_results if r["passed"])
        self._log("US Entities", f"{passed}/{len(self.us_entity_results)} passed")

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
            anon = self.client.anonymize_unique(tc["text"])
            anon_text = anon["anonymized_text"]
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            has_expected = any(
                tc["expected_type"] in k for k in anon["entity_mapping"]
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
            anon = self.client.anonymize_unique(rt["text"])
            deanon = self.client.deanonymize(anon["id"], anon["anonymized_text"])
            self.ckyc_pran_apaar_results.append({
                "label": rt["label"],
                "text": rt["text"],
                "anonymized": anon["anonymized_text"],
                "expected_type": rt["expected_type"],
                "all_hidden": True,
                "has_expected": True,
                "passed": deanon["text"] == rt["text"],
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
            body = self.client.anonymize(tc["text"])
            anon_text = body["anonymized_text"]
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            has_expected = any(
                tc["expected_type"] in k for k in body.get("entity_mapping", {})
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
        body = self.client.anonymize(rt_text)
        deanon = self.client.deanonymize(body["id"], body["anonymized_text"])
        self.customer_id_results.append({
            "label": "Round-trip (Customer ID)",
            "text": rt_text,
            "anonymized": body["anonymized_text"],
            "expected_type": "CUSTOMER_ID",
            "all_hidden": True,
            "has_expected": True,
            "passed": deanon["text"] == rt_text,
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
            body = self.client.anonymize(tc["text"])
            anon_text = body["anonymized_text"]
            all_hidden = all(v not in anon_text for v in tc["pii_values"])
            all_types_found = all(
                any(et in k for k in body.get("entity_mapping", {}))
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
        body = self.client.anonymize(combo_rt_text)
        deanon = self.client.deanonymize(body["id"], body["anonymized_text"])
        combo_rt_passed = (
            "435678912" in deanon["text"]
            and "917020048230123" in deanon["text"]
            and "9845012345" in deanon["text"]
        )
        self.customer_id_results.append({
            "label": "Combo round-trip (CustID + Account + Phone)",
            "text": combo_rt_text,
            "anonymized": body["anonymized_text"],
            "expected_type": "CUSTOMER_ID, IN_BANK_ACCOUNT, PHONE_NUMBER",
            "all_hidden": True,
            "has_expected": True,
            "passed": combo_rt_passed,
        })

        passed = sum(1 for r in self.customer_id_results if r["passed"])
        self._log("Customer ID", f"{passed}/{len(self.customer_id_results)} passed")

    def run_pqc_encrypt(self) -> None:
        """Test PQC encryption backend (ML-KEM-768 + AES-256-GCM).

        Covers: anonymization with encrypt, deanonymize without flag,
        deanonymize with flag, non-determinism, and multi-entity scenarios.
        """
        pqc_app_id = self.client.ensure_app("__testPQCTestApp")

        # Configure multiple entity types for encryption
        self.client.update_app_config(pqc_app_id, "IN_DRIVING_LICENSE", "encrypt")
        self.client.update_app_config(pqc_app_id, "EMAIL_ADDRESS", "encrypt")
        self.client.update_app_config(pqc_app_id, "PHONE_NUMBER", "encrypt")

        pqc_scenarios = [
            {
                "label": "DL number only",
                "text": "My driving license is MH 14 2019 0012345.",
                "pii_values": ["MH 14 2019 0012345"],
            },
            {
                "label": "Email address only",
                "text": "Contact sneha.patil@gmail.com for details.",
                "pii_values": ["sneha.patil@gmail.com"],
            },
            {
                "label": "Phone number only",
                "text": "Reach me at 9823567890 for updates.",
                "pii_values": ["9823567890"],
            },
            {
                "label": "Multi-entity: DL + Email",
                "text": "DL: KA-09-2023-5554321, email: vikas.gautam@example.com.",
                "pii_values": ["KA-09-2023-5554321", "vikas.gautam@example.com"],
            },
            {
                "label": "Multi-entity: Email + Phone + Person",
                "text": "Sneha Patil can be reached at sneha@contoso.com or 9876543210.",
                "pii_values": ["sneha@contoso.com", "9876543210"],
            },
            {
                "label": "Banking scenario with encrypted PII",
                "text": (
                    "Client Rajesh Kumar, DL: TN 01 2018 9990001, "
                    "email: rajesh.kumar@gmail.com, phone: 9123456789. "
                    "Please process KYC verification."
                ),
                "pii_values": ["TN 01 2018 9990001", "rajesh.kumar@gmail.com", "9123456789"],
            },
        ]

        # ── Phase 1: Anonymize ──────────────────────────────────────────
        anon_cache: dict[str, dict] = {}
        for sc in pqc_scenarios:
            anon = self.client.anonymize(sc["text"], app_id=pqc_app_id)
            anon_cache[sc["label"]] = anon
            encrypt_mapping = anon.get("encrypt_mapping", {})

            # Check that each target PII value is hidden & present in encrypt_mapping
            all_hidden = all(pv not in anon["anonymized_text"] for pv in sc["pii_values"])
            originals_in_mapping = list(encrypt_mapping.values())
            all_mapped = all(pv in originals_in_mapping for pv in sc["pii_values"])

            self.pqc_encrypt_results.append({
                "label": sc["label"],
                "encrypt_count": len(encrypt_mapping),
                "expected_count": len(sc["pii_values"]),
                "all_hidden": all_hidden,
                "all_mapped": all_mapped,
                "passed": all_hidden and all_mapped,
            })

        # ── Phase 2: Deanonymize WITHOUT include_encrypted ──────────────
        for sc in pqc_scenarios:
            anon = anon_cache[sc["label"]]
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=pqc_app_id,
            )
            cipher_tokens = list(anon.get("encrypt_mapping", {}).keys())
            cipher_still_present = any(t in restored["text"] for t in cipher_tokens) if cipher_tokens else True
            pii_not_restored = all(pv not in restored["text"] for pv in sc["pii_values"])

            self.pqc_encrypt_no_flag.append({
                "label": sc["label"],
                "cipher_present": cipher_still_present,
                "pii_not_restored": pii_not_restored,
                "passed": pii_not_restored,
            })

        # ── Phase 3: Deanonymize WITH include_encrypted=True ────────────
        for sc in pqc_scenarios:
            anon = anon_cache[sc["label"]]
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"],
                app_id=pqc_app_id, include_encrypted=True,
            )
            pii_restored = all(pv in restored["text"] for pv in sc["pii_values"])
            exact_match = sc["text"] == restored["text"]

            self.pqc_encrypt_with_flag.append({
                "label": sc["label"],
                "pii_restored": pii_restored,
                "exact": exact_match,
                "passed": pii_restored,
            })
            self.pqc_encrypt_detail.append({
                "scenario": sc["label"],
                "original_text": sc["text"],
                "anonymized_text": anon["anonymized_text"],
                "restored_text": restored["text"],
                "entity_mapping": anon.get("entity_mapping", {}),
                "encrypt_mapping": anon.get("encrypt_mapping", {}),
                "exact": exact_match,
            })

        # ── Phase 4: Non-determinism check ──────────────────────────────
        first_sc = pqc_scenarios[0]
        anon1 = self.client.anonymize_unique(first_sc["text"], app_id=pqc_app_id)
        anon2 = self.client.anonymize_unique(first_sc["text"], app_id=pqc_app_id)
        tokens1 = set(anon1.get("encrypt_mapping", {}).keys())
        tokens2 = set(anon2.get("encrypt_mapping", {}).keys())
        self.pqc_encrypt_nondeterministic = {
            "tokens_found": bool(tokens1 and tokens2),
            "different": not (tokens1 & tokens2) if tokens1 and tokens2 else False,
        }

        anon_passed = sum(1 for r in self.pqc_encrypt_results if r["passed"])
        noflag_passed = sum(1 for r in self.pqc_encrypt_no_flag if r["passed"])
        flag_passed = sum(1 for r in self.pqc_encrypt_with_flag if r["passed"])
        total_tests = len(pqc_scenarios)
        self._log(
            "PQC Encrypt",
            f"Anon={anon_passed}/{total_tests} "
            f"NoFlag={noflag_passed}/{total_tests} "
            f"WithFlag={flag_passed}/{total_tests}",
        )

    def run_fake_strategy(self) -> None:
        """Test the 'fake' anonymization strategy.

        Covers: format-preserving fake replacement, within-request
        consistency, round-trip deanonymization, format validation,
        and mixed strategies with fake.
        """
        fake_app_id = self.client.ensure_app("__testFakeStrategyTestApp")

        # Configure entity types to use "fake"
        for etype in ["IN_DRIVING_LICENSE", "EMAIL_ADDRESS", "PHONE_NUMBER"]:
            self.client.update_app_config(fake_app_id, etype, "fake")

        fake_scenarios = [
            {
                "label": "DL number (space-separated)",
                "text": "My driving license is MH 14 2019 0012345.",
                "pii_values": ["MH 14 2019 0012345"],
                "pii_types": ["IN_DRIVING_LICENSE"],
            },
            {
                "label": "DL number (hyphen-separated)",
                "text": "Licence number KA-09-2023-5554321 is on file.",
                "pii_values": ["KA-09-2023-5554321"],
                "pii_types": ["IN_DRIVING_LICENSE"],
            },
            {
                "label": "DL with verification context",
                "text": (
                    "Please verify driving licence UP 16 2020 1234567 "
                    "against the government database."
                ),
                "pii_values": ["UP 16 2020 1234567"],
                "pii_types": ["IN_DRIVING_LICENSE"],
            },
            {
                "label": "Email address (Gmail)",
                "text": "Contact sneha.patil@gmail.com for details.",
                "pii_values": ["sneha.patil@gmail.com"],
                "pii_types": ["EMAIL_ADDRESS"],
            },
            {
                "label": "Email address (corporate)",
                "text": "Send documents to ravi.kumar@woodgrovebank.co.in please.",
                "pii_values": ["ravi.kumar@woodgrovebank.co.in"],
                "pii_types": ["EMAIL_ADDRESS"],
            },
            {
                "label": "Phone number (+91 space)",
                "text": "Reach me at +91 9823567890 for updates.",
                "pii_values": ["+91 9823567890"],
                "pii_types": ["PHONE_NUMBER"],
            },
            {
                "label": "Phone number (+91 hyphen)",
                "text": "Call the branch at +91-8765432109 for enquiries.",
                "pii_values": ["+91-8765432109"],
                "pii_types": ["PHONE_NUMBER"],
            },
            {
                "label": "Phone number (landline 0XX-XXXXXXXX)",
                "text": "Office landline: 022-24567890 available 9-5.",
                "pii_values": ["022-24567890"],
                "pii_types": ["PHONE_NUMBER"],
            },
            {
                "label": "Multi-entity: DL + Email + Phone",
                "text": (
                    "DL: KA-09-2023-5554321, "
                    "email: sneha.patil@example.com, phone: +91 9876543210."
                ),
                "pii_values": [
                    "KA-09-2023-5554321",
                    "sneha.patil@example.com",
                    "+91 9876543210",
                ],
                "pii_types": [
                    "IN_DRIVING_LICENSE",
                    "EMAIL_ADDRESS",
                    "PHONE_NUMBER",
                ],
            },
            {
                "label": "Banking KYC: DL + Email + Phone",
                "text": (
                    "KYC for DL: TN 01 2018 9990001, "
                    "email: amit.sharma@gmail.com, phone: +91 9123456789. "
                    "Please complete the verification process."
                ),
                "pii_values": [
                    "TN 01 2018 9990001",
                    "amit.sharma@gmail.com",
                    "+91 9123456789",
                ],
                "pii_types": [
                    "IN_DRIVING_LICENSE",
                    "EMAIL_ADDRESS",
                    "PHONE_NUMBER",
                ],
            },
            {
                "label": "Loan application: DL + Email + Phone",
                "text": (
                    "Loan applicant, DL: GJ-15-2021-7771234, "
                    "reachable at deepa.verma@yahoo.com or +91 7012345678."
                ),
                "pii_values": [
                    "GJ-15-2021-7771234",
                    "deepa.verma@yahoo.com",
                    "+91 7012345678",
                ],
                "pii_types": [
                    "IN_DRIVING_LICENSE",
                    "EMAIL_ADDRESS",
                    "PHONE_NUMBER",
                ],
            },
            {
                "label": "Insurance claim: DL + Phone",
                "text": (
                    "Claimant DL: DL 07 2015 0099887, "
                    "contact +91 9988776655 for survey scheduling."
                ),
                "pii_values": [
                    "DL 07 2015 0099887",
                    "+91 9988776655",
                ],
                "pii_types": [
                    "IN_DRIVING_LICENSE",
                    "PHONE_NUMBER",
                ],
            },
        ]

        # ── Phase 1: Basic anonymization — originals replaced ───────────
        anon_cache: dict[str, dict] = {}
        for sc in fake_scenarios:
            anon = self.client.anonymize(sc["text"], app_id=fake_app_id)
            anon_cache[sc["label"]] = anon
            entity_mapping = anon.get("entity_mapping", {})

            all_hidden = all(
                pv not in anon["anonymized_text"] for pv in sc["pii_values"]
            )
            originals_in_mapping = list(entity_mapping.values())
            all_mapped = all(
                pv in originals_in_mapping for pv in sc["pii_values"]
            )

            self.fake_strategy_results.append({
                "label": sc["label"],
                "original_text": sc["text"],
                "anonymized_text": anon["anonymized_text"],
                "entity_mapping": dict(entity_mapping),
                "fake_count": len(entity_mapping),
                "expected_count": len(sc["pii_values"]),
                "all_hidden": all_hidden,
                "all_mapped": all_mapped,
                "passed": all_hidden and all_mapped,
            })

        # ── Phase 2: Format validation — fakes look realistic ───────────
        format_checks = [
            {
                "label": "DL space-separated format",
                "text": "My driving license is MH 14 2019 0012345.",
                "entity_type": "IN_DRIVING_LICENSE",
                "original": "MH 14 2019 0012345",
                "pattern": r"[A-Z]{2} \d{2} \d{4} \d{7}",
            },
            {
                "label": "DL hyphen-separated format",
                "text": "Licence number KA-09-2023-5554321 is on file.",
                "entity_type": "IN_DRIVING_LICENSE",
                "original": "KA-09-2023-5554321",
                "pattern": r"[A-Z]{2}-\d{2}-\d{4}-\d{7}",
            },
            {
                "label": "DL another state (space)",
                "text": "Verify DL UP 16 2020 1234567 for the applicant.",
                "entity_type": "IN_DRIVING_LICENSE",
                "original": "UP 16 2020 1234567",
                "pattern": r"[A-Z]{2} \d{2} \d{4} \d{7}",
            },
            {
                "label": "Email format (user@domain.tld)",
                "text": "Email: test.user@example.com for verification.",
                "entity_type": "EMAIL_ADDRESS",
                "original": "test.user@example.com",
                "pattern": r"[a-z]+@[a-z]+\.[a-z]+",
            },
            {
                "label": "Email format (corporate domain)",
                "text": "Notify ravi.kumar@woodgrovebank.co.in about the update.",
                "entity_type": "EMAIL_ADDRESS",
                "original": "ravi.kumar@woodgrovebank.co.in",
                "pattern": r"[a-z]+@[a-z]+\.[a-z]+",
            },
            {
                "label": "Phone +91 space format",
                "text": "Call +91 9876543210 immediately.",
                "entity_type": "PHONE_NUMBER",
                "original": "+91 9876543210",
                "pattern": r"\+91[ -][7-9]\d{9}",
            },
            {
                "label": "Phone +91 hyphen format",
                "text": "Reach +91-8765432109 for account queries.",
                "entity_type": "PHONE_NUMBER",
                "original": "+91-8765432109",
                "pattern": r"\+91[ -][7-9]\d{9}",
            },
        ]

        for fc in format_checks:
            anon = self.client.anonymize_unique(fc["text"], app_id=fake_app_id)
            mapping = anon.get("entity_mapping", {})
            fake_value = None
            for fv, orig in mapping.items():
                if orig == fc["original"]:
                    fake_value = fv
                    break
            format_valid = (
                bool(re.match(fc["pattern"], fake_value))
                if fake_value
                else False
            )
            self.fake_format_results.append({
                "label": fc["label"],
                "original_text": fc["text"],
                "anonymized_text": anon.get("anonymized_text", ""),
                "entity_mapping": dict(mapping),
                "original": fc["original"],
                "fake_value": fake_value or "(not found)",
                "pattern": fc["pattern"],
                "format_valid": format_valid,
                "passed": format_valid,
            })

        # ── Phase 3: Consistency — same value → same fake in one request ─
        consistency_cases = [
            {
                "label": "Same DL (spaces) appears twice",
                "text": (
                    "Primary DL: MH 14 2019 0012345. "
                    "Confirm DL: MH 14 2019 0012345."
                ),
                "repeated_value": "MH 14 2019 0012345",
            },
            {
                "label": "Same DL (hyphens) appears twice",
                "text": (
                    "Applicant DL: KA-09-2023-5554321. "
                    "Verified DL: KA-09-2023-5554321."
                ),
                "repeated_value": "KA-09-2023-5554321",
            },
            {
                "label": "Same email appears twice",
                "text": (
                    "Send to sneha@example.com and cc sneha@example.com."
                ),
                "repeated_value": "sneha@example.com",
            },
            {
                "label": "Same phone appears twice",
                "text": (
                    "Primary contact: +91 9876543210. "
                    "Alternate: +91 9876543210."
                ),
                "repeated_value": "+91 9876543210",
            },
        ]

        for cc in consistency_cases:
            anon = self.client.anonymize_unique(cc["text"], app_id=fake_app_id)
            mapping = anon.get("entity_mapping", {})
            # Find how many fake values map to the repeated original
            fakes_for_value = [
                fv for fv, orig in mapping.items()
                if orig == cc["repeated_value"]
            ]
            # Should be exactly 1 (same original → same fake)
            consistent = len(fakes_for_value) == 1
            # The fake value should appear twice in anonymized text
            if fakes_for_value:
                count_in_text = anon["anonymized_text"].count(fakes_for_value[0])
            else:
                count_in_text = 0

            self.fake_consistency_results.append({
                "label": cc["label"],
                "original_text": cc["text"],
                "anonymized_text": anon.get("anonymized_text", ""),
                "entity_mapping": dict(mapping),
                "repeated_value": cc["repeated_value"],
                "fake_value": fakes_for_value[0] if fakes_for_value else "(none)",
                "unique_fakes": len(fakes_for_value),
                "occurrences_in_text": count_in_text,
                "consistent": consistent,
                "appears_twice": count_in_text == 2,
                "passed": consistent and count_in_text == 2,
            })

        # ── Phase 4: Round-trip deanonymization ─────────────────────────
        for sc in fake_scenarios:
            anon = self.client.anonymize_unique(sc["text"], app_id=fake_app_id)
            restored = self.client.deanonymize(
                anon["id"], anon["anonymized_text"], app_id=fake_app_id
            )
            pii_restored = all(
                pv in restored["text"] for pv in sc["pii_values"]
            )
            exact_match = sc["text"] == restored["text"]

            self.fake_round_trip_results.append({
                "label": sc["label"],
                "original_text": sc["text"],
                "anonymized_text": anon.get("anonymized_text", ""),
                "restored_text": restored.get("text", ""),
                "entity_mapping": dict(anon.get("entity_mapping", {})),
                "pii_restored": pii_restored,
                "exact": exact_match,
                "passed": pii_restored,
            })

        # ── Phase 5: Mixed strategies — fake + hash + encrypt together ──
        mixed_app_id = self.client.ensure_app("__testMixedWithFakeApp")
        self.client.update_app_config(mixed_app_id, "IN_DRIVING_LICENSE", "fake")
        self.client.update_app_config(mixed_app_id, "EMAIL_ADDRESS", "hash")
        self.client.update_app_config(mixed_app_id, "PHONE_NUMBER", "encrypt")

        mixed_scenarios = [
            {
                "label": "DL=fake, Email=hash, Phone=encrypt",
                "text": (
                    "Client DL: MH 14 2019 0012345, "
                    "email: test@example.com, phone: +91 9876543210."
                ),
                "dl_value": "MH 14 2019 0012345",
                "email_value": "test@example.com",
                "phone_value": "+91 9876543210",
            },
        ]

        for ms in mixed_scenarios:
            anon = self.client.anonymize_unique(ms["text"], app_id=mixed_app_id)
            entity_map = anon.get("entity_mapping", {})
            hash_map = anon.get("hash_mapping", {})
            encrypt_map = anon.get("encrypt_mapping", {})

            # DL should be in entity_mapping (fake)
            dl_in_fake = ms["dl_value"] in entity_map.values()
            # Email should be in hash_mapping
            email_in_hash = ms["email_value"] in hash_map.values()
            # Phone should be in encrypt_mapping
            phone_in_encrypt = ms["phone_value"] in encrypt_map.values()

            # All originals should be hidden
            all_hidden = all(
                v not in anon["anonymized_text"]
                for v in [ms["dl_value"], ms["email_value"], ms["phone_value"]]
            )

            self.fake_mixed_results.append({
                "label": ms["label"],
                "original_text": ms["text"],
                "anonymized_text": anon.get("anonymized_text", ""),
                "entity_mapping": dict(entity_map),
                "hash_mapping": dict(hash_map),
                "encrypt_mapping": dict(encrypt_map),
                "dl_in_fake": dl_in_fake,
                "email_in_hash": email_in_hash,
                "phone_in_encrypt": phone_in_encrypt,
                "all_hidden": all_hidden,
                "passed": dl_in_fake and email_in_hash and phone_in_encrypt and all_hidden,
            })

        anon_passed = sum(1 for r in self.fake_strategy_results if r["passed"])
        fmt_passed = sum(1 for r in self.fake_format_results if r["passed"])
        cons_passed = sum(1 for r in self.fake_consistency_results if r["passed"])
        rt_passed = sum(1 for r in self.fake_round_trip_results if r["passed"])
        mix_passed = sum(1 for r in self.fake_mixed_results if r["passed"])
        self._log(
            "Fake Strategy",
            f"Anon={anon_passed}/{len(self.fake_strategy_results)} "
            f"Format={fmt_passed}/{len(self.fake_format_results)} "
            f"Consist={cons_passed}/{len(self.fake_consistency_results)} "
            f"RT={rt_passed}/{len(self.fake_round_trip_results)} "
            f"Mixed={mix_passed}/{len(self.fake_mixed_results)}",
        )

    def run_cleanup(self) -> None:
        self._log("Cleanup", "skipped — __test apps retained")

    def run_allow_lists(self) -> None:
        """Test entity-type and entity-keyword allow-lists."""
        al_app_id = self.client.ensure_app("__testAllowListApp")
        test_text = (
            "Customer Rajesh Kumar at Contoso Bank, India. "
            "PAN: ABCPS7234F. Email: rajesh@example.com. Mobile: 9845012345."
        )

        # ── Test 1: Entity-type allow-list (exclude ORGANIZATION) ────────
        self.client.set_entity_type_allow_list(al_app_id, ["ORGANIZATION"])
        r1 = self.client.anonymize(test_text, app_id=al_app_id)
        et_types = {p.strip("{}").rsplit("_", 1)[0] for p in r1["entity_mapping"]}
        et_org_excluded = "ORGANIZATION" not in et_types
        et_person_present = "PERSON" in et_types

        # ── Test 2: Entity-keyword allow-list ────────────────────────────
        self.client.set_entity_type_allow_list(al_app_id, [])
        self.client.set_entity_keyword_allow_list(al_app_id, {
            "LOCATION": ["India"],
        })
        r2 = self.client.anonymize(test_text, app_id=al_app_id)
        ekw_text = r2["anonymized_text"]
        ekw_india_visible = "India" in ekw_text
        ekw_person_masked = "Rajesh Kumar" not in ekw_text

        # ── Test 3: Entity-keyword doesn't affect other entity types ─────
        ekw_email_masked = "rajesh@example.com" not in ekw_text

        # ── Test 4: Combined — entity-type + entity-keyword together ─────
        self.client.set_entity_type_allow_list(al_app_id, ["EMAIL_ADDRESS"])
        r3 = self.client.anonymize(test_text, app_id=al_app_id)
        combined_text = r3["anonymized_text"]
        combined_email_visible = "rajesh@example.com" in combined_text
        combined_india_visible = "India" in combined_text
        combined_person_masked = "Rajesh Kumar" not in combined_text

        # ── Test 5: Round-trip still works with allow-lists ──────────────
        rt_result = self.client.deanonymize(r3["id"], r3["anonymized_text"], app_id=al_app_id)
        rt_exact = rt_result["text"] == test_text

        # ── Test 6: Verify GET endpoints return stored values ────────────
        stored_et = self.client.get_entity_type_allow_list(al_app_id)
        stored_ekw = self.client.get_entity_keyword_allow_list(al_app_id)
        get_et_ok = stored_et == ["EMAIL_ADDRESS"]
        get_ekw_ok = stored_ekw == {"LOCATION": ["India"]}

        # ── Cleanup ──────────────────────────────────────────────────────
        self.client.set_entity_type_allow_list(al_app_id, [])
        self.client.set_entity_keyword_allow_list(al_app_id, {})

        # ── Store results for HTML report ────────────────────────────────
        tests = [
            ("ET: ORGANIZATION excluded", et_org_excluded),
            ("ET: PERSON still detected", et_person_present),
            ("EKW: 'India' visible as LOCATION", ekw_india_visible),
            ("EKW: PERSON still masked", ekw_person_masked),
            ("EKW: EMAIL still masked", ekw_email_masked),
            ("Combined: EMAIL visible (ET allow)", combined_email_visible),
            ("Combined: India visible (EKW allow)", combined_india_visible),
            ("Combined: PERSON masked", combined_person_masked),
            ("Combined: round-trip exact", rt_exact),
            ("GET: entity-type stored", get_et_ok),
            ("GET: entity-keyword stored", get_ekw_ok),
        ]
        self.allow_list_results = {
            "tests": tests,
            "original_text": test_text,
            "scenarios": [
                {
                    "name": "Entity-Type Allow-List: Exclude ORGANIZATION",
                    "config": {"entity_type_allow_list": ["ORGANIZATION"], "entity_keyword_allow_list": {}},
                    "anonymized_text": r1["anonymized_text"],
                    "entity_mapping": r1["entity_mapping"],
                },
                {
                    "name": "Entity-Keyword Allow-List: Allow 'India' as LOCATION",
                    "config": {"entity_type_allow_list": [], "entity_keyword_allow_list": {"LOCATION": ["India"]}},
                    "anonymized_text": r2["anonymized_text"],
                    "entity_mapping": r2["entity_mapping"],
                },
                {
                    "name": "Combined: ET=EMAIL_ADDRESS + EKW=LOCATION:India",
                    "config": {"entity_type_allow_list": ["EMAIL_ADDRESS"], "entity_keyword_allow_list": {"LOCATION": ["India"]}},
                    "anonymized_text": r3["anonymized_text"],
                    "entity_mapping": r3["entity_mapping"],
                    "deanonymized_text": rt_result["text"],
                },
            ],
        }

        passed = sum(1 for _, ok in tests if ok)
        total = len(tests)
        self._log("Allow-Lists", f"{passed}/{total} passed")
        if passed < total:
            for label, ok in tests:
                if not ok:
                    self._log("Allow-Lists", f"  ❌ FAIL: {label}")

    # ── Orchestrator ─────────────────────────────────────────────────────

    def run(self) -> None:
        start = time.monotonic()
        print("\n🛡️  PII Shield — Indian Banking Test Suite\n")

        sections = [
            ("Setup", self.run_setup),
            # Strategy 1: Replace (default)
            ("Anonymization", self.run_anonymization),
            ("Coverage", self.run_coverage),
            ("Round-Trip", self.run_round_trips),
            # Strategy 2: Hash
            ("Hash DL Strategy", self.run_hash_dl),
            ("Hash Phone Strategy", self.run_hash_phone),
            # Strategy 3: Encrypt
            ("Encrypt DL Strategy", self.run_encrypt_dl),
            ("Encrypt Email Strategy", self.run_encrypt_email),
            # Strategy 4: PQC Encrypt
            ("PQC Encrypt", self.run_pqc_encrypt),
            # Strategy 5: Fake
            ("Fake Strategy", self.run_fake_strategy),
            # Other tests
            ("Multi-Tenant Apps", self.run_multi_tenant),
            ("Mixed-Strategy Apps", self.run_mixed_strategy_apps),
            ("Edge Cases", self.run_edge_cases),
            ("LLM Sandwich", self.run_llm_sandwich),
            ("Structured Data", self.run_structured_data),
            ("Address Indicator", self.run_address_indicator),
            ("Case Robustness", self.run_case_robustness),
            ("Address Completeness", self.run_address_completeness),
            ("Account vs Phone", self.run_account_phone_disambiguation),
            ("Person Initials", self.run_person_initials),
            ("Person Titles", self.run_person_titles),
            ("NRP Alignment", self.run_nrp_alignment),
            ("Multi-line Context", self.run_multiline_context),
            ("Mixed-case Names", self.run_mixed_case_names),
            ("Address Units", self.run_address_units),
            ("Key-value Context", self.run_key_value_context),
            ("Geo-Coordinates", self.run_geo_coordinates),
            ("NRP Detection", self.run_nrp),
            ("US Entities", self.run_us_entities),
            ("CKYC/PRAN/APAAR", self.run_ckyc_pran_apaar),
            ("Customer ID", self.run_customer_id),
            ("Allow-Lists", self.run_allow_lists),
            ("Cleanup", self.run_cleanup),
        ]

        for name, fn in sections:
            try:
                fn()
            except Exception as exc:
                print(f"  ❌ {name} FAILED: {exc}")

        self.elapsed = time.monotonic() - start
        print(f"\n  Done in {self.elapsed:.1f}s\n")


# ── HTML Report Generator ───────────────────────────────────────────────────

H = html_escape  # shorthand


def _icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _icon3(val: float) -> str:
    if val >= 100:
        return "✅"
    if val >= 50:
        return "⚠️"
    return "❌"


def _status_class(ok: bool) -> str:
    return "pass" if ok else "fail"


def _render_io_detail(
    w,
    label: str,
    passed: bool,
    badge: str,
    record: dict,
    extra_rows: list[tuple[str, str]] | None = None,
) -> None:
    """Render one collapsible card with the exact input/output text pair.

    ``record`` must carry ``full_text``, ``full_anonymized``, ``mapping`` and
    ``restored``; the summary tables above truncate, so this is where the
    untruncated pair lives.
    """
    cls = "pass" if passed else "fail"
    w(f'<details class="scenario-card {cls}">')
    w(f"<summary>{H(label)} &nbsp; {_icon(passed)} &nbsp; "
      f"<small>{badge}</small></summary>")
    w(f'<div class="text-block"><strong>Input:</strong><br>'
      f'<pre style="white-space:pre-wrap;margin:0;">{H(record["full_text"])}</pre></div>')
    w(f'<div class="text-block"><strong>Output (anonymized):</strong><br>'
      f'<pre style="white-space:pre-wrap;margin:0;">{H(record["full_anonymized"])}</pre></div>')

    mapping = record.get("mapping") or {}
    if mapping:
        w("<table><thead><tr><th>Placeholder</th><th>Original</th>"
          "</tr></thead><tbody>")
        for placeholder, original in mapping.items():
            w(f"<tr><td><code>{H(placeholder)}</code></td>"
              f"<td>{H(original)}</td></tr>")
        w("</tbody></table>")
    else:
        w("<p><em>No entities detected.</em></p>")

    restored = record.get("restored", "")
    exact = restored == record["full_text"]
    colour = "#d4edda" if exact else "#f8d7da"
    w("<div class=\"text-block\"><strong>De-anonymized (round-trip):</strong><br>"
      f'<pre style="background:{colour};padding:8px;border-radius:4px;'
      f'white-space:pre-wrap;margin:0;">{H(restored)}</pre></div>')
    w(f"<p>{'✅ Exact match' if exact else '❌ Mismatch'}</p>")

    for row_label, row_value in extra_rows or []:
        w(f"<p><strong>{H(row_label)}:</strong> {H(row_value)}</p>")

    w("</details>")


def _render_expectation_results(
    w,
    results: list[dict],
    kept_label: str,
    noun: str,
    detail_heading: str,
) -> None:
    """Render the table and detail cards for ``TestRunner._check_expectations``."""
    if not results:
        return
    w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
      "<th>Expected</th><th>Detected</th><th>No leak</th>"
      f"<th>Correct type</th><th>{H(kept_label)}</th><th>Round-trip</th>"
      "<th>Result</th></tr></thead><tbody>")
    for r in results:
        w(f'<tr><td>{H(r["label"])}</td>'
          f'<td><small>{H(r["text"])}</small></td>'
          f'<td><small>{H(r["expected"])}</small></td>'
          f'<td><small>{H(r["actual"])}</small></td>'
          f'<td>{_icon(r["no_leak"])}</td>'
          f'<td>{_icon(r["correct_type"])}</td>'
          f'<td>{_icon(r["kept"])}</td>'
          f'<td>{_icon(r["round_trip"])}</td>'
          f'<td>{_icon(r["passed"])}</td></tr>')
    w("</tbody></table>")
    passed = sum(1 for r in results if r["passed"])
    w(f"<p><strong>{passed}/{len(results)}</strong> {H(noun)} tests passed.</p>")

    w(f"<h3>{H(detail_heading)}</h3>")
    for r in results:
        _render_io_detail(
            w,
            label=r["label"],
            passed=r["passed"],
            badge=(
                f'{_icon(r["no_leak"])} no leak &nbsp; '
                f'{_icon(r["correct_type"])} correct type &nbsp; '
                f'{_icon(r["kept"])} {H(kept_label.lower())} &nbsp; '
                f'{_icon(r["round_trip"])} round-trip'
            ),
            record=r,
            extra_rows=[
                ("Expected", r["expected"]),
                ("Detected", r["actual"]),
                ("Leaked", r["leaked"]),
                ("Must stay in output", r["must_keep"]),
            ],
        )


def generate_html_report(runner: TestRunner, output_path: str) -> None:
    """Render the collected test results as a self-contained HTML file."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total_scenarios = len(runner.scenarios)

    # Compute summary stats
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
    hashed_inexact = sum(
        1
        for r in runner.round_trip_results
        if r.has_hashed_entities and not r.exact_match
    )
    failures = total_scenarios - exact_rt - hashed_inexact
    overall_cov = runner.coverage.get("__overall__", {})
    edge_passed = sum(1 for e in runner.edge_cases if e.passed)

    retail_exact = sum(1 for r in runner.app_round_trip if r.retail_exact)
    audit_exact = sum(1 for r in runner.app_round_trip if r.audit_exact)

    struct_exact = sum(1 for r in runner.structured_rt if r.exact_match)
    struct_valid = sum(1 for r in runner.structured_rt if r.structure_valid)

    # New strategy-specific stats
    encrypt_email_restored = sum(
        1 for r in runner.encrypt_email_with_flag if r.get("exact")
    )
    hash_phone_restored = sum(
        1 for r in runner.hash_phone_with_flag if r.get("exact")
    )
    comp_default_exact = sum(
        1 for r in runner.mixed_strategy_results
        if r.get("compliance_default_exact")
    )
    comp_full_exact = sum(
        1 for r in runner.mixed_strategy_results
        if r.get("compliance_full_exact")
    )
    supp_default_exact = sum(
        1 for r in runner.mixed_strategy_results
        if r.get("support_default_exact")
    )

    # ── Build HTML ───────────────────────────────────────────────────────
    parts: list[str] = []

    def w(line: str = "") -> None:
        parts.append(line)

    w("<!DOCTYPE html>")
    w('<html lang="en">')
    w("<head>")
    w('<meta charset="UTF-8">')
    w('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    w("<title>PII Shield — Indian Banking Test Report</title>")
    w("<style>")
    w(CSS)
    w("</style>")
    w("</head>")
    w("<body>")
    w('<div class="container">')

    # Header
    w('<h1>🛡️ PII Shield — Indian Banking Test Report</h1>')
    w(f'<p class="timestamp">Generated: {ts} &nbsp;|&nbsp; '
      f'Duration: {runner.elapsed:.1f}s &nbsp;|&nbsp; '
      f'Scenarios: {total_scenarios}</p>')

    # ── Summary Dashboard ────────────────────────────────────────────────
    w('<h2 id="summary">📊 Summary Dashboard</h2>')
    w('<div class="dashboard">')
    for label, value in [
        ("Scenarios", str(total_scenarios)),
        ("Entities Detected", str(total_entities)),
        ("Entity Types", str(len(unique_types))),
        (
            "Coverage",
            f"{overall_cov.get('hits', 0)}/{overall_cov.get('checks', 0)} "
            f"({overall_cov.get('rate', 0):.0f}%)",
        ),
        ("Exact Round-Trips", f"{exact_rt}/{total_scenarios}"),
        ("Edge Cases", f"{edge_passed}/{len(runner.edge_cases)}"),
        ("Retail App RT", f"{retail_exact}/{total_scenarios}"),
        ("Audit App RT", f"{audit_exact}/{total_scenarios}"),
        (
            "Encrypt Email RT",
            f"{encrypt_email_restored}/{len(runner.encrypt_email_with_flag)}"
            if runner.encrypt_email_with_flag else "—",
        ),
        (
            "Hash Phone RT",
            f"{hash_phone_restored}/{len(runner.hash_phone_with_flag)}"
            if runner.hash_phone_with_flag else "—",
        ),
        (
            "Compliance App",
            f"{comp_full_exact}/{total_scenarios}"
            if runner.mixed_strategy_results else "—",
        ),
        (
            "Support App",
            f"{supp_default_exact}/{total_scenarios}"
            if runner.mixed_strategy_results else "—",
        ),
    ]:
        w(f'<div class="card"><div class="card-value">{value}</div>'
          f'<div class="card-label">{label}</div></div>')
    w("</div>")

    # ── 1. Replace Strategy (Default) ────────────────────────────────────
    w('<h2 id="replace-strategy">1. Replace Strategy (Default)</h2>')
    w("<p>The default anonymization strategy replaces detected PII with "
      "type-tagged placeholders (e.g. <code>&lt;PERSON_1&gt;</code>). "
      "Fully reversible via de-anonymization.</p>")
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td>indian-banking-tests</td><td><em>All entity types</em></td>'
      '<td><code>replace</code> (default)</td></tr>'
      '</tbody></table></div>')

    # ── Section 1a: Anonymization Results ────────────────────────────────
    w('<h3 id="anonymization">1a. Anonymization Results</h3>')
    for s in runner.scenarios:
        sr = runner.anon_results[s["id"]]
        matched = len(sr.matched_types)
        expected_count = len(sr.expected_types)
        status = "pass" if matched == expected_count else "warn" if matched > 0 else "fail"
        coverage_icon = _icon(matched == expected_count) if matched == expected_count else "⚠️" if matched > 0 else "❌"
        w(f'<details class="scenario-card {status}">')
        w(f"<summary>{H(sr.scenario_name)} &nbsp; {coverage_icon} "
          f"{matched}/{expected_count} types</summary>")
        w(f'<div class="text-block"><strong>Original:</strong><br>'
          f"{H(sr.original_text)}</div>")
        w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
          f"{H(sr.anonymized_text)}</div>")

        if sr.entities_found:
            w("<table><thead><tr><th>Entity Type</th><th>Value</th>"
              "<th>Score</th></tr></thead><tbody>")
            for e in sorted(sr.entities_found, key=lambda x: x["start"]):
                val = sr.original_text[e["start"]: e["end"]]
                w(f"<tr><td><code>{H(e['entity_type'])}</code></td>"
                  f"<td>{H(val)}</td><td>{e['score']:.2f}</td></tr>")
            w("</tbody></table>")

        w(f"<p><strong>Coverage:</strong> {matched}/{expected_count} expected types</p>")
        if sr.missed_types:
            w(f'<p class="missed">⚠️ Missed: '
              f'{", ".join(f"<code>{H(m)}</code>" for m in sorted(sr.missed_types))}</p>')
        if sr.extra_types:
            w(f'<p class="extra">ℹ️ Bonus: '
              f'{", ".join(f"<code>{H(e)}</code>" for e in sorted(sr.extra_types))}</p>')
        w("</details>")

    # ── Section 1b: Detection Coverage ──────────────────────────────────
    w('<h3 id="coverage">1b. Detection Coverage by Entity Type</h3>')
    w("<table><thead><tr><th>Entity Type</th><th>Expected In</th>"
      "<th>Detected In</th><th>Hit Rate</th></tr></thead><tbody>")
    for etype, stats in runner.coverage.items():
        if etype == "__overall__":
            continue
        rate = stats["rate"]
        icon = _icon3(rate)
        w(f"<tr><td><code>{H(etype)}</code></td>"
          f"<td>{stats['expected']} scenarios</td>"
          f"<td>{stats['detected']} scenarios</td>"
          f"<td>{icon} {rate:.0f}%</td></tr>")
    w("</tbody></table>")
    w(f"<p><strong>Overall: {overall_cov.get('hits', 0)}/{overall_cov.get('checks', 0)} "
      f"entity-scenario checks passed ({overall_cov.get('rate', 0):.0f}%)</strong></p>")

    # ── Section 1c: Round-Trip Results ──────────────────────────────────
    w('<h3 id="roundtrip">1c. Anonymize → De-anonymize Round-Trip</h3>')
    w("<table><thead><tr><th>#</th><th>Scenario</th><th>Exact Match</th>"
      "<th>Status</th></tr></thead><tbody>")
    for i, r in enumerate(runner.round_trip_results, 1):
        if r.exact_match:
            status_txt = "✅ Perfect"
        else:
            status_txt = "❌ Mismatch"
        cls = "pass" if r.exact_match else "fail"
        w(f'<tr class="{cls}"><td>{i}</td><td>{H(r.scenario_name)}</td>'
          f"<td>{_icon(r.exact_match)}</td>"
          f"<td>{status_txt}</td></tr>")
    w("</tbody></table>")
    w(f"<p><strong>{exact_rt}/{total_scenarios} exact round-trips</strong></p>")

    # ── Section 1d: Detailed Round-Trip ─────────────────────────────────
    w('<h3 id="roundtrip-detail">1d. Detailed Round-Trip Inspection</h3>')
    for r in runner.round_trip_results:
        cls = "pass" if r.exact_match else "warn" if r.has_hashed_entities else "fail"
        w(f'<details class="scenario-card {cls}">')
        w(f"<summary>{H(r.scenario_name)} &nbsp; {_icon(r.exact_match)}</summary>")
        if r.entity_mapping:
            w("<table><thead><tr><th>Placeholder</th><th>Original Value</th>"
              "</tr></thead><tbody>")
            for ph, orig in r.entity_mapping.items():
                w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
            w("</tbody></table>")
        else:
            w("<p><em>No reversible entities</em></p>")
        w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
          f"{H(r.anonymized)}</div>")
        w(f'<div class="text-block"><strong>Restored:</strong><br>'
          f"{H(r.restored)}</div>")
        if r.exact_match:
            w("<p>✅ <strong>Exact match with original</strong></p>")
        elif r.has_hashed_entities:
            w("<p>⚠️ <strong>Inexact — hashed entities are irreversible (expected)</strong></p>")
        else:
            w("<p>❌ <strong>Mismatch — see below:</strong></p>")
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f"{H(r.original)}</div>")
        w("</details>")

    # ── 2. Hash Strategy ─────────────────────────────────────────────────
    w('<h2 id="hash-strategy">2. Hash Strategy</h2>')
    w("<p>Hashing replaces PII with a one-way cryptographic digest. "
      "The original value cannot be recovered — useful for irreversible "
      "pseudonymisation of identifiers.</p>")
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '<th>De-anonymize Flag</th></tr></thead><tbody>'
      '<tr><td>HashDLTestApp</td><td><code>IN_DRIVING_LICENSE</code></td>'
      '<td><code>hash</code></td>'
      '<td><code>include_hashed=false</code> (default — irreversible)</td></tr>'
      '<tr><td>HashPhoneTestApp</td><td><code>PHONE_NUMBER</code></td>'
      '<td><code>hash</code></td>'
      '<td>Tested with both <code>include_hashed=false</code> and '
      '<code>include_hashed=true</code></td></tr>'
      '</tbody></table></div>')

    # Helper for hash detailed inspection cards
    def _render_hash_detail(
        detail_list: list[dict], restored_label: str,
        success_key: str = "exact", pass_msg: str = "Exact round-trip match",
        fail_msg: str = "Inexact — hashed entities are irreversible (expected)",
        fail_style: str = "warn",
    ) -> None:
        for d in detail_list:
            ok = d[success_key]
            cls = "pass" if ok else fail_style
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(d["scenario"])} &nbsp; {_icon(ok)}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(d["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(d["anonymized_text"])}</div>')
            w(f'<div class="text-block"><strong>{restored_label}:</strong><br>'
              f'{H(d["restored_text"])}</div>')
            if d["entity_mapping"]:
                w("<table><thead><tr><th>Placeholder</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for ph, orig in d["entity_mapping"].items():
                    w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            if d["hash_mapping"]:
                w("<table><thead><tr><th>Hash Token</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for tok, orig in d["hash_mapping"].items():
                    w(f"<tr><td><code>{H(tok[:40])}…</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            else:
                w("<p><em>No hashed entities</em></p>")
            if ok:
                w(f"<p>✅ <strong>{pass_msg}</strong></p>")
            else:
                w(f"<p>⚠️ <strong>{fail_msg}</strong></p>")
            w("</details>")

    # ── Section 2a: Hash DL Strategy ────────────────────────────────────
    w('<h3 id="hash-dl">2a. Driving Licenses</h3>')
    w("<p>DL numbers are hashed (one-way). De-anonymize without "
      "<code>include_hashed</code> will <strong>not</strong> restore the original — "
      "this is expected behaviour.</p>")
    if runner.hash_dl_results:
        w("<table><thead><tr><th>Scenario</th><th>Description</th><th>DL Hashed?</th>"
          "<th>DL Info</th></tr></thead><tbody>")
        for r in runner.hash_dl_results:
            w(f'<tr class="{_status_class(r["dl_hashed"])}">'
              f'<td>{H(r["scenario"])}</td>'
              f'<td><small>{H(r["description"])}</small></td>'
              f'<td>{_icon(r["dl_hashed"])}</td>'
              f'<td>{H(r["dl_info"])}</td></tr>')
        w("</tbody></table>")

    # ── Section 2b: Driving Licenses — Detailed Inspection ──────────────
    if runner.hash_dl_detail:
        w('<h3 id="hash-dl-detail">2b. Driving Licenses — Detailed Inspection</h3>')
        _render_hash_detail(
            runner.hash_dl_detail, "Restored (default — no include_hashed flag)",
            success_key="dl_hashed",
            pass_msg="DL successfully hashed — not in entity_mapping",
            fail_msg="DL unexpectedly in entity_mapping",
            fail_style="fail",
        )

    # ── Section 2c: Hash Phone Strategy ─────────────────────────────────
    w('<h3 id="hash-phone">2c. Phone Numbers</h3>')

    if runner.hash_phone_results:
        w("<h4>Anonymization</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Description</th><th>Phone in Entity Mapping?</th>"
          "<th>Hash Count</th></tr></thead><tbody>")
        for r in runner.hash_phone_results:
            status = "⚠️ Yes" if r["phone_in_entity_mapping"] else "✅ Hashed"
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td><small>{H(r["description"])}</small></td><td>{status}</td>'
              f'<td>{r["hash_count"]}</td></tr>')
        w("</tbody></table>")

    if runner.hash_phone_no_flag:
        w("<h4>De-anonymize (include_hashed=False)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Phone Restored?</th>"
          "</tr></thead><tbody>")
        for r in runner.hash_phone_no_flag:
            status = "⚠️ Yes" if r["phone_restored"] else "✅ No (irreversible)"
            w(f'<tr><td>{H(r["scenario"])}</td><td>{status}</td></tr>')
        w("</tbody></table>")

    if runner.hash_phone_with_flag:
        w("<h4>De-anonymize (include_hashed=True)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Phone Restored?</th>"
          "<th>Exact Round-Trip?</th></tr></thead><tbody>")
        for r in runner.hash_phone_with_flag:
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td>{_icon(r["phone_restored"])}</td>'
              f'<td>{_icon(r["exact"])}</td></tr>')
        w("</tbody></table>")

    # ── Section 2d: Phone Numbers — Detailed Inspection ─────────────────
    if runner.hash_phone_detail:
        w('<h3 id="hash-phone-detail">2d. Phone Numbers — Detailed Inspection</h3>')
        _render_hash_detail(runner.hash_phone_detail, "Restored (include_hashed=True)")

    # ── 3. Encrypt Strategy ──────────────────────────────────────────────
    w('<h2 id="encrypt-strategy">3. Encrypt Strategy</h2>')
    w("<p>Encryption replaces PII with a ciphertext token that can be "
      "decrypted during de-anonymization when "
      "<code>include_encrypted=True</code> is specified. "
      "Non-deterministic by design.</p>")
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td>EncryptDLTestApp</td><td><code>IN_DRIVING_LICENSE</code></td>'
      '<td><code>encrypt</code></td></tr>'
      '<tr><td>EncryptEmailTestApp</td><td><code>EMAIL_ADDRESS</code></td>'
      '<td><code>encrypt</code></td></tr>'
      '</tbody></table></div>')

    # ── Section 3a: Encrypt DL Strategy ─────────────────────────────────
    w('<h3 id="encrypt-dl">3a. Driving Licenses</h3>')

    if runner.encrypt_dl_results.get("anon_rows"):
        w("<h4>Anonymization</h4>")
        w("<table><thead><tr><th>Scenario</th><th>DL in Entity Mapping?</th>"
          "<th>Encrypt Count</th></tr></thead><tbody>")
        for r in runner.encrypt_dl_results["anon_rows"]:
            status = "⚠️ Yes" if r["dl_in_mapping"] else "✅ Encrypted"
            w(f'<tr><td>{H(r["scenario"])}</td><td>{status}</td>'
              f'<td>{r["encrypt_count"]}</td></tr>')
        w("</tbody></table>")

    if runner.encrypt_no_flag:
        w("<h4>De-anonymize (include_encrypted=False)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>DL Restored?</th>"
          "<th>Ciphertext Present?</th></tr></thead><tbody>")
        for r in runner.encrypt_no_flag:
            dl_status = "⚠️ Yes" if r["dl_restored"] else "✅ No (secure)"
            w(f'<tr><td>{H(r["scenario"])}</td><td>{dl_status}</td>'
              f'<td>{"Yes" if r["cipher_present"] else "No"}</td></tr>')
        w("</tbody></table>")

    if runner.encrypt_with_flag:
        w("<h4>De-anonymize (include_encrypted=True)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>DL Restored?</th>"
          "<th>Exact Round-Trip?</th></tr></thead><tbody>")
        for r in runner.encrypt_with_flag:
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td>{_icon(r["dl_restored"])}</td>'
              f'<td>{_icon(r["exact"])}</td></tr>')
        w("</tbody></table>")

    nd = runner.encrypt_nondeterministic
    if nd.get("tokens_found"):
        w(f'<p><strong>Non-determinism check:</strong> '
          f'{_icon(nd["different"])} '
          f'{"Confirmed — different ciphertexts" if nd["different"] else "Unexpected — same ciphertexts"}'
          f"</p>")

    # ── Section 3b: Encrypt Email Strategy ──────────────────────────────
    w('<h3 id="encrypt-email">3b. Email Addresses</h3>')

    if runner.encrypt_email_results:
        w("<h4>Anonymization</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Email in Entity Mapping?</th>"
          "<th>Encrypt Count</th></tr></thead><tbody>")
        for r in runner.encrypt_email_results:
            status = "⚠️ Yes" if r["email_in_entity_mapping"] else "✅ Encrypted"
            w(f'<tr><td>{H(r["scenario"])}</td><td>{status}</td>'
              f'<td>{r["encrypt_count"]}</td></tr>')
        w("</tbody></table>")

    if runner.encrypt_email_no_flag:
        w("<h4>De-anonymize (include_encrypted=False)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Email Restored?</th>"
          "<th>Ciphertext Present?</th></tr></thead><tbody>")
        for r in runner.encrypt_email_no_flag:
            status = "⚠️ Yes" if r["email_restored"] else "✅ No (secure)"
            w(f'<tr><td>{H(r["scenario"])}</td><td>{status}</td>'
              f'<td>{"Yes" if r["cipher_present"] else "No"}</td></tr>')
        w("</tbody></table>")

    if runner.encrypt_email_with_flag:
        w("<h4>De-anonymize (include_encrypted=True)</h4>")
        w("<table><thead><tr><th>Scenario</th><th>Email Restored?</th>"
          "<th>Exact Round-Trip?</th></tr></thead><tbody>")
        for r in runner.encrypt_email_with_flag:
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td>{_icon(r["email_restored"])}</td>'
              f'<td>{_icon(r["exact"])}</td></tr>')
        w("</tbody></table>")

    # ── Section 3c: Encrypt Detailed Inspection ─────────────────────────
    all_encrypt_detail = runner.encrypt_dl_detail + runner.encrypt_email_detail
    if all_encrypt_detail:
        w('<h3 id="encrypt-detail">3c. Detailed Inspection</h3>')
        for d in all_encrypt_detail:
            cls = "pass" if d["exact"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(d["scenario"])} &nbsp; {_icon(d["exact"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(d["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(d["anonymized_text"])}</div>')
            w(f'<div class="text-block"><strong>Restored (include_encrypted=True):</strong><br>'
              f'{H(d["restored_text"])}</div>')
            if d["entity_mapping"]:
                w("<table><thead><tr><th>Placeholder</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for ph, orig in d["entity_mapping"].items():
                    w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            if d["encrypt_mapping"]:
                w("<table><thead><tr><th>Cipher Token</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for tok, orig in d["encrypt_mapping"].items():
                    w(f"<tr><td><code>{H(tok[:40])}…</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            if d["exact"]:
                w("<p>✅ <strong>Exact round-trip match</strong></p>")
            else:
                w("<p>❌ <strong>Round-trip mismatch</strong></p>")
            w("</details>")

    # ── 4. PQC Encryption ───────────────────────────────────────────────
    w('<h2 id="pqc-encrypt">4. PQC Encryption (ML-KEM-768 + AES-256-GCM)</h2>')
    w("<p>Tests the post-quantum encryption backend across single-entity and "
      "multi-entity scenarios. Covers anonymization, deanonymize without "
      "<code>include_encrypted</code> (encrypted tokens remain opaque), "
      "deanonymize with <code>include_encrypted=true</code> (PII restored), "
      "and non-determinism verification.</p>")
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td rowspan="3">PQCTestApp</td>'
      '<td><code>IN_DRIVING_LICENSE</code></td><td><code>encrypt</code></td></tr>'
      '<tr><td><code>EMAIL_ADDRESS</code></td><td><code>encrypt</code></td></tr>'
      '<tr><td><code>PHONE_NUMBER</code></td><td><code>encrypt</code></td></tr>'
      '</tbody></table>'
      '<p><small>Uses PQC backend (<code>ENCRYPTION_BACKEND=pqc</code>) with '
      'ML-KEM-768 key encapsulation + AES-256-GCM symmetric encryption.</small></p>'
      '</div>')

    if runner.pqc_encrypt_results:
        w("<h3>4a. Anonymization with PQC Encrypt</h3>")
        w("<table><thead><tr><th>Scenario</th><th>Encrypted Count</th>"
          "<th>Expected</th><th>All Hidden?</th><th>All Mapped?</th>"
          "<th>Result</th></tr></thead><tbody>")
        for r in runner.pqc_encrypt_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{r["encrypt_count"]}</td>'
              f'<td>{r["expected_count"]}</td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["all_mapped"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        pqc_anon_passed = sum(1 for r in runner.pqc_encrypt_results if r["passed"])
        w(f"<p><strong>{pqc_anon_passed}/{len(runner.pqc_encrypt_results)}</strong> "
          f"anonymization tests passed.</p>")

    if runner.pqc_encrypt_no_flag:
        w("<h3>4b. De-anonymize (without include_encrypted flag)</h3>")
        w("<p>Encrypted tokens should remain opaque — PII must <strong>not</strong> be restored.</p>")
        w("<table><thead><tr><th>Scenario</th><th>PII Not Restored?</th>"
          "<th>Result</th></tr></thead><tbody>")
        for r in runner.pqc_encrypt_no_flag:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["pii_not_restored"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        noflag_passed = sum(1 for r in runner.pqc_encrypt_no_flag if r["passed"])
        w(f"<p><strong>{noflag_passed}/{len(runner.pqc_encrypt_no_flag)}</strong> "
          f"correctly withheld encrypted PII.</p>")

    if runner.pqc_encrypt_with_flag:
        w("<h3>4c. De-anonymize (include_encrypted=True)</h3>")
        w("<p>All encrypted PII values should be restored to their original form.</p>")
        w("<table><thead><tr><th>Scenario</th><th>PII Restored?</th>"
          "<th>Exact Round-Trip?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.pqc_encrypt_with_flag:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["pii_restored"])}</td>'
              f'<td>{_icon(r["exact"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        flag_passed = sum(1 for r in runner.pqc_encrypt_with_flag if r["passed"])
        w(f"<p><strong>{flag_passed}/{len(runner.pqc_encrypt_with_flag)}</strong> "
          f"encrypted round-trips restored.</p>")

    if runner.pqc_encrypt_nondeterministic:
        nd = runner.pqc_encrypt_nondeterministic
        w("<h3>4d. Non-determinism Check</h3>")
        w("<p>Encrypting the same text twice must produce different tokens "
          "(each KEM encapsulation uses fresh randomness).</p>")
        w(f"<p>Tokens generated: {_icon(nd.get('tokens_found', False))} &nbsp; "
          f"Tokens different: {_icon(nd.get('different', False))}</p>")

    # ── Section 4e: PQC Detailed Inspection ─────────────────────────────
    if runner.pqc_encrypt_detail:
        w('<h3 id="pqc-detail">4e. Detailed Inspection</h3>')
        for d in runner.pqc_encrypt_detail:
            cls = "pass" if d["exact"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(d["scenario"])} &nbsp; {_icon(d["exact"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(d["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(d["anonymized_text"])}</div>')
            w(f'<div class="text-block"><strong>Restored (include_encrypted=True):</strong><br>'
              f'{H(d["restored_text"])}</div>')
            if d["entity_mapping"]:
                w("<table><thead><tr><th>Placeholder</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for ph, orig in d["entity_mapping"].items():
                    w(f"<tr><td><code>{H(ph)}</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            if d["encrypt_mapping"]:
                w("<table><thead><tr><th>PQC Cipher Token</th><th>Original Value</th>"
                  "</tr></thead><tbody>")
                for tok, orig in d["encrypt_mapping"].items():
                    w(f"<tr><td><code>{H(tok[:40])}…</code></td><td>{H(orig)}</td></tr>")
                w("</tbody></table>")
            if d["exact"]:
                w("<p>✅ <strong>Exact round-trip match</strong></p>")
            else:
                w("<p>❌ <strong>Round-trip mismatch</strong></p>")
            w("</details>")

    # ── 5. Fake Strategy ────────────────────────────────────────────────
    w('<h2 id="fake-strategy">5. Fake Strategy (Format-Preserving Replacement)</h2>')
    w("<p>Tests the <code>fake</code> anonymization strategy, which replaces PII "
      "with structurally valid but fictitious values. Covers basic anonymization, "
      "format validation, within-request consistency, round-trip deanonymization, "
      "and mixed strategies.</p>")
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td rowspan="3">FakeStrategyTestApp</td>'
      '<td><code>IN_DRIVING_LICENSE</code></td><td><code>fake</code></td></tr>'
      '<tr><td><code>EMAIL_ADDRESS</code></td><td><code>fake</code></td></tr>'
      '<tr><td><code>PHONE_NUMBER</code></td><td><code>fake</code></td></tr>'
      '</tbody></table></div>')

    # Helper: render an entity-mapping table inside a details card
    def _mapping_table(mapping: dict, label: str = "Fake Value → Original") -> None:
        if not mapping:
            return
        w(f"<table><thead><tr><th colspan='2'>{label}</th></tr>"
          "<tr><th>Replacement</th><th>Original</th></tr></thead><tbody>")
        for k, v in mapping.items():
            w(f"<tr><td><code>{H(k)}</code></td><td><code>{H(v)}</code></td></tr>")
        w("</tbody></table>")

    if runner.fake_strategy_results:
        w("<h3>5a. Anonymization with Fake Strategy</h3>")
        w("<table><thead><tr><th>Scenario</th><th>Fake Count</th>"
          "<th>Expected</th><th>All Hidden?</th><th>All Mapped?</th>"
          "<th>Result</th></tr></thead><tbody>")
        for r in runner.fake_strategy_results:
            w(f'<tr class="{"pass" if r["passed"] else "fail"}">'
              f'<td>{H(r["label"])}</td>'
              f'<td>{r["fake_count"]}</td>'
              f'<td>{r["expected_count"]}</td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["all_mapped"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        fake_anon_passed = sum(1 for r in runner.fake_strategy_results if r["passed"])
        w(f"<p><strong>{fake_anon_passed}/{len(runner.fake_strategy_results)}</strong> "
          f"anonymization tests passed.</p>")

        # Collapsible details per scenario
        for r in runner.fake_strategy_results:
            cls = "pass" if r["passed"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(r["label"])} &nbsp; {_icon(r["passed"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(r["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(r["anonymized_text"])}</div>')
            _mapping_table(r["entity_mapping"])
            w("</details>")

    if runner.fake_format_results:
        w("<h3>5b. Format Validation</h3>")
        w("<p>Verifies that fake values match the expected format patterns for each entity type.</p>")
        w("<table><thead><tr><th>Check</th><th>Original</th><th>Fake Value</th>"
          "<th>Pattern</th><th>Format Valid?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.fake_format_results:
            w(f'<tr class="{"pass" if r["passed"] else "fail"}">'
              f'<td>{H(r["label"])}</td>'
              f'<td><code>{H(r["original"])}</code></td>'
              f'<td><code>{H(r["fake_value"])}</code></td>'
              f'<td><code>{H(r["pattern"])}</code></td>'
              f'<td>{_icon(r["format_valid"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

        for r in runner.fake_format_results:
            cls = "pass" if r["passed"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(r["label"])} &nbsp; {_icon(r["passed"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(r["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(r["anonymized_text"])}</div>')
            w(f'<p><strong>Original PII:</strong> <code>{H(r["original"])}</code> '
              f'→ <strong>Fake:</strong> <code>{H(r["fake_value"])}</code></p>')
            w(f'<p><strong>Regex pattern:</strong> <code>{H(r["pattern"])}</code> '
              f'— Match: {_icon(r["format_valid"])}</p>')
            _mapping_table(r["entity_mapping"])
            w("</details>")

    if runner.fake_consistency_results:
        w("<h3>5c. Within-Request Consistency</h3>")
        w("<p>Same PII value appearing multiple times should produce the same fake value.</p>")
        w("<table><thead><tr><th>Case</th><th>Repeated Value</th>"
          "<th>Unique Fakes</th><th>Occurrences in Text</th>"
          "<th>Consistent?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.fake_consistency_results:
            w(f'<tr class="{"pass" if r["passed"] else "fail"}">'
              f'<td>{H(r["label"])}</td>'
              f'<td><code>{H(r["repeated_value"])}</code></td>'
              f'<td>{r["unique_fakes"]}</td>'
              f'<td>{r["occurrences_in_text"]}</td>'
              f'<td>{_icon(r["consistent"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

        for r in runner.fake_consistency_results:
            cls = "pass" if r["passed"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(r["label"])} &nbsp; {_icon(r["passed"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(r["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(r["anonymized_text"])}</div>')
            w(f'<p><strong>Repeated value:</strong> <code>{H(r["repeated_value"])}</code> '
              f'→ <strong>Fake:</strong> <code>{H(str(r["fake_value"]))}</code></p>')
            w(f'<p>Unique fake values for this PII: <strong>{r["unique_fakes"]}</strong> '
              f'(expected 1) &nbsp; | &nbsp; '
              f'Occurrences in anonymized text: <strong>{r["occurrences_in_text"]}</strong> '
              f'(expected 2)</p>')
            _mapping_table(r["entity_mapping"])
            w("</details>")

    if runner.fake_round_trip_results:
        w("<h3>5d. Round-Trip Deanonymization</h3>")
        w("<p>Fake values should deanonymize back to original PII.</p>")
        w("<table><thead><tr><th>Scenario</th><th>PII Restored?</th>"
          "<th>Exact Match?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.fake_round_trip_results:
            w(f'<tr class="{"pass" if r["passed"] else "fail"}">'
              f'<td>{H(r["label"])}</td>'
              f'<td>{_icon(r["pii_restored"])}</td>'
              f'<td>{_icon(r["exact"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        fake_rt_passed = sum(1 for r in runner.fake_round_trip_results if r["passed"])
        w(f"<p><strong>{fake_rt_passed}/{len(runner.fake_round_trip_results)}</strong> "
          f"round-trip tests passed.</p>")

        for r in runner.fake_round_trip_results:
            cls = "pass" if r["passed"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(r["label"])} &nbsp; {_icon(r["passed"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(r["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized (with fakes):</strong><br>'
              f'{H(r["anonymized_text"])}</div>')
            w(f'<div class="text-block"><strong>Restored:</strong><br>'
              f'{H(r["restored_text"])}</div>')
            _mapping_table(r["entity_mapping"])
            if r["exact"]:
                w("<p>✅ <strong>Exact match with original</strong></p>")
            elif r["pii_restored"]:
                w("<p>⚠️ <strong>All PII values restored but text not byte-identical</strong></p>")
            else:
                w("<p>❌ <strong>Some PII values were NOT restored</strong></p>")
            w("</details>")

    if runner.fake_mixed_results:
        w("<h3>5e. Mixed Strategies (Fake + Hash + Encrypt)</h3>")
        w("<p>Verifies that fake works alongside hash and encrypt in the same request. "
          "Config: <code>DL=fake</code>, <code>Email=hash</code>, <code>Phone=encrypt</code>.</p>")
        w("<table><thead><tr><th>Scenario</th><th>DL in Fake?</th>"
          "<th>Email in Hash?</th><th>Phone in Encrypt?</th>"
          "<th>All Hidden?</th><th>Result</th></tr></thead><tbody>")
        for r in runner.fake_mixed_results:
            w(f'<tr class="{"pass" if r["passed"] else "fail"}">'
              f'<td>{H(r["label"])}</td>'
              f'<td>{_icon(r["dl_in_fake"])}</td>'
              f'<td>{_icon(r["email_in_hash"])}</td>'
              f'<td>{_icon(r["phone_in_encrypt"])}</td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")

        for r in runner.fake_mixed_results:
            cls = "pass" if r["passed"] else "fail"
            w(f'<details class="scenario-card {cls}">')
            w(f'<summary>{H(r["label"])} &nbsp; {_icon(r["passed"])}</summary>')
            w(f'<div class="text-block"><strong>Original:</strong><br>'
              f'{H(r["original_text"])}</div>')
            w(f'<div class="text-block"><strong>Anonymized:</strong><br>'
              f'{H(r["anonymized_text"])}</div>')
            if r["entity_mapping"]:
                _mapping_table(r["entity_mapping"], "Fake Mapping (entity_mapping)")
            if r["hash_mapping"]:
                _mapping_table(r["hash_mapping"], "Hash Mapping (hash_mapping)")
            if r["encrypt_mapping"]:
                _mapping_table(r["encrypt_mapping"], "Encrypt Mapping (encrypt_mapping)")
            w("</details>")

    # ── Section 6: Multi-Tenant ──────────────────────────────────────────
    w('<h2 id="multi-tenant">6. Multi-Tenant App Round-Trip</h2>')
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td>RetailBankingApp</td><td><em>All entity types</em></td>'
      '<td><code>replace</code> (default)</td></tr>'
      '<tr><td>InternalAuditApp</td><td><code>IN_DRIVING_LICENSE</code></td>'
      '<td><code>hash</code></td></tr>'
      '</tbody></table></div>')
    if runner.app_round_trip:
        w("<table><thead><tr><th>Scenario</th><th>Has DL?</th>"
          "<th>RetailBankingApp</th><th>InternalAuditApp</th>"
          "</tr></thead><tbody>")
        for r in runner.app_round_trip:
            dl_flag = "🔑 Yes" if r.has_dl else "No"
            w(f"<tr><td>{H(r.scenario_name)}</td><td>{dl_flag}</td>"
              f"<td>{_icon(r.retail_exact)}</td>"
              f"<td>{_icon(r.audit_exact)}</td></tr>")
        w("</tbody></table>")
        w(f"<p><strong>RetailBankingApp:</strong> {retail_exact}/{total_scenarios} exact &nbsp;|&nbsp; "
          f"<strong>InternalAuditApp:</strong> {audit_exact}/{total_scenarios} exact</p>")

    # ── 7. Mixed-Strategy Multi-Tenant Apps ─────────────────────────────
    w('<h2 id="mixed-strategy">7. Mixed-Strategy Multi-Tenant Apps</h2>')
    w('<div class="config-summary"><strong>🔧 App Configuration</strong>'
      '<table><thead><tr><th>App Name</th><th>Entity Type</th><th>Strategy</th>'
      '</tr></thead><tbody>'
      '<tr><td rowspan="3">ComplianceApp</td>'
      '<td><code>EMAIL_ADDRESS</code></td><td><code>encrypt</code></td></tr>'
      '<tr><td><code>PHONE_NUMBER</code></td><td><code>hash</code></td></tr>'
      '<tr><td><code>IN_DRIVING_LICENSE</code></td><td><code>hash</code></td></tr>'
      '<tr><td>CustomerSupportApp</td><td><em>All entity types</em></td>'
      '<td><code>replace</code> (default)</td></tr>'
      '</tbody></table></div>')

    if runner.mixed_strategy_results:
        w("<h3>7a. Default Round-Trip (no include flags)</h3>")
        w("<table><thead><tr><th>Scenario</th><th>Has Email?</th>"
          "<th>Has Phone?</th><th>Has DL?</th>"
          "<th>ComplianceApp</th><th>CustomerSupportApp</th>"
          "</tr></thead><tbody>")
        for r in runner.mixed_strategy_results:
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td>{"📧" if r["has_email"] else "—"}</td>'
              f'<td>{"📱" if r["has_phone"] else "—"}</td>'
              f'<td>{"🔑" if r["has_dl"] else "—"}</td>'
              f'<td>{_icon(r["compliance_default_exact"])}</td>'
              f'<td>{_icon(r["support_default_exact"])}</td></tr>')
        w("</tbody></table>")

        comp_default = sum(
            1 for r in runner.mixed_strategy_results
            if r["compliance_default_exact"]
        )
        supp_default = sum(
            1 for r in runner.mixed_strategy_results
            if r["support_default_exact"]
        )
        w(f"<p><strong>ComplianceApp:</strong> {comp_default}/{total_scenarios} exact &nbsp;|&nbsp; "
          f"<strong>CustomerSupportApp:</strong> {supp_default}/{total_scenarios} exact</p>")

        w("<h3>7b. Full Round-Trip (include_hashed + include_encrypted)</h3>")
        w("<table><thead><tr><th>Scenario</th>"
          "<th>ComplianceApp</th><th>CustomerSupportApp</th>"
          "</tr></thead><tbody>")
        for r in runner.mixed_strategy_results:
            w(f'<tr><td>{H(r["scenario"])}</td>'
              f'<td>{_icon(r["compliance_full_exact"])}</td>'
              f'<td>{_icon(r["support_full_exact"])}</td></tr>')
        w("</tbody></table>")

        comp_full = sum(
            1 for r in runner.mixed_strategy_results
            if r["compliance_full_exact"]
        )
        supp_full = sum(
            1 for r in runner.mixed_strategy_results
            if r["support_full_exact"]
        )
        w(f"<p><strong>ComplianceApp (full):</strong> {comp_full}/{total_scenarios} exact &nbsp;|&nbsp; "
          f"<strong>CustomerSupportApp (full):</strong> {supp_full}/{total_scenarios} exact</p>")

    # ── 8. Edge Cases ───────────────────────────────────────────────────
    w('<h2 id="edge-cases">8. Edge Cases</h2>')
    if runner.edge_cases:
        w("<table><thead><tr><th>Test</th><th>Expected</th>"
          "<th>Actual</th><th>Result</th></tr></thead><tbody>")
        for e in runner.edge_cases:
            w(f'<tr class="{_status_class(e.passed)}">'
              f"<td>{H(e.name)}</td><td>HTTP {e.expected_status}</td>"
              f"<td>HTTP {e.actual_status}</td>"
              f"<td>{_icon(e.passed)}</td></tr>")
        w("</tbody></table>")

    # ── 9. LLM Sandwich Pattern ────────────────────────────────────────
    w('<h2 id="llm-sandwich">9. LLM Sandwich Pattern</h2>')
    llm = runner.llm_sandwich
    if llm:
        resolved = llm.get("placeholders_resolved", False)
        w(f'<p>{_icon(resolved)} <strong>'
          f'{"All placeholders resolved" if resolved else "Unresolved placeholders remain"}'
          f'</strong></p>')
        w(f'<details><summary>Show detailed output</summary>')
        w(f'<div class="scenario-card {"pass" if resolved else "fail"}">')
        w(f"<h3>{H(llm.get('scenario', ''))}</h3>")
        w(f'<div class="text-block"><strong>Anonymized input:</strong><br>'
          f"{H(llm.get('anonymized', ''))}</div>")
        w(f'<div class="text-block"><strong>Simulated LLM response:</strong><br>'
          f"{H(llm.get('llm_response', ''))}</div>")
        w(f'<div class="text-block"><strong>De-anonymized output:</strong><br>'
          f"{H(llm.get('restored', ''))}</div>")
        w("</div></details>")

    # ── 10. Structured Data (JSON) Tests ────────────────────────────────
    w('<h2 id="structured">10. Structured Data (JSON) Tests</h2>')

    # Detection results
    for sid, sr in runner.structured_results.items():
        matched = len(sr.matched_types)
        expected_count = len(sr.expected_types)
        status = "pass" if matched == expected_count else "warn" if matched > 0 else "fail"
        w(f'<div class="scenario-card {status}">')
        w(f"<h3>{H(sr.scenario_name)}</h3>")
        w(f'<div class="text-block"><strong>Original:</strong><pre>{H(sr.original_text)}</pre></div>')
        w(f'<div class="text-block"><strong>Anonymized:</strong><pre>{H(sr.anonymized_text)}</pre></div>')
        w(f"<p><strong>Coverage:</strong> {matched}/{expected_count} expected types</p>")
        if sr.missed_types:
            w(f'<p class="missed">⚠️ Missed: '
              f'{", ".join(f"<code>{H(m)}</code>" for m in sorted(sr.missed_types))}</p>')
        w("</div>")

    # Round-trip
    if runner.structured_rt:
        w("<h3>10b. Structured Data Round-Trip</h3>")
        w("<table><thead><tr><th>#</th><th>Scenario</th><th>Exact Match</th>"
          "<th>JSON Valid</th><th>Status</th></tr></thead><tbody>")
        for i, r in enumerate(runner.structured_rt, 1):
            if r.exact_match and r.structure_valid:
                status_txt = "✅ Perfect"
            elif r.structure_valid:
                status_txt = "⚠️ Structure OK, content changed"
            else:
                status_txt = "❌ Structure broken"
            w(f"<tr><td>{i}</td><td>{H(r.scenario_name)}</td>"
              f"<td>{_icon(r.exact_match)}</td>"
              f"<td>{_icon(r.structure_valid)}</td>"
              f"<td>{status_txt}</td></tr>")
        w("</tbody></table>")
        w(f"<p><strong>{struct_exact}/{len(runner.structured_rt)} exact round-trips, "
          f"{struct_valid}/{len(runner.structured_rt)} structurally valid</strong></p>")

    # ── 11. Address Indicator Recognition ───────────────────────────────
    w('<h2 id="address-indicator">11. Address Indicator Recognition</h2>')
    w("<p>Verifies that address indicator words (<code>Address:</code>, "
      "<code>Flat</code>, <code>residing at</code>, <code>Apartment</code>) "
      "cause apartment/building names to be recognized as ADDRESS (not PERSON).</p>")

    if runner.address_indicator_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Not PERSON?</th><th>Is ADDRESS?</th><th>Result</th>"
          "</tr></thead><tbody>")
        for r in runner.address_indicator_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td>{_icon(r["person_ok"])}</td>'
              f'<td>{_icon(r["address_ok"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        addr_passed = sum(1 for r in runner.address_indicator_results if r["passed"])
        w(f"<p><strong>{addr_passed}/{len(runner.address_indicator_results)}</strong> "
          f"address indicator tests passed.</p>")

    # ── 11b. Case Robustness (ALL-CAPS / lowercase) ─────────────────────
    w('<h2 id="case-robustness">11b. Case Robustness</h2>')
    w("<p>Cased NER models rely on capitalisation, so ALL-CAPS names come back "
      "mislabelled <code>ORGANIZATION</code> or truncated, and all-lowercase "
      "names are missed outright. Each name is sent in Title, UPPER and lower "
      "casing and must be detected as <code>PERSON</code> every time, with the "
      "round-trip restoring the original casing. The final rows guard the "
      "precision risks: case-sensitive identifiers (PAN, IFSC) must survive, "
      "and ordinary prose must not gain spurious entities.</p>")

    if runner.case_robustness_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Anonymized</th><th>Detected?</th><th>Correct type?</th>"
          "<th>Round-trip</th><th>Result</th></tr></thead><tbody>")
        for r in runner.case_robustness_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><small>{H(r["anonymized"])}</small></td>'
              f'<td>{_icon(r["detected"])}</td>'
              f'<td>{_icon(r["not_mislabelled"])}</td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        case_passed = sum(1 for r in runner.case_robustness_results if r["passed"])
        w(f"<p><strong>{case_passed}/{len(runner.case_robustness_results)}</strong> "
          f"case robustness tests passed.</p>")

        w("<h3>11b-i. Case Robustness — Input / Output Detail</h3>")
        for r in runner.case_robustness_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["detected"])} detected &nbsp; '
                    f'{_icon(r["not_mislabelled"])} type &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
            )

    # ── 11c. Address Completeness ───────────────────────────────────────
    w('<h2 id="address-completeness">11c. Address Completeness</h2>')
    w("<p>A flat/unit number left outside the <code>ADDRESS</code> span "
      "(<code>F3003</code>, <code>A-101</code>, the <code>12</code> of "
      "<code>12 MG Road</code>) is an exposed identifier. These cases assert "
      "every address fragment lands inside the span, and that ordinary "
      "organisation mentions outside address context are not folded in.</p>")

    if runner.address_completeness_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Address captured</th><th>Missing</th><th>Complete?</th>"
          "<th>No over-reach</th><th>Round-trip</th><th>Result</th>"
          "</tr></thead><tbody>")
        for r in runner.address_completeness_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><small>{H(r["address"])}</small></td>'
              f'<td><small>{H(r["missing"])}</small></td>'
              f'<td>{_icon(r["complete"])}</td>'
              f'<td>{_icon(r["no_overreach"])}</td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        ac_passed = sum(1 for r in runner.address_completeness_results if r["passed"])
        w(f"<p><strong>{ac_passed}/{len(runner.address_completeness_results)}</strong> "
          f"address completeness tests passed.</p>")

        w("<h3>11c-i. Address Completeness — Input / Output Detail</h3>")
        for r in runner.address_completeness_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["complete"])} complete &nbsp; '
                    f'{_icon(r["no_overreach"])} no over-reach &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
                extra_rows=[("Missing fragments", r["missing"])],
            )

    # ── 11d. Account Number vs Phone Number ─────────────────────────────
    w('<h2 id="account-phone">11d. Account Number vs Phone Number</h2>')
    w("<p>A bare run of 9-18 digits is a valid Indian bank account number, "
      "and the phone recognizer claims the same digits whenever they also form "
      "a mobile (<code>9876543210</code>) or a landline without its leading 0 "
      "(<code>5498721032</code>). Scores cannot separate them — the phone "
      "pattern scores 0.60 against the account pattern's 0.10, and "
      "<code>number</code> sits in the phone recognizer's context list, so "
      "<em>bank account number</em> boosts the phone score to 1.00. The "
      "nearest surrounding cue decides, with transfer markers (IFSC, NEFT) "
      "also counting when they follow the digits.</p>")

    if runner.account_phone_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Value</th><th>Expected</th><th>Actual</th><th>Round-trip</th>"
          "<th>Result</th></tr></thead><tbody>")
        for r in runner.account_phone_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><code>{H(r["value"])}</code></td>'
              f'<td><code>{H(r["expected"])}</code></td>'
              f'<td><code>{H(r["actual"])}</code></td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        ap_passed = sum(1 for r in runner.account_phone_results if r["passed"])
        w(f"<p><strong>{ap_passed}/{len(runner.account_phone_results)}</strong> "
          f"account/phone disambiguation tests passed.</p>")

        w("<h3>11d-i. Account vs Phone — Input / Output Detail</h3>")
        for r in runner.account_phone_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["correct_type"])} type &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
                extra_rows=[
                    ("Value under test", r["value"]),
                    ("Expected type", r["expected"]),
                    ("Actual type", r["actual"]),
                ],
            )

    # ── 11e. Names With Dotted Initials ─────────────────────────────────
    w('<h2 id="person-initials">11e. Names With Dotted Initials</h2>')
    w("<p>Cased NER models end the entity at dotted initials: queried "
      "directly, <code>Mr. R.K. Sharma</code> returns only "
      "<code>PERSON 'R.K.'</code> and the surname is never detected, leaking "
      "it. The same name without periods returns one complete span, which "
      "isolates the periods as the trigger.</p>")

    if runner.person_initials_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Expected span</th><th>Detected</th><th>Full span?</th>"
          "<th>No leak</th><th>Round-trip</th><th>Result</th>"
          "</tr></thead><tbody>")
        for r in runner.person_initials_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><small>{H(r["expected"])}</small></td>'
              f'<td><small>{H(r["actual"])}</small></td>'
              f'<td>{_icon(r["full_span"])}</td>'
              f'<td>{_icon(r["no_leak"])}</td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        pi_passed = sum(1 for r in runner.person_initials_results if r["passed"])
        w(f"<p><strong>{pi_passed}/{len(runner.person_initials_results)}</strong> "
          f"initials tests passed.</p>")

        w("<h3>11e-i. Dotted Initials — Input / Output Detail</h3>")
        for r in runner.person_initials_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["full_span"])} full span &nbsp; '
                    f'{_icon(r["no_leak"])} no leak &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
                extra_rows=[("Expected span", r["expected"])],
            )

    # ── 11f. Honorific and Professional Titles ──────────────────────────
    w('<h2 id="person-titles">11f. Honorific and Professional Titles</h2>')
    w("<p>Only the Western titles were known to the pipeline. "
      "<code>CA Abhay Sharma</code> and <code>CS Priya</code> came back as "
      "<code>ORGANIZATION</code> — still redacted by default, but leaked "
      "outright for an app allow-listing ORGANIZATION — while "
      "<code>Er. Ram</code> tagged the abbreviation itself as PERSON and left "
      "a stray <code>.</code> entity behind. Titles stay outside the span, "
      "matching the existing behaviour for <code>Mr. Rajesh Sharma</code>.</p>")

    if runner.person_titles_results:
        w("<table><thead><tr><th>Title</th><th>Input (truncated)</th>"
          "<th>Expected name</th><th>Entities</th><th>Is PERSON?</th>"
          "<th>No junk span</th><th>Round-trip</th><th>Result</th>"
          "</tr></thead><tbody>")
        for r in runner.person_titles_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><small>{H(r["expected"])}</small></td>'
              f'<td><small>{H(r["actual"])}</small></td>'
              f'<td>{_icon(r["as_person"])}</td>'
              f'<td>{_icon(r["no_junk"])}</td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        pt_passed = sum(1 for r in runner.person_titles_results if r["passed"])
        w(f"<p><strong>{pt_passed}/{len(runner.person_titles_results)}</strong> "
          f"title tests passed.</p>")

        w("<h3>11f-i. Titles — Input / Output Detail</h3>")
        for r in runner.person_titles_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["as_person"])} PERSON &nbsp; '
                    f'{_icon(r["no_junk"])} no junk &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
                extra_rows=[("Expected name", r["expected"])],
            )

    # ── 11g. NRP Alignment ──────────────────────────────────────────────
    w('<h2 id="nrp-alignment">11g. NRP Alignment</h2>')
    w("<p>NRP (nationality / religious / political group) is personal data only "
      "when it describes a <em>person</em>. The NER model emits it for any "
      "demonym, so aggregate business language — <code>South Indian "
      "branches</code>, <code>Indian banking sector</code> — was redacted even "
      "though it identifies nobody. Used predicatively (<code>the customer is "
      "Indian</code>) or before a singular person noun (<code>a Muslim "
      "woman</code>) it describes an individual and must stay.</p>")

    if runner.nrp_alignment_results:
        w("<table><thead><tr><th>Test Case</th><th>Input (truncated)</th>"
          "<th>Expectation</th><th>NRP detected</th><th>As expected?</th>"
          "<th>Round-trip</th><th>Result</th></tr></thead><tbody>")
        for r in runner.nrp_alignment_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><small>{H(r["text"])}</small></td>'
              f'<td><small>{H(r["expectation"])}</small></td>'
              f'<td><small>{H(r["nrp"])}</small></td>'
              f'<td>{_icon(r["as_expected"])}</td>'
              f'<td>{_icon(r["round_trip"])}</td>'
              f'<td>{_icon(r["passed"])}</td></tr>')
        w("</tbody></table>")
        na_passed = sum(1 for r in runner.nrp_alignment_results if r["passed"])
        w(f"<p><strong>{na_passed}/{len(runner.nrp_alignment_results)}</strong> "
          f"NRP alignment tests passed.</p>")

        w("<h3>11g-i. NRP Alignment — Input / Output Detail</h3>")
        for r in runner.nrp_alignment_results:
            _render_io_detail(
                w,
                label=r["label"],
                passed=r["passed"],
                badge=(
                    f'{_icon(r["as_expected"])} as expected &nbsp; '
                    f'{_icon(r["round_trip"])} round-trip'
                ),
                record=r,
                extra_rows=[
                    ("Expectation", r["expectation"]),
                    ("NRP detected", r["nrp"]),
                ],
            )

    # ── 11h. Multi-line Input ───────────────────────────────────────────
    w('<h2 id="multiline">11h. Multi-line Input</h2>')
    w("<p>In forms, lists and chat messages every line is its own statement. "
      "A keyword on one line used to relabel an entity on the next: in "
      "<code>residing at Mumbai. ⏎ My name is Mr. R.K. Sharma.</code> the word "
      "<code>residing</code> turned <code>R.K.</code> into a LOCATION, so the "
      "surname was never joined to it and leaked. Keywords now count only on "
      "the entity's own line and sentence, plus a line that introduces it "
      "(<code>Aadhaar number:</code>, <code>Correspondence Address</code>). An "
      "address wrapped over two lines stays one address, while a list of "
      "cities stays separate. ⏎ marks a line break.</p>")
    _render_expectation_results(
        w, runner.multiline_results,
        kept_label="Lines kept", noun="multi-line",
        detail_heading="11h-i. Multi-line Input — Input / Output Detail",
    )

    # ── 11i. Mixed-case Names ───────────────────────────────────────────
    w('<h2 id="mixed-case">11i. Mixed-case Names</h2>')
    w("<p>Names are often typed with only the first word capitalised. A cased "
      "NER model stops at the first uncased word, so in <code>My name is "
      "Venkata narasimha raju.</code> only <code>Venkata</code> was masked and "
      "the middle and last names leaked. The case-recovery pass, which re-runs "
      "NER over a re-cased copy, now keeps such names as PERSON, cut at any "
      "lowercase word that is a verb, preposition, article, common "
      "vocabulary or a relation word, so ordinary words typed after a name "
      "(<code>Ramesh paid electricity bill</code>, <code>Kavitha mother is the "
      "joint holder</code>) are not masked.</p>")
    _render_expectation_results(
        w, runner.mixed_case_results,
        kept_label="Context kept", noun="mixed-case name",
        detail_heading="11i-i. Mixed-case Names — Input / Output Detail",
    )

    # ── 11j. Address Units ──────────────────────────────────────────────
    w('<h2 id="address-units">11j. Address Units</h2>')
    w("<p>Flat numbers, block letters and their labels (<code>Flat no. 302, "
      "C 23</code>, <code>H.No. 12-3-456</code>, <code>2nd Floor, B Wing</code>) "
      "have no recognizer. Only the single number directly before an address "
      "was absorbed, and the <code>.</code> of <code>no.</code> was read as the "
      "end of a sentence, so <code>my address is Flat no. 302, C 23, Prestige "
      "Towers, Bangalore.</code> left <code>no. 302, C</code> exposed. The whole "
      "unit designation is now absorbed into the ADDRESS, while <code>no</code> "
      "in an ordinary sentence (<code>Order no. 12345</code>), a date before an "
      "address and separate sentences stay as they were. ⏎ marks a line "
      "break.</p>")
    _render_expectation_results(
        w, runner.address_units_results,
        kept_label="Context kept", noun="address unit",
        detail_heading="11j-i. Address Units — Input / Output Detail",
    )

    # ── 11k. Key-value Context ──────────────────────────────────────────
    w('<h2 id="key-value">11k. Key-value Context</h2>')
    w("<p>Context words are read from spaCy's tokens, and spaCy keeps a URL, "
      "or a key=value pair written without spaces, as one token. In "
      "<code>https://api.com?aadhaar=987654321098&amp;pan=ABCPK1234L</code> the "
      "key <code>aadhaar</code> was never seen as context, so the Aadhaar "
      "number, which scores below the threshold without it, leaked while the "
      "PAN next to it was masked. The key directly before a value now counts "
      "as a context word (<code>aadhaar_no=</code>, <code>aadhaarNumber=</code>, "
      "<code>Aadhaar:</code>), while keys that are not context words "
      "(<code>txn_id=</code>) change nothing.</p>")
    _render_expectation_results(
        w, runner.key_value_results,
        kept_label="Context kept", noun="key-value context",
        detail_heading="11k-i. Key-value Context — Input / Output Detail",
    )

    # ── 12. Geo-Coordinate Detection ────────────────────────────────────
    w('<h2 id="geo-coordinates">12. Geo-Coordinate Detection</h2>')
    w("<p>Verifies that geographic coordinates (latitude/longitude) in "
      "decimal degree, labeled, cardinal, and DMS formats are detected "
      "and anonymized as <code>GEO_COORDINATE</code>.</p>")

    if runner.geo_coordinate_results:
        geo_passed = sum(1 for r in runner.geo_coordinate_results if r["passed"])
        w(f"<p><strong>{geo_passed}/{len(runner.geo_coordinate_results)}</strong> "
          f"geo-coordinate tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Detected?</th>"
          "<th>Result</th><th>Details</th>"
          "</tr></thead><tbody>")
        for i, r in enumerate(runner.geo_coordinate_results):
            detail_id = f"geo-detail-{i}"
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td>{_icon(r["detected"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<div style="margin-top:6px;">'
              f'<strong>Original:</strong>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r.get("original_full", r["text"]))}</pre>'
              f'<strong>Anonymized:</strong>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r.get("anonymized_full", r["anonymized"]))}</pre>'
              f'</div></details></td></tr>')
        w("</tbody></table>")

    # ── 13. NRP Detection ────────────────────────────────────────────────
    w('<h2 id="nrp-detection">13. NRP Detection (Nationality / Religion / Political)</h2>')
    w("<p>Verifies detection of NRP entities — nationalities, religious groups, "
      "and political affiliations.</p>")
    if runner.nrp_results:
        nrp_passed = sum(1 for r in runner.nrp_results if r["passed"])
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

    # ── 14. US Entity Detection ──────────────────────────────────────────
    w('<h2 id="us-entities">14. US Entity Detection (SSN / ITIN / Passport / DL)</h2>')
    w("<p>Verifies detection of US entity types alongside Indian recognizers.</p>")
    if runner.us_entity_results:
        us_passed = sum(1 for r in runner.us_entity_results if r["passed"])
        w(f"<p><strong>{us_passed}/{len(runner.us_entity_results)}</strong> "
          f"US entity tests passed.</p>")
        w("<table><thead><tr><th>Test Case</th><th>Expected Type</th>"
          "<th>All PII Hidden?</th><th>Result</th><th>Details</th></tr></thead><tbody>")
        for r in runner.us_entity_results:
            w(f'<tr><td>{H(r["label"])}</td>'
              f'<td><code>{H(r["expected_type"])}</code></td>'
              f'<td>{_icon(r["all_hidden"])}</td>'
              f'<td>{_icon(r["passed"])}</td>'
              f'<td><details><summary>Show</summary>'
              f'<pre style="background:#f8f9fa;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["text"])}</pre>'
              f'<pre style="background:#fff3cd;padding:8px;border-radius:4px;'
              f'white-space:pre-wrap;margin:4px 0;">{H(r["anonymized"])}</pre>'
              f'</details></td></tr>')
        w("</tbody></table>")

    # ── 15. CKYC/PRAN/APAAR Detection ────────────────────────────────────
    w('<h2 id="ckyc-pran-apaar">15. CKYC / PRAN / APAAR Detection</h2>')
    w("<p>Verifies detection of Indian CKYC, PRAN (NPS), and APAAR (student ID) entities, "
      "including disambiguation from Aadhaar.</p>")
    if runner.ckyc_pran_apaar_results:
        cpa_passed = sum(1 for r in runner.ckyc_pran_apaar_results if r["passed"])
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

    # ── 16. Customer ID Detection ─────────────────────────────────────────
    w('<h2 id="customer-id">16. Customer ID Detection</h2>')
    w("<p>Verifies detection of banking Customer ID (9-digit) entities with "
      "context-based scoring.</p>")
    if runner.customer_id_results:
        cid_passed = sum(1 for r in runner.customer_id_results if r["passed"])
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

    # ── 17. Allow-List Tests ─────────────────────────────────────────────
    al = runner.allow_list_results
    al_passed_count = 0
    al_total_count = 0
    if al and al.get("tests"):
        w('<h2 id="allow-lists">15. Allow-List Tests</h2>')

        al_tests = al["tests"]
        al_passed_count = sum(1 for _, ok in al_tests if ok)
        al_total_count = len(al_tests)
        w(f'<p><strong>{al_passed_count}/{al_total_count}</strong> allow-list checks passed.</p>')
        w('<table><thead><tr><th>Test</th><th>Result</th></tr></thead><tbody>')
        for label, ok in al_tests:
            icon = "✅" if ok else "❌"
            cls = "" if ok else ' class="fail"'
            w(f'<tr{cls}><td>{html_escape(label)}</td><td>{icon}</td></tr>')
        w("</tbody></table>")

        original = al.get("original_text", "")
        w(f'<h3>Test Input</h3>')
        w(f'<pre style="background:#f8f9fa;padding:12px;border-radius:6px;white-space:pre-wrap;">{html_escape(original)}</pre>')

        for i, scenario in enumerate(al.get("scenarios", []), 1):
            name = scenario["name"]
            config = scenario["config"]
            anon = scenario.get("anonymized_text", "")
            mapping = scenario.get("entity_mapping", {})
            deanon = scenario.get("deanonymized_text", "")

            et_cfg = config.get("entity_type_allow_list", [])
            ekw_cfg = config.get("entity_keyword_allow_list", {})
            config_parts = []
            if et_cfg:
                config_parts.append(f"<strong>Entity-Type Allow-List:</strong> {html_escape(', '.join(et_cfg))}")
            if ekw_cfg:
                ekw_str = "; ".join(f"{k}: {', '.join(v)}" for k, v in ekw_cfg.items())
                config_parts.append(f"<strong>Entity-Keyword Allow-List:</strong> {html_escape(ekw_str)}")
            config_html = "<br>".join(config_parts) if config_parts else "<em>(no allow-lists)</em>"

            w(f'<details style="margin:12px 0;border:1px solid #ddd;border-radius:6px;padding:8px;">')
            w(f'<summary style="cursor:pointer;font-weight:bold;">Scenario {i}: {html_escape(name)}</summary>')
            w(f'<div style="margin-top:8px;">')
            w(f'<p style="background:#e8f4fd;padding:8px;border-radius:4px;">{config_html}</p>')

            w(f'<h4>Anonymized Text</h4>')
            w(f'<pre style="background:#fff3cd;padding:10px;border-radius:4px;white-space:pre-wrap;">{html_escape(anon)}</pre>')

            if mapping:
                w('<h4>Entity Mapping</h4>')
                w('<table><thead><tr><th>Placeholder</th><th>Original</th></tr></thead><tbody>')
                for placeholder, orig in mapping.items():
                    w(f'<tr><td><code>{html_escape(placeholder)}</code></td><td>{html_escape(orig)}</td></tr>')
                w('</tbody></table>')

            if deanon:
                w(f'<h4>De-anonymized Text (Round-Trip)</h4>')
                match = deanon == original
                color = "#d4edda" if match else "#f8d7da"
                w(f'<pre style="background:{color};padding:10px;border-radius:4px;white-space:pre-wrap;">{html_escape(deanon)}</pre>')
                w(f'<p>{"✅ Exact match" if match else "❌ Mismatch"}</p>')

            w('</div></details>')

    # ── Final Summary ────────────────────────────────────────────────────
    w('<h2 id="final-summary">📋 Final Summary</h2>')
    w("<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>")
    addr_passed = sum(1 for r in runner.address_indicator_results if r["passed"])
    summary_rows = [
        ("Test scenarios", str(total_scenarios)),
        ("Total PII entities detected", str(total_entities)),
        ("Unique entity types", str(len(unique_types))),
        ("Entity types found", ", ".join(f"<code>{H(t)}</code>" for t in unique_types)),
        ("Detection coverage", f"{overall_cov.get('hits', 0)}/{overall_cov.get('checks', 0)} ({overall_cov.get('rate', 0):.0f}%)"),
        ("Exact round-trips (default config)", f"{exact_rt}/{total_scenarios}"),
        ("Inexact due to hashing (expected)", str(hashed_inexact)),
        ("Unexpected failures", str(failures)),
        ("RetailBankingApp round-trips", f"{retail_exact}/{total_scenarios}"),
        ("InternalAuditApp round-trips", f"{audit_exact}/{total_scenarios}"),
        ("Edge cases passed", f"{edge_passed}/{len(runner.edge_cases)}"),
        ("Structured data exact RT", f"{struct_exact}/{len(runner.structured_rt)}"),
        ("Structured data JSON valid", f"{struct_valid}/{len(runner.structured_rt)}"),
        ("Encrypt EMAIL RT (with flag)", f"{encrypt_email_restored}/{len(runner.encrypt_email_with_flag)}"),
        ("Hash PHONE RT (with flag)", f"{hash_phone_restored}/{len(runner.hash_phone_with_flag)}"),
        ("ComplianceApp RT (default)", f"{comp_default_exact}/{total_scenarios}"),
        ("ComplianceApp RT (full)", f"{comp_full_exact}/{total_scenarios}"),
        ("CustomerSupportApp RT", f"{supp_default_exact}/{total_scenarios}"),
        ("Address indicator tests", f"{addr_passed}/{len(runner.address_indicator_results)}"),
        ("Case robustness tests", f"{sum(1 for r in runner.case_robustness_results if r['passed'])}/{len(runner.case_robustness_results)}"),
        ("Address completeness tests", f"{sum(1 for r in runner.address_completeness_results if r['passed'])}/{len(runner.address_completeness_results)}"),
        ("Account vs phone tests", f"{sum(1 for r in runner.account_phone_results if r['passed'])}/{len(runner.account_phone_results)}"),
        ("Dotted initials tests", f"{sum(1 for r in runner.person_initials_results if r['passed'])}/{len(runner.person_initials_results)}"),
        ("Honorific title tests", f"{sum(1 for r in runner.person_titles_results if r['passed'])}/{len(runner.person_titles_results)}"),
        ("NRP alignment tests", f"{sum(1 for r in runner.nrp_alignment_results if r['passed'])}/{len(runner.nrp_alignment_results)}"),
        ("Multi-line context tests", f"{sum(1 for r in runner.multiline_results if r['passed'])}/{len(runner.multiline_results)}"),
        ("Mixed-case name tests", f"{sum(1 for r in runner.mixed_case_results if r['passed'])}/{len(runner.mixed_case_results)}"),
        ("Address unit tests", f"{sum(1 for r in runner.address_units_results if r['passed'])}/{len(runner.address_units_results)}"),
        ("Key-value context tests", f"{sum(1 for r in runner.key_value_results if r['passed'])}/{len(runner.key_value_results)}"),
        ("Geo-coordinate tests", f"{sum(1 for r in runner.geo_coordinate_results if r['passed'])}/{len(runner.geo_coordinate_results)}"),
        ("NRP detection tests", f"{sum(1 for r in runner.nrp_results if r['passed'])}/{len(runner.nrp_results)}"),
        ("US entity tests", f"{sum(1 for r in runner.us_entity_results if r['passed'])}/{len(runner.us_entity_results)}"),
        ("CKYC/PRAN/APAAR tests", f"{sum(1 for r in runner.ckyc_pran_apaar_results if r['passed'])}/{len(runner.ckyc_pran_apaar_results)}"),
        ("Customer ID tests", f"{sum(1 for r in runner.customer_id_results if r['passed'])}/{len(runner.customer_id_results)}"),
        ("PQC encrypt anon", f"{sum(1 for r in runner.pqc_encrypt_results if r['passed'])}/{len(runner.pqc_encrypt_results)}"),
        ("PQC encrypt RT (with flag)", f"{sum(1 for r in runner.pqc_encrypt_with_flag if r['passed'])}/{len(runner.pqc_encrypt_with_flag)}"),
        ("Fake strategy anon", f"{sum(1 for r in runner.fake_strategy_results if r['passed'])}/{len(runner.fake_strategy_results)}"),
        ("Fake strategy format valid", f"{sum(1 for r in runner.fake_format_results if r['passed'])}/{len(runner.fake_format_results)}"),
        ("Fake strategy consistency", f"{sum(1 for r in runner.fake_consistency_results if r['passed'])}/{len(runner.fake_consistency_results)}"),
        ("Fake strategy RT", f"{sum(1 for r in runner.fake_round_trip_results if r['passed'])}/{len(runner.fake_round_trip_results)}"),
        ("Fake + hash + encrypt mixed", f"{sum(1 for r in runner.fake_mixed_results if r['passed'])}/{len(runner.fake_mixed_results)}"),
        ("Allow-list tests", f"{al_passed_count}/{al_total_count}"),
        ("Execution time", f"{runner.elapsed:.1f}s"),
    ]
    for label, value in summary_rows:
        w(f"<tr><td>{label}</td><td>{value}</td></tr>")
    w("</tbody></table>")

    w("</div>")  # container
    w("</body></html>")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text("\n".join(parts), encoding="utf-8")


# ── CSS ──────────────────────────────────────────────────────────────────────

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
h2 { margin-top: 2rem; margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 2px solid var(--accent); }
h3 { margin: 1rem 0 0.5rem; }
.timestamp { color: var(--muted); margin-bottom: 1.5rem; }

/* Dashboard cards */
.dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
             gap: 1rem; margin-bottom: 1.5rem; }
.card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
        padding: 1rem; text-align: center; }
.card-value { font-size: 1.5rem; font-weight: 700; color: var(--accent); }
.card-label { font-size: 0.85rem; color: var(--muted); margin-top: 0.25rem; }

/* Tables */
table { width: 100%; border-collapse: collapse; margin: 0.75rem 0; background: var(--card-bg); }
th, td { padding: 0.5rem 0.75rem; border: 1px solid var(--border); text-align: left; font-size: 0.9rem; }
th { background: #e9ecef; font-weight: 600; }
tr.pass { background: var(--pass); }
tr.warn { background: var(--warn); }
tr.fail { background: var(--fail); }

/* Scenario cards */
.config-summary { background: #eef6ff; border: 1px solid #b8d4f0; border-radius: 8px;
                  padding: 0.75rem 1rem; margin: 0.75rem 0; }
.config-summary table { margin-top: 0.5rem; }
.config-summary small { color: #555; }
.scenario-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
                 padding: 1rem; margin: 1rem 0; border-left: 4px solid var(--border); }
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

@media (max-width: 768px) {
    .dashboard { grid-template-columns: repeat(2, 1fr); }
    table { font-size: 0.8rem; }
    th, td { padding: 0.35rem 0.5rem; }
}
"""

# ── CLI Entry Point ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PII Shield — Indian Banking Test Suite (CLI + HTML Report)"
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"PII Shield API base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"HTML report output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    # Resolve test data path
    data_path = Path("indian_banking_test_data.json")
    if not data_path.exists():
        data_path = Path("examples/indian_banking_test_data.json")
    if not data_path.exists():
        data_path = Path(__file__).parent / "indian_banking_test_data.json"
    if not data_path.exists():
        print(f"❌ Cannot find indian_banking_test_data.json")
        sys.exit(1)

    with open(data_path) as f:
        scenarios = json.load(f)
    print(f"  Loaded {len(scenarios)} test scenarios from {data_path}")

    # Check API connectivity
    client = PIIShieldClient(args.api_url)
    try:
        client.list_apps()
    except Exception as exc:
        print(f"❌ Cannot reach API at {args.api_url}: {exc}")
        print("   Start the server: docker compose up -d")
        sys.exit(1)

    # Run tests
    runner = TestRunner(client, scenarios)
    runner.run()

    # Generate report
    generate_html_report(runner, args.output)
    abs_path = Path(args.output).resolve()
    print(f"  📄 HTML report written to: {abs_path}\n")


if __name__ == "__main__":
    main()
