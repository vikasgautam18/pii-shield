"""Pure post-processing pipeline functions for PII entity results.

All functions in this module are **pure** — they have no I/O dependencies
(no Redis, HTTP, OTel, or filesystem access) and operate solely on their
inputs.  They are used by both the library's ``PiiShieldEngine`` and the
FastAPI service layer.
"""

import re

from presidio_analyzer import RecognizerResult

from pii_shield.context_enhancer import key_start
from pii_shield.text_lines import (
    CONTEXT_OFF_LINE_KEY,
    INLINE_SPACE,
    block_start,
    line_end,
)

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


# ---------------------------------------------------------------------------
# Context windows — bounded by the entity's own line and sentence
# ---------------------------------------------------------------------------

# Several steps relabel an entity from keywords near it.  A keyword in another
# statement says nothing about the entity, so those windows stop at line breaks
# and sentence ends.  A line directly above still counts when it introduces the
# entity's line — a "Label:" line, or a short heading ending in the keyword, see
# ``text_lines.block_start``.  Steps that only widen a mask (address merging)
# keep their wider windows, so bounding never unmasks anything.
_TERMINATOR = re.compile(r"[.!?](?=[ \t])")

# Words that end in "." without ending the sentence ("Acct. No. 12", "Opp.
# Park").  Words under three characters ("No.", "R.K.") never end a sentence,
# and neither do honorifics ("Mr.", "Smt.") from _PERSON_TITLES.
_ABBREVIATIONS = frozenset({
    # banking / identity
    "acc", "acct", "accts", "bal", "amt", "cust", "ref", "mob", "tel", "nos",
    "qty", "approx", "etc",
    # address
    "addr", "res", "resi", "perm", "corr", "opp", "ave", "hwy", "mkt", "tal",
    "teh", "dist", "distt", "vill", "sec", "sect", "bldg", "apt", "apts",
    "appt", "flr", "hno", "qtr", "soc", "hsg", "chs", "ext", "blk", "est",
    "ind", "stn",
    # organisations / roles
    "govt", "dept", "ltd", "pvt", "inc", "corp", "univ", "inst", "assn",
    "mgr", "asst", "exec",
    # months
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
})


def _is_sentence_end(text: str, i: int) -> bool:
    """Whether the terminator at ``text[i]`` ends a sentence."""
    if text[i] != ".":
        return True
    j = i
    while j > 0 and text[j - 1].isalnum():
        j -= 1
    word = text[j:i].lower()
    return (
        len(word) >= 3
        and word not in _ABBREVIATIONS
        and word not in _PERSON_TITLES
    )


def _context_start(
    text: str,
    pos: int,
    width: int,
    introducer: re.Pattern[str] | None = None,
) -> int:
    """Start of a lookback window of at most *width* chars ending at *pos*.

    *introducer* is the keyword pattern of the calling step: a line above that
    ends with one of those keywords introduces *pos*'s line and stays in view.
    """
    start = max(block_start(text, pos, introducer), pos - width)
    for match in _TERMINATOR.finditer(text, start, pos):
        if _is_sentence_end(text, match.start()):
            start = match.end()
    return start


def _context_end(text: str, pos: int, width: int) -> int:
    """End of a lookahead window of at most *width* chars starting at *pos*."""
    end = min(line_end(text, pos), pos + width)
    for match in _TERMINATOR.finditer(text, pos, end):
        if _is_sentence_end(text, match.start()):
            return match.start()
    return end


# ---------------------------------------------------------------------------
# Split NER spans at line breaks
# ---------------------------------------------------------------------------


