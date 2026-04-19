"""Custom credit card recognizer with relaxed Luhn validation.

Presidio's built-in ``CreditCardRecognizer`` discards any match that fails
the Luhn checksum (score → 0).  In practice many PII-bearing texts contain
numbers that *look* like credit cards (correct prefix, length, separators)
but use made-up digits.  Discarding them entirely means obvious PII leaks
through undetected.

This recognizer keeps Luhn-failing matches at the original pattern score
(0.3) so that context words like "credit card" or "card number" can still
boost the result above the detection threshold.
"""

from typing import List, Optional, Tuple

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer


class CreditCardImprovedRecognizer(PatternRecognizer):
    """Detect credit card numbers; Luhn failure lowers score instead of discarding."""

    PATTERNS = [
        Pattern(
            "All Credit Cards (weak)",
            r"\b(?!1\d{12}(?!\d))"
            r"((4\d{3})|(5[0-5]\d{2})|(6\d{3})|(1\d{3})|(3\d{3}))"
            r"[- ]?(\d{3,4})[- ]?(\d{3,4})[- ]?(\d{3,5})\b",
            0.3,
        ),
    ]

    CONTEXT = [
        "credit",
        "card",
        "visa",
        "mastercard",
        "cc ",
        "amex",
        "discover",
        "jcb",
        "diners",
        "maestro",
        "instapayment",
    ]

    def __init__(
        self,
        patterns: Optional[List[Pattern]] = None,
        context: Optional[List[str]] = None,
        supported_language: str = "en",
        supported_entity: str = "CREDIT_CARD",
        replacement_pairs: Optional[List[Tuple[str, str]]] = None,
    ):
        self.replacement_pairs = (
            replacement_pairs if replacement_pairs else [("-", ""), (" ", "")]
        )
        patterns = patterns if patterns else self.PATTERNS
        context = context if context else self.CONTEXT
        super().__init__(
            supported_entity=supported_entity,
            patterns=patterns,
            context=context,
            supported_language=supported_language,
        )

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        """Return True on Luhn pass (→ MAX_SCORE), None on failure (→ keep pattern score)."""
        sanitized = EntityRecognizer.sanitize_value(
            pattern_text, self.replacement_pairs
        )
        if self._luhn_checksum(sanitized):
            return True
        # Return None so Presidio keeps the original pattern score (0.3)
        # instead of dropping the result entirely.
        return None

    @staticmethod
    def _luhn_checksum(sanitized_value: str) -> bool:
        digits = [int(d) for d in sanitized_value]
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(int(x) for x in str(d * 2))
        return checksum % 10 == 0
