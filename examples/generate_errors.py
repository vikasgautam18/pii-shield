"""Generate error traffic against the PII Shield API to populate the error dashboard.

Usage:
    1. Start the stack:   docker compose up --build
    2. Run this script:   python examples/generate_errors.py
    3. Open Grafana:      http://localhost:3000
    4. Navigate to:       Dashboards → PII Shield — Errors

The script triggers 404, 400, and validation errors across multiple
endpoints so every panel in the error dashboard has data to display.
Metrics export every 5 s, so allow a few seconds before refreshing.
"""

import sys
import time

import requests

BASE_URL = "http://localhost:8000"
MAX_RETRIES = 10
RETRY_DELAY = 3


def _wait_for_api() -> bool:
    """Block until the API is reachable or retries are exhausted."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(f"{BASE_URL}/supported-entities", timeout=5)
            if r.status_code == 200:
                print(f"[ok] API is up (attempt {attempt})\n")
                return True
        except requests.ConnectionError:
            pass
        print(f"  Waiting for API… (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(RETRY_DELAY)
    return False


def _fire(method: str, path: str, **kwargs) -> None:
    """Send a request and print a one-line summary."""
    url = f"{BASE_URL}{path}"
    resp = getattr(requests, method)(url, timeout=10, **kwargs)
    tag = "[ok]" if resp.status_code < 400 else "[x]"
    detail = ""
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        pass
    print(f"  {tag} {method.upper()} {path}  → {resp.status_code}  {detail}")


def generate_errors() -> None:
    print("── 404 Not Found errors ──────────────────────────────")
    _fire("get", "/apps/nonexistent-app-id")
    _fire("delete", "/apps/nonexistent-app-id")
    _fire("put", "/apps/nonexistent-app-id/config",
          json={"entity_type": "PERSON", "strategy": "hash"})
    _fire("post", "/deanonymize",
          json={"id": "00000000-0000-0000-0000-000000000000", "text": "hello"})
    _fire("post", "/anonymize_unique",
          json={"text": "Hello world", "language": "en"},
          headers={"X-App-Id": "nonexistent-app-id"})

    print("\n── 400 Validation errors ─────────────────────────────")
    # Register a real app so we can send a bad config update
    resp = requests.post(f"{BASE_URL}/apps",
                         json={"app_name": "error-test-app"}, timeout=10)
    if resp.status_code == 201:
        app_id = resp.json()["app_id"]
        print(f"  (registered temp app: {app_id})")
        _fire("put", f"/apps/{app_id}/config",
              json={"entity_type": "PERSON", "strategy": "invalid_strategy"})
        # Clean up
        requests.delete(f"{BASE_URL}/apps/{app_id}", timeout=10)
    else:
        print(f"  (could not register temp app: {resp.status_code})")

    print("\n── Generating a batch of errors for rate panels ──────")
    for i in range(10):
        requests.get(f"{BASE_URL}/apps/fake-{i}", timeout=10)
    print(f"  Sent 10 additional 404 requests")


if __name__ == "__main__":
    print("PII Shield — Error Traffic Generator")
    print("=" * 55)
    if not _wait_for_api():
        print("[x] API not reachable. Is the stack running? (docker compose up --build)")
        sys.exit(1)

    generate_errors()

    print("\n" + "=" * 55)
    print("Done! Open Grafana at http://localhost:3000")
    print("Dashboard: PII Shield — Errors")
    print("(metrics update every ~5 s)")
