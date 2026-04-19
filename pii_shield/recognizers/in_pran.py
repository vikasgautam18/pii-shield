"""Custom Presidio recognizer for Indian PRAN (Permanent Retirement Account Number).

Format: 12-digit numeric code issued by the Central Recordkeeping Agency
(CRA) for National Pension System (NPS) subscribers under PFRDA.

The base score is intentionally low (0.15) — below the default 0.35
detection threshold — so that a 12-digit number is only classified as
PRAN when context words such as *pran*, *nps*, *pension*, or *pfrda*
are nearby.  Without context, Aadhaar (base 0.3–0.85) wins the overlap.

When context keywords are found in the text, ``analyze()`` elevates
the score to 0.95 so that PRAN beats the built-in PhoneRecognizer
(0.85) for 12-digit numbers in pension context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import re

from presidio_analyzer import Pattern, PatternRecognizer

if TYPE_CHECKING:
    from presidio_analyzer import RecognizerResult
    from presidio_analyzer.nlp_engine import NlpArtifacts

_BASE_SCORE = 0.15
_BOOSTED_SCORE = 0.95

_PATTERN = Pattern(
    name="in_pran_standard",
    regex=r"\b\d{12}\b",
    score=_BASE_SCORE,
)

# Context keywords that indicate PRAN (checked with word boundaries).
_CONTEXT_KEYWORDS = [
    "pran",
    "national pension",
    "nps",
    "pension",
    "retirement",
    "pfrda",
    "annuity",
    "tier i",
    "tier ii",
    "subscriber",
    "pension fund",
    "cra",
]

_CONTEXT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CONTEXT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


class InPranRecognizer(PatternRecognizer):
    """Detects Indian PRAN numbers (National Pension System) in text."""

    ENTITIES = ["IN_PRAN"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_PRAN",
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
