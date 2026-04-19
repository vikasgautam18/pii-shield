"""Custom Presidio recognizer for Indian CKYC (Central KYC) numbers.

Format: 14-digit numeric code issued by CERSAI (Central Registry of
Securitisation, Asset Reconstruction and Security Interest of India).

Variants:
  - Standard:  14 consecutive digits (e.g. ``10002345678901``)
  - Prefixed:  ``L`` / ``S`` / ``O`` prefix + 14 digits for simplified-measures,
    small-account, and OTP-based eKYC accounts respectively.

Context keywords such as *ckyc*, *central kyc*, *cersai*, *kin*, and
*kyc identifier* boost detection confidence.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# Standard 14-digit CKYC number
_PATTERN_STANDARD = Pattern(
    name="in_ckyc_standard",
    regex=r"\b\d{14}\b",
    score=0.3,
)

# Prefixed CKYC (L = simplified, S = small account, O = OTP eKYC)
_PATTERN_PREFIXED = Pattern(
    name="in_ckyc_prefixed",
    regex=r"\b[LSO]\d{14}\b",
    score=0.5,
)


class InCkycRecognizer(PatternRecognizer):
    """Detects Indian CKYC (Central KYC Identifier) numbers in text."""

    ENTITIES = ["IN_CKYC"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_CKYC",
            supported_language="en",
            patterns=[_PATTERN_PREFIXED, _PATTERN_STANDARD],
        )
