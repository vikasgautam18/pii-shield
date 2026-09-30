"""Tests for unit designators in addresses — "Flat no. 302, C 23, Prestige Towers".

The flat number, block letter and their labels have no recognizer.  Only the
unit number directly before an address was absorbed, and the "." of "no." was
read as the end of a sentence, so "my address is Flat no. 302, C 23, Prestige
Towers, Bangalore." left "no. 302, C" exposed.  The whole unit designation is
now absorbed, abbreviations no longer split an address, and the glue pattern
between address parts runs in linear time.
"""

import time

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import (
    _has_sentence_break,
    _is_address_glue,
    _unit_token_kind,
    merge_address_entities,
    remove_overlapping,
)


def _result(text: str, value: str, entity_type: str, score: float = 0.85) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type=entity_type, start=start, end=start + len(value), score=score
    )
    recognizer = "InPinCodeRecognizer" if entity_type == "IN_PIN_CODE" else "TransformersRecognizer"
    r.recognition_metadata = {"recognizer_name": recognizer}
    return r


def _merged(text: str, *entities: tuple[str, str]) -> list[tuple[str, str]]:
    results = [_result(text, value, entity_type) for value, entity_type in entities]
    merged = remove_overlapping(merge_address_entities(results, text))
    return sorted((text[r.start : r.end], r.entity_type) for r in merged)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestSentenceBreak:
    @pytest.mark.parametrize(
        "text",
        ["Flat no. 302", "Opp. City Mall", "H.No. 12", "Shanti Co-op. Hsg. Soc., Baner"],
    )
    def test_abbreviation_does_not_end_a_sentence(self, text):
        assert not _has_sentence_break(text, 0, len(text))

    @pytest.mark.parametrize(
        "text",
        [
            "Pune. Mumbai",
            "Pune 411001. Mumbai",
            'Pune". Mumbai',
            "Pune; Mumbai",
            "Pune! Mumbai",
            "Pune? Mumbai",
            "Pune \u2014 Mumbai",
        ],
    )
    def test_sentence_end_is_a_break(self, text):
        assert _has_sentence_break(text, 0, len(text))


class TestAddressGlue:
    @pytest.mark.parametrize("gap", ["", ", ", " branch, ", " Road, ", " no. "])
    def test_separators_and_address_words(self, gap):
        assert _is_address_glue(gap)

    @pytest.mark.parametrize("gap", [", Baner, ", " and ", " 302, "])
    def test_other_words(self, gap):
        assert not _is_address_glue(gap)


class TestUnitTokenKind:
    @pytest.mark.parametrize(
        "token", ["302", "F3003", "A-101", "12B", "12-3-456", "45/2", "2nd", "C", "B-Wing"]
    )
    def test_values(self, token):
        assert _unit_token_kind(token) == "value"

    @pytest.mark.parametrize("token", ["Flat", "no", "No", "Wing", "Floor", "Opp"])
    def test_labels(self, token):
        assert _unit_token_kind(token) == "label"

    @pytest.mark.parametrize("token", ["Prestige", "is", "sales", "Co-op", "FY", "c"])
    def test_other_words(self, token):
        assert _unit_token_kind(token) is None


class TestLooseGlueIsLinear:
    def test_long_word_before_a_stray_character(self):
        # The nested pattern this replaced took seconds at 24 letters and
        # doubled with every letter more.
        text = "Address: Pune " + "a" * 45 + ": Mumbai"
        results = [_result(text, "Pune", "LOCATION"), _result(text, "Mumbai", "LOCATION")]
        began = time.perf_counter()
        merged = merge_address_entities(results, text)
        assert time.perf_counter() - began < 1
        assert sorted(r.entity_type for r in merged) == ["LOCATION", "LOCATION"]


# ---------------------------------------------------------------------------
# merge_address_entities
# ---------------------------------------------------------------------------


