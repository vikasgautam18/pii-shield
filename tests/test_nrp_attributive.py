"""Tests for attributive NRP suppression.

NRP (nationality / religious / political group) is personal data only when it
describes a person.  The NER model emits it for any demonym, so aggregate
business language — "South Indian branches", "Indian banking sector" — was
redacted even though it identifies nobody.  Used predicatively ("the customer
is Indian") or before a singular person noun ("a Muslim woman") it describes an
individual and must stay.
"""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import filter_attributive_nrp


def _nrp(text: str, value: str) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type="NRP", start=start, end=start + len(value), score=0.85
    )
    r.recognition_metadata = {"recognizer_name": "TransformersRecognizer"}
    return r


def _kept(text: str, value: str) -> bool:
    return bool(filter_attributive_nrp([_nrp(text, value)], text))


# ---------------------------------------------------------------------------
# suppressed — the demonym modifies a thing or a market segment
# ---------------------------------------------------------------------------


def test_branches_not_a_person():
    text = "Our South Indian branches have shown 20% growth this quarter."
    assert not _kept(text, "South Indian")


def test_region_not_a_person():
    text = "The North Indian region has the highest loan disbursement."
    assert not _kept(text, "North Indian")


def test_sector_not_a_person():
    text = "Indian banking sector is growing rapidly."
    assert not _kept(text, "Indian")


def test_plural_customers_are_a_segment():
    text = "South Indian customers prefer mobile banking."
    assert not _kept(text, "South Indian")


def test_market_not_a_person():
    text = "The South Indian market has high potential for home loans."
    assert not _kept(text, "South Indian")


def test_areas_not_a_person():
    text = "South Indian rural areas need more ATM coverage."
    assert not _kept(text, "South Indian")


def test_sme_customers_are_a_segment():
    text = "The bank is targeting South Indian SME customers."
    assert not _kept(text, "South Indian")


def test_loans_not_a_person():
    text = "South Indian agriculture loans are performing well."
    assert not _kept(text, "South Indian")


def test_portfolio_not_a_person():
    text = "The South Indian credit card portfolio is expanding."
    assert not _kept(text, "South Indian")


def test_nri_customers_are_a_segment():
    text = "South Indian NRI customers are our focus segment."
    assert not _kept(text, "South Indian")


def test_adoption_not_a_person():
    text = "The South Indian digital banking adoption is high."
    assert not _kept(text, "South Indian")


def test_services_not_a_person():
    text = "South Indian wealth management services are in demand."
    assert not _kept(text, "South Indian")


# ---------------------------------------------------------------------------
# kept — the demonym describes an individual
# ---------------------------------------------------------------------------


def test_predicate_position_kept():
    text = "The customer is Indian and lives in Mumbai."
    assert _kept(text, "Indian")


def test_end_of_sentence_kept():
    text = "He is a South Indian Hindu."
    assert _kept(text, "South Indian Hindu")


def test_followed_by_preposition_kept():
    text = "Rajesh is a Tamil Brahmin from Chennai."
    assert _kept(text, "Tamil Brahmin")


def test_singular_person_noun_kept():
    text = "The applicant is a Muslim woman aged 34."
    assert _kept(text, "Muslim")


def test_attributive_person_noun_without_copula_kept():
    # No "is" here — the person noun alone must be enough.
    text = "The Muslim woman filed a complaint."
    assert _kept(text, "Muslim")


def test_gentleman_kept():
    text = "The account holder is a Sikh gentleman."
    assert _kept(text, "Sikh")


def test_label_value_kept():
    text = "Nationality: Indian"
    assert _kept(text, "Indian")


def test_singular_customer_kept():
    # Singular denotes one human; the plural form is a segment.
    text = "A South Indian customer raised the issue."
    assert _kept(text, "South Indian")


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def test_other_entity_types_untouched():
    text = "South Indian branches have grown"
    r = RecognizerResult(entity_type="LOCATION", start=0, end=12, score=0.9)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    assert filter_attributive_nrp([r], text) == [r]


def test_person_never_suppressed():
    text = "Indian branches have grown"
    r = RecognizerResult(entity_type="PERSON", start=0, end=6, score=0.9)
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    assert filter_attributive_nrp([r], text) == [r]


def test_religious_role_noun_kept():
    # Regression: "devotee" is a person, so the NRP qualifying it is PII.
    text = "Indian national and Buddhist devotee Rahul Verma opened an account."
    assert _kept(text, "Buddhist")


def test_national_kept():
    text = "NRE account opening for Indian national Amit Sharma."
    assert _kept(text, "Indian")


def test_family_role_noun_kept():
    text = "The Hindu widow claimed the deposit."
    assert _kept(text, "Hindu")


# ---------------------------------------------------------------------------
# predicate position beats any following occupation noun
# ---------------------------------------------------------------------------


def test_copula_keeps_nrp_before_occupation():
    # Regression: occupations are unbounded, so a person-noun list alone
    # silently dropped these — special-category data leaking in clear.
    for text, value in (
        ("The account holder is a Tamil speaker from Coimbatore.", "Tamil"),
        ("The borrower is a Punjabi farmer seeking a crop loan.", "Punjabi"),
        ("He is a Gujarati businessman with three accounts.", "Gujarati"),
        ("He is a Rajput landowner from Rajasthan.", "Rajput"),
        ("She is a Malayali nurse working in Dubai.", "Malayali"),
    ):
        assert _kept(text, value), text


def test_copula_variants():
    for text, value in (
        ("The applicants are Bengali weavers.", "Bengali"),
        ("The nominee was a Sikh soldier.", "Sikh"),
        ("The customer remains an Indian taxpayer.", "Indian"),
    ):
        assert _kept(text, value), text


def test_copula_does_not_rescue_business_language():
    # "are" here belongs to the predicate after the noun phrase, not before
    # the demonym, so these stay suppressed.
    text = "South Indian agriculture loans are performing well."
    assert not _kept(text, "South Indian")


def test_targeting_verb_is_not_a_copula():
    text = "The bank is targeting South Indian SME customers."
    assert not _kept(text, "South Indian")
