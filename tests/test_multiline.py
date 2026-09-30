"""Tests for multi-line input: context on one line must not leak into another.

In forms, lists and chat messages every line is its own statement.  A keyword
on one line must not relabel an entity on another, and no NER entity may run
across a line break.  A line that introduces the next one — a "Label:" line or
a short heading ending in the keyword — still counts, and keywords elsewhere may
still widen an address, since masking more never exposes anything.
"""

import re

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import (
    merge_address_entities,
    normalize_person_titles,
    prefer_line_context,
    reclassify_person_as_location,
    reclassify_phone_as_bank_account,
    split_at_line_breaks,
)
from pii_shield.recognizers.customer_id import CustomerIdRecognizer
from pii_shield.recognizers.in_aadhaar import InAadhaarImprovedRecognizer
from pii_shield.recognizers.in_apaar import InApaarRecognizer
from pii_shield.recognizers.in_pin_code import InPinCodeRecognizer
from pii_shield.recognizers.in_pran import InPranRecognizer
from pii_shield.text_lines import CONTEXT_OFF_LINE_KEY, block_start, line_end, line_start


def _result(
    text: str,
    value: str,
    entity_type: str,
    recognizer: str = "TransformersRecognizer",
    score: float = 0.9,
) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type=entity_type, start=start, end=start + len(value), score=score
    )
    r.recognition_metadata = {"recognizer_name": recognizer}
    return r


def _spans(text: str, results: list[RecognizerResult]) -> list[tuple[str, str]]:
    return sorted((text[r.start : r.end], r.entity_type) for r in results)


def _matches(recognizer, entity: str, text: str) -> list[str]:
    return [text[r.start : r.end] for r in recognizer.analyze(text, entities=[entity])]


# ---------------------------------------------------------------------------
# Line helpers
# ---------------------------------------------------------------------------


class TestLineHelpers:
    text = "Name: Priya\nAadhaar number:\n2345 6789 0123\nCity: Pune"

    def test_line_bounds(self):
        pos = self.text.index("Priya")
        assert line_start(self.text, pos) == 0
        assert line_end(self.text, pos) == self.text.index("\n")

    def test_last_line_ends_at_end_of_text(self):
        assert line_end(self.text, self.text.index("Pune")) == len(self.text)

    def test_block_includes_label_line_above(self):
        pos = self.text.index("2345")
        assert block_start(self.text, pos) == self.text.index("Aadhaar number:")

    def test_block_excludes_ordinary_line_above(self):
        pos = self.text.index("Pune")
        assert block_start(self.text, pos) == line_start(self.text, pos)

    def test_block_on_first_line(self):
        assert block_start(self.text, self.text.index("Priya")) == 0

    def test_label_line_with_windows_line_ending(self):
        text = "Aadhaar number:\r\n2345 6789 0123"
        assert block_start(text, text.index("2345")) == 0

    def test_heading_ending_in_keyword_introduces_next_line(self):
        text = "Correspondence Address\nKumar Pinnacle"
        keyword = re.compile(r"(?i)\baddress\b")
        assert block_start(text, text.index("Kumar"), keyword) == 0

    def test_line_merely_containing_keyword_does_not(self):
        text = "Address verified yesterday\nKumar Pinnacle"
        keyword = re.compile(r"(?i)\baddress\b")
        pos = text.index("Kumar")
        assert block_start(text, pos, keyword) == pos

    @pytest.mark.parametrize(
        ("line_above", "introduces"),
        [
            ("Account", True),
            ("Bank account", True),
            ("Savings bank account", True),
            ("Details of bank account", False),
            ("Please update my account", False),
        ],
    )
    def test_only_short_headings_introduce_the_next_line(self, line_above, introduces):
        text = f"{line_above}\n9876543210"
        keyword = re.compile(r"(?i)\baccount\b")
        pos = text.index("9876543210")
        assert (block_start(text, pos, keyword) == 0) is introduces


# ---------------------------------------------------------------------------
# PERSON → LOCATION: location words only count on the entity's own line
# ---------------------------------------------------------------------------


