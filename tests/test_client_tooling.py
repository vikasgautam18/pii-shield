"""Tests for the service-token tooling: scripts/get_token.py and examples/_auth.py."""

import base64
import importlib.util
import json
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


get_token = _load("pii_get_token", REPO_ROOT / "scripts" / "get_token.py")
examples_auth = _load("pii_examples_auth", REPO_ROOT / "examples" / "_auth.py")

SECRET = "super-secret-client-value-123"
ISSUER = "https://idp.test/realms/pii"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"


def b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def fake_jwt(payload=None) -> str:
    payload = {"sub": "svc", "aud": "pii-shield-api", "roles": ["PIIShield.User"], "exp": 1893456000, **(payload or {})}
    return f"{b64({'alg': 'RS256', 'kid': 'k1'})}.{b64(payload)}.signature-part"


class FakeResponse:
    def __init__(self, status=200, body=None, not_json=False):
        self.status_code = status
        self._body = body if body is not None else {}
        self._not_json = not_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._not_json:
            raise ValueError("not json")
        return self._body


class FakeHttp:
    def __init__(self, *, token_endpoint=TOKEN_URL, token_response=None, discovery_error=None, post_error=None):
        self.token_endpoint = token_endpoint
        self.token_response = token_response or FakeResponse(200, {"access_token": "tok-123", "expires_in": 300})
        self.discovery_error = discovery_error
        self.post_error = post_error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, None, kwargs))
        if self.discovery_error:
            raise self.discovery_error
        return FakeResponse(200, {"issuer": ISSUER, "token_endpoint": self.token_endpoint})

    def post(self, url, data=None, **kwargs):
        self.calls.append(("POST", url, data, kwargs))
        if self.post_error:
            raise self.post_error
        return self.token_response


