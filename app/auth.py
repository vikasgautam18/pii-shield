"""API-side OIDC bearer-token authentication and role-based authorization.

When ``AUTH_ENABLED=true`` every route requires a valid access token (default-deny)
unless its endpoint is marked with :func:`public`. Tokens are verified against the
identity provider's published signing keys (OIDC discovery + JWKS) and roles are
read from a configurable claim. When authentication is disabled, every dependency
returns an open principal immediately, so behaviour is identical to a deployment
without this module (PyJWT is not even imported).

Failure semantics: a missing or invalid token is a 401, a valid token without the
required role is a 403, and an inability to obtain signing keys is a 503 -- the API
fails closed and never lets a request through unauthenticated.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPBearer
from opentelemetry import metrics

from app.auth_settings import AuthSettings, extract_roles, load_auth_settings

logger = logging.getLogger("pii-shield.auth")
meter = metrics.get_meter("pii-shield")

auth_rejections = meter.create_counter(
    "pii.auth.rejections",
    description="Requests rejected by authentication or authorization",
    unit="requests",
)

# JWKS refreshes triggered by an unknown key id are throttled: PyJWT's own
# PyJWKClient refetches on *every* unknown kid, which would let anyone send
# random key ids to turn the API into a request amplifier against the IdP.
_MIN_REFRESH_INTERVAL = 30.0
_COLD_RETRY_INTERVAL = 5.0
# How long cached keys may keep serving (stale-if-error) when the IdP is unreachable.
_MAX_STALE_SECONDS = 86400.0
_HTTP_TIMEOUT = 5.0
_MAX_TOKEN_LENGTH = 16384

_ALG_KEY_TYPE = {
    "RS256": "RSA", "RS384": "RSA", "RS512": "RSA",
    "PS256": "RSA", "PS384": "RSA", "PS512": "RSA",
    "ES256": "EC", "ES384": "EC", "ES512": "EC",
}


class TokenError(Exception):
    """The presented token is not acceptable (HTTP 401)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AuthUnavailable(Exception):
    """Signing keys cannot be obtained, so tokens cannot be verified (HTTP 503)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, derived from validated token claims."""

    subject: str = ""
    name: str = ""
    username: str = ""
    client_id: str = ""
    kind: str = "anonymous"  # "user" | "service" | "anonymous"
    roles: frozenset[str] = frozenset()
    is_user: bool = False
    is_admin: bool = False
    expires_at: int | None = None

    @property
    def actor(self) -> str:
        """Label recorded in the audit log; empty when nobody is authenticated."""
        if self.kind == "anonymous":
            return ""
        if self.kind == "service":
            return f"service:{self.client_id or self.username or self.subject}"
        return self.username or self.name or self.subject


# Returned when authentication is disabled: everyone is effectively an admin.
OPEN_PRINCIPAL = Principal(kind="anonymous", is_user=True, is_admin=True)
# Returned for explicitly public routes when authentication is enabled: no rights.
ANONYMOUS = Principal(kind="anonymous")


