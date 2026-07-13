"""Tests for pii_shield.streaming.StreamingDeanonymizer."""

import pytest

from pii_shield.errors import PiiShieldError
from pii_shield.streaming import StreamingDeanonymizer


def _feed_all(sd, chunks):
    out = "".join(sd.feed(c) for c in chunks)
    return out + sd.flush()


def test_no_split_whole_string():
    sd = StreamingDeanonymizer({"{{PERSON_1}}": "Rahul Sharma"})
    assert _feed_all(sd, ["hello {{PERSON_1}} bye"]) == "hello Rahul Sharma bye"


def test_placeholder_split_across_chunks_no_leak():
    sd = StreamingDeanonymizer({"{{PERSON_1}}": "Rahul Sharma"})
    # Split right in the middle of the placeholder token.
    emitted = []
    emitted.append(sd.feed("Contact {{PER"))
    # Nothing containing the partial token should have been emitted yet.
    assert "{{PER" not in "".join(emitted)
    emitted.append(sd.feed("SON_1}} now"))
    emitted.append(sd.flush())
    full = "".join(emitted)
    assert full == "Contact Rahul Sharma now"
    assert "{{" not in full  # no placeholder leaked


def test_char_by_char_streaming():
    mapping = {"{{PERSON_1}}": "Rahul", "{{EMAIL_ADDRESS_1}}": "r@x.com"}
    sd = StreamingDeanonymizer(mapping)
    text = "Hi {{PERSON_1}}, mail {{EMAIL_ADDRESS_1}}!"
    out = _feed_all(sd, list(text))
    assert out == "Hi Rahul, mail r@x.com!"
    assert "{{" not in out


def test_longest_first_no_prefix_clobber():
    mapping = {"{{PERSON_1}}": "Rahul", "{{PERSON_10}}": "Priya"}
    sd = StreamingDeanonymizer(mapping)
    out = _feed_all(sd, ["{{PERSON_10}} and {{PERSON_1}}"])
    assert out == "Priya and Rahul"


def test_trailing_partial_flushed_as_is():
    # An incomplete token that never completes is emitted verbatim on flush.
    sd = StreamingDeanonymizer({"{{PERSON_1}}": "Rahul"})
    out = _feed_all(sd, ["dangling {{PER"])
    assert out == "dangling {{PER"


def test_empty_mapping_passthrough():
    sd = StreamingDeanonymizer({})
    assert _feed_all(sd, ["nothing to ", "restore"]) == "nothing to restore"


def test_encrypt_and_hash_mappings_restored():
    sd = StreamingDeanonymizer(
        {"{{PERSON_1}}": "Rahul"},
        hash_mapping={"deadbeef": "secret-hashed"},
        encrypt_mapping={"cipherZ": "secret-enc"},
    )
    out = _feed_all(sd, ["a {{PERSON_1}} b cipherZ c deadbeef"])
    assert out == "a Rahul b secret-enc c secret-hashed"


def test_feed_after_flush_raises():
    sd = StreamingDeanonymizer({"{{PERSON_1}}": "Rahul"})
    sd.flush()
    with pytest.raises(PiiShieldError):
        sd.feed("more")


def test_context_manager_flushes():
    sd = StreamingDeanonymizer({"{{PERSON_1}}": "Rahul"})
    with sd:
        first = sd.feed("hi {{PERSON_1}}")
    # After context exit the stream is closed.
    with pytest.raises(PiiShieldError):
        sd.feed("x")
    assert "{{" not in first or first == "hi "  # partial safely held before exit
