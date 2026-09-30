"""Tests for ALL-CAPS recovery — case normalisation and result merging.

Cased NER models mislabel, truncate, or entirely miss all-caps names.  These
cover the normalisation that restores the casing signal, and the merge that
folds the recovered spans back in without disturbing validated recognizers.
"""

from presidio_analyzer import RecognizerResult

from pii_shield.pipeline import (
    _recovery_allowed,
    merge_recovered_results,
    normalize_case,
)


def _result(entity_type: str, text: str, value: str, score: float = 0.85) -> RecognizerResult:
    start = text.index(value)
    r = RecognizerResult(
        entity_type=entity_type, start=start, end=start + len(value), score=score
    )
    r.recognition_metadata = {"recognizer_name": "SpacyRecognizer"}
    return r


# ---------------------------------------------------------------------------
# normalize_case
# ---------------------------------------------------------------------------


def test_returns_none_when_nothing_to_normalize():
    # Normally-cased text must skip the second NER pass entirely.
    assert normalize_case("Rajesh Kumar Sharma has some concerns") is None


def test_title_cases_all_caps_name():
    out = normalize_case("RAJESH KUMAR SHARMA has some concerns")
    assert out == "Rajesh Kumar Sharma has some concerns"


def test_preserves_length_exactly():
    # The offsets of entities found in the normalised copy are applied to the
    # original text, so any length change would corrupt every span after it.
    for text in (
        "RAJESH KUMAR SHARMA residing at F3003, MUMBAI 400097",
        "APPLICANT PRIYA VENKATESAN SIGNED ON 12/03/2025",
        "Contact PRIYA at priya@contoso.com",
        "MIXED Case TEXT with ABCPS1234K and SBIN0001234",
    ):
        out = normalize_case(text)
        assert out is not None
        assert len(out) == len(text)


def test_leaves_pan_untouched():
    # InPanImprovedRecognizer matches case-sensitively; lowering it would drop
    # the high-confidence validated pattern.
    out = normalize_case("HER PAN IS ABCPS1234K")
    assert "ABCPS1234K" in out


def test_leaves_ifsc_and_digit_adjacent_tokens_untouched():
    out = normalize_case("TRANSFER VIA IFSC SBIN0001234 TO MH12AB1234")
    assert "SBIN0001234" in out
    assert "MH12AB1234" in out
    assert "IFSC" in out  # acronym, not "Ifsc"


def test_skips_known_acronyms():
    out = normalize_case("DOB AND KYC AND UPI DETAILS")
    assert "DOB" in out and "KYC" in out and "UPI" in out
    assert "Dob" not in out


def test_honours_extra_skip():
    out = normalize_case("CONTOSO ACME LIMITED", extra_skip=frozenset({"CONTOSO"}))
    assert out is not None
    assert "CONTOSO" in out
    assert "Acme" in out


def test_ignores_single_character_tokens():
    # "F" in a flat number must not be treated as a word to normalise.
    out = normalize_case("FLAT F3003 KANAKIA ZEN")
    assert "F3003" in out


def test_handles_hyphenated_names():
    out = normalize_case("ANNE-MARIE DSOUZA called")
    assert out == "Anne-Marie Dsouza called"


def test_leaves_mixed_case_alone():
    # MixedCase tokens already carry a meaningful casing signal and must be
    # preserved even while badly-cased neighbours are rewritten.
    out = normalize_case("RAJESH met McDonald and iPhone")
    assert out is not None
    assert "McDonald" in out and "iPhone" in out
    assert "Rajesh" in out


def test_normalizes_only_the_badly_cased_portion():
    text = "The applicant RAJESH SHARMA signed today"
    assert normalize_case(text) == "The applicant Rajesh Sharma signed today"


# ---------------------------------------------------------------------------
# merge_recovered_results
# ---------------------------------------------------------------------------


def test_recovered_person_replaces_mislabelled_organization():
    text = "RAJESH KUMAR SHARMA has some concerns"
    primary = [_result("ORGANIZATION", text, "RAJESH KUMAR SHARMA")]
    recovered = [_result("PERSON", text, "RAJESH KUMAR SHARMA")]
    out = merge_recovered_results(primary, recovered, text)
    assert [r.entity_type for r in out] == ["PERSON"]


