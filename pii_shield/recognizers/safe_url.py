"""URL recognizer that a long run of dots cannot stall.

Presidio 2.2.362's URL patterns backtrack quadratically on a long run of host-name characters that
contains dots: about 2 s for 1,000 dots and 9 s for 2,000 on a fast laptop, and a chain like "a.a.a."
behaves the same.  Dotted leader lines in forms and tables of contents ("Name ..........") and OCR noise
trigger it.  Presidio 2.2.363 only softens this, so the text is neutralised before it is scanned, and
every offset stays valid because nothing is added or removed.

The dots are replaced by "_", not by a space: "_" is not a host-name character, so the host scan can no
longer run along the whole chain, yet the path or query of a URL still runs straight through it.  A space
would end the match and leave the rest of a URL such as ``?token=<long.dotted.token>`` unmasked.
"""

import re

from presidio_analyzer.predefined_recognizers import UrlRecognizer

# Four or more dots in a row never occur in a host name: leader lines, ellipses, OCR noise.
_DOT_RUN = re.compile(r"\.{4,}")

# No host name is longer than 253 characters, so the dots of a longer run of host characters are
# never the dots of one.
_LONG_HOST_RUN = re.compile(r"[A-Za-z0-9.\-]{254,}")

_FILLER = "_"


def neutralise_dot_runs(text: str) -> str:
    """A same-length copy of *text* whose stall-prone dots are underscores."""
    text = _DOT_RUN.sub(lambda m: _FILLER * len(m.group()), text)
    return _LONG_HOST_RUN.sub(lambda m: m.group().replace(".", _FILLER), text)


class SafeUrlRecognizer(UrlRecognizer):
    """``UrlRecognizer`` scanning a dot-neutralised copy of the text; results map 1:1 onto the original."""

    def __init__(self, **kwargs) -> None:
        # Keep the built-in name so DISABLED_RECOGNIZERS and context overrides still match it.
        kwargs.setdefault("name", "UrlRecognizer")
        super().__init__(**kwargs)

    def analyze(self, text, entities=None, nlp_artifacts=None, regex_flags=None):
        return super().analyze(neutralise_dot_runs(text), entities, nlp_artifacts, regex_flags)
