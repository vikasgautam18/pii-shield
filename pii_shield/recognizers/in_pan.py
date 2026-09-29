"""Custom Presidio recognizer for Indian PAN (Permanent Account Number).

Format: ``AAAAA9999A`` — 10 characters:
  - 5 uppercase letters
  - 4 digits
  - 1 uppercase letter (checksum letter)

The **4th character** encodes the holder type and is restricted to a known set:
  P=Individual, C=Company, H=HUF, F=Firm/LLP, A=AOP, T=Trust, B=Body of
  Individuals, L=Local Authority, J=Artificial Juridical Person, G=Government.

The 5th character is the first letter of the holder's surname / entity name.

This custom recognizer improves on Presidio's built-in ``InPanRecognizer`` by:
  - validating the 4th (holder-type) character for a high-confidence match,
    which sharply reduces false positives on random 5-letter/4-digit strings;
  - keeping a lower-confidence generic pattern (context-boosted) so lowercase
    or slightly malformed PANs are still caught.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# Valid 4th-character holder-type codes.
_HOLDER_TYPES = "ABCFGHJLPT"

# High confidence: uppercase, valid holder-type 4th char. e.g. ABCPS7234F
_PATTERN_VALIDATED = Pattern(
    name="in_pan_validated",
    regex=rf"\b[A-Z]{{3}}[{_HOLDER_TYPES}][A-Z]\d{{4}}[A-Z]\b",
    score=0.85,
)

# Generic 5-letters/4-digits/1-letter, either case. Context boosts this.
_PATTERN_GENERIC = Pattern(
    name="in_pan_generic",
    regex=r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b",
    score=0.4,
)

_CONTEXT = [
    "pan",
    "permanent account number",
    "pan card",
    "pan no",
    "pan number",
    "income tax",
    "tax",
    "taxpayer",
    "itd",
    "kyc",
]

# Case-sensitive matching (re.MULTILINE | re.DOTALL, i.e. WITHOUT re.IGNORECASE)
# so the validated pattern only fires on a properly uppercased PAN. Presidio
# defaults to IGNORECASE, which would otherwise let lowercase PANs score high.
_CASE_SENSITIVE_FLAGS = 24


class InPanImprovedRecognizer(PatternRecognizer):
    """Detects Indian PAN (Permanent Account Number) values in text."""

    ENTITIES = ["IN_PAN"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_PAN",
            supported_language="en",
            patterns=[_PATTERN_VALIDATED, _PATTERN_GENERIC],
            context=_CONTEXT,
            global_regex_flags=_CASE_SENSITIVE_FLAGS,
        )
