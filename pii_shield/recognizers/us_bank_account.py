"""Custom Presidio recognizer for US bank account numbers.

Replaces Presidio's built-in ``UsBankRecognizer`` (which is disabled by
default) with US-specific context words — major US bank names and
US payment terms — so that US bank accounts are distinguishable from
Indian bank accounts detected by ``InBankAccountRecognizer``.

The base score (0.10) matches ``InBankAccountRecognizer`` so that
context words alone decide which entity type wins when both match.
"""

from presidio_analyzer import Pattern, PatternRecognizer

_PATTERN = Pattern(
    name="us_bank_account",
    regex=r"\b[0-9]{8,17}\b",
    score=0.12,
)


class UsBankAccountRecognizer(PatternRecognizer):
    """Detects US bank account numbers using contextual clues."""

    ENTITIES = ["US_BANK_NUMBER"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="US_BANK_NUMBER",
            supported_language="en",
            patterns=[_PATTERN],
        )