class TestLocationContextStaysOnItsLine:
    def _type(self, text: str, value: str) -> str:
        results = [_result(text, value, "PERSON")]
        return reclassify_person_as_location(results, text)[0].entity_type

    def test_reported_example(self):
        text = (
            "Customer RAJESH KUMAR SHARMA residing at Mumbai.\n"
            "My name is Mr. R.K. Sharma."
        )
        assert self._type(text, "R.K.") == "PERSON"

    def test_previous_sentence_on_same_line(self):
        text = (
            "Customer RAJESH KUMAR SHARMA residing at Mumbai. "
            "My name is Mr. R.K. Sharma."
        )
        assert self._type(text, "R.K.") == "PERSON"

    def test_location_word_on_previous_line(self):
        text = "Branch: Andheri West\nCustomer: Priya Menon"
        assert self._type(text, "Priya Menon") == "PERSON"

    def test_form_without_colons(self):
        text = "Branch Andheri West\nCustomer Priya Menon"
        assert self._type(text, "Priya Menon") == "PERSON"

    def test_location_word_on_same_line(self):
        text = "Branch: Andheri West\nCustomer: Priya Menon"
        assert self._type(text, "Andheri West") == "LOCATION"

    def test_label_line_above_counts(self):
        text = "Branch:\nAndheri West"
        assert self._type(text, "Andheri West") == "LOCATION"

    def test_heading_line_above_counts(self):
        text = "Correspondence Address\nKumar Pinnacle"
        assert self._type(text, "Kumar Pinnacle") == "LOCATION"

    def test_sentence_ending_in_location_word_does_not_count(self):
        text = "This is my new address\nRahul Sharma called"
        assert self._type(text, "Rahul Sharma") == "PERSON"

    def test_abbreviations_do_not_end_the_sentence(self):
        text = "Flat 12, Hsg. Soc. Kumar Pinnacle"
        assert self._type(text, "Kumar Pinnacle") == "LOCATION"


# ---------------------------------------------------------------------------
# PHONE_NUMBER → IN_BANK_ACCOUNT: account cues only count on the number's line
# ---------------------------------------------------------------------------


class TestAccountCuesStayOnTheirLine:
    def _type(self, text: str) -> str:
        results = [_result(text, "9876543210", "PHONE_NUMBER", "InPhoneRecognizer")]
        return reclassify_phone_as_bank_account(results, text)[0].entity_type

    def test_account_word_on_previous_line(self):
        assert self._type("My account was blocked\n9876543210 is my new mobile") == "PHONE_NUMBER"

    def test_ifsc_on_next_line(self):
        assert self._type("Mobile: 9876543210\nIFSC: SBIN0001234") == "PHONE_NUMBER"

    def test_account_label_line_above(self):
        assert self._type("Account number:\n9876543210") == "IN_BANK_ACCOUNT"

    def test_account_heading_line_above(self):
        assert self._type("Bank account\n9876543210") == "IN_BANK_ACCOUNT"

    def test_sentence_ending_in_account_word_on_previous_line(self):
        assert self._type("Please update my account\n9876543210 is my new mobile") == "PHONE_NUMBER"

    def test_account_word_in_previous_sentence(self):
        assert self._type("I closed my old account. 9876543210 is my new mobile") == "PHONE_NUMBER"

    def test_ifsc_in_next_sentence(self):
        assert self._type("Call me on 9876543210. IFSC SBIN0001234 is for transfers") == "PHONE_NUMBER"

    def test_abbreviated_account_cue(self):
        assert self._type("Acct. No. 9876543210") == "IN_BANK_ACCOUNT"


# ---------------------------------------------------------------------------
# ADDRESS merging across lines
# ---------------------------------------------------------------------------


def _address_results(
    text: str, locations: list[str], pin: str | None = None
) -> list[RecognizerResult]:
    results = [_result(text, loc, "LOCATION") for loc in locations]
    if pin:
        results.append(_result(text, pin, "IN_PIN_CODE", "InPinCodeRecognizer", 0.6))
    return results