class JwksCache:
    """Signing-key cache backed by OIDC discovery and the IdP's JWKS endpoint."""

    def __init__(
        self,
        settings: AuthSettings,
        *,
        http_get: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http_get = http_get or requests.get
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, tuple[str, Any]] = {}
        self._loaded_at: float | None = None
        self._last_attempt: float | None = None
        self._issuer = settings.issuer

    @property
    def issuer(self) -> str:
        return self._issuer

    def lookup(self, kid: str) -> tuple[str, Any] | None:
        """Non-blocking lookup of a key in a fresh cache; ``None`` means refresh needed."""
        if self._loaded_at is None:
            return None
        if self._clock() - self._loaded_at >= self._settings.jwks_cache_seconds:
            return None
        return self._keys.get(kid)

    def refresh_for(self, kid: str) -> tuple[str, Any]:
        """Return the key for ``kid``, refreshing the cache (throttled) if needed.

        Blocking: performs HTTP requests, so async callers run it in a thread.
        """
        if not self._lock.acquire(blocking=False):
            # Another thread is already talking to the IdP. A key we still hold (fresh, or stale within the
            # allowed window) is good enough for this request, so it must not queue behind a slow or hung
            # refresh; only callers without a usable key wait for the outcome.
            entry = self._keys.get(kid)
            if entry is not None and self._usable(self._clock()):
                return entry
            self._lock.acquire()
        try:
            return self._refresh_locked(kid)
        finally:
            self._lock.release()

    def _refresh_locked(self, kid: str) -> tuple[str, Any]:
        now = self._clock()
        hit = self.lookup(kid)
        if hit is not None:
            return hit  # another thread refreshed while we waited for the lock

        cold = self._loaded_at is None
        interval = _COLD_RETRY_INTERVAL if cold else _MIN_REFRESH_INTERVAL
        throttled = self._last_attempt is not None and now - self._last_attempt < interval
        refresh_failed = False
        if not throttled:
            self._last_attempt = now
            try:
                self._fetch()
            except Exception as exc:  # network, HTTP, JSON or content errors
                refresh_failed = True
                logger.warning("Signing-key refresh failed: %s", exc)

        entry = self._keys.get(kid)
        if entry is not None and self._usable(now):
            return entry  # fresh, or stale-if-error within the allowed window
        if self._loaded_at is None or refresh_failed or self._expired(now):
            raise AuthUnavailable("signing keys unavailable")
        raise TokenError("unknown_kid")

    def load(self) -> None:
        """Force a fetch (used for start-up warm-up); raises on failure."""
        with self._lock:
            self._last_attempt = self._clock()
            self._fetch()

    def _expired(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at >= self._settings.jwks_cache_seconds

    def _usable(self, now: float) -> bool:
        if self._loaded_at is None:
            return False
        return now - self._loaded_at <= self._settings.jwks_cache_seconds + _MAX_STALE_SECONDS

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._http_get(url, timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"{url} did not return a JSON object")
        return data

    def _fetch(self) -> None:
        import jwt  # imported lazily so disabled deployments never need PyJWT

        settings = self._settings
        issuer, jwks_uri = settings.issuer, settings.jwks_url
        if not issuer or not jwks_uri:
            doc = self._get_json(settings.metadata_url)
            if not issuer:
                issuer = str(doc.get("issuer", ""))
                # OIDC Discovery: the document's issuer must equal the URL it was fetched for.
                if issuer.rstrip("/") != settings.issuer_url.rstrip("/"):
                    raise ValueError(
                        f"discovery issuer {issuer!r} does not match OIDC_ISSUER_URL "
                        f"{settings.issuer_url!r}; set OIDC_ISSUER to the expected 'iss' value"
                    )
            jwks_uri = jwks_uri or str(doc.get("jwks_uri", ""))
        if not issuer or not jwks_uri:
            raise ValueError("discovery document is missing 'issuer' or 'jwks_uri'")

        keys: dict[str, tuple[str, Any]] = {}
        for jwk in self._get_json(jwks_uri).get("keys", []):
            if not isinstance(jwk, dict):
                continue
            kid, kty = jwk.get("kid"), jwk.get("kty")
            if not kid or kty not in ("RSA", "EC") or jwk.get("use") not in (None, "sig"):
                continue
            try:
                keys[str(kid)] = (str(kty), jwt.PyJWK(jwk).key)
            except Exception as exc:  # a malformed key must not poison the others
                logger.warning("Ignoring unusable signing key %s: %s", kid, exc)
        if not keys:
            raise ValueError("JWKS contains no usable signing keys")

        self._keys = keys
        self._issuer = issuer
        self._loaded_at = self._clock()


class TokenValidator:
    """Verifies bearer access tokens and turns their claims into a :class:`Principal`."""

    def __init__(self, settings: AuthSettings, cache: JwksCache | None = None) -> None:
        self.settings = settings
        self.cache = cache or JwksCache(settings)

    def validate(self, token: str) -> Principal:
        """Synchronous validation; may block on a signing-key refresh."""
        alg, kid = self._peek_header(token)
        entry = self.cache.lookup(kid) or self.cache.refresh_for(kid)
        return self._verify(token, alg, entry)

    async def avalidate(self, token: str) -> Principal:
        """Async validation: cache hits are pure CPU work and stay on the event loop."""
        alg, kid = self._peek_header(token)
        entry = self.cache.lookup(kid)
        if entry is None:
            # Only a cache miss touches the network, so only then do we use a thread.
            entry = await asyncio.to_thread(self.cache.refresh_for, kid)
        return self._verify(token, alg, entry)

    def warm_up(self) -> bool:
        """Best-effort start-up fetch of discovery + keys; never raises."""
        try:
            self.cache.load()
            return True
        except Exception as exc:
            logger.warning("Could not load signing keys at start-up (will retry on demand): %s", exc)
            return False

    def _peek_header(self, token: str) -> tuple[str, str]:
        import jwt

        if len(token) > _MAX_TOKEN_LENGTH:
            raise TokenError("token_too_large")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenError("malformed") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in self.settings.algorithms:
            raise TokenError("alg_not_allowed")
        if not kid:
            raise TokenError("missing_kid")
        return str(alg), str(kid)

    def _verify(self, token: str, alg: str, entry: tuple[str, Any]) -> Principal:
        import jwt

        kty, key = entry
        if _ALG_KEY_TYPE.get(alg) != kty:
            raise TokenError("key_type_mismatch")
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=list(self.settings.audiences),
                issuer=self.cache.issuer,
                leeway=self.settings.clock_skew_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenError("expired") from None
        except jwt.InvalidAudienceError:
            raise TokenError("bad_audience") from None
        except jwt.InvalidIssuerError:
            raise TokenError("bad_issuer") from None
        except jwt.MissingRequiredClaimError:
            raise TokenError("missing_claim") from None
        except jwt.ImmatureSignatureError:
            raise TokenError("not_yet_valid") from None
        except jwt.InvalidSignatureError:
            raise TokenError("bad_signature") from None
        except jwt.PyJWTError:
            raise TokenError("invalid") from None
        return self._principal(claims)

    def _principal(self, claims: dict[str, Any]) -> Principal:
        settings = self.settings
        roles = extract_roles(claims, settings.roles_claims)
        is_admin = bool(roles & settings.admin_roles)
        is_user = is_admin or bool(roles & settings.user_roles)

        username = str(claims.get("preferred_username") or claims.get("upn") or claims.get("email") or "")
        client_id = str(claims.get("azp") or claims.get("appid") or claims.get("client_id") or "")
        is_service = (
            claims.get("idtyp") == "app"
            or claims.get("gty") == "client-credentials"
            or username.startswith("service-account-")
            or (not username and bool(client_id))
        )
        exp = claims.get("exp")
        return Principal(
            subject=str(claims.get("sub", "")),
            name=str(claims.get("name") or ""),
            username=username,
            client_id=client_id,
            kind="service" if is_service else "user",
            roles=roles,
            is_user=is_user,
            is_admin=is_admin,
            expires_at=int(exp) if isinstance(exp, (int, float)) else None,
        )


@dataclass
class AuthState:
    settings: AuthSettings
    validator: TokenValidator | None


_state: AuthState | None = None
_state_lock = threading.Lock()


def get_auth_state() -> AuthState:
    """Process-wide auth state, built (and validated) on first use."""
    global _state
    if _state is None:
        with _state_lock:
            if _state is None:
                settings = load_auth_settings("api")
                validator = TokenValidator(settings) if settings.enabled else None
                _state = AuthState(settings, validator)
                if settings.enabled:
                    logger.info("Authentication enabled: %s", settings.safe_summary())
                else:
                    logger.info("Authentication disabled (AUTH_ENABLED is not true)")
    return _state


def configure_auth(settings: AuthSettings, validator: TokenValidator | None = None) -> AuthState:
    """Install explicit auth state (used by tests)."""
    global _state
    if settings.enabled and validator is None:
        validator = TokenValidator(settings)
    _state = AuthState(settings, validator if settings.enabled else None)
    return _state


def reset_auth() -> None:
    """Forget cached auth state so the next access re-reads the environment."""
    global _state
    _state = None


async def warm_up_auth() -> None:
    """Load discovery and signing keys at start-up so the first request is not slow."""
    state = get_auth_state()
    if state.settings.enabled and state.validator is not None:
        loaded = await asyncio.to_thread(state.validator.warm_up)
        if loaded:
            logger.info("Signing keys loaded for issuer %s", state.validator.cache.issuer)


# ---------------------------------------------------------------------------
# FastAPI integration
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="OIDC access token (JWT) issued for this API.",
)


