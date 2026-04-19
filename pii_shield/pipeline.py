"""Pure post-processing pipeline functions for PII entity results.

All functions in this module are **pure** — they have no I/O dependencies
(no Redis, HTTP, OTel, or filesystem access) and operate solely on their
inputs.  They are used by both the library's ``PiiShieldEngine`` and the
FastAPI service layer.
"""

import re

from presidio_analyzer import RecognizerResult

# ---------------------------------------------------------------------------
# DATE_TIME validation — filter SpaCy NER false positives
# ---------------------------------------------------------------------------

_MONTH_NAMES = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

_VALID_DATETIME_PATTERNS = re.compile(
    r"(?i)(?:"
    # Month names (full or abbreviated): "March", "8th March 2025", "Mar 2025"
    rf"(?:{_MONTH_NAMES})"
    # Ordinal day references: "8th", "1st", "23rd", "2nd"
    r"|(?:\b\d{1,2}(?:st|nd|rd|th)\b)"
    # Slash/hyphen date formats: 15/03/2025, 2025-03-15, 03-15-2025
    r"|(?:\b\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{1,4}\b)"
    # Time patterns: 10:30, 2:45 PM, 14:22:00
    r"|(?:\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?\b)"
    # ISO 8601 datetime: 2025-03-15T14:22:00
    r"|(?:\b\d{4}-\d{2}-\d{2}T)"
    # Relative date words
    r"|(?:\b(?:today|tomorrow|yesterday|last|next|ago|week|month|year)\b)"
    # Standalone 4-digit year (1900-2099) — only when by itself or with context
    r"|(?:\b(?:19|20)\d{2}\b)"
    r")"
)


