"""Tests for app.ui_launcher: secrets rendering, safe file handling and exec behaviour."""

import os
import stat
import tomllib
from pathlib import Path

import pytest

from app import ui_launcher
from app.auth_settings import load_auth_settings
from app.ui_launcher import (
    GENERATED_MARKER,
    SecretsFileConflict,
    build_command,
    main,
    remove_generated_secrets,
    render_secrets,
    toml_string,
    write_secrets,
)

UI_ENV = {
    "AUTH_ENABLED": "true",
    "OIDC_ISSUER_URL": "https://idp.example.com/realms/pii",
    "OIDC_CLIENT_ID": "pii-ui",
    "OIDC_CLIENT_SECRET": "client-secret-value",
    "AUTH_COOKIE_SECRET": "k" * 48,
}


@pytest.fixture()
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("AUTH_", "OIDC_", "UI_REDIRECT")):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture()
def enabled_env(clean_env):
    for name, value in UI_ENV.items():
        clean_env.setenv(name, value)
    return clean_env


class FakeExec:
    def __init__(self):
        self.calls = []

    def __call__(self, file, argv):
        self.calls.append((file, argv))


def parse(text: str) -> dict:
    return tomllib.loads(text)


class TestTomlString:
    @pytest.mark.parametrize(
        "value",
        [
            "plain",
            'with "quotes"',
            "back\\slash",
            "line1\nline2\r\n",
            "tab\there",
            "ünïcödé ✓",
            "ctrl\x01\x1f\x7f",
            "",
            "https://idp/{tenant}/v2.0?a=b&c=d#frag",
        ],
    )
    def test_round_trips_through_a_toml_parser(self, value):
        assert tomllib.loads(f"v = {toml_string(value)}")["v"] == value


class TestRenderSecrets:
    def test_contains_everything_streamlit_needs(self, enabled_env):
        settings = load_auth_settings("ui")
        text = render_secrets(settings)
        assert text.startswith(GENERATED_MARKER)
        auth = parse(text)["auth"]
        assert auth["redirect_uri"] == "http://localhost:{port}/oauth2callback"
        assert auth["cookie_secret"] == UI_ENV["AUTH_COOKIE_SECRET"]
        assert auth["client_id"] == "pii-ui"
        assert auth["client_secret"] == "client-secret-value"
        assert auth["server_metadata_url"] == (
            "https://idp.example.com/realms/pii/.well-known/openid-configuration"
        )
        assert auth["expose_tokens"] == ["access"]
        assert auth["client_kwargs"] == {"scope": "openid profile email", "code_challenge_method": "S256"}

    def test_api_scope_prompt_and_overrides(self, enabled_env):
        enabled_env.setenv("OIDC_API_SCOPE", "api://abc/access_as_user")
        enabled_env.setenv("OIDC_PROMPT", "login")
        enabled_env.setenv("UI_REDIRECT_URI", "https://playground.example.com/oauth2callback")
        enabled_env.setenv("OIDC_SERVER_METADATA_URL", "https://meta.example.com/.well-known/openid-configuration")
        auth = parse(render_secrets(load_auth_settings("ui")))["auth"]
        assert auth["client_kwargs"]["scope"] == "openid profile email api://abc/access_as_user"
        assert auth["client_kwargs"]["prompt"] == "login"
        assert auth["redirect_uri"] == "https://playground.example.com/oauth2callback"
        assert auth["server_metadata_url"] == "https://meta.example.com/.well-known/openid-configuration"

    def test_awkward_secret_characters_survive(self, enabled_env):
        secret = 'p"a\\ss\nw0rd' + "x" * 40
        enabled_env.setenv("OIDC_CLIENT_SECRET", secret)
        assert parse(render_secrets(load_auth_settings("ui")))["auth"]["client_secret"] == secret


class TestWriteSecrets:
    def test_creates_a_private_file(self, tmp_path):
        path = tmp_path / ".streamlit" / "secrets.toml"
        assert write_secrets(GENERATED_MARKER + "\n[auth]\n", path) is True
        assert path.read_text().startswith(GENERATED_MARKER)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_is_idempotent_and_replaces_changed_content(self, tmp_path):
        path = tmp_path / "secrets.toml"
        first = GENERATED_MARKER + "\n[auth]\na = 1\n"
        assert write_secrets(first, path) is True
        assert write_secrets(first, path) is False
        second = GENERATED_MARKER + "\n[auth]\na = 2\n"
        assert write_secrets(second, path) is True
        assert path.read_text() == second
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_leaves_no_temporary_files_behind(self, tmp_path):
        path = tmp_path / "secrets.toml"
        write_secrets(GENERATED_MARKER + "\n", path)
        write_secrets(GENERATED_MARKER + "\nchanged\n", path)
        assert [p.name for p in tmp_path.iterdir()] == ["secrets.toml"]

    def test_refuses_to_overwrite_a_foreign_file(self, tmp_path):
        path = tmp_path / "secrets.toml"
        path.write_text('db_password = "mine"\n')
        with pytest.raises(SecretsFileConflict):
            write_secrets(GENERATED_MARKER + "\n", path)
        assert path.read_text() == 'db_password = "mine"\n'


