"""Custom Presidio recognizer for banking Customer IDs.

Format: 9-digit numeric code assigned to each customer by the bank.
For example, Axis Bank assigns a 9-digit Customer ID used as the
primary login identifier for net banking and mobile banking.

The base score is intentionally low (0.10) — matching IN_BANK_ACCOUNT —
so that a 9-digit number is only classified as CUSTOMER_ID when context
words such as *customer id*, *custid*, *cif*, or *net banking* are
nearby.  Without context, the generic bank account recognizer handles it.

When context keywords are found in the text, ``analyze()`` elevates
the score to 0.95 so that CUSTOMER_ID beats IN_BANK_ACCOUNT
(0.10 + context boost ≈ 0.55).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import re

from presidio_analyzer import Pattern, PatternRecognizer

if TYPE_CHECKING:
    from presidio_analyzer import RecognizerResult
    from presidio_analyzer.nlp_engine import NlpArtifacts

_BASE_SCORE = 0.10
_BOOSTED_SCORE = 0.95

_PATTERN = Pattern(
    name="customer_id_9digit",
    regex=r"\b\d{9}\b",
    score=_BASE_SCORE,
)

# Context keywords that indicate a Customer ID (checked with word boundaries).
_CONTEXT_KEYWORDS = [
    "customer id",
    "customer number",
    "customer no",
    "cust id",
    "custid",
    "cust no",
    "cif",
    "cif number",
    "cif no",
    "net banking",
    "internet banking",
    "mobile banking",
    "login id",
    "user id",
    "banking id",
    "welcome kit",
    "welcome letter",
]

_CONTEXT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CONTEXT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


class CustomerIdRecognizer(PatternRecognizer):
    """Detects banking Customer IDs (9-digit numeric) in text."""

    ENTITIES = ["CUSTOMER_ID"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="CUSTOMER_ID",
            supported_language="en",
            patterns=[_PATTERN],
        )

    def analyze(
        self,
        text: str,
        entities: list[str] | None = None,
        nlp_artifacts: NlpArtifacts | None = None,
        regex_flags: int | None = None,
    ) -> list[RecognizerResult]:
        results = super().analyze(text, entities, nlp_artifacts, regex_flags)
        if results:
            has_context = bool(_CONTEXT_RE.search(text))
            if has_context:
                for r in results:
                    r.score = _BOOSTED_SCORE
        return results