class TestAddressMergeAcrossLines:
    def test_organization_on_next_line_is_not_an_address(self):
        text = (
            "Address: Flat 301, Kumar Pinnacle, Baner, Pune 411045\n"
            "Employer: Infosys Technologies"
        )
        results = _address_results(text, ["Kumar Pinnacle", "Baner", "Pune"], "411045")
        results.append(_result(text, "Infosys Technologies", "ORGANIZATION"))
        assert _spans(text, merge_address_entities(results, text)) == [
            ("301, Kumar Pinnacle, Baner, Pune 411045", "ADDRESS"),
            ("Infosys Technologies", "ORGANIZATION"),
        ]

    def test_indicator_in_previous_sentence_does_not_promote(self):
        text = "My address changed. Infosys Technologies is my employer."
        results = [_result(text, "Infosys Technologies", "ORGANIZATION")]
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Infosys Technologies", "ORGANIZATION"),
        ]

    def test_organization_after_unit_number_is_promoted(self):
        text = "Address as per records\nF3003, Shivam Residency Co-op Society"
        results = [_result(text, "Shivam Residency Co-op Society", "ORGANIZATION")]
        assert _spans(text, merge_address_entities(results, text)) == [
            ("F3003, Shivam Residency Co-op Society", "ADDRESS"),
        ]

    def test_list_of_cities_stays_separate(self):
        text = "Cities covered: Pune,\nMumbai,\nDelhi"
        results = _address_results(text, ["Pune", "Mumbai", "Delhi"])
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Delhi", "LOCATION"), ("Mumbai", "LOCATION"), ("Pune", "LOCATION"),
        ]

    def test_locations_on_consecutive_lines_stay_separate(self):
        text = "I visited Pune\nMumbai is next"
        results = _address_results(text, ["Pune", "Mumbai"])
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Mumbai", "LOCATION"), ("Pune", "LOCATION"),
        ]

    def test_wrapped_address_is_one_address(self):
        text = "Address: Flat 301, Kumar Pinnacle,\nBaner, Pune 411045"
        results = _address_results(text, ["Kumar Pinnacle", "Baner", "Pune"], "411045")
        assert _spans(text, merge_address_entities(results, text)) == [
            ("301, Kumar Pinnacle,\nBaner, Pune 411045", "ADDRESS"),
        ]

    def test_wrapped_address_masks_missed_locality_on_next_line(self):
        # NER missed "Aundh"; the address must still cover it.
        text = "Address: Flat 301, Kumar Pinnacle,\nBaner, Aundh, Pune 411045"
        results = _address_results(text, ["Kumar Pinnacle", "Baner", "Pune"], "411045")
        assert _spans(text, merge_address_entities(results, text)) == [
            ("301, Kumar Pinnacle,\nBaner, Aundh, Pune 411045", "ADDRESS"),
        ]

    def test_address_under_label_one_part_per_line(self):
        text = "Address:\nKumar Pinnacle\nBaner\nPune 411045"
        results = _address_results(text, ["Kumar Pinnacle", "Baner", "Pune"], "411045")
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Kumar Pinnacle\nBaner\nPune 411045", "ADDRESS"),
        ]

    def test_address_ends_at_its_pin_code(self):
        text = "Address: Flat 301, Kumar Pinnacle, Pune 411045\nMumbai office will call you"
        results = _address_results(text, ["Kumar Pinnacle", "Pune", "Mumbai"], "411045")
        assert _spans(text, merge_address_entities(results, text)) == [
            ("301, Kumar Pinnacle, Pune 411045", "ADDRESS"), ("Mumbai", "LOCATION"),
        ]

    def test_unit_number_at_start_of_line_is_absorbed(self):
        text = "Address:\nF3003, Kumar Pinnacle, Baner, Pune 411045"
        results = _address_results(text, ["Kumar Pinnacle", "Baner", "Pune"], "411045")
        assert _spans(text, merge_address_entities(results, text)) == [
            ("F3003, Kumar Pinnacle, Baner, Pune 411045", "ADDRESS"),
        ]

    # Keywords from another line or sentence may still widen an address.

    def test_building_under_heading_joins_its_address(self):
        text = "Correspondence Address\nshivam residency, survey 45, kharadi, pune 411014"
        results = [_result(text, "shivam", "PERSON")]
        results.append(_result(text, "411014", "IN_PIN_CODE", "InPinCodeRecognizer", 0.6))
        assert _spans(text, merge_address_entities(results, text)) == [
            ("shivam residency, survey 45, kharadi, pune 411014", "ADDRESS"),
        ]

    def test_building_after_address_sentence_joins_its_address(self):
        text = "My address has changed. shivam residency, survey 45, kharadi, pune 411014."
        results = [_result(text, "shivam", "PERSON")]
        results.append(_result(text, "411014", "IN_PIN_CODE", "InPinCodeRecognizer", 0.6))
        assert _spans(text, merge_address_entities(results, text)) == [
            ("shivam residency, survey 45, kharadi, pune 411014", "ADDRESS"),
        ]

    def test_community_name_under_heading_joins_its_address(self):
        text = "Address\nMaratha Colony, Pune 411001"
        results = _address_results(text, ["Pune"], "411001")
        results.append(_result(text, "Maratha", "NRP"))
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Maratha Colony, Pune 411001", "ADDRESS"),
        ]

    def test_lone_person_near_previous_line_keyword_stays_person(self):
        text = (
            "Customer RAJESH KUMAR SHARMA residing at Mumbai.\n"
            "My name is Mr. R.K. Sharma."
        )
        results = _address_results(text, ["Mumbai"]) + [_result(text, "R.K. Sharma", "PERSON")]
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Mumbai", "LOCATION"), ("R.K. Sharma", "PERSON"),
        ]

    def test_name_on_new_line_is_not_pulled_into_address(self):
        text = "Customer residing at Mumbai\nMy name is Rahul"
        results = _address_results(text, ["Mumbai"]) + [_result(text, "Rahul", "PERSON")]
        assert _spans(text, merge_address_entities(results, text)) == [
            ("Mumbai", "LOCATION"), ("Rahul", "PERSON"),
        ]


