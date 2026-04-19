import pytest
from fastapi.testclient import TestClient

from app.main import app
from pii_shield.recognizers.geo_coordinate import GeoCoordinateRecognizer


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Unit tests — recognizer pattern matching
# ---------------------------------------------------------------------------


class TestGeoCoordinateRecognizerPatterns:
    """Verify the recognizer detects geo-coordinates in isolation."""

    recognizer = GeoCoordinateRecognizer()

    def _match(self, text: str) -> list:
        return self.recognizer.analyze(text, entities=["GEO_COORDINATE"])

    # ── Positive: DD pairs ────────────────────────────────────────────────

    def test_dd_pair_basic(self):
        results = self._match("coordinates 28.6139, 77.2090")
        assert len(results) == 1
        assert results[0].entity_type == "GEO_COORDINATE"

    def test_dd_pair_negative_lat(self):
        results = self._match("gps -33.8688, 151.2093")
        assert len(results) == 1

    def test_dd_pair_negative_lon(self):
        results = self._match("coords 40.7128, -74.0060")
        assert len(results) == 1

    def test_dd_pair_both_negative(self):
        results = self._match("location -34.6037, -58.3816")
        assert len(results) == 1

    def test_dd_pair_no_space_after_comma(self):
        results = self._match("coords 28.6139,77.2090")
        assert len(results) == 1

    def test_dd_pair_semicolon_separator(self):
        results = self._match("position 28.6139; 77.2090")
        assert len(results) == 1

    def test_dd_pair_high_precision(self):
        results = self._match("gps 28.613939, 77.209021")
        assert len(results) == 1

    def test_dd_pair_boundary_values(self):
        """Lat ±90, lon ±180 are valid boundary values."""
        results = self._match("coordinates 90.00, 180.00")
        assert len(results) == 1

    def test_dd_pair_zero_zero(self):
        results = self._match("coordinates 0.0000, 0.0000")
        assert len(results) == 1

    def test_multiple_pairs(self):
        text = "From coordinates 28.6139, 77.2090 to coordinates 19.0760, 72.8777"
        results = self._match(text)
        assert len(results) == 2

    # ── Positive: Labeled ─────────────────────────────────────────────────

    def test_labeled_lat_colon(self):
        results = self._match("lat: 28.6139")
        assert len(results) == 1

    def test_labeled_latitude_colon(self):
        results = self._match("latitude: 28.6139")
        assert len(results) == 1

    def test_labeled_lon_colon(self):
        results = self._match("lon: 77.2090")
        assert len(results) == 1

    def test_labeled_longitude_equals(self):
        results = self._match("longitude = -77.2090")
        assert len(results) == 1

    def test_labeled_lng(self):
        results = self._match("lng: 77.2090")
        assert len(results) == 1

    def test_labeled_case_insensitive(self):
        results = self._match("LAT: 28.6139")
        assert len(results) == 1

    # ── Positive: Cardinal direction ──────────────────────────────────────

    def test_cardinal_north(self):
        results = self._match("28.6139° N")
        assert len(results) == 1

    def test_cardinal_south(self):
        results = self._match("33.8688° S")
        assert len(results) == 1

    def test_cardinal_east(self):
        results = self._match("77.2090° E")
        assert len(results) == 1

    def test_cardinal_west(self):
        results = self._match("74.0060° W")
        assert len(results) == 1

    def test_cardinal_no_degree_symbol(self):
        results = self._match("28.6139 N")
        assert len(results) == 1

    def test_cardinal_no_space(self):
        results = self._match("28.6139°N")
        assert len(results) == 1

    # ── Positive: DMS ─────────────────────────────────────────────────────

    def test_dms_basic(self):
        results = self._match("28°36'50\"N")
        assert len(results) == 1

    def test_dms_spaced(self):
        results = self._match("28° 36' 50\" N")
        assert len(results) == 1

    def test_dms_with_decimal_seconds(self):
        results = self._match("28°36'50.4\"N")
        assert len(results) == 1

    def test_dms_minutes_only(self):
        """DMS without seconds should still match."""
        results = self._match("28°36'N")
        assert len(results) == 1

    # ── Negative cases ────────────────────────────────────────────────────

    def test_out_of_range_lat(self):
        """Latitude > 90 should be invalidated."""
        results = self._match("coordinates 95.0000, 77.2090")
        assert len(results) == 0

    def test_out_of_range_lon(self):
        """Longitude > 180 should be invalidated."""
        results = self._match("coordinates 28.6139, 200.0000")
        assert len(results) == 0

    def test_both_out_of_range(self):
        results = self._match("coordinates 95.0000, 200.0000")
        assert len(results) == 0

    def test_empty_text(self):
        results = self._match("")
        assert len(results) == 0

    def test_no_pii(self):
        results = self._match("Nothing sensitive here.")
        assert len(results) == 0

    def test_single_decimal_no_context(self):
        """A lone decimal without context should not be detected as pair."""
        results = self._match("The value is 42.5")
        # Should not match DD pair (needs two values)
        pair_results = [r for r in results if r.recognition_metadata.get(
            "pattern_name") == "geo_dd_pair"]
        assert len(pair_results) == 0

    def test_ip_address_not_matched(self):
        """IP addresses should not be matched as coordinates."""
        results = self._match("Server IP is 192.168.1.1")
        # IP has 4 octets, not a coordinate format
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Integration tests — geo-coordinate detection via /anonymize_unique
# ---------------------------------------------------------------------------


