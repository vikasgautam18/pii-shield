"""End-to-end authentication tests against the real FastAPI app (app.main).

With ``AUTH_ENABLED`` unset the whole existing suite is the regression test for
"auth off changes nothing"; this module covers the enabled behaviour: default-deny,
the role matrix for every route, the 401/403/503 contract, ``/me``, ``/health``
and the audit actor.
"""

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import audit_store
from app.auth import JwksCache, TokenValidator, configure_auth, get_auth_state, reset_auth
from app.auth_settings import AuthConfigError, load_auth_settings
from app.main import app
from tests._auth_helpers import FakeIdp, SigningKey

MISSING = "missing-app"

# (method, route template, JSON body). Authorized calls to these resolve to
# 200/201/204/404 (never 401/403); only the authorization outcome is asserted.
USER_ROUTES = [
    ("GET", "/supported-entities", None),
    ("POST", "/anonymize_unique", {"text": "Contact john@example.com", "language": "en"}),
    ("POST", "/deanonymize", {"id": "missing-session", "text": "x"}),
    ("GET", "/apps", None),
    ("GET", "/apps/{app_id}", None),
]
ADMIN_ROUTES = [
    ("POST", "/apps", {"app_name": "MatrixApp"}),
    ("PUT", "/apps/{app_id}/config", {"entity_type": "EMAIL_ADDRESS", "strategy": "hash"}),
    ("DELETE", "/apps/{app_id}", None),
    ("GET", "/apps/{app_id}/allow-list", None),
    ("PUT", "/apps/{app_id}/allow-list", {"allow_list": []}),
    ("GET", "/apps/{app_id}/entity-type-allow-list", None),
    ("PUT", "/apps/{app_id}/entity-type-allow-list", {"entity_type_allow_list": []}),
    ("GET", "/apps/{app_id}/entity-keyword-allow-list", None),
    ("PUT", "/apps/{app_id}/entity-keyword-allow-list", {"entity_keyword_allow_list": {}}),
    ("GET", "/audit-log", None),
]
AUTHENTICATED_ONLY_ROUTES = [("GET", "/me", None)]
PUBLIC_ROUTES = {("GET", "/health")}


@pytest.fixture(autouse=True)
def _temp_audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_store, "AUDIT_DB_PATH", str(tmp_path / "audit.db"))


@pytest.fixture()
def idp():
    return FakeIdp()


@pytest.fixture()
def auth_on(idp):
    settings = idp.settings()
    configure_auth(settings, TokenValidator(settings, JwksCache(settings, http_get=idp.http_get)))
    return idp


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def call(client, method, template, body=None, headers=None):
    return client.request(method, template.replace("{app_id}", MISSING), json=body, headers=headers)


def user_token(idp, **claims):
    return idp.token(roles=["PIIShield.User"], **claims)


def admin_token(idp, **claims):
    return idp.token(roles=["PIIShield.Admin"], **claims)


class TestRouteInventory:
    def test_every_route_is_classified(self):
        declared = {
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods - {"HEAD", "OPTIONS"}
        }
        classified = {
            (method, path)
            for method, path, _ in USER_ROUTES + ADMIN_ROUTES + AUTHENTICATED_ONLY_ROUTES
        } | PUBLIC_ROUTES
        assert declared == classified, (
            "Routes changed: classify every new route (public / authenticated / user / admin) "
            f"in tests/test_auth_endpoints.py. Unclassified: {sorted(declared - classified)}; "
            f"stale: {sorted(classified - declared)}"
        )

    def test_only_health_is_marked_public(self):
        public = {
            (method, route.path)
            for route in app.routes
            if isinstance(route, APIRoute) and getattr(route.endpoint, "__auth_public__", False)
            for method in route.methods - {"HEAD", "OPTIONS"}
        }
        assert public == PUBLIC_ROUTES


class TestAuthDisabled:
    def test_open_access_without_a_token(self, client):
        assert client.get("/apps").status_code == 200
        assert client.get("/audit-log").status_code == 200

    def test_a_garbage_token_is_ignored_not_validated(self, client):
        assert client.get("/apps", headers=bearer("garbage")).status_code == 200

    def test_health_and_me(self, client):
        assert client.get("/health").json() == {"status": "ok"}
        me = client.get("/me").json()
        assert me["auth_enabled"] is False
        assert me["is_user"] is True and me["is_admin"] is True
        assert me["kind"] == "anonymous" and me["subject"] is None

    def test_actor_is_empty_in_the_audit_log(self, client):
        client.post("/apps", json={"app_name": "OpenApp"})
        assert [e["actor"] for e in client.get("/audit-log").json()] == [""]


