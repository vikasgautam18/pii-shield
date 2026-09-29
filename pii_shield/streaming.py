"""Streaming-safe de-anonymization for token-by-token LLM output.

:func:`~pii_shield.engine.PiiShieldEngine.deanonymize` operates on a *complete*
string.  When an LLM response is streamed, a placeholder such as
``{{PERSON_1}}`` can be split across two chunks (``{{PER`` + ``SON_1}}``); a
naive per-chunk ``str.replace`` would emit a broken/unresolved placeholder to
the end user and never restore the value.

:class:`StreamingDeanonymizer` solves this by buffering a **prefix-aware tail**:
it only emits text that cannot contain a partial token, holding back the
smallest trailing suffix that is a proper prefix of some mapping key until more
input arrives (or :meth:`flush` is called at end of stream).
"""

from __future__ import annotations

from pii_shield.errors import PiiShieldError


class StreamingDeanonymizer:
    """Incrementally restore PII in a streamed response without leaking split tokens.

    Parameters
    ----------
    entity_mapping :
        Placeholder / fake value → original PII.
    hash_mapping, encrypt_mapping :
        Optional additional token → original PII mappings to also restore.

    Usage::

        sd = conv.streaming_deanonymizer()
        for chunk in llm_stream:
            user_visible = sd.feed(chunk)   # emit as it becomes safe
            ...
        user_visible += sd.flush()          # drain the final tail
    """

    def __init__(
        self,
        entity_mapping: dict[str, str],
        hash_mapping: dict[str, str] | None = None,
        encrypt_mapping: dict[str, str] | None = None,
    ) -> None:
        merged: dict[str, str] = {}
        merged.update(entity_mapping or {})
        if encrypt_mapping:
            merged.update(encrypt_mapping)
        if hash_mapping:
            merged.update(hash_mapping)
        self._mapping = merged
        # Longest-first so e.g. {{PERSON_10}} is restored before {{PERSON_1}}.
        self._keys_sorted = sorted(merged, key=len, reverse=True)
        self._maxlen = max((len(k) for k in merged), default=0)
        self._buffer = ""
        self._closed = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore(self, text: str) -> str:
        """Replace every complete token in *text* (longest-first)."""
        for key in self._keys_sorted:
            if key in text:
                text = text.replace(key, self._mapping[key])
        return text

    def _holdback(self) -> int:
        """Return how many trailing chars to withhold as a possible partial token.

        The largest ``j`` (``1 <= j < maxlen``) for which the buffer's last ``j``
        characters form a *proper prefix* of some mapping key — i.e. a token that
        might still be completing on the next chunk.
        """
        buf = self._buffer
        max_j = min(self._maxlen - 1, len(buf))
        for j in range(max_j, 0, -1):
            suffix = buf[-j:]
            for key in self._keys_sorted:
                if len(key) > j and key.startswith(suffix):
                    return j
        return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> str:
        """Ingest *chunk*, returning any text that is now safe to emit."""
        if self._closed:
            raise PiiShieldError("Cannot feed() a StreamingDeanonymizer after flush().")
        if not chunk:
            return ""
        self._buffer += chunk
        if self._maxlen == 0:
            emitted, self._buffer = self._buffer, ""
            return emitted
        hold = self._holdback()
        cut = len(self._buffer) - hold
        emit_region = self._buffer[:cut]
        self._buffer = self._buffer[cut:]
        return self._restore(emit_region)

    def flush(self) -> str:
        """Restore and return the remaining buffered text; closes the stream."""
        self._closed = True
        out = self._restore(self._buffer)
        self._buffer = ""
        return out

    def __enter__(self) -> "StreamingDeanonymizer":
        return self

    def __exit__(self, *exc) -> None:
        # Best-effort close; callers should capture flush() output explicitly.
        if not self._closed:
            self.flush()