class TestGeoCoordinateAnonymize:
    def test_coordinate_pair_anonymized(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "The GPS coordinates are 28.6139, 77.2090 for the site.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "28.6139" not in body["anonymized_text"]
        anon = body["anonymized_text"]
        assert "{{GEO_COORDINATE_" in anon

    def test_labeled_coordinates_anonymized(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "lat: 28.6139 lon: 77.2090",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "28.6139" not in body["anonymized_text"]
        assert "77.2090" not in body["anonymized_text"]

    def test_dms_coordinates_anonymized(self, client):
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "Site location is 28°36'50\"N 77°12'32\"E.",
                "language": "en",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "28°36" not in body["anonymized_text"]

    def test_round_trip(self, client):
        original = "GPS coordinates: 28.6139, 77.2090"
        anon_resp = client.post(
            "/anonymize_unique",
            json={"text": original, "language": "en"},
        )
        assert anon_resp.status_code == 200
        anon_body = anon_resp.json()
        assert "28.6139" not in anon_body["anonymized_text"]

        deanon_resp = client.post(
            "/deanonymize",
            json={"id": anon_body["id"], "text": anon_body["anonymized_text"]},
        )
        assert deanon_resp.status_code == 200
        assert "28.6139" in deanon_resp.json()["text"]

    def test_coordinates_in_json_text(self, client):
        json_text = '{"location": {"coordinates": "28.6139, 77.2090"}}'
        resp = client.post(
            "/anonymize_unique",
            json={"text": json_text, "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "28.6139" not in body["anonymized_text"]

    def test_mixed_pii_with_coordinates(self, client):
        text = (
            "Customer Rajesh Kumar (email: rajesh@example.com) "
            "visited the branch at GPS coordinates 19.0760, 72.8777."
        )
        resp = client.post(
            "/anonymize_unique",
            json={"text": text, "language": "en"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "19.0760" not in body["anonymized_text"]
        assert "rajesh@example.com" not in body["anonymized_text"]

    def test_coordinate_in_entity_type_allow_list(self, client):
        """GEO_COORDINATE in entity_type_allow_list should skip detection."""
        resp = client.post(
            "/anonymize_unique",
            json={
                "text": "GPS coordinates: 28.6139, 77.2090",
                "language": "en",
                "entity_type_allow_list": ["GEO_COORDINATE"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "28.6139" in body["anonymized_text"]