class TestDefaultDeny:
    @pytest.mark.parametrize("method,template,body", USER_ROUTES + ADMIN_ROUTES + AUTHENTICATED_ONLY_ROUTES)
    def test_missing_token_is_401_on_every_protected_route(self, auth_on, client, method, template, body):
        resp = call(client, method, template, body)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_health_stays_public(self, auth_on, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_health_works_even_when_the_idp_is_down(self, auth_on, client):
        auth_on.fail = True
        assert client.get("/health").status_code == 200

    def test_openapi_and_docs_remain_reachable_and_declare_bearer_auth(self, auth_on, client):
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "HTTPBearer" in schema["components"]["securitySchemes"]

    def test_validation_errors_are_not_reachable_without_auth(self, auth_on, client):
        # An unauthenticated caller must get 401, not a 422 that leaks the schema.
        assert client.post("/anonymize_unique", json={"nope": 1}).status_code == 401


class TestTokenRejection:
    @pytest.mark.parametrize(
        "make",
        [
            lambda idp: idp.token(exp=1),
            lambda idp: idp.token(aud="other-api"),
            lambda idp: idp.token(iss="https://evil.test/realms/pii"),
            lambda idp: idp.token(key=SigningKey.shared("impostor"), headers={"kid": "key-1"}),
            lambda idp: "not-a-jwt",
        ],
        ids=["expired", "wrong-audience", "wrong-issuer", "bad-signature", "malformed"],
    )
    def test_invalid_tokens_are_401_with_a_generic_body(self, auth_on, client, make):
        resp = client.get("/apps", headers=bearer(make(auth_on)))
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid or expired access token."}
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]

    @pytest.mark.parametrize("header", ["Basic abc123", "Bearer", "Bearer   ", "abc"])
    def test_non_bearer_authorization_is_treated_as_missing(self, auth_on, client, header):
        resp = client.get("/apps", headers={"Authorization": header})
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    def test_bearer_scheme_is_case_insensitive(self, auth_on, client):
        resp = client.get("/apps", headers={"Authorization": f"bearer {user_token(auth_on)}"})
        assert resp.status_code == 200

    def test_idp_outage_with_a_cold_cache_fails_closed_with_503(self, auth_on, client):
        auth_on.fail = True
        resp = client.get("/apps", headers=bearer(user_token(auth_on)))
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"

    def test_rejections_never_log_the_token(self, auth_on, client, caplog):
        token = auth_on.token(aud="other-api")
        with caplog.at_level(logging.DEBUG):
            client.get("/apps", headers=bearer(token))
        assert token not in caplog.text
        assert token.split(".")[2] not in caplog.text