class TestUnitDesignatorAbsorption:
    def test_reported_address(self):
        text = "my address is Flat no. 302, C 23, Prestige Towers, Bangalore."
        assert _merged(
            text,
            ("Flat", "LOCATION"),
            ("Prestige Towers", "LOCATION"),
            ("Bangalore", "LOCATION"),
        ) == [("Flat no. 302, C 23, Prestige Towers, Bangalore", "ADDRESS")]

    def test_unit_label_tagged_on_its_own_is_absorbed(self):
        # No indicator precedes "Flat", so it is not merged as an address part,
        # but the address that follows absorbs it with its number.
        text = "Send it to Flat no. 302, C 23, Prestige Towers, Bangalore."
        assert _merged(
            text,
            ("Flat", "LOCATION"),
            ("Prestige Towers", "LOCATION"),
            ("Bangalore", "LOCATION"),
        ) == [("Flat no. 302, C 23, Prestige Towers, Bangalore", "ADDRESS")]

    @pytest.mark.parametrize(
        ("text", "entities"),
        [
            (
                "H.No. 12-3-456, Banjara Hills, Hyderabad 500034",
                [("Banjara Hills", "LOCATION"), ("Hyderabad", "LOCATION"), ("500034", "IN_PIN_CODE")],
            ),
            (
                "Door No. 45/2, 2nd Floor, B-Wing, Lodha Park, Worli, Mumbai 400018",
                [("Lodha Park", "LOCATION"), ("Worli", "LOCATION"), ("Mumbai", "LOCATION"),
                 ("400018", "IN_PIN_CODE")],
            ),
            (
                "Flat 5B, Tower C, DLF Phase 2, Gurgaon",
                [("DLF Phase 2", "LOCATION"), ("Gurgaon", "LOCATION")],
            ),
            (
                "Apt. 1204, Lodha Bellissimo, Worli, Mumbai",
                [("Lodha Bellissimo", "LOCATION"), ("Worli", "LOCATION"), ("Mumbai", "LOCATION")],
            ),
        ],
    )
    def test_whole_unit_designation_is_absorbed(self, text, entities):
        assert _merged(text, *entities) == [(text, "ADDRESS")]

    def test_abbreviation_period_does_not_split_an_address(self):
        text = "Address: Flat 12, Shanti Co-op. Hsg. Soc., Baner Road, Aundh, Pune 411007"
        assert _merged(
            text,
            ("Shanti Co-op", "LOCATION"),
            ("Baner Road", "LOCATION"),
            ("Aundh", "LOCATION"),
            ("Pune", "LOCATION"),
            ("411007", "IN_PIN_CODE"),
        ) == [("Flat 12, Shanti Co-op. Hsg. Soc., Baner Road, Aundh, Pune 411007", "ADDRESS")]

    def test_wrapped_unit_designation_under_an_indicator(self):
        text = "Address: Flat no. 302,\nC 23, Prestige Towers,\nBangalore 560001"
        assert _merged(
            text,
            ("Prestige Towers", "LOCATION"),
            ("Bangalore", "LOCATION"),
            ("560001", "IN_PIN_CODE"),
        ) == [("Flat no. 302,\nC 23, Prestige Towers,\nBangalore 560001", "ADDRESS")]

    def test_label_before_a_number_inside_the_address(self):
        # NER tagged "No"; the address group starts there, so "Door" is the
        # only word left to absorb.
        text = "Correspondence address: Door No. 45/2, 3rd Cross, Jayanagar, Bangalore 560011"
        assert _merged(
            text,
            ("No", "LOCATION"),
            ("Jayanagar", "LOCATION"),
            ("Bangalore", "LOCATION"),
            ("560011", "IN_PIN_CODE"),
        ) == [("Door No. 45/2, 3rd Cross, Jayanagar, Bangalore 560011", "ADDRESS")]