def public(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Mark an endpoint as reachable without a token even when auth is enabled."""
    endpoint.__auth_public__ = True  # type: ignore[attr-defined]
    return endpoint


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", request.url.path))


def _reject(
    request: Request,
    status_code: int,
    reason: str,
    detail: str,
    *,
    token_supplied: bool = True,
) -> None:
    path = _route_path(request)
    auth_rejections.add(1, {"reason": reason, "route": path})
    logger.warning("Auth rejected (%s, HTTP %d) on %s", reason, status_code, path)
    headers: dict[str, str] = {}
    if status_code == 401:
        headers["WWW-Authenticate"] = 'Bearer error="invalid_token"' if token_supplied else "Bearer"
    elif status_code == 503:
        headers["Retry-After"] = "5"
    raise HTTPException(status_code=status_code, detail=detail, headers=headers)


async def authenticate(request: Request) -> Principal:
    """Global dependency: authenticate every request unless the route is public."""
    state = get_auth_state()
    if not state.settings.enabled or state.validator is None:
        return OPEN_PRINCIPAL

    route = request.scope.get("route")
    if getattr(getattr(route, "endpoint", None), "__auth_public__", False):
        return ANONYMOUS

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        _reject(request, 401, "missing_token", "Authentication required.", token_supplied=False)

    try:
        principal = await state.validator.avalidate(token)
    except TokenError as exc:
        _reject(request, 401, exc.reason, "Invalid or expired access token.")
    except AuthUnavailable as exc:
        _reject(request, 503, exc.reason, "Authentication is temporarily unavailable.")
    request.state.principal = principal
    return principal


async def require_authenticated(
    principal: Principal = Depends(authenticate),
    _scheme: Any = Security(_bearer_scheme),
) -> Principal:
    """Any valid token (no specific role). ``_scheme`` documents the lock in OpenAPI."""
    return principal


async def require_user(
    request: Request,
    principal: Principal = Depends(authenticate),
    _scheme: Any = Security(_bearer_scheme),
) -> Principal:
    if not principal.is_user:
        _reject(request, 403, "insufficient_role", "Insufficient permissions: the user role is required.")
    return principal


async def require_admin(
    request: Request,
    principal: Principal = Depends(authenticate),
    _scheme: Any = Security(_bearer_scheme),
) -> Principal:
    if not principal.is_admin:
        _reject(request, 403, "insufficient_role", "Insufficient permissions: the admin role is required.")
    return principal
