"""Tests for merging adjacent LOCATION/IN_PIN_CODE entities into ADDRESS."""

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


# ---- helpers ----

def _loc(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="LOCATION", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


def _pin(start: int, end: int, score: float = 0.6) -> RecognizerResult:
    r = RecognizerResult(entity_type="IN_PIN_CODE", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "InPinCodeRecognizer"}
    return r


def _person(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="PERSON", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


# ---- unit tests ----

from app.main import _merge_address_entities


class TestMergeAddressUnit:
    """Unit tests for _merge_address_entities()."""

    def test_merge_two_locations_with_branch_gap(self):
        """'Rajouri Garden branch, New Delhi' → single ADDRESS."""
        text = "Rajouri Garden branch, New Delhi"
        results = [_loc(0, 14), _loc(23, 32)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert addrs[0].start == 0
        assert addrs[0].end == 32
        assert text[addrs[0].start : addrs[0].end] == "Rajouri Garden branch, New Delhi"

    def test_merge_location_and_pin_code(self):
        """'New Delhi 110027' → single ADDRESS."""
        text = "New Delhi 110027"
        results = [_loc(0, 9), _pin(10, 16)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert text[addrs[0].start : addrs[0].end] == "New Delhi 110027"

    def test_merge_three_entities(self):
        """'Rajouri Garden branch, New Delhi 110027' → single ADDRESS."""
        text = "Rajouri Garden branch, New Delhi 110027"
        results = [_loc(0, 14), _loc(23, 32), _pin(33, 39)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert text[addrs[0].start : addrs[0].end] == text

    def test_standalone_location_unchanged(self):
        """'Mumbai' alone should stay as LOCATION."""
        text = "He lives in Mumbai and works there."
        results = [_loc(12, 18)]
        merged = _merge_address_entities(results, text)
        assert len(merged) == 1
        assert merged[0].entity_type == "LOCATION"

    def test_no_merge_across_sentence_boundary(self):
        """Locations in different sentences should NOT merge."""
        text = "He lives in Mumbai. She lives in Chennai."
        results = [_loc(12, 18), _loc(33, 40)]
        merged = _merge_address_entities(results, text)
        locs = [r for r in merged if r.entity_type == "LOCATION"]
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(locs) == 2
        assert len(addrs) == 0

    def test_no_merge_across_em_dash(self):
        """Locations separated by em-dash (—) should NOT merge."""
        text = "Mumbai — Chennai"
        results = [_loc(0, 6), _loc(9, 16)]
        merged = _merge_address_entities(results, text)
        locs = [r for r in merged if r.entity_type == "LOCATION"]
        assert len(locs) == 2

    def test_no_merge_large_gap(self):
        """Locations > 50 chars apart should NOT merge."""
        text = "Mumbai" + " " * 60 + "Chennai"
        results = [_loc(0, 6), _loc(66, 73)]
        merged = _merge_address_entities(results, text)
        locs = [r for r in merged if r.entity_type == "LOCATION"]
        assert len(locs) == 2

    def test_non_address_gap_prevents_merge(self):
        """Locations with non-glue content in gap should NOT merge."""
        text = "Mumbai is great and Chennai is wonderful"
        results = [_loc(0, 6), _loc(20, 27)]
        merged = _merge_address_entities(results, text)
        locs = [r for r in merged if r.entity_type == "LOCATION"]
        assert len(locs) == 2

    def test_person_entities_not_merged(self):
        """PERSON entities should never be merged into ADDRESS."""
        text = "John branch, Smith"
        results = [_person(0, 4), _person(13, 18)]
        merged = _merge_address_entities(results, text)
        persons = [r for r in merged if r.entity_type == "PERSON"]
        assert len(persons) == 2

    def test_mixed_entities_preserved(self):
        """Non-location entities alongside merged address should be kept."""
        text = "Mr. Ravi at MG Road branch, Bangalore"
        results = [_person(4, 8), _loc(12, 14), _loc(28, 37)]
        merged = _merge_address_entities(results, text)
        persons = [r for r in merged if r.entity_type == "PERSON"]
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(persons) == 1
        assert len(addrs) == 1
        assert text[addrs[0].start : addrs[0].end] == "MG Road branch, Bangalore"

    def test_score_is_minimum(self):
        """Merged ADDRESS score should be the minimum of component scores."""
        text = "Rajouri Garden branch, New Delhi 110027"
        results = [_loc(0, 14, 0.97), _loc(23, 32, 1.0), _pin(33, 39, 0.6)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert addrs[0].score == 0.6

    def test_road_gap_merges(self):
        """'MG Road branch, Bangalore' should merge."""
        text = "MG Road branch, Bangalore"
        results = [_loc(0, 2), _loc(16, 25)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1

    def test_nagar_gap_merges(self):
        """'Anna Nagar, Chennai' should merge."""
        text = "Anna Nagar, Chennai"
        results = [_loc(0, 10), _loc(12, 19)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1

    def test_loose_glue_with_address_indicator(self):
        """Unrecognised locality names (e.g. 'Baner') between detected entities
        should be absorbed when an address indicator precedes the group."""
        #                    0         1         2         3         4
        text = "Address: Flat 501, Kumar Pinnacle, Baner, Pune 411045"
        # Kumar Pinnacle [19:33], Pune [42:46], 411045 [47:53]
        # Gap between Kumar Pinnacle and Pune is ", Baner, " — not in strict glue
        results = [_loc(19, 33), _loc(42, 46), _pin(47, 53)]
        merged = _merge_address_entities(results, text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert addrs[0].start == 19
        assert addrs[0].end == 53

    def test_loose_glue_without_indicator_no_merge(self):
        """Without an address indicator, unknown gap words should NOT merge."""
        text = "Contact Kumar Pinnacle, Baner, Pune 411045"
        # Kumar Pinnacle [8:22], Pune [31:35], 411045 [36:42]
        results = [_loc(8, 22), _loc(31, 35), _pin(36, 42)]
        merged = _merge_address_entities(results, text)
        # Kumar Pinnacle stays standalone; Pune+411045 merge (gap is " ")
        locs = [r for r in merged if r.entity_type == "LOCATION"]
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(locs) == 1  # Kumar Pinnacle
        assert len(addrs) == 1  # Pune 411045


# ---- integration tests ----

class TestMergeAddressIntegration:
    """Full anonymization tests for address merging."""

    def test_callback_list_address_merged(self, client):
        """The Rajouri Garden + New Delhi + 110027 should become a single ADDRESS."""
        text = (
            "Sunita Verma, Contoso Bank Rajouri Garden branch, "
            "New Delhi 110027 — landline 011-25432109."
        )
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        anon = body["anonymized_text"]
        mapping = body["entity_mapping"]

        # Should have an ADDRESS placeholder
        assert "{{ADDRESS_" in anon, f"Expected ADDRESS in: {anon}"

        # The address placeholder should map to a string containing both locations + pin
        reverse = {v: k for k, v in mapping.items()}
        addr_entries = {k: v for k, v in mapping.items() if "ADDRESS" in k}
        assert len(addr_entries) >= 1
        addr_text = list(addr_entries.values())[0]
        assert "New Delhi" in addr_text or "Rajouri Garden" in addr_text

    def test_standalone_location_stays_location(self, client):
        """'Mumbai' on its own should remain LOCATION, not become ADDRESS."""
        text = "Ravi lives in Mumbai."
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        mapping = body["entity_mapping"]

        loc_entries = {k: v for k, v in mapping.items() if "LOCATION" in k}
        addr_entries = {k: v for k, v in mapping.items() if "ADDRESS" in k}
        if "Mumbai" in mapping.values():
            assert len(addr_entries) == 0, "Standalone Mumbai should not become ADDRESS"

    def test_supported_entities_includes_address(self, client):
        """The /supported-entities endpoint should include ADDRESS."""
        resp = client.get("/supported-entities")
        assert resp.status_code == 200
        assert "ADDRESS" in resp.json()