# ---------------------------------------------------------------------------
# NER spans are split at line breaks
# ---------------------------------------------------------------------------


class TestSplitAtLineBreaks:
    def test_allow_listed_fragment_is_dropped(self):
        text = "Student: Ananya Rao\nAadhaar: 234567890123"
        results = [_result(text, "Ananya Rao\nAadhaar", "PERSON")]
        out = split_at_line_breaks(results, text, ["Aadhaar"])
        assert _spans(text, out) == [("Ananya Rao", "PERSON")]

    def test_every_other_fragment_stays_masked(self):
        text = "I visited Pune\nMumbai is next"
        out = split_at_line_breaks([_result(text, "Pune\nMumbai", "LOCATION")], text)
        assert _spans(text, out) == [("Mumbai", "LOCATION"), ("Pune", "LOCATION")]

    def test_surname_that_is_also_a_title_stays_masked(self):
        text = "Beneficiary name: Sunita\nKumari"
        out = split_at_line_breaks([_result(text, "Sunita\nKumari", "PERSON")], text)
        assert _spans(text, out) == [("Kumari", "PERSON"), ("Sunita", "PERSON")]

    def test_fragments_keep_score_and_recognizer(self):
        text = "I visited Pune\nMumbai is next"
        out = split_at_line_breaks([_result(text, "Pune\nMumbai", "LOCATION", score=0.77)], text)
        assert {r.score for r in out} == {0.77}
        assert {r.recognition_metadata["recognizer_name"] for r in out} == {"TransformersRecognizer"}

    def test_fragment_without_letters_or_digits_is_dropped(self):
        text = "Signed Ananya Rao\n--\nBranch Manager"
        out = split_at_line_breaks([_result(text, "Ananya Rao\n--", "PERSON")], text)
        assert _spans(text, out) == [("Ananya Rao", "PERSON")]

    def test_windows_line_ending(self):
        text = "I visited Pune\r\nMumbai is next"
        out = split_at_line_breaks([_result(text, "Pune\r\nMumbai", "LOCATION")], text)
        assert _spans(text, out) == [("Mumbai", "LOCATION"), ("Pune", "LOCATION")]

    def test_pattern_entities_are_left_alone(self):
        text = "Qty 12\nMay 2025"
        date = _result(text, "12\nMay 2025", "DATE_TIME", "DateRecognizer")
        assert split_at_line_breaks([date], text) == [date]

    def test_single_line_span_is_unchanged(self):
        text = "Customer Priya Menon called"
        person = _result(text, "Priya Menon", "PERSON")
        assert split_at_line_breaks([person], text) == [person]


# ---------------------------------------------------------------------------
# Titles never reach across a line break
# ---------------------------------------------------------------------------


