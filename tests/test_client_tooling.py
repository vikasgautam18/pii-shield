"""Tests for the service-token tooling: scripts/get_token.py and examples/_auth.py (including the access
preflight the test runners run before they start)."""

import base64
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
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


ME_ADMIN = {"auth_enabled": True, "subject": "svc-1", "name": "Admin Svc", "kind": "service",
            "roles": ["PIIShield.Admin"], "is_user": True, "is_admin": True}
ME_USER = {"auth_enabled": True, "subject": "svc-2", "name": "User Svc", "kind": "service",
           "roles": ["PIIShield.User"], "is_user": True, "is_admin": False}


class TestApiAccessCheck:
    @staticmethod
    def serve(monkeypatch, response):
        """Make requests.get answer with ``response`` (raised if it is an exception); return the calls."""
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(requests, "get", fake_get)
        return calls

    def test_an_admin_token_passes_and_is_described(self, monkeypatch):
        calls = self.serve(monkeypatch, FakeResponse(200, {**ME_ADMIN, "expires_at": time.time() + 1830}))
        summary = examples_auth.check_api_access("https://api.test/", "tok")
        assert "Admin Svc" in summary and "PIIShield.Admin" in summary and "service" in summary
        assert "30 min" in summary
        url, kwargs = calls[0]
        assert url == "https://api.test/me"
        assert kwargs["headers"] == examples_auth.auth_headers("tok")

    def test_a_user_token_is_refused_when_admin_is_needed(self, monkeypatch):
        self.serve(monkeypatch, FakeResponse(200, ME_USER))
        with pytest.raises(examples_auth.ApiAccessError, match="admin role.*User Svc.*PIIShield.User"):
            examples_auth.check_api_access("https://api.test", "tok")
        assert "User Svc" in examples_auth.check_api_access("https://api.test", "tok", require_admin=False)

    def test_a_token_without_roles_is_described_as_having_none(self, monkeypatch):
        self.serve(monkeypatch, FakeResponse(200, {**ME_USER, "roles": [], "is_user": False}))
        with pytest.raises(examples_auth.ApiAccessError, match="roles: none"):
            examples_auth.check_api_access("https://api.test", "tok")

    def test_a_missing_token_and_a_rejected_token_get_different_advice(self, monkeypatch):
        self.serve(monkeypatch, FakeResponse(401, {"detail": "Authentication required."}))
        with pytest.raises(examples_auth.ApiAccessError, match="no access token.*API_ACCESS_TOKEN"):
            examples_auth.check_api_access("https://api.test")
        with pytest.raises(examples_auth.ApiAccessError, match="rejected the access token"):
            examples_auth.check_api_access("https://api.test", "stale")

    def test_an_api_without_me_predates_authentication(self, monkeypatch):
        self.serve(monkeypatch, FakeResponse(404, {"detail": "Not Found"}))
        assert "does not use authentication" in examples_auth.check_api_access("https://api.test")

    def test_authentication_off_needs_neither_a_token_nor_the_admin_role(self, monkeypatch):
        anonymous = {"auth_enabled": False, "kind": "anonymous", "roles": [],
                     "is_user": True, "is_admin": True}
        self.serve(monkeypatch, FakeResponse(200, anonymous))
        assert "off" in examples_auth.check_api_access("https://api.test")

    @pytest.mark.parametrize(
        "response",
        [FakeResponse(503, {}), FakeResponse(200, not_json=True), FakeResponse(200, ["not", "a", "mapping"])],
    )
    def test_unexpected_answers_are_reported(self, monkeypatch, response):
        self.serve(monkeypatch, response)
        with pytest.raises(examples_auth.ApiAccessError, match="Unexpected answer"):
            examples_auth.check_api_access("https://api.test", "tok")

    def test_an_unreachable_api_is_reported(self, monkeypatch):
        self.serve(monkeypatch, requests.ConnectionError("refused"))
        with pytest.raises(examples_auth.ApiAccessError, match="Cannot reach API at https://api.test"):
            examples_auth.check_api_access("https://api.test", "tok")


class _FakeApiHandler(http.server.BaseHTTPRequestHandler):
    """Answers canned routes. A route key is ``"/path"`` (any method) or ``"POST /path"``; its value is a
    ``(status, json body)`` pair, or a list of them that is answered in order (the last one then repeats)."""

    routes: dict = {}

    def _respond(self, method):
        answer = self.routes.get(f"{method} {self.path}") or self.routes.get(self.path) or (404, {"detail": "Not Found"})
        if isinstance(answer, list):
            status, body = answer.pop(0) if len(answer) > 1 else answer[0]
        else:
            status, body = answer
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 (http.server naming)
        self._respond("GET")

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._respond("POST")

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_api():
    """Start a tiny HTTP server answering canned routes (see ``_FakeApiHandler``)."""
    servers = []

    def start(routes):
        handler = type("Handler", (_FakeApiHandler,), {"routes": routes})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("runner", ["run_indian_banking_tests.py", "run_indian_banking_tests_azure.py"])
class TestRunnerPreflight:
    """Both runners must stop before the first test when the token cannot do the job."""

    @staticmethod
    def run(runner, url, tmp_path, token=""):
        env = {k: v for k, v in os.environ.items() if k != "API_ACCESS_TOKEN"}
        env["PYTHONIOENCODING"] = "utf-8"
        command = [sys.executable, str(REPO_ROOT / "examples" / runner),
                   "--api-url", url, "--output", str(tmp_path / "report.html")]
        if token:
            command += ["--access-token", token]
        return subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True,
                              encoding="utf-8", timeout=120)

    def test_stops_when_the_api_wants_a_token(self, runner, fake_api, tmp_path):
        denied = (401, {"detail": "Authentication required."})
        url = fake_api({"/apps": denied, "/me": denied})
        result = self.run(runner, url, tmp_path)
        assert result.returncode == 1
        assert "requires authentication" in result.stdout and "API_ACCESS_TOKEN" in result.stdout
        assert "FAILED" not in result.stdout
        assert not (tmp_path / "report.html").exists()

    def test_stops_when_the_token_is_not_an_admin(self, runner, fake_api, tmp_path):
        url = fake_api({"/apps": (200, []), "/me": (200, ME_USER)})
        result = self.run(runner, url, tmp_path, token="user-token")
        assert result.returncode == 1
        assert "admin role" in result.stdout and "User Svc" in result.stdout
        assert "FAILED" not in result.stdout
        assert not (tmp_path / "report.html").exists()

    @pytest.mark.parametrize(
        "routes",
        [
            # the setup section's lookup of existing apps is refused after the connectivity check succeeded
            {"/me": (200, ME_ADMIN), "GET /apps": [(200, []), (401, {"detail": "Authentication required."})]},
            # the lookup works but registering the first application is refused
            {"/me": (200, ME_ADMIN), "GET /apps": (200, []),
             "POST /apps": (401, {"detail": "Authentication required."})},
        ],
        ids=["lookup", "register"],
    )
    def test_stops_at_once_when_the_token_stops_working_midway(self, runner, fake_api, tmp_path, routes):
        """A token that passes the preflight but is rejected later (it expired) must not fail every section."""
        result = self.run(runner, fake_api(routes), tmp_path, token="admin-token")
        assert result.returncode == 1
        assert "Authenticated as Admin Svc" in result.stdout  # the preflight itself passed
        assert "stopped accepting the access token" in result.stdout and "Fetch a fresh token" in result.stdout
        assert result.stdout.count("FAILED") == 1  # it stopped at the first failure instead of failing every section
        assert "401" in result.stdout and "string indices" not in result.stdout
        assert not (tmp_path / "report.html").exists()
