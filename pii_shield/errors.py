"""Typed exception hierarchy for PII Shield.

These exceptions let a host application distinguish failure classes and
implement a **fail-closed** policy — e.g. block an LLM call when
anonymization fails rather than silently forwarding raw PII.

All PII Shield errors derive from :class:`PiiShieldError`, so callers can
catch the whole family with a single ``except PiiShieldError``.
"""

from __future__ import annotations


class PiiShieldError(Exception):
    """Base class for all PII Shield errors."""


class InvalidInputError(PiiShieldError):
    """Raised when input arguments are invalid (e.g. non-string text)."""


class DetectionError(PiiShieldError):
    """Raised when PII detection/anonymization fails internally.

    Wraps lower-level NLP/analyzer failures so a host wrapper can treat any
    detection failure as a single, catchable condition and fail closed.
    """


class DeanonymizationError(PiiShieldError):
    """Raised when restoring original values from a mapping fails."""


class SessionStoreError(PiiShieldError):
    """Raised when a conversation/session store operation fails."""
