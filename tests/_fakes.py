"""Shared test doubles for the library-mode (middleware) test suite.

``FakeEngine`` provides deterministic, model-free PII "detection" while reusing
the *real* placeholder-assignment / text-building / de-anonymization logic from
:class:`~pii_shield.engine.PiiShieldEngine`, so stateful conversation tests
exercise the genuine algorithm without loading an NLP model.
"""

from __future__ import annotations

from pii_shield.engine import PiiShieldEngine


class FakeResult:
    """Minimal stand-in for a Presidio ``RecognizerResult``."""

    def __init__(self, entity_type: str, start: int, end: int, score: float = 0.9):
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = score
        self.recognition_metadata: dict = {}


class FakeEngine:
    """Deterministic engine stub.

    Parameters
    ----------
    detections :
        List of ``(entity_type, substring)`` pairs.  Every occurrence of each
        substring found in a text is reported as an entity.
    """

    def __init__(self, detections: list[tuple[str, str]]):
        self._detections = detections

    def _run_pipeline(
        self,
        text,
        language="en",
        allow_list=None,
        entity_type_allow_list=None,
        entity_keyword_allow_list=None,
        score_threshold=None,
        entity_type_include_list=None,
    ):
        allow = set(allow_list or [])
        et_allow = set(entity_type_allow_list or [])
        results: list[FakeResult] = []
        for etype, sub in self._detections:
            if etype in et_allow or not sub:
                continue
            start = text.find(sub)
            while start != -1:
                if sub not in allow:
                    results.append(FakeResult(etype, start, start + len(sub)))
                start = text.find(sub, start + len(sub))
        # Naive longest-first overlap removal (mirrors engine intent).
        results.sort(key=lambda r: (-(r.end - r.start), r.start))
        kept: list[FakeResult] = []
        for r in results:
            if not any(r.start < k.end and r.end > k.start for k in kept):
                kept.append(r)
        if entity_type_include_list is not None:
            kept = [r for r in kept if r.entity_type in entity_type_include_list]
        return kept

    # Reuse the REAL logic under test (these methods don't touch the analyzer).
    _assign_replacements = PiiShieldEngine._assign_replacements
    _build_text = staticmethod(PiiShieldEngine._build_text)
    deanonymize = PiiShieldEngine.deanonymize
