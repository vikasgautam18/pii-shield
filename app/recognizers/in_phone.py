"""Custom Presidio recognizer for Indian phone numbers.

Indian phone number formats:
  - Mobile:   +91 98450 12345, +91-9845012345, 09845012345
  - Landline: 022-24561789, 011-23456789, 080 41234567

The built-in Presidio ``PhoneRecognizer`` relies on ``python-phonenumbers``
which gives a base score of only 0.4 (boosted to 0.75 with context).
SpaCy's NER model often tags hyphenated numbers as DATE_TIME with score
0.85, winning the overlap resolution.  This recognizer uses explicit regex
patterns with higher scores to ensure Indian phone numbers win over
false-positive DATE_TIME detections.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# ── Mobile patterns ──────────────────────────────────────────────────────

# +91 prefix:  +91 9845012345, +91-98450-12345, +919845012345
_PATTERN_MOBILE_PLUS91 = Pattern(
    name="in_phone_mobile_plus91",
    regex=r"\+91[\s\-]?[6-9]\d[\s\-]?\d{4}[\s\-]?\d{4}\b",
    score=0.7,
)

# 0 prefix:  09845012345
_PATTERN_MOBILE_ZERO = Pattern(
    name="in_phone_mobile_zero",
    regex=r"\b0[6-9]\d{9}\b",
    score=0.6,
)

# Bare 10-digit mobile (no prefix):  9443256789
# Needs context words like "mobile", "phone", etc. to score high enough.
_PATTERN_MOBILE_BARE = Pattern(
    name="in_phone_mobile_bare",
    regex=r"\b[6-9]\d{9}\b",
    score=0.6,
)

# ── Landline patterns ────────────────────────────────────────────────────
# Indian STD codes: 0 + 2-digit area code + 8-digit subscriber (metros),
#   0 + 3-digit area code + 7-digit subscriber,
#   0 + 4-digit area code + 6-digit subscriber.

# 2-digit area code (0XX): 011, 022, 033, 044, 079, 080, etc.
_PATTERN_LANDLINE_2DIGIT = Pattern(
    name="in_phone_landline_2digit_std",
    regex=r"\b0[1-9][0-9][\s\-][2-9]\d{7}\b",
    score=0.6,
)

# 3-digit area code (0XXX): 0120, 0141, 0821, etc.
_PATTERN_LANDLINE_3DIGIT = Pattern(
    name="in_phone_landline_3digit_std",
    regex=r"\b0[1-9][0-9]{2}[\s\-][2-9]\d{6}\b",
    score=0.6,
)

# 4-digit area code (0XXXX): 01onal, 02xxx, etc.
_PATTERN_LANDLINE_4DIGIT = Pattern(
    name="in_phone_landline_4digit_std",
    regex=r"\b0[1-9][0-9]{3}[\s\-][2-9]\d{5}\b",
    score=0.6,
)

_CONTEXT_WORDS = [
    "phone",
    "mobile",
    "contact",
    "tel",
    "telephone",
    "cell",
    "landline",
    "call",
    "whatsapp",
    "sms",
    "reach",
    "dial",
    "number",
    "ph",
]


class InPhoneRecognizer(PatternRecognizer):
    """Detects Indian phone numbers (mobile and landline) in text."""

    ENTITIES = ["PHONE_NUMBER"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="PHONE_NUMBER",
            supported_language="en",
            patterns=[
                _PATTERN_MOBILE_PLUS91,
                _PATTERN_MOBILE_BARE,
                _PATTERN_LANDLINE_2DIGIT,
                _PATTERN_LANDLINE_3DIGIT,
                _PATTERN_LANDLINE_4DIGIT,
                _PATTERN_MOBILE_ZERO,
            ],
            context=_CONTEXT_WORDS,
        )
