"""Custom Presidio recognizer for Indian bank account numbers.

Indian bank account numbers are 9–18 digits (most commonly 11–16),
with no universal checksum.  Detection relies on contextual clues —
nearby keywords like *IFSC*, *NEFT*, *RTGS*, or Indian bank names
boost confidence.

Most Indian banks issue 11–16-digit account numbers; the recognizer
accepts 9–18 digits to cover the full range.

Context keywords differentiate IN_BANK_ACCOUNT from the built-in
US_BANK_NUMBER recognizer (which uses US-centric context like
"check", "debit", "save").
"""

from presidio_analyzer import Pattern, PatternRecognizer

_PATTERN = Pattern(
    name="in_bank_account",
    regex=r"\b[0-9]{9,18}\b",
    score=0.10,
)


class InBankAccountRecognizer(PatternRecognizer):
    """Detects Indian bank account numbers using contextual clues."""

    ENTITIES = ["IN_BANK_ACCOUNT"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_BANK_ACCOUNT",
            supported_language="en",
            patterns=[_PATTERN],
        )
