import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /supported-entities
# ---------------------------------------------------------------------------

class TestSupportedEntities:
    def test_no_non_us_foreign_country_entities(self, client):
        """Non-US/non-India country-specific entities must not appear."""
        resp = client.get("/supported-entities")
        assert resp.status_code == 200
        entities = resp.json()
        foreign_entities = {
            "UK_NHS", "SG_NRIC_FIN",
            "AU_ABN", "AU_ACN", "AU_TFN", "AU_MEDICARE",
        }
        found = foreign_entities & set(entities)
        assert not found, f"Foreign entities still present: {found}"

    def test_us_entities_present(self, client):
        """US-specific entities must be present when US recognizers are enabled."""
        resp = client.get("/supported-entities")
        entities = set(resp.json())
        expected = {"US_SSN", "US_ITIN", "US_PASSPORT", "US_DRIVER_LICENSE"}
        missing = expected - entities
        assert not missing, f"Expected US entities missing: {missing}"

    def test_india_entities_present(self, client):
        """India-specific entities must be present."""
        resp = client.get("/supported-entities")
        entities = set(resp.json())
        expected = {"IN_PAN", "IN_AADHAAR", "IN_APAAR", "IN_CKYC",
                    "IN_PRAN", "IN_VEHICLE_REGISTRATION",
                    "IN_PASSPORT", "IN_VOTER", "IN_DRIVING_LICENSE",
                    "PHONE_NUMBER", "IN_PIN_CODE", "CUSTOMER_ID"}
        missing = expected - entities
        assert not missing, f"Expected India entities missing: {missing}"

    def test_universal_entities_present(self, client):
        """Universal entities must be present."""
        resp = client.get("/supported-entities")
        entities = set(resp.json())
        expected = {"CREDIT_CARD", "EMAIL_ADDRESS", "IP_ADDRESS",
                    "DATE_TIME", "PERSON", "LOCATION", "URL"}
        missing = expected - entities
        assert not missing, f"Expected universal entities missing: {missing}"


# ---------------------------------------------------------------------------
# US Entity Detection (SSN / ITIN / Passport / DL)
# ---------------------------------------------------------------------------

