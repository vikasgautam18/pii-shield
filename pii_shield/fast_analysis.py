"""Linear-time stand-ins for the two steps of Presidio's ``analyze()`` that grow quadratically with matches.

* ``EntityRecognizer.remove_duplicates`` compares every candidate with every result kept so far, and every
  recognizer and the analyzer itself call it.
* ``LemmaContextAwareEnhancer`` deep-copies every result and, for each one, scans the token list for its
  position and the keyword *list* for each neighbouring lemma (see ``pii_shield.context_enhancer``).

Documents dense in matches (statements, logs, tables, JSON) and deployments with many recognizers produce
thousands of raw matches, and these two steps then cost as much as a third of a request.  Both replacements
return exactly what Presidio's code returns, in the same order; ``tests/test_fast_analysis.py`` checks that
against Presidio's own implementation on random inputs.  They apply to the Presidio version they were checked
against (``requirements.txt`` pins it) and can be switched off with ``FAST_ANALYSIS=false``.

``install()`` also times duplicate removal, whichever implementation runs, as the ``dedup`` detail of the
``anonymize()`` call in progress (``pii_shield.observability``).
"""

import functools
import logging
import os
import time
from bisect import bisect_left, bisect_right
from importlib import metadata

from presidio_analyzer import RecognizerResult
from presidio_analyzer.entity_recognizer import EntityRecognizer

from pii_shield.observability import current_recorder

logger = logging.getLogger("pii-shield")

CHECKED_PRESIDIO_VERSION = "2.2.362"

_presidio_remove_duplicates = EntityRecognizer.remove_duplicates


@functools.lru_cache(maxsize=1)
def _presidio_fits() -> bool:
    try:
        return metadata.version("presidio-analyzer") == CHECKED_PRESIDIO_VERSION
    except metadata.PackageNotFoundError:
        return False


def enabled() -> bool:
    """Whether the fast paths are on (``FAST_ANALYSIS``, default true) and fit the installed Presidio."""
    return os.getenv("FAST_ANALYSIS", "true").strip().lower() == "true" and _presidio_fits()


def fast_remove_duplicates(results: list[RecognizerResult]) -> list[RecognizerResult]:
    """``EntityRecognizer.remove_duplicates`` in O(N log N).

    Presidio keeps a result unless an earlier-ranked result of the same entity type contains it (ranking:
    score descending, then start, then length descending).  Containment is transitive, so it does not matter
    whether the container was itself kept; this keeps, per entity type, the largest end seen among the
    results ranked so far for every start up to the candidate's in a Fenwick tree.
    """
    results = list(set(results))
    results.sort(key=lambda x: (-x.score, x.start, -(x.end - x.start)))
    by_type: dict[str, list[RecognizerResult]] = {}
    for result in results:
        if result.score == 0:
            continue
        by_type.setdefault(result.entity_type, []).append(result)

    kept: set[int] = set()
    for ranked in by_type.values():
        starts = sorted({r.start for r in ranked})
        tree = [-1] * (len(starts) + 1)
        for result in ranked:
            i = bisect_right(starts, result.start)
            largest_end = -1
            while i > 0:
                largest_end = max(largest_end, tree[i])
                i -= i & -i
            if largest_end >= result.end:
                continue
            kept.add(id(result))
            i = bisect_left(starts, result.start) + 1
            while i <= len(starts):
                tree[i] = max(tree[i], result.end)
                i += i & -i
    return [r for r in results if id(r) in kept]


def _timed(remove_duplicates):
    """*remove_duplicates* that adds its duration to the ``dedup`` detail of the running ``anonymize()`` call.

    Duplicate removal runs once per pattern recognizer and once more at the end of ``analyze()``, some 34 times
    for one request, so the hook is two clock reads and one dict update, and does nothing outside a call.
    """

    @functools.wraps(remove_duplicates)
    def timed(results):
        started = time.perf_counter()
        try:
            return remove_duplicates(results)
        finally:
            current_recorder().add_detail("dedup", (time.perf_counter() - started) * 1000.0)

    return timed


# Built once from the original functions, never from what is installed, so installing again never stacks wrappers.
_timed_fast_remove_duplicates = _timed(fast_remove_duplicates)
_timed_presidio_remove_duplicates = _timed(_presidio_remove_duplicates)


def install() -> bool:
    """Put a timed ``remove_duplicates`` in place of Presidio's (idempotent).

    It runs the linear-time version when the fast paths are on, Presidio's own when they are off or do not fit,
    so ``analyze.dedup`` is measured either way.  Returns whether the fast paths are on.
    """
    fast = enabled()
    if not fast:
        logger.info("Fast analysis paths are off (FAST_ANALYSIS=false or an unchecked Presidio version)")
    EntityRecognizer.remove_duplicates = staticmethod(
        _timed_fast_remove_duplicates if fast else _timed_presidio_remove_duplicates
    )
    return fast


def uninstall() -> None:
    EntityRecognizer.remove_duplicates = staticmethod(_presidio_remove_duplicates)
