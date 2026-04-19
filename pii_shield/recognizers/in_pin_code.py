"""Custom Presidio recognizer for Indian PIN (postal) codes.

Format: 6 digits, first digit 1–8.
  - First digit encodes the postal region (1–8; 0 and 9 are not used)
  - Commonly written as a bare number (e.g. ``560038``) or with a
    space after the third digit (e.g. ``560 038``)

Context keywords such as *pin*, *postal*, *zip*, *pincode*, and
*post office* boost detection confidence.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# Standard 6-digit PIN code: 560038
_PATTERN_STANDARD = Pattern(
    name="in_pin_code_standard",
    regex=r"\b[1-8]\d{5}\b",
    score=0.6,
)

# Space after third digit: 560 038
_PATTERN_SPACE = Pattern(
    name="in_pin_code_space",
    regex=r"\b[1-8]\d{2}\s\d{3}\b",
    score=0.65,
)


class InPinCodeRecognizer(PatternRecognizer):
    """Detects Indian PIN (postal) codes in text."""

    ENTITIES = ["IN_PIN_CODE"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_PIN_CODE",
            supported_language="en",
            patterns=[_PATTERN_STANDARD, _PATTERN_SPACE],
        )