class TestUSEntityDetection:
    """Verify US PII is detected and anonymized in realistic text scenarios."""

    def _anonymize(self, client, text: str) -> dict:
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
        )
        assert resp.status_code == 200
        return resp.json()

    def _detected_types(self, body: dict) -> set[str]:
        return {k.split("_", 1)[0].strip("{") + "_" + k.split("_", 1)[1].split("_")[0]
                for k in body["entity_mapping"]}

    def _has_entity(self, body: dict, entity_prefix: str) -> bool:
        return any(entity_prefix in k for k in body["entity_mapping"])

    # -- SSN -----------------------------------------------------------------

    def test_ssn_with_label(self, client):
        """SSN prefixed by 'SSN:' should be detected."""
        body = self._anonymize(client, "SSN: 219-09-9999")
        assert self._has_entity(body, "US_SSN"), (
            f"Expected US_SSN in mapping, got: {body['entity_mapping']}"
        )
        assert "219-09-9999" not in body["anonymized_text"]

    def test_ssn_in_employment_context(self, client):
        """SSN on a W-2 employment form should be detected."""
        text = "The W-2 form shows SSN 456-78-9012 for the employee."
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_SSN"), (
            f"Expected US_SSN in mapping, got: {body['entity_mapping']}"
        )
        assert "456-78-9012" not in body["anonymized_text"]

    def test_ssn_in_loan_application(self, client):
        """SSN provided in a banking context should be detected."""
        text = (
            "John provided his Social Security Number 287-14-5633 "
            "to the bank for the mortgage application."
        )
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_SSN"), (
            f"Expected US_SSN in mapping, got: {body['entity_mapping']}"
        )
        assert "287-14-5633" not in body["anonymized_text"]

    def test_ssn_round_trip(self, client):
        """SSN should survive anonymize → deanonymize round trip."""
        text = "Employee SSN: 321-54-9876 is on file."
        anon = self._anonymize(client, text)
        assert "321-54-9876" not in anon["anonymized_text"]

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        assert "321-54-9876" in resp.json()["text"]

    # -- ITIN ----------------------------------------------------------------

    def test_itin_with_label(self, client):
        """ITIN prefixed by 'ITIN:' should be detected (9xx range)."""
        body = self._anonymize(client, "ITIN: 912-70-1234")
        assert self._has_entity(body, "US_ITIN"), (
            f"Expected US_ITIN in mapping, got: {body['entity_mapping']}"
        )
        assert "912-70-1234" not in body["anonymized_text"]

    def test_itin_in_tax_filing(self, client):
        """ITIN in a tax-filing sentence should be detected."""
        text = "File taxes with ITIN 988-71-5432 on form 1040-NR."
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_ITIN"), (
            f"Expected US_ITIN in mapping, got: {body['entity_mapping']}"
        )
        assert "988-71-5432" not in body["anonymized_text"]

    def test_itin_full_name_context(self, client):
        """ITIN with full 'Individual Taxpayer Identification Number' label."""
        text = "Individual Taxpayer Identification Number: 900-78-5612"
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_ITIN"), (
            f"Expected US_ITIN in mapping, got: {body['entity_mapping']}"
        )
        assert "900-78-5612" not in body["anonymized_text"]

    # -- US Passport ---------------------------------------------------------

    def test_passport_nine_digit(self, client):
        """Nine-digit US passport number with context should be detected."""
        text = "US Passport number: 445566771"
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_PASSPORT"), (
            f"Expected US_PASSPORT in mapping, got: {body['entity_mapping']}"
        )
        assert "445566771" not in body["anonymized_text"]

    def test_passport_alphanumeric(self, client):
        """Alphanumeric US passport (C-prefix) should be detected."""
        text = "passport number C12345678"
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_PASSPORT"), (
            f"Expected US_PASSPORT in mapping, got: {body['entity_mapping']}"
        )
        assert "C12345678" not in body["anonymized_text"]

    def test_passport_at_border(self, client):
        """Passport in a travel context should be detected."""
        text = "She presented her US passport 556677889 at the border."
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_PASSPORT"), (
            f"Expected US_PASSPORT in mapping, got: {body['entity_mapping']}"
        )
        assert "556677889" not in body["anonymized_text"]

    # -- US Driver License ---------------------------------------------------

    def test_driver_license_florida(self, client):
        """Florida-format DL (letter + digits) should be detected."""
        text = "Florida driver license: G123-456-78-901-0"
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_DRIVER_LICENSE"), (
            f"Expected US_DRIVER_LICENSE in mapping, got: {body['entity_mapping']}"
        )

    def test_driver_license_california(self, client):
        """California-format DL (letter + 7 digits) should be detected."""
        text = "My California driver license number is B1234568."
        body = self._anonymize(client, text)
        assert self._has_entity(body, "US_DRIVER_LICENSE"), (
            f"Expected US_DRIVER_LICENSE in mapping, got: {body['entity_mapping']}"
        )


# ---------------------------------------------------------------------------
# POST /anonymize_unique
# ---------------------------------------------------------------------------