class TestUnitDesignatorGuards:
    def test_no_before_a_name_is_not_a_unit_label(self):
        text = "There is no Prestige Towers, Bangalore in our records."
        assert _merged(
            text, ("Prestige Towers", "LOCATION"), ("Bangalore", "LOCATION")
        ) == [("Prestige Towers, Bangalore", "ADDRESS")]

    def test_label_without_a_number_is_not_absorbed(self):
        text = "Our sales unit, Andheri, Mumbai handles it."
        assert _merged(text, ("Andheri", "LOCATION"), ("Mumbai", "LOCATION")) == [
            ("Andheri, Mumbai", "ADDRESS"),
        ]

    def test_year_range_is_not_a_house_number(self):
        text = "In FY 2024-25, Andheri, Mumbai posted record deposits."
        assert _merged(text, ("Andheri", "LOCATION"), ("Mumbai", "LOCATION")) == [
            ("Andheri, Mumbai", "ADDRESS"),
        ]

    def test_decimal_is_not_a_unit_number(self):
        text = "Rates rose to 5.30, Andheri, Mumbai reported."
        assert _merged(text, ("Andheri", "LOCATION"), ("Mumbai", "LOCATION")) == [
            ("Andheri, Mumbai", "ADDRESS"),
        ]

    def test_stops_at_another_entity(self):
        text = "Contoso Bank T Nagar branch, Chennai"
        assert _merged(
            text,
            ("Contoso Bank T", "ORGANIZATION"),
            ("Nagar", "LOCATION"),
            ("Chennai", "LOCATION"),
        ) == [("Contoso Bank T", "ORGANIZATION"), ("Nagar branch, Chennai", "ADDRESS")]

    def test_date_keeps_its_own_label(self):
        text = "Delivered on 12/05/2024, Prestige Towers, Bangalore."
        assert _merged(
            text,
            ("12/05/2024", "DATE_TIME"),
            ("Prestige Towers", "LOCATION"),
            ("Bangalore", "LOCATION"),
        ) == [("12/05/2024", "DATE_TIME"), ("Prestige Towers, Bangalore", "ADDRESS")]

    def test_number_on_an_unrelated_line_is_not_absorbed(self):
        text = "Ref no. 302,\nPrestige Towers, Bangalore"
        assert _merged(
            text, ("Prestige Towers", "LOCATION"), ("Bangalore", "LOCATION")
        ) == [("Prestige Towers, Bangalore", "ADDRESS")]

    def test_year_range_does_not_promote_an_organization(self):
        # The indicator in the previous sentence makes the name a candidate;
        # only a unit number directly before it could promote it to ADDRESS.
        text = "Address proof is pending. FY 2024-25, Contoso Bank reported higher profits."
        assert _merged(text, ("Contoso Bank", "ORGANIZATION")) == [
            ("Contoso Bank", "ORGANIZATION"),
        ]

    def test_sentences_stay_separate(self):
        text = "I live in Pune. Mumbai is where I work."
        assert _merged(text, ("Pune", "LOCATION"), ("Mumbai", "LOCATION")) == [
            ("Mumbai", "LOCATION"), ("Pune", "LOCATION"),
        ]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def _anonymize(client, text: str) -> tuple[str, dict[str, str], str]:
    resp = client.post("/anonymize_unique", json={"text": text})
    assert resp.status_code == 200
    data = resp.json()
    restored = client.post(
        "/deanonymize", json={"id": data["id"], "text": data["anonymized_text"]}
    ).json()["text"]
    return data["anonymized_text"], data["entity_mapping"], restored


class TestAddressUnitsEndToEnd:
    @pytest.mark.parametrize(
        ("text", "exposed", "kept"),
        [
            ("my address is Flat no. 302, C 23, Prestige Towers, Bangalore.",
             ["Flat", "302", "C 23", "Prestige", "Bangalore"], "my address is "),
            ("Send it to Flat no. 302, C 23, Prestige Towers, Bangalore. My mobile is 9876543210.",
             ["Flat", "302", "C 23", "Prestige", "9876543210"], "Send it to "),
            ("Residing at H.No. 12-3-456, Street No. 5, Banjara Hills, Hyderabad 500034",
             ["H.No", "12-3-456", "Banjara", "500034"], "Residing at "),
            ("Correspondence address: Door No. 45/2, 3rd Cross, Jayanagar 4th Block, Bangalore 560011",
             ["Door", "45/2", "3rd Cross", "Jayanagar", "560011"], "Correspondence address: "),
            ("Please update my address to Plot No. 17, Sector 21, Kharghar, Navi Mumbai 410210",
             ["Plot", "17", "Kharghar", "410210"], "Please update my address to "),
            ("I live in Apt. 1204, Lodha Bellissimo, Worli, Mumbai",
             ["Apt", "1204", "Lodha", "Worli"], "I live in "),
            ("My address is Room no. 4, Sai Kripa Chawl, Dharavi, Mumbai",
             ["Room", "no. 4", "Sai Kripa", "Dharavi"], "My address is "),
        ],
    )
    def test_whole_address_is_masked(self, client, text, exposed, kept):
        anonymized, mapping, restored = _anonymize(client, text)
        assert not [part for part in exposed if part in anonymized]
        assert kept in anonymized
        assert any(k.startswith("{{ADDRESS_") for k in mapping)
        assert restored == text

    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("Order no. 12345 shipped to Bangalore yesterday.", "Order no. 12345 shipped to"),
            ("Room no. 4 is booked for the meeting in Mumbai.", "Room no. 4 is booked for the meeting in"),
            ("My flat rent of 25000 is due in Pune.", "My flat rent of 25000 is due in"),
            ("Delivered on 12/05/2024, Prestige Towers, Bangalore.", "Delivered on "),
        ],
    )
    def test_ordinary_text_stays(self, client, text, kept):
        anonymized, _, restored = _anonymize(client, text)
        assert kept in anonymized
        assert restored == text

    def test_separate_sentences_stay_separate_locations(self, client):
        anonymized, mapping, _ = _anonymize(client, "I live in Pune. Mumbai is where I work.")
        assert anonymized == "I live in {{LOCATION_2}}. {{LOCATION_1}} is where I work."
        assert not any(k.startswith("{{ADDRESS_") for k in mapping)
