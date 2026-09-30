"""Keyword score boost shared by the context-only recognizers.

APAAR, PRAN and Customer IDs are bare digit strings, indistinguishable from an
Aadhaar or a bank account number without a keyword such as "apaar", "pension"
or "customer id".  Their recognizers start below the detection threshold and
are lifted to the boosted score whenever such a keyword appears anywhere in
the text, so the number is always masked.

When the keyword is not on the number's own line, or on a line introducing it,
the match is flagged with ``CONTEXT_OFF_LINE_KEY``.  The pipeline then lets a
recognizer with its own keyword on the number's line decide the type
(``pipeline.prefer_line_context``).
"""

from __future__ import annotations

import re

from presidio_analyzer import RecognizerResult

from pii_shield.text_lines import CONTEXT_OFF_LINE_KEY, block_start, line_end


def boost_by_context(
    results: list[RecognizerResult],
    text: str,
    context_re: re.Pattern[str],
    boosted_score: float,
) -> list[RecognizerResult]:
    """Raise *results* to *boosted_score* when *context_re* matches in *text*."""
    if not results or not context_re.search(text):
        return results
    for r in results:
        r.score = boosted_score
        own_lines = text[block_start(text, r.start, context_re) : line_end(text, r.end)]
        if not context_re.search(own_lines):
            r.recognition_metadata[CONTEXT_OFF_LINE_KEY] = True
    return results
