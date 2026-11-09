#!/usr/bin/env python3
"""Print an OIDC access token for PII Shield using the client-credentials flow.

For services, scripts and CI that call the API when it runs with AUTH_ENABLED=true.
Cloud-agnostic: the token endpoint is found through standard OIDC discovery, so it
works with Microsoft Entra ID, Keycloak, Okta and other OIDC providers. Only the
access token is written to stdout, so it composes with shell substitution::

    export API_ACCESS_TOKEN="$(python scripts/get_token.py)"
    curl -H "Authorization: Bearer $API_ACCESS_TOKEN" https://pii-shield.example.com/apps

Configuration (flags override the environment; the client secret is read only from
the environment or --client-secret-file so it never appears in a process list)::

    OIDC_ISSUER_URL      issuer / discovery base          (or --issuer-url)
    OIDC_CLIENT_ID       the service's client id          (or --client-id)
    OIDC_CLIENT_SECRET   the service's client secret
    OIDC_SCOPE           scope to request (optional)      (or --scope)
    OIDC_TOKEN_AUDIENCE  'audience' parameter (Auth0, ...)(or --audience)
    OIDC_TOKEN_URL       skip discovery                   (or --token-url)
    OIDC_ALLOW_INSECURE_HTTP=true   permit http:// (local dev IdPs only)

Examples::

    # Keycloak dev profile (docker compose --profile auth-dev up)
    OIDC_ISSUER_URL=http://localhost:8080/realms/pii-shield OIDC_ALLOW_INSECURE_HTTP=true \\
    OIDC_CLIENT_ID=pii-shield-svc OIDC_CLIENT_SECRET=dev-only-service-client-secret \\
      python scripts/get_token.py

    # Microsoft Entra ID: client-credentials tokens use the API's ".default" scope
    OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant-id>/v2.0 \\
    OIDC_CLIENT_ID=<service-app-id> OIDC_CLIENT_SECRET=... \\
    OIDC_SCOPE=api://<api-app-id>/.default \\
      python scripts/get_token.py

Use --show-claims to inspect (without verifying) what the token says -- handy when the
API answers 401/403 and you need to see the audience, issuer or roles it carries.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

TIMEOUT_SECONDS = 15
_TRUE = {"1", "true", "yes", "on"}


class TokenToolError(Exception):
    """A problem worth reporting to the user (never contains secrets)."""


def _require_https(url: str, what: str, allow_http: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise TokenToolError(f"{what} must be an absolute http(s) URL, got {url!r}.")
    if parsed.scheme == "http" and not allow_http:
        raise TokenToolError(
            f"{what} uses plain http, which would send the client secret in clear text. "
            "Set OIDC_ALLOW_INSECURE_HTTP=true only for a local development IdP."
        )


def discover_token_endpoint(issuer_url: str, http: Any = requests) -> str:
    url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = http.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        endpoint = response.json().get("token_endpoint")
    except (requests.RequestException, ValueError) as exc:
        raise TokenToolError(f"OIDC discovery failed for {url}: {exc}") from None
    if not endpoint:
        raise TokenToolError(f"The discovery document at {url} has no token_endpoint.")
    return str(endpoint)


def fetch_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str = "",
    audience: str = "",
    auth_method: str = "post",
    http: Any = requests,
) -> dict[str, Any]:
    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    if audience:
        data["audience"] = audience
    kwargs: dict[str, Any] = {}
    if auth_method == "basic":
        kwargs["auth"] = (client_id, client_secret)
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    try:
        response = http.post(token_url, data=data, timeout=TIMEOUT_SECONDS, **kwargs)
    except requests.RequestException as exc:
        raise TokenToolError(f"Could not reach the token endpoint {token_url}: {exc}") from None
    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = f": {body.get('error', '')} {body.get('error_description', '')}".rstrip()
        except ValueError:
            pass
        raise TokenToolError(f"Token endpoint answered HTTP {response.status_code}{detail}")
    try:
        body = response.json()
    except ValueError:
        raise TokenToolError("Token endpoint did not return JSON.") from None
    if not body.get("access_token"):
        raise TokenToolError("Token response has no access_token.")
    return body


def decode_claims(token: str) -> dict[str, Any]:
    """Decode (WITHOUT verifying) a JWT's header and payload for inspection."""

    def part(segment: str) -> Any:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    pieces = token.split(".")
    if len(pieces) != 3:
        raise TokenToolError("The access token is not a JWT, so there are no claims to show.")
    try:
        return {"header": part(pieces[0]), "payload": part(pieces[1])}
    except (ValueError, json.JSONDecodeError):
        raise TokenToolError("The access token could not be decoded as a JWT.") from None


