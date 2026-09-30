"""Tests for PHONE_NUMBER → IN_BANK_ACCOUNT disambiguation.

A bare run of 9-18 digits is a valid Indian bank account number, and the phone
recognizer claims the same digits whenever they also form a valid phone
number: a mobile ("9876543210") or a landline without its leading 0
("5498721032").  Scores cannot separate them — the phone pattern scores 0.60
against the account pattern's 0.10, and "number" sits in the phone recognizer's
context list, so "bank account number" boosts the phone score to 1.00.  Only
the nearest surrounding cue carries the answer.
"""

import pytest
from fastapi.testclient import TestClient
from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import reclassify_phone_as_bank_account


def _phone(text: str, value: str, score: float = 1.0) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type="PHONE_NUMBER", start=start, end=start + len(value), score=score
    )
    r.recognition_metadata = {"recognizer_name": "InPhoneRecognizer"}
    return r


def _types(text: str, results: list[RecognizerResult]) -> list[str]:
    return [r.entity_type for r in reclassify_phone_as_bank_account(results, text)]


# ---------------------------------------------------------------------------
# reclassified on account cues
# ---------------------------------------------------------------------------


def test_bank_account_number_is_reclassified():
    text = "His bank account number is 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_account_number_without_bank_word():
    text = "His account number is 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_ac_abbreviation_cue():
    text = "A/C no 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_beneficiary_cue():
    text = "Credit the beneficiary 9876543210 today"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_cue_in_longer_sentence():
    text = (
        "Venkatanarasimharajuvaripeta Subramaniam has applied for a loan in SBI. "
        "His bank account number is 9876543210"
    )
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        # A valid landline once its leading 0 is dropped, so the phone
        # recognizer claims it.
        ("My Account number is 5498721032.", "5498721032"),
        ("My account number is 54987210321.", "54987210321"),
        ("Beneficiary account 123456789", "123456789"),
        ("Account number: 501001234567890123", "501001234567890123"),
    ],
)
def test_any_bare_account_length_number_is_reclassified(text, value):
    assert _types(text, [_phone(text, value)]) == ["IN_BANK_ACCOUNT"]


# ---------------------------------------------------------------------------
# left alone when it really is a phone
# ---------------------------------------------------------------------------


def test_mobile_cue_keeps_phone():
    text = "His mobile number is 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


def test_no_cue_keeps_phone():
    text = "Call me on 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


def test_nearest_cue_wins_for_each_number():
    # The account cue precedes the first number, the mobile cue the second.
    text = "His bank account number is 9876543210 and mobile is 9123456780"
    out = reclassify_phone_as_bank_account(
        [_phone(text, "9876543210"), _phone(text, "9123456780")], text
    )
    by_value = {text[r.start : r.end]: r.entity_type for r in out}
    assert by_value["9876543210"] == "IN_BANK_ACCOUNT"
    assert by_value["9123456780"] == "PHONE_NUMBER"


def test_plus91_prefix_never_reclassified():
    # An explicit country code means it is unambiguously a phone number.
    text = "Account holder reachable at +91 98765 43210"
    assert _types(text, [_phone(text, "+91 98765 43210")]) == ["PHONE_NUMBER"]


def test_zero_prefixed_number_never_reclassified():
    text = "My account contact is 09876543210"
    assert _types(text, [_phone(text, "09876543210")]) == ["PHONE_NUMBER"]


def test_separated_number_never_reclassified():
    text = "Account desk on 98765-43210"
    assert _types(text, [_phone(text, "98765-43210")]) == ["PHONE_NUMBER"]


def test_mobile_with_country_code_never_reclassified():
    # "91" without the "+" is still a country code in front of a mobile.
    text = "Registered mobile for the account: 919876543210"
    assert _types(text, [_phone(text, "919876543210")]) == ["PHONE_NUMBER"]


def test_landline_shaped_number_with_phone_cue_stays_phone():
    text = "My phone number is 5498721032."
    assert _types(text, [_phone(text, "5498721032")]) == ["PHONE_NUMBER"]


@pytest.mark.parametrize("cue", ["helpline", "toll-free", "tollfree", "fax"])
def test_helpline_and_fax_cues_keep_phone(cue):
    text = f"Account {cue} 9876543210"
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


@pytest.mark.parametrize("value", ["12345678", "1234567890123456789"])
def test_number_outside_account_length_stays_phone(value):
    # Indian bank account numbers are 9-18 digits.
    text = f"His account number is {value}"
    assert _types(text, [_phone(text, value)]) == ["PHONE_NUMBER"]


def test_distant_account_cue_is_ignored():
    # Beyond the 45-char window the cue is too far to be about this number.
    text = (
        "The account was opened at the branch last year after a long wait, "
        "and you can reach the desk on 9876543210"
    )
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


def test_non_phone_entities_untouched():
    text = "His bank account number is 9876543210"
    r = RecognizerResult(entity_type="IN_BANK_ACCOUNT", start=27, end=37, score=0.55)
    r.recognition_metadata = {"recognizer_name": "InBankAccountRecognizer"}
    assert _types(text, [r]) == ["IN_BANK_ACCOUNT"]


# ---------------------------------------------------------------------------
# transfer markers after the number
# ---------------------------------------------------------------------------


def test_following_ifsc_reclassifies():
    text = "Transfer to 9876543210 IFSC SBIN0001234"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_following_neft_reclassifies():
    text = "NEFT credit to 9876543210 via RTGS today"
    assert _types(text, [_phone(text, "9876543210")]) == ["IN_BANK_ACCOUNT"]


def test_generic_account_word_after_number_does_not_reclassify():
    # "account" after the digits is too weak — people write this about phones.
    text = "Call me on 9876543210 for account queries"
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


def test_distant_ifsc_does_not_reclassify():
    text = (
        "Call me on 9876543210 whenever you get a free moment during the day "
        "and I will share the IFSC"
    )
    assert _types(text, [_phone(text, "9876543210")]) == ["PHONE_NUMBER"]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("My Account number is 5498721032.", "5498721032", "IN_BANK_ACCOUNT"),
        ("My account number is 54987210321.", "54987210321", "IN_BANK_ACCOUNT"),
        ("My account number is 5498721032 and my mobile is 9876543210",
         "5498721032", "IN_BANK_ACCOUNT"),
        ("My account number is 5498721032 and my mobile is 9876543210",
         "9876543210", "PHONE_NUMBER"),
        ("My phone number is 5498721032.", "5498721032", "PHONE_NUMBER"),
    ],
)
def test_label_through_the_api(client, text, value, expected):
    data = client.post("/anonymize_unique", json={"text": text}).json()
    labels = {v: k.strip("{}").rsplit("_", 1)[0] for k, v in data["entity_mapping"].items()}
    assert labels.get(value) == expected
    assert value not in data["anonymized_text"]