def is_valid_datetime(text: str) -> bool:
    """Return True if *text* looks like a plausible date/time expression.

    Used to filter out SpaCy NER DATE_TIME false positives such as bare
    postal codes (``238879``) or phone number fragments (``91234567``).
    """
    digit_count = sum(c.isdigit() for c in text)
    if digit_count > 8 and not _VALID_DATETIME_PATTERNS.search(text.replace(" ", "")):
        non_digit = re.sub(r"\d", "", text)
        if not _VALID_DATETIME_PATTERNS.search(non_digit):
            return False
    return bool(_VALID_DATETIME_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# PERSON → LOCATION reclassification for Indian place names
# ---------------------------------------------------------------------------

_LOCATION_CONTEXT_WORDS = re.compile(
    r"(?i)\b(?:"
    # Administrative divisions
    r"village|taluka|tehsil|mandal|district|block|subdivision"
    # Urban / locality
    r"|city|town|nagar|colony|layout|sector|vihar|puram|enclave|kunj|bagh"
    # Roads / addresses
    r"|road|street|marg|lane|chowk|gali|path|avenue"
    # Institutional
    r"|branch|office|station|airport|junction|terminal"
    # Address indicators / building context
    r"|address|flat|apartment|tower|building|complex|society|floor|plot"
    r"|residing|residence|located|situated"
    r")\b"
)

_LOCATION_CONTEXT_WINDOW = 40

_NER_RECOGNIZERS = {"SpacyRecognizer", "TransformersRecognizer", "StanzaRecognizer"}


def reclassify_person_as_location(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Reclassify NER PERSON entities as LOCATION when preceded by location context.

    NER models (SpaCy, Transformers, Stanza) frequently misclassify Indian
    place names (e.g. Sholapur, Kumar Pinnacle) as PERSON.  When a PERSON
    entity is preceded by words like *village*, *taluka*, *road*, *address*,
    *flat*, etc., it is almost certainly a location.
    """
    updated: list[RecognizerResult] = []
    for r in results:
        if (
            r.entity_type == "PERSON"
            and r.recognition_metadata.get("recognizer_name") in _NER_RECOGNIZERS
        ):
            window_start = max(0, r.start - _LOCATION_CONTEXT_WINDOW)
            preceding = text[window_start : r.start]
            if _LOCATION_CONTEXT_WORDS.search(preceding):
                r.entity_type = "LOCATION"
        updated.append(r)
    return updated


# ---------------------------------------------------------------------------
# Merge adjacent LOCATION / IN_PIN_CODE entities into ADDRESS
# ---------------------------------------------------------------------------

_MERGEABLE_TYPES = {"LOCATION", "IN_PIN_CODE"}

_ADDRESS_GLUE = re.compile(
    r"(?i)^[\s,]*"
    r"(?:(?:branch|road|street|marg|lane|chowk|nagar|colony|layout|sector"
    r"|area|vihar|puram|enclave|kunj|bagh|garden|park|avenue|block"
    r"|phase|extension|ext|east|west|north|south|nr|near|opp|behind"
    r"|plot|no|number|floor|building|tower|complex|apartment|society"
    r"|village|taluka|tehsil|mandal|district|city|town)[\s,]*)*$"
)

_ADDRESS_LOOSE_GLUE = re.compile(
    r"^[\s,]*(?:[A-Za-z0-9\-]+[\s,]*)*$"
)

_ADDRESS_INDICATOR = re.compile(
    r"(?i)\b(?:address|residing\s+at|residence|lives?\s+at|located\s+at"
    r"|situated\s+at|flat|apartment|plot)\b"
)

_ADDRESS_INDICATOR_WINDOW = 80

_SENTENCE_BOUNDARY = re.compile(r"[.;!?\u2014]")  # includes em-dash —

_MAX_MERGE_GAP = 50


def merge_address_entities(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Merge adjacent LOCATION / IN_PIN_CODE entities into a single ADDRESS.

    When two or more location-family entities are close together (gap ≤ 50
    chars) and the gap contains only address "glue" words (branch, road,
    nagar, etc.), they are combined into one ADDRESS entity.  Single
    standalone LOCATION entities remain unchanged.

    When an address indicator (e.g. "Address:", "Flat", "residing at") is
    found near the first entity, a looser glue rule is used that also allows
    short comma-separated tokens — this handles locality names the NER
    missed (e.g. "Baner" between "Kumar Pinnacle" and "Pune 411045").
    """
    mergeable = sorted(
        [r for r in results if r.entity_type in _MERGEABLE_TYPES],
        key=lambda r: r.start,
    )
    others = [r for r in results if r.entity_type not in _MERGEABLE_TYPES]

    if not mergeable:
        return results

    def _gap_is_valid(prev: RecognizerResult, curr: RecognizerResult) -> bool:
        gap = text[prev.end : curr.start]
        if len(gap) > _MAX_MERGE_GAP or _SENTENCE_BOUNDARY.search(gap):
            return False
        if _ADDRESS_GLUE.match(gap):
            return True
        window_start = max(0, prev.start - _ADDRESS_INDICATOR_WINDOW)
        preceding = text[window_start : prev.start]
        if _ADDRESS_INDICATOR.search(preceding) and _ADDRESS_LOOSE_GLUE.match(gap):
            return True
        return False

    groups: list[list[RecognizerResult]] = [[mergeable[0]]]
    for r in mergeable[1:]:
        prev = groups[-1][-1]
        if _gap_is_valid(prev, r):
            groups[-1].append(r)
        else:
            groups.append([r])

    merged: list[RecognizerResult] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
        else:
            start = group[0].start
            end = group[-1].end
            score = min(r.score for r in group)
            addr = RecognizerResult(
                entity_type="ADDRESS",
                start=start,
                end=end,
                score=score,
            )
            addr.recognition_metadata = {
                "recognizer_name": "AddressMerger",
            }
            merged.append(addr)

    return others + merged


def remove_overlapping(
    results: list[RecognizerResult],
) -> list[RecognizerResult]:
    """Keep non-overlapping entities, preferring longer spans then higher scores.

    Sorting by span length first ensures that a broader entity (e.g.
    PHONE_NUMBER covering ``+91 9811034567``) wins over a narrower
    high-score entity it fully encompasses (e.g. UK_NHS covering
    ``9811034567``).  This prevents partial PII exposure and avoids
    placeholder corruption caused by replacing partially-overlapping spans.
    """
    sorted_results = sorted(
        results,
        key=lambda r: (-(r.end - r.start), -r.score, r.start),
    )
    filtered: list[RecognizerResult] = []
    for result in sorted_results:
        if not any(
            result.start < kept.end and result.end > kept.start
            for kept in filtered
        ):
            filtered.append(result)
    return filtered