def _describe(claims: dict[str, Any]) -> str:
    payload = dict(claims["payload"])
    for name in ("exp", "iat", "nbf"):
        if isinstance(payload.get(name), (int, float)):
            when = datetime.fromtimestamp(payload[name], tz=timezone.utc).isoformat()
            payload[f"{name} (utc)"] = when
    return json.dumps({"header": claims["header"], "payload": payload}, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--issuer-url", default=env("OIDC_ISSUER_URL", ""))
    parser.add_argument("--token-url", default=env("OIDC_TOKEN_URL", ""), help="Skip discovery.")
    parser.add_argument("--client-id", default=env("OIDC_CLIENT_ID", ""))
    parser.add_argument("--client-secret-file", help="Read the client secret from this file.")
    parser.add_argument("--scope", default=env("OIDC_SCOPE", ""))
    parser.add_argument("--audience", default=env("OIDC_TOKEN_AUDIENCE", ""))
    parser.add_argument(
        "--auth-method",
        choices=["post", "basic"],
        default="post",
        help="How the client authenticates: in the form body (default) or HTTP Basic.",
    )
    parser.add_argument("--json", action="store_true", help="Print the whole token response as JSON.")
    parser.add_argument("--show-claims", action="store_true", help="Print the token's claims (unverified) to stderr.")
    return parser


def run(argv: Sequence[str] | None = None, http: Any = requests) -> int:
    args = build_parser().parse_args(argv)
    try:
        secret = os.environ.get("OIDC_CLIENT_SECRET", "")
        if args.client_secret_file:
            secret = Path(args.client_secret_file).read_text(encoding="utf-8").strip()
        allow_http = os.environ.get("OIDC_ALLOW_INSECURE_HTTP", "").strip().lower() in _TRUE

        missing = [
            name
            for name, value in (
                ("OIDC_CLIENT_ID / --client-id", args.client_id),
                ("OIDC_CLIENT_SECRET / --client-secret-file", secret),
            )
            if not value
        ]
        if not (args.issuer_url or args.token_url):
            missing.insert(0, "OIDC_ISSUER_URL / --issuer-url (or --token-url)")
        if missing:
            raise TokenToolError("Missing configuration: " + ", ".join(missing))

        if args.token_url:
            token_url = args.token_url
        else:
            _require_https(args.issuer_url, "The issuer URL", allow_http)
            token_url = discover_token_endpoint(args.issuer_url, http)
        _require_https(token_url, "The token endpoint", allow_http)

        body = fetch_token(
            token_url,
            args.client_id,
            secret,
            scope=args.scope,
            audience=args.audience,
            auth_method=args.auth_method,
            http=http,
        )
        if args.show_claims:
            try:
                print(_describe(decode_claims(body["access_token"])), file=sys.stderr)
            except TokenToolError as exc:  # e.g. an opaque (non-JWT) token: still print it
                print(f"[get_token] {exc}", file=sys.stderr)
        print(json.dumps(body) if args.json else body["access_token"])
        return 0
    except (TokenToolError, OSError) as exc:
        print(f"[get_token] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
