"""The test suite must not depend on the developer's authentication environment.

Compose passes a `.env` that enables authentication (for example the Keycloak dev settings) into the
container, and app.main reads the auth settings at import time. tests/conftest.py scrubs that environment;
these tests run a small, unrelated test file in a child pytest to prove it still does. (The file is an admin-endpoint
one on purpose: it fails under authentication but does not depend on NLP settings such as DISABLED_RECOGNIZERS.)
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

HOSTILE_ENVIRONMENTS = {
    "enabled and fully configured": {
        "AUTH_ENABLED": "true",
        "OIDC_ISSUER_URL": "http://keycloak:8080/realms/pii-shield",
        "OIDC_AUDIENCE": "pii-shield-api",
    },
    "malformed switch (would fail fast at import)": {"AUTH_ENABLED": "maybe"},
}

# Telemetry is irrelevant here. Blank it so the child does not wait at exit for a collector that is not there.
QUIET_TELEMETRY = {"OTEL_EXPORTER_OTLP_ENDPOINT": "", "APPLICATIONINSIGHTS_CONNECTION_STRING": ""}


@pytest.mark.parametrize("extra", HOSTILE_ENVIRONMENTS.values(), ids=HOSTILE_ENVIRONMENTS.keys())
def test_unrelated_tests_ignore_the_ambient_auth_environment(extra):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_app_registration.py"],
        cwd=REPO_ROOT,
        env={**os.environ, **QUIET_TELEMETRY, **extra},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-500:]
