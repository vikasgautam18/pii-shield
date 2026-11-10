"""Bearer-token support shared by the example/test scripts that call a PII Shield API.

When the API runs with ``AUTH_ENABLED=true`` every call needs an access token::

    export API_ACCESS_TOKEN="$(python scripts/get_token.py)"
    python examples/run_indian_banking_tests.py --api-url https://pii-shield.example.com

The token comes from ``--access-token`` or the ``API_ACCESS_TOKEN`` environment
variable. Without one the scripts behave exactly as before (an API with auth off).

Scripts that register or delete applications also call ``check_api_access`` before they start, so a missing,
expired or too-weak token stops them with a clear message instead of a wall of per-test HTTP errors.

Used by:
  - examples/run_indian_banking_tests.py / run_indian_banking_tests_azure.py  (requests, check_api_access)
  - examples/scale_test.py                                                    (aiohttp, check_api_access)
  - scripts/cleanup_test_apps.py                                              (requests)
"""

from __future__ import annotations

import argparse
import os
import time

import requests

ENV_VAR = "API_ACCESS_TOKEN"


def add_token_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--access-token",
        default=os.getenv(ENV_VAR, ""),
        help=(
            f"Bearer access token for an API running with AUTH_ENABLED=true "
            f"(default: ${ENV_VAR}). Create one with scripts/get_token.py."
        ),
    )


def auth_headers(token: str) -> dict[str, str]:
    """Headers for clients that take default headers (e.g. ``aiohttp.ClientSession``)."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def install_bearer_token(token: str, api_base: str) -> None:
    """Make every ``requests`` call to ``api_base`` carry ``Authorization: Bearer <token>``.

    Works for ``requests.get/post/...`` and for ``requests.Session`` objects, so the
    large scripts need no per-call changes. The header is only added for URLs under
    ``api_base`` (the token is never sent to other hosts) and never overrides an
    ``Authorization`` header the caller set explicitly. No-op when ``token`` is empty.
    """
    if not token:
        return
    base = api_base.rstrip("/").lower()
    original = requests.sessions.Session.request

    def request(self, method, url, **kwargs):
        if str(url).lower().startswith(base):
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault("Authorization", f"Bearer {token}")
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = request


class ApiAccessError(RuntimeError):
    """The API will not let a script do its job: no token, a rejected token, or too little privilege."""


def _span(seconds: float) -> str:
    """Short human form of a duration for messages."""
    seconds = max(int(seconds), 0)
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


def check_api_access(
    api_base: str,
    token: str = "",
    *,
    require_admin: bool = True,
    timeout: float = 30,
    need_seconds: float = 0,
    verify: bool = True,
) -> str:
    """Fail fast, with an actionable message, if the API would reject this script's calls.

    Asks ``GET /me`` who the API thinks the caller is. An API that predates authentication has no ``/me``
    and needs nothing, and one with authentication off answers as an anonymous admin. Returns a one-line
    summary to print, or raises :class:`ApiAccessError`. ``require_admin`` is for scripts that register or
    delete applications, which only admins may do. ``need_seconds`` is how long the script will keep using the
    token: the summary warns when the token will not last that long. ``verify`` is passed to ``requests``
    (False only for test endpoints with self-signed certificates). Call it once the API is known to answer.
    """
    base = api_base.rstrip("/")
    try:
        resp = requests.get(f"{base}/me", headers=auth_headers(token), timeout=timeout, verify=verify)
    except requests.RequestException as exc:
        raise ApiAccessError(f"Cannot reach API at {api_base}: {exc}") from exc

    if resp.status_code == 404:
        return "API does not use authentication (it has no /me endpoint)"
    if resp.status_code == 401:
        if not token:
            raise ApiAccessError(
                "The API requires authentication but no access token was given. "
                f"Set {ENV_VAR} or pass --access-token (create a token with scripts/get_token.py)."
            )
        raise ApiAccessError(
            "The API rejected the access token (HTTP 401): it may have expired or be meant for a "
            "different API. Create a fresh one with scripts/get_token.py."
        )
    try:
        me = resp.json()
    except ValueError:
        me = None
    if resp.status_code != 200 or not isinstance(me, dict):
        raise ApiAccessError(f"Unexpected answer from {base}/me (HTTP {resp.status_code}).")

    if not me.get("auth_enabled"):
        return "Authentication is off on this API"

    who = me.get("name") or me.get("subject") or "an unknown caller"
    roles = ", ".join(me.get("roles") or []) or "none"
    if require_admin and not me.get("is_admin"):
        raise ApiAccessError(
            "This script registers and deletes applications, which needs the admin role, but the token "
            f"is for {who} (roles: {roles}). Use a token for an admin account or service."
        )
    summary = f"Authenticated as {who} ({me.get('kind', 'user')}; roles: {roles})"
    expires_at = me.get("expires_at")
    if isinstance(expires_at, (int, float)):
        remaining = int(expires_at - time.time())
        summary += f"; token valid for another {max(remaining // 60, 0)} min"
        if need_seconds and remaining < need_seconds:
            summary += (
                f". WARNING: that is less than the planned {_span(need_seconds)} run, so the API will start "
                "refusing it partway and the run will stop early. Use a longer-lived token or a shorter run"
            )
    return summary
