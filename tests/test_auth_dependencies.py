"""Dependency-level tests for app.auth on a tiny FastAPI app (no NLP engine needed).

These pin the HTTP contract of the dependencies themselves -- 401/403/503, header
handling, ordering relative to body validation, the ``@public`` marker and the
OpenAPI security declaration. ``test_auth_endpoints.py`` then checks the real routes.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.auth import (
    JwksCache,
    Principal,
    TokenValidator,
    authenticate,
    configure_auth,
    public,
    require_admin,
    require_authenticated,
    require_user,
    reset_auth,
)
from app.auth_settings import AuthSettings
from tests._auth_helpers import FakeIdp


class Payload(BaseModel):
    value: int


def build_app() -> FastAPI:
    mini = FastAPI(dependencies=[Depends(authenticate)])

    @mini.get("/public")
    @public
    async def public_route():
        return {"ok": True}

    @mini.get("/open")
    async def open_route(principal: Principal = Depends(authenticate)):
        return {"kind": principal.kind}

    @mini.get("/user", dependencies=[Depends(require_user)])
    async def user_route():
        return {"ok": True}

    @mini.get("/admin")
    async def admin_route(principal: Principal = Depends(require_admin)):
        return {"actor": principal.actor}

    @mini.get("/me")
    async def me_route(principal: Principal = Depends(require_authenticated)):
        return {"subject": principal.subject}

    @mini.post("/body", dependencies=[Depends(require_user)])
    async def body_route(payload: Payload):
        return {"value": payload.value}

    return mini


@pytest.fixture(autouse=True)
def _clean_auth_state():
    reset_auth()
    yield
    reset_auth()


@pytest.fixture()
def idp():
    return FakeIdp()


@pytest.fixture()
def client(idp):
    settings = idp.settings()
    configure_auth(settings, TokenValidator(settings, JwksCache(settings, http_get=idp.http_get)))
    return TestClient(build_app(), raise_server_exceptions=False)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestEnabled:
    def test_public_route_needs_no_token(self, client):
        assert client.get("/public").json() == {"ok": True}

    def test_missing_token_is_401_with_bearer_challenge(self, client):
        resp = client.get("/user")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
        assert resp.json() == {"detail": "Authentication required."}

    def test_unmarked_route_without_a_role_requirement_still_needs_a_token(self, client):
        assert client.get("/open").status_code == 401

    def test_public_marker_does_not_leak_to_other_routes(self, client, idp):
        assert client.get("/open", headers=bearer(idp.token())).json() == {"kind": "user"}

    def test_invalid_token_is_401_invalid_token(self, client, idp):
        resp = client.get("/user", headers=bearer(idp.token(aud="other")))
        assert resp.status_code == 401
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]
        assert resp.json() == {"detail": "Invalid or expired access token."}

    def test_authentication_precedes_body_validation(self, client, idp):
        assert client.post("/body", json={"value": "not-an-int"}).status_code == 401
        resp = client.post("/body", json={"value": "not-an-int"}, headers=bearer(idp.token()))
        assert resp.status_code == 422  # only authenticated callers see validation details
        assert client.post("/body", json={"value": 3}, headers=bearer(idp.token())).json() == {"value": 3}

    def test_user_role_gates(self, client, idp):
        assert client.get("/user", headers=bearer(idp.token(roles=["PIIShield.User"]))).status_code == 200
        assert client.get("/user", headers=bearer(idp.token(roles=["nope"]))).status_code == 403

    def test_admin_role_gates_and_actor_is_available(self, client, idp):
        resp = client.get("/admin", headers=bearer(idp.token(roles=["PIIShield.User"])))
        assert resp.status_code == 403
        resp = client.get("/admin", headers=bearer(idp.token(roles=["PIIShield.Admin"])))
        assert resp.json() == {"actor": "alice@example.com"}

    def test_require_authenticated_accepts_a_token_without_roles(self, client, idp):
        assert client.get("/me", headers=bearer(idp.token(roles=None))).json() == {"subject": "user-123"}

    def test_outage_is_503_with_retry_after(self, client, idp):
        idp.fail = True
        resp = client.get("/user", headers=bearer(idp.token()))
        assert resp.status_code == 503
        assert resp.headers["retry-after"] == "5"

    def test_openapi_documents_bearer_auth_only_on_protected_routes(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert paths["/user"]["get"]["security"] == [{"HTTPBearer": []}]
        assert paths["/admin"]["get"]["security"] == [{"HTTPBearer": []}]
        assert "security" not in paths["/public"]["get"]


class TestDisabled:
    @pytest.fixture()
    def open_client(self):
        configure_auth(AuthSettings(enabled=False))
        return TestClient(build_app(), raise_server_exceptions=False)

    def test_everything_is_open_and_principal_is_admin(self, open_client):
        assert open_client.get("/user").status_code == 200
        assert open_client.get("/admin").json() == {"actor": ""}
        assert open_client.get("/open").json() == {"kind": "anonymous"}

    def test_tokens_are_not_inspected(self, open_client):
        assert open_client.get("/user", headers=bearer("garbage")).status_code == 200
