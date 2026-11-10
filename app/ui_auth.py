"""Streamlit-side authentication: login gate, API session and bearer-token client.

Imported flat (``from ui_auth import ...``) by the Streamlit scripts, like ``themes``.
When ``AUTH_ENABLED`` is off every helper is a no-op and ``st.user`` is never touched,
so the UIs behave exactly as before.

When it is on, each script calls :func:`require_login` right after ``apply_theme()``
and before any data call:

* not signed in            -> sign-in screen (``st.login``) and ``st.stop()``
* signed in                -> ``GET /me`` with the user's access token; the *API* is the
  source of truth for identity and roles, so no JWT parsing or IdP-specific claim
  handling happens here
* expired / no access / admin role missing / API unreachable -> an explanatory screen

Streamlit does not refresh tokens, so at access-token expiry the user signs in again
(one click when the IdP session is still alive). Tokens are never displayed or logged.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import requests
import streamlit as st

try:  # package context (tests, ``python -m``)
    from app.auth_settings import auth_enabled
except ImportError:  # Streamlit script context: app/ is sys.path[0]
    from auth_settings import auth_enabled

EXPIRY_SKEW_SECONDS = 30
_ME_KEY = "_pii_auth_me"
_RERUN_KEY = "_pii_auth_401_fingerprint"
# The API scales to zero when idle. The platform holds the first request while the replica starts and loads its
# NER model (about a minute), so allow that long for the reply, but still fail fast when nothing is listening.
_ME_CONNECT_TIMEOUT_SECONDS = 5
_ME_READ_TIMEOUT_SECONDS = 90
_WAITING_FOR_API_MESSAGE = "Contacting the PII Shield API. After a period of inactivity this can take about a minute…"


# ── Pure decision logic (unit-tested without a Streamlit runtime) ────────────


def token_fingerprint(token: str) -> str:
    """Short, non-reversible identifier for a token (safe to keep in session state)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def is_expired(expires_at: int | None, now: float, skew: int = EXPIRY_SKEW_SECONDS) -> bool:
    return expires_at is not None and now >= expires_at - skew


def bearer_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def evaluate_me(status: int | None, body: Any, required: str) -> str:
    """Map the outcome of ``GET /me`` to a gate decision.

    ``required`` is ``"user"`` (playground) or ``"admin"`` (admin UI).
    Returns one of: ok, expired, no_access, admin_required, unreachable, error.
    """
    if status is None:
        return "unreachable"
    if status == 401:
        return "expired"
    if status != 200 or not isinstance(body, dict):
        return "error"
    if required == "admin":
        if body.get("is_admin"):
            return "ok"
        return "admin_required" if body.get("is_user") else "no_access"
    return "ok" if body.get("is_user") else "no_access"


def should_rerun_after_401(fingerprint: str, last_rerun_fingerprint: str | None) -> bool:
    """Re-run at most once per token after a 401, so a stubborn 401 cannot loop."""
    return fingerprint != last_rerun_fingerprint


# ── Session and API client ───────────────────────────────────────────────────


@dataclass(frozen=True)
class UiSession:
    enabled: bool
    token: str = ""
    subject: str = ""
    name: str = ""
    is_admin: bool = False
    expires_at: int | None = None

    def headers(self) -> dict[str, str]:
        return bearer_headers(self.token) if self.enabled else {}


class ApiClient:
    """``requests``-style client that adds the signed-in user's bearer token.

    It returns the raw :class:`requests.Response` and lets ``requests`` exceptions
    propagate, so existing ``except requests.ConnectionError`` handling keeps working.
    """

    def __init__(self, session: UiSession) -> None:
        self._session = session

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = {**self._session.headers(), **(kwargs.pop("headers", None) or {})}
        response = requests.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401 and self._session.enabled:
            _handle_unauthorized(self._session.token)
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", url, **kwargs)


def _handle_unauthorized(token: str) -> None:
    # The token stopped working mid-session (expiry, revocation, key rotation).
    # Drop the cached identity and re-run so the gate shows the sign-in-again screen.
    fingerprint = token_fingerprint(token)
    if should_rerun_after_401(fingerprint, st.session_state.get(_RERUN_KEY)):
        st.session_state[_RERUN_KEY] = fingerprint
        st.session_state.pop(_ME_KEY, None)
        st.rerun()


# ── Streamlit glue ───────────────────────────────────────────────────────────


def _access_token() -> str:
    try:
        return str(st.user.tokens["access"] or "")
    except (KeyError, AttributeError, TypeError):
        return ""