def test_recovered_span_replaces_truncated_primary_span():
    text = "CHEQUE ISSUED BY SANJAY GUPTA bounced"
    primary = [_result("ORGANIZATION", text, "GUPTA")]
    recovered = [_result("PERSON", text, "SANJAY GUPTA")]
    out = merge_recovered_results(primary, recovered, text)
    assert len(out) == 1
    assert text[out[0].start : out[0].end] == "SANJAY GUPTA"


def test_validated_entity_always_wins_over_recovered():
    # A recovered PERSON overlapping a validated PAN must be discarded — the
    # regex ran against the untouched original and is authoritative.
    text = "HOLDER ABCPS1234K ON FILE"
    primary = [_result("IN_PAN", text, "ABCPS1234K")]
    recovered = [_result("PERSON", text, "ABCPS1234K")]
    out = merge_recovered_results(primary, recovered, text)
    assert [r.entity_type for r in out] == ["IN_PAN"]


def test_keeps_non_overlapping_primary_ner():
    text = "RAJESH SHARMA emailed Priya Nair"
    primary = [_result("PERSON", text, "Priya Nair")]
    recovered = [_result("PERSON", text, "RAJESH SHARMA")]
    out = merge_recovered_results(primary, recovered, text)
    assert len(out) == 2


def test_keeps_non_ner_entities_that_do_not_overlap():
    text = "PRIYA at priya@contoso.com"
    primary = [_result("EMAIL_ADDRESS", text, "priya@contoso.com")]
    recovered = [_result("PERSON", text, "PRIYA")]
    out = merge_recovered_results(primary, recovered, text)
    assert {r.entity_type for r in out} == {"EMAIL_ADDRESS", "PERSON"}


def test_empty_recovery_returns_primary_unchanged():
    text = "RAJESH KUMAR SHARMA"
    primary = [_result("ORGANIZATION", text, "RAJESH KUMAR SHARMA")]
    assert merge_recovered_results(primary, [], text) is primary


def test_recovery_fully_suppressed_by_validated_entity_returns_primary():
    text = "ABCPS1234K"
    primary = [_result("IN_PAN", text, "ABCPS1234K")]
    recovered = [_result("PERSON", text, "ABCPS1234K")]
    assert merge_recovered_results(primary, recovered, text) is primary


# ---------------------------------------------------------------------------
# true-casing behaviour
# ---------------------------------------------------------------------------


def test_lowercases_common_words_instead_of_title_casing():
    # Title-casing every word ("Signed The Form At Mumbai Branch") is itself
    # out-of-distribution and hides name boundaries from NER.
    out = normalize_case(
        "APPLICANT PRIYA VENKATESAN SIGNED THE FORM AT MUMBAI BRANCH"
    )
    assert out == "applicant Priya Venkatesan signed the form at Mumbai branch"


def test_true_casing_preserves_length():
    text = "CHEQUE ISSUED BY SANJAY GUPTA BOUNCED DUE TO INSUFFICIENT FUNDS"
    out = normalize_case(text)
    assert out == "cheque issued by Sanjay Gupta bounced due to insufficient funds"
    assert len(out) == len(text)


def test_all_caps_person_span_is_not_extended():
    # merge_adjacent_person_tokens treats a capitalised neighbour as a name
    # part; in ALL-CAPS text that would absorb ordinary words.
    from pii_shield.pipeline import merge_adjacent_person_tokens

    text = "CHEQUE ISSUED BY SANJAY GUPTA BOUNCED"
    out = merge_adjacent_person_tokens([_result("PERSON", text, "SANJAY GUPTA")], text)
    assert text[out[0].start : out[0].end] == "SANJAY GUPTA"


def test_mixed_case_person_span_is_still_extended():
    # The original motivating case must keep working.
    from pii_shield.pipeline import merge_adjacent_person_tokens

    text = "Contact VIkas Gautam today"
    out = merge_adjacent_person_tokens([_result("PERSON", text, "Gautam")], text)
    assert text[out[0].start : out[0].end] == "VIkas Gautam"