class TestTitlesAcrossLines:
    def _titles_then_split(self, text: str, value: str, entity_type: str = "PERSON"):
        results = normalize_person_titles([_result(text, value, entity_type)], text)
        return _spans(text, split_at_line_breaks(results, text))

    @pytest.mark.parametrize(
        ("text", "value", "expected"),
        [
            ("Surname: Kumari\nPriya Sharma", "Kumari\nPriya Sharma",
             [("Kumari", "PERSON"), ("Priya Sharma", "PERSON")]),
            ("Father: Pandit\nRavi Shankar", "Pandit\nRavi Shankar",
             [("Pandit", "PERSON"), ("Ravi Shankar", "PERSON")]),
            ("Father: Pandit\r\nRavi Shankar", "Pandit\r\nRavi Shankar",
             [("Pandit", "PERSON"), ("Ravi Shankar", "PERSON")]),
        ],
    )
    def test_title_like_surname_ending_a_line_stays_masked(self, text, value, expected):
        assert self._titles_then_split(text, value) == expected

    def test_title_alone_on_line_above_makes_organization_a_person(self):
        text = "Regards,\nCA\nAbhay Sharma"
        assert self._titles_then_split(text, "CA\nAbhay Sharma", "ORGANIZATION") == [
            ("Abhay Sharma", "PERSON"), ("CA", "PERSON"),
        ]

    def test_title_on_same_line_is_still_trimmed(self):
        text = "Mr. Rahul\nSharma"
        assert self._titles_then_split(text, "Mr. Rahul\nSharma") == [
            ("Rahul", "PERSON"), ("Sharma", "PERSON"),
        ]

    def test_title_before_surname_on_line_above_is_trimmed_only_there(self):
        text = "Smt. Kumari\nPriya"
        assert self._titles_then_split(text, "Smt. Kumari\nPriya") == [
            ("Kumari", "PERSON"), ("Priya", "PERSON"),
        ]


# ---------------------------------------------------------------------------
# Digit patterns
# ---------------------------------------------------------------------------


class TestDigitPatternsAcrossLines:
    def test_aadhaar_before_numbered_list(self):
        text = "Aadhaar: 2345 6789 0123\n1. Submit the form"
        assert _matches(InAadhaarImprovedRecognizer(), "IN_AADHAAR", text) == ["2345 6789 0123"]

    def test_hard_wrapped_aadhaar_still_detected(self):
        text = "Aadhaar: 2345 6789\n0123"
        assert _matches(InAadhaarImprovedRecognizer(), "IN_AADHAAR", text) == ["2345 6789\n0123"]

    def test_aadhaar_inside_longer_digit_group_still_rejected(self):
        text = "Card 4111 1111 1111 1111"
        assert _matches(InAadhaarImprovedRecognizer(), "IN_AADHAAR", text) == []

    def test_hard_wrapped_pin_code_still_detected(self):
        assert _matches(InPinCodeRecognizer(), "IN_PIN_CODE", "Pune 560\n038") == ["560\n038"]


# ---------------------------------------------------------------------------
# Context-only recognizers flag a keyword that is not on the number's line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recognizer", "entity", "number", "keyword"),
    [
        (InApaarRecognizer(), "IN_APAAR", "123456789012", "APAAR"),
        (InPranRecognizer(), "IN_PRAN", "123456789012", "PRAN"),
        (CustomerIdRecognizer(), "CUSTOMER_ID", "123456789", "Customer ID"),
    ],
)
class TestContextOffLineFlag:
    def _result_for(self, recognizer, entity, text, number) -> RecognizerResult:
        (result,) = [
            r for r in recognizer.analyze(text, entities=[entity])
            if text[r.start : r.end] == number
        ]
        return result

    @pytest.mark.parametrize(
        "layout", ["{keyword}: {number}", "{keyword}:\n{number}", "{keyword}\n{number}"]
    )
    def test_keyword_on_or_introducing_the_line(self, recognizer, entity, number, keyword, layout):
        text = layout.format(keyword=keyword, number=number)
        result = self._result_for(recognizer, entity, text, number)
        assert result.score == 0.95
        assert not result.recognition_metadata.get(CONTEXT_OFF_LINE_KEY)

    def test_keyword_elsewhere_keeps_score_but_is_flagged(self, recognizer, entity, number, keyword):
        text = f"{keyword} details below\nName: Ananya Rao\nID: {number}"
        result = self._result_for(recognizer, entity, text, number)
        assert result.score == 0.95
        assert result.recognition_metadata[CONTEXT_OFF_LINE_KEY]

    def test_no_keyword_stays_below_threshold(self, recognizer, entity, number, keyword):
        result = self._result_for(recognizer, entity, f"Reference: {number}", number)
        assert result.score < 0.35
        assert not result.recognition_metadata.get(CONTEXT_OFF_LINE_KEY)


