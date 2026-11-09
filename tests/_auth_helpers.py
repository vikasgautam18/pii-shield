"""Test doubles for authentication tests.

``FakeIdp`` is an in-memory identity provider: it owns RSA signing keys, serves OIDC
discovery and JWKS documents through an injectable ``http_get`` (no network), and
mints signed access tokens. ``FakeClock`` makes cache expiry and refresh throttling
deterministic.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import JwksCache, TokenValidator
from app.auth_settings import AuthSettings, load_auth_settings

ISSUER = "https://idp.test/realms/pii"
AUDIENCE = "pii-shield-api"

_KEY_CACHE: dict[str, "SigningKey"] = {}


class SigningKey:
    """An RSA key pair that can sign JWTs and publish its public JWK."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @classmethod
    def shared(cls, kid: str) -> "SigningKey":
        # RSA key generation is slow; reuse keys across tests.
        if kid not in _KEY_CACHE:
            _KEY_CACHE[kid] = cls(kid)
        return _KEY_CACHE[kid]

    def jwk(self) -> dict[str, Any]:
        data = jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        data.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return data

    def sign(
        self,
        claims: dict[str, Any],
        *,
        alg: str = "RS256",
        headers: dict[str, Any] | None = None,
    ) -> str:
        return jwt.encode(claims, self.private_key, algorithm=alg, headers={"kid": self.kid, **(headers or {})})


class FakeClock:
    """Monotonic clock tests can advance by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class FakeIdp:
    def __init__(self, *, issuer: str = ISSUER, audience: str = AUDIENCE) -> None:
        self.issuer = issuer
        self.audience = audience
        self.keys: list[SigningKey] = [SigningKey.shared("key-1")]
        self.discovery_issuer = issuer  # what the discovery document claims
        self.fail = False  # simulate an outage
        self.jwks_override: dict[str, Any] | None = None
        self.calls: list[str] = []
        # Set ``hold`` to make every request block until it is set (a hung identity provider);
        # ``in_flight`` is set as soon as a request is waiting.
        self.hold: threading.Event | None = None
        self.in_flight = threading.Event()

    @property
    def key(self) -> SigningKey:
        return self.keys[0]

    @property
    def discovery_url(self) -> str:
        return f"{self.issuer}/.well-known/openid-configuration"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/certs"

    def fetch_count(self, url: str) -> int:
        return self.calls.count(url)

    def http_get(self, url: str, timeout: float | None = None) -> _Response:
        self.calls.append(url)
        if self.hold is not None:
            self.in_flight.set()
            self.hold.wait(timeout=10)
        if self.fail:
            raise requests.ConnectionError("identity provider unreachable")
        if url == self.discovery_url:
            return _Response({"issuer": self.discovery_issuer, "jwks_uri": self.jwks_url})
        if url == self.jwks_url:
            return _Response(self.jwks_override or {"keys": [k.jwk() for k in self.keys]})
        raise requests.HTTPError(f"404 for {url}")

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """Default valid claims; pass ``name=None`` to remove a claim."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": "user-123",
            "preferred_username": "alice@example.com",
            "name": "Alice Example",
            "roles": ["PIIShield.User"],
            "iat": now,
            "nbf": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return {name: value for name, value in claims.items() if value is not None}

    def token(
        self,
        *,
        key: SigningKey | None = None,
        alg: str = "RS256",
        headers: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> str:
        return (key or self.key).sign(self.claims(**overrides), alg=alg, headers=headers)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "AUTH_ENABLED": "true",
            "OIDC_ISSUER_URL": self.issuer,
            "OIDC_AUDIENCE": self.audience,
            **extra,
        }

    def settings(self, **extra: str) -> AuthSettings:
        return load_auth_settings("api", env=self.env(**extra))

    def validator(self, clock: FakeClock | None = None, **extra: str) -> TokenValidator:
        settings = self.settings(**extra)
        cache = JwksCache(settings, http_get=self.http_get, clock=clock or FakeClock())
        return TokenValidator(settings, cache)
