"""Custom Presidio recognizer for Indian Driving License numbers.

Format: SS RR YYYY NNNNNNN
  SS      — Two-letter Indian state / UT code
  RR      — Two-digit RTO code
  YYYY    — Four-digit year of issue
  NNNNNNN — Seven-digit serial number

Supported separator styles: spaces, hyphens, or no separators.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# All valid Indian state and union territory codes
_STATE_CODES = (
    "AN|AP|AR|AS|BR|CG|CH|DD|DL|GA|GJ|HP|HR|JH|JK|"
    "KA|KL|LA|LD|MH|ML|MN|MP|MZ|NL|OD|PB|PY|RJ|SK|"
    "TN|TR|TS|UK|UP|WB"
)

# Pattern with space separators: MH 01 2020 1234567
_PATTERN_SPACES = Pattern(
    name="in_dl_spaces",
    regex=rf"\b(?:{_STATE_CODES})\s\d{{2}}\s\d{{4}}\s\d{{7}}\b",
    score=0.85,
)

# Pattern with hyphen separators: MH-01-2020-1234567
_PATTERN_HYPHENS = Pattern(
    name="in_dl_hyphens",
    regex=rf"\b(?:{_STATE_CODES})-\d{{2}}-\d{{4}}-\d{{7}}\b",
    score=0.85,
)

# Pattern with no separators: MH0120201234567
_PATTERN_NO_SEP = Pattern(
    name="in_dl_no_separator",
    regex=rf"\b(?:{_STATE_CODES})\d{{2}}\d{{4}}\d{{7}}\b",
    score=0.6,
)


class InDrivingLicenseRecognizer(PatternRecognizer):
    """Detects Indian Driving License numbers in text."""

    ENTITIES = ["IN_DRIVING_LICENSE"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_DRIVING_LICENSE",
            supported_language="en",
            patterns=[_PATTERN_SPACES, _PATTERN_HYPHENS, _PATTERN_NO_SEP],
        )
