"""Tests for app.auth: token validation, principal mapping and the signing-key cache."""

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth import AuthUnavailable, TokenError
from tests._auth_helpers import FakeClock, FakeIdp, SigningKey

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def idp():
    return FakeIdp()


def reason_of(excinfo) -> str:
    return excinfo.value.reason


class TestValidTokens:
    def test_user_token_maps_to_principal(self, idp):
        principal = idp.validator().validate(idp.token())
        assert principal.subject == "user-123"
        assert principal.username == "alice@example.com"
        assert principal.name == "Alice Example"
        assert principal.kind == "user"
        assert principal.roles == {"PIIShield.User"}
        assert principal.is_user and not principal.is_admin
        assert principal.actor == "alice@example.com"
        assert principal.expires_at is not None

    def test_admin_role_implies_user(self, idp):
        principal = idp.validator().validate(idp.token(roles=["PIIShield.Admin"]))
        assert principal.is_admin and principal.is_user

    def test_unrecognised_role_grants_nothing(self, idp):
        principal = idp.validator().validate(idp.token(roles=["something-else"]))
        assert not principal.is_user and not principal.is_admin

    def test_token_without_roles_grants_nothing(self, idp):
        principal = idp.validator().validate(idp.token(roles=None))
        assert principal.roles == frozenset()
        assert not principal.is_user

    def test_entra_style_service_token(self, idp):
        token = idp.token(preferred_username=None, name=None, azp="svc-client", idtyp="app")
        principal = idp.validator().validate(token)
        assert principal.kind == "service"
        assert principal.client_id == "svc-client"
        assert principal.actor == "service:svc-client"

    def test_keycloak_service_account_token(self, idp):
        token = idp.token(preferred_username="service-account-pii-shield-svc", azp="pii-shield-svc")
        principal = idp.validator().validate(token)
        assert principal.kind == "service"
        assert principal.actor == "service:pii-shield-svc"

    def test_actor_falls_back_to_name_then_subject(self, idp):
        v = idp.validator()
        assert v.validate(idp.token(preferred_username=None)).actor == "Alice Example"
        assert v.validate(idp.token(preferred_username=None, name=None)).actor == "user-123"

    def test_roles_from_nested_claim_path(self, idp):
        v = idp.validator(OIDC_ROLES_CLAIM="realm_access.roles")
        token = idp.token(roles=None, realm_access={"roles": ["PIIShield.Admin"]})
        assert v.validate(token).is_admin

    def test_roles_from_space_delimited_scope_claim(self, idp):
        v = idp.validator(OIDC_ROLES_CLAIM="scp", AUTH_USER_ROLES="access_as_user")
        token = idp.token(roles=None, scp="access_as_user other")
        assert v.validate(token).is_user

    def test_any_configured_audience_is_accepted(self, idp):
        v = idp.validator(OIDC_AUDIENCE="api://abc,pii-shield-api")
        assert v.validate(idp.token(aud="api://abc")).is_user
        assert v.validate(idp.token(aud=["other", "pii-shield-api"])).is_user

    def test_issuer_override_replaces_discovery_value(self, idp):
        idp.discovery_issuer = "https://internal-name.test/realms/pii"
        v = idp.validator(OIDC_ISSUER=idp.issuer)
        assert v.validate(idp.token()).is_user


