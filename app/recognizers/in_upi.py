"""Custom Presidio recognizer for Indian UPI IDs (Virtual Payment Addresses).

Format: username@handle
  - Username: alphanumeric + dots, 2–256 chars
  - Handle: NPCI-approved bank/PSP suffix (e.g. ybl, oksbi, paytm)

UPI IDs look similar to email addresses but use a closed set of
bank/payment-provider handles instead of domain names with TLDs.
"""

from presidio_analyzer import Pattern, PatternRecognizer

# Comprehensive list of known NPCI-approved UPI handles (bank + PSP + app).
# Update periodically as NPCI approves new handles.
_UPI_HANDLES = (
    # BHIM / NPCI
    "upi",
    # PhonePe (Yes Bank, ICICI, Axis)
    "ybl", "ibl", "axl",
    # Google Pay
    "okhdfcbank", "oksbi", "okicici", "okaxis",
    # Paytm
    "paytm", "ptm", "ptyes", "ptaxis", "ptsbi", "pthdfc",
    # Amazon Pay
    "apl", "yapl", "rapl",
    # Major banks
    "sbi", "icici", "hdfcbank", "axisbank", "kotak", "yesbank",
    "pnb", "boi", "unionbank", "canarabank", "indianbank",
    "iob", "idbi", "rbl", "aubank", "federal", "indus",
    "hsbc", "dbs", "sc", "citi", "deutsche", "barb",
    # Fintech / wallets
    "freecharge", "airtel", "jio", "ikwik", "abfspay",
    "fam", "yesfam", "sliceaxis",
    # CRED
    "axisb",
    # Others
    "jupiteraxis", "tapicici", "waaxis", "wahdfcbank",
    "nsdl", "kbl", "kvb", "dlb", "tmb", "dcb", "equitas",
    "bandhan", "idfc", "idfcfirst",
)

_HANDLES_PATTERN = "|".join(_UPI_HANDLES)

# Full UPI ID pattern: username@handle (no TLD after handle).
# Negative lookahead (?!\.\w) prevents matching emails like user@sbi.co.in
_PATTERN_UPI = Pattern(
    name="in_upi_id",
    regex=rf"\b[a-zA-Z0-9][a-zA-Z0-9._]{{0,255}}@(?:{_HANDLES_PATTERN})\b(?!\.\w)",
    score=0.7,
)

_CONTEXT_WORDS = [
    "upi",
    "vpa",
    "pay",
    "payment",
    "transfer",
    "send",
    "receive",
    "gpay",
    "google pay",
    "phonepe",
    "paytm",
    "bhim",
    "wallet",
    "merchant",
    "qr",
    "scan",
    "collect",
    "request",
]


class InUpiIdRecognizer(PatternRecognizer):
    """Detects Indian UPI IDs (Virtual Payment Addresses) in text."""

    ENTITIES = ["IN_UPI_ID"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_UPI_ID",
            supported_language="en",
            patterns=[_PATTERN_UPI],
            context=_CONTEXT_WORDS,
        )
