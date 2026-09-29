"""Ready-made :class:`~pii_shield.session_store.ValueCodec` implementations.

A codec transparently protects the **plaintext-PII** conversation vault at
rest: it is applied to the serialized :class:`ConversationState` before it is
written to a :class:`SessionStore`, and reversed on load.  Wire one in via the
``value_codec`` argument of the reference stores::

    from pii_shield import SqliteSessionStore
    from pii_shield.codecs import FernetValueCodec

    codec = FernetValueCodec(FernetValueCodec.generate_key())  # persist the key!
    store = SqliteSessionStore("sessions.db", value_codec=codec)

Only :class:`FernetValueCodec` (symmetric AES-128-CBC + HMAC via ``cryptography``,
a core dependency) is provided here.  For post-quantum protection, wrap
:mod:`pii_shield.operators.pqc_encrypt` in a codec with the same two methods.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


class FernetValueCodec:
    """Encrypt serialized session state with Fernet (AES-128-CBC + HMAC-SHA256).

    Parameters
    ----------
    key :
        A urlsafe-base64 Fernet key (``str`` or ``bytes``).  Generate one with
        :meth:`generate_key` and store it securely (e.g. Key Vault) — losing it
        makes existing stored conversations unrecoverable.
    """

    def __init__(self, key: str | bytes):
        if isinstance(key, str):
            key = key.encode("ascii")
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key() -> str:
        """Return a fresh urlsafe-base64 Fernet key as a string."""
        return Fernet.generate_key().decode("ascii")

    def encode(self, text: str) -> str:
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decode(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