class TestRemoveGenerated:
    def test_removes_only_files_it_generated(self, tmp_path):
        ours = tmp_path / "ours.toml"
        ours.write_text(GENERATED_MARKER + "\n")
        theirs = tmp_path / "theirs.toml"
        theirs.write_text("x = 1\n")
        assert remove_generated_secrets(ours) is True and not ours.exists()
        assert remove_generated_secrets(theirs) is False and theirs.exists()
        assert remove_generated_secrets(tmp_path / "missing.toml") is False


class TestMain:
    def test_enabled_writes_secrets_then_execs_streamlit(self, enabled_env, tmp_path):
        fake = FakeExec()
        assert main(["playground"], exec_fn=fake, workdir=tmp_path) == 0

        secrets = tmp_path / ".streamlit" / "secrets.toml"
        assert parse(secrets.read_text())["auth"]["client_id"] == "pii-ui"
        (file, argv), = fake.calls
        assert argv[argv.index("run"):] == [
            "run", "app/streamlit_app.py",
            "--server.port", "7860",
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
            "--client.toolbarMode", "minimal",
        ]
        assert file == argv[0]

    def test_admin_defaults_overrides_and_extra_args(self, enabled_env, tmp_path):
        fake = FakeExec()
        main(["admin"], exec_fn=fake, workdir=tmp_path)
        assert "app/streamlit_admin.py" in fake.calls[0][1]
        assert fake.calls[0][1][fake.calls[0][1].index("--server.port") + 1] == "7861"

        fake = FakeExec()
        main(["admin", "--port", "9000", "--address", "127.0.0.1", "--", "--server.fileWatcherType", "none"],
             exec_fn=fake, workdir=tmp_path)
        argv = fake.calls[0][1]
        assert argv[argv.index("--server.port") + 1] == "9000"
        assert argv[argv.index("--server.address") + 1] == "127.0.0.1"
        assert argv[-2:] == ["--server.fileWatcherType", "none"]
        assert "--" not in argv

    def test_disabled_generates_nothing_and_still_starts_streamlit(self, clean_env, tmp_path):
        fake = FakeExec()
        assert main(["playground"], exec_fn=fake, workdir=tmp_path) == 0
        assert not (tmp_path / ".streamlit").exists()
        assert len(fake.calls) == 1

    def test_disabled_removes_a_stale_generated_file_but_keeps_foreign_ones(self, clean_env, tmp_path):
        secrets = tmp_path / ".streamlit" / "secrets.toml"
        secrets.parent.mkdir()
        secrets.write_text(GENERATED_MARKER + "\n[auth]\n")
        main(["playground"], exec_fn=FakeExec(), workdir=tmp_path)
        assert not secrets.exists()

        secrets.write_text('keep = "me"\n')
        main(["playground"], exec_fn=FakeExec(), workdir=tmp_path)
        assert secrets.read_text() == 'keep = "me"\n'

    def test_invalid_configuration_exits_nonzero_without_starting_streamlit(
        self, clean_env, tmp_path, capsys
    ):
        clean_env.setenv("AUTH_ENABLED", "true")
        fake = FakeExec()
        assert main(["playground"], exec_fn=fake, workdir=tmp_path) == 2
        assert fake.calls == []
        assert not (tmp_path / ".streamlit").exists()
        assert "OIDC_ISSUER_URL" in capsys.readouterr().err

    def test_invalid_toggle_value_never_means_off(self, clean_env, tmp_path):
        clean_env.setenv("AUTH_ENABLED", "ture")
        fake = FakeExec()
        assert main(["playground"], exec_fn=fake, workdir=tmp_path) == 2
        assert fake.calls == []

    def test_foreign_secrets_file_blocks_startup(self, enabled_env, tmp_path, capsys):
        secrets = tmp_path / ".streamlit" / "secrets.toml"
        secrets.parent.mkdir()
        secrets.write_text('db_password = "mine"\n')
        fake = FakeExec()
        assert main(["playground"], exec_fn=fake, workdir=tmp_path) == 2
        assert fake.calls == []
        assert secrets.read_text() == 'db_password = "mine"\n'
        assert "not generated by app.ui_launcher" in capsys.readouterr().err

    def test_secrets_are_never_printed(self, enabled_env, tmp_path, capsys):
        main(["playground"], exec_fn=FakeExec(), workdir=tmp_path)
        captured = capsys.readouterr()
        for secret in (UI_ENV["OIDC_CLIENT_SECRET"], UI_ENV["AUTH_COOKIE_SECRET"]):
            assert secret not in captured.out + captured.err


def test_build_command_falls_back_to_python_module_when_streamlit_is_not_on_path(monkeypatch):
    monkeypatch.setattr(ui_launcher.shutil, "which", lambda name: None)
    command = build_command("playground", 7860, "0.0.0.0")
    assert command[1:3] == ["-m", "streamlit"]
