"""Tests for app.ui_auth: pure gate logic, the login gate glue and the API client.

Streamlit itself is replaced by a small recording fake, so these run without a
Streamlit runtime or browser; the real login flow is covered by the end-to-end checks.
"""

import contextlib
import time
from types import SimpleNamespace

import pytest
import requests

from app import ui_auth
from app.ui_auth import (
    ApiClient,
    UiSession,
    bearer_headers,
    evaluate_me,
    is_expired,
    require_login,
    should_rerun_after_401,
    token_fingerprint,
)

API_BASE = "http://api.test"


class Stopped(Exception):
    """Raised by the fake ``st.stop()`` to emulate Streamlit ending the script run."""


class Rerun(Exception):
    """Raised by the fake ``st.rerun()``."""


class _Sidebar:
    def __init__(self, fake):
        self._fake = fake

    def __enter__(self):
        return self._fake

    def __exit__(self, *exc):
        return False


class FakeStreamlit:
    _ELEMENTS = {"title", "subheader", "write", "warning", "error", "caption", "markdown", "button"}

    def __init__(self, *, logged_in=True, token="access-token-abc"):
        self.session_state = {}
        self.events = []
        self.spinning = False
        self.sidebar = _Sidebar(self)
        user = SimpleNamespace(tokens={"access": token} if token else {})
        if logged_in is not None:  # None emulates an app without OIDC configuration
            user.is_logged_in = logged_in
        self.user = user

    @contextlib.contextmanager
    def spinner(self, text="", **kwargs):
        self.events.append(("spinner", (text,), kwargs))
        self.spinning = True
        try:
            yield
        finally:
            self.spinning = False

    def stop(self):
        raise Stopped

    def rerun(self):
        raise Rerun

    def login(self):
        pass

    def logout(self):
        pass

    def __getattr__(self, name):
        if name in self._ELEMENTS:
            def record(*args, **kwargs):
                self.events.append((name, args, kwargs))
            return record
        raise AttributeError(name)

    def kinds(self):
        return [kind for kind, _, _ in self.events]

    def text(self):
        return " ".join(str(a) for _, args, _ in self.events for a in args)


def me_body(**overrides):
    body = {
        "auth_enabled": True,
        "subject": "user-1",
        "name": "Alice Example",
        "kind": "user",
        "roles": ["PIIShield.User"],
        "is_user": True,
        "is_admin": False,
        "expires_at": int(time.time()) + 600,
    }
    body.update(overrides)
    return body


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")


@pytest.fixture()
def fake_st(monkeypatch):
    fake = FakeStreamlit()
    monkeypatch.setattr(ui_auth, "st", fake)
    return fake


def stub_me(monkeypatch, status=200, body=None, calls=None):
    def fetch(api_base, token):
        if calls is not None:
            calls.append((api_base, token))
        return status, me_body() if body is None and status == 200 else body

    monkeypatch.setattr(ui_auth, "_fetch_me", fetch)


class TestPureLogic:
    @pytest.mark.parametrize(
        "status,body,required,expected",
        [
            (None, None, "user", "unreachable"),
            (401, None, "user", "expired"),
            (500, None, "user", "error"),
            (200, None, "user", "error"),
            (200, "text", "user", "error"),
            (200, {"is_user": True}, "user", "ok"),
            (200, {"is_user": False}, "user", "no_access"),
            (200, {}, "user", "no_access"),
            (200, {"is_user": True, "is_admin": True}, "admin", "ok"),
            (200, {"is_user": True, "is_admin": False}, "admin", "admin_required"),
            (200, {"is_user": False, "is_admin": False}, "admin", "no_access"),
        ],
    )
    def test_evaluate_me(self, status, body, required, expected):
        assert evaluate_me(status, body, required) == expected

    def test_is_expired_uses_a_skew(self):
        assert is_expired(None, 1000.0) is False
        assert is_expired(2000, 1000.0) is False
        assert is_expired(1020, 1000.0) is True  # inside the 30 s skew
        assert is_expired(1000, 1000.0) is True

    def test_token_fingerprint_is_stable_and_does_not_leak_the_token(self):
        token = "header.payload.signature"
        assert token_fingerprint(token) == token_fingerprint(token)
        assert token_fingerprint(token) != token_fingerprint(token + "x")
        assert token not in token_fingerprint(token)
        assert len(token_fingerprint(token)) == 16

    def test_bearer_headers(self):
        assert bearer_headers("abc") == {"Authorization": "Bearer abc"}
        assert bearer_headers("") == {} and bearer_headers(None) == {}

    def test_rerun_only_once_per_token(self):
        assert should_rerun_after_401("fp1", None) is True
        assert should_rerun_after_401("fp1", "fp1") is False
        assert should_rerun_after_401("fp2", "fp1") is True

    def test_session_headers(self):
        assert UiSession(enabled=True, token="t").headers() == {"Authorization": "Bearer t"}
        assert UiSession(enabled=False, token="t").headers() == {}
        assert UiSession(enabled=False).headers() == {}


