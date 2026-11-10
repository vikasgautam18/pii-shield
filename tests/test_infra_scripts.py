"""Tests for infra/scripts/03-verify.sh, run against stand-ins for curl, az and terraform (no cloud is touched).

The stand-in curl plays an API: authentication off, on (401 without a token) or an older API without GET /me.
"""

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "infra" / "scripts" / "03-verify.sh"

FAKE_CURL = r"""
url=""; method=GET; token=0
while [ $# -gt 0 ]; do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    -o|-w|--max-time|-d) shift 2 ;;
    -H) case "$2" in "Authorization: Bearer "*) token=1 ;; esac; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
rest="${url#*://}"
case "$rest" in */*) path="/${rest#*/}" ;; *) path="/" ;; esac
authorised=0
if [ "$token" = 1 ] && [ "${FAKE_TOKEN_OK:-1}" = 1 ]; then authorised=1; fi
case "$path" in
  /health|/) code=200 ;;
  /me)
    case "${FAKE_AUTH:-off}" in
      on) if [ "$authorised" = 1 ]; then code=200; else code=401; fi ;;
      old) code=404 ;;
      *) code=200 ;;
    esac ;;
  *) if [ "${FAKE_AUTH:-off}" = on ] && [ "$authorised" != 1 ]; then code=401; else code=200; fi ;;
esac
if [ "$method" = POST ]; then
  if [ "$code" = 200 ]; then printf '{"anonymized_text": "stub"}\n%s' "$code"; else printf '{"detail": "no"}\n%s' "$code"; fi
else
  printf '%s' "$code"
fi
"""


def run_verify(tmp_path, auth, env=None):
    bin_dir, log = tmp_path / "bin", tmp_path / "curl.log"
    bin_dir.mkdir()
    stubs = {"curl": f'echo "$*" >> "{log}"\n{FAKE_CURL}', "az": "echo app.stub\n", "terraform": "exit 1\n"}
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(0o755)
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_AUTH": auth,
        "RG_NAME": "rg", "API_APP_NAME": "api", "PLAYGROUND_APP_NAME": "playground", "ADMIN_APP_NAME": "admin",
        **(env or {}),
    }
    result = subprocess.run([shutil.which("bash"), str(SCRIPT)], cwd=REPO_ROOT, env=full_env, capture_output=True,
                            encoding="utf-8", timeout=60)
    return result, (log.read_text().splitlines() if log.exists() else [])


class TestVerifyAuthentication:
    def test_authentication_is_detected_and_said_so(self, tmp_path):
        result, calls = run_verify(tmp_path, "on")
        out = result.stdout
        assert result.returncode == 0
        assert "Authentication is on (GET /me without a token answered HTTP 401)" in out
        assert "[PASS] GET /apps without a token is rejected (HTTP 401)" in out
        assert "[PASS] GET /audit-log without a token is rejected (HTTP 401)" in out
        assert "[SKIP] authenticated checks" in out and "[FAIL]" not in out
        assert "Results: 5 passed, 0 failed" in out
        assert any(c.endswith("/me") for c in calls), "the API was asked"

    def test_a_valid_token_is_accepted_without_setting_auth_enabled(self, tmp_path):
        result, _ = run_verify(tmp_path, "on", {"VERIFY_ACCESS_TOKEN": "good-token"})
        assert result.returncode == 0
        assert "[PASS] GET /me accepts the access token (HTTP 200)" in result.stdout
        assert "Results: 9 passed, 0 failed" in result.stdout

    def test_an_open_api_needs_no_token(self, tmp_path):
        result, _ = run_verify(tmp_path, "off")
        assert result.returncode == 0
        assert "Authentication is on" not in result.stdout
        assert "[PASS] GET /apps (HTTP 200)" in result.stdout
        assert "Results: 6 passed, 0 failed" in result.stdout

    def test_an_api_without_the_me_route_is_treated_as_open(self, tmp_path):
        result, _ = run_verify(tmp_path, "old")
        assert result.returncode == 0 and "Results: 6 passed, 0 failed" in result.stdout

    def test_an_explicit_true_does_not_ask_the_api(self, tmp_path):
        result, calls = run_verify(tmp_path, "on", {"AUTH_ENABLED": "true"})
        assert result.returncode == 0 and "Authentication is on" not in result.stdout
        assert not [c for c in calls if c.endswith("/me")], "without a token there is no GET /me at all"

    def test_an_explicit_false_is_respected_and_the_401s_explain_themselves(self, tmp_path):
        result, calls = run_verify(tmp_path, "on", {"AUTH_ENABLED": "false"})
        out = result.stdout
        assert result.returncode == 1
        assert "[FAIL] GET /apps (HTTP 401, expected 200)" in out
        assert "[FAIL] POST /anonymize_unique (HTTP 401)" in out
        assert "requires authentication" in out and "AUTH_ENABLED=true" in out
        assert not [c for c in calls if c.endswith("/me")]

    def test_a_rejected_token_gets_its_own_hint(self, tmp_path):
        result, _ = run_verify(tmp_path, "on", {"VERIFY_ACCESS_TOKEN": "stale", "FAKE_TOKEN_OK": "0"})
        assert result.returncode == 1
        assert "rejected the access token" in result.stdout
        assert "requires authentication" not in result.stdout
