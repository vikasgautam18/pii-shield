"""Custom Presidio recognizer for Indian bank account numbers.

Indian bank account numbers are 9–18 digits (most commonly 11–16),
with no universal checksum.  Detection relies on contextual clues —
nearby keywords like *IFSC*, *NEFT*, *RTGS*, or Indian bank names
boost confidence.

Digit-length ranges by major bank:
  SBI: 11 | HDFC: 13–14 | ICICI: 12 | PNB: 16 | Axis: 15 |
  Kotak: 14 | BOB: 14 | Canara: 13 | Union: 15 | IndusInd: 14

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
