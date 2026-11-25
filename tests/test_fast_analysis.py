"""The fast paths must return exactly what Presidio's own code returns.

Presidio's ``EntityRecognizer.remove_duplicates`` and ``LemmaContextAwareEnhancer`` are quadratic in the number
of matches; ``pii_shield.fast_analysis`` and ``KeyValueContextEnhancer`` replace them.  Every test here runs the
original beside the replacement on random inputs and compares the output element by element.
"""

import random

import pytest
import spacy
from presidio_analyzer import AnalysisExplanation, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.entity_recognizer import EntityRecognizer
from presidio_analyzer.nlp_engine import NlpArtifacts

from pii_shield import fast_analysis
from pii_shield.context_enhancer import KeyValueContextEnhancer, _key_words_in_token
from pii_shield.fast_analysis import fast_remove_duplicates

TYPES = ("PERSON", "IN_PAN", "PHONE_NUMBER")


def _signature(results):
    return [(r.entity_type, r.start, r.end, r.score) for r in results]


class TestRemoveDuplicates:
    @pytest.mark.parametrize("seed", range(60))
    def test_matches_presidio_on_random_results(self, seed):
        rnd = random.Random(seed)
        results = []
        for _ in range(rnd.randint(0, 70)):
            start = rnd.randint(0, 40)
            results.append(
                RecognizerResult(
                    entity_type=rnd.choice(TYPES),
                    start=start,
                    end=start + rnd.randint(1, 25),
                    score=rnd.choice((0, 0.3, 0.35, 0.5, 0.85, 1.0)),
                )
            )
        expected = fast_analysis._presidio_remove_duplicates(list(results))
        assert _signature(fast_remove_duplicates(list(results))) == _signature(expected)

    def test_contained_result_of_another_type_survives(self):
        wide = RecognizerResult("PERSON", 0, 20, 0.9)
        inner_same = RecognizerResult("PERSON", 5, 10, 0.8)
        inner_other = RecognizerResult("IN_PAN", 5, 10, 0.8)
        kept = fast_remove_duplicates([inner_same, wide, inner_other])
        assert _signature(kept) == _signature([wide, inner_other])

    def test_identical_results_collapse_and_zero_scores_are_dropped(self):
        a = RecognizerResult("PERSON", 0, 5, 0.9)
        b = RecognizerResult("PERSON", 0, 5, 0.9)
        zero = RecognizerResult("PERSON", 10, 15, 0)
        assert _signature(fast_remove_duplicates([a, b, zero])) == [("PERSON", 0, 5, 0.9)]

    def test_install_replaces_and_uninstall_restores_presidio_s_function(self):
        try:
            assert fast_analysis.install() is True
            assert EntityRecognizer.remove_duplicates is fast_remove_duplicates
        finally:
            fast_analysis.uninstall()
        assert EntityRecognizer.remove_duplicates is fast_analysis._presidio_remove_duplicates

    def test_the_switch_and_an_unchecked_presidio_turn_it_off(self, monkeypatch):
        monkeypatch.setenv("FAST_ANALYSIS", "false")
        assert fast_analysis.enabled() is False
        assert fast_analysis.install() is False
        monkeypatch.setenv("FAST_ANALYSIS", "true")
        assert fast_analysis.enabled() is True
        monkeypatch.setattr(fast_analysis, "_presidio_fits", lambda: False)
        assert fast_analysis.enabled() is False


class _Reference(LemmaContextAwareEnhancer):
    """The enhancer as it was before the fast paths: Presidio's, plus the key words of a key=value pair."""

    def _extract_surrounding_words(self, nlp_artifacts, word, start):
        return list(super()._extract_surrounding_words(nlp_artifacts, word, start)) + _key_words_in_token(
            nlp_artifacts, start
        )


class _Engine:
    """The two questions NlpArtifacts asks of an NLP engine when it picks its keywords."""

    @staticmethod
    def is_stopword(word, language):
        return word in {"the", "of", "is", "a", "to"}

    @staticmethod
    def is_punct(word, language):
        return not any(c.isalnum() for c in word)


WORDS = [
    "account", "number", "pan", "card", "aadhaar", "uid", "phone", "mobile", "the", "of", "is", "to", "customer",
    "ABCPS1234K", "9876543210", "987654321098", "pan=ABCPS1234K", "uid=987654321098", "aadhaar:987654321098",
    "https://x.test/?pan=ABCPS1234K&uid=1", "a/c", "Rajesh", "Kumar", "-", ",", "mobile:9876543210", "name=Rajesh",
]


