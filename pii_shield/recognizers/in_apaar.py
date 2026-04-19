"""Custom Presidio recognizer for Indian APAAR ID.

Format: 12-digit numeric student identifier issued under the Automated
Permanent Academic Account Registry (APAAR), also known as "One Nation,
One Student ID".  Linked to the Academic Bank of Credits (ABC) and
DigiLocker, managed by the Ministry of Education.

The base score is intentionally low (0.15) — below the default 0.35
detection threshold — so that a 12-digit number is only classified as
APAAR when context words such as *apaar*, *student*, *academic*, or
*digilocker* are nearby.  Without context, Aadhaar (base 0.3–0.85)
wins the overlap.

When context keywords are found in the text, ``analyze()`` elevates
the score to 0.95 so that APAAR beats the built-in PhoneRecognizer
(0.85) for 12-digit numbers in educational context.
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
    name="in_apaar_standard",
    regex=r"\b\d{12}\b",
    score=_BASE_SCORE,
)

# Context keywords that indicate APAAR (checked with word boundaries).
_CONTEXT_KEYWORDS = [
    "apaar",
    "academic bank of credits",
    "permanent education number",
    "student id",
    "student",
    "academic",
    "abc",
    "digilocker",
    "education",
    "enrollment",
    "school",
    "university",
    "college",
    "pen",
]

_CONTEXT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CONTEXT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


class InApaarRecognizer(PatternRecognizer):
    """Detects Indian APAAR IDs (One Nation, One Student ID) in text."""

    ENTITIES = ["IN_APAAR"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_APAAR",
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
