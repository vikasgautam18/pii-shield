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
        # Starts at "Flat" (9), not "Kumar Pinnacle" (19) — a unit number or
        # its label left outside the ADDRESS span would leak.
        assert addrs[0].start == 9
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


# ---- address-context merging (ORGANIZATION / NRP + unit numbers) ----


def _org(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="ORGANIZATION", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


def _nrp(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="NRP", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


class TestAddressContextMerging:
    """Building/society names come back as LOCATION, ORGANIZATION or NRP
    depending on context, so near an address indicator all three count."""

    def test_lone_nrp_after_indicator_becomes_address(self):
        text = "residing at F3003, Kanakia Zen World"
        start = text.index("Kanakia")
        merged = _merge_address_entities([_nrp(start, len(text))], text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        # Absorbs the flat number "F3003" so it is not left exposed.
        assert text[addrs[0].start : addrs[0].end] == "F3003, Kanakia Zen World"

    def test_lone_organization_after_indicator_becomes_address(self):
        text = "He lives at Prestige Shantiniketan"
        start = text.index("Prestige")
        merged = _merge_address_entities([_org(start, len(text))], text)
        assert [r.entity_type for r in merged] == ["ADDRESS"]

    def test_organization_without_indicator_is_untouched(self):
        # No address indicator -> ordinary ORGANIZATION detection is unaffected.
        text = "She works at Contoso Manufacturing on weekdays"
        start = text.index("Contoso")
        merged = _merge_address_entities([_org(start, start + 21)], text)
        assert [r.entity_type for r in merged] == ["ORGANIZATION"]

    def test_nrp_without_indicator_is_untouched(self):
        text = "The Kanakia Zen group sponsors the event"
        merged = _merge_address_entities([_nrp(4, 21)], text)
        assert [r.entity_type for r in merged] == ["NRP"]

    def test_unit_number_absorbed_into_multi_entity_address(self):
        text = "Send it to 12 MG Road, Bengaluru 560001"
        road = text.index("MG Road")
        merged = _merge_address_entities(
            [_loc(road, road + 7), _loc(23, 32), _pin(33, 39)], text
        )
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert text[addrs[0].start : addrs[0].end].startswith("12 MG Road")

    def test_ordinary_word_before_address_is_not_absorbed(self):
        # Only unit-number-shaped tokens are absorbed, never plain words.
        text = "Address: near Kumar Pinnacle, Pune 411045"
        merged = _merge_address_entities([_loc(14, 28), _loc(30, 34), _pin(35, 41)], text)
        addrs = [r for r in merged if r.entity_type == "ADDRESS"]
        assert len(addrs) == 1
        assert text[addrs[0].start : addrs[0].end].startswith("Kumar Pinnacle")

    def test_standalone_location_still_not_promoted(self):
        # Regression guard: a lone LOCATION must keep its type even with an
        # indicator present, so ordinary place mentions are not over-redacted.
        text = "He lives at Mumbai and works there."
        merged = _merge_address_entities([_loc(12, 18)], text)
        assert [r.entity_type for r in merged] == ["LOCATION"]
