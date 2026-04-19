"""Post-quantum encryption operator using ML-KEM-768 + AES-256-GCM.

Provides a Presidio-compatible operator that encrypts PII values using
NIST FIPS 203 ML-KEM-768 for quantum-resistant key encapsulation and
AES-256-GCM for authenticated symmetric encryption.

Wire format (base64url-encoded):
    [2-byte kem_ct_len (big-endian)][kem_ciphertext][12-byte nonce][aes-gcm ciphertext + 16-byte tag]
"""

import base64
import logging
import os
import struct
import threading
from typing import Dict

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from mlkem import ML_KEM, MLKEM_768_PARAMETERS
from presidio_anonymizer.operators import Operator, OperatorType

logger = logging.getLogger(__name__)

_HKDF_INFO = b"PII-Shield-PQC-AES256GCM-v1"
_AES_NONCE_LEN = 12  # 96-bit nonce for AES-GCM
_PQC_MAGIC = b"\x00PQ"  # 3-byte magic prefix to identify PQC tokens

_ml_kem = ML_KEM(MLKEM_768_PARAMETERS)

# Module-level cache for auto-generated keys
_auto_ek: bytes | None = None
_auto_dk: bytes | None = None
_key_lock = threading.Lock()


# -------------------------------------------------------------------
# Key management
# -------------------------------------------------------------------

def _get_keys() -> tuple[bytes, bytes]:
    """Return (encapsulation_key, decapsulation_key) from env or auto-generate."""
    global _auto_ek, _auto_dk

    ek_b64 = os.getenv("PQC_ENCAPSULATION_KEY", "")
    dk_b64 = os.getenv("PQC_DECAPSULATION_KEY", "")

    if ek_b64 and dk_b64:
        return base64.b64decode(ek_b64), base64.b64decode(dk_b64)

    with _key_lock:
        if _auto_ek is None or _auto_dk is None:
            logger.warning(
                "PQC_ENCAPSULATION_KEY / PQC_DECAPSULATION_KEY not set. "
                "Auto-generating ephemeral ML-KEM-768 key pair. "
                "Set these env vars for production use (run: python -m app.operators.keygen)."
            )
            ek, dk = _ml_kem.key_gen()
            _auto_ek = bytes(ek)
            _auto_dk = bytes(dk)
        return _auto_ek, _auto_dk


def _get_encapsulation_key() -> bytes:
    ek, _ = _get_keys()
    return ek


def _get_decapsulation_key() -> bytes:
    _, dk = _get_keys()
    return dk


# -------------------------------------------------------------------
# Core encrypt / decrypt
# -------------------------------------------------------------------

def _derive_aes_key(shared_secret: bytes) -> bytes:
    """Derive a 32-byte AES-256 key from the KEM shared secret via HKDF-SHA256."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(shared_secret)


def encrypt(plaintext: str) -> str:
    """Encrypt plaintext using ML-KEM-768 + AES-256-GCM.

    Returns a base64url-encoded blob with PQC magic prefix.
    """
    ek = _get_encapsulation_key()

    # 1. KEM encapsulation → shared secret + KEM ciphertext
    shared_secret, kem_ct = _ml_kem.encaps(ek)

    # 2. Derive AES-256 key from shared secret
    aes_key = _derive_aes_key(bytes(shared_secret))

    # 3. AES-256-GCM encrypt
    nonce = os.urandom(_AES_NONCE_LEN)
    aesgcm = AESGCM(aes_key)
    aes_ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

    # 4. Pack: [magic(3B)][kem_ct_len(2B)][kem_ct][nonce(12B)][aes_ct]
    kem_ct_bytes = bytes(kem_ct)
    blob = (
        _PQC_MAGIC
        + struct.pack("!H", len(kem_ct_bytes))
        + kem_ct_bytes
        + nonce
        + aes_ct
    )
    return base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt()."""
    dk = _get_decapsulation_key()
    blob = base64.urlsafe_b64decode(token.encode("ascii"))

    # 1. Skip magic prefix
    if blob[:3] != _PQC_MAGIC:
        raise ValueError("Not a PQC-encrypted token (invalid magic prefix)")
    blob = blob[3:]

    # 2. Unpack
    (kem_ct_len,) = struct.unpack("!H", blob[:2])
    kem_ct = blob[2 : 2 + kem_ct_len]
    nonce = blob[2 + kem_ct_len : 2 + kem_ct_len + _AES_NONCE_LEN]
    aes_ct = blob[2 + kem_ct_len + _AES_NONCE_LEN :]

    # 3. KEM decapsulation → shared secret
    shared_secret = _ml_kem.decaps(dk, kem_ct)

    # 4. Derive same AES-256 key
    aes_key = _derive_aes_key(bytes(shared_secret))

    # 5. AES-256-GCM decrypt
    aesgcm = AESGCM(aes_key)
    plaintext_bytes = aesgcm.decrypt(nonce, aes_ct, None)
    return plaintext_bytes.decode("utf-8")


def is_pqc_token(token: str) -> bool:
    """Check if a base64url-encoded token is a PQC-encrypted value."""
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
        return blob[:3] == _PQC_MAGIC
    except Exception:
        return False


# -------------------------------------------------------------------
# Presidio Operator
# -------------------------------------------------------------------

class PqcEncryptOperator(Operator):
    """Encrypt text using ML-KEM-768 + AES-256-GCM (post-quantum safe)."""

    def operate(self, text: str = None, params: Dict = None) -> str:
        return encrypt(text)

    def validate(self, params: Dict = None) -> None:
        pass

    def operator_name(self) -> str:
        return "pqc_encrypt"

    def operator_type(self) -> OperatorType:
        return OperatorType.Anonymize
