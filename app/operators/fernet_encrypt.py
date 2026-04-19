"""Custom Presidio operator that encrypts text using Fernet (AES-128-CBC + HMAC-SHA256)."""

import base64
import os
from typing import Dict

from cryptography.fernet import Fernet
from presidio_anonymizer.operators import Operator, OperatorType


def _get_fernet() -> Fernet:
    """Return a Fernet instance using the configured encryption key."""
    key = os.getenv("PII_SHIELD_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "PII_SHIELD_ENCRYPTION_KEY environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def decrypt(token: str) -> str:
    """Decrypt a Fernet token back to the original plaintext string."""
    f = _get_fernet()
    return f.decrypt(token.encode()).decode()


def is_fernet_token(token: str) -> bool:
    """Check if a string looks like a Fernet token (version byte 0x80)."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        return len(raw) >= 1 and raw[0] == 0x80
    except Exception:
        return False


class FernetEncryptOperator(Operator):
    """Encrypt text using Fernet (AES-128-CBC + HMAC-SHA256) and return the token."""

    def operate(self, text: str = None, params: Dict = None) -> str:
        f = _get_fernet()
        return f.encrypt(text.encode()).decode()

    def validate(self, params: Dict = None) -> None:
        pass

    def operator_name(self) -> str:
        return "fernet_encrypt"

    def operator_type(self) -> OperatorType:
        return OperatorType.Anonymize