# ---------------------------------------------------------------------------
# lowercase recovery
# ---------------------------------------------------------------------------


def test_title_cases_all_lowercase_name():
    out = normalize_case("cheque issued by sanjay gupta bounced")
    assert out == "cheque issued by Sanjay Gupta bounced"


def test_lowercase_normalization_preserves_length():
    for text in (
        "rajesh kumar sharma residing at f3003, kanakia zen world",
        "my name is anil deshmukh and my pan is abcps1234k",
        "please contact priya venkatesan at priya@contoso.com",
    ):
        out = normalize_case(text)
        assert out is not None
        assert len(out) == len(text)


def test_lowercase_leaves_identifiers_untouched():
    # Digit-adjacent tokens are skipped, so a lowercase PAN is not re-cased
    # into something the case-sensitive recognizer would treat differently.
    out = normalize_case("anil deshmukh pan abcps1234k ifsc sbin0001234")
    assert out is not None
    assert "abcps1234k" in out
    assert "sbin0001234" in out
    assert "Anil Deshmukh" in out


def test_lowercase_recovery_accepts_person():
    text = "cheque issued by sanjay gupta bounced"
    out = merge_recovered_results([], [_result("PERSON", text, "sanjay gupta")], text)
    assert [r.entity_type for r in out] == ["PERSON"]


def test_lowercase_recovery_rejects_organization_and_nrp():
    # Measured: every spurious lowercase recovery was ORGANIZATION or NRP.
    text = "please reset my internet banking password immediately"
    recovered = [
        _result("NRP", text, "internet banking password"),
        _result("ORGANIZATION", text, "banking"),
    ]
    assert merge_recovered_results([], recovered, text) == []


def test_uppercase_recovery_still_accepts_all_ner_types():
    # ALL-CAPS keeps word-boundary cues, so it stays eligible for every type.
    text = "MEETING AT KANAKIA ZEN WORLD"
    out = merge_recovered_results([], [_result("ORGANIZATION", text, "KANAKIA ZEN WORLD")], text)
    assert [r.entity_type for r in out] == ["ORGANIZATION"]


# ---------------------------------------------------------------------------
# segment-aware lowercase rewriting
# ---------------------------------------------------------------------------


def test_cased_prose_is_left_alone():
    # Regression: title-casing "visited" here fused the whole phrase into one
    # bogus PERSON span and swallowed an allow-listed organisation.
    assert normalize_case("Kavitha visited Contoso Bank in Chennai.") is None


def test_lowercase_name_rewritten_even_beside_cased_words():
    # The reported case: a lowercase name sitting next to a Title-Case address.
    out = normalize_case("rajesh kumar sharma residing at F3003, Kanakia Zen world")
    assert out is not None
    assert "Rajesh Kumar Sharma" in out


def test_recovery_rejects_mixed_case_span():
    # "Kavitha visited Contoso Bank" must not fuse into one PERSON.
    assert not _recovery_allowed("Kavitha visited Contoso Bank", "PERSON")


def test_recovery_accepts_all_lowercase_person():
    assert _recovery_allowed("rajesh kumar sharma", "PERSON")


def test_recovery_rejects_all_lowercase_non_person():
    assert not _recovery_allowed("internet banking password", "NRP")


def test_isolated_lowercase_word_does_not_trigger_pass():
    # One non-common lowercase word is not an uncased name, so no second pass.
    assert normalize_case("the transaction failed because of insufficient balance") is None


def test_all_caps_still_rewritten_inside_cased_segment():
    out = normalize_case("The applicant RAJESH SHARMA signed today")
    assert out == "The applicant Rajesh Sharma signed today"


def test_mixed_casing_across_lines_preserves_length():
    text = (
        "RAJESH KUMAR SHARMA residing at F3003\n"
        "rajesh kumar sharma residing at F3003\n"
        "Rajesh Kumar Sharma residing at F3003"
    )
    out = normalize_case(text)
    assert out is not None and len(out) == len(text)
