"""Tests for pii_shield.errors and pii_shield.observability.emit."""

from pii_shield.errors import (
    DeanonymizationError,
    DetectionError,
    InvalidInputError,
    PiiShieldError,
    SessionStoreError,
)
from pii_shield.observability import Event, emit


def test_error_hierarchy():
    for exc in (
        DetectionError,
        InvalidInputError,
        DeanonymizationError,
        SessionStoreError,
    ):
        assert issubclass(exc, PiiShieldError)
    assert issubclass(PiiShieldError, Exception)


def test_emit_none_hook_is_noop():
    # Should not raise.
    emit(None, Event(name="anonymize"))


def test_emit_invokes_hook():
    captured = []
    emit(lambda e: captured.append(e), Event(name="anonymize", entity_count=3))
    assert len(captured) == 1
    assert captured[0].entity_count == 3


def test_emit_swallows_hook_errors():
    def boom(_event):
        raise RuntimeError("telemetry down")

    # Telemetry must never break the hot path.
    emit(boom, Event(name="deanonymize"))