class TestRejectedTokens:
    def test_expired_token(self, idp):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(idp.token(exp=1))
        assert reason_of(exc) == "expired"

    def test_clock_skew_leeway(self, idp):
        import time

        recently_expired = int(time.time()) - 30
        assert idp.validator(OIDC_CLOCK_SKEW_SECONDS="60").validate(idp.token(exp=recently_expired)).is_user
        with pytest.raises(TokenError) as exc:
            idp.validator(OIDC_CLOCK_SKEW_SECONDS="0").validate(idp.token(exp=recently_expired))
        assert reason_of(exc) == "expired"

    def test_token_not_yet_valid(self, idp):
        import time

        with pytest.raises(TokenError) as exc:
            idp.validator().validate(idp.token(nbf=int(time.time()) + 3600))
        assert reason_of(exc) == "not_yet_valid"

    def test_wrong_audience(self, idp):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(idp.token(aud="someone-else"))
        assert reason_of(exc) == "bad_audience"

    def test_wrong_issuer(self, idp):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(idp.token(iss="https://evil.test/realms/pii"))
        assert reason_of(exc) == "bad_issuer"

    def test_signature_from_a_different_key(self, idp):
        impostor = SigningKey.shared("impostor")
        token = idp.token(key=impostor, headers={"kid": "key-1"})
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(token)
        assert reason_of(exc) == "bad_signature"

    @pytest.mark.parametrize("claim", ["exp", "aud", "iss"])
    def test_required_claims(self, idp, claim):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(idp.token(**{claim: None}))
        assert reason_of(exc) == "missing_claim"

    def test_alg_none_is_rejected(self, idp):
        token = jwt.encode(idp.claims(), key=None, algorithm="none", headers={"kid": "key-1"})
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(token)
        assert reason_of(exc) == "alg_not_allowed"

    def test_symmetric_algorithms_are_rejected(self, idp):
        token = jwt.encode(idp.claims(), "x" * 64, algorithm="HS256", headers={"kid": "key-1"})
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(token)
        assert reason_of(exc) == "alg_not_allowed"

    def test_token_without_kid(self, idp):
        token = jwt.encode(idp.claims(), idp.key.private_key, algorithm="RS256")
        with pytest.raises(TokenError) as exc:
            idp.validator().validate(token)
        assert reason_of(exc) == "missing_kid"

    def test_malformed_token(self, idp):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate("not-a-jwt")
        assert reason_of(exc) == "malformed"

    def test_oversized_token(self, idp):
        with pytest.raises(TokenError) as exc:
            idp.validator().validate("a" * 20000)
        assert reason_of(exc) == "token_too_large"

    def test_algorithm_must_match_key_type(self, idp):
        ec_key = ec.generate_private_key(ec.SECP256R1())
        token = jwt.encode(idp.claims(), ec_key, algorithm="ES256", headers={"kid": "key-1"})
        v = idp.validator(OIDC_ALGORITHMS="RS256,ES256")
        with pytest.raises(TokenError) as exc:
            v.validate(token)
        assert reason_of(exc) == "key_type_mismatch"


