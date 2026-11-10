"""Authentication settings shared by the API, the Streamlit UIs and the UI launcher.

Standard library only: this module is imported as ``app.auth_settings`` by the API
and tests, and as a flat ``auth_settings`` from Streamlit scripts (where ``app/``
is ``sys.path[0]``), so it must not pull in FastAPI, Streamlit or PyJWT.

Everything is controlled by one master toggle, ``AUTH_ENABLED`` (default ``false``).
When it is off nothing else is read or validated, so stray ``OIDC_*`` variables can
never break a disabled deployment. When it is on, misconfiguration raises
:class:`AuthConfigError` so a service refuses to start instead of silently running open.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

DEFAULT_USER_ROLE = "PIIShield.User"
DEFAULT_ADMIN_ROLE = "PIIShield.Admin"
DEFAULT_ROLES_CLAIM = "roles"
DEFAULT_UI_SCOPES = ("openid", "profile", "email")
# Streamlit substitutes {port} with the server port, which keeps the generated
# secrets file identical for every UI started from the same working directory.
DEFAULT_REDIRECT_URI = "http://localhost:{port}/oauth2callback"

# Only asymmetric algorithms are accepted: keys come from the IdP's JWKS, so
# "none" and HS* (shared-secret) algorithms must never be honoured.
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)

MIN_COOKIE_SECRET_LENGTH = 32

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class AuthConfigError(ValueError):
    """Raised when authentication is enabled but its configuration is invalid."""


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    issuer_url: str = ""
    audiences: tuple[str, ...] = ()
    roles_claims: tuple[str, ...] = (DEFAULT_ROLES_CLAIM,)
    user_roles: frozenset[str] = frozenset({DEFAULT_USER_ROLE})
    admin_roles: frozenset[str] = frozenset({DEFAULT_ADMIN_ROLE})
    issuer: str = ""
    jwks_url: str = ""
    algorithms: tuple[str, ...] = ("RS256",)
    clock_skew_seconds: int = 60
    jwks_cache_seconds: int = 3600
    allow_insecure_http: bool = False
    # UI-only settings.
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    api_scope: str = ""
    ui_scopes: tuple[str, ...] = DEFAULT_UI_SCOPES
    server_metadata_url: str = ""
    prompt: str = ""
    cookie_secret: str = field(default="", repr=False)
    redirect_uri: str = DEFAULT_REDIRECT_URI

    @property
    def metadata_url(self) -> str:
        """OIDC discovery document URL (explicit override or derived from the issuer)."""
        if self.server_metadata_url:
            return self.server_metadata_url
        return f"{self.issuer_url.rstrip('/')}/.well-known/openid-configuration"

    @property
    def scope_string(self) -> str:
        """Space-delimited scopes the UI requests (API scope appended, de-duplicated)."""
        scopes = list(self.ui_scopes)
        if self.api_scope and self.api_scope not in scopes:
            scopes.append(self.api_scope)
        return " ".join(scopes)

    def safe_summary(self) -> dict[str, Any]:
        """Non-secret view of the settings, suitable for startup logging."""
        return {
            "enabled": self.enabled,
            "issuer_url": self.issuer_url,
            "audiences": list(self.audiences),
            "roles_claims": list(self.roles_claims),
            "user_roles": sorted(self.user_roles),
            "admin_roles": sorted(self.admin_roles),
            "algorithms": list(self.algorithms),
        }


def _get(env: Mapping[str, str], name: str) -> str:
    # Empty values are treated as unset, so ``VAR=${VAR:-}`` pass-through in
    # docker-compose and ACA behaves like a missing variable.
    return (env.get(name) or "").strip()


def split_list(raw: str) -> list[str]:
    """Split a comma-separated list, trimming blanks."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_bool(raw: str, name: str, default: bool = False) -> bool:
    value = raw.strip().lower()
    if not value:
        return default
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise AuthConfigError(
        f"{name} must be one of true/false/1/0/yes/no/on/off, got {raw!r}."
    )


def auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Strictly parse the master toggle. An invalid value is an error, never 'off'."""
    env = os.environ if env is None else env
    return parse_bool(_get(env, "AUTH_ENABLED"), "AUTH_ENABLED")


def _parse_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    raw = _get(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer, got {raw!r}.")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}, got {value}.")
        return default
    return value


def _check_url(
    errors: list[str], name: str, value: str, *, allow_http: bool
) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        errors.append(f"{name} must be an absolute http(s) URL, got {value!r}.")
        return
    if parsed.scheme == "http" and not allow_http:
        errors.append(
            f"{name} must use https (set OIDC_ALLOW_INSECURE_HTTP=true for local development only)."
        )


def _check_redirect_uri(errors: list[str], value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        errors.append(f"UI_REDIRECT_URI must be an absolute http(s) URL, got {value!r}.")
        return
    if not parsed.path.endswith("/oauth2callback"):
        errors.append("UI_REDIRECT_URI must end with /oauth2callback (Streamlit's callback path).")
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOCAL_HOSTS:
        errors.append(
            "UI_REDIRECT_URI must use https unless it points at localhost "
            "(the authorization code would otherwise travel in clear text)."
        )


def load_auth_settings(
    component: str = "api", env: Mapping[str, str] | None = None
) -> AuthSettings:
    """Load and validate settings for ``component`` (``api``, ``ui`` or ``all``).

    Returns immediately with ``enabled=False`` when ``AUTH_ENABLED`` is off.
    Raises :class:`AuthConfigError` listing *every* problem when enabled but invalid.
    Secret values are never included in error messages.
    """
    if component not in ("api", "ui", "all"):
        raise ValueError(f"Unknown component {component!r}; expected 'api', 'ui' or 'all'.")
    env = os.environ if env is None else env
    if not auth_enabled(env):
        return AuthSettings(enabled=False)

    errors: list[str] = []
    check_api = component in ("api", "all")
    check_ui = component in ("ui", "all")

    allow_http = parse_bool(
        _get(env, "OIDC_ALLOW_INSECURE_HTTP"), "OIDC_ALLOW_INSECURE_HTTP"
    )

    issuer_url = _get(env, "OIDC_ISSUER_URL")
    if not issuer_url:
        errors.append("OIDC_ISSUER_URL is required when AUTH_ENABLED=true.")
    else:
        _check_url(errors, "OIDC_ISSUER_URL", issuer_url, allow_http=allow_http)

    roles_claims = tuple(split_list(_get(env, "OIDC_ROLES_CLAIM"))) or (DEFAULT_ROLES_CLAIM,)
    user_roles = frozenset(split_list(_get(env, "AUTH_USER_ROLES"))) or frozenset({DEFAULT_USER_ROLE})
    admin_roles = frozenset(split_list(_get(env, "AUTH_ADMIN_ROLES"))) or frozenset({DEFAULT_ADMIN_ROLE})

    audiences: tuple[str, ...] = ()
    issuer = jwks_url = ""
    algorithms: tuple[str, ...] = ("RS256",)
    skew = 60
    jwks_cache = 3600
    if check_api:
        audiences = tuple(split_list(_get(env, "OIDC_AUDIENCE")))
        if not audiences:
            errors.append("OIDC_AUDIENCE is required when AUTH_ENABLED=true.")

        algorithms = tuple(split_list(_get(env, "OIDC_ALGORITHMS"))) or ("RS256",)
        unsupported = [a for a in algorithms if a not in ALLOWED_ALGORITHMS]
        if unsupported:
            errors.append(
                f"OIDC_ALGORITHMS contains unsupported values {unsupported}; allowed: "
                f"{sorted(ALLOWED_ALGORITHMS)} ('none' and HS* are never accepted)."
            )

        issuer = _get(env, "OIDC_ISSUER")
        jwks_url = _get(env, "OIDC_JWKS_URL")
        if jwks_url:
            _check_url(errors, "OIDC_JWKS_URL", jwks_url, allow_http=allow_http)
        skew = _parse_int(env, "OIDC_CLOCK_SKEW_SECONDS", 60, 0, 300, errors)
        jwks_cache = _parse_int(env, "OIDC_JWKS_CACHE_SECONDS", 3600, 60, 86400, errors)

    client_id = client_secret = api_scope = server_metadata_url = prompt = cookie_secret = ""
    ui_scopes = DEFAULT_UI_SCOPES
    redirect_uri = DEFAULT_REDIRECT_URI
    if check_ui:
        client_id = _get(env, "OIDC_CLIENT_ID")
        client_secret = _get(env, "OIDC_CLIENT_SECRET")
        if not client_id:
            errors.append("OIDC_CLIENT_ID is required for the UIs when AUTH_ENABLED=true.")
        if not client_secret:
            errors.append("OIDC_CLIENT_SECRET is required for the UIs when AUTH_ENABLED=true.")

        cookie_secret = _get(env, "AUTH_COOKIE_SECRET")
        if not cookie_secret:
            errors.append("AUTH_COOKIE_SECRET is required for the UIs when AUTH_ENABLED=true.")
        elif len(cookie_secret) < MIN_COOKIE_SECRET_LENGTH:
            errors.append(
                f"AUTH_COOKIE_SECRET must be at least {MIN_COOKIE_SECRET_LENGTH} characters."
            )

        api_scope = _get(env, "OIDC_API_SCOPE")
        raw_scopes = _get(env, "OIDC_UI_SCOPES")
        if raw_scopes:
            ui_scopes = tuple(s for s in re.split(r"[\s,]+", raw_scopes) if s)
        if "openid" not in ui_scopes:
            errors.append("OIDC_UI_SCOPES must include 'openid'.")

        server_metadata_url = _get(env, "OIDC_SERVER_METADATA_URL")
        if server_metadata_url:
            _check_url(errors, "OIDC_SERVER_METADATA_URL", server_metadata_url, allow_http=allow_http)
        prompt = _get(env, "OIDC_PROMPT")

        redirect_uri = _get(env, "UI_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        _check_redirect_uri(errors, redirect_uri)

    if errors:
        details = "\n".join(f"  - {message}" for message in errors)
        raise AuthConfigError(f"Invalid authentication configuration:\n{details}")

    return AuthSettings(
        enabled=True,
        issuer_url=issuer_url,
        audiences=audiences,
        roles_claims=roles_claims,
        user_roles=user_roles,
        admin_roles=admin_roles,
        issuer=issuer,
        jwks_url=jwks_url,
        algorithms=algorithms,
        clock_skew_seconds=skew,
        jwks_cache_seconds=jwks_cache,
        allow_insecure_http=allow_http,
        client_id=client_id,
        client_secret=client_secret,
        api_scope=api_scope,
        ui_scopes=ui_scopes,
        server_metadata_url=server_metadata_url,
        prompt=prompt,
        cookie_secret=cookie_secret,
        redirect_uri=redirect_uri,
    )


def _lookup_claim(claims: Mapping[str, Any], path: str) -> Any:
    # An exact key wins, which supports namespaced claims that contain dots
    # (for example "https://example.com/roles"); otherwise walk a dotted path.
    if path in claims:
        return claims[path]
    node: Any = claims
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return None
    return node


def extract_roles(claims: Mapping[str, Any], paths: tuple[str, ...]) -> frozenset[str]:
    """Collect role/scope/group values from one or more claim paths.

    Handles list values (``roles``, ``groups``), space-delimited strings (``scp``,
    ``scope``) and nested objects addressed with dot-notation
    (``realm_access.roles``). Unknown or malformed claims contribute nothing.
    """
    found: set[str] = set()
    for path in paths:
        value = _lookup_claim(claims, path)
        if isinstance(value, str):
            found.update(value.split())
        elif isinstance(value, (list, tuple, set, frozenset)):
            found.update(str(item).strip() for item in value if str(item).strip())
    return frozenset(found)