def _is_logged_in() -> bool | None:
    """``None`` when Streamlit has no OIDC configuration (``st.user`` lacks the attribute)."""
    try:
        return bool(st.user.is_logged_in)
    except AttributeError:
        return None


def _fetch_me(api_base: str, token: str) -> tuple[int | None, Any]:
    try:
        response = requests.get(
            f"{api_base}/me",
            headers=bearer_headers(token),
            timeout=(_ME_CONNECT_TIMEOUT_SECONDS, _ME_READ_TIMEOUT_SECONDS),
        )
    except requests.RequestException:
        return None, None
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    return response.status_code, body


def _login_screen() -> None:
    st.title("🛡️ PII Shield")
    st.subheader("Sign in required")
    st.write("This application is protected. Sign in with your organization account to continue.")
    st.button("Sign in", on_click=st.login, type="primary", key="pii_auth_sign_in")


def _expired_screen() -> None:
    st.title("🛡️ PII Shield")
    st.warning("Your session has expired. Please sign in again.")
    st.button("Sign in again", on_click=st.login, type="primary", key="pii_auth_sign_in_again")


def _denied_screen(message: str, me: dict[str, Any] | None) -> None:
    st.title("🛡️ PII Shield")
    st.error(message)
    who = (me or {}).get("name") or (me or {}).get("subject")
    if who:
        st.caption(f"Signed in as {who}.")
    st.caption(
        "Ask an administrator to assign you the required role, then sign out and sign in again "
        "so your new role is included in your token."
    )
    st.button("Sign out", on_click=st.logout, key="pii_auth_sign_out_denied")


def _error_screen(message: str) -> None:
    st.title("🛡️ PII Shield")
    st.error(message)
    st.button("Retry", key="pii_auth_retry")


def _identity_sidebar(me: dict[str, Any]) -> None:
    with st.sidebar:
        st.caption("Signed in as")
        st.markdown(f"**{me.get('name') or me.get('subject') or 'unknown'}**")
        st.button("Sign out", on_click=st.logout, key="pii_auth_sign_out")


def require_login(*, required: str, api_base: str) -> UiSession:
    """Gate a Streamlit script behind sign-in and the ``required`` API role.

    Returns a :class:`UiSession` whose ``headers()`` authenticate API calls.
    Calls ``st.stop()`` (never returning) whenever the user may not proceed.
    """
    if not auth_enabled():
        return UiSession(enabled=False)

    logged_in = _is_logged_in()
    if logged_in is None:
        _error_screen(
            "Authentication is enabled but Streamlit's OIDC configuration is missing. "
            "Start this UI with `python -m app.ui_launcher` so it can be generated from the environment."
        )
        st.stop()
    if not logged_in:
        _login_screen()
        st.stop()

    token = _access_token()
    if not token:
        _error_screen(
            "Signed in, but no API access token is available. The identity provider "
            "configuration (scopes / token exposure) needs attention."
        )
        st.stop()

    fingerprint = token_fingerprint(token)
    cached = st.session_state.get(_ME_KEY)
    me = cached["me"] if cached and cached.get("fingerprint") == fingerprint else None

    status: int | None = 200
    if me is not None and is_expired(me.get("expires_at"), time.time()):
        status, me = 401, None
    elif me is None:
        with st.spinner(_WAITING_FOR_API_MESSAGE):
            status, body = _fetch_me(api_base, token)
        if status == 200 and isinstance(body, dict):
            me = body
            st.session_state[_ME_KEY] = {"fingerprint": fingerprint, "me": me}
        else:
            me = None

    outcome = evaluate_me(status, me, required)
    if outcome == "expired":
        st.session_state.pop(_ME_KEY, None)
        _expired_screen()
        st.stop()
    elif outcome == "no_access":
        _denied_screen("Your account does not have access to PII Shield.", me)
        st.stop()
    elif outcome == "admin_required":
        _denied_screen("The admin role is required to use this screen.", me)
        st.stop()
    elif outcome == "unreachable":
        _error_screen(f"Cannot reach the PII Shield API at {api_base}.")
        st.stop()
    elif outcome != "ok":
        _error_screen(f"Unexpected response from the PII Shield API (HTTP {status}).")
        st.stop()

    assert me is not None  # outcome == "ok" implies a parsed /me body
    _identity_sidebar(me)
    return UiSession(
        enabled=True,
        token=token,
        subject=str(me.get("subject") or ""),
        name=str(me.get("name") or ""),
        is_admin=bool(me.get("is_admin")),
        expires_at=me.get("expires_at"),
    )