class TestRoleMatrix:
    @pytest.mark.parametrize("method,template,body", USER_ROUTES + ADMIN_ROUTES + AUTHENTICATED_ONLY_ROUTES)
    def test_token_without_a_known_role_is_forbidden_everywhere_but_me(
        self, auth_on, client, method, template, body
    ):
        headers = bearer(auth_on.token(roles=["unrelated"]))
        resp = call(client, method, template, body, headers)
        expected = 200 if template == "/me" else 403
        assert resp.status_code == expected

    @pytest.mark.parametrize("method,template,body", USER_ROUTES)
    def test_user_can_reach_data_plane_routes(self, auth_on, client, method, template, body):
        resp = call(client, method, template, body, bearer(user_token(auth_on)))
        assert resp.status_code not in (401, 403)

    @pytest.mark.parametrize("method,template,body", ADMIN_ROUTES)
    def test_user_cannot_reach_admin_routes(self, auth_on, client, method, template, body):
        resp = call(client, method, template, body, bearer(user_token(auth_on)))
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Insufficient permissions: the admin role is required."}

    @pytest.mark.parametrize("method,template,body", USER_ROUTES + ADMIN_ROUTES + AUTHENTICATED_ONLY_ROUTES)
    def test_admin_can_reach_everything(self, auth_on, client, method, template, body):
        resp = call(client, method, template, body, bearer(admin_token(auth_on)))
        assert resp.status_code not in (401, 403)

    def test_service_principal_with_user_role_can_anonymize(self, auth_on, client):
        token = auth_on.token(
            preferred_username=None, name=None, azp="svc-client", idtyp="app", roles=["PIIShield.User"]
        )
        resp = client.post(
            "/anonymize_unique",
            json={"text": "Contact john@example.com", "language": "en"},
            headers=bearer(token),
        )
        assert resp.status_code == 200
        assert "john@example.com" not in resp.json()["anonymized_text"]

    def test_custom_role_names_and_claim_are_honoured(self, idp, client):
        settings = idp.settings(
            AUTH_USER_ROLES="reader", AUTH_ADMIN_ROLES="owner", OIDC_ROLES_CLAIM="realm_access.roles"
        )
        configure_auth(settings, TokenValidator(settings, JwksCache(settings, http_get=idp.http_get)))
        token = idp.token(roles=None, realm_access={"roles": ["reader"]})
        assert client.get("/apps", headers=bearer(token)).status_code == 200
        assert client.get("/audit-log", headers=bearer(token)).status_code == 403
        owner = idp.token(roles=None, realm_access={"roles": ["owner"]})
        assert client.get("/audit-log", headers=bearer(owner)).status_code == 200


class TestMe:
    def test_user(self, auth_on, client):
        me = client.get("/me", headers=bearer(user_token(auth_on))).json()
        assert me["auth_enabled"] is True
        assert me["subject"] == "user-123"
        assert me["name"] == "Alice Example"
        assert me["kind"] == "user"
        assert me["roles"] == ["PIIShield.User"]
        assert me["is_user"] is True and me["is_admin"] is False
        assert isinstance(me["expires_at"], int)

    def test_admin(self, auth_on, client):
        me = client.get("/me", headers=bearer(admin_token(auth_on))).json()
        assert me["is_admin"] is True and me["is_user"] is True

    def test_service(self, auth_on, client):
        token = auth_on.token(preferred_username=None, name=None, azp="svc", idtyp="app")
        me = client.get("/me", headers=bearer(token)).json()
        assert me["kind"] == "service" and me["name"] is None


class TestAuditActor:
    def test_admin_actions_record_the_user(self, auth_on, client):
        headers = bearer(admin_token(auth_on))
        created = client.post("/apps", json={"app_name": "AuditedApp"}, headers=headers).json()
        app_id = created["app_id"]
        client.put(f"/apps/{app_id}/config", json={"entity_type": "EMAIL_ADDRESS", "strategy": "hash"}, headers=headers)
        client.put(f"/apps/{app_id}/allow-list", json={"allow_list": ["Contoso"]}, headers=headers)
        client.put(f"/apps/{app_id}/entity-type-allow-list", json={"entity_type_allow_list": ["URL"]}, headers=headers)
        client.put(
            f"/apps/{app_id}/entity-keyword-allow-list",
            json={"entity_keyword_allow_list": {"LOCATION": ["India"]}},
            headers=headers,
        )
        client.delete(f"/apps/{app_id}", headers=headers)

        entries = client.get("/audit-log", headers=headers).json()
        assert len(entries) == 6
        assert {e["actor"] for e in entries} == {"alice@example.com"}

    def test_service_actions_record_the_client(self, auth_on, client):
        token = auth_on.token(
            preferred_username=None, name=None, azp="svc-admin", idtyp="app", roles=["PIIShield.Admin"]
        )
        client.post("/apps", json={"app_name": "BySvc"}, headers=bearer(token))
        entries = client.get("/audit-log", headers=bearer(token)).json()
        assert entries[0]["actor"] == "service:svc-admin"


class TestStartupValidation:
    def test_invalid_configuration_stops_startup(self, monkeypatch):
        reset_auth()
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        with pytest.raises(AuthConfigError):
            get_auth_state()

    def test_environment_driven_state_when_disabled(self, monkeypatch):
        reset_auth()
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        assert get_auth_state().settings.enabled is False
        assert load_auth_settings("api", env={}).enabled is False
