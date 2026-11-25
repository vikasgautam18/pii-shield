"""Times Presidio's duplicate removal as the ``dedup`` detail of the ``anonymize()`` call in progress.

``AnalyzerEngine.analyze()`` ends with ``EntityRecognizer.remove_duplicates``, and so does every pattern
recognizer on its own matches (some 30 times for one request).  Presidio gives no hook there, and the call
is made on the class, so the function is replaced by one that adds its duration to the recorder of the
running call (``pii_shield.observability.current_recorder``) and returns exactly what Presidio's function
returns.  Outside a call the recorder is the no-op one: the cost is two clock reads.

``install()`` is idempotent and keeps Presidio's function reachable as ``__wrapped__``; however often it runs
(or the module is reloaded) the replacement wraps Presidio's own function, never an earlier replacement.
"""

import functools
import logging
import time

from presidio_analyzer.entity_recognizer import EntityRecognizer

from pii_shield.observability import current_recorder

logger = logging.getLogger("pii-shield")

_MARK = "_pii_shield_dedup_timed"


def _timed(remove_duplicates):
    """*remove_duplicates* that adds its duration to the ``dedup`` detail of the step that is running."""

    @functools.wraps(remove_duplicates)
    def timed(results):
        started = time.perf_counter()
        try:
            return remove_duplicates(results)
        finally:
            current_recorder().add_detail("dedup", (time.perf_counter() - started) * 1000.0)

    setattr(timed, _MARK, True)
    return timed


def install() -> bool:
    """Put the timed ``remove_duplicates`` in place of Presidio's; returns whether it is in place.

    A Presidio without ``EntityRecognizer.remove_duplicates`` is left alone: the ``dedup`` detail is then missing,
    and detection is not affected.
    """
    current = getattr(EntityRecognizer, "remove_duplicates", None)
    if current is None:
        logger.warning("Presidio has no EntityRecognizer.remove_duplicates; duplicate removal is not timed")
        return False
    if not getattr(current, _MARK, False):
        EntityRecognizer.remove_duplicates = staticmethod(_timed(current))
    return True
