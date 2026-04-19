"""Custom Presidio operator that replaces PII with format-preserving fake data.

Generates structurally valid but fictitious values for each entity type.
Uses only Python stdlib (random, string) — zero external dependencies.
"""

import random
import string
from typing import Dict

from presidio_anonymizer.operators import Operator, OperatorType

# Common Indian bank codes used in IFSC prefixes
_BANK_CODES = [
    "HDFC", "ICIC", "SBIN", "PNB", "AXIS", "BOI", "IOBA", "KARB",
    "IDFB", "YESB", "UTIB", "CNRB", "BARB", "VIJB", "UBIN", "PUNB",
]

# Fictitious two-letter codes for fake DLs — deliberately NOT valid
# Indian state/UT codes, so generated numbers can never collide with real ones.
_FAKE_STATE_CODES = [
    "XX", "ZZ", "QQ", "XA", "XB", "XC", "XD", "XE", "XF", "XG",
    "ZA", "ZB", "ZC", "ZD", "ZE", "ZF", "QA", "QB", "QC", "QD",
]

# UPI PSP handles
_UPI_HANDLES = ["ybl", "oksbi", "okhdfcbank", "paytm", "axl", "sbi", "icici"]

# Sample first / last names for PERSON fakes
_FIRST_NAMES = [
    "Aarav", "Aditi", "Amit", "Ananya", "Arjun", "Deepa", "Gaurav",
    "Isha", "Kiran", "Meera", "Neha", "Priya", "Rahul", "Ravi",
    "Sanjay", "Sneha", "Suresh", "Tanvi", "Vikram", "Zara",
]
_LAST_NAMES = [
    "Agarwal", "Bhat", "Choudhury", "Desai", "Gupta", "Iyer",
    "Joshi", "Kumar", "Mehta", "Nair", "Patel", "Rao", "Reddy",
    "Shah", "Sharma", "Singh", "Srinivasan", "Thakur", "Verma",
]

# Sample city/location names for LOCATION fakes
_LOCATIONS = [
    "Agra", "Ahmedabad", "Bengaluru", "Bhopal", "Chennai", "Coimbatore",
    "Delhi", "Goa", "Gurgaon", "Hyderabad", "Indore", "Jaipur",
    "Kochi", "Kolkata", "Lucknow", "Mumbai", "Mysuru", "Nagpur",
    "Noida", "Pune", "Surat", "Trivandrum", "Udaipur", "Varanasi",
]


class FakeDataOperator(Operator):
    """Replace PII with structurally valid fake values."""

    _MAX_RETRIES = 20

    def operate(self, text: str = None, params: Dict = None) -> str:
        entity_type = params.get("entity_type", "") if params else ""
        generator = _GENERATORS.get(entity_type, _generic_format_preserve)
        original = text or ""
        for _ in range(self._MAX_RETRIES):
            fake = generator(original)
            if fake != original:
                return fake
        # Extremely unlikely fallback — append a distinguishing suffix
        return fake + "0" if fake == original else fake

    def validate(self, params: Dict = None) -> None:
        pass

    def operator_name(self) -> str:
        return "fake_data"

    def operator_type(self) -> OperatorType:
        return OperatorType.Anonymize


# ---------------------------------------------------------------------------
# Per-entity generators
# ---------------------------------------------------------------------------

def _fake_aadhaar(original: str) -> str:
    """Generate fake Aadhaar preserving separator style (space/hyphen/none)."""
    digits = [str(random.randint(2, 9))] + [
        str(random.randint(0, 9)) for _ in range(11)
    ]
    if " " in original:
        return f"{''.join(digits[:4])} {''.join(digits[4:8])} {''.join(digits[8:])}"
    if "-" in original:
        return f"{''.join(digits[:4])}-{''.join(digits[4:8])}-{''.join(digits[8:])}"
    return "".join(digits)


def _fake_pan(_original: str) -> str:
    """Generate fake PAN: AAAAA9999A."""
    first_three = "".join(random.choices(string.ascii_uppercase, k=3))
    status = random.choice(["P", "C", "T", "H", "F", "A"])
    fifth = random.choice(string.ascii_uppercase)
    digits = "".join(random.choices(string.digits, k=4))
    check = random.choice(string.ascii_uppercase)
    return f"{first_three}{status}{fifth}{digits}{check}"


