"""Context enhancement that also reads the key of a ``key=value`` pair.

Presidio raises a recognizer's score when one of its context words is among
the words around a match, and reads those words from spaCy's tokens.  spaCy
keeps a URL, and a ``key=value`` pair written without spaces, as one token, so
in "https://api.com?aadhaar=987654321098" or "aadhaar:987654321098" the key is
never a word of its own.  An Aadhaar number written without separators scores
too low to be kept without its context word, so it leaked.
"""

import copy
import re
import time
from bisect import bisect_right

from presidio_analyzer import RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.context_aware_enhancers.context_aware_enhancer import ContextAwareEnhancer

from pii_shield import fast_analysis
from pii_shield.observability import current_recorder

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

    Presidio's enhancer deep-copies every result and, per result, scans the token
    list for its position and a keyword *list* for each neighbouring lemma, so it
    grows with the square of the matches in a dense document.  Here the results
    are copied shallowly and the lookups are a bisection and set membership; the
    scores and explanations are exactly Presidio's (``pii_shield.fast_analysis``).
    """

    def enhance_using_context(self, text, raw_results, nlp_artifacts, recognizers, context=None):
        started = time.perf_counter()
        try:
            return self._enhance(text, raw_results, nlp_artifacts, recognizers, context)
        finally:
            current_recorder().add_detail("context_enhance", (time.perf_counter() - started) * 1000.0)

    def _enhance(self, text, raw_results, nlp_artifacts, recognizers, context):
        """``enhance_using_context`` itself, so that both of its paths are timed by one wrapper."""
        if not fast_analysis.enabled():
            return super().enhance_using_context(text, raw_results, nlp_artifacts, recognizers, context)
        results = [_copy_result(r) for r in raw_results]
        recognizers_by_id = {recognizer.id: recognizer for recognizer in recognizers}
        context = [word.lower() for word in context] if context else []
        if nlp_artifacts is None:
            return results
        for result in results:
            metadata = result.recognition_metadata
            recognizer = None
            if metadata and RecognizerResult.RECOGNIZER_IDENTIFIER_KEY in metadata:
                recognizer = recognizers_by_id.get(metadata[RecognizerResult.RECOGNIZER_IDENTIFIER_KEY])
            if not recognizer or not recognizer.context:
                continue
            if metadata.get(RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY):
                continue
            surrounding_words = self._extract_surrounding_words(
                nlp_artifacts=nlp_artifacts, word=text[result.start : result.end], start=result.start
            )
            surrounding_words.extend(context)
            supportive_word = self._find_supportive_word_in_context(
                surrounding_words, recognizer.context, self.context_matching_mode
            )
            if supportive_word != "":
                result.score += self.context_similarity_factor
                result.score = max(result.score, self.min_score_with_context_similarity)
                result.score = min(result.score, ContextAwareEnhancer.MAX_SCORE)
                result.analysis_explanation.set_supportive_context_word(supportive_word)
                result.analysis_explanation.set_improved_score(result.score)
        return results

    def _extract_surrounding_words(self, nlp_artifacts, word: str, start: int) -> list[str]:
        if fast_analysis.enabled():
            words = self._surrounding_words(nlp_artifacts, word, start)
        else:
            words = super()._extract_surrounding_words(nlp_artifacts, word, start)
        return list(words) + _key_words_in_token(nlp_artifacts, start)

    def _surrounding_words(self, nlp_artifacts, word: str, start: int) -> list[str]:
        """``LemmaContextAwareEnhancer._extract_surrounding_words`` without the linear scans."""
        if not nlp_artifacts.tokens:
            return [""]
        ends, keywords = _artifact_lookups(nlp_artifacts)
        match = bisect_right(ends, start)  # the first token that ends after the match starts
        if match >= len(ends):
            raise ValueError(
                "Did not find word '" + word + "' in the list of tokens although it is expected to be found"
            )
        lemmas = nlp_artifacts.lemmas
        words: list[str] = []
        for step, count in ((-1, self.context_prefix_count), (1, self.context_suffix_count)):
            i, remaining = match, count + 1
            while 0 <= i < len(lemmas) and remaining > 0:
                lemma = lemmas[i].lower()
                if lemma in keywords:
                    words.append(lemma)
                    remaining -= 1
                i += step
        return list(set(words))


def _copy_result(result: RecognizerResult) -> RecognizerResult:
    """A copy the enhancer may update: the score, the metadata dict and the explanation are its own."""
    copied = copy.copy(result)
    if result.recognition_metadata is not None:
        copied.recognition_metadata = dict(result.recognition_metadata)
    if result.analysis_explanation is not None:
        copied.analysis_explanation = copy.copy(result.analysis_explanation)
    return copied


def _artifact_lookups(nlp_artifacts):
    """The token end offsets and the keyword set of one text, computed once per set of artifacts."""
    cached = getattr(nlp_artifacts, "_pii_shield_lookups", None)
    if cached is None:
        ends = [index + len(token) for index, token in zip(nlp_artifacts.tokens_indices, nlp_artifacts.tokens)]
        cached = (ends, frozenset(nlp_artifacts.keywords))
        nlp_artifacts._pii_shield_lookups = cached
    return cached


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
