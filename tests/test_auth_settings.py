"""Tests for app.auth_settings: strict toggle parsing, validation and claim helpers."""

import pytest

from app.auth_settings import (
    AuthConfigError,
    AuthSettings,
    auth_enabled,
    extract_roles,
    load_auth_settings,
    split_list,
)

API_ENV = {
    "AUTH_ENABLED": "true",
    "OIDC_ISSUER_URL": "https://idp.example.com/realms/pii",
    "OIDC_AUDIENCE": "pii-shield-api",
}

UI_ENV = {
    "AUTH_ENABLED": "true",
    "OIDC_ISSUER_URL": "https://idp.example.com/realms/pii",
    "OIDC_CLIENT_ID": "pii-ui",
    "OIDC_CLIENT_SECRET": "super-secret-client-value",
    "AUTH_COOKIE_SECRET": "c" * 40,
}


class TestToggle:
    def test_disabled_by_default(self):
        assert load_auth_settings("api", env={}).enabled is False
        assert auth_enabled({}) is False

    @pytest.mark.parametrize("value", ["", "   ", "false", "FALSE", "0", "no", "off"])
    def test_falsey_values_disable(self, value):
        assert auth_enabled({"AUTH_ENABLED": value}) is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
    def test_truthy_values_enable(self, value):
        assert auth_enabled({"AUTH_ENABLED": value}) is True

    @pytest.mark.parametrize("value", ["ture", "2", "enabled", "disable"])
    def test_invalid_value_is_an_error_not_off(self, value):
        with pytest.raises(AuthConfigError, match="AUTH_ENABLED"):
            auth_enabled({"AUTH_ENABLED": value})
        with pytest.raises(AuthConfigError, match="AUTH_ENABLED"):
            load_auth_settings("api", env={"AUTH_ENABLED": value})

    def test_disabled_ignores_broken_oidc_variables(self):
        env = {
            "AUTH_ENABLED": "false",
            "OIDC_CLOCK_SKEW_SECONDS": "abc",
            "OIDC_ALGORITHMS": "HS256",
            "OIDC_ISSUER_URL": "not-a-url",
        }
        assert load_auth_settings("all", env=env).enabled is False

    def test_unknown_component_rejected(self):
        with pytest.raises(ValueError, match="component"):
            load_auth_settings("worker", env={})