def _fake_driving_license(original: str) -> str:
    """Generate fake Indian driving licence preserving separator style."""
    state = random.choice(_FAKE_STATE_CODES)
    two_digits = str(random.randint(10, 99))
    year = str(random.randint(1990, 2025))
    unique = "".join(random.choices(string.digits, k=7))
    # Detect separator: space, hyphen, or none
    stripped = original.strip()
    if " " in stripped:
        return f"{state} {two_digits} {year} {unique}"
    if "-" in stripped:
        return f"{state}-{two_digits}-{year}-{unique}"
    return f"{state}{two_digits}{year}{unique}"


def _fake_phone(original: str) -> str:
    """Generate fake Indian phone number preserving prefix style."""
    if original.startswith("+91"):
        first = str(random.randint(7, 9))
        rest = "".join(random.choices(string.digits, k=9))
        # Preserve spacing after +91 if present
        if len(original) > 3 and original[3] in (" ", "-"):
            return f"+91{original[3]}{first}{rest}"
        return f"+91{first}{rest}"
    if original.startswith("0"):
        return "0" + "".join(
            random.choices(string.digits, k=max(len(original) - 1, 9))
        )
    first = str(random.randint(7, 9))
    rest = "".join(random.choices(string.digits, k=9))
    return first + rest


def _fake_upi(_original: str) -> str:
    """Generate fake UPI ID: username@handle."""
    username = "".join(random.choices(string.ascii_lowercase, k=random.randint(5, 10)))
    handle = random.choice(_UPI_HANDLES)
    return f"{username}@{handle}"


def _fake_pin_code(_original: str) -> str:
    """Generate fake Indian PIN code (6 digits, first digit 1-9)."""
    return str(random.randint(1, 9)) + "".join(
        random.choices(string.digits, k=5)
    )


def _fake_credit_card(_original: str) -> str:
    """Generate fake 16-digit credit card number."""
    return "".join(random.choices(string.digits, k=16))


def _fake_email(original: str) -> str:
    """Generate fake email preserving domain structure."""
    domains = ["example.com", "test.org", "sample.net", "demo.io", "mail.test"]
    user = "".join(random.choices(string.ascii_lowercase, k=random.randint(5, 10)))
    if "@" in original:
        # Preserve TLD structure
        return f"{user}@{random.choice(domains)}"
    return f"{user}@{random.choice(domains)}"


def _fake_person(_original: str) -> str:
    """Generate fake Indian name."""
    return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"


def _fake_location(_original: str) -> str:
    """Generate fake Indian city/location name."""
    return random.choice(_LOCATIONS)


def _fake_ifsc(_original: str) -> str:
    """Generate fake IFSC code: XXXX0NNNNNN."""
    bank = random.choice(_BANK_CODES)
    branch = "".join(random.choices(string.digits, k=6))
    return f"{bank}0{branch}"


def _fake_bank_account(_original: str) -> str:
    """Generate fake bank account number (11-16 digits)."""
    length = random.choice([11, 12, 14, 15, 16])
    return "".join(random.choices(string.digits, k=length))


def _generic_format_preserve(text: str) -> str:
    """Fallback: replace each char with a same-class random char."""
    result = []
    for c in text:
        if c.isdigit():
            result.append(random.choice(string.digits))
        elif c.isupper():
            result.append(random.choice(string.ascii_uppercase))
        elif c.islower():
            result.append(random.choice(string.ascii_lowercase))
        else:
            result.append(c)
    return "".join(result)


# Entity type → generator mapping
_GENERATORS = {
    "IN_AADHAAR": _fake_aadhaar,
    "IN_PAN": _fake_pan,
    "IN_DRIVING_LICENSE": _fake_driving_license,
    "PHONE_NUMBER": _fake_phone,
    "IN_UPI_ID": _fake_upi,
    "IN_PIN_CODE": _fake_pin_code,
    "CREDIT_CARD": _fake_credit_card,
    "EMAIL_ADDRESS": _fake_email,
    "PERSON": _fake_person,
    "LOCATION": _fake_location,
    "DATE_TIME": _generic_format_preserve,
    "ADDRESS": _generic_format_preserve,
    "IN_IFSC": _fake_ifsc,
    "IN_BANK_ACCOUNT": _fake_bank_account,
}