class TestSigningKeyCache:
    def test_first_use_fetches_discovery_and_keys_then_caches(self, idp):
        v = idp.validator()
        v.validate(idp.token())
        assert idp.calls == [idp.discovery_url, idp.jwks_url]
        v.validate(idp.token())
        assert len(idp.calls) == 2

    def test_overrides_skip_discovery(self, idp):
        v = idp.validator(OIDC_ISSUER=idp.issuer, OIDC_JWKS_URL=idp.jwks_url)
        v.validate(idp.token())
        assert idp.calls == [idp.jwks_url]

    def test_rotated_key_is_picked_up_after_the_throttle_window(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock)
        v.validate(idp.token())

        new_key = SigningKey.shared("key-2")
        idp.keys.append(new_key)
        token = idp.token(key=new_key)

        clock.advance(10)  # inside the throttle window: no refresh, unknown key
        with pytest.raises(TokenError) as exc:
            v.validate(token)
        assert reason_of(exc) == "unknown_kid"
        assert idp.fetch_count(idp.jwks_url) == 1

        clock.advance(25)  # window elapsed: refresh finds the new key
        assert v.validate(token).is_user
        assert idp.fetch_count(idp.jwks_url) == 2

    def test_random_key_ids_cannot_amplify_requests_to_the_idp(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock)
        v.validate(idp.token())
        baseline = idp.fetch_count(idp.jwks_url)

        for i in range(25):
            clock.advance(0.5)
            with pytest.raises(TokenError):
                v.validate(idp.token(headers={"kid": f"random-{i}"}))
        assert idp.fetch_count(idp.jwks_url) <= baseline + 1

    def test_outage_at_cold_start_fails_closed_then_recovers(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock)
        idp.fail = True
        token = idp.token()
        with pytest.raises(AuthUnavailable):
            v.validate(token)
        attempts = len(idp.calls)

        with pytest.raises(AuthUnavailable):  # throttled: no extra IdP traffic
            v.validate(token)
        assert len(idp.calls) == attempts

        idp.fail = False
        clock.advance(6)
        assert v.validate(token).is_user

    def test_cached_keys_keep_working_when_the_idp_goes_down(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock, OIDC_JWKS_CACHE_SECONDS="60")
        v.validate(idp.token())

        idp.fail = True
        clock.advance(61)  # cache expired, refresh fails -> stale keys still serve
        assert v.validate(idp.token()).is_user

    def test_stale_keys_stop_serving_after_the_maximum_stale_age(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock, OIDC_JWKS_CACHE_SECONDS="60")
        v.validate(idp.token())

        idp.fail = True
        clock.advance(60 + 86400 + 1)
        with pytest.raises(AuthUnavailable):
            v.validate(idp.token())

    def test_a_hung_refresh_does_not_stall_requests_that_have_a_usable_key(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock, OIDC_JWKS_CACHE_SECONDS="60")
        v.validate(idp.token())
        clock.advance(61)  # cache expired: every request now wants a refresh

        idp.hold = threading.Event()  # the identity provider hangs
        refresher = threading.Thread(target=lambda: v.validate(idp.token()))
        refresher.start()  # takes the refresh and blocks inside the IdP call
        assert idp.in_flight.wait(timeout=5)

        outcome: dict[str, object] = {}
        waiter = threading.Thread(target=lambda: outcome.setdefault("principal", v.validate(idp.token())))
        waiter.start()
        waiter.join(timeout=2)
        stalled = waiter.is_alive()
        idp.hold.set()  # let the hung refresh finish either way
        refresher.join(timeout=5)
        waiter.join(timeout=5)

        assert not stalled, "a request waited for the in-flight refresh although it holds a usable key"
        assert outcome["principal"].is_user
        assert idp.fetch_count(idp.jwks_url) == 2  # the initial load and the single refresh

    def test_a_request_without_a_usable_key_waits_for_the_refresh_in_flight(self, idp):
        clock = FakeClock()
        v = idp.validator(clock=clock)
        v.validate(idp.token())
        new_key = SigningKey.shared("key-2")
        idp.keys.append(new_key)
        token = idp.token(key=new_key)
        clock.advance(40)  # past the throttle window, cache still fresh

        idp.hold = threading.Event()
        refresher = threading.Thread(target=lambda: v.validate(token))
        refresher.start()
        assert idp.in_flight.wait(timeout=5)

        outcome: dict[str, object] = {}
        waiter = threading.Thread(target=lambda: outcome.setdefault("principal", v.validate(token)))
        waiter.start()
        waiter.join(timeout=0.5)
        waiting = waiter.is_alive()  # no cached key for key-2: it must wait for the refresh, not fail
        idp.hold.set()
        refresher.join(timeout=5)
        waiter.join(timeout=5)

        assert waiting
        assert outcome["principal"].is_user
        assert idp.fetch_count(idp.jwks_url) == 2  # one refresh served both callers

    def test_discovery_issuer_mismatch_fails_closed(self, idp, caplog):
        idp.discovery_issuer = "https://elsewhere.test/realms/pii"
        v = idp.validator()
        with caplog.at_level(logging.WARNING, logger="pii-shield.auth"):
            with pytest.raises(AuthUnavailable):
                v.validate(idp.token())
        assert "does not match OIDC_ISSUER_URL" in caplog.text

    def test_unusable_keys_are_skipped_but_valid_ones_survive(self, idp):
        good = idp.key.jwk()
        idp.jwks_override = {
            "keys": [
                {"kty": "RSA", "kid": "enc-key", "use": "enc", "n": "AQAB", "e": "AQAB"},
                {"kty": "RSA", "kid": "broken", "use": "sig", "n": "not-base64!", "e": "AQAB"},
                {"kty": "oct", "kid": "symmetric", "k": "c2VjcmV0"},
                "garbage",
                good,
            ]
        }
        assert idp.validator().validate(idp.token()).is_user

    def test_empty_jwks_fails_closed(self, idp):
        idp.jwks_override = {"keys": []}
        with pytest.raises(AuthUnavailable):
            idp.validator().validate(idp.token())

    def test_warm_up_never_raises(self, idp):
        v = idp.validator()
        assert v.warm_up() is True
        idp.fail = True
        assert idp.validator().warm_up() is False

    def test_failures_do_not_log_the_token(self, idp, caplog):
        token = idp.token()
        idp.fail = True
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(AuthUnavailable):
                idp.validator().validate(token)
        assert token not in caplog.text
        assert token.split(".")[2] not in caplog.text


class TestAsyncValidation:
    @staticmethod
    def run(coro):
        # asyncio.run() would leave this thread without a current event loop and break
        # older tests that call asyncio.get_event_loop(); use a private loop instead.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_async_validation_matches_sync(self, idp):
        v = idp.validator()
        principal = self.run(v.avalidate(idp.token(roles=["PIIShield.Admin"])))
        assert principal.is_admin
        # Second call is served from the cache without touching the IdP again.
        calls = len(idp.calls)
        self.run(v.avalidate(idp.token()))
        assert len(idp.calls) == calls

    def test_async_validation_propagates_errors(self, idp):
        v = idp.validator()
        with pytest.raises(TokenError):
            self.run(v.avalidate(idp.token(aud="nope")))
        idp.fail = True
        with pytest.raises(AuthUnavailable):
            self.run(idp.validator().avalidate(idp.token()))


def test_disabled_mode_never_imports_pyjwt():
    code = textwrap.dedent(
        """
        import asyncio, sys
        from starlette.requests import Request
        from app import auth

        scope = {"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""}
        principal = asyncio.run(auth.authenticate(Request(scope)))
        assert principal.is_admin and principal.is_user
        assert "jwt" not in sys.modules, "PyJWT must not be imported when auth is disabled"
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTH_", "OIDC_"))}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