class TestApiSettings:
    def test_missing_required_values_are_all_reported(self):
        with pytest.raises(AuthConfigError) as exc:
            load_auth_settings("api", env={"AUTH_ENABLED": "true"})
        message = str(exc.value)
        assert "OIDC_ISSUER_URL" in message
        assert "OIDC_AUDIENCE" in message

    def test_minimal_valid_config_and_defaults(self):
        s = load_auth_settings("api", env=API_ENV)
        assert s.enabled is True
        assert s.audiences == ("pii-shield-api",)
        assert s.roles_claims == ("roles",)
        assert s.user_roles == frozenset({"PIIShield.User"})
        assert s.admin_roles == frozenset({"PIIShield.Admin"})
        assert s.algorithms == ("RS256",)
        assert s.clock_skew_seconds == 60
        assert s.jwks_cache_seconds == 3600
        assert s.issuer == "" and s.jwks_url == ""

    def test_lists_are_parsed(self):
        env = {
            **API_ENV,
            "OIDC_AUDIENCE": "api://abc, abc ,",
            "OIDC_ROLES_CLAIM": "roles,realm_access.roles",
            "AUTH_USER_ROLES": "user, service",
            "AUTH_ADMIN_ROLES": "admin",
            "OIDC_ALGORITHMS": "RS256,ES256",
        }
        s = load_auth_settings("api", env=env)
        assert s.audiences == ("api://abc", "abc")
        assert s.roles_claims == ("roles", "realm_access.roles")
        assert s.user_roles == frozenset({"user", "service"})
        assert s.admin_roles == frozenset({"admin"})
        assert s.algorithms == ("RS256", "ES256")

    def test_empty_values_fall_back_to_defaults(self):
        # docker-compose passes unset variables through as empty strings.
        env = {**API_ENV, "OIDC_ROLES_CLAIM": "", "AUTH_USER_ROLES": "", "OIDC_ALGORITHMS": ""}
        s = load_auth_settings("api", env=env)
        assert s.roles_claims == ("roles",)
        assert s.user_roles == frozenset({"PIIShield.User"})
        assert s.algorithms == ("RS256",)

    def test_http_issuer_requires_explicit_opt_in(self):
        env = {**API_ENV, "OIDC_ISSUER_URL": "http://keycloak:8080/realms/pii"}
        with pytest.raises(AuthConfigError, match="https"):
            load_auth_settings("api", env=env)
        s = load_auth_settings("api", env={**env, "OIDC_ALLOW_INSECURE_HTTP": "true"})
        assert s.allow_insecure_http is True

    def test_non_url_issuer_rejected(self):
        with pytest.raises(AuthConfigError, match="OIDC_ISSUER_URL"):
            load_auth_settings("api", env={**API_ENV, "OIDC_ISSUER_URL": "idp.example.com"})

    @pytest.mark.parametrize("algs", ["none", "HS256", "RS256,HS512", "rs256"])
    def test_unsafe_or_unknown_algorithms_rejected(self, algs):
        with pytest.raises(AuthConfigError, match="OIDC_ALGORITHMS"):
            load_auth_settings("api", env={**API_ENV, "OIDC_ALGORITHMS": algs})

    @pytest.mark.parametrize(
        "name,value",
        [
            ("OIDC_CLOCK_SKEW_SECONDS", "-1"),
            ("OIDC_CLOCK_SKEW_SECONDS", "301"),
            ("OIDC_CLOCK_SKEW_SECONDS", "x"),
            ("OIDC_JWKS_CACHE_SECONDS", "10"),
            ("OIDC_JWKS_CACHE_SECONDS", "100000"),
        ],
    )
    def test_numeric_bounds(self, name, value):
        with pytest.raises(AuthConfigError, match=name):
            load_auth_settings("api", env={**API_ENV, name: value})

    def test_api_component_does_not_require_ui_settings(self):
        assert load_auth_settings("api", env=API_ENV).client_id == ""

    def test_override_urls_are_validated(self):
        with pytest.raises(AuthConfigError, match="OIDC_JWKS_URL"):
            load_auth_settings("api", env={**API_ENV, "OIDC_JWKS_URL": "http://keycloak/certs"})


