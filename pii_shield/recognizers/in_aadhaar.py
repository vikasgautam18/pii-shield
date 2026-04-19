"""Custom Presidio recognizer for Indian Aadhaar numbers.

Format: XXXX XXXX XXXX  (12 digits)
  - Separated by spaces, hyphens, or no separators
  - First digit is never 0 or 1

The built-in Presidio ``InAadhaarRecognizer`` only matches 12 consecutive
digits (``\\b[0-9]{12}\\b``), missing the common space- and
hyphen-separated formats printed on physical cards and used in documents.

This recognizer replaces the built-in one with broader pattern coverage.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# Space-separated: 9876 5432 1098
# Lookbehind/lookahead prevent matching inside larger groups (e.g. credit cards).
_PATTERN_SPACES = Pattern(
    name="in_aadhaar_spaces",
    regex=r"(?<!\d )\b[2-9]\d{3}\s\d{4}\s\d{4}\b(?!\s\d)",
    score=0.85,
)

# Hyphen-separated: 9876-5432-1098
# Lookbehind/lookahead prevent matching inside larger groups (e.g. credit cards).
_PATTERN_HYPHENS = Pattern(
    name="in_aadhaar_hyphens",
    regex=r"(?<!\d-)\b[2-9]\d{3}-\d{4}-\d{4}\b(?!-\d)",
    score=0.85,
)

# No separator: 987654321098
_PATTERN_NO_SEP = Pattern(
    name="in_aadhaar_no_separator",
    regex=r"\b[2-9]\d{11}\b",
    score=0.3,
)


class InAadhaarImprovedRecognizer(PatternRecognizer):
    """Detects Indian Aadhaar numbers in text."""

    ENTITIES = ["IN_AADHAAR"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_AADHAAR",
            supported_language="en",
            patterns=[_PATTERN_SPACES, _PATTERN_HYPHENS, _PATTERN_NO_SEP],
        )
