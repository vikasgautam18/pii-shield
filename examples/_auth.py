"""Bearer-token support shared by the example/test scripts that call a PII Shield API.

When the API runs with ``AUTH_ENABLED=true`` every call needs an access token::

    export API_ACCESS_TOKEN="$(python scripts/get_token.py)"
    python examples/run_indian_banking_tests.py --api-url https://pii-shield.example.com

The token comes from ``--access-token`` or the ``API_ACCESS_TOKEN`` environment
variable. Without one the scripts behave exactly as before (an API with auth off).

Used by:
  - examples/run_indian_banking_tests.py / run_indian_banking_tests_azure.py  (requests)
  - examples/scale_test.py                                                    (aiohttp)
  - scripts/cleanup_test_apps.py                                              (requests)
"""

from __future__ import annotations

import argparse
import os

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