@pytest.fixture()
def env(monkeypatch):
    for name in ("OIDC_ISSUER_URL", "OIDC_TOKEN_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_SCOPE",
                 "OIDC_TOKEN_AUDIENCE", "OIDC_ALLOW_INSECURE_HTTP", "API_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture()
def configured(env):
    env.setenv("OIDC_ISSUER_URL", ISSUER)
    env.setenv("OIDC_CLIENT_ID", "svc-client")
    env.setenv("OIDC_CLIENT_SECRET", SECRET)
    return env


class TestGetToken:
    def test_prints_only_the_access_token(self, configured, capsys):
        http = FakeHttp()
        assert get_token.run([], http=http) == 0
        captured = capsys.readouterr()
        assert captured.out == "tok-123\n"
        assert captured.err == ""
        assert http.calls[0][:2] == ("GET", DISCOVERY_URL)
        method, url, data, _ = http.calls[1]
        assert (method, url) == ("POST", TOKEN_URL)
        assert data == {
            "grant_type": "client_credentials",
            "client_id": "svc-client",
            "client_secret": SECRET,
        }

    def test_scope_and_audience_are_sent(self, configured):
        http = FakeHttp()
        get_token.run(["--scope", "api://abc/.default", "--audience", "https://api"], http=http)
        data = http.calls[1][2]
        assert data["scope"] == "api://abc/.default" and data["audience"] == "https://api"

    def test_environment_provides_scope(self, configured):
        configured.setenv("OIDC_SCOPE", "pii-shield")
        http = FakeHttp()
        get_token.run([], http=http)
        assert http.calls[1][2]["scope"] == "pii-shield"

    def test_basic_auth_keeps_the_secret_out_of_the_body(self, configured):
        http = FakeHttp()
        get_token.run(["--auth-method", "basic"], http=http)
        _, _, data, kwargs = http.calls[1]
        assert kwargs["auth"] == ("svc-client", SECRET)
        assert "client_secret" not in data and "client_id" not in data

    def test_token_url_skips_discovery(self, env):
        env.setenv("OIDC_CLIENT_ID", "svc")
        env.setenv("OIDC_CLIENT_SECRET", SECRET)
        http = FakeHttp()
        assert get_token.run(["--token-url", TOKEN_URL], http=http) == 0
        assert [c[0] for c in http.calls] == ["POST"]

    def test_json_output(self, configured, capsys):
        get_token.run(["--json"], http=FakeHttp())
        assert json.loads(capsys.readouterr().out)["access_token"] == "tok-123"

    def test_secret_can_come_from_a_file(self, env, tmp_path):
        env.setenv("OIDC_ISSUER_URL", ISSUER)
        env.setenv("OIDC_CLIENT_ID", "svc")
        secret_file = tmp_path / "secret"
        secret_file.write_text(f"{SECRET}\n")
        http = FakeHttp()
        assert get_token.run(["--client-secret-file", str(secret_file)], http=http) == 0
        assert http.calls[1][2]["client_secret"] == SECRET

    def test_unreadable_secret_file_is_reported(self, env, capsys):
        env.setenv("OIDC_ISSUER_URL", ISSUER)
        env.setenv("OIDC_CLIENT_ID", "svc")
        assert get_token.run(["--client-secret-file", "/nonexistent/secret"], http=FakeHttp()) == 1
        assert "[get_token]" in capsys.readouterr().err

    def test_missing_configuration_is_listed_without_any_network_call(self, env, capsys):
        http = FakeHttp()
        assert get_token.run([], http=http) == 1
        err = capsys.readouterr().err
        for name in ("OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
            assert name in err
        assert http.calls == []

    def test_plain_http_is_refused_before_the_secret_is_sent(self, configured, capsys):
        configured.setenv("OIDC_ISSUER_URL", "http://keycloak.local/realms/pii")
        http = FakeHttp()
        assert get_token.run([], http=http) == 1
        assert "plain http" in capsys.readouterr().err
        assert http.calls == []

    def test_plain_http_is_allowed_with_the_explicit_opt_in(self, configured):
        configured.setenv("OIDC_ISSUER_URL", "http://localhost:8080/realms/pii")
        configured.setenv("OIDC_ALLOW_INSECURE_HTTP", "true")
        http = FakeHttp(token_endpoint="http://localhost:8080/realms/pii/protocol/openid-connect/token")
        assert get_token.run([], http=http) == 0

    def test_an_http_token_endpoint_announced_by_discovery_is_refused(self, configured, capsys):
        http = FakeHttp(token_endpoint="http://idp.test/token")
        assert get_token.run([], http=http) == 1
        assert "token endpoint" in capsys.readouterr().err.lower()
        assert [c[0] for c in http.calls] == ["GET"]  # the secret was never posted

    def test_error_responses_are_explained_without_leaking_the_secret(self, configured, capsys):
        bad = FakeResponse(401, {"error": "invalid_client", "error_description": "Client authentication failed"})
        assert get_token.run([], http=FakeHttp(token_response=bad)) == 1
        captured = capsys.readouterr()
        assert "HTTP 401" in captured.err and "invalid_client" in captured.err
        assert captured.out == ""
        assert SECRET not in captured.err

    @pytest.mark.parametrize(
        "http",
        [
            FakeHttp(discovery_error=requests.ConnectionError("dns failure")),
            FakeHttp(post_error=requests.ConnectionError("refused")),
            FakeHttp(token_response=FakeResponse(200, not_json=True)),
            FakeHttp(token_response=FakeResponse(200, {"token_type": "Bearer"})),
        ],
        ids=["discovery-down", "token-endpoint-down", "not-json", "no-access-token"],
    )
    def test_failures_exit_nonzero_and_never_print_the_secret(self, configured, capsys, http):
        assert get_token.run([], http=http) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and SECRET not in captured.err

    def test_show_claims_prints_decoded_claims_to_stderr_only(self, configured, capsys):
        token = fake_jwt()
        http = FakeHttp(token_response=FakeResponse(200, {"access_token": token}))
        assert get_token.run(["--show-claims"], http=http) == 0
        captured = capsys.readouterr()
        assert captured.out == token + "\n"
        claims = json.loads(captured.err)
        assert claims["payload"]["roles"] == ["PIIShield.User"]
        assert claims["header"]["kid"] == "k1"
        assert "2030-" in claims["payload"]["exp (utc)"]
        assert "signature-part" not in captured.err

    def test_show_claims_on_an_opaque_token_still_prints_the_token(self, configured, capsys):
        assert get_token.run(["--show-claims"], http=FakeHttp()) == 0
        captured = capsys.readouterr()
        assert captured.out == "tok-123\n"
        assert "not a JWT" in captured.err

    def test_decode_claims_handles_unpadded_base64url(self):
        claims = get_token.decode_claims(fake_jwt({"x": "a"}))
        assert claims["payload"]["x"] == "a"
        with pytest.raises(get_token.TokenToolError):
            get_token.decode_claims("only.two")
        with pytest.raises(get_token.TokenToolError):
            get_token.decode_claims("%%%.%%%.%%%")


class TestBearerHelper:
    @pytest.fixture()
    def sent(self, monkeypatch):
        """Record what reaches requests.Session.request; the helper wraps this recorder."""
        record = []

        def recorder(self, method, url, **kwargs):
            record.append({"method": method, "url": url, "headers": kwargs.get("headers")})
            return FakeResponse(200, {})

        monkeypatch.setattr(requests.sessions.Session, "request", recorder)
        return record

    def test_default_comes_from_the_environment_and_the_flag_wins(self, env):
        import argparse

        env.setenv("API_ACCESS_TOKEN", "from-env")
        parser = argparse.ArgumentParser()
        examples_auth.add_token_argument(parser)
        assert parser.parse_args([]).access_token == "from-env"
        assert parser.parse_args(["--access-token", "from-flag"]).access_token == "from-flag"
        env.delenv("API_ACCESS_TOKEN")
        parser = argparse.ArgumentParser()
        examples_auth.add_token_argument(parser)
        assert parser.parse_args([]).access_token == ""

    def test_auth_headers(self):
        assert examples_auth.auth_headers("t") == {"Authorization": "Bearer t"}
        assert examples_auth.auth_headers("") == {}

    def test_adds_the_header_to_requests_under_the_api_base(self, sent):
        examples_auth.install_bearer_token("tok", "http://api.test:8000/")
        requests.get("http://api.test:8000/apps", timeout=5)
        requests.post("http://api.test:8000/anonymize_unique", json={}, headers={"X-App-Id": "a1"})
        with requests.Session() as session:
            session.delete("http://API.test:8000/apps/x")
        assert sent[0]["headers"] == {"Authorization": "Bearer tok"}
        assert sent[1]["headers"] == {"X-App-Id": "a1", "Authorization": "Bearer tok"}
        assert sent[2]["headers"] == {"Authorization": "Bearer tok"}

    def test_never_sends_the_token_to_other_hosts(self, sent):
        examples_auth.install_bearer_token("tok", "http://api.test:8000")
        requests.get("http://elsewhere.test/apps")
        requests.get("http://api.test:8001/apps")
        assert all(call["headers"] is None for call in sent)

    def test_does_not_override_an_explicit_authorization_header(self, sent):
        examples_auth.install_bearer_token("tok", "http://api.test")
        requests.get("http://api.test/apps", headers={"Authorization": "Basic abc"})
        assert sent[0]["headers"] == {"Authorization": "Basic abc"}

    def test_empty_token_changes_nothing(self, sent):
        before = requests.sessions.Session.request
        examples_auth.install_bearer_token("", "http://api.test")
        assert requests.sessions.Session.request is before
        requests.get("http://api.test/apps")
        assert sent[0]["headers"] is None