def _artifacts(rnd):
    text = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 90)))
    doc = spacy.blank("en")(text)
    for token in doc:
        token.lemma_ = token.text.lower()
    artifacts = NlpArtifacts(
        entities=[],
        tokens=doc,
        tokens_indices=[t.idx for t in doc],
        lemmas=[t.lemma_ for t in doc],
        nlp_engine=_Engine(),
        language="en",
    )
    return text, artifacts


def _recognizers():
    contexts = (["pan", "card"], ["aadhaar", "uid", "number"], ["phone", "mobile"], [])
    return [
        PatternRecognizer(f"TYPE_{i}", patterns=[Pattern("p", r"\d+", 0.3)], context=context)
        for i, context in enumerate(contexts)
    ]


def _results(rnd, text, recognizers):
    results = []
    for _ in range(rnd.randint(0, 40)):
        start = rnd.randint(0, max(0, len(text) - 1))
        recognizer = rnd.choice(recognizers + [None])
        metadata = None
        if recognizer is not None:
            metadata = {RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: recognizer.id}
            if rnd.random() < 0.1:
                metadata[RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY] = True
        elif rnd.random() < 0.5:
            metadata = {RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: "unknown-recognizer"}
        score = rnd.choice((0.0, 0.01, 0.3, 0.5, 0.85, 0.99, 1.0))
        results.append(
            RecognizerResult(
                entity_type=rnd.choice(TYPES),
                start=start,
                end=min(len(text), start + rnd.randint(1, 15)),
                score=score,
                analysis_explanation=AnalysisExplanation(recognizer="r", original_score=score),
                recognition_metadata=metadata,
            )
        )
    return results


def _outcome(results):
    return [
        (
            r.entity_type,
            r.start,
            r.end,
            r.score,
            r.analysis_explanation.supportive_context_word,
            r.analysis_explanation.score,
            r.analysis_explanation.original_score,
            dict(r.recognition_metadata or {}),
        )
        for r in results
    ]


class TestContextEnhancer:
    @pytest.mark.parametrize("mode", ("substring", "whole_word"))
    @pytest.mark.parametrize("seed", range(40))
    def test_matches_presidio_on_random_documents(self, seed, mode):
        rnd = random.Random(seed)
        text, artifacts = _artifacts(rnd)
        recognizers = _recognizers()
        raw = _results(rnd, text, recognizers)
        context = rnd.choice((None, [], ["Card", "uid"]))
        arguments = dict(
            context_similarity_factor=0.45, min_score_with_context_similarity=0.4, context_prefix_count=5,
            context_suffix_count=5, context_matching_mode=mode,
        )
        before = _outcome(raw)
        expected = _Reference(**arguments).enhance_using_context(text, raw, artifacts, recognizers, context)
        actual = KeyValueContextEnhancer(**arguments).enhance_using_context(text, raw, artifacts, recognizers, context)
        assert _outcome(actual) == _outcome(expected)
        assert _outcome(raw) == before, "the results passed in are never modified"
        assert any(a is not r for a, r in zip(actual, raw)) or not raw

    def test_missing_artifacts_return_copies_like_presidio(self):
        raw = [RecognizerResult("PERSON", 0, 4, 0.5, AnalysisExplanation("r", 0.5), {"recognizer_identifier": "x"})]
        out = KeyValueContextEnhancer().enhance_using_context("text", raw, None, [], None)
        assert _outcome(out) == _outcome(raw) and out[0] is not raw[0]

    def test_a_match_beyond_the_last_token_is_an_error_in_both(self):
        rnd = random.Random(1)
        text, artifacts = _artifacts(rnd)
        recognizers = _recognizers()
        beyond = RecognizerResult(
            "PERSON", len(text) + 5, len(text) + 9, 0.5, AnalysisExplanation("r", 0.5),
            {RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: recognizers[0].id},
        )
        with pytest.raises(ValueError):
            _Reference().enhance_using_context(text, [beyond], artifacts, recognizers, None)
        with pytest.raises(ValueError):
            KeyValueContextEnhancer().enhance_using_context(text, [beyond], artifacts, recognizers, None)

    def test_switching_the_fast_path_off_gives_the_same_scores(self, monkeypatch):
        rnd = random.Random(7)
        text, artifacts = _artifacts(rnd)
        recognizers = _recognizers()
        raw = _results(rnd, text, recognizers)
        fast = KeyValueContextEnhancer().enhance_using_context(text, raw, artifacts, recognizers, None)
        monkeypatch.setenv("FAST_ANALYSIS", "false")
        slow = KeyValueContextEnhancer().enhance_using_context(text, raw, artifacts, recognizers, None)
        assert _outcome(fast) == _outcome(slow)