class TestUiSettings:
    def test_valid_ui_config_and_defaults(self):
        s = load_auth_settings("ui", env=UI_ENV)
        assert s.client_id == "pii-ui"
        assert s.redirect_uri == "http://localhost:{port}/oauth2callback"
        assert s.ui_scopes == ("openid", "profile", "email")
        assert s.scope_string == "openid profile email"
        assert s.metadata_url == "https://idp.example.com/realms/pii/.well-known/openid-configuration"

    def test_ui_component_does_not_require_api_settings(self):
        assert load_auth_settings("ui", env=UI_ENV).audiences == ()

    def test_missing_ui_values_are_all_reported(self):
        env = {"AUTH_ENABLED": "true", "OIDC_ISSUER_URL": "https://idp.example.com"}
        with pytest.raises(AuthConfigError) as exc:
            load_auth_settings("ui", env=env)
        message = str(exc.value)
        for name in ("OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "AUTH_COOKIE_SECRET"):
            assert name in message

    def test_short_cookie_secret_rejected(self):
        with pytest.raises(AuthConfigError, match="at least 32"):
            load_auth_settings("ui", env={**UI_ENV, "AUTH_COOKIE_SECRET": "short"})

    def test_api_scope_is_appended_without_duplicates(self):
        env = {**UI_ENV, "OIDC_API_SCOPE": "api://abc/access_as_user"}
        assert load_auth_settings("ui", env=env).scope_string == (
            "openid profile email api://abc/access_as_user"
        )
        env = {**UI_ENV, "OIDC_UI_SCOPES": "openid,profile", "OIDC_API_SCOPE": "profile"}
        assert load_auth_settings("ui", env=env).scope_string == "openid profile"

    def test_scopes_must_include_openid(self):
        with pytest.raises(AuthConfigError, match="openid"):
            load_auth_settings("ui", env={**UI_ENV, "OIDC_UI_SCOPES": "profile email"})

    @pytest.mark.parametrize(
        "uri",
        [
            "https://playground.example.com/oauth2callback",
            "http://localhost:7860/oauth2callback",
            "http://127.0.0.1:7861/oauth2callback",
            "http://localhost:{port}/oauth2callback",
        ],
    )
    def test_valid_redirect_uris(self, uri):
        assert load_auth_settings("ui", env={**UI_ENV, "UI_REDIRECT_URI": uri}).redirect_uri == uri

    @pytest.mark.parametrize(
        "uri,fragment",
        [
            ("http://playground.example.com/oauth2callback", "https"),
            ("https://playground.example.com/callback", "oauth2callback"),
            ("playground.example.com/oauth2callback", "absolute"),
        ],
    )
    def test_invalid_redirect_uris(self, uri, fragment):
        with pytest.raises(AuthConfigError, match=fragment):
            load_auth_settings("ui", env={**UI_ENV, "UI_REDIRECT_URI": uri})

    def test_metadata_url_override(self):
        env = {**UI_ENV, "OIDC_SERVER_METADATA_URL": "https://other.example.com/.well-known/openid-configuration"}
        assert load_auth_settings("ui", env=env).metadata_url == env["OIDC_SERVER_METADATA_URL"]

    def test_all_component_validates_both_sides(self):
        with pytest.raises(AuthConfigError) as exc:
            load_auth_settings("all", env={"AUTH_ENABLED": "true"})
        message = str(exc.value)
        assert "OIDC_AUDIENCE" in message and "OIDC_CLIENT_ID" in message


class TestSecretHygiene:
    def test_secrets_never_appear_in_errors_repr_or_summary(self):
        env = {**UI_ENV, "OIDC_UI_SCOPES": "profile"}  # force a validation error
        with pytest.raises(AuthConfigError) as exc:
            load_auth_settings("ui", env=env)
        assert "super-secret-client-value" not in str(exc.value)
        assert env["AUTH_COOKIE_SECRET"] not in str(exc.value)

        s = load_auth_settings("ui", env=UI_ENV)
        assert "super-secret-client-value" not in repr(s)
        assert UI_ENV["AUTH_COOKIE_SECRET"] not in repr(s)
        assert "super-secret-client-value" not in repr(s.safe_summary())

    def test_settings_are_immutable(self):
        s = AuthSettings(enabled=False)
        with pytest.raises(Exception):
            s.enabled = True  # type: ignore[misc]


class TestExtractRoles:
    def test_list_claim(self):
        assert extract_roles({"roles": ["a", "b"]}, ("roles",)) == {"a", "b"}

    def test_space_delimited_string_claim(self):
        assert extract_roles({"scp": "read write"}, ("scp",)) == {"read", "write"}

    def test_nested_dot_path(self):
        claims = {"realm_access": {"roles": ["PIIShield.Admin", "offline_access"]}}
        assert extract_roles(claims, ("realm_access.roles",)) == {"PIIShield.Admin", "offline_access"}

    def test_namespaced_claim_with_dots_is_matched_exactly(self):
        claims = {"https://example.com/roles": ["admin"]}
        assert extract_roles(claims, ("https://example.com/roles",)) == {"admin"}

    def test_union_of_multiple_paths(self):
        claims = {"roles": ["a"], "groups": ["b"]}
        assert extract_roles(claims, ("roles", "groups")) == {"a", "b"}

    def test_missing_or_malformed_claims_contribute_nothing(self):
        assert extract_roles({}, ("roles",)) == frozenset()
        assert extract_roles({"roles": 5}, ("roles",)) == frozenset()
        assert extract_roles({"roles": {"a": 1}}, ("roles",)) == frozenset()
        assert extract_roles({"realm_access": "x"}, ("realm_access.roles",)) == frozenset()

    def test_blank_items_are_dropped(self):
        assert extract_roles({"roles": ["a", " ", ""]}, ("roles",)) == {"a"}


def test_split_list():
    assert split_list("a, b,, c ") == ["a", "b", "c"]
    assert split_list("") == []
