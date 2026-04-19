"""Custom Presidio recognizer for natural-language date expressions.

Presidio's built-in ``DateRecognizer`` only handles numeric/abbreviated
formats (dd/mm/yyyy, dd-MMM-yyyy, etc.).  SpaCy's NER catches natural-
language dates like "22nd February 2025" or "8th March 2025", but
HuggingFace token-classification models (e.g. ``dslim/bert-base-NER``)
often lack a DATE entity type.

This recognizer fills that gap with regex patterns for common English
natural-language date formats.
"""

from presidio_analyzer import Pattern, PatternRecognizer

_MONTHS = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)

_ORDINAL_DAY = r"(?:[1-9]|[12]\d|3[01])(?:st|nd|rd|th)"
_PLAIN_DAY = r"(?:0?[1-9]|[12]\d|3[01])"

# "22nd February 2025", "8th March 2025", "1st Jan 2024"
_PATTERN_ORDINAL_MONTH_YEAR = Pattern(
    name="date_ordinal_month_year",
    regex=rf"\b{_ORDINAL_DAY}\s+{_MONTHS}[\s,]+\d{{4}}\b",
    score=0.85,
)

# "22nd February", "8th March" (no year)
_PATTERN_ORDINAL_MONTH = Pattern(
    name="date_ordinal_month",
    regex=rf"\b{_ORDINAL_DAY}\s+{_MONTHS}\b",
    score=0.6,
)

# "February 22, 2025", "March 8, 2025", "Jan 1, 2024"
_PATTERN_MONTH_DAY_YEAR = Pattern(
    name="date_month_day_year",
    regex=rf"\b{_MONTHS}\s+{_PLAIN_DAY}[\s,]+\d{{4}}\b",
    score=0.85,
)

# "February 2025", "March 2025"
_PATTERN_MONTH_YEAR = Pattern(
    name="date_month_year",
    regex=rf"\b{_MONTHS}[\s,]+\d{{4}}\b",
    score=0.5,
)

# "20 February 2025", "8 March 2025" (plain day without ordinal)
_PATTERN_DAY_MONTH_YEAR = Pattern(
    name="date_day_month_year",
    regex=rf"\b{_PLAIN_DAY}\s+{_MONTHS}[\s,]+\d{{4}}\b",
    score=0.85,
)


class NaturalDateRecognizer(PatternRecognizer):
    """Detect natural-language date expressions like '22nd February 2025'."""

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="DATE_TIME",
            patterns=[
                _PATTERN_ORDINAL_MONTH_YEAR,
                _PATTERN_MONTH_DAY_YEAR,
                _PATTERN_DAY_MONTH_YEAR,
                _PATTERN_ORDINAL_MONTH,
                _PATTERN_MONTH_YEAR,
            ],
            supported_language="en",
            name="NaturalDateRecognizer",
            **kwargs,
        )
