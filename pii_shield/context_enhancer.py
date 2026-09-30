"""Context enhancement that also reads the key of a ``key=value`` pair.

Presidio raises a recognizer's score when one of its context words is among
the words around a match, and reads those words from spaCy's tokens.  spaCy
keeps a URL, and a ``key=value`` pair written without spaces, as one token, so
in "https://api.com?aadhaar=987654321098" or "aadhaar:987654321098" the key is
never a word of its own.  An Aadhaar number written without separators scores
too low to be kept without its context word, so it leaked.
"""

import re
from bisect import bisect_right

from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer

# Characters a key may hold: "aadhaar", "aadhaar_no", "customer.pan", "acct-no".
_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)

_MAX_KEY_LENGTH = 40

# The words of a key: "aadhaar_no" -> "aadhaar", "no"; "aadhaarNumber" ->
# "aadhaar", "Number"; "PAN_NO" -> "PAN", "NO".
_KEY_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def key_start(text: str, separator: int) -> int | None:
    """Start of the key that ends at the "=" or ":" at ``text[separator]``.

    None when the run of key characters before it is implausibly long for a
    key.  The key may be empty (``separator`` itself is returned).
    """
    begin = separator
    while begin > 0 and text[begin - 1] in _KEY_CHARS:
        begin -= 1
        if separator - begin > _MAX_KEY_LENGTH:
            return None
    return begin


def key_words_before(token: str, offset: int) -> list[str]:
    """Lowercased words of the key written directly before *offset* in *token*.

    The value must follow the key's "=" or ":" directly, as in
    "?aadhaar_no=987654321098"; otherwise, or when the key is implausibly
    long, there is no key and the result is empty.
    """
    if offset <= 0 or token[offset - 1] not in "=:":
        return []
    begin = key_start(token, offset - 1)
    if begin is None:
        return []
    return [word.lower() for word in _KEY_WORD.findall(token[begin : offset - 1])]


class KeyValueContextEnhancer(LemmaContextAwareEnhancer):
    """Also counts the key of a ``key=value`` pair written inside one token.

    Only a match that starts inside a token gains words, and only the key
    directly before it, so "&pan=" is context for the PAN after it but not for
    an Aadhaar number elsewhere in the same URL.  The words are matched against
    each recognizer's own context list and boost the score by the same amount
    as any other context word.
    """

    def _extract_surrounding_words(self, nlp_artifacts, word: str, start: int) -> list[str]:
        words = super()._extract_surrounding_words(nlp_artifacts, word, start)
        return list(words) + _key_words_in_token(nlp_artifacts, start)


def _key_words_in_token(nlp_artifacts, start: int) -> list[str]:
    """Key words in the same token as a match starting at *start*."""
    indices = nlp_artifacts.tokens_indices or []
    i = bisect_right(indices, start) - 1
    if i < 0:
        return []
    token = str(nlp_artifacts.tokens[i])
    token_start = indices[i]
    if token_start < start < token_start + len(token):
        return key_words_before(token, start - token_start)
    return []