def split_at_line_breaks(
    results: list[RecognizerResult],
    text: str,
    allow_list: list[str] | None = None,
) -> list[RecognizerResult]:
    """Split NER spans that run across a line break into one span per line.

    The NER tokenizer treats a line break as ordinary whitespace, so a name
    ending one line can swallow the first word of the next ("Ananya Rao" +
    "Aadhaar").  Each fragment keeps the original type and score, so nothing is
    unmasked, except fragments that are allow-listed or have no letters or
    digits, which are dropped.  Pattern-based entities are left alone: their
    regexes decide themselves whether they may span lines.
    """
    allowed = set(allow_list or ())
    out: list[RecognizerResult] = []
    for r in results:
        span = text[r.start : r.end]
        if r.entity_type not in _NER_ENTITY_TYPES or "\n" not in span:
            out.append(r)
            continue
        pos = r.start
        for piece in span.split("\n"):
            start = pos + len(piece) - len(piece.lstrip())
            end = pos + len(piece.rstrip())
            pos += len(piece) + 1
            fragment = text[start:end]
            if fragment in allowed or not any(c.isalnum() for c in fragment):
                continue
            out.append(
                RecognizerResult(
                    entity_type=r.entity_type,
                    start=start,
                    end=end,
                    score=r.score,
                    analysis_explanation=r.analysis_explanation,
                    recognition_metadata=dict(r.recognition_metadata or {}),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Line-local evidence decides between recognizers claiming the same number
# ---------------------------------------------------------------------------


def prefer_line_context(
    results: list[RecognizerResult],
    text: str,
    context_patterns: dict[str, re.Pattern[str]],
) -> list[RecognizerResult]:
    """Let a keyword on the number's own line decide between two recognizers.

    The context-only recognizers (APAAR, PRAN, Customer ID) accept a bare number
    whenever their keyword appears anywhere in the text, flagging matches whose
    keyword sits on another line.  When a different recognizer claims exactly
    the same digits and one of *its* context words is on the number's line (or
    on a line introducing it), that recognizer decides the type: in "Student:
    Ananya Rao\\nAadhaar: 234567890123" the number is an Aadhaar.  Only the type
    changes — the competitor covers the same span, so nothing is unmasked.

    *context_patterns* maps a recognizer name to a pattern of its context words.
    """
    def _meta(r: RecognizerResult) -> dict:
        return r.recognition_metadata or {}

    def _has_line_evidence(r: RecognizerResult) -> bool:
        pattern = context_patterns.get(_meta(r).get("recognizer_name"))
        if pattern is None:
            return False
        own_lines = text[block_start(text, r.start, pattern) : line_end(text, r.end)]
        return bool(pattern.search(own_lines))

    by_span: dict[tuple[int, int], list[RecognizerResult]] = {}
    for r in results:
        by_span.setdefault((r.start, r.end), []).append(r)

    return [
        r for r in results
        if not _meta(r).get(CONTEXT_OFF_LINE_KEY)
        or not any(
            other.entity_type != r.entity_type
            and not _meta(other).get(CONTEXT_OFF_LINE_KEY)
            and _has_line_evidence(other)
            for other in by_span[(r.start, r.end)]
        )
    ]


# ---------------------------------------------------------------------------
# PHONE_NUMBER → IN_BANK_ACCOUNT reclassification
# ---------------------------------------------------------------------------

# A bare run of 9-18 digits is a valid Indian bank account number, and the
# phone recognizer claims the same digits whenever they also form a valid phone
# number: a mobile ("9876543210") or a landline without its leading 0
# ("5498721032").  The regexes cannot settle it.  Scores cannot settle it
# either: the phone pattern scores 0.60 against the account pattern's 0.10, and
# "number" sits in the phone recognizer's context list, so "bank account number"
# boosts the *phone* score to 1.00.  Only the surrounding words carry the
# answer, so the nearest preceding cue decides.
_BARE_ACCOUNT_SHAPED = re.compile(r"\A[1-9]\d{8,17}\Z")

# A mobile number written with its country code but without the "+"
# ("919876543210") is a phone, just like "+91 98765 43210".
_MOBILE_WITH_COUNTRY_CODE = re.compile(r"\A91[6-9]\d{9}\Z")

_ACCOUNT_CUE = re.compile(r"(?i)\b(?:a/c|ac\s*no|acct|account|passbook|beneficiary)\b")

_PHONE_CUE = re.compile(
    r"(?i)\b(?:mobile|phone|cell|contact|whatsapp|telephone|tel|landline"
    r"|call|dial|sms|reach|helpline|toll|tollfree|fax)\b"
)

_ACCOUNT_CUE_WINDOW = 45

# Transfer markers never accompany a phone number, so they settle the case even
# when they follow the digits ("Transfer to 9876543210 IFSC SBIN0001234").
# Deliberately excludes generic words like "account", which legitimately appear
# after a phone number ("call me on X for account queries").
_STRONG_BANK_CUE = re.compile(r"(?i)\b(?:ifsc|neft|rtgs|imps|micr|swift)\b")

_STRONG_CUE_WINDOW = 30


def reclassify_phone_as_bank_account(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Relabel a bare PHONE_NUMBER as IN_BANK_ACCOUNT on account cues.

    Only fires when the span is a bare run of 9-18 digits, the length of an
    Indian bank account number (a ``+91`` or ``91`` country code, leading
    ``0`` or any separator means it really is a phone), and either the nearest
    cue before it is an account word rather than a phone word, or a transfer
    marker (IFSC, NEFT, ...) sits just after it.  Using the *nearest* preceding
    cue keeps "account number is X and mobile is Y" correct for both numbers.
    Cues only count on the number's own line and sentence, or on a line
    directly above that introduces it ("Account number:", "Bank account").
    """
    for r in results:
        if r.entity_type != "PHONE_NUMBER":
            continue
        digits = text[r.start : r.end]
        if not _BARE_ACCOUNT_SHAPED.match(digits) or _MOBILE_WITH_COUNTRY_CODE.match(digits):
            continue
        window = text[
            _context_start(text, r.start, _ACCOUNT_CUE_WINDOW, _ACCOUNT_CUE) : r.start
        ]
        account_at = max((m.start() for m in _ACCOUNT_CUE.finditer(window)), default=-1)
        phone_at = max((m.start() for m in _PHONE_CUE.finditer(window)), default=-1)
        following = text[r.end : _context_end(text, r.end, _STRONG_CUE_WINDOW)]
        if account_at > phone_at or _STRONG_BANK_CUE.search(following):
            r.entity_type = "IN_BANK_ACCOUNT"
    return results


def reclassify_person_as_location(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Reclassify NER PERSON entities as LOCATION when preceded by location context.

    NER models (SpaCy, Transformers, Stanza) frequently misclassify Indian
    place names (e.g. Sholapur, Kumar Pinnacle) as PERSON.  When a PERSON
    entity is preceded by words like *village*, *taluka*, *road*, *address*,
    *flat*, etc., it is almost certainly a location.  Only words on the same
    line and in the same sentence count — "residing at Mumbai. My name is
    R.K. Sharma" is about a person — plus a line directly above that
    introduces it ("Branch:", "Correspondence Address").
    """
    updated: list[RecognizerResult] = []
    for r in results:
        if (
            r.entity_type == "PERSON"
            and r.recognition_metadata.get("recognizer_name") in _NER_RECOGNIZERS
        ):
            window_start = _context_start(
                text, r.start, _LOCATION_CONTEXT_WINDOW, _LOCATION_CONTEXT_WORDS
            )
            preceding = text[window_start : r.start]
            if _LOCATION_CONTEXT_WORDS.search(preceding):
                r.entity_type = "LOCATION"
        updated.append(r)
    return updated


# ---------------------------------------------------------------------------
# Extend PERSON spans to adjacent capitalized name tokens (recall for names
# whose first token NER missed, e.g. odd capitalization "VIkas Gautam")
# ---------------------------------------------------------------------------

# Indian professional / honorific prefixes.  "Md" is deliberately absent: in
# Indian usage "Md. Faisal" abbreviates Mohammed and is part of the name.
_PERSON_TITLES = frozenset({
    # generic
    "mr", "mrs", "ms", "miss", "mister", "madam", "sir",
    # academic / medical
    "dr", "doctor", "prof", "professor",
    # professional
    "er", "engr", "ca", "cs", "cma", "adv", "advocate",
    # Indian honorifics
    "shri", "sri", "smt", "kum", "kumari", "pt", "pandit",
    # services / judiciary / clergy
    "capt", "captain", "col", "colonel", "maj", "major", "lt", "gen", "brig",
    "justice", "hon", "honble", "rev", "fr",
})

# Capitalized words that commonly sit next to a name but are NOT part of it:
# pronouns/determiners, greetings, honorific titles, and common role acronyms.
_NON_NAME_TOKENS = {
    "i", "my", "me", "we", "our", "your", "his", "her", "their", "the", "a", "an",
    "hello", "hi", "hey", "dear",
    "ceo", "cto", "cfo", "coo", "vp",
} | set(_PERSON_TITLES)

_SENTENCE_END_CHARS = ".!?;:\n\u2014"

_MAX_NAME_TOKENS_ADDED = 2


def merge_adjacent_person_tokens(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Extend NER PERSON spans left to adjacent capitalized name tokens.

    NER models (esp. spaCy) sometimes drop the first token of a multi-word name
    when its capitalization is unusual (e.g. ``VIkas Gautam`` -> only
    ``Gautam``).  This extends a PERSON span leftward across single spaces to
    include immediately-preceding capitalized alphabetic tokens that look like
    name parts, so the whole name is captured.

    Guards against over-merging: skips sentence-initial words (so greetings /
    sentence starts like "Hello Vikas" are not absorbed), skips a small set of
    pronouns / titles / role acronyms, and adds at most two tokens.
    """
    for r in results:
        if (
            r.entity_type != "PERSON"
            or r.recognition_metadata.get("recognizer_name") not in _NER_RECOGNIZERS
        ):
            continue
        # In ALL-CAPS text every token is capitalised, so "capitalised token" is
        # no longer evidence of a name part and extending would swallow ordinary
        # words ("ISSUED BY SANJAY GUPTA").  Spans in such text come from the
        # ALL-CAPS recovery pass, which already resolved them on true-cased text.
        if text[r.start : r.end].isupper():
            continue
        new_start = r.start
        added = 0
        while added < _MAX_NAME_TOKENS_ADDED:
            # Require exactly one space immediately before the current span.
            if new_start < 2 or text[new_start - 1] != " ":
                break
            space_idx = new_start - 1
            k = space_idx - 1
            while k >= 0 and text[k].isalpha():
                k -= 1
            tok_start = k + 1
            token = text[tok_start:space_idx]
            if len(token) < 2 or not token[0].isupper() or not token.isalpha():
                break
            if token.lower() in _NON_NAME_TOKENS:
                break
            # Token must be preceded by a space/start (a clean word boundary)...
            if tok_start > 0 and text[tok_start - 1] != " ":
                break
            # ...and must NOT be sentence-initial (avoid absorbing sentence starts).
            m = tok_start - 1
            while m >= 0 and text[m] == " ":
                m -= 1
            if m < 0 or text[m] in _SENTENCE_END_CHARS:
                break
            new_start = tok_start
            added += 1
        if new_start != r.start:
            r.start = new_start
    return results


# ---------------------------------------------------------------------------
# Extend PERSON spans right across a surname following dotted initials
# ---------------------------------------------------------------------------

# Dotted initials terminate the entity in cased NER models: "Mr. R.K. Sharma"
# yields only "R.K." (score 1.00) and the surname is never detected, leaking it.
# "R K Sharma" without dots returns the whole name, so the periods are the
# trigger.  Restricting extension to initials-shaped spans keeps ordinary names
# untouched.
_INITIALS_SPAN = re.compile(r"\A(?:[A-Za-z]\.\s*){1,4}\Z")


def extend_person_over_initials(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Extend an initials-only PERSON span right over the following surname.

    Only applies to spans that are purely dotted initials ("R.K.", "A.P.J.",
    "S."), and absorbs at most two following capitalised tokens that are not
    ordinary vocabulary — so "R.K. Sharma" is captured whole while "A.B. Next
    week" stops at the initials.
    """
    for r in results:
        if r.entity_type != "PERSON":
            continue
        if not _INITIALS_SPAN.match(text[r.start : r.end]):
            continue
        new_end = r.end
        added = 0
        initials = 0
        while added < _MAX_NAME_TOKENS_ADDED:
            if new_end >= len(text) or text[new_end] != " ":
                break
            tok_start = new_end + 1
            # A further single-letter initial ("K.") belongs to the name; NER
            # may return only the first of several spaced initials.
            if (
                initials < 4
                and tok_start + 1 < len(text)
                and text[tok_start].isupper()
                and text[tok_start + 1] == "."
            ):
                new_end = tok_start + 2
                initials += 1
                continue
            k = tok_start
            while k < len(text) and text[k].isalpha():
                k += 1
            token = text[tok_start:k]
            if len(token) < 2 or not token[0].isupper():
                break
            lowered = token.lower()
            if lowered in _NON_NAME_TOKENS or lowered in _LOWERCASE_WORDS:
                break
            new_end = k
            added += 1
        if new_end != r.end:
            r.end = new_end
    return results


# ---------------------------------------------------------------------------
# Drop sentence-initial PERSON false positives (common non-name words that NER
# mis-tags as a name when capitalized at the start of an imperative sentence,
# e.g. "Email me at ..." -> PERSON "Email")
# ---------------------------------------------------------------------------

# Curated to EXCLUDE any plausible given name — only clear action verbs,
# greetings, and discourse markers that are essentially never people's names.
_COMMON_NON_NAME_STARTERS = {
    # imperative / action verbs
    "email", "call", "text", "send", "contact", "book", "update", "transfer",
    "pay", "check", "help", "tell", "show", "find", "share", "forward", "reply",
    "cancel", "confirm", "verify", "register", "subscribe", "schedule", "order",
    "download", "upload", "submit", "apply", "request", "kindly", "remind",
    # greetings / closings / discourse markers
    "hello", "hey", "hi", "thanks", "thank", "regards", "cheers", "welcome",
    "greetings", "please", "sure", "okay",
}


def _is_sentence_initial(text: str, pos: int) -> bool:
    """True if the char at *pos* begins a sentence (start of text or after . ! ?)."""
    m = pos - 1
    while m >= 0 and text[m] == " ":
        m -= 1
    return m < 0 or text[m] in _SENTENCE_END_CHARS


def filter_person_false_positives(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Drop single-token, sentence-initial PERSON entities that are common
    non-name words (e.g. "Email" in "Email me at ...").

    Very conservative: only fires when the span is a single token, sits at a
    sentence start, and its lowercase form is in a curated word list that
    excludes plausible given names.
    """
    out: list[RecognizerResult] = []
    for r in results:
        if (
            r.entity_type == "PERSON"
            and r.recognition_metadata.get("recognizer_name") in _NER_RECOGNIZERS
        ):
            span = text[r.start : r.end]
            if (
                " " not in span
                and span.lower() in _COMMON_NON_NAME_STARTERS
                and _is_sentence_initial(text, r.start)
            ):
                continue  # drop false positive
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Honorific / professional title normalisation
# ---------------------------------------------------------------------------

# Cased NER handles the familiar Western titles but not Indian professional
# ones.  "CA Abhay Sharma" and "CS Priya" come back as ORGANIZATION — which
# still redacts under the default strategy but leaks the name outright for any
# app that allow-lists ORGANIZATION, and applies the wrong per-entity strategy.
# "Er. Ram" and "Col. Rana" go the other way: the abbreviation itself is tagged
# PERSON, leaving a stray "." span behind.
_TITLE_ALTERNATION = "|".join(sorted(_PERSON_TITLES, key=len, reverse=True))

# One or more titles leading a span: "CA ", "Smt. ", "Lt. Col. ".  Only
# whitespace within the line counts: a title-like word ending the line above
# may be a surname ("Kumari", "Pandit"), not a title of the name below it.
_TITLE_PREFIX = re.compile(
    rf"(?i)\A(?:(?:{_TITLE_ALTERNATION})\.?{INLINE_SPACE}+)+"
)

# Titles filling the span's whole first line, the name on the next: "CA\n".
_TITLE_LINE = re.compile(
    rf"(?i)\A(?:(?:{_TITLE_ALTERNATION})\.?{INLINE_SPACE}*)+\r?\n"
)

# The span is nothing but a title: "Er", "Col."
_TITLE_ONLY = re.compile(rf"(?i)\A(?:{_TITLE_ALTERNATION})\.?\Z")

_TITLE_BEARING_TYPES = frozenset({"PERSON", "ORGANIZATION", "NRP"})


def _preceded_by_title(text: str, pos: int) -> bool:
    """True if the word immediately before *pos* is an honorific."""
    i = pos
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] == ".":
        i -= 1
    j = i
    while j > 0 and text[j - 1].isalpha():
        j -= 1
    return text[j:i].lower() in _PERSON_TITLES


def normalize_person_titles(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Resolve entities around honorific and professional titles.

    Three corrections, in order:

    1. Drop spans that are only a title ("Er", "Col.") or carry no alphanumeric
       content at all — the stray "." left behind by "Er. Ram Kumar".
    2. Trim a leading title off a span and treat the remainder as a PERSON, so
       "CA Abhay Sharma" tagged ORGANIZATION becomes PERSON "Abhay Sharma".
    3. Relabel an ORGANIZATION/NRP that directly follows a title as PERSON,
       covering "Smt. Kavita" where the title sits outside the span.

    Titles are always left outside the span, matching the existing behaviour
    for "Mr. Rajesh Sharma" — a title is not itself PII.  The exception is a
    title-like word alone at the end of a line with the name on the next line:
    it may be a surname ("Surname: Kumari\\nPriya Sharma"), so it stays in the
    span and only the type becomes PERSON; ``split_at_line_breaks`` later
    masks each line separately.
    """
    out: list[RecognizerResult] = []
    for r in results:
        span = text[r.start : r.end]
        if r.entity_type in _NER_ENTITY_TYPES:
            stripped = span.strip()
            if not any(c.isalnum() for c in stripped):
                continue
            if _TITLE_ONLY.match(stripped):
                continue
        if r.entity_type in _TITLE_BEARING_TYPES:
            match = _TITLE_PREFIX.match(span)
            if match and match.end() < len(span):
                r.start += match.end()
                r.entity_type = "PERSON"
                out.append(r)
                continue
            if _TITLE_LINE.match(span):
                r.entity_type = "PERSON"
                out.append(r)
                continue
            if r.entity_type != "PERSON" and _preceded_by_title(text, r.start):
                r.entity_type = "PERSON"
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Merge adjacent LOCATION / IN_PIN_CODE entities into ADDRESS
# ---------------------------------------------------------------------------

_MERGEABLE_TYPES = {"LOCATION", "IN_PIN_CODE"}

# Words that may sit between two parts of one address ("MG Road branch,
# Bangalore"); a gap holding only these and separators always merges.
_ADDRESS_GLUE_WORDS = frozenset({
    "branch", "road", "street", "marg", "lane", "chowk", "nagar", "colony",
    "layout", "sector", "area", "vihar", "puram", "enclave", "kunj", "bagh",
    "garden", "park", "avenue", "block", "phase", "extension", "ext", "east",
    "west", "north", "south", "nr", "near", "opp", "behind", "plot", "no",
    "number", "floor", "building", "tower", "complex", "apartment", "society",
    "village", "taluka", "tehsil", "mandal", "district", "city", "town",
})

# Separators inside an address.  "." is one because of abbreviations ("no.",
# "Opp."); a "." that ends a sentence stops the merge before the gap is read.
_GAP_SEPARATORS = re.compile(r"[\s,.]+")

# What an address introduced by an indicator may hold between two detected
# parts: words NER missed ("Baner"), numbers, "45/2", "#12".  A single character
# class, so it runs in linear time: the nested pattern it replaces backtracked
# exponentially on a long word followed by any other character.
_ADDRESS_LOOSE_GLUE = re.compile(r"[A-Za-z0-9\-/#\s,.]*")

_ADDRESS_INDICATOR = re.compile(
    r"(?i)\b(?:address|residing\s+at|residence|lives?\s+at|located\s+at"
    r"|situated\s+at|flat|apartment|plot)\b"
)

_ADDRESS_INDICATOR_WINDOW = 80

_BREAK_MARK = re.compile(r"[.;!?\u2014]")  # includes em-dash —

_MAX_MERGE_GAP = 50

# Building / society names are tagged inconsistently by NER — the same name can
# come back as LOCATION, ORGANIZATION or NRP depending on surrounding words.
# Near an address indicator they are address components, so they are allowed to
# merge there and nowhere else (for promoting one on its own, see
# merge_address_entities).
_ADDRESS_CONTEXT_TYPES = frozenset({"ORGANIZATION", "NRP"})

# Flat / unit / house numbers: "F3003", "A-101", "501", "12B".  There is no
# recognizer for these, so they are absorbed from the text immediately before an
# address group rather than detected independently.
_UNIT_NUMBER_TOKEN = re.compile(r"[A-Za-z]{0,2}-?\d{1,5}[A-Za-z]?\Z")

# House numbers written in parts: "12-3-456", "45/2".
_MULTI_PART_NUMBER = re.compile(
    r"[A-Za-z]{0,2}-?\d{1,5}[A-Za-z]?(?:[-/]\d{1,5}[A-Za-z]?)+\Z"
)

# Dates and year ranges ("12/05", "2024-25") look like multi-part house
# numbers, so those are absorbed only after a unit label ("H.No. 12-3-456") or
# inside an address an indicator introduced.
_NUMBER_IN_PARTS = re.compile(r"\d[-/]\d")

# Floor numbers: "2nd", "11th".
_ORDINAL_TOKEN = re.compile(r"(?i)\d{1,3}(?:st|nd|rd|th)\Z")

# Words that name a unit or a part of one: "Flat no. 302", "H.No. 12",
# "B Wing", "3rd Floor", "Survey No. 45/2", "5th Cross", "Shop 4, Opp.".
_UNIT_LABELS = frozenset({
    "flat", "no", "nos", "number", "house", "h", "plot", "door", "d", "block",
    "blk", "wing", "floor", "flr", "apt", "apartment", "unit", "room", "shop",
    "suite", "tower", "bldg", "building", "qtr", "quarter", "survey", "sy",
    "gali", "ward", "sector", "phase", "stage", "cross", "main", "street",
    "lane", "road", "opp",
})

# "no" names a unit only before a number: "Flat no. 302", but never "there is
# no Prestige Towers".
_NUMBER_LABELS = frozenset({"no", "nos", "number"})

# NER sometimes tags a unit label on its own ("Flat" as LOCATION); such an
# entity is absorbed with the rest.  A date ("12/05/2024") keeps its own label.
_UNIT_LABEL_TYPES = frozenset({"LOCATION", "ORGANIZATION", "NRP", "PERSON"})

_UNIT_WORD = re.compile(r"[A-Za-z0-9\-/]+")

# How far before an address its unit designators may start ("Flat No. 1203,
# B Wing, 12th Floor, Tower C, " is 45 characters).
_MAX_UNIT_SPAN = 60


def _has_address_indicator(text: str, pos: int) -> bool:
    """True if an address indicator appears shortly before *pos*.

    Deliberately not bounded by lines or sentences: it only decides how far an
    address extends, and a wider address never exposes anything.
    """
    return bool(
        _ADDRESS_INDICATOR.search(
            text[max(0, pos - _ADDRESS_INDICATOR_WINDOW) : pos]
        )
    )


def _indicator_in_statement(text: str, pos: int) -> bool:
    """True if an address indicator introduces *pos* within its own statement."""
    start = _context_start(text, pos, _ADDRESS_INDICATOR_WINDOW, _ADDRESS_INDICATOR)
    return bool(_ADDRESS_INDICATOR.search(text[start:pos]))


def _near_location_word(text: str, pos: int) -> bool:
    """True if a location word appears within the 40 chars before *pos*.

    This is the unbounded window ``reclassify_person_as_location`` used before
    it was confined to the entity's own statement.  Merging still uses it, so a
    building name tagged PERSON joins its address exactly as before.
    """
    window = text[max(0, pos - _LOCATION_CONTEXT_WINDOW) : pos]
    return bool(_LOCATION_CONTEXT_WORDS.search(window))


def _absorb_unit_number(text: str, start: int) -> int:
    """Extend *start* left over a flat/unit number such as ``F3003,``.

    Returns the original *start* when the preceding token is not a unit number,
    so ordinary words are never swallowed into the address.  Only decides
    whether a lone building name follows a unit number; an address absorbs its
    full unit designation with ``_absorb_unit_designators``.
    """
    sep = start
    while sep > 0 and text[sep - 1] in " ,":
        sep -= 1
    if sep == start or start - sep > 2:
        return start  # needs a separator, but not a wide gap
    tok = sep
    while tok > 0 and (text[tok - 1].isalnum() or text[tok - 1] == "-"):
        tok -= 1
    token = text[tok:sep]
    if not token or not any(c.isdigit() for c in token):
        return start
    if not _UNIT_NUMBER_TOKEN.fullmatch(token):
        return start
    if tok and text[tok - 1] not in " \t(\n":
        return start
    return tok


def _has_sentence_break(text: str, start: int, end: int) -> bool:
    """Whether a sentence ends inside ``text[start:end]``.

    ";", "!", "?" and "—" always end one.  A "." ends one only when whitespace
    or the end of the text follows it and it does not close an abbreviation
    (see ``_is_sentence_end``), so "Flat no. 302" and "Opp. City Mall" are not
    split.
    """
    for match in _BREAK_MARK.finditer(text, start, end):
        i = match.start()
        if text[i] != ".":
            return True
        if text[i + 1 : i + 2].strip():
            continue  # inside a word: "H.No", "1.5"
        if not text[i - 1 : i].isalnum() or _is_sentence_end(text, i):
            return True
    return False


def _is_address_glue(gap: str) -> bool:
    """Whether *gap* holds only separators and address words ("branch, ")."""
    return all(
        word.lower() in _ADDRESS_GLUE_WORDS
        for word in _GAP_SEPARATORS.split(gap)
        if word
    )


def _unit_token_kind(token: str) -> str | None:
    """Classify a word written before an address.

    Returns "value" for a unit number, floor or block letter ("302",
    "12-3-456", "2nd", "C"), "label" for a word naming one ("Flat", "No",
    "Wing"), and None for anything else.  A hyphenated word takes the kinds of
    its parts ("B-Wing" is a value).
    """
    if (
        _UNIT_NUMBER_TOKEN.fullmatch(token)
        or _MULTI_PART_NUMBER.fullmatch(token)
        or _ORDINAL_TOKEN.fullmatch(token)
        or (len(token) == 1 and token.isupper())
    ):
        return "value"
    if token.lower() in _UNIT_LABELS:
        return "label"
    parts = [part for part in token.split("-") if part]
    if len(parts) > 1:
        kinds = [_unit_token_kind(part) for part in parts]
        if None not in kinds:
            return "value" if "value" in kinds else "label"
    return None


def _is_unit_designation(span: str) -> bool:
    """Whether *span* holds only unit numbers and labels ("Flat", "C 23")."""
    words = _UNIT_WORD.findall(span)
    return bool(words) and all(_unit_token_kind(w) is not None for w in words)


def _unit_word_before(text: str, pos: int) -> str:
    """The word before *pos*, skipping spaces, dots and "#" ("No" in "H.No. 12")."""
    end = pos
    while end > 0 and text[end - 1] in " \t.#":
        end -= 1
    begin = end
    while begin > 0 and (text[begin - 1].isalnum() or text[begin - 1] == "-"):
        begin -= 1
    return text[begin:end]


def _opens_with_unit_value(text: str, pos: int) -> bool:
    """Whether the text at *pos* opens with a unit number or letter ("No. 45/2")."""
    for match in _UNIT_WORD.finditer(text, pos, pos + _MAX_UNIT_SPAN):
        kind = _unit_token_kind(match.group())
        if kind != "label":
            return kind == "value"
    return False


def _absorb_unit_designators(
    text: str, start: int, taken: list[RecognizerResult]
) -> int:
    """Extend *start* left over the unit designators written before an address.

    In "Flat no. 302, C 23, Prestige Towers" the flat number, block letter and
    their labels have no recognizer, so the words directly before the address
    are absorbed while each is a unit number, floor, block letter or unit label
    (see ``_unit_token_kind``).  Nothing is absorbed unless one of them, or the
    start of the address itself ("Door" before "No. 45/2"), is a number or
    letter, and "no" counts only before a number, so "the unit" or "there is
    no" is never swallowed.  A line break is crossed only after a comma in an
    address an indicator introduced.

    A word another entity in *taken* covers is absorbed only when that entity
    is itself just a unit designation ("Flat" tagged LOCATION); otherwise the
    walk stops, so the "T" of "Contoso Bank T" before "Nagar" is left alone.
    """
    new_start = start
    has_value = _opens_with_unit_value(text, start)
    while True:
        sep = new_start
        while sep > 0 and new_start - sep < 4 and text[sep - 1] in " \t\r\n,.#":
            sep -= 1
        gap = text[sep:new_start]
        if not gap:
            break  # a word must be separated from what follows it
        if "\n" in gap and not (
            gap.lstrip(" \t").startswith(",") and _has_address_indicator(text, sep)
        ):
            break
        tok = sep
        while tok > 0 and (text[tok - 1].isalnum() or text[tok - 1] in "-/"):
            tok -= 1
        token = text[tok:sep]
        kind = _unit_token_kind(token) if token else None
        if kind is None or start - tok > _MAX_UNIT_SPAN:
            break
        if tok and text[tok - 1] not in " \t\r\n(,.#":
            break
        if text[tok - 1 : tok] == "." and text[tok - 2 : tok - 1].isdigit():
            break  # a decimal such as "5.30", not a unit number
        if token.lower() in _NUMBER_LABELS:
            following = _UNIT_WORD.match(text, new_start)
            if not following or not any(c.isdigit() for c in following.group()):
                break
        if _NUMBER_IN_PARTS.search(token) and not (
            _unit_token_kind(_unit_word_before(text, tok)) == "label"
            or _has_address_indicator(text, tok)
        ):
            break
        covering = [r for r in taken if r.start < sep and r.end > tok]
        if any(
            r.entity_type not in _UNIT_LABEL_TYPES
            or r.end > new_start
            or not _is_unit_designation(text[r.start : r.end])
            for r in covering
        ):
            break
        new_start = min([tok] + [r.start for r in covering])
        has_value = has_value or kind == "value"
    return new_start if has_value else start


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
    missed (e.g. "Baner" between "Kumar Pinnacle" and "Pune 411045").  In that
    context ORGANIZATION / NRP spans also count as address components, a lone
    such span is promoted to ADDRESS, and the unit designators written before
    the address ("Flat no. 302, C 23,") are absorbed so they are not left
    exposed.  A "." ends an address only when it ends a sentence, not when it
    closes an abbreviation such as "no." or "Opp.".

    Keywords may come from another line or sentence when they only widen the
    address, since that never exposes anything, but they do not relabel an
    entity left on its own: a building name tagged PERSON joins a neighbouring
    address part yet stays PERSON when alone, and a lone ORGANIZATION becomes
    an ADDRESS only when its own statement marks it as one.  A bare line break
    ends an address unless an indicator introduced it and its PIN code has not
    been reached, so a list of cities stays separate; a name on a new line is
    never pulled into the address above it.
    """
    def _is_person_candidate(r: RecognizerResult) -> bool:
        return (
            r.entity_type == "PERSON"
            and r.recognition_metadata.get("recognizer_name") in _NER_RECOGNIZERS
            and _near_location_word(text, r.start)
        )

    def _is_mergeable(r: RecognizerResult) -> bool:
        if r.entity_type in _MERGEABLE_TYPES:
            return True
        if r.entity_type in _ADDRESS_CONTEXT_TYPES:
            return _has_address_indicator(text, r.start)
        return _is_person_candidate(r)

    def _promote_alone(r: RecognizerResult) -> bool:
        if r.entity_type == "NRP":
            # Left alone it could be dropped by filter_attributive_nrp.
            return True
        if r.entity_type == "ORGANIZATION":
            return (
                _indicator_in_statement(text, r.start)
                or _absorb_unit_number(text, r.start) != r.start
            )
        return False

    mergeable = sorted(
        [r for r in results if _is_mergeable(r)],
        key=lambda r: r.start,
    )
    others = [r for r in results if not _is_mergeable(r)]

    if not mergeable:
        return results

    def _gap_is_valid(
        first: RecognizerResult,
        prev: RecognizerResult,
        curr: RecognizerResult,
    ) -> bool:
        gap = text[prev.end : curr.start]
        if len(gap) > _MAX_MERGE_GAP or _has_sentence_break(
            text, prev.end, curr.start
        ):
            return False
        introduced = _has_address_indicator(text, first.start)
        crosses_line = "\n" in gap
        if _is_address_glue(gap):
            # Only separators and address words in between, so leaving them
            # out exposes nothing.
            return not crosses_line or (
                introduced and prev.entity_type != "IN_PIN_CODE"
            )
        if crosses_line and curr.entity_type == "PERSON":
            return False
        return (introduced or _has_address_indicator(text, prev.start)) and bool(
            _ADDRESS_LOOSE_GLUE.fullmatch(gap)
        )

    groups: list[list[RecognizerResult]] = [[mergeable[0]]]
    for r in mergeable[1:]:
        group = groups[-1]
        if _gap_is_valid(group[0], group[-1], r):
            group.append(r)
        else:
            groups.append([r])

    merged: list[RecognizerResult] = []
    for group in groups:
        if len(group) == 1 and not _promote_alone(group[0]):
            merged.append(group[0])
            continue
        start = group[0].start
        end = group[-1].end
        if _has_address_indicator(text, start) or len(group) > 1:
            in_group = {id(r) for r in group}
            taken = [r for r in results if id(r) not in in_group]
            start = _absorb_unit_designators(text, start, taken)
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


# ---------------------------------------------------------------------------
# ALL-CAPS recovery — case-normalise text so NER can be re-run over it
# ---------------------------------------------------------------------------

# Cased NER models (dslim/bert-base-NER and its ONNX derivatives) are trained on
# corpora where ALL-CAPS tokens are almost always organisations.  All-caps names
# are therefore out-of-distribution: they get mislabelled as ORGANIZATION /
# LOCATION, truncated to a single token, or missed entirely.  Re-casing those
# tokens restores the casing signal the model depends on.

# Acronyms that must keep their casing — title-casing them ("Ifsc", "Dob")
# invents word-like tokens the NER model may then mistake for names.  The engine
# extends this set with the YAML global allow-list at runtime.
_NEVER_NORMALIZE = frozenset({
    # Indian banking / payments
    "IFSC", "NEFT", "RTGS", "IMPS", "UPI", "NACH", "ECS", "EMI", "MICR",
    "NPCI", "RBI", "SBI", "HDFC", "ICICI", "IDBI", "PNB",
    # Identity / KYC
    "PAN", "KYC", "CKYC", "AADHAAR", "AADHAR", "GST", "GSTIN", "PRAN", "NPS",
    "APAAR", "CERSAI", "PFRDA", "CIF", "NRE", "NRO", "NRI", "TDS", "ITR",
    # Generic labels / units
    "DOB", "ID", "OTP", "PIN", "SMS", "ATM", "URL", "IP", "GPS", "SWIFT",
    "BIC", "SSN", "ITIN", "STR", "REF", "INR", "USD", "USA", "UK", "AM", "PM",
    "CEO", "CTO", "CFO", "COO", "VP", "HR", "IT",
})

_ALPHA_RUN = re.compile(r"[A-Za-z]+")


def _needs_recovery(text: str, extra_skip: frozenset[str]) -> bool:
    """Whether *text* contains anything a second NER pass could recover.

    True for any ALL-CAPS word, or a run of two or more consecutive lowercase
    words that are not ordinary vocabulary — the signature of an uncased name.
    Ordinary prose contains isolated lowercase words only, so it short-circuits
    here and never pays for the extra inference.
    """
    run = 0
    for match in _ALPHA_RUN.finditer(text):
        token = match.group(0)
        if len(token) < 2:
            continue
        upper = token.upper()
        if upper in _NEVER_NORMALIZE or upper in extra_skip:
            run = 0
            continue
        if token.isupper():
            return True
        if token.islower() and token not in _LOWERCASE_WORDS:
            run += 1
            if run >= 2:
                return True
        else:
            run = 0
    return False

_NER_ENTITY_TYPES = frozenset({"PERSON", "LOCATION", "ORGANIZATION", "NRP"})

# Title-casing *every* all-caps word turns a sentence into Title Case ("Signed
# The Form At Mumbai Branch"), which is itself out-of-distribution and hides the
# name boundaries NER relies on.  Common words are lowercased instead, which
# approximates true-casing closely enough for the model to locate names.
try:  # spaCy is a core dependency, but degrade gracefully if it is absent
    from spacy.lang.en.stop_words import STOP_WORDS as _SPACY_STOP_WORDS
except ImportError:  # pragma: no cover
    _SPACY_STOP_WORDS: frozenset[str] = frozenset()

# Domain vocabulary common in banking / KYC / support text.  Curated to EXCLUDE
# any plausible given name or surname (no "may", "mark", "bill", "grace", ...),
# because lowercasing a real name would hide it from NER.
_COMMON_DOCUMENT_WORDS = frozenset({
    # account / banking nouns
    "account", "accounts", "holder", "applicant", "customer", "branch", "bank",
    "statement", "cheque", "loan", "amount", "transfer", "payment", "payments",
    "balance", "deposit", "withdrawal", "interest", "charges", "refund",
    "funds", "insufficient", "credited", "debited", "savings", "nominee",
    "beneficiary", "transaction", "transactions", "instalment", "premium",
    # document / process nouns
    "form", "forms", "detail", "details", "record", "records", "document",
    "documents", "copy", "signature", "reference", "complaint", "request",
    "verification", "registration", "application", "relationship", "spouse",
    "address", "residence", "number", "date", "birth", "gender", "occupation",
    "employer", "income", "salary", "status", "purpose", "remarks", "subject",
    # verbs common in correspondence
    "signed", "issued", "bounced", "sanctioned", "residing", "called",
    "informed", "received", "submitted", "requested", "raised", "escalated",
    "escalate", "update", "updated", "verify", "confirmed", "approved",
    "rejected", "registered", "attached", "dated", "kindly", "regarding",
    "concerns", "concern", "immediately", "urgent", "pending", "active",
    "closed", "blocked", "manager", "officer", "executive", "department",
    # very common general vocabulary (kept lowercase so it is never mistaken
    # for a proper noun once re-cased)
    "today", "tomorrow", "yesterday", "morning", "evening", "night", "week",
    "month", "year", "day", "days", "time", "times", "date", "soon", "later",
    "met", "meet", "meeting", "sent", "send", "sending", "gave", "given",
    "want", "wants", "wanted", "need", "needs", "needed", "know", "knows",
    "told", "said", "says", "ask", "asked", "asking", "help", "helped",
    "user", "users", "people", "person", "team", "teams", "work", "working",
    "issue", "issues", "problem", "problems", "error", "errors", "wrong",
    "right", "correct", "incorrect", "invoice", "invoices", "order", "orders",
    "price", "cost", "costs", "total", "sum", "list", "note", "notes",
    "call", "called", "calling", "email", "emails", "phone", "mobile",
    "card", "cards", "credit", "debit", "cash", "money", "bill", "bills",
    "service", "services", "support", "customer", "client", "clients",
    "password", "login", "logout", "account", "accounts", "profile",
    "app", "application", "website", "page", "link", "message", "messages",
    "internet", "online", "offline", "system", "server", "network",
    "financial", "annual", "monthly", "weekly", "daily", "quarterly",
    "market", "branch", "office", "home", "house", "building",
})

_LOWERCASE_WORDS = frozenset(_SPACY_STOP_WORDS) | _COMMON_DOCUMENT_WORDS

# ---------------------------------------------------------------------------
# Mixed-case names — "Venkata narasimha raju"
# ---------------------------------------------------------------------------

# Part-of-speech tags that never belong to a person's name.  A lowercase word
# tagged with one of these is ordinary vocabulary ("paid", "bought", "from"),
# whereas uncased name parts come back as PROPN, NOUN or ADJ ("kumar",
# "raju", "narasimha").  Measured: in "Ramesh paid electricity bill" the
# re-cased "Ramesh Paid" is tagged PERSON, and only the tag on "paid" (VERB)
# tells it apart from "Rajesh kumar".
_ORDINARY_POS = frozenset({
    "VERB", "AUX", "ADP", "DET", "PRON", "CCONJ", "SCONJ", "PART", "ADV",
    "INTJ", "NUM", "SYM",
})

# Relation and possession words that trail a first name in informal text
# ("Kavitha mother is the joint holder", "Rahul laptop was stolen").  spaCy
# tags them NOUN exactly like the uncased surnames "sharma" and "raju", so they
# are listed.  Deliberately excludes words that are also Indian name parts
# ("devi", "mani", "baby").
_RELATION_WORDS = frozenset({
    "mother", "father", "mom", "dad", "mum", "papa", "mummy", "son", "daughter",
    "wife", "husband", "hubby", "brother", "sister", "bro", "sis", "uncle",
    "aunt", "aunty", "auntie", "cousin", "nephew", "niece", "grandfather",
    "grandmother", "grandpa", "grandma", "friend", "boss", "colleague",
    "neighbour", "neighbor", "landlord", "tenant", "fiance", "fiancee",
    "family", "kids", "children", "parents", "bhai", "bhaiya", "didi", "jiju",
    "bhabhi", "ji", "sahab", "saab", "laptop", "car", "bike", "scooter",
    "wallet", "purse", "bag", "wedding", "marriage", "birthday", "resume",
    "school", "college",
})

# Honorifics that are also common name parts ("Sunita kumari", "Ravi shankar
# pandit", "Venkata sri ram"); every other title is never part of a name.
_NAME_PART_TITLES = frozenset({"kumari", "kum", "pandit", "sri", "shri"})

# Letter runs with internal apostrophes, in any script: "d'souza", "martínez".
_NAME_TOKEN = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*")


def ordinary_word_starts(doc) -> frozenset[int] | None:
    """Start offsets of the tokens in a spaCy *doc* tagged as non-name words.

    ``None`` when the NLP pipeline produced no part-of-speech tags; the
    recovery steps then keep rejecting mixed-case spans, as they did before
    tags were used.
    """
    if doc is None or not doc.has_annotation("POS"):
        return None
    return frozenset(t.idx for t in doc if t.pos_ in _ORDINARY_POS)


def common_word_starts(
    doc, english_words: frozenset[str] | None
) -> frozenset[int]:
    """Start offsets of common English words in *doc* not tagged as proper nouns.

    *english_words* is the WordNet word list from spaCy's English lemmatizer.
    It is too broad to veto name parts outright — "lakshmi", "krishna" and
    "shah" are WordNet words too — so it only decides whether a single word
    next to a name is worth a second NER pass (``needs_name_recovery``).
    Without it, every word not tagged as a proper noun counts as common.
    """
    if doc is None or not doc.has_annotation("POS"):
        return frozenset()
    return frozenset(
        t.idx for t in doc
        if t.pos_ != "PROPN"
        and (
            english_words is None
            or t.lower_ in english_words
            or t.lemma_.lower() in english_words
        )
    )


def _is_uncased_name_word(token: str, start: int, ordinary: frozenset[int]) -> bool:
    """Whether a lowercase *token* at *start* could be an uncased name part."""
    return (
        len(token) >= 2
        and token.islower()
        and token not in _LOWERCASE_WORDS
        and token not in _RELATION_WORDS
        and (token not in _PERSON_TITLES or token in _NAME_PART_TITLES)
        and start not in ordinary
    )


def _name_runs(
    text: str, start: int, end: int, ordinary: frozenset[int]
) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` at ordinary lowercase words.

    Returns the pieces that contain at least one uncased name word — the only
    part of a mixed-case span the recovery pass can add, since the correctly
    cased words were already judged by the primary pass.  Words are letter runs
    in any script with internal apostrophes ("d'souza", "martínez"), and
    single letters ("r" in "Venkata r raju") stay inside a piece unless they
    are ordinary words such as "a".
    """
    runs: list[tuple[int, int]] = []
    run_start = run_end = None
    has_name = False
    for match in _NAME_TOKEN.finditer(text, start, end):
        token = match.group(0)
        single_letter = len(token) == 1 and (
            text[match.end() : match.end() + 1] == "." or token not in _LOWERCASE_WORDS
        )
        if (
            token.islower()
            and not single_letter
            and not _is_uncased_name_word(token, match.start(), ordinary)
        ):
            if run_start is not None and has_name:
                runs.append((run_start, run_end))
            run_start = run_end = None
            has_name = False
            continue
        if run_start is None:
            run_start = match.start()
        run_end = match.end()
        has_name = has_name or (token.islower() and not single_letter)
    if run_start is not None and has_name:
        runs.append((run_start, run_end))
    return runs


def _is_word_char(text: str, i: int) -> bool:
    return 0 <= i < len(text) and (text[i].isalnum() or text[i] == "_")


def _word_after(text: str, pos: int) -> tuple[str, int] | None:
    """The word one space or tab after *pos*, with its start offset."""
    if text[pos : pos + 1] not in (" ", "\t"):
        return None
    match = _ALPHA_RUN.match(text, pos + 1)
    if match is None or _is_word_char(text, match.end()):
        return None
    return match.group(0), match.start()


def _word_before(text: str, pos: int) -> tuple[str, int] | None:
    """The word one space or tab before *pos*, with its start offset."""
    end = pos - 1
    if end < 1 or text[end] not in " \t":
        return None
    start = end
    while start > 0 and text[start - 1].isascii() and text[start - 1].isalpha():
        start -= 1
    if start == end or _is_word_char(text, start - 1):
        return None
    return text[start:end], start


def needs_name_recovery(
    results: list[RecognizerResult],
    text: str,
    ordinary: frozenset[int] | None,
    common: frozenset[int] = frozenset(),
) -> bool:
    """Whether an NER entity sits directly next to an uncased name word.

    "Rajesh Kumar sharma submitted" has only one uncased word, too few for
    ``_needs_recovery`` to pay for a second NER pass, but "sharma" right after
    a detected name is the likely surname, so recovery is worth running.  A
    common English word (*common*, see ``common_word_starts``) is not: "Kavitha
    mother is the joint holder" must not re-case "mother" into a name.
    """
    if ordinary is None:
        return False
    for r in results:
        if r.entity_type not in _NER_ENTITY_TYPES:
            continue
        for neighbour in (_word_after(text, r.end), _word_before(text, r.start)):
            if (
                neighbour
                and neighbour[1] not in common
                and _is_uncased_name_word(*neighbour, ordinary)
            ):
                return True
    return False


def _recovery_allowed(span: str, entity_type: str) -> bool:
    """Whether a span recovered from re-cased text may keep *entity_type*.

    Decided by how the span was cased **originally**:

    * ALL-CAPS — word boundaries survive, so every NER type is trustworthy.
    * all-lowercase — no capitalisation cue at all, so the model readily
      invents organisations out of ordinary noun phrases ("Internet Banking
      Password", "Credit Team").  Only PERSON is accepted.
    * mixed — the correctly cased words were already judged by the primary
      pass, so re-casing ordinary words only adds noise, e.g. "Kavitha visited
      Contoso Bank" fusing into one PERSON.  Rejected here; for a PERSON,
      ``merge_recovered_results`` may still keep the uncased name part
      ("narasimha raju" in "Venkata narasimha raju").
    """
    has_upper = any(c.isupper() for c in span)
    has_lower = any(c.islower() for c in span)
    if has_upper and not has_lower:
        return True
    if has_lower and not has_upper:
        return entity_type == "PERSON"
    return False


def normalize_case(
    text: str,
    extra_skip: frozenset[str] = frozenset(),
    force: bool = False,
) -> str | None:
    """Return *text* with badly-cased words re-cased, or ``None`` if unchanged.

    Both ALL-CAPS and all-lowercase words are rewritten, since a cased NER model
    reads either as out-of-distribution.  Likely proper nouns are title-cased
    and common words are lowercased, approximating true-casing.  MixedCase
    tokens are left alone — their casing is already meaningful.

    Lowercase rewriting applies wherever an uncased name is plausible, but the
    caller gates what may be recovered from it — see ``_recovery_allowed``.

    The result is **exactly the same length** as *text*, so entity offsets found
    in it map 1:1 back onto the original.  Matching is restricted to ASCII
    ``[A-Za-z]`` because some Unicode letters change length when lowercased
    (``"\u0130".lower()`` is two characters), which would corrupt those offsets.

    Tokens adjacent to a digit are left untouched so structured identifiers such
    as PAN (``ABCPS1234K``) and IFSC (``SBIN0001234``) survive intact — this
    matters because ``InPanImprovedRecognizer`` matches case-sensitively.

    Returning ``None`` when nothing changed lets callers skip the second NER
    pass entirely for normally-cased text.  *force* rewrites the text even when
    ``_needs_recovery`` sees nothing to recover — see ``needs_name_recovery``.
    """
    if not force and not _needs_recovery(text, extra_skip):
        return None
    chars: list[str] | None = None
    for match in _ALPHA_RUN.finditer(text):
        token = match.group(0)
        if len(token) < 2 or not (token.isupper() or token.islower()):
            continue
        upper = token.upper()
        if upper in _NEVER_NORMALIZE or upper in extra_skip:
            continue
        start, end = match.span()
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if before.isdigit() or after.isdigit():
            continue
        lowered = token.lower()
        recased = (
            lowered if lowered in _LOWERCASE_WORDS
            else lowered[0].upper() + lowered[1:]
        )
        if recased == token:
            continue
        if chars is None:
            chars = list(text)
        chars[start:end] = recased
    return "".join(chars) if chars is not None else None


def _key_value_span(text: str, start: int, end: int) -> tuple[int, int]:
    """The span of ``text[start:end]`` together with its key, if it has one.

    For a value written as "uid=987654321098" or "aadhaar_no:987654321098"
    the key and its separator are included; otherwise the span is returned
    as it is.
    """
    if text[start - 1 : start] not in ("=", ":"):
        return start, end
    begin = key_start(text, start - 1)
    return (start - 1 if begin is None else begin), end


def _parts_outside(
    r: RecognizerResult, text: str, cuts: list[tuple[int, int]]
) -> list[RecognizerResult]:
    """The pieces of *r* outside *cuts*, as results of the same type.

    Each piece is trimmed to start and end with a letter or digit, and pieces
    without a letter are dropped.
    """
    spans = []
    pos = r.start
    for cut_start, cut_end in sorted(cuts):
        if cut_start > pos:
            spans.append((pos, cut_start))
        pos = max(pos, cut_end)
    if pos < r.end:
        spans.append((pos, r.end))

    parts = []
    for start, end in spans:
        while start < end and not text[start].isalnum():
            start += 1
        while end > start and not text[end - 1].isalnum():
            end -= 1
        if any(c.isalpha() for c in text[start:end]):
            parts.append(RecognizerResult(
                entity_type=r.entity_type,
                start=start,
                end=end,
                score=r.score,
                analysis_explanation=r.analysis_explanation,
                recognition_metadata=dict(r.recognition_metadata or {}),
            ))
    return parts


def _key_value_cuts(
    r: RecognizerResult, text: str, validated: list[tuple[int, int]]
) -> list[tuple[int, int]] | None:
    """The key=value pairs to cut out of a recovered result *r*.

    Each validated entity *r* overlaps must be the value of a key=value pair
    ("uid=987654321098"); the pair's span is returned for each.  None when *r*
    overlaps any other validated entity, so that it is dropped as before.
    """
    cuts = []
    for v_start, v_end in validated:
        if not (r.start < v_end and r.end > v_start):
            continue
        if text[v_start - 1 : v_start] not in ("=", ":"):
            return None
        cuts.append(_key_value_span(text, v_start, v_end))
    return cuts


def _cut(
    r: RecognizerResult, text: str, cuts: list[tuple[int, int]]
) -> list[RecognizerResult]:
    """*r* without the *cuts* it overlaps; *r* itself when it overlaps none."""
    if any(r.start < end and r.end > start for start, end in cuts):
        return _parts_outside(r, text, cuts)
    return [r]


def remove_allowed_overlaps(
    results: list[RecognizerResult],
    text: str,
    allowed_spans: list[tuple[int, int]],
) -> list[RecognizerResult]:
    """Remove what overlaps an allow-listed entity, which stays unmasked.

    A pattern entity overlapping one is dropped (the URL inside an allowed
    email address).  An NER span instead keeps its parts outside the allowed
    entity and that entity's key: NER covers whole spaCy tokens, and a name
    written against a key=value pair ("Rahul Sharma&account=12345678901")
    shares its token with the value, so dropping the span would expose it.
    """
    kept: list[RecognizerResult] = []
    for r in results:
        overlapping = [(s, e) for s, e in allowed_spans if r.start < e and r.end > s]
        if not overlapping:
            kept.append(r)
        elif r.entity_type in _NER_ENTITY_TYPES:
            cuts = [_key_value_span(text, s, e) for s, e in overlapping]
            kept.extend(_parts_outside(r, text, cuts))
    return kept


def merge_recovered_results(
    primary: list[RecognizerResult],
    recovered: list[RecognizerResult],
    text: str,
    ordinary: frozenset[int] | None = None,
) -> list[RecognizerResult]:
    """Fold NER results recovered from re-cased text into *primary*.

    Recovered spans win over overlapping NER spans from the primary pass —
    those are precisely the mislabelled or truncated detections this recovery
    exists to replace.  Non-NER entities (PAN, phone, email, ...) always win,
    because they come from validated recognizers run against the untouched
    original text.

    Spans whose original text was all-lowercase are accepted only as PERSON.
    Measured on lowercase text, every spurious recovery was ORGANIZATION or
    NRP ("Internet Banking Password", "Credit Team"), while the genuine gain
    was entirely PERSON — all-caps input still carries word-boundary cues that
    lowercase lacks, so it stays eligible for the full set of NER types.

    Mixed-case spans are accepted only as PERSON, and only the part holding an
    uncased name word: "Venkata narasimha raju" is kept whole, while "Ramesh
    paid" is cut at the verb and adds nothing.  *ordinary* holds the offsets
    of words that cannot be name parts (``ordinary_word_starts``); without it,
    mixed-case spans are rejected outright.  A lowercase entity the recovery
    split off right after an accepted name ("Suresh babu" + "naidu") is taken
    as its surname.  Such pieces are widened over every primary NER span they
    overlap, so they never mask less than the primary pass did ("Anil d'souza"
    stays whole even if only "souza" survives the cut).

    A recovered span that runs into the value of a key=value pair loses only
    that pair: NER expands a tag to whole spaCy tokens, and a key=value pair is
    one token, so "user=rahul uid=987654321098" comes back as "rahul
    uid=987654321098" and must still mask "rahul" once the Aadhaar number is
    found.  Whether the span is accepted is decided before the pair is cut,
    exactly as when the value went undetected, and the parts left are widened
    like the pieces above.  A span overlapping any other validated entity is
    dropped.
    """
    validated = [
        (r.start, r.end) for r in primary if r.entity_type not in _NER_ENTITY_TYPES
    ]

    accepted: list[RecognizerResult] = []
    pieces: list[RecognizerResult] = []
    leftovers: list[RecognizerResult] = []
    for found in recovered:
        cuts = _key_value_cuts(found, text, validated)
        if cuts is None:
            continue
        if _recovery_allowed(text[found.start : found.end], found.entity_type):
            spans, are_pieces = [found], False
        elif found.entity_type == "PERSON" and ordinary is not None:
            spans = [
                RecognizerResult(
                    entity_type="PERSON",
                    start=start,
                    end=end,
                    score=found.score,
                    analysis_explanation=found.analysis_explanation,
                    recognition_metadata=dict(found.recognition_metadata or {}),
                )
                for start, end in _name_runs(text, found.start, found.end, ordinary)
            ]
            are_pieces = True
        else:
            leftovers.extend(_cut(found, text, cuts))
            continue
        for span in spans:
            parts = _cut(span, text, cuts)
            accepted.extend(parts)
            if are_pieces or not (len(parts) == 1 and parts[0] is span):
                pieces.extend(parts)

    if ordinary is not None:
        persons = sorted(
            (a for a in accepted if a.entity_type == "PERSON"), key=lambda a: a.start
        )
        for person in persons:
            for r in sorted(leftovers, key=lambda r: r.start):
                if (
                    text[person.end : r.start] in (" ", "\t")
                    and text[r.start : r.end].islower()
                    and _name_runs(text, r.start, r.end, ordinary) == [(r.start, r.end)]
                ):
                    person.end = r.end
                    if all(person is not p for p in pieces):
                        pieces.append(person)

    primary_ner = [r for r in primary if r.entity_type in _NER_ENTITY_TYPES]
    for piece in pieces:
        widened = True
        while widened:
            widened = False
            for r in primary_ner:
                if (
                    r.start < piece.end
                    and r.end > piece.start
                    and (r.start < piece.start or r.end > piece.end)
                ):
                    piece.start = min(piece.start, r.start)
                    piece.end = max(piece.end, r.end)
                    widened = True

    if not accepted:
        return primary
    kept = [
        r for r in primary
        if r.entity_type not in _NER_ENTITY_TYPES
        or not any(r.start < a.end and r.end > a.start for a in accepted)
    ]
    return kept + accepted


# ---------------------------------------------------------------------------
# Attributive NRP suppression
# ---------------------------------------------------------------------------

# NRP (nationality / religious / political group) is personal data only when it
# describes a person.  The NER model emits it for any demonym, so aggregate
# business language — "South Indian branches", "Indian banking sector", "South
# Indian SME customers" — was redacted even though it identifies no one.
#
# The distinguishing signal is what the demonym modifies.  Used attributively
# in front of a thing or a market segment it is descriptive, not personal; used
# predicatively ("the customer is Indian") or in front of a singular person noun
# ("a Muslim woman") it describes an individual and stays.

# Singular nouns denoting one human being.  Plurals are deliberately absent:
# "South Indian customers" is a segment, whereas "a South Indian customer" is a
# person, and that distinction is exactly what separates the two cases.
_PERSON_HEAD_NOUNS = frozenset({
    # generic humans
    "man", "woman", "gentleman", "lady", "boy", "girl", "person", "individual",
    "male", "female", "child", "adult", "senior", "citizen", "resident",
    "national", "expatriate", "immigrant", "migrant", "native",
    # banking / legal roles
    "customer", "client", "applicant", "borrower", "nominee", "holder",
    "depositor", "guardian", "heir", "spouse", "employee", "member",
    "student", "patient", "tenant", "landlord", "director", "partner",
    "proprietor", "beneficiary", "signatory", "witness", "guarantor",
    "trustee", "executor", "payee", "remitter", "subscriber", "investor",
    "shareholder", "taxpayer", "pensioner", "retiree", "candidate",
    "complainant", "petitioner", "defendant", "accused",
    # family
    "father", "mother", "son", "daughter", "husband", "wife", "brother",
    "sister", "parent", "widow", "widower",
    # religious / community roles — an NRP directly qualifies these
    "devotee", "follower", "believer", "worshipper", "priest", "monk", "nun",
    "pilgrim", "convert", "cleric", "scholar", "elder",
})

_NRP_WORD = re.compile(r"[A-Za-z][A-Za-z\-']*")

# How many words after the entity are inspected for a person noun.
_NRP_LOOKAHEAD_WORDS = 4

# A copula immediately before the demonym makes the whole phrase a predicate
# describing the subject — "the borrower is a Punjabi farmer" is about a person
# whatever noun follows.  Without this, the forward scan would have to enumerate
# every occupation (farmer, speaker, businessman, landowner, weaver, ...), which
# is unbounded, and a suffix rule would misfire on business nouns such as
# "sector" and "quarter".
_COPULA_BEFORE = re.compile(
    r"(?i)\b(?:is|are|was|were|am|be|been|being|remains?|becomes?)\s+"
    r"(?:an?|the)?\s*$"
)


def _in_predicate_position(text: str, start: int) -> bool:
    """True if the demonym is the complement of a copula."""
    return bool(_COPULA_BEFORE.search(text[max(0, start - 40) : start]))


def _modifies_a_person(text: str, start: int, end: int) -> bool:
    """Whether the demonym at [start, end) describes an individual.

    Predicate position always does: the complement describes the subject.
    Otherwise the immediately following run of content words is scanned, up to
    the first function word or punctuation — the clause boundary.  An empty run
    means the demonym was not used attributively at all, which again leaves it
    describing the subject.
    """
    if _in_predicate_position(text, start):
        return True
    cursor = end
    for _ in range(_NRP_LOOKAHEAD_WORDS):
        match = _NRP_WORD.search(text, cursor)
        if match is None:
            break
        gap = text[cursor : match.start()]
        if any(c in ".,;:!?()[]{}\"'\n" for c in gap):
            break
        word = match.group(0).lower()
        if word in _SPACY_STOP_WORDS:
            break
        if word in _PERSON_HEAD_NOUNS:
            return True
        cursor = match.end()
    return cursor == end


def filter_attributive_nrp(
    results: list[RecognizerResult],
    text: str,
) -> list[RecognizerResult]:
    """Drop NRP entities that describe a thing rather than a person.

    Runs after address merging so that a building name promoted to ADDRESS is
    unaffected, and only ever removes NRP — every other entity type is passed
    through untouched.
    """
    return [
        r for r in results
        if r.entity_type != "NRP" or _modifies_a_person(text, r.start, r.end)
    ]