class TestPreferLineContext:
    text = "Student: Ananya Rao\nAadhaar: 234567890123"
    patterns = {
        "InAadhaarImprovedRecognizer": re.compile(r"(?i)\baadhaar\b"),
        "PhoneRecognizer": re.compile(r"(?i)\bphone\b"),
    }

    def _apaar(self, off_line: bool = True) -> RecognizerResult:
        r = _result(self.text, "234567890123", "IN_APAAR", "InApaarRecognizer", 0.95)
        if off_line:
            r.recognition_metadata[CONTEXT_OFF_LINE_KEY] = True
        return r

    def test_off_line_id_yields_to_line_local_keyword(self):
        aadhaar = _result(self.text, "234567890123", "IN_AADHAAR", "InAadhaarImprovedRecognizer", 0.75)
        out = prefer_line_context([self._apaar(), aadhaar], self.text, self.patterns)
        assert _spans(self.text, out) == [("234567890123", "IN_AADHAAR")]

    def test_off_line_id_kept_against_competitor_without_keyword(self):
        phone = _result(self.text, "234567890123", "PHONE_NUMBER", "PhoneRecognizer", 0.4)
        out = prefer_line_context([self._apaar(), phone], self.text, self.patterns)
        assert len(out) == 2

    def test_line_local_id_is_never_dropped(self):
        aadhaar = _result(self.text, "234567890123", "IN_AADHAAR", "InAadhaarImprovedRecognizer", 0.75)
        out = prefer_line_context([self._apaar(off_line=False), aadhaar], self.text, self.patterns)
        assert len(out) == 2

    def test_off_line_id_alone_is_kept(self):
        out = prefer_line_context([self._apaar()], self.text, self.patterns)
        assert _spans(self.text, out) == [("234567890123", "IN_APAAR")]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def _anonymize(client, text: str) -> tuple[str, dict[str, str]]:
    resp = client.post("/anonymize_unique", json={"text": text})
    assert resp.status_code == 200
    data = resp.json()
    return data["anonymized_text"], {v: k for k, v in data["entity_mapping"].items()}


class TestMultiLineEndToEnd:
    def test_reported_example(self, client):
        text = (
            "Customer RAJESH KUMAR SHARMA residing at Mumbai.\n"
            "My name is Mr. R.K. Sharma."
        )
        anonymized, reverse = _anonymize(client, text)
        assert "PERSON" in reverse["R.K. Sharma"]
        assert "sharma" not in anonymized.lower()

    def test_ifsc_on_next_line_keeps_the_phone(self, client):
        _, reverse = _anonymize(client, "Mobile: 9876543210\nIFSC: SBIN0001234")
        assert "PHONE_NUMBER" in reverse["9876543210"]

    def test_aadhaar_before_numbered_list_is_masked(self, client):
        anonymized, reverse = _anonymize(client, "Aadhaar: 2345 6789 0123\n1. Submit the form")
        assert "IN_AADHAAR" in reverse["2345 6789 0123"]
        assert "2345" not in anonymized

    def test_line_breaks_survive_anonymization(self, client):
        anonymized, reverse = _anonymize(client, "Student: Ananya Rao\nAadhaar: 234567890123")
        assert "\nAadhaar: " in anonymized
        assert "IN_AADHAAR" in reverse["234567890123"]
        assert "Ananya" not in anonymized

    @pytest.mark.parametrize(
        ("text", "surname"),
        [
            ("Beneficiary name: Sunita\nKumari", "Kumari"),
            ("Account holder Suresh\nPandit visited the branch today.", "Pandit"),
            ("Surname: Kumari\nPriya Sharma called yesterday", "Kumari"),
            ("Father: Pandit\nRavi Shankar", "Pandit"),
        ],
    )
    def test_surname_that_is_also_a_title_is_masked(self, client, text, surname):
        anonymized, _ = _anonymize(client, text)
        assert surname not in anonymized

    @pytest.mark.parametrize(
        "text",
        [
            "Correspondence Address\nshivam residency, survey 45, kharadi, pune 411014",
            "My address has changed. shivam residency, survey 45, kharadi, pune 411014.",
        ],
    )
    def test_address_after_heading_or_sentence_is_masked(self, client, text):
        anonymized, _ = _anonymize(client, text)
        for part in ("residency", "survey 45", "kharadi"):
            assert part not in anonymized


class TestUserScoreThreshold:
    """A keyword on another line must not lower the score below a strict threshold."""

    @pytest.mark.parametrize(
        ("text", "entity"),
        [
            ("Customer ID\n123456789", "CUSTOMER_ID"),
            ("PRAN\n110012345678", "IN_PRAN"),
            ("APAAR details\nName: Ananya Rao\nID: 123456789012", "IN_APAAR"),
        ],
    )
    def test_detected_at_threshold_above_one_half(self, text, entity):
        from app.main import engine

        found = {e.entity_type for e in engine.detect(text, score_threshold=0.6)}
        assert entity in found
