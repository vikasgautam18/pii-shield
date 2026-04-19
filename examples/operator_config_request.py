"""Example: Configure per-entity anonymization strategies via /operator-config.

[WARN] DEPRECATED: The /operator-config endpoints have been retired.
Use per-app configuration via /apps/{app_id}/config instead.
See the indian_banking_tests.ipynb notebook for up-to-date examples.

This script is kept for historical reference only and will not work
against current versions of the PII Shield API.

Previously demonstrated:
  1. GET  /operator-config  — view current strategy per entity type
  2. POST /anonymize        — see default SHA3 hashing for DL numbers
  3. PUT  /operator-config  — switch IN_DRIVING_LICENSE to "replace"
  4. POST /anonymize        — see placeholder replacement instead of hash
"""

import requests

BASE_URL = "http://localhost:8000"
DL_TEXT = "The applicant holds driving license MH 14 2019 0012345."


def show_config():
    resp = requests.get(f"{BASE_URL}/operator-config")
    resp.raise_for_status()
    print("Current operator config:")
    for entity, strategy in resp.json()["config"].items():
        print(f"  {entity:30s} → {strategy}")
    print()


def anonymize(text: str):
    resp = requests.post(
        f"{BASE_URL}/anonymize",
        json={"text": text, "language": "en"},
    )
    resp.raise_for_status()
    return resp.json()


def set_strategy(entity_type: str, strategy: str):
    resp = requests.put(
        f"{BASE_URL}/operator-config",
        json={"entity_type": entity_type, "strategy": strategy},
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    print("=" * 60)
    print("  Operator Config — Anonymization Strategy Example")
    print("=" * 60)

    # 1. Show default config
    print("\n--- Step 1: View default configuration ---\n")
    show_config()

    # 2. Anonymize with default (hash)
    print("--- Step 2: Anonymize with default strategy (hash) ---\n")
    print(f"Input:  {DL_TEXT}")
    result = anonymize(DL_TEXT)
    print(f"Output: {result['anonymized_text']}\n")

    # 3. Switch to replace
    print("--- Step 3: Switch IN_DRIVING_LICENSE to 'replace' ---\n")
    set_strategy("IN_DRIVING_LICENSE", "replace")
    show_config()

    # 4. Anonymize again — now uses placeholder
    print("--- Step 4: Anonymize with 'replace' strategy ---\n")
    print(f"Input:  {DL_TEXT}")
    result = anonymize(DL_TEXT)
    print(f"Output: {result['anonymized_text']}\n")

    # 5. Restore default
    print("--- Step 5: Restore default (hash) ---\n")
    set_strategy("IN_DRIVING_LICENSE", "hash")
    show_config()