class TestAnonymizeUnique:
    def test_unique_ids_assigned(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "John Smith emailed Jane Doe.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "id" in body
        assert "{{PERSON_" in body["anonymized_text"]
        assert len(body["entity_mapping"]) > 0
        # Verify all mapping values are substrings of the original
        for original_val in body["entity_mapping"].values():
            assert original_val in body["text"]

    def test_returns_valid_uuid(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "My email is test@example.com"},
        )
        body = resp.json()
        uuid.UUID(body["id"])  # raises if not a valid UUID

    def test_duplicate_entity_gets_same_placeholder(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Email test@example.com and also test@example.com.",
                "language": "en",
            },
        )
        body = resp.json()
        # The same email should produce only one mapping entry
        email_placeholders = [
            k for k in body["entity_mapping"] if "EMAIL_ADDRESS" in k
        ]
        assert len(email_placeholders) == 1

    def test_no_pii_returns_original(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={"text": "Nothing sensitive here.", "language": "en"},
        )
        body = resp.json()
        assert body["anonymized_text"] == "Nothing sensitive here."
        assert body["entity_mapping"] == {}

    def test_no_false_in_pan_on_common_words(self, client):
        """'cardholder' must not be detected as IN_PAN (score_threshold filters it)."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Dispute raised by cardholder Ananya Iyer on credit card 4532-0150-1234-5671.",
                "language": "en",
            },
        )
        body = resp.json()
        assert "cardholder" not in body["entity_mapping"].values() or "IN_PAN" not in body["anonymized_text"].split("cardholder")[0]
        # More directly: no IN_PAN placeholder should appear for 'cardholder'
        pan_keys = [k for k in body["entity_mapping"] if "IN_PAN" in k]
        for k in pan_keys:
            assert body["entity_mapping"][k] != "cardholder"

    def test_allow_list_excludes_term(self, client):
        """Terms in allow_list must not be anonymized."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Account at Contoso Bank, Chennai branch.",
                "language": "en",
                "allow_list": ["Contoso Bank"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "Contoso Bank" in body["anonymized_text"]
        # Contoso Bank should NOT appear in entity_mapping values
        assert "Contoso Bank" not in body["entity_mapping"].values()

    def test_allow_list_preserves_other_entities(self, client):
        """Allow-list should only exclude specified terms; other PII still detected."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Kavitha visited Contoso Bank in Chennai.",
                "language": "en",
                "allow_list": ["Contoso Bank"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "Contoso Bank" in body["anonymized_text"]
        assert "{{PERSON_" in body["anonymized_text"]


# ---------------------------------------------------------------------------
# POST /deanonymize
# ---------------------------------------------------------------------------

class TestDeanonymize:
    def _anonymize(self, client, text: str) -> dict:
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
        )
        assert resp.status_code == 200
        return resp.json()

    def test_round_trip(self, client):
        """anonymize → deanonymize should recover the original text."""
        original = "Contact John Smith at john@example.com."
        anon = self._anonymize(client, original)

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": anon["anonymized_text"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == anon["id"]
        # Every original PII value should be restored
        assert "john@example.com" in body["text"]

    def test_deanonymize_modified_text(self, client):
        """Placeholders in LLM-modified text should still be restored."""
        anon = self._anonymize(client, "Hi, I'm Jane Doe.")

        # Simulate an LLM wrapping the placeholder in a sentence
        modified = f"The user mentioned is {list(anon['entity_mapping'].keys())[0]}."
        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": modified},
        )
        assert resp.status_code == 200
        body = resp.json()
        # The placeholder should have been replaced with the original value
        first_original = list(anon["entity_mapping"].values())[0]
        assert first_original in body["text"]

    def test_invalid_id_returns_404(self, client):
        resp = client.post(
            "/deanonymize",
            json={"id": "nonexistent-id", "text": "some text"},
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "No anonymization session found" in detail
        assert "expire after" in detail

    def test_no_placeholders_returns_text_as_is(self, client):
        anon = self._anonymize(client, "Call 555-123-4567.")

        resp = client.post(
            "/deanonymize",
            json={"id": anon["id"], "text": "No placeholders here."},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "No placeholders here."


# ---------------------------------------------------------------------------
# Overlap removal unit tests
# ---------------------------------------------------------------------------

class TestRemoveOverlapping:
    """Verify _remove_overlapping handles partial and full overlaps."""

    def _r(self, entity_type, start, end, score):
        from presidio_analyzer import RecognizerResult
        return RecognizerResult(entity_type=entity_type, start=start, end=end, score=score)

    def test_fully_contained_entity_removed(self):
        from app.main import _remove_overlapping
        results = [
            self._r("PHONE_NUMBER", 10, 24, 0.75),
            self._r("UK_NHS", 14, 24, 1.0),
        ]
        filtered = _remove_overlapping(results)
        types = {r.entity_type for r in filtered}
        assert types == {"PHONE_NUMBER"}

    def test_partial_overlap_keeps_longer_span(self):
        from app.main import _remove_overlapping
        results = [
            self._r("ENTITY_A", 10, 20, 0.9),
            self._r("ENTITY_B", 15, 25, 0.9),
        ]
        filtered = _remove_overlapping(results)
        assert len(filtered) == 1
        assert filtered[0].entity_type == "ENTITY_A"

    def test_non_overlapping_both_kept(self):
        from app.main import _remove_overlapping
        results = [
            self._r("PERSON", 0, 10, 0.85),
            self._r("EMAIL_ADDRESS", 20, 40, 1.0),
        ]
        filtered = _remove_overlapping(results)
        types = {r.entity_type for r in filtered}
        assert types == {"PERSON", "EMAIL_ADDRESS"}

    def test_same_span_keeps_longer_then_higher_score(self):
        from app.main import _remove_overlapping
        results = [
            self._r("UK_NHS", 10, 20, 1.0),
            self._r("DATE_TIME", 10, 20, 0.85),
        ]
        filtered = _remove_overlapping(results)
        assert len(filtered) == 1
        assert filtered[0].entity_type == "UK_NHS"


# ---------------------------------------------------------------------------
# State store unit tests
# ---------------------------------------------------------------------------

class TestStateStore:
    def test_save_and_get(self):
        import asyncio
        from app.state_store import AnonymizationRecord, AnonymizationStore

        s = AnonymizationStore()
        rec = AnonymizationRecord(
            original_text="hello",
            anonymized_text="{{PERSON_1}}",
            entity_mapping={"{{PERSON_1}}": "hello"},
        )
        rid = asyncio.get_event_loop().run_until_complete(s.save(rec))
        loaded = asyncio.get_event_loop().run_until_complete(s.get(rid))
        assert loaded is not None
        assert loaded.original_text == rec.original_text
        assert loaded.anonymized_text == rec.anonymized_text
        assert loaded.entity_mapping == rec.entity_mapping

    def test_save_with_app_id_and_hash_mapping(self):
        import asyncio
        from app.state_store import AnonymizationRecord, AnonymizationStore

        s = AnonymizationStore()
        rec = AnonymizationRecord(
            original_text="DL: MH 14 2019 0012345",
            anonymized_text="DL: abc123hash",
            entity_mapping={},
            app_id="test-app-id",
            hash_mapping={"abc123hash": "MH 14 2019 0012345"},
        )
        rid = asyncio.get_event_loop().run_until_complete(s.save(rec))
        loaded = asyncio.get_event_loop().run_until_complete(s.get(rid))
        assert loaded is not None
        assert loaded.app_id == "test-app-id"
        assert loaded.hash_mapping == {"abc123hash": "MH 14 2019 0012345"}

    def test_get_missing_returns_none(self):
        import asyncio
        from app.state_store import AnonymizationStore

        s = AnonymizationStore()
        assert asyncio.get_event_loop().run_until_complete(s.get("does-not-exist")) is None

    def test_delete(self):
        import asyncio
        from app.state_store import AnonymizationRecord, AnonymizationStore

        s = AnonymizationStore()
        rec = AnonymizationRecord(
            original_text="x", anonymized_text="y", entity_mapping={}
        )
        rid = asyncio.get_event_loop().run_until_complete(s.save(rec))
        assert asyncio.get_event_loop().run_until_complete(s.delete(rid)) is True
        assert asyncio.get_event_loop().run_until_complete(s.get(rid)) is None
        assert asyncio.get_event_loop().run_until_complete(s.delete(rid)) is False
