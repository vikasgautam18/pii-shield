"""Line-boundary helpers shared by the recognizers and the pipeline.

In forms, lists and chat messages every line is its own statement, so the
words on one line say nothing about an entity on another.  The exception is a
line that introduces the one below it: a label ending in ":" ("Aadhaar
number:"), or a heading that ends in the keyword being looked for
("Correspondence Address").
"""

import re

# Regex class for whitespace other than a line break, for patterns that must
# not look past the end of the current line.
INLINE_SPACE = r"[^\S\r\n]"

# Metadata flag set by the context-only recognizers (APAAR, PRAN, Customer ID)
# when the keyword that raised their score is not on the number's own line.
CONTEXT_OFF_LINE_KEY = "context_off_line"


def line_start(text: str, pos: int) -> int:
    """Index of the first character of the line containing *pos*."""
    return text.rfind("\n", 0, pos) + 1


def line_end(text: str, pos: int) -> int:
    """Index of the line break ending the line that contains *pos*."""
    end = text.find("\n", pos)
    return len(text) if end == -1 else end


def block_start(
    text: str, pos: int, introducer: re.Pattern[str] | None = None
) -> int:
    """Start of *pos*'s line, or of the line above it when that line introduces it.

    The line above introduces *pos*'s line when it is a label ending in ":" or,
    given an *introducer* pattern, when it ends with a match of that pattern —
    the heading "Correspondence Address" introduces the address below it.
    """
    start = line_start(text, pos)
    if start:
        above = line_start(text, start - 1)
        line = text[above : start - 1].rstrip()
        if line.endswith(":") or (
            introducer is not None and _ends_with(line, introducer)
        ):
            return above
    return start


def _ends_with(line: str, pattern: re.Pattern[str]) -> bool:
    """Whether *line* ends with a match of *pattern*, ignoring a trailing dash."""
    stripped = line.rstrip(" \t-\u2013\u2014")
    return any(m.end() == len(stripped) for m in pattern.finditer(stripped))
