"""Tests for PERSON → LOCATION reclassification of Indian place names."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


# ---- Unit tests for _reclassify_person_as_location ----

from app.main import _reclassify_person_as_location
from presidio_analyzer import RecognizerResult


def _spacy_person(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="PERSON", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


def _spacy_location(start: int, end: int, score: float = 0.85) -> RecognizerResult:
    r = RecognizerResult(entity_type="LOCATION", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


def _other_person(start: int, end: int, score: float = 0.9) -> RecognizerResult:
    r = RecognizerResult(entity_type="PERSON", start=start, end=end, score=score)
    r.recognition_metadata = {"recognizer_name": "SomeOtherRecognizer"}
    return r


class TestReclassifyUnit:
    """Unit tests for the _reclassify_person_as_location helper."""

    def test_village_context(self):
        text = "village Malkhed"
        results = [_spacy_person(8, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_taluka_context(self):
        text = "Taluka Sholapur"
        results = [_spacy_person(7, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_district_context(self):
        text = "district Pune"
        results = [_spacy_person(9, 13)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_road_context(self):
        text = "Barshi Road, Sholapur"
        #                      ^12    ^20
        results = [_spacy_person(13, 21)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_branch_context(self):
        text = "Branch: Contoso Bank, Barshi Road, Sholapur"
        results = [_spacy_person(35, 43)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_nagar_context(self):
        text = "near Rajiv Nagar, Lucknow"
        results = [_spacy_person(18, 25)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_tehsil_context(self):
        text = "Tehsil Badnapur"
        results = [_spacy_person(7, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_town_context(self):
        text = "town of Miraj"
        results = [_spacy_person(8, 13)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_city_context(self):
        text = "city Bengaluru"
        results = [_spacy_person(5, 14)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_no_context_stays_person(self):
        """A PERSON without location context must stay PERSON."""
        text = "Mr. Govind Rao applied"
        results = [_spacy_person(4, 14)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "PERSON"

    def test_non_spacy_person_untouched(self):
        """Only NER-sourced PERSON entities should be reclassified."""
        text = "village Malkhed"
        results = [_other_person(8, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "PERSON"

    def test_transformers_person_reclassified(self):
        """TransformersRecognizer PERSON entities should also be reclassified."""
        text = "Address: Flat 501, Kumar Pinnacle"
        r = RecognizerResult(entity_type="PERSON", start=19, end=33, score=1.0)
        r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
        out = _reclassify_person_as_location([r], text)
        assert out[0].entity_type == "LOCATION"

    def test_stanza_person_reclassified(self):
        """StanzaRecognizer PERSON entities should also be reclassified."""
        text = "Flat 201, Sagar Heights"
        r = RecognizerResult(entity_type="PERSON", start=10, end=23, score=0.9)
        r.recognition_metadata = {"recognizer_name": "StanzaRecognizer"}
        out = _reclassify_person_as_location([r], text)
        assert out[0].entity_type == "LOCATION"

    def test_address_context(self):
        """The word 'Address' should trigger reclassification."""
        text = "Address: Kumar Pinnacle"
        results = [_spacy_person(9, 23)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_flat_context(self):
        """The word 'Flat' should trigger reclassification."""
        text = "Flat 501, Kumar Pinnacle"
        results = [_spacy_person(10, 24)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_apartment_context(self):
        """The word 'apartment' should trigger reclassification."""
        text = "apartment Gandhi Towers"
        results = [_spacy_person(10, 23)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_residing_context(self):
        """The word 'residing' should trigger reclassification."""
        text = "residing at Kumar Pinnacle"
        results = [_spacy_person(12, 26)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_already_location_untouched(self):
        """Entities already typed LOCATION should not be affected."""
        text = "village Malkhed"
        results = [_spacy_location(8, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_context_too_far_away(self):
        """Context word more than 40 chars away should not trigger reclassification."""
        text = "village" + " " * 50 + "Malkhed"
        start = text.index("Malkhed")
        results = [_spacy_person(start, start + 7)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "PERSON"

    def test_context_case_insensitive(self):
        text = "VILLAGE Malkhed"
        results = [_spacy_person(8, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_multiple_entities_mixed(self):
        """Mix of real person and location-context person."""
        text = "Mr. Govind Rao from village Malkhed"
        person = _spacy_person(4, 14)   # Govind Rao
        place = _spacy_person(28, 35)   # Malkhed
        out = _reclassify_person_as_location([person, place], text)
        assert out[0].entity_type == "PERSON"    # Govind Rao stays
        assert out[1].entity_type == "LOCATION"  # Malkhed reclassified

    def test_street_context(self):
        text = "Street Bazaar Area"
        results = [_spacy_person(7, 13)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_sector_context(self):
        text = "Sector 21, Chandigarh"
        results = [_spacy_person(11, 21)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_office_context(self):
        text = "office Jabalpur"
        results = [_spacy_person(7, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"

    def test_mandal_context(self):
        text = "Mandal Adilabad"
        results = [_spacy_person(7, 15)]
        out = _reclassify_person_as_location(results, text)
        assert out[0].entity_type == "LOCATION"


# ---- Integration tests ----

class TestReclassifyIntegration:
    """Full anonymization tests verifying reclassification end-to-end."""

    def test_rural_banking_scenario(self, client):
        """The full rural banking text from the user's report."""
        text = (
            "Agricultural loan application from Govind Rao, village Malkhed, "
            "Taluka Sholapur, Maharashtra 413005."
        )
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        anon = body["anonymized_text"]
        mapping = body["entity_mapping"]

        # Build reverse map: original → placeholder
        reverse = {v: k for k, v in mapping.items()}

        # Govind Rao should remain PERSON
        if "Govind Rao" in reverse:
            assert "PERSON" in reverse["Govind Rao"], (
                f"Govind Rao mapped to {reverse['Govind Rao']}, expected PERSON"
            )

        # Malkhed and Sholapur should be LOCATION (reclassified from PERSON)
        for place in ["Malkhed", "Sholapur"]:
            if place in reverse:
                assert "LOCATION" in reverse[place], (
                    f"{place} mapped to {reverse[place]}, expected LOCATION"
                )

    def test_branch_address_reclassified(self, client):
        """Branch: ... Sholapur should become LOCATION."""
        text = "Branch: Contoso Bank, Barshi Road, Sholapur."
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        mapping = body["entity_mapping"]
        reverse = {v: k for k, v in mapping.items()}

        if "Sholapur" in reverse:
            assert "LOCATION" in reverse["Sholapur"]

    def test_real_person_preserved(self, client):
        """Real person names without location context stay PERSON."""
        text = "Customer Rajesh Kumar submitted the application."
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        mapping = body["entity_mapping"]
        reverse = {v: k for k, v in mapping.items()}

        if "Rajesh Kumar" in reverse:
            assert "PERSON" in reverse["Rajesh Kumar"]

    def test_apartment_name_in_address_block(self, client):
        """Apartment names (e.g. 'Kumar Pinnacle') after 'Address: Flat ...' should become ADDRESS, not PERSON."""
        text = (
            "Address: Flat 501, Kumar Pinnacle, Baner, Pune 411045."
        )
        resp = client.post("/anonymize_unique", json={"text": text})
        assert resp.status_code == 200
        body = resp.json()
        mapping = body["entity_mapping"]
        reverse = {v: k for k, v in mapping.items()}

        # "Kumar Pinnacle" should NOT appear as PERSON
        for original, placeholder in reverse.items():
            if "Kumar" in original:
                assert "PERSON" not in placeholder, (
                    f"'{original}' mapped to {placeholder}, expected LOCATION or ADDRESS"
                )