class TestRequireLogin:
    def test_disabled_never_touches_streamlit_user(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        explosive = SimpleNamespace()  # any attribute access on st would raise
        monkeypatch.setattr(ui_auth, "st", explosive)
        session = require_login(required="admin", api_base=API_BASE)
        assert session == UiSession(enabled=False)
        assert session.headers() == {}

    def test_not_signed_in_shows_sign_in_and_stops(self, enabled, monkeypatch):
        fake = FakeStreamlit(logged_in=False)
        monkeypatch.setattr(ui_auth, "st", fake)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "Sign in" in fake.text()
        assert "button" in fake.kinds()

    def test_missing_oidc_configuration_is_reported(self, enabled, monkeypatch):
        fake = FakeStreamlit(logged_in=None)
        monkeypatch.setattr(ui_auth, "st", fake)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "ui_launcher" in fake.text()

    def test_missing_access_token_is_reported(self, enabled, monkeypatch):
        fake = FakeStreamlit(token="")
        monkeypatch.setattr(ui_auth, "st", fake)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "no API access token" in fake.text()

    def test_authorised_user_gets_a_session_with_bearer_headers(self, enabled, fake_st, monkeypatch):
        calls = []
        stub_me(monkeypatch, calls=calls)
        session = require_login(required="user", api_base=API_BASE)
        assert session.enabled and session.name == "Alice Example" and session.subject == "user-1"
        assert session.is_admin is False
        assert session.headers() == {"Authorization": "Bearer access-token-abc"}
        assert calls == [(API_BASE, "access-token-abc")]
        assert "Alice Example" in fake_st.text()  # identity shown in the sidebar

    def test_identity_is_cached_per_token(self, enabled, fake_st, monkeypatch):
        calls = []
        stub_me(monkeypatch, calls=calls)
        require_login(required="user", api_base=API_BASE)
        require_login(required="user", api_base=API_BASE)
        assert len(calls) == 1
        assert fake_st.kinds().count("spinner") == 1  # the cached run does not wait for the API again

        fake_st.user.tokens["access"] = "a-brand-new-token"  # e.g. after signing in again
        require_login(required="user", api_base=API_BASE)
        assert len(calls) == 2

    def test_explains_the_wait_while_a_sleeping_api_answers(self, enabled, fake_st, monkeypatch):
        spinning_during_call = []

        def fetch(api_base, token):
            spinning_during_call.append(fake_st.spinning)
            return 200, me_body()

        monkeypatch.setattr(ui_auth, "_fetch_me", fetch)
        require_login(required="user", api_base=API_BASE)
        assert spinning_during_call == [True]
        assert fake_st.spinning is False
        assert "about a minute" in fake_st.text()

    def test_cached_identity_past_its_expiry_prompts_sign_in_again(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, body=me_body(expires_at=int(time.time()) + 600))
        require_login(required="user", api_base=API_BASE)

        later = time.time() + 3600
        monkeypatch.setattr(ui_auth.time, "time", lambda: later)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "expired" in fake_st.text()
        assert ui_auth._ME_KEY not in fake_st.session_state

    def test_rejected_token_shows_session_expired(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, status=401, body={"detail": "x"})
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "Your session has expired" in fake_st.text()
        assert ui_auth._ME_KEY not in fake_st.session_state

    def test_user_without_a_role_has_no_access(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, body=me_body(roles=[], is_user=False))
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "does not have access" in fake_st.text()
        assert "Alice Example" in fake_st.text()

    def test_admin_screen_requires_the_admin_role(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, body=me_body(is_admin=False))
        with pytest.raises(Stopped):
            require_login(required="admin", api_base=API_BASE)
        assert "admin role is required" in fake_st.text()

    def test_admin_is_allowed_on_both_screens(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, body=me_body(is_admin=True, roles=["PIIShield.Admin"]))
        assert require_login(required="admin", api_base=API_BASE).is_admin is True
        assert require_login(required="user", api_base=API_BASE).is_admin is True

    def test_unreachable_api(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, status=None, body=None)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert API_BASE in fake_st.text()

    def test_unexpected_status(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, status=503, body=None)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert "HTTP 503" in fake_st.text()

    def test_failures_are_not_cached(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch, status=503, body=None)
        with pytest.raises(Stopped):
            require_login(required="user", api_base=API_BASE)
        assert ui_auth._ME_KEY not in fake_st.session_state

    def test_the_token_is_never_rendered(self, enabled, fake_st, monkeypatch):
        stub_me(monkeypatch)
        require_login(required="user", api_base=API_BASE)
        assert "access-token-abc" not in repr(fake_st.events)


class TestFetchMe:
    def test_success(self, monkeypatch):
        class Response:
            status_code = 200

            def json(self):
                return {"is_user": True}

        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured.update(url=url, headers=headers, timeout=timeout)
            return Response()

        monkeypatch.setattr(requests, "get", fake_get)
        assert ui_auth._fetch_me(API_BASE, "tok") == (200, {"is_user": True})
        assert captured["url"] == f"{API_BASE}/me"
        assert captured["headers"] == {"Authorization": "Bearer tok"}

    def test_waits_for_a_scaled_to_zero_api_but_fails_fast_when_nothing_listens(self, monkeypatch):
        class Response:
            status_code = 200

            def json(self):
                return {"is_user": True}

        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["timeout"] = timeout
            return Response()

        monkeypatch.setattr(requests, "get", fake_get)
        ui_auth._fetch_me(API_BASE, "tok")
        assert isinstance(captured["timeout"], tuple), "needs separate connect and read timeouts"
        connect, read = captured["timeout"]
        assert connect <= 10  # a dead API is reported quickly
        assert read >= 60  # a cold start (loading the NER model) takes about a minute

    def test_connection_errors_and_bad_json(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(requests, "get", boom)
        assert ui_auth._fetch_me(API_BASE, "tok") == (None, None)

        class Html:
            status_code = 502

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(requests, "get", lambda *a, **k: Html())
        assert ui_auth._fetch_me(API_BASE, "tok") == (502, None)


class TestApiClient:
    @pytest.fixture()
    def sent(self, monkeypatch):
        record = {}

        def fake_request(method, url, **kwargs):
            record.update(method=method, url=url, kwargs=kwargs)
            return SimpleNamespace(status_code=record.get("status", 200))

        monkeypatch.setattr(requests, "request", fake_request)
        return record

    def test_adds_the_bearer_header_and_keeps_caller_headers(self, sent):
        client = ApiClient(UiSession(enabled=True, token="tok"))
        client.post("http://x/anonymize_unique", json={"a": 1}, headers={"X-App-Id": "app1"}, timeout=30)
        assert sent["method"] == "POST"
        assert sent["kwargs"]["headers"] == {"Authorization": "Bearer tok", "X-App-Id": "app1"}
        assert sent["kwargs"]["json"] == {"a": 1} and sent["kwargs"]["timeout"] == 30

    def test_sends_no_authorization_when_auth_is_disabled(self, sent):
        ApiClient(UiSession(enabled=False)).get("http://x/apps")
        assert sent["kwargs"]["headers"] == {}

    @pytest.mark.parametrize("verb", ["get", "post", "put", "delete"])
    def test_verbs(self, sent, verb):
        getattr(ApiClient(UiSession(enabled=True, token="t")), verb)("http://x/y")
        assert sent["method"] == verb.upper()

    def test_401_triggers_one_rerun_per_token_then_returns_the_response(self, sent, monkeypatch):
        fake = FakeStreamlit()
        fake.session_state[ui_auth._ME_KEY] = {"fingerprint": "x", "me": {}}
        monkeypatch.setattr(ui_auth, "st", fake)
        sent["status"] = 401
        client = ApiClient(UiSession(enabled=True, token="tok"))

        with pytest.raises(Rerun):
            client.get("http://x/apps")
        assert ui_auth._ME_KEY not in fake.session_state  # forces the gate to re-check identity

        resp = client.get("http://x/apps")  # same token again: no rerun loop
        assert resp.status_code == 401

        with pytest.raises(Rerun):  # a different token gets its own single rerun
            ApiClient(UiSession(enabled=True, token="other")).get("http://x/apps")

    def test_other_statuses_pass_through(self, sent, monkeypatch):
        monkeypatch.setattr(ui_auth, "st", FakeStreamlit())
        for status in (200, 403, 404, 500):
            sent["status"] = status
            assert ApiClient(UiSession(enabled=True, token="t")).get("http://x/y").status_code == status

    def test_a_401_without_auth_enabled_is_not_special(self, sent, monkeypatch):
        monkeypatch.setattr(ui_auth, "st", SimpleNamespace())  # must not be touched
        sent["status"] = 401
        assert ApiClient(UiSession(enabled=False)).get("http://x/y").status_code == 401

    def test_connection_errors_propagate_unchanged(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(requests, "request", boom)
        with pytest.raises(requests.ConnectionError):
            ApiClient(UiSession(enabled=True, token="t")).get("http://x/y")
